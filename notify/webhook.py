"""Webhook 通知:非阻塞、带重试、去重、脱敏。任何失败都不能影响交易线程。

- send() 只把消息放进有界队列就返回,由后台线程发送;队列满时丢弃最旧的非 FATAL 消息,绝不阻塞调用方;
- 失败按 retry_delays 重试(默认 1/3/5 秒),最终失败只记录在 failed/last_error 里,不抛异常;
- 相同(级别, 标题, 内容)在 dedup_window 秒内只发一次(FATAL 不去重),避免故障时刷屏;
- 发送前把 secrets 里的敏感字符串(密码、授权码、token)替换成 ***,防止通过消息泄露;
- 三种格式:generic(JSON)、feishu(飞书机器人)、telegram(Bot API)。
"""
import json
import queue
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from urllib.parse import urlparse

import requests

LEVELS = {"INFO": 10, "WARN": 20, "ERROR": 30, "FATAL": 40}
_STOP = object()

# close() 最多等这么久。队列上限 200、单条最坏耗时约 34 秒,不设上限的话:
# 对端持续超时 + 队列塞满 → 排空预算接近 2 小时,交易进程在退出阶段被吊住。
# 上限的意义是"宁可丢一部分非 FATAL 告警,也不能让退出流程拖住"。
# FATAL 在 close() 时会被提到队首,优先送达。
MAX_DRAIN_TIMEOUT = 300.0


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:      # 太短的字符串替换会误伤正常文本
            text = text.replace(secret, "***")
    return text


def _redact_key(key, secrets: Iterable[str]):
    """dict 的**键**也要脱敏 —— 键名里带密码时,只脱敏值等于没脱敏。

    JSON 对象的键必须是字符串,非字符串键(如 int)由 json 负责转换,
    这里只保证"转换前的原值"不会把密钥带出去。
    两个不同的键脱敏后可能撞成同一个(键名里带密钥时才会发生),
    撞上的那个字段会丢失 —— 这是刻意的取舍:宁可丢字段,也不能把密钥发出去。
    """
    if isinstance(key, str):
        return redact(key, secrets)
    if key is None or isinstance(key, (int, float, bool)):
        # 数字/布尔键在 `json.dumps` 里会变成字符串("12345"、"true")——
        # 直接原样保留就等于把"键恰好等于密钥"的场景漏出去。
        # 所以按 JSON 形态先脱敏:命中就返回脱敏后的字符串(键类型变了,但不会泄密)。
        try:
            canonical = json.dumps(key)
        except Exception:
            canonical = str(key)
        safe = redact(canonical, secrets)
        return safe if safe != canonical else key
    try:
        return redact(str(key), secrets)
    except Exception:
        return "<key>"


def redact_obj(value, secrets: Iterable[str]):
    """对任意 JSON 结构做脱敏(dict / list / 标量)。

    不能先把结构序列化成 JSON 字符串再替换:序列化成字符串时引号、换行等
    已经被转义,`str.replace` 找不到原样的密钥 → 密钥留在 extra 里发出去;
    而如果密钥本身含 JSON 语法字符,替换还有可能把整条 JSON 弄坏。
    在**结构上**逐个字符串脱敏,两个问题都没有。

    三处容易漏的地方一起处理:
    - dict 的**键**也可能含密钥(_redact_key);
    - 非 JSON 标量(datetime、Decimal、自定义对象…)最终会被 `json.dumps(default=str)`
      转成字符串 —— 必须**先转成字符串再脱敏**,否则 `str(obj)` 里带的密码会原样发出去;
    - 数字、布尔、None 原样保留(转成字符串会改变语义,也可能引入无意义的信息泄漏)。
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {_redact_key(k, secrets): redact_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, secrets) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    # 其它类型最终会被 json.dumps(default=str) 转成字符串:先转再脱敏,顺序不能反。
    try:
        text = str(value)
    except Exception:       # 个别对象的 __str__ 会抛异常,不能因此把整条告警丢掉
        return f"<{type(value).__name__}>"
    return redact(text, secrets)


def _url_secrets(url: str) -> tuple[str, ...]:
    """URL 里可能含 token(如 Telegram 的 /bot<token>/sendMessage)。requests 的异常文本只含路径部分,
    所以完整 URL、路径、查询串和每个较长的路径段都要加入脱敏列表。"""
    if not url:
        return ()
    parsed = urlparse(url)
    parts = [url, parsed.path, parsed.query, *[seg for seg in parsed.path.split("/") if len(seg) >= 6]]
    return tuple(dict.fromkeys(p for p in parts if p and len(p) >= 4))


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        kind: str = "generic",
        *,
        chat_id: str = "",
        timeout: float = 5.0,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 5.0),
        dedup_window: float = 60.0,
        max_queue: int = 200,
        min_level: str = "INFO",
        secrets: Iterable[str] = (),
        host: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if kind not in ("generic", "feishu", "telegram"):
            raise ValueError(f"不支持的 webhook 类型: {kind}")
        if kind == "telegram" and not chat_id:
            raise ValueError("telegram 需要 chat_id")
        if min_level not in LEVELS:
            raise ValueError(f"未知级别: {min_level}")
        self.url, self.kind, self.chat_id = url, kind, chat_id
        self.timeout, self.retry_delays, self.dedup_window = timeout, retry_delays, dedup_window
        self.min_level = LEVELS[min_level]
        self.secrets = tuple(secrets)
        self._url_secrets = _url_secrets(url)
        self.host = host or socket.gethostname()
        self._sleep, self._clock = sleep, clock
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._recent: dict[tuple, float] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 计数器由发送线程与后台线程同时写(+=/-= 不是原子操作),单独用一把锁保护
        self._counter_lock = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = ""
        self.history: deque[dict] = deque(maxlen=50)   # 最近发送过的消息,便于排查
        self._inflight = 0          # 后台线程已经取走、但还没发完的消息数
        self._closed = threading.Event()   # 已请求停止:后台线程排空队列后自行退出

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def flush_timeout(self) -> float:
        """**单条**消息的最坏发送耗时(秒)。

        后台线程自己会重试:最坏情况 = (1 + 重试次数) × 单次超时 + 全部退避时间。
        这只是**一条**消息的上限 —— 队列里排着 N 条时,总耗时是 N 倍
        (消息是串行发送的)。要把队列排空,看 `drain_timeout`。
        """
        return self.timeout * (1 + len(self.retry_delays)) + sum(self.retry_delays) + 5.0

    @property
    def drain_timeout(self) -> float:
        """把**当前队列**里剩下的消息全部发完所需的上限(秒)。

        队列是串行消费的,所以总耗时 ≈ 单条最坏耗时 × 剩余条数。
        只按单条算,队列里有多条时 close() 会提前返回,"sent" 仍偏小 ——
        慢一点的对端就被误判成"没送达"。

        还要算上**正在发的那一条**:worker 已经把它从队列里取走了,`qsize()` 看不到它。
        漏掉它 → close() 少等一条 → 退出时最后那条 FATAL 告警随进程一起消失。
        """
        with self._counter_lock:
            inflight = self._inflight
        pending = max(self._queue.qsize() + inflight, 1)
        return self.flush_timeout * pending

    # ---------- 对外接口 ----------

    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool:
        """放入发送队列。返回是否被接受(被过滤、去重、禁用或丢弃时返回 False)。永不阻塞、永不抛异常。"""
        try:
            if not self.enabled or LEVELS.get(level, 0) < self.min_level:
                return False
            if self._closed.is_set():
                # 已经关闭:再收的消息只会躺在队列里没人发(后台线程已退出),
                # 返回 False 让调用方知道"没发出去",而不是假装收下了。
                self._bump("dropped", 1)
                return False
            title, message = redact(title, self.secrets), redact(message, self.secrets)
            if level != "FATAL" and self._is_duplicate((level, title, message)):
                self._bump("dropped", 1)
                return False
            # 脱敏在结构上做(见 redact_obj):键、非 JSON 标量的字符串形态都在那里被挡住。
            # 这里再做一次 JSON 往返,只是保证最后进队列的是纯 JSON 类型。
            safe_extra = json.loads(json.dumps(redact_obj(extra or {}, self.secrets),
                                              ensure_ascii=False, default=str))
            item = {"level": level, "title": title, "message": message,
                    "extra": safe_extra,
                    "time": datetime.now().isoformat(timespec="seconds")}
            self._ensure_started()
            return self._enqueue(item, level)
        except Exception as exc:  # 通知模块自身的任何错误都不能影响交易
            self.last_error = repr(exc)
            return False

    def close(self, drain_timeout: float = 10.0) -> None:
        """停止后台线程。尽力把队列里剩余的消息发完(最多 drain_timeout 秒)。

        `drain_timeout` 是**排空整个队列的总预算**,不是单条消息的预算 ——
        单条消息的最坏耗时看 `flush_timeout` 属性,整队列看 `drain_timeout` 属性。
        (参数名故意与属性同名同义,避免"传进来的是单条预算"这种误用。)
        停止哨兵是**追加**到队列末尾的,所以后台线程退出时队列必然已经排空;
        这里只是要保证有足够的时间等它排空。

        两点保护:
        - 实际等待时间封顶 `MAX_DRAIN_TIMEOUT`(300 秒):对端持续超时 + 队列塞满时,
          没有上限的排空会把交易进程吊在退出阶段两小时;
        - 关闭前把 FATAL 提到队首:退出时最重要的告警("还有持仓")不该排在几十条 INFO 后面。
        """
        thread = self._thread
        self._closed.set()
        if thread is None:
            return
        self._prioritize_fatal()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # 队列满,哨兵放不进去 —— 无妨:`_closed` 已经置位,
            # 后台线程排空队列后会自己退出(见 `_worker`)。
            pass
        budget = min(max(drain_timeout, 0.0), MAX_DRAIN_TIMEOUT)
        deadline = time.monotonic() + budget
        while thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(min(remaining, 0.2))

    def _prioritize_fatal(self) -> None:
        """把队列里的 FATAL 消息提到最前面(停止哨兵保持在最后)。"""
        with self._queue.mutex:
            items = list(self._queue.queue)
            if len(items) < 2:
                return
            stops = [i for i in items if i is _STOP]
            msgs = [i for i in items if i is not _STOP]
            # 按"是不是 FATAL"分成两堆(不能用 `not in`,dict 的相等比较会按值判等)
            fatal = [i for i in msgs if isinstance(i, dict) and i.get("level") == "FATAL"]
            others = [i for i in msgs
                      if not (isinstance(i, dict) and i.get("level") == "FATAL")]
            if not fatal:
                return
            self._queue.queue.clear()
            for item in fatal + others + stops:
                self._queue.queue.append(item)
            self._queue.not_empty.notify_all()

    # ---------- 内部 ----------

    def _is_duplicate(self, key: tuple) -> bool:
        now = self._clock()
        with self._lock:
            for k in [k for k, t in self._recent.items() if now - t > self.dedup_window]:
                del self._recent[k]
            if key in self._recent:
                return True
            self._recent[key] = now
            return False

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="webhook", daemon=True)
                self._thread.start()

    def _enqueue(self, item: dict, level: str) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            if level != "FATAL":
                self._bump("dropped", 1)
                return False
            return self._evict_for_fatal(item)

    def _bump(self, name: str, delta: int = 1) -> None:
        """计数器自增。发送线程与后台线程会同时写,必须加锁,否则数字会丢。"""
        with self._counter_lock:
            setattr(self, name, getattr(self, name) + delta)

    def _evict_for_fatal(self, item: dict) -> bool:
        """FATAL 不能丢:优先挤掉队列里最旧的非 FATAL 消息;队列里全是 FATAL 时才挤掉最旧的 FATAL。"""
        with self._queue.mutex:
            pending = self._queue.queue
            if len(pending) >= self._queue.maxsize:
                real = [i for i, m in enumerate(pending) if m is not _STOP]          # 绝不能挤掉停止哨兵,否则 worker 收不到停止信号
                victim = next((i for i in real if pending[i].get("level") != "FATAL"), real[0] if real else None)
                if victim is None:
                    self._bump("dropped", 1)
                    return False
                del pending[victim]
                self._bump("dropped", 1)
            pending.append(item)
            self._queue.unfinished_tasks += 1
            self._queue.not_empty.notify()
        return True

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                # 队列空了。如果已经请求停止,就在这里退出 ——
                # 不能只依赖队列里的停止哨兵:队列满的时候哨兵根本放不进去,
                # 后台线程会永远收不到停止信号,close() 只能干等到预算耗尽。
                if self._closed.is_set():
                    return
                continue
            if item is _STOP:
                return
            # 这条消息已经不在队列里了,但它还没发完 —— 排空预算必须把它算进去
            with self._counter_lock:
                self._inflight += 1
            try:
                self._deliver(item)
            finally:
                with self._counter_lock:
                    self._inflight -= 1

    def _payload(self, item: dict) -> dict:
        text = f"[{item['level']}] {item['title']}\n{item['message']}\n@{self.host} {item['time']}"
        if self.kind == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if self.kind == "telegram":
            return {"chat_id": self.chat_id, "text": text}
        return {**item, "host": self.host}

    def _body_error(self, resp) -> str | None:
        """检查响应体里有没有"接口层面的失败"。

        飞书机器人**失败时也返回 HTTP 200**,失败信息在 JSON 体里
        (成功 `{"code":0,...}`,失败如 `{"code":19001,"msg":"invalid token"}`;旧版是 `StatusCode`)。
        只看 HTTP 状态码会把"根本没送达"当成发送成功 —— 告警静默丢失,
        而运行手册的验收标准恰恰是"告警全部到达手机"。
        其它类型(generic / telegram)没有统一的响应体语义,只看状态码。
        """
        if self.kind != "feishu":
            return None
        try:
            data = resp.json()
        except Exception:
            # 飞书正常一定回 JSON。代理/网关返回 HTML 之类的 2xx,说明这条消息**没有**被飞书接收,
            # 按成功处理就是静默丢告警 —— 这里判为失败(宁可重试并留下 last_error)。
            return "飞书响应体不是 JSON,无法确认已送达"
        if not isinstance(data, dict):
            return f"飞书响应体不是 JSON 对象,无法确认已送达({type(data).__name__})"
        found_code = False
        for key in ("code", "StatusCode"):
            if key not in data:
                continue
            found_code = True
            value = data[key]
            # 对端可能把业务码写成字符串("0"),直接和 0 比会把成功当成失败;
            # 但反过来,None / 空串 / 非数字这类**无法确认**的值也不能当成成功
            # —— 宁可重试并留下 last_error,也不能把"没送达"记成"已送达"。
            if isinstance(value, bool):
                failed = value
            elif isinstance(value, (int, float)):
                failed = int(value) != 0
            elif isinstance(value, str):
                try:
                    failed = int(value.strip()) != 0
                except ValueError:
                    failed = True
            else:
                failed = True
            if failed:
                msg = data.get("msg") or data.get("StatusMessage") or ""
                return f"飞书接口返回失败 {key}={value} {msg}".strip()
        if not found_code:
            # 飞书一定会给 code(或旧版的 StatusCode)。一个没有业务码的 JSON 体
            # (例如代理网关回 {"error": "..."})无法证明消息真的被飞书接收 ——
            # 按成功处理就是静默丢告警,所以同样判为失败。
            return "飞书响应体里没有业务码,无法确认已送达"
        return None

    def _deliver(self, item: dict) -> None:
        payload = self._payload(item)
        attempts = 1 + len(self.retry_delays)
        for attempt in range(attempts):
            try:
                resp = requests.post(self.url, json=payload, timeout=self.timeout)
                if 200 <= resp.status_code < 300:
                    problem = self._body_error(resp)
                    if problem is None:
                        self._bump("sent", 1)
                        self.history.append(item)
                        return
                    # 响应体里带回了错误信息,可能含 token 之类的敏感内容,落库前脱敏
                    self.last_error = redact(problem, self.secrets + self._url_secrets)
                else:
                    self.last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                self.last_error = redact(repr(exc), self.secrets + self._url_secrets)   # 异常文本里可能带 token/URL 的路径
            if attempt < attempts - 1:
                self._sleep(self.retry_delays[attempt])
        self._bump("failed", 1)
