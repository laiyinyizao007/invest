"""状态页的 FastAPI 应用。

**派生自 vnpy_webtrader 1.1.0 的 `web.py`(MIT)**:保留应用骨架与静态页托管的写法,其余全部改写。
版权声明、对上游的逐项改动见 `dashboard/NOTICE-vnpy_webtrader.md`;决策与边界见 `docs/adr/20261112-status-dashboard.md`。

硬性约束(由 tests/test_dashboard_app.py 与 tests/test_web_contract.py 守住):
- 阶段①所有路由只有 GET / HEAD;任何方法不符一律 405。没有登录端点、没有任何写操作、没有委托类接口。
- 本模块不 import vnpy / trading;数据只来自 `dashboard.collectors`(读文件),响应一律经 `dashboard.safe.sanitize`。
- 关闭 FastAPI 自动文档与斜杠重定向;错误统一为契约的 {"error","code"},且不回显请求内容。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from dashboard import auth, collectors as col, models as m
from dashboard.config import Settings
from dashboard.safe import sanitize

VERSION = "1.0.0"
PHASE = 1
ALLOWED_METHODS = ("GET", "HEAD")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

PLACEHOLDER = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>invest 状态页</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>invest 状态页</h1>
<p>后端已运行,但前端尚未构建:<code>dashboard/static/</code> 里没有构建产物清单(<code>manifest.json</code>)。</p>
<p>前端构建与入库流程见 <code>docs/web/05-lovable-guide.md</code>。API 可直接访问(需要令牌):<code>/api/meta</code>、<code>/api/overview</code> 等。</p>
</body></html>
"""

# 前端路由白名单(SPA 回退只对这些路径生效)。来源:docs/web/01-product-spec.md 的页面清单。
# 为什么不是『所有无扩展名路径』:那样 /order、/ws/、/token 这类乱写或上游遗留的路径也会得到 200 的前端页面,
# 路由边界就无法测试,前端同事误请求时也会得到令人困惑的 HTML。新增页面时在此登记并更新测试。
FRONTEND_ROUTES = (
    re.compile(r"^(trading|alerts|days|recording|research)$"),
    re.compile(r"^docs/(unattended-log|simnow-drills)$"),
    re.compile(r"^research/runs/[0-9]{8}-[0-9]{6}-[0-9]{6}$"),
    # 阶段②-⑤ 的页面:后端对应阶段上线前前端不显示入口,但直接访问深链接时仍应得到前端页面(由前端按 capabilities 处理)
    re.compile(r"^(live|equity|control|audit|nodes|data|drills|config)$"),
    re.compile(r"^research/(runs|candidates/[a-z0-9_]{1,40}\.py)$"),
)

ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "bad_request",
               429: "rate_limited", 503: "unavailable"}


def error_response(status: int, message: str, code: str | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "code": code or ERROR_CODES.get(status, "internal")}, status_code=status, headers=headers)


# ---------------------------------------------------------------- 静态托管:只服务构建清单内、哈希匹配的文件

class StaticSite:
    def __init__(self, static_dir: Path) -> None:
        self.files: dict[str, Path] = {}
        self.rejected: list[str] = []
        manifest = static_dir / "manifest.json"
        try:
            listing = json.loads(manifest.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError, AttributeError):
            listing = {}
        root = static_dir.resolve()
        for rel, digest in listing.items():
            path = (root / rel).resolve()
            if path.is_file() and root in path.parents and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                self.files[rel.replace("\\", "/")] = path
            else:
                self.rejected.append(rel)            # 缺失或哈希对不上:不服务

    @property
    def has_index(self) -> bool:
        return "index.html" in self.files

    def lookup(self, rel: str) -> Path | None:
        return self.files.get(rel)


# ---------------------------------------------------------------- 应用

def create_app(settings: Settings, token: str, *, clock: Callable[[], float] = time.time,
               mono: Callable[[], float] = time.monotonic) -> FastAPI:
    app = FastAPI(title="invest 状态页", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    site = StaticSite(settings.paths["static_dir"])
    limiter = auth.RateLimiter(settings.raw["rate_limit_per_minute"], mono)            # 已鉴权请求
    fail_limiter = auth.RateLimiter(settings.raw["rate_limit_per_minute"] // 4 or 1, mono)   # 鉴权失败请求:更严,专门防暴力猜测
    app.state.settings, app.state.site = settings, site

    def respond(payload) -> JSONResponse:
        """唯一的 JSON 出口:先净化(令牌也作为已知秘密脱敏),再序列化。"""
        return JSONResponse(sanitize(payload, secrets=[token]), headers={"Cache-Control": "no-store"})

    # ---- 中间件:方法白名单与安全响应头。鉴权不在这里——它挂在每个 API 路由的依赖项上(见 require_token) ----
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ALLOWED_METHODS:
            resp = error_response(405, "方法不允许", headers={"Allow": ", ".join(ALLOWED_METHODS)})
        else:
            resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        if request.url.path.startswith("/api"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- 统一错误形状:不回显请求内容,不泄露内部细节 ----
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        status = exc.status_code
        text = {401: "未授权", 404: "不存在", 405: "方法不允许", 429: "请求过于频繁"}.get(status, "请求失败")
        return error_response(status, text, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(400, "参数不合法")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        return error_response(500, "服务器内部错误", code="internal")

    def require_token(request: Request) -> None:
        """路由级鉴权:命中 API 路由就必然经过这里,不依赖路径字符串前缀(审查发现:前缀判断与路由匹配是两套规则,容易分叉)。
        鉴权失败与已鉴权请求分桶限流:本机任何进程用垃圾请求都挤不掉合法用户,而暴力猜测仍受更严的限制。"""
        if not auth.token_matches(token, request.headers.get("authorization")):
            wait = fail_limiter.check()
            if wait is not None:
                raise StarletteHTTPException(429, headers={"Retry-After": str(int(wait) + 1)})
            raise StarletteHTTPException(401, headers={"WWW-Authenticate": "Bearer"})
        wait = limiter.check()
        if wait is not None:
            raise StarletteHTTPException(429, headers={"Retry-After": str(int(wait) + 1)})

    app.state.require_token = require_token

    def get(path: str):
        """API 路由:一律 GET/HEAD,且一律带鉴权依赖。"""
        return app.api_route(path, methods=list(ALLOWED_METHODS), dependencies=[Depends(require_token)])

    def get_public(path: str):
        """静态页与 SPA 回退:无数据,不需要令牌。"""
        return app.api_route(path, methods=list(ALLOWED_METHODS))

    # ---- API:全部只读 ----
    @get("/api/meta")
    def meta():
        now = datetime.fromtimestamp(clock()).astimezone()
        return respond({
            "version": VERSION, "phase": PHASE, "server_time": now.isoformat(timespec="seconds"),
            "server_timezone": str(now.tzinfo), "exchange_timezone": "Asia/Shanghai",
            "environment": settings.environment, "polling_hint_seconds": settings.raw["polling_hint_seconds"],
            "capabilities": {"live": False, "equity": False, "control": False, "stream": False,
                             "research_detail": True, "data_views": False, "nodes": False},
        })

    @get("/api/overview")
    def overview():
        return respond(col.collect_overview(settings, clock()))

    @get("/api/trading")
    def trading():
        return respond(col._collect_safely("trading", col.collect_trading, settings, clock()).section)

    @get("/api/alerts")
    def alerts():
        return respond(col._collect_safely("alerts", col.collect_alerts, settings, clock()).section)

    @get("/api/days")
    def days(limit: int | None = None):
        if limit is not None and not 1 <= limit <= 60:
            return error_response(400, "limit 必须在 1 到 60 之间")
        now = clock()
        return respond(col._collect_safely("days", lambda s, n: col.collect_days(s, n, limit), settings, now).section)

    @get("/api/recording")
    def recording():
        return respond(col._collect_safely("recording", col.collect_recording, settings, clock()).section)

    @get("/api/research")
    def research():
        return respond(col._collect_safely("research", col.collect_research, settings, clock()).section)

    @get("/api/research/runs/{run_id}")
    def run_detail(run_id: str):
        detail = col.get_run(settings, run_id)
        return respond(detail) if detail is not None else error_response(404, "不存在")

    @get("/api/docs/{key}")
    def doc(key: str):
        d = col.get_doc(settings, key)
        return respond(d) if d is not None else error_response(404, "不存在")

    # ---- 静态页:不需要令牌;SPA 回退只对不含扩展名的路径生效,且永不接管 /api ----
    def index_response() -> Response:
        if site.has_index:
            return FileResponse(site.lookup("index.html"), media_type="text/html", headers={"Cache-Control": "no-cache"})
        return HTMLResponse(PLACEHOLDER, headers={"Cache-Control": "no-cache"})

    @get_public("/")
    def root():
        return index_response()

    @get_public("/{rel_path:path}")
    def static_or_spa(rel_path: str):
        if rel_path == "api" or rel_path.startswith("api/") or rel_path.startswith("/") or "//" in rel_path:
            return error_response(404, "不存在")                                   # /api 一律不进前端;含双斜杠的歧义路径不进前端
        path = site.lookup(rel_path)
        if path is not None:
            headers = {"Cache-Control": "public, max-age=31536000, immutable" if rel_path.startswith("assets/") else "no-cache"}
            return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", headers=headers)
        if any(r.match(rel_path) for r in FRONTEND_ROUTES):                       # 前端路由(/trading 等)
            return index_response()
        return error_response(404, "不存在")

    return app
