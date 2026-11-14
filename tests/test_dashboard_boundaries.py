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
    token_file = tmp / "data" / "dashboard" / "status.token"
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
    token = (tmp / "data" / "dashboard" / "status.token").read_text(encoding="utf-8").strip()
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
    assert not (tmp_path / "data" / "dashboard" / "status.token").exists()


def test_killing_the_dashboard_leaves_the_state_files_untouched(server):
    """面板只读:无论运行多久、被怎么杀,它读过的状态文件字节都不变(交易侧看到的世界不受面板影响)。"""
    base, tmp, proc = server
    token = (tmp / "data" / "dashboard" / "status.token").read_text(encoding="utf-8").strip()
    hb = tmp / "data" / "state" / "heartbeat"
    before = (hb.read_bytes(), hb.stat().st_mtime_ns)
    for p in ("/api/overview", "/api/trading", "/api/days", "/api/alerts"):
        http(base + p, token)
    proc.kill()
    proc.wait(timeout=10)
    assert (hb.read_bytes(), hb.stat().st_mtime_ns) == before


def test_uvicorn_is_started_without_the_server_header():
    """不对外暴露 uvicorn 版本。"""
    assert "server_header=False" in (ROOT / "scripts" / "status_server.py").read_text(encoding="utf-8")


def test_a_real_server_does_not_send_a_server_header(server):
    base, tmp, proc = server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert r.headers.get("Server") is None


def test_the_dashboard_does_not_need_requests_or_other_trading_environment_packages():
    """审查发现:dashboard.safe 曾经 notify.webhook 间接 import requests,而 requests 不在 requirements-dashboard.txt 里,
    只装了 Web 依赖的环境会导入失败。现在 redact 在无依赖的 notify/redact.py。"""
    pytest.importorskip("fastapi")
    code = ("import sys; sys.path.insert(0, r'%s'); import dashboard.app, dashboard.collectors, dashboard.safe; "
            "print('LOADED', sorted(m for m in ('requests', 'pandas', 'akshare', 'numpy') if m in sys.modules))" % ROOT)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().endswith("LOADED []"), out.stdout


def test_every_third_party_import_in_the_dashboard_is_declared_in_requirements_dashboard():
    """面板代码用到的第三方包必须都在 requirements-dashboard.txt 里(标准库与本仓库的包除外)。"""
    declared = {re.split(r"[<>=~! #]", l.strip(), maxsplit=1)[0].lower().replace("_", "-")
                for l in (ROOT / "requirements-dashboard.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")}
    provides = {"fastapi": "fastapi", "starlette": "fastapi", "uvicorn": "uvicorn", "httpx": "httpx"}     # import 名 -> 声明的包
    stdlib = set(sys.stdlib_module_names)
    local = {"dashboard", "evidence", "notify", "risk"}
    used = set()
    for p in list(DASH.glob("*.py")) + [ROOT / "scripts" / "status_server.py"]:
        used |= imports_of(p)
    third = {m for m in used if m not in stdlib and m not in local and m != "__future__"}
    missing = {m for m in third if provides.get(m, m).lower().replace("_", "-") not in declared}
    assert not missing, f"面板导入了未声明的第三方包: {sorted(missing)}"


def snapshot_tree(root: Path):
    """目录树的完整快照:每个文件的相对路径、内容哈希、mtime_ns,以及每个目录的 mtime_ns(目录 mtime 会因为在里面创建/删除文件而变)。"""
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        out[rel] = ("dir", st.st_mtime_ns) if p.is_dir() else ("file", hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
    return out


TRADING_SIDE_DIRS = ["data/state", "runtime", ".ops", "pending_strategies", "data/research", "data/research-control"]


def test_the_dashboard_never_touches_the_trading_side_directory_trees(server):
    """G8 第 2 轮审查(Critical):令牌文件曾默认写在 data/state/ ——交易系统的状态目录里,违反『绝不写入交易系统』。
    之前的测试只检查被读取的文件内容没变,没检查目录:创建文件会改变目录的 mtime,也可能触发交易侧的目录扫描。
    现在对交易侧每一棵目录树做完整快照(含目录自身的 mtime),启动、请求、被杀之后必须逐项相同。"""
    base, tmp, proc = server
    for d in TRADING_SIDE_DIRS:
        (tmp / d).mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "state" / "account_guard.json").write_text(
        json.dumps({"account_id": "1", "last_equity": 1.0, "peak_equity": 1.0, "day": "d", "day_start_equity": 1.0, "hard_locked": False}), encoding="utf-8")
    token = (tmp / "data" / "dashboard" / "status.token").read_text(encoding="utf-8").strip()
    before = {d: snapshot_tree(tmp / d) for d in TRADING_SIDE_DIRS}
    for p in ("/api/overview", "/api/trading", "/api/alerts", "/api/days", "/api/recording", "/api/research", "/api/docs/unattended-log"):
        http(base + p, token)
    proc.kill()
    proc.wait(timeout=10)
    after = {d: snapshot_tree(tmp / d) for d in TRADING_SIDE_DIRS}
    assert before == after


def test_the_default_token_location_is_outside_every_directory_the_dashboard_reads():
    sys.path.insert(0, str(ROOT))
    from dashboard import config
    s = config.load(path=ROOT / "no-such.json", root=ROOT)
    token_dir = s.paths["token_file"].parent
    read_dirs = [s.paths["state_dir"], s.paths["recording_dir"], *s.paths["log_dirs"], s.paths["research_config"].parent]
    for d in read_dirs:
        assert token_dir != d and d not in token_dir.parents and token_dir not in d.parents, (token_dir, d)


def test_importing_and_checking_the_dashboard_writes_no_bytecode_anywhere(tmp_path):
    """首次 import 会在被导入的包目录(包括交易侧的 risk/)写 __pycache__——面板必须不写。在一份干净副本里跑入口验证。"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1]
    dst = tmp_path / "copy"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for pkg in ("dashboard", "risk", "evidence", "notify", "scripts"):
        shutil.copytree(src / pkg, dst / pkg, ignore=ignore)
    env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    proc = subprocess.run([sys.executable, str(dst / "scripts" / "status_server.py"), "--root", str(dst), "--check"],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert list(dst.rglob("__pycache__")) == [] and list(dst.rglob("*.pyc")) == []
