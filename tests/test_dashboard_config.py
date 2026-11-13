"""dashboard/config.py:默认值、覆盖、校验、路径不越界;以及与交易侧常量的一致性。"""
import json

import pytest

from dashboard import config as c


def _write(tmp_path, data):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_defaults_load_without_a_config_file(tmp_path):
    s = c.load(path=tmp_path / "missing.json", root=tmp_path)
    assert s.environment == {"label": "unknown", "display": "环境未知"}
    assert s.raw["host"] == "127.0.0.1" and s.raw["port"] == 8770
    assert s.limits == {"daily_loss_yuan": 600.0, "hard_drawdown_yuan": 2000.0}       # 取风控默认值
    assert s.paths["state_dir"] == (tmp_path / "data" / "state").resolve()


def test_overrides_merge_and_limits_can_be_customised(tmp_path):
    s = c.load(path=_write(tmp_path, {"environment": "simnow", "port": 9000, "limits": {"daily_loss_yuan": 500},
                                      "stale_after_seconds": {"trading": 30}}), root=tmp_path)
    assert s.environment["display"] == "SimNow 模拟盘" and s.raw["port"] == 9000
    assert s.limits["daily_loss_yuan"] == 500 and s.limits["hard_drawdown_yuan"] == 2000.0
    assert s.stale_after("trading") == 30.0 and s.stale_after("alerts") == 300.0        # 未覆盖的保留默认


@pytest.mark.parametrize("bad", [
    {"environment": "prod"}, {"port": 0}, {"port": "80"}, {"port": True}, {"polling_hint_seconds": 0},
    {"stale_after_seconds": {"trading": -1}}, {"limits": {"daily_loss_yuan": -5}}, {"unknown_key": 1},
    {"state_dir": "../outside"}, {"token_file": "/etc/passwd"}, {"log_dirs": ["../../x"]},
    {"docs": {"a": "docs/a.md"}},                       # docs 与 docs_titles 键不一致
])
def test_invalid_config_is_rejected(tmp_path, bad):
    with pytest.raises(c.ConfigError):
        c.load(path=_write(tmp_path, bad), root=tmp_path)


def test_non_object_or_broken_json_is_rejected(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(c.ConfigError):
        c.load(path=p, root=tmp_path)
    p.write_text("{bad", encoding="utf-8")
    with pytest.raises(c.ConfigError):
        c.load(path=p, root=tmp_path)


def test_shipped_config_file_is_valid_if_present():
    assert c.load() is not None


def test_pause_flag_name_matches_the_trading_side_constant():
    """面板不 import trading,所以常量写了两份——用测试保证不会悄悄分叉。读源码文本,不 import(trading 会拖入 vnpy)。"""
    import re
    from pathlib import Path
    text = (Path(c.ROOT) / "trading" / "guard_rule.py").read_text(encoding="utf-8")
    assert re.search(r'PAUSE_FLAG_NAME\s*=\s*"([^"]+)"', text).group(1) == c.PAUSE_FLAG_NAME
