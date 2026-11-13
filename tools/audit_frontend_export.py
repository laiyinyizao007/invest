"""前端导出物审计:检查 Lovable(或任何工具)导出的前端是否满足状态页的硬性约束。

用法:
    python tools/audit_frontend_export.py --dist <构建产物目录> [--src <源码目录>] [--api-dir src/api] [--strict]

检查项(error 必须清零;warn 需要人工看一眼,--strict 时也算失败):
  dist:  外部 URL(含已知的 Lovable/Supabase/统计/CDN 域名)、内联 <script>、外链 <script>/<link>、eval / new Function
  src:   dangerouslySetInnerHTML / innerHTML / document.write、令牌写入 localStorage、
         Supabase 与统计类依赖、API 客户端之外的 fetch/axios/XMLHttpRequest、交易类控件文案
退出码:0 通过;1 有 error(或 --strict 下有 warn);2 参数错误。
约束来源:docs/web/03-frontend-handoff.md §2 与 docs/web/02-api-contract.md §8。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

TEXT_EXT = {".html", ".js", ".mjs", ".css", ".json", ".svg", ".map"}
SRC_EXT = {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".html", ".css"}

# 允许出现在产物里的域名:XML 命名空间与主流库写在代码里的文档/错误链接(不会发请求)。
ALLOWED_HOSTS = {"www.w3.org", "w3.org", "react.dev", "reactjs.org", "www.reactjs.org", "reactrouter.com",
                 "radix-ui.com", "www.radix-ui.com", "localhost", "127.0.0.1", "example.com"}
KNOWN_BAD = ("gpteng.co", "lovable.dev", "lovable.app", "lovableproject.com", "supabase.co", "supabase.com",
             "googleapis.com", "gstatic.com", "google-analytics.com", "googletagmanager.com", "jsdelivr.net",
             "unpkg.com", "cdnjs.cloudflare.com", "posthog", "sentry.io", "mixpanel", "amplitude", "hotjar", "clarity.ms")
BAD_DEPS = ("@supabase/", "supabase", "posthog", "@sentry/", "mixpanel", "amplitude", "hotjar", "react-ga", "@vercel/analytics", "gtag")
TRADING_WORDS = ("下单", "撤单", "一键全平", "紧急制动", "批准上线", "一键回滚", "Panic", "Approve", "Rollback")

URL_RE = re.compile(r"""(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s"'`)<>\\]*""")
HTTP_RE = re.compile(r"https?://[^\s\"'`)<>\\]+")


@dataclass
class Finding:
    level: str          # error | warn
    where: str
    what: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.what}"


def _files(root: Path, exts: set[str]):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts and "node_modules" not in p.parts:
            yield p


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def audit_dist(dist: Path) -> list[Finding]:
    out: list[Finding] = []
    for p in _files(dist, TEXT_EXT):
        rel = p.relative_to(dist).as_posix()
        text = _read(p)
        for m in HTTP_RE.finditer(text):
            host = (urlparse(m.group(0)).hostname or "").lower()
            if host in ALLOWED_HOSTS:
                continue
            bad = next((b for b in KNOWN_BAD if b in host), None)
            out.append(Finding("error", rel, f"外部 URL {m.group(0)[:80]}" + (f"(已知禁止:{bad})" if bad else "")))
        if p.suffix.lower() == ".html":
            for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text, re.S | re.I):
                attrs, body = m.group(1), m.group(2).strip()
                src = re.search(r"""src\s*=\s*["']([^"']+)""", attrs)
                if src is None and body:
                    out.append(Finding("error", rel, "内联 <script>(与 CSP script-src 'self' 冲突)"))
                if src and re.match(r"(?:https?:)?//", src.group(1)):
                    out.append(Finding("error", rel, f"外链脚本 {src.group(1)[:80]}"))
            for m in re.finditer(r"<link\b[^>]*href\s*=\s*[\"']((?:https?:)?//[^\"']+)", text, re.I):
                out.append(Finding("error", rel, f"外链资源 {m.group(1)[:80]}"))
        if p.suffix.lower() in {".js", ".mjs"}:
            if re.search(r"\beval\s*\(", text):
                out.append(Finding("warn", rel, "出现 eval( —— 若来自第三方库需确认不在运行路径上,CSP 会拦截"))
            if re.search(r"new\s+Function\s*\(", text):
                out.append(Finding("warn", rel, "出现 new Function( —— 同上"))
    return out


def audit_src(src: Path, api_dir: str) -> list[Finding]:
    out: list[Finding] = []
    pkg = src.parent / "package.json" if (src.parent / "package.json").exists() else src / "package.json"
    if pkg.exists():
        data = json.loads(_read(pkg) or "{}")
        for section in ("dependencies", "devDependencies"):
            for name in data.get(section, {}):
                if any(b in name for b in BAD_DEPS):
                    out.append(Finding("error", f"package.json({section})", f"禁止的依赖 {name}(外部后端/统计)"))
        if "lovable-tagger" in data.get("devDependencies", {}) or "lovable-tagger" in data.get("dependencies", {}):
            out.append(Finding("warn", "package.json", "lovable-tagger:仅应在开发构建里启用,确认生产产物不含 data-lov-* 属性"))
    for p in _files(src, SRC_EXT):
        rel = p.relative_to(src).as_posix()
        text = _read(p)
        for pat, why in ((r"dangerouslySetInnerHTML", "dangerouslySetInnerHTML"), (r"\.innerHTML\b", "innerHTML"),
                         (r"\bv-html\b", "v-html"), (r"document\.write\s*\(", "document.write")):
            if re.search(pat, text):
                out.append(Finding("error", rel, f"禁止使用 {why}(服务器文本必须按纯文本渲染)"))
        for m in re.finditer(r"localStorage\s*\.\s*setItem\s*\(\s*[\"']([^\"']+)", text):
            key = m.group(1)
            if re.search(r"token|auth|secret|password|bearer", key, re.I):
                out.append(Finding("error", rel, f"令牌写入 localStorage(键 {key!r});只允许 sessionStorage"))
            else:
                out.append(Finding("warn", rel, f"localStorage 写入 {key!r}:仅允许主题等偏好,请确认"))
        if re.search(r"""sessionStorage[^;\n]*\n?[^;\n]*console\.""", text) and re.search(r"console\.\w+\([^)]*token", text, re.I):
            out.append(Finding("error", rel, "疑似把令牌打到控制台"))
        if not rel.startswith(api_dir.rstrip("/").removeprefix("src/") + "/") and not rel.startswith(api_dir.rstrip("/") + "/"):
            if re.search(r"\bfetch\s*\(|\baxios\b|XMLHttpRequest", text):
                out.append(Finding("error", rel, f"在 API 客户端目录({api_dir})之外发起请求"))
        for w in TRADING_WORDS:
            if w in text:
                out.append(Finding("warn", rel, f"出现交易类文案 {w!r}:确认不是按钮/表单/菜单(解释性文字可忽略)"))
        for m in URL_RE.finditer(text):
            host = (urlparse(m.group(0) if m.group(0).startswith("http") else "https:" + m.group(0)).hostname or "").lower()
            if host and host not in ALLOWED_HOSTS and not host.endswith((".ts", ".tsx", ".js", ".css")):
                bad = next((b for b in KNOWN_BAD if b in host), None)
                if bad:
                    out.append(Finding("error", rel, f"源码中的外部地址 {m.group(0)[:80]}(已知禁止:{bad})"))
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dist", type=Path, help="构建产物目录(如 dist/)")
    ap.add_argument("--src", type=Path, help="源码目录(如 src/)")
    ap.add_argument("--api-dir", default="src/api", help="唯一允许发起请求的目录(相对项目根)")
    ap.add_argument("--strict", action="store_true", help="warn 也算失败")
    args = ap.parse_args(argv)
    if not args.dist and not args.src:
        print("至少给 --dist 或 --src 之一", file=sys.stderr)
        return 2
    for label, path in (("--dist", args.dist), ("--src", args.src)):
        if path and not path.is_dir():
            print(f"{label} 不是目录: {path}", file=sys.stderr)
            return 2
    findings: list[Finding] = []
    if args.dist:
        findings += audit_dist(args.dist)
    if args.src:
        findings += audit_src(args.src, args.api_dir)
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]
    for f in findings:
        print(f)
    print(f"\n审计结果:{len(errors)} 个 error,{len(warns)} 个 warn")
    if errors or (args.strict and warns):
        print("未通过。")
        return 1
    print("通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
