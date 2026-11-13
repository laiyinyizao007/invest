"""状态页的鉴权:令牌文件 + 常量时间比较 + 滚动窗口限流。

取代上游 vnpy_webtrader 的 JWT 登录(上游 SECRET_KEY 写死为 "test"、登录是 POST)。这里没有登录端点:
令牌是一个随机字符串,存在不入库的文件里,请求带 `Authorization: Bearer <令牌>`。
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
from collections import deque
from pathlib import Path

MIN_TOKEN_LENGTH = 24


class TokenError(RuntimeError):
    pass


def load_or_create_token(path: Path) -> tuple[str, bool]:
    """读取令牌;文件不存在则生成并写入。返回 (令牌, 是否新建)。令牌内容绝不打印。

    - 已有文件内容过短(<24 字符)视为不安全,拒绝使用而不是悄悄替换,避免覆盖运维者的意图。
    - 新建文件在 POSIX 上收紧为 0600;Windows 依赖用户目录的默认 ACL。
    """
    path = Path(path)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise TokenError(f"令牌文件内容过短(<{MIN_TOKEN_LENGTH} 字符),拒绝使用;删除该文件让服务重新生成")
        return token, False
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)       # x:不覆盖;并发启动时只有一个能创建成功
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def token_matches(expected: str, header: str | None) -> bool:
    """常量时间比较。缺失、格式不对一律 False;不论哪种失败,调用方返回同样的 401。"""
    given = parse_bearer(header)
    if given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class RateLimiter:
    """滚动 60 秒窗口内最多 N 次(全局,面板只有一个使用者)。时钟可注入。"""

    def __init__(self, limit_per_minute: int, clock) -> None:
        self.limit, self.clock = limit_per_minute, clock
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> float | None:
        """放行返回 None;超限返回建议等待的秒数(Retry-After)。"""
        now = self.clock()
        with self._lock:
            while self._hits and now - self._hits[0] >= 60.0:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return max(60.0 - (now - self._hits[0]), 1.0)
            self._hits.append(now)
            return None
