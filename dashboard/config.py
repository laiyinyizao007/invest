"""状态页配置:路径、阈值、环境标签。

配置文件 `config/status_dashboard.json` 可选(不存在则全部用默认值),**不含任何秘密**。
所有相对路径都相对项目根,并校验解析后没有越出项目根(与研究沙盒配置同一做法)。
面板不读取 `.env`;环境标签(simnow / guoxin-test / live)由本配置显式给出。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from risk.account_guard import AccountLimits

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "status_dashboard.json"

# 与 trading.guard_rule.PAUSE_FLAG_NAME 必须相等(面板不能 import trading,测试里断言两者一致)
PAUSE_FLAG_NAME = "pause_trading.flag"

ENVIRONMENTS = {
    "simnow": "SimNow 模拟盘",
    "guoxin-test": "国信测试环境",
    "live": "实盘",
    "unknown": "环境未知",
}

DEFAULTS: dict = {
    "environment": "unknown",
    "host": "127.0.0.1",
    "port": 8770,
    "polling_hint_seconds": 8,
    "rate_limit_per_minute": 120,
    "state_dir": "data/state",
    "log_dirs": ["runtime/vntrader/log"],
    "recording_dir": ".ops/recording",
    "research_config": "config/research_sandbox.json",
    "static_dir": "dashboard/static",
    "token_file": "data/state/status.token",
    "stale_after_seconds": {"trading": 60.0, "alerts": 300.0, "days": 600.0, "recording": 120.0, "research": 3600.0, "docs": 86400.0},
    "docs": {"unattended-log": "docs/unattended-log.md", "simnow-drills": "docs/simnow-drills.md"},
    "docs_titles": {"unattended-log": "无人值守运行记录", "simnow-drills": "SimNow 演练记录"},
    "days_default": 14,
    "recent_runs": 10,
    "limits": {"daily_loss_yuan": None, "hard_drawdown_yuan": None},      # None = 取风控默认值
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k not in base:
            raise ConfigError(f"未知配置项: {k}")
        out[k] = _merge(base[k], v) if isinstance(base[k], dict) and isinstance(v, dict) and k not in ("docs", "docs_titles") else v
    return out


@dataclass(frozen=True)
class Settings:
    raw: dict
    root: Path = ROOT
    paths: dict = field(default_factory=dict)

    # ---- 便捷访问 ----
    @property
    def environment(self) -> dict:
        label = self.raw["environment"]
        return {"label": label, "display": ENVIRONMENTS[label]}

    @property
    def limits(self) -> dict:
        d = AccountLimits()
        cfg = self.raw["limits"]
        return {"daily_loss_yuan": cfg["daily_loss_yuan"] if cfg["daily_loss_yuan"] is not None else d.daily_loss_limit,
                "hard_drawdown_yuan": cfg["hard_drawdown_yuan"] if cfg["hard_drawdown_yuan"] is not None else d.max_drawdown}

    def stale_after(self, name: str) -> float:
        return float(self.raw["stale_after_seconds"][name])


def _resolve(root: Path, rel: str, what: str) -> Path:
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise ConfigError(f"{what} 解析后越出项目根目录: {rel!r}")
    return path


def load(path: Path | None = None, root: Path = ROOT) -> Settings:
    """读配置(缺文件用默认值),校验取值与路径。非法即抛 ConfigError,由入口转为拒绝启动。"""
    raw = json.loads(json.dumps(DEFAULTS))
    cfg_path = CONFIG_PATH if path is None else path
    if cfg_path.exists():
        try:
            over = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
        if not isinstance(over, dict):
            raise ConfigError("配置文件顶层必须是对象")
        raw = _merge(raw, over)
    if raw["environment"] not in ENVIRONMENTS:
        raise ConfigError(f"environment 必须是 {sorted(ENVIRONMENTS)} 之一")
    if not isinstance(raw["port"], int) or isinstance(raw["port"], bool) or not 1 <= raw["port"] <= 65535:
        raise ConfigError("port 必须是 1-65535 的整数")
    for key in ("polling_hint_seconds", "rate_limit_per_minute", "days_default", "recent_runs"):
        if not isinstance(raw[key], int) or isinstance(raw[key], bool) or raw[key] < 1:
            raise ConfigError(f"{key} 必须是正整数")
    for key, value in raw["stale_after_seconds"].items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"stale_after_seconds.{key} 必须是正数")
    for key in ("daily_loss_yuan", "hard_drawdown_yuan"):
        v = raw["limits"][key]
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0):
            raise ConfigError(f"limits.{key} 必须是正数或 null")
    root = root.resolve()
    paths = {
        "state_dir": _resolve(root, raw["state_dir"], "state_dir"),
        "recording_dir": _resolve(root, raw["recording_dir"], "recording_dir"),
        "research_config": _resolve(root, raw["research_config"], "research_config"),
        "static_dir": _resolve(root, raw["static_dir"], "static_dir"),
        "token_file": _resolve(root, raw["token_file"], "token_file"),
        "log_dirs": [_resolve(root, d, "log_dirs") for d in raw["log_dirs"]],
        "docs": {k: _resolve(root, v, f"docs.{k}") for k, v in raw["docs"].items()},
    }
    if set(raw["docs"]) != set(raw["docs_titles"]):
        raise ConfigError("docs 与 docs_titles 的键必须一致")
    return Settings(raw=raw, root=root, paths=paths)
