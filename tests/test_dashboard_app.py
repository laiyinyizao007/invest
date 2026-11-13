"""dashboard/app.py:契约合规与安全边界。用 TestClient 发真实请求。"""
import hashlib
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="状态页依赖见 requirements-dashboard.txt")
pytest.importorskip("httpx")
from starlette.testclient import TestClient  # noqa: E402

from dashboard import app as appmod, config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "docs" / "web" / "status-api.openapi.json").read_text(encoding="utf-8"))
NOW = 1_800_000_000.0
TOKEN = "T" * 12 + "k3y" * 10
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT = "66170099"


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"environment": "simnow", "rate_limit_per_minute": 1000}), encoding="utf-8")
    settings = config.load(path=cfg, root=tmp_path)
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True)
    (st / "heartbeat").write_text(str(NOW - 4), encoding="utf-8")
    (st / "account_guard.json").write_text(json.dumps({
        "account_id": ACCOUNT, "last_equity": 9870.5, "peak_equity": 10120.0, "day": "2026-11-13", "day_start_equity": 10000.0,
        "daily_locked_day": None, "hard_locked": False, "hard_reason": ""}), encoding="utf-8")
    import os
    os.utime(st / "account_guard.json", (NOW - 3, NOW - 3))        # NOW 是虚构的 2027 年;真实 mtime 会被正确判为落后几个月
    os.utime(st / "heartbeat", (NOW - 4, NOW - 4))
    return tmp_path, settings


def client(settings, **kw):
    return TestClient(appmod.create_app(settings, TOKEN, clock=lambda: NOW, **kw))


def api_paths():
    return [p for p in SPEC["paths"] if SPEC["paths"][p].get("get", {}).get("x-phase") == 1]


# ---------------------------------------------------------------- 路由边界(阶段①的核心承诺)

def test_the_route_set_is_exactly_phase_one_plus_static(env):
    _, settings = env
    app = appmod.create_app(settings, TOKEN)
    routes = {r.path: set(r.methods) for r in app.routes if hasattr(r, "methods")}
    contract = {re.sub(r"\{(\w+)\}", r"{\1}", p) for p in api_paths()}
    api_in_app = {p for p in routes if p.startswith("/api")}
    assert api_in_app == contract
    assert set(routes) - api_in_app == {"/", "/{rel_path:path}"}                         # 只有静态页与 SPA 兜底
    for path, methods in routes.items():
        assert methods <= {"GET", "HEAD"}, (path, methods)


def test_no_forbidden_route_exists_in_the_running_app(env):
    _, settings = env
    app = appmod.create_app(settings, TOKEN)
    forbidden = re.compile(r"order|cancel|connect|subscribe|unlock|approve|token|login|risk|strategy/(start|stop)|ws\b", re.I)
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert not [p for p in paths if forbidden.search(p)], paths


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
@pytest.mark.parametrize("path", ["/api/trading", "/api/overview", "/", "/trading", "/api/control/pause", "/order", "/token"])
def test_every_non_read_method_is_405_with_the_contract_error_shape(env, method, path):
    _, settings = env
    r = getattr(client(settings), method)(path, headers=AUTH)
    assert r.status_code == 405
    assert r.json() == {"error": "方法不允许", "code": "bad_request"} and r.headers["allow"] == "GET, HEAD"


def test_head_is_allowed_on_api_routes(env):
    _, settings = env
    r = client(settings).head("/api/trading", headers=AUTH)
    assert r.status_code == 200 and r.text == ""


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/api", "/api/", "/api/trading/", "/api/nope", "/api/trading/extra",
                                  "/api/control/pause", "/api/live", "/api/equity", "/ws/", "/tick", "/order"])
def test_hidden_or_unknown_paths_are_404_json_never_the_frontend_page(env, path):
    _, settings = env
    r = client(settings).get(path, headers=AUTH)
    assert r.status_code == 404, path
    assert r.json()["code"] == "not_found" and "<html" not in r.text.lower()


# ---------------------------------------------------------------- 鉴权

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"},
                                     {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": "Bearer "}])
@pytest.mark.parametrize("path", [p.replace("{run_id}", "20261112-210000-000000").replace("{key}", "unattended-log") for p in api_paths()])
def test_every_api_route_requires_the_token(env, headers, path):
    _, settings = env
    r = client(settings).get(path, headers=headers)
    assert r.status_code == 401
    assert r.json() == {"error": "未授权", "code": "unauthorized"} and r.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in r.text


def test_token_in_the_query_string_is_not_accepted(env):
    _, settings = env
    assert client(settings).get(f"/api/trading?token={TOKEN}").status_code == 401


def test_the_static_page_needs_no_token_but_carries_no_data(env):
    _, settings = env
    r = client(settings).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert ACCOUNT not in r.text and "9870" not in r.text


# ---------------------------------------------------------------- 响应契约与净化

def test_trading_response_matches_the_contract_shape_and_hides_the_account(env):
    _, settings = env
    r = client(settings).get("/api/trading", headers=AUTH)
    body = r.json()
    assert r.status_code == 200 and body["state"] == "ok" and body["data"]["guard"]["account_masked"] == "66****99"
    assert ACCOUNT not in r.text and TOKEN not in r.text
    assert body["data"]["guard"]["daily_pnl"] == -129.5


def test_every_phase_one_response_validates_against_the_openapi_schemas(env):
    jsonschema = pytest.importorskip("jsonschema")
    tmp, settings = env
    (tmp / "docs").mkdir()
    (tmp / "docs" / "unattended-log.md").write_text("# x\n", encoding="utf-8")
    c = client(settings)
    comps = SPEC["components"]
    for path in api_paths():
        concrete = path.replace("{run_id}", "20261112-210000-000000").replace("{key}", "unattended-log")
        r = c.get(concrete, headers=AUTH)
        if r.status_code == 404:                                                       # 无数据的详情接口:验证错误形状
            schema = comps["schemas"]["Error"]
        else:
            schema = SPEC["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        errors = list(jsonschema.Draft202012Validator({**schema, "components": comps}).iter_errors(r.json()))
        assert not errors, (path, [e.message for e in errors[:2]])


def test_meta_reports_phase_one_capabilities_and_the_configured_environment(env):
    _, settings = env
    d = client(settings).get("/api/meta", headers=AUTH).json()
    assert d["phase"] == 1 and d["environment"] == {"label": "simnow", "display": "SimNow 模拟盘"}
    assert d["capabilities"]["control"] is False and d["capabilities"]["live"] is False and d["capabilities"]["stream"] is False
    assert d["exchange_timezone"] == "Asia/Shanghai"


def test_overview_without_any_data_is_unknown_and_never_ok(env):
    tmp, settings = env
    for p in (tmp / "data" / "state").iterdir():
        p.unlink()
    d = client(settings).get("/api/overview", headers=AUTH).json()
    assert d["level"] == "unknown" and d["reasons"]


def test_a_secret_planted_in_a_data_file_never_reaches_the_response(env):
    tmp, settings = env
    (tmp / "docs").mkdir()
    (tmp / "docs" / "unattended-log.md").write_text(f"日志来源: C:\\Dev\\projects\\invest\\log\nBearer {TOKEN}\nCTP_PASSWORD=hunter22hunter\n", encoding="utf-8")
    text = client(settings).get("/api/docs/unattended-log", headers=AUTH).text
    assert "C:\\\\Dev" not in text and TOKEN not in text and "hunter22" not in text


def test_validation_errors_do_not_echo_the_input(env):
    _, settings = env
    r = client(settings).get("/api/days?limit=<script>alert(1)</script>", headers=AUTH)
    assert r.status_code == 400 and "script" not in r.text and r.json()["code"] == "bad_request"
    assert client(settings).get("/api/days?limit=0", headers=AUTH).status_code == 400
    assert client(settings).get("/api/days?limit=61", headers=AUTH).status_code == 400
    assert client(settings).get("/api/days?limit=5", headers=AUTH).status_code == 200


def test_an_internal_error_is_a_generic_500_without_details(env, monkeypatch):
    _, settings = env
    monkeypatch.setattr(appmod.col, "collect_overview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-internal-detail")))
    r = TestClient(appmod.create_app(settings, TOKEN), raise_server_exceptions=False).get("/api/overview", headers=AUTH)
    assert r.status_code == 500 and r.json() == {"error": "服务器内部错误", "code": "internal"} and "secret-internal" not in r.text


# ---------------------------------------------------------------- 详情接口的白名单

@pytest.mark.parametrize("bad", ["..%2f..%2fsecret", "%2e%2e%2f%2e%2e%2fsecret", "20261112-210000-000000%2f..%2fx", "x", "20261112-210000-00000a"])
def test_run_detail_rejects_anything_that_is_not_a_run_id(env, bad):
    _, settings = env
    assert client(settings).get(f"/api/research/runs/{bad}", headers=AUTH).status_code == 404


@pytest.mark.parametrize("bad", ["..%2f.env", "..%5c.env", "docs%2funattended-log.md", "unattended-log.md", "C:%5cWindows%5cwin.ini", ".env", "UNATTENDED-LOG"])
def test_doc_endpoint_accepts_only_whitelisted_keys(env, bad):
    tmp, settings = env
    (tmp / ".env").write_text("CTP_PASSWORD=secret", encoding="utf-8")
    r = client(settings).get(f"/api/docs/{bad}", headers=AUTH)
    assert r.status_code == 404 and "secret" not in r.text


# ---------------------------------------------------------------- 响应头与限流

@pytest.mark.parametrize("path,headers", [("/", {}), ("/trading", {}), ("/api/trading", AUTH), ("/api/nope", AUTH), ("/api/trading", {})])
def test_security_headers_on_every_kind_of_response(env, path, headers):
    _, settings = env
    r = client(settings).get(path, headers=headers)
    assert r.headers["content-security-policy"] == appmod.CSP and "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in r.headers                                # 无 CORS


def test_api_responses_are_never_cached(env):
    _, settings = env
    c = client(settings)
    for path, h in (("/api/trading", AUTH), ("/api/nope", AUTH), ("/api/trading", {})):
        assert c.get(path, headers=h).headers["cache-control"] == "no-store"


def test_cross_origin_preflight_is_refused(env):
    _, settings = env
    r = client(settings).options("/api/trading", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 405 and "access-control-allow-origin" not in r.headers


def test_rate_limit_returns_429_with_retry_after(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rate_limit_per_minute": 3}), encoding="utf-8")
    settings = config.load(path=cfg, root=tmp_path)
    clk = [0.0]
    c = TestClient(appmod.create_app(settings, TOKEN, mono=lambda: clk[0]))
    codes = [c.get("/api/meta", headers=AUTH).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = c.get("/api/meta", headers=AUTH)
    assert r.json()["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
    clk[0] = 61
    assert c.get("/api/meta", headers=AUTH).status_code == 200


def test_failed_auth_attempts_are_throttled_in_their_own_stricter_bucket(tmp_path):
    """防暴力猜测令牌:鉴权失败请求有独立且更严的限额(总限额的 1/4)。"""
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rate_limit_per_minute": 8}), encoding="utf-8")
    c = TestClient(appmod.create_app(config.load(path=cfg, root=tmp_path), TOKEN, mono=lambda: 0.0))
    assert [c.get("/api/meta").status_code for _ in range(4)] == [401, 401, 429, 429]


def test_garbage_requests_cannot_lock_the_legitimate_user_out(tmp_path):
    """审查发现:单桶限流下,任何本机进程用垃圾请求就能耗尽额度,让合法用户收到 429。分桶后不会。"""
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rate_limit_per_minute": 8}), encoding="utf-8")
    c = TestClient(appmod.create_app(config.load(path=cfg, root=tmp_path), TOKEN, mono=lambda: 0.0))
    for _ in range(50):
        c.get("/api/meta", headers={"Authorization": "Bearer garbage"})                 # 失败桶被打满
    assert c.get("/api/meta", headers=AUTH).status_code == 200                          # 合法请求不受影响


def test_the_authenticated_bucket_still_limits_a_runaway_legitimate_client(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"rate_limit_per_minute": 3}), encoding="utf-8")
    c = TestClient(appmod.create_app(config.load(path=cfg, root=tmp_path), TOKEN, mono=lambda: 0.0))
    assert [c.get("/api/meta", headers=AUTH).status_code for _ in range(5)] == [200, 200, 200, 429, 429]


# ---------------------------------------------------------------- 静态托管

def build_static(tmp, files, manifest=True, tamper=None):
    d = tmp / "dashboard" / "static"
    (d / "assets").mkdir(parents=True, exist_ok=True)
    listing = {}
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body)
        listing[rel] = hashlib.sha256(body).hexdigest()
    if tamper:
        (d / tamper).write_bytes(b"TAMPERED")
    if manifest:
        (d / "manifest.json").write_text(json.dumps({"files": listing}), encoding="utf-8")
    return d


def test_placeholder_page_without_a_manifest_has_no_external_resources(env):
    _, settings = env
    r = client(settings).get("/")
    assert "前端尚未构建" in r.text and not re.search(r"https?://", r.text)


def test_manifest_files_are_served_with_the_right_cache_policy(env):
    tmp, settings = env
    build_static(tmp, {"index.html": b"<!doctype html><title>app</title>", "assets/app-abc123.js": b"console.log(1)", "assets/app-abc123.css": b"b{}"})
    c = client(settings)
    idx = c.get("/")
    assert idx.status_code == 200 and "<title>app</title>" in idx.text and idx.headers["cache-control"] == "no-cache"
    js = c.get("/assets/app-abc123.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "immutable" in js.headers["cache-control"]
    assert "css" in c.get("/assets/app-abc123.css").headers["content-type"]


def test_spa_routes_fall_back_to_index_but_missing_assets_are_404(env):
    tmp, settings = env
    build_static(tmp, {"index.html": b"<title>app</title>", "assets/a.js": b"1"})
    c = client(settings)
    assert "<title>app</title>" in c.get("/trading").text and "<title>app</title>" in c.get("/docs/unattended-log").text
    assert c.get("/assets/missing.js").status_code == 404 and c.get("/x.map").status_code == 404


def test_files_not_in_the_manifest_or_with_a_wrong_hash_are_not_served(env):
    tmp, settings = env
    d = build_static(tmp, {"index.html": b"<title>app</title>", "assets/a.js": b"1"}, tamper="assets/a.js")
    (d / "assets" / "extra.js").write_bytes(b"not in manifest")
    (tmp / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    c = client(settings)
    assert c.get("/assets/a.js").status_code == 404                                       # 哈希对不上
    assert c.get("/assets/extra.js").status_code == 404                                   # 不在清单
    for p in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt", "/manifest.json"):
        r = c.get(p)
        assert "TOP SECRET" not in r.text and r.status_code in (200, 404), p
    assert "app" in c.get("/").text
    assert c.app.state.site.rejected == ["assets/a.js"]


def test_a_manifest_entry_pointing_outside_the_static_dir_is_ignored(env):
    tmp, settings = env
    (tmp / "outside.txt").write_bytes(b"x")
    d = tmp / "dashboard" / "static"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"files": {"../../outside.txt": hashlib.sha256(b"x").hexdigest()}}), encoding="utf-8")
    assert client(settings).app.state.site.files == {}


def test_the_method_whitelist_holds_even_for_paths_that_do_not_exist(env):
    """方法白名单是中间件层的独立防线:对任何路径(含不存在的)发非读方法,都是同一个 405,而不是由路由决定。"""
    _, settings = env
    for path in ("/api/definitely-not-a-route", "/nothing/here", "/api/control/pause"):
        r = client(settings).post(path, headers=AUTH, content=b"{}")
        assert r.status_code == 405 and r.json()["error"] == "方法不允许" and r.headers["allow"] == "GET, HEAD", path


def test_api_prefix_is_never_handed_to_the_frontend_fallback_even_for_valid_looking_routes(env, monkeypatch):
    """即使前端路由白名单将来被误加了 api 开头的条目,/api 下的未知路径也必须是 404 JSON。"""
    _, settings = env
    monkeypatch.setattr(appmod, "FRONTEND_ROUTES", (re.compile(r".*"),))             # 最坏情况:白名单放行一切
    for path in ("/api/anything", "/api/trading/x"):
        r = client(settings).get(path, headers=AUTH)
        assert r.status_code == 404 and r.json()["code"] == "not_found", path


def test_rendered_frontend_routes_serve_the_page_and_the_whitelist_covers_every_page_in_the_spec():
    spec = (ROOT / "docs" / "web" / "01-product-spec.md").read_text(encoding="utf-8")
    pages = {"trading", "alerts", "days", "recording", "research", "live", "equity", "control", "audit", "nodes"}
    assert all(any(r.match(p) for r in appmod.FRONTEND_ROUTES) for p in pages)
    assert any(r.match("docs/unattended-log") for r in appmod.FRONTEND_ROUTES) and not any(r.match("docs/../.env") for r in appmod.FRONTEND_ROUTES)
    assert all(f"`/{p}`" in spec or p in spec for p in ("trading", "alerts"))


# ---------------------------------------------------------------- G8 第 1 轮对抗性审查发现

def raw_get(app, path, headers=()):
    """直接喂 ASGI scope,绕过任何 HTTP 客户端的 URL 解析。
    为什么必须这样:httpx 会把『//api/trading』当成协议相对 URL(主机 api、路径 /trading)改写后再发,
    用 TestClient 测这类路径测的其实是客户端而不是服务器——这个盲区是 G8 审查时发现的。"""
    import asyncio
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"127.0.0.1")] + list(headers),
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8770)}
    out = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"].decode("utf-8", "replace")


@pytest.mark.parametrize("path", ["//api/trading", "///api/trading", "//", "//trading", "/trading//", "/api//trading", "//docs/unattended-log", "/./api/trading"])
def test_ambiguous_raw_paths_are_never_served_as_the_frontend_or_as_data(env, path):
    """原始路径(未经客户端改写)含双斜杠等歧义写法时:不得返回前端页面,更不得返回数据。"""
    _, settings = env
    status, body = raw_get(appmod.create_app(settings, TOKEN), path)
    assert status in (401, 404), (path, status)
    assert "invest 状态页" not in body and '"state"' not in body, path


def test_raw_canonical_paths_still_work_so_the_ambiguity_test_is_meaningful(env):
    _, settings = env
    app = appmod.create_app(settings, TOKEN)
    assert raw_get(app, "/trading")[0] == 200
    assert raw_get(app, "/api/trading")[0] == 401
    assert raw_get(app, "/api/trading", headers=[(b"authorization", f"Bearer {TOKEN}".encode())])[0] == 200


def test_authentication_is_attached_to_each_api_route_not_derived_from_a_path_prefix(env):
    """鉴权必须跟路由绑定:不论请求路径怎么写,只要命中 API 路由就必须已鉴权。用不带令牌的请求遍历大量写法,
    任何 200 且含 state 字段的响应都是泄露。"""
    _, settings = env
    c = client(settings)
    shapes = ["/api/trading", "/api//trading", "//api/trading", "/api/./trading", "/./api/trading", "/api/trading/", "/api/trading//",
              "/api/%74rading", "/%61pi/trading", "/api/trading%2f", "/api/trading;x=1", "/api/trading?a=b", "/API/trading", "/Api/trading"]
    for p in shapes:
        r = c.get(p)
        assert not (r.status_code == 200 and '"state"' in r.text), p


def test_every_api_route_declares_the_auth_dependency(env):
    """路由级保证:app.routes 里每个 /api 路由都挂着鉴权依赖(不是只靠中间件)。"""
    _, settings = env
    app = appmod.create_app(settings, TOKEN)
    api_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api")]
    assert api_routes
    for r in api_routes:
        deps = [d.call for d in r.dependant.dependencies]
        assert app.state.require_token in deps, r.path


# ---------------------------------------------------------------- G8 第 2 轮(codex #9 #11)

def test_a_static_file_replaced_after_startup_is_no_longer_served(env):
    """曾经:清单哈希只在创建应用时校验一次,之后文件被替换仍会未经鉴权公开返回(还带一年 immutable 缓存)。"""
    tmp, settings = env
    d = build_static(tmp, {"index.html": b"<title>app</title>", "assets/app.js": b"console.log('SAFE')"})
    c = client(settings)
    assert c.get("/assets/app.js").status_code == 200
    (d / "assets" / "app.js").write_bytes(b"SECRET C:\Users\alice\.env TOKEN=abc")             # 启动之后被替换
    r = c.get("/assets/app.js")
    assert r.status_code == 404 and "SECRET" not in r.text


def test_a_static_file_replaced_by_same_size_content_with_restored_mtime_is_still_rejected(env):
    tmp, settings = env
    d = build_static(tmp, {"index.html": b"<title>app</title>", "assets/app.js": b"AAAA"})
    c = client(settings)
    st = (d / "assets" / "app.js").stat()
    (d / "assets" / "app.js").write_bytes(b"BBBB")
    import os
    os.utime(d / "assets" / "app.js", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert c.get("/assets/app.js").status_code == 404


def test_a_deleted_static_file_is_a_404_not_a_500(env):
    tmp, settings = env
    d = build_static(tmp, {"index.html": b"<title>app</title>", "assets/app.js": b"1"})
    c = client(settings)
    (d / "assets" / "app.js").unlink()
    assert c.get("/assets/app.js").status_code == 404


def test_index_html_replaced_after_startup_falls_back_to_the_placeholder_not_the_tampered_page(env):
    tmp, settings = env
    d = build_static(tmp, {"index.html": b"<title>app</title>"})
    c = client(settings)
    (d / "index.html").write_bytes(b"<script>steal()</script>")
    assert "steal()" not in c.get("/").text and "steal()" not in c.get("/trading").text


def test_head_on_an_api_route_requires_auth_and_sends_no_body_at_the_asgi_level(env):
    """曾经:HEAD 会执行完整采集,并在 ASGI 层发出完整 JSON body(真实 uvicorn 会丢弃,但应用本身语义不对)。"""
    _, settings = env
    app = appmodapp(settings)
    status, _ = raw_method(app, "HEAD", "/api/overview")
    assert status == 401                                                 # 无令牌:HEAD 同样要鉴权(错误响应可以带说明正文)
    status, body = raw_method(app, "HEAD", "/api/overview", headers=[(b"authorization", f"Bearer {TOKEN}".encode())])
    assert status == 200 and body == ""                                  # 有令牌:成功的 HEAD 没有 body


def test_head_does_not_run_the_expensive_collectors(env, monkeypatch):
    _, settings = env
    calls = []
    monkeypatch.setattr(appmod.col, "collect_overview", lambda *a, **k: (calls.append(1), {})[1])
    app = appmodapp(settings)
    raw_method(app, "HEAD", "/api/overview", headers=[(b"authorization", f"Bearer {TOKEN}".encode())])
    assert calls == []
    raw_method(app, "GET", "/api/overview", headers=[(b"authorization", f"Bearer {TOKEN}".encode())])
    assert calls == [1]


def appmodapp(settings):
    return appmod.create_app(settings, TOKEN, clock=lambda: NOW)


def raw_method(app, method, path, headers=()):
    import asyncio
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"127.0.0.1")] + list(headers),
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8770)}
    out = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"].decode("utf-8", "replace")
