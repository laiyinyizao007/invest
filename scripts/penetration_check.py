"""期货公司看穿式监管(穿透式)测试演练:按步骤连接测试环境、做一遍标准动作,并生成**脱敏**的留痕报告。

做的事(对应期货公司常要求的"登录、订阅行情、报单、撤单、持仓查询"):
    1 交易服务器连接 → 授权验证(AppID/AuthCode) → 登录 → 结算确认 → 合约表
    2 行情服务器连接 → 登录
    3 订阅行情并等到 Tick(非交易时段收不到,只记为提示,不算失败)
    4 查询账户与持仓
    5 (可选 --with-order)报一张**跌停价买单**再立即撤单,验证报撤单通道
报告写到 `data/penetration/penetration-<时间>.md`(data/ 不入库),可作为给期货公司的操作留痕。

用法(在运行交易程序的那台 Windows 上,项目根目录,.env 已按期货公司给的测试环境参数填好):
    python scripts/penetration_check.py --confirm-broker-test
    python scripts/penetration_check.py --confirm-broker-test --with-order --symbol c2701.DCE

安全约束:
- 必须加 `--confirm-broker-test`;
- 只允许两种目标:`CTP_ENVIRONMENT=测试`(期货公司看穿式测试环境),或交易地址在 SimNow 白名单内(预演用);
  `CTP_ENVIRONMENT=实盘` 且地址不是 SimNow = 生产 API,**一律拒绝**(没有强制放行开关);
- 报告里账号只保留头尾、密码/授权码/AppID 全部替换为 ***;
- 报单只在 --with-order 且处于交易时段时进行,价格取跌停价(通常不成交),成交了会大声告警并以非零码退出。
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

sys.path.insert(0, str(bootstrap.ROOT / "scripts"))

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Direction, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK  # noqa: E402
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

from ctp_tools.collector import Collector, check_positions, close_engine, in_trading_session  # noqa: E402
from notify.webhook import redact  # noqa: E402
from ctp_tools.simnow import is_simnow_address, normalize_address  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402

REPORT_DIR = bootstrap.ROOT / "data" / "penetration"

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOGIN = 3
EXIT_POSITION = 5

# vnpy_ctp 网关写出的里程碑日志(措辞取自 vnpy_ctp 6.7.11.4 的 ctp_gateway.py)。
MILESTONES = (
    ("td_connected", "交易服务器连接成功"),
    ("td_auth", "交易服务器授权验证成功"),
    ("td_login", "交易服务器登录成功"),
    ("settlement", "结算信息确认成功"),
    ("contracts", "合约信息查询成功"),
    ("md_connected", "行情服务器连接成功"),
    ("md_login", "行情服务器登录成功"),
)
MILESTONE_LABELS = {
    "td_connected": "交易前置连接", "td_auth": "授权验证(AppID/AuthCode)", "td_login": "交易登录",
    "settlement": "结算单确认(仅网关日志,网关不校验回报错误)", "contracts": "合约表查询", "md_connected": "行情前置连接",
    "md_login": "行情登录",
}
# 这些日志表示流程已经失败,继续等没有意义
FAILURE_MARKERS = ("授权验证失败", "交易服务器登录失败", "行情服务器登录失败")


from ctp_tools.penetration import (
    Run, check_target, do_order_cancel, failure_lines, limit_down_price, mask_account,
    milestone_status, package_version, render_report, run_checks as _run_checks,
    trade_connected, wait_milestones,
)


def run_checks(args, symbol, main_engine, collector, secrets=()):
    return _run_checks(args, symbol, main_engine, collector, secrets, in_trading_session)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="测试合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--confirm-broker-test", action="store_true", help="确认目标是期货公司的测试环境")
    parser.add_argument("--with-order", action="store_true", help="交易时段内报一张跌停价买单并撤销")
    parser.add_argument("--connect-timeout", type=float, default=120.0, help="等待登录全流程的最长时间(秒)")
    parser.add_argument("--tick-timeout", type=float, default=30.0, help="等待 Tick 的最长时间(秒)")
    parser.add_argument("--position-wait", type=float, default=8.0, help="持仓查询等待窗口(秒)")
    parser.add_argument("--out-dir", default="", help=f"报告目录(默认 {REPORT_DIR})")
    args = parser.parse_args(argv)
    # 非 UTF-8 控制台(管道/英文系统)下中文输出不应让脚本崩溃
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    bootstrap.load_env()
    env = os.environ

    symbol = args.symbol or env.get("LIVE_VT_SYMBOL", "")
    if not symbol:
        print("未指定合约:--symbol 或 .env 的 LIVE_VT_SYMBOL", file=sys.stderr)
        return EXIT_CONFIG
    try:
        setting = build_ctp_setting(env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    ok, note = check_target(setting["柜台环境"], setting["交易服务器"], args.confirm_broker_test)
    print(note, file=sys.stderr if not ok else sys.stdout)
    if not ok:
        return EXIT_CONFIG

    secrets = [env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""), env.get("CTP_APP_ID", ""),
               env.get("CTP_USERNAME", ""), env.get("WEBHOOK_URL", "")]

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    from vnpy_ctp import CtpGateway
    if "柜台环境" not in CtpGateway.default_setting:
        print("当前 vnpy_ctp 不支持「柜台环境」设置项(版本过旧或解析到了别的 Python 环境):"
              "无法保证「测试」会走测试版 API,拒绝连接。请用项目 .venv 运行。", file=sys.stderr)
        return EXIT_CONFIG
    main_engine.add_gateway(CtpGateway)
    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)

    print(f"连接 {symbol} ...")
    main_engine.connect(setting, "CTP")
    closed = False
    try:
        run, code = run_checks(args, symbol, main_engine, collector, tuple(secrets))
    finally:
        closed = close_engine(main_engine)
    run.add("关闭连接", "PASS" if closed else "WARN",
            "引擎在限时内正常关闭" if closed else "关闭超时,进程将被强制结束(国信「退出后是否正常注销」可能受影响,请观察)")

    import platform
    import socket
    meta = {
        "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "target": note,
        "account": mask_account(env.get("CTP_USERNAME", "")), "symbol": symbol,
        "vnpy": package_version("vnpy"), "vnpy_ctp": package_version("vnpy_ctp"),
        "python": platform.python_version(), "host": socket.gethostname(),
    }
    with collector.lock:
        log_lines = list(collector.log_lines)
    report = render_report(run, meta, log_lines, secrets)
    out_dir = Path(args.out_dir) if args.out_dir else REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"penetration-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {out}")
    if code == EXIT_OK and run.failed():
        code = EXIT_STEP_FAILED
    return code


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # CTP 原生线程在断连风暴下可能不退出,强制结束进程
        os._exit(code)
