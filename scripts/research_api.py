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
import atexit
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

import bootstrap
import research_config as rc
import research_sandbox as rs
from research_grid import MIN_GRID_CELLS
from risk import constitution as const
from risk.report import backtest_stats_row, render_report
from risk.candidate_eval import TRADING_DAYS_PER_YEAR
from risk.dsr import MAX_RUNS_USED, MAX_TRIAL_SHARPE
from risk import lineage
from risk.heatmap import HeatmapError, save_heatmap, validate_heatmap
from risk.trial_ledger import (TrialLedgerError, grid_run_variances, record_sharpes, record_trials, total_trials,
                               valid_trial_sharpes)

EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 0, 2, 3, 124

# 阶段(与 scripts/research_gate.py 的同名常量保持一致;有一处用例锁定两者相等)。
# 阶段**只由框架决定**,决定之后写进结果字典,候选没有通道能改它(见 describe_error)。
PHASE_DECLARATION = "declaration"      # 声明期:我们自己写的消息(网格声明、类校验)
PHASE_SELECTION = "selection"          # 选参窗口回放:进化回路放开的那一层
PHASE_VALIDATION = "validation"        # 验证窗口回放:**报错原文不外传**
PHASES = (PHASE_DECLARATION, PHASE_SELECTION, PHASE_VALIDATION)

# 子进程结果的年化夏普合理上界:超过它的结果一律拒绝采信(伪造的兜底,不是策略门槛)。
# 真实策略的年化夏普在个位数;50 已经宽松到不可能是误伤。
MAX_ABS_OOS_SHARPE = 50.0
# 子进程结果文件的大小上限:父进程读它之前先看大小。
# 候选能回传任意长的数组(比如百万条 pnl),没有上限时父进程会把它整个读进内存——
# 沙盒不给子进程设资源上限,所以这条是父进程侧唯一的兜底(fail-closed:超限即拒绝采信)。
# 4 MB 对真实结果绰绰有余:回测结果只有 trade 级 pnl 与一行 stats,门槛结果只有汇总指标。
MAX_RESULT_BYTES = 4 * 1024 * 1024
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


def describe_error(exc: BaseException, phase: str) -> dict:
    """把策略运行时异常压成 {type, message, phase}:取 traceback 末尾的异常行,抹掉本机路径,截断到 200 字符。

    注意 message 可能含已批准代码里 raise 出来的文本,属于「会进入 LLM 上下文的外部文本」,所以限长并只保留可打印字符。

    `phase` **必须由调用点显式给出**,而且只能取 PHASES 里的值:
    - 阶段**绝不**从异常对象上读:候选可以给自己的异常挂 `invest_phase="selection"`,
      旧实现"只补不改"就会保留它,于是验证窗口的报错原文被当成选参期的消息放出去,而梯子一次都没扣
      (第 22 轮 codex 的 High,实测消息里带出了验证窗口的收盘价);
    - 阶段**也绝不**按异常类名推断(第 27 轮 codex 的 Medium-2):旧实现有
      `name == "GridError" ⇒ declaration` 这条兜底,而异常类名是**候选自己起的名字**——
      候选只要在验证窗口回放里 `raise GridError("close=2728.0")`,验证窗口的数字就被当成
      "我们自己写的声明期消息"放了出去(实测 view.error.phase == "declaration")。
      框架自己的 GridError 由 research_gate 的调用点**显式**传 PHASE_DECLARATION。
    - 传进来的阶段不在 PHASES 里(调用点写错了)→ 按验证窗口处理(fail-closed:原文不放行)。"""
    text = str(exc)
    name, msg = type(exc).__name__, text
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        m = _EXC_LINE.match(line)
        if m:
            name, msg = m.group(1), m.group(2)
            break
    msg = _PATH_LIKE.sub("<path>", msg)
    msg = "".join(ch for ch in msg if ch.isprintable())[:200]
    if phase not in PHASES:
        phase = PHASE_VALIDATION                       # 阶段不明 = 按验证窗口处理(fail-closed)
    return {"type": name.split(".")[-1], "message": msg, "phase": phase}


def trials_path(paths: dict) -> Path:
    return Path(paths["results_dir"]) / "trials.jsonl"


def prior_trials(paths: dict) -> int:
    """累计试验次数,供子进程计算 DSR。账本损坏则拒绝作业(fail-closed):少记试验等于放水。"""
    try:
        return total_trials(trials_path(paths))
    except TrialLedgerError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc


def take_heatmap(body: dict) -> tuple[dict | None, str | None]:
    """把热力图从作业结果里**拿出来**(它不并入落盘的结果,更不并入给外层的响应),并校验。

    正文来自候选所在的子进程,不可信:不合法就丢弃并说明,不让一份坏图挡住整个评估结果。"""
    heatmap = body.pop("heatmap", None)
    if heatmap is None:
        return None, None
    try:
        validate_heatmap(heatmap)
    except HeatmapError as exc:
        return None, f"热力图被丢弃: {exc}"
    return heatmap, None


def save_job_heatmap(paths: dict, run_id: str, heatmap: dict) -> None:
    """结果已落盘、run_id 已确定之后,把热力图另存给面板。写不进去就大声失败:缺失的图不能让面板显示成正常。"""
    try:
        save_heatmap(Path(paths["results_dir"]) / "heatmaps", run_id, heatmap)
    except HeatmapError as exc:
        raise JobError(EXIT_FAILED, f"结果已保存(run_id={run_id}),但热力图写入失败: {exc}") from exc


def prior_run_variances(paths: dict) -> list[float]:
    """账本里最近各次评估的格子夏普方差,供子进程算 DSR 的经验方差(取中位数)。账本损坏则拒绝作业(fail-closed)。"""
    try:
        return grid_run_variances(trials_path(paths))
    except TrialLedgerError as exc:
        raise JobError(EXIT_REFUSED, str(exc)) from exc


def run_variances_argv(variances: list[float]) -> list[str]:
    """把先验方差列表变成子进程命令行参数(JSON,repr 保证浮点数无损往返;至多 MAX_RUNS_USED 个)。"""
    return ["--prior-run-variances", json.dumps([float(v) for v in variances[-MAX_RUNS_USED:]])]


def record_job_sharpes(paths: dict, body: dict) -> None:
    """作业**成功返回后**把本次的逐试验夏普写进账本(不计入试验次数),并把它们从结果里拿掉。

    - 网格里每个有效格子的日夏普(`trials.cell_sharpes`)记为 grid;门槛评估的池化样本外日夏普记为 gate;
    - 样本可能来自候选进程:**整批校验**,有一个坏值就整批丢弃(不是只信剩下的),并在结果里写明丢弃;
    - 写不进账本就拒绝(fail-closed):少记样本只会让 V 退回理论值,但不能让它**静默**少记。"""
    trials = body.get("trials")
    if isinstance(trials, dict) and "cell_sharpes" in trials:
        raw = trials.pop("cell_sharpes")
        clean = valid_trial_sharpes(raw) if raw else None
        if raw and clean is None:
            trials["cell_sharpes_dropped"] = "整批丢弃:含非有限数或超出上限的值"
        elif clean:
            try:
                record_sharpes(trials_path(paths), "grid", clean, at=time.time())
            except TrialLedgerError as exc:
                raise JobError(EXIT_REFUSED, str(exc)) from exc
    metrics = body.get("metrics")
    oos = metrics.get("oos_sharpe") if isinstance(metrics, dict) else None
    if isinstance(oos, (int, float)) and not isinstance(oos, bool) and math.isfinite(oos):
        daily = valid_trial_sharpes([oos / math.sqrt(TRADING_DAYS_PER_YEAR)])
        if daily:
            try:
                record_sharpes(trials_path(paths), "gate", daily, at=time.time())
            except TrialLedgerError as exc:
                raise JobError(EXIT_REFUSED, str(exc)) from exc



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

    回传前把本机路径替换掉:父层对调用方的承诺是"message 不含本机路径与密钥"。"""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return "子进程拒绝执行(原因见本机日志)"
    reason = lines[-1]
    # Windows 盘符路径(含带空格的目录)、POSIX 绝对路径、引号里的整段路径都要盖住:
    # 早先只处理"不含空格的盘符路径",`C:\Program Files\...` 这类只替换掉前半段。
    reason = re.sub(r"[A-Za-z]:[\\/][^\s\"']*", "<路径>", reason)
    reason = re.sub(r"(?<![\w.])/(?:[^\s\"']*/)*[^\s\"']*", "<路径>", reason)
    reason = re.sub(r"\"[^\"]*\"|'[^']*'", "<路径>", reason)
    return reason[:200]


# ---------------- 子进程结果通道 ----------------
# 结果**不走 stdout**。
#
# 早先的约定是:子进程把结果打成一行 `RESEARCH_RESULT_JSON:{...}`,父进程取 stdout 里
# **最后**一行以这个前缀开头的行。stdout 是候选自己的地盘——候选可以随便 print,
# 也可以注册 atexit 钩子(atexit 不在沙盒的禁用模块名单里)在框架打印之后再打一行,
# 于是候选伪造的那一行会**赢**(第 27 轮 codex 的 Critical)。
#
# 现在:父进程造一份**每次作业都不一样**的交接单(随机临时目录 + 随机结果文件路径 + 随机 MAC 密钥),
# 经子进程的 stdin 交给子进程;子进程把结果写进那个文件并附上 MAC;父进程**验签**通过才采信,
# 对不上就 fail-closed(stderr 留证据)。stdout 只当日志。
#
# 残余(诚实登记,见 ADR 第 27 轮):候选代码与框架代码在**同一个子进程**里跑,
# 候选若能反射到运行时(拿到 sys、翻到帧的局部变量),仍可能读到交接单并伪造结果。
# 要彻底堵住,得让候选在**另一个进程**里执行、由框架进程独立汇总——那是架构改动,已登记为后续工作。
# 现在能堵住的是**盲伪造**:不知道随机路径与随机密钥的伪造(含 atexit 打印那一行)一律被验签拒绝。
#
# 写结果用到的运行时对象在**模块导入时**(候选还没跑)先按住指纹:候选若改写了它们,
# 结果就可能被它编排,这种结果一律不采信。
_JSON_DUMPS = json.dumps
_JSON_LOADS = json.loads
_HMAC_NEW = hmac.new
_SHA256 = hashlib.sha256
_OS_REPLACE = os.replace
_PATH_WRITE_TEXT = Path.write_text
_ATEXIT_CLEAR = atexit._clear


def _make_handoff() -> dict:
    """父进程造交接单:随机临时目录 + 随机结果文件路径 + 随机 MAC 密钥。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="research-result-"))
    return {"path": str(tmpdir / "result.json"), "key": secrets.token_hex(32), "dir": str(tmpdir)}


def _discard_handoff(handoff: dict) -> None:
    """结果读走之后清掉临时目录(读失败也要清)。"""
    try:
        shutil.rmtree(handoff.get("dir", ""), ignore_errors=True)
    except OSError:
        pass


def check_result_bounds(body: dict) -> None:
    """结果里的数字必须站得住:验证窗口的夏普超出合理上界 → 拒绝采信(伪造的兜底)。

    验签挡的是"结果被人换过";这里挡的是"结果里的数字本身不合理"(例如被编排成 oos_sharpe=1e9)。
    策略门槛(夏普上界、DSR)一律不放宽——这里只是把明显不成立的结果挡在门外。

    **两种结果形状都要查**:门槛/评估作业的结果把夏普放在 `metrics.oos_sharpe` / `metrics.stress_sharpe`,
    而**回测**作业的结果放在 `stats.sharpe`(第 27 轮 codex 的 Critical 伪造的正是这个形状)。
    只查一种形状 = 给另一种形状留了一个"数字明显不成立却照样被采信"的口子。"""
    def out_of_range(label: str, value) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or abs(value) > MAX_ABS_OOS_SHARPE):
            raise JobError(EXIT_FAILED, f"子进程回传的 {label} 超出合理范围,已拒绝采信")

    metrics = body.get("metrics")
    if isinstance(metrics, dict):
        for key in ("oos_sharpe", "stress_sharpe"):
            value = metrics.get(key)
            if value is None:
                continue
            out_of_range(key, value)
    stats = body.get("stats")
    if isinstance(stats, dict) and stats.get("sharpe") is not None:
        out_of_range("stats.sharpe", stats["sharpe"])


def _read_child_result(handoff: dict) -> dict:
    """读回子进程写的结果并**验签**。签名对不上 = 结果被换过 → 拒绝采信(fail-closed)。"""
    path = Path(handoff["path"])
    try:
        size = path.stat().st_size
    except OSError:
        raise JobError(EXIT_FAILED, "子进程没有回传结果(结果文件缺失)")
    if size > MAX_RESULT_BYTES:
        raise JobError(EXIT_FAILED, "子进程结果文件过大,已拒绝采信")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise JobError(EXIT_FAILED, "子进程没有回传结果(结果文件缺失)")
    try:
        envelope = json.loads(raw)
        body_text, mac = envelope["body"], envelope["mac"]
    except (ValueError, KeyError, TypeError):
        raise JobError(EXIT_FAILED, "子进程结果信封不合法")
    if not isinstance(body_text, str) or not isinstance(mac, str):
        raise JobError(EXIT_FAILED, "子进程结果信封不合法")
    expected = hmac.new(handoff["key"].encode("utf-8"), body_text.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise JobError(EXIT_FAILED, "子进程结果校验失败(疑似被伪造),已拒绝采信")
    try:
        body = json.loads(body_text)
    except ValueError:
        raise JobError(EXIT_FAILED, "子进程结果不是合法 JSON")
    if not isinstance(body, dict):
        raise JobError(EXIT_FAILED, "子进程结果不是 JSON 对象")
    check_result_bounds(body)
    return body


def spawn_child(cmd: list[str], timeout: float) -> dict:
    """在受限环境(环境变量白名单)的子进程里运行,返回子进程回传的 JSON;失败抛 JobError。

    结果通道见上方说明:交接单经 stdin 交给子进程,结果写进随机文件并附 MAC,父进程验签后才采信。
    stdout 只是日志(候选爱打印什么打印什么),不再参与结果的判定。"""
    handoff = _make_handoff()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", cwd=bootstrap.ROOT,
                                env=rs.sandbox_env(os.environ))
        try:
            stdout, stderr = proc.communicate(input=json.dumps(handoff, ensure_ascii=False) + "\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.communicate()
            raise JobError(EXIT_TIMEOUT, f"超过 {timeout}s,已终止")
        if proc.returncode == EXIT_REFUSED:
            # 子进程的"拒绝"要**原样传到父层**,不能一律压成"回测失败":
            # 子入口自己复查的那些项(受保护参数、路径越界、审批缺失)的原因必须能回到调用方。
            print("拒绝:" + chr(10) + (stderr or stdout)[-1500:], file=sys.stderr)
            raise JobError(EXIT_REFUSED, "拒绝执行:" + _child_reason(stderr))
        if proc.returncode != 0:
            # 详细输出只留在本机 stderr;回传给调用方的 message 不含子进程原始输出(可能含本机路径)
            print("失败:" + chr(10) + (stderr or stdout)[-1500:], file=sys.stderr)
            raise JobError(EXIT_FAILED, "回测失败(详情见本机日志)")
        return _read_child_result(handoff)
    finally:
        _discard_handoff(handoff)


NEEDS_APPROVAL = "策略代码尚未批准:请先读代码并执行 research_api.py approve --strategy <文件>(或本次加 --confirm-reviewed)"


def check_approval(path: Path, paths: dict, confirm: bool, sha256: str | None = None) -> None:
    """人工批准检查,未通过抛 JobError。confirm=True(仅本机 CLI 可传)等价于「我现在读过并批准」,会记录哈希。

    `sha256` 是**已经读出来的那份字节**的哈希:父进程只读一次候选文件,
    审批检查、静态筛查、传给子进程的哈希、结果里记的哈希全部用这同一个值——
    否则"审批时看到的""筛查时看到的""执行时校验的""结果里记的"可能是四份不同的内容。"""
    try:
        if confirm:
            rs.record_approval(path, paths["approvals_file"])
        elif sha256 is not None:
            if sha256 not in rs._load_approvals(paths["approvals_file"]):
                raise JobError(EXIT_REFUSED, NEEDS_APPROVAL)
        elif not rs.is_approved(path, paths["approvals_file"]):
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


def child_run_variances(args) -> list[float] | None:
    """子进程自己校验先验方差列表 `--prior-run-variances`(JSON 数组)。

    它也是调用方给的值,不能假设父进程查过。这些数进的是 DSR 的 V,而 V 取 max(中位数, 理论值),只能更严;
    但仍要卡住范围:个数 ≤ MAX_RUNS_USED,每个是 [0, 1] 内的有限数(格子日夏普限幅在 ±1 以内,方差不可能更大)。
    返回 None 表示没给(DSR 按"样本不足"退回理论值,并在结果里写明)。"""
    raw = getattr(args, "prior_run_variances", None)
    if raw is None:
        return None
    if not isinstance(raw, str):                       # 命令行参数一定是 str;bytes/数字之类一律拒
        raise JobError(EXIT_REFUSED, "--prior-run-variances 必须是 JSON 数组字符串")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise JobError(EXIT_REFUSED, "--prior-run-variances 必须是 JSON 数组") from exc
    if not isinstance(values, list) or len(values) > MAX_RUNS_USED:
        raise JobError(EXIT_REFUSED, f"--prior-run-variances 必须是个数 ≤ {MAX_RUNS_USED} 的数组")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise JobError(EXIT_REFUSED, "--prior-run-variances 里每个数必须是 [0, 1] 内的有限数")
    return [float(v) for v in values]


def child_file(path: Path) -> Path:
    """子进程自己确认候选文件仍在 pending 目录内。

    父入口用 `resolve_pending` 解析过路径,但子入口能被直接调用,
    不该假设路径是父进程给的——否则可以直接把 `--file` 指到仓库里任何 .py。"""
    cfg = rc.load()
    try:
        return rs.resolve_pending(str(path), bootstrap.ROOT, cfg["paths"]["pending_dir"])
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
        if not rs.is_approved_sha(sha256, paths["approvals_file"]):
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
                                cfg["screening"]["extra_forbidden_calls"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    try:
        code = compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise JobError(EXIT_REFUSED, f"策略文件语法错误: {exc}") from exc
    # 模块元数据用标准 loader 准备(保留 __spec__/__loader__/__package__),
    # 但**不调用** exec_module:执行的是上面编译好的、经过校验的那份字节,
    # .pyc 没有机会生效(那正是第 4 轮修掉的 .pyc 绕过)。
    module = types.ModuleType("pending_strategy")
    module.__file__ = str(path)
    try:
        spec = importlib.util.spec_from_file_location("pending_strategy", path)
        if spec is not None:
            module.__spec__ = spec
            module.__loader__ = spec.loader
            module.__package__ = spec.parent or ""
            module.__name__ = spec.name
    except Exception:                                        # noqa: BLE001 - 元数据拿不到不该拦住执行
        pass
    # 候选执行时用**最小内建集合**:静态层能封住已知的写法,封不住能力本身——
    # `e.__traceback__.tb_frame.f_builtins.setattr(...)` 这类路径上的名字都不在禁用名单里,
    # 而 `unittest.mock.patch.object` 只要能 import 就能改写实例属性。
    # 把 setattr/getattr/eval/exec/open 这些**从内建里拿掉**,那些路径自然就不存在了
    # (连 frame.f_builtins 也只会拿到这份受限字典)。详见 scripts/research_sandbox.py::safe_builtins。
    module.__dict__["__builtins__"] = rs.safe_builtins(cfg["screening"]["extra_forbidden_modules"])
    # 运行期第四层:候选执行**前后**各拍一张"受保护对象身份指纹"。
    # 静态层按名字判断,拦不住"换个写法"改写框架对象——第 9 轮 codex 实测:
    # `import risk.risk_guard as rg; rg.risk_per_trade = lambda ...: 0.0`
    # 三行就架空 300 元单笔预算,而四层防线全部放行。指纹比对不看写法,只看结果:
    # 不管候选用哪种写法(rg 直接改、别名改、type(cls) 改),换了对象就对不上。
    protected_before = const.snapshot_protected_state()
    try:
        exec(code, module.__dict__)                           # noqa: S102 —— 执行的就是上面校验过的那份字节
    except (Exception, SystemExit) as exc:                     # noqa: BLE001 - 候选自身的错,按结构化错误回传
        # SystemExit 也要兜住:候选在导入期 `raise SystemExit()` 不该让子进程带 traceback 死掉,
        # 父进程只会看到"回测失败",那条给 LLM 的诊断通道就断了。
        raise CandidateError(describe_error(exc, PHASE_DECLARATION)) from exc
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
    raw, source = read_candidate(path)           # 字节数按原始字节算:换行转换后的长度不是文件真实大小
    sha = rs.file_sha256(path)
    findings = screen(path, cfg)
    write(f"文件: {path.name}   大小: {len(raw)} 字节   sha256: {sha}")
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
    # 只读一次:哈希、筛查、审批、子进程参数、结果记录全部用这同一份字节。
    raw, source = read_candidate(path)
    strategy_sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"])
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
                                cfg["screening"]["extra_forbidden_calls"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    check_approval(path, paths, confirm, strategy_sha)
    variances = prior_run_variances(paths)
    trials = charge_trials(paths, "gate_run", 1)      # 这次门槛评估本身算一次试验(先记账再跑,失败不退),账本为空也不给 0

    cmd = [sys.executable, str(Path(__file__).resolve()), "_gate_child", "--file", str(path), "--cls", cls,
           "--params", json.dumps(params), "--random-runs", str(random_runs),
           "--on-plateau", "1" if on_plateau else "0", "--sha256", strategy_sha,
           "--prior-trials", str(trials), *run_variances_argv(variances)]
    body = spawn_child(cmd, timeout)
    if "error" not in body:
        record_job_sharpes(paths, body)
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


def save_with_report(result: dict, paths: dict) -> None:
    """落盘前补上确定性中文报告(纯函数生成,出错也只是一行说明),并写入 run_id。"""
    result["report"] = render_report(result)
    result["run_id"] = rs.save_result(result, paths["results_dir"])


def check_campaign(campaign) -> dict | None:
    """校验战役信息 {campaign_id, parent_sha256, generation}(都是调用方给的,不可信)。没有则返回 None。"""
    if campaign is None:
        return None
    if not isinstance(campaign, dict) or set(campaign) - {"campaign_id", "parent_sha256", "generation"}:
        raise JobError(EXIT_REFUSED, "campaign 只接受 campaign_id / parent_sha256 / generation 三个字段")
    campaign_id, parent, generation = campaign.get("campaign_id"), campaign.get("parent_sha256"), campaign.get("generation")
    if not isinstance(campaign_id, str) or not lineage.CAMPAIGN_ID.fullmatch(campaign_id):
        raise JobError(EXIT_REFUSED, "campaign_id 不合法(字母数字开头,可含 . _ -,至多 64 个字符)")
    if parent is not None and (not isinstance(parent, str) or not lineage.SHA256.fullmatch(parent)):
        raise JobError(EXIT_REFUSED, "parent_sha256 必须是 64 位小写十六进制或不填")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation <= lineage.MAX_GENERATION:
        raise JobError(EXIT_REFUSED, "generation 必须是非负整数")
    return {"campaign_id": campaign_id, "parent_sha256": parent, "generation": generation}


def record_lineage(paths: dict, campaign: dict, result: dict, source: str) -> None:
    """结果已落盘之后把它记进谱系台账。写不进去就大声失败:缺失的台账记录不能让进化曲线显示成正常。"""
    try:
        record = lineage.record_from_result(result, source, parent_sha256=campaign["parent_sha256"],
                                            generation=campaign["generation"])
        lineage.append_record(Path(paths["results_dir"]) / "evolution", campaign["campaign_id"], record)
    except lineage.LineageError as exc:
        raise JobError(EXIT_FAILED, f"结果已保存(run_id={result.get('run_id')}),但谱系台账写入失败: {exc}") from exc


def evaluate_job(*, strategy: str, cls: str, random_runs: int | None = None, timeout: float | None = None,
                 confirm: bool = False, campaign: dict | None = None) -> dict:
    """候选在类上声明 `param_grid`（三个参数、各至少 3 个递增取值）：先在选参窗口扫网格并判定参数高原，
    再用选出的参数在滚动验证窗口上做完整的晋级门槛评估。"""
    cfg, paths = settings()
    campaign = check_campaign(campaign)                  # 先查:战役信息不合法就不必白跑一次
    timeout = timeout if timeout is not None else cfg["gate"]["timeout_seconds"] + cfg["grid"]["timeout_seconds"]
    random_runs = random_runs if random_runs is not None else cfg["gate"]["random_runs"]
    path = resolve_strategy(strategy, cfg)
    low, high = rc.INT_BOUNDS[("gate", "random_runs")]
    if isinstance(random_runs, bool) or not isinstance(random_runs, int) or not low <= random_runs <= high:
        raise JobError(EXIT_REFUSED, f"random_runs 必须是 [{low}, {high}] 内的整数(配置边界)")
    raw, source = read_candidate(path)                  # 只读一次,见 backtest_job 的说明
    strategy_sha = hashlib.sha256(raw).hexdigest()
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"])
    if findings:
        raise JobError(EXIT_REFUSED, "静态筛查未通过:" + chr(10) + "  " + (chr(10) + "  ").join(findings))
    check_approval(path, paths, confirm, strategy_sha)
    prior = prior_trials(paths)                   # 先读后扣:子进程算 DSR 用 prior + 实际格子数,预扣的最小网格不能算两次
    variances = prior_run_variances(paths)        # 同样先读:本次的夏普是作业跑完才有的,不能进自己的先验
    charge_trials(paths, "grid_cell", MIN_GRID_CELLS)

    cmd = [sys.executable, str(Path(__file__).resolve()), "_evaluate_child", "--file", str(path), "--cls", cls,
           "--random-runs", str(random_runs), "--sha256", strategy_sha, "--prior-trials", str(prior),
           *run_variances_argv(variances)]
    body = spawn_child(cmd, timeout)
    heatmap, heatmap_note = None, None
    # 格子数补记**不论成败**:失败的一轮同样消耗了试验(ADR 第 9 条)。
    # 早先只在成功时补记,于是"扫了很多格子却在验证期失败"的轮次少记格子数,
    # 后续的 DSR 少算试验次数 = 悄悄放松已锁定的 DSR ≥ 0.95。
    top_up_grid_trials(paths, body)
    if "error" not in body:
        record_job_sharpes(paths, body)
        heatmap, heatmap_note = take_heatmap(body)
    base = {"kind": "evaluate", "strategy_file": path.name, "strategy_sha256": strategy_sha,
            "class": cls, "key": "C15", "protocol_notice": EVALUATE_NOTICE,
            "ran_at": datetime.now().isoformat(timespec="seconds")}
    if "error" in body:
        result = {**base, "status": "strategy_error", "passed": False, "error": body["error"]}
        if isinstance(body.get("trials"), dict):
            result["trials"] = body["trials"]              # 失败的一轮用掉的试验次数要看得见
    else:
        result = {**base, **body}
        if heatmap_note:
            result["heatmap_note"] = heatmap_note
    save_with_report(result, paths)
    if heatmap is not None:
        save_job_heatmap(paths, result["run_id"], heatmap)
    if campaign is not None:
        record_lineage(paths, campaign, result, source)
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


def _child_handoff() -> dict:
    """子进程在**写结果的那一刻**才从 stdin 读父进程的交接单。

    交接单走 stdin(不是环境变量):环境变量会原样进入子进程,而候选能通过反射摸到运行时;
    stdin 在候选执行期间还躺在管道里,候选若偷读,框架随后就读不到 → 直接拒绝(fail-closed),
    也就是"偷交接单"只会让这次作业**响亮地失败**,不会让伪造的结果被采信。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            raise OSError("没有可读的 stdin")
        line = sys.stdin.readline()
    except (OSError, ValueError):
        raise JobError(EXIT_REFUSED, "缺少父进程交接单:子入口只能由父进程拉起")
    try:
        handoff = json.loads(line)
    except ValueError:
        raise JobError(EXIT_REFUSED, "父进程交接单不是合法 JSON")
    if (not isinstance(handoff, dict) or not isinstance(handoff.get("path"), str)
            or not isinstance(handoff.get("key"), str) or not handoff["key"]):
        raise JobError(EXIT_REFUSED, "父进程交接单不合法")
    return handoff


def _clear_atexit() -> None:
    """清掉候选注册的 atexit 钩子。

    结果写完就退出:钩子若还能跑,就只剩"事后篡改"的机会(候选可以顺手把结果文件改掉)。
    框架自己在这条路径上没有必须执行的 atexit,所以整体清空是安全的。

    用**导入时按住的那个** `atexit._clear`:候选若把 `atexit._clear` 换成空函数,
    这里调用的就还是原版(否则"清空"这一步会被候选自己取消)。"""
    try:
        _ATEXIT_CLEAR()
    except Exception:                                    # noqa: BLE001 - 清不掉就清不掉,验签照样兜着
        pass


def emit_result(body: dict) -> None:
    """把结果写进父进程建的随机文件,并附上 MAC。stdout 只是日志,不是结果通道。"""
    if (json.dumps is not _JSON_DUMPS or json.loads is not _JSON_LOADS or hmac.new is not _HMAC_NEW
            or hashlib.sha256 is not _SHA256 or os.replace is not _OS_REPLACE
            or Path.write_text is not _PATH_WRITE_TEXT or atexit._clear is not _ATEXIT_CLEAR):
        raise JobError(EXIT_REFUSED, "写结果前发现运行时对象被改写,拒绝回传结果")
    handoff = _child_handoff()
    # 真结果已经算好了,现在把候选注册的 atexit 钩子清掉:钩子是在进程退出时跑的,
    # 不清掉的话它能在**结果写完之后**再改一次那个文件(改坏了会被父进程验签拒掉,
    # 但那是一次"结果明明跑完了却被判失败"的响亮失败)。清空只影响这一个子进程。
    _clear_atexit()
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    mac = hmac.new(handoff["key"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    path = Path(handoff["path"])
    envelope = json.dumps({"mac": mac, "body": payload}, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(envelope, encoding="utf-8")
    os.replace(tmp, path)


def _emit_or_fail(body: dict) -> int:
    """把结果写回去;写不回去(没有交接单、信封坏了)就**响亮地失败**,不静默退出 0。"""
    try:
        emit_result(body)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    return EXIT_OK


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
        run_variances = child_run_variances(args)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if args.on_plateau not in ("0", "1"):
        print("--on-plateau 只能是 0 或 1", file=sys.stderr)
        return EXIT_REFUSED
    try:
        cls = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CandidateError as exc:
        return _emit_or_fail({"error": exc.detail})
    bars = load_bars_csv(wf.DATA_FILE, wf.SYMBOL, Exchange.DCE)
    windows = [(w.name, w.valid_start, w.valid_end) for w in wf.build_windows()]
    try:
        result = run_gate(candidate_cls=cls, params=params, windows=windows, bars=bars,
                          runner=engine_runner(wf.KEY), random_cls=RandomEntry15mStrategy, random_runs=random_runs,
                          data_adjusted=True, on_plateau=args.on_plateau == "1", capital=DEFAULT_CAPITAL,
                          prior_trials=prior_trials, run_variances=run_variances)
    except Exception as exc:
        # 门槛评估跑在滚动**验证窗口**上:这一层的报错原文不外传(阶段=validation)
        return _emit_or_fail({"error": describe_error(exc, PHASE_VALIDATION)})
    return _emit_or_fail(result)


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
        run_variances = child_run_variances(args)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        candidate = import_candidate(path, args.cls, args.sha256)
    except JobError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except CandidateError as exc:
        return _emit_or_fail({"error": exc.detail})
    bars = load_bars_csv(wf.DATA_FILE, wf.SYMBOL, Exchange.DCE)
    windows = wf.build_windows()
    cfg = rc.load()                          # 选参网格上限来自配置:统一入口重构时漏掉了这一行
    try:
        result = run_evaluation(
            candidate_cls=candidate, bars=bars, runner=engine_runner(wf.KEY), random_cls=RandomEntry15mStrategy,
            random_runs=random_runs, capital=DEFAULT_CAPITAL, max_cells=cfg["grid"]["max_cells"],
            min_round_trips=cfg["grid"]["min_round_trips"], select_window=(windows[0].train_start, windows[0].train_end),
            validation_windows=[(w.name, w.valid_start, w.valid_end) for w in windows], prior_trials=prior_trials,
            run_variances=run_variances)
    except Exception as exc:
        # 评估跑在**验证窗口**上(选参窗口里的错由 research_gate 自己分段并显式给阶段);
        # 这里兜住的是框架侧的意外异常,按验证窗口处理(fail-closed:原文不外传)
        return _emit_or_fail({"error": describe_error(exc, PHASE_VALIDATION)})
    return _emit_or_fail(result)


def run_child(args) -> int:
    """子进程:加载策略并回测。只在受限环境(sandbox_env)里由 cmd_backtest 拉起。"""
    from backtest_lib import run_backtest
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
        return _emit_or_fail({"error": exc.detail})
    try:
        stats, _, trades = run_backtest(strategy_class, params, args.key, start, end)
    except Exception as exc:               # 策略自己的 bug:回传结构化摘要(不含路径),退出码仍为 0
        # 阶段由**段**决定:训练段就是选参窗口那一层(放开),验证段按验证窗口处理(原文不外传)
        phase = PHASE_SELECTION if args.segment == "train" else PHASE_VALIDATION
        return _emit_or_fail({"error": describe_error(exc, phase)})
    row = backtest_stats_row(stats)          # 投影规则见 risk/report.py:面板校验用的是同一份
    pnls = rs.round_trip_pnls(trades, INSTRUMENTS[args.key].size)
    return _emit_or_fail({"stats": row, "pnls": pnls})


# ---------- results ----------

def cmd_results(args) -> int:
    _, paths = settings()
    try:
        emit(rs.load_result(args.run_id, paths["results_dir"]) if args.run_id else rs.list_results(paths["results_dir"]))
    except rs.SandboxError as exc:
        return refuse(str(exc))
    return EXIT_OK


def _add_run_variance_args(parser: argparse.ArgumentParser) -> None:
    """内部子命令的先验方差列表(见 child_run_variances):JSON 数组字符串。"""
    parser.add_argument("--prior-run-variances", default=None)


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
    _add_run_variance_args(gchild)
    gchild.set_defaults(func=run_gate_child)
    echild = sub.add_parser("_evaluate_child")  # 内部使用
    for name in ("file", "cls", "sha256"):
        echild.add_argument(f"--{name}", required=True)
    echild.add_argument("--random-runs", type=int, required=True)
    echild.add_argument("--prior-trials", type=int, default=None)
    _add_run_variance_args(echild)
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
