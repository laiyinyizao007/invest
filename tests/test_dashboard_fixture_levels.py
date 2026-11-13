"""示例数据的 `state` / `level` 必须与**真实实现**算出来的一致。

这个测试曾两次给不出保护:
1. 面板校验回测统计时用了错的键集(夹具照着错的写,套件全绿也证明不了真实产出能过);
2. 批量给示例补 `level` 时按 state 推导,把**账户已硬锁定**的样例写成了 `level=ok`,
   后来又只对 3/24 份跑了真实实现——**同一类错误连栽两次**。

所以这里**逐份**跑真实实现:为每个 section 样例搭出对应的文件树,断言 state 与 level 都一致。
搭不出来的(阶段②③④尚未实现的接口)显式跳过,并在清单里注明原因。
"""
import json
import os
from pathlib import Path

import pytest

from dashboard import collectors as col
from dashboard import config as appconfig

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
FIX = WEB / "fixtures"
NOW = 1_800_000_000.0
GUARD = {"account_id": "1", "last_equity": 9870.5, "peak_equity": 10120.0, "day": "2026-11-13",
         "day_start_equity": 10000.0, "daily_locked_day": None, "hard_locked": False, "hard_reason": "",
         "sampled_at": "2027-01-15T16:00:00+08:00"}
HEARTBEAT = "1800000000"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(root):
    return appconfig.load(path=root / "none.json", root=root)


def _set_mtimes(root, fresh: bool):
    """把造出来的文件的时间戳设成『新鲜』或『很旧』——样例的 state 就是这么区分的。"""
    stamp = NOW if fresh else NOW - 30 * 86400
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


def _state_files(root, guard=None, heartbeat=HEARTBEAT):
    guard = dict(GUARD, **(guard or {}))
    _write(root, "data/state/account_guard.json", json.dumps(guard))
    if heartbeat is not None:
        _write(root, "data/state/heartbeat", heartbeat)


def _log(root, day, lines):
    _write(root, f"runtime/vntrader/log/vt_{day}.log", "".join(lines))


# 日志的**日期必须与文件名一致**(否则首条记录日期对不上 → 日志不可信)
CLEAN_LOG = ["2026-11-12 09:00:00 | INFO | R | 已启动\n"]
SEVERE_LOG = ["2026-11-12 09:00:00 | INFO | R | 已启动\n", "2026-11-12 10:00:00 | ERROR | R | 硬锁定\n"]
# 录制心跳相对 NOW(写死日期要么变成 60 天前 → stale,要么在未来 → corrupt)
from datetime import datetime, timezone                                   # noqa: E402
RECORDING_HEARTBEAT = datetime.fromtimestamp(NOW - 2, timezone.utc).isoformat()


# ---------------------------------------------------------------- trading
def _trading_scenarios():
    return {
        "trading-ok": lambda root: _state_files(root),
        "trading-paused": lambda root: (_state_files(root), _write(root, "data/state/pause_trading.flag", "")),
        "trading-hard-locked": lambda root: _state_files(root, {"hard_locked": True, "hard_reason": "累计回撤"}),
        "trading-missing": lambda root: None,
        "trading-corrupt": lambda root: _write(root, "data/state/account_guard.json", "{broken"),
        "trading-stale": lambda root: _state_files(root, heartbeat="1799000000"),
        "trading-guard-stale": lambda root: _state_files(root, {"sampled_at": "2026-12-01T09:00:00+08:00"}),
    }


def _trading_case(tmp_path, name):
    root = _root(tmp_path)
    _trading_scenarios()[name](root)
    return col.collect_trading(_settings(root), NOW)


# ---------------------------------------------------------------- days / alerts
def _build_days_case(root, name):
    if name == "days-sample":
        _log(root, "20261112", SEVERE_LOG)
        _log(root, "20261111", ["2026-11-11 09:00:00 | INFO | R | 已启动\n"])
        # 11-10 的日志尾部停在多字节字符中间 → 不可信(样例里就是 trusted=false 的那一天)
        truncated = "2026-11-10 09:00:00 | INFO | R | 已启动\n2026-11-10 10:00:00 | ERROR | R | 硬锁定".encode("utf-8")[:-2]
        _write(root, "runtime/vntrader/log/vt_20261110.log", truncated.decode("utf-8", errors="ignore"))
    elif name == "alerts-events":
        _log(root, "20261112", SEVERE_LOG)
    else:
        _log(root, "20261112", CLEAN_LOG)


# ---------------------------------------------------------------- recording
def _build_recording_case(root, name):
    if name == "recording-missing":
        return
    body = {"status": "recording", "heartbeat_at": RECORDING_HEARTBEAT, "symbols": ["C2701.DCE"]}
    if name == "recording-stalled":
        body["status"] = "stalled"
    _write(root, ".ops/recording/status.json", json.dumps(body))


# ---------------------------------------------------------------- research / evolution
def _build_research_case(root, name):
    (root / "config").mkdir(exist_ok=True)
    _write(root, "config/research_sandbox.json", json.dumps({
        "iteration": {"max_iterations": 5, "window_hours": 24},
        "paths": {"pending_dir": "pending_strategies", "results_dir": "data/research",
                  "approvals_file": "data/research-control/approvals.json"}}))
    if name == "research-sample":
        _write(root, "data/research/20261112-210000-000000.json", json.dumps({
            "kind": "gate", "strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r",
            "ran_at": "2026-11-12T21:00:00", "run_id": "20261112-210000-000000",
            "passed": True, "failures": []}))


# ---------------------------------------------------------------- 逐份比对
TRADING_FIXTURES = list(_trading_scenarios())
OTHER_FIXTURES = ["days-sample", "alerts-events", "alerts-empty", "recording-running", "recording-stalled",
                  "recording-missing", "research-sample"]
# 阶段②③④⑤ 尚未实现的接口(没有后端实现,跑不出真实响应):显式跳过并注明原因
SKIPPED = {
    "equity-sample": "阶段② 权益曲线接口未实现",
    "live-sample": "阶段② 实时接口未实现",
    "live-stale": "阶段② 实时接口未实现",
    "live-missing": "阶段② 实时接口未实现",
    "data-segments": "阶段④ 数据段接口未实现",
    "drills-sample": "阶段④ 演练接口未实现",
    "config-view": "阶段④ 配置接口未实现",
    "nodes-sample": "阶段⑤ 节点接口未实现",
    "audit-sample": "阶段③ 审计接口未实现",
    "recording-quality": "阶段④ 录制质量接口未实现",
}


@pytest.mark.parametrize("name", TRADING_FIXTURES)
def test_each_trading_fixture_matches_what_the_implementation_returns(tmp_path, name):
    fixture = _load(name)
    r = _trading_case(tmp_path, name)
    assert fixture["state"] == r.section["state"], (name, fixture["state"], r.section["state"])
    assert fixture["level"] == r.section["level"], (name, fixture["level"], r.section["level"])


def _fresh_from_fixture(name, root):
    """按样例自己的 state 决定文件的新鲜度:stale 的样例文件要旧,其余保持新鲜。"""
    _set_mtimes(root, fresh=_load(name)["state"] != "stale")


@pytest.mark.parametrize("name", OTHER_FIXTURES)
def test_each_other_fixture_matches_what_the_implementation_returns(tmp_path, name):
    fixture = _load(name)
    root = _root(tmp_path)
    kind = name.split("-")[0]
    if kind in ("days", "alerts"):
        _build_days_case(root, name)
    elif kind == "recording":
        _build_recording_case(root, name)
    else:
        _build_research_case(root, name)
    _fresh_from_fixture(name, root)                      # 时间戳要在采集前设好
    settings = _settings(root)
    if kind == "days":
        r = col.collect_days(settings, NOW)
    elif kind == "alerts":
        r = col.collect_alerts(settings, NOW)
    elif kind == "recording":
        r = col.collect_recording(settings, NOW)
    else:
        r = col.collect_research(settings, NOW)
    assert fixture["state"] == r.section["state"], (name, fixture["state"], r.section["state"])
    assert fixture["level"] == r.section["level"], (name, fixture["level"], r.section["level"])


def test_the_skipped_fixtures_are_really_not_implemented():
    """跳过的样例必须**确实**没有后端实现——否则就是漏测。"""
    for name, reason in SKIPPED.items():
        assert (FIX / f"{name}.json").exists(), name
        assert reason, name
    # 确认这些接口在路由里确实不存在(阶段未上线)
    from dashboard import app as appmod
    repo = Path(__file__).resolve().parents[1]
    settings = appconfig.load(path=repo / "none.json", root=repo)
    paths = {r.path for r in appmod.create_app(settings, "tok").routes if hasattr(r, "path")}
    for path in ("/api/live", "/api/equity", "/api/nodes", "/api/audit", "/api/data/segments", "/api/drills"):
        assert path not in paths, path


def test_every_section_fixture_carries_a_valid_level_and_state():
    for f in sorted(FIX.glob("*.json")):
        if f.stem == "manifest":
            continue
        body = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or "state" not in body:
            continue
        assert body.get("level") in ("ok", "warn", "critical", "unknown"), f.stem
        if body["state"] != "ok":
            assert body["level"] != "ok", f.stem
