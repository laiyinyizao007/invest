"""空环境下,没有任何区块能显示成『正常』。

每个区块各自有『缺失时怎么办』的测试,但**跨区块的保证**没有:空环境下总览不能是绿的。
这是本项目最核心的一条原则(缺失/过期/损坏绝不显示成正常),值得有一个整体性的守卫。
"""
import json
from pathlib import Path

from dashboard import collectors as col
from dashboard import config as appconfig

BLOCKS = ("trading", "alerts", "days", "recording", "research", "evolution")
NOW = 1_800_000_000.0


def _empty_settings(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "status_dashboard.json").write_text(json.dumps({"environment": "simnow"}), encoding="utf-8")
    return appconfig.load(path=root / "config" / "status_dashboard.json", root=root)


def test_no_block_looks_healthy_when_there_is_nothing_to_read(tmp_path):
    settings = _empty_settings(tmp_path)
    for name in BLOCKS:
        r = getattr(col, f"collect_{name}")(settings, NOW)
        assert not (r.section["state"] == "ok" and r.level == "ok"), (name, r.section["state"], r.level)
        assert r.section["state"] in ("missing", "unknown", "corrupt"), (name, r.section["state"])


def test_the_overview_is_not_ok_when_nothing_can_be_read(tmp_path):
    settings = _empty_settings(tmp_path)
    overview = col.collect_overview(settings, NOW)
    assert overview["level"] != "ok", overview["level"]
    for name, section in overview["sections"].items():
        assert not (section["state"] == "ok" and section["level"] == "ok"), (name, section)
