"""状态页服务入口(只读 Web 监控面板)。

用法(项目根目录):
    python scripts/status_server.py --check          # 只校验配置,不启动
    python scripts/status_server.py                  # 启动,监听 127.0.0.1:8770(可在 config/status_dashboard.json 改端口)

- 只监听本机回环地址(配置层强制;经局域网访问属于阶段⑤,须另写 ADR)。
- 令牌文件默认 data/dashboard/status.token(面板专属目录,不在交易侧目录树里),不存在则自动生成。**令牌内容不会被打印**;浏览器里打开页面后手工输入。
- 本进程不连接交易进程、不读取 .env、不 import vnpy / trading;杀掉它不影响交易。
- 依赖见 requirements-dashboard.txt(与交易环境隔离)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import auth, config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="只校验配置并打印摘要,不启动服务")
    ap.add_argument("--config", type=Path, default=None, help="配置文件(默认 config/status_dashboard.json,可不存在)")
    ap.add_argument("--root", type=Path, default=ROOT, help="项目根(路径解析的基准;测试用,默认仓库根)")
    ap.add_argument("--port", type=int, default=None, help="覆盖配置里的端口(0 = 随机空闲端口,测试用)")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        settings = config.load(path=args.config, root=args.root)
        if args.port is not None:
            if not 0 <= args.port <= 65535:
                raise config.ConfigError("--port 必须在 0-65535 之间")
            settings.raw["port"] = args.port
    except config.ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    env = settings.environment
    print(f"环境标签: {env['label']}({env['display']})  监听: {settings.raw['host']}:{settings.raw['port']}")
    print(f"状态目录: {settings.paths['state_dir'].relative_to(settings.root)}   日志目录: "
          + ", ".join(str(p.relative_to(settings.root)) for p in settings.paths["log_dirs"]))
    if env["label"] == "unknown":
        print("提示: environment 未配置,页面横幅会显示『环境未知』。请在 config/status_dashboard.json 里设置 simnow / guoxin-test / live。")
    if args.check:
        print("配置校验通过(--check 不启动服务)。")
        return 0

    try:
        token, created = auth.load_or_create_token(settings.paths["token_file"])
    except auth.TokenError as exc:
        print(f"令牌错误: {exc}", file=sys.stderr)
        return 2
    rel = settings.paths["token_file"].relative_to(settings.root)
    print(f"令牌{'已生成' if created else '已读取'}: {rel}(内容不打印;在浏览器的令牌输入框里粘贴它)")

    try:
        import uvicorn

        from dashboard.app import create_app
    except ImportError as exc:
        print(f"缺少 Web 依赖: {exc}。请安装 requirements-dashboard.txt(与交易环境隔离)。", file=sys.stderr)
        return 3
    app = create_app(settings, token)
    from dashboard import collectors
    collectors.warm_up(settings)                 # 日志可能很大:启动时先解析一遍,第一个请求就不用等
    if app.state.site.rejected:
        print(f"警告: 前端清单里有 {len(app.state.site.rejected)} 个文件缺失或哈希不符,不会被托管: {app.state.site.rejected[:5]}", file=sys.stderr)
    if not app.state.site.has_index:
        print("提示: 尚未找到前端构建产物(dashboard/static/manifest.json),将显示占位页;API 可正常使用。")
    uvicorn.run(app, host=settings.raw["host"], port=settings.raw["port"], log_level="warning", access_log=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
