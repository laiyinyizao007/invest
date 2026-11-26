"""框架侧候选进程启动器(`scripts/research_candidate_process.py`)的行为探针(Issue #29 S1 · P2 T2.1)。

放行:正常候选照常拿到等长信号。拦截:出错 / 超时 / 输出洪水 / 污染协议通道 / 畸形响应,
一律整格判失败(抛 `CandidateProcessError`),不截断、不修补,并且不留临时目录。
"""
from __future__ import annotations

import glob
import tempfile
import time
from pathlib import Path

import pytest

import research_candidate_process as rcp
from research.candidate import protocol as proto
from test_candidate_process_isolation import PASS_SOURCE, synth_bars


def _leftovers() -> set[str]:
    base = Path(tempfile.gettempdir())
    return {p for p in glob.glob(str(base / "cand-rt-*")) + glob.glob(str(base / "cand-cwd-*"))}


def _run(source: str, *, bars=None, timeout: float = 60.0, params: dict | None = None, class_name: str = "Candidate"):
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
