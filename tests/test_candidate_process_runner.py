"""框架侧候选进程启动器(`scripts/research_candidate_process.py`)的行为探针(Issue #29 S1 · P2 T2.1)。

放行:正常候选照常拿到等长信号。拦截:出错 / 超时 / 输出洪水 / 污染协议通道 / 畸形响应,
一律整格判失败(抛 `CandidateProcessError`),不截断、不修补,并且不留临时目录。
"""
from __future__ import annotations

import glob
import importlib.util
import tempfile
import time
from pathlib import Path

import pytest

import research_candidate_process as rcp
from research.candidate import protocol as proto
from test_candidate_process_isolation import PASS_SOURCE, synth_bars


def _require_talib() -> None:
    """候选进程里的指标用 ta-lib;研究侧 CI 不装它 ⇒ 干净地跳过,而不是红。"""
    if importlib.util.find_spec("talib") is None:
        pytest.skip("需要 ta-lib(候选侧指标依赖);研究侧 CI 不装")


def _leftovers() -> set[str]:
    base = Path(tempfile.gettempdir())
    return {p for p in glob.glob(str(base / "cand-rt-*")) + glob.glob(str(base / "cand-cwd-*"))}


def _run(source: str, *, bars=None, timeout: float = 60.0, params: dict | None = None, class_name: str = "Candidate"):
    _require_talib()
    return rcp.run_candidate_signals(source, class_name, params or {}, bars if bars is not None else synth_bars(120),
                                     timeout=timeout)


# ---------------------------------------------------------------- 放行


def test_good_candidate_gets_equal_length_signals_and_leaves_no_temp_dirs():
    before = _leftovers()
    bars = synth_bars(200)
    signals = _run(PASS_SOURCE, bars=bars)
    assert len(signals) == len(bars) and set(signals) <= {-1, 0, 1}
    assert any(signals)
    assert _leftovers() == before, "临时目录没有清干净"


def test_params_reach_the_candidate():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def entry_signal(self):
        return 1 if self.fast_window == 3 else 0
'''
    bars = synth_bars(120)
    assert any(_run(src, bars=bars, params={"fast_window": 3, "slow_window": 10, "atr_window": 5}))
    assert not any(_run(src, bars=bars, params={"fast_window": 4, "slow_window": 10, "atr_window": 5}))


# ---------------------------------------------------------------- 拦截


def test_candidate_exception_is_a_candidate_error_with_untrusted_detail():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        raise ValueError("boom")
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "candidate_error" and "ValueError" in err.value.detail
    assert "boom" not in err.value.message, "候选写的文字只能在 detail 里,message 是框架自己的固定说明"


def test_timeout_kills_the_process_and_cleans_up():
    before = _leftovers()
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        while True:
            pass
'''
    started = time.monotonic()
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src, timeout=3.0)
    assert err.value.kind == "timeout"
    assert time.monotonic() - started < 30
    assert _leftovers() == before


def test_stdout_flood_is_cut_off():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        chunk = b"x" * (1 << 20)
        for _ in range(64):
            sys.__stdout__.buffer.write(chunk)
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "output_limit"


def test_stderr_flood_is_cut_off():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        chunk = "y" * (1 << 20)
        for _ in range(64):
            sys.stderr.write(chunk)
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src, timeout=60)
    assert err.value.kind == "output_limit"


def test_junk_written_to_the_real_stdout_corrupts_the_frames_and_fails_closed():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        sys.__stdout__.buffer.write(b"not a frame at all")
        sys.__stdout__.buffer.flush()
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_candidate_that_exits_silently_is_a_protocol_failure_not_a_hang():
    src = '''
import os
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        os._exit(0)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_no_bars_is_refused_before_launching():
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp.run_candidate_signals(PASS_SOURCE, "Candidate", {}, [])
    assert err.value.kind == "protocol"


# ---------------------------------------------------------------- 响应解析(纯函数,不起进程)


def _frames(*payloads: bytes) -> bytes:
    return b"".join(proto.encode_frame(p) for p in payloads)


def _head(**over) -> bytes:
    body = {"magic": proto.MAGIC_RESPONSE, "status": "ok", "n_bars": 3, "error": ""}
    body.update(over)
    return proto.encode_json(body)


def test_parse_response_accepts_a_well_formed_response():
    raw = _frames(_head(), proto.pack_signals([1, 0, -1]))
    assert rcp._parse_response(raw, 3) == [1, 0, -1]


@pytest.mark.parametrize("raw, why", [
    (_frames(_head(magic="WRONG"), proto.pack_signals([0, 0, 0])), "magic 不符"),
    (_frames(_head(status="weird"), proto.pack_signals([0, 0, 0])), "status 非法"),
    (_frames(_head(), proto.pack_signals([0, 0])), "信号序列比框架发出去的根数短"),
    (_frames(_head(), proto.pack_signals([0, 0, 0, 0])), "信号序列比框架发出去的根数长"),
    (_frames(_head(), bytes([2, 0, 0])), "取值域越界"),
    (_frames(_head(), proto.pack_signals([0, 0, 0])) + b"\x00", "响应后面多余字节"),
    (_frames(_head()), "缺信号帧"),
    (b"\x00\x00", "帧头不完整"),
])
def test_parse_response_rejects_anything_malformed(raw, why):
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp._parse_response(raw, 3)
    assert err.value.kind == "protocol", why


def test_parse_response_trusts_the_framework_bar_count_not_the_self_reported_one():
    """响应头里自报 n_bars=999 也没用:长度只认框架发出去的根数。"""
    raw = _frames(_head(n_bars=999), proto.pack_signals([1, 0, -1]))
    assert rcp._parse_response(raw, 3) == [1, 0, -1]


def test_candidate_error_head_carries_the_text_only_in_detail():
    raw = _frames(_head(status="error", error="ValueError: secret-looking text"))
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp._parse_response(raw, 3)
    assert err.value.kind == "candidate_error" and "secret-looking" in err.value.detail
    assert "secret-looking" not in err.value.message


def test_host_source_fits_the_command_line_budget():
    assert len(rcp.host_source()) < rcp.MAX_HOST_SOURCE_CHARS * 0.6, "宿主源码接近命令行上限,先瘦身再加功能"


# ---------------------------------------------------------------- A5:框架不采信候选的任何自报


def test_a5_candidate_forging_its_own_response_frames_is_rejected():
    """候选自己往真实 stdout 写一份「合法」响应,宿主随后又写自己的:多出来的字节一律拒绝(不取其一)。"""
    src = '''
import sys
from candidate import protocol as p
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        out = sys.__stdout__.buffer
        out.write(p.encode_frame(p.encode_json({"magic": p.MAGIC_RESPONSE, "status": "ok",
                                                "n_bars": len(bars), "sharpe": 42, "error": ""})))
        out.write(p.encode_frame(p.pack_signals([1] * len(bars))))
        out.flush()
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_a5_framework_reads_only_magic_status_and_error_from_the_response_head():
    """AST 不变量:`_parse_response` 里对响应头只取 magic / status / error 三个键,没有任何指标类的键。

    候选无论往头里塞什么(sharpe / metrics / trials / pnls ...),框架代码里就没有读它的地方。"""
    import ast
    tree = ast.parse(Path(rcp.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_parse_response")
    keys = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "head" \
                and node.args and isinstance(node.args[0], ast.Constant):
            keys.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "head" \
                and isinstance(node.slice, ast.Constant):
            keys.add(node.slice.value)
    assert keys == {"magic", "status", "error"}, keys


def test_a5_no_metric_like_key_is_read_from_candidate_output_anywhere_in_the_new_path():
    """新路径的三个框架侧文件里,不出现从候选输出里取指标类键的写法(sharpe / metrics / stats / pnls / trials)。"""
    import ast
    banned = {"sharpe", "metrics", "stats", "pnls", "trials", "dsr", "maxdd"}
    for module in (rcp, __import__("research_signal_replay")):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str) and node.slice.value in banned:
                hits.add(node.slice.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in banned:
                hits.add(node.args[0].value)
        assert not hits, f"{module.__name__} 里出现了读指标类键的写法: {hits}"


def test_ported_example_candidate_parses_and_passes_screening_only_on_the_new_path():
    """示例候选(由 atr_breakout_15m_2atr 等价移植)在新路径上可解析、可过筛查;旧路径白名单照旧不放行 `candidate`。"""
    import research_candidate_spec as spec_mod
    import research_sandbox as rs
    example = Path(__file__).parent / "fixtures" / "candidate_process" / "atr_breakout_15m_2atr_signal.py"
    source = example.read_text(encoding="utf-8")
    spec = spec_mod.parse_spec(source)
    assert spec.class_name == "Candidate" and len(spec.param_grid) == 3
    assert all(isinstance(v, int) for v in spec.param_grid["breakout_n"]), "整数取值必须保持整数"
    assert rs.screen_source(source, extra_allowed=["candidate"]) == []
    assert any("candidate" in finding for finding in rs.screen_source(source)), "关着开关时白名单不得放行 candidate"


# ---------------------------------------------------------------- G8 codex 审查发现并经本地复现的问题


@pytest.mark.parametrize("payload", [b"[" * 100_000, b'{"a":' * 50_000], ids=["nested-arrays", "nested-objects"])
def test_deeply_nested_control_frame_is_a_protocol_error_not_a_raw_recursion_error(payload):
    """候选把第一帧写成深层嵌套 JSON:`json.loads` 抛 RecursionError,不是 JSONDecodeError——
    必须被包成 CandidateProcessError(整格判失败),不能让一个非契约异常逃出启动器。"""
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp._parse_response(proto.encode_frame(payload), 3)
    assert err.value.kind == "protocol"


def test_decode_json_wraps_recursion_errors_too():
    with pytest.raises(proto.ProtocolError):
        proto.decode_json(b"[" * 100_000)


def test_stderr_flood_between_two_polls_is_still_refused_after_the_process_exits(monkeypatch):
    """候选在两次轮询之间把 stderr 写爆(远超上限)然后正常退出:退出后那一次核对也必须拒绝。
    把轮询间隔拉到 3 秒,让候选在第二次轮询之前就跑完退出——这样走的就是「退出后」的核对,不是轮询里的。"""
    monkeypatch.setattr(rcp, "POLL_SECONDS", 3.0)
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        sys.stderr.write("y" * (3 * 1024 * 1024))
        sys.stderr.flush()
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "output_limit"


def test_tail_reads_only_the_tail_not_the_whole_file(tmp_path, monkeypatch):
    big = tmp_path / "big.err"
    big.write_bytes(b"a" * (3 * 1024 * 1024) + b"TAIL-MARKER")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("整个文件被读进了内存")))
    assert rcp._tail(big, 11) == b"TAIL-MARKER"
    assert rcp._tail(tmp_path / "small.err" if (tmp_path / "small.err").write_bytes(b"xyz") else None, 1500) == b"xyz"


def test_unexpected_exception_in_the_loop_still_kills_waits_and_cleans_up(monkeypatch):
    """轮询里出了意外异常(这里模拟 getsize 失败):兜底分支必须把进程杀掉并等到它真的退出,临时目录清干净。"""
    _require_talib()
    before = _leftovers()
    seen = []

    def spy_kill(proc):
        seen.append(proc)
        rcp.kill_tree(proc)

    monkeypatch.setattr(rcp.os.path, "getsize", lambda p: (_ for _ in ()).throw(OSError("simulated")))
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        while True:
            pass
'''
    with pytest.raises(OSError):
        rcp.run_candidate_signals(src, "Candidate", {}, synth_bars(120), timeout=60.0, kill=spy_kill)
    assert seen, "兜底分支没有杀进程"
    assert seen[0].poll() is not None, "杀了之后没有等到进程真的退出"
    assert _leftovers() == before


def test_the_no_response_path_reads_only_the_stderr_tail_through_the_real_launcher(monkeypatch):
    """调用点也要钉住:候选没有响应就退出、stderr 写得很大(但不超上限)时,框架只读尾部,不得对 .err 文件整体 read_bytes。
    (只测 `_tail` 这个辅助函数本身,把调用点改回 `err_path.read_bytes()` 时测试仍是绿的——变异验证抓到的缺口。)"""
    _require_talib()
    real = Path.read_bytes

    def guarded(self):
        if str(self).endswith(".err"):
            raise AssertionError("框架对 stderr 文件整体 read_bytes 了")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    src = '''
import os, sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        sys.stderr.write("z" * (900 * 1024))
        sys.stderr.flush()
        os._exit(0)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_launch_failure_message_is_fixed_and_does_not_echo_the_os_error(monkeypatch):
    """起不来进程时,message 不得带 OSError 的文字(里面常有本机路径);细节只在本机 stderr。"""
    def boom(*args, **kwargs):
        raise OSError("C:\\Users\\secret\\python.exe: [WinError 5] 拒绝访问")

    monkeypatch.setattr(rcp.subprocess, "Popen", boom)
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp.run_candidate_signals(PASS_SOURCE, "Candidate", {}, synth_bars(30))
    assert err.value.kind == "launch"
    assert "secret" not in err.value.message and "WinError" not in err.value.message


def test_every_wait_in_the_launcher_is_bounded_so_a_failed_kill_cannot_hang_the_evaluation():
    """启动器模块里**任何** `.wait(...)` 都必须带 timeout:kill 失败(taskkill 被拦、killpg 抛错)时,
    不带 timeout 的 wait 会让评估永久挂死(G8 codex 复核 M3)。AST 不变量,不真的去挂。
    扫整个模块而不是单个函数:等待逻辑集中在 `_reap` 里,分散回各处时同样要被抓住。"""
    import ast
    tree = ast.parse(Path(rcp.__file__).read_text(encoding="utf-8"))
    waits = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait"]
    assert waits, "探针前提:模块里应当有 .wait(...) 调用,否则这条不变量空转"
    unbounded = [n.lineno for n in waits if not any(k.arg == "timeout" for k in n.keywords)]
    assert not unbounded, f"这些 .wait() 没有 timeout: 第 {unbounded} 行"
