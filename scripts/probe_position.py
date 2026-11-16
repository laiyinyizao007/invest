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

安全:
- 只允许连**可信前置**(国信测试前置或 SimNow)。探针虽然不下单,但它要**登录**——
  登录就是把账号密码发给那台机器,所以前置地址必须过闸门(生产 API 不放行);
- 控制台输出脱敏:账号只留头尾,密码/授权码/AppID 不打印,网关日志里的敏感串也替换掉。

退出码:0=登录成功并完成查询 1=没登录成功(前置不可达/账号未授权) 2=配置或闸门不通过。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402  必须最先导入:隔离 vn.py 状态目录

PROBE_ROOT = bootstrap.ROOT / ".vntrader_probe"
ISOLATED = bootstrap.isolate_state(PROBE_ROOT.name)
if not ISOLATED:
    # 日志模块在设置 TEMP_DIR 之前就被导入时,日志会写进实盘目录。
    # 探针是只读的,但日志里可能有账号和资金信息,不能混进实盘目录。
    print("状态目录隔离未生效(vnpy 日志模块在设置 TEMP_DIR 之前已被导入),"
          "探针日志会写进实盘目录,拒绝运行。请直接用 python scripts/probe_position.py 启动。", file=sys.stderr)
    sys.exit(2)

from ctp_tools.broker_targets import allowed_for_broker  # noqa: E402
from ctp_tools.collector import Collector, check_positions, close_engine, direction_text  # noqa: E402
from ctp_tools.penetration import mask_account  # noqa: E402
from ctp_tools.profile import load_profile  # noqa: E402
from ctp_tools.simnow import normalize_address  # noqa: E402
from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.object import SubscribeRequest  # noqa: E402
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_LOG, EVENT_POSITION, EVENT_TICK  # noqa: E402

MILESTONES = ("交易服务器连接成功", "交易服务器授权验证成功", "交易服务器登录成功",
              "结算信息确认成功", "合约信息查询成功", "行情服务器连接成功", "行情服务器登录成功")

EXIT_OK, EXIT_LOGIN, EXIT_CONFIG = 0, 1, 2


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", default="", help="用指定档案(如 .env.guoxin);默认用项目 .env")
    parser.add_argument("--seconds", type=int, default=60, help="观察多久秒(默认 60)")
    return parser.parse_args(argv)


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """把密钥类字符串从输出里替换掉(复用脱敏的唯一出处,免得两处逻辑漂移)。"""
    from notify.redact import redact

    return redact(text, secrets)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    profile = args.profile
    seconds = args.seconds
    try:
        loaded = load_profile(profile, root=bootstrap.ROOT)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if loaded.hard_conflicts:
        print("进程环境变量与项目 .env 不一致(又没有指定档案),无法判断该连哪台机器,已退出:"
              "请清掉这些环境变量,或用 --profile 明确指定档案。", file=sys.stderr)
        for c in loaded.hard_conflicts:
            print(f"  - {c}", file=sys.stderr)
        return EXIT_CONFIG
    for c in loaded.notes:
        print(f"⚠ {c}(已取档案的值)", file=sys.stderr)
    env = loaded.env
    missing = [k for k in ("CTP_USERNAME", "CTP_PASSWORD", "CTP_BROKER_ID",
                            "CTP_TD_ADDRESS", "CTP_MD_ADDRESS") if not env.get(k)]
    if missing:
        print(f"档案里缺少 {', '.join(missing)},无法连接。", file=sys.stderr)
        return EXIT_CONFIG

    # 前置地址闸门:探针要登录,登录就是把账号密码发给那台机器。
    # 这里用**可信地址全集**当闸门(国信测试前置或 SimNow),不要求 .env 里另配白名单——
    # 探针是诊断工具,用途就是「试一下这台机器通不通」,让它必须先改配置才能用
    # 就没人用了。但生产 API 的地址一定不在全集里,所以闸门依然挡得住。
    allowed_td, allowed_md = allowed_for_broker(env.get("CTP_BROKER_ID", ""))
    if not allowed_td:
        # 不再退回「可信地址全集」:那会破坏 BrokerID 绑定(国信 BrokerID + SimNow 前置
        # 在取证/清场里被拒、在探针里却放行,三个工具对同一份配置的判断就不一致了)。
        print(f"经纪商代码 {env.get('CTP_BROKER_ID', '')!r} 没有绑定的可信前置地址表,拒绝连接"
              "(探针也要登录,不能连到没授权的机器)。", file=sys.stderr)
        return EXIT_CONFIG
    td_address = normalize_address(env["CTP_TD_ADDRESS"])
    md_address = normalize_address(env["CTP_MD_ADDRESS"])
    if td_address not in allowed_td:
        print(f"交易前置 {td_address} 不在可信前置地址表内,拒绝连接"
              "(探针也要登录,不能连到没授权的机器)。", file=sys.stderr)
        return EXIT_CONFIG
    if md_address not in allowed_md:
        print(f"行情前置 {md_address} 不在可信前置地址表内,拒绝连接。", file=sys.stderr)
        return EXIT_CONFIG

    secrets = tuple(s for s in (env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""),
                                env.get("CTP_APP_ID", ""), env.get("CTP_USERNAME", "")) if s)
    account = mask_account(env.get("CTP_USERNAME", ""))

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)
    event_engine.register(EVENT_POSITION, collector.on_position)
    event_engine.register(EVENT_ACCOUNT, collector.on_account)   # 持仓查询「没有行」时用账户回报证明柜台在应答

    from vnpy_ctp import CtpGateway
    main_engine.add_gateway(CtpGateway)
    setting = {
        "用户名": env["CTP_USERNAME"],
        "密码": env["CTP_PASSWORD"],
        "经纪商代码": env["CTP_BROKER_ID"],
        "交易服务器": env["CTP_TD_ADDRESS"],
        "行情服务器": env["CTP_MD_ADDRESS"],
        "产品名称": env.get("CTP_APP_ID", ""),
        "授权编码": env.get("CTP_AUTH_CODE", ""),
        "柜台环境": env.get("CTP_ENVIRONMENT", "实盘"),
    }
    print(f"档案 {profile or '(当前 .env)'} 交易前置 {setting['交易服务器']} "
          f"行情前置 {setting['行情服务器']} 柜台环境={setting['柜台环境']} "
          f"BrokerID={setting['经纪商代码']} 账号={account}")
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
        print(f"  {'✔' if hit else '✘'} {text}"
              + (f"  {_redact(hit[-1].split('| ', 1)[-1], secrets)}" if hit else ""))
    disconnects = [line for line in lines if "连接断开" in line]
    if disconnects:
        print(f"--- 断连 {len(disconnects)} 次,最近一条:"
              f"{_redact(disconnects[-1].split('| ', 1)[-1], secrets)}")

    symbol = env.get("LIVE_VT_SYMBOL", "")
    if logged_in:
        contract = collector.contract_of(symbol)
        if contract is not None:
            main_engine.subscribe(SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange), "CTP")
            time.sleep(10)
        held, connected = check_positions(main_engine, wait=10.0, early_exit=False, collector=collector)
        gateway = main_engine.get_gateway("CTP")
        td = getattr(gateway, "td_api", None)
        print(f"td_api.connect_status = {getattr(td, 'connect_status', None)}  "
              f"login_status = {getattr(td, 'login_status', None)}  check_positions 认为 connected={connected}")
        print(f"非零持仓条数 = {len(held)}")
        for h in held:
            print("  持仓:", h)
        for p in main_engine.get_all_positions():
            # 方向用 `direction_text` 打印固定中文:`.value` 是 gettext 翻译过的
            # (英文 locale 下是 'Long'),同一份持仓在不同机器上输出不一样,没法直接对比
            # (2026-11-16 codex 复核 L2:CHANGELOG 说「全部改用它」,这里漏了)。
            print(f"  vn.py 持仓: {p.vt_symbol} {direction_text(p.direction)} volume={p.volume} "
                  f"yd={p.yd_volume} frozen={p.frozen}")
        for a in main_engine.get_all_accounts():
            print(f"  资金: 余额={a.balance} 可用={a.available} 冻结={a.frozen}")
    else:
        print("未登录成功,下面的查询不可信,已跳过。")
    print("探针结束(未发任何委托)")
    close_engine(main_engine)
    # CTP 的原生线程是非守护线程,close_engine 超时后进程不会自己退出 → 强制退出。
    # 先 flush:os._exit 不会替我们刷缓冲。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(EXIT_OK if logged_in else EXIT_LOGIN)


if __name__ == "__main__":
    sys.exit(main())
