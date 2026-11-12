"""使用官方 vnpy_datarecorder 录制 C/MA Tick 与 1 分钟 Bar。

此入口只连接行情、订阅具体合约并写入独立数据库；不加载交易策略、不发单、不查询账户。
默认只允许 SimNow，国信测试必须显式使用 ``guoxin-test`` profile。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap
from simnow_guard import check_simnow_address

PROFILE_SIMNOW = "simnow"
PROFILE_GUOXIN = "guoxin-test"
PROFILES = frozenset({PROFILE_SIMNOW, PROFILE_GUOXIN})
SYMBOL_RE = re.compile(r"^(c\d{3,4}\.DCE|MA\d{3,4}\.CZCE)$", re.IGNORECASE)
REQUIRED_MARKET_CTP = (
    "CTP_USERNAME", "CTP_PASSWORD", "CTP_BROKER_ID", "CTP_MD_ADDRESS",
)


def _require_td_for_simnow(env: Mapping[str, str], profile: str) -> None:
    if profile == PROFILE_SIMNOW and not env.get("CTP_TD_ADDRESS"):
        raise ValueError("SimNow profile 必须提供 CTP_TD_ADDRESS 以执行地址白名单校验")




def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(item.strip() for item in value.split(",") if item.strip())
    if not symbols:
        raise ValueError("CTP_RECORD_SYMBOLS 必须包含至少一个具体 C/MA 合约")
    invalid = [item for item in symbols if not SYMBOL_RE.fullmatch(item)]
    if invalid:
        raise ValueError("只允许具体合约 cxxxx.DCE 或 MAxxxx.CZCE: " + ", ".join(invalid))
    normalized = tuple(item.upper() for item in symbols)
    if not any(item.startswith("C") for item in normalized):
        raise ValueError("CTP_RECORD_SYMBOLS 必须包含 C 合约")
    if not any(item.startswith("MA") for item in normalized):
        raise ValueError("CTP_RECORD_SYMBOLS 必须包含 MA 合约")
    return normalized


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or value == "":
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def validate_config(env: Mapping[str, str]) -> dict:
    profile = (env.get("CTP_RECORD_PROFILE") or PROFILE_SIMNOW).strip()
    if profile not in PROFILES:
        raise ValueError(f"CTP_RECORD_PROFILE 只允许 {', '.join(sorted(PROFILES))}")
    missing = [name for name in REQUIRED_MARKET_CTP if not env.get(name)]
    if missing:
        raise ValueError("缺少环境变量: " + ", ".join(missing))
    symbols = parse_symbols(env.get("CTP_RECORD_SYMBOLS", ""))
    environment = (env.get("CTP_ENVIRONMENT") or "实盘").strip()
    if environment not in {"实盘", "测试"}:
        raise ValueError("CTP_ENVIRONMENT 只能是「实盘」或「测试」")
    if profile == PROFILE_GUOXIN and environment != "测试":
        raise ValueError("guoxin-test 必须使用 CTP_ENVIRONMENT=测试")
    _require_td_for_simnow(env, profile)
    if profile == PROFILE_SIMNOW:
        allowed, note = check_simnow_address(env["CTP_TD_ADDRESS"])
        if not allowed:
            raise ValueError(note)
    if not _bool_env(env, "CTP_RECORD_TICK", True) and not _bool_env(env, "CTP_RECORD_1M", True):
        raise ValueError("CTP_RECORD_TICK 与 CTP_RECORD_1M 不能同时关闭")
    return {
        "profile": profile,
        "environment": environment,
        "symbols": symbols,
        "record_tick": _bool_env(env, "CTP_RECORD_TICK", True),
        "record_1m": _bool_env(env, "CTP_RECORD_1M", True),
        "state_dir": Path(env.get("CTP_RECORD_STATE_DIR", ".ops/recording")),
        "database_dir": Path(env.get("CTP_RECORD_DATABASE_DIR", ".vntrader_record")),
    }


def public_config(config: dict) -> dict:
    return {
        "profile": config["profile"],
        "environment": config["environment"],
        "symbols": list(config["symbols"]),
        "record_tick": config["record_tick"],
        "record_1m": config["record_1m"],
        "state_dir": str(config["state_dir"]),
        "database_dir": str(config["database_dir"]),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RecorderStatus:
    def __init__(self, path: Path, config: dict):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "pid": os.getpid(), "started_at": _utc_now(), "status": "starting",
            "profile": config["profile"], "symbols": list(config["symbols"]),
            "database_dir": str(config["database_dir"]), "last_tick": None,
            "last_bar": None, "last_error": None,
        }
        self.write()

    def update(self, **values) -> None:
        with self.lock:
            self.data.update(values)
            self.data["heartbeat_at"] = _utc_now()
            self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="只校验配置，不连接柜台")
    return parser


def run(config: dict, env: Mapping[str, str]) -> int:
    """启动 vn.py 主引擎和官方 RecorderEngine；仅用于已通过配置检查的环境。"""
    state_root = (bootstrap.ROOT / config["database_dir"]).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    config["state_dir"] = (bootstrap.ROOT / config["state_dir"]).resolve()
    config["database_dir"] = state_root
    config["state_dir"].mkdir(parents=True, exist_ok=True)

    # vnpy_sqlite 在导入时缓存 SETTINGS 和 TEMP_DIR，必须先完成隔离再导入数据库/录制器。
    bootstrap.isolate_state(state_root.name)
    from vnpy.trader.setting import SETTINGS
    SETTINGS["database.database"] = "database.db"
    os.chdir(state_root)

    from vnpy.event import Event, EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
    from vnpy_ctp import CtpGateway
    from vnpy_datarecorder import DataRecorderApp

    class MarketDataOnlyCtpGateway(CtpGateway):
        """只初始化 CTP 行情前置，不建立交易前置或查询账户/持仓。"""

        def connect(self, setting: dict) -> None:
            address = setting["行情服务器"]
            if not address.startswith(("tcp://", "ssl://", "socks")):
                address = "tcp://" + address
            self.md_api.connect(
                address,
                setting["用户名"],
                setting["密码"],
                setting["经纪商代码"],
                setting.get("柜台环境", "实盘") == "实盘",
            )

        def close(self) -> None:
            self.md_api.close()

        def init_query(self) -> None:
            # 交易账户/持仓查询属于录制入口禁止触碰的交易状态。
            return

    setting = {
        "用户名": env["CTP_USERNAME"], "密码": env["CTP_PASSWORD"],
        "经纪商代码": env["CTP_BROKER_ID"], "行情服务器": env["CTP_MD_ADDRESS"],
        "柜台环境": env.get("CTP_ENVIRONMENT", "实盘"),
    }
    status = RecorderStatus(config["state_dir"] / "status.json", config)
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(MarketDataOnlyCtpGateway)
    main_engine.add_app(DataRecorderApp)
    recorder = main_engine.get_engine("DataRecorder")
    subscribed = set()
    stop = threading.Event()

    def on_contract(event: Event) -> None:
        contract = event.data
        vt_symbol = contract.vt_symbol.upper()
        if vt_symbol not in config["symbols"] or vt_symbol in subscribed:
            return
        subscribed.add(vt_symbol)
        if config["record_tick"]:
            recorder.add_tick_recording(vt_symbol)
        if config["record_1m"]:
            recorder.add_bar_recording(vt_symbol)
        status.update(status="recording", connected_symbols=sorted(subscribed))

    def on_tick(event: Event) -> None:
        tick = event.data
        if tick.vt_symbol.upper() in config["symbols"]:
            status.update(last_tick=tick.datetime.isoformat() if tick.datetime else None)

    event_engine.register(EVENT_CONTRACT, on_contract)
    event_engine.register(EVENT_TICK, on_tick)
    event_engine.start()
    status.update(status="connecting")
    main_engine.connect(setting, "CTP")

    def request_stop(_signum=None, _frame=None):
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass
    try:
        while not stop.wait(5):
            status.update(status="recording" if subscribed else "connected")
    finally:
        status.update(status="stopping")
        main_engine.close()
        status.update(status="stopped")
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    if env is None:
        bootstrap.load_env()
        env = os.environ
    try:
        config = validate_config(env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(argv)
    if args.check:
        print("配置检查通过: " + json.dumps(public_config(config), ensure_ascii=False, sort_keys=True))
        return 0
    try:
        return run(config, env)
    except Exception as exc:
        print(f"录制启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
