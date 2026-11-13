"""ADR『状态页』的硬性承诺,变成自动化检查:
交易路径对面板零依赖;面板不碰 vnpy / trading / .env;Web 依赖与交易环境隔离;上游 MIT 声明在位;入口真实可启动。"""
import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
FORBIDDEN_TOP = {"vnpy", "vnpy_ctp", "trading", "bootstrap", "scripts", "strategies"}
PY_FILES = sorted(DASH.glob("*.py")) + [ROOT / "evidence" / "logstats.py"]


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_dashboard_modules_never_import_trading_side_code(path):
    assert not (imports_of(path) & FORBIDDEN_TOP), path.name


def test_importing_the_app_in_a_fresh_process_loads_no_trading_side_modules():
    pytest.importorskip("fastapi")
    code = ("import sys; sys.path.insert(0, r'%s'); import dashboard.app; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in %r); print('BAD', bad)" % (ROOT, FORBIDDEN_TOP | {"ctp_tools"}))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().endswith("BAD []"), out.stdout


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_dashboard_never_reads_env_files_or_the_process_environment(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"dotenv|os\.environ|os\.getenv|getenv\(|open\([^)]*\.env|\"\.env\"|'\.env'", text), path.name


def test_web_dependencies_stay_out_of_the_trading_environment():
    for name in ("requirements.txt", "Dockerfile", "docker-compose.yml", "docker-compose.research.yml"):
        p = ROOT / name
        if p.exists():
            assert not re.search(r"fastapi|uvicorn|starlette", p.read_text(encoding="utf-8"), re.I), name
    req = (ROOT / "requirements-dashboard.txt").read_text(encoding="utf-8")
    assert "fastapi" in req and "uvicorn" in req


def test_upstream_mit_notice_is_present_with_copyright_and_hash():
    notice = (DASH / "NOTICE-vnpy_webtrader.md").read_text(encoding="utf-8")
    assert "The MIT License (MIT)" in notice and "Copyright (c) 2015-present, Xiaoyou Chen" in notice
    assert "cf5b81268767ca20069b2073a32abacfe2eac21c3b09486dec47abc171ff0b16" in notice and "1.1.0" in notice
    assert "Permission is hereby granted" in notice and "THE SOFTWARE IS PROVIDED" in notice
    assert "vnpy_webtrader" in (DASH / "app.py").read_text(encoding="utf-8")[:800]       # 派生文件头部注明来源


def _names_and_route_literals(path: Path) -> tuple[set[str], set[str]]:
    """代码里出现的标识符/导入名,以及传给 get(...) / api_route(...) 的路径字面量。注释与说明性字符串不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names, routes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {a.name.split(".")[-1] for a in node.names} | {a.asname for a in node.names if a.asname}
            if isinstance(node, ast.ImportFrom) and node.module:
                names |= set(node.module.split("."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("get", "api_route", "route") and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/"):    # dict.get("windows") 之类不是路由
                routes.add(arg.value)
    return names, routes


def test_the_upstream_dangerous_code_is_not_present_in_dashboard_code():
    banned_names = {"send_order", "cancel_order", "RpcClient", "RpcServer", "WebEngine", "OAuth2PasswordBearer", "OAuth2PasswordRequestForm",
                    "jose", "passlib", "CryptContext", "WebSocket", "WebSocketDisconnect", "websocket", "websockets", "SECRET_KEY", "create_access_token"}
    all_routes = set()
    for p in DASH.glob("*.py"):
        names, routes = _names_and_route_literals(p)
        assert not (names & banned_names), (p.name, names & banned_names)
        all_routes |= routes
    assert all_routes and not [r for r in all_routes if re.search(r"order|token|login|ws|cancel|connect|subscribe", r, re.I)], all_routes


def test_the_dangerous_code_detector_actually_detects(tmp_path):
    """检测器自检:喂一段带上游危险代码的源码,必须被发现(防止检测器本身失灵而一直绿)。"""
    bad = tmp_path / "bad.py"
    bad.write_text("\n".join([
        "from fastapi import WebSocket",
        "from vnpy.rpc import RpcClient",
        "@app.get('/order')",
        "def send_order(): pass",
        "@app.get('/token')",
        "def t(): pass",
        "",
    ]), encoding="utf-8")
    names, routes = _names_and_route_literals(bad)
    assert {"WebSocket", "RpcClient", "send_order"} <= names and "/token" in routes


# ---------------------------------------------------------------- 真实进程冒烟

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


@pytest.fixture
def server(tmp_path):
    pytest.importorskip("uvicorn")
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True)
    (st / "heartbeat").write_text(str(time.time()), encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow"}), encoding="utf-8")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "scripts" / "status_server.py"), "--root", str(tmp_path), "--config", str(cfg), "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise AssertionError(f"服务提前退出: {proc.stderr.read()[-800:]}")
            try:
                http(base + "/")
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("服务未在 10 秒内就绪")
        yield base, tmp_path, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_real_server_process_serves_the_api_with_a_generated_token(server):
    base, tmp, proc = server
    token_file = tmp / "data" / "state" / "status.token"
    assert token_file.exists()
    token = token_file.read_text(encoding="utf-8").strip()
    assert http(base + "/api/meta")[0] == 401
    status, body = http(base + "/api/meta", token)
    assert status == 200 and json.loads(body)["environment"]["label"] == "simnow"
    status, body = http(base + "/api/trading", token)
    assert status == 200 and json.loads(body)["state"] in ("unknown", "stale", "ok")
    assert http(base + "/order", token)[0] == 404 and http(base + "/docs")[0] == 404
    assert http(base + "/")[0] == 200


def test_the_server_never_prints_the_token(server):
    base, tmp, proc = server
    token = (tmp / "data" / "state" / "status.token").read_text(encoding="utf-8").strip()
    http(base + "/api/meta", token)
    proc.terminate()
    out, err = proc.communicate(timeout=10)
    assert token not in out and token not in err


def test_the_entry_point_refuses_to_start_on_a_non_loopback_host(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"host": "0.0.0.0"}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "status_server.py"), "--root", str(tmp_path), "--config", str(cfg), "--check"],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 2 and "回环" in r.stderr


def test_check_mode_validates_without_starting_or_creating_a_token(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "status_server.py"), "--root", str(tmp_path), "--check"],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0 and "配置校验通过" in r.stdout and "环境未知" in r.stdout
    assert not (tmp_path / "data" / "state" / "status.token").exists()


def test_killing_the_dashboard_leaves_the_state_files_untouched(server):
    """面板只读:无论运行多久、被怎么杀,它读过的状态文件字节都不变(交易侧看到的世界不受面板影响)。"""
    base, tmp, proc = server
    token = (tmp / "data" / "state" / "status.token").read_text(encoding="utf-8").strip()
    hb = tmp / "data" / "state" / "heartbeat"
    before = (hb.read_bytes(), hb.stat().st_mtime_ns)
    for p in ("/api/overview", "/api/trading", "/api/days", "/api/alerts"):
        http(base + p, token)
    proc.kill()
    proc.wait(timeout=10)
    assert (hb.read_bytes(), hb.stat().st_mtime_ns) == before
