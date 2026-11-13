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
