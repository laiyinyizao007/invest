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


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(tmp_path, host):
    assert c.load(path=_write(tmp_path, {"host": host}), root=tmp_path).raw["host"] == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.com", "", "127.0.0.1.evil.com", "localhost.evil.com", 127, None])
def test_non_loopback_hosts_are_refused_at_config_level(tmp_path, host):
    """阶段①只监听本机回环;经局域网访问是阶段⑤的事,须另写 ADR。这条承诺在配置层落实,任何入口都绕不过。"""
    with pytest.raises(c.ConfigError, match="回环"):
        c.load(path=_write(tmp_path, {"host": host}), root=tmp_path)


def test_default_config_file_follows_the_root_argument(tmp_path):
    """审查发现:root 变了,缺省配置路径却仍指向仓库根,导致 --root 无法隔离(测试读到了真实的 config/status_dashboard.json)。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "status_dashboard.json").write_text(json.dumps({"environment": "live"}), encoding="utf-8")
    assert c.load(root=tmp_path).environment["label"] == "live"           # 读的是 root 下的配置
    other = tmp_path / "empty"
    other.mkdir()
    assert c.load(root=other).environment["label"] == "unknown"           # root 下没有就用默认值,而不是回落到仓库根


@pytest.mark.parametrize("token_file", ["data/state/status.token", "data/state/sub/t", "runtime/data/state/t", "runtime/vntrader/log/t", ".ops/recording/t",
                                        "pending_strategies/t", "data/research/t", "data/research-control/t"])
def test_a_token_file_inside_a_directory_the_dashboard_reads_is_refused(tmp_path, token_file):
    """令牌文件不能放在交易侧的状态/日志/录制/研究目录里:创建它就是在写交易系统的目录。"""
    cfg = _write(tmp_path, {"token_file": token_file, "log_dirs": ["runtime/vntrader/log"], "state_dir": "data/state"})
    with pytest.raises(c.ConfigError, match="令牌"):
        c.load(path=cfg, root=tmp_path)


def test_the_default_token_file_is_in_its_own_directory(tmp_path):
    s = c.load(path=tmp_path / "none.json", root=tmp_path)
    assert s.paths["token_file"] == (tmp_path / "data" / "dashboard" / "status.token").resolve()


def test_guard_staleness_threshold_stays_coupled_to_the_runtime_persistence_cadence():
    """面板把『风控状态文件落后 >180 秒』判为陈旧,依据是运行时至少每 60 秒落盘一次。
    面板不能 import trading/risk 之外的运行时代码,所以这里读源码文本守住这个耦合:
    运行时改了落盘节奏(间隔变大、不再每秒调用 update)而面板阈值没跟着改,这条测试会提醒。"""
    import re
    from dashboard import collectors
    root = c.ROOT
    guard_src = (root / "risk" / "account_guard.py").read_text(encoding="utf-8")
    m = re.search(r"_last_saved_at\)\.total_seconds\(\)\s*>=\s*(\d+)", guard_src)
    assert m, "account_guard.update() 的『至少每 N 秒落盘』条件找不到了——请复核 GUARD_STALE_AFTER 的依据"
    persist_every = float(m.group(1))
    assert collectors.GUARD_STALE_AFTER >= 2 * persist_every, "面板的陈旧阈值应至少是运行时落盘间隔的 2 倍,否则会误报"
    assert collectors.GUARD_STALE_AFTER <= 20 * persist_every, "阈值过大会让真正陈旧的风控状态长期显示为新鲜"
    rule_src = (root / "trading" / "guard_rule.py").read_text(encoding="utf-8")
    assert "def on_timer" in rule_src and "self.guard.update(" in rule_src, "update() 不再由 on_timer 驱动——落盘节奏的前提变了"


@pytest.mark.parametrize("token_file", [".vntrader_drill/t", ".vntrader_brokertest/t", ".vntrader_probe/t", "risk/t", "data/other/t", "status.token",
                                        "data/dashboard/../state/t", "data/t"])
def test_the_token_file_is_allowed_only_inside_the_dashboard_own_directory(tmp_path, token_file):
    """白名单而非拒绝名单:拒绝名单永远会漏掉新增的交易侧目录(.vntrader_drill 等)。令牌只能在 data/dashboard/ 下。"""
    cfg = _write(tmp_path, {"token_file": token_file})
    with pytest.raises(c.ConfigError, match="令牌"):
        c.load(path=cfg, root=tmp_path)


def test_a_token_file_in_a_subdirectory_of_the_dashboard_directory_is_fine(tmp_path):
    cfg = _write(tmp_path, {"token_file": "data/dashboard/sub/t.token"})
    assert c.load(path=cfg, root=tmp_path).paths["token_file"].name == "t.token"
