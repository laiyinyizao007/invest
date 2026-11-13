"""状态页配置:路径、阈值、环境标签。

配置文件 `config/status_dashboard.json` 可选(不存在则全部用默认值),**不含任何秘密**。
所有相对路径都相对项目根,并校验解析后没有越出项目根(与研究沙盒配置同一做法)。
面板不读取 `.env`;环境标签(simnow / guoxin-test / live)由本配置显式给出。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from risk.account_guard import AccountLimits

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "status_dashboard.json"

# 与 trading.guard_rule.PAUSE_FLAG_NAME 必须相等(面板不能 import trading,测试里断言两者一致)
PAUSE_FLAG_NAME = "pause_trading.flag"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

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
    "evolution_dir": "data/research/evolution",     # 谱系台账(引擎侧写,面板只读)
    "heatmap_dir": "data/research/heatmaps",         # 参数网格得分表(引擎侧写,面板只读)
    "static_dir": "dashboard/static",
    "token_file": "data/dashboard/status.token",           # 面板专属目录,不在任何交易侧目录树里(见 _check_token_location)
    "stale_after_seconds": {"trading": 60.0, "alerts": 300.0, "days": 600.0, "recording": 120.0, "research": 3600.0, "docs": 86400.0},
    "docs": {"unattended-log": "docs/unattended-log.md", "simnow-drills": "docs/simnow-drills.md"},
    "docs_titles": {"unattended-log": "无人值守运行记录", "simnow-drills": "SimNow 演练记录"},
    "evolution": {"enabled": False, "stagnant_generations": 5, "stagnant_epsilon": 0.05, "overfit_mean_gap": 1.0, "log_limit": 50,
                "summary_ttl": 30.0},   # 战役摘要缓存的秒数:总览每 8 秒轮询,不能每次都重读全部台账
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


def _check_evolution(cfg: Any) -> None:
    """进化面板的配置取值。**非法值在启动时就被拒**,而不是让页面在半夜 500
    (config.load 的既定约定:非法即抛 ConfigError,由入口转为拒绝启动)。"""
    if not isinstance(cfg, dict):
        raise ConfigError("evolution 必须是对象")
    if not isinstance(cfg.get("enabled"), bool):
        raise ConfigError("evolution.enabled 必须是 true/false")
    for key in ("stagnant_generations", "log_limit"):
        value = cfg.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"evolution.{key} 必须是正整数")
    for key in ("stagnant_epsilon", "overfit_mean_gap", "summary_ttl"):
        value = cfg.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ConfigError(f"evolution.{key} 必须是非负有限数")


def _resolve(root: Path, rel: str, what: str) -> Path:
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise ConfigError(f"{what} 解析后越出项目根目录: {rel!r}")
    return path


def load(path: Path | None = None, root: Path = ROOT) -> Settings:
    """读配置(缺文件用默认值),校验取值与路径。非法即抛 ConfigError,由入口转为拒绝启动。"""
    raw = json.loads(json.dumps(DEFAULTS))
    cfg_path = (root / "config" / "status_dashboard.json") if path is None else path       # 缺省配置跟着 root 走
    if cfg_path.exists():
        try:
            over = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
        if not isinstance(over, dict):
            raise ConfigError("配置文件顶层必须是对象")
        raw = _merge(raw, over)
    if raw["host"] not in LOOPBACK_HOSTS:
        raise ConfigError(f"阶段①只允许监听本机回环地址 {sorted(LOOPBACK_HOSTS)},收到 {raw['host']!r}(经局域网访问属于阶段⑤,须另写 ADR)")
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
    _check_evolution(raw.get("evolution"))
    root = root.resolve()
    paths = {
        "state_dir": _resolve(root, raw["state_dir"], "state_dir"),
        "recording_dir": _resolve(root, raw["recording_dir"], "recording_dir"),
        "research_config": _resolve(root, raw["research_config"], "research_config"),
        "evolution_dir": _resolve(root, raw["evolution_dir"], "evolution_dir"),
        "heatmap_dir": _resolve(root, raw["heatmap_dir"], "heatmap_dir"),
        "static_dir": _resolve(root, raw["static_dir"], "static_dir"),
        "token_file": _resolve(root, raw["token_file"], "token_file"),
        "log_dirs": [_resolve(root, d, "log_dirs") for d in raw["log_dirs"]],
        "docs": {k: _resolve(root, v, f"docs.{k}") for k, v in raw["docs"].items()},
    }
    if set(raw["docs"]) != set(raw["docs_titles"]):
        raise ConfigError("docs 与 docs_titles 的键必须一致")
    _check_token_location(paths, root, raw["token_file"])
    return Settings(raw=raw, root=root, paths=paths)


# 面板专属目录:令牌文件只许放在这里(白名单)。
# 曾用拒绝名单列出交易侧目录,结果漏掉了 .vntrader_drill / .vntrader_brokertest / .vntrader_probe——拒绝名单永远追不上新增的目录。
DASHBOARD_DIR = "data/dashboard"


def _is_link(path: Path) -> bool:
    """符号链接或 Windows junction(reparse point)。不存在的路径不算链接。"""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)         # Path.is_junction 是 Python 3.12 才有的
        if is_junction is not None:
            return bool(is_junction())
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)  # 旧版本 Windows:直接看 reparse point 属性
        return bool(attrs & 0x400)
    except FileNotFoundError:
        return False
    except OSError:
        return True                           # 无法判断就按有风险处理(fail-closed)


def _check_token_location(paths: dict, root: Path, rel: str | None = None) -> None:
    """令牌文件必须位于面板专属目录 data/dashboard/ 之下,其他位置一律拒绝。

    G8 第 2 轮审查(Critical):令牌曾默认写在 data/state/——交易系统的状态目录。创建文件会改变目录的 mtime、
    占用同一块磁盘空间,也可能触发交易侧对目录的扫描或备份,违反『面板绝不写入交易系统』。
    G8 第 3 轮(High):白名单根目录自己被 resolve() 之后再比较,若 data/dashboard 是指向 data/state 的 junction,
    白名单与令牌会一起落到 data/state,校验反而通过。现在:① 按**字面路径**比较(不跟随链接);
    ② 从 data/ 往下到令牌所在目录的每一级都不得是符号链接/junction;③ 解析后的位置仍须在专属目录内。
    """
    token_dir = paths["token_file"].parent
    lexical = Path(os.path.abspath(root / rel)) if rel is not None else paths["token_file"]
    lexical_allowed = Path(os.path.abspath(root / DASHBOARD_DIR))
    lexical_dir = lexical.parent
    if lexical_dir != lexical_allowed and lexical_allowed not in lexical_dir.parents:
        raise ConfigError(f"令牌文件只能放在面板专属目录 {DASHBOARD_DIR}/ 之下(收到 {lexical.relative_to(root) if root in lexical.parents else lexical});"
                          "放在别处就是在写交易系统或其他进程的目录")
    node = lexical_dir
    while node != root and root in node.parents:               # 从令牌目录一路检查到项目根之下第一级
        if _is_link(node):
            raise ConfigError(f"令牌文件所在路径里有符号链接或 junction({node.relative_to(root)}):它可能把令牌写进交易侧目录;请改用真实目录")
        node = node.parent
    resolved_allowed = (root / DASHBOARD_DIR).resolve()
    if token_dir != resolved_allowed and resolved_allowed not in token_dir.parents:
        raise ConfigError(f"令牌文件解析后不在面板专属目录 {DASHBOARD_DIR}/ 之内")
    if resolved_allowed != lexical_allowed:                    # 白名单根自己被重定向
        raise ConfigError(f"面板专属目录 {DASHBOARD_DIR}/ 被链接到别处,令牌可能写进交易侧目录")


def verify_token_location(settings: "Settings") -> None:
    """创建令牌前再验一次:配置加载与实际创建之间,目录可能被换成链接。"""
    _check_token_location(settings.paths, settings.root, settings.raw["token_file"])
