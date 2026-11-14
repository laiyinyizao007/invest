"""研究候选的带外批准进程(阶段四 4b):只监听 127.0.0.1 的一次性网页,人来点确认。

为什么是独立进程而不是网关端点:
  - 批准必须发生在模型接触不到的位置。本进程不与毕昇共网、不与状态页同进程,
    毕昇/DSH/MCP 都只能「请求批准」,拿不到写 approvals.json 的能力。
  - 批准记录绑定**内容哈希**:本进程自己重算候选文件的 sha256,要求人输入**完整哈希**,
    写入前再重算一次,文件在阅读期间被改动就拒绝。

流程:
    python scripts/research_approval_server.py --strategy <候选名> [--port 0] [--timeout 900]
启动后先向 stdout 打印一行 JSON(状态、sha256;**地址只在没有 --hide-url 时才有**),
随后服务网页(默认自己开浏览器;`--no-browser` 可关掉);人确认或拒绝后进程退出。

**页面地址的去向**:`--hide-url` 时地址**不落盘、不进 stdout**,只被这个进程拿去开浏览器——
它只活在进程的内存里(第 5 轮 codex 的 H1:此前 MCP 把它写进本机文件,而那个文件是
「模型没有本地文件工具」这个**未经验证**的前提下才安全的)。

安全约束:
  - 只绑定 127.0.0.1;校验 Host(防 DNS rebinding)与 Origin(防跨站 POST);不开 CORS。
  - URL 里带一次性令牌,页面里另有一次性 CSRF 令牌,两者都要对得上才写批准。
  - 只认 pending_strategies/ 里的候选,只写 approvals.json,不碰策略目录、不装载。
  - **批准页与批准记录不把哈希发给模型**:批准页只在浏览器里给人看;
    MCP 侧的工具返回里不含 URL、不含哈希(否则有本地 HTTP 能力的模型能自己批自己)。
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import html
import json
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import research_config as rc
import research_console as rconsole
import research_sandbox as rs

# 项目根目录:**不 import bootstrap**(那会把 vnpy 交易栈与 .env 拖进来);
# 数据根默认与代码根相同,可用 INVEST_ROOT 覆盖(与 research-mcp 同一约定),
# 便于在 worktree/副本里批准网关实际挂载的那棵目录树。
ROOT = rc.ROOT

# 人要输入**完整**的 64 位哈希(不是前缀)。前缀只有 8 位时,一个能读到页面的自动化程序
# 可以枚举 2^32 次撞中;而模型侧根本不该拿到哈希(见模块 docstring)。
HASH_LEN = 64
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_TIMEOUT = 900
MAX_BODY = 4096
MAX_ATTEMPTS = 5              # 哈希填错时允许重试的次数,超过就关闭页面
DISPLAY_LIMIT = 200_000       # 页面上显示源码的上限(字符)


class ApprovalError(RuntimeError):
    """批准被拒绝(筛查未过、哈希不符、文件被改、令牌不对)。"""


class ScrubUnavailable(RuntimeError):
    """脱敏模块不可用。

    这是 **fail-closed** 的理由(第 8 轮 codex 的 H2):脱敏坏了还照常发页面的话,
    人看到的是「看不到源码 + 一行哈希」,却照样能把一份**自己根本没看过**的代码批准掉。
    与其这样,不如根本不发页面。"""


def load_config() -> dict:
    """读沙盒配置:优先用**数据根**(INVEST_ROOT)里的那份,否则用代码根的。

    批准进程读的是网关实际挂载的那棵目录树,配置也得跟它一致,
    否则会出现"路径按 A 解析、限制按 B 解析"的错配。
    """
    data_cfg = ROOT / "config" / "research_sandbox.json"
    if data_cfg.is_file():
        return rc.load(data_cfg)
    return rc.load()


def scrub(text: str) -> str:
    """把要显示给人的文本脱敏(ADR 第 4 条:页面上的源码不得带出凭据)。

    脱敏模块不可用时**抛错**(fail-closed),不返回占位串:占位串会让页面变成
    「看不到源码也能批准」,那正是「人要读过」这条前提要防的事(第 8 轮 codex 的 H2)。
    """
    try:
        from dashboard import safe as _safe
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都必须拒绝发页面
        raise ScrubUnavailable(f"脱敏模块不可用,拒绝提供批准页: {type(exc).__name__}: {exc}") from exc
    return _safe.scrub_text(text, limit=DISPLAY_LIMIT)


# ---------- 候选与父代 ----------

def resolve_candidate(name: str, pending_dir: str = rs.PENDING_DIR) -> Path:
    if not name.endswith(".py"):
        name = name + ".py"
    try:
        return rs.resolve_pending(name, ROOT, pending_dir)
    except rs.SandboxError as exc:
        raise ApprovalError(str(exc)) from None


def read_source(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ApprovalError("候选文件无法读取") from None
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApprovalError("候选文件不是 UTF-8 文本") from None


def class_bases(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    names.append(base.attr)
    return names


def find_parent(source: str) -> tuple[Path, str] | None:
    """找出候选继承的策略基类(父代)所在文件。

    不靠 CamelCase→snake 的文件名猜测(TrendAtr15mStrategy 对应的文件名带数字分段,
    猜法很容易错),改为解析 strategies/ 下每个模块,按**类名**匹配。
    """
    wanted = set(class_bases(source))
    if not wanted:
        return None
    strategies_dir = ROOT / "strategies"
    if not strategies_dir.is_dir():
        return None
    for path in sorted(strategies_dir.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in wanted:
                return path, node.name
    return None


def build_diff(candidate: Path, source: str) -> tuple[str, str] | None:
    parent = find_parent(source)
    if parent is None:
        return None
    parent_path, parent_class = parent
    try:
        parent_source = parent_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        rel_parent = parent_path.relative_to(ROOT).as_posix()
    except ValueError:
        rel_parent = parent_path.name
    diff = difflib.unified_diff(
        parent_source.splitlines(),
        source.splitlines(),
        fromfile=f"父策略 {parent_class}({rel_parent})",
        tofile=f"候选 {candidate.name}",
        lineterm="",
        n=3,
    )
    return rel_parent, "\n".join(diff)


# ---------- 页面 ----------

def render_page(*, name: str, size: int, sha: str, findings: list[str], diff_text: str | None,
                parent_path: str | None, source: str, csrf: str, error: str | None = None,
                attempts_left: int | None = None) -> bytes:
    def esc(text: str) -> str:
        return html.escape(text, quote=True)

    screen_html = "通过" if not findings else "未通过"
    findings_html = "".join(f"<li>{esc(f)}</li>" for f in findings)
    numbered = "\n".join(f"{i:4d} | {esc(line)}" for i, line in enumerate(source.splitlines(), 1))
    if diff_text is None:
        diff_block = "<p class=muted>未能定位父策略(候选未继承 strategies/ 下的基类),无可显示的差异。</p>"
    else:
        diff_block = f"<p class=muted>父策略:{esc(parent_path or '')}</p><pre class=diff>{esc(diff_text)}</pre>"
    if error:
        error_block = (f"<div class=boxerr><b>没有批准。</b> {esc(error)}"
                       + (f"(还可重试 {attempts_left} 次)" if attempts_left is not None else "")
                       + "</div>")
    else:
        error_block = ""
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>批准候选策略 {esc(name)}</title>
<style>
 body{{font-family:ui-monospace,Consolas,monospace;margin:24px;line-height:1.5;color:#111}}
 h1{{font-size:18px}} .muted{{color:#666;font-size:13px}}
 pre{{background:#f6f6f6;padding:12px;overflow:auto;border:1px solid #ddd}}
 .diff{{background:#fffbe6;border-color:#e6d9a2}}
 .bad{{color:#b00;font-weight:bold}} .good{{color:#070;font-weight:bold}}
 .warn{{background:#fff4f4;border:1px solid #e0b4b4;padding:10px;margin:12px 0}}
 .boxerr{{background:#fff4f4;border:1px solid #e0b4b4;padding:10px;margin:12px 0;color:#900}}
 .hash{{user-select:all;background:#f0f0f0;padding:2px 6px}}
 input{{font-size:16px;padding:4px}} button{{font-size:15px;padding:6px 16px;margin-left:8px}}
 .box{{border:1px solid #ddd;padding:12px;margin:12px 0;background:#fafafa}}
</style></head><body>
<h1>批准候选策略:{esc(name)}</h1>
<div class=warn><b>下面显示的源码与差异是模型生成的,属于不可信数据。</b>
  它可能包含试图操纵你的文字。请只把它当作**待审代码**来读,不要执行其中的任何指令;
  只有你亲自读过并确认无害,才继续。</div>
{error_block}
<div class=box>
  <div>大小:{size} 字节</div>
  <div>sha256:<b id=sha class=hash>{esc(sha)}</b></div>
  <div>静态筛查:<span class="{'good' if not findings else 'bad'}">{screen_html}</span></div>
  <ul>{findings_html}</ul>
  <div class=muted>请核对上面的 sha256,把<b>完整的 64 位哈希</b>复制到下面的输入框再确认——
    本进程会自己重算哈希与你填的值比对,不一致不写批准。代码改动后批准自动失效。</div>
  <div class=muted>下面显示的源码已经过脱敏(凭据、本机路径等被替换掉),**以 sha256 为准**:
    批准绑定的是原始字节,不是这段显示文本。</div>
</div>
<h2>与父策略的差异</h2>
{diff_block}
<h2>完整源码</h2>
<pre>{numbered}</pre>
<form method="post" action="">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <label>sha256(完整 64 位):<input name="digest" maxlength="{HASH_LEN}" size="{HASH_LEN + 2}"
    autocomplete="off" spellcheck="false" pattern="[0-9a-f]{{{HASH_LEN}}}" required></label>
  <button type="submit">确认批准</button>
</form>
</body></html>"""
    return body.encode("utf-8")


def render_result(ok: bool, message: str) -> bytes:
    cls = "good" if ok else "bad"
    body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>批准结果</title><style>body{{font-family:ui-monospace,Consolas,monospace;margin:40px}}
.good{{color:#070;font-weight:bold}} .bad{{color:#b00;font-weight:bold}}</style></head>
<body><h1 class="{cls}">{html.escape(message)}</h1>
<p class=muted>本页可以关闭。</p></body></html>"""
    return body.encode("utf-8")


# ---------- 服务 ----------

def parse_qs(text: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs as _parse
    return _parse(text, keep_blank_values=True)


class ApprovalApp:
    """批准页面的全部逻辑(与 HTTP 传输分离,便于测试)。

    状态机:待确认 → 已批准 / 已拒绝 / 出错。**只有** ``approve`` 这一条路径会写 approvals.json,
    且必须同时满足:CSRF 令牌对、URL 令牌对、Host/Origin 是本机、人填的**完整哈希**与**当前文件**的
    哈希一致、静态筛查通过。

    填错哈希(或者 CSRF 不对)不会关闭页面:人可以改好了再填,超过 ``MAX_ATTEMPTS`` 次才关闭。
    """

    def __init__(self, *, path: Path, raw: bytes, source: str, sha: str, findings: list[str],
                 diff_text: str | None, parent: str | None, approvals_file: Path,
                 token: str, csrf: str, port: int = 0,
                 display_source: str | None = None) -> None:
        self.path = path
        self.raw = raw
        self.source = source
        self.sha = sha
        self.findings = findings
        # 页面上显示的源码:**脱敏一次,存在这里**。
        # 一是脱敏失败要 fail-closed(见 ScrubUnavailable:宁可不发页面,也不能让人在
        # 「看不到源码」的情况下批准);二是不每次渲染现算——每次 GET 都重解析
        # strategies/*.py 找父类,策略一多就是每次 GET 一次 O(#策略) 的 AST 解析。
        self.display_source = scrub(source) if display_source is None else display_source
        self.diff_text = scrub(diff_text) if diff_text is not None else None
        self.parent = parent
        self.approvals_file = approvals_file
        self.token = token
        self.csrf = csrf
        self.port = port
        self.decision: str | None = None       # None / "approved" / "rejected" / "error"
        self.message = ""
        self.attempts = 0
        self._lock = threading.Lock()          # 状态机由多线程并发访问(ThunderingHTTPServer)

    # -- 传输层的安全校验 --

    def _host_ok(self, headers: dict) -> bool:
        host = (headers.get("host") or headers.get("Host") or "").strip()
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"} if self.port else set()
        return host in allowed

    def _origin_ok(self, headers: dict) -> bool:
        # Origin 与 Sec-Fetch-Site **至少要有一个**。
        #
        # **它们防的是什么、不是什么**(第 5 轮 codex 的实测纠正):
        #   - 防的是**跨站浏览器 CSRF**:恶意网页让浏览器带着别人的 Origin/Sec-Fetch-Site
        #     来 POST,这两个头由浏览器填,跨站时必然对不上。
        #   - **防不了本机上的普通程序**:一个跑在 127.0.0.1 上的 Python/curl 可以自己写
        #     `Origin: http://127.0.0.1:<port>` 与 `Sec-Fetch-Site: same-origin`,
        #     实测能一路 POST 到「已批准」(第 5 轮 codex 复现)。
        # 真正挡住本机程序的是**本页 URL 里的一次性令牌**(192 位)+ 页面内的 CSRF 令牌
        # + 完整 64 位 sha256;而令牌只发给浏览器,不落盘、不进模型通道
        # (见 tools/research_mcp/server.py 的 _request_approval)。
        origin = headers.get("origin")
        site = (headers.get("sec-fetch-site") or "").strip()
        if origin is None and not site:
            return False
        if origin is None:
            return True                     # 只有 Sec-Fetch-Site:由 _fetch_site_ok 继续把关
        allowed = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"} if self.port else set()
        return origin in allowed

    def _fetch_site_ok(self, headers: dict) -> bool:
        """Sec-Fetch-Site 由浏览器自己填:跨站提交会是 cross-site。同样**挡不住本机程序**。"""
        site = (headers.get("sec-fetch-site") or "").strip().lower()
        return site in ("", "same-origin", "none")

    # -- 请求处理 --

    def _page(self, error: str | None = None, code: int = 200) -> tuple[int, bytes]:
        return code, render_page(
            name=self.path.name, size=len(self.raw), sha=self.sha, findings=self.findings,
            diff_text=self.diff_text, parent_path=self.parent,
            source=self.display_source, csrf=self.csrf, error=error,
            attempts_left=max(0, MAX_ATTEMPTS - self.attempts),
        )

    def handle(self, method: str, path: str, headers: dict, payload: bytes) -> tuple[int, bytes]:
        # **头名一律小写化**:HTTP 头名大小写不敏感,但 `Sec-Fetch-Site` 这种
        # 只有小写形式才查得到——漏掉一次大小写,跨站提交就绕过了来源检查。
        # HTTP 传输层已经小写化过,这里再兜一次底(直接调用 handle 的调用方也受益)。
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        if not self._host_ok(headers):
            return 403, b"forbidden"
        # 令牌比对用常数时间:与 csrf 令牌同一条口径(第 7 轮 codex 的 F10)。
        # 路径比较走字符串相等会提前返回,理论上给逐字节猜令牌留了时间差。
        # 第 10 轮 codex 的 Medium:原先这里**先**用 `!=` 判掉,下面的常数时间比较
        # 是死代码(永远到不了)。要么留一条,要么留常数时间那条——不能两条都留、却宣称
        # 走的是常数时间那条。现在只留常数时间这条。
        if not secrets.compare_digest(path.encode("utf-8"), f"/a/{self.token}".encode("utf-8")):
            return 404, b"not found"
        if method == "GET":
            with self._lock:
                if self.decision is not None:
                    # 已经做出决定(批准/拒绝/出错):不再受理任何请求,批准页面即刻失效
                    return 409, render_result(False, "已经做出决定,本页已关闭。")
            return self._page()
        if method != "POST":
            return 405, b"method not allowed"
        if not self._origin_ok(headers) or not self._fetch_site_ok(headers):
            return 403, b"forbidden origin"
        if not payload or len(payload) > MAX_BODY:
            return 400, render_result(False, "请求体不合法,未批准。")
        fields = parse_qs(payload.decode("utf-8", "replace"))
        csrf = (fields.get("csrf") or [""])[0]
        digest = (fields.get("digest") or [""])[0].strip().lower()
        # 决定与计数都在锁里改:两个 POST 并发时,不能出现"一边在写账本、一边报拒绝"。
        with self._lock:
            if self.decision is not None:
                return 409, render_result(False, "已经做出决定,本页已关闭。")
            if not secrets.compare_digest(csrf.encode("utf-8"), self.csrf.encode("utf-8")):
                # CSRF 不对:不把源码回给请求方(可能是过期页面,也可能是本机上的其它程序),
                # 但**不**因此关闭页面——人可以刷新重来。
                # 用 bytes 比较:str 版 compare_digest 只接受 ASCII,请求方填非 ASCII 会抛 TypeError。
                self.attempts += 1
                if self.attempts >= MAX_ATTEMPTS:
                    return self._decide(403, "rejected", "CSRF 令牌不对,未批准。")
                return 403, render_result(False, "CSRF 令牌不对,请刷新页面后重试。")
            if not HASH_RE.match(digest):
                self.attempts += 1
                if self.attempts >= MAX_ATTEMPTS:
                    return self._decide(400, "rejected", "请填完整的 64 位 sha256(小写十六进制)。")
                return self._page("哈希格式不对:要完整的 64 位小写十六进制。", code=400)
            try:
                sha_now = rs.file_sha256(self.path)
            except OSError:
                sha_now = ""
            if sha_now != self.sha:
                # 文件在阅读期间被改了:页面已失效,不能让人"重试"到批准另一份内容。
                return self._decide(409, "rejected", "文件在阅读期间被改动,未批准;请重新打开本页。")
            if not secrets.compare_digest(sha_now.encode("ascii"), digest.encode("ascii")):
                self.attempts += 1
                if self.attempts >= MAX_ATTEMPTS:
                    return self._decide(400, "rejected", "你填的哈希与当前文件不符,未批准。")
                return self._page("你填的哈希与当前文件不符。", code=400)
            if self.findings:
                return self._decide(400, "rejected", "静态筛查未通过,不予批准。")
            try:
                # 传的是**刚刚核对过的那个哈希**:不再重读一次文件,避免两次读之间内容被改。
                rs.record_approval(self.path, self.approvals_file, sha256=sha_now)
            except rs.SandboxError as exc:
                return self._decide(400, "error", f"写入批准记录失败:{exc}")
            return self._decide(200, "approved", "已批准(绑定到当前文件内容)。")

    def _decide(self, code: int, decision: str, message: str) -> tuple[int, bytes]:
        self.decision = decision
        self.message = message
        return code, render_result(decision == "approved", message)


class ApprovalHandler(BaseHTTPRequestHandler):
    server_version = "research-approval/1.0"
    app: ApprovalApp | None = None      # 由 serve() 注入
    timeout = 30                        # 单个连接最多挂 30 秒(防慢连接占住线程)

    def log_message(self, fmt, *args):  # 静音:默认实现会往 stderr 写访问日志
        pass

    def _headers(self) -> dict:
        return {k.lower(): v for k, v in self.headers.items()}

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器提前关页/刷新:正常的收尾。不接住的话整段 traceback 会打进日志,
            # 把真正的错误淹没(批准进程只跑一次,日志很短,更经不起噪声)。
            self.close_connection = True

    def do_GET(self):  # noqa: N802
        if self.app is None:
            self._send(500, b"no app")
            return
        code, body = self.app.handle("GET", self.path, self._headers(), b"")
        self._send(code, body)

    def do_POST(self):  # noqa: N802
        if self.app is None:
            self._send(500, b"no app")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._send(400, render_result(False, "Content-Length 不合法,未批准。"))
            return
        if length > MAX_BODY:
            # 先看大小再读:别让本机进程用超大 Content-Length 撑内存
            self.close_connection = True
            self._send(413, render_result(False, "请求体过大,未批准。"))
            return
        payload = self.rfile.read(length) if length > 0 else b""
        code, body = self.app.handle("POST", self.path, self._headers(), payload)
        self._send(code, body)
        if self.app.decision is not None:
            # 一次决定:立刻停服务。留着页面就等于留着可再提交的批准入口,
            # 而且 MCP 那边的等待会一直拖到超时。
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def build_server(*, path, raw: bytes, source: str, sha: str, findings, diff,
                 approvals_file, port: int = 0) -> tuple[ThreadingHTTPServer, ApprovalApp]:
    """绑定批准页的 HTTP 服务。

    **只绑 127.0.0.1**:绑定地址写死在这里,不留配置口子——
    批准页是「本机网页」,一旦绑到 0.0.0.0 就变成局域网可达,同网段的人不用令牌
    就能拿到候选源码(令牌虽在 URL 里,但源文件本身就该只在本机看)。
    抽成函数是为了能直接断言绑定地址(第 6 轮 codex 的 B/M:原来这条边界没有用例锁住)。
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ApprovalHandler)
    try:
        actual_port = httpd.server_address[1]
        # 构造 ApprovalApp 时会做脱敏:脱敏不可用就在这里抛 ScrubUnavailable(fail-closed),
        # 页面根本不会发出去——不能让人在「看不到源码」的情况下批准代码。
        # 脱敏失败时必须把已绑定的 socket 关掉,否则会留下一个空转的监听端口(第 5 轮 codex 的 L2)。
        app = ApprovalApp(
            path=path, raw=raw, source=source, sha=sha, findings=findings,
            diff_text=diff[1] if diff else None, parent=diff[0] if diff else None,
            approvals_file=approvals_file, token=secrets.token_urlsafe(24),
            csrf=secrets.token_urlsafe(24), port=actual_port,
        )
    except BaseException:
        httpd.server_close()
        raise
    httpd.RequestHandlerClass = type("BoundHandler", (ApprovalHandler,), {"app": app})
    return httpd, app


def serve(strategy: str, port: int, timeout: int,
          hide_url: bool = False, open_browser: bool = True) -> int:
    cfg = load_config()
    paths = rc.resolve_paths(cfg, ROOT)
    pending_dir = cfg["paths"]["pending_dir"]
    path = resolve_candidate(strategy, pending_dir)
    # 名字规则:网关会拒的文件,批准页也不该收(否则同一个文件两个地方口径不一致)。
    pattern = cfg["candidates"]["name_pattern"]
    if not re.fullmatch(pattern, path.name):
        raise ApprovalError(f"候选文件名不符合规则({pattern}): {path.name}")
    raw, source = read_source(path)
    max_bytes = int(cfg["candidates"]["max_source_bytes"])
    if len(raw) > max_bytes:
        raise ApprovalError(f"候选源码超过上限({len(raw)} > {max_bytes} 字节),不予批准")
    findings = rs.screen_source(source, cfg["screening"]["extra_forbidden_modules"],
                                cfg["screening"]["extra_forbidden_calls"],
                                cfg["screening"]["allowed_modules"])
    # 哈希由**刚读出来的那份字节**算:页面显示的源码与账本里记的哈希必须是同一份内容。
    # 分两次读的话,两次读之间文件被改,人看到的是 A、批准的却是 B。
    sha = hashlib.sha256(raw).hexdigest()
    diff = build_diff(path, source)

    httpd, app = build_server(path=path, raw=raw, source=source, sha=sha, findings=findings,
                              diff=diff, approvals_file=paths["approvals_file"], port=port)
    actual_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/a/{app.token}"
    # **URL 只发给浏览器,不落盘、不进模型通道**(第 5 轮 codex 的 H1):
    # 之前 MCP 把它写进 `.local/research-mcp/approval-page.txt`,而那个文件是
    # 「模型没有本地文件工具」这个**未经验证**的前提下才安全的。现在这条前提不再需要:
    # 批准进程自己开浏览器,URL 压根不离开这个进程。
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                                   # noqa: BLE001 - 开浏览器失败不该中断批准流程
            pass
    # **stdout 里既没有哈希,也没有页面地址**(第 7 轮 codex 的 F3):
    # stdout 是模型可见通道的候选——人把终端输出粘进聊天窗口是极常见的动作,
    # 而页面地址里带着一次性令牌:模型拿到地址就能自己 GET 页面,把 csrf 令牌与候选哈希
    # 一次凑齐并 POST 成功——「人批准」当场变成「模型自我批准」。
    # 哈希在**页面**上给人核对;地址只走浏览器;两者要落到本机 stdout 上时,走 stderr。
    ready = {"event": "ready", "strategy": path.name,
             "screening_passed": not findings, "findings": findings}
    print(json.dumps(ready, ensure_ascii=False), flush=True)
    if not hide_url:
        # 人在终端直接跑这个脚本时,地址要给人看(浏览器没自动打开的话要手动复制)——
        # 但只走 stderr(人的通道),并带上「不要粘进聊天窗口」的提示。
        print("批准页地址(只在本机 stderr,**不要粘进聊天窗口**):", url, file=sys.stderr, flush=True)
        print("核对用的 sha256 在页面上;本页地址含一次性令牌,泄露等于把批准权交出去。",
              file=sys.stderr, flush=True)
    timer = threading.Timer(timeout, httpd.shutdown)
    timer.daemon = True
    timer.start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        timer.cancel()
        httpd.server_close()
    if app.decision == "approved":
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="研究候选的带外批准进程(只监听 127.0.0.1)")
    parser.add_argument("--strategy", required=True, help="候选文件名(可省略 .py)")
    parser.add_argument("--port", type=int, default=0, help="监听端口,0 表示自动分配")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="多少秒后无人批准就退出")
    parser.add_argument("--hide-url", action="store_true",
                        help="不在 stdout 的 ready 事件里带页面地址(被 MCP 拉起时用:地址不外流)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器(测试用)")
    args = parser.parse_args(argv)
    try:
        return serve(args.strategy, args.port, args.timeout,
                     hide_url=args.hide_url, open_browser=not args.no_browser)
    except ApprovalError as exc:
        print(json.dumps({"event": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    except ScrubUnavailable as exc:
        # 脱敏不可用:**不发页面**(fail-closed)。让人在没有源码的情况下批准,等于没批准。
        print(json.dumps({"event": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "error", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    rconsole.reconfigure_stdio()
    sys.exit(main())
