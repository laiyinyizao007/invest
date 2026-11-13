"""梯子式量化:纯逻辑 + tmp_path,不依赖 vn.py。规则见 risk/ladder.py 与 ADR「阶段 3 修订」。"""
import json
import math

import pytest

from risk.ladder import (DEFAULT_QUERY_BUDGET, LADDER_ETA, Ladder, LadderError, LadderExhausted, round_to_step)


@pytest.fixture
def ladder(tmp_path):
    return Ladder(tmp_path / "ladder.json")


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
    out = ladder.submit(1.37)                                    # 1.37 / 1.10 = 1.245 → 1 个台阶
    assert out["reported"] == 1.10
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


def test_status_does_not_consume_a_query(ladder):
    ladder.submit(1.2)
    for _ in range(10):
        ladder.status()
    assert ladder.status()["queries_used"] == 1


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "l.json"
    Ladder(path).submit(2.5)
    again = Ladder(path)
    assert again.status()["reported"] == 2.20 and again.status()["queries_used"] == 1
    assert again.submit(0.1)["reported"] == 2.20                 # 重启不会让梯子从头开始


@pytest.mark.parametrize("content", [
    "not json", "[]", "{}", '{"version": 1}',
    json.dumps({"version": 2, "eta": 1.1, "budget": 60, "queries": 0, "best": 0.0}),
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": -1, "best": 0.0}),
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": 61, "best": 0.0}),
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": 0, "best": -1.1}),
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": 0, "best": 1.0}),         # 不是 η 的倍数
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": True, "best": 0.0}),
    json.dumps({"version": 1, "eta": 1.1, "budget": 60, "queries": 0, "best": 0.0, "x": 1}),  # 多出的字段
])
def test_a_corrupted_state_fails_closed_and_is_not_silently_reset(tmp_path, content):
    path = tmp_path / "l.json"
    path.write_text(content, encoding="utf-8")
    ladder = Ladder(path)
    with pytest.raises(LadderError):
        ladder.submit(5.0)
    with pytest.raises(LadderError):
        ladder.status()
    assert path.read_text(encoding="utf-8") == content           # 损坏的文件原样留着,不被覆盖成"全新"


def test_a_state_file_cannot_loosen_the_registered_step(tmp_path):
    """状态文件里写个更小的 η 想让梯子更爱更新——必须被当成损坏。"""
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"version": 1, "eta": 0.1, "budget": 60, "queries": 0, "best": 0.0}), encoding="utf-8")
    with pytest.raises(LadderError):
        Ladder(path).submit(5.0)


def test_a_state_file_cannot_raise_the_registered_budget(tmp_path):
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"version": 1, "eta": 1.1, "budget": 10_000, "queries": 0, "best": 0.0}), encoding="utf-8")
    with pytest.raises(LadderError):
        Ladder(path).submit(5.0)


def test_a_state_file_may_tighten_the_budget(tmp_path):
    """人工可以把预算调得更紧(只能更严,与 DSR 的 V 同一思路)。"""
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"version": 1, "eta": 1.1, "budget": 2, "queries": 2, "best": 0.0}), encoding="utf-8")
    with pytest.raises(LadderExhausted):
        Ladder(path).submit(5.0)


@pytest.mark.parametrize("eta", [0.1, 0.25, 1.0, 1.0999, 2.0, float("nan"), True])
def test_the_step_cannot_be_overridden_in_code(tmp_path, eta):
    with pytest.raises(LadderError):
        Ladder(tmp_path / "l.json", eta=eta)


@pytest.mark.parametrize("budget", [0, -1, DEFAULT_QUERY_BUDGET + 1, 10**6, True, 1.5])
def test_the_budget_cannot_exceed_the_registered_ceiling(tmp_path, budget):
    with pytest.raises(LadderError):
        Ladder(tmp_path / "l.json", budget=budget)


def test_a_failed_write_fails_closed_and_releases_nothing(tmp_path, monkeypatch):
    """写不进状态就不能放出汇报值:否则一次失败的写入就等于一次免费的查询。"""
    ladder = Ladder(tmp_path / "l.json")
    ladder.submit(0.0)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("risk.ladder.os.replace", boom)
    with pytest.raises(LadderError):
        ladder.submit(9.0)
    monkeypatch.undo()
    assert ladder.status()["queries_used"] == 1 and ladder.status()["reported"] == 0.0


def test_the_state_file_never_contains_non_finite_numbers(ladder):
    ladder.submit(float("nan"))
    text = ladder.path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)
