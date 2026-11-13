"""状态页响应的唯一净化出口。

所有要返回给浏览器的数据,序列化前都必须经过 `sanitize()`。这里集中处理:
- 账号:只留头尾各 2 位,星号数固定(不泄露长度);
- 令牌、`CTP_*` 环境变量赋值、敏感键名的值;
- 本机绝对路径(Windows 盘符路径与常见 POSIX 目录);
- 已登记的明文秘密(经 `notify.webhook.redact`,脱敏的唯一出处);
- 非有限浮点(NaN / inf 不是合法 JSON)、过深嵌套、过长字符串与列表。

面板不读取 `.env`,所以不知道交易账号的明文;账号靠键名(`account_id` 等)识别并掩码,
文本里残留的账号由调用方把已知账号放进 `secrets` 处理。
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from notify.webhook import redact

MAX_DEPTH = 12
MAX_ITEMS = 200
MAX_STR = 2000

ACCOUNT_KEYS = frozenset({"account_id", "accountid", "account", "user_id", "userid", "username", "investor_id"})
# 『pass』的处理要小心:passed / bypass / compass / pass_rate 是普通键(曾因含 pass 被整个替换成 ***)。
# 规则:password / passwd / passphrase 在任何位置都算敏感;单独的 pass 只在它是整个键或位于键的末尾时才算(user-pass、db_pass)。
# 其余词(secret / token / api_key ...)含义明确,宁严勿松。
SENSITIVE_KEY = re.compile(
    r"(?i)pass(?:word|wd|phrase)|(?:^|[^a-z])pass$|secret|token|auth_?code|app_?id|api_?key|credential|private_?key")

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_CTP_ENV = re.compile(r"(?i)\bCTP_[A-Z0-9_]+\s*[=:]\s*\S+")
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|*?]*")
_POSIX_PATH = re.compile(r"(?<![\w.:/-])/(?:home|Users|root|var|opt|etc|tmp|mnt|srv|app)/[^\s\"'<>]*")


def mask_account(value: object) -> str:
    """`66170099` -> `66****99`。太短或非字符串一律 `****`。星号数固定,不透露账号长度。"""
    text = value if isinstance(value, str) else ""
    if len(text) < 5:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def scrub_text(text: str, *, secrets: Iterable[str] = (), limit: int = MAX_STR) -> str:
    """文本净化。保留换行与普通内容,只替换已知的泄露形态。"""
    out = redact(text, secrets)
    out = _BEARER.sub("Bearer ***", out)
    out = _CTP_ENV.sub("<env>", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)
    return clip(out, limit)


def sanitize(value: Any, *, secrets: Iterable[str] = (), max_items: int = MAX_ITEMS, max_str: int = MAX_STR, _depth: int = 0) -> Any:
    """递归净化任意可 JSON 化的结构,返回新对象(不修改入参)。"""
    secrets = tuple(secrets)
    if _depth > MAX_DEPTH:
        return "<too deep>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return scrub_text(value, secrets=secrets, limit=max_str)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = scrub_text(str(key), secrets=secrets, limit=200)
            if SENSITIVE_KEY.search(name):
                out[name] = "***"
            elif name.lower() in ACCOUNT_KEYS and isinstance(item, str):
                out[name] = mask_account(item)
            else:
                out[name] = sanitize(item, secrets=secrets, max_items=max_items, max_str=max_str, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:max_items]
        return [sanitize(i, secrets=secrets, max_items=max_items, max_str=max_str, _depth=_depth + 1) for i in items]
    return scrub_text(str(value), secrets=secrets, limit=max_str)
