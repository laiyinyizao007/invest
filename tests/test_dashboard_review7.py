"""codex 第五次复核(44.6/50)的 2 个 Medium + 2 个 Low。"""
import json
import os
import time

import pytest

from dashboard import collectors as col
from dashboard import config as appconfig
from dashboard import safe
from dashboard import evolution as ev

ZW = chr(0x200B)


def _record(campaign="c", generation=1, sharpe=0.4):
    return {"campaign_id": campaign, "generation": generation, "candidate_sha256": "a" * 64, "parent_sha256": None,
            "run_id": "20261112-210000-000001", "status": "evaluated", "sharpe": sharpe, "total_return": 0.03,
            "max_drawdown": 0.19, "dsr": 0.3, "n_trials": 4, "fail_reason": None, "reflection": "x",
            "created_at": "2026-11-12T21:00:00", "cell": "c", "diagnosis": None}


def _make_campaigns(root, count, broken_last=False):
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        body = json.dumps(_record(f"camp{i}"))
        if broken_last and i == count - 1:
            body = "{broken"
        (d / f"camp{i}.jsonl").write_text(body + "\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------- Medium 1:未检查的战役不能显示绿色
def test_unscanned_campaigns_must_not_leave_the_block_green(tmp_path, monkeypatch):
    """超过上限时,第 51 个台账即使损坏,区块也不能是 ok——『没检查』不等于『没问题』。"""
    root = tmp_path / "repo"
    _make_campaigns(root, 51, broken_last=True)
    settings = appconfig.load(path=root / "none.json", root=root)
    monkeypatch.setattr(col, "MAX_CAMPAIGNS_SUMMARIZED", 50)
    r = col.collect_evolution(settings, 1_800_000_000.0)
    assert r.level != "ok", "有战役没被检查,区块不能显示正常"
    assert r.section["message"] and "未检查" in r.section["message"]
    assert r.reasons and "未检查" in r.reasons[0]["text"]
    assert "已检查" in r.headline


def test_the_campaign_list_reports_totals_separately_from_the_rows(tmp_path, monkeypatch):
    """『未统计』不该伪装成一个可点的战役行;总数/已检查/未检查单独给。"""
    root = tmp_path / "repo"
    _make_campaigns(root, 5)
    settings = appconfig.load(path=root / "none.json", root=root)
    monkeypatch.setattr(col, "MAX_CAMPAIGNS_SUMMARIZED", 2)
    r = col.collect_evolution(settings, 1_800_000_000.0)
    data = r.section["data"]
    assert data["total"] == 5 and data["checked"] == 2 and data["unscanned"] == 3
    assert all(not c["campaign_id"].startswith("(") for c in data["campaigns"])


# ---------------------------------------------------------------- Medium 2:零宽字符拆开其它路径形态
@pytest.mark.parametrize("text,leak", [
    ("\\" + ZW + "Users\\Alice\\secret.txt", "Alice"),
    ("%" + "5C" + "%E2%80%8B" + "Users%5CAlice%5Csecret.txt", "Alice"),
    ("Ｃ" + ZW + "：＼Users＼Alice＼secret.txt", "Alice"),
    ("%EF%BC%A3%E2%80%8B%EF%BC%9A%EF%BC%BCUsers%EF%BC%BCAlice", "Alice"),
])
def test_zero_width_characters_cannot_split_any_path_shape(text, leak):
    """盘符路径已经容忍零宽字符,其它形态(无盘符根路径、全角)也要一致。"""
    out = safe.scrub_text(text)
    assert leak not in out, out


# ---------------------------------------------------------------- Low 1:缓存不能用元数据判断内容没变
def test_the_campaign_summary_cache_notices_a_same_size_rewrite(tmp_path):
    """同长度改写并恢复 mtime 后,摘要不能还返回旧值。"""
    root = tmp_path / "repo"
    d = _make_campaigns(root, 1)
    settings = appconfig.load(path=root / "none.json", root=root)
    first = col.collect_evolution(settings, 1_800_000_000.0).section["data"]["campaigns"][0]
    assert first["best_sharpe"] == 0.4

    path = d / "camp0.jsonl"
    stat = path.stat()
    path.write_text(json.dumps(_record("camp0", sharpe=9.9)) + "\n", encoding="utf-8")
    os.utime(path, (stat.st_atime, stat.st_mtime))            # 恢复 mtime
    second = col.collect_evolution(settings, 1_800_000_000.0).section["data"]["campaigns"][0]
    assert second["best_sharpe"] == 9.9, "缓存不能只靠元数据判断内容没变"


def test_the_candidate_source_cache_notices_a_same_size_rewrite(tmp_path):
    """候选源码审阅接口:同长度改写后不能还返回旧源码。"""
    import hashlib

    d = tmp_path / "pending"
    d.mkdir()
    path = d / "x.py"
    path.write_bytes(b"VALUE = 1\n")
    sha = hashlib.sha256(b"VALUE = 1\n").hexdigest()
    assert ev.candidate_source(d, sha) == "VALUE = 1\n"
    stat = path.stat()
    path.write_bytes(b"VALUE = 2\n")
    os.utime(path, (stat.st_atime, stat.st_mtime))
    assert ev.candidate_source(d, sha) is None, "旧哈希不该再匹配到新内容"


# ---------------------------------------------------------------- Low 2:常量只定义一次
def test_the_campaign_limit_constant_is_defined_exactly_once():
    src = open(col.__file__, encoding="utf-8").read()
    assert src.count("MAX_CAMPAIGNS_SUMMARIZED = ") == 1, "常量重复定义了"


def test_the_wire_level_of_the_evolution_section_is_not_ok_when_something_is_unknown(tmp_path, monkeypatch):
    """端点只返回 Section(没有 level 字段),契约说前端『先看 state』——所以『部分未检查』必须落到 state。"""
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    _make_campaigns(root, 5)
    settings = appconfig.load(path=root / "none.json", root=root)
    monkeypatch.setattr(col, "MAX_CAMPAIGNS_SUMMARIZED", 2)
    section = col.collect_evolution(settings, 1_800_000_000.0).section
    assert section["state"] == "unknown", "state 必须是 unknown,否则卡片还是绿的"
    assert section["data"] is not None, "已统计的部分仍然要显示"
    assert "未检查" in (section["message"] or "")


def test_the_evolution_section_is_ok_when_everything_was_checked(tmp_path):
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    _make_campaigns(root, 2)
    settings = appconfig.load(path=root / "none.json", root=root)
    section = col.collect_evolution(settings, 1_800_000_000.0).section
    assert section["state"] == "ok" and section["data"]["unscanned"] == 0


def test_a_corrupt_ledger_also_makes_the_state_unknown(tmp_path):
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    _make_campaigns(root, 1, broken_last=True)
    settings = appconfig.load(path=root / "none.json", root=root)
    section = col.collect_evolution(settings, 1_800_000_000.0).section
    assert section["state"] == "unknown"


def test_a_continuously_improving_campaign_is_not_reported_as_stagnant():
    """曾经拿**全局最好**当基准,窗口内的最好值等于全局最好时判定恒成立 → 正在进步的战役也被说成停滞。"""
    from dashboard import evolution as ev
    records = [ev.parse_record({"campaign_id": "c", "generation": g, "candidate_sha256": f"{g:064x}",
                                "sharpe": 1.0 + g * 0.1}) for g in range(1, 21)]
    flags = ev.curve(records, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)["flags"]
    assert not [f for f in flags if f["kind"] == "stagnant"], flags


def test_a_genuinely_flat_tail_is_reported_as_stagnant():
    from dashboard import evolution as ev
    records = [ev.parse_record({"campaign_id": "c", "generation": g, "candidate_sha256": f"{g:064x}",
                                "sharpe": 2.0}) for g in range(1, 11)]
    flags = ev.curve(records, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)["flags"]
    assert [f for f in flags if f["kind"] == "stagnant"]


def test_a_campaign_that_improves_then_flats_is_reported_as_stagnant():
    from dashboard import evolution as ev
    sharpes = [1.0, 1.5, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
    records = [ev.parse_record({"campaign_id": "c", "generation": g, "candidate_sha256": f"{g:064x}",
                                "sharpe": v}) for g, v in enumerate(sharpes, start=1)]
    flags = ev.curve(records, stagnant_generations=5, stagnant_epsilon=0.05, overfit_mean_gap=1.0)["flags"]
    assert [f for f in flags if f["kind"] == "stagnant"]


def test_the_log_page_limit_is_not_capped_by_a_hardcoded_number(tmp_path):
    """配置成 500 时就该给 500:写死的上限会让配置项有一半是摆设。"""
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    _make_campaigns(root, 1)
    ledger_dir = root / "data" / "research" / "evolution"
    path = ledger_dir / "camp0.jsonl"
    path.write_text("".join(json.dumps(_record("camp0", generation=g)) + "\n" for g in range(1, 300)), encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    settings.raw["evolution"]["log_limit"] = 250
    page = col.get_evolution_log(settings, "camp0", "", 1000)
    assert page is not None and len(page["records"]) == 250


# ---------------------------------------------------------------- 净化层的两个尾巴
def test_a_zero_width_character_inside_a_root_directory_name_is_still_scrubbed():
    """零宽字符夹在目录名中间时曾原样保留。"""
    zw = chr(0x200B)
    text = chr(92) + "Us" + zw + "ers" + chr(92) + "Alice" + chr(92) + "secret.txt"
    out = safe.scrub_text(text)
    assert "Alice" not in out and "secret.txt" not in out, out


def test_an_ordinary_rooted_path_is_still_scrubbed_after_the_name_tolerance_change():
    """改了目录名的容忍写法,不能把正常的根路径漏掉。"""
    text = chr(92) + "Users" + chr(92) + "Alice" + chr(92) + "x"
    out = safe.scrub_text(text)
    assert "Alice" not in out, out
