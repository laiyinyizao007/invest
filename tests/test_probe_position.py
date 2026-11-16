"""只读探针的档案解析与前置闸门:优先级必须明确,否则会连到别的前置。不联网。"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import probe_position as p
from ctp_tools.profile import load_profile


@pytest.fixture(autouse=True)
def clean_process_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CTP_") or key == "LIVE_VT_SYMBOL":
            monkeypatch.delenv(key, raising=False)


def test_profile_overrides_dotenv_and_process_env(tmp_path, monkeypatch):
    """指定了档案就必须以档案为准:否则 --profile .env.guoxin 会被 .env 里的旧值盖掉。"""
    dotenv = tmp_path / ".env"
    dotenv.write_text("CTP_TD_ADDRESS=1.1.1.1:1\nCTP_USERNAME=dotenv_user\n", encoding="utf-8")
    profile = tmp_path / ".env.guoxin"
    profile.write_text("CTP_TD_ADDRESS=121.13.218.19:41285\n", encoding="utf-8")
    monkeypatch.setenv("CTP_TD_ADDRESS", "9.9.9.9:9")
    loaded = load_profile(str(profile), root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"   # 档案优先
    assert loaded.env["CTP_USERNAME"] == "dotenv_user"              # .env 次之
    assert loaded.notes                                    # 进程环境里的旧值已报出来(只提示)
    assert loaded.hard_conflicts == []                     # 指定了档案就不是硬冲突
    plain = load_profile("", root=tmp_path)
    assert plain.env["CTP_TD_ADDRESS"] == "1.1.1.1:1"               # 没指定档案就用 .env
    assert plain.hard_conflicts                             # 没指定档案 → 进程环境与 .env 打架 = 硬冲突


def test_missing_profile_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(p.bootstrap, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_profile(".env.不存在", root=tmp_path)



def _profile(env, hard=(), notes=()):
    """构造 ctp_tools.profile.Profile(load_profile 的返回值)。"""
    from ctp_tools.profile import Profile

    return Profile(env=dict(env), hard_conflicts=list(hard), notes=list(notes))

def test_cli_arguments_match_the_documented_usage():
    args = p.parse_args(["--profile", ".env.guoxin", "--seconds", "45"])
    assert args.profile == ".env.guoxin"
    assert args.seconds == 45
    assert p.parse_args([]).seconds == 60


def test_probe_refuses_a_front_outside_the_trusted_table(monkeypatch, capsys):
    """探针要登录,登录就是把账号密码发给那台机器——地址必须过闸门。"""
    monkeypatch.setenv("CTP_USERNAME", "012345678")
    monkeypatch.setenv("CTP_PASSWORD", "pw")
    monkeypatch.setenv("CTP_BROKER_ID", "8030")
    monkeypatch.setenv("CTP_TD_ADDRESS", "10.0.0.1:41285")     # 不可信前置
    monkeypatch.setenv("CTP_MD_ADDRESS", "121.13.218.19:41293")
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile(dict(os.environ)))
    assert p.main([]) == p.EXIT_CONFIG
    assert "不在可信前置地址表内" in capsys.readouterr().err


def test_probe_refuses_when_the_market_front_is_not_trusted(monkeypatch, capsys):
    monkeypatch.setenv("CTP_USERNAME", "012345678")
    monkeypatch.setenv("CTP_PASSWORD", "pw")
    monkeypatch.setenv("CTP_BROKER_ID", "8030")
    monkeypatch.setenv("CTP_TD_ADDRESS", "121.13.218.19:41285")
    monkeypatch.setenv("CTP_MD_ADDRESS", "8.8.8.8:41293")       # 不可信行情前置
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile(dict(os.environ)))
    assert p.main([]) == p.EXIT_CONFIG
    assert "行情前置" in capsys.readouterr().err


def test_probe_output_is_redacted():
    """账号与密码不能出现在探针输出里。"""
    from notify.redact import redact

    assert redact("用户 012345678 登录成功", ("012345678",)) == "用户 *** 登录成功"


def test_a_missing_profile_file_is_reported(monkeypatch, capsys):
    def missing(profile="", root=None):
        raise FileNotFoundError("找不到档案 .env.guoxin")

    monkeypatch.setattr(p, "load_profile", missing)
    assert p.main(["--profile", ".env.guoxin"]) == p.EXIT_CONFIG
    assert "找不到档案" in capsys.readouterr().err


def test_missing_credentials_stop_the_probe(monkeypatch, capsys):
    """档案里没有账号密码就没法登录:直接说缺什么,不打印别的。"""
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile({"CTP_BROKER_ID": "8030"}))
    assert p.main([]) == p.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "CTP_USERNAME" in err and "CTP_PASSWORD" in err


def test_conflicts_with_the_process_environment_are_reported(monkeypatch, capsys):
    """进程环境里的旧值会被档案盖掉:冲突要说出来,别让人以为连的就是档案里的。"""
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile(
        {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw", "CTP_BROKER_ID": "8030",
         "CTP_TD_ADDRESS": "10.0.0.1:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293"},
        notes=["CTP_TD_ADDRESS:进程环境里是 x,档案里是 y(取档案的值)"]))
    # 冲突提示在前置闸门之前打印;地址不可信 → 停在这里,不会去连网
    assert p.main([]) == p.EXIT_CONFIG
    assert "取档案的值" in capsys.readouterr().err


# ---------- 登录成功后的那条路 ----------

class _FakeCollector:
    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.log_lines = ["交易服务器连接成功", "交易服务器登录成功"]
        self.queried = 0
        self.position_rows = []

    def on_log(self, event):
        pass

    def on_tick(self, event):
        pass

    def on_contract(self, event):
        pass

    def on_position(self, event):
        pass

    def on_account(self, event):
        pass

    def mark_positions(self):
        return len(self.position_rows)

    def positions_since(self, mark):
        return list(self.position_rows[mark:])

    def contract_of(self, vt_symbol):
        return None


class _FakeGateway:
    def __init__(self):
        self.td_api = SimpleNamespace(connect_status=True, login_status=True)

    def query_position(self):
        pass


class _FakeMainEngine:
    def __init__(self, event_engine=None):
        self.gateway = _FakeGateway()
        self.connected = None
        self.subscribed = []

    def add_gateway(self, gateway):
        pass

    def connect(self, setting, gateway_name):
        self.connected = (setting, gateway_name)

    def get_gateway(self, name):
        return self.gateway

    def subscribe(self, request, gateway_name):
        self.subscribed.append(request)

    def get_all_positions(self):
        return []

    def get_all_accounts(self):
        return []


def test_a_successful_login_queries_the_positions(monkeypatch, capsys):
    """登录成功后:查持仓、打印交易通道状态,并说明「探针没发任何委托」。"""
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile(
        {"CTP_USERNAME": "012345678", "CTP_PASSWORD": "pw", "CTP_BROKER_ID": "8030",
         "CTP_TD_ADDRESS": "121.13.218.19:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293"}))
    monkeypatch.setattr(p, "Collector", _FakeCollector)
    monkeypatch.setattr(p, "check_positions", lambda engine, wait, early_exit, collector=None: ([], True))
    monkeypatch.setattr(p, "close_engine", lambda engine: None)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    # 探针在模块层 import 了 EventEngine/MainEngine,所以要替换探针模块里的名字,
    # 而不是 vnpy 包里的(换包里的名字不生效,还会把 vnpy 自己的线程拉起来)。
    monkeypatch.setattr(p, "EventEngine", lambda: SimpleNamespace(register=lambda *a: None))
    monkeypatch.setattr(p, "MainEngine", lambda event_engine=None: _FakeMainEngine())

    class _FakeCtpGateway:
        default_name = "CTP"

    monkeypatch.setattr("vnpy_ctp.CtpGateway", _FakeCtpGateway)
    # 探针结束时是 os._exit(CTP 原生线程是非守护线程,正常退出路径走不通):
    # 测试里把它换成抛 SystemExit,免得把 pytest 一起带走。
    monkeypatch.setattr(p.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        p.main([])
    assert exc.value.code == p.EXIT_OK
    out = capsys.readouterr().out
    assert "非零持仓条数 = 0" in out
    assert "login_status = True" in out
    assert "探针结束(未发任何委托)" in out


def test_an_unrecognized_position_direction_is_shown_as_no_direction(monkeypatch, capsys):
    """认不出来的持仓方向显示「无方向」,不把内部枚举名漏给看报告的人(2026-11-16 codex 复核 L4)。

    `.value` 是被 gettext 翻译过的枚举值(英文机器上是 'Long'),直接打出来会让同一份持仓
    在不同机器上显示成不同文案;认不出来的时候更不能打枚举名 —— 看报告的人不认得它。
    """
    monkeypatch.setattr(p, "load_profile", lambda profile="", root=None: _profile(
        {"CTP_USERNAME": "012345678", "CTP_PASSWORD": "pw", "CTP_BROKER_ID": "8030",
         "CTP_TD_ADDRESS": "121.13.218.19:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293"}))
    monkeypatch.setattr(p, "Collector", _FakeCollector)
    monkeypatch.setattr(p, "check_positions", lambda engine, wait, early_exit, collector=None: ([], True))
    monkeypatch.setattr(p, "close_engine", lambda engine: None)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    monkeypatch.setattr(p, "EventEngine", lambda: SimpleNamespace(register=lambda *a: None))

    class _Engine(_FakeMainEngine):
        def get_all_positions(self):
            return [SimpleNamespace(vt_symbol="c2701.DCE", direction="???", volume=3,
                                    yd_volume=0, frozen=0)]

    monkeypatch.setattr(p, "MainEngine", lambda event_engine=None: _Engine())

    class _FakeCtpGateway:
        default_name = "CTP"

    monkeypatch.setattr("vnpy_ctp.CtpGateway", _FakeCtpGateway)
    monkeypatch.setattr(p.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        p.main([])
    out = capsys.readouterr().out
    assert "c2701.DCE 无方向 volume=3" in out
    assert "???" not in out


def test_an_unknown_broker_id_is_refused_not_widened_to_the_known_set(monkeypatch, capsys, tmp_path):
    """BrokerID 没有绑定前置表 → 拒绝,而不是退回「国信 ∪ SimNow」全集(那会破坏 BrokerID 绑定)。"""
    import probe_position as pp
    from ctp_tools.profile import Profile

    env = {"CTP_USERNAME": "u12345678", "CTP_PASSWORD": "p", "CTP_BROKER_ID": "12345",
           "CTP_TD_ADDRESS": "121.13.218.19:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293"}
    monkeypatch.setattr(pp, "load_profile", lambda profile="", root=None: Profile(env=env, hard_conflicts=[], notes=[]))
    assert pp.main([]) == pp.EXIT_CONFIG
    assert "没有绑定的可信前置地址表" in capsys.readouterr().err
