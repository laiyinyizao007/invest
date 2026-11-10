"""VeighNa Trader 图形界面入口:用于在 SimNow 上手动观察行情/委托/成交,以及手动试验下单与撤单。

用法(项目根目录):
    python scripts/run_gui.py                 # 打开界面并按 .env 自动连接 CTP
    python scripts/run_gui.py --subscribe c2701.DCE   # 合约表到位后自动订阅行情(默认取 .env 的 LIVE_VT_SYMBOL)
    python scripts/run_gui.py --no-connect    # 只打开界面,用菜单"系统 → 连接CTP"手动连接
    python scripts/run_gui.py --check         # 只校验配置与依赖,不打开界面

与无头版(run_headless.py)的区别:
- 同样挂载账户风控(日亏 600 元 / 累计回撤 2000 元硬锁定、平仓放行等),界面里手动下单也受它约束;
- **没有**启动对账、行情停更/持仓漂移/策略崩溃看门狗和 Webhook 告警——这些只在无头版里;
- 硬锁定生效时本脚本拒绝连接;
- 策略在界面的 CTA 策略页里手动添加、初始化、启动(策略类 TrendAtrStrategy 会被自动发现);
- 与无头版**不要同时运行**:两个进程会共用同一个账号和 .vntrader 下的策略状态文件。

这是人工观察与试验用的入口,不是无人值守的运行方式。
"""
import argparse
import os
import sys
from collections.abc import Mapping

import bootstrap

from dotenv import load_dotenv

from trading.runtime import build_ctp_setting


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-connect", action="store_true", help="不自动连接,用界面菜单手动连接")
    parser.add_argument("--subscribe", default=os.environ.get("LIVE_VT_SYMBOL", ""),
                        help="合约表到位后自动订阅的合约,如 c2701.DCE(默认取 LIVE_VT_SYMBOL;留空则不订阅)")
    parser.add_argument("--check", action="store_true", help="只校验配置与依赖,不打开界面")
    return parser.parse_args(argv)


def build_gui(env: Mapping[str, str], auto_connect: bool, subscribe: str = ""):
    """组装主引擎与主窗口,返回 (qapp, main_window, main_engine)。不进入事件循环,便于测试。"""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.event import EVENT_CONTRACT
    from vnpy.trader.object import SubscribeRequest
    from vnpy.trader.ui import MainWindow, create_qapp
    from vnpy_ctabacktester import CtaBacktesterApp
    from vnpy_ctastrategy import CtaStrategyApp
    from vnpy_ctp import CtpGateway

    from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK
    from trading.safe_risk import SafeRiskManagerApp

    gateway_setting = build_ctp_setting(env)
    qapp = create_qapp("invest · SimNow")
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)
    risk = main_engine.add_app(SafeRiskManagerApp)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)

    def on_alert(event):
        a = event.data
        main_engine.write_log(f"[风控 {a['level']}] {a['title']}: {a['message']}")

    def on_hard_lock(event):
        main_engine.write_log(f"[风控 FATAL] 硬锁定: {event.data.get('reason', '')}(已平仓: {event.data.get('flat')})")

    def on_contract(event):
        c = event.data
        if subscribe and c.vt_symbol == subscribe:           # 合约表到位后自动订阅,否则界面的行情表是空的
            main_engine.subscribe(SubscribeRequest(symbol=c.symbol, exchange=c.exchange), c.gateway_name)
            main_engine.write_log(f"已自动订阅行情: {subscribe}")

    event_engine.register(EVENT_CONTRACT, on_contract)
    event_engine.register(EVENT_GUARD_ALERT, on_alert)
    event_engine.register(EVENT_GUARD_HARD_LOCK, on_hard_lock)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    if auto_connect:
        rule = risk.rules["账户风控"]
        if rule.guard.hard_locked:
            main_engine.write_log(f"硬锁定生效中,不连接: {rule.guard.hard_reason}。核对后用 scripts/unlock_account.py 解锁。")
        else:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda: main_engine.connect(gateway_setting, "CTP"))
    return qapp, main_window, main_engine


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    if env is None:
        load_dotenv(bootstrap.ROOT / ".env", override=False)
        env = os.environ
    args = parse_args(argv)
    try:
        build_ctp_setting(env)
        import PySide6  # noqa: F401
        import vnpy_ctp  # noqa: F401
    except (ValueError, ImportError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print("配置检查通过: CTP 配置齐全,界面依赖可用")
        return 0
    qapp, _window, _engine = build_gui(env, auto_connect=not args.no_connect, subscribe=args.subscribe)
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
