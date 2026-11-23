"""research-mcp:DSH 里的研究入口(只暴露三个工具)。

边界(ADR 20261113-dsh-research-entry-and-out-of-band-approval;Issue #59 修订,见 ADR 20261120):
  - DSH 只是对话入口,毕昇仍是编排器。本服务把「想法」交给毕昇工作流,读回结构化结果。
  - **模型不能授予批准;批准由人触发。** 写 approvals.json 的路径有两条:
    a) 带外批准页(独立进程 `scripts/research_approval_server.py`,人输完整哈希);
    b) 会话内哈希回贴(Issue #59 方案 A,用户明确接受其削弱):人**亲自**把工具返回的
       64 位哈希粘贴进会话,模型带 confirm_sha256 调用本工具,服务端比对自算哈希后经
       `record_approval`(唯一写入者)落账。落账哈希用的是**服务端自算值**,
       与盘上文件绑定;模型无法让账本记录与文件内容不符。
    两者都只认当前会话本轮的候选,都要求静态筛查先通过。
  - 不暴露提交源码、回测、门槛、装载策略、读写任意文件、任意命令。
  - **批准页的 URL 一律不进工具返回值**(codex 第 2 轮实测复现的 Critical:模型拿到
    URL+哈希就能自己 POST 批准页)。inline 模式的**候选哈希有意放行**——人要粘贴的就是它;
    除这一个值外,脱敏规则(路径/URL/命令/凭据)照旧全洗。

工具(原始名用 ASCII:DSH 的模型侧函数名要满足 DeepSeek 的命名约束;ADR 里的中文名是概念名):
  - start_research(idea)   发起研究:把想法填进毕昇工作流的输入节点并启动
  - research_status()       查进度:当前节点、最近输出;若人工批准已落地则推进工作流
  - request_approval(name, confirm_sha256?, mode?) 请求批准:inline(默认)= 会话内两阶段;
    page = 带外批准页(原样保留)

配置(环境变量,DSH 侧需显式传入——DSH 默认不把 KEY/TOKEN/SECRET/PASSWORD 名的变量传给子进程):
  BISHENG_OPENAPI_KEY  毕昇开放 API 密钥(bs-sak-*);缺省读 <root>/.local/bisheng-openapi.key
  BISHENG_BASE_URL     默认 http://127.0.0.1:7860/api/v2
  BISHENG_WORKFLOW_ID  默认「invest 研究工作流 v3(15分钟+门槛)」
  BISHENG_IDEA_NODE    默认 input_idea
  INVEST_ROOT          仓库根目录(默认按本文件位置推断)
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

CODE_ROOT = Path(__file__).resolve().parents[2]          # 代码所在检出(批准脚本从这里找)
ROOT = Path(os.environ.get("INVEST_ROOT") or CODE_ROOT).resolve()  # 数据根(候选目录/批准记录/网关挂载的那棵)
# **代码一律从 CODE_ROOT 取**:ROOT 是**数据**根(候选目录/批准记录/配置/密钥),
# 它的 scripts/ 目录未必与 CODE_ROOT 是同一份代码——worktree + INVEST_ROOT 就是这种情形
# (worktree 的代码 + 主检出的数据)。从 ROOT/scripts 导入会执行**另一份**代码,
# 表现是版本错配的接口对不上(第 6 轮实测:新 server.py 配旧 research_sandbox.py → AttributeError)。
# 数据根的切换由 research_config 自己处理(它读 INVEST_ROOT),这里只管代码。
if str(CODE_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(CODE_ROOT / "scripts"))

import research_config as rc           # noqa: E402
import research_console as rconsole    # noqa: E402
import research_sandbox as rs          # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:7860/api/v2"
DEFAULT_WORKFLOW_ID = "1194f8540b9944fea4353b07f0493cbf"     # invest 研究工作流 v3(15分钟+门槛)
DEFAULT_IDEA_NODE = "input_idea"
STATE_FILE = ROOT / ".local" / "research-mcp" / "sessions.json"
KEY_FILE = ROOT / ".local" / "bisheng-openapi.key"
# **没有「批准页地址文件」**:地址只发给浏览器,不落盘、不进模型通道
# (第 5 轮 codex 的 H1——写盘的文件只在「模型没有本地文件工具」这个未验证的前提下才安全)。
APPROVAL_WAIT_SECONDS = 600
READY_TIMEOUT_SECONDS = 30
MAX_IDEA_LEN = 2000

# 批准子进程只拿到跑起来必需的这些环境变量:不传 CTP 凭据、不传 .env 交易段
# (ADR 第 3 条的边界声明要落到实处,而不是只在文档里说)。
APPROVAL_ENV_ALLOW = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "TZ", "VIRTUAL_ENV", "RESEARCH_SANDBOX_CONFIG",
)

mcp = FastMCP("research-mcp", instructions=(
    "研究入口:把研究想法交给毕昇工作流跑,由人在本机审阅并批准候选策略。"
    "你只能发起研究、查进度、请求批准;批准动作由人完成,你无权批准。"
))


# ---------- 配置 ----------

def openapi_key() -> str:
    """毕昇开放 API 密钥:与网关/CLI **同源**(第 14 轮 codex 的 M3/L10)。

    口径在 `research_config.bisheng_openapi_key`:环境变量 `BISHENG_OPENAPI_KEY` 优先,
    其次密钥文件(这里是 MCP 侧自己的 `KEY_FILE`)。原先这条口径只写在这里,
    别的通道没有——于是同一份凭据在三条通道里的「从哪读」都不一样。
    """
    return rc.bisheng_openapi_key(ROOT, key_file=KEY_FILE)


def base_url() -> str:
    return (os.environ.get("BISHENG_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def workflow_id() -> str:
    return (os.environ.get("BISHENG_WORKFLOW_ID") or DEFAULT_WORKFLOW_ID).strip()


def idea_node() -> str:
    return (os.environ.get("BISHENG_IDEA_NODE") or DEFAULT_IDEA_NODE).strip()


def load_config() -> dict:
    """读沙盒配置:优先用**数据根**(INVEST_ROOT)里的那份,否则用代码根的。

    批准进程读的是网关实际挂载的那棵目录树,配置也得跟它一致。
    """
    data_cfg = ROOT / "config" / "research_sandbox.json"
    if data_cfg.is_file():
        return rc.load(data_cfg)
    return rc.load()


def approvals_file() -> Path:
    cfg = load_config()
    return rc.resolve_paths(cfg, ROOT)["approvals_file"]


def pending_dir() -> Path:
    cfg = load_config()
    return rc.resolve_paths(cfg, ROOT)["pending_dir"]


def pending_dir_name() -> str:
    """候选目录名(来自配置):网关/CLI 用的就是这一个,这里必须一致。"""
    return load_config()["paths"]["pending_dir"]


# ---------- 会话状态 ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def current_session() -> dict | None:
    state = load_state()
    session = state.get("current")
    return session if isinstance(session, dict) else None


def set_current(session: dict) -> None:
    state = load_state()
    state["current"] = session
    save_state(state)


# ---------- 毕昇开放 API ----------

class BishengError(RuntimeError):
    pass


def invoke(*, session_id: str | None = None, message_id: int | None = None,
           node: str | None = None, text: str | None = None, timeout: int = 900) -> dict:
    """调毕昇开放 API。返回 {session_id, events}。

    续跑协议(实测确定):**必须**同时带 session_id 与 message_id;
    message_id 是上一次 invoke 返回的、等待输入那条消息的 id。
    """
    key = openapi_key()
    if not key:
        raise BishengError("没找到毕昇开放 API 密钥:设 BISHENG_OPENAPI_KEY 或放 " + str(KEY_FILE))
    body: dict = {"workflow_id": workflow_id(), "stream": False}
    if session_id:
        body["session_id"] = session_id
    if message_id:
        body["message_id"] = message_id
    if node is not None:
        body["input"] = {node: {"user_input": text or "", "dialog_files_content": []}}
    req = urllib.request.Request(
        base_url() + "/workflow/invoke",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise BishengError(f"毕昇开放 API 返回 {exc.code}:{exc.read()[:300].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as exc:
        raise BishengError(f"连不上毕昇开放 API({base_url()}):{exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        # 读超时抛的是 TimeoutError(OSError 子类),**不是** URLError:
        # 不接住它,裸异常会一路冲到模型面前(变成 ToolError 而不是可读的失败说明)。
        raise BishengError(f"毕昇开放 API 请求失败({base_url()}):{type(exc).__name__}: {exc}") from None
    if payload.get("status_code") != 200:
        raise BishengError(f"毕昇开放 API 报错:{payload.get('status_message')}")
    return payload.get("data") or {}


def last_input_event(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event") == "input":
            return event
    return None


def output_texts(events: list[dict]) -> list[str]:
    texts = []
    for event in events:
        if event.get("event") != "output_msg":
            continue
        schema = event.get("output_schema") or {}
        message = schema.get("message")
        if isinstance(message, str) and message.strip():
            texts.append(message)
    return texts


# ---------- 回给模型的文本要过一道脱敏 ----------

# 「python scripts/research_api.py approve --strategy x.py」这类批准命令:
# 念给模型听,模型就能照着去提示人粘贴哈希——「人读过内容」的前提就没了。
# 批准进程自己的命令行(`research_approval_server.py --strategy x`)同样算批准命令:
# 模型拿着它就能诱导人去跑终端里的批准流程,那台机器上没有「只批本轮候选」的约束。
# 规则本体在 research_sandbox 里(网关与 MCP 共用一条口径)。


def known_secret_values() -> tuple[str, ...]:
    """**按值**要盖掉的凭据清单(第 11 轮 codex 的 F6)。

    清洗规则本来是按**名字**认凭据(`CTP_PASSWORD=…`),可候选一旦把值单独写进
    异常文本(不带名字),名字那一层就认不出来——按值替换是最后一道。
    网关侧原本就有(`_scrub(payload, (self.token,))`),MCP 侧原先**没有**,
    于是「网关盖住、MCP 不盖」就是一条漏的通道。

    清单本体在 `research_config.known_secret_values`:三条模型可见通道
    (网关 / MCP / CLI)**共用同一份**(第 14 轮 codex 的 M3/L10)——
    原先 MCP 有「密钥 + 令牌」,CLI 与网关只有令牌,口径漂移的那一条就是漏的那一条。

    这里取两样:MCP 自己手里的毕昇开放 API 密钥,和网关的鉴权令牌。
    读不到就跳过(不因为读不到令牌就让整条脱敏失效),但**在 stderr 留痕**。
    """
    try:
        cfg = load_config()
    except (OSError, ValueError, rc.ConfigError) as exc:
        print(f"[research-mcp] 读不到沙盒配置,按值脱敏这一层降级: {exc}", file=sys.stderr)
        cfg = None
    return rc.known_secret_values(cfg=cfg, root=ROOT, label="research-mcp", key_file=KEY_FILE)


def redact_for_model(text: str) -> str:
    """回给模型的文本先脱敏(ADR 安全约束 #5)。

    - 64 位十六进制(候选的 sha256)不进模型通道:模型拿到哈希就能在聊天里念给人,
      「人核对完整 64 位哈希」这道仪式就退化成橡皮章。
    - 批准命令不进模型通道:同上,模型不该替人安排批准。
    - 批准页地址不进模型通道:地址里带一次性令牌,模型拿到就能自己去凑齐 csrf 与哈希。
    - 本机绝对路径不进模型通道:模型看到本机目录结构没有用处,却多了一条指纹识别的信息。
    - 凭据形态不进模型通道:候选一旦读到 .env 并写进异常文本,凭据就跟着出去了。
    毕昇工作流的自由文本里**可能**自己打印出哈希(网关的提交响应曾经带 sha256),
    所以这道脱敏在出口处做,不指望上游干净。
    口径与网关侧共用 `rs.scrub_text`:两边规则不一样,等于「哪条通道漏了就是漏了」
    (第 6 轮 codex 的 H1、第 7 轮 codex 的 F2/F5)。
    """
    if not text:
        return text
    return rs.scrub_text(text, secrets=known_secret_values())


def node_failures(events: list[dict]) -> list[str]:
    failures = []
    for event in events:
        if event.get("event") == "node_run":
            schema = event.get("output_schema") or {}
            reason = schema.get("reason") if isinstance(schema, dict) else None
            if reason:
                failures.append(str(reason)[:300])
    return failures


def find_candidate(texts: list[str]) -> str | None:
    """从工作流输出里**正则**捞候选文件名(只作兜底)。

    正则捞出来的名字必须再在 pending_strategies/ 里核对存在,否则不认——
    否则模型可以在输出里写任意文件名,把批准流程引到别的文件上。
    """
    for text in texts:
        match = re.search(r"([a-z0-9_]{1,40}\.py)", text)
        if match:
            return match.group(1)
    return None


def pending_snapshot() -> set[str]:
    """候选目录里的候选文件名。

    **只收符合命名规则的 .py**:目录里混进 .json/.swp/.bak 时,它们会被当成候选,
    而候选哈希算不出来 → 本轮永远卡在「候选文件读不到」。
    """
    try:
        pattern = re.compile(load_config()["candidates"]["name_pattern"])
    except (KeyError, TypeError, re.error):
        return set()
    try:
        return {p.name for p in pending_dir().iterdir() if p.is_file() and pattern.fullmatch(p.name)}
    except OSError:
        return set()


def newest_pending(names: set[str]) -> str | None:
    """多个新候选时取**最新**的那个(按修改时间),不是字典序第一个。"""
    best: str | None = None
    best_mtime: float | None = None
    for name in names:
        try:
            mtime = (pending_dir() / name).stat().st_mtime
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = name, mtime
    return best


def candidate_sha(name: str) -> str | None:
    try:
        path = rs.resolve_pending(name, ROOT, pending_dir_name())
    except rs.SandboxError:
        return None
    try:
        return rs.file_sha256(path)
    except OSError:
        return None


def approval_state(session: dict) -> tuple[bool, str]:
    """候选当前的批准状态 (是否已批准, 说明)。

    绑定的是**内容哈希**:发起研究时记下的 candidate_sha 与现在的文件内容必须一致,
    否则「批准过的那份」和「现在这份」不是同一个东西,不能算批准。
    """
    name = session.get("candidate")
    if not name:
        return False, ""
    sha = candidate_sha(name)
    if not sha:
        return False, "候选文件读不到"
    recorded = session.get("candidate_sha")
    if recorded and recorded != sha:
        return False, "候选文件在发起研究后被改动过,批准对不上"
    try:
        approved = rs.is_approved_sha(sha, approvals_file())
    except rs.SandboxError:
        # 批准记录损坏/不可读:fail-closed(不推进工作流),但给模型一句能读懂的话。
        return False, "批准记录读不到或已损坏,拒绝继续"
    except OSError:
        return False, "批准记录读不到"
    if approved:
        return True, ""
    return False, "尚未批准"


def is_candidate_approved(name: str) -> bool:
    """按候选名判断是否已批准(兼容旧调用点)。"""
    sha = candidate_sha(name)
    if not sha:
        return False
    try:
        return rs.is_approved_sha(sha, approvals_file())
    except OSError:
        return False


# ---------- 工具实现(同步核心) ----------

def _start_research(idea: str) -> str:
    idea = (idea or "").strip()
    if not idea:
        return "请给出研究想法(一句话说明要验证什么假设)。"
    if len(idea) > MAX_IDEA_LEN:
        return f"研究想法太长了({len(idea)} 字,上限 {MAX_IDEA_LEN}),请精简后重发。"
    before = pending_snapshot()

    def begin() -> tuple[str | None, str, int | None]:
        """起一个新会话,返回 (session_id, 等待输入的节点, message_id)。"""
        first = invoke()
        events = first.get("events") or []
        waiting = last_input_event(events)
        if waiting is None:
            raise BishengError("毕昇工作流没有停在输入节点,无法提交想法。" + _diagnose(events))
        return first.get("session_id"), waiting.get("node_id"), waiting.get("message_id")

    try:
        session_id = None
        message_id = None
        node = idea_node()
        resumed = False
        previous = current_session()
        if previous and previous.get("waiting_node") == node and previous.get("waiting_message_id"):
            # 上一轮评估完回到想法节点:在同一会话里继续,反馈才对得上
            session_id = previous.get("session_id")
            message_id = previous.get("waiting_message_id")
            resumed = True
        else:
            session_id, node, message_id = begin()
        try:
            second = invoke(session_id=session_id, message_id=message_id, node=node, text=idea)
        except BishengError:
            if not resumed:
                raise
            # 上一轮会话已失效(毕昇会话过期):退回全新会话重试一次。
            # 不这么做,每次发起研究都会去续同一个死会话,用户被永久卡死。
            resumed = False
            session_id, node, message_id = begin()
            second = invoke(session_id=session_id, message_id=message_id, node=node, text=idea)
        events2 = second.get("events") or []
        texts = [redact_for_model(t) for t in output_texts(events2)]
        failures = [redact_for_model(f) for f in node_failures(events2)]
        # 候选名优先取**文件系统增量**(毕昇真的落盘了什么),正则只作兜底且要在候选目录里核对存在。
        new_files = pending_snapshot() - before
        candidate = newest_pending(new_files)
        if candidate is None:
            guess = find_candidate(texts)
            if guess and guess in pending_snapshot():
                candidate = guess
        waiting2 = last_input_event(events2)
        session = {
            "session_id": second.get("session_id") or session_id,
            "workflow_id": workflow_id(),
            "idea": idea,
            "candidate": candidate,
            "candidate_sha": candidate_sha(candidate) if candidate else None,
            "waiting_node": waiting2.get("node_id") if waiting2 else None,
            "waiting_message_id": waiting2.get("message_id") if waiting2 else None,
            "last_outputs": texts[-3:],
            "failures": failures,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        set_current(session)
        lines = [f"已发起研究。想法:{idea}", f"毕昇会话:{session['session_id']}"]
        if resumed:
            lines.append("(接着上一轮继续,反馈已带在会话上下文里)")
        if candidate:
            lines.append(f"候选已提交:{candidate}(尚未批准,不能运行)")
        if waiting2:
            lines.append(f"当前停在节点:{waiting2.get('node_id')}(等待人工处理)")
        if failures:
            lines.append("工作流有节点失败:" + "; ".join(failures[:3]))
        for text in texts[-2:]:
            lines.append("输出:" + text[:600])
        return "\n".join(lines)
    except BishengError as exc:
        # 毕昇的错误文本里可能带工作流输出(含哈希):统一再脱敏一层(第 5 轮 codex 的 L4)
        return redact_for_model(f"发起研究失败:{exc}")


def _research_status() -> str:
    session = current_session()
    if not session:
        return "当前没有进行中的研究。先用 start_research 发起。"
    lines = [f"会话:{session.get('session_id')}", f"想法:{session.get('idea')}"]
    candidate = session.get("candidate")
    approved, reason = approval_state(session) if candidate else (False, "")
    if candidate:
        tail = f" —— {reason}" if reason and not approved else ""
        lines.append(f"候选:{candidate}(已批准:{'是' if approved else '否'}{tail})")
    waiting = session.get("waiting_node")
    if waiting and waiting != "input_wait":
        lines.append(f"当前停在节点:{waiting}")
    elif waiting == "input_wait" and candidate and approved:
        lines.append("人工批准已落地,正在推进毕昇工作流……")
        try:
            data = invoke(session_id=session.get("session_id"),
                          message_id=session.get("waiting_message_id"),
                          node="input_wait", text="继续")
        except BishengError as exc:
            return redact_for_model("\n".join(lines + [f"推进失败:{exc}"]))
        events = data.get("events") or []
        texts = [redact_for_model(t) for t in output_texts(events)]
        failures = [redact_for_model(f) for f in node_failures(events)]
        waiting2 = last_input_event(events)
        session["waiting_node"] = waiting2.get("node_id") if waiting2 else None
        session["waiting_message_id"] = waiting2.get("message_id") if waiting2 else None
        session["last_outputs"] = texts[-3:]
        session["failures"] = failures
        session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        set_current(session)
        for text in texts[-2:]:
            lines.append("输出:" + text[:1200])
        if failures:
            lines.append("有节点失败:" + "; ".join(failures[:3]))
    elif waiting == "input_wait":
        lines.append("当前停在「等待批准」节点;批准前不会继续。用 request_approval 请求人工审阅。")
    return "\n".join(lines)


def _approval_session_error(name: str) -> tuple[dict | None, str]:
    """会话内批准与页面批准**共用**的前置:只认当前会话本轮的候选。

    返回 (session, "") 或 (None, 拒绝原因)。
    """
    session = current_session()
    if not session:
        # 没有会话 = 本轮研究还没发起:**任何**文件名都不该被推到人面前批准
        # (第 5 轮 codex 的 L5:否则模型可以把候选目录里的任意旧文件推给人批)。
        # 人要直接批某个候选,走终端里的 `research_api.py review/approve`,不走这个工具。
        return None, ("当前没有进行中的研究,不能请求批准。先发起研究,确认候选文件名后再请求批准。")
    if session.get("candidate") and name != session.get("candidate"):
        # 只能为**本轮研究**的候选请求批准:否则模型可以拿批准流程去批别的文件。
        return None, (f"当前研究的候选是 {session.get('candidate')},不能批准 {name}。"
                      "先查进度确认候选文件名。")
    if not session.get("candidate"):
        # 有会话、却没确定候选(工作流没落下新候选):这时任何文件名都不该被批准,
        # 否则模型可以借批准流程去批候选目录里的任意旧文件。
        return None, ("还没确定本轮研究的候选文件(候选目录里没出现新文件),不能请求批准。"
                      "先查进度确认候选文件名,或重新发起研究。")
    return session, ""


def _inline_approval_target(name: str, session: dict) -> tuple[Path | None, str, list[str], str]:
    """会话内回贴批准的共用取材(两阶段共用):解析候选、跑静态筛查、算哈希。

    返回 (path, sha256, findings, error);error 非空时其余值无意义。
    哈希绑定与 approval_state 同一口径:发起研究时记下的 candidate_sha 与现在的
    文件内容必须一致,否则「将被批准的」不是「发起研究时的那份」。
    """
    try:
        path = rs.resolve_pending(name, ROOT, pending_dir_name())
    except rs.SandboxError as exc:
        return None, "", [], f"候选文件解析失败:{exc}"
    try:
        raw = path.read_bytes()
        source = raw.decode("utf-8-sig")
    except OSError:
        return None, "", [], "策略文件无法读取或不可读"
    except UnicodeDecodeError as exc:
        return None, "", [], f"策略文件不是合法的 UTF-8: {exc}"
    # 静态筛查与 CLI approve 同一前置:筛查不过,连哈希都不发(人不用看一份注定被拒的代码)。
    # 配置缺键也拒绝(fail-closed):空清单会让筛查静默放行,与 CLI 的直接索引口径一致。
    scr = load_config().get("screening") or {}
    try:
        findings = rs.screen_source(source, scr["extra_forbidden_modules"],
                                    scr["extra_forbidden_calls"], scr["allowed_modules"])
    except KeyError:
        return None, "", [], "筛查配置缺失,拒绝批准(fail-closed)。"
    try:
        sha = rs.file_sha256(path)
    except OSError as exc:
        return None, "", [], f"策略文件哈希计算失败:{exc}"
    recorded = session.get("candidate_sha")
    if recorded and recorded != sha:
        return None, "", [], "候选文件在发起研究后被改动过,批准对不上;请重新发起研究。"
    return path, sha, findings, ""


# 会话内回贴模式的**唯一**脱敏豁免占位符:先用它替掉哈希再过 redact_for_model,
# 洗完换回来。占位符不含十六进制、不含路径分隔符、不像批准命令——所有脱敏规则都打不中它。
INLINE_HASH_PLACEHOLDER = "@@INLINE-APPROVAL-HASH-@@"

_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _redact_keeping_inline_hash(text: str, sha: str) -> str:
    """inline 模式的返回值清洗:哈希这一个值**有意**放行(人要粘贴的就是它,Issue #59),
    其余规则(路径/批准页地址/批准命令/凭据)照旧全洗。

    页面模式的返回值仍走 redact_for_model,URL 与哈希照旧不进模型通道。
    """
    return redact_for_model(text.replace(sha, INLINE_HASH_PLACEHOLDER)).replace(
        INLINE_HASH_PLACEHOLDER, sha)


def _request_approval_inline(strategy: str, confirm_sha256: str | None) -> str:
    name = (strategy or "").strip()
    if not name:
        return "请给出候选文件名。"
    session, err = _approval_session_error(name)
    if err:
        return err
    approved, _why = approval_state({"candidate": name, "candidate_sha": session.get("candidate_sha")})
    if approved:
        return "该候选已经批准过,不需要再批。"
    path, sha, findings, err = _inline_approval_target(name, session)
    if err:
        return redact_for_model(err)
    if findings:
        # 静态筛查不过 = 与 CLI approve 同一拒绝口径:不给哈希,更不给批准。
        return redact_for_model("静态筛查未通过,不予批准:" + "; ".join(findings)
                                + "。修正候选后需要重新发起研究。")
    confirm = (confirm_sha256 or "").strip().lower()
    if not confirm:
        # 第一阶段:发哈希与位置,**不发源码**——源码由人在本机编辑器里直接读盘上文件
        # (聊天里经模型转述的内容不可作为审阅依据;盘上的文件才是将要被执行的字节)。
        lines = [
            f"候选 {name} 的批准信息(会话内确认,不打开浏览器):",
            f"- 文件:候选目录(pending_strategies)下的 {name},请人工在本机用编辑器打开、通读源码;",
            f"- 该文件当前内容的 sha256:{INLINE_HASH_PLACEHOLDER}",
            "- 静态筛查:通过。",
            "请人工确认源码无误后,把上面这串 64 位哈希**亲自**粘贴到会话里;"
            "模型收到后带 confirm_sha256 参数再次调用本工具完成批准。",
            "模型不能代替人粘贴哈希;粘贴进来的哈希只在与文件当前内容一致时才会落账。",
        ]
        return _redact_keeping_inline_hash("\n".join(lines), sha)
    # 第二阶段:人工粘贴回来的哈希。落账用的是**服务端自算**的 sha(不是回贴值),
    # 且 record_approval 内部还会再重算一次文件哈希——批什么 = 盘上什么文件,绑定不经模型之手。
    if not _HEX64_RE.fullmatch(confirm):
        return redact_for_model("粘贴的哈希格式不对:要完整的 64 位小写十六进制。"
                                "重新调用本工具(不带 confirm_sha256)可重新获取。")
    if not secrets.compare_digest(confirm, sha):
        return redact_for_model("你粘贴的哈希与当前候选内容不符,未批准。"
                                "重新调用本工具(不带 confirm_sha256)可重新获取。")
    try:
        rs.record_approval(path, approvals_file(), sha256=sha, via="inline-chat")
    except rs.SandboxError as exc:
        return redact_for_model(f"写入批准记录失败:{exc}")
    return redact_for_model("\n".join([
        f"人工已通过会话内哈希回贴批准候选 {name}(账本 via=inline-chat)。",
        "批准绑定到当前文件内容;代码再改一个字节,批准即失效。",
    ]))


def _request_approval_via_page(strategy: str) -> str:
    name = (strategy or "").strip()
    if not name:
        return "请给出候选文件名。"
    session, err = _approval_session_error(name)
    if err:
        return redact_for_model(err)
    script = CODE_ROOT / "scripts" / "research_approval_server.py"
    if not script.is_file():
        return redact_for_model(f"找不到批准进程:{script}")
    env = {k: v for k, v in os.environ.items() if k in APPROVAL_ENV_ALLOW and v}
    env["INVEST_ROOT"] = str(ROOT)            # 批准进程读的是网关实际挂载的那棵目录树
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        # stdin **必须**是 DEVNULL(Issue #57):本服务跑在 stdio 上,自己的 stdin 是 DSH 的协议管道,
        # SDK 的读线程一直阻塞在上面。不指定 stdin 时子进程会继承这条管道,Windows 上子进程
        # 启动即卡死——不吐 ready、不退出、不报错,30 秒后这里只能报「没起来」(2026-11-19 实测)。
        proc = subprocess.Popen(
            [sys.executable, str(script), "--strategy", name, "--timeout", str(APPROVAL_WAIT_SECONDS),
             "--hide-url"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(CODE_ROOT), env=env,
        )
    except OSError as exc:
        return redact_for_model(f"拉不起批准进程:{exc}")

    # 读子进程的输出用后台线程:Windows 的管道上 readline() 会一直阻塞,
    # 子进程若既不输出也不退出,工具调用就挂死了。
    out_lines: queue.Queue = queue.Queue()
    err_lines: list[str] = []

    def pump_out():
        try:
            for line in proc.stdout:
                out_lines.put(line)
        except Exception:                                   # noqa: BLE001 - 管道关掉不算错误
            pass

    def pump_err():
        try:
            for line in proc.stderr:
                err_lines.append(line)
        except Exception:                                   # noqa: BLE001
            pass

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()

    ready = None
    error_event = None
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            line = out_lines.get(timeout=0.2)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        try:
            ready = json.loads(line)
        except ValueError:
            continue
        if ready.get("event") == "ready":
            break
        if ready.get("event") == "error":
            # 批准进程 fail-closed(例如脱敏模块不可用):把原因透出去,
            # 否则人只看到「没起来」,不知道是「看不到源码所以拒绝发页面」(第 5 轮 codex 的 L1)。
            error_event = ready
            break
    if not ready or ready.get("event") != "ready":
        exit_code = proc.poll()               # 先取退出码再 kill:None = 到时限仍活着(无响应)
        proc.kill()
        if error_event:
            message = str(error_event.get("message") or "")
            # 错误消息里可能有本机路径:只给本机 stderr,工具返回值用脱敏后的版本。
            print("[research-mcp] 批准进程拒绝:" + message, file=sys.stderr)
            return redact_for_model("批准进程拒绝提供批准页:" + message)
        # 子进程的原始 stderr 里可能有本机路径:不原样回传给模型(模型看到的一切都算外发),
        # 只留在本机日志里。
        if err_lines:
            print("[research-mcp] 批准进程异常:" + err_lines[-1].strip(), file=sys.stderr)
        # 三种失败分开说(Issue #57):「候选不存在」由批准进程的 error 事件报(上面那支);
        # 这里只剩「没吐 ready 就退出」与「到时限仍无响应」——原先一律说「候选可能不存在」,误导排查。
        if exit_code is None:
            print(f"[research-mcp] 批准进程 {READY_TIMEOUT_SECONDS} 秒内无响应,已终止", file=sys.stderr)
            # 文案是静态的,仍过一遍 redact_for_model:「所有工具返回都经清洗」这条不变量不留例外。
            return redact_for_model(f"批准进程无响应({READY_TIMEOUT_SECONDS} 秒内没有就绪),已终止。"
                                    "请让用户查看本机 MCP 日志,或在本机终端里重新拉起批准页。")
        return redact_for_model(f"批准进程未就绪即退出(退出码 {exit_code}),详细原因见本机 MCP 日志。")
    # **页面地址不落盘、不进模型通道**(第 5 轮 codex 的 H1):
    # 批准进程自己开浏览器,URL 压根不离开那个进程;这里也就没有 URL 可写文件。
    # 之前把 URL 写进 `.local/research-mcp/approval-page.txt`,而那个文件只在
    # 「模型没有本地文件工具」这个**未经验证**的前提下才安全。
    lines = [
        f"已在本机浏览器打开候选 {ready.get('strategy')} 的批准页。",
        "请人工阅读源码、核对页面上的 sha256 后确认;页面地址与哈希不在本消息里返回"
        "(否则能发本地 HTTP 请求的程序可以自行批准)。",
        # **批准命令不进模型通道**(第 6 轮 codex 的 M2):命令一旦交回模型,
        # 模型就能诱导人去跑终端里的批准流程——而那条路没有「只批本轮候选」的约束。
        # 兜底提示只说「去本机终端重拉批准页」,命令本身只留在本机 stderr。
        "若浏览器没有自动打开,请让用户在本机终端里为该候选重新拉起批准页"
        "(具体命令见本机 MCP 日志,不在本消息里返回)。",
    ]
    # 兜底命令**不带** --hide-url:浏览器没自动打开时,人需要从 stderr 上把地址复制到浏览器。
    # 地址不进 stdout(批准进程的 ready 事件里没有它),stderr 是人的通道,并且带警告
    # (第 7 轮 codex 的 F3)。
    print("[research-mcp] 浏览器未自动打开时的兜底命令:python "
          f"{CODE_ROOT / 'scripts' / 'research_approval_server.py'} --strategy {ready.get('strategy')}",
          file=sys.stderr)
    if not ready.get("screening_passed"):
        lines.append("注意:静态筛查未通过:" + "; ".join(ready.get("findings") or []))
    timed_out = False
    try:
        proc.wait(timeout=APPROVAL_WAIT_SECONDS + 30)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
    if timed_out:
        # 超时也可能是因为人在**另一个**批准页(终端里手动跑的那个)做了决定:
        # 批准记录写的是同一个文件,这里复查一次,免得白等一场。
        approved, _ = approval_state({"candidate": name, "candidate_sha": candidate_sha(name)})
        if approved:
            out = "\n".join(lines + ["人工已批准。", _research_status()])
        else:
            out = "\n".join(lines + ["等待超时,未收到人工决定。"])
    elif proc.returncode != 0:
        out = "\n".join(lines + ["人工没有批准(或页面出错)。工作流不会继续。"])
    else:
        lines.append("人工已批准。")
        out = "\n".join(lines + [_research_status()])
    return redact_for_model(out)


# ---------- 工具入口(async,避免把事件循环卡住) ----------

@mcp.tool()
async def start_research(idea: str) -> str:
    """发起研究:把研究想法交给毕昇工作流(读研究资料 → 提假设 → 写候选 → 提交 → 停在待批准)。

    只负责提交想法与回传状态。候选代码要人审阅并批准后才能跑,本工具不做这件事。
    若上一轮评估后工作流已回到「研究想法」节点(等修正),本次想法会**接着那一轮**发,
    而不是另起一个会话——毕昇的失败反馈就在那轮上下文里。
    """
    return await asyncio.to_thread(_start_research, idea)


@mcp.tool()
async def research_status() -> str:
    """查进度:当前研究的状态;若人工批准已落地,自动推进毕昇工作流并回传评估结果。"""
    return await asyncio.to_thread(_research_status)


@mcp.tool()
async def request_approval(strategy: str, confirm_sha256: str | None = None, mode: str = "inline") -> str:
    """请求批准。两种模式(Issue #59,用户选定方案 A):

    - inline(默认,两阶段):
      1. 第一次调用(不带 confirm_sha256):返回候选文件名与该文件当前内容的 sha256。
         人工在本机编辑器里打开候选目录下的源码文件、通读后,把哈希**亲自**粘贴进会话;
      2. 模型带 confirm_sha256=<人粘贴的哈希> 再次调用:服务端与自算哈希比对一致,
         且静态筛查通过时,经 record_approval(唯一写入者)落账(via=inline-chat)。
      只能批准当前会话本轮的候选;粘贴的哈希与文件不符、筛查不过、文件在发起研究后被改过,都拒绝。
    - page(带外页面,原样保留):拉起本机批准页(只监听 127.0.0.1),页面展示完整源码、
      静态筛查结果与父策略差异;人输入完整 sha256 确认。页面地址与哈希不在返回值里。

    批准绑定**内容哈希**:代码再改一个字节,批准即失效。
    """
    if (mode or "inline").strip().lower() == "page":
        return await asyncio.to_thread(_request_approval_via_page, strategy)
    return await asyncio.to_thread(_request_approval_inline, strategy, confirm_sha256)


def _diagnose(events: list[dict]) -> str:
    failures = node_failures(events)
    if failures:
        return " 节点失败:" + "; ".join(failures[:3])
    texts = output_texts(events)
    if texts:
        return " 最近输出:" + texts[-1][:300]
    return ""


RESEARCH_PROFILE = "research"


def deployment_problem(env=None) -> str | None:
    """本服务只在**专用 research DSH_HOME** 下提供服务(Issue #57);不满足时返回原因。

    「批准只能由人做」成立于前提:研究会话里的模型没有本机读写、命令执行、桌面操控能力。
    `dsh-agent-preset` 不能屏蔽全局插件,所以这条前提只能由「整个 DSH_HOME 只为研究而建」来保证
    (启动脚本 `start-research-dsh.ps1` 负责建它并用 profile_check 核对)。日常 Desktop 不设 DSH_HOME
    (实测),而且它的目录下有 web/lifehub 等多个 profile——两种情况在这里都会被拒。
    以 `.` 开头的目录是 DSH 自己的内部目录(如 `.generations`),不算 profile。
    """
    env = os.environ if env is None else env
    home = (env.get("DSH_HOME") or "").strip()
    # 模板里是 `!!js process.env.DSH_HOME`(守卫不许字面量兜底):没设时 DSH 传来的是字符串 "undefined"。
    if home.lower() in ("undefined", "null"):
        home = ""
    if not home:
        return ("没有 DSH_HOME:research-mcp 只在专用 research DSH_HOME 下提供服务,"
                "请用 tools/research_mcp/start-research-dsh.ps1 启动。")
    profiles = Path(home) / "profiles"
    if not profiles.is_dir():
        return "DSH_HOME 下没有 profiles 目录,不是由启动脚本建立的专用 research 环境。"
    names = sorted(p.name for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    if names != [RESEARCH_PROFILE]:
        return (f"DSH_HOME 下的 profile 必须只有 {RESEARCH_PROFILE!r},实际 {names}:"
                "这不是专用 research 环境(全局插件会混进研究会话)。")
    return None


if __name__ == "__main__":
    # 本机日志走 stderr,Windows 的 cp1252 控制台会把中文变成 \uXXXX 转义,
    # 看不清「批准进程拒绝」之类的原因。stdout 交给 MCP SDK(它自己按 UTF-8 重包一层),
    # 这里改 stdout 的编码不影响协议。
    rconsole.reconfigure_stdio()
    problem = deployment_problem()
    if problem:
        # **不拦截**(2026-11-19 用户决定接受风险,ADR「Issue #57 发现」):日常 Desktop 里照常提供服务,
        # 只在本机 stderr 留一行提示——这里的模型可能拿得到终端/桌面操控,「只有人能批准」靠的是
        # 「不该做 + 有记录」,不再是「做不到」。要「做不到」就用 start-research-dsh.ps1(严格模式)。
        print("[research-mcp] 注意:不在专用 research 环境(严格模式)下运行:" + problem, file=sys.stderr)
    mcp.run()
