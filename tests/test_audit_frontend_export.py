"""tools/audit_frontend_export.py:用模拟的前端导出物验证审计闸门。"""
import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("audit_frontend_export", Path(__file__).resolve().parents[1] / "tools" / "audit_frontend_export.py")
a = importlib.util.module_from_spec(SPEC)
sys.modules["audit_frontend_export"] = a      # @dataclass 需要模块已登记
SPEC.loader.exec_module(a)

CLEAN_HTML = '<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css"></head><body><div id="root"></div><script type="module" src="/assets/app.js"></script></body></html>'


def _dist(tmp_path, html=CLEAN_HTML, js="console.log('ok')"):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(html, encoding="utf-8")
    (d / "assets" / "app.js").write_text(js, encoding="utf-8")
    (d / "assets" / "app.css").write_text("body{margin:0}", encoding="utf-8")
    return d


def _src(tmp_path, files: dict, deps: dict | None = None):
    root = tmp_path / "proj"
    src = root / "src"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"dependencies": deps or {"react": "^18"}}), encoding="utf-8")
    return src


def _levels(findings):
    return [(f.level, f.what) for f in findings]


def test_clean_dist_passes(tmp_path):
    assert a.audit_dist(_dist(tmp_path)) == []


def test_lovable_style_external_edit_script_is_rejected(tmp_path):
    html = CLEAN_HTML.replace("<body>", '<body><script src="https://cdn.gpteng.co/gptengineer.js" type="module"></script>')
    errors = [f for f in a.audit_dist(_dist(tmp_path, html)) if f.level == "error"]
    assert any("gpteng.co" in f.what for f in errors) and any("外链脚本" in f.what for f in errors)


def test_inline_script_is_rejected_but_json_ld_style_empty_script_is_not(tmp_path):
    html = CLEAN_HTML.replace("<body>", "<body><script>window.x=1</script>")
    assert any("内联" in f.what for f in a.audit_dist(_dist(tmp_path, html)))


def test_external_fonts_and_supabase_urls_in_bundles_are_rejected(tmp_path):
    js = 'fetch("https://abcd.supabase.co/rest/v1/x");const f="https://fonts.googleapis.com/css2?family=Inter"'
    errors = [f for f in a.audit_dist(_dist(tmp_path, js=js)) if f.level == "error"]
    assert any("supabase" in f.what for f in errors) and any("googleapis" in f.what for f in errors)


def test_known_library_constants_and_xml_namespaces_are_allowed(tmp_path):
    js = 'var ns="http://www.w3.org/2000/svg";var e="https://react.dev/errors/31"'
    assert a.audit_dist(_dist(tmp_path, js=js)) == []


def test_unknown_external_host_is_an_error(tmp_path):
    assert any(f.level == "error" for f in a.audit_dist(_dist(tmp_path, js='var u="https://tracker.example.org/p.js"')))


def test_source_rules(tmp_path):
    src = _src(tmp_path, {
        "components/Bad.tsx": "export const B=({h}:{h:string})=><div dangerouslySetInnerHTML={{__html:h}}/>;",
        "pages/Token.tsx": "localStorage.setItem('auth_token', t); localStorage.setItem('theme','dark');",
        "pages/Fetch.tsx": "const r = await fetch('/api/trading');",
        "api/client.ts": "export const get = (p:string) => fetch(p);",
        "pages/Btn.tsx": "export const X = () => <button>一键全平</button>;",
    }, deps={"react": "^18", "@supabase/supabase-js": "^2"})
    f = _levels(a.audit_src(src, "src/api"))
    text = "\n".join(w for _, w in f)
    assert ("error", "禁止使用 dangerouslySetInnerHTML(服务器文本必须按纯文本渲染)") in f
    assert "令牌写入 localStorage" in text
    assert any(lvl == "warn" and "'theme'" in w for lvl, w in f)           # 主题偏好只是 warn
    assert "在 API 客户端目录(src/api)之外发起请求" in text
    assert text.count("API 客户端目录") == 1                                 # api/client.ts 内的 fetch 不报
    assert "禁止的依赖 @supabase/supabase-js" in text
    assert any(lvl == "warn" and "一键全平" in w for lvl, w in f)


def test_clean_source_passes_even_in_strict_mode(tmp_path):
    src = _src(tmp_path, {"api/client.ts": "export const get=(p:string)=>fetch(p,{headers:{}})", "App.tsx": "export default function A(){return <p>ok</p>}"})
    dist = _dist(tmp_path)
    assert a.main(["--dist", str(dist), "--src", str(src), "--strict"]) == 0


def test_cli_exit_codes(tmp_path, capsys):
    bad = _dist(tmp_path, CLEAN_HTML.replace("<body>", '<body><script src="//cdn.example.net/x.js"></script>'))
    assert a.main(["--dist", str(bad)]) == 1
    assert a.main([]) == 2
    assert a.main(["--dist", str(tmp_path / "nope")]) == 2
    assert "未通过" in capsys.readouterr().out


def test_warn_only_fails_in_strict_mode(tmp_path):
    src = _src(tmp_path, {"api/client.ts": "export const x=1", "P.tsx": "const t='Approve button text'"})
    assert a.main(["--src", str(src)]) == 0
    assert a.main(["--src", str(src), "--strict"]) == 1
