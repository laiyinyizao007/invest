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
