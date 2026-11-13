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
    (app.paths["results_dir"] / "ladder").mkdir(parents=True, exist_ok=True)
    (app.paths["results_dir"] / "ladder" / "ladder.json").write_text(
        json.dumps({"version": 1, "eta": 1.1, "budget": 1, "queries": 1, "best": 0.0}), encoding="utf-8")


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
    (app.paths["results_dir"] / "ladder" / "ladder.json").write_text(
        json.dumps({"version": 1, "eta": 1.1, "budget": 1, "queries": 1, "best": 3.3}), encoding="utf-8")
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
