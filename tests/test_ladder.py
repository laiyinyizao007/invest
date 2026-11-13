"""梯子式量化:纯逻辑 + tmp_path,不依赖 vn.py。规则见 risk/ladder.py 与 ADR「阶段 3 修订」。

状态是**只追加、带哈希链的日志** + 头指针(第 15 轮 codex 的 High:原先是一个只校验形状的 JSON,
`queries`/`best` 改小都被接受,续期与回滚无法区分)。
"""
import json
import math

import pytest

from risk.ladder import (DEFAULT_QUERY_BUDGET, GENESIS, LADDER_ETA, Ladder, LadderError, LadderExhausted, round_to_step)


@pytest.fixture
def ladder(tmp_path):
    return Ladder(tmp_path / "ladder.json")


def log_rows(ladder):
    return [json.loads(line) for line in ladder.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_log(ladder, rows):
    ladder.log_path.write_text("".join(json.dumps(r, separators=(",", ":")) + chr(10) for r in rows), encoding="utf-8")


def head(ladder):
    return json.loads(ladder.path.read_text(encoding="utf-8"))


# ---------- 行为 ----------

def test_eta_is_the_value_registered_in_the_adr():
    """η = 1.10 是在任何候选生成之前登记的(ADR「阶段 3 前置」),代码常量必须与之一致。"""
    assert LADDER_ETA == 1.10


def test_a_fresh_ladder_reports_zero_until_a_full_step_is_beaten(ladder):
    for sharpe in (-3.0, 0.0, 0.5, 1.0, 1.09):
        out = ladder.submit(sharpe)
        assert out["reported"] == 0.0 and out["updated"] is False


def test_it_updates_only_when_the_best_is_beaten_by_at_least_one_step(ladder):
    assert ladder.submit(1.10)["reported"] == 1.10               # 恰好一个台阶:更新
    assert ladder.submit(2.19)["reported"] == 1.10               # 差 0.01 不够一个台阶:重复上次值
    out = ladder.submit(2.20)
    assert out["reported"] == 2.20 and out["updated"] is True


def test_the_reported_value_is_rounded_to_a_multiple_of_the_step(ladder):
    assert ladder.submit(1.37)["reported"] == 1.10               # 1.37 / 1.10 = 1.245 → 1 个台阶
    out = ladder.submit(3.60)                                    # 3.60 / 1.10 = 3.27 → 3 个台阶,且比 1.10 高出 ≥ 1 台阶
    assert out["reported"] == round_to_step(3.60) == 3.30
    assert math.isclose(out["reported"] / LADDER_ETA, round(out["reported"] / LADDER_ETA), abs_tol=1e-9)


def test_a_repeated_report_leaks_nothing_new(ladder):
    """两个差别很大但都没跨过台阶的成绩,读到的是同一个数——这正是梯子要的。"""
    ladder.submit(2.30)
    assert ladder.submit(2.35)["reported"] == ladder.submit(3.00)["reported"] == 2.20


def test_the_best_never_decreases(ladder):
    ladder.submit(3.5)
    assert ladder.submit(-10.0)["reported"] == 3.30
    assert ladder.submit(0.0)["reported"] == 3.30


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, True, "1.5"])
def test_non_finite_or_malformed_input_counts_as_no_improvement_but_still_costs_a_query(ladder, bad):
    before = ladder.status()["queries_used"]
    out = ladder.submit(bad)
    assert out["reported"] == 0.0 and out["updated"] is False
    assert ladder.status()["queries_used"] == before + 1         # 不能靠喂垃圾来白嫖查询


def test_every_submit_costs_a_query_and_the_budget_is_a_hard_stop(tmp_path):
    ladder = Ladder(tmp_path / "l.json", budget=3)
    for _ in range(3):
        ladder.submit(0.0)
    assert ladder.status()["queries_left"] == 0
    with pytest.raises(LadderExhausted):
        ladder.submit(9.0)
    assert ladder.status()["queries_used"] == 3                  # 被拒绝的查询不改变状态
    assert ladder.status()["reported"] == 0.0                    # 也没有因此放出任何新信息


def test_status_and_preview_do_not_consume_a_query(ladder):
    ladder.submit(1.2)
    for _ in range(10):
        ladder.status()
        ladder.preview(5.0)
    assert ladder.status()["queries_used"] == 1 and len(log_rows(ladder)) == 1


def test_preview_says_exactly_what_submit_will_return(ladder):
    ladder.submit(1.2)
    for sharpe in (0.1, 1.2, 2.3, 7.7, float("nan")):
        planned = ladder.preview(sharpe)
        done = Ladder(ladder.path).submit(sharpe)
        assert {k: planned[k] for k in ("reported", "updated", "queries_used", "queries_left")} == \
               {k: done[k] for k in ("reported", "updated", "queries_used", "queries_left")}


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "l.json"
    Ladder(path).submit(2.5)
    again = Ladder(path)
    assert again.status()["reported"] == 2.20 and again.status()["queries_used"] == 1
    assert again.submit(0.1)["reported"] == 2.20                 # 重启不会让梯子从头开始


# ---------- 幂等:同一个 run 重复提交只回放,不再扣查询(第 15 轮 codex 的 High) ----------

def test_submitting_the_same_run_again_replays_the_value_and_costs_nothing(ladder):
    first = ladder.submit(2.5, run_id="run1")
    for _ in range(5):
        again = ladder.submit(2.5, run_id="run1")
        assert again["reported"] == first["reported"] and again["replay"] is True
    assert ladder.status()["queries_used"] == 1 and len(log_rows(ladder)) == 1


def test_a_replay_returns_the_value_of_that_time_not_the_current_best(ladder):
    first = ladder.submit(1.3, run_id="old")                     # 当时汇报 1.10
    ladder.submit(5.5, run_id="new")                             # 梯子之后升到 5.5
    assert ladder.submit(1.3, run_id="old")["reported"] == first["reported"] == 1.10


def test_a_replay_works_even_when_the_budget_is_exhausted(tmp_path):
    ladder = Ladder(tmp_path / "l.json", budget=1)
    first = ladder.submit(2.5, run_id="run1")
    with pytest.raises(LadderExhausted):
        ladder.submit(2.5, run_id="run2")
    assert ladder.submit(2.5, run_id="run1")["reported"] == first["reported"]


def test_a_replay_ignores_the_sharpe_given_the_second_time(ladder):
    ladder.submit(2.5, run_id="run1")
    assert ladder.submit(99.0, run_id="run1")["reported"] == 2.20 and ladder.status()["queries_used"] == 1


@pytest.mark.parametrize("run_id", ["", "../x", "a b", "x" * 65, 5, True])
def test_an_invalid_run_id_is_refused(ladder, run_id):
    with pytest.raises(LadderError):
        ladder.submit(1.0, run_id=run_id)


def test_has_tells_whether_a_run_was_submitted(ladder):
    ladder.submit(1.0, run_id="a")
    assert ladder.has("a") and not ladder.has("b")


# ---------- 续期:显式、留痕、保留 best ----------

def test_renewal_resets_the_queries_keeps_the_best_and_leaves_an_audit_record(tmp_path):
    ladder = Ladder(tmp_path / "l.json", budget=2)
    ladder.submit(3.5)
    ladder.submit(0.0)
    with pytest.raises(LadderExhausted):
        ladder.submit(1.0)
    out = ladder.renew("人工续期:第 1 批候选已复核")
    assert out["reported"] == 3.30 and out["queries_left"] == 2
    assert ladder.status()["queries_used"] == 0 and ladder.status()["reported"] == 3.30     # best 没被重置
    row = log_rows(ladder)[-1]
    assert row["event"] == "renew" and row["note"] == "人工续期:第 1 批候选已复核"
    assert ladder.submit(0.0)["queries_used"] == 1                                         # 续期后继续可用


@pytest.mark.parametrize("note", ["", "   ", None, 5, "x" * 201])
def test_renewal_needs_a_real_reason(ladder, note):
    with pytest.raises(LadderError):
        ladder.renew(note)


# ---------- 防篡改 / 防回滚 / 防截断(第 15 轮 codex 的 High) ----------

def two_steps(tmp_path):
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(5.5, run_id="a")
    ladder.submit(0.0, run_id="b")
    return ladder


def test_rolling_the_head_pointer_back_is_detected(tmp_path):
    """codex 复现:把状态里的 best/queries 改小被原样接受。现在头指针必须与日志对得上。"""
    ladder = two_steps(tmp_path)
    h = head(ladder)
    h["queries"], h["best"] = 0, 1.1
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()
    with pytest.raises(LadderError):
        ladder.submit(9.0)


def test_truncating_the_log_is_detected_because_the_head_is_ahead(tmp_path):
    ladder = two_steps(tmp_path)
    write_log(ladder, log_rows(ladder)[:1])
    with pytest.raises(LadderError):
        ladder.status()


def test_editing_a_log_row_breaks_the_hash_chain(tmp_path):
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    rows[1]["queries"] = 1                                       # 把"已用 2 次"改成"已用 1 次"(买回一次查询)
    write_log(ladder, rows)
    with pytest.raises(LadderError):
        ladder.status()


def test_lowering_best_in_the_log_is_detected(tmp_path):
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    rows[0]["best"] = 1.1
    write_log(ladder, rows)
    with pytest.raises(LadderError):
        ladder.status()


def test_even_a_consistently_rehashed_forgery_cannot_roll_the_best_back(tmp_path):
    """就算有人重算了哈希链:状态转移规则(best 只升、queries 恰好 +1)本身也不允许回滚。"""
    from risk.ladder import _body, _digest
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    rows[1]["best"] = 0.0                                        # 第二条把 best 从 5.5 降回 0
    rows[1]["hash"] = _digest(rows[1]["prev"], _body(rows[1]))
    write_log(ladder, rows)
    h = head(ladder)
    h["hash"] = rows[1]["hash"]
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_even_a_consistently_rehashed_forgery_cannot_buy_back_a_query(tmp_path):
    """重算了哈希链也不行:`queries` 每次提交必须恰好 +1,把它改小(买回查询)违反状态转移规则。"""
    from risk.ladder import _body, _digest
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    rows[1]["queries"] = 1                                       # 第二次提交的 queries 与第一次相同:等于白送一次
    rows[1]["hash"] = _digest(rows[1]["prev"], _body(rows[1]))
    write_log(ladder, rows)
    h = head(ladder)
    h.update(hash=rows[1]["hash"], queries=1)
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_even_a_consistently_rehashed_forgery_cannot_exceed_the_budget(tmp_path):
    from risk.ladder import _body, _digest
    ladder = Ladder(tmp_path / "l.json", budget=2)
    ladder.submit(0.0)
    ladder.submit(0.0)
    rows = log_rows(ladder)
    forged = {"seq": 3, "event": "submit", "run_id": None, "queries": 3, "best": 0.0, "budget": 2, "note": None,
              "prev": rows[-1]["hash"], "hash": ""}                       # 第 3 次提交,超过预算 2
    forged["hash"] = _digest(forged["prev"], _body(forged))
    write_log(ladder, rows + [forged])
    h = head(ladder)
    h.update(seq=3, hash=forged["hash"], queries=3)
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_a_forged_renewal_that_also_changes_best_is_refused(tmp_path):
    from risk.ladder import _body, _digest
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    forged = {"seq": 3, "event": "renew", "run_id": None, "queries": 0, "best": 0.0, "budget": 60, "note": "x",
              "prev": rows[-1]["hash"], "hash": ""}
    forged["hash"] = _digest(forged["prev"], _body(forged))
    write_log(ladder, rows + [forged])
    h = head(ladder)
    h.update(seq=3, hash=forged["hash"])
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_a_log_without_a_head_or_a_head_without_a_log_is_corruption(tmp_path):
    ladder = two_steps(tmp_path)
    ladder.path.unlink()
    with pytest.raises(LadderError):
        ladder.status()
    other = two_steps(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    other.log_path.unlink()
    with pytest.raises(LadderError):
        other.status()


def test_a_head_that_lags_by_one_row_is_repaired_from_the_log(tmp_path):
    """日志已追加、头指针还没写完就崩了:日志是事实来源,头指针按它修复,不是损坏。"""
    ladder = two_steps(tmp_path)
    rows = log_rows(ladder)
    h = head(ladder)
    h.update(seq=1, hash=rows[0]["hash"], queries=1, best=rows[0]["best"])
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    assert ladder.status()["queries_used"] == 2
    assert head(ladder)["seq"] == 2


def test_a_crash_after_the_very_first_log_row_is_repaired_too(tmp_path):
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(2.0)
    ladder.path.unlink()
    assert ladder.status()["queries_used"] == 1 and ladder.path.exists()


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"seq": 1}'])
def test_a_garbled_head_fails_closed_and_is_left_untouched(tmp_path, content):
    ladder = two_steps(tmp_path)
    ladder.path.write_text(content, encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.submit(5.0)
    assert ladder.path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("line", ["not json", "{}", "[]", '{"seq": 1}'])
def test_a_garbled_log_fails_closed(tmp_path, line):
    ladder = two_steps(tmp_path)
    ladder.log_path.write_text(line + chr(10), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_the_log_chain_starts_from_the_genesis_hash(ladder):
    ladder.submit(1.0)
    assert log_rows(ladder)[0]["prev"] == GENESIS


def test_a_state_cannot_loosen_the_registered_budget(tmp_path):
    from risk.ladder import _body, _digest
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(1.0)
    rows = log_rows(ladder)
    rows[0]["budget"] = 10_000
    rows[0]["hash"] = _digest(rows[0]["prev"], _body(rows[0]))
    write_log(ladder, rows)
    h = head(ladder)
    h["hash"] = rows[0]["hash"]
    ladder.path.write_text(json.dumps(h), encoding="utf-8")
    with pytest.raises(LadderError):
        ladder.status()


def test_a_tighter_budget_set_by_an_instance_applies_to_an_existing_ladder(tmp_path):
    Ladder(tmp_path / "l.json").submit(1.0)
    tight = Ladder(tmp_path / "l.json", budget=1)
    with pytest.raises(LadderExhausted):
        tight.submit(1.0)


@pytest.mark.parametrize("eta", [0.1, 0.25, 1.0, 1.0999, 2.0, float("nan"), True])
def test_the_step_cannot_be_overridden_in_code(tmp_path, eta):
    with pytest.raises(LadderError):
        Ladder(tmp_path / "l.json", eta=eta)


@pytest.mark.parametrize("budget", [0, -1, DEFAULT_QUERY_BUDGET + 1, 10**6, True, 1.5])
def test_the_budget_cannot_exceed_the_registered_ceiling(tmp_path, budget):
    with pytest.raises(LadderError):
        Ladder(tmp_path / "l.json", budget=budget)


def test_a_failed_log_write_fails_closed_and_releases_nothing(tmp_path, monkeypatch):
    """写不进状态就不能放出汇报值:否则一次失败的写入就等于一次免费的查询。"""
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(0.0)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("risk.ladder.os.fsync", boom)
    with pytest.raises(LadderError):
        ladder.submit(9.0)
    monkeypatch.undo()
    assert ladder.status()["reported"] == 0.0


def test_error_messages_name_no_local_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(LadderError) as exc:
        Ladder(blocker / "sub" / "l.json").submit(1.0)
    assert str(tmp_path) not in str(exc.value) and "blocker" not in str(exc.value)


def test_the_files_never_contain_non_finite_numbers(ladder):
    ladder.submit(float("nan"))
    for text in (ladder.path.read_text(encoding="utf-8"), ladder.log_path.read_text(encoding="utf-8")):
        assert "NaN" not in text and "Infinity" not in text
        for line in text.splitlines():
            json.loads(line)
