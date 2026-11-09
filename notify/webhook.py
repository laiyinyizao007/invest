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


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:      # 太短的字符串替换会误伤正常文本
            text = text.replace(secret, "***")
    return text


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
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = ""
        self.history: deque[dict] = deque(maxlen=50)   # 最近发送过的消息,便于排查

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # ---------- 对外接口 ----------

    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool:
        """放入发送队列。返回是否被接受(被过滤、去重、禁用或丢弃时返回 False)。永不阻塞、永不抛异常。"""
        try:
            if not self.enabled or LEVELS.get(level, 0) < self.min_level:
                return False
            title, message = redact(title, self.secrets), redact(message, self.secrets)
            if level != "FATAL" and self._is_duplicate((level, title, message)):
                self.dropped += 1
                return False
            item = {"level": level, "title": title, "message": message,
                    "extra": json.loads(redact(json.dumps(extra or {}, ensure_ascii=False, default=str), self.secrets)),
                    "time": datetime.now().isoformat(timespec="seconds")}
            self._ensure_started()
            return self._enqueue(item, level)
        except Exception as exc:  # 通知模块自身的任何错误都不能影响交易
            self.last_error = repr(exc)
            return False

    def close(self, flush_timeout: float = 10.0) -> None:
        """停止后台线程。尽力把队列里剩余的消息发完(最多 flush_timeout 秒)。"""
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        thread.join(flush_timeout)

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
                self.dropped += 1
                return False
            return self._evict_for_fatal(item)

    def _evict_for_fatal(self, item: dict) -> bool:
        """FATAL 不能丢:优先挤掉队列里最旧的非 FATAL 消息;队列里全是 FATAL 时才挤掉最旧的 FATAL。"""
        with self._queue.mutex:
            pending = self._queue.queue
            if len(pending) >= self._queue.maxsize:
                real = [i for i, m in enumerate(pending) if m is not _STOP]          # 绝不能挤掉停止哨兵,否则 worker 收不到停止信号
                victim = next((i for i in real if pending[i].get("level") != "FATAL"), real[0] if real else None)
                if victim is None:
                    self.dropped += 1
                    return False
                del pending[victim]
                self.dropped += 1
            pending.append(item)
            self._queue.unfinished_tasks += 1
            self._queue.not_empty.notify()
        return True

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            self._deliver(item)

    def _payload(self, item: dict) -> dict:
        text = f"[{item['level']}] {item['title']}\n{item['message']}\n@{self.host} {item['time']}"
        if self.kind == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if self.kind == "telegram":
            return {"chat_id": self.chat_id, "text": text}
        return {**item, "host": self.host}

    def _deliver(self, item: dict) -> None:
        payload = self._payload(item)
        attempts = 1 + len(self.retry_delays)
        for attempt in range(attempts):
            try:
                resp = requests.post(self.url, json=payload, timeout=self.timeout)
                if 200 <= resp.status_code < 300:
                    self.sent += 1
                    self.history.append(item)
                    return
                self.last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                self.last_error = redact(repr(exc), self.secrets + self._url_secrets)   # 异常文本里可能带 token/URL 的路径
            if attempt < attempts - 1:
                self._sleep(self.retry_delays[attempt])
        self.failed += 1
