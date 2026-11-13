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
# 赋值形态:`password=x`、`password: x`,以及**带引号的 JSON 键**(`"password": "x"`)。
# 引号形态曾被漏掉:日志或候选代码里的 JSON 字面量会把秘密原样带出去。
_ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth[_-]?code|app[_-]?id)\b"
                     r"(\s*[\"']?\s*[=:]\s*[\"']?)[^\s;,\"'&]+")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization)\b(\s*[=:]\s*)(?:(?:basic|bearer|digest|token)\s+)?[^\s;,]+")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)\b(\s*[=:]\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_CTP_ENV = re.compile(r"(?i)\bCTP_[A-Z0-9_]+\s*[=:]\s*\S+")

_UNC = re.compile(r"\\\\[?.]?\\?[^\s\"'<>|*?]+")                                                    # \\server\share  \\?\C:\...  \\.\pipe\x
# 盘符后接反斜杠,或接**单个**正斜杠:`https://x` 里的 `s:/` 后面还有一个 `/`,所以不会被当成盘符;而 `fooC:\Users` 这种紧贴字母的写法仍会命中
# 反斜杠写法:目录名里允许单个空格(`C:\Users\Alice Smith\x.txt`——空格后面必须还有反斜杠才算目录的一部分,所以普通句子不会被吞掉);
# 正斜杠写法:盘符前不能是字母数字/斜杠/点(挡住 URL 路径里的 `a:/b`),而反斜杠写法不加这个限制(`fooC:\Users` 仍要命中)。
# 零宽/方向控制字符:能拆开路径结构(C:\u200bUsers)。**不删除它们**(那会改动与路径无关的原文),
# 而是让路径规则容忍它们——路径被整段替换时这些字符随之消失,路径之外的原文一个字节不动。
_WIN_PATH = re.compile(r"[A-Za-z](?:[\u200b-\u200f\u2060\ufeff\u202a-\u202e]*)?:(?:[\u200b-\u200f\u2060\ufeff\u202a-\u202e]*)?\\(?:[^\\\s\"'<>|*?]+(?: [^\\\s\"'<>|*?]+)*\\)*[^\s\"'<>|*?]*|(?:(?<![\w/.%-])|(?<=file:///))[A-Za-z]:/(?!/)[^\s\"'<>|*?]*")
# 引号包围的路径可以含空格(C:\Users\Alice Smith\...):整段替换,否则只遮住空格之前的部分
_QUOTED_PATH = re.compile(r"(?P<q>[\"'])(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|root|var|opt|etc|tmp|mnt|srv|app|usr|private|workspace|Volumes|Library)/)[^\"'<>|*?\r\n]*(?P=q)")
# 全角冒号/斜杠写成的盘符路径(C：＼Users＼x):只在确实含全角路径字符时才替换,半角版本由 _WIN_PATH 处理
_FULLWIDTH_PATH = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ][:：][\\/＼／][^\s\"'<>|*?]*")
_FULLWIDTH_MARKS = "：＼／"
_PCT_U = re.compile(r"(?i)%u([0-9a-f]{4})")                                                       # 非标准的 %uXXXX 写法,只用于检测副本
_ENC_WIN_PATH = re.compile(r"(?i)\b[a-z]%3a(?:%5c|%2f)[^\s\"'<>]*")                                  # C%3A%5CUsers%5C...
_ENC_POSIX_PATH = re.compile(r"(?i)(?<![\w.:/%-])%2f(?:home|users|root|var|opt|etc|tmp|mnt|srv|app|private|workspace|volumes)%2f[^\s\"'<>]*")
_PCT_TOKEN = re.compile(r"[^\s\"'<>]*%(?:[0-9A-Fa-f]{2}|[uU][0-9A-Fa-f]{4})[^\s\"'<>]*")                                   # 含百分号编码的片段
_DECODE_ROUNDS = 8                                                                                 # 解码到稳定为止,最多 8 轮(%5C、%255C、%25255C…);更深的嵌套不在威胁模型内
_POSIX_PATH = re.compile(r"(?<![\w.:/%-])/(?:home|Users|root|var|opt|etc|tmp|mnt|srv|app|usr|private|workspace|Volumes|Library|proc|media|snap)/[^\s\"'<>]*")
# 没有盘符的 Windows 根路径(`\Users\Alice\...`):盘符路径有规则,这个形态曾被漏掉
_WIN_ROOTED_PATH = re.compile(r"(?<![\w.:/\\])\\(?:Users|Windows|ProgramData|Program Files|Applications|home|tmp|opt|srv|usr|var|etc|Volumes|Library|private)\\?[^\s\"'<>|*?]*")


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
        current = _PCT_U.sub(lambda m: chr(int(m.group(1), 16)), current)   # %u 写法:每轮都要再试一次
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
        if _looks_like_path(current):
            return True
    current = _PCT_U.sub(lambda m: chr(int(m.group(1), 16)), current)
    return _looks_like_path(current)


def _looks_like_path(text: str) -> bool:
    """文本里有没有路径。**规则要齐全**:漏掉一条就等于留下一个绕过
    (曾漏:没有盘符的根路径、全角写法)。"""
    if _UNC.search(text) or _WIN_PATH.search(text) or _WIN_ROOTED_PATH.search(text) or _POSIX_PATH.search(text):
        return True
    full = _FULLWIDTH_PATH.search(text)
    if full and (any(ch in full.group(0) for ch in _FULLWIDTH_MARKS) or not full.group(0)[0].isascii()):
        return True                       # 全角写法:『Ｃ：＼Users＼x』。半角的 s://(https://)不算
    return False


def _scrub_fullwidth(match: re.Match) -> str:
    text = match.group(0)
    # 盘符字母本身是全角(Ｃ)也算——必须按字符判断,不能写成 `in "Ａ-Ｚ"`(那只是字符串成员测试,不是范围)
    return "<path>" if any(ch in text for ch in _FULLWIDTH_MARKS) or not text[0].isascii() else text


def _scrub_encoded_token(match: re.Match) -> str:
    return "<path>" if _decoded_has_path(match.group(0)) else match.group(0)


def scrub_text(text: str, *, secrets: Iterable[str] = (), limit: int = MAX_STR) -> str:
    """文本净化。保留换行与普通内容,只替换已知的泄露形态。

    **输入先截断**:否则净化在输出末尾才截断,输入多大就要处理多大——一个几百 KB 的百分号串
    会让 8 轮解码做很久(实测 600 KB 要 300 毫秒)。被截掉的部分本来也不会显示,不会泄露。
    """
    # **先脱敏再截断**:反过来会让跨过截断点的秘密只露出前缀(实测泄露 9 个字符)。
    # redact 是线性替换,开销可忽略;真正贵的是下面的多轮解码,那才是需要先截断的原因。
    out = redact(text, secrets)
    if len(out) > limit + 1:                 # 多留一个字符:末尾的 clip 仍能加上省略号
        out = out[: limit + 1]
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _COOKIE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _BEARER.sub("Bearer ***", out)
    out = _CTP_ENV.sub("<env>", out)
    out = _QUOTED_PATH.sub("<path>", out)
    out = _FULLWIDTH_PATH.sub(_scrub_fullwidth, out)
    out = _UNC.sub("<path>", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _ENC_WIN_PATH.sub("<path>", out)
    out = _ENC_POSIX_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)
    out = _WIN_ROOTED_PATH.sub("<path>", out)
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
