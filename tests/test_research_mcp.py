"""research-mcp:工具目录边界、状态推进、批准只能由人做。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "research_mcp"))
import server as mcp_server                       # noqa: E402

assert Path(mcp_server.__file__).resolve() == \
    Path(__file__).resolve().parents[1] / "tools" / "research_mcp" / "server.py"


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "STATE_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(mcp_server, "KEY_FILE", tmp_path / "missing.key")
    monkeypatch.setattr(mcp_server, "ROOT", tmp_path)
    monkeypatch.setenv("BISHENG_OPENAPI_KEY", "bs-sak-test")
    return tmp_path


def test_the_research_modules_come_from_the_code_root_not_the_data_root():
    """**代码一律从 CODE_ROOT 取**:ROOT 是数据根,它的 scripts/ 未必是同一份代码。

    worktree + INVEST_ROOT 就是这种情形(worktree 的代码 + 主检出的数据)。
    从 ROOT/scripts 导入会执行**另一份**代码,表现是版本错配的接口对不上
    (第 6 轮实测:新的 server.py 配旧的 research_sandbox.py → AttributeError)。
    """
    import research_config
    import research_console
    import research_sandbox

    code_root = Path(mcp_server.__file__).resolve().parents[2]
    for module in (research_config, research_console, research_sandbox):
        assert Path(module.__file__).resolve().parent == code_root / "scripts", \
            f"{module.__name__} 来自 {module.__file__},不是代码根 {code_root / 'scripts'}"


# ---------- 工具目录(边界) ----------

def test_exactly_three_tools_and_nothing_else():
    import asyncio

    async def names():
        app = mcp_server.mcp
        listed = await app.list_tools()
        return sorted(t.name for t in listed)

    got = asyncio.run(names())
    assert got == ["request_approval", "research_status", "start_research"]


def test_module_never_writes_approval_records():
    """MCP 只有「请求批准」的权限,批准记录的唯一写入者是批准进程。"""
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "record_approval" not in source
    assert "approvals_file" in source                      # 只用来读


def test_tool_descriptions_do_not_promise_approval():
    import asyncio

    async def descs():
        listed = await mcp_server.mcp.list_tools()
        return {t.name: (t.description or "") for t in listed}

    got = asyncio.run(descs())
    assert "没有**写批准的权限" in got["request_approval"]
    assert "人工决定" in got["request_approval"]


# ---------- 状态 ----------

def test_status_without_session_says_so(clean_state):
    out = mcp_server._research_status()
    assert "没有进行中的研究" in out


def test_candidate_extracted_from_workflow_output():
    assert mcp_server.find_candidate(["候选已提交为 abc_1.py(提交结果:{...})"]) == "abc_1.py"
    assert mcp_server.find_candidate(["没有文件"]) is None


def test_start_research_reports_candidate_and_waiting_node(clean_state, monkeypatch):
    calls = []
    pending = clean_state / "pending_strategies"
    pending.mkdir()
    (pending / "cand_1.py").write_text("class C: pass\n", encoding="utf-8")

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if "session_id" not in kwargs:
            return {"session_id": "s1_async_task_id",
                    "events": [{"event": "input", "node_id": "input_idea", "message_id": 11}]}
        return {"session_id": "s1_async_task_id",
                "events": [{"event": "output_msg", "node_id": "output_tip",
                            "output_schema": {"message": "候选已提交为 cand_1.py(提交结果:{...})"}},
                           {"event": "input", "node_id": "input_wait", "message_id": 12}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("均线突破")

    assert "cand_1.py" in out
    assert "input_wait" in out
    assert calls[0].get("session_id") is None
    assert calls[1]["node"] == "input_idea"
    assert calls[1]["text"] == "均线突破"
    state = json.loads(mcp_server.STATE_FILE.read_text(encoding="utf-8"))
    assert state["current"]["candidate"] == "cand_1.py"
    assert state["current"]["waiting_node"] == "input_wait"


def test_start_research_never_leaks_the_hash_or_the_approve_command(clean_state, monkeypatch):
    """回归测试(第 8 轮 codex 的 C1/C2):工作流文本里的哈希与批准命令不能进模型通道。

    毕昇的 tip 消息曾经把网关的提交响应(含 sha256)与
    「python scripts/research_api.py approve --strategy x.py」原样回给模型:
    模型拿到哈希就能在聊天里念给人,「人核对完整 64 位哈希」这道仪式退化成橡皮章。
    """
    sha = "ab" * 32
    pending = clean_state / "pending_strategies"
    pending.mkdir()
    (pending / "cand_1.py").write_text("class C: pass\n", encoding="utf-8")

    def fake_invoke(**kwargs):
        if "session_id" not in kwargs:
            return {"session_id": "s1", "events": [{"event": "input", "node_id": "input_idea",
                                                    "message_id": 11}]}
        return {"session_id": "s1", "events": [
            {"event": "output_msg", "node_id": "output_tip",
             "output_schema": {"message": f"候选已提交为 cand_1.py,提交结果:{{\"sha256\": \"{sha}\"}}。"}},
            {"event": "output_msg", "node_id": "output_tip",
             "output_schema": {"message": "请执行:python scripts/research_api.py approve --strategy cand_1.py"}},
            {"event": "input", "node_id": "input_wait", "message_id": 12}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("均线突破")

    assert sha not in out
    assert "research_api.py approve" not in out
    assert "sha256 已隐去" in out
    assert "批准命令已隐去" in out


def test_redact_for_model_also_hides_local_absolute_paths():
    """本机绝对路径不进模型通道:模型看到目录结构没有用处,却多了一条指纹信息。

    注意不能误伤:URL、裸文件名、相对路径(`pending_strategies/x.py`)必须原样保留。
    """
    text = ("候选已提交为 cand_1.py。报错了:C:\\Users\\someone\\data\\research-control\\approvals.json "
            "不可写。参考 https://example.com/docs/a/b 与 pending_strategies/cand_1.py")

    out = mcp_server.redact_for_model(text)

    assert "Dev" not in out
    assert "research-control" not in out
    assert "<路径>" in out
    assert "https://example.com/docs/a/b" in out
    assert "pending_strategies/cand_1.py" in out
    assert "cand_1.py" in out


def test_redact_for_model_scrubs_known_secret_values_by_value(clean_state):
    """MCP 侧也要**按值**盖掉已知凭据(第 11 轮 codex 的 F6)。

    清洗规则本来按**名字**认凭据,候选一旦把裸值写进异常文本(不带名字),
    名字那一层就认不出来。网关侧原本有按值清洗,MCP 侧原先没有——
    「网关盖住、MCP 不盖」就是一条漏的通道。
    """
    assert "bs-sak-test" in mcp_server.known_secret_values()

    out = mcp_server.redact_for_model("调用失败:key bs-sak-test 无效")

    assert "bs-sak-test" not in out
    assert "<凭据已隐去>" in out


def test_candidate_comes_from_the_filesystem_not_from_the_model_prose(clean_state, monkeypatch):
    """候选名以**文件系统增量**为准:模型在输出里报的文件名不算数。

    否则模型可以随口报一个文件名,把批准流程引到别的文件上。
    """
    pending = clean_state / "pending_strategies"
    pending.mkdir()

    def fake_invoke(**kwargs):
        if "session_id" not in kwargs:
            return {"session_id": "s1", "events": [{"event": "input", "node_id": "input_idea",
                                                     "message_id": 11}]}
        # 毕昇在这一步真的落盘了一个文件
        (pending / "real_1.py").write_text("class C: pass\n", encoding="utf-8")
        return {"session_id": "s1",
                "events": [{"event": "output_msg", "node_id": "output_tip",
                            "output_schema": {"message": "候选已提交为 ghost_9.py"}},
                           {"event": "input", "node_id": "input_wait", "message_id": 12}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("想法")

    assert "候选已提交:real_1.py" in out


def test_candidate_named_in_prose_must_exist_in_the_pending_dir(clean_state, monkeypatch):
    """正则兜底捞到的文件名不在候选目录里 → 不认。"""

    def fake_invoke(**kwargs):
        if "session_id" not in kwargs:
            return {"session_id": "s1", "events": [{"event": "input", "node_id": "input_idea",
                                                     "message_id": 11}]}
        return {"session_id": "s1",
                "events": [{"event": "output_msg", "node_id": "output_tip",
                            "output_schema": {"message": "候选已提交为 ghost_9.py"}},
                           {"event": "input", "node_id": "input_wait", "message_id": 12}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("想法")

    assert "候选已提交:" not in out
    state = json.loads(mcp_server.STATE_FILE.read_text(encoding="utf-8"))
    assert state["current"]["candidate"] is None


def test_start_research_without_idea_is_rejected(clean_state):
    assert "研究想法" in mcp_server._start_research("   ")


def test_start_research_continues_previous_session_after_feedback(clean_state, monkeypatch):
    """评估后工作流回到「研究想法」节点:下一次发起接着同一会话走,反馈才对得上。"""
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法",
                                       "candidate": "cand_1.py", "waiting_node": "input_idea",
                                       "waiting_message_id": 61}})
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        return {"session_id": "s1",
                "events": [{"event": "output_msg", "node_id": "output_tip",
                            "output_schema": {"message": "候选已提交为 cand_2.py(提交结果:{...})"}},
                           {"event": "input", "node_id": "input_wait", "message_id": 70}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("换个思路")

    assert "cand_2.py" in out
    assert len(calls) == 1                       # 没有另起会话
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["message_id"] == 61
    assert calls[0]["node"] == "input_idea"
    assert calls[0]["text"] == "换个思路"


def test_start_research_starts_new_session_when_previous_is_awaiting_approval(clean_state, monkeypatch):
    """上一轮还停在「等待批准」节点时,发起新研究要另起会话,不能把想法喂给批准节点。"""
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法",
                                       "candidate": "cand_1.py", "waiting_node": "input_wait",
                                       "waiting_message_id": 58}})
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if "session_id" not in kwargs:
            return {"session_id": "s2",
                    "events": [{"event": "input", "node_id": "input_idea", "message_id": 80}]}
        return {"session_id": "s2",
                "events": [{"event": "input", "node_id": "input_wait", "message_id": 81}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    mcp_server._start_research("新想法")

    assert calls[0].get("session_id") is None     # 先起新会话
    assert calls[1]["node"] == "input_idea"


def test_start_research_reports_backend_failure(clean_state, monkeypatch):
    def boom(**kwargs):
        raise mcp_server.BishengError("毕昇没起来")

    monkeypatch.setattr(mcp_server, "invoke", boom)

    assert "毕昇没起来" in mcp_server._start_research("想法")


def test_start_research_falls_back_to_a_new_session_when_resume_fails(clean_state, monkeypatch):
    """上一轮会话失效(毕昇会话过期)时退回全新会话,不能把用户卡在死会话里。"""
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法",
                                       "candidate": "cand_1.py", "waiting_node": "input_idea",
                                       "waiting_message_id": 61}})
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if kwargs.get("session_id") == "s1":
            raise mcp_server.BishengError("会话已失效")
        if "session_id" not in kwargs:
            return {"session_id": "s2",
                    "events": [{"event": "input", "node_id": "input_idea", "message_id": 90}]}
        return {"session_id": "s2",
                "events": [{"event": "input", "node_id": "input_wait", "message_id": 91}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._start_research("新想法")

    assert "发起研究失败" not in out
    assert calls[0]["session_id"] == "s1"          # 先试续跑
    assert calls[1].get("session_id") is None      # 失效后退回全新会话
    assert calls[2]["session_id"] == "s2"
    state = json.loads(mcp_server.STATE_FILE.read_text(encoding="utf-8"))
    assert state["current"]["session_id"] == "s2"


def test_status_resumes_workflow_only_after_human_approval(clean_state, monkeypatch):
    state = {"current": {"session_id": "s1", "idea": "想法", "candidate": "cand_1.py",
                          "waiting_node": "input_wait", "waiting_message_id": 12,
                          "last_outputs": [], "failures": []}}
    mcp_server.save_state(state)

    monkeypatch.setattr(mcp_server, "approval_state", lambda session: (False, "尚未批准"))
    monkeypatch.setattr(mcp_server, "invoke", lambda **kwargs: pytest.fail("未批准就推进了工作流"))

    out = mcp_server._research_status()
    assert "等待批准" in out


def test_status_resumes_and_returns_evaluation_after_approval(clean_state, monkeypatch):
    state = {"current": {"session_id": "s1", "idea": "想法", "candidate": "cand_1.py",
                          "waiting_node": "input_wait", "waiting_message_id": 12,
                          "last_outputs": [], "failures": []}}
    mcp_server.save_state(state)
    monkeypatch.setattr(mcp_server, "approval_state", lambda session: (True, ""))
    seen = {}

    def fake_invoke(**kwargs):
        seen.update(kwargs)
        return {"session_id": "s1",
                "events": [{"event": "output_msg", "node_id": "output_result",
                            "output_schema": {"message": "结论:未通过(1 项未过)"}}]}

    monkeypatch.setattr(mcp_server, "invoke", fake_invoke)

    out = mcp_server._research_status()

    assert "结论:未通过" in out
    assert seen["node"] == "input_wait"
    assert seen["message_id"] == 12
    assert seen["text"] == "继续"


def test_request_approval_without_strategy_is_rejected(clean_state):
    assert "候选文件名" in mcp_server._request_approval("")


# ---------- 批准:模型不能自己批准自己 ----------

class FakeApprovalProc:
    """假批准进程:吐出 ready 那行 JSON,然后正常退出(等于人批准了)。"""

    def __init__(self, ready):
        self.stdout = [json.dumps(ready, ensure_ascii=False) + "\n"]
        self.stderr = []
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_request_approval_never_returns_the_url_or_the_hash(clean_state, monkeypatch):
    """回归测试(codex 第 2 轮的 Critical、第 5 轮的 H1):工具返回里不能有批准页地址和哈希。

    有了它们,一个有本地 HTTP 能力的模型就能自己去 POST 批准页,
    把「带外人工批准」变成「模型自我批准」。

    第 5 轮起**更严**:批准页地址连磁盘都不落盘——批准进程自己开浏览器,
    URL 只活在那一个进程的内存里,MCP 侧压根拿不到。
    """
    sha = "a" * 64
    url = "http://127.0.0.1:45678/a/secret-token"
    # **故意让 ready 里带上 url**:正常路径下批准进程用 --hide-url 不会打印地址,
    # 但这条用例要锁的是「MCP 读到带地址的 stdout 也会把它丢掉」——
    # ready 里没有 url 的时候,`assert url not in out` 恒真,这条用例就是空的
    # (第 6 轮 codex 的 B/M)。
    ready = {"event": "ready", "strategy": "cand.py", "sha256": sha, "url": url,
             "screening_passed": True, "findings": []}
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda *a, **k: FakeApprovalProc(ready))
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand.py",
                                       "waiting_node": "input_wait", "waiting_message_id": 12}})

    out = mcp_server._request_approval("cand.py")

    assert url not in out
    assert sha not in out
    assert "http://" not in out
    assert "a" * 64 not in out
    # 注意:这里断的是「哈希值」不在,不是「sha256 这个词」不在——
    # 文案里要告诉人去核对页面上的 sha256,那个词必须留着。
    assert sha not in out
    assert "<sha256" not in out
    # 地址不落盘:整个状态目录里没有第二个文件写着 URL
    for path in clean_state.rglob("*"):
        if path.is_file():
            assert url not in path.read_text(encoding="utf-8", errors="replace")
    # 批准进程是自己开浏览器的,MCP 这边不再 import webbrowser
    assert not hasattr(mcp_server, "webbrowser")


def test_request_approval_never_returns_the_approve_command(clean_state, monkeypatch):
    """**批准命令不进模型通道**(第 6 轮 codex 的 A/M2)。

    命令一旦交回模型,模型就能诱导人去跑终端里的批准流程——那条路上
    没有「只批本轮候选」的约束(工具侧有,CLI 侧没有)。
    """
    ready = {"event": "ready", "strategy": "cand.py", "sha256": "c" * 64,
             "screening_passed": True, "findings": []}
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda *a, **k: FakeApprovalProc(ready))
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand.py",
                                       "waiting_node": "input_wait", "waiting_message_id": 12}})

    out = mcp_server._request_approval("cand.py")

    assert "research_approval_server.py" not in out
    assert "research_api.py approve" not in out


def test_request_approval_passes_hide_url_to_the_approval_process(clean_state, monkeypatch):
    """批准进程必须带 --hide-url:否则它会把 URL 打到 stdout,MCP 就能读到。"""
    calls = []
    ready = {"event": "ready", "strategy": "cand.py", "sha256": "c" * 64,
             "screening_passed": True, "findings": []}

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeApprovalProc(ready)

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand.py",
                                       "waiting_node": "input_wait", "waiting_message_id": 12}})

    mcp_server._request_approval("cand.py")

    assert calls, "没有拉起批准进程"
    assert "--hide-url" in calls[0]


def test_request_approval_only_approves_the_current_candidate(clean_state, monkeypatch):
    """不能拿批准流程去批别的文件:只能批本轮研究的候选。"""
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand_1.py",
                                       "waiting_node": "input_wait", "waiting_message_id": 12}})

    def boom(*a, **k):
        raise AssertionError("不该拉起批准进程")

    monkeypatch.setattr(mcp_server.subprocess, "Popen", boom)

    out = mcp_server._request_approval("other.py")

    assert "cand_1.py" in out
    assert "other.py" in out


def test_request_approval_refuses_when_the_session_has_no_candidate(clean_state, monkeypatch):
    """有会话却没确定候选时,任何文件名都不该被批准。

    否则模型可以借批准流程去批候选目录里的任意旧文件(它不是本轮研究的产物)。
    """
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": None,
                                       "candidate_sha": None, "waiting_node": "input_wait",
                                       "waiting_message_id": 12}})

    def boom(*a, **k):
        raise AssertionError("不该拉起批准进程")

    monkeypatch.setattr(mcp_server.subprocess, "Popen", boom)

    out = mcp_server._request_approval("old_candidate.py")

    assert "候选" in out
    assert "不能请求批准" in out
    assert "old_candidate.py" not in out


def test_request_approval_without_a_session_is_refused(clean_state, monkeypatch):
    """没有会话记录时**拒绝**拉起批准进程(第 5 轮 codex 的 L5)。

    原来这里会照常拉起,于是模型可以把候选目录里的**任意旧文件**推给人批——
    批准流程本身成了「批任意文件」的后门。人要直接批某个候选,
    走终端里的 `research_api.py review/approve`,不走这个工具。
    """
    def boom(*a, **k):
        raise AssertionError("不该拉起批准进程")

    monkeypatch.setattr(mcp_server.subprocess, "Popen", boom)

    out = mcp_server._request_approval("cand.py")

    assert "没有进行中的研究" in out
    assert "先发起研究" in out


def test_request_approval_surfaces_the_fail_closed_reason(clean_state, monkeypatch):
    """批准进程 fail-closed 时(比如脱敏模块不可用),原因要透出来(第 5 轮 codex 的 L1)。

    否则人只看到「批准进程没起来」,不知道是「看不到源码所以拒绝发页面」。
    """
    ready = {"event": "error", "message": "脱敏模块不可用,拒绝提供批准页"}
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda *a, **k: FakeApprovalProc(ready))
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand.py",
                                       "waiting_node": "input_wait", "waiting_message_id": 12}})

    out = mcp_server._request_approval("cand.py")

    assert "拒绝" in out
    assert "脱敏模块不可用" in out


# ---------- 批准绑定的是内容哈希 ----------

def test_approval_state_requires_the_same_content_hash(clean_state, monkeypatch):
    """发起研究之后候选文件被改动过 → 之前的批准不算数。"""
    pending = clean_state / "pending_strategies"
    pending.mkdir()
    cand = pending / "cand_1.py"
    cand.write_text("class C: pass\n", encoding="utf-8")
    sha_now = mcp_server.rs.file_sha256(cand)
    approvals = clean_state / "approvals.json"
    approvals.write_text(json.dumps({sha_now: {"name": "cand_1.py", "at": "now"}}), encoding="utf-8")
    monkeypatch.setattr(mcp_server, "approvals_file", lambda: approvals)

    session = {"candidate": "cand_1.py", "candidate_sha": sha_now}
    assert mcp_server.approval_state(session)[0] is True

    # 文件在发起研究之后被改了
    cand.write_text("class C: pass  # 改了\n", encoding="utf-8")
    approved, reason = mcp_server.approval_state(session)
    assert approved is False
    assert "改动" in reason


def test_long_idea_is_rejected(clean_state):
    out = mcp_server._start_research("x" * (mcp_server.MAX_IDEA_LEN + 1))
    assert "太长" in out
