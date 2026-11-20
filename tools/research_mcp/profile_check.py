"""专用 research profile 的边界检查器(Issue #57)。

输入:`dsh --profile research --patch dsh-research-preset.patch.yml --patch dsh-research-profile.patch.yml --dump-config`
的输出(YAML)。输出:问题清单;空清单 = 通过。

**为什么要有它**:ADR 20261113 第 3 条「批准只能由人做」成立于前提——research 会话里的模型
没有本机文件读写、命令执行、桌面操控能力。`dsh-agent-preset` 没有「不继承全局插件」的配置项,
所以这条前提只能由**整个 profile 的组装结果**保证,而不是由 Preset 模板保证
(2026-11-19:同一份 Preset 装进日常 Desktop profile,模型可见工具 130+ 个)。

**判据是白名单,不是黑名单**(ADR 史上名字黑名单反复被绕过),任何一条不满足即失败:
1. dump 的每个顶层条目都必须是带 id 的映射(读不懂 = 不放行);
2. 启用的 Agent Preset 恰好一个,并且与仓库里的 `dsh-research-preset.patch.yml` **逐字段相同**
   (persona 文案、research-mcp 的包名/serverName/启动命令/环境变量全在内——不靠「路径里含某段字符串」);
3. Preset 之外每个**启用**条目都必须在白名单里,且**包名**与白名单记录一致(同一个 id 换了实现也判失败);
4. 任何启用条目的配置里(递归)都不得出现 `research-mcp` 以外的 `dsh-mcp-client`;
5. `@deepseek-ai/dsh-base` 的版本必须等于白名单记录的版本(DSH 升级后须重新审阅,fail-closed)。

`disabled` 的 `!!js` 表达式只精确求值 `process.platform` 两种写法;其余一律按**启用**算(保守)。

用法:
    python tools/research_mcp/profile_check.py <dump.yml>     # 0 通过 / 1 有问题 / 2 输入不可用
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWLIST_FILE = HERE / "research-profile-allowlist.json"
PRESET_TEMPLATE = HERE / "dsh-research-preset.patch.yml"
PRESET_PACKAGE = "@deepseek-ai/dsh-agent-preset"
MCP_PACKAGE = "@deepseek-ai/dsh-mcp-client"
BASE_PACKAGE = "@deepseek-ai/dsh-base"
RESEARCH_PRESET_ID = "preset-research"


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


def load_template_preset(path: Path = PRESET_TEMPLATE) -> dict:
    """仓库里 research Preset 模板的那一条(`- insert:` 下 id 为 preset-research)。"""
    patch = load_dump(path.read_text(encoding="utf-8"))
    for item in patch:
        for entry in (item.get("insert") or []) if isinstance(item, dict) else []:
            if isinstance(entry, dict) and entry.get("id") == RESEARCH_PRESET_ID:
                return entry
    raise ValueError(f"模板里没有 {RESEARCH_PRESET_ID}")


def is_disabled(entry: dict, platform: str | None = None) -> bool:
    """条目是否被禁用。认不出的 JS 条件 ⇒ 按启用算(fail-closed:宁可多报,不可漏报)。"""
    platform = sys.platform if platform is None else platform
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


def _strip_meta(value):
    """去掉 DSH 组装时加的 `__` 开头元数据(如 __dshPluginOwner),其余原样比较。"""
    if isinstance(value, dict):
        return {k: _strip_meta(v) for k, v in value.items() if not str(k).startswith("__")}
    if isinstance(value, list):
        return [_strip_meta(v) for v in value]
    return value


def _nested_mcp(value, platform: str, path: str) -> list[str]:
    """递归找**启用**的 dsh-mcp-client 条目;被禁用的子树整棵不算(它不会被加载)。"""
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("disabled") is not None and is_disabled(value, platform):
            return found
        if _name(value) == MCP_PACKAGE:
            found.append(f"{path}:{value.get('id')!r}")
        for key, child in value.items():
            found += _nested_mcp(child, platform, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found += _nested_mcp(child, platform, f"{path}[{index}]")
    return found


GROUP_NAME = "cordis:group"


def _flatten(dump: list, platform: str, problems: list[str], where: str = "") -> list[dict]:
    """展开 `cordis:group`:分组只是容器,它的 config 是子条目列表,子条目逐个按普通条目判。

    不展开的话,只要有人把**分组的 id** 放进白名单,组里的终端工具之类就整体放行了
    (日常 Desktop 里实有 `optional-persistent-terminals` 组,内含 `optional-tool-terminal`)。
    被禁用的分组整组不加载,不展开。
    """
    out: list[dict] = []
    for index, entry in enumerate(dump):
        if not isinstance(entry, dict) or not entry.get("id"):
            problems.append(f"{where}第 {index} 个条目不是带 id 的映射:{type(entry).__name__}(读不懂 = 不放行)")
            continue
        if _name(entry) == GROUP_NAME:
            if is_disabled(entry, platform):
                continue
            children = entry.get("config")
            if not isinstance(children, list):
                problems.append(f"分组 {entry['id']!r} 的 config 不是子条目列表(读不懂 = 不放行)")
                continue
            out += _flatten(children, platform, problems, f"分组 {entry['id']!r} 的")
            continue
        out.append(entry)
    return out


def check(dump: list, allowlist: dict, template: dict | None = None, platform: str | None = None) -> list[str]:
    platform = sys.platform if platform is None else platform
    template = load_template_preset() if template is None else template
    allowed: dict = allowlist["enabled_global"]
    problems: list[str] = []
    enabled_presets: list[dict] = []
    base_versions: set[str] = set()

    for entry in _flatten(dump, platform, problems):
        owner = entry.get("__dshPluginOwner")
        if isinstance(owner, dict) and owner.get("packageName") == BASE_PACKAGE:
            base_versions.add(str(owner.get("version")))
        if is_disabled(entry, platform):
            continue
        entry_id = str(entry["id"])
        if _name(entry) == PRESET_PACKAGE:
            enabled_presets.append(entry)
            continue
        if _name(entry) == MCP_PACKAGE:
            problems.append(f"全局层有启用的 MCP 条目 {entry_id!r}(只允许 research Preset 内的 research-mcp)")
            continue
        if entry_id not in allowed:
            problems.append(f"未列入白名单的启用条目:{entry_id!r}({_name(entry)})")
        elif allowed[entry_id] != _name(entry):
            problems.append(f"白名单条目 {entry_id!r} 的包名变了:{_name(entry)!r} ≠ 记录的 {allowed[entry_id]!r}")
        nested = _nested_mcp(entry.get("config"), platform, entry_id)
        if nested:
            problems.append(f"条目 {entry_id!r} 的配置里嵌着启用的 MCP:{', '.join(nested)}")

    ids = [str((p.get("config") or {}).get("id")) for p in enabled_presets]
    if ids != ["research"]:
        problems.append(f"启用的 Agent Preset 必须恰为 ['research'],实际 {ids}")
    for preset in enabled_presets:
        if (preset.get("config") or {}).get("id") != "research":
            continue
        if _strip_meta(preset) != _strip_meta(template):
            problems.append("research Preset 与仓库模板 dsh-research-preset.patch.yml 不一致(须逐字段相同)")

    expected = str(allowlist.get("dsh_version"))
    if base_versions != {expected}:
        problems.append(f"{BASE_PACKAGE} 版本 {sorted(base_versions) or '缺失'} ≠ 白名单审阅时的 {expected}:须重新审阅")
    return problems


def load_allowlist(path: Path = ALLOWLIST_FILE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("enabled_global")
    if not isinstance(entries, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items()):
        raise ValueError("白名单文件缺 enabled_global(id → 包名)映射")
    if not isinstance(data.get("dsh_version"), str):
        raise ValueError("白名单文件缺 dsh_version")
    return data


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
        allowlist = load_allowlist()
        template = load_template_preset()
    except Exception as exc:                                  # noqa: BLE001 - 判不了 = 不放行
        print(f"[profile_check] 输入不可用:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    problems = check(dump, allowlist, template)
    for line in problems:
        print("[profile_check] " + line, file=sys.stderr)
    if problems:
        print(f"[profile_check] 未通过:{len(problems)} 项", file=sys.stderr)
        return 1
    print("[profile_check] 通过", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
