"""外部心跳发送端(路线图「外部心跳看门狗」):交易机定期向外部端点推送脱敏心跳,外部连续 ≥3 分钟收不到就告警手机。

**只告警,不接管、不平仓**(明确不做「云端/另一台机器接管交易、远程平仓」):
本模块只发不收——不解析对端回包,没有任何按回包执行动作的入口。

约束:
- 间隔 ≤60 秒:外部 3 分钟阈值内至少能漏两次才告警,避免偶发丢包误报;
- 外部端点必须 https(本机回环地址除外,便于测试与本地中继);
- 载荷经 `notify.redact.redact_obj` 结构化脱敏(不依赖 requests);
- 任何异常(含状态对象无法序列化)都不抛给调用方,只累计连续失败次数;失败后至少隔 RETRY_SECONDS 再试;
- 不跟随重定向(否则构造时的 https/回环限制会被绕过);可被多线程调用。

接入交易进程的定时器属阶段 F-b;外部接收端(超时告警)不在本仓库内。只依赖标准库与 notify 包。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime
from urllib.parse import urlparse

from notify.redact import redact_obj

MAX_INTERVAL_SECONDS = 60
RETRY_SECONDS = 5.0             # 发送失败后至少隔这么久再试,避免高频调用形成重试风暴
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向:构造时校验的 https/回环限制只对初始地址有效,跟随重定向就等于绕过了它。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"心跳端点返回重定向({newurl}),拒绝跟随", headers, fp)


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _http_post(url: str, body: bytes, timeout: float = 5.0) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with _build_opener().open(req, timeout=timeout) as resp:         # 协议已在构造时限定为 https/回环 http,且不跟随重定向
        resp.read(0)                                                 # 不读、不解析回包


class HeartbeatSender:
    def __init__(self, url: str, *, interval_seconds: float = 30, secrets: Iterable[str] = (), host: str = "",
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] = datetime.now,
                 post: Callable[[str, bytes], object] | None = None):
        parsed = urlparse(url)
        loopback_http = parsed.scheme == "http" and parsed.hostname in _LOOPBACK
        if parsed.scheme != "https" and not loopback_http:
            raise ValueError("外部心跳端点必须是 https(本机回环地址除外)")
        if not 0 < interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValueError(f"心跳间隔必须在 (0, {MAX_INTERVAL_SECONDS}] 秒内:外部 3 分钟阈值前至少要能漏两次")
        self._url = url
        self._interval = float(interval_seconds)
        self._secrets = tuple(str(s) for s in secrets if s is not None and not isinstance(s, bool) and str(s))
        self._host = host
        self._clock, self._wall = clock, wall
        self._post = post or _http_post
        self._last_sent: float | None = None
        self._last_failed: float | None = None
        self._lock = threading.Lock()                 # 交易进程可能从多个线程调用
        self.consecutive_failures = 0

    def beat(self, status: dict) -> bool:
        """到点就发送一次;返回是否发送成功。间隔内、或失败后的退避期内调用直接返回 False(不发送)。

        任何异常(状态对象无法序列化、脱敏出错、网络失败)都不会抛给调用方,只累计连续失败次数。
        """
        with self._lock:
            now = self._clock()
            if self._last_sent is not None and now - self._last_sent < self._interval:
                return False
            if self._last_failed is not None and now - self._last_failed < RETRY_SECONDS:
                return False
            try:
                payload = redact_obj({"kind": "heartbeat", "ts": self._wall().isoformat(timespec="seconds"),
                                      "host": self._host, "status": status}, self._secrets)
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self._post(self._url, body)
            except Exception:                      # noqa: BLE001  心跳失败绝不能影响交易进程(含 RecursionError 等)
                self.consecutive_failures += 1
                self._last_failed = now
                return False
            self._last_sent = now
            self._last_failed = None
            self.consecutive_failures = 0
            return True
