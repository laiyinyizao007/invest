"""状态页的鉴权:令牌文件 + 常量时间比较 + 滚动窗口限流。

取代上游 vnpy_webtrader 的 JWT 登录(上游 SECRET_KEY 写死为 "test"、登录是 POST)。这里没有登录端点:
令牌是一个随机字符串,存在不入库的文件里,请求带 `Authorization: Bearer <令牌>`。
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

MIN_TOKEN_LENGTH = 24


class TokenError(RuntimeError):
    pass


READ_RETRIES = 20            # 赢家刚创建文件、内容还没写完时,读取端短暂重试
READ_RETRY_PAUSE = 0.05


def _read_existing(path: Path) -> str:
    """读取已有的令牌文件。内容过短时先短暂重试(并发创建者可能还没写完),仍然过短才拒绝。

    所有 I/O 与解码错误都包成 TokenError,**消息里不含路径**,并 `from None` 切断异常链——
    否则未捕获的 traceback 会把本机绝对路径打到终端(G8 第 2 轮审查 #12)。
    """
    token = ""
    for attempt in range(READ_RETRIES):
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise TokenError("令牌文件无法读取(权限或编码问题)") from None
        if len(token) >= MIN_TOKEN_LENGTH:
            return token
        if attempt < READ_RETRIES - 1:
            time.sleep(READ_RETRY_PAUSE)
    raise TokenError(f"令牌文件内容过短(<{MIN_TOKEN_LENGTH} 字符),拒绝使用;删除该文件让服务重新生成")


def load_or_create_token(path: Path) -> tuple[str, bool]:
    """读取令牌;文件不存在则生成并写入。返回 (令牌, 是否新建)。令牌内容绝不打印。

    - 已有文件内容过短(<24 字符)视为不安全,拒绝使用而不是悄悄替换,避免覆盖运维者的意图。
    - 新建文件在 POSIX 上收紧为 0600;Windows 依赖用户目录的默认 ACL。
    - **并发首次启动是安全的**:先把令牌写进临时文件,再用 `os.link` 原子发布——目标已存在则失败(不覆盖),
      所以恰好一个创建者成功,其余的读取赢家的文件;而且 `path` 在任何时刻要么不存在、要么内容完整,
      不存在『文件已创建但还是空的』的窗口。文件系统不支持硬链接时 fail-closed(不发布空文件),提示手工创建。
    """
    path = Path(path)
    if path.exists():
        return _read_existing(path), False
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)                                            # 原子:目标已存在则 FileExistsError
            return token, True
        except FileExistsError:
            return _read_existing(path), False                           # 输了:读赢家的
        except OSError:
            # 不支持硬链接:fail-closed。曾经退回 O_EXCL 直接创建目标文件——那会先发布一个空文件,
            # 写入途中崩溃就让下次启动永久遇到『内容过短』(G8 第 3 轮 #5)。没有原子发布手段就不发布。
            raise TokenError("令牌文件无法原子创建:该文件系统不支持硬链接。请手工创建令牌文件"
                             "(至少 24 个随机字符,仅本人可读),再启动服务") from None
    except TokenError:
        raise
    except (OSError, UnicodeError):
        raise TokenError("令牌文件无法创建(权限、磁盘或路径问题)") from None
    finally:
        if tmp is not None:                                               # 从创建临时文件起就要清理:fsync 失败时它里面已经有令牌
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
