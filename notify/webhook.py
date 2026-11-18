"""Webhook 通知:非阻塞、带重试、去重、脱敏。任何失败都不能影响交易线程。

- send() 只把消息放进有界队列就返回,由后台线程发送;队列满时丢弃最旧的非 FATAL 消息,绝不阻塞调用方;
- 失败按 retry_delays 重试(默认 1/3/5 秒),最终失败只记录在 failed/last_error 里,不抛异常;
- 相同(级别, 标题, 内容)在 dedup_window 秒内只发一次(FATAL 不去重),避免故障时刷屏;
- 发送前把 secrets 里的敏感字符串(密码、授权码、token)替换成 ***,防止通过消息泄露;
- 三种格式:generic(JSON)、feishu(飞书机器人)、telegram(Bot API)。
- 可选的**落盘待补发队列**(outbox_path):发送最终失败的消息不丢,写入 JSONL 文件,网络恢复后由后台线程补发(带「【补发】」前缀,
  保留原始发生时间);进程重启后继续补发;积压的 INFO 合并成一条摘要,避免恢复后刷屏。
  起因:夜盘真实切断网络 240 秒的演练里,断网期间产生的「行情假死」「网关连接断开」告警恢复后也没有补发(旧版只重试约十几秒就放弃)。
"""
import json
import os
import queue
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

LEVELS = {"INFO": 10, "WARN": 20, "ERROR": 30, "FATAL": 40}
_STOP = object()

# close() 最多等这么久。队列上限 200、单条最坏耗时约 34 秒,不设上限的话:
# 对端持续超时 + 队列塞满 → 排空预算接近 2 小时,交易进程在退出阶段被吊住。
# 上限的意义是"宁可丢一部分非 FATAL 告警,也不能让退出流程拖住"。
# FATAL 在 close() 时会被提到队首,优先送达。
MAX_DRAIN_TIMEOUT = 300.0

# 待补发队列:积压的 INFO 达到这个条数就合并成一条摘要(摘要里最多列这么多行)
DIGEST_MIN_ITEMS = 3
DIGEST_MAX_LINES = 20


from notify.redact import redact  # noqa: E402,F401  脱敏的唯一出处已移到 notify/redact.py(无 requests 依赖),这里重新导出保持兼容


from notify.redact import _redact_key, redact_obj  # noqa: E402,F401  结构化脱敏也移到 notify/redact.py(无 requests 依赖),这里重新导出保持兼容


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
        outbox_path: str | os.PathLike | None = None,
        outbox_max: int = 200,
        outbox_max_age: float = 86_400.0,
        redeliver_interval: float = 30.0,
        now: Callable[[], datetime] = datetime.now,
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
        # ---- 落盘待补发队列 ----
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self._outbox_max, self._outbox_max_age = max(1, int(outbox_max)), float(outbox_max_age)
        self._redeliver_interval = float(redeliver_interval)
        self._now = now
        self._outbox_lock = threading.Lock()          # 保护 _outbox 列表与文件
        self._redeliver_lock = threading.Lock()       # 同一时刻只允许一个补发过程(后台线程与手动调用不重复发)
        self.deferred = 0         # 发送最终失败而暂存进待补发队列的条数
        self.redelivered = 0      # 从待补发队列补发成功的条数
        self.expired = 0          # 超过 outbox_max_age 被放弃的条数
        self._down_until = float("-inf")      # 已知断网到这个时刻为止:新消息只试一次,不走完整重试
        self._last_redeliver = float("-inf")
        self._outbox: list[dict] = self._load_outbox()
        if self._outbox and self.enabled:
            self._ensure_started()            # 上次进程遗留的告警:启动就开始补发,不必等第一条新告警

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def pending_outbox(self) -> int:
        """待补发队列里还有多少条。"""
        with self._outbox_lock:
            return len(self._outbox)

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
        self._persist_pending_queue()         # 预算用完仍没发出去的,落盘留给下次进程补发,而不是随进程消失

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
                self._maybe_redeliver()
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

    def _try_post(self, payload: dict) -> bool:
        """发一次。成功返回 True;失败返回 False 并把(脱敏后的)原因记进 last_error。"""
        try:
            resp = requests.post(self.url, json=payload, timeout=self.timeout)
            if 200 <= resp.status_code < 300:
                problem = self._body_error(resp)
                if problem is None:
                    return True
                # 响应体里带回了错误信息,可能含 token 之类的敏感内容,落库前脱敏
                self.last_error = redact(problem, self.secrets + self._url_secrets)
            else:
                self.last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            self.last_error = redact(repr(exc), self.secrets + self._url_secrets)   # 异常文本里可能带 token/URL 的路径
        return False

    def _deliver(self, item: dict) -> None:
        payload = self._payload(item)
        # 已知断网(刚有一次投递或补发完整失败)时只试一次:否则队列里每条消息都要空等约 34 秒,
        # 断网越久积压越多,最终把队列塞满、反而丢消息。
        known_down = self._outbox_path is not None and self._clock() < self._down_until
        attempts = 1 if known_down else 1 + len(self.retry_delays)
        for attempt in range(attempts):
            if self._try_post(payload):
                self._bump("sent", 1)
                self.history.append(item)
                self._down_until = float("-inf")
                return
            if attempt < attempts - 1:
                self._sleep(self.retry_delays[attempt])
        if self._outbox_path is not None:
            self._down_until = self._clock() + self._redeliver_interval
        self._defer(item)

    # ---------- 落盘待补发队列 ----------

    def _load_outbox(self) -> list[dict]:
        if self._outbox_path is None or not self._outbox_path.exists():
            return []
        entries: list[dict] = []
        try:
            for line in self._outbox_path.read_text(encoding="utf-8").splitlines():
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and {"level", "title", "message", "time"} <= data.keys():
                    entries.append(data)
        except OSError as exc:
            self.last_error = repr(exc)
        return entries[-self._outbox_max:]

    def _save_outbox_locked(self) -> None:
        """整体改写(临时文件 + fsync + 原子替换):进程在任何时刻被杀,文件要么是旧版要么是新版,不会是半截。"""
        if self._outbox_path is None:
            return
        try:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._outbox_path.with_name(self._outbox_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                for entry in self._outbox:
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._outbox_path)
        except OSError as exc:
            self.last_error = repr(exc)      # 落盘失败只记录,绝不抛:通知模块不能影响交易

    def _defer(self, item: dict) -> None:
        """投递最终失败:有待补发队列就暂存,否则按旧行为计为 failed(放弃)。"""
        if self._outbox_path is None:
            self._bump("failed", 1)
            return
        with self._outbox_lock:
            self._outbox.append(item)
            while len(self._outbox) > self._outbox_max:
                # 挤掉最旧的非 FATAL;全是 FATAL 时才挤掉最旧的 FATAL
                victim = next((i for i, e in enumerate(self._outbox) if e.get("level") != "FATAL"), 0)
                del self._outbox[victim]
                self._bump("dropped", 1)
            self._save_outbox_locked()
        self._bump("deferred", 1)

    def _persist_pending_queue(self) -> None:
        """把内存队列里还没发出去的消息落盘(close() 预算耗尽、进程即将退出时调用)。"""
        if self._outbox_path is None:
            return
        leftovers = []
        while True:
            try:
                queued = self._queue.get_nowait()
            except queue.Empty:
                break
            if queued is not _STOP:
                leftovers.append(queued)
        for queued in leftovers:
            self._defer(queued)

    def _expire_locked(self) -> None:
        now = self._now()
        kept = []
        for entry in self._outbox:
            try:
                age = (now - datetime.fromisoformat(entry["time"])).total_seconds()
            except (ValueError, TypeError):
                age = 0.0                    # 时间戳无法解析:宁可补发也不静默丢弃
            if age > self._outbox_max_age:
                self._bump("expired", 1)
            else:
                kept.append(entry)
        if len(kept) != len(self._outbox):
            self._outbox = kept
            self._save_outbox_locked()

    def _remove_from_outbox(self, entries: list[dict]) -> None:
        gone = {id(e) for e in entries}
        with self._outbox_lock:
            self._outbox = [e for e in self._outbox if id(e) not in gone]
            self._save_outbox_locked()

    def _digest(self, infos: list[dict]) -> dict:
        shown = infos[:DIGEST_MAX_LINES]
        lines = [f"{e['time']} {e['title']}" for e in shown]
        if len(infos) > len(shown):
            lines.append(f"其余 {len(infos) - len(shown)} 条 INFO 告警已省略")
        return {"level": "INFO", "title": f"【补发摘要】{len(infos)} 条 INFO 告警", "message": "\n".join(lines),
                "extra": {}, "time": self._now().isoformat(timespec="seconds")}

    def process_outbox_once(self) -> int:
        """尝试补发一轮,返回本轮成功发出的**告警条数**(摘要里的每条也各算一条)。

        顺序:FATAL/ERROR/WARN 按发生先后逐条补发;INFO 少于 DIGEST_MIN_ITEMS 条逐条发,否则合并成一条摘要。
        遇到第一次失败立刻停止(网络显然还没恢复),不对每条都空等超时。"""
        if not self._redeliver_lock.acquire(blocking=False):
            return 0
        try:
            with self._outbox_lock:
                self._expire_locked()
                entries = list(self._outbox)
            if not entries:
                return 0
            urgent = [e for e in entries if e.get("level") != "INFO"]
            infos = [e for e in entries if e.get("level") == "INFO"]
            sent = 0
            plan: list[tuple[dict, list[dict]]] = [(dict(e, title="【补发】" + e["title"]), [e]) for e in urgent]
            if len(infos) >= DIGEST_MIN_ITEMS:
                plan.append((self._digest(infos), infos))
            else:
                plan += [(dict(e, title="【补发】" + e["title"]), [e]) for e in infos]
            for message, sources in plan:
                if not self._try_post(self._payload(message)):
                    self._down_until = self._clock() + self._redeliver_interval
                    return sent
                self._remove_from_outbox(sources)
                self._bump("sent", 1)
                self._bump("redelivered", len(sources))
                self.history.append(message)
                self._down_until = float("-inf")
                sent += len(sources)
            return sent
        finally:
            self._redeliver_lock.release()

    def _maybe_redeliver(self) -> None:
        """后台线程空闲时调用:待补发队列非空且距上次尝试已过 redeliver_interval 就补发一轮。"""
        if self._outbox_path is None or self._clock() - self._last_redeliver < self._redeliver_interval:
            return
        with self._outbox_lock:
            if not self._outbox:
                return
        self._last_redeliver = self._clock()
        try:
            self.process_outbox_once()
        except Exception as exc:     # noqa: BLE001  补发出错不能打断后台线程
            self.last_error = repr(exc)
