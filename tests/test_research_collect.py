"""收菜(`scripts/research_collect.py`):把**已被人批准**的候选收进 `approved/`。

收菜是这个沙盒里最危险的一步——它改变的是「运行时唯一认的目录」的内容。
所以这里逐条钉住它的拒绝路径:账本外的、批准后被改过的、路径穿越的、非交互的、哈希不全的,
以及「复制出来的字节和批准的字节不是同一份」。
"""
import hashlib
import json
from pathlib import Path

import pytest

import research_config as rc
import research_collect as collect

CANDIDATE = '''from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    fast_window = 8
    slow_window = 20
    atr_mult = 2.0
'''


class FakeTTY:
    """只用来让 `sys.stdin.isatty()` 返回 True;输入走注入的 input_fn。"""

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return ""


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """一个临时的沙盒:pending/、approved/、账本都在 tmp_path 下。"""
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "approved").mkdir()
    (tmp_path / "data" / "research-control").mkdir(parents=True)
    ledger = tmp_path / "data" / "research-control" / "approvals.json"
    ledger.write_text("{}", encoding="utf-8")
    return tmp_path


def _write_candidate(root: Path, name: str = "cand.py", source: str = CANDIDATE) -> Path:
    path = root / "pending_strategies" / name
    path.write_text(source, encoding="utf-8")
    return path


def _approve(root: Path, path: Path, sha: str | None = None) -> str:
    sha = sha or hashlib.sha256(path.read_bytes()).hexdigest()
    ledger = root / "data" / "research-control" / "approvals.json"
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data[sha] = {"name": path.name, "at": "2026-11-14T00:00:00"}
    ledger.write_text(json.dumps(data), encoding="utf-8")
    return sha


def test_collect_moves_an_approved_candidate_into_approved_dir(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(sandbox)
    sha = _approve(sandbox, source)

    result = collect.collect_job("cand.py", input_fn=lambda _: sha, write=lambda _t: None)

    assert result["collected"] is True
    target = sandbox / "approved" / "cand.py"
    assert target.is_file()
    assert target.read_bytes() == source.read_bytes()


def test_collect_refuses_a_file_that_was_never_approved(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    _write_candidate(sandbox)                     # 批准账本里没有它

    with pytest.raises(collect.CollectError) as excinfo:
        collect.collect_job("cand.py", input_fn=lambda _: "0" * 64, write=lambda _t: None)

    assert "没有人工批准记录" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_collect_refuses_when_the_file_changed_after_approval(sandbox, monkeypatch):
    """TOCTOU:人批准的是 A,收菜时文件已经是 B —— 必须拒绝。"""
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(sandbox)
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    _approve(sandbox, source, original_sha)
    source.write_text(CANDIDATE + "\n# 改过了\n", encoding="utf-8")   # 内容变了

    with pytest.raises(collect.CollectError) as excinfo:
        collect.collect_job("cand.py", input_fn=lambda _: original_sha, write=lambda _t: None)

    assert "没有人工批准记录" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_collect_refuses_path_traversal(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    (sandbox / "pending_strategies" / "cand.py").write_text(CANDIDATE, encoding="utf-8")

    for bad in ("../cand.py", "sub/cand.py", "..\\cand.py", "/etc/passwd"):
        with pytest.raises(collect.CollectError):
            collect.collect_job(bad, input_fn=lambda _: "0" * 64, write=lambda _t: None)


def test_collect_refuses_a_name_outside_the_pattern(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    (sandbox / "pending_strategies" / "Camel.py").write_text(CANDIDATE, encoding="utf-8")

    with pytest.raises(collect.CollectError):
        collect.collect_job("Camel.py", input_fn=lambda _: "0" * 64, write=lambda _t: None)


def test_collect_refuses_a_missing_file(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())

    with pytest.raises(collect.CollectError):
        collect.collect_job("nope.py", input_fn=lambda _: "0" * 64, write=lambda _t: None)


def test_collect_refuses_when_stdin_is_not_a_tty(sandbox, monkeypatch):
    """非交互调用不得把代码收进运行时认的目录(任何程序都能喂一个 y)。"""
    source = _write_candidate(sandbox)
    sha = _approve(sandbox, source)
    monkeypatch.setattr(collect.sys, "stdin", pytest.importorskip("io").StringIO())

    with pytest.raises(collect.CollectError) as excinfo:
        collect.collect_job("cand.py", input_fn=lambda _: sha, write=lambda _t: None)

    assert "交互式终端" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_collect_refuses_a_wrong_or_incomplete_hash(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(sandbox)
    sha = _approve(sandbox, source)

    for bad in ("y", "", sha[:32], sha.upper() + "0"):
        with pytest.raises(collect.CollectError) as excinfo:
            collect.collect_job("cand.py", input_fn=lambda _, b=bad: b, write=lambda _t: None)
        assert "哈希不匹配" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_collect_detects_a_copy_that_is_not_byte_identical(sandbox, monkeypatch):
    """写入后复算:复制出来的字节必须与批准的字节一致,不一致就删掉并拒绝。"""
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(sandbox)
    sha = _approve(sandbox, source)

    def broken_write(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload + b"# tampered\n")

    monkeypatch.setattr(collect, "_atomic_write", broken_write)

    with pytest.raises(collect.CollectError) as excinfo:
        collect.collect_job("cand.py", input_fn=lambda _: sha, write=lambda _t: None)

    assert "写入后校验不一致" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_collect_refuses_a_candidate_that_fails_the_static_screen(sandbox, monkeypatch):
    """收菜时再筛一遍:账本命中只保证「这份字节被批准过」,不保证「它符合沙盒规则」。"""
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(
        sandbox,
        source=CANDIDATE + "\n    def entry_signal(self) -> int:\n"
                "        self.guard.max_losses = 99\n        return 0\n",
    )
    sha = _approve(sandbox, source)

    with pytest.raises(collect.CollectError) as excinfo:
        collect.collect_job("cand.py", input_fn=lambda _: sha, write=lambda _t: None)

    assert "静态筛查未通过" in excinfo.value.message
    assert not (sandbox / "approved" / "cand.py").exists()


def test_list_reports_the_ledger_state(sandbox, monkeypatch):
    monkeypatch.setattr(collect.sys, "stdin", FakeTTY())
    source = _write_candidate(sandbox)
    sha = _approve(sandbox, source)
    collect.collect_job("cand.py", input_fn=lambda _: sha, write=lambda _t: None)
    # 收进来之后再改 approved/ 里的文件:账本就对不上了
    (sandbox / "approved" / "cand.py").write_text("# 事后被改\n", encoding="utf-8")

    listed = collect.list_job(write=lambda _t: None)

    assert [e["name"] for e in listed["entries"]] == ["cand.py"]
    assert listed["entries"][0]["approved"] is False


def test_the_gateway_cannot_reach_the_collect_module():
    """收菜必须**不可**从网关触达:网关 import 得到 research_api,但不该 import 得到收菜。"""
    gateway = (Path(__file__).resolve().parents[1] / "scripts" / "research_gateway.py").read_text(
        encoding="utf-8")
    assert "research_collect" not in gateway
    assert "collect" not in {route[1] for route in []}     # 网关的端点白名单里没有收菜
