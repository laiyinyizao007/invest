"""只读探针:登录后查询持仓与资金,打印结果后退出。**不下任何单。**

用途:
1. 取证脚本的空仓闸门会把「有持仓」和「查不到持仓(连接异常)」合成同一句话,
   这里把两件事分开看清楚(查询用 CTP 的持仓查询接口,不发委托、不撤单);
2. 判断某个前置能不能连上:打印里程碑(连接成功/授权验证/登录成功/4097 断开),
   用来区分「前置没开」「客户端版本不对」「账号没授权」。

用法:
    python scripts/probe_position.py                        # 用 .env 里的档案
    python scripts/probe_position.py --profile .env.guoxin  # 用指定档案(国信测试环境)
    python scripts/probe_position.py --seconds 45           # 观察多久(默认 60)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402  必须最先导入:隔离 vn.py 状态目录

PROBE_ROOT = bootstrap.ROOT / ".vntrader_probe"
ISOLATED = bootstrap.isolate_state(PROBE_ROOT.name)

from ctp_tools.collector import Collector, check_positions, close_engine  # noqa: E402
from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.object import SubscribeRequest  # noqa: E402
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_TICK  # noqa: E402

MILESTONES = ("交易服务器连接成功", "交易服务器授权验证成功", "交易服务器登录成功",
              "结算信息确认成功", "合约信息查询成功", "行情服务器连接成功", "行情服务器登录成功")


def load_profile(profile: str) -> dict[str, str]:
    """取档案里的值;进程环境变量优先(dotenv override=False 的同一套语义)。"""
    env = dict(os.environ)
    if profile:
        from dotenv import dotenv_values
        path = Path(profile)
        if not path.is_absolute():
            path = bootstrap.ROOT / path
        values = dotenv_values(path, encoding="utf-8-sig")
        for key, value in values.items():
            if value is not None:
                env.setdefault(key, value)
    return env


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else ""
    seconds = 60
    env = load_profile(profile)
    bootstrap.load_env()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)

    from vnpy_ctp import CtpGateway
    main_engine.add_gateway(CtpGateway)
    setting = {
        "用户名": env["CTP_USERNAME"],
        "密码": env["CTP_PASSWORD"],
        "经纪商代码": env["CTP_BROKER_ID"],
        "交易服务器": env["CTP_TD_ADDRESS"],
        "行情服务器": env["CTP_MD_ADDRESS"],
        "产品名称": env["CTP_APP_ID"],
        "授权编码": env["CTP_AUTH_CODE"],
        "柜台环境": env.get("CTP_ENVIRONMENT", "实盘"),
    }
    print(f"档案 {profile or '(当前 .env)'} 交易前置 {setting['交易服务器']} "
          f"行情前置 {setting['行情服务器']} 柜台环境={setting['柜台环境']} "
          f"BrokerID={setting['经纪商代码']}")
    main_engine.connect(setting, "CTP")

    deadline = time.monotonic() + seconds
    logged_in = False
    while time.monotonic() < deadline:
        with collector.lock:
            lines = list(collector.log_lines)
        if any("交易服务器登录成功" in line for line in lines):
            logged_in = True
            break
        if any("授权验证失败" in line or "登录失败" in line for line in lines):
            break
        time.sleep(0.5)

    with collector.lock:
        lines = list(collector.log_lines)
    print("--- 里程碑 ---")
    for text in MILESTONES:
        hit = [line for line in lines if text in line]
        print(f"  {'✔' if hit else '✘'} {text}" + (f"  {hit[-1].split('| ', 1)[-1]}" if hit else ""))
    disconnects = [line for line in lines if "连接断开" in line]
    if disconnects:
        print(f"--- 断连 {len(disconnects)} 次,最近一条:{disconnects[-1].split('| ', 1)[-1]}")

    symbol = env.get("LIVE_VT_SYMBOL", "")
    vt_symbol = symbol
    if "." in symbol:
        product, number = symbol.split(".", 1)
        vt_symbol = f"{number}.{product.upper()}"
    if logged_in:
        contract = collector.contract_of(symbol)
        if contract is not None:
            main_engine.subscribe(SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange), "CTP")
            time.sleep(10)
        held, connected = check_positions(main_engine, vt_symbol, wait=10.0, early_exit=False)
        gateway = main_engine.get_gateway("CTP")
        td = getattr(gateway, "td_api", None)
        print(f"td_api.connect_status = {getattr(td, 'connect_status', None)}  "
              f"login_status = {getattr(td, 'login_status', None)}  check_positions 认为 connected={connected}")
        print(f"非零持仓条数 = {len(held)}")
        for h in held:
            print("  持仓:", h)
        for p in main_engine.get_all_positions():
            print(f"  vn.py 持仓: {p.vt_symbol} {p.direction.value} volume={p.volume} yd={p.yd_volume} frozen={p.frozen}")
        for a in main_engine.get_all_accounts():
            print(f"  资金: 余额={a.balance} 可用={a.available} 冻结={a.frozen}")
    else:
        print("未登录成功,下面的查询不可信,已跳过。")
    print("探针结束(未发任何委托)")
    close_engine(main_engine)
    # CTP 的原生线程是非守护线程,close_engine 超时后进程不会自己退出 → 强制退出。
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
