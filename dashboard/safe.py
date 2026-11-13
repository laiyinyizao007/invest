"""状态页响应的唯一净化出口。

所有要返回给浏览器的数据,序列化前都必须经过 `sanitize()`。这里集中处理:
- 账号:只留头尾各 2 位,星号数固定(不泄露长度);任何类型的值(含数字)都掩码;
- 令牌、Authorization / Cookie 头、`password=` 之类的赋值、`CTP_*` 环境变量;
- 敏感键名的值(键名先做 Unicode 规范化与去分隔符,所以 apiKey / api_key / x-api-key / 全角写法落到同一形态);
- 本机绝对路径(Windows 盘符、UNC、`\\?\` 扩展路径、URL 编码的盘符路径、常见 POSIX 目录);
- 已登记的明文秘密(经 `notify.redact.redact`,脱敏的唯一出处);
- 非有限浮点(NaN / inf 不是合法 JSON)、过深嵌套、过长字符串与列表。

面板不读取 `.env`,所以不知道交易账号的明文;账号靠键名识别并掩码,文本里残留的账号由调用方把已知账号放进 `secrets` 处理。
**这是兜底,不是主要防线**:主要防线是 collectors 只挑选需要的字段、不原样转发任何文件内容。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

from notify.redact import redact

MAX_DEPTH = 12
MAX_ITEMS = 200
MAX_STR = 2000


def _norm_key(key: str) -> str:
    """键名规范化:NFKC(全角→半角)→ 小写 → 去掉 - _ . 空格。apiKey / api_key / x-api-key / API KEY 都变成 apikey / xapikey。"""
    return re.sub(r"[-_.\s]+", "", unicodedata.normalize("NFKC", key).lower())


# 规范化之后的键名里,出现这些词即视为账号(值无论什么类型都掩码)。
_ACCOUNT_WORDS = ("accountid", "accountno", "accountnumber", "brokeraccount", "clientaccount", "investorid", "loginid", "userid", "username")
_ACCOUNT_EXACT = frozenset({"account"})        # 泛用词 user / login 不放进来:契约里 WhoAmI.user 是用户名(viewer-a),不是交易账号

# 规范化之后的键名里,出现这些词即视为凭据(值整体替换成 ***)。
# 『pass』要小心:passed / bypass / compass / passrate 是普通键(曾因含 pass 被整个替换成 ***)。
# 规则:password / passwd / pwd / passphrase 在任何位置都算;单独的 pass 只在它是整个键或位于键末尾时才算(userpass、dbpass)。
_SECRET_WORDS = ("password", "passwd", "passphrase", "pwd", "secret", "token", "authcode", "appid", "apikey", "credential", "privatekey",
                 "authorization", "cookie", "sessionid", "session")
# 单独的 pass:必须在**规范化之前**判断,因为去掉分隔符后 user-pass 与 bypass 无法区分。
# 只有『整个键就是 pass』或『以分隔符加 pass 结尾』(user-pass、db_pass、DB.PASS)才算;bypass / compass 不算。
_LONE_PASS = re.compile(r"(?i)(?:^|[-_.\s])pass$")


def is_secret_key(key: str) -> bool:
    k = _norm_key(key)
    return any(w in k for w in _SECRET_WORDS) or bool(_LONE_PASS.search(unicodedata.normalize("NFKC", key)))


def is_account_key(key: str) -> bool:
    k = _norm_key(key)
    return k in _ACCOUNT_EXACT or any(w in k for w in _ACCOUNT_WORDS)


# ---- 文本里的泄露形态 ----
# 敏感词 = / : 值。值取到空白、分号或逗号为止;Authorization 要连 scheme 带凭据一起吞(Basic xxx / Bearer xxx)。
_ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth[_-]?code|app[_-]?id)\b(\s*[=:]\s*)[^\s;,&]+")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization)\b(\s*[=:]\s*)(?:(?:basic|bearer|digest|token)\s+)?[^\s;,]+")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)\b(\s*[=:]\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_CTP_ENV = re.compile(r"(?i)\bCTP_[A-Z0-9_]+\s*[=:]\s*\S+")

_UNC = re.compile(r"\\\\[?.]?\\?[^\s\"'<>|*?]+")                                                    # \\server\share  \\?\C:\...  \\.\pipe\x
_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?]*")            # 盘符前不能是字母数字,否则 https://x 里的 s:// 会被当成盘符
_ENC_WIN_PATH = re.compile(r"(?i)\b[a-z]%3a(?:%5c|%2f)[^\s\"'<>]*")                                  # C%3A%5CUsers%5C...
_ENC_POSIX_PATH = re.compile(r"(?i)(?<![\w.:/%-])%2f(?:home|users|root|var|opt|etc|tmp|mnt|srv|app|private|workspace|volumes)%2f[^\s\"'<>]*")
_PCT_TOKEN = re.compile(r"[^\s\"'<>]*%[0-9A-Fa-f]{2}[^\s\"'<>]*")                                   # 含百分号编码的片段
_DECODE_ROUNDS = 2                                                                                 # 单层与双层编码(%5C、%255C);更深的嵌套不在威胁模型内
_POSIX_PATH = re.compile(r"(?<![\w.:/%-])/(?:home|Users|root|var|opt|etc|tmp|mnt|srv|app|usr|private|workspace|Volumes|Library|proc|media|snap)/[^\s\"'<>]*")


def mask_account(value: object) -> str:
    """`66170099` -> `66****99`。任何类型的值先转成字符串;太短或为空一律 `****`。星号数固定,不透露账号长度。"""
    text = "" if value is None or isinstance(value, bool) else str(value)
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    if len(text) < 5:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _decoded_has_path(token: str) -> bool:
    """对**检测副本**做有界的百分号解码,看解码后是否出现路径。替换作用于原文,所以没有路径的编码文本(%E4%B8%AD、%20)不受影响。"""
    current = token
    for _ in range(_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
        if _UNC.search(current) or _WIN_PATH.search(current) or _POSIX_PATH.search(current):
            return True
    return False


def _scrub_encoded_token(match: re.Match) -> str:
    return "<path>" if _decoded_has_path(match.group(0)) else match.group(0)


def scrub_text(text: str, *, secrets: Iterable[str] = (), limit: int = MAX_STR) -> str:
    """文本净化。保留换行与普通内容,只替换已知的泄露形态。"""
    out = redact(text, secrets)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _COOKIE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _BEARER.sub("Bearer ***", out)
    out = _CTP_ENV.sub("<env>", out)
    out = _UNC.sub("<path>", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _ENC_WIN_PATH.sub("<path>", out)
    out = _ENC_POSIX_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)
    out = _PCT_TOKEN.sub(_scrub_encoded_token, out)
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
            if is_secret_key(name):
                out[name] = "***"
            elif is_account_key(name) and isinstance(item, (str, int, float)) and not isinstance(item, bool):
                out[name] = mask_account(item)                 # 标量(含数字)都掩码;容器(如 live.data.account 资金对象)继续递归
            else:
                out[name] = sanitize(item, secrets=secrets, max_items=max_items, max_str=max_str, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:max_items]
        return [sanitize(i, secrets=secrets, max_items=max_items, max_str=max_str, _depth=_depth + 1) for i in items]
    return scrub_text(str(value), secrets=secrets, limit=max_str)
