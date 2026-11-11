"""国信程序化测试相关功能:阈值预警(3.1)、暂停交易(5.1)、终端信息整机字段。
见 docs/adr/20261111-broker-test-compliance.md。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import collect_terminal_info as ti  # noqa: E402
import pause_trading  # noqa: E402
from test_guard_integration import Env, make_req  # noqa: E402
from trading.guard_rule import PAUSE_FLAG_NAME, THRESHOLD_ALERT_INTERVAL  # noqa: E402
from vnpy.trader.constant import Offset  # noqa: E402


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.tick()
    yield e
    e.main.close()


def count(env, title):
    return env.alert_titles().count(title)


# ---------- 3.1 阈值预警 ----------

def test_threshold_breach_raises_a_warn_alert(env):
    env.engine.rules["每日上限检查"].total_order_limit = 2
    assert env.send(make_req()) and env.send(make_req(price=2201.0))
    assert env.send(make_req(price=2202.0)) == ""                       # 第 3 笔超过上限 → 被拒
    env.tick()
    assert count(env, "风控阈值触发") == 1
    alert = next(a for a in env.alerts if a["title"] == "风控阈值触发")
    assert alert["level"] == "WARN" and "汇总委托笔数" in alert["message"] and "达到上限2" in alert["message"]


def test_same_metric_is_throttled_but_other_metrics_alert_separately(env):
    daily = env.engine.rules["每日上限检查"]
    daily.total_order_limit = 1
    env.send(make_req())
    for price in (2201.0, 2202.0, 2203.0):
        assert env.send(make_req(price=price)) == ""
    assert count(env, "风控阈值触发") == 1                              # 同一指标节流
    daily.total_order_limit = 1000                                      # 规则链按顺序短路:先放开笔数,才轮得到手数规则
    env.engine.rules["委托规模检查"].order_volume_limit = 1
    assert env.send(make_req(volume=2, price=2204.0)) == ""
    assert count(env, "风控阈值触发") == 2                              # 手数是另一个指标


def test_throttle_expires(env):
    env.engine.rules["每日上限检查"].total_order_limit = 1
    env.send(make_req())
    env.send(make_req(price=2201.0))
    env.clock.t += THRESHOLD_ALERT_INTERVAL + 1
    env.send(make_req(price=2202.0))
    assert count(env, "风控阈值触发") == 2


def test_validity_failures_are_not_threshold_alerts(env):
    assert env.send(make_req(price=2200.5)) == ""                       # 价格不是最小变动价位的整数倍
    assert count(env, "风控阈值触发") == 0


# ---------- 5.1 暂停交易 ----------

def flag(env) -> Path:
    return env.state_file.parent / PAUSE_FLAG_NAME


def test_pause_blocks_opens_but_not_closes_and_resume_restores(env):
    assert env.send(make_req())
    pause_trading.pause(env.state_file.parent, "演示")
    assert env.send(make_req(price=2201.0)) == ""                       # 开仓被拒
    assert env.send(make_req(offset=Offset.CLOSE, price=2201.0))        # 平仓放行
    env.tick()
    assert count(env, "交易已暂停") == 1
    env.tick()
    assert count(env, "交易已暂停") == 1                                # 边沿触发,不重复
    assert pause_trading.resume(env.state_file.parent)
    env.tick()
    assert count(env, "交易已恢复") == 1
    assert env.send(make_req(price=2202.0))


# ---------- pause_trading 命令行 ----------

def test_pause_cli_on_status_off(tmp_path, capsys):
    assert pause_trading.main(["status", "--state-dir", str(tmp_path)]) == 0
    assert "未暂停" in capsys.readouterr().out
    assert pause_trading.main(["on", "--reason", "测试", "--state-dir", str(tmp_path)]) == 0
    assert (tmp_path / PAUSE_FLAG_NAME).exists()
    pause_trading.main(["status", "--state-dir", str(tmp_path)])
    assert "暂停中" in capsys.readouterr().out and "测试" in (tmp_path / PAUSE_FLAG_NAME).read_text(encoding="utf-8")
    assert pause_trading.main(["off", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / PAUSE_FLAG_NAME).exists()
    assert pause_trading.main(["off", "--state-dir", str(tmp_path)]) == 0
    assert "无需恢复" in capsys.readouterr().out


# ---------- 终端信息:整机 ----------

def test_machine_info_and_render():
    m = ti.machine_info({"Manufacturer": " Dell Inc. ", "Model": "XPS", "TotalPhysicalMemory": 34359738368})
    assert m == {"manufacturer": "Dell Inc.", "model": "XPS", "memory_gb": 32}
    assert ti.machine_info({}) == {"manufacturer": "", "model": "", "memory_gb": 0}
    from test_collect_terminal_info import make_info
    assert "整机:Dell Inc. XPS(内存 32 GB)" in ti.render_text(make_info(machine=m))


# ---------- 优雅登出 ----------

from types import SimpleNamespace  # noqa: E402

from trading.graceful_exit import logout_ctp  # noqa: E402


class FakeApi:
    def __init__(self, logged_in=True, fail=False):
        self.login_status, self.reqid, self.brokerid, self.userid = logged_in, 5, "8030", "u1"
        self.fail, self.calls = fail, []

    def reqUserLogout(self, req, reqid):
        if self.fail:
            raise RuntimeError("api broke")
        self.calls.append((req, reqid))


class FakeMain:
    def __init__(self, td=None, md=None, raises=False):
        self.gw, self.raises = SimpleNamespace(td_api=td, md_api=md), raises

    def get_gateway(self, name):
        if self.raises:
            raise KeyError(name)
        return self.gw


def test_logout_sends_a_request_to_each_logged_in_channel_and_waits():
    td, md, slept = FakeApi(), FakeApi(), []
    results = logout_ctp(FakeMain(td, md), sleep=slept.append)
    assert td.calls == [({"BrokerID": "8030", "UserID": "u1"}, 6)] and md.calls == [({"BrokerID": "8030", "UserID": "u1"}, 6)]
    assert results == ["交易: 已发送登出请求", "行情: 已发送登出请求"] and slept == [1.5]


def test_logout_skips_channels_that_are_not_logged_in_and_does_not_wait():
    td, md, slept = FakeApi(logged_in=False), FakeApi(logged_in=False), []
    results = logout_ctp(FakeMain(td, md), sleep=slept.append)
    assert not td.calls and not md.calls and slept == [] and all("未登录" in r for r in results)


def test_logout_failure_on_one_channel_does_not_block_the_other_or_raise():
    td, md = FakeApi(fail=True), FakeApi()
    results = logout_ctp(FakeMain(td, md), sleep=lambda s: None)
    assert "登出请求失败" in results[0] and md.calls and "已发送" in results[1]


def test_logout_tolerates_fake_gateways_and_missing_gateway():
    assert all("跳过" in r for r in logout_ctp(FakeMain(None, None), sleep=lambda s: None))
    assert "取网关失败" in logout_ctp(FakeMain(raises=True), sleep=lambda s: None)[0]


# ---------- 评审后加固 ----------

def test_contract_level_thresholds_are_throttled_per_contract(env):
    """A 合约刚预警过,B 合约的突破不能被同一个节流键吞掉。"""
    from vnpy.trader.constant import Exchange
    rule = env.rule
    rule._on_threshold_log("委托被拦截，合约委托笔数3达到上限3：OrderRequest(symbol='c2601', exchange=<Exchange.DCE: 'DCE'>)")
    rule._on_threshold_log("委托被拦截，合约委托笔数3达到上限3：OrderRequest(symbol='ma2601', exchange=<Exchange.CZCE: 'CZCE'>)")
    rule._on_threshold_log("委托被拦截，合约委托笔数4达到上限3：OrderRequest(symbol='c2601', exchange=<Exchange.DCE: 'DCE'>)")
    assert env.alert_titles().count("风控阈值触发") == 2


def test_int_and_float_volume_share_one_throttle_key(env):
    env.rule._on_threshold_log("委托被拦截，委托数量2超过上限1：OrderRequest(symbol='c2601')")
    env.rule._on_threshold_log("委托被拦截，委托数量2.0超过上限1：OrderRequest(symbol='c2601')")
    assert env.alert_titles().count("风控阈值触发") == 1


def test_pause_cli_reports_oserror_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pause_trading, "resume", lambda d: (_ for _ in ()).throw(PermissionError("locked")))
    assert pause_trading.main(["off", "--state-dir", str(tmp_path)]) == 1
    assert "操作失败" in capsys.readouterr().err
