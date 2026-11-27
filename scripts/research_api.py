"""研究沙盒 CLI(阶段四 4a:人工驱动)。外层(人或将来的 Bisheng)与确定性内核之间唯一的通道。

子命令即端点白名单的 CLI 形态(docs/roadmap.md §2.2):
    data       列出可用数据段及其「测试段已消费」状态
    approve    人工批准候选策略(绑定代码哈希);backtest/gate 只运行已批准的代码
    gate       晋级门槛评估(15 分钟玉米,滚动验证段 + 成本翻倍 + 随机入场对照)
    backtest   对 pending_strategies/ 内的策略在训练/验证段做沙盒回测
    results    读取结果摘要与诊断书(不带 RUN_ID 则列出全部)

明确不存在:下单、查账户、改风控参数、连 CTP、访问测试段。输出一律是 JSON,便于程序化消费。
威胁模型见 scripts/research_sandbox.py 文件头:这是纵深防御而非沙盒,运行前须由人读过代码。
退出码:0 成功;1 未预期异常(traceback);2 被拒绝/参数错误;3 回测失败;124 超时。
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
import types
from datetime import datetime
from pathlib import Path

import research_config as rc
import research_console as rconsole
import research_sandbox as rs
from research_grid import MIN_AXIS_VALUES
from risk import constitution as const
from risk.report import backtest_stats_row, render_report
from risk.trial_ledger import TrialLedgerError, record_trials, total_trials

CHILD_MARK = "RESEARCH_RESULT_JSON:"
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 0, 2, 3, 124
# gate 无法验证候选参数是怎么选出来的,只能提醒:参数若参考过验证窗口(2022-07 ~ 2023-12)的表现,该窗口就不再是样本外
PROTOCOL_NOTICE = ("样本外有效的前提:候选参数与 --plateau-file 必须在没看过滚动验证窗口(2022-07-01 ~ 2023-12-31)的情况下选定;"
                   "optimize_15m.py 默认的训练段(至 2022-12)与验证段(2023)都与这些窗口重叠;请用 optimize_15m.py --select-only(训练段止于 2022-06-30)选参。")


def settings() -> tuple[dict, dict]:
    """(配置, 解析后的路径)。每次调用现读,配置非法时抛 ConfigError(由 main 统一转为拒绝)。"""
    cfg = rc.load()
    return cfg, rc.resolve_paths(cfg, rc.ROOT)


def _model_visible_secrets() -> tuple[str, ...]:
    """CLI 这一条通道要**按值**盖掉的凭据(第 12 轮 codex 的 M5)。

    清洗规则本来按**名字**认凭据(`CTP_PASSWORD=…`),可候选一旦把令牌**连名字一起**
    写进异常文本(或者只写值),名字那一层就认不出来——按值替换是最后一道。
    网关与 MCP 两条通道原先都有这一层,只有 CLI 没有,于是同一段文本在网关/MCP 被盖住、
    在 CLI 的 stdout 上原样出去。

    清单本体在 `research_config.known_secret_values`:三条模型可见通道(网关 / MCP / CLI)
    **共用同一份**(第 14 轮 codex 的 M3/L10)——原先 CLI 与网关只有网关令牌、
    没有毕昇开放 API 密钥,MCP 才有,于是「谁漏了就是谁漏了」。
    读不到令牌时**在 stderr 留一行痕**(第 13 轮 codex 的 L2):静默降级会让人以为
    「按值那一层还在」。stderr 不是模型可见通道(交互提示语与哈希只走 stderr),
    所以这行不影响脱敏口径。
    """
    return rc.known_secret_values(label="research-api")


def emit(obj) -> None:
    """CLI 的**唯一 stdout 出口**:出去的 JSON 一律先过模型可见通道的清洗。

    为什么 stdout 也要洗(第 7 轮 codex 的 F2):CLI 的 stdout 是「模型可见通道的候选」——
    毕昇工作流将来完全可能直接调它。原先这里只做 `json.dumps`,于是
    `strategy_sha256` 与结果文件里的哈希原样出去:模型拿到哈希就能在聊天里替人念哈希,
    「人核对完整 64 位哈希」当场退化成橡皮章。

    人要看的哈希与源码走 **stderr**(approve/review 已经这么做了),stdout 只留 JSON。
    """
    if isinstance(obj, dict):
        obj = rs.sanitize_result_for_model(obj)               # 候选自己写的报错文字不出 CLI stdout(Issue #94)
    print(json.dumps(rs.scrub_model_visible(obj, secrets=_model_visible_secrets()),
                     ensure_ascii=False, indent=2))


def refuse(message: str) -> int:
    print(f"拒绝: {message}", file=sys.stderr)
    return EXIT_REFUSED


# ---------- 作业(CLI 与网关共用) ----------

def kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":      # venv 的 python.exe 是启动器,须杀整棵进程树
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b:?\s*(.*)$")


def describe_error(exc: BaseException) -> dict:
    """把策略运行时异常压成 {type, message}:取 traceback 末尾的异常行,洗掉敏感内容,截断到 200 字符。

    注意 message 可能含已批准代码里 raise 出来的文本,属于「会进入 LLM 上下文的外部文本」,
    所以限长并只保留可打印字符;清洗走共享口径 `rs.scrub_text`(本机路径、哈希、凭据、批准命令),
    免得这里再有一套更弱的正则(第 7 轮 codex 的 F5)。"""
    text = str(exc)
    name, msg = type(exc).__name__, text
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        m = _EXC_LINE.match(line)
        if m:
            name, msg = m.group(1), m.group(2)
            break
    msg = rs.scrub_text(msg)
    msg = "".join(ch for ch in msg if ch.isprintable())[:200]
    return {"type": name.split(".")[-1], "message": msg}


def trials_path(paths: dict) -> Path:
    return Path(paths["results_dir"]) / "trials.jsonl"


def check_product(product) -> None:
    """评估品种只能取 walk_forward_optimizer.PRODUCTS 白名单里的键(Issue #69);其余一律拒绝。"""
    # 只认 research_sandbox.PRODUCTS(无交易栈依赖):父进程/网关里不得 import walk_forward_optimizer(它会拖进 vnpy)
    if not isinstance(product, str) or product not in rs.PRODUCTS:
        raise JobError(EXIT_REFUSED, f"product 只能是 {list(rs.PRODUCTS)}")


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


class CandidateError(Exception):
    """候选代码自身在**导入期**出的错(语法之外的运行时错误、缺模块、模块级副作用报错)。

    与 JobError 分开:JobError 是"宪法/配置拒绝了这份候选"(退出码 2),
    CandidateError 是"这份候选自己跑不起来"(属于研究中的正常结果)。
    两者都走结构化回传,免得 LLM 那条反馈通道因为一个 import 错误就断掉——
    早先这类异常直接带 traceback 退出 1,父进程只报"回测失败",候选拿不到诊断信息。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def _child_reason(stderr: str) -> str:
    """从子进程的 stderr 里取出拒绝原因(最后一行非空输出)。

    子进程的"拒绝"(退出码 2)必须**原样传到父层**:早先一律压成"回测失败(退出码 3)",
    于是子入口里那些拒绝(受保护参数、路径越界、审批缺失)的原因只进了本机 stderr,
    永远进不了返回给调用方/LLM 的结果里——而那条通道正是为"把诊断送回去"设计的。

    回传前走**唯一**的清洗口径 `rs.scrub_text`:本机路径、凭据、哈希、批准命令都盖掉。
    早先这里自带三套正则(盘符/POSIX/引号),是第四套口径,迟早与主线漂移
    (第 10 轮 codex 的 Low)。
    语义变化(第 10 轮 codex 的 Low):旧实现会把引号里的**一切**整段替换成 `<路径>`;
    现在统一走 `rs.scrub_text`,只认路径/哈希/凭据这些**形态**,引号里的普通文本不再被吞
    (写在这里免得日后当成回归)。
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return "子进程拒绝执行(原因见本机日志)"
    return rs.scrub_text(lines[-1])[:200]


def spawn_child(cmd: list[str], timeout: float) -> dict:
    """在受限环境(环境变量白名单)的子进程里运行,返回子进程回传的 JSON;失败抛 JobError。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", cwd=rc.CODE_ROOT, env=rs.sandbox_env(os.environ))
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
        if proc.returncode == EXIT_REFUSED:
            # 子进程的"拒绝"要**原样传到父层**,不能一律压成"回测失败":
            # 子入口自己复查的那些项(受保护参数、路径越界、审批缺失)的原因必须能回到调用方。
            raise JobError(EXIT_REFUSED, "拒绝执行:" + _child_reason(stderr))
        raise JobError(EXIT_FAILED, "回测失败(详情见本机日志)")
    return json.loads(line[len(CHILD_MARK):])


# **这条消息会经网关回到模型**,所以里面不能有批准命令(第 6 轮 codex 的 M2/B 的 M):
# 命令一旦交回模型,模型就能诱导人去跑终端里的批准流程——那条路上没有
# 「只批本轮候选」的约束。人要看的命令另走 stderr(见 APPROVAL_HINT 与 run_cli_job)。
NEEDS_APPROVAL = ("策略代码尚未批准:请让本机的人完成人工批准"
                  "(带外批准页,或本机终端里的 approve 子命令);"
                  "批准绑定代码哈希,代码改动后需重新批准;模型不能代替人批准。")

# 人看的提示:只在**本机 stderr** 出(CLI 拒绝时),不进模型通道。
APPROVAL_HINT = ("本机人工批准:在交互式终端里运行 "
                 "python scripts/research_api.py approve --strategy <文件>"
                 "(粘贴完整 sha256 确认;或本次加 --confirm-reviewed)")


def _prompt_hash(sha: str) -> str:
    """请人把完整哈希粘贴回来(提示语走 stderr,stdout 只出 JSON)。

    单独抽出来是为了能单测:确认动作要能验「哈希不匹配就不批准」。
    """
    print(f"本次候选 sha256: {sha}", file=sys.stderr)
    print("把上面的 sha256 完整粘贴回来以确认批准: ", end="", file=sys.stderr)
    try:
        return input("")
    except (EOFError, OSError):
        return ""


def check_approval(path: Path, paths: dict, confirm: bool, sha256: str | None = None) -> None:
    """人工批准检查,未通过抛 JobError。confirm=True(仅本机 CLI 可传)等价于「我现在读过并批准」,会记录哈希。

    `sha256` 是**已经读出来的那份字节**的哈希:父进程只读一次候选文件,
    审批检查、静态筛查、传给子进程的哈希、结果里记的哈希全部用这同一个值——
    否则"审批时看到的""筛查时看到的""执行时校验的""结果里记的"可能是四份不同的内容。"""
    try:
        if confirm:
            # `--confirm-reviewed` 是「人刚读过并批准」的快捷方式,只在**交互式终端**里有效:
            # 非 TTY 时任何程序都能自己带上这个开关把批准做掉(第 8 轮 codex 的 H1)。
            if not sys.stdin.isatty():
                raise JobError(EXIT_REFUSED, "--confirm-reviewed 只能在交互式终端里使用"
                                            "(自动化调用不得批准代码);"
                                            "请改用 approve 子命令粘贴完整 sha256。")
            # TTY 只是「有个控制台」,不是「有人在」:Windows 用 CREATE_NEW_CONSOLE、
            # POSIX 用 pty.spawn 都能伪造出 isatty=True。
            # 所以这里也要**粘贴完整哈希**——与 approve/review 同一条口径
            # (第 7 轮 codex 的 F7)。
            sha = str(sha256 or rs.file_sha256(path)).strip().lower()
            answer = _prompt_hash(sha)
            if answer.strip().lower() != sha:
                raise JobError(EXIT_REFUSED, "哈希不匹配,未批准。")
            # 记的是**已经读出来并核对过的那份**的哈希:不在这里重读文件,
            # 免得"核对的内容"与"记下的内容"之间出现时间差(TOCTOU)。
            rs.record_approval(path, paths["approvals_file"], sha256=sha, via="cli")
        elif sha256 is not None:
            if not rs.is_approved_sha_any(sha256, paths["approvals_file"], paths.get("auto_approvals_file")):
                raise JobError(EXIT_REFUSED, NEEDS_APPROVAL)
        elif not rs.is_approved_sha_any(rs.file_sha256(path), paths["approvals_file"], paths.get("auto_approvals_file")):
            raise JobError(EXIT_REFUSED, NEEDS_APPROVAL)
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))


def read_candidate(path: Path) -> tuple[bytes, str]:
    """读候选文件:返回(原始字节, 源码文本)。

    **读一次,各处都用同一份**:
    - 解码统一 `utf-8-sig`:带 BOM 的候选(Windows 编辑器常见)在标准 loader 下能正常导入,
      若按纯 `utf-8` 解码,BOM 会留在字符串里让 `ast.parse` 报"语法错误"——
      于是同一份带 BOM 的候选在父入口被拒、在子入口却能过,两处行为不一致(第 6 轮 codex 实测)。
    - 哈希、筛查、编译、执行都基于这同一份字节,分开打开文件会留下 TOCTOU 窗口。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        # 同样不带路径(exc 里通常带文件名);细节留在本机日志里
        raise JobError(EXIT_REFUSED, "策略文件无法读取或不可读") from None
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise JobError(EXIT_REFUSED, f"策略文件不是合法的 UTF-8: {exc}") from exc


def screen(path: Path, cfg: dict) -> list[str]:
    """静态筛查。与 `import_candidate` 共用同一个读取器,免得两处解码口径不一致。"""
    _, source = read_candidate(path)
    return rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                            cfg["screening"]["extra_forbidden_calls"],
                            cfg["screening"]["allowed_modules"])


def check_params_allowed(params: dict) -> None:
    """调用方给的设置字典不得包含受保护参数(risk_budget 等):否则风控底线可被请求方绕过。"""
    try:
        constitution = const.load()
    except const.ConstitutionError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc
    findings = const.check_params(params, constitution)
    if findings:
        raise JobError(EXIT_REFUSED, "拒绝执行: " + "; ".join(findings))


def child_params(args) -> dict:
    """子进程自己解析并校验参数。

    子入口(`_child` / `_gate_child`)是**独立的信任边界**:它能被直接调用,
    不能假设父进程已经查过参数——否则绕过父入口直接传 `--params '{"risk_budget": 1e9}'`
    就能改写风控底线。"""
    try:
        params = json.loads(args.params)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JobError(EXIT_REFUSED, f"--params 不是合法 JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise JobError(EXIT_REFUSED, "--params 必须是 JSON 对象")
    check_params_allowed(params)
    return params


def child_int_args(args) -> tuple[int, int]:
    """子进程自己校验两个整数参数:`--random-runs` 与 `--prior-trials`。

    这两个也是调用方给的值,不能假设父进程查过:
    - `--random-runs` 越界会让子进程跑巨量随机入场对照(子进程自身没有超时,超时只由父进程兜),
      直接调用即可拖资源;
    - `--prior-trials` 为负更糟:evaluate 路径会先做 `prior + 格子数` 再算 DSR,
      负数会让 N 变小,而 DSR 随 N 增大而变严——于是**门槛被放松**。
      (gate 路径不加分号,负数会被 candidate_eval 的 `n_trials >= 1` 拦下,evaluate 路径漏了。)

    返回 (random_runs, prior_trials);prior_trials 为 None 表示不参与 DSR。"""
    random_runs = getattr(args, "random_runs", None)
    low, high = rc.INT_BOUNDS[("gate", "random_runs")]
    if isinstance(random_runs, bool) or not isinstance(random_runs, int) or not low <= random_runs <= high:
        raise JobError(EXIT_REFUSED, f"--random-runs 必须是 [{low}, {high}] 内的整数(配置边界)")
    prior = getattr(args, "prior_trials", None)
    if prior is not None:
        if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
            raise JobError(EXIT_REFUSED, "--prior-trials 必须是非负整数")
    return random_runs, prior


def child_file(path: Path) -> Path:
    """子进程自己确认候选文件仍在 pending 目录内。

    父入口用 `resolve_pending` 解析过路径,但子入口能被直接调用,
    不该假设路径是父进程给的——否则可以直接把 `--file` 指到仓库里任何 .py。"""
    cfg = rc.load()
    try:
        return rs.resolve_pending(str(path), rc.ROOT, cfg["paths"]["pending_dir"])
    except rs.SandboxError:
        # 消息里不带路径:拒绝原因会回传给调用方,而回传的约定是不含本机路径
        raise JobError(EXIT_REFUSED, "策略文件不在 pending_strategies/ 目录内") from None


def child_approved(sha256: str) -> None:
    """子进程自己查审批记录(按**哈希**查,不再读一遍文件)。

    `--sha256` 只证明"文件没变",**不证明"人看过"**——哈希谁都能自己算。
    子入口能被直接调用,不查审批就能跑一份从未被人审阅过的候选,
    而"运行前由人读过代码"正是这套设计最核心的前提(威胁模型的第一条)。
    审批记录按内容哈希存,所以文件一改就自动失效。

    查的是**父进程算好的那个哈希**:`import_candidate` 随后会再读一次文件并校验
    "当前内容 == 这个哈希",所以被执行的字节一定就是被批准的那一份——
    不必为了查审批再读一遍文件(两处读之间文件若被改,查的就是另一个版本的内容)。"""
    _, paths = settings()
    try:
        if not rs.is_approved_sha_any(sha256, paths["approvals_file"], paths.get("auto_approvals_file")):
            raise JobError(EXIT_REFUSED, NEEDS_APPROVAL)
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc


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
    运行时类校验,整条 gate 路径只剩静态筛查一层。复制粘贴的防线会漏,这里收敛成一个函数。

    哈希、筛查、编译、执行**必须基于同一份字节**:分开打开文件会留下 TOCTOU 窗口,
    而用 `spec.loader.exec_module()` 载入时,Python 可能从 `__pycache__` 里取时间戳有效的旧 `.pyc`
    ——那意味着人工批准的、算过哈希的、过了静态筛查的源码,和实际执行的字节码**不是一回事**。
    所以这里只读一次字节,再对同一份内容做完所有事。"""
    raw, source = read_candidate(path)                      # 读一次,哈希与筛查都用这同一份
    if hashlib.sha256(raw).hexdigest() != sha256:            # 批准之后文件被改动(TOCTOU):拒绝运行
        raise JobError(EXIT_REFUSED, "策略文件在批准后被改动,拒绝运行")
    cfg = rc.load()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    try:
        code = compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise JobError(EXIT_REFUSED, f"策略文件语法错误: {exc}") from exc
    # 模块元数据只留 `__file__`/`__package__`/`__name__`:**不给候选真正的 loader**(第 21 轮实测的逃逸之三)。
    #
    # 此前把 `spec` 与 `spec.loader` 挂到候选模块上("保留标准 loader 元数据"),
    # 于是候选可以:
    #     import types
    #     m = types.ModuleType("pending_strategy")
    #     __loader__.exec_module(m)          # 用**真实内建**把候选源码再跑一遍
    #     b = m.__dict__["__builtins__"]      # 真实内建:setattr / eval / exec / open 全在
    # 受限内建那一层就此作废(实测)。现在 loader 与 spec 都不给:
    # `__spec__`/`__loader__` 保持模块默认值(None),候选摸不到能执行代码的入口。
    # 执行的是上面编译好的、经过校验的那份字节,.pyc 没有机会生效(第 4 轮修掉的 .pyc 绕过)。
    module = types.ModuleType("pending_strategy")
    module.__file__ = str(path)
    try:
        spec = importlib.util.spec_from_file_location("pending_strategy", path)
        if spec is not None:
            module.__package__ = spec.parent or ""
            module.__name__ = spec.name
    except Exception:                                        # noqa: BLE001 - 元数据拿不到不该拦住执行
        pass
    # 候选执行时用**最小内建集合**:静态层能封住已知的写法,封不住能力本身——
    # `e.__traceback__.tb_frame.f_builtins.setattr(...)` 这类路径上的名字都不在禁用名单里,
    # 而 `unittest.mock.patch.object` 只要能 import 就能改写实例属性。
    # 把 setattr/getattr/eval/exec/open 这些**从内建里拿掉**,能封住一批已知的路径
    # (连 frame.f_builtins 也只会拿到这份受限字典)。详见 scripts/research_sandbox.py::safe_builtins。
    #
    # **但这一层不是边界**(第 21 轮实测,别把它当边界写进结论里):真实内建还能从
    # ①候选模块的 loader ②异常链上的帧 ③任何第三方模块的 `__dict__["__builtins__"]` 拿到。
    # ①②已在本文件与 scripts/research_sandbox.py 里封掉;③**仍然敞着**——
    # 进程内执行不受信代码无法做到安全,真正的边界是人工审批 + 容器隔离(见 ADR 残余风险)。
    #
    # 另外加一道**按路径**的导入裁决(第 21 轮):名单只拦得住"名字",
    # 拦不住"候选自己写一个辅助模块再 import 它"——辅助模块是在 import 机制里执行的,
    # 拿到的是真实内建,静态层与受限内建对它都不生效。
    # 快照必须在**候选开始运行之前**拍:文件必须早就存在,否则拒绝导入。
    started_at_ns = time.time_ns()
    # 白名单**显式**传:靠默认参数的话,将来有人把默认值改成
    # ("strategies","scripts") 就能放开整个 scripts/,而登记在宪法历史守卫里的
    # 那个常量根本不会被改动(第 24 轮 codex Medium 2)。
    repo_guard = rs.RepoImportGuard(rc.CODE_ROOT,
                                    allowed_dirs=rs.ALLOWED_REPO_IMPORT_DIRS,
                                    snapshot=rs.snapshot_allowed_files(rc.CODE_ROOT),
                                    started_at_ns=started_at_ns)
    # 两边合成同一个调用:`allowed_modules`(main #22,导入**白名单**,配置只能追加)
    # 与 `repo_guard`(#26,按**路径**的导入裁决)是两道独立的闸,谁也不替代谁。
    module.__dict__["__builtins__"] = rs.safe_builtins(cfg["screening"]["extra_forbidden_modules"],
                                                      cfg["screening"]["allowed_modules"],
                                                      repo_guard=repo_guard)
    # **启动期核对**(第 26 轮 codex 第二轮复核的 Critical 1,实测):
    # 框架自己在跑候选**之前**就要预加载受保护的模块
    # (`risk/constitution.py::_preload_protected_modules` → `import strategies.…`),
    # 而导入机制导入子模块**之前必定先执行父包的 `__init__.py`**。
    # 于是"上一轮候选改了 `strategies/__init__.py`"的载荷,会在本轮**预加载阶段**就
    # 被执行——那时守卫还没构造,用的是**真内建**,在沙盒**之外**
    # (实测:候选还没跑起来,标记文件就已经被写出来了)。
    # 守卫那一侧只能拦"候选自己 import",拦不住"框架自己 import",
    # 所以这里在**预加载之前**先把受信目录整体与仓库提交核对一遍,不一致就拒绝运行。
    trusted_problems = rs.verify_trusted_tree(rc.CODE_ROOT, rs.ALLOWED_REPO_IMPORT_DIRS)
    if trusted_problems:
        raise JobError(EXIT_REFUSED, "受信目录与仓库提交不一致,拒绝运行:"
                       + chr(10) + "  " + (chr(10) + "  ").join(trusted_problems))
    # 运行期第四层:候选执行**前后**各拍一张"受保护对象身份指纹"。
    # 静态层按名字判断,拦不住"换个写法"改写框架对象——第 9 轮 codex 实测:
    # `import risk.risk_guard as rg; rg.risk_per_trade = lambda ...: 0.0`
    # 三行就架空 300 元单笔预算,而四层防线全部放行。指纹比对不看写法,只看结果:
    # 不管候选用哪种写法(rg 直接改、别名改、type(cls) 改),换了对象就对不上。
    protected_before = const.snapshot_protected_state()
    try:
        # 候选执行期间,受限导入器把会漏的模块换成**代理**(危险属性取不到);
        # `guarded_scope` 退出时无条件还原:宿主机自己的代码不该看到代理
        # (第 10 轮 codex 的 Critical 修复)。
        # `candidate_window`(Issue #31):导入期的审计钩子窗口——白名单里的 numpy/pandas 给出的是
        # 函数级的写文件/反序列化/起进程能力(np.memmap 写 pickle + pd.read_pickle),名字层规则看不见它们,
        # 所以在事件层按能力拒。窗口只罩住候选代码真正运行的这一段;框架自己的读写都在窗口外。
        # 第 40 轮 Medium 4:交易栈的**导入期副作用**必须在候选窗口**之外**发生。
        # `vnpy.trader.logger` 在**导入期**就会拉起 loguru → colorama → `ctypes.windll.kernel32`
        # (实测:在候选窗口里导入它会被审计钩子按能力拒掉 `ctypes.dlopen`,报出来的是
        # `AttributeError: kernel32`——看起来像颜色库坏了,实际上是钩子按能力拒了 ctypes)。
        #
        # **实测过的现状(别读成"碰巧")**:窗口打开之前,交易栈其实**早就**被载入了,
        # 走的是**间接**的两条链——子进程入口的 `from backtest_lib import ...`
        # (它顶层导了 `vnpy_ctastrategy.backtesting`),以及**上面那行**
        # `const.snapshot_protected_state()` → `_preload_protected_modules()`
        # → `strategies.…` → vnpy → logger。两条都是"别的包恰好 import 了 vnpy"的结果。
        #
        # 那为什么还要**显式**导一遍?因为上面两条都是**间接**的:哪天子进程入口不再先导
        # `backtest_lib`,或模板包改成不 import vnpy,窗口内的第一次导入就变成"真·第一次"
        # ⇒ 被拒,而且**报错点离原因很远**(看起来像颜色库坏了)。
        # 这里把不变量**显式**钉在开窗口的这一刻,不依赖任何别的包的内部行为——
        # 这是**纵深防御**,不是"补一个坏掉的洞"。
        #
        # 刻意**不用** `importlib.import_module`:那在宪法守卫眼里是"动态导入任意模块"的
        # 执行入口,会被要求自己成对取快照并比对(这里导的是**写死的两个框架模块**,
        # 本来就没什么可比的)。写成静态 `import` 语句既表达清楚了"就是这两个",
        # 也不会让守卫把这里误判成一个裸奔的执行点。
        #
        # 导不进去(本机没装交易栈)就跳过——常驻研究进程本来就不许 import vnpy
        # (见 scripts/backtest_lib.py 的说明),所以这里**只影响真的要跑候选的子进程**。
        #
        # **为什么写在 `import_candidate` 里而不是抽成一个函数**:这个不变量绑定的是
        # "开窗口之前",而开窗口的只有这一处;抽出去就得靠"每个调用方都记得先调它",
        # 那正是本条要消灭的"碰巧没发生"。研究栈的交易栈 import 只许出现在
        # `import_candidate` 这个已登记的例外作用域里(见 tests/test_research_boundary.py),
        # 放在这里**不需要为它新开一个例外**,边界规则一个字都不用改。
        try:
            import vnpy.trader.logger              # noqa: F401 - 目的就是它的导入期副作用
        except Exception:                          # noqa: BLE001 - 没装交易栈就跳过,不是错误
            pass
        try:
            import vnpy_ctastrategy                # noqa: F401 - 同上
        except Exception:                          # noqa: BLE001
            pass
        with rs.guarded_scope(), rs.candidate_window():
            exec(code, module.__dict__)                       # noqa: S102 —— 执行的就是上面校验过的那份字节
    except (Exception, SystemExit) as exc:                     # noqa: BLE001 - 候选自身的错,按结构化错误回传
        # SystemExit 也要兜住:候选在导入期 `raise SystemExit()` 不该让子进程带 traceback 死掉,
        # 父进程只会看到"回测失败",那条给 LLM 的诊断通道就断了。
        raise CandidateError(describe_error(exc)) from exc
    # 第二个实参**当场**拍:必须是"这一次调用"的新快照,不能是提前存好的变量或它的拷贝
    # (宪法历史守卫按这条核对执行点:拿拷贝比对,指纹必然一致,第四层形同虚设)。
    tampered = const.check_protected_state(protected_before, const.snapshot_protected_state())
    if tampered:
        raise JobError(EXIT_REFUSED, "候选在导入期改写了受保护的框架对象:"
                       + chr(10) + "  " + (chr(10) + "  ").join(tampered))
    from vnpy_ctastrategy import CtaTemplate
    candidate = getattr(module, cls_name, None)
    if not (isinstance(candidate, type) and issubclass(candidate, CtaTemplate)):
        # 找不到类 = 调用方要了一个候选里没有的类,是**参数错误**(拒绝),不是运行失败
        raise JobError(EXIT_REFUSED, f"{cls_name} 不存在或不是 CtaTemplate 子类")
    check_candidate_class(candidate)                          # 运行时再查一次:挡住批准后被替换的候选
    return candidate


def require_clean(path: Path, cfg: dict) -> None:
    findings = screen(path, cfg)
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))


MAX_STRATEGY_NAME = 300


def _collapse_repeated_py(name: str) -> str:
    """把末尾连续重复的 ".py" 收成一个;**线性**(不用正则:131KB 的 ".py" 重复串上正则要 21 秒,拖住整个网关)。"""
    end = len(name)
    while end >= 6 and name.endswith(".py", 0, end) and name.endswith(".py", 0, end - 3):
        end -= 3
    return name[:end]


def resolve_strategy(strategy: str, cfg: dict) -> Path:
    # Issue #75:毕昇流程自己会在命名节点的输出后补 ".py";命名模型偶尔违反"不带扩展名"而输出 "x.py",
    # 拼出 "x.py.py" ⇒ 评估一次也没跑就 400。
    # **先按原名解析**(磁盘上真有 x.py.py 就用它,绝不悄悄换成 x.py);只有原名不存在才回退到
    # 收掉重复 .py 的名字。回退后仍走 resolve_pending 的目录/后缀/存在性三道关;批准绑定文件内容哈希。
    # 失败时的错误信息仍写调用方给的原名。带 ":" 的名字不回退(Windows 备用数据流)。
    pending_dir = cfg["paths"]["pending_dir"]
    if isinstance(strategy, str) and len(strategy) > MAX_STRATEGY_NAME:
        raise JobError(EXIT_REFUSED, "策略名过长")
    try:
        return rs.resolve_pending(strategy, rc.ROOT, pending_dir)
    except rs.SandboxError as exc:
        original = exc
    if isinstance(strategy, str) and ":" not in strategy:
        collapsed = _collapse_repeated_py(strategy)
        if collapsed != strategy:
            try:
                return rs.resolve_pending(collapsed, rc.ROOT, pending_dir)
            except rs.SandboxError:
                pass
    raise JobError(EXIT_REFUSED, str(original))


def data_report() -> list:
    import segment_ledger
    import segment_windows
    out = []
    for key in rs.KEYS:
        windows = segment_windows.windows_for(key)
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
            # 这条只说"沙盒**不把**测试段喂给候选、也不给候选读库的函数";
            # 进程内读任意本机文件(含派生 CSV 里的测试段数据)仍未被拦(同进程残余,Issue #29)。
            # 别把它读成"候选无论如何都看不到测试段"。
            "test_segment_accessible_via_sandbox": False,
            "test_segment_read_via_local_file": True,
            "test_segment_consumed": consumed,
        })
    return out


def approve_job(strategy: str, input_fn=None, write=None) -> dict:
    """人工批准:打印文件摘要与 sha256,要求人在**交互式终端**里把完整哈希粘贴回来。

    为什么不是「敲个 y 就行」:
      - 非交互(stdin 是管道)时,任何程序都能自己喂字符串把批准做掉——那就不是「人在场」了。
        因此非 TTY 一律拒绝(与带外批准网页同一条口径:那边也要人输入完整 64 位哈希)。
      - 要的是**人核对过的那份内容**:摘要与哈希打在 stderr 上(留给人的通道),
        stdout 仍然只出 JSON,便于脚本解析。
    """
    cfg, paths = settings()
    path = resolve_strategy(strategy, cfg)
    raw, source = read_candidate(path)
    # 哈希与筛查都用**这一份**字节:分开读会出现"显示的大小/哈希"与"实际批准的"不是同一份内容。
    sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    say = write or (lambda text: print(text, file=sys.stderr))
    say(f"文件: {path.name}   大小: {len(raw)} 字节   sha256: {sha}")
    say("⚠ 这行 sha256 只用在本机终端里核对,**不要抄进聊天窗口**(抄出去就等于把批准权交出去)。")
    say("静态筛查: " + ("通过" if not findings else "未通过"))
    for finding in findings:
        say(f"  - {finding}")
    if findings:
        raise JobError(EXIT_REFUSED, "拒绝批准: 静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    if not sys.stdin.isatty():
        raise JobError(EXIT_REFUSED, "approve 必须在交互式终端里执行(当前 stdin 不是终端);"
                                    "自动化调用不得批准代码。")
    try:
        # 提示语走 stderr,stdout 只出 JSON:`input(prompt)` 会把提示语写到 stdout,
        # 那样 stdout 就不是纯 JSON 了(第 5 轮 codex 的 M6:stdout 是模型可见通道的候选)。
        say("把上面的 sha256 完整粘贴回来以确认批准: ")
        answer = (input_fn or input)("")
    except (EOFError, OSError):
        answer = ""
    if answer.strip().lower() != sha:
        say("哈希不匹配,未批准。")
        return {"approved": False, "name": path.name}
    try:
        rs.record_approval(path, paths["approvals_file"], sha256=sha, via="cli")
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))
    say("已批准(绑定到上面的 sha256;代码改动后需要重新批准)。")
    return {"approved": True, "already": False, "name": path.name}


def review_job(strategy: str, input_fn=None, write=None) -> dict:
    """先看代码再批准:打印文件摘要、静态筛查结果和**带行号的完整源码**,再要求人粘贴完整 sha256。

    与 `approve` 同一条口径(第 8 轮 codex 的 H1):`review` 曾是「敲个 y 就行」,而且没有 TTY 门——
    `echo y | research_api.py review` 就能把批准做掉,等于把批准权交给了能喂字符串的程序。
    现在:非交互终端一律拒绝;确认方式是**粘贴完整 64 位哈希**(人读过的那份内容的哈希);
    stdout 仍只出 JSON,人读的内容走 stderr。
    """
    cfg, paths = settings()
    path = resolve_strategy(strategy, cfg)
    raw, source = read_candidate(path)           # 字节数按原始字节算:换行转换后的长度不是文件真实大小
    sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    say = write or (lambda text: print(text, file=sys.stderr))
    say(f"文件: {path.name}   大小: {len(raw)} 字节   sha256: {sha}")
    say("⚠ 这行 sha256 只用在本机终端里核对,**不要抄进聊天窗口**(抄出去就等于把批准权交出去)。")
    say("静态筛查: " + ("通过" if not findings else "未通过"))
    for finding in findings:
        say(f"  - {finding}")
    say("-" * 60)
    for number, line in enumerate(source.splitlines(), 1):
        say(f"{number:4d} | {line}")
    say("-" * 60)
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过,不予批准")
    try:
        if rs.is_approved_sha(sha, paths["approvals_file"]):
            say("该内容(同一哈希)已批准过。")
            return {"approved": True, "already": True, "name": path.name}
        if not sys.stdin.isatty():
            raise JobError(EXIT_REFUSED, "review 必须在交互式终端里执行(当前 stdin 不是终端);"
                                        "自动化调用不得批准代码。")
        try:
            # 提示语走 stderr,stdout 只出 JSON(同 approve_job)。
            say("读完以上代码,把上面的 sha256 完整粘贴回来以确认批准: ")
            answer = (input_fn or input)("")
        except (EOFError, OSError):
            answer = ""
        if answer.strip().lower() != sha:
            say("哈希不匹配,未批准。")
            return {"approved": False, "name": path.name}
        if rs.file_sha256(path) != sha:                       # 批准必须绑定到用户刚读过的内容
            raise JobError(EXIT_REFUSED, "文件在阅读期间被改动,未批准;请重新运行 review")
        rs.record_approval(path, paths["approvals_file"], sha256=sha, via="cli")
    except rs.SandboxError as exc:
        raise JobError(EXIT_REFUSED, str(exc))
    say("已批准(绑定到上面的 sha256;代码改动后需要重新批准)。")
    return {"approved": True, "already": False, "name": path.name}


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
    # 只读一次:哈希、筛查、审批、子进程参数、结果记录全部用这同一份字节。
    raw, source = read_candidate(path)
    strategy_sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    check_approval(path, paths, confirm, strategy_sha)

    cmd = [sys.executable, str(Path(__file__).resolve()), "_child", "--file", str(path), "--cls", cls,
           "--key", key, "--segment", segment, "--params", json.dumps(params), "--sha256", strategy_sha]
    body = spawn_child(cmd, timeout)
    if "error" in body:                    # 策略自身报错是研究中的正常结果:记录下来供反馈,而不是当作网关故障
        result = {"kind": "backtest", "status": "strategy_error", "strategy_file": path.name,
                  "strategy_sha256": strategy_sha, "class": cls, "key": key,
                  "segment": segment, "params": params, "ran_at": datetime.now().isoformat(timespec="seconds"),
                  "error": body["error"]}
        save_with_report(result, paths)
        return result

    from risk.diagnosis import diagnose
    result = {
        "kind": "backtest", "strategy_file": path.name, "strategy_sha256": strategy_sha,
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
    raw, source = read_candidate(path)                  # 只读一次,见 backtest_job 的说明
    strategy_sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    check_approval(path, paths, confirm, strategy_sha)
    trials = charge_trials(paths, "gate_run", 1)      # 这次门槛评估本身算一次试验(先记账再跑,失败不退),账本为空也不给 0

    cmd = [sys.executable, str(Path(__file__).resolve()), "_gate_child", "--file", str(path), "--cls", cls,
           "--params", json.dumps(params), "--random-runs", str(random_runs),
           "--on-plateau", "1" if on_plateau else "0", "--sha256", strategy_sha,
           "--prior-trials", str(trials)]
    body = spawn_child(cmd, timeout)
    if "error" in body:
        result = {"kind": "gate", "status": "strategy_error", "passed": False, "strategy_file": path.name,
                  "strategy_sha256": strategy_sha, "class": cls, "key": "C15",
                  "params": params, "ran_at": datetime.now().isoformat(timespec="seconds"), "error": body["error"]}
        save_with_report(result, paths)
        return result
    result = {
        "kind": "gate", "strategy_file": path.name, "strategy_sha256": strategy_sha,
        "class": cls, "key": "C15", "params": params, "plateau_note": plateau_note,
        "protocol_notice": PROTOCOL_NOTICE,
        "ran_at": datetime.now().isoformat(timespec="seconds"), **body,
    }
    save_with_report(result, paths)
    return result


EVALUATE_NOTICE = ("evaluate 的样本外由服务端保证：参数只在选参窗口（2020-07-22 ~ 2022-06-30）上按候选声明的 param_grid 选出，"
                   "门槛评估只用其后的滚动验证窗口（2022-07 ~ 2023-12），两段不重叠；测试段不可达。")


def save_with_report(result: dict, paths: dict, run_id: str | None = None) -> None:
    """落盘前补上确定性中文报告(纯函数生成,出错也只是一行说明),并写入 run_id。
    run_id 给定(异步评估启动时已分配)就用它,否则现分配。"""
    result["report"] = render_report(result)
    result["run_id"] = rs.save_result(result, paths["results_dir"], run_id=run_id)


def evaluate_prepare(*, strategy: str, cls: str, random_runs: int | None = None, timeout: float | None = None,
                     confirm: bool = False, product: str = "C15") -> dict:
    """评估的**同步前半段**(Issue #84):入参校验、策略名解析、静态筛查、**批准检查**、试验预扣——
    全部在这里做完,任何拒绝都在启动前抛 JobError。返回交给 `evaluate_run` 的上下文(仅进程内使用,不序列化)。
    异步网关与同步 CLI 共用这一段,所以「异步」不可能绕过任何前置检查。"""
    cfg, paths = settings()
    check_product(product)
    timeout = timeout if timeout is not None else cfg["gate"]["timeout_seconds"] + cfg["grid"]["timeout_seconds"]
    random_runs = random_runs if random_runs is not None else cfg["gate"]["random_runs"]
    path = resolve_strategy(strategy, cfg)
    low, high = rc.INT_BOUNDS[("gate", "random_runs")]
    if isinstance(random_runs, bool) or not isinstance(random_runs, int) or not low <= random_runs <= high:
        raise JobError(EXIT_REFUSED, f"random_runs 必须是 [{low}, {high}] 内的整数(配置边界)")
    raw, source = read_candidate(path)                  # 只读一次,见 backtest_job 的说明
    strategy_sha = hashlib.sha256(raw).hexdigest()
    # 候选独立进程开关(Issue #29 S1,默认关):开着时新格式候选要 `from candidate.signal_template import ...`,
    # 静态筛查只在**这条路径上**多放行 `candidate` 这个导入根;关着时白名单一个字都不变。
    candidate_process = cfg["isolation"]["candidate_process"]
    allowed_modules = list(cfg["screening"]["allowed_modules"]) + (["candidate"] if candidate_process else [])
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"], allowed_modules)
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    check_approval(path, paths, confirm, strategy_sha)
    prior = prior_trials(paths)                   # 先读后扣:子进程算 DSR 用 prior + 实际格子数,预扣的最小网格不能算两次
    charge_trials(paths, "grid_cell", MIN_GRID_CELLS)
    return {"path": path, "strategy_file": path.name, "cls": cls, "product": product, "timeout": timeout,
            "random_runs": random_runs, "strategy_sha": strategy_sha, "prior": prior, "paths": paths,
            "candidate_process": candidate_process}


def evaluate_run(ctx: dict, run_id: str | None = None) -> dict:
    """评估的**慢的后半段**:起子进程、补记网格试验、落盘。run_id 给定则沿用(异步),否则现分配(同步)。"""
    path, cls, product, paths = ctx["path"], ctx["cls"], ctx["product"], ctx["paths"]
    # 开关来自 prepare 读到的**配置文件**,不是调用方传的参数;开=框架侧子进程(不载入候选)+ 候选宿主进程
    child_name = "_evaluate_signal_child" if ctx.get("candidate_process") else "_evaluate_child"
    cmd = [sys.executable, str(Path(__file__).resolve()), child_name, "--file", str(path), "--cls", cls,
           "--random-runs", str(ctx["random_runs"]), "--sha256", ctx["strategy_sha"], "--prior-trials", str(ctx["prior"]),
           "--product", product]
    body = spawn_child(cmd, ctx["timeout"])
    if "error" not in body:
        top_up_grid_trials(paths, body)
    base = {"kind": "evaluate", "strategy_file": path.name, "strategy_sha256": ctx["strategy_sha"],
            "class": cls, "key": product, "protocol_notice": EVALUATE_NOTICE,
            "ran_at": datetime.now().isoformat(timespec="seconds")}
    if ctx.get("candidate_process"):
        base["isolation"] = "candidate-process"       # 留痕:这份结果是候选独立进程路径算出来的
    if "error" in body:
        result = {**base, "status": "strategy_error", "passed": False, "error": body["error"]}
    else:
        result = {**base, **body}
    save_with_report(result, paths, run_id)
    return result


def evaluate_job(*, strategy: str, cls: str, random_runs: int | None = None, timeout: float | None = None,
                 confirm: bool = False, product: str = "C15") -> dict:
    """候选在类上声明 `param_grid`（三个参数、各至少 3 个递增取值）：先在选参窗口扫网格并判定参数高原，
    再用选出的参数在滚动验证窗口上做完整的晋级门槛评估。同步入口(CLI 用):前半段 + 后半段连着跑。"""
    return evaluate_run(evaluate_prepare(strategy=strategy, cls=cls, random_runs=random_runs, timeout=timeout,
                                         confirm=confirm, product=product))


def run_cli_job(func) -> int:
    """CLI 适配:作业成功则打印 JSON,JobError 则把消息打到 stderr 并返回对应退出码。"""
    try:
        emit(func())
        return EXIT_OK
    except (JobError, rs.SandboxError) as exc:
        code = getattr(exc, "code", EXIT_REFUSED)
        message = getattr(exc, "message", str(exc))
        print(("拒绝: " if code == EXIT_REFUSED else "") + message, file=sys.stderr)
        if message == NEEDS_APPROVAL:
            # 批准命令只给人看(stderr),不进模型通道:同一条消息经网关回到模型时
            # 会把命令一起带出去(第 6 轮 codex 的 M2/B 的 M)。
            print(APPROVAL_HINT, file=sys.stderr)
        return code


def cmd_review(args) -> int:
    # 走 run_cli_job:stdout 出 JSON(与 approve/backtest 同一契约),人读的内容走 stderr。
    # 原来直接丢掉 review_job 的返回值,脚本化调用既拿不到批准结果也拿不到「未批准」标志
    # (第 6 轮 codex 的 M6)。
    return run_cli_job(lambda: review_job(args.strategy))


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
                                            timeout=args.timeout, confirm=args.confirm_reviewed, product=args.product))


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
    try:
        path = child_file(Path(args.file))    # 自己确认文件仍在 pending 目录内
        child_approved(args.sha256)            # 自己查审批记录:"人看过代码"这条前提不能靠父入口担保
        params = child_params(args)           # 先查参数:比载入候选更便宜,也让"绕过父入口"这条路走不通
        random_runs, prior_trials = child_int_args(args)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if args.on_plateau not in ("0", "1"):
        print("--on-plateau 只能是 0 或 1", file=sys.stderr)
        return EXIT_REFUSED
    # 行情与窗口在候选**导入之前**取定成局部值(Issue #31 D):原先 `load_bars_csv` 在 `import_candidate` 之后,
    # 候选在导入期若能改写 CSV,框架随后读到的就是被改过的价格("样本外由服务端保证"落空)。
    # 与评估输入同一个模式:先取定,再让候选运行。
    bars = load_bars_csv(wf.DATA_FILE, wf.SYMBOL, Exchange.DCE)
    windows = [(w.name, w.valid_start, w.valid_end) for w in wf.build_windows()]
    try:
        cls = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CandidateError as exc:
        print(CHILD_MARK + json.dumps({"error": exc.detail}, ensure_ascii=False))
        return EXIT_OK
    try:
        with rs.candidate_window():
            result = run_gate(candidate_cls=cls, params=params, windows=windows, bars=bars,
                              runner=engine_runner(wf.KEY), random_cls=RandomEntry15mStrategy, random_runs=random_runs,
                              data_adjusted=True, on_plateau=args.on_plateau == "1", capital=DEFAULT_CAPITAL,
                              prior_trials=prior_trials)
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
    try:
        path = child_file(Path(args.file))    # 自己确认文件仍在 pending 目录内
        child_approved(args.sha256)            # 自己查审批记录:"人看过代码"这条前提不能靠父入口担保
        random_runs, prior_trials = child_int_args(args)   # 整数参数自己复查(负的 prior_trials 会放松 DSR 门槛)
        check_product(args.product)            # 品种白名单子进程自己再查一次
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        data_file, symbol = wf.PRODUCTS[args.product]      # 白名单与数据映射漂移时给出可读错误,不抛裸 KeyError
    except KeyError:
        print(f"品种 {args.product} 没有登记数据映射(walk_forward_optimizer.PRODUCTS)", file=sys.stderr)
        return EXIT_REFUSED
    if not Path(data_file).is_file():
        print(f"品种 {args.product} 的数据文件缺失:{Path(data_file).name}", file=sys.stderr)
        return EXIT_REFUSED
    # 行情、窗口、配置在候选**导入之前**取定(Issue #31 D,理由见 run_gate_child)。
    bars = load_bars_csv(data_file, symbol, Exchange.DCE)
    windows = wf.build_windows()
    cfg = rc.load()                          # 选参网格上限来自配置:统一入口重构时漏掉了这一行
    try:
        candidate = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CandidateError as exc:
        print(CHILD_MARK + json.dumps({"error": exc.detail}, ensure_ascii=False))
        return EXIT_OK
    try:
        with rs.candidate_window():
            result = run_evaluation(
                candidate_cls=candidate, bars=bars, runner=engine_runner(args.product), random_cls=RandomEntry15mStrategy,
                random_runs=random_runs, capital=DEFAULT_CAPITAL, max_cells=cfg["grid"]["max_cells"],
                min_round_trips=cfg["grid"]["min_round_trips"], select_window=(windows[0].train_start, windows[0].train_end),
                validation_windows=[(w.name, w.valid_start, w.valid_end) for w in windows], prior_trials=prior_trials)
    except Exception as exc:                  # 含 GridError：声明有问题也是候选自己的问题，作为结果回传
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    print(CHILD_MARK + json.dumps(result, ensure_ascii=False))
    return EXIT_OK


def run_evaluate_signal_child(args) -> int:
    """框架侧评估子进程(候选独立进程路径,Issue #29 S1)。**不载入候选**:候选源码只被当文本读、当文本交给宿主进程。

    与 `run_evaluate_child` 同一套自查(路径、审批、整数参数、品种白名单),同一套评估(`run_evaluation`),
    区别只在 runner:遇到候选(代理类)就起候选宿主进程取信号,再交给框架自己的引擎回放。
    格子数、试验数全是本进程自己跑出来的数,候选无处自报。
    """
    import research_candidate_process as rcp
    import research_candidate_spec as rspec
    import research_signal_replay as rsr
    import walk_forward_optimizer as wf
    from backtest_lib import DEFAULT_CAPITAL, load_bars_csv
    from research_gate import engine_runner, run_evaluation
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    from vnpy.trader.constant import Exchange
    try:
        path = child_file(Path(args.file))
        child_approved(args.sha256)
        random_runs, prior_trials = child_int_args(args)
        check_product(args.product)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        data_file, symbol = wf.PRODUCTS[args.product]
    except KeyError:
        print(f"品种 {args.product} 没有登记数据映射(walk_forward_optimizer.PRODUCTS)", file=sys.stderr)
        return EXIT_REFUSED
    if not Path(data_file).is_file():
        print(f"品种 {args.product} 的数据文件缺失:{Path(data_file).name}", file=sys.stderr)
        return EXIT_REFUSED
    # 候选源码只读一次,并核对它就是被批准的那一份字节(与旧路径 import_candidate 同一条哈希前提)。
    # 读法与旧路径**同一个函数**:`utf-8-sig`(带 BOM 的候选在旧路径能跑)、读不了是「拒绝」而不是「失败」。
    try:
        raw, source = read_candidate(path)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        print("候选文件内容与批准时的哈希不符,拒绝执行", file=sys.stderr)
        return EXIT_REFUSED
    # 行情、窗口、配置在起任何候选进程之前取定(与旧路径同一条纪律)
    bars = load_bars_csv(data_file, symbol, Exchange.DCE)
    windows = wf.build_windows()
    cfg = rc.load()
    # 静态筛查在子进程里**再做一遍**:旧子入口在 import_candidate 里就是这么做的(批准与筛查是两道独立的闸;
    # 自动批准账里的代码甚至没人读过)。新子入口不得比它弱。`candidate` 导入根只在这条路径上放行。
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                list(cfg["screening"]["allowed_modules"]) + ["candidate"])
    if findings:
        print("静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings), file=sys.stderr)
        return EXIT_REFUSED
    try:
        spec = rspec.parse_spec(source)
        if spec.class_name != args.cls:
            # `--cls` 来自调用方(最终来自模型/工作流):完整信息留本机,回给模型的是固定说明,不回显它
            raise rspec.SpecError(f"--cls {args.cls} 与源码里唯一的候选类 {spec.class_name} 不符",
                                  public="指定的类名与源码里唯一的候选类不符")
        rspec.check_framework_defaults(spec, TrendAtr15mStrategy.parameters)
    except rspec.SpecError as exc:
        # 候选自己的声明有问题 = 研究中的正常结果(与旧路径的 GridError 对称),按结构化错误回传:
        # 完整信息(含候选可控的标识符)只写本机 stderr,回给模型的只有固定说明 `exc.public`
        print(f"候选声明被拒绝: {exc}", file=sys.stderr)
        print(CHILD_MARK + json.dumps({"error": {"type": "SpecError", "message": exc.public}}, ensure_ascii=False))
        return EXIT_OK
    # 类体默认值里的受保护参数(`risk_budget = 1e9`)在起**任何**候选进程之前就硬拒绝,与旧路径一致
    # (旧路径是 check_class 在进评估之前拒绝、退出码 2)。静态筛查通常会先拦住;这一层不依赖它,是纵深防御。
    try:
        check_params_allowed(rspec.framework_setting(spec, None, TrendAtr15mStrategy.parameters))
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    proxy = rspec.proxy_class(spec)
    runner = rsr.make_signal_runner(
        source=source, spec=spec, proxy=proxy, base=TrendAtr15mStrategy, engine=engine_runner(args.product),
        check_setting=check_params_allowed, framework_parameters=TrendAtr15mStrategy.parameters,
        timeout=float(cfg["backtest"]["timeout_seconds"]))
    try:
        result = run_evaluation(
            candidate_cls=proxy, bars=bars, runner=runner, random_cls=RandomEntry15mStrategy,
            random_runs=random_runs, capital=DEFAULT_CAPITAL, max_cells=cfg["grid"]["max_cells"],
            min_round_trips=cfg["grid"]["min_round_trips"], select_window=(windows[0].train_start, windows[0].train_end),
            validation_windows=[(w.name, w.valid_start, w.valid_end) for w in windows], prior_trials=prior_trials)
    except rcp.CandidateProcessError as exc:
        # 候选写的文字(detail)不可信:走与旧路径同一条净化(describe_error);框架自己的失败只给固定说明
        if exc.kind == "candidate_error":
            err = describe_error(RuntimeError(exc.detail))
        else:
            err = {"type": "CandidateProcessError", "message": rs.scrub_text(exc.message)[:200]}
        print(CHILD_MARK + json.dumps({"error": err}, ensure_ascii=False))
        return EXIT_OK
    except Exception as exc:                  # 含 GridError、受保护参数拒绝:候选自己的问题,作为结果回传
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    print(CHILD_MARK + json.dumps(result, ensure_ascii=False))
    return EXIT_OK


def run_child(args) -> int:
    """子进程:加载策略并回测。只在受限环境(sandbox_env)里由 cmd_backtest 拉起。"""
    # `vnpy_database_paths` 在**交易侧**模块里(常驻研究进程不许 import vnpy,见 tests/test_research_boundary.py)。
    from backtest_lib import run_backtest, vnpy_database_paths
    from strategies.instruments import INSTRUMENTS
    try:
        path = child_file(Path(args.file))    # 自己确认文件仍在 pending 目录内
        child_approved(args.sha256)            # 自己查审批记录:"人看过代码"这条前提不能靠父入口担保
        params = child_params(args)           # 先查参数:比载入候选更便宜,也让"绕过父入口"这条路走不通
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        start, end = rs.segment_window(args.key, args.segment)
    except rs.SandboxError as exc:
        # 测试段/未知品种在这里被拒:退出码必须是"拒绝"(2),不能是未捕获异常的 1
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    try:
        strategy_class = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CandidateError as exc:
        print(CHILD_MARK + json.dumps({"error": exc.detail}, ensure_ascii=False))
        return EXIT_OK
    # 回测要读 vnpy 数据库(sqlite)。**在窗口之前**由框架先把连接建好(`get_database()` 缓存在模块全局里),
    # 窗口里的 `load_bar` 等只复用这条已有连接,不再发新的 `sqlite3.connect`——于是窗口里**不需要、也不登记任何 sqlite 路径**
    # (第 38 轮:登记既不必要,也不解决问题;候选拿活动句柄的入口 `get_database`/`database` 在对象层已不可达)。
    # 框架自己在窗口里读 vnpy 库(sqlite):peewee 的连接是**惰性**打开的,第一次查询才 connect,
    # 那次 connect 落在窗口内会被钩子拒(路径在允许目录之外)。所以给"框架读库"一条**窄**豁免:
    # 只豁免 `sqlite3.connect` 到那个库文件——**只豁免 connect 事件**:连上之后的 SQL 写
    # (`UPDATE`/`DELETE`/`ATTACH`/`VACUUM`)在事件层发不出带路径的事件,既没被豁免也没被拦;
    # 候选今天改不动库文件,靠的是**能力层**(sqlite3 模块在危险名单里、库句柄不可达),不是靠这条豁免。
    # (第 43 轮:原注释写成"不豁免 open/写,候选照样改不动库文件",听起来像这条豁免顺带保证了写不被允许——是错的。)
    # (候选也拿不到句柄:`get_database`/`database` 在对象层不可达,见 ADR 第 38/39 轮。)
    try:
        with rs.candidate_window(), rs.framework_exempt(sqlite_paths=vnpy_database_paths()):
            stats, _, trades = run_backtest(strategy_class, params, args.key, start, end)
    except Exception as exc:               # 策略自己的 bug:回传结构化摘要(不含路径),退出码仍为 0
        print(CHILD_MARK + json.dumps({"error": describe_error(exc)}))
        return EXIT_OK
    row = backtest_stats_row(stats)          # 投影规则见 risk/report.py:面板校验用的是同一份
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
    ev.add_argument("--product", default="C15", help="评估品种(白名单见 walk_forward_optimizer.PRODUCTS);缺省 C15=玉米,与旧行为一致")
    ev.add_argument("--confirm-reviewed", action="store_true")
    ev.set_defaults(func=cmd_evaluate)
    rv = sub.add_parser("review", help="先看代码再批准:显示带行号的源码与静态筛查结果,"
                                        "粘贴完整 64 位 sha256 才批准")
    rv.add_argument("--strategy", required=True)
    rv.set_defaults(func=cmd_review)
    ap = sub.add_parser("approve", help="人工批准:读过代码后执行,批准绑定到代码内容的 sha256")
    ap.add_argument("--strategy", required=True)
    ap.set_defaults(func=cmd_approve)
    rr = sub.add_parser("results", help="读取结果与诊断书")
    rr.add_argument("run_id", nargs="?", default="")
    rr.set_defaults(func=cmd_results)
    child = sub.add_parser("_child")           # 内部使用
    for name in ("file", "cls", "params", "sha256"):
        child.add_argument(f"--{name}", required=True)
    # 隐藏入口的取值范围也在这里先卡一道:子进程能被直接调用,不该假设父进程查过
    child.add_argument("--key", required=True, choices=rs.KEYS)
    child.add_argument("--segment", required=True, choices=rs.SEGMENTS)
    child.set_defaults(func=run_child)
    gchild = sub.add_parser("_gate_child")     # 内部使用
    for name in ("file", "cls", "params", "sha256"):
        gchild.add_argument(f"--{name}", required=True)
    gchild.add_argument("--on-plateau", required=True, choices=("0", "1"))
    gchild.add_argument("--random-runs", type=int, required=True)
    gchild.add_argument("--prior-trials", type=int, default=None)
    gchild.set_defaults(func=run_gate_child)
    echild = sub.add_parser("_evaluate_child")  # 内部使用
    for name in ("file", "cls", "sha256"):
        echild.add_argument(f"--{name}", required=True)
    echild.add_argument("--random-runs", type=int, required=True)
    echild.add_argument("--prior-trials", type=int, default=None)
    echild.add_argument("--product", default="C15")      # 子进程自己再按白名单核对,不假设父进程查过
    echild.set_defaults(func=run_evaluate_child)
    schild = sub.add_parser("_evaluate_signal_child")   # 内部使用:候选独立进程路径的框架侧评估(不载入候选)
    for name in ("file", "cls", "sha256"):
        schild.add_argument(f"--{name}", required=True)
    schild.add_argument("--random-runs", type=int, required=True)
    schild.add_argument("--prior-trials", type=int, default=None)
    schild.add_argument("--product", default="C15")
    schild.set_defaults(func=run_evaluate_signal_child)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except rc.ConfigError as exc:
        return refuse(f"配置非法,拒绝运行: {exc}")


if __name__ == "__main__":
    rconsole.reconfigure_stdio()
    sys.exit(main())
