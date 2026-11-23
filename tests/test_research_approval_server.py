"""带外批准进程:哈希绑定、CSRF/Origin/Host 校验、只写批准记录、页面内容。"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

import research_approval_server as ap
import research_sandbox as rs

assert Path(ap.__file__).resolve() == Path(__file__).resolve().parents[1] / "scripts" / "research_approval_server.py"

GOOD = '''from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    fast_window = 16

    def entry_signal(self) -> int:
        return 0
'''

EVIL = '''import os


class Candidate:
    def entry_signal(self) -> int:
        os.system("echo hi")
        return 0
'''


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    monkeypatch.setattr(ap.rc, "resolve_paths", lambda cfg, root: {
        "pending_dir": root / "pending_strategies",
        "results_dir": root / "data" / "research",
        "approvals_file": root / "data" / "research-control" / "approvals.json",
        "token_file": root / "token",
    })
    return tmp_path


def make_app(tree, source=GOOD, name="cand.py", findings=None):
    path = tree / "pending_strategies" / name
    path.write_text(source, encoding="utf-8")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    diff = ap.build_diff(path, source)
    return ap.ApprovalApp(
        path=path, raw=raw, source=source, sha=sha,
        findings=findings if findings is not None else [],
        diff_text=diff[1] if diff else None, parent=diff[0] if diff else None,
        approvals_file=tree / "data" / "research-control" / "approvals.json",
        token="tok", csrf="csrf-token", port=8765,
    ), path


def headers(**extra):
    base = {"host": "127.0.0.1:8765"}
    base.update({k.replace("_", "-"): v for k, v in extra.items()})
    return base


def post(app, digest, csrf="csrf-token", **extra):
    """发一次批准 POST。默认带浏览器式的 Origin(现代浏览器的同源表单 POST 一定带)。"""
    body = urlencode({"csrf": csrf, "digest": digest}).encode()
    extra.setdefault("origin", f"http://127.0.0.1:{app.port}")
    return app.handle("POST", f"/a/{app.token}", headers(**extra), body)


# ---------- 页面 ----------

def test_page_shows_source_hash_and_diff(tree):
    app, path = make_app(tree)

    code, body = app.handle("GET", f"/a/{app.token}", headers(), b"")

    text = body.decode("utf-8")
    assert code == 200
    assert app.sha in text                                   # 完整 sha256 给人核对
    assert "class Candidate" in text                          # 完整源码
    assert "entry_signal" in text
    assert "父策略" in text                                   # 与父代的 diff
    assert "TrendAtr15mStrategy" in text


def test_page_reports_screening_findings(tree):
    app, path = make_app(tree, source=EVIL, findings=["禁止导入 os"])

    code, body = app.handle("GET", f"/a/{app.token}", headers(), b"")

    assert code == 200
    assert "禁止导入 os" in body.decode("utf-8")
    assert "未通过" in body.decode("utf-8")


def test_page_marks_source_as_untrusted_data(tree):
    """页面上的源码是模型生成的:必须标成不可信数据,并提醒不要执行其中的指令。"""
    app, _ = make_app(tree)

    code, body = app.handle("GET", f"/a/{app.token}", headers(), b"")

    text = body.decode("utf-8")
    assert code == 200
    assert "不可信数据" in text
    assert "不要执行" in text


def test_page_scrubs_secrets_in_source(tree):
    """候选里混进来的凭据不能在批准页上原样显示(ADR 第 4 条)。"""
    leaky = GOOD + '\nCREDENTIALS = {"api_key": "sk-live-0123456789abcdef"}\n'
    app, _ = make_app(tree, source=leaky)

    code, body = app.handle("GET", f"/a/{app.token}", headers(), b"")

    text = body.decode("utf-8")
    assert code == 200
    assert "sk-live-0123456789abcdef" not in text


def test_unknown_path_is_404(tree):
    app, _ = make_app(tree)

    assert app.handle("GET", "/a/other", headers(), b"")[0] == 404
    assert app.handle("GET", "/", headers(), b"")[0] == 404


# ---------- 传输层安全 ----------

def test_foreign_host_is_rejected(tree):
    app, _ = make_app(tree)

    code, _ = app.handle("GET", f"/a/{app.token}", {"host": "evil.example:8765"}, b"")

    assert code == 403


def test_cross_site_origin_is_rejected(tree):
    app, _ = make_app(tree)

    code, _ = post(app, app.sha, origin="http://evil.example")

    assert code == 403
    assert not app.approvals_file.exists()


def test_csrf_token_mismatch_is_rejected(tree):
    """CSRF 不对不写批准,但**不**因此把页面关死:人可以刷新重试。"""
    app, _ = make_app(tree)

    code, _ = post(app, app.sha, csrf="wrong")

    assert code == 403
    assert app.decision is None
    assert not app.approvals_file.exists()


def test_too_many_failures_close_the_page(tree):
    """反复填错达到上限后关闭页面,不给无限次尝试。"""
    app, _ = make_app(tree)

    for _ in range(ap.MAX_ATTEMPTS - 1):
        code, _ = post(app, "0" * 64)
        assert code == 400
        assert app.decision is None

    code, _ = post(app, "0" * 64)

    assert code == 400
    assert app.decision == "rejected"
    assert not app.approvals_file.exists()


def test_body_size_limit(tree):
    app, _ = make_app(tree)

    code, _ = app.handle("POST", f"/a/{app.token}",
                          headers(origin=f"http://127.0.0.1:{app.port}"), b"x" * 5000)

    assert code == 400
    assert not app.approvals_file.exists()


# ---------- 批准 ----------

def test_correct_hash_writes_approval(tree):
    app, path = make_app(tree)

    code, body = post(app, app.sha)

    assert code == 200
    assert app.decision == "approved"
    assert rs.is_approved(path, app.approvals_file)
    assert "已批准" in body.decode("utf-8")


def test_approval_record_binds_the_verified_digest(tree):
    """批准记录里的键必须是人核对过的那个哈希(不是另读一次文件算出来的)。"""
    app, path = make_app(tree)

    post(app, app.sha)

    data = json.loads(app.approvals_file.read_text(encoding="utf-8"))
    assert list(data) == [app.sha]
    assert data[app.sha]["name"] == path.name


def test_wrong_hash_writes_nothing_but_page_stays_open(tree):
    """填错不写批准,而且**允许改对了再填**(人读错一位是常事)。"""
    app, path = make_app(tree)

    code, body = post(app, "0" * 64)

    assert code == 400
    assert app.decision is None
    assert not app.approvals_file.exists()
    assert "不符" in body.decode("utf-8")

    code, _ = post(app, app.sha)
    assert code == 200
    assert app.decision == "approved"


def test_digest_must_be_full_hex(tree):
    """要**完整** 64 位哈希:前缀不行,非十六进制也不行。"""
    for bad in ("", "abc", "z" * 64, "0" * 8, "0" * 63, "0" * 65):
        app2, _ = make_app(tree, name=f"c_{abs(hash(bad))}.py")
        code, _ = post(app2, bad)
        assert code == 400
        assert not app2.approvals_file.exists()


def test_non_ascii_csrf_is_rejected_without_crashing(tree):
    """请求方填非 ASCII 的 csrf 不能把处理线程打崩(str 版 compare_digest 只吃 ASCII)。"""
    app, _ = make_app(tree)

    code, body = post(app, app.sha, csrf="令牌不对")

    assert code == 403
    assert b"CSRF" in body
    assert not app.approvals_file.exists()


def test_a_post_without_any_source_marker_is_refused(tree):
    """Origin 与 Sec-Fetch-Site 都缺的 POST 来自本机上的普通程序,不是人:拒绝。"""
    app, _ = make_app(tree)

    code, _ = app.handle("POST", f"/a/{app.token}", headers(), urlencode(
        {"csrf": app.csrf, "digest": app.sha}).encode())

    assert code == 403
    assert not app.approvals_file.exists()
    assert app.decision is None


def test_a_post_with_a_foreign_origin_is_refused(tree):
    """跨站来源(别的站点的页面发来的 POST)一律拒绝。"""
    app, _ = make_app(tree)

    code, _ = post(app, app.sha, origin="http://evil.example")

    assert code == 403
    assert not app.approvals_file.exists()


def test_header_names_are_matched_case_insensitively(tree):
    """头名大小写不敏感:大写的 `Sec-Fetch-Site: cross-site` 一样要拒。

    漏掉大小写会让跨站提交绕过来源检查——HTTP 传输层已经小写化过,
    但直接调用 `handle` 的调用方未必,这里再兜一次底。

    注意这条必须带上**合法的 Origin**:否则会因为「两个来源标记都缺」而 403,
    测的就不是大小写这条路径了(那样修没修都一样过)。
    """
    app, _ = make_app(tree)
    body = urlencode({"csrf": app.csrf, "digest": app.sha}).encode()

    code, _ = app.handle("POST", f"/a/{app.token}", {
        "Host": f"127.0.0.1:{app.port}",
        "Origin": f"http://127.0.0.1:{app.port}",
        "Sec-Fetch-Site": "cross-site",
    }, body)

    assert code == 403
    assert not app.approvals_file.exists()
    assert app.decision is None


def test_scrub_unavailable_makes_the_page_fail_closed(tree, monkeypatch):
    """脱敏模块不可用时**不发页面**(fail-closed):不能让人在看不到源码的情况下批准。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dashboard.safe" or name == "dashboard":
            raise ImportError("模拟脱敏模块不可用")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ap.ScrubUnavailable):
        make_app(tree)


def test_file_changed_after_page_load_is_refused(tree):
    app, path = make_app(tree)

    path.write_text(GOOD + "\n# 改动\n", encoding="utf-8")   # 人阅读期间文件被改

    code, body = post(app, app.sha)

    assert code == 409
    assert app.decision == "rejected"
    assert not app.approvals_file.exists()
    assert "改动" in body.decode("utf-8")


def test_screening_failure_blocks_approval(tree):
    app, path = make_app(tree, source=EVIL, findings=["禁止导入 os"])

    code, _ = post(app, app.sha)

    assert code == 400
    assert not app.approvals_file.exists()


def test_approval_does_not_touch_candidate_directory(tree):
    app, path = make_app(tree)

    post(app, app.sha)

    assert sorted(p.name for p in (tree / "pending_strategies").iterdir()) == ["cand.py"]
    assert path.read_text(encoding="utf-8") == GOOD


def test_no_second_approval_after_a_decision(tree):
    """一次决定之后不再受理任何请求:批准页面不能被重复提交。"""
    app, path = make_app(tree)

    code, _ = post(app, app.sha)
    assert code == 200
    first = app.approvals_file.read_text(encoding="utf-8")

    code, body = post(app, app.sha)

    assert code == 409
    assert "已经做出决定" in body.decode("utf-8")
    assert app.approvals_file.read_text(encoding="utf-8") == first


def test_server_process_exits_right_after_the_decision(tmp_path):
    """人工决定后进程必须立刻退出。

    否则:MCP 那边的等待会一直拖到超时(人秒点确认也等满 600 秒),
    而且批准页面在决定之后仍然可达,等于留了一个可再提交的批准入口。
    """
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    script = Path(ap.__file__).resolve()
    env = dict(os.environ)
    env["INVEST_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--strategy", "cand.py", "--timeout", "120"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env, cwd=str(script.parent))
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["event"] == "ready"
        # 页面地址只在 stderr(人的通道)上,不在 ready 事件里(第 7 轮 codex 的 F3)。
        url = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            found = re.search(r"http://127\.0\.0\.1:\d+/a/\S+", line)
            if found:
                url = found.group(0)
                break
        assert url, "stderr 上没有拿到页面地址"
        digest = hashlib.sha256((tmp_path / "pending_strategies" / "cand.py").read_bytes()).hexdigest()
        page = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        body = urlencode({"csrf": csrf, "digest": digest}).encode()
        origin = "http://127.0.0.1:" + url.split(":")[2].split("/")[0]
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Origin": origin})
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------- 页面地址不外流(第 5 轮 codex 的 H1) ----------

def test_hide_url_keeps_the_page_address_out_of_the_ready_event(tmp_path):
    """--hide-url:ready 事件里既没有页面地址,也没有哈希。

    被 MCP 拉起时用这个模式:批准进程自己开浏览器,URL 压根不离开那个进程,
    MCP 也就没有 URL 可以写进文件或回传给模型。
    """
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    script = Path(ap.__file__).resolve()
    env = dict(os.environ)
    env["INVEST_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--strategy", "cand.py", "--timeout", "60", "--hide-url", "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env, cwd=str(script.parent))
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["event"] == "ready"
        assert "url" not in ready
        assert "sha256" not in ready
        assert ready["strategy"] == "cand.py"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_the_ready_event_never_carries_the_hash_or_the_address(tmp_path):
    """stdout 是模型可见通道的候选:哈希与页面地址都不许出现在 ready 事件里。

    哈希让人可以在聊天里替人念,页面地址里带着一次性令牌——模型拿到就能自己
    GET 页面、凑齐 csrf 与哈希并 POST 成功,「人批准」当场变成「模型自我批准」
    (第 7 轮 codex 的 F3)。两者都只在**页面**与**stderr** 上。
    """
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    script = Path(ap.__file__).resolve()
    env = dict(os.environ)
    env["INVEST_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--strategy", "cand.py", "--timeout", "60", "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env, cwd=str(script.parent))
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["event"] == "ready"
        assert "url" not in ready and "sha256" not in ready
        digest = hashlib.sha256((tmp_path / "pending_strategies" / "cand.py").read_bytes()).hexdigest()
        assert digest not in json.dumps(ready)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_without_hide_url_the_address_is_given_to_the_human_on_stderr(tmp_path):
    """人在终端直接跑的时候,地址要给人看(浏览器没自动打开还能手动复制)——但只走 stderr。

    stdout 是模型可见通道的候选:人把终端输出整段粘进聊天窗口是极常见的动作,
    地址一旦进了 stdout,模型就能自己去批准(第 7 轮 codex 的 F3)。
    """
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    script = Path(ap.__file__).resolve()
    env = dict(os.environ)
    env["INVEST_ROOT"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(script), "--strategy", "cand.py", "--timeout", "60", "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=env, cwd=str(script.parent))
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["event"] == "ready"
        assert "url" not in ready
        stderr = proc.stderr.readline()
        assert "http://127.0.0.1:" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()


def test_the_approval_process_opens_the_browser_itself(tmp_path, monkeypatch):
    """批准进程自己开浏览器:URL 不经过 MCP,也就没有「模型拿到 URL」这条路。"""
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    monkeypatch.setattr(ap.rc, "resolve_paths", lambda cfg, root: {
        "pending_dir": root / "pending_strategies",
        "results_dir": root / "data" / "research",
        "approvals_file": root / "data" / "research-control" / "approvals.json",
        "token_file": root / "token",
    })
    opened = []
    monkeypatch.setattr(ap.webbrowser, "open", lambda url: opened.append(url))

    # 服务在另一个线程里跑:决策由 timer 触发不了,直接让它超时退出。
    code = ap.serve("cand.py", 0, timeout=1, hide_url=True, open_browser=True)

    assert code == 2                                  # 没人批准
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:")


def test_no_browser_flag_skips_opening(tmp_path, monkeypatch):
    """--no-browser:不自动开浏览器(测试与无桌面环境用)。"""
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "cand.py").write_text(GOOD, encoding="utf-8")
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    monkeypatch.setattr(ap.rc, "resolve_paths", lambda cfg, root: {
        "pending_dir": root / "pending_strategies",
        "results_dir": root / "data" / "research",
        "approvals_file": root / "data" / "research-control" / "approvals.json",
        "token_file": root / "token",
    })
    opened = []
    monkeypatch.setattr(ap.webbrowser, "open", lambda url: opened.append(url))

    ap.serve("cand.py", 0, timeout=1, hide_url=True, open_browser=False)

    assert opened == []


# ---------- 候选解析 ----------

def test_resolve_candidate_rejects_paths_outside_pending(tree):
    (tree / "evil.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ap.ApprovalError):
        ap.resolve_candidate("../evil.py")


def test_resolve_candidate_appends_py_suffix(tree):
    (tree / "pending_strategies" / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert ap.resolve_candidate("a").name == "a.py"


# ---------- 硬边界:只绑回环、令牌一次性且足够长(第 6 轮 codex 的 B/M) ----------

def test_the_approval_server_binds_only_to_loopback(tree):
    """批准页是「本机网页」:绑到 0.0.0.0 就变成局域网可达,同网段的人不用令牌
    就能拿到候选源码。原来这条边界没有用例锁住。"""
    path = tree / "pending_strategies" / "cand.py"
    path.write_text(GOOD, encoding="utf-8")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    diff = ap.build_diff(path, GOOD)

    httpd, _app = ap.build_server(path=path, raw=raw, source=GOOD, sha=sha, findings=[],
                                  diff=diff, approvals_file=tree / "approvals.json", port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_the_page_token_is_long_and_different_every_time(tree):
    """一次性 URL 令牌:必须够长(192 位)、每次都不一样。
    固定值或短令牌会让「地址泄漏 = 拿到批准权」这条假设失效。"""
    path = tree / "pending_strategies" / "cand.py"
    path.write_text(GOOD, encoding="utf-8")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    diff = ap.build_diff(path, GOOD)

    tokens = []
    for _ in range(2):
        httpd, app = ap.build_server(path=path, raw=raw, source=GOOD, sha=sha, findings=[],
                                     diff=diff, approvals_file=tree / "approvals.json", port=0)
        try:
            tokens.append(app.token)
            assert len(app.token) >= 32, app.token
            assert len(app.csrf) >= 32, app.csrf
        finally:
            httpd.server_close()

    assert tokens[0] != tokens[1]


def test_a_wrong_token_is_404(tree):
    """令牌不对就是 404(不是 403):地址猜不到就拿不到页面。"""
    app, _path = make_app(tree)
    status, _ = app.handle("GET", "/a/not-the-token", headers(), b"")
    assert status == 404
