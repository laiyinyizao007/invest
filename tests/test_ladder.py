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
    assert ladder.status()["queries_used"] == 2                       # 只读入口读得到真实状态……
    assert head(ladder)["seq"] == 1                                   # ……但**不写文件**(第 16 轮 L2)
    ladder.submit(0.0)                                                # 写入口才修复
    assert head(ladder)["seq"] == 3


def test_a_crash_after_the_very_first_log_row_is_repaired_too(tmp_path):
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(2.0)
    ladder.path.unlink()
    assert ladder.status()["queries_used"] == 1 and not ladder.path.exists()     # 只读入口不改状态
    ladder.submit(0.0)
    assert ladder.path.exists()


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


# ---------- 第 16 轮 codex 的 H2:一致回滚必须被锚点发现 ----------

def anchored(tmp_path):
    return Ladder(tmp_path / "state" / "l.json", anchor=tmp_path / "elsewhere" / "l.anchor")


def consistent_truncate(ladder, keep):
    """截断日志到前 keep 行,并把头指针同步改成与第 keep 行一致、哈希链完全合法的状态——只靠链检测不到的回滚。"""
    lines = ladder.log_path.read_text(encoding="utf-8").splitlines(keepends=True)
    ladder.log_path.write_text("".join(lines[:keep]), encoding="utf-8")
    last = json.loads(lines[keep - 1])
    ladder.path.write_text(json.dumps({"version": 2, "seq": last["seq"], "hash": last["hash"], "queries": last["queries"],
                                       "best": last["best"], "budget": last["budget"], "eta": 1.1}), encoding="utf-8")


def test_without_an_anchor_a_consistent_truncation_is_NOT_detected_which_is_why_the_anchor_exists(tmp_path):
    """如实记录:没有锚点时,这类回滚检测不到(上一版文档的\\"回滚 fail-closed\\"是过度声明)。"""
    ladder = Ladder(tmp_path / "l.json")
    for _ in range(5):
        ladder.submit(0.0)
    consistent_truncate(ladder, 2)
    assert ladder.status()["queries_used"] == 2                       # 被接受:退回了 3 次查询


def test_with_an_anchor_a_consistent_truncation_fails_closed(tmp_path):
    ladder = anchored(tmp_path)
    for _ in range(5):
        ladder.submit(0.0)
    consistent_truncate(ladder, 2)
    for call in (ladder.status, lambda: ladder.submit(0.0), lambda: ladder.has("x")):
        with pytest.raises(LadderError, match="锚点"):
            call()


def test_with_an_anchor_replacing_the_whole_chain_with_another_valid_chain_fails_closed(tmp_path):
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    other = Ladder(tmp_path / "o" / "l.json")                         # 另一条同样合法、但不是这条的链
    for _ in range(3):
        other.submit(3.0)                                             # best 不同 → 哈希不同(内容相同的链哈希是确定的)
    ladder.log_path.write_text(other.log_path.read_text(encoding="utf-8"), encoding="utf-8")
    ladder.path.write_text(other.path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(LadderError, match="锚点"):
        ladder.status()


def test_with_an_anchor_deleting_only_the_anchor_or_only_the_log_fails_closed(tmp_path):
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    ladder.anchor.unlink()
    with pytest.raises(LadderError, match="锚点"):
        ladder.status()
    ladder2 = anchored(tmp_path / "b")
    for _ in range(3):
        ladder2.submit(0.0)
    ladder2.log_path.unlink()
    ladder2.path.unlink()
    with pytest.raises(LadderError, match="锚点"):
        ladder2.status()


def test_a_crash_before_the_anchor_is_written_is_not_a_false_alarm(tmp_path):
    """锚点只会落后:崩在写锚点之前,下一次读取是正常的(锚点不超前)。"""
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    lines = ladder.anchor.read_text(encoding="utf-8").splitlines(keepends=True)
    ladder.anchor.write_text("".join(lines[:-1]), encoding="utf-8")
    assert ladder.status()["queries_used"] == 3
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 4


def test_a_garbled_anchor_fails_closed(tmp_path):
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    ladder.anchor.write_text("garbage" + chr(10), encoding="utf-8")
    with pytest.raises(LadderError, match="锚点"):
        ladder.status()


def test_deleting_everything_including_the_anchor_is_still_undetectable_and_that_is_documented(tmp_path):
    """如实记录的边界:能同时改三处/全删的人,梯子拦不住。"""
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    for f in (ladder.path, ladder.log_path, ladder.anchor):
        f.unlink()
    assert ladder.status()["queries_used"] == 0


def test_status_has_and_preview_never_write(tmp_path):
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    before = {f: f.read_bytes() for f in (ladder.path, ladder.log_path, ladder.anchor)}
    ladder.status(), ladder.has("x"), ladder.preview(2.0)
    assert before == {f: f.read_bytes() for f in before}


# ---------- 第 17 轮 codex ----------

def test_a_truncated_anchor_does_NOT_defeat_a_consistent_truncation_only_if_it_is_not_cut_too(tmp_path):
    """如实记录(H1):锚点与日志**一起**截,梯子发现不了——锚点是误操作探测器,不是回滚探测器。"""
    ladder = anchored(tmp_path)
    for _ in range(5):
        ladder.submit(0.0)
    consistent_truncate(ladder, 2)
    lines = ladder.anchor.read_text(encoding="utf-8").splitlines(keepends=True)
    ladder.anchor.write_text("".join(lines[:2]), encoding="utf-8")
    assert ladder.status()["queries_used"] == 2                       # 被接受:这是残余 #12 里写明的边界


@pytest.mark.parametrize("content", [
    "0 " + "a" * 64 + chr(10),                                       # seq=0:曾经把下标读成 rows[-1]
    "2 " + "a" * 64 + chr(10) + "1 " + "b" * 64 + chr(10),           # 不递增
    "1 " + "a" * 64 + chr(10) + "1 " + "a" * 64 + chr(10),           # 重复
])
def test_an_anchor_whose_sequence_is_not_strictly_increasing_from_one_fails_closed(tmp_path, content):
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    ladder.anchor.write_text(content, encoding="utf-8")
    with pytest.raises(LadderError, match="锚点"):
        ladder.status()


def test_a_torn_anchor_tail_is_ignored_and_trimmed_on_the_next_write(tmp_path):
    """锚点末行崩在追加中途:梯子照常可用,下次写入前把那半行截掉。

    注意这里的构造顺序:日志那一行**先 fsync**,锚点行后写,所以真实的撕裂场景里
    日志里已经有那一条,锚点碎片只是那一次写入的前缀。"""
    ladder = anchored(tmp_path)
    for _ in range(4):
        ladder.submit(0.0)
    lines = ladder.anchor.read_text(encoding="utf-8").splitlines()
    ladder.anchor.write_text("\n".join(lines[:-1] + [lines[-1][:8]]), encoding="utf-8")   # 崩在哈希中途
    assert ladder.status()["queries_used"] == 4
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 5
    assert all(len(l.split(" ")[1]) == 64 for l in ladder.anchor.read_text(encoding="utf-8").splitlines())


def test_a_torn_anchor_tail_whose_hash_prefix_disagrees_with_the_log_is_detected(tmp_path):
    """锚点碎片里的哈希前缀与日志那一条对不上 → 那一条被改过,fail-closed。

    这是"用锚点见证范围"的全部价值所在:第 19 轮曾用"日志自己的哈希"补范围,
    那是拿日志证明日志——补大之后锚点真正的见证从未被核对,反而留了一条绕过路径。"""
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    lines = ladder.anchor.read_text(encoding="utf-8").splitlines()
    seq = lines[-1].split(" ")[0]
    ladder.anchor.write_text("\n".join(lines[:-1] + [seq + " ffffffffffff"]), encoding="utf-8")

    with pytest.raises(LadderError, match="对不上"):
        ladder.status()


def test_an_anchor_naming_a_record_the_log_does_not_have_is_corruption(tmp_path):
    """锚点提到了日志里没有的那一条 → 日志被截断或回滚,fail-closed。

    日志那一条先于锚点行 fsync,所以这种状态在正常运行下不可能出现。"""
    ladder = anchored(tmp_path)
    for _ in range(2):
        ladder.submit(0.0)
    with open(ladder.anchor, "a", encoding="utf-8") as stream:
        stream.write("3 deadbeef")                                    # 锚点说第 3 条在,日志里没有
    with pytest.raises(LadderError, match="不存在"):
        ladder.status()


def test_a_torn_log_tail_is_ignored_and_trimmed_on_the_next_write(tmp_path):
    ladder = anchored(tmp_path)
    for _ in range(2):
        ladder.submit(0.0)
    with open(ladder.log_path, "a", encoding="utf-8") as stream:
        stream.write('{"seq":3,"eve')
    assert ladder.status()["queries_used"] == 2
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 3


def test_log_and_head_deleted_while_the_anchor_remains_is_recoverable_only_by_an_explicit_reset(tmp_path):
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    ladder.log_path.unlink()
    ladder.path.unlink()
    with pytest.raises(LadderError, match="reset"):
        ladder.status()                                               # 报错里给出出路
    out = ladder.reset("清理结果目录时只带走了一半文件")   # 已经坏了:不需要 force
    assert out["archived"] and ladder.status()["queries_used"] == 0
    assert (ladder.path.parent / "ladder.reset.log").exists()


def test_reset_archives_everything_and_keeps_the_history_and_never_overwrites_an_archive(tmp_path):
    ladder = anchored(tmp_path)
    ladder.submit(5.5)
    first = ladder.reset("第一次", force=True)
    ladder.submit(0.0)
    second = ladder.reset("第二次", force=True)
    assert first["archive_number"] == 1 and second["archive_number"] == 2
    assert any(p.name.endswith(".bak-1") for p in ladder.path.parent.iterdir())
    assert ladder.status()["reported"] == 0.0


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201, None])
def test_reset_needs_a_real_reason_and_touches_nothing_without_it(tmp_path, reason):
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    with pytest.raises(LadderError):
        ladder.reset(reason)
    assert ladder.status()["queries_used"] == 1


def test_force_renew_after_exhaustion_is_not_labelled_forced(tmp_path):
    ladder = Ladder(tmp_path / "l.json", budget=2)
    ladder.submit(0.0)
    ladder.submit(0.0)
    ladder.renew("用尽后续期", force=True)
    assert "[强制]" not in ladder.log_path.read_text(encoding="utf-8")
    ladder.renew("提前续期", force=True)
    assert "[强制] 提前续期" in ladder.log_path.read_text(encoding="utf-8")


# ---------- 第 19 轮 codex ----------

def test_two_processes_cannot_interleave_a_reset_and_a_submit(tmp_path):
    """第 19 轮 codex 的 M2:**跨进程**实测,不是同一进程里模拟。

    没有跨进程锁时:CLI 的 `reset` 把日志挪走,与网关那次已经读到旧状态的 `submit` 交错,
    submit 用陈旧的 rows 算出 seq=N+1 写进空日志,之后每次读都报"seq 不连续"——梯子永久报废。
    这里起两个真进程:一个反复 submit,另一个反复 reset,最后梯子必须仍然可用
    (能读出状态、能继续提交),不能出现断链。

    没有这把锁时本用例会变红:submit 会把 seq=N+1 的记录写进刚被挪空的日志。"""
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    repo = str(Path(__file__).resolve().parents[1])
    script = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from risk.ladder import Ladder, LadderError
        mode, path, anchor = sys.argv[1], sys.argv[2], sys.argv[3]
        ladder = Ladder(Path(path), anchor=Path(anchor))
        deadline = time.time() + 4.0
        ok = 0
        while time.time() < deadline:
            try:
                if mode == "submit":
                    ladder.submit(0.0)
                else:
                    ladder.reset("并发演练", force=True)
                ok += 1
            except LadderError:
                pass
            except Exception:
                pass
        print(ok)
    """)

    script_path = tmp_path / "worker.py"
    script_path.write_text(script, encoding="utf-8")

    ladder = anchored(tmp_path)
    for _ in range(2):
        ladder.submit(0.0)
    env = dict(os.environ)
    env["PYTHONPATH"] = repo
    env["PYTHONIOENCODING"] = "utf-8"
    procs = [subprocess.Popen([sys.executable, str(script_path), mode, str(ladder.path), str(ladder.anchor)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for mode in ("submit", "reset")]
    for proc in procs:
        proc.wait(timeout=180)

    # 梯子必须仍然可用:能读出状态,还能继续提交
    assert ladder.status()["queries_used"] >= 0
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] >= 1

def test_a_complete_last_line_that_only_lacks_its_newline_is_kept_and_the_ladder_stays_usable(tmp_path):
    """第 19 轮 codex 的 M1(我上轮引入的回归):"完整但缺换行"不是撕裂。

    上轮把"文件不以换行结尾"一律当成撕裂,于是短写只差一个换行时整行被丢掉——
    而头指针与锚点已经指向那一行,梯子从此**永久报废**(第 17 轮同一场景是好的)。"""
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    text = ladder.log_path.read_text(encoding="utf-8")
    ladder.log_path.write_text(text.rstrip(chr(10)), encoding="utf-8")        # 只少一个换行
    assert ladder.status()["queries_used"] == 3                                # 梯子仍然可用
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 4
    assert ladder.log_path.read_text(encoding="utf-8").endswith(chr(10))


def test_a_torn_log_tail_is_still_dropped(tmp_path):
    """撕裂(崩在追加中途)照旧丢弃——与"完整但缺换行"分开。"""
    ladder = anchored(tmp_path)
    for _ in range(2):
        ladder.submit(0.0)
    with open(ladder.log_path, "a", encoding="utf-8") as stream:
        stream.write('{"seq":3,"eve')
    assert ladder.status()["queries_used"] == 2
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 3


def test_a_complete_anchor_line_that_only_lacks_its_newline_is_kept(tmp_path):
    """第 19 轮 codex 的 L4:锚点末行完整只是缺换行时不能被丢掉。"""
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    text = ladder.anchor.read_text(encoding="utf-8")
    ladder.anchor.write_text(text.rstrip(chr(10)), encoding="utf-8")
    assert ladder.status()["queries_used"] == 3
    ladder.submit(0.0)
    assert ladder.status()["queries_used"] == 4


def test_a_reset_that_races_with_a_submit_cannot_corrupt_the_chain(tmp_path):
    """第 19 轮 codex 的 M2:reset 与网关并发时,不得写出断链的记录。

    没有这层防护时:submit 读到 3 行 → 人工 reset 把日志挪走 → submit 用陈旧的 rows
    算出 seq=4 写进空日志 → 之后每次读都报"seq 不连续",**梯子永久报废**。
    """
    ladder = anchored(tmp_path)
    for _ in range(3):
        ladder.submit(0.0)
    stale_rows = ladder._load(repair=False)
    ladder.reset("并发演练", force=True)
    with pytest.raises(LadderError, match="期间"):
        ladder._append(stale_rows, "submit", run_id="x", queries=4, best=0.0, note=None)
    assert ladder.status()["queries_used"] == 0                              # 梯子没有被写坏


def test_reset_leaves_an_audit_intent_line_before_it_moves_anything(tmp_path):
    """第 19 轮 codex 的 M3:审计必须在搬文件**之前**落盘,中途失败也不该零审计。"""
    ladder = anchored(tmp_path)
    ladder.submit(5.5)
    ladder.reset("第一次", force=True)
    lines = [json.loads(x) for x in (ladder.path.parent / "ladder.reset.log").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["event"] == "reset-intent"
    assert lines[0]["discarded_best"] == 5.5                                  # 被丢弃的 best 也留痕
    assert lines[0]["reason"] == "第一次"


def test_reset_records_the_run_ids_it_orphans(tmp_path):
    """第 19 轮 codex 的 H1/L2:reset 之后这些 run 永远读不出来,必须能查。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0, run_id="run-a")
    ladder.submit(0.0, run_id="run-b")
    out = ladder.reset("清空", force=True)
    assert out["orphaned_count"] == 2
    assert set(out["orphaned_run_ids"]) == {"run-a", "run-b"}
    assert set(ladder.orphaned_run_ids()) == {"run-a", "run-b"}


def test_reset_refuses_a_healthy_ladder_unless_forced(tmp_path):
    """第 19 轮 codex 的 L1:比 renew 更重的 reset 不能没有护栏。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    with pytest.raises(LadderError, match="force"):
        ladder.reset("手滑")
    assert ladder.status()["queries_used"] == 1                                # 什么都没动


def test_a_refused_reset_leaves_a_trace(tmp_path):
    """第 19 轮 codex 的 MEDIUM:有人试图重置一件状态正常的梯子,这本身就是"动过手"的证据。

    不记的话,事后只看归档文件根本看不出有人试过——被拒绝的尝试与从未发生过无法区分。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0)
    with pytest.raises(LadderError, match="force"):
        ladder.reset("手滑")
    log = ladder.path.parent / "ladder.reset.log"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["event"] == "reset-refused"
    assert lines[0]["reason"] == "手滑"
    assert not any(p.name.startswith("ladder.json.bak") for p in tmp_path.iterdir())   # 确实什么都没挪


def test_a_reset_whose_audit_cannot_be_written_stops_before_it_moves_anything(tmp_path):
    """第 19 轮 codex 的 MEDIUM:意图行写不进去就**不许动东西**。

    "审计先落盘"是这套排序的全部意义:意图行静默丢失之后照样挪文件,
    事后只剩归档文件、看不出谁动过手,等于名存实亡。

    这里走**真实**路径:把 `ladder.reset.log` 做成一个目录,让审计追加真的写不进去,
    于是 `_append_reset_log` 拿到的是真 OSError——正好验证 `required=True` 的 fail-closed 接线。"""
    ladder = anchored(tmp_path)
    reported = ladder.submit(3.0)["reported"]
    (ladder.path.parent / "ladder.reset.log").mkdir()          # 审计路径不可写(目录占了同名路径)

    with pytest.raises(LadderError, match="fail-closed"):
        ladder.reset("手滑", force=True)

    # 梯子原封不动:状态还在,归档文件一个都没多
    assert ladder.status()["queries_used"] == 1
    assert ladder.status()["reported"] == reported
    assert not any(".bak-" in p.name for p in tmp_path.iterdir())


def test_reset_of_a_damaged_ladder_needs_no_force(tmp_path):
    """已经坏了的时候 reset 正是出路,不能被护栏挡住。"""
    ladder = anchored(tmp_path)
    for _ in range(2):
        ladder.submit(0.0)
    ladder.log_path.unlink()
    ladder.path.unlink()
    out = ladder.reset("修砖")
    assert out["archived"]
    assert ladder.status()["queries_used"] == 0


def test_reset_archives_the_reveal_store_so_no_dead_archives_are_left(tmp_path):
    """第 19 轮 codex 的 L2:揭示存档要么一起归档,要么留下永远失效的死档。"""
    ladder = anchored(tmp_path)
    outward = tmp_path / "outward"
    outward.mkdir()
    (outward / "run-a.json").write_text("{}", encoding="utf-8")
    ladder.submit(0.0, run_id="run-a")
    ladder.reset("清空", force=True, outward=outward)
    assert not (outward / "run-a.json").exists()
    assert any(p.name.startswith("outward.bak-") for p in tmp_path.iterdir())


# ---------- 第 20 轮 codex ----------

def test_a_lock_file_whose_byte_cannot_be_written_fails_closed_as_a_ladder_error(tmp_path, monkeypatch):
    """第 20 轮 codex 的 L2:锁文件那一个字节写不下去时,必须报"梯子不可用"。

    此前是裸调 `stream.write`,OSError 一路冒到网关,变成"内部错误"——
    运维看不出是梯子的锁坏了,更看不出系统因此 fail-closed(什么都没做)。"""
    import risk.ladder as ladder_mod
    real_open = open

    class BadLockStream:
        def write(self, _data):
            raise OSError("磁盘满了")
        def flush(self):
            pass
        def seek(self, *_a):
            return 0
        def tell(self):
            return 0
        def fileno(self):
            return 0
        def close(self):
            pass

    def fake_open(file, mode="r", *args, **kwargs):
        if "a+b" in mode and str(file).endswith(".lock"):
            return BadLockStream()
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(ladder_mod, "open", fake_open, raising=False)
    ladder = Ladder(tmp_path / "ladder.json")
    with pytest.raises(LadderError, match="锁文件"):
        ladder.submit(1.10)
    assert not (tmp_path / "ladder.json").exists()          # fail-closed:什么都没做


def test_an_anchor_line_ending_with_a_carriage_return_keeps_its_witness(tmp_path):
    """第 20 轮 codex 的 L3:撕裂判定按字节找最后一个换行符,而锚点行的解析不容忍行尾的回车。

    两者口径不一致时,一条**完整**的见证会被当成撕裂截掉——锚点从此不再见证最新那条记录,
    而外部锚点存在的全部意义就是那一条比对。"""
    ladder = anchored(tmp_path)
    ladder.submit(1.10)
    ladder.submit(2.20)
    raw = ladder.anchor.read_bytes()
    ladder.anchor.write_bytes(raw.rstrip(b"\n") + b"\r")   # 末行的换行变成回车(别的工具写的 CRLF/半截)
    ladder.submit(3.30)

    seqs = [int(line.split(" ")[0]) for line in ladder.anchor.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert seqs == [1, 2, 3], f"锚点的见证序号必须连续,不能有缺口:{seqs}"
    assert ladder.status()["reported"] == 3.30


def test_the_log_and_the_anchor_agree_on_how_many_records_there_are(tmp_path):
    """第 20 轮 codex 的 L3:读日志与读锚点必须用同一套行切分规则。

    否则"读"看到的行数与"按字节截尾"看到的行数不一致,撕裂判定就会误伤完整的末行。"""
    ladder = anchored(tmp_path)
    ladder.submit(1.10)
    raw = ladder.log_path.read_bytes()
    ladder.log_path.write_bytes(raw.rstrip(b"\n") + b"\r")
    ladder.submit(2.20)

    rows = log_rows(ladder)
    assert len(rows) == 2, f"日志里应当有两条完整记录,实际 {len(rows)}"
    assert rows[-1]["best"] == 2.20
    assert ladder.status()["queries_used"] == 2


def test_reset_records_every_orphaned_run_and_the_count_it_returns_matches(tmp_path):
    """第 20 轮 codex 的 M4:被孤立的 run 名单此前只记前 200 个,返回值却报总数。

    于是"这次重置孤立了哪些 run"变得不可信:名单说没有,总数说有。"""
    ladder = anchored(tmp_path)
    total = 250
    done = 0
    while done < total:
        try:
            ladder.submit(0.0, run_id=f"run-{done:03d}")
        except LadderExhausted:
            ladder.renew("测试用续期", force=True)
            continue                          # 预算用尽时那次提交**没有**发生,重试同一个 run_id
        done += 1

    out = ladder.reset("清空", force=True)

    assert out["orphaned_count"] == total                        # 返回值 = 落盘内容
    assert len(out["orphaned_run_ids"]) == total
    assert len(ladder.orphaned_run_ids()) == total
    assert set(ladder.orphaned_run_ids()) == {f"run-{i:03d}" for i in range(total)}
    line = [json.loads(x) for x in (ladder.path.parent / "ladder.reset.log")
            .read_text(encoding="utf-8").splitlines() if x.strip()][0]
    assert line["orphaned_count"] == total and len(line["orphaned_run_ids"]) == total


def test_the_reset_return_value_mirrors_the_audit_intent_line_key_for_key(tmp_path):
    """第 20 轮自查更正:返回值与审计意图行**逐键一致**(键名、类型、值都一样)。

    此前返回值里写的是 `"orphaned_run_ids": len(...)`——同一个键名在返回值里是**个数**、
    在审计行里是**名单**,两边对不上:"这次重置孤立了哪些 run"于是有两个互相矛盾的说法,
    而"返回值与落盘内容一致"这句话当时并不成立。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0, run_id="run-a")
    ladder.submit(0.0, run_id="run-b")

    out = ladder.reset("清空", force=True)
    line = [json.loads(x) for x in (ladder.path.parent / "ladder.reset.log")
            .read_text(encoding="utf-8").splitlines() if x.strip()][0]

    assert line["event"] == "reset-intent"
    for key in ("discarded_best", "discarded_queries", "orphaned_count", "orphaned_run_ids"):
        assert key in out, f"返回值少了 {key}"
        assert key in line, f"审计行少了 {key}"
        assert out[key] == line[key], f"{key}: 返回值 {out[key]!r} 与审计行 {line[key]!r} 对不上"
        assert type(out[key]) is type(line[key]), f"{key}: 两边类型不一致"


def test_a_snapshot_taken_before_a_reset_does_not_replay_the_pre_reset_best(tmp_path):
    """第 20 轮 codex 的 M1:快照会过期。

    过期快照里留着重置前的 best 与查询数:拿它回放就是把已经作废的成绩当成现在的话报出去(泄漏),
    而且 `queries_left` 是旧账。"""
    ladder = anchored(tmp_path)
    ladder.submit(2.20, run_id="run-a")
    snap = ladder.snapshot()
    assert snap.preview(0.0, run_id="run-a")["reported"] == 2.20

    ladder.reset("人工重置", force=True)

    assert snap.has("run-a") is False                            # 快照必须发现自己过期了
    assert snap.preview(0.0, run_id="run-a")["reported"] == 0.0   # 不再回放重置前的成绩


def test_a_snapshot_reports_the_current_remaining_queries_after_a_new_submit(tmp_path):
    """第 20 轮 codex 的 M1:快照过期不只影响 best,`queries_left` 也是旧账。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0, run_id="run-a")
    snap = ladder.snapshot()
    assert snap.preview(0.0)["queries_left"] == 58                # 快照里只记着一次提交
    ladder.submit(0.0, run_id="run-b")
    assert snap.preview(0.0)["queries_left"] == 57                # 现在的账:两次提交


def test_a_snapshot_picks_up_a_run_submitted_after_it_was_taken(tmp_path):
    """第 20 轮 codex 的 M1:快照的"变更令牌"要能看出别人提交过。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0, run_id="run-a")
    snap = ladder.snapshot()
    assert snap.has("run-b") is False

    ladder.submit(0.0, run_id="run-b")

    assert snap.has("run-b") is True


def test_a_snapshot_answers_has_and_preview_without_rereading_the_log(tmp_path):
    """第 19 轮 codex 的 L3:快照只读一次,之后走内存。"""
    ladder = anchored(tmp_path)
    ladder.submit(0.0, run_id="run-a")
    snap = ladder.snapshot()
    assert snap.has("run-a") and not snap.has("run-b")
    assert snap.preview(0.0, run_id="run-a")["replay"] is True
    assert ladder.status()["queries_used"] == 1                                # 快照没有消耗查询
