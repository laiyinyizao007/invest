"""候选独立进程路径的接线:配置开关 / evaluate_prepare 筛查放行 / evaluate_run 分流 / 框架侧子入口
(Issue #29 S1 · P2 T2.5 / T2.6)。

关着时与现状一致(默认);开着时候选**从不**在框架进程里被载入。
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys

import pytest

import research_api as api
import research_candidate_process as rcp
import research_config as rc

requires_trading_stack = pytest.mark.skipif(
    importlib.util.find_spec("vnpy_ctastrategy") is None,
    reason="需要交易栈(vnpy_ctastrategy):CI 的研究侧闸门不装交易环境")

NEW_SOURCE = '''
from candidate.signal_template import SignalTemplate


class Candidate(SignalTemplate):
    parameters = ["fast_window", "slow_window", "atr_mult", "atr_window", "breakout_n"]
    param_grid = {"breakout_n": [10, 20, 30], "fast_window": [5, 8, 10], "atr_mult": [1.5, 2.0, 2.5]}
    breakout_n = 20

    def entry_signal(self):
        return 0
'''
OLD_SOURCE = '''
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    parameters = TrendAtr15mStrategy.parameters + ["breakout_n"]
    param_grid = {"breakout_n": [10, 20, 30], "fast_window": [5, 8, 10], "atr_mult": [1.5, 2.0, 2.5]}
'''


class _FakeTTY:
    def isatty(self):
        return True

    def readline(self):
        return ""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    monkeypatch.setattr(api.rc, "ROOT", tmp_path)
    monkeypatch.setattr(api.sys, "stdin", _FakeTTY())
    monkeypatch.setattr(api, "_prompt_hash", lambda sha: sha)
    return tmp_path


def set_flag(monkeypatch, value: bool):
    real = api.rc.load

    def load(*a, **k):
        cfg = real(*a, **k)
        cfg["isolation"]["candidate_process"] = value
        return cfg

    monkeypatch.setattr(api.rc, "load", load)


def write_strategy(tree, source: str):
    path = tree / "pending_strategies" / "s.py"
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 配置开关


def test_flag_defaults_to_off():
    assert rc.DEFAULTS["isolation"]["candidate_process"] is False
    assert rc.load()["isolation"]["candidate_process"] is False


@pytest.mark.parametrize("bad", ["yes", 1, None, [], {}])
def test_flag_must_be_a_real_boolean(tmp_path, bad):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"isolation": {"candidate_process": bad}}), encoding="utf-8")
    with pytest.raises(rc.ConfigError):
        rc.load(cfg)


def test_unknown_isolation_keys_are_refused(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"isolation": {"candidate_process": False, "sneaky": True}}), encoding="utf-8")
    with pytest.raises(rc.ConfigError):
        rc.load(cfg)


def test_the_flag_cannot_be_set_by_a_caller():
    """开关只来自配置文件:评估入口的签名里没有它,CLI 子入口也没有对应参数。"""
    for func in (api.evaluate_prepare, api.evaluate_job):
        names = set(inspect.signature(func).parameters)
        assert not {n for n in names if "isolation" in n or "candidate_process" in n}, func.__name__
    parser = api.build_parser()
    for argv in (["evaluate", "--strategy", "s.py", "--class", "S", "--candidate-process"],
                 ["_evaluate_signal_child", "--file", "f", "--cls", "S", "--random-runs", "5", "--sha256", "a",
                  "--candidate-process"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# ---------------------------------------------------------------- evaluate_prepare:筛查放行只在开关开时


def test_prepare_refuses_the_new_format_when_the_flag_is_off(tree, monkeypatch):
    set_flag(monkeypatch, False)
    write_strategy(tree, NEW_SOURCE)
    with pytest.raises(api.JobError, match="candidate"):
        api.evaluate_prepare(strategy="s.py", cls="Candidate", confirm=True)


def test_prepare_accepts_the_new_format_when_the_flag_is_on_and_records_it_in_ctx(tree, monkeypatch):
    set_flag(monkeypatch, True)
    write_strategy(tree, NEW_SOURCE)
    ctx = api.evaluate_prepare(strategy="s.py", cls="Candidate", confirm=True)
    assert ctx["candidate_process"] is True


def test_prepare_still_screens_everything_else_when_the_flag_is_on(tree, monkeypatch):
    """放行的只有 `candidate` 这个导入根:别的危险写法照旧被拦。"""
    set_flag(monkeypatch, True)
    write_strategy(tree, NEW_SOURCE + "\nimport os\n")
    with pytest.raises(api.JobError, match="禁止导入 os"):
        api.evaluate_prepare(strategy="s.py", cls="Candidate", confirm=True)


def test_prepare_prepays_the_same_trials_whichever_path(tree, monkeypatch):
    """两条路径预扣同样的试验数:新路径不得因此少记。"""
    counts = {}
    for flag, source in ((False, OLD_SOURCE), (True, NEW_SOURCE)):
        set_flag(monkeypatch, flag)
        write_strategy(tree, source)
        before = api.prior_trials(api.settings()[1])
        # 批准走 prepare 自己的 confirm 路径(假 TTY + 假「人粘回哈希」),本文件不直接调判定点
        api.evaluate_prepare(strategy="s.py", cls="Candidate", confirm=True)
        counts[flag] = api.prior_trials(api.settings()[1]) - before
    assert counts[False] == counts[True] == api.MIN_GRID_CELLS


# ---------------------------------------------------------------- evaluate_run:分流


def _fake_result():
    return {"passed": False, "failures": ["x"], "params": {}, "selection": {"n_cells": 27}, "metrics": {}}


@pytest.mark.parametrize("flag, child, marked", [(False, "_evaluate_child", False), (True, "_evaluate_signal_child", True)])
def test_evaluate_run_picks_the_child_by_the_flag(tree, monkeypatch, flag, child, marked):
    set_flag(monkeypatch, flag)
    write_strategy(tree, NEW_SOURCE if flag else OLD_SOURCE)
    seen = {}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: seen.update(cmd=cmd) or _fake_result())
    result = api.evaluate_job(strategy="s.py", cls="Candidate", confirm=True)
    assert child in seen["cmd"] and ("_evaluate_signal_child" in seen["cmd"]) is flag
    assert ("isolation" in result) is marked
    assert result.get("isolation", "candidate-process") == "candidate-process"


def test_signal_child_parser_has_the_same_arguments_as_the_old_child():
    parser = api.build_parser()
    args = parser.parse_args(["_evaluate_signal_child", "--file", "f", "--cls", "S", "--random-runs", "50",
                              "--sha256", "abc", "--product", "V15", "--prior-trials", "9"])
    assert (args.random_runs, args.sha256, args.product, args.prior_trials) == (50, "abc", "V15", 9)


# ---------------------------------------------------------------- 框架侧子入口


def _write_bars_csv(path):
    path.write_text("datetime,open,high,low,close,volume,open_interest\n"
                    "2023-01-03 09:00:00,10,11,9,10.5,100,1000\n"
                    "2023-01-03 09:15:00,10,11,9,10.5,100,1000\n", encoding="utf-8")


@pytest.fixture
def child_env(monkeypatch, tmp_path):
    import research_gate
    import walk_forward_optimizer as wf
    csv = tmp_path / "bars.csv"
    _write_bars_csv(csv)
    monkeypatch.setitem(wf.PRODUCTS, "C15", (csv, wf.PRODUCTS["C15"][1]))
    cand = tmp_path / "cand.py"
    monkeypatch.setattr(api, "child_file", lambda p: cand)
    monkeypatch.setattr(api, "child_approved", lambda sha: None)
    monkeypatch.setattr(api, "child_int_args", lambda a: (1, 1))
    monkeypatch.setattr(api, "import_candidate", lambda *a, **k: pytest.fail("候选独立进程路径不得在框架进程里载入候选"))
    return cand, research_gate


def _child_args(sha: str, cls: str = "Candidate"):
    return api.build_parser().parse_args(["_evaluate_signal_child", "--file", "x.py", "--cls", cls,
                                          "--random-runs", "1", "--sha256", sha])


def _printed_result(capsys):
    line = next(ln for ln in reversed(capsys.readouterr().out.splitlines()) if ln.startswith(api.CHILD_MARK))
    return json.loads(line[len(api.CHILD_MARK):])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@requires_trading_stack
def test_signal_child_never_loads_the_candidate_and_hands_the_proxy_to_the_evaluation(child_env, monkeypatch, capsys):
    cand, research_gate = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"passed": False, "failures": ["x"], "trials": {"prior": 1, "grid_cells": 27, "total": 28}}

    monkeypatch.setattr(research_gate, "run_evaluation", fake_run)
    before = set(sys.modules)
    code = api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE)))
    assert code == api.EXIT_OK
    assert _printed_result(capsys)["trials"]["grid_cells"] == 27
    proxy = captured["candidate_cls"]
    assert proxy.__name__ == "Candidate" and proxy.__module__ == "candidate_proxy"
    assert proxy.param_grid["breakout_n"] == [10, 20, 30] and all(isinstance(v, int) for v in proxy.param_grid["breakout_n"])
    assert callable(captured["runner"])
    new = set(sys.modules) - before
    assert not {m for m in new if m == "candidate" or m.startswith("candidate.") or m == "candidate_code"}, \
        "框架进程里出现了候选侧模块:候选被载入了"


@requires_trading_stack
def test_signal_child_refuses_when_the_file_is_not_the_approved_bytes(child_env, capsys):
    cand, _ = child_env
    cand.write_text(NEW_SOURCE + "\n# tampered\n", encoding="utf-8")
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_REFUSED
    assert "哈希不符" in capsys.readouterr().err


@requires_trading_stack
@pytest.mark.parametrize("source, cls, why", [
    (OLD_SOURCE, "Candidate", "旧格式候选(继承框架模板)在新路径上不被接受"),
    (NEW_SOURCE.replace('param_grid = {"breakout_n": [10, 20, 30], "fast_window": [5, 8, 10], "atr_mult": [1.5, 2.0, 2.5]}',
                        "param_grid = dict(a=[1, 2, 3])"), "Candidate", "动态网格读不出 ⇒ fail-closed"),
    (NEW_SOURCE, "Other", "--cls 与源码里的类名不符"),
])
def test_signal_child_turns_unreadable_declarations_into_a_structured_error(child_env, capsys, source, cls, why):
    cand, _ = child_env
    cand.write_bytes(source.encode("utf-8"))
    assert api.run_evaluate_signal_child(_child_args(_sha(source), cls)) == api.EXIT_OK, why
    err = _printed_result(capsys)["error"]
    assert err["type"] == "SpecError" and err["message"], why


@requires_trading_stack
def test_signal_child_sanitises_candidate_written_text_but_keeps_framework_failures_fixed(child_env, monkeypatch, capsys):
    cand, research_gate = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))

    def raising(kind, detail=""):
        def fake_run(**kwargs):
            raise rcp.CandidateProcessError(kind, "框架的固定说明", detail=detail)
        return fake_run

    monkeypatch.setattr(research_gate, "run_evaluation", raising("candidate_error", "ValueError: C:\\secret\\path boom"))
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_OK
    err = _printed_result(capsys)["error"]
    assert err["type"] == "ValueError" and "C:\\secret" not in err["message"]

    monkeypatch.setattr(research_gate, "run_evaluation", raising("timeout", "候选写的文字不该出现"))
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_OK
    err = _printed_result(capsys)["error"]
    assert err == {"type": "CandidateProcessError", "message": "框架的固定说明"}


@requires_trading_stack
def test_signal_child_does_its_own_checks_before_doing_anything(child_env, monkeypatch, capsys):
    """子入口是独立的信任边界:没批准就拒绝,不读候选也不起进程。"""
    cand, _ = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))

    def not_approved(sha):
        raise api.JobError(api.EXIT_REFUSED, api.NEEDS_APPROVAL)

    monkeypatch.setattr(api, "child_approved", not_approved)
    monkeypatch.setattr(rcp, "run_candidate_signals", lambda *a, **k: pytest.fail("未批准不得起候选进程"))
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_REFUSED
    assert api.NEEDS_APPROVAL in capsys.readouterr().err


# ---------------------------------------------------------------- 类体默认值:读不懂就拒绝,不悄悄丢掉(G8 第一轮前自查发现)


# 框架模板认识的参数名(取自 TrendAtr15mStrategy.parameters 里与指标窗口、止损距离有关的几项);
# 纯函数测试用字面量名单,免得研究侧 CI(无交易栈)导入不了真模板
FRAMEWORK_PARAMETER_NAMES = ("fast_window", "slow_window", "atr_mult", "atr_window")


def _src_with_body(extra: str) -> str:
    return NEW_SOURCE.replace("    breakout_n = 20\n", "    breakout_n = 20\n" + extra, 1)


@pytest.mark.parametrize("extra, why", [
    ("    atr_mult: float = 2.5\n", "带类型注解的字面量默认值"),
    ("    atr_mult = -2.5\n", "负数字面量(一元运算,不是 Constant)"),
])
def test_class_body_defaults_written_in_other_literal_forms_are_still_seen(extra, why):
    import research_candidate_spec as spec_mod
    spec = spec_mod.parse_spec(_src_with_body(extra))
    assert spec.defaults.get("atr_mult") in (2.5, -2.5), why
    assert "atr_mult" not in spec.nonliteral


@pytest.mark.parametrize("extra", [
    "    atr_mult = 1.5 * 2\n",
    "    atr_mult = ATR_BASE\n",
    "    slow_window: int = 10 * 6\n",
])
def test_non_literal_default_for_a_framework_parameter_is_refused_not_dropped(extra):
    """框架参数(止损距离、指标窗口)的类体默认值读不出字面量 ⇒ 拒绝评估。

    悄悄丢掉会让框架侧用模板默认值,而旧路径里候选类属性直接生效:同一份源码两条路径的止损不同、且没有任何报错。"""
    import research_candidate_spec as spec_mod
    spec = spec_mod.parse_spec(_src_with_body(extra))
    with pytest.raises(spec_mod.SpecError, match="字面量"):
        spec_mod.check_framework_defaults(spec, FRAMEWORK_PARAMETER_NAMES)


def test_non_literal_attribute_that_is_not_a_framework_parameter_is_fine():
    import research_candidate_spec as spec_mod
    spec = spec_mod.parse_spec(_src_with_body("    helper_table = [1, 2, 3]\n    breakout_limit = 10 * 2\n"))
    spec_mod.check_framework_defaults(spec, FRAMEWORK_PARAMETER_NAMES)       # 不抛


@requires_trading_stack
def test_signal_child_refuses_a_non_literal_framework_default_as_a_structured_error(child_env, capsys):
    cand, _ = child_env
    source = _src_with_body("    atr_mult = 1.5 * 2\n")
    cand.write_bytes(source.encode("utf-8"))
    assert api.run_evaluate_signal_child(_child_args(_sha(source))) == api.EXIT_OK
    err = _printed_result(capsys)["error"]
    assert err["type"] == "SpecError" and "atr_mult" in err["message"]


@requires_trading_stack
@pytest.mark.parametrize("extra, why", [
    ("\nimport os\n", "禁用模块"),
    ("\nimport subprocess\n", "禁用模块"),
    ("\nx = eval('1')\n", "禁用调用"),
])
def test_signal_child_rescreens_the_approved_bytes_like_the_old_child_does(child_env, capsys, extra, why):
    """子入口是独立的信任边界:旧子入口在 import_candidate 里**再筛查一遍**(批准与筛查是两道独立的闸),
    新子入口不得比它弱——人批准过的代码若过不了静态筛查,也不得被交给候选进程。"""
    cand, _ = child_env
    source = NEW_SOURCE + extra
    cand.write_bytes(source.encode("utf-8"))
    assert api.run_evaluate_signal_child(_child_args(_sha(source))) == api.EXIT_REFUSED, why
    assert "静态筛查" in capsys.readouterr().err


@requires_trading_stack
def test_signal_child_screening_allows_the_candidate_import_root_only(child_env, monkeypatch, capsys):
    """放行侧:正常的新格式候选能过子入口里的这道筛查(`candidate` 导入根被放行,别的照旧拦)。"""
    cand, research_gate = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))
    monkeypatch.setattr(research_gate, "run_evaluation", lambda **kw: {"passed": False, "failures": [], "trials": None})
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_OK
    assert "error" not in _printed_result(capsys)


@requires_trading_stack
def test_signal_child_cls_mismatch_reports_a_fixed_text_not_the_caller_supplied_name(child_env, capsys):
    """`--cls` 来自调用方(最终来自模型/工作流):回给模型的说明里不得回显它。"""
    cand, _ = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE), "EVIL_CLS_QZX")) == api.EXIT_OK
    err = _printed_result(capsys)["error"]
    assert err["type"] == "SpecError" and "EVIL_CLS_QZX" not in err["message"]


@requires_trading_stack
def test_signal_child_spec_error_message_is_the_public_text(child_env, capsys):
    cand, _ = child_env
    source = _src_with_body("    EVIL_IDENT_QZX = 1\n    EVIL_IDENT_QZX = 2\n")
    cand.write_bytes(source.encode("utf-8"))
    assert api.run_evaluate_signal_child(_child_args(_sha(source))) == api.EXIT_OK
    captured = capsys.readouterr()
    result = json.loads(next(ln for ln in reversed(captured.out.splitlines()) if ln.startswith(api.CHILD_MARK))[len(api.CHILD_MARK):])
    assert "EVIL_IDENT_QZX" not in result["error"]["message"]
    assert "EVIL_IDENT_QZX" in captured.err                            # 完整信息留在本机 stderr(给人看)


@requires_trading_stack
def test_signal_child_reads_the_candidate_like_the_old_child_does_bom_and_unreadable(child_env, monkeypatch, capsys):
    """带 BOM 的候选在旧路径(utf-8-sig)能跑,新路径不得因此误拒;读不了文件是「拒绝」不是「失败」。"""
    cand, research_gate = child_env
    raw = b"\xef\xbb\xbf" + NEW_SOURCE.encode("utf-8")
    cand.write_bytes(raw)
    monkeypatch.setattr(research_gate, "run_evaluation", lambda **kw: {"passed": False, "failures": [], "trials": None})
    assert api.run_evaluate_signal_child(_child_args(hashlib.sha256(raw).hexdigest())) == api.EXIT_OK
    assert "error" not in _printed_result(capsys)
    cand.unlink()
    assert api.run_evaluate_signal_child(_child_args(hashlib.sha256(raw).hexdigest())) == api.EXIT_REFUSED


@requires_trading_stack
def test_signal_child_scrubs_local_paths_out_of_framework_side_failure_messages(child_env, monkeypatch, capsys):
    """框架侧失败的 message 也过统一的净化口径:即便将来某条消息里混进了本机路径,也出不去。"""
    cand, research_gate = child_env
    cand.write_bytes(NEW_SOURCE.encode("utf-8"))

    def fake_run(**kwargs):
        raise rcp.CandidateProcessError("protocol", "failed near C:\\Users\\secretname\\AppData\\x.py")

    monkeypatch.setattr(research_gate, "run_evaluation", fake_run)
    assert api.run_evaluate_signal_child(_child_args(_sha(NEW_SOURCE))) == api.EXIT_OK
    err = _printed_result(capsys)["error"]
    assert err["type"] == "CandidateProcessError" and "secretname" not in err["message"]
