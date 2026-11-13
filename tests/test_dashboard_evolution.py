"""进化引擎读取侧:台账校验、曲线聚合与提示标志、热力图切片、代码与差异、路径安全。"""
import json
import os
import time
from pathlib import Path

import pytest

from dashboard import app as appmod
from dashboard import config, evolution as ev

NOW = 1_800_000_000.0
CAMPAIGN = "camp-2026-11"


def settings(tmp_path):
    return config.load(path=tmp_path / "none.json", root=tmp_path)


def ledger(tmp_path, rows, campaign=CAMPAIGN):
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{campaign}.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return p


def row(gen=1, sha="a" * 64, **over):
    r = {"campaign_id": CAMPAIGN, "generation": gen, "candidate_sha256": sha, "parent_sha256": None,
         "run_id": "20261112-210000-000000", "status": "evaluated", "sharpe": 1.0, "total_return": 0.12,
         "max_drawdown": 0.05, "dsr": 0.9, "n_trials": 3, "fail_reason": None, "reflection": "反思文本",
         "created_at": "2026-11-12T21:00:00", "cell": "fast-mid", "diagnosis": {"weakest": "drawdown"}}
    r.update(over)
    return r


# ---------------------------------------------------------------- 台账
def test_a_missing_campaign_is_empty_not_an_error(tmp_path):
    assert ev.read_campaign(tmp_path / "data" / "research" / "evolution", "nope") == []


def test_records_are_sorted_by_generation(tmp_path):
    ledger(tmp_path, [row(gen=3, sha="c" * 64), row(gen=1, sha="a" * 64), row(gen=2, sha="b" * 64)])
    records = ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN)
    assert [r.generation for r in records] == [1, 2, 3]


@pytest.mark.parametrize("bad", [
    {"generation": -1}, {"generation": "1"}, {"generation": True}, {"candidate_sha256": "abc"},
    {"candidate_sha256": "A" * 64}, {"n_trials": -1}, {"n_trials": "3"},
])
def test_a_malformed_record_breaks_the_ledger_instead_of_being_shown(tmp_path, bad):
    ledger(tmp_path, [row(**bad)])
    with pytest.raises(ev.EvolutionError):
        ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN)


def test_a_ledger_with_invalid_utf8_is_broken(tmp_path):
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / f"{CAMPAIGN}.jsonl").write_bytes(b'{"generation": 1}\n\xff\xfe\n')
    with pytest.raises(ev.EvolutionError):
        ev.read_campaign(d, CAMPAIGN)


def test_a_ledger_with_a_broken_json_line_is_broken(tmp_path):
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / f"{CAMPAIGN}.jsonl").write_text('{"generation": 1}\n{broken\n', encoding="utf-8")
    with pytest.raises(ev.EvolutionError):
        ev.read_campaign(d, CAMPAIGN)


def test_campaign_names_are_listed_and_odd_files_are_skipped(tmp_path):
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp-a.jsonl").write_text("", encoding="utf-8")
    (d / "notes.txt").write_text("", encoding="utf-8")
    (d / "bad name.jsonl").write_text("", encoding="utf-8")
    assert ev.campaigns(d) == ["camp-a"]


# ---------------------------------------------------------------- 曲线
def _curve(records, **kw):
    args = {"stagnant_generations": 5, "stagnant_epsilon": 0.05, "overfit_mean_gap": 1.0}
    args.update(kw)
    return ev.curve(records, **args)


def test_the_curve_gives_elite_mean_and_quartiles_per_generation(tmp_path):
    ledger(tmp_path, [row(gen=1, sha="a" * 64, sharpe=0.5), row(gen=1, sha="b" * 64, sharpe=1.5),
                       row(gen=1, sha="c" * 64, sharpe=1.0)])
    c = _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN))
    point = c["points"][0]
    assert point["generation"] == 1 and point["count"] == 3
    assert point["elite_sharpe"] == 1.5 and point["mean_sharpe"] == 1.0
    assert point["p25_sharpe"] <= point["mean_sharpe"] <= point["p75_sharpe"]
    assert c["generations"] == 1 and c["latest_generation"] == 1


def test_a_generation_without_sharpe_values_is_still_a_point(tmp_path):
    ledger(tmp_path, [row(gen=1, sha="a" * 64, sharpe=None)])
    c = _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN))
    assert c["points"][0]["elite_sharpe"] is None and c["points"][0]["count"] == 1


def test_stagnation_is_flagged_only_after_enough_generations(tmp_path):
    rows = [row(gen=g, sha=f"{g:064x}") for g in range(1, 5)]
    ledger(tmp_path, rows)
    assert not [f for f in _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN))["flags"]
                if f["kind"] == "stagnant"]
    rows.append(row(gen=5, sha="f" * 64))
    ledger(tmp_path, rows)
    flags = [f for f in _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN))["flags"]
             if f["kind"] == "stagnant"]
    assert flags and "没有提升" in flags[0]["text"]


def test_an_overfit_suspect_flag_is_a_hint_with_a_gap_threshold(tmp_path):
    ledger(tmp_path, [row(gen=1, sha="a" * 64, sharpe=3.0), row(gen=1, sha="b" * 64, sharpe=0.5)])
    flags = [f for f in _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN))["flags"]
             if f["kind"] == "overfit_suspect"]
    assert flags and flags[0]["level"] == "warn" and "热力图" in flags[0]["text"]
    assert not [f for f in _curve(ev.read_campaign(tmp_path / "data" / "research" / "evolution", CAMPAIGN),
                                  overfit_mean_gap=5.0)["flags"] if f["kind"] == "overfit_suspect"]


# ---------------------------------------------------------------- 热力图
def heatmap(tmp_path, run_id="20261112-210000-000000", body=None):
    d = tmp_path / "data" / "research" / "heatmaps"
    d.mkdir(parents=True, exist_ok=True)
    payload = body if body is not None else {
        "axes": {"a": [1.0, 2.0], "b": [10, 20], "c": [0.5]},
        "scores": [[0, 0, 0, 1.2], [1, 0, 0, 0.9], [0, 1, 0, "-inf"], [1, 1, 0, 0.4]],
        "best_cell": [0, 0, 0], "on_plateau": False, "window": ["2020-07-22", "2022-06-30"], "source": "selection",
    }
    (d / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return d


def test_a_missing_heatmap_is_none(tmp_path):
    assert ev.read_heatmap(tmp_path / "data" / "research" / "heatmaps", "nope") is None


def test_the_heatmap_is_cut_to_two_dimensions_and_invalid_cells_stay_none(tmp_path):
    d = heatmap(tmp_path)
    h = ev.read_heatmap(d, "20261112-210000-000000")
    assert h["axes"]["x"]["name"] == "a" and h["axes"]["y"]["name"] == "b"
    assert h["grid"][0][0] == 1.2 and h["grid"][1][1] == 0.4
    assert h["grid"][1][0] is None                       # "-inf" 显式为空,不画成 0
    assert h["valid_cells"] == 3 and h["n_cells"] == 4
    assert h["on_plateau"] is False
    assert "选参窗口" in (h["note"] or "")


@pytest.mark.parametrize("body", [
    {"axes": {"a": [1.0]}, "scores": []},                                        # 轴不是三个
    {"axes": {"a": [1.0], "b": [2.0], "c": [3.0]}, "scores": "x"},
    {"axes": {"a": [], "b": [2.0], "c": [3.0]}, "scores": []},
    {"axes": {"a": [1.0], "b": [2.0], "c": [3.0]}, "scores": [[0, 0, 0]]},         # 得分表形状不对
    {"axes": {"a": [1.0], "b": [2.0], "c": [3.0]}, "scores": [[0, 0, 0, "x"]]},    # 得分非法
    {"axes": {"a": [1.0], "b": [2.0], "c": [3.0]}, "scores": [[0, 0, 0, float("nan")]]},
])
def test_a_malformed_heatmap_is_rejected(tmp_path, body):
    d = heatmap(tmp_path, body=body)
    with pytest.raises(ev.EvolutionError):
        ev.read_heatmap(d, "20261112-210000-000000")


def test_a_heatmap_with_invalid_utf8_is_rejected(tmp_path):
    d = tmp_path / "data" / "research" / "heatmaps"
    d.mkdir(parents=True)
    (d / "20261112-210000-000000.json").write_bytes(b'{"axes": "\xff\xfe"}')
    with pytest.raises(ev.EvolutionError):
        ev.read_heatmap(d, "20261112-210000-000000")


# ---------------------------------------------------------------- 代码与差异
def test_candidate_source_is_found_by_hash_not_by_name(tmp_path):
    d = tmp_path / "pending_strategies"
    d.mkdir()
    src = "class C: pass\n"
    import hashlib
    sha = hashlib.sha256(src.encode()).hexdigest()
    (d / "whatever.py").write_bytes(src.encode("utf-8"))      # 按字节写:Windows 的 write_text 会改写换行符
    assert ev.candidate_source(d, sha) == src
    assert ev.candidate_source(d, "b" * 64) is None
    assert ev.candidate_source(d, "not-a-hash") is None


def test_the_diff_is_unified_and_readable(tmp_path):
    out = ev.diff_text("a\nb\n", "a\nc\n")
    assert out.startswith("--- parent") and "+++ candidate" in out
    assert "-b" in out and "+c" in out


# ---------------------------------------------------------------- 路径安全
def test_a_campaign_file_that_is_a_symlink_out_of_the_directory_is_not_listed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.jsonl").write_text("", encoding="utf-8")
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    try:
        os.symlink(outside / "evil.jsonl", d / "camp.jsonl")
    except OSError:
        pytest.skip("无法创建符号链接")
    assert ev.campaigns(d) == []


# ---------------------------------------------------------------- 与契约示例对齐
def test_the_curve_the_ledger_and_the_heatmap_match_the_contract_examples(tmp_path):
    """collector 的输出必须与 docs/web/fixtures 的示例同形:前端按示例开发,真实响应不能另说一套。"""
    import json as _json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    web = Path(__file__).resolve().parents[1] / "docs" / "web"
    spec = _json.loads((web / "status-api.openapi.json").read_text(encoding="utf-8"))
    components = spec["components"]

    def validate(name, ref, payload):
        schema = {"$ref": ref, "components": components}
        errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=str)
        assert not errors, "; ".join(e.message for e in errors[:3])

    rows = [
        {"campaign_id": "camp-2026-11", "generation": 1, "candidate_sha256": "a" * 64, "parent_sha256": None,
         "run_id": "20261112-210000-000001", "status": "evaluated", "sharpe": 0.42, "total_return": 0.031,
         "max_drawdown": 0.19, "dsr": 0.31, "n_trials": 4, "fail_reason": None, "cell": "fast-mid",
         "diagnosis": {"weakest": "sharpe"}, "reflection": "反思文本", "created_at": "2026-11-12T21:00:00"},
        {"campaign_id": "camp-2026-11", "generation": 2, "candidate_sha256": "b" * 64, "parent_sha256": "a" * 64,
         "run_id": "20261112-210000-000002", "status": "evaluated", "sharpe": 0.78, "total_return": 0.068,
         "max_drawdown": 0.16, "dsr": 0.55, "n_trials": 4, "fail_reason": None, "cell": "fast-mid",
         "diagnosis": {"weakest": "drawdown"}, "reflection": "反思文本", "created_at": "2026-11-12T21:10:00"},
    ]
    ledger(tmp_path, rows)
    records = ev.read_campaign(tmp_path / "data" / "research" / "evolution", "camp-2026-11")
    curve_payload = {"campaign_id": "camp-2026-11", **_curve(records)}
    validate("curve", "#/components/schemas/EvolutionCurve", curve_payload)
    log_payload = {"campaign_id": "camp-2026-11",
                   "records": [{**r.brief(), "reflection": r.reflection, "created_at": r.created_at} for r in records],
                   "next_cursor": None}
    validate("log", "#/components/schemas/EvolutionLog", log_payload)

    d = heatmap(tmp_path)
    heat = ev.read_heatmap(d, "20261112-210000-000000")
    validate("heatmap", "#/components/schemas/Heatmap", heat)


def test_a_ledger_of_millions_of_empty_lines_is_rejected_without_building_a_huge_list(tmp_path):
    """8 MB 的空行文件:一次性 splitlines() 会产生八百万个列表元素。逐行扫描并在行数超限时立刻停止。"""
    import tracemalloc
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / f"{CAMPAIGN}.jsonl").write_bytes(b"\n" * (4 * 1024 * 1024))
    tracemalloc.start()
    try:
        with pytest.raises(ev.EvolutionError):
            ev.read_campaign(d, CAMPAIGN)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024, f"峰值内存 {peak // 1024 // 1024} MB"


# ---------------------------------------------------------------- 区块级别:损坏不能说成正常
def test_a_campaign_with_a_corrupt_ledger_is_not_reported_as_a_healthy_evolution_section(tmp_path):
    """台账损坏 = 无法判断这个战役的状态。整体级别不能是 ok(『不制造虚假安心』)。"""
    from dashboard import collectors as col
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp-bad.jsonl").write_text("{broken\n", encoding="utf-8")
    s = settings(tmp_path)
    r = col.collect_evolution(s, NOW)
    assert r.level != "ok" and r.reasons
    assert r.section["data"]["campaigns"][0]["state"] == "corrupt"
    assert "损坏" in r.reasons[0]["text"]


def test_an_empty_ledger_file_is_a_campaign_with_no_data_not_a_broken_one(tmp_path):
    from dashboard import collectors as col
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp-empty.jsonl").write_text("", encoding="utf-8")
    r = col.collect_evolution(settings(tmp_path), NOW)
    assert r.section["data"]["campaigns"][0]["state"] == "missing"


def test_a_corrupt_ledger_is_distinguishable_from_a_missing_campaign(tmp_path):
    """『战役不存在』与『台账坏了』必须能区分:前者是 404,后者是数据不可信。"""
    from dashboard import collectors as col
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp-bad.jsonl").write_text("{broken\n", encoding="utf-8")
    s = settings(tmp_path)
    with pytest.raises(col.EvolutionUnavailable):
        col.get_evolution_curve(s, "camp-bad")
    with pytest.raises(col.EvolutionUnavailable):
        col.get_evolution_log(s, "camp-bad")
    assert col.get_evolution_curve(s, "camp-missing") is None      # 不存在 → None(404)
    assert col.get_evolution_log(s, "camp-missing") is None


def test_a_heatmap_whose_cells_do_not_match_the_axis_lengths_is_rejected(tmp_path):
    """格子数与坐标轴长度对不上:网格会有空洞,前端画出来的热力图是错的(缺格显示成无效或错位)。"""
    d = heatmap(tmp_path, body={
        "axes": {"a": [1.0, 2.0], "b": [10, 20], "c": [0.5]},
        "scores": [[0, 0, 0, 1.2], [1, 0, 0, 0.9], [0, 1, 0, "-inf"], [1, 1, 0, 0.4]],
        "best_cell": [0, 0, 0],
    })
    # 故意把 b 轴写成三格而得分表只有两行
    d2 = heatmap(tmp_path, "20261112-210000-000001", body={
        "axes": {"a": [1.0, 2.0], "b": [10, 20, 30], "c": [0.5]},
        "scores": [[0, 0, 0, 1.2], [1, 0, 0, 0.9], [0, 1, 0, "-inf"], [1, 1, 0, 0.4]],
    })
    with pytest.raises(ev.EvolutionError):
        ev.read_heatmap(d2, "20261112-210000-000001")


def test_heatmap_cells_outside_the_declared_axis_ranges_are_rejected(tmp_path):
    d = heatmap(tmp_path, "20261112-210000-000002", body={
        "axes": {"a": [1.0, 2.0], "b": [10, 20], "c": [0.5]},
        "scores": [[0, 0, 0, 1.2], [5, 0, 0, 0.9]],
    })
    with pytest.raises(ev.EvolutionError):
        ev.read_heatmap(d, "20261112-210000-000002")


# ---------------------------------------------------------------- 路由(阶段④上线时)
def _client(settings, token="tok"):
    from fastapi.testclient import TestClient
    return TestClient(appmod.create_app(settings, token))


def test_the_evolution_routes_are_only_registered_when_the_phase_is_enabled(tmp_path):
    """与阶段②–⑤ 的既有约定一致:接口在契约里先存在,路由随阶段上线才注册。默认关闭时连路由都不存在。"""
    from dashboard import app as appmod
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow"}), encoding="utf-8")
    off = config.load(path=cfg, root=tmp_path)
    paths = {r.path for r in appmod.create_app(off, "tok").routes if hasattr(r, "path")}
    assert "/api/research/evolution" not in paths

    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    on = config.load(path=cfg, root=tmp_path)
    paths = {r.path for r in appmod.create_app(on, "tok").routes if hasattr(r, "path")}
    assert "/api/research/evolution" in paths and "/api/research/evolution/{campaign_id}/curve" in paths


def test_the_evolution_endpoints_need_the_token(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    s = config.load(path=cfg, root=tmp_path)
    c = _client(s)
    for path in ("/api/research/evolution", "/api/research/evolution/camp/curve",
                 "/api/research/evolution/camp/log", "/api/research/runs/20261112-210000-000000/heatmap"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"Authorization": "Bearer tok"}).status_code in (200, 404), path


def test_a_corrupt_campaign_returns_a_distinct_error_not_a_404(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    s = config.load(path=cfg, root=tmp_path)
    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp-bad.jsonl").write_text("{broken\n", encoding="utf-8")
    c = _client(s)
    r = c.get("/api/research/evolution/camp-bad/curve", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 503 and r.json()["code"] == "unavailable"
    assert c.get("/api/research/evolution/nope/curve", headers={"Authorization": "Bearer tok"}).status_code == 404


def test_meta_reports_the_evolution_capability(tmp_path):
    """前端按 capabilities 决定是否显示进化视图的入口。"""
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow"}), encoding="utf-8")
    off = _client(config.load(path=cfg, root=tmp_path))
    assert off.get("/api/meta", headers={"Authorization": "Bearer tok"}).json()["capabilities"]["evolution"] is False
    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    on = _client(config.load(path=cfg, root=tmp_path))
    assert on.get("/api/meta", headers={"Authorization": "Bearer tok"}).json()["capabilities"]["evolution"] is True


def test_the_campaign_list_matches_the_contract_field_by_field(tmp_path):
    """契约要求的每个字段都要给;少一个字段前端就会读到 undefined。"""
    import json as _json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    web = Path(__file__).resolve().parents[1] / "docs" / "web"
    spec = _json.loads((web / "status-api.openapi.json").read_text(encoding="utf-8"))
    components = spec["components"]

    def check(ref, payload):
        errors = sorted(Draft202012Validator({"$ref": ref, "components": components}).iter_errors(payload), key=str)
        assert not errors, "; ".join(e.message for e in errors[:3])

    ledger(tmp_path, [row(gen=1, sha="a" * 64)])
    from dashboard import collectors as col
    r = col.collect_evolution(settings(tmp_path), NOW)
    # 端点返回的是**裸数据**(state/level/message 在数据里),契约也这么写
    check("#/components/schemas/EvolutionList",
          {**(r.section["data"] or {}), "state": r.section["state"],
           "level": r.section["level"], "message": r.section["message"]})
    check("#/components/schemas/EvolutionCampaign", r.section["data"]["campaigns"][0])


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "a/b", "a\b", "camp.jsonl", "camp\n", "%2e%2e",
                                 "x" * 100, "camp-2026-11\x00"])
def test_a_campaign_id_that_tries_to_escape_is_refused(tmp_path, bad):
    """战役号会拼进文件路径:必须挡住路径穿越。"""
    from dashboard import collectors as col
    s = settings(tmp_path)
    assert col.get_evolution_curve(s, bad) is None
    assert col.get_evolution_log(s, bad) is None


@pytest.mark.parametrize("bad", ["../../secret", "a/b", "not-a-hash", "A" * 64, "x" * 64])
def test_a_sha_that_tries_to_escape_is_refused(tmp_path, bad):
    from dashboard import collectors as col
    s = settings(tmp_path)
    assert col.get_candidate_code(s, bad) is None
    assert col.get_candidate_diff(s, bad, "a" * 64) is None


def test_the_diff_endpoint_refuses_a_parent_that_is_not_a_hash(tmp_path):
    from dashboard import collectors as col
    s = settings(tmp_path)
    assert col.get_candidate_diff(s, "a" * 64, "../../secret") is None


def test_the_reflection_text_is_sanitized_before_it_leaves_the_process(tmp_path):
    """反思是**不可信的大模型文本**:它可能带路径、账号、令牌。出口必须净化(唯一的出口是 dashboard/safe.py)。"""
    from fastapi.testclient import TestClient

    from dashboard import app as appmod
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    s = config.load(path=cfg, root=tmp_path)
    ledger(tmp_path, [row(gen=1, sha="a" * 64, reflection="读取 " + chr(92) + "Users" + chr(92) + "Alice" + chr(92) + "secret.env 失败")])
    c = TestClient(appmod.create_app(s, "tok"))
    r = c.get("/api/research/evolution/camp-2026-11/log", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    body = r.text
    assert "Alice" not in body and "secret.env" not in body
    assert "<path>" in body


def test_the_candidate_directory_comes_from_the_sandbox_config_not_a_hardcoded_path(tmp_path):
    """沙盒把待审目录改到别处时,面板必须跟着改:写死路径会显示『源码已不在』。"""
    import hashlib
    from dashboard import collectors as col

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "research_sandbox.json").write_text(json.dumps({
        "iteration": {"max_iterations": 5, "window_hours": 24},
        "paths": {"pending_dir": "other_pending", "results_dir": "data/research",
                  "approvals_file": "data/research-control/approvals.json"}}), encoding="utf-8")
    other = tmp_path / "other_pending"
    other.mkdir()
    src = "class C: pass\n"
    sha = hashlib.sha256(src.encode()).hexdigest()
    (other / "x.py").write_bytes(src.encode())
    s = settings(tmp_path)
    out = col.get_candidate_code(s, sha)
    assert out is not None and out["found"] is True


def test_a_heatmap_file_that_is_a_symlink_out_of_the_directory_is_refused(tmp_path):
    """与研究结果文件同一口径:符号链接/junction 指向目录外的一律不读。"""
    import os

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"axes": {"a": [1.0], "b": [2.0], "c": [3.0]}, "scores": [[0, 0, 0, 1.0]]}),
                        encoding="utf-8")
    d = tmp_path / "heatmaps"
    d.mkdir()
    link = d / "20261112-210000-000000.json"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("无法创建符号链接")
    with pytest.raises(ev.EvolutionError):
        ev.read_heatmap(d, "20261112-210000-000000")


def test_the_stagnation_flag_requires_consecutive_generation_numbers(tmp_path):
    """代号跳号(1、7、30)说明中间有代没跑,那不是『连续 5 代停滞』。"""
    from dashboard import evolution as ev
    records = [ev.parse_record({"campaign_id": "c", "generation": g, "candidate_sha256": f"{g:064x}",
                                "sharpe": 1.0}) for g in (1, 7, 30, 44, 60)]
    sparse = ev.curve(records, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)
    assert not [f for f in sparse["flags"] if f["kind"] == "stagnant"], "代号稀疏时不该说『连续停滞』"
    dense = [ev.parse_record({"campaign_id": "c", "generation": g, "candidate_sha256": f"{g:064x}",
                               "sharpe": 1.0}) for g in range(1, 6)]
    flags = ev.curve(dense, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)["flags"]
    assert [f for f in flags if f["kind"] == "stagnant"]


def test_the_percentiles_match_the_standard_interpolation():
    """曲线上的 P25/P75 是服务端算的,前端不重算——算错了整张图就是错的。"""
    from dashboard import evolution as ev
    assert ev._percentile([1, 2, 3, 4], 0.25) == 1.75
    assert ev._percentile([1, 2, 3, 4], 0.75) == 3.25
    assert ev._percentile([1, 2, 3], 0.5) == 2.0
    assert ev._percentile([], 0.25) is None
    assert ev._percentile([7.0], 0.75) == 7.0


def test_the_curve_counts_candidates_without_a_sharpe_in_the_generation_count():
    """一代里有的候选还没跑出夏普:代数要算上它们,但均值与分位数只算有值的。"""
    from dashboard import evolution as ev
    records = [
        ev.parse_record({"campaign_id": "c", "generation": 1, "candidate_sha256": "a" * 64,
                         "sharpe": 2.0, "total_return": 0.1, "max_drawdown": 0.1}),
        ev.parse_record({"campaign_id": "c", "generation": 1, "candidate_sha256": "b" * 64,
                         "sharpe": None, "total_return": None, "max_drawdown": None}),
    ]
    point = ev.curve(records, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)["points"][0]
    assert point["count"] == 2 and point["elite_sharpe"] == 2.0 and point["mean_sharpe"] == 2.0


def test_the_log_page_size_comes_from_the_configuration(tmp_path):
    """evolution.log_limit 是默认页大小,也是上限:调用方不能要更大的页。"""
    from dashboard import collectors as col

    ledger(tmp_path, [row(gen=1, sha=f"{i:064x}") for i in range(10)])
    s = settings(tmp_path)
    s.raw["evolution"]["log_limit"] = 3
    page = col.get_evolution_log(s, "camp-2026-11")
    assert page is not None and len(page["records"]) == 3 and page["next_cursor"] == "3"
    bigger = col.get_evolution_log(s, "camp-2026-11", "", 100)
    assert bigger is not None and len(bigger["records"]) == 3


def test_a_malformed_parent_hash_is_rejected_not_truncated(tmp_path):
    """截断出来的哈希是废的:前端拿它请求差异会 404,看起来像『父代源码不在』。"""
    from dashboard import evolution as ev
    for bad in ("b" * 63, "b" * 65, "Z" * 64, 123):
        with pytest.raises(ev.EvolutionError):
            ev.parse_record({"campaign_id": "c", "generation": 1, "candidate_sha256": "a" * 64,
                             "parent_sha256": bad})
    ok = ev.parse_record({"campaign_id": "c", "generation": 1, "candidate_sha256": "a" * 64,
                          "parent_sha256": "b" * 64})
    assert ok.parent_sha256 == "b" * 64


def test_the_campaign_list_is_ordered_by_modification_time_newest_first(tmp_path):
    """超出统计上限时被略过的应该是**最旧**的战役,不是按名字排在后面的。"""
    import os

    d = tmp_path / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    for i, age in enumerate((0, 100, 200)):                 # 三个战役,mtime 依次更旧
        p = d / f"camp{i}.jsonl"
        p.write_text("{}\n", encoding="utf-8")
        os.utime(p, (NOW - age, NOW - age))
    assert ev.campaigns(d) == ["camp0", "camp1", "camp2"]


def test_the_diff_compares_the_parent_source_with_the_candidate_source(tmp_path):
    """差异是『父代源码 → 本代源码』;父代不在了要说 available=False,不能给出空 diff 让人以为没有差异。"""
    import hashlib

    from dashboard import collectors as col

    d = tmp_path / "pending_strategies"
    d.mkdir(parents=True)
    src1 = "class C:\n    x = 1\n"
    src2 = "class C:\n    x = 2\n"
    (d / "gen1.py").write_bytes(src1.encode())
    (d / "gen2.py").write_bytes(src2.encode())
    sha1 = hashlib.sha256(src1.encode()).hexdigest()
    sha2 = hashlib.sha256(src2.encode()).hexdigest()
    s = settings(tmp_path)
    out = col.get_candidate_diff(s, sha2, sha1)
    assert out is not None and out["available"] is True
    assert "-    x = 1" in out["diff"] and "+    x = 2" in out["diff"]
    # 父代源码不在
    missing = col.get_candidate_diff(s, sha2, "f" * 64)
    assert missing is not None and missing["available"] is False and missing["diff"] == ""
