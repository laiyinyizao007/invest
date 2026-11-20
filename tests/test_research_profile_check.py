"""专用 research profile 的边界检查器(Issue #57)。

这组用例锁的是「research 会话的模型可见工具由环境保证」:同一份 research Preset 装进带全局工具插件的
profile 里,模型就能拿到终端、桌面操控、子代理(2026-11-19 实测 130+ 个工具)。检查器是白名单式的——
没列进白名单的启用条目一律判失败。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "tools" / "research_mcp"
sys.path.insert(0, str(MCP_DIR))
import profile_check as pc                                   # noqa: E402

ALLOWED = pc.load_allowlist()


def _entry(entry_id, name="@deepseek-ai/dsh-something", **extra):
    return {"id": entry_id, "name": name, **extra}


def _research_preset(plugins=None):
    plugins = plugins if plugins is not None else [
        {"id": "persona", "name": "@deepseek-ai/dsh-persona", "config": {"prefix": "x"}},
        {"id": "research-mcp", "name": pc.MCP_PACKAGE,
         "config": {"serverName": "research", "transport": "stdio",
                    "args": [pc.Js("(process.env.INVEST_REPO || process.cwd()) + '/tools/research_mcp/server.py'")]}},
    ]
    return {"id": "preset-research", "name": pc.PRESET_PACKAGE,
            "config": {"id": "research", "plugins": plugins}}


def _builtin_preset(preset_id, disabled=True):
    entry = {"id": f"preset-{preset_id}", "name": pc.PRESET_PACKAGE,
             "config": {"id": preset_id, "plugins": [{"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash"}]}}
    if disabled:
        entry["disabled"] = True
    return entry


def _clean_dump():
    """一份「合格」的专用 profile:白名单里的全局条目 + 被禁用的内置 Preset + research Preset。"""
    dump = [_entry(i) for i in sorted(ALLOWED)]
    dump += [_entry("tool-bash", "@deepseek-ai/dsh-tool-bash", disabled=True)]
    dump += [_builtin_preset(p) for p in ("standard", "ptc", "minimal", "cordis")]
    dump.append(_research_preset())
    return dump


# ---------- 正例 ----------

def test_the_dedicated_profile_passes():
    assert pc.check(_clean_dump(), ALLOWED, platform="win32") == []


def test_the_allowlist_has_no_tool_plugins_besides_the_working_directory_one():
    """白名单里会向模型注册工具的只能是 tool-working-directory(只读当前目录)。"""
    tools = sorted(i for i in ALLOWED if i.startswith("tool-"))
    assert tools == ["tool-working-directory"], tools
    for forbidden in ("tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-web", "tool-jobs",
                      "tool-skill", "tool-subagent", "auto-review", "agent-team", "tool-agent-team"):
        assert forbidden not in ALLOWED


# ---------- 反例:今天在日常 Desktop profile 里看到的那些 ----------

@pytest.mark.parametrize("extra", [
    _entry("tool-agent-team", "@deepseek-ai/dsh-experimental-tool-agent-team"),
    _entry("auto-review", "@deepseek-ai/dsh-experimental-auto-review"),
    _entry("terminal", "dsh-terminal"),
    _entry("subagent-claude-code", "dsh-subagent-claude-code"),
])
def test_any_unlisted_enabled_global_entry_fails(extra):
    problems = pc.check(_clean_dump() + [extra], ALLOWED, platform="win32")
    assert any(repr(extra["id"]) in p for p in problems), problems


def test_a_global_mcp_server_fails_even_if_someone_allowlisted_its_id():
    """桌面操控这类 MCP 挂在全局层:即使有人把它的 id 塞进白名单,MCP 条目本身也要判失败。"""
    wincu = _entry("mcp-wincu", pc.MCP_PACKAGE, config={"serverName": "wincu"})
    problems = pc.check(_clean_dump() + [wincu], ALLOWED | {"mcp-wincu"}, platform="win32")
    assert any("MCP" in p and "mcp-wincu" in p for p in problems), problems


def test_a_disabled_unlisted_entry_is_fine():
    assert pc.check(_clean_dump() + [_entry("terminal", "dsh-terminal", disabled=True)],
                    ALLOWED, platform="win32") == []


def test_an_enabled_builtin_preset_fails():
    """standard 等内置 Preset 在 Preset 内部启用 bash/fs:切过去就绕开了 research 的边界。"""
    dump = [e for e in _clean_dump() if e.get("id") != "preset-standard"] + [_builtin_preset("standard", False)]
    problems = pc.check(dump, ALLOWED, platform="win32")
    assert any("Agent Preset" in p for p in problems), problems


def test_a_missing_research_preset_fails():
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"]
    assert any("Agent Preset" in p for p in pc.check(dump, ALLOWED, platform="win32"))


def test_an_extra_plugin_in_the_research_preset_fails():
    plugins = _research_preset()["config"]["plugins"] + [{"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash"}]
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"] + [_research_preset(plugins)]
    problems = pc.check(dump, ALLOWED, platform="win32")
    assert any("research Preset 的插件" in p for p in problems), problems


def test_a_disabled_extra_plugin_in_the_research_preset_is_fine():
    plugins = _research_preset()["config"]["plugins"] + [
        {"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash", "disabled": True}]
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"] + [_research_preset(plugins)]
    assert pc.check(dump, ALLOWED, platform="win32") == []


def test_research_mcp_must_launch_the_repo_server():
    plugins = _research_preset()["config"]["plugins"]
    plugins[1]["config"]["args"] = ["C:/elsewhere/server.py"]
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"] + [_research_preset(plugins)]
    problems = pc.check(dump, ALLOWED, platform="win32")
    assert any("server.py" in p for p in problems), problems


def test_research_mcp_must_be_the_research_server_name():
    plugins = _research_preset()["config"]["plugins"]
    plugins[1]["config"]["serverName"] = "other"
    dump = [e for e in _clean_dump() if e.get("id") != "preset-research"] + [_research_preset(plugins)]
    assert any("serverName" in p for p in pc.check(dump, ALLOWED, platform="win32"))


# ---------- `!!js` 的 disabled:认不出就按启用算 ----------

def test_unknown_js_condition_counts_as_enabled():
    entry = _entry("terminal", "dsh-terminal", disabled=pc.Js("ctx.get('x') ? true : false"))
    assert not pc.is_disabled(entry, "win32")
    problems = pc.check(_clean_dump() + [entry], ALLOWED, platform="win32")
    assert any("'terminal'" in p for p in problems), problems


@pytest.mark.parametrize("expr,platform,expected", [
    ("process.platform === 'win32'", "win32", True),
    ("process.platform === 'win32'", "linux", False),
    ("process.platform !== 'win32'", "win32", False),
    ("process.platform !== 'win32'", "linux", True),
])
def test_platform_conditions_are_evaluated(expr, platform, expected):
    assert pc.is_disabled(_entry("x", disabled=pc.Js(expr)), platform) is expected


def test_a_string_disabled_value_is_not_treated_as_true():
    """只有布尔 True 才算禁用:`disabled: "yes"` 这种不是 DSH 的禁用写法,按启用算。"""
    assert not pc.is_disabled(_entry("x", disabled="yes"), "win32")


# ---------- YAML 读取与命令行退出码 ----------

def test_load_dump_reads_js_tags():
    dump = pc.load_dump("- id: a\n  name: n\n  disabled: !!js process.platform === 'win32'\n")
    assert isinstance(dump[0]["disabled"], pc.Js)


def test_load_dump_rejects_non_list():
    with pytest.raises(ValueError):
        pc.load_dump("a: 1\n")


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
    good = _write_dump(tmp_path, _clean_dump())
    assert pc.main([str(good)]) == 0
    bad = _write_dump(tmp_path, _clean_dump() + [_entry("terminal", "dsh-terminal")])
    assert pc.main([str(bad)]) == 1
    junk = tmp_path / "junk.yml"
    junk.write_text("a: [", encoding="utf-8")
    assert pc.main([str(junk)]) == 2
    assert pc.main([]) == 2
    assert pc.main([str(tmp_path / "missing.yml")]) == 2


def test_broken_allowlist_is_unusable(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text('{"enabled_global_ids": "tool-bash"}', encoding="utf-8")
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
    assert pc.check(pc.load_dump(proc.stdout), ALLOWED) == []
