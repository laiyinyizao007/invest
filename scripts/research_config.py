"""研究沙盒配置:所有可调参数集中在 `config/research_sandbox.json`。

设计原则:
- 文件缺失 -> 用 DEFAULTS;文件存在但内容非法(未知键、类型错、越界)-> 抛 ConfigError 拒绝启动,绝不悄悄回退成默认值。
- 安全相关的数值有硬边界(例如 feedback.limit 不得为 0 或超过 10、gateway.host 不得是 0.0.0.0),
  配置能调参,但不能把边界调没。
- screening 的三个列表(extra_forbidden_modules / extra_forbidden_calls / allowed_modules)
  只能**追加**:禁用项只能加,白名单也只能加,不能删减内置的禁用清单(内置清单在 research_sandbox.py)。
- 配置文件不含任何密钥;令牌只记录**文件路径**,令牌本身存放在该路径下(已被 .gitignore 的 data/ 覆盖)。
决议见 docs/adr/20261112-research-sandbox-config-and-approval.md。
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# 项目根:**不 import bootstrap**。
# bootstrap 会 `from vnpy.trader.setting import SETTINGS` 并 `load_dotenv(.env)`——
# 那是**交易栈**:研究侧一旦 import 它,就把 vnpy 与 .env 里的交易凭据拖了进来
# (研究进程的内存里就有了 CTP 账号),与「研究侧不碰 .env 交易段」的边界声明冲突。
# 这里按本文件位置推断代码根,并把代码根放进 sys.path(risk/strategies 才可导入)。
CODE_ROOT = Path(__file__).resolve().parents[1]
# 数据根:默认与代码根相同;可用 INVEST_ROOT 指到网关实际挂载的那棵目录树。
ROOT = Path(os.environ.get("INVEST_ROOT") or CODE_ROOT).resolve()
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

CONFIG_PATH = CODE_ROOT / "config" / "research_sandbox.json"

DEFAULTS: dict[str, dict[str, Any]] = {
    "iteration": {"max_iterations": 5, "window_hours": 24},
    "feedback": {"limit": 3},
    "gate": {"random_runs": 200, "timeout_seconds": 3600},
    "backtest": {"timeout_seconds": 600},
    "grid": {"min_round_trips": 10, "max_cells": 343, "timeout_seconds": 1800},
    "candidates": {"max_source_bytes": 65536, "name_pattern": r"^[a-z0-9_]{1,40}\.py$"},
    "gateway": {"host": "127.0.0.1", "port": 8765, "token_file": "data/research-control/gateway.token",
                "rate_limit_per_minute": 30, "max_body_bytes": 131072},
    "paths": {"pending_dir": "pending_strategies", "results_dir": "data/research",
              "approvals_file": "data/research-control/approvals.json",
              "collected_file": "data/research-control/collected.json",
              "approved_dir": "approved",
              "auto_approvals_file": "data/research-control/auto_approvals.json",
              "attempts_file": "data/research/attempts.jsonl"},
    # 研究沙盒自动批准(Issue #67)。**默认关**;由人在本机改这个文件打开,模型改不了它。
    # 打开后,通过静态筛查的候选由服务端写入**独立账本** auto_approvals.json(via=auto-research)。
    # 该账本**只被研究沙盒读**;运行侧/装载门/实盘准入仍只认人工账 approvals.json。
    "auto_approve": {"enabled": False},
    "screening": {"extra_forbidden_modules": [], "extra_forbidden_calls": [], "allowed_modules": []},
}

# (最小, 最大);bool 不算整数
INT_BOUNDS = {
    ("iteration", "max_iterations"): (1, 50),
    ("iteration", "window_hours"): (1, 720),
    ("feedback", "limit"): (1, 10),
    ("gate", "random_runs"): (20, 2000),
    ("gate", "timeout_seconds"): (1, 86_400),
    ("backtest", "timeout_seconds"): (1, 86_400),
    ("grid", "min_round_trips"): (1, 1_000),
    ("grid", "max_cells"): (27, 1_000),
    ("grid", "timeout_seconds"): (1, 86_400),
    ("candidates", "max_source_bytes"): (1, 1_048_576),
    ("gateway", "port"): (1, 65_535),
    ("gateway", "rate_limit_per_minute"): (1, 6_000),
    ("gateway", "max_body_bytes"): (1, 4_194_304),
}
FORBIDDEN_HOSTS = {"0.0.0.0", "::", "", "*", "0", "::0"}
# 候选策略目录不得指向这些(正式策略、批准目录),凭据文件也不得被借用为令牌文件
RESERVED_DIRS = {"strategies", "approved", "risk", "trading", "scripts", "tests", "config", ".git"}
RESERVED_FILES = {".env", ".env.simnow.bak"}
# 批准记录与网关令牌**固定**放在这个目录下(第 7 轮 codex 的 F8):
# 之前只按「文件名不在黑名单里」判断,于是 `approvals_file="data/research-control/.env.simnow"`
# 能借走凭据文件、`token_file="data/creds.env"` 能把凭据内容当成网关令牌。
# 改成**结构约束**:目录固定、文件名固定,没有借名的余地。
CONTROL_DIR = ("data", "research-control")
APPROVALS_FILE_NAME = "approvals.json"
# 自动批准的**独立**账本:与人工账同目录、固定文件名。运行侧不读它(见 tests/test_auto_approval_isolation.py)。
AUTO_APPROVALS_FILE_NAME = "auto_approvals.json"
# 每次研究尝试的追加记录(含被拒/失败的)。固定在 results 目录下。
ATTEMPTS_FILE_NAME = "attempts.jsonl"
# 「已收菜」记录:收菜脚本写、运行时装载读。证明这段字节**经过了收菜那道关口**
# (人工 cp 拷进 approved/ 就绕不过它)。
COLLECTED_FILE_NAME = "collected.json"
TOKEN_FILE_NAME = "gateway.token"
# 批准目录:固定在项目根下的 `approved/`。收菜往这里写,运行时装载只从这里读。
APPROVED_DIR_NAME = "approved"
SAFE_NAME_PATTERNS_MUST_REJECT = ("../x.py", "a/b.py", "a\\b.py", ".py", "x.txt")


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict, where: str = "") -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in base:
            raise ConfigError(f"未知配置项: {where}{key}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"配置项 {where}{key} 必须是对象")
            result[key] = _merge(base[key], value, f"{where}{key}.")
        else:
            result[key] = value
    return result


def _relative_inside(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} 必须是非空字符串")
    path = Path(value)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ConfigError(f"{name} 必须是项目内的相对路径: {value!r}")
    return path


def _is_unspecified_address(host: str) -> bool:
    """`0.0.0.0` / `::` 的任意等价写法(`::0`、`0:0:0:0:0:0:0:0`、`[::]`)都是「所有网卡」
    (第 16 轮 A 的 L3:原先只枚举了字面量)。"""
    import ipaddress
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_unspecified
    except ValueError:
        return False


def _validate(cfg: dict) -> None:
    for (section, key), (low, high) in INT_BOUNDS.items():
        value = cfg[section][key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{section}.{key} 必须是整数")
        if not low <= value <= high:
            raise ConfigError(f"{section}.{key}={value} 超出允许范围 [{low}, {high}]")

    host = cfg["gateway"]["host"]
    if not isinstance(host, str) or host.strip() in FORBIDDEN_HOSTS or _is_unspecified_address(host):
        raise ConfigError(f"gateway.host={host!r} 不允许:网关不得监听所有网卡")

    pattern = cfg["candidates"]["name_pattern"]
    try:
        compiled = re.compile(pattern)
    except (re.error, TypeError) as exc:
        raise ConfigError(f"candidates.name_pattern 不是合法正则: {exc}") from exc
    for bad in SAFE_NAME_PATTERNS_MUST_REJECT:                # 文件名规则不得放开到允许路径分隔符/空名/非 .py
        if compiled.fullmatch(bad):
            raise ConfigError(f"candidates.name_pattern 过宽:不应匹配 {bad!r}")

    # 大小写不敏感:`Strategies` 在不分大小写的文件系统上就是真正的 `strategies/`
    # (第 6 轮 codex 的 M3:这条规则原来只加在 pending_dir 上,token_file / results_dir /
    #  approvals_file 还能被指到受保护目录或凭据文件上——比如 token_file="data/.ENV"
    #  会把网关令牌直接写进 CTP 凭据文件)。
    reserved_dirs = {d.lower() for d in RESERVED_DIRS}
    reserved_files = {f.lower() for f in RESERVED_FILES}
    for key in ("pending_dir", "results_dir", "approvals_file"):
        path = _relative_inside(cfg["paths"][key], f"paths.{key}")
        if path.parts[0].lower() in reserved_dirs:
            raise ConfigError(f"paths.{key} 不得指向受保护目录 {path.parts[0]!r}")
        if key == "approvals_file" and path.name.lower() in reserved_files:
            raise ConfigError(f"paths.approvals_file 不得借用凭据文件 {path.name!r}")
    # 结构约束:批准记录固定在 data/research-control/approvals.json(第 7 轮 codex 的 F8)
    approvals = _relative_inside(cfg["paths"]["approvals_file"], "paths.approvals_file")
    if approvals.parts != CONTROL_DIR + (APPROVALS_FILE_NAME,):
        raise ConfigError(f"paths.approvals_file 必须固定为 {'/'.join(CONTROL_DIR)}/{APPROVALS_FILE_NAME}")
    # 批准目录同样**固定**:它是「运行时唯一认的目录」,收菜往这里写、装载从这里读。
    # 位置不能配——配了就有「把批准目录指到别处」的口子(比如指到 strategies/ 里,
    # 于是候选目录与受信目录混为一谈)。它同时在 RESERVED_DIRS 里,所以不能走上面那条
    # 「不得指向受保护目录」的通用检查(那条针对的是**可指向别处**的路径),改为结构约束。
    collected = _relative_inside(cfg["paths"]["collected_file"], "paths.collected_file")
    if collected.parts != CONTROL_DIR + (COLLECTED_FILE_NAME,):
        raise ConfigError(f"paths.collected_file 必须固定为 {'/'.join(CONTROL_DIR)}/{COLLECTED_FILE_NAME}")
    approved = _relative_inside(cfg["paths"]["approved_dir"], "paths.approved_dir")
    if approved.parts != (APPROVED_DIR_NAME,):
        raise ConfigError(f"paths.approved_dir 必须固定为 {APPROVED_DIR_NAME}")
    auto = _relative_inside(cfg["paths"]["auto_approvals_file"], "paths.auto_approvals_file")
    if auto.parts != CONTROL_DIR + (AUTO_APPROVALS_FILE_NAME,):
        raise ConfigError(f"paths.auto_approvals_file 必须固定为 {'/'.join(CONTROL_DIR)}/{AUTO_APPROVALS_FILE_NAME}")
    attempts = _relative_inside(cfg["paths"]["attempts_file"], "paths.attempts_file")
    if attempts.parts != ("data", "research", ATTEMPTS_FILE_NAME):
        raise ConfigError(f"paths.attempts_file 必须固定为 data/research/{ATTEMPTS_FILE_NAME}")
    if not isinstance(cfg["auto_approve"]["enabled"], bool):
        raise ConfigError("auto_approve.enabled 必须是布尔值")
    token = _relative_inside(cfg["gateway"]["token_file"], "gateway.token_file")
    if token.name.lower() in reserved_files or token.parts[0].lower() in reserved_dirs:
        raise ConfigError("gateway.token_file 不得借用凭据文件或源码目录")
    if token.parts != CONTROL_DIR + (TOKEN_FILE_NAME,):
        raise ConfigError(f"gateway.token_file 必须固定为 {'/'.join(CONTROL_DIR)}/{TOKEN_FILE_NAME}")

    for key in ("extra_forbidden_modules", "extra_forbidden_calls", "allowed_modules"):
        value = cfg["screening"][key]
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            raise ConfigError(f"screening.{key} 必须是非空字符串列表")


def load(path: Path | str | None = None) -> dict:
    # 显式参数 > 环境变量 RESEARCH_SANDBOX_CONFIG(容器里用它指向启动时生成的配置)> 随仓库的默认文件
    path = Path(path) if path is not None else Path(os.environ.get("RESEARCH_SANDBOX_CONFIG") or CONFIG_PATH)
    if not path.exists():
        cfg = copy.deepcopy(DEFAULTS)
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"配置文件无法读取,拒绝启动(fail-closed): {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件顶层必须是对象")
        cfg = _merge(DEFAULTS, raw)
    _validate(cfg)
    return cfg


def resolve_paths(cfg: dict, root: Path) -> dict[str, Path]:
    """把配置里的相对路径解析为 root 下的绝对路径(再次确认没有越出 root)。"""
    root = root.resolve()
    out = {
        "pending_dir": cfg["paths"]["pending_dir"], "results_dir": cfg["paths"]["results_dir"],
        "approvals_file": cfg["paths"]["approvals_file"], "token_file": cfg["gateway"]["token_file"],
        "approved_dir": cfg["paths"]["approved_dir"],
        "collected_file": cfg["paths"]["collected_file"],
        "auto_approvals_file": cfg["paths"]["auto_approvals_file"],
        "attempts_file": cfg["paths"]["attempts_file"],
    }
    resolved = {}
    for key, rel in out.items():
        path = (root / rel).resolve()
        if root not in path.parents:
            raise ConfigError(f"{key} 解析后越出项目根目录: {rel!r}")
        resolved[key] = path
    return resolved


# ---------- 已知凭据值(按值脱敏用) ----------

# 毕昇开放 API 密钥文件(相对**数据根**):环境变量优先,其次这个文件。
# 与 `tools/research_mcp/server.py` 的 `KEY_FILE` 同一处约定。
BISHENG_KEY_RELATIVE = Path(".local") / "bisheng-openapi.key"


def bisheng_openapi_key(root: Path | None = None, key_file: Path | None = None) -> str:
    """毕昇开放 API 密钥:环境变量 `BISHENG_OPENAPI_KEY` 优先,其次密钥文件。

    `key_file` 显式给路径(MCP 侧有自己的一份常量);不给就用数据根下的默认位置。

    读不到就返回空串——**不让整条脱敏失效**(第 11 轮 codex 的 F6)。
    """
    env = (os.environ.get("BISHENG_OPENAPI_KEY") or "").strip()
    if env and env.lower() not in ("undefined", "null"):
        return env
    base = Path(root) if root is not None else ROOT
    path = Path(key_file) if key_file is not None else base / BISHENG_KEY_RELATIVE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def known_secret_values(cfg: dict | None = None, root: Path | None = None,
                        label: str = "research", token: str | None = None,
                        key_file: Path | None = None) -> tuple[str, ...]:
    """**按值**要盖掉的已知凭据清单:毕昇开放 API 密钥 + 网关鉴权令牌。

    三条模型可见通道(网关 / MCP / CLI)共用这一份清单(第 14 轮 codex 的 M3/L10):
    清单不共享 = 口径漂移 = 漏的那一条就是漏的那一条。原先 MCP 有「密钥 + 令牌」,
    CLI 与网关只有令牌——候选一旦把毕昇密钥写进异常文本,MCP 盖住、CLI/网关不盖。

    `token` 给了就用给的(网关手里已经握着令牌,不必再读一次文件);
    没给就从配置里的令牌文件读。令牌读不到时**在 stderr 留痕**(第 13 轮 codex 的 L2):
    静默降级会让人以为「按值那一层还在」。stderr 不是模型可见通道,不影响脱敏口径。
    """
    base = Path(root) if root is not None else ROOT
    values: list[str] = []
    key = bisheng_openapi_key(base, key_file)
    if key:
        values.append(key)
    if token is None:
        token = ""
        try:
            cfg = cfg if cfg is not None else load()
            token = resolve_paths(cfg, base)["token_file"].read_text(encoding="utf-8").strip()
        except (OSError, ValueError, ConfigError, KeyError) as exc:
            print(f"[{label}] 读不到网关令牌,按值脱敏这一层降级: {exc}", file=sys.stderr)
            token = ""
    token = (token or "").strip()
    if token:
        values.append(token)
    return tuple(values)
