"""专用 research profile 的边界检查器(Issue #57)。

输入:`dsh --profile <p> --patch dsh-research-preset.patch.yml --patch dsh-research-profile.patch.yml --dump-config`
的输出(YAML)。输出:问题清单;空清单 = 通过。

**为什么要有它**:ADR 20261113 第 3 条「批准只能由人做」成立于前提——research 会话里的模型
没有本机文件读写、命令执行、桌面操控能力。`dsh-agent-preset` 没有「不继承全局插件」的配置项,
所以这条前提只能由**整个 profile 的组装结果**保证,而不是由 Preset 模板保证
(2026-11-19:同一份 Preset 装进日常 Desktop profile,模型可见工具 130+ 个)。

**判据是白名单,不是黑名单**(ADR 史上名字黑名单反复被绕过):
1. 启用的 Agent Preset 恰好一个,id 为 `research`,插件恰为 `[persona, research-mcp]`;
2. `research-mcp` 是 `dsh-mcp-client`,serverName 为 `research`,启动的是 `tools/research_mcp/server.py`;
3. Preset 之外每个**启用**的条目,id 都必须在白名单里——没列进去的一律判失败(fail-closed);
4. 任何地方都不得有 `research-mcp` 以外的启用的 `dsh-mcp-client` 条目。

`disabled` 的 `!!js` 表达式只精确求值 `process.platform` 两种写法;其余一律按**启用**算(保守)。

用法:
    python tools/research_mcp/profile_check.py <dump.yml>     # 0 通过 / 1 有问题 / 2 输入不可用
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ALLOWLIST_FILE = Path(__file__).resolve().with_name("research-profile-allowlist.json")
PRESET_PACKAGE = "@deepseek-ai/dsh-agent-preset"
MCP_PACKAGE = "@deepseek-ai/dsh-mcp-client"
EXPECTED_PRESET_PLUGINS = ["persona", "research-mcp"]
SERVER_SUFFIX = "/tools/research_mcp/server.py"


class Js(str):
    """YAML 里 `!!js` 标签的值(DSH 运行期求值的 JS 表达式)。"""


def load_dump(text: str) -> list:
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: Js(loader.construct_scalar(node)))
    data = yaml.load(text, Loader=_Loader)
    if not isinstance(data, list):
        raise ValueError("dump 不是条目列表")
    return data


def is_disabled(entry: dict, platform: str = sys.platform) -> bool:
    """条目是否被禁用。认不出的 JS 条件 ⇒ 按启用算(fail-closed:宁可多报,不可漏报)。"""
    flag = entry.get("disabled", False)
    if isinstance(flag, Js):
        expr = flag.strip()
        on_windows = platform == "win32"
        if expr == "process.platform === 'win32'":
            return on_windows
        if expr == "process.platform !== 'win32'":
            return not on_windows
        return False
    return flag is True


def _name(entry: dict) -> str:
    return str(entry.get("name") or "")


def _plugins(entry: dict) -> list:
    config = entry.get("config")
    plugins = config.get("plugins") if isinstance(config, dict) else None
    return plugins if isinstance(plugins, list) else []


def _check_research_mcp(plugin: dict) -> list[str]:
    problems = []
    if _name(plugin) != MCP_PACKAGE:
        problems.append(f"research-mcp 的包名应为 {MCP_PACKAGE},实际 {_name(plugin)!r}")
    config = plugin.get("config") if isinstance(plugin.get("config"), dict) else {}
    if config.get("serverName") != "research":
        problems.append(f"research-mcp 的 serverName 应为 'research',实际 {config.get('serverName')!r}")
    args = config.get("args") if isinstance(config.get("args"), list) else []
    first = str(args[0]).replace("\\", "/") if args else ""
    if SERVER_SUFFIX not in first:
        problems.append("research-mcp 启动的不是 tools/research_mcp/server.py")
    return problems


def check(dump: list, allowed_ids: set[str], platform: str = sys.platform) -> list[str]:
    problems: list[str] = []
    enabled_presets = []
    for entry in dump:
        if not isinstance(entry, dict) or is_disabled(entry, platform):
            continue
        entry_id = str(entry.get("id") or "")
        if _name(entry) == PRESET_PACKAGE:
            enabled_presets.append(entry)
            continue
        if _name(entry) == MCP_PACKAGE:
            problems.append(f"全局层有启用的 MCP 条目 {entry_id!r}(只允许 research Preset 内的 research-mcp)")
            continue
        if entry_id not in allowed_ids:
            problems.append(f"未列入白名单的启用条目:{entry_id!r}({_name(entry)})")

    ids = [str((p.get("config") or {}).get("id")) for p in enabled_presets]
    if ids != ["research"]:
        problems.append(f"启用的 Agent Preset 必须恰为 ['research'],实际 {ids}")
    for preset in enabled_presets:
        if (preset.get("config") or {}).get("id") != "research":
            continue
        active = [p for p in _plugins(preset) if isinstance(p, dict) and not is_disabled(p, platform)]
        got = [str(p.get("id")) for p in active]
        if got != EXPECTED_PRESET_PLUGINS:
            problems.append(f"research Preset 的插件必须恰为 {EXPECTED_PRESET_PLUGINS},实际 {got}")
        for plugin in active:
            if plugin.get("id") == "research-mcp":
                problems.extend(_check_research_mcp(plugin))
            elif _name(plugin) == MCP_PACKAGE:
                problems.append(f"research Preset 里有别的 MCP 条目 {plugin.get('id')!r}")
    return problems


def load_allowlist(path: Path = ALLOWLIST_FILE) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data.get("enabled_global_ids")
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValueError("白名单文件缺 enabled_global_ids 字符串列表")
    return set(ids)


def _utf8_stderr() -> None:
    """Windows 控制台默认 cp1252:中文会被打成 \\uXXXX 或直接抛 UnicodeEncodeError。"""
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stderr()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("用法:profile_check.py <dump.yml>", file=sys.stderr)
        return 2
    try:
        dump = load_dump(Path(args[0]).read_text(encoding="utf-8"))
        allowed = load_allowlist()
    except Exception as exc:                                  # noqa: BLE001 - 判不了 = 不放行
        print(f"[profile_check] 输入不可用:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    problems = check(dump, allowed)
    for line in problems:
        print("[profile_check] " + line, file=sys.stderr)
    if problems:
        print(f"[profile_check] 未通过:{len(problems)} 项", file=sys.stderr)
        return 1
    print("[profile_check] 通过", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
