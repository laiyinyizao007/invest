"""研究沙盒配置:所有可调参数集中在 `config/research_sandbox.json`。

设计原则:
- 文件缺失 -> 用 DEFAULTS;文件存在但内容非法(未知键、类型错、越界)-> 抛 ConfigError 拒绝启动,绝不悄悄回退成默认值。
- 安全相关的数值有硬边界(例如 feedback.limit 不得为 0 或超过 10、gateway.host 不得是 0.0.0.0),
  配置能调参,但不能把边界调没。
- screening 的两个列表只能**追加**禁用项,不能删减内置的禁用清单(内置清单在 research_sandbox.py)。
- 配置文件不含任何密钥;令牌只记录**文件路径**,令牌本身存放在该路径下(已被 .gitignore 的 data/ 覆盖)。
决议见 docs/adr/20261112-research-sandbox-config-and-approval.md。
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import bootstrap

CONFIG_PATH = bootstrap.ROOT / "config" / "research_sandbox.json"

DEFAULTS: dict[str, dict[str, Any]] = {
    "iteration": {"max_iterations": 5},
    "feedback": {"limit": 3},
    "gate": {"random_runs": 200, "timeout_seconds": 3600},
    "backtest": {"timeout_seconds": 600},
    "candidates": {"max_source_bytes": 65536, "name_pattern": r"^[a-z0-9_]{1,40}\.py$"},
    "gateway": {"host": "127.0.0.1", "port": 8765, "token_file": "data/research-control/gateway.token",
                "rate_limit_per_minute": 30, "max_body_bytes": 131072},
    "paths": {"pending_dir": "pending_strategies", "results_dir": "data/research",
              "approvals_file": "data/research-control/approvals.json"},
    "screening": {"extra_forbidden_modules": [], "extra_forbidden_calls": []},
}

# (最小, 最大);bool 不算整数
INT_BOUNDS = {
    ("iteration", "max_iterations"): (1, 50),
    ("feedback", "limit"): (1, 10),
    ("gate", "random_runs"): (20, 2000),
    ("gate", "timeout_seconds"): (1, 86_400),
    ("backtest", "timeout_seconds"): (1, 86_400),
    ("candidates", "max_source_bytes"): (1, 1_048_576),
    ("gateway", "port"): (1, 65_535),
    ("gateway", "rate_limit_per_minute"): (1, 6_000),
    ("gateway", "max_body_bytes"): (1, 4_194_304),
}
FORBIDDEN_HOSTS = {"0.0.0.0", "::", "", "*"}
# 候选策略目录不得指向这些(正式策略、批准目录),凭据文件也不得被借用为令牌文件
RESERVED_DIRS = {"strategies", "approved", "risk", "trading", "scripts", "tests", "config"}
RESERVED_FILES = {".env", ".env.simnow.bak"}
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


def _validate(cfg: dict) -> None:
    for (section, key), (low, high) in INT_BOUNDS.items():
        value = cfg[section][key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{section}.{key} 必须是整数")
        if not low <= value <= high:
            raise ConfigError(f"{section}.{key}={value} 超出允许范围 [{low}, {high}]")

    host = cfg["gateway"]["host"]
    if not isinstance(host, str) or host.strip() in FORBIDDEN_HOSTS:
        raise ConfigError(f"gateway.host={host!r} 不允许:网关不得监听所有网卡")

    pattern = cfg["candidates"]["name_pattern"]
    try:
        compiled = re.compile(pattern)
    except (re.error, TypeError) as exc:
        raise ConfigError(f"candidates.name_pattern 不是合法正则: {exc}") from exc
    for bad in SAFE_NAME_PATTERNS_MUST_REJECT:                # 文件名规则不得放开到允许路径分隔符/空名/非 .py
        if compiled.fullmatch(bad):
            raise ConfigError(f"candidates.name_pattern 过宽:不应匹配 {bad!r}")

    for key in ("pending_dir", "results_dir", "approvals_file"):
        path = _relative_inside(cfg["paths"][key], f"paths.{key}")
        if key == "pending_dir" and path.parts[0] in RESERVED_DIRS:
            raise ConfigError(f"paths.pending_dir 不得指向受保护目录 {path.parts[0]!r}")
    token = _relative_inside(cfg["gateway"]["token_file"], "gateway.token_file")
    if token.name in RESERVED_FILES or token.parts[0] in RESERVED_DIRS:
        raise ConfigError("gateway.token_file 不得借用凭据文件或源码目录")

    for key in ("extra_forbidden_modules", "extra_forbidden_calls"):
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
    }
    resolved = {}
    for key, rel in out.items():
        path = (root / rel).resolve()
        if root not in path.parents:
            raise ConfigError(f"{key} 解析后越出项目根目录: {rel!r}")
        resolved[key] = path
    return resolved
