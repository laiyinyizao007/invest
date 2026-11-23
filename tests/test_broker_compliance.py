"""国信程序化测试相关功能:阈值预警(3.1)、暂停交易(5.1)、终端信息整机字段。
见 docs/adr/20261111-broker-test-compliance.md。"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

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


# ---------- 6.2 批量撤单(请求文件 → 定时器撤销全部开仓挂单) ----------

import cancel_all_orders  # noqa: E402
from trading.guard_rule import CANCEL_ALL_REQUEST_NAME  # noqa: E402
from vnpy.trader.constant import Status  # noqa: E402


def _active(env) -> list:
    return env.main.get_all_active_orders()


def test_cancel_all_request_cancels_open_orders_keeps_closing_ones_and_clears_request(env):
    opens = [env.send(make_req(price=p)) for p in (2100.0, 2101.0)]
    close = env.send(make_req(offset=Offset.CLOSE, price=2102.0))
    assert all(opens) and close
    request = cancel_all_orders.request(env.state_file.parent, "演示")
    assert request.exists()
    env.tick()
    assert [env.main.get_order(o).status for o in opens] == [Status.CANCELLED] * 2
    assert env.main.get_order(close).is_active()                           # 平仓单不在撤销集合内
    assert not request.exists()                                            # 请求只处理一次
    assert count(env, "批量撤单") == 1
    alert = next(a for a in env.alerts if a["title"] == "批量撤单")
    assert alert["level"] == "WARN" and "2 笔" in alert["message"] and "演示" in alert["message"]
    env.tick()
    assert count(env, "批量撤单") == 1                                     # 不重复处理


def test_no_request_means_nothing_is_cancelled(env):
    oid = env.send(make_req())
    env.tick()
    assert env.main.get_order(oid).is_active() and count(env, "批量撤单") == 0


def test_cancel_all_request_with_no_open_orders_still_clears_and_reports_zero(env):
    request = cancel_all_orders.request(env.state_file.parent)
    env.tick()
    assert not request.exists()
    assert "0 笔" in next(a for a in env.alerts if a["title"] == "批量撤单")["message"]


def test_undeletable_request_is_processed_once_and_reported(env, monkeypatch):
    oid = env.send(make_req())
    request = cancel_all_orders.request(env.state_file.parent)
    import trading.guard_rule as gr
    real_replace = gr.os.replace

    def deny(src, dst, *a, **k):
        if Path(src).name == CANCEL_ALL_REQUEST_NAME:
            raise PermissionError("locked")               # 文件被占用:无法认领(改名)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(gr.os, "replace", deny)
    env.tick()
    env.tick()
    assert env.main.get_order(oid).status == Status.CANCELLED               # 删不掉也照样撤单
    assert count(env, "批量撤单") == 1                                     # 同一份请求不反复处理
    assert count(env, "批量撤单请求无法删除") == 1
    assert request.exists()


def test_cancel_all_cli_writes_request(tmp_path, capsys):
    assert cancel_all_orders.main(["--reason", "收盘前清挂单", "--state-dir", str(tmp_path)]) == 0
    req = tmp_path / CANCEL_ALL_REQUEST_NAME
    assert req.exists() and "收盘前清挂单" in req.read_text(encoding="utf-8")
    assert "已提交批量撤单请求" in capsys.readouterr().out


def test_cancel_all_cli_reports_oserror_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cancel_all_orders, "request", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    assert cancel_all_orders.main(["--state-dir", str(tmp_path)]) == 1
    assert "操作失败" in capsys.readouterr().err


def test_a_request_written_while_processing_is_not_lost(env, monkeypatch):
    """处理期间(撤单发出后、请求清理前)新写入的请求不能被一并删掉:下一个周期要再处理一次。"""
    first = env.send(make_req(price=2100.0))
    cancel_all_orders.request(env.state_file.parent, "第一次")
    real = env.rule._cancel_all_open_orders
    late: list[str] = []

    def cancel_then_new_request():
        sent = real()
        if not late:
            late.append(env.send(make_req(price=2101.0)))                 # 处理过程中又来一笔开仓单
            cancel_all_orders.request(env.state_file.parent, "第二次")     # 以及一份新请求
        return sent

    monkeypatch.setattr(env.rule, "_cancel_all_open_orders", cancel_then_new_request)
    env.tick()
    assert env.main.get_order(first).status == Status.CANCELLED
    assert (env.state_file.parent / CANCEL_ALL_REQUEST_NAME).exists()      # 新请求还在
    env.tick()
    assert env.main.get_order(late[0]).status == Status.CANCELLED
    assert count(env, "批量撤单") == 2
    assert not (env.state_file.parent / CANCEL_ALL_REQUEST_NAME).exists()


def test_a_claimed_request_left_by_a_crash_is_processed_on_the_next_cycle(env):
    """认领(改名 .processing)后进程崩溃:重启/下一周期必须把它再处理一次,不能丢掉应急请求。"""
    oid = env.send(make_req())
    claimed = env.state_file.parent / (CANCEL_ALL_REQUEST_NAME + ".processing")
    claimed.parent.mkdir(parents=True, exist_ok=True)
    claimed.write_text("2026-11-17T13:00:00 崩溃前的请求\n", encoding="utf-8")
    env.tick()
    assert env.main.get_order(oid).status == Status.CANCELLED
    assert not claimed.exists()
    assert "崩溃前的请求" in next(a for a in env.alerts if a["title"] == "批量撤单")["message"]


def test_a_request_taken_by_someone_else_between_read_and_claim_is_ignored(env, monkeypatch):
    import trading.guard_rule as gr
    oid = env.send(make_req())
    cancel_all_orders.request(env.state_file.parent)

    def gone(src, dst, *a, **k):
        raise FileNotFoundError(src)

    monkeypatch.setattr(gr.os, "replace", gone)
    env.tick()
    assert env.main.get_order(oid).is_active() and count(env, "批量撤单") == 0


def test_request_reason_is_bounded_and_control_chars_are_stripped(env):
    path = env.state_file.parent / CANCEL_ALL_REQUEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("x\x1b[31m\r\n" + "长" * 5000).encode("utf-8"))
    env.tick()
    msg = next(a for a in env.alerts if a["title"] == "批量撤单")["message"]
    assert "\x1b" not in msg and "\n" not in msg and len(msg) < 400


def test_alert_counts_only_cancels_actually_sent(env):
    env.gw.cancel_works = False                             # 撤单不落地:委托保持活动
    oid = env.send(make_req())
    assert env.rule._cancel_order(oid, env.clock.t)        # 刚发过撤单(重试间隔内),本次不会再发
    cancel_all_orders.request(env.state_file.parent)
    env.tick()
    msg = next(a for a in env.alerts if a["title"] == "批量撤单")["message"]
    assert "开仓挂单 1 笔,本次发出撤单 0 笔" in msg and "此前已在撤单中" in msg


def test_cli_writes_atomically_and_bounds_the_reason(tmp_path):
    path = cancel_all_orders.request(tmp_path, "原因\n第二行" + "x" * 1000)
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 1 and len(text) < 200
    assert [p.name for p in tmp_path.iterdir()] == [CANCEL_ALL_REQUEST_NAME]   # 没有残留临时文件


def test_reason_in_alert_excludes_the_cli_timestamp(env):
    cancel_all_orders.request(env.state_file.parent, "收盘前清挂单")
    env.tick()
    msg = next(a for a in env.alerts if a["title"] == "批量撤单")["message"]
    assert "(收盘前清挂单)" in msg and "T" not in msg.split("(")[1].split(")")[0]


def test_undeletable_claimed_request_alerts_only_once(env, monkeypatch):
    oid = env.send(make_req())
    cancel_all_orders.request(env.state_file.parent)
    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self.name.endswith(".processing"):
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    for _ in range(3):
        env.tick()
    assert env.main.get_order(oid).status == Status.CANCELLED
    assert count(env, "批量撤单请求无法删除") == 1


def test_unlink_alert_resumes_after_the_stuck_claim_is_cleared(env, monkeypatch):
    real_unlink = Path.unlink
    deny_on = {"yes": True}

    def deny(self, *a, **k):
        if self.name.endswith(".processing") and deny_on["yes"]:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    cancel_all_orders.request(env.state_file.parent, "同一原因", now=__import__("datetime").datetime(2026, 11, 17, 13, 0))
    env.tick()
    deny_on["yes"] = False
    env.tick()                                             # 卡住的认领文件被处理并删掉
    deny_on["yes"] = True
    cancel_all_orders.request(env.state_file.parent, "同一原因", now=__import__("datetime").datetime(2026, 11, 17, 13, 0))
    env.tick()                                             # 内容完全相同的新请求再次删不掉:要重新告警
    assert count(env, "批量撤单请求无法删除") == 2
