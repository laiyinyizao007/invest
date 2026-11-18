"""外部心跳发送端(路线图「外部心跳看门狗」):交易机定期向外部端点推送脱敏心跳,外部连续 ≥3 分钟收不到就告警手机。

**只告警,不接管、不平仓**(明确不做「云端/另一台机器接管交易、远程平仓」):
本模块只发不收——不解析对端回包,没有任何按回包执行动作的入口。

约束:
- 间隔 ≤60 秒:外部 3 分钟阈值内至少能漏两次才告警,避免偶发丢包误报;
- 外部端点必须 https(本机回环地址除外,便于测试与本地中继);
- 载荷经 `notify.redact.redact_obj` 结构化脱敏(不依赖 requests);
- 发送失败不抛异常、不阻塞调用方,只累计连续失败次数(失败不占用间隔,下次调用立即重试)。

接入交易进程的定时器属阶段 F-b;外部接收端(超时告警)不在本仓库内。只依赖标准库与 notify 包。
"""
from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime
from urllib.parse import urlparse

from notify.redact import redact_obj

MAX_INTERVAL_SECONDS = 60
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _http_post(url: str, body: bytes, timeout: float = 5.0) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:     # noqa: S310  (协议已在构造时限定为 https/回环 http)
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
        self._secrets = tuple(s for s in secrets if s)
        self._host = host
        self._clock, self._wall = clock, wall
        self._post = post or _http_post
        self._last_sent: float | None = None
        self.consecutive_failures = 0

    def beat(self, status: dict) -> bool:
        """到点就发送一次;返回是否发送成功。间隔内调用直接返回 False(不发送)。"""
        now = self._clock()
        if self._last_sent is not None and now - self._last_sent < self._interval:
            return False
        payload = redact_obj({"kind": "heartbeat", "ts": self._wall().isoformat(timespec="seconds"),
                              "host": self._host, "status": status}, self._secrets)
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self._post(self._url, body)
        except Exception:                      # noqa: BLE001  心跳失败绝不能影响交易进程
            self.consecutive_failures += 1
            return False
        self._last_sent = now
        self.consecutive_failures = 0
        return True
