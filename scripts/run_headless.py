"""SimNow / 实盘的无头运行入口(无 GUI,适合服务器与 Docker)。

用法(项目根目录):
    python scripts/run_headless.py --symbol c2701.DCE --history CADJ.DCE
    python scripts/run_headless.py --check          # 只校验配置与依赖,不连接、不交易

配置来自环境变量(优先)或项目根目录的 .env(见 config/.env.example)。密码、授权码只从环境读取,
不出现在命令行参数里,也不会写进日志或通知(通知会把它们替换成 ***)。

退出码:0 正常停止/硬锁定后确认空仓/硬锁定生效中(不要自动重启);2 配置错误/缺少实盘准入批准;
3 启动失败(可重试);4 对账失败(需人工处理)。

实盘准入:目标是生产 API(CTP_ENVIRONMENT=实盘 且交易前置不是 SimNow)时,必须存在
data/state/promotion_approval.json(由 scripts/record_promotion.py 在晋级门槛全部通过后写出),否则退出码 2。
SimNow 与期货公司看穿式测试环境(CTP_ENVIRONMENT=测试)不受限。

重要:这是**管道测试**用的运行方式。当前策略在回测里没有发现可延续的优势(见 ADR),
在没有通过晋级门槛之前,不要连接真实资金账户。
"""
import argparse
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from notify.webhook import WebhookNotifier
from risk.live_admission import APPROVAL_RELATIVE, check_live_admission
from ctp_tools.simnow import is_simnow_address
from trading.runtime import EXIT_OK, RuntimeConfig, TradingRuntime, build_ctp_setting


def webhook_outbox_path(env: Mapping[str, str]) -> Path | None:
    """待补发队列文件。默认 data/state/webhook_outbox.jsonl(容器里是挂载出来的卷,重启不丢);
    WEBHOOK_OUTBOX=off 关闭;写别的路径则改用该路径。"""
    value = (env.get("WEBHOOK_OUTBOX", "") or "").strip()
    if value.lower() in ("off", "none", "false", "0"):
        return None
    return Path(value) if value else bootstrap.ROOT / "data" / "state" / "webhook_outbox.jsonl"


def build_notifier(env: Mapping[str, str]) -> WebhookNotifier:
    url = env.get("WEBHOOK_URL", "")
    secrets = [env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""), env.get("WEBHOOK_TELEGRAM_CHAT_ID", "")]
    return WebhookNotifier(
        url,
        kind=env.get("WEBHOOK_KIND", "generic"),
        chat_id=env.get("WEBHOOK_TELEGRAM_CHAT_ID", ""),
        min_level=env.get("WEBHOOK_MIN_LEVEL", "INFO"),
        secrets=[s for s in secrets if s],
        outbox_path=webhook_outbox_path(env),
    )


def parse_args(argv: list[str] | None, env: Mapping[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default=env.get("LIVE_VT_SYMBOL", ""), help="交易的具体合约,如 c2701.DCE")
    parser.add_argument("--history", default=env.get("HISTORY_VT_SYMBOL", ""),
                        help="预热用的历史日线代码,如 CADJ.DCE(由 build_continuous.py --save 写入数据库)")
    parser.add_argument("--setting", default="{}", help='策略参数 JSON,如 \'{"fast_window":10,"slow_window":40}\'')
    parser.add_argument("--heartbeat", type=float, default=float(env.get("HEARTBEAT_SECONDS", "0") or 0))
    parser.add_argument("--approved", default="", help="从批准目录装载策略:approved/ 里的文件名,如 my_candidate.py")
    parser.add_argument("--approved-dir", default="", help="批准目录路径(默认:项目根下的 approved/)")
    parser.add_argument("--approvals-file", default="", help="批准账本路径(默认:data/research-control/approvals.json)")
    parser.add_argument("--approved-class", default="Candidate", help="批准文件里的类名(默认 Candidate)")
    parser.add_argument("--check", action="store_true", help="只校验配置与依赖,不连接、不交易")
    return parser.parse_args(argv)


def install_signal_handlers(runtime: TradingRuntime) -> list[int]:
    """SIGINT/SIGTERM(以及 Windows 的 SIGBREAK)触发优雅停止。返回成功安装的信号。"""
    def handler(signum, frame):
        runtime.request_stop(EXIT_OK, f"收到终止信号 {signum}")

    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
            installed.append(int(sig))
        except (ValueError, OSError):   # 非主线程等情况
            pass
    return installed


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    gateway_loader: Callable[[], type] | None = None,
    runtime_factory: Callable[..., TradingRuntime] = TradingRuntime,
    approval_path: Path | None = None,
) -> int:
    if env is None:
        bootstrap.load_env()     # 已存在的环境变量(如 Docker 注入)优先
        env = os.environ
    args = parse_args(argv, env)

    try:
        if not args.symbol:
            raise ValueError("未指定交易合约:--symbol 或环境变量 LIVE_VT_SYMBOL")
        gateway_setting = build_ctp_setting(env)
        strategy_setting = json.loads(args.setting)
        if args.history:
            strategy_setting["history_vt_symbol"] = args.history
        notifier = build_notifier(env)
        if gateway_loader is None:
            from vnpy_ctp import CtpGateway           # 延迟导入:无 CTP 库的平台上 --check 之外才会报错
            gateway_class = CtpGateway
        else:
            gateway_class = gateway_loader()
    except (ValueError, json.JSONDecodeError, ImportError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)              # 异常信息里只有变量名,不含值
        return 2

    admitted, admission_note = check_live_admission(
        gateway_setting["柜台环境"], is_simnow_address(gateway_setting["交易服务器"]),
        approval_path if approval_path is not None else bootstrap.ROOT / APPROVAL_RELATIVE)
    if not admitted:
        print(f"拒绝启动: {admission_note}", file=sys.stderr)
        return 2

    config = RuntimeConfig(
        vt_symbol=args.symbol,
        strategy_setting=strategy_setting,
        gateway_setting=gateway_setting,
        heartbeat_seconds=args.heartbeat,
        heartbeat_file=bootstrap.ROOT / "data" / "state" / "heartbeat",
        approved_file=args.approved or None,
        approved_dir=Path(args.approved_dir) if args.approved_dir else None,
        approvals_file=Path(args.approvals_file) if args.approvals_file else None,
        approved_strategy_class=args.approved_class,
    )
    if args.check:
        print(f"配置检查通过: 合约 {args.symbol},历史 {args.history or '(同合约)'},"
              f"通知 {'已配置' if notifier.enabled else '未配置'},账户风控: 当日 600 元 / 累计 2000 元")
        return 0

    runtime = runtime_factory(config, gateway_class, notifier=notifier)
    install_signal_handlers(runtime)
    return runtime.run()


if __name__ == "__main__":
    sys.exit(main())
