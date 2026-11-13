"""国信测试一键流程:检查 → 切档案 → 取证运行 → 生成报告 → 列出还要人工做的事。

用法(项目根目录或双击 scripts/guoxin_oneclick.cmd):
    python scripts/guoxin_oneclick.py                 # 完整流程(含 3.1 断网演练,需要你动手)
    python scripts/guoxin_oneclick.py --no-interactive   # 不做断网演练(3.1 标「待人工」)
    python scripts/guoxin_oneclick.py --resume data/broker_test/<目录>   # 在已有证据上补测并重新出报告
    python scripts/guoxin_oneclick.py --check-only    # 只做检查,不登录、不发单
    python scripts/guoxin_oneclick.py --rehearsal     # SimNow 预演:用 .env.simnow 把整条流程演一遍(报告不能交给国信)

安全(这几条是硬约束,改代码时别绕过):
1. 只放行国信测试环境(CTP_ENVIRONMENT=测试、BrokerID 8030、前置在国信给的地址表内);
   预演只放行 SimNow(地址在 SimNow 白名单内)。不通过就退出,不会连接。
2. 校验的是**运行时真正生效的环境**:进程环境变量里的 CTP_* 会覆盖 .env(dotenv override=False),
   所以档案里没有而进程里有、或者两者不一致,一律拒绝——否则会出现「文件校验通过、实际连到别处」。
3. 前置地址白名单会**下推到取证脚本**(BROKER_TEST_ALLOWED_TD/MD),运行期再校验一次,
   不只在一键流程里查一遍。
4. 一次流程只登录一次(国信会检查频繁登录),所以不自动重试。
5. 不打印账号、密码、授权码;跑完把原来的 .env 还回去。

退出码:0=全过 1=有失败 2=配置/环境错 3=登录失败 4=取证运行异常退出。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import bootstrap  # noqa: E402

    # 隔离必须发生在任何会导入 vn.py logger 的模块之前(否则日志/数据库会写进实盘目录)。
    bootstrap.isolate_state(".vntrader_brokertest")
    ROOT = bootstrap.ROOT
except Exception:  # noqa: BLE001  已被别的模块导入过(vnpy 已初始化)时不再隔离
    pass

from ctp_tools.broker_targets import (  # noqa: E402
    GUOXIN_TEST_BROKER_ID as EXPECTED_BROKER_ID,
    GUOXIN_TEST_MD as ALLOWED_MD,
    GUOXIN_TEST_TD as ALLOWED_TD,
    SIMNOW_BROKER_ID,
    SIMNOW_MD,
    SIMNOW_TD,
)
from ctp_tools.collector import in_trading_session  # noqa: E402
from ctp_tools.profile import _same_value, describe_env_value  # noqa: E402  (打印配置值的唯一脱敏出处)
from ctp_tools.simnow import normalize_address  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")
TEMPLATE_NAME = "国信期货有限责任公司外部接入系统技术测试报告20260717.docx"

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_LOGIN, EXIT_CRASH = 0, 1, 2, 3, 4
TEST_TIMEOUT = 1800        # 取证运行上限(200 笔稳定运行 + 各项等待,30 分钟足够)
REPORT_TIMEOUT = 300       # 出报告上限
# 这些键一旦在进程环境里出现,就会覆盖 .env 里的同名值(dotenv override=False),
# 所以它们必须参与「生效环境」的一致性校验。
GATED_KEYS = (
    "CTP_ENVIRONMENT", "CTP_BROKER_ID", "CTP_TD_ADDRESS", "CTP_MD_ADDRESS",
    "BROKER_TEST_ALLOWED_TD", "BROKER_TEST_ALLOWED_MD",
)
# 打印配置值的脱敏规则与凭据键表只住在 ctp_tools/profile.py(describe_env_value / SECRET_KEYS):
# 两份实现迟早漂移,而漂移的方向往往是「某一处开始打印原文」。


def read_env(path: Path) -> dict[str, str]:
    """用 python-dotenv 解析档案:引号/注释/BOM 的处理和运行时(dotenv)完全一致。

    自己手写 split("=") 会在这些地方和运行时产生分歧(闸门看到的值 ≠ 柜台拿到的值):
    带 BOM 的文件、KEY="v" 的引号、行内注释。
    """
    from dotenv import dotenv_values

    values = dotenv_values(path, encoding="utf-8-sig")
    return {k: v for k, v in values.items() if v is not None}


def effective_env(file_env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """算出运行时真正生效的环境,返回(生效环境, 冲突说明)。

    bootstrap.load_env 用的是 load_dotenv(override=False):进程里已有的变量优先,
    .env 只补缺。所以闸门必须校验合成后的结果,而不是只校验文件。

    冲突判定覆盖**所有** CTP_* 键和 LIVE_VT_SYMBOL,不只 GATED_KEYS:
    账号/密码/授权码同样会被进程环境变量盖掉,只校验地址不校验凭据,
    就会出现「连的是国信地址、登的是另一个账号」这种最难查的情况。
    """
    eff = dict(file_env)
    conflicts: list[str] = []
    for key, value in os.environ.items():
        if not (key in GATED_KEYS or key.startswith("CTP_") or key == "LIVE_VT_SYMBOL"):
            continue
        if key not in file_env:
            conflicts.append(f"{key} 只在进程环境里(={describe_env_value(key, value)}),"
                             "档案里没有——运行时它会盖住档案")
        elif not _same_value(key, file_env[key], value):
            conflicts.append(f"{key} 进程环境变量(={describe_env_value(key, value)})与档案"
                             f"(={describe_env_value(key, file_env[key])})不一致,运行时用进程里的")
        eff[key] = value
    return eff, conflicts


def _missing_credentials(env: dict[str, str]) -> list[str]:
    return [f"缺少 {key}" for key in ("CTP_USERNAME", "CTP_PASSWORD", "CTP_APP_ID", "CTP_AUTH_CODE", "LIVE_VT_SYMBOL")
            if not env.get(key)]


def check_env(env: dict[str, str]) -> list[str]:
    """返回不满足「国信测试环境」的问题列表;空 = 通过。"""
    problems = []
    if env.get("CTP_ENVIRONMENT") != "测试":
        problems.append(f"CTP_ENVIRONMENT 应为「测试」,当前 {env.get('CTP_ENVIRONMENT')!r}")
    if env.get("CTP_BROKER_ID") != EXPECTED_BROKER_ID:
        problems.append(f"CTP_BROKER_ID 应为 {EXPECTED_BROKER_ID},当前 {env.get('CTP_BROKER_ID')!r}")
    td = normalize_address(env.get("CTP_TD_ADDRESS", ""))
    md = normalize_address(env.get("CTP_MD_ADDRESS", ""))
    if td not in ALLOWED_TD:
        problems.append(f"交易前置 {env.get('CTP_TD_ADDRESS')!r} 不在国信测试地址表 {sorted(ALLOWED_TD)} 内")
    if md not in ALLOWED_MD:
        problems.append(f"行情前置 {env.get('CTP_MD_ADDRESS')!r} 不在国信测试地址表 {sorted(ALLOWED_MD)} 内")
    problems += _missing_credentials(env)
    return problems


def check_simnow_env(env: dict[str, str]) -> list[str]:
    """预演档案检查:只放行 SimNow(BrokerID 9999 + 交易/行情前置都在 SimNow 白名单内)。"""
    problems = []
    if env.get("CTP_BROKER_ID") != SIMNOW_BROKER_ID:
        problems.append(f"预演档案的 CTP_BROKER_ID 应为 {SIMNOW_BROKER_ID},当前 {env.get('CTP_BROKER_ID')!r}")
    if env.get("CTP_ENVIRONMENT") != "实盘":
        # SimNow 用的是生产版 API,所以柜台环境必须是「实盘」;写成「测试」会让
        # ctp_tools.penetration.check_target 走「测试环境免检地址」那条分支,闸门形同虚设。
        problems.append(f"预演档案的 CTP_ENVIRONMENT 应为「实盘」,当前 {env.get('CTP_ENVIRONMENT')!r}")
    td = normalize_address(env.get("CTP_TD_ADDRESS", ""))
    md = normalize_address(env.get("CTP_MD_ADDRESS", ""))
    if td not in SIMNOW_TD:
        problems.append(f"交易前置 {env.get('CTP_TD_ADDRESS')!r} 不是 SimNow 地址")
    if md not in SIMNOW_MD:
        problems.append(f"行情前置 {env.get('CTP_MD_ADDRESS')!r} 不是 SimNow 地址")
    problems += _missing_credentials(env)
    return problems


def in_session(now: datetime) -> bool:
    """是否在交易时段(北京时间)。时段表与 ctp_tools.collector 一致,含夜盘跨午夜到 02:30。"""
    return in_trading_session(now.replace(tzinfo=None))


def main_repo() -> Path | None:
    """worktree 里没有 .env/.env.guoxin/模板时,回主仓库(同级 invest 目录)找。"""
    candidate = ROOT.parent / "invest"
    return candidate if candidate.is_dir() and candidate.resolve() != ROOT.resolve() else None


def find_file(*relative: str) -> Path | None:
    for base in (ROOT, main_repo()):
        if base is not None:
            for rel in relative:
                if (base / rel).exists():
                    return base / rel
    return None


def python_exe() -> str:
    """优先项目 venv(worktree 没有就用主仓库的),否则当前解释器。"""
    for base in (ROOT, main_repo()):
        if base is not None:
            exe = base / ".venv" / "Scripts" / "python.exe"
            if exe.exists():
                return str(exe)
    return sys.executable


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def child_env(effective: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """给子进程准备环境变量。

    继承来的 CTP_*/LIVE_VT_SYMBOL **全部清掉**,再放回闸门校验过的那一份:
    闸门校验的是「档案 + 进程环境」合成后的结果,子进程必须拿到同一个结果,
    否则会出现「这里校验通过、子进程连到别处」。extra 里的是白名单等运行期参数,
    优先级最高。
    """
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CTP_") or k == "LIVE_VT_SYMBOL" or k in GATED_KEYS)}
    for key, value in effective.items():
        if key.startswith("CTP_") or key == "LIVE_VT_SYMBOL" or key in GATED_KEYS:
            env[key] = value
    env.update(extra)
    return env


_CURRENT_CHILD: subprocess.Popen | None = None


def terminate_child(grace: float = 30.0) -> None:
    """父进程被 Ctrl+C/终止时,先礼后兵地收掉还在跑的子进程。

    子进程用独立进程组启动(为了能给它单发 CTRL_BREAK),控制台的 Ctrl+C 到不了它;
    父进程一退,它就成了孤儿,继续在柜台上发单(200 笔稳定运行),没人收尾、没人撤单。
    """
    proc = _CURRENT_CHILD
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        print("子进程没有在限时内收尾,强制结束(柜台上可能留有委托,请人工核对)。", file=sys.stderr)
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def run_child(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> int:
    """跑一个子进程,超时**先礼后兵**。

    直接强杀(SIGKILL)的话,子进程里正在跑的收尾逻辑(撤单、登出、
    解除暂停标志)一个都不会执行 —— 柜台上可能留着演示委托,
    下一轮运行就带着脏状态开始。所以超时后先发中断信号,给它 30 秒自己收工,
    还不动才强杀。

    Windows 上没有 SIGINT 给别人的进程用,靠 `CREATE_NEW_PROCESS_GROUP` +
    `CTRL_BREAK_EVENT` 达到同样效果。
    """
    global _CURRENT_CHILD
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, creationflags=creationflags)
    except OSError as exc:
        print(f"启动子进程失败:{exc!r}", file=sys.stderr)
        return EXIT_CRASH
    _CURRENT_CHILD = proc
    try:
        return _wait_child(proc, timeout)
    finally:
        _CURRENT_CHILD = None


def _wait_child(proc: subprocess.Popen, timeout: float) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"子进程超过 {timeout:.0f}s 还没结束,先请它自己收尾(撤单/登出)。", file=sys.stderr)
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
    except (OSError, ValueError) as exc:
        print(f"发中断信号失败({exc!r}),直接终止。", file=sys.stderr)
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        print("子进程 30 秒内没有收尾,强制结束(柜台上可能留有委托,请人工核对)。", file=sys.stderr)
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return EXIT_CRASH


def mode_of(rehearsal: bool) -> str:
    return "rehearsal" if rehearsal else "guoxin"


def write_mode_marker(out_dir: Path, mode: str, env: dict[str, str]) -> None:
    """在证据目录里写下本次的模式与实际连接的前置,--resume 时据此拒绝模式混用。

    **补测时不覆盖已有的 mode.json**:那文件是这个目录的「身份」,
    覆盖它等于把原来那次运行的记录改掉(而且 created_at 会变成补测时间,
    看不出这份证据是什么时候产生的)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    marker_path = out_dir / "mode.json"
    if marker_path.exists():
        return
    marker = {
        "mode": mode,
        "broker_id": env.get("CTP_BROKER_ID", ""),
        "environment": env.get("CTP_ENVIRONMENT", ""),
        "td_front": normalize_address(env.get("CTP_TD_ADDRESS", "")),
        "md_front": normalize_address(env.get("CTP_MD_ADDRESS", "")),
        "created_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
    }
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


def check_resume_mode(resume_dir: Path, mode: str) -> list[str]:
    """--resume 的目录必须和当前模式一致,否则 SimNow 的结果会覆盖国信的证据。"""
    problems = []
    marker_path = resume_dir / "mode.json"
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("mode") != mode:
            problems.append(f"{resume_dir} 是「{marker.get('mode')}」模式的证据目录,不能拿「{mode}」模式去补测")
    else:
        if mode == "rehearsal":
            problems.append(f"{resume_dir} 没有 mode.json,无法确认它不是正式证据目录,预演模式拒绝在它上面补测")
        else:
            problems.append(f"⚠ {resume_dir} 没有 mode.json(旧目录),已按正式模式继续")
    if not (resume_dir / "evidence.json").exists():
        problems.append(f"{resume_dir} 里没有 evidence.json,不是取证证据目录")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-interactive", action="store_true", help="不做 3.1 断网演练")
    parser.add_argument("--resume", type=Path, default=None, help="在已有证据目录上补测")
    parser.add_argument("--check-only", action="store_true", help="只检查,不登录不发单")
    parser.add_argument("--rehearsal", action="store_true", help="SimNow 预演(.env.simnow):演练流程,产出的报告不能提交国信")
    parser.add_argument("--yes", action="store_true", help="跳过「前置地址确认」提示(非交互环境用)")
    parser.add_argument("--force", action="store_true", help="非交易时段也照样跑(会白耗一次券商登录额度)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    step("1/5 检查")
    now = datetime.now(BEIJING)
    print(f"北京时间 {now:%Y-%m-%d %H:%M}(本机时间可能不同,以北京时间为准)")
    if not in_session(now):
        print("⚠ 现在不在交易时段:发单类测试项(稳定运行、开平撤、撤单阈值、错误提示、暂停)会标「未测试」。"
              "建议北京时间 21:10–22:30(夜盘)或日盘开盘后再跑。")
        if not args.check_only and not args.force:
            if args.yes:
                print("已指定 --yes 但没有 --force:非交易时段不自动开跑(避免白耗一次登录额度)。请加 --force。")
                return EXIT_CONFIG
            if input("仍要继续?输入 y 继续:").strip().lower() != "y":
                return EXIT_CONFIG
    profile = ".env.simnow" if args.rehearsal else ".env.guoxin"
    label = "SimNow 预演档案" if args.rehearsal else "国信档案"
    source = find_file(profile)
    if source is None:
        print(f"找不到 {profile}({label})。", file=sys.stderr)
        return EXIT_CONFIG
    file_env = read_env(source)
    env, conflicts = effective_env(file_env)
    if conflicts:
        print(f"{label}检查未通过(进程环境变量与档案冲突),已退出(没有连接任何环境):", file=sys.stderr)
        for c in conflicts:
            print(f"  - {c}", file=sys.stderr)
        print("  处理:先清掉这些环境变量(set VAR= 后重开终端),再重跑。", file=sys.stderr)
        return EXIT_CONFIG
    problems = check_simnow_env(env) if args.rehearsal else check_env(env)
    if problems:
        print(f"{label}检查未通过,已退出(没有连接任何环境):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{label} {source} 检查通过:环境={env['CTP_ENVIRONMENT']} BrokerID={env['CTP_BROKER_ID']} "
          f"交易={env['CTP_TD_ADDRESS']} 行情={env['CTP_MD_ADDRESS']}")
    template = find_file(f"docs/{TEMPLATE_NAME}")
    if template is None:
        print(f"找不到报告模板 docs/{TEMPLATE_NAME}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"报告模板 {template}")
    if args.rehearsal:
        print("⚠ 预演模式:连的是 SimNow,不是国信测试环境。证据与报告只用来验证流程,**不能交给国信**;"
              "SimNow 的柜台错误文本、流控、成交规则都和国信不同。")
    if args.check_only:
        print("--check-only:到此为止。")
        return EXIT_OK

    if args.resume is not None:
        args.resume = args.resume.resolve()
        resume_problems = check_resume_mode(args.resume, mode_of(args.rehearsal))
        if any("不能拿" in p or "拒绝" in p or "没有 evidence.json" in p for p in resume_problems):
            print("补测目录检查未通过,已退出:", file=sys.stderr)
            for p in resume_problems:
                print(f"  - {p}", file=sys.stderr)
            return EXIT_CONFIG
        for p in resume_problems:
            print(p)

    step("2/5 切换档案")
    target = ROOT / ".env"
    original_env = target.read_bytes() if target.exists() else None
    wrote_env = {"done": False}

    def restore_env() -> None:
        """把 .env 还原成运行前的版本(幂等,多处调用都安全)。"""
        try:
            if original_env is None:
                # 运行前根本没有 .env:把本次写入的档案删掉,否则这台机器上所有
                # 走 bootstrap.load_env() 的脚本都会静默停在国信测试账号上。
                if wrote_env["done"] and target.exists():
                    target.unlink()
                    wrote_env["done"] = False
                    print("\n运行前没有 .env,已删除本次写入的档案。")
            elif target.exists() and target.read_bytes() != original_env:
                target.write_bytes(original_env)
                print("\n已把 .env 还原成运行前的版本。")
        except OSError as exc:      # 还原失败要吵,否则这台机器会静默停在国信账号上
            print(f"⚠ 还原 .env 失败({exc}),请手工把 .env 改回运行前的内容。", file=sys.stderr)

    # 被 Ctrl+C / kill 打断时也要还原:进程被 SIGKILL 时无法还原,
    # 所以额外把备份文件留好,便于事后手工恢复。
    def _on_signal(signum, _frame):        # noqa: ANN001
        terminate_child()          # 先收子进程(撤单/登出),再还原 .env,最后才退出
        restore_env()
        raise KeyboardInterrupt

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass
    if not args.yes:
        answer = input(f"请确认 {env['CTP_TD_ADDRESS']} " + ("是 SimNow 前置。确认输入 y:" if args.rehearsal else "是国信给的测试前置(不是 SimNow/生产)。确认输入 y:")).strip().lower()
        if answer != "y":
            print("未确认,已退出。")
            return EXIT_CONFIG
    if target.exists() and target.read_bytes() != source.read_bytes():
        backup = ROOT / f".env.prev.{now.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(target, backup)
        print(f"当前 .env 与{label}不同,已另存为 {backup.name}")
    wrote_env["done"] = True          # 先置位:复制到一半失败,也要把半截文件清理/还原掉
    if original_env is None:
        print("提示:本次运行前没有 .env;若进程被强杀,请手工删除项目根目录的 .env(它是国信档案的副本)。")
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        print(f"写入 {target} 失败({exc!r}),已退出(没有连接任何环境)。", file=sys.stderr)
        restore_env()
        return EXIT_CONFIG
    print(f"已写入 {target}(.env 被 gitignore,不会提交)")

    try:
        step(f"3/5 取证运行(会向{'SimNow 预演环境' if args.rehearsal else '国信测试环境'}发委托;"
             "脚本先确认空仓,有持仓则一笔不发)")
        out_dir = args.resume or (ROOT / "data" / "broker_test" / (("rehearsal-" if args.rehearsal else "") + now.strftime("%Y%m%d-%H%M%S")))
        write_mode_marker(out_dir, mode_of(args.rehearsal), env)
        cmd = [python_exe(), str(ROOT / "scripts" / "broker_test.py"), "--confirm-broker-test", "--out-dir", str(out_dir)]
        if args.rehearsal:
            # 预演连的是 SimNow,不是国信:取证运行器必须知道这一点,
            # 否则它会拒绝(它出的是国信的报告),或者产出「自称国信」的证据。
            cmd.append("--rehearsal")
        if args.resume:
            cmd = [python_exe(), str(ROOT / "scripts" / "broker_test.py"), "--confirm-broker-test", "--resume", str(args.resume)]
            if args.rehearsal:
                cmd.append("--rehearsal")
        if not args.no_interactive:
            cmd.append("--interactive")
            print("提示:运行到「3.1 连接断开监测」会让你按回车,然后请断开本机网络、看到提示后恢复。")
        # 前置地址白名单下推给取证脚本:运行期再校验一次,而不是只在一键流程里查一遍。
        allowed_td = ",".join(sorted(SIMNOW_TD if args.rehearsal else ALLOWED_TD))
        allowed_md = ",".join(sorted(SIMNOW_MD if args.rehearsal else ALLOWED_MD))
        # 档案里已经写了白名单就用档案的(子进程还会与代码里的可信全集求交集,只能收窄);
        # 没写才用这里的全集兜底。两边都有而不相等,子进程会把它判成硬冲突(预演档案
        # 只写了 :30001,而这里的 SimNow 全集有 4 个地址)。
        extra = {"PYTHONIOENCODING": "utf-8"}
        if not env.get("BROKER_TEST_ALLOWED_TD"):
            extra["BROKER_TEST_ALLOWED_TD"] = allowed_td
        if not env.get("BROKER_TEST_ALLOWED_MD"):
            extra["BROKER_TEST_ALLOWED_MD"] = allowed_md
        child = child_env(env, extra)
        code = run_child(cmd, ROOT, child, TEST_TIMEOUT)
        if code == EXIT_CRASH:
            print(f"取证运行超时或异常退出({TEST_TIMEOUT}s 上限),已终止。", file=sys.stderr)
        print(f"\n取证运行退出码 {code}(0=全过 1=有失败 2=配置错 3=登录失败 4=异常退出)")
        if code in (EXIT_CONFIG, EXIT_LOGIN):
            print("没有登录成功或配置有误,不生成报告。把上面的输出贴给我。", file=sys.stderr)
            return code
        if code not in (EXIT_OK, EXIT_FAILED):     # 4(崩溃/超时)或任何别的退出码:都按异常处理
            print("取证运行异常退出(崩溃/超时),不生成报告——半份证据不能当报告用。把上面的输出贴给我。", file=sys.stderr)
            return code

        step("4/5 生成报告")
        report_out = None
        if args.rehearsal:
            # 预演报告不能和正式报告同名同模板:模板正文里预填的是国信的 BrokerID/前置地址,
            # 生成出来会「自称」是国信环境。用独立文件名 + 正文水印标出这是预演。
            report_out = out_dir / f"预演-SimNow-不可交国信-{now.strftime('%Y%m%d-%H%M%S')}.docx"
        rcmd = [python_exe(), str(ROOT / "scripts" / "broker_report.py"), "--evidence", str(out_dir),
                "--template", str(template)]
        if report_out is not None:
            rcmd += ["--out", str(report_out), "--watermark", "SimNow 预演 · 非国信测试环境 · 不可提交国信"]
        rcode = run_child(rcmd, ROOT, child_env(env, {"PYTHONIOENCODING": "utf-8"}), REPORT_TIMEOUT)
        if rcode == EXIT_CRASH:
            print(f"报告生成超时({REPORT_TIMEOUT}s),证据仍在:", out_dir, file=sys.stderr)
            return EXIT_CRASH
        if rcode != 0:
            print("报告生成失败,证据仍在:", out_dir, file=sys.stderr)
            return rcode

        step("5/5 还需要你手动完成")
        if args.rehearsal:
            print(f"预演完成:{out_dir}\n流程跑通后,正式运行去掉 --rehearsal。预演产出的报告**不能**提交国信。")
            return EXIT_OK if code == EXIT_OK else EXIT_FAILED
        print(f"""证据与报告目录:{out_dir}
  1. 看上面「报告里还缺什么」;FAIL 项不要重跑(会多一次登录),把输出贴给我分析。
  2. 「未测试/待人工」项在交易时段用:python scripts/guoxin_oneclick.py --resume "{out_dir}"
  3. 国信原版采集自检工具(datacollectforWindows-1.2.0.exe,24576 字节,SHA-256 以 2b49e154 开头):
     在采集信息齐全的机器上运行并登录,然后通知国信期货技术部。
  4. 报告签字(第五页)+ 盖四处章(第五页、两份报告首页、骑缝章)。""")
        try:
            import os as _os

            _os.startfile(out_dir)           # type: ignore[attr-defined]  Windows:打开结果目录
        except (AttributeError, OSError):
            pass
        return EXIT_OK if code == EXIT_OK else EXIT_FAILED
    finally:
        terminate_child()          # 任何退出路径都不留孤儿子进程
        # 用完把原来的 .env 还回去:否则这台机器上别的运行会静默切到国信测试账号。
        restore_env()


if __name__ == "__main__":
    sys.exit(main())
