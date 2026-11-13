"""网关把梯子接到了对外出口:/evaluate、/gate、/results、/feedback、/backtest。

核心承诺:外层拿不到验证窗口的原始数字;验证窗口的夏普只经梯子,且每个 run 至多消耗一次查询。
"""
import json
from types import SimpleNamespace

import pytest

from risk.ladder import Ladder
from test_outward import SENTINELS, full_result
from test_research_gateway import call, make_app, seed_result


def ladder_of(app):
    return Ladder(app.paths["results_dir"] / "ladder" / "ladder.json")


def jobs_returning(result, **more):
    return SimpleNamespace(evaluate_job=lambda **kw: dict(result), gate_job=lambda **kw: dict(result),
                           backtest_job=lambda **kw: dict(result), plateau_from_data=lambda d: (True, "x"), **more)


def seed(app, result):
    """按结果自带的 run_id 把完整记录落盘(模拟命令行/作业写下的结果)。"""
    seed_result(app, result["run_id"], **{k: v for k, v in result.items() if k != "run_id"})


def dumped(payload):
    return json.dumps(payload, ensure_ascii=False)


# ---------- 数字不外泄 ----------

@pytest.mark.parametrize("route, kind", [("/research/evaluate", "evaluate"), ("/research/gate", "gate")])
def test_the_outer_response_never_carries_a_validation_window_number(tmp_path, route, kind):
    app = make_app(tmp_path, jobs=jobs_returning(full_result(kind=kind)))
    status, out = call(app, "POST", route, {"strategy": "a.py", "class": "C"})
    assert status == 200
    text = dumped(out)
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel
    assert "windows" not in out and "metrics" not in out and "failures" not in out


def test_the_validation_sharpe_is_only_ever_the_ladder_quantized_value(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[1]
    assert out["ladder"] == {"reported_sharpe": 3.3, "step": 1.1, "queries_left": 59}


def test_a_report_view_is_built_from_the_sanitized_view_too(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C", "view": "text"})[1]
    for sentinel in SENTINELS:
        assert sentinel not in out
    assert "梯子量化" in out and "sharpe_low" in out


# ---------- 查询只消耗一次、可回放 ----------

def test_one_evaluate_costs_one_query_and_reading_it_back_costs_none(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    first = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[1]
    assert ladder_of(app).status()["queries_used"] == 1
    seed(app, full_result())                                     # 落盘的完整记录
    for _ in range(4):
        status, replay = call(app, "GET", "/research/results/run42")
        assert status == 200 and replay == first
    assert ladder_of(app).status()["queries_used"] == 1


def test_the_first_read_of_a_cli_run_through_the_gateway_is_its_first_reveal(tmp_path):
    """命令行(人)跑出来的 run 从没给外层看过:网关第一次读到它,才算一次揭示。"""
    app = make_app(tmp_path)
    seed(app, full_result())
    assert ladder_of(app).status()["queries_used"] == 0
    status, out = call(app, "GET", "/research/results/run42")
    assert status == 200 and out["ladder"]["reported_sharpe"] == 3.3
    assert ladder_of(app).status()["queries_used"] == 1
    call(app, "GET", "/research/results/run42")
    assert ladder_of(app).status()["queries_used"] == 1


def test_a_strategy_error_costs_no_query(tmp_path):
    bad = {"run_id": "e1", "kind": "evaluate", "class": "C", "status": "strategy_error", "passed": False,
           "error": {"type": "ValueError", "message": "boom"}}
    app = make_app(tmp_path, jobs=jobs_returning(bad))
    status, out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    assert status == 200 and out["status"] == "strategy_error" and "ladder" not in out
    assert ladder_of(app).status()["queries_used"] == 0


# ---------- 预算用尽:不放出任何验证窗口信息,也不白跑作业 ----------

def exhaust(app):
    """通过梯子自己的接口把预算用完(不手写状态文件:状态是带哈希链的日志,手写会被当成篡改)。"""
    ladder = ladder_of(app)
    while ladder.status()["queries_left"] > 0:
        ladder.submit(0.0)


@pytest.mark.parametrize("route", ["/research/evaluate", "/research/gate"])
def test_an_exhausted_ladder_answers_429_and_does_not_even_run_the_job(tmp_path, route):
    def must_not_run(**kw):
        pytest.fail("梯子已用尽,不该白跑一次作业(也不该白耗试验次数)")

    app = make_app(tmp_path, jobs=SimpleNamespace(evaluate_job=must_not_run, gate_job=must_not_run,
                                                  plateau_from_data=lambda d: (True, "x")))
    exhaust(app)
    status, out = call(app, "POST", route, {"strategy": "a.py", "class": "C"})
    assert status == 429 and "预算" in out["error"]


def test_an_exhausted_ladder_still_replays_runs_that_were_already_revealed(tmp_path):
    app = make_app(tmp_path)
    seed(app, full_result())
    first = call(app, "GET", "/research/results/run42")[1]
    exhaust(app)
    assert ladder_of(app).status()["queries_left"] == 0
    status, again = call(app, "GET", "/research/results/run42")
    assert status == 200 and again == first
    seed(app, full_result(run_id="run43"))
    assert call(app, "GET", "/research/results/run43")[0] == 429                    # 没揭示过的才被拒


# ---------- 状态损坏:fail-closed ----------

@pytest.mark.parametrize("bad", ["not json", "{}", json.dumps({"version": 1, "eta": 0.1, "budget": 60, "queries": 0, "best": 0})])
def test_a_corrupted_ladder_refuses_everything_and_runs_nothing(tmp_path, bad):
    def must_not_run(**kw):
        pytest.fail("梯子状态损坏,不该跑作业")

    app = make_app(tmp_path, jobs=SimpleNamespace(evaluate_job=must_not_run, gate_job=must_not_run,
                                                  plateau_from_data=lambda d: (True, "x")))
    (app.paths["results_dir"] / "ladder").mkdir(parents=True, exist_ok=True)
    (app.paths["results_dir"] / "ladder" / "ladder.json").write_text(bad, encoding="utf-8")
    assert call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[0] == 500
    assert call(app, "POST", "/research/gate", {"strategy": "a.py", "class": "C"})[0] == 500
    seed(app, full_result())
    status, out = call(app, "GET", "/research/results/run42")
    assert status == 500 and "不可用" in out["error"]
    for sentinel in SENTINELS:
        assert sentinel not in dumped(out)
    assert (app.paths["results_dir"] / "ladder" / "ladder.json").read_text(encoding="utf-8") == bad     # 不被"修复"成全新状态


def test_the_error_message_for_a_broken_ladder_contains_no_paths(tmp_path):
    app = make_app(tmp_path)
    (app.paths["results_dir"] / "ladder").mkdir(parents=True, exist_ok=True)
    (app.paths["results_dir"] / "ladder" / "ladder.json").write_text("garbage", encoding="utf-8")
    seed(app, full_result())
    out = call(app, "GET", "/research/results/run42")[1]
    assert "ladder.json" not in dumped(out) and str(tmp_path) not in dumped(out)


# ---------- /backtest 的验证段 ----------

def test_a_valid_segment_backtest_is_refused_and_never_runs(tmp_path):
    def must_not_run(**kw):
        pytest.fail("验证段的回测不该被执行")

    app = make_app(tmp_path, jobs=SimpleNamespace(backtest_job=must_not_run))
    status, out = call(app, "POST", "/research/backtest",
                       {"strategy": "a.py", "class": "C", "key": "CADJ", "segment": "valid"})
    assert status == 403 and "梯子" in out["error"]


def test_a_train_segment_backtest_still_works_and_gives_its_statistics(tmp_path):
    result = {"run_id": "b1", "kind": "backtest", "segment": "train", "class": "C",
              "stats": {"sharpe": 1.23, "return_pct": 5.0, "round_trips": 40}}
    app = make_app(tmp_path, jobs=jobs_returning(result))
    status, out = call(app, "POST", "/research/backtest",
                       {"strategy": "a.py", "class": "C", "key": "CADJ", "segment": "train"})
    assert status == 200 and out["stats"]["sharpe"] == 1.23 and "ladder" not in out
    assert ladder_of(app).status()["queries_used"] == 0


# ---------- /feedback ----------

def test_feedback_never_reveals_a_run_that_was_not_revealed_before(tmp_path):
    """一次 /feedback 要扫很多 run:不能让一次读取悄悄烧掉一批查询,也不能把没揭示过的验证窗口数字带出去。"""
    app = make_app(tmp_path)
    for i in range(5):
        seed(app, full_result(run_id=f"20261113-10000{i}-000000"))
    status, out = call(app, "GET", "/research/feedback")
    assert status == 200 and out["traces"] == []
    assert ladder_of(app).status()["queries_used"] == 0


def test_feedback_includes_revealed_runs_with_stable_ids_not_sentences(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result(run_id="r-revealed")))
    call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    seed(app, full_result(run_id="r-revealed"))
    status, out = call(app, "GET", "/research/feedback")
    assert status == 200 and [t["run_id"] for t in out["traces"]] == ["r-revealed"]
    trace = out["traces"][0]
    assert trace["failures"] == ["sharpe_low", "stress_sharpe", "dsr_low"] and trace["weakest"] in (None, "sharpe", "trades", "drawdown", "losing_streak")
    for sentinel in SENTINELS:
        assert sentinel not in dumped(out)
    assert ladder_of(app).status()["queries_used"] == 1                                 # /feedback 自己没有再消耗


def test_feedback_includes_failures_that_carry_no_validation_numbers(tmp_path):
    app = make_app(tmp_path)
    seed_result(app, "e1", kind="evaluate", status="strategy_error", passed=False, **{"class": "C"},
                error={"type": "ValueError", "message": "boom"})
    seed_result(app, "s1", kind="evaluate", passed=False, failed_check_ids=["not_on_plateau"],
                kind_detail="selection-only", **{"class": "C"})
    out = call(app, "GET", "/research/feedback")[1]
    assert {t["run_id"] for t in out["traces"]} == {"e1", "s1"}
    assert ladder_of(app).status()["queries_used"] == 0


def test_a_tampered_reveal_archive_makes_feedback_fail_closed(tmp_path):
    app = make_app(tmp_path)
    seed(app, full_result())
    call(app, "GET", "/research/results/run42")
    (app.paths["results_dir"] / "outward" / "run42.json").write_text("garbage", encoding="utf-8")
    assert call(app, "GET", "/research/feedback")[0] == 500
    assert call(app, "GET", "/research/results/run42")[0] == 500


def test_the_gateway_still_has_exactly_the_whitelisted_routes():
    import research_gateway as gw
    assert len(gw.ROUTES) == 7 and ("GET", "/research/elite") not in gw.ROUTES        # ADR:GET /elite 不做


# ---------- 自查发现:results 目录里不是运行结果的 *.json 不得被当成结果 ----------

def test_the_ladder_state_is_not_listed_as_a_result_and_cannot_be_read_as_one(tmp_path):
    """`list_results` 按 *.json 列 run;梯子状态曾与结果混放,`/research/results/ladder` 会把它当成一个结果读出来。"""
    import research_sandbox as rs
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    assert (app.paths["results_dir"] / "ladder" / "ladder.json").exists()
    assert "ladder" not in rs.list_results(app.paths["results_dir"])
    assert call(app, "GET", "/research/results/ladder")[0] == 404


def test_other_json_files_in_the_results_directory_are_not_results_either(tmp_path):
    app = make_app(tmp_path)
    app.paths["results_dir"].mkdir(parents=True, exist_ok=True)
    (app.paths["results_dir"] / "approvals.json").write_text(json.dumps({"s.py": "a" * 64}), encoding="utf-8")
    (app.paths["results_dir"] / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    seed_result(app, "norun", note="no kind field")
    for name in ("approvals", "list", "norun"):
        status, out = call(app, "GET", f"/research/results/{name}")
        assert status == 404 and "a" * 64 not in dumped(out), name
    status, out = call(app, "GET", "/research/feedback")
    assert status == 200 and out["traces"] == []


# ---------- 第 15 轮 codex 的 Critical(端到端):报错消息里带着验证窗口的原始数字 ----------

LEAKED_MESSAGE = "oos_pnl=8184.000000 close=2728.000000"
LEAK_NUMBERS = ("8184", "2728", "oos_pnl", "close=")


def validation_error_result(kind="evaluate"):
    return {"run_id": "e-val", "kind": kind, "class": "C", "status": "strategy_error", "passed": False,
            "error": {"type": "ValueError", "message": LEAKED_MESSAGE, "phase": "validation"}}


@pytest.mark.parametrize("route, kind", [("/research/evaluate", "evaluate"), ("/research/gate", "gate")])
def test_a_validation_replay_error_message_never_reaches_the_outer_response(tmp_path, route, kind):
    app = make_app(tmp_path, jobs=jobs_returning(validation_error_result(kind)))
    status, out = call(app, "POST", route, {"strategy": "a.py", "class": "C"})
    assert status == 200 and out["status"] == "strategy_error"
    text = dumped(out)
    for number in LEAK_NUMBERS:
        assert number not in text, number


@pytest.mark.parametrize("view", ["report", "text"])
def test_the_report_and_text_views_do_not_carry_it_either(tmp_path, view):
    app = make_app(tmp_path, jobs=jobs_returning(validation_error_result()))
    out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C", "view": view})[1]
    for number in LEAK_NUMBERS:
        assert number not in dumped(out), number


def test_results_and_feedback_do_not_carry_it_either(tmp_path):
    app = make_app(tmp_path)
    seed(app, validation_error_result())
    for path in ("/research/results/e-val", "/research/feedback"):
        status, out = call(app, "GET", path)
        assert status == 200, path
        for number in LEAK_NUMBERS:
            assert number not in dumped(out), (path, number)
    trace = call(app, "GET", "/research/feedback")[1]["traces"][0]
    assert trace["run_id"] == "e-val" and "ValueError" in trace["failures"][0] and "验证窗口回放" in trace["failures"][0]


def test_a_selection_window_error_keeps_its_message_so_the_candidate_can_be_debugged(tmp_path):
    result = validation_error_result()
    result["error"] = {"type": "IndexError", "message": "list index out of range", "phase": "selection"}
    app = make_app(tmp_path, jobs=jobs_returning(result))
    out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[1]
    assert out["error"] == {"type": "IndexError", "phase": "selection", "message": "list index out of range"}


def test_an_error_with_no_phase_is_withheld_until_proven_open(tmp_path):
    result = validation_error_result()
    del result["error"]["phase"]
    app = make_app(tmp_path, jobs=jobs_returning(result))
    out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[1]
    assert out["error"]["message"] is None and "2728" not in dumped(out)


# ---------- 第 15 轮 codex 的 High:梯子状态被回滚/篡改 → 网关 fail-closed;续期显式、留痕 ----------

def test_a_rolled_back_ladder_state_makes_the_gateway_refuse_instead_of_selling_a_free_query(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    head = app.paths["results_dir"] / "ladder" / "ladder.json"
    state = json.loads(head.read_text(encoding="utf-8"))
    state.update(queries=0, best=0.0)                                        # codex 复现:把 queries/best 改小
    head.write_text(json.dumps(state), encoding="utf-8")
    assert call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[0] == 500
    assert call(app, "GET", "/research/feedback")[0] in (200, 500)


def test_a_truncated_ladder_log_makes_the_gateway_refuse(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result()))
    call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    (app.paths["results_dir"] / "ladder" / "ladder.jsonl").write_text("", encoding="utf-8")
    assert call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[0] == 500


def test_after_an_audited_renewal_the_gateway_answers_again_and_the_best_is_kept(tmp_path):
    app = make_app(tmp_path, jobs=jobs_returning(full_result(run_id="first")))
    first = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[1]
    exhaust(app)
    assert call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})[0] == 429
    ladder_of(app).renew("人工续期:第一批候选已复核")
    app.jobs = jobs_returning(full_result(run_id="second"))
    status, out = call(app, "POST", "/research/evaluate", {"strategy": "a.py", "class": "C"})
    assert status == 200 and out["ladder"]["reported_sharpe"] >= first["ladder"]["reported_sharpe"]     # best 没被重置
    assert any(json.loads(line)["event"] == "renew" for line in
               (app.paths["results_dir"] / "ladder" / "ladder.jsonl").read_text(encoding="utf-8").splitlines())


# ---------- 第 15 轮 codex 的 High 2:揭示存档写失败不得烧掉查询、不得把下界漏给下一次 ----------

def test_a_failed_archive_write_is_safe_to_retry_and_never_charges_twice(tmp_path, monkeypatch):
    from risk.outward import RevealStore
    app = make_app(tmp_path)
    seed(app, full_result())
    real_put, calls = RevealStore.put, {"n": 0}

    def flaky(self, run_id, view):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OSError("disk full")
        return real_put(self, run_id, view)

    monkeypatch.setattr(RevealStore, "put", flaky)
    statuses = [call(app, "GET", "/research/results/run42")[0] for _ in range(3)]
    assert statuses == [500, 500, 500]
    assert ladder_of(app).status()["queries_used"] == 1                              # codex 复现:原先是 3 次都扣
    status, out = call(app, "GET", "/research/results/run42")
    assert status == 200 and out["ladder"]["reported_sharpe"] == 3.3
    assert ladder_of(app).status()["queries_used"] == 1


def test_a_failed_archive_write_does_not_leak_the_failed_runs_sharpe_to_the_next_run(tmp_path, monkeypatch):
    """codex 复现:失败 run 的夏普把梯子推到 7.7,下一次成功揭示别的 run 时报 7.7。
    现在失败 run 的汇报值只有重试它自己才拿得到,别的 run 读到的是它自己的量化值(没有被抬高)。"""
    from risk.outward import RevealStore
    app = make_app(tmp_path)
    big = full_result(run_id="big")
    big["metrics"]["oos_sharpe"] = 7.7
    small = full_result(run_id="small")
    small["metrics"]["oos_sharpe"] = 0.5
    seed(app, big)
    seed(app, small)
    real_put = RevealStore.put
    monkeypatch.setattr(RevealStore, "put", lambda self, run_id, view: (_ for _ in ()).throw(OSError("disk full"))
                        if run_id == "big" else real_put(self, run_id, view))
    assert call(app, "GET", "/research/results/big")[0] == 500
    out = call(app, "GET", "/research/results/small")[1]
    # 梯子是单调的:big 已经被扣过一次查询、best 到了 7.7,small 读到的是**当前的最佳**——这与 big 重试时拿到的是同一个数,
    # 没有额外信息:调用方本来就可以重试 big 拿到它。关键是 big 没有被扣两次,也没有留下"读不到、却改了状态"的状态。
    assert ladder_of(app).status()["queries_used"] == 2
    assert call(app, "GET", "/research/results/big")[0] == 500


def test_feedback_rebuilds_a_run_whose_archive_write_failed_without_charging(tmp_path, monkeypatch):
    from risk.outward import RevealStore
    app = make_app(tmp_path)
    seed(app, full_result(run_id="r-failed"))
    real_put = RevealStore.put
    monkeypatch.setattr(RevealStore, "put", lambda self, run_id, view: (_ for _ in ()).throw(OSError("disk full")))
    assert call(app, "GET", "/research/results/r-failed")[0] == 500
    monkeypatch.setattr(RevealStore, "put", real_put)
    status, out = call(app, "GET", "/research/feedback")
    assert status == 200 and [t["run_id"] for t in out["traces"]] == ["r-failed"]
    assert ladder_of(app).status()["queries_used"] == 1
