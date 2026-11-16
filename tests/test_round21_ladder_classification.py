"""第 21 轮 codex 复核(38/50)的回归用例:分类与可用性边界。

对应 codex 第 21 轮复核的发现:
- **High-1**:一份**非法 UTF-8**的揭示存档,曾把整个 `/feedback` 打成 500「内部错误」
  (`UnicodeDecodeError` 是 `ValueError` 的子类,两个 `except` 谁也没接住它);
- **High-2**:"合法人工重置不算篡改"(第 20 轮的 H2)在**"梯子已经坏了"**这条正经路径上
  根本没生效——而那恰恰是 `reset` 的用武之地;
- **Medium-1**:伪造的孤立名单能把真正的篡改计数器 `skipped.tampered` 压成 0;
- **Medium-2 / L-1**:名单读不出来曾被说成"篡改"(第 20 轮刚立的"不可用 ≠ 篡改"原则
  在梯子这条路径上没有覆盖到);
- **L-2 / L-3 / L-4**:CLI 口径、名单集合每条结果重建一次、每个请求多 new 一个梯子。

这些用例都写成**端到端**的:分类错误只有在"从 HTTP 响应看计数"这一层才看得见。
"""
import json
from types import SimpleNamespace

import pytest

import research_config as rc
import research_gateway as gw
from risk.ladder import LadderError, anchor_path_for

TOKEN = "t" * 40
RUN = "20261112-210000-000000"


def make_app(tmp_path, **cfg_over):
    cfg = rc.load()
    for section, values in cfg_over.items():
        cfg[section].update(values)
    (tmp_path / "pending_strategies").mkdir(exist_ok=True)
    paths = {"pending_dir": tmp_path / "pending_strategies", "results_dir": tmp_path / "data" / "research",
             "approvals_file": tmp_path / "data" / "research" / "approvals.json", "token_file": tmp_path / "tok"}
    return gw.GatewayApp(cfg, paths, TOKEN, jobs=SimpleNamespace(), clock=lambda: 0.0, wall=lambda: 1_000_000.0)


def call(app, method, path, body=None, token=TOKEN):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    if payload:
        headers["content-type"] = "application/json"
    return app.handle(method, path, headers, payload)


def seed_revealed(app, run_id=RUN, **fields):
    """播一份**已经揭示过**的失败结果(带验证窗口夏普,并先过一次梯子)。"""
    d = app.paths["results_dir"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "metrics": {"oos_sharpe": 0.0}, **fields}),
                                     encoding="utf-8")
    app._ladder().submit(0.0, run_id=run_id)


def archive_of(app, run_id=RUN):
    return app.paths["results_dir"] / "outward" / f"{run_id}.json"


def break_the_anchor(app):
    """把锚点写坏:日志与锚点对不上 -> 梯子 fail-closed -> 这正是 `reset` 的用武之地。"""
    anchor = anchor_path_for(app.paths["results_dir"])
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"not-a-real-anchor\n")


# ---------- High-1:非法 UTF-8 的揭示存档 ----------

def test_feedback_counts_an_archive_with_broken_utf8_as_tampered_not_an_internal_error(tmp_path):
    """第 21 轮 codex 的 High-1:`UnicodeDecodeError` 是 `ValueError` 的子类,不是 `OSError`。

    此前 `read_text` 直接把它抛了出来,而两个 `except` 一个只接 `OSError`、一个只接解析期的
    `ValueError`——于是一份坏存档把**整个** `/feedback` 打成 500「内部错误」,
    进化回路唯一的诊断通道直接下线,运维看到的还是"内部错误"而不是篡改信号。"""
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")                      # 先生成揭示存档
    archive_of(app).write_bytes(b'{"version":1,"run_id":"\xff\xfe"}')

    status, body = call(app, "GET", "/research/feedback")

    assert status == 200
    assert body["skipped"]["tampered"] == 1                   # 计成篡改,不是"内部错误"
    assert body["skipped"]["unavailable"] == 0
    assert body["skipped"]["invalid_result"] == 0


def test_the_single_run_endpoint_reports_a_broken_utf8_archive_as_a_tamper_not_an_internal_error(tmp_path):
    """第 21 轮 codex 的 High-1 的另一半:单条接口此前同样退化成"内部错误"。"""
    app = make_app(tmp_path)
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")
    archive_of(app).write_bytes(b'{"version":1,"run_id":"\xff\xfe"}')

    status, body = call(app, "GET", f"/research/results/{RUN}")

    assert status == 500
    assert body["error"] == gw.LADDER_UNAVAILABLE             # 不是"内部错误"
    assert "内部错误" not in json.dumps(body, ensure_ascii=False)


# ---------- High-2:梯子已经坏了时的 reset ----------

def test_a_reset_on_a_broken_ladder_still_records_the_orphaned_runs(tmp_path):
    """第 21 轮 codex 的 High-2:对账失败**不等于** run_id 读不出来。

    此前 `reset` 只在 `_load()` 成功时才回填孤立名单,而"梯子已经坏了"正是 `reset` 的用武之地
    (它的 docstring 就这么写的)——于是名单恒为空,"合法人工重置不算篡改"这条承诺
    只在"梯子健康 + --force"这条被劝阻的路径上兑现。"""
    app = make_app(tmp_path)
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    break_the_anchor(app)                                     # 梯子坏掉(日志与锚点对不上)

    out = app._ladder().reset("人工重置")                      # 坏梯子允许重置(不需要 force)

    assert out["orphaned_count"] == 1
    assert out["orphaned_run_ids"] == [RUN]
    line = [json.loads(x) for x in (app.paths["results_dir"] / "ladder" / "ladder.reset.log")
            .read_text(encoding="utf-8").splitlines() if x.strip()][0]
    assert line["event"] == "reset-intent"
    assert line["orphaned_run_ids"] == [RUN]                   # 审计行与返回值一致


def test_a_reset_on_a_broken_ladder_still_reports_the_run_as_orphaned_not_tampered(tmp_path):
    """第 21 轮 codex 的 High-2 的端到端后果:坏梯子重置之后,那个 run 必须仍然被当作"被孤立"。"""
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")                      # 生成揭示存档
    break_the_anchor(app)
    app._ladder().reset("人工重置")

    status, body = call(app, "GET", "/research/feedback")

    assert status == 200
    assert body["skipped"]["orphaned_by_reset"] == 1
    assert body["skipped"]["tampered"] == 0
    status, body = call(app, "GET", f"/research/results/{RUN}")
    assert status == 410                                      # 孤立不是篡改:回 410 而不是 500
    assert "重置" in body["error"]


# ---------- Medium-1:伪造的孤立名单不得压掉篡改计数器 ----------

def test_a_forged_orphan_entry_cannot_suppress_the_tamper_counter(tmp_path):
    """第 21 轮 codex 的 Medium-1:孤立名单是无哈希链的普通文本,**可以伪造**。

    此前 `/feedback` 先查名单、后看异常自己的判定,于是往 `ladder.reset.log` 里追加一条
    点名的 `reset-intent`,就能把真正的篡改计数 `skipped.tampered` 压成 0——
    数据确实不放行,但这个计数器是运维面板上**唯一的**篡改信号。"""
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")                      # 生成揭示存档
    stored = json.loads(archive_of(app).read_text(encoding="utf-8"))
    stored["ladder"]["reported_sharpe"] = 999.0                # 篡改存档里的汇报值
    archive_of(app).write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    reset_log = app.paths["results_dir"] / "ladder" / "ladder.reset.log"
    reset_log.parent.mkdir(parents=True, exist_ok=True)
    reset_log.write_text(json.dumps({"event": "reset-intent", "archive": 1, "reason": "伪造",
                                     "discarded_best": None, "discarded_queries": None,
                                     "orphaned_count": 1, "orphaned_run_ids": [RUN]},
                                    ensure_ascii=False) + "\n", encoding="utf-8")

    status, body = call(app, "GET", "/research/feedback")

    assert status == 200
    assert body["skipped"]["tampered"] == 1                   # 伪造的名单压不掉篡改信号
    assert body["skipped"]["orphaned_by_reset"] == 0


# ---------- Medium-2 / L-1:名单读不出来不等于"被篡改" ----------

def test_a_broken_reset_log_does_not_take_the_feedback_channel_down(tmp_path):
    """第 21 轮 codex 的 Medium-2:`ladder.reset.log` 非法 UTF-8 曾让 `/feedback` 回 500「内部错误」。"""
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")
    reset_log = app.paths["results_dir"] / "ladder" / "ladder.reset.log"
    reset_log.parent.mkdir(parents=True, exist_ok=True)
    reset_log.write_bytes(b'{"event":"reset-intent","reason":"\xff\xfe"}')

    status, body = call(app, "GET", "/research/feedback")

    assert status == 200                                      # 此前是 500「内部错误」
    assert "skipped" in body                                  # 通道仍然给出结构化的跳过计数
    assert body["skipped"]["tampered"] == 0


def test_an_unreadable_orphan_list_is_reported_as_unavailable_not_as_tampering(tmp_path):
    """第 21 轮 codex 的 L-1:名单读不出来,不该被说成"篡改"。

    第 20 轮刚立的分类原则(不可用 ≠ 篡改)在梯子这条路径上没有覆盖到:
    此前读不到名单就当作"不是孤立",于是合法人工重置又被记成安全事件。"""
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    call(app, "GET", "/research/feedback")
    app._ladder().reset("人工重置", force=True)                 # 这个 run 现在不在梯子里了
    reset_log = app.paths["results_dir"] / "ladder" / "ladder.reset.log"
    reset_log.write_bytes(b'{"event":"reset-intent","reason":"\xff\xfe"}')   # 名单读不出来

    status, body = call(app, "GET", "/research/feedback")

    assert status == 200
    assert body["skipped"]["unavailable"] == 1                 # 可用性故障
    assert body["skipped"]["tampered"] == 0                    # 不是篡改
    assert body["skipped"]["orphaned_by_reset"] == 0


def test_the_ladder_reports_an_unreadable_orphan_list_as_an_error_not_as_an_empty_list(tmp_path):
    """第 21 轮 codex 的 L-1(单元级):"读不到"与"确实没有孤立项"必须能分开。"""
    app = make_app(tmp_path)
    ladder = app._ladder()
    reset_log = app.paths["results_dir"] / "ladder" / "ladder.reset.log"
    reset_log.parent.mkdir(parents=True, exist_ok=True)
    reset_log.write_bytes(b'{"event":"reset-intent","reason":"\xff\xfe"}')

    with pytest.raises(LadderError):
        ladder.orphaned_run_ids()


# ---------- L-2 / L-3 / L-4 ----------

def test_the_ladder_cli_help_mentions_the_reset_subcommand(capsys):
    """第 21 轮 codex 的 L-2:`--help` 里看不到 `reset`,而它是最重的一项操作。"""
    import research_ladder

    with pytest.raises(SystemExit):
        research_ladder.main(["--help"])

    out = capsys.readouterr().out
    assert "reset" in out
    assert "renew" in out


def test_the_orphan_set_is_cached_instead_of_rebuilt_for_every_result(tmp_path):
    """第 21 轮 codex 的 L-3:每次重建一个 set 是 O(结果数 × 名单长)。"""
    app = make_app(tmp_path)
    ladder = app._ladder()
    ladder.reset("人工重置", force=True)

    first = ladder.orphaned_run_id_set()
    second = ladder.orphaned_run_id_set()

    assert first is second                                      # 同一个缓存对象,没有重建


def test_the_feedback_request_uses_one_ladder_instance(tmp_path, monkeypatch):
    """第 21 轮 codex 的 L-4:此前为读孤立名单又 new 了一个梯子(每请求多解析一次重置日志)。"""
    import risk.ladder as ladder_mod
    app = make_app(tmp_path, feedback={"limit": 5})
    seed_revealed(app, kind="gate", passed=False, failures=["x"], failed_check_ids=["sharpe_low"],
                  **{"class": "S", "key": "C15"})
    made = []
    original = ladder_mod.Ladder.__init__

    def counting(self, *args, **kwargs):
        made.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(ladder_mod.Ladder, "__init__", counting)
    call(app, "GET", "/research/feedback")

    assert len(made) == 1                                       # 一次请求只建一个梯子
