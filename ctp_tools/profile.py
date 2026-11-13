"""按档案解析出**真正生效**的 CTP 配置(`.env` / `.env.guoxin` / `.env.simnow`)。

优先级:**指定的档案 > 项目 `.env` > 进程环境变量**。

为什么档案压过进程环境:操作员明确说了「用哪个档案」,而进程里残留的
`CTP_*` 变量往往是上一次运行留下的,让它赢反而会连到别的地方。

**冲突必须报出来,而且要分两类**
--------------------------------
`load_profile` 返回三类信息,调用方按「会不会连错机器」区别对待:

- `hard_conflicts`(**硬冲突,必须拒绝**):进程环境变量与项目 `.env` 不一致,
  而且**没有指定档案**。这时候「用哪个」只能猜:取证运行器如果走
  `bootstrap.load_env()`(`dotenv.load_dotenv(override=False)`,进程环境赢)
  连的是进程环境里那台机器,本模块算出来的是 `.env` 里那台:
  清场就可能平到**另一个账户**的仓位上。宁可停下让人确认。
- `notes`(**提示,不拦**):操作员**显式指定了档案**,档案里的值与
  `.env`/进程环境不一致。这是操作员的正常意图(「用这个档案」),
  不是错误——但仍要打印出来,免得拿错账户还不知道。

指定档案时,档案里**没有**的键仍按 `.env` 解析;此时 `.env` 与进程环境
不一致依旧是硬冲突(那两个来源谁对谁错依然只能猜)。

只用这个模块解析,不要在各个脚本里各写一份 `dotenv_values`——
解析方式(BOM、引号、行内注释)一旦和运行时不一致,
闸门看到的值和柜台拿到的值就会不同。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from ctp_tools.simnow import normalize_address

# 参与冲突检查的键:这些键决定「连到哪台机器、用哪个账号」。
GATED_PREFIXES = ("CTP_",)
GATED_KEYS = ("LIVE_VT_SYMBOL", "BROKER_TEST_ALLOWED_TD", "BROKER_TEST_ALLOWED_MD")

# 这些键的值是**凭据**,冲突/提示信息里绝不能出现原文(G7 敏感信息脱敏)。
# 冲突信息会被打印到 stderr,也可能被写进证据文件 —— 打印出去就等于泄露。
SECRET_KEYS = ("CTP_PASSWORD", "CTP_AUTH_CODE", "CTP_APP_ID", "CTP_AUTHCODE",
               "WEBHOOK_URL", "WEBHOOK_TELEGRAM_CHAT_ID")
# 账号算 PII:只留头尾各 2 位。
ACCOUNT_KEYS = ("CTP_USERNAME",)


def describe_env_value(key: str, value: str) -> str:
    """把环境变量的值变成**可以安全打印**的形式。

    这个函数是「打印配置值」的唯一出处(原先 `scripts/guoxin_oneclick.py` 里有
    一份,两份迟早漂移):凭据类键一律「(已隐藏)」,账号只留头尾各 2 位。
    冲突信息会被打印到 stderr,也可能被写进证据文件 —— 打印出去就等于泄露。
    """
    if key in SECRET_KEYS:
        return "(已隐藏)"
    if key in ACCOUNT_KEYS:
        if len(value) < 8:
            return "*" * len(value)
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return repr(value)


class Profile(NamedTuple):
    """一次档案解析的结果。"""

    env: dict[str, str]
    hard_conflicts: list[str]      # 必须拒绝:来源之间互相矛盾,猜不出来
    notes: list[str]               # 只提示:操作员的显式意图


def _read(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(path, encoding="utf-8-sig")
    return {k: v for k, v in values.items() if v is not None}


def _is_gated(key: str) -> bool:
    return key in GATED_KEYS or key.startswith(GATED_PREFIXES)


# 值是「地址集合」的键:顺序/空白/大小写不同不算冲突(一键流程会把排序后的全集
# 下推给子进程,而 .env 里是手写顺序 —— 按字符串比就会把它误判成硬冲突)。
SET_KEYS = ("BROKER_TEST_ALLOWED_TD", "BROKER_TEST_ALLOWED_MD")


def _same_value(key: str, a: str, b: str) -> bool:
    if key in SET_KEYS:
        def norm(raw: str) -> frozenset[str]:
            # 与 resolve_allowed 同一套归一化:tcp:// 前缀/路径不同的同一地址不算冲突
            return frozenset(normalize_address(x) for x in raw.replace(";", ",").split(",") if x.strip())
        return norm(a) == norm(b)
    return a == b


def load_profile(profile: str = "", root: Path | None = None) -> Profile:
    """返回 `Profile(env, hard_conflicts, notes)`。

    profile:档案文件名或路径(如 `.env.guoxin`);空 = 只用项目 `.env`。
    root:项目根目录(默认取 scripts/bootstrap 的 ROOT)。
    """
    if root is None:
        root = Path(__file__).resolve().parent.parent

    env: dict[str, str] = dict(os.environ)
    base = Path(root) / ".env"
    hard: list[str] = []
    notes: list[str] = []
    if base.exists():
        base_values = _read(base)
        for key, value in base_values.items():
            if _is_gated(key) and key in os.environ and not _same_value(key, os.environ[key], value):
                hard.append(f"{key}:进程环境里是 {describe_env_value(key, os.environ[key])},"
                            f"项目 .env 里是 {describe_env_value(key, value)}(取 .env 的值)")
        env.update(base_values)

    # 只在进程环境里、.env 里没有的「连哪台/用谁」键:会静默生效,同样要报。
    base_keys = set(_read(base)) if base.exists() else set()
    profile_keys: set[str] = set()
    if profile:
        _p = Path(profile) if Path(profile).is_absolute() else Path(root) / profile
        if _p.exists():
            profile_keys = set(_read(_p))
    for key, value in os.environ.items():
        if key in SET_KEYS and key not in base_keys and key not in profile_keys:
            notes.append(f"{key}:只在进程环境里(={describe_env_value(key, value)}),会作为白名单生效(仍与可信地址表求交集)")
        elif _is_gated(key) and key not in base_keys and key not in profile_keys:
            hard.append(f"{key}:只在进程环境里(={describe_env_value(key, value)}),"
                        "档案和 .env 里都没有——它会静默生效")

    if profile:
        path = Path(profile)
        if not path.is_absolute():
            path = Path(root) / path
        if not path.exists():
            raise FileNotFoundError(f"找不到档案 {path}")
        values = _read(path)
        for key, value in values.items():
            if not _is_gated(key):
                env[key] = value
                continue
            if key in env and not _same_value(key, env[key], value):
                # 档案覆盖 .env / 进程环境:操作员明说「用这个档案」,属正常意图。
                # 但「进程环境 vs .env」那一层如果是硬冲突,档案没定义这个键时
                # 依旧不能靠猜——那种情况在下面单独处理。
                notes.append(f"{key}:生效配置里是 {describe_env_value(key, env[key])},"
                             f"档案里是 {describe_env_value(key, value)}(取档案的值)")
            env[key] = value
        # 档案没有定义、但 .env 与进程环境打架的键:仍然无法判断,升级为硬冲突。
        for text in list(hard):
            key = text.split(":", 1)[0]
            if key in values:
                hard.remove(text)
                notes.append(f"{key}:进程环境与 .env 不一致,档案 {path.name} 已显式指定,取档案的值")
    return Profile(env=env, hard_conflicts=hard, notes=notes)
