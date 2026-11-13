"""研究沙盒 CLI(阶段四 4a:人工驱动)。外层(人或将来的 Bisheng)与确定性内核之间唯一的通道。

子命令即端点白名单的 CLI 形态(docs/roadmap.md §2.2):
    data       列出可用数据段及其「测试段已消费」状态
    approve    人工批准候选策略(绑定代码哈希);backtest/gate 只运行已批准的代码
    gate       晋级门槛评估(15 分钟玉米,滚动验证段 + 成本翻倍 + 随机入场对照)
    backtest   对 pending_strategies/ 内的策略在训练/验证段做沙盒回测
    results    读取结果摘要与诊断书(不带 RUN_ID 则列出全部)

明确不存在:下单、查账户、改风控参数、连 CTP、访问测试段。输出一律是 JSON,便于程序化消费。
威胁模型见 scripts/research_sandbox.py 文件头:这是纵深防御而非沙盒,运行前须由人读过代码。
退出码:0 成功;2 被拒绝/参数错误;3 回测失败;124 超时。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import bootstrap
import research_config as rc
import research_sandbox as rs
from research_grid import MIN_AXIS_VALUES
from risk import constitution as const
from risk.report import render_report
from risk.trial_ledger import TrialLedgerError, record_trials, total_trials

CHILD_MARK = "RESEARCH_RESULT_JSON:"
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 0, 2, 3, 124
# gate 无法验证候选参数是怎么选出来的,只能提醒:参数若参考过验证窗口(2022-07 ~ 2023-12)的表现,该窗口就不再是样本外
PROTOCOL_NOTICE = ("样本外有效的前提:候选参数与 --plateau-file 必须在没看过滚动验证窗口(2022-07-01 ~ 2023-12-31)的情况下选定;"
                   "optimize_15m.py 默认的训练段(至 2022-12)与验证段(2023)都与这些窗口重叠;请用 optimize_15m.py --select-only(训练段止于 2022-06-30)选参。")


def settings() -> tuple[dict, dict]:
    """(配置, 解析后的路径)。每次调用现读,配置非法时抛 ConfigError(由 main 统一转为拒绝)。"""
    cfg = rc.load()
    return cfg, rc.resolve_paths(cfg, bootstrap.ROOT)


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def refuse(message: str) -> int:
    print(f"拒绝: {message}", file=sys.stderr)
    return EXIT_REFUSED


# ---------- 作业(CLI 与网关共用) ----------

def kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":      # venv 的 python.exe 是启动器,须杀整棵进程树
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


_PATH_LIKE = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:app|usr|home|tmp|root)/)[^\s\"')]*")
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b:?\s*(.*)$")


def describe_error(exc: BaseException) -> dict:
    """把策略运行时异常压成 {type, message}:取 traceback 末尾的异常行,抹掉本机路径,截断到 200 字符。

    注意 message 可能含已批准代码里 raise 出来的文本,属于「会进入 LLM 上下文的外部文本」,所以限长并只保留可打印字符。"""
    text = str(exc)
    name, msg = type(exc).__name__, text
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        m = _EXC_LINE.match(line)
        if m:
            name, msg = m.group(1), m.group(2)
            break
    msg = _PATH_LIKE.sub("<path>", msg)
    msg = "".join(ch for ch in msg if ch.isprintable())[:200]
    return {"type": name.split(".")[-1], "message": msg}


def trials_path(paths: dict) -> Path:
    return Path(paths["results_dir"]) / "trials.jsonl"


def prior_trials(paths: dict) -> int:
    """累计试验次数,供子进程计算 DSR。账本损坏则拒绝作业(fail-closed):少记试验等于放水。"""
    try:
        return total_trials(trials_path(paths))
    except TrialLedgerError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc


MIN_GRID_CELLS = MIN_AXIS_VALUES ** 3     # 合法网格至少 3 个参数 × 每个 ≥3 个取值


def charge_trials(paths: dict, kind: str, count: int) -> int:
    """在启动子进程**之前**先记账并返回新的累计数:超时、崩溃、后半段异常都不会让试验变成免费的。"""
    try:
        return record_trials(trials_path(paths), kind, count, at=time.time())
    except TrialLedgerError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc


def top_up_grid_trials(paths: dict, body: dict) -> None:
    """evaluate 已按最小网格预扣 MIN_GRID_CELLS;子进程回报的格子数更大时补记差额(更小的回报不可信,不退还)。"""
    trials = body.get("trials")
    if not isinstance(trials, dict):
        return
    cells = trials.get("grid_cells")
    if isinstance(cells, int) and not isinstance(cells, bool) and cells > MIN_GRID_CELLS:
        charge_trials(paths, "grid_cell", cells - MIN_GRID_CELLS)


class JobError(Exception):
    """作业被拒绝或失败。code 沿用 CLI 退出码;message 可安全回传给调用方(不含本机路径与密钥)。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def spawn_child(cmd: list[str], timeout: float) -> dict:
    """在受限环境(环境变量白名单)的子进程里运行,返回子进程回传的 JSON;失败抛 JobError。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", cwd=bootstrap.ROOT, env=rs.sandbox_env(os.environ))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        raise JobError(EXIT_TIMEOUT, f"超过 {timeout}s,已终止")
    line = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith(CHILD_MARK)), None)
    if proc.returncode != 0 or line is None:
        # 详细输出只留在本机 stderr;回传给调用方的 message 不含子进程原始输出(可能含本机路径)
        print("失败:" + chr(10) + (stderr or stdout)[-1500:], file=sys.stderr)
        raise JobError(EXIT_FAILED, "回测失败(详情见本机日志)")
    return json.loads(line[len(CHILD_MARK):])


NEEDS_APPROVAL = "策略代码尚未批准:请先读代码并执行 research_api.py approve --strategy <文件>(或本次加 --confirm-reviewed)"


def check_approval(path: Path, paths: dict, confirm: bool) -> None:
    """人工批准检查,未通过抛 JobError。confirm=True(仅本机 CLI 可传)等价于「我现在读过并批准」,会记录哈希。"""
    try:
        if confirm:
            rs.record_approval(path, paths["approvals_file"])
        elif not rs.is_approved(path, paths["approvals_file"]):
            raise JobError(EXIT_REFUSED, NEEDS_APPROVAL)
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))


def screen(path: Path, cfg: dict) -> list[str]:
    return rs.screen_source(path.read_text(encoding="utf-8"), cfg["screening"]["extra_forbidden_modules"],
                            cfg["screening"]["extra_forbidden_calls"])


def check_params_allowed(params: dict) -> None:
    """调用方给的设置字典不得包含受保护参数(risk_budget 等):否则风控底线可被请求方绕过。"""
    try:
        constitution = const.load()
    except const.ConstitutionError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc
    findings = const.check_params(params, constitution)
    if findings:
        raise JobError(EXIT_REFUSED, "拒绝执行: " + "; ".join(findings))


def check_candidate_class(candidate: type) -> None:
    """运行时校验候选类:必须继承模板,且不得重写白名单以外的模板成员(含类创建后打的补丁)。"""
    try:
        constitution = const.load()
        template = const.load_template(constitution)
    except const.ConstitutionError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc
    findings = const.check_class(candidate, template, constitution)
    if findings:
        raise JobError(EXIT_REFUSED, "拒绝执行: 候选类不符合不可变宪法:" + "; ".join(findings))


def import_candidate(path: Path, cls_name: str, sha256: str) -> type:
    """在子进程里载入候选类,并**统一**做完三层校验(哈希、静态筛查、运行时类校验)。

    三层校验必须走同一个入口:早先三个子进程各自复制一遍流程,结果 gate 入口漏掉了
    运行时类校验,整条 gate 路径只剩静态筛查一层。复制粘贴的防线会漏,这里收敛成一个函数。"""
    if rs.file_sha256(path) != sha256:                        # 批准之后文件被改动(TOCTOU):拒绝运行
        raise JobError(EXIT_REFUSED, "策略文件在批准后被改动,拒绝运行")
    if screen(path, rc.load()):
        raise JobError(EXIT_REFUSED, "静态筛查未通过")
    spec = importlib.util.spec_from_file_location("pending_strategy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from vnpy_ctastrategy import CtaTemplate
    candidate = getattr(module, cls_name, None)
    if not (isinstance(candidate, type) and issubclass(candidate, CtaTemplate)):
        raise JobError(EXIT_FAILED, f"{cls_name} 不存在或不是 CtaTemplate 子类")
    check_candidate_class(candidate)                          # 运行时再查一次:挡住批准后被替换的候选
    return candidate


def require_clean(path: Path, cfg: dict) -> None:
    findings = screen(path, cfg)
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))


def resolve_strategy(strategy: str, cfg: dict) -> Path:
    try:
        return rs.resolve_pending(strategy, bootstrap.ROOT, cfg["paths"]["pending_dir"])
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))


def data_report() -> list:
    import optimize
    import segment_ledger
    out = []
    for key in rs.KEYS:
        windows = optimize.windows_for(key)
        t_start, t_end = windows["test"]
        ledger_key = f"optimize:{key}:{t_start:%Y-%m-%d}~{t_end:%Y-%m-%d}"
        try:
            segment_ledger.check(ledger_key)
            consumed = False
        except segment_ledger.SegmentAlreadyUsed:
            consumed = True
        out.append({
            "key": key,
            "segments": {seg: [f"{windows[seg][0]:%Y-%m-%d}", f"{windows[seg][1]:%Y-%m-%d}"] for seg in rs.SEGMENTS},
            "test_segment_accessible_via_sandbox": False,
            "test_segment_consumed": consumed,
        })
    return out


def approve_job(strategy: str) -> dict:
    cfg, paths = settings()
    path = resolve_strategy(strategy, cfg)
    findings = screen(path, cfg)
    if findings:
        raise JobError(EXIT_REFUSED, "拒绝批准: 静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    try:
        sha = rs.record_approval(path, paths["approvals_file"])
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))
    return {"approved": path.name, "sha256": sha}


def review_job(strategy: str, input_fn=None, write=print) -> dict:
    """先看代码再批准:打印文件摘要、静态筛查结果和**带行号的完整源码**,再问一次 [y/N]。
    只有明确输入 y 才记录批准(绑定代码哈希);空输入、其它输入、stdin 已关闭一律视为不批准。
    静态筛查不过的直接拒绝,不提问。批准只能在本机由人触发,外层没有任何端点能调用它。"""
    cfg, paths = settings()
    path = resolve_strategy(strategy, cfg)
    source = path.read_text(encoding="utf-8")
    sha = rs.file_sha256(path)
    findings = screen(path, cfg)
    write(f"文件: {path.name}   大小: {len(source.encode('utf-8'))} 字节   sha256: {sha}")
    write("静态筛查: " + ("通过" if not findings else "未通过"))
    for finding in findings:
        write(f"  - {finding}")
    write("-" * 60)
    for number, line in enumerate(source.splitlines(), 1):
        write(f"{number:4d} | {line}")
    write("-" * 60)
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过,不予批准")
    try:
        if rs.is_approved(path, paths["approvals_file"]):
            write("该内容(同一哈希)已批准过。")
            return {"approved": True, "already": True, "sha256": sha, "name": path.name}
        try:
            answer = (input_fn or input)("读完以上代码,确认批准运行? [y/N] ")     # 调用时再取 input,便于替换
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            write("未批准。")
            return {"approved": False, "sha256": sha, "name": path.name}
        if rs.file_sha256(path) != sha:                       # 批准必须绑定到用户刚读过的内容
            raise JobError(EXIT_REFUSED, "文件在阅读期间被改动,未批准;请重新运行 review")
        rs.record_approval(path, paths["approvals_file"])
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))
    write("已批准(绑定到上面的 sha256;代码改动后需要重新批准)。")
    return {"approved": True, "already": False, "sha256": sha, "name": path.name}


def backtest_job(*, strategy: str, cls: str, key: str, segment: str, params: dict, timeout: float | None = None,
                 confirm: bool = False) -> dict:
    cfg, paths = settings()
    timeout = timeout if timeout is not None else cfg["backtest"]["timeout_seconds"]
    path = resolve_strategy(strategy, cfg)
    try:
        start, end = rs.segment_window(key, segment)
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))
    if not isinstance(params, dict):
        raise JobError(EXIT_REFUSED, "params 必须是 JSON 对象")
    check_params_allowed(params)
    require_clean(path, cfg)
    check_approval(path, paths, confirm)
    source = path.read_text(encoding="utf-8")

    cmd = [sys.executable, str(Path(__file__).resolve()), "_child", "--file", str(path), "--cls", cls,
           "--key", key, "--segment", segment, "--params", json.dumps(params), "--sha256", rs.file_sha256(path)]
    body = spawn_child(cmd, timeout)
    if "error" in body:                    # 策略自身报错是研究中的正常结果:记录下来供反馈,而不是当作网关故障
        result = {"kind": "backtest", "status": "strategy_error", "strategy_file": path.name,
                  "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "class": cls, "key": key,
                  "segment": segment, "params": params, "ran_at": datetime.now().isoformat(timespec="seconds"),
                  "error": body["error"]}
        save_with_report(result, paths)
        return result

    from risk.diagnosis import diagnose
    result = {
        "kind": "backtest", "strategy_file": path.name, "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "class": cls, "key": key, "segment": segment, "params": params,
        "window": [f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"], "ran_at": datetime.now().isoformat(timespec="seconds"),
        "stats": body["stats"], "diagnosis": diagnose(body["stats"], body["pnls"]),
    }
    save_with_report(result, paths)
    return result


def plateau_from_data(data: dict) -> tuple[bool, str]:
    """由参数网格得分(cell + scores)判定是否在高原上。"""
    try:
        cell = tuple(int(x) for x in data["cell"])
        scores = {(int(a), int(b), int(c)): float(v) for a, b, c, v in data["scores"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise rs.SandboxError(f"参数高原数据无法解析: {exc}") from exc
    from risk.plateau import parameter_on_plateau
    ok = parameter_on_plateau(scores, cell)
    return ok, "参数高原判定来自提供的网格得分" + ("" if ok else "(不在高原上)")


def load_plateau(path: str | None) -> tuple[bool, str]:
    """读取参数网格得分文件并判定;未提供时 fail-closed(按未通过处理)并说明原因。"""
    if not path:
        return False, "未提供 --plateau-file:参数高原未评估,按未通过处理"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise rs.SandboxError(f"--plateau-file 无法解析: {exc}") from exc
    ok, note = plateau_from_data(data)
    return ok, note.replace("提供的网格得分", "--plateau-file")


def gate_job(*, strategy: str, cls: str, params: dict, plateau: tuple[bool, str], random_runs: int | None = None,
             timeout: float | None = None, confirm: bool = False) -> dict:
    cfg, paths = settings()
    timeout = timeout if timeout is not None else cfg["gate"]["timeout_seconds"]
    random_runs = random_runs if random_runs is not None else cfg["gate"]["random_runs"]
    path = resolve_strategy(strategy, cfg)
    if not isinstance(params, dict):
        raise JobError(EXIT_REFUSED, "params 必须是 JSON 对象")
    low, high = rc.INT_BOUNDS[("gate", "random_runs")]
    if isinstance(random_runs, bool) or not isinstance(random_runs, int) or not low <= random_runs <= high:
        raise JobError(EXIT_REFUSED, f"random_runs 必须是 [{low}, {high}] 内的整数(配置边界)")
    check_params_allowed(params)
    on_plateau, plateau_note = plateau
    require_clean(path, cfg)
    check_approval(path, paths, confirm)
    source = path.read_text(encoding="utf-8")
    trials = charge_trials(paths, "gate_run", 1)      # 这次门槛评估本身算一次试验(先记账再跑,失败不退),账本为空也不给 0

    cmd = [sys.executable, str(Path(__file__).resolve()), "_gate_child", "--file", str(path), "--cls", cls,
           "--params", json.dumps(params), "--random-runs", str(random_runs),
           "--on-plateau", "1" if on_plateau else "0", "--sha256", rs.file_sha256(path),
           "--prior-trials", str(trials)]
    body = spawn_child(cmd, timeout)
    if "error" in body:
        result = {"kind": "gate", "status": "strategy_error", "passed": False, "strategy_file": path.name,
                  "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "class": cls, "key": "C15",
                  "params": params, "ran_at": datetime.now().isoformat(timespec="seconds"), "error": body["error"]}
        save_with_report(result, paths)
        return result
    result = {
        "kind": "gate", "strategy_file": path.name, "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "class": cls, "key": "C15", "params": params, "plateau_note": plateau_note,
        "protocol_notice": PROTOCOL_NOTICE,
        "ran_at": datetime.now().isoformat(timespec="seconds"), **body,
    }
    save_with_report(result, paths)
    return result


EVALUATE_NOTICE = ("evaluate 的样本外由服务端保证：参数只在选参窗口（2020-07-22 ~ 2022-06-30）上按候选声明的 param_grid 选出，"
                   "门槛评估只用其后的滚动验证窗口（2022-07 ~ 2023-12），两段不重叠；测试段不可达。")


def save_with_report(result: dict, paths: dict) -> None:
    """落盘前补上确定性中文报告(纯函数生成,出错也只是一行说明),并写入 run_id。"""
    result["report"] = render_report(result)
    result["run_id"] = rs.save_result(result, paths["results_dir"])


def evaluate_job(*, strategy: str, cls: str, random_runs: int | None = None, timeout: float | None = None,
                 confirm: bool = False) -> dict:
    """候选在类上声明 `param_grid`（三个参数、各至少 3 个递增取值）：先在选参窗口扫网格并判定参数高原，
    再用选出的参数在滚动验证窗口上做完整的晋级门槛评估。"""
    cfg, paths = settings()
    timeout = timeout if timeout is not None else cfg["gate"]["timeout_seconds"] + cfg["grid"]["timeout_seconds"]
    random_runs = random_runs if random_runs is not None else cfg["gate"]["random_runs"]
    path = resolve_strategy(strategy, cfg)
    low, high = rc.INT_BOUNDS[("gate", "random_runs")]
    if isinstance(random_runs, bool) or not isinstance(random_runs, int) or not low <= random_runs <= high:
        raise JobError(EXIT_REFUSED, f"random_runs 必须是 [{low}, {high}] 内的整数(配置边界)")
    require_clean(path, cfg)
    check_approval(path, paths, confirm)
    source = path.read_text(encoding="utf-8")
    prior = prior_trials(paths)                   # 先读后扣:子进程算 DSR 用 prior + 实际格子数,预扣的最小网格不能算两次
    charge_trials(paths, "grid_cell", MIN_GRID_CELLS)

    cmd = [sys.executable, str(Path(__file__).resolve()), "_evaluate_child", "--file", str(path), "--cls", cls,
           "--random-runs", str(random_runs), "--sha256", rs.file_sha256(path), "--prior-trials", str(prior)]
    body = spawn_child(cmd, timeout)
    if "error" not in body:
        top_up_grid_trials(paths, body)
    base = {"kind": "evaluate", "strategy_file": path.name, "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "class": cls, "key": "C15", "protocol_notice": EVALUATE_NOTICE,
            "ran_at": datetime.now().isoformat(timespec="seconds")}
    if "error" in body:
        result = {**base, "status": "strategy_error", "passed": False, "error": body["error"]}
    else:
        result = {**base, **body}
    save_with_report(result, paths)
    return result


def run_cli_job(func) -> int:
    """CLI 适配:作业成功则打印 JSON,JobError 则把消息打到 stderr 并返回对应退出码。"""
    try:
        emit(func())
        return EXIT_OK
    except (JobError, rs.SandboxError) as exc:
        code = getattr(exc, "code", EXIT_REFUSED)
        message = getattr(exc, "message", str(exc))
        print(("拒绝: " if code == EXIT_REFUSED else "") + message, file=sys.stderr)
        return code


def cmd_review(args) -> int:
    try:
        review_job(args.strategy)
    except (JobError, rs.SandboxError) as exc:
        print("拒绝: " + getattr(exc, "message", str(exc)), file=sys.stderr)
        return getattr(exc, "code", EXIT_REFUSED)
    return EXIT_OK


def cmd_data(_args) -> int:
    return run_cli_job(data_report)


def cmd_approve(args) -> int:
    return run_cli_job(lambda: approve_job(args.strategy))


def cmd_backtest(args) -> int:
    def job():
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as exc:
            raise JobError(EXIT_REFUSED, f"--params 不是合法 JSON: {exc}")
        return backtest_job(strategy=args.strategy, cls=args.cls, key=args.key, segment=args.segment, params=params,
                            timeout=args.timeout, confirm=args.confirm_reviewed)
    return run_cli_job(job)


def cmd_evaluate(args) -> int:
    return run_cli_job(lambda: evaluate_job(strategy=args.strategy, cls=args.cls, random_runs=args.random_runs,
                                            timeout=args.timeout, confirm=args.confirm_reviewed))


def cmd_gate(args) -> int:
    def job():
        try:
            params = json.loads(args.params)
            plateau = load_plateau(args.plateau_file)
        except json.JSONDecodeError as exc:
            raise JobError(EXIT_REFUSED, f"--params 不是合法 JSON: {exc}")
        return gate_job(strategy=args.strategy, cls=args.cls, params=params, plateau=plateau,
                        random_runs=args.random_runs, timeout=args.timeout, confirm=args.confirm_reviewed)
    return run_cli_job(job)


def run_gate_child(args) -> int:
    """子进程:在 15 分钟玉米数据的滚动验证段上做门槛评估。测试段不可达(research_gate 内断言)。"""
    import walk_forward_optimizer as wf
    from backtest_lib import DEFAULT_CAPITAL, load_bars_csv
    from research_gate import engine_runner, run_gate
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    from vnpy.trader.constant import Exchange
    path = Path(args.file)
    try:
        cls = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    bars = load_bars_csv(wf.DATA_FILE, wf.SYMBOL, Exchange.DCE)
    windows = [(w.name, w.valid_start, w.valid_end) for w in wf.build_windows()]
    try:
        result = run_gate(candidate_cls=cls, params=json.loads(args.params), windows=windows, bars=bars,
                          runner=engine_runner(wf.KEY), random_cls=RandomEntry15mStrategy, random_runs=args.random_runs,
                          data_adjusted=True, on_plateau=args.on_plateau == "1", capital=DEFAULT_CAPITAL,
                          prior_trials=args.prior_trials)
    except Exception as exc:
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    print(CHILD_MARK + json.dumps(result, ensure_ascii=False))
    return EXIT_OK


def run_evaluate_child(args) -> int:
    """子进程：加载候选，读取其 param_grid，在选参窗口选参，再在滚动验证窗口做门槛评估。测试段不可达。"""
    import walk_forward_optimizer as wf
    from backtest_lib import DEFAULT_CAPITAL, load_bars_csv
    from research_gate import engine_runner, run_evaluation
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    from vnpy.trader.constant import Exchange
    path = Path(args.file)
    try:
        candidate = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    bars = load_bars_csv(wf.DATA_FILE, wf.SYMBOL, Exchange.DCE)
    windows = wf.build_windows()
    try:
        result = run_evaluation(
            candidate_cls=candidate, bars=bars, runner=engine_runner(wf.KEY), random_cls=RandomEntry15mStrategy,
            random_runs=args.random_runs, capital=DEFAULT_CAPITAL, max_cells=cfg["grid"]["max_cells"],
            min_round_trips=cfg["grid"]["min_round_trips"], select_window=(windows[0].train_start, windows[0].train_end),
            validation_windows=[(w.name, w.valid_start, w.valid_end) for w in windows], prior_trials=args.prior_trials)
    except Exception as exc:                  # 含 GridError：声明有问题也是候选自己的问题，作为结果回传
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    print(CHILD_MARK + json.dumps(result, ensure_ascii=False))
    return EXIT_OK


def run_child(args) -> int:
    """子进程:加载策略并回测。只在受限环境(sandbox_env)里由 cmd_backtest 拉起。"""
    from backtest_lib import run_backtest
    from strategies.instruments import INSTRUMENTS
    start, end = rs.segment_window(args.key, args.segment)
    try:
        strategy_class = import_candidate(Path(args.file), args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        stats, _, trades = run_backtest(strategy_class, json.loads(args.params), args.key, start, end)
    except Exception as exc:               # 策略自己的 bug:回传结构化摘要(不含路径),退出码仍为 0
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    stats = stats or {}
    row = {
        "return_pct": stats.get("total_return", 0.0), "maxdd_pct": stats.get("max_ddpercent", 0.0),
        "maxdd_yuan": stats.get("max_drawdown", 0.0), "sharpe": stats.get("sharpe_ratio", 0.0),
        "round_trips": int(stats.get("total_trade_count", 0)) // 2, "end_balance": stats.get("end_balance", 0.0),
        "net_pnl": stats.get("total_net_pnl", 0.0),
        "total_cost": stats.get("total_commission", 0.0) + stats.get("total_slippage", 0.0),
    }
    pnls = rs.round_trip_pnls(trades, INSTRUMENTS[args.key].size)
    print(CHILD_MARK + json.dumps({"stats": row, "pnls": pnls}))
    return EXIT_OK


# ---------- results ----------

def cmd_results(args) -> int:
    _, paths = settings()
    try:
        emit(rs.load_result(args.run_id, paths["results_dir"]) if args.run_id else rs.list_results(paths["results_dir"]))
    except rs.SandboxError as exc:
        return refuse(str(exc))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("data", help="列出数据段与测试段消费状态").set_defaults(func=cmd_data)
    bt = sub.add_parser("backtest", help="沙盒回测(仅训练/验证段)")
    bt.add_argument("--strategy", required=True, help="pending_strategies/ 内的 .py 文件")
    bt.add_argument("--class", dest="cls", required=True, help="策略类名")
    bt.add_argument("--key", required=True, choices=rs.KEYS)
    bt.add_argument("--segment", required=True, choices=rs.SEGMENTS)
    bt.add_argument("--params", default="{}", help="策略参数 JSON 对象")
    bt.add_argument("--timeout", type=float, default=None, help="秒;缺省取配置 backtest.timeout_seconds")
    bt.add_argument("--confirm-reviewed", action="store_true", help="我现在读过代码并批准(等价于先执行 approve)")
    bt.set_defaults(func=cmd_backtest)
    gt = sub.add_parser("gate", help="晋级门槛评估(15 分钟玉米,滚动验证段;不触碰测试段)")
    gt.add_argument("--strategy", required=True)
    gt.add_argument("--class", dest="cls", required=True)
    gt.add_argument("--params", default="{}")
    gt.add_argument("--random-runs", type=int, default=None, help="随机入场对照次数;缺省取配置 gate.random_runs(种子 0..N-1)")
    gt.add_argument("--plateau-file", default=None, help="参数网格得分 JSON;缺省则参数高原按未通过处理")
    gt.add_argument("--timeout", type=float, default=None, help="秒;缺省取配置 gate.timeout_seconds")
    gt.add_argument("--confirm-reviewed", action="store_true")
    gt.set_defaults(func=cmd_gate)
    ev = sub.add_parser("evaluate", help="选参 + 晋级门槛评估(候选在类上声明 param_grid;15 分钟玉米;不触碰测试段)")
    ev.add_argument("--strategy", required=True)
    ev.add_argument("--class", dest="cls", required=True)
    ev.add_argument("--random-runs", type=int, default=None, help="随机入场对照次数;缺省取配置 gate.random_runs")
    ev.add_argument("--timeout", type=float, default=None, help="秒;缺省取配置 gate.timeout_seconds + grid.timeout_seconds")
    ev.add_argument("--confirm-reviewed", action="store_true")
    ev.set_defaults(func=cmd_evaluate)
    rv = sub.add_parser("review", help="先看代码再批准:显示带行号的源码与静态筛查结果,输入 y 才批准")
    rv.add_argument("--strategy", required=True)
    rv.set_defaults(func=cmd_review)
    ap = sub.add_parser("approve", help="人工批准:读过代码后执行,批准绑定到代码内容的 sha256")
    ap.add_argument("--strategy", required=True)
    ap.set_defaults(func=cmd_approve)
    rr = sub.add_parser("results", help="读取结果与诊断书")
    rr.add_argument("run_id", nargs="?", default="")
    rr.set_defaults(func=cmd_results)
    child = sub.add_parser("_child")           # 内部使用
    for name in ("file", "cls", "key", "segment", "params", "sha256"):
        child.add_argument(f"--{name}", required=True)
    child.set_defaults(func=run_child)
    gchild = sub.add_parser("_gate_child")     # 内部使用
    for name in ("file", "cls", "params", "on-plateau", "sha256"):
        gchild.add_argument(f"--{name}", required=True)
    gchild.add_argument("--random-runs", type=int, required=True)
    gchild.add_argument("--prior-trials", type=int, default=None)
    gchild.set_defaults(func=run_gate_child)
    echild = sub.add_parser("_evaluate_child")  # 内部使用
    for name in ("file", "cls", "sha256"):
        echild.add_argument(f"--{name}", required=True)
    echild.add_argument("--random-runs", type=int, required=True)
    echild.add_argument("--prior-trials", type=int, default=None)
    echild.set_defaults(func=run_evaluate_child)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except rc.ConfigError as exc:
        return refuse(f"配置非法,拒绝运行: {exc}")


if __name__ == "__main__":
    sys.exit(main())
