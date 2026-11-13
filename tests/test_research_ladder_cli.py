"""梯子的人工入口:status 只读、renew 显式留痕;手工编辑状态文件会被当成篡改。"""
import json

import pytest

import research_api as api
import research_ladder as cli
from risk.ladder import Ladder


@pytest.fixture
def paths(tmp_path, monkeypatch):
    p = {"results_dir": tmp_path / "data" / "research"}
    monkeypatch.setattr(api, "settings", lambda: ({}, p))
    return p


def run(*argv):
    return cli.main(list(argv))


def test_status_is_read_only_and_works_on_a_fresh_ladder(paths, capsys):
    assert run("status") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queries_used"] == 0 and out["reported"] == 0.0
    assert not (paths["results_dir"] / "ladder").exists()                  # 只读:没有凭空造出状态文件


def test_renew_keeps_best_resets_queries_and_leaves_an_audit_record(paths, capsys):
    ladder = cli.ladder_for(paths)
    ladder.submit(5.5)
    ladder.submit(0.0)
    assert run("renew", "--reason", "第 1 批候选已复核") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reported"] == 5.5 and out["queries_left"] == 60
    log = (paths["results_dir"] / "ladder" / "ladder.jsonl").read_text(encoding="utf-8")
    assert "第 1 批候选已复核" in log and '"event":"renew"' in log


def test_renew_without_a_reason_is_refused(paths):
    with pytest.raises(SystemExit):
        run("renew")


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_is_refused_and_nothing_is_written(paths, capsys, reason):
    cli.ladder_for(paths).submit(1.0)
    before = (paths["results_dir"] / "ladder" / "ladder.jsonl").read_text(encoding="utf-8")
    assert run("renew", "--reason", reason) == 1
    assert (paths["results_dir"] / "ladder" / "ladder.jsonl").read_text(encoding="utf-8") == before
    assert "拒绝" in capsys.readouterr().err


def test_a_hand_edited_state_is_refused_so_renewal_cannot_be_done_by_editing_a_file(paths, capsys):
    ladder = cli.ladder_for(paths)
    ladder.submit(5.5)
    head = paths["results_dir"] / "ladder" / "ladder.json"
    state = json.loads(head.read_text(encoding="utf-8"))
    state["queries"] = 0                                                   # 手工"续期"
    head.write_text(json.dumps(state), encoding="utf-8")
    assert run("status") == 1 and "拒绝" in capsys.readouterr().err
    assert run("renew", "--reason", "想靠改文件续期") == 1                  # 也不能在被篡改的状态上继续


def test_the_gateway_has_no_route_that_can_renew():
    import research_gateway as gw
    assert not any("ladder" in path or "renew" in path for _, path in gw.ROUTES)
