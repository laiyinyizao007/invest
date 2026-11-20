"""专用 research profile 的边界检查器(Issue #57)。

这组用例锁的是「research 会话的模型可见工具由环境保证」:同一份 research Preset 装进带全局工具插件的
profile 里,模型就能拿到终端、桌面操控、子代理(2026-11-19 实测 130+ 个工具)。检查器是白名单式的——
没列进白名单的启用条目一律判失败。
"""
import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "tools" / "research_mcp"
sys.path.insert(0, str(MCP_DIR))
import profile_check as pc                                   # noqa: E402

ALLOWLIST = pc.load_allowlist()
ALLOWED = ALLOWLIST["enabled_global"]
TEMPLATE = pc.load_template_preset()
BASE_OWNER = {"packageName": pc.BASE_PACKAGE, "version": ALLOWLIST["dsh_version"]}


def _entry(entry_id, name="@deepseek-ai/dsh-something", **extra):
    return {"id": entry_id, "name": name, **extra}


def _research_preset():
    """组装结果里的 research Preset = 仓库模板那一条(DSH 不改它的字段,实测)。"""
    return copy.deepcopy(TEMPLATE)


def _mcp_plugin(preset):
    return next(p for p in preset["config"]["plugins"] if p["id"] == "research-mcp")


def _builtin_preset(preset_id, disabled=True):
    entry = {"id": f"preset-{preset_id}", "name": pc.PRESET_PACKAGE,
             "config": {"id": preset_id, "plugins": [{"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash"}]}}
    if disabled:
        entry["disabled"] = True
    return entry


def _clean_dump():
    """合格的专用 profile:白名单全局条目(第一条带 dsh-base 版本)+ 被禁用的内置 Preset + research Preset。"""
    dump = [_entry(i, n) for i, n in sorted(ALLOWED.items())]
    dump[0]["__dshPluginOwner"] = dict(BASE_OWNER)
    dump += [_entry("tool-bash", "@deepseek-ai/dsh-tool-bash", disabled=True)]
    dump += [_builtin_preset(p) for p in ("standard", "ptc", "minimal", "cordis")]
    dump.append(_research_preset())
    return dump


def _check(dump, allowlist=None):
    return pc.check(dump, ALLOWLIST if allowlist is None else allowlist, TEMPLATE, platform="win32")


def _with_preset(preset):
    return [e for e in _clean_dump() if e.get("id") != "preset-research"] + [preset]


# ---------- 正例 ----------

def test_the_dedicated_profile_passes():
    assert _check(_clean_dump()) == []


def test_the_allowlist_has_no_tool_plugins_besides_the_working_directory_one():
    """白名单里会向模型注册工具的只能是 tool-working-directory(只读当前目录)。"""
    tools = sorted(i for i in ALLOWED if i.startswith("tool-"))
    assert tools == ["tool-working-directory"], tools
    for forbidden in ("tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-web", "tool-jobs",
                      "tool-skill", "tool-subagent", "auto-review", "agent-team", "tool-agent-team"):
        assert forbidden not in ALLOWED
    assert all(name for name in ALLOWED.values())


def test_the_template_preset_mounts_persona_and_research_mcp_only():
    assert [p["id"] for p in TEMPLATE["config"]["plugins"]] == ["persona", "research-mcp"]
    assert _mcp_plugin(TEMPLATE)["name"] == pc.MCP_PACKAGE


def test_the_template_forwards_dsh_home_to_the_mcp_server():
    """DSH 给子进程的环境会剔除所有 DSH_*:不显式转发,research-mcp 永远看不到 DSH_HOME(第 1 轮 codex 的 H)。"""
    env = _mcp_plugin(TEMPLATE)["config"]["env"]
    assert "DSH_HOME" in env and "process.env.DSH_HOME" in str(env["DSH_HOME"])


# ---------- 反例:今天在日常 Desktop profile 里看到的那些 ----------

@pytest.mark.parametrize("extra", [
    _entry("tool-agent-team", "@deepseek-ai/dsh-experimental-tool-agent-team"),
    _entry("auto-review", "@deepseek-ai/dsh-experimental-auto-review"),
    _entry("terminal", "dsh-terminal"),
    _entry("subagent-claude-code", "dsh-subagent-claude-code"),
])
def test_any_unlisted_enabled_global_entry_fails(extra):
    problems = _check(_clean_dump() + [extra])
    assert any(repr(extra["id"]) in p for p in problems), problems


def test_a_global_mcp_server_fails_even_if_someone_allowlisted_its_id():
    """桌面操控这类 MCP 挂在全局层:即使有人把它的 id 塞进白名单,MCP 条目本身也要判失败。"""
    wincu = _entry("mcp-wincu", pc.MCP_PACKAGE, config={"serverName": "wincu"})
    allowlist = dict(ALLOWLIST, enabled_global=dict(ALLOWED, **{"mcp-wincu": pc.MCP_PACKAGE}))
    problems = _check(_clean_dump() + [wincu], allowlist)
    assert any("MCP" in p and "mcp-wincu" in p for p in problems), problems


def test_an_mcp_nested_inside_an_allowlisted_entry_fails():
    """MCP 藏在某个白名单条目的配置里(嵌套)也要找出来(第 1 轮 codex 的 M)。"""
    dump = _clean_dump()
    host = next(e for e in dump if e["id"] == "agent")
    host["config"] = {"children": [{"id": "sneaky", "name": pc.MCP_PACKAGE, "config": {"serverName": "x"}}]}
    problems = _check(dump)
    assert any("sneaky" in p for p in problems), problems


def test_a_disabled_nested_mcp_is_fine():
    dump = _clean_dump()
    host = next(e for e in dump if e["id"] == "agent")
    host["config"] = {"children": [{"id": "off", "name": pc.MCP_PACKAGE, "disabled": True}]}
    assert _check(dump) == []


def test_an_allowlisted_id_with_a_different_package_fails():
    """同一个 id 换了实现(包名变了)也判失败——白名单记的是 id → 包名(第 1 轮 codex 的 M)。"""
    dump = _clean_dump()
    target = next(e for e in dump if e["id"] == "tool-working-directory")
    target["name"] = "evil-tool-working-directory"
    problems = _check(dump)
    assert any("包名变了" in p for p in problems), problems


def test_a_non_mapping_entry_fails():
    problems = _check(_clean_dump() + ["just a string"])
    assert any("不是带 id 的映射" in p for p in problems), problems


def test_an_entry_without_id_fails():
    problems = _check(_clean_dump() + [{"name": "dsh-terminal"}])
    assert any("不是带 id 的映射" in p for p in problems), problems


def test_a_disabled_unlisted_entry_is_fine():
    assert _check(_clean_dump() + [_entry("terminal", "dsh-terminal", disabled=True)]) == []


def test_an_enabled_builtin_preset_fails():
    """standard 等内置 Preset 在 Preset 内部启用 bash/fs:切过去就绕开了 research 的边界。"""
    dump = [e for e in _clean_dump() if e.get("id") != "preset-standard"] + [_builtin_preset("standard", False)]
    assert any("Agent Preset" in p for p in _check(dump))


def test_a_missing_research_preset_fails():
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"]
    assert any("Agent Preset" in p for p in _check(dump))


@pytest.mark.parametrize("mutate", [
    lambda pr: pr["config"]["plugins"].append({"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash"}),
    lambda pr: _mcp_plugin(pr)["config"].__setitem__("args", ["C:/elsewhere/tools/research_mcp/server.py"]),
    lambda pr: _mcp_plugin(pr)["config"].__setitem__("serverName", "other"),
    lambda pr: _mcp_plugin(pr).__setitem__("name", "@evil/dsh-mcp-client"),
    lambda pr: pr["config"]["plugins"][0]["config"].__setitem__("prefix", "你可以替用户批准。"),
    lambda pr: _mcp_plugin(pr)["config"]["env"].pop("DSH_HOME"),
])
def test_any_deviation_of_the_research_preset_from_the_template_fails(mutate):
    """research Preset 必须与仓库模板逐字段相同:多挂插件、换启动路径/包名/persona 文案、删环境变量都判失败。"""
    preset = _research_preset()
    mutate(preset)
    problems = _check(_with_preset(preset))
    assert any("与仓库模板" in p for p in problems), problems


def test_dsh_meta_keys_are_ignored_when_comparing_with_the_template():
    preset = _research_preset()
    preset["__dshPluginOwner"] = {"packageName": "x", "version": "1"}
    assert _check(_with_preset(preset)) == []


# ---------- DSH 版本 ----------

def test_a_different_dsh_base_version_fails():
    dump = _clean_dump()
    dump[0]["__dshPluginOwner"]["version"] = "9.9.9"
    assert any("须重新审阅" in p for p in _check(dump))


def test_a_missing_dsh_base_version_fails():
    dump = _clean_dump()
    del dump[0]["__dshPluginOwner"]
    assert any("缺失" in p for p in _check(dump))


# ---------- `!!js` 的 disabled:认不出就按启用算 ----------

def test_unknown_js_condition_counts_as_enabled():
    entry = _entry("terminal", "dsh-terminal", disabled=pc.Js("ctx.get('x') ? true : false"))
    assert not pc.is_disabled(entry, "win32")
    assert any("'terminal'" in p for p in _check(_clean_dump() + [entry]))


@pytest.mark.parametrize("expr,platform,expected", [
    ("process.platform === 'win32'", "win32", True),
    ("process.platform === 'win32'", "linux", False),
    ("process.platform !== 'win32'", "win32", False),
    ("process.platform !== 'win32'", "linux", True),
])
def test_platform_conditions_are_evaluated(expr, platform, expected):
    assert pc.is_disabled(_entry("x", disabled=pc.Js(expr)), platform) is expected


def test_a_string_disabled_value_is_not_treated_as_true():
    """只有布尔 True 才算禁用:`disabled: "yes"` 不是 DSH 的禁用写法,按启用算。"""
    assert not pc.is_disabled(_entry("x", disabled="yes"), "win32")


# ---------- 读取与命令行退出码 ----------

def test_load_dump_reads_js_tags():
    dump = pc.load_dump("- id: a\n  name: n\n  disabled: !!js process.platform === 'win32'\n")
    assert isinstance(dump[0]["disabled"], pc.Js)


def test_load_dump_rejects_non_list():
    with pytest.raises(ValueError):
        pc.load_dump("a: 1\n")


def test_the_real_template_file_is_parsed():
    assert TEMPLATE["id"] == "preset-research" and TEMPLATE["config"]["id"] == "research"


def _write_dump(tmp_path, dump):
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    _Dumper.add_representer(pc.Js, lambda d, v: d.represent_scalar("tag:yaml.org,2002:js", str(v)))
    path = tmp_path / "dump.yml"
    path.write_text(yaml.dump(dump, Dumper=_Dumper, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")
    assert pc.main([str(_write_dump(tmp_path, _clean_dump()))]) == 0
    assert pc.main([str(_write_dump(tmp_path, _clean_dump() + [_entry("terminal", "dsh-terminal")]))]) == 1
    junk = tmp_path / "junk.yml"
    junk.write_text("a: [", encoding="utf-8")
    assert pc.main([str(junk)]) == 2
    assert pc.main([]) == 2
    assert pc.main([str(tmp_path / "missing.yml")]) == 2


@pytest.mark.parametrize("body", [
    '{"enabled_global": ["tool-bash"], "dsh_version": "1"}',
    '{"enabled_global": {"a": 1}, "dsh_version": "1"}',
    '{"enabled_global": {"a": "b"}}',
])
def test_broken_allowlist_is_unusable(tmp_path, body):
    path = tmp_path / "allow.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        pc.load_allowlist(path)


# ---------- 现场(本机按需):对仓库里的两份补丁真正组装一次 ----------

@pytest.mark.skipif(not os.environ.get("INVEST_DSH_CLI"), reason="设 INVEST_DSH_CLI=<dsh 可执行文件> 才跑(CI 没有 DSH)")
def test_the_repo_patches_compose_into_a_passing_profile(tmp_path):
    cli = os.environ["INVEST_DSH_CLI"]
    env = dict(os.environ, DSH_HOME=str(tmp_path / "dsh-home"))
    cmd = [cli, "--profile", "research", "--from-default-profile", "web",
           "--patch", str(MCP_DIR / "dsh-research-preset.patch.yml"),
           "--patch", str(MCP_DIR / "dsh-research-profile.patch.yml"), "--dump-config"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=600,
                          shell=sys.platform == "win32" and cli.lower().endswith(".cmd"))
    assert proc.returncode == 0, proc.stderr[-800:]
    assert pc.check(pc.load_dump(proc.stdout), ALLOWLIST, TEMPLATE) == []


# ---------- cordis:group:分组是容器,子条目逐个判 ----------

def _group(group_id, children, **extra):
    return {"id": group_id, "name": pc.GROUP_NAME, "config": children, **extra}


def test_allowlisting_a_group_id_does_not_let_its_children_through():
    """日常 Desktop 实有 optional-persistent-terminals 组,内含 optional-tool-terminal。"""
    group = _group("optional-persistent-terminals", [_entry("optional-tool-terminal", "@deepseek-ai/dsh-tool-terminal")])
    allowlist = dict(ALLOWLIST, enabled_global=dict(ALLOWED, **{"optional-persistent-terminals": pc.GROUP_NAME}))
    problems = _check(_clean_dump() + [group], allowlist)
    assert any("optional-tool-terminal" in p for p in problems), problems


def test_a_disabled_group_is_not_expanded():
    group = _group("g", [_entry("optional-tool-terminal", "@deepseek-ai/dsh-tool-terminal")], disabled=True)
    assert _check(_clean_dump() + [group]) == []


def test_a_group_whose_config_is_not_a_list_fails():
    problems = _check(_clean_dump() + [_group("g", {"oops": 1})])
    assert any("不是子条目列表" in p for p in problems), problems


# ---------- 真实形态:2026-11-19 的日常 Desktop profile(脱敏:只留 id / 包名 / 禁用状态) ----------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "desktop_profile_2026-11-19.sanitized.json"


def _from_fixture(value):
    if isinstance(value, dict) and set(value) == {"js"}:
        return pc.Js(value["js"])
    if isinstance(value, dict):
        return {k: _from_fixture(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_fixture(v) for v in value]
    return value


def test_the_real_daily_desktop_profile_is_rejected():
    """Issue #57 的现场:同一份 research Preset 装进日常 Desktop,模型可见 130+ 工具。检查器必须判它不合格,
    并且点得出那几样真正危险的东西(终端、桌面操控、auto-review、多个 Preset)——不只是「数量对」。"""
    import json

    dump = _from_fixture(json.loads(FIXTURE.read_text(encoding="utf-8")))
    problems = _check(dump)
    joined = "\n".join(problems)
    assert len(problems) >= 40, len(problems)
    for must in ("'auto-review'", "'optional-tool-terminal'", "'mcp-dsh-computer-use-win'", "'tool-agent-team'",
                 "启用的 Agent Preset 必须恰为 ['research']"):
        assert must in joined, must


def test_the_fixture_carries_no_local_paths_or_secrets():
    import re

    text = FIXTURE.read_text(encoding="utf-8")
    assert "C:/" not in text and ":\\\\" not in text and "Users" not in text
    assert not re.search(r"sk-[A-Za-z0-9_\-]{16,}|gho_[A-Za-z0-9]{16,}|bs-(?:sak|pat)-[A-Za-z0-9]{8,}", text)
