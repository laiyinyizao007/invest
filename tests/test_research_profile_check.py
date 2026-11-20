"""专用 research profile 的边界检查器(Issue #57)。

这组用例锁的是「research 会话的模型可见工具由环境保证」:同一份 research Preset 装进带全局工具插件的
profile 里,模型就能拿到终端、桌面操控、子代理(2026-11-19 实测 130+ 个工具)。检查器是白名单式的——
没列进白名单的启用条目一律判失败。
"""
import copy
import os
import re
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
REGISTRANTS = ALLOWLIST["model_tool_registrants"]
MUST_DISABLE_IDS = {k for k, v in REGISTRANTS.items() if v["decision"] == pc.MUST_DISABLE}
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


def test_the_global_layer_registers_no_model_tool():
    """全局层**没有任何条目会向模型注册工具**——这才是「模型看不见终端/桌面操控」的根据。

    此前只靠「id 叫不叫 tool-」判断(G8 第 3 轮 codex 的 MEDIUM):
    实测 `plugin-manager` 不叫 tool-,却在 apply() 里**无条件**注册了 plugin_manager
    ⇒「全局层只有 tool-working-directory 注册模型工具」那句断言是**假的**。
    ⇒ 现在改成:会注册工具的条目逐条登记在 `model_tool_registrants` 里(带源码位置),
    decision=must-disable 的**一律不得**出现在白名单里,也不得在组装结果里是启用状态。

    集合用 `==` 钉死而不是 `>=`:少登记一条 ⇒ 那个洞重新打开而这里照样绿。
    """
    assert MUST_DISABLE_IDS == {"plugin-manager", "tool-working-directory", "mcp-resources"}
    assert not (MUST_DISABLE_IDS & set(ALLOWED))
    for forbidden in ("tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-web", "tool-jobs",
                      "tool-skill", "tool-subagent", "auto-review", "agent-team", "tool-agent-team"):
        assert forbidden not in ALLOWED
    assert all(name for name in ALLOWED.values())


def test_every_model_tool_registrant_carries_its_evidence():
    """每条注册方都要记下**源码位置**、它注册了什么、判定是什么——否则「它到底注册了什么」只是印象。"""
    for entry_id, record in REGISTRANTS.items():
        assert record["package"], entry_id
        assert record["evidence"] and ":" in record["evidence"], (entry_id, record["evidence"])
        assert record["decision"] in pc.REGISTRANT_DECISIONS, entry_id
        assert record["note"], entry_id
        if record["decision"] == pc.MUST_DISABLE:
            assert record["tools"], f"{entry_id} 判成 must-disable,却没登记它注册了哪些工具"
        else:
            assert not record["tools"], f"{entry_id} 不判成 must-disable,就不该有模型可见的工具"


def test_the_profile_patch_disables_every_tool_registrant():
    """白名单说「这几个必须禁用」⇒ profile 补丁里就得**真的**禁用它们。

    这是两份清单之间的接缝:白名单(哪些条目可以启用)与补丁(哪些条目被禁用)各写一半,
    少写任何一边 ⇒ 组装结果里那个条目就是启用的 ⇒ 模型看得见它注册的工具。
    真实组装要 DSH(CI 上跑不了),所以这里**读补丁文件本身**把这条接缝钉住。
    """
    patch = pc.load_dump((MCP_DIR / "dsh-research-profile.patch.yml").read_text(encoding="utf-8"))
    disabled_ids = {e["id"] for e in patch
                    if isinstance(e, dict) and e.get("id") and e.get("disabled") is True}
    missing = sorted(MUST_DISABLE_IDS - disabled_ids)
    assert not missing, (
        f"补丁里没有禁用这些会注册模型工具的条目:{missing}"
        f"(补丁里已禁用的:{sorted(disabled_ids)})")


def test_an_enabled_tool_registrant_fails():
    """组装结果里 plugin-manager 是**启用**状态 ⇒ 判失败。

    场景:有人改了白名单、却**忘了**在 profile 补丁里禁用它(或者补丁被手工改坏)
    ⇒ 组装结果里那个条目是启用的 ⇒ 模型看得见它注册的工具。

    这里白名单里**没有**它 ⇒ 命中的必须是「会向模型注册工具」这条规则本身;
    「未列入白名单」那条消息不含这几个字,顶替不了它
    (否则这条用例就钉不住那条规则——见证红了却可能是别的原因)。
    """
    entry = _entry("plugin-manager", "@deepseek-ai/dsh-plugin-manager")
    problems = _check(_clean_dump() + [entry], ALLOWLIST)
    assert any("会向模型注册工具" in p for p in problems), problems


def test_an_allowlisted_tool_registrant_still_fails_when_enabled():
    """就算有人把它的 id 塞进了白名单,组装结果里它**是启用**状态照样判失败。

    两份清单各写一半:白名单说「谁可以启用」、补丁说「谁被禁用」。
    两路都要在(ADR 20261113 的 C 节):只改其中一份 ⇒ 另一条规则必须独立地把洞堵上。
    """
    entry = _entry("plugin-manager", "@deepseek-ai/dsh-plugin-manager")
    allowlist = dict(ALLOWLIST, enabled_global=dict(ALLOWED, **{"plugin-manager": "@deepseek-ai/dsh-plugin-manager"}))
    problems = _check(_clean_dump() + [entry], allowlist)
    assert any("白名单里混进" in p for p in problems), problems
    # 「会向模型注册工具」这条(组装结果那一路)必须**单独**也在
    # ——「白名单里混进」那条消息里也有这几个字,不能顶替它。
    assert any("会向模型注册工具" in p and "混进" not in p for p in problems), problems


def test_allowlisting_a_tool_registrant_fails():
    """白名单自己混进注册方 ⇒ 判失败(不必等组装结果出来)。"""
    allowlist = dict(ALLOWLIST, enabled_global=dict(ALLOWED, **{"plugin-manager": "@deepseek-ai/dsh-plugin-manager"}))
    problems = _check(_clean_dump(), allowlist)
    assert any("白名单里混进" in p for p in problems), problems


def test_a_registrant_whose_package_changed_fails():
    """注册方的 id 不变、包名换了 ⇒ 判失败。

    清单里 `package` 这一栏此前**只存不用** ⇒ 「同一个 id 换了实现」这件事没人管,
    而登记的源码证据已经不适用于这个包了(G8 第 4 轮 codex 的必须改 #1)。
    """
    recorded = REGISTRANTS["plugin-manager"]["package"]
    entry = _entry("plugin-manager", recorded + "-fork")
    problems = _check(_clean_dump() + [entry], ALLOWLIST)
    assert any("包名与清单记录不一致" in p for p in problems), problems


def test_a_registrant_with_the_recorded_package_is_not_flagged_for_its_package():
    """反向对照:包名与清单登记一致时,不得报「包名不一致」(否则这条规则就成了永远在响的铃)。"""
    recorded = REGISTRANTS["plugin-manager"]["package"]
    entry = _entry("plugin-manager", recorded)
    problems = _check(_clean_dump() + [entry], ALLOWLIST)
    assert not any("包名与清单记录不一致" in p for p in problems), problems


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
    target = next(e for e in dump if e["id"] == "subagent")
    target["name"] = "evil-subagent"
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


def test_cli_reports_cannot_judge_as_exit_2(tmp_path, monkeypatch):
    """`check()` 自己崩了 ⇒ 「**判不了**」⇒ 退出码必须是 2,不能和「判出问题了」的 1 撞码。

    未捕获的 traceback 走 Python 默认退出码 1 ⇒ 调用方分不清「拒绝」与「崩了」,
    排查信息也会指向错的方向。仓库里「判不了与失败混为一谈」本来就是一条要守的规则。
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pc, "check", boom)
    assert pc.main([str(_write_dump(tmp_path, _clean_dump()))]) == 2


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

def test_the_repo_patches_compose_into_a_passing_profile(tmp_path):
    """现场(本机按需):真把两份补丁组装一次,再拿检查器核对。

    CI 上没有 DSH ⇒ 这里用**运行期**跳过(`if` 条件里的 `pytest.skip`),
    不是 `@pytest.mark.skipif` 装饰器:纪律守卫要求「允许跳过」必须是
    **条件里**的运行期退出——标记类跳过一律不许(那不是数据依赖,是整条永远不跑)。
    """
    cli = os.environ.get("INVEST_DSH_CLI")
    if not cli:
        pytest.skip("设 INVEST_DSH_CLI=<dsh 可执行文件> 才跑(CI 没有 DSH)")
    env = dict(os.environ, DSH_HOME=str(tmp_path / "dsh-home"))
    cmd = [cli, "--profile", "research", "--from-default-profile", "web",
           "--patch", str(MCP_DIR / "dsh-research-preset.patch.yml"),
           "--patch", str(MCP_DIR / "dsh-research-profile.patch.yml"), "--dump-config"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=600,
                          shell=sys.platform == "win32" and cli.lower().endswith(".cmd"))
    assert proc.returncode == 0, proc.stderr[-800:]
    assert pc.check(pc.load_dump(proc.stdout), ALLOWLIST, TEMPLATE) == []


# ---------- 源码级核对:白名单里那些「会注册模型工具」的包,到底还是不是 ----------

# 工具注册的调用形态:`ctx.tools.register(...)`、沙盒门面 `harness.registerTool(...)`、
# 以及历史上的 `addTool(...)`。少一种形态就少一批包 ⇒ 这里三个都搜。
REGISTRATION_CALL = re.compile(r"\.tools\.register\s*\(|registerTool\s*\(|addTool\s*\(")


def _dsh_package_root() -> Path | None:
    """本机 DSH 安装的 `@deepseek-ai` 包目录;拿不到就返回 None(⇒ 运行期跳过)。

    两条来源:显式的 `DSH_TOOL_SCAN_ROOT`;或从 `INVEST_DSH_CLI`
    (<…>/node_modules/.bin/dsh.cmd)推出来。<…>/node_modules/@deepseek-ai。
    """
    explicit = os.environ.get("DSH_TOOL_SCAN_ROOT")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_dir() else None
    cli = os.environ.get("INVEST_DSH_CLI")
    if cli:
        candidate = Path(cli).resolve().parent.parent / "@deepseek-ai"
        return candidate if candidate.is_dir() else None
    return None


def _package_source_files(root: Path, package_name: str) -> list[Path]:
    """一个包名(或包名 + 子路径导出)对应的源码文件。

    子路径导出(`@deepseek-ai/dsh-tool-cordis/host`)只加载**一个**文件——
    扫整个包会把同包里没被启用的那半边也算进来 ⇒ 判据会虚高。
    """
    assert package_name.startswith("@deepseek-ai/"), package_name
    pkg, _, sub = package_name[len("@deepseek-ai/"):].partition("/")
    pkg_dir = root / pkg
    assert pkg_dir.is_dir(), f"本机 DSH 里没有这个包:{package_name}({pkg_dir})"
    if sub:
        import json as _json

        manifest = _json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
        target = (manifest.get("exports") or {}).get("./" + sub)
        if isinstance(target, dict):
            target = target.get("default")
        if isinstance(target, str):
            rel = target[2:] if target.startswith("./") else target
            return [pkg_dir / rel]
    return sorted(pkg_dir.glob("lib/**/*.js"))


def _packages_with_registration(root: Path, package_names: set[str]) -> set[str]:
    found = set()
    for name in sorted(package_names):
        for path in _package_source_files(root, name):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if REGISTRATION_CALL.search(text):
                found.add(name)
                break
    return found


def test_the_source_scan_matches_the_recorded_registrants():
    """把「全局层不向模型注册工具」这句断言**落到源码上**。

    G8 第 3 轮 codex 的 MEDIUM:此前那句「只有 tool-working-directory 注册模型工具」
    只靠 **id 命名 + 现场观测**,没有源码级证据——实测 `plugin-manager` 不叫 tool-,
    却真的注册了 `plugin_manager` ⇒ 那句话是假的。
    ⇒ 这里把本机 DSH 的包目录扫一遍,凡是含注册调用的包必须与清单登记的**完全一致**:
       多出来一个(某个包新学会了注册工具)或少掉一个(证据过期了)都判红。
    """
    root = _dsh_package_root()
    if root is None:
        pytest.skip("没找到本机 DSH 的包目录(设 DSH_TOOL_SCAN_ROOT 或 INVEST_DSH_CLI 才跑)")
    universe = set(ALLOWED.values()) | {r["package"] for r in REGISTRANTS.values()}
    scanned = _packages_with_registration(root, universe)
    recorded = {r["package"] for r in REGISTRANTS.values()}
    assert scanned == recorded, (
        f"源码里含工具注册调用的包变了:实测 {sorted(scanned)},清单登记 {sorted(recorded)}"
        " ⇒ DSH 升级或某个包改了 ⇒ 须重新审阅 model_tool_registrants(别照抄旧结论)")


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


# ---------- 形状:插件条目只允许出现在三个地方 ----------

def test_an_entry_with_a_nested_plugin_list_fails():
    """普通条目的 config 里塞了一份「看起来像插件条目」的列表 ⇒ 判失败。

    插件条目只允许出现在:顶层、分组(`cordis:group`)的 config 列表、Preset 的 config.plugins 列表。
    前两个由 `_flatten` 展开、第三个由「与模板逐字段相同」钉住;
    别的形状**读不懂**那是配置还是插件 ⇒ 按本文件的规矩不放行(G8 第 4 轮 codex 的必须改 #2)。
    """
    dump = _clean_dump()
    host = next(e for e in dump if e["id"] == "agent")
    host["config"] = {"children": [_entry("plugin-manager", "@deepseek-ai/dsh-plugin-manager")]}
    problems = _check(dump)
    assert any("嵌着看起来像插件条目的列表" in p for p in problems), problems


def test_an_entry_with_a_plain_list_config_is_fine():
    """反向对照:配置里本来就有列表(不是插件条目)⇒ 不得判失败。"""
    dump = _clean_dump()
    host = next(e for e in dump if e["id"] == "agent")
    host["config"] = {"paths": ["a", "b"], "opts": [{"x": 1}]}
    assert _check(dump) == []


def test_the_research_presets_plugin_list_is_a_legal_nesting():
    """反向对照:Preset 自己的 config.plugins 是**合法**的嵌套位置 ⇒ 不得判失败。

    (research Preset 由「与仓库模板逐字段相同」钉住,那份模板里就只有 persona + research-mcp。)
    """
    problems = _check(_clean_dump())
    assert not any("嵌着看起来像插件条目的列表" in p for p in problems), problems


def test_a_disabled_nested_entry_list_is_fine():
    """反向对照:整棵被禁用的子树不会被加载 ⇒ 不得判失败(与 `_nested_mcp` 同一口径)。"""
    dump = _clean_dump()
    host = next(e for e in dump if e["id"] == "agent")
    host["config"] = {"children": [_entry("off", "@deepseek-ai/dsh-tool-bash", disabled=True)]}
    assert _check(dump) == []


# ---------- 真实形态:2026-11-19 的日常 Desktop profile(脱敏:只留 id / 包名 / 禁用状态) ----------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "desktop_profile_2026-11-19.sanitized.json"

# 「日常 Desktop profile 有多少项问题」是**夹具 × 白名单 × 模板**三者的函数。
# ADR 与 CHANGELOG 里都写着这个数(人读的是文档)⇒ 这里把它钉成**精确值**,
# 并让两份文档跟它对账:
#   - 数变了(白名单/夹具/模板动了)⇒ 这条用例先红,提醒把文档一起改;
#   - 文档里的数漂了 ⇒ 同一条用例红。
# 此前只断言「>= 40」⇒ 文档写 54、实测 20 也照样绿 ⇒ 那句断言等于没有。
DESKTOP_PROFILE_PROBLEMS = 60

# 夹具里允许出现的 npm scope。第三方插件的**发布者**是谁属于「这台机器装了什么」,
# 不是检查器需要的 ⇒ 脱敏成 `@third-party` 占位(包名主干与插件 id 留着——
# 检查器报的是 id)。以后往夹具加条目,scope 必须先在这里登记。
KNOWN_VENDOR_SCOPES = {"deepseek-ai", "third-party"}

_BACKSLASH = chr(92)

# 隐私形态清单:形态名 → (正则, **该抓住**的样本, **不该抓住**的样本)。
#
# 第 2 轮 codex 的 L2:此前只查三种最粗的形态(`C:/`、`:\\`、`Users`),
# 邮箱 / IP / GUID / UNC / 各类 token 都能从缝里溜过去 ⇒ 这里按**形态**列全。
#
# 为什么每条都带两组样本:
#   - 「该抓住的」是**探针**:正则写坏(漏转义、括号没闭合)时会永远匹配不到
#     ⇒ 检查变成空转,而「没有发现泄漏」与「根本没在查」长得一模一样;
#   - 「不该抓住的」是**反向对照**:正则宽了会把正常内容也判成泄漏
#     (例如把 `file:///…` 里的 `e:/` 当成盘符)⇒ 天天假红,最后被人注释掉。
FORBIDDEN_PATTERNS = {
    "邮箱": (
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        ["a@b.com", "x.starlight@gmail.com"],
        ["@deepseek-ai/dsh-x", "file:///a/b.js"],
    ),
    "IPv4": (
        r"(?<![\d.])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])",
        ["192.168.1.10", "10.0.0.1"],
        ["0.2.1-rc.3", "1.2.3"],
    ),
    "GUID": (
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        ["123e4567-e89b-12d3-a456-426614174000"],
        ["0.2.1-rc.3", "preset-research"],
    ),
    "UNC 路径": (
        # **两个分隔符都要写两个反斜杠**:正则里 `\\` 才是「一个反斜杠」;
        # 只写一个会被当成转义方括号(`\[` = 字面量「[」)⇒ 这条检查会**永远匹配不到**
        # ——探针当场逮住了它(见 test_each_privacy_pattern_actually_catches_its_shape)。
        _BACKSLASH * 2 + r"[A-Za-z0-9_.-]+" + _BACKSLASH * 2 + r"[A-Za-z0-9_.-]+",
        [_BACKSLASH * 2 + "server" + _BACKSLASH + "share" + _BACKSLASH + "x.json"],
        ["C:/Users/x", "file:///a/b.js"],
    ),
    "盘符路径": (
        # 字符类里要写**两个**反斜杠(`\\` = 一个字面反斜杠);
        # 只写一个会被当成对下一个字符的转义(`\/` = 斜杠)⇒ 这条只会匹配正斜杠,
        # `D:\…` 那种形态会**漏掉**——探针当场逮住了它。
        #
        # 否定回顾(`(?<![A-Za-z0-9])`)挡的是**协议名里的冒号**:`http:` 的 `p:`、
        # `file:` 的 `e:`——前面紧挨着词字符。它**挡不住** `file:///e:/x.js`
        # (前面是斜杠)⇒ 那一条照样命中。而那**本来就该命中**:Windows 上
        # `file:///C:/Users/<名>/` 就是真路径,是泄漏。
        r"(?<![A-Za-z0-9])[A-Za-z]:[" + _BACKSLASH * 2 + r"/]",
        ["C:/Users/laiyi", "D:" + _BACKSLASH + "data" + _BACKSLASH + "x.json",
         "file:///e:/x.js"],           # ← 带盘符的 file URL 也是真路径,照样该报
        ["file:///a/b.js"],             # ← 没有盘符的普通 file URL:不该报
    ),
    # 用户目录按**两种平台形态**分列:Windows 是 `C:\Users\<名>`,
    # POSIX 是 `/home/<名>`。合写成一条 `...(?:Users|home)[\\/]` 会漏掉 `/home/x`
    # (斜杠不在「词字符」里,前面接不上)⇒ 分列。
    "Windows 用户目录": (
        # 分隔符写 `{1,2}`:夹具是 **JSON 文本**,一个真实路径在原文里写成
        # `C:\\Users\\bob`(反斜杠被 JSON 转义成两个)⇒ 只认单个分隔符,
        # 在**被扫描物**的真实形态上就是漏的。探针只喂原始形态时看不出这个洞
        # (G8 第 2 轮 codex 的 L2)——现在两种形态都进样本。
        r"(?i)[A-Za-z]:[" + _BACKSLASH * 2 + r"/]{1,2}Users[" + _BACKSLASH * 2
        + r"/]{1,2}[^" + _BACKSLASH * 2 + r"\"']+",
        ["C:" + _BACKSLASH + "Users" + _BACKSLASH + "laiyi" + _BACKSLASH + "x.json",
         "C:/Users/laiyi/x.json",
         # JSON 转义后的形态(夹具里真实存在的形态)
         "C:" + _BACKSLASH * 2 + "Users" + _BACKSLASH * 2 + "laiyi"
         + _BACKSLASH * 2 + "x.json"],
        ["file:///a/b.js"],
    ),
    "POSIX 家目录": (
        r"(?<![A-Za-z0-9._-])/(?:home|Users)/[A-Za-z0-9._-]+",
        ["/home/laiyi/x.json", "/Users/laiyi/x.json"],
        ["@deepseek-ai/dsh-home", "file:///a/b.js"],
    ),
    "GitHub 令牌": (
        r"(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})",
        ["ghp_" + "a" * 36, "github_pat_" + "b" * 30],
        ["ghp_short", "gho_"],
    ),
    "OpenAI 类令牌": (
        r"sk-[A-Za-z0-9_-]{16,}",
        ["sk-" + "a" * 24],
        ["sk-short"],
    ),
    "毕昇令牌": (
        r"bs-(?:sak|pat)-[A-Za-z0-9]{8,}",
        ["bs-sak-" + "a" * 12],
        ["bs-x"],
    ),
    "裸密钥赋值": (
        r"(?i)(?:api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{12,}",
        ['api_key = "abcdefghijklmnop"', "BISHENG_OPENAPI_KEY=abcdefghijklmnop"],
        ["tokenizer", "password_policy"],
    ),
}


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
    assert len(problems) == DESKTOP_PROFILE_PROBLEMS, (
        f"问题数变成 {len(problems)} 了(期望 {DESKTOP_PROFILE_PROBLEMS}):"
        "夹具/白名单/模板变了 ⇒ ADR 与 CHANGELOG 里写的同一个数要一起改")
    for must in ("'auto-review'", "'optional-tool-terminal'", "'mcp-dsh-computer-use-win'", "'tool-agent-team'",
                 "启用的 Agent Preset 必须恰为 ['research']"):
        assert must in joined, must


def test_the_documents_quote_the_same_problem_count():
    """ADR 与 CHANGELOG 里写的「N 项」必须等于实测值——文档里的数不许自己漂。

    第 2 轮 codex 的 L1:文档写「54 项」、用例只断言「>= 40」⇒ 两边对不上也绿。
    ⇒ 这里把两份文档里那个数**取出来**跟实测常量比。
    """
    import re

    adr = (ROOT / "docs" / "adr" / "20261113-dsh-research-entry-and-out-of-band-approval.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    quoted = {
        "ADR": re.findall(r"profile_check\.py`? 对该 profile 报 (\d+) 项", adr),
        "CHANGELOG": re.findall(r"判出 (\d+) 项", changelog),
    }
    for where, numbers in quoted.items():
        assert numbers, f"{where} 里找不到「N 项」那个数了(那句话改了?改文档时一并改这里)"
        assert set(numbers) == {str(DESKTOP_PROFILE_PROBLEMS)}, (
            f"{where} 里写的是 {numbers},实测是 {DESKTOP_PROFILE_PROBLEMS} ⇒ 文档与代码对不上")


def _fixture_texts(raw: str) -> list[str]:
    r"""夹具的两种文本形态:原始 JSON 文本 + 反序列化后的每一个字符串。

    为什么两遍都要扫(G8 第 2 轮 codex 的 L2):JSON 会把反斜杠写成两个
    (`C:\\Users\\bob` 在原文里),而**反序列化后**才是真实路径(`C:\Users\bob`)——
    只扫原文,「用户目录」那条规则在被扫描物的真实形态上就是漏的;
    只扫反序列化结果,原文里被转义藏起来的形态又会漏。两遍都扫,两边都堵。
    """
    import json

    texts = [raw]
    stack = [json.loads(raw)]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            texts.append(item)
    return texts


def test_the_fixture_carries_no_local_paths_or_secrets():
    """脱敏夹具不许带出本机路径、账号、凭据。

    第 2 轮 codex 的 L2:此前只查三种最粗的形态(`C:/`、`:\\\\`、`Users`),
    邮箱 / IP / GUID / UNC / 各类 token 都能从缝里溜过去 ⇒ 这里按**形态**列全,
    每条一个正则,命中即红。

    盘符那条带**否定回顾**(`(?<![A-Za-z0-9])`):挡的是**协议名里的冒号**
    (`http:` 的 `p:`、`file:` 的 `e:`——前面紧挨着词字符)。
    它挡不住 `file:///e:/x.js`(前面是斜杠)⇒ 那一条照样命中,而那**本来就该命中**
    (Windows 上带盘符的 file URL 就是真路径)。
    """
    import re

    raw = FIXTURE.read_text(encoding="utf-8")
    for what, (pattern, _must_hit, _must_not) in FORBIDDEN_PATTERNS.items():
        for text in _fixture_texts(raw):
            hits = re.findall(pattern, text)
            assert not hits, f"夹具里出现了{what}:{hits[:3]}"
    # 非 ASCII:中文/本地化用户名之类一律不许进来(夹具是机器生成的,应当纯 ASCII)
    non_ascii = sorted({c for c in raw if ord(c) > 127})
    assert not non_ascii, f"夹具里有非 ASCII 字符:{non_ascii[:10]}"


def test_each_privacy_pattern_actually_catches_its_shape():
    """隐私检查的每条正则都必须**真的能抓住**它对应的形态。

    否则一条写坏的正则(漏了转义、括号没闭合)会**永远匹配不到** ⇒ 检查变成空转,
    而「没有发现泄漏」与「根本没在查」在结果上长得一模一样。
    ⇒ 每条规则配两组样本:**该抓住的**与**不该抓住的**。
    后一组是反向对照:正则宽了会把正常内容也判成泄漏(例如把 `file:///…` 里的
    `e:/` 当成盘符),那样这条检查会天天假红,最后被人注释掉。
    """
    import re

    for what, (pattern, must_hit, must_not) in FORBIDDEN_PATTERNS.items():
        for sample in must_hit:
            assert re.search(pattern, sample), f"{what} 的正则抓不住该形态的样本:{sample!r}"
        for sample in must_not:
            assert not re.search(pattern, sample), f"{what} 的正则太宽,误判了正常内容:{sample!r}"


def test_the_fixture_is_scanned_in_both_forms():
    """「两遍都扫」这件事本身不能是空转。

    注意:当前夹具里**一个反斜杠都没有**(也不该有——有就是路径泄漏)⇒
    第二遍(反序列化)对**今天这份**夹具不新增任何命中。它是**前瞻**:
    以后有人往夹具里加了带反斜杠的字符串,那一遍才显形。
    ⇒ 这里测的是**机制**本身(拿一段人造的转义 JSON 验),不是要求夹具变脏。
    """
    raw = FIXTURE.read_text(encoding="utf-8")
    texts = _fixture_texts(raw)
    assert raw in texts, "原文自己没进扫描集合"
    assert len(texts) > 10, f"扫的形态太少(只有 {len(texts)} 个):{texts[:3]}"
    assert any(t != raw for t in texts), "没有任何反序列化后的字符串"

    # 机制:JSON 原文里是 `C:\\Users\\bob`(反斜杠被转义成两个),
    # 反序列化后必须显形出**单反斜杠**的真实路径形态。
    sample = '{"a": "C:\\\\Users\\\\bob\\\\x.json"}'
    forms = _fixture_texts(sample)
    assert sample in forms, "原文没进扫描集合"
    assert "C:\\Users\\bob\\x.json" in forms, forms


def test_the_fixture_only_names_known_vendor_scopes():
    """夹具里只允许出现已登记的 npm scope。

    为什么连 scope 也要管:第三方插件的**发布者**是谁属于「这台机器装了什么」,
    不是检查器需要的 ⇒ 已脱敏成 `@third-party`。⇒ 以后往夹具加条目,
    新的 scope 必须先在这里登记(新增 = 一次有意识的决定,不是随手带进来)。
    """
    import re

    text = FIXTURE.read_text(encoding="utf-8")
    scopes = set(re.findall(r"@([A-Za-z0-9._-]+)/", text))
    assert scopes == KNOWN_VENDOR_SCOPES, sorted(scopes)


def test_a_group_that_is_also_named_like_an_mcp_client_is_not_passed_silently():
    """G8 第 2 轮 A 的 L2:分组展开发生在 MCP 包名检查之前。`name` 只能有一个值,
    所以「既是 cordis:group 又叫 dsh-mcp-client」的条目实际上就是 MCP 条目——它不得被当成分组展开放行。"""
    odd = {"id": "x", "name": pc.MCP_PACKAGE, "config": [_entry("c", "dsh-terminal")]}
    problems = _check(_clean_dump() + [odd])
    assert any("MCP" in p and "'x'" in p for p in problems), problems
