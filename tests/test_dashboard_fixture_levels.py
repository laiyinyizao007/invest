"""示例数据的 `level` 必须与真实实现算出来的一致。

曾出过两次同类错误:
1. 面板校验回测统计时用了错的键集(夹具照着错的写,套件全绿也证明不了真实产出能过);
2. 批量给示例补 `level` 时按 state 推导,把**账户已硬锁定**的样例写成了 `level=ok`——
   前端同事照着这个样例做,硬锁定就会显示成正常。
所以这个测试**跑真实实现**再和样例比。
"""
import json
import os
from pathlib import Path

import pytest

from dashboard import collectors as col
from dashboard import config as appconfig

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
FIX = WEB / "fixtures"
NOW = 1_800_000_000.0


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _settings(tmp_path, **files):
    root = tmp_path / "repo"
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return appconfig.load(path=root / "none.json", root=root)


def _state_dir(tmp_path, guard, heartbeat="1800000000"):
    return {"data/state/account_guard.json": json.dumps(guard), "data/state/heartbeat": heartbeat}


def _guard(**over):
    g = {"account_id": "1", "last_equity": 9870.5, "peak_equity": 10120.0, "day": "2026-11-13",
         "day_start_equity": 10000.0, "daily_locked_day": None, "hard_locked": False, "hard_reason": "",
         "sampled_at": "2027-01-15T16:00:00+08:00"}
    g.update(over)
    return g


def test_the_hard_locked_fixture_has_the_level_the_implementation_would_produce(tmp_path):
    """硬锁定必须是 critical:前端照着样例做,level=ok 会让硬锁定显示成正常。"""
    fixture = _load("trading-hard-locked")
    assert fixture["level"] == "critical"
    settings = _settings(tmp_path, **_state_dir(tmp_path, _guard(hard_locked=True, hard_reason="累计回撤超过 2000 元")))
    r = col.collect_trading(settings, NOW)
    assert r.level == "critical" and r.section["level"] == "critical"


def test_the_paused_fixture_matches_the_implementation(tmp_path):
    fixture = _load("trading-paused")
    assert fixture["level"] == "warn"
    files = _state_dir(tmp_path, _guard())
    files["data/state/pause_trading.flag"] = ""
    settings = _settings(tmp_path, **files)
    r = col.collect_trading(settings, NOW)
    assert r.level == "warn" and r.section["level"] == "warn"


def test_the_stalled_recording_fixture_matches_the_implementation(tmp_path):
    fixture = _load("recording-stalled")
    assert fixture["level"] == "warn"
    body = {"status": "stalled", "heartbeat_at": "2026-11-13T00:29:58+00:00", "symbols": ["C2701.DCE"]}
    settings = _settings(tmp_path, **{".ops/recording/status.json": json.dumps(body)})
    r = col.collect_recording(settings, NOW)
    assert r.level == "warn" and r.section["level"] == "warn"


def test_every_section_fixture_carries_a_valid_level():
    for f in sorted(FIX.glob("*.json")):
        if f.stem == "manifest":
            continue
        body = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or "state" not in body:
            continue
        assert body.get("level") in ("ok", "warn", "critical", "unknown"), f.stem
        # state 不是 ok 时,level 不能是 ok(否则前端渲染成绿色)
        if body["state"] != "ok":
            assert body["level"] != "ok", f.stem


def test_the_streak_headline_does_not_say_nine_out_of_five():
    """连续天数可以超过要求(达标之后继续跑),写成『9/5 天』读起来像出了错。"""
    from dashboard import collectors as col

    assert col._streak_headline(3) == "连续无异常 3/5 天"
    assert "已达标" in col._streak_headline(9)
    assert "/5" not in col._streak_headline(9)
