"""后果域分档器(`tools/check_domain.py`)的测试。

**为什么要双向探针**:只测「该拦的拦住了」会漏掉「该放的被误伤」——后者让研究档形同虚设,
整份分档就退化成一句口号。所以每个规则都配一条放行用例和一条拦截用例。

**为什么要有动态探针而不是只测静态清单**:清单是手写的,会随仓库演进而失准。
`test_whitelist_matches_repo_layout` 会真的去仓库里扫一遍,确认:
- 白名单里的文件**都真实存在**(否则清单里躺着已删除的文件,分档会永远放行一个不存在的路径);
- 生产 import 闭包**不与研究白名单相交**(否则某个「研究文件」其实被交易运行时读到,
  放行它就是放行一条真钱链路)。这条是本文件里最有价值的断言:它用依赖图证伪清单。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_domain as cd  # noqa: E402
import verdict_graph as vg  # noqa: E402


# --------------------------------------------------------------------------
# 单元层:分类规则
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "scripts/optimize.py",
    "scripts/optimize_15m.py",
    "scripts/fetch_data.py",
    "scripts/import_minute.py",
    "scripts/build_continuous.py",
    "scripts/calc_ladder_eta.py",
    "pending_strategies/atr_breakout_15m_2atr.py",
    "tools/bisheng/build_v3.py",
    "evidence/logstats.py",
    "CLAUDE.md",
    "PROJECTWIKI.md",
    "CHANGELOG.md",
])
def test_research_paths_pass(path: str) -> None:
    """放行:纯研究产物的文件判为研究域。"""
    tier, _ = cd.classify(path)
    assert tier == "research", f"{path} 应判研究域,实际 {tier}"


def test_walk_forward_optimizer_is_never_downgrade() -> None:
    """滚动优化器提供**晋级门槛评估用的窗口与数据文件**,必须不可降级。

    事实链:`research_api.py:887-890` 取 `wf.DATA_FILE` 与 `wf.build_windows()`
    交给 `run_gate(...)` → `research_gate.py:67` → `ce.evaluate(...)` → `check_gate`。
    改它 = 改 check_gate 的输入。它还自带一份 `TEST_SEGMENT_START`
    (与 `scripts/segment_windows.py` 重复的边界实现),同样属方法学闸门面。

    这条是 codex 第 1 轮复核的 H1(High):初版把它留在研究白名单,
    而同一判据下的 `research_grid.py` 已在不可降级清单里——**同一能力两种分档**。
    """
    assert cd.classify("scripts/walk_forward_optimizer.py")[0] == "never_downgrade"
    assert "scripts/walk_forward_optimizer.py" not in cd.RESEARCH_FILES


@pytest.mark.parametrize("path", [
    # 晋级门槛的判定与输入
    "risk/promotion_gate.py",
    "risk/candidate_eval.py",
    "risk/dsr.py",
    "risk/plateau.py",
    "risk/trial_ledger.py",
    # 实盘准入与批准链
    "risk/live_admission.py",
    "scripts/record_promotion.py",
    # 批准链(名字像研究代码,但改它=改「什么被批准」)
    "scripts/research_sandbox.py",
    "scripts/research_api.py",
    "scripts/research_approval_server.py",
    "scripts/research_console.py",
    # 隔离边界
    "scripts/research_gateway.py",
    "scripts/research_config.py",
    "config/research_sandbox.json",
    # 门槛评估的编排层(名字像研究,但输出直接喂进 check_gate)
    "scripts/research_gate.py",
    "scripts/research_grid.py",
    # 回测引擎(跑完核对风控底线)
    "scripts/backtest_lib.py",
    # 人类读的判定呈现面
    "risk/report.py",
    "risk/diagnosis.py",
    # 方法学闸门
    "scripts/segment_windows.py",
    "scripts/segment_ledger.py",
    "config/test_segment_usage.json",
    # 风控底线
    "risk/risk_guard.py",
    "risk/account_guard.py",
    "risk/order_policy.py",
    "risk/positions.py",
    # 不可变宪法
    "risk/constitution.py",
    "config/immutable_constitution.json",
    # 运行时与柜台
    "trading/runtime.py",
    "ctp_tools/simnow.py",
    "strategies/trend_atr_strategy.py",
    "notify/webhook.py",
    "dashboard/app.py",
    "scripts/pause_trading.py",
    "scripts/unlock_account.py",
    "scripts/use_env.py",
    "scripts/simnow_guard.py",
    # 模型可见通道
    "tools/research_mcp/server.py",
    # 凭据与部署
    ".env",
    "config/.env.example",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    "pyproject.toml",
    # CI 检出能力
    ".github/workflows/research-guard.yml",
    "tools/check_constitution_history.py",
    "tests/test_research_boundary.py",
])
def test_never_downgrade_paths_are_blocked(path: str) -> None:
    """拦截:判定/授权/隔离面一律不可降级。"""
    tier, _ = cd.classify(path)
    assert tier == "never_downgrade", f"{path} 应判不可降级,实际 {tier}"


@pytest.mark.parametrize("path", [
    "some/unknown/file.py",           # 未知路径 → 默认拒绝
    "risk/new_module.py",
    "scripts/new_thing.py",
    "docs/web/status-api.openapi.json",  # 面板契约 → 白名单例外
    "docs/live-readiness.md",
])
def test_unknown_and_excluded_paths_are_blocked(path: str) -> None:
    """拦截:不在白名单的一律执行域(fail-closed 默认)。"""
    tier, _ = cd.classify(path)
    assert tier == "execution", f"{path} 应判执行域,实际 {tier}"


@pytest.mark.parametrize("path", [
    "docs/adr/20261114-consequence-domain-tiers.md",
    "docs/adr/20261109-negative-backtest-result.md",
    "docs/plans/20261112-bisheng-integration-spec.md",
])
def test_decision_records_are_research_domain(path: str) -> None:
    """ADR 与方案文档判研究域。

    理由:研究方法学的决策记录恰恰是研究产物,判执行域会让研究档在最需要
    留下记录的地方失效。**但**「文档声明了防线状态」这一情形由 CLAUDE.md §4
    的文档主题升档规则兜住——那是内容判据,机器判不了(见 ADR 的「已知局限」)。
    """
    tier, _ = cd.classify(path)
    assert tier == "research", f"{path} 应判研究域,实际 {tier}"


def test_whitelist_entries_beat_directory_default() -> None:
    """单文件白名单优先于目录默认;两者冲突时以更保守的一方为准。"""
    # scripts/ 不是研究目录,但 optimize.py 在单文件白名单里
    assert cd.classify("scripts/optimize.py")[0] == "research"
    # 同目录下的其他脚本不在白名单 → 执行域或不可降级
    assert cd.classify("scripts/bootstrap.py")[0] == "never_downgrade"
    assert cd.classify("scripts/unattended_report.py")[0] == "never_downgrade"


def test_path_normalization() -> None:
    """路径写法差异不得改变判定——否则换个斜杠就能绕过。"""
    for variant in ("scripts/optimize.py", "./scripts/optimize.py",
                    "scripts\\optimize.py", "  scripts/optimize.py  "):
        assert cd.classify(variant)[0] == "research", variant
    for variant in ("risk/live_admission.py", "./risk/live_admission.py",
                    "risk\\live_admission.py"):
        assert cd.classify(variant)[0] == "never_downgrade", variant


def test_verdict_is_most_conservative() -> None:
    """总判定取最保守:只要有一个文件不是研究域,整体就是执行域。"""
    assert cd.verdict([("a", "research", "")]) == "research"
    assert cd.verdict([("a", "research", ""), ("b", "execution", "")]) == "execution"
    assert cd.verdict([("a", "research", ""), ("b", "never_downgrade", "")]) == "execution"
    assert cd.verdict([]) == "execution"   # 空集不放行


# --------------------------------------------------------------------------
# 动态探针:用仓库真实布局与真实依赖图证伪清单
# --------------------------------------------------------------------------

def _repo_files() -> list[str]:
    """仓库里的全部文件(已入库 + 未跟踪)。

    必须含未跟踪文件:新写的文件还没 `git add` 时,`git ls-files` 看不到它——
    那道「全仓不变量」就会漏掉刚新建的、恰好拥有判定点的文件。
    """
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    status = subprocess.run(
        # `-uall`:不带它,git 会把**未跟踪的整个目录**折叠成一行 `?? newpkg/`,
        # 目录里的文件就看不见了 —— 新建目录里的文件会绕过所有扫描。
        ["git", "-C", str(ROOT), "status", "--porcelain", "-uall"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    extra: list[str] = []
    for line in status:
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        raw = raw.strip('"')
        if raw and raw not in tracked:
            extra.append(raw)
    return tracked + extra


def test_whitelist_entries_all_exist() -> None:
    """白名单里的每个路径都必须真实存在。

    否则清单会躺着已删除的文件:分档器对这些路径永远放行,而实际上那个文件早没了——
    更糟的是有人**新建**同名文件时会自动获得研究域待遇,绕过分档。

    判定用「已入库 OR 在磁盘上存在」:新建但尚未 `git add` 的文件也算数,
    否则本文件自己(CLAUDE.md)刚写出来时这条会红。
    """
    tracked = set(_repo_files())
    missing = [p for p in cd.RESEARCH_FILES
               if p not in tracked and not (ROOT / p).exists()]
    assert not missing, f"研究白名单里有仓库中不存在的文件: {missing}"


def test_research_dir_prefixes_are_deliberate() -> None:
    """研究**目录前缀**清单里的每一条,都必须**要么真实存在、要么是运行时目录**。

    目录前缀不做「存在性」校验——其中 `pending_strategies/`、`data/research/`、
    `data/research-control/`、`data/results/` 是 **gitignore 的运行时目录**,
    在干净的 worktree 里本来就不存在(已实测确认),校验它们会误报。

    但也不能因此放任:一条**写错**的目录前缀(比如 `pendng_strategies/`)会
    静默失效——该目录下的文件落进默认拒绝(安全方向),可是研究文件会因此
    拿不到轻流程(可用性损失,且没人会发现)。

    所以判据是**穷尽**的:每个前缀必须落在下面两类之一——
    ①**真实存在**的目录(已入库的:tools/bisheng/、evidence/、docs/adr/、docs/plans/);
    ②**gitignore 的运行时目录**(pending_strategies/、data/* —— 运行时才有内容)。
    两类都不满足 → 前缀写错了 → 红。
    (这条比「拼写近似」有意义:后者对 8 个前缀里 6 个不可达——codex 第 2 轮的 Low-4。)
    """
    for prefix in cd.RESEARCH_DIRS:
        assert prefix.endswith("/"), f"目录前缀应以 / 结尾:{prefix}"
        if (ROOT / prefix.rstrip("/")).exists():
            continue  # ①目录真实存在
        # ②必须是 gitignore 的运行时目录:它不在库里,运行时才会有内容。
        #    **必须探一个目录里的文件**,不能探目录本身:
        #    `git check-ignore` 对**不存在**的路径会给出假匹配
        #    (实测:任何不存在的路径都报 ".gitignore:65" 且退出码 0),
        #    那样这条判据就恒真、等于没有。
        probe = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", f"{prefix}__probe__.tmp"],
            capture_output=True, text=True,
        )
        assert probe.returncode == 0, (
            f"目录前缀 {prefix} 既不是真实存在的目录,也不是 gitignore 的运行时目录"
            f" —— 多半是写错了"
        )


def test_never_downgrade_entries_all_exist() -> None:
    """不可降级清单同理:不能有幻觉条目(会让人误以为某个真实文件被保护)。

    例外:凭据文件按设计 gitignore、不入库,所以不能用 git ls-files 校验——
    它们恰恰**不在**入库列表里才正常。改用「存在于磁盘 OR 属于凭据模式」判定。
    """
    tracked = set(_repo_files())
    missing = []
    for p in cd.NEVER_DOWNGRADE:
        if p.endswith("/") or "*" in p:
            continue  # 目录前缀与通配模式:按模式校验,见 test_credential_patterns_are_blocked
        if p in tracked:
            continue
        if (ROOT / p).exists():
            continue  # 未入库但存在于磁盘(如本机新增的文件)
        missing.append(p)
    assert not missing, f"不可降级清单里有仓库中不存在的文件: {missing}"


def test_credential_patterns_are_blocked() -> None:
    """凭据文件按模式拦截:根目录与 config/ 下的 .env 全家都不能被放行。

    这些文件 gitignore、不入库,动态探针扫不到它们,所以这里直接点名断言。
    """
    for p in (".env", ".env.guoxin", ".env.simnow", ".env.example",
              "config/.env.example"):
        assert cd.classify(p)[0] == "never_downgrade", p


def _production_closure() -> set[str]:
    """算出生产入口(交易运行时)的本地 import 闭包。

    **模块解析必须认 `scripts/` 下的裸模块导入**:`scripts/run_headless.py` 里是
    `import bootstrap`,解析到的是 `scripts/bootstrap.py`——而 bootstrap 负责加载
    `.env` 的交易凭据、设置时区与导入顺序。只按「仓库根 + 模块名」解析会**漏掉它**,
    闭包就不完整(实测 17 项 vs 完整 18 项)。
    解析器与 `tools/verdict_graph.py` 保持同一口径,免得两处各算各的。
    """
    def mod_to_path(mod: str) -> Path | None:
        p = mod.replace(".", "/")
        for cand in (ROOT / f"{p}.py", ROOT / p / "__init__.py",
                     ROOT / "scripts" / f"{p}.py", ROOT / "scripts" / p / "__init__.py"):
            if cand.exists():
                return cand
        return None

    def imports_of(fp: Path) -> set[str]:
        try:
            tree = ast.parse(fp.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return set()
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module)
        return found

    entries = [ROOT / "trading" / "runtime.py",
               ROOT / "scripts" / "run_headless.py",
               ROOT / "scripts" / "run_gui.py"]
    seen: set[Path] = set()
    stack = list(entries)
    closure: set[str] = set()
    while stack:
        fp = stack.pop()
        if fp in seen:
            continue
        seen.add(fp)
        try:
            closure.add(fp.relative_to(ROOT).as_posix())
        except ValueError:
            continue
        for mod in imports_of(fp):
            target = mod_to_path(mod)
            if target is not None and target not in seen:
                stack.append(target)
    return closure


def test_production_closure_does_not_intersect_research_whitelist() -> None:
    """**本文件最重要的一条**:研究白名单不得与生产 import 闭包相交。

    白名单是手写的,会失准;依赖图不会。如果某个「研究文件」被交易运行时 import,
    那么改它就改到了真钱链路上,却会被分档器放行走轻流程 —— 这是清单失效的最坏形式。
    这条断言用真实依赖图把清单证伪一次。
    """
    closure = _production_closure()
    offenders = sorted(p for p in closure if cd.classify(p)[0] == "research")
    assert not offenders, f"这些文件被生产运行时 import,却判成了研究域: {offenders}"


def test_production_closure_is_not_mostly_research() -> None:
    """反向探针:生产闭包里应当**没有**任何研究域文件。

    与上一条互为印证:若上一条因实现 bug 恒真,这一条会先炸出来。
    """
    closure = _production_closure()
    research_in_closure = sorted(p for p in closure if cd.classify(p)[0] == "research")
    assert not research_in_closure
    # 生产闭包非空,否则说明依赖图算法坏了(上面的断言会变成空转)
    assert len(closure) >= 5, f"生产闭包异常小,依赖图算法可能失效: {closure}"


def test_critical_defense_files_are_never_downgrade() -> None:
    """关键防线文件逐个点名断言(不依赖清单整体逻辑)。"""
    critical = [
        "risk/live_admission.py",
        "risk/promotion_gate.py",
        "risk/candidate_eval.py",
        "risk/dsr.py",
        "risk/plateau.py",
        "risk/trial_ledger.py",
        "risk/constitution.py",
        "scripts/research_sandbox.py",
        "scripts/research_api.py",
        "scripts/research_approval_server.py",
        "scripts/research_gateway.py",
        "scripts/research_gate.py",
        "scripts/research_grid.py",
        "scripts/backtest_lib.py",
        "scripts/segment_windows.py",
        "scripts/segment_ledger.py",
        "config/immutable_constitution.json",
        "config/test_segment_usage.json",
    ]
    for path in critical:
        assert cd.classify(path)[0] == "never_downgrade", path


# --------------------------------------------------------------------------
# 动态探针:研究白名单不得够到任何「真钱链路判定点」
# --------------------------------------------------------------------------
# 动态探针:用真实代码证伪白名单
# --------------------------------------------------------------------------
# 判定点的分析在 tools/verdict_graph.py 里(纯标准库,函数级)。
# 它的判据是**直接归属**:文件自己定义或直接调用了判定点 → 它拥有那个决定。


def _research_whitelist_files() -> list[str]:
    """研究域白名单覆盖到的全部仓库文件。"""
    files = list(cd.RESEARCH_FILES)
    for prefix in cd.RESEARCH_DIRS:
        files += [rel for rel in _repo_files() if rel.startswith(prefix)]
    return sorted(set(files))


def test_research_whitelist_owns_no_verdict_sink() -> None:
    """**本文件最关键的一条**:研究白名单里的文件不得**自己**拥有任何真钱链路的判定点。

    清单是手写的,会失准;代码不会。这条断言用真实 AST 把清单证伪:
    只要某个白名单文件自己定义或直接调用了 `check_gate` / `record_approval`
    / `screen_source` 之类的闸门,它就不是「只影响研究产物」,必须降级。

    这条测试真的抓到过误判:`risk/candidate_eval.py`(直接调 check_gate)、
    `scripts/research_sandbox.py`(批准账本唯一写入者)、
    `scripts/backtest_lib.py`(跑完核对风控底线)、
    `scripts/research_api.py` / `research_approval_server.py` / `research_gateway.py`(筛查与批准链)。
    """
    offenders: dict[str, list[str]] = {}
    for rel in _research_whitelist_files():
        fp = ROOT / rel
        if not fp.exists():
            continue
        sinks = vg.owned_sinks(fp)
        if sinks:
            offenders[rel] = sorted(sinks)

    assert not offenders, (
        "这些研究白名单文件自己拥有真钱链路的判定点,必须改为不可降级: "
        + "; ".join(f"{k} -> {v}" for k, v in sorted(offenders.items()))
    )


def test_every_sink_owning_file_is_never_downgrade() -> None:
    """**全仓不变量**:任何拥有判定点的文件,都必须被判为不可降级。

    上一条只查白名单里已有的文件;这一条查**全仓**——
    新增一个文件、它直接调 `check_gate`,若分档器没把它列为不可降级,这里就会红。
    这是防止清单随仓库演进而悄悄失效的那道闸门。
    """
    offenders: list[str] = []
    for rel in _repo_files():
        fp = ROOT / rel
        if not fp.exists():
            continue
        if vg.owned_sinks(fp) and cd.classify(rel)[0] != "never_downgrade":
            offenders.append(rel)
    assert not offenders, (
        "这些文件自己拥有真钱链路的判定点,却没有被判为不可降级: "
        f"{sorted(offenders)}"
    )


def test_every_gate_input_provider_is_never_downgrade() -> None:
    """**全仓不变量(数据流那一支)**:判定**输入**的提供者也必须不可降级。

    上一条查的是「自己调用判定点」的文件。这条查的是**另一类**:不调用判定点,
    但它的**值**被喂进判定点的文件。

    实测例子(codex 第 1 轮复核的 H1,High):`scripts/research_api.py` 从
    `walk_forward_optimizer` 取 `wf.DATA_FILE`(用哪份数据)、`wf.build_windows()`
    (在哪个窗口上评估)、`wf.SYMBOL`、`wf.KEY`,再交给 `run_gate(...)` → `check_gate`。
    改那个模块 = 改 check_gate 的输入,而调用图上看它「谁都不拥有」——
    所以这条不变量是**独立**的,不是上一条的重复。
    """
    files = []
    for rel in _repo_files():
        fp = ROOT / rel
        if fp.exists() and fp.suffix == ".py":
            files.append(fp)
    offenders = [
        rel for rel in sorted(vg.all_gate_input_providers(files))
        if cd.classify(rel)[0] != "never_downgrade"
    ]
    assert not offenders, (
        "这些文件的值会流进真钱链路的判定点,却没有被判为不可降级: "
        f"{offenders}"
    )


def test_verdict_graph_detector_itself_is_accurate() -> None:
    """**探针自己也要被检验**:分析器必须既不漏报,也不误报。

    一个只会喊狼来了的探针,等于没有探针(本仓库吃过一次「假安心」的亏:
    状态页把损坏的心跳当成运行中)。所以这里用一组人工核对的期望值夹住它:
    - 期望「拥有判定点」的,必须被检出;
    - 期望「不拥有」的,必须不被检出——尤其要挡住那种「经助手间接到达,
      就把助手助手的判定算到调用者头上」的误伤。
    """
    expect_owned = {
        "risk/candidate_eval.py": "check_gate",
        "risk/live_admission.py": "check_live_admission",
        "risk/constitution.py": "screen_source",
        "scripts/backtest_lib.py": "check_instance_state",
        "scripts/research_sandbox.py": "record_approval",
        "scripts/research_api.py": "screen_source",
        "scripts/research_approval_server.py": "record_approval",
        "scripts/research_gateway.py": "screen_source",
        "scripts/run_headless.py": "check_live_admission",
        "scripts/record_promotion.py": "write_approval",
    }
    expect_not_owned = [
        # 只调用纯数学函数的:import 了 candidate_eval,但没碰门槛判定
        "scripts/calc_ladder_eta.py",
        # 只产出选参结果,不判定门槛是否通过
        "scripts/optimize.py",
        "scripts/optimize_15m.py",
        "scripts/walk_forward_optimizer.py",
        # 纯函数:输出虽是门槛的输入,但自己不做判定
        "risk/dsr.py",
        "risk/plateau.py",
        "risk/trial_ledger.py",
        "risk/report.py",
        "risk/diagnosis.py",
    ]
    for rel, sink in expect_owned.items():
        owned = vg.owned_sinks(ROOT / rel)
        assert sink in owned, f"{rel} 应拥有 {sink},实际 {sorted(owned)}"
    for rel in expect_not_owned:
        owned = vg.owned_sinks(ROOT / rel)
        assert not owned, f"{rel} 不应拥有判定点,实际 {sorted(owned)}"


@pytest.mark.parametrize("source,expect", [
    # 直接导入并调用
    ("from risk.promotion_gate import check_gate\ndef go(c):\n    return check_gate(c)\n", "check_gate"),
    # 模块别名 + 属性调用
    ("import risk.promotion_gate as pg\ndef go(c):\n    return pg.check_gate(c)\n", "check_gate"),
    # **函数别名**:裸名字与判定点对不上,只看裸名字会漏报
    ("from risk.promotion_gate import check_gate as cg\ndef go(c):\n    return cg(c)\n", "check_gate"),
])
def test_verdict_graph_detects_all_call_forms(source: str, expect: str, tmp_path: Path) -> None:
    """三种调用形态都必须被检出——尤其**函数别名**那一种。

    codex 第 1 轮复核的 H2(High):`from X import f as g` 后调 `g(...)`,
    裸名字 `g` 与判定点的名字对不上,只看裸名字的版本**漏报**,
    等于给了一条绕过所有探针的路。
    """
    probe = ROOT / "scripts" / "zz_probe_alias.py"
    probe.write_text(source, encoding="utf-8")
    try:
        owned = vg.owned_sinks(probe)
        assert expect in owned, f"应检出 {expect},实际 {sorted(owned)}"
    finally:
        probe.unlink(missing_ok=True)


def test_path_with_dotdot_cannot_escape_to_research() -> None:
    """`..` 不得被用来绕过默认拒绝。

    `data/research/../risk/live_admission.py` 若按字面匹配到 `data/research/` 前缀,
    就会判成**研究域**——一条绕过所有闸门的路。git 不会产出这种路径,
    但分档器是命令行工具,路径由人给。
    """
    for path in ("data/research/../risk/live_admission.py",
                 "data/research/../../risk/promotion_gate.py",
                 "../../etc/passwd"):
        tier, _ = cd.classify(path)
        assert tier != "research", f"{path} 不应判研究域,实际 {tier}"


# --------------------------------------------------------------------------
# CLI 层:退出码语义
# --------------------------------------------------------------------------

def test_cli_exit_codes(tmp_path: Path) -> None:
    """退出码:0 全研究域 / 1 含执行域 / 2 用法错误。"""
    script = str(ROOT / "tools" / "check_domain.py")

    # **必须显式指定 encoding="utf-8"**:CLI 现在输出 UTF-8(Windows 控制台是 cp1252),
    # 而 `text=True` 默认按 locale 解码 —— 解码失败时 stdout 变成 None,
    # 这个用例就变成「只看退出码、看不见输出」的**空跑通过**
    # (codex 第 2 轮的 Low-1)。
    ok = subprocess.run([sys.executable, script, "scripts/optimize.py"],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert ok.stdout is not None and "研究域" in ok.stdout, "CLI 输出必须能被读到"

    blocked = subprocess.run([sys.executable, script, "scripts/optimize.py",
                              "risk/live_admission.py"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    assert blocked.returncode == 1
    assert blocked.stdout is not None and "不可降级" in blocked.stdout

    empty = subprocess.run([sys.executable, script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert empty.returncode == 2


def test_cli_survives_non_utf8_console() -> None:
    """CLI 在**非 UTF-8 控制台**上不得崩溃(codex 第 1 轮复核的 M5)。

    Windows 控制台默认 cp1252,打印中文档位标签会抛 `UnicodeEncodeError`——
    即 CLAUDE.md 文档里那条命令在本仓库主力开发环境(Windows)直接崩,
    退出码也变成 1(与「判为执行域」无法区分)。
    这里用 `PYTHONIOENCODING=cp1252` 复现那个环境。
    """
    script = str(ROOT / "tools" / "check_domain.py")
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    proc = subprocess.run([sys.executable, script, "scripts/optimize.py"],
                          capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"cp1252 下崩溃了:\n{proc.stdout}\n{proc.stderr}"
    assert "UnicodeEncodeError" not in proc.stderr
    assert "研究域" in proc.stdout


def test_cli_reports_reason_per_file(capsys: pytest.CaptureFixture[str]) -> None:
    """报告必须给出理由——分档结论要能被复核,不能是黑盒。"""
    rc = cd.main(["scripts/optimize.py", "risk/live_admission.py"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "研究域" in out and "不可降级" in out
    assert "研究白名单单文件" in out
    assert "真钱链路" in out


# --------------------------------------------------------------------------
# 文档里的条数必须是真的
# --------------------------------------------------------------------------

def test_documented_test_count_matches_reality() -> None:
    """文档里写的「本文件有 N 条」必须与实际收集到的条数一致。

    **为什么要有这条**:本仓库吃过一次亏——复核期间工作树被并发改动,
    六处文档的条数当场作废(见 CHANGELOG 「数字口径统一重算」)。
    本仓库的既有结论是「今后改动测试条数都要重跑一次 collect」——
    但那靠人记。这条断言把它变成机器保证:
    **改了本文件(加/删用例)而没同步文档,这里立刻红。**
    """
    actual = len(_collect_count())
    docs = {
        "CLAUDE.md": ROOT / "CLAUDE.md",
        "CHANGELOG.md": ROOT / "CHANGELOG.md",
        "docs/adr/20261114-consequence-domain-tiers.md":
            ROOT / "docs" / "adr" / "20261114-consequence-domain-tiers.md",
    }
    stale: list[str] = []
    for name, path in docs.items():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        counted = False
        for line in text.splitlines():
            if "test_check_domain.py" not in line:
                continue
            # 只看**确实是条数声明**的行:`\d+ 条`。
            # 光看「含条字」会误伤散文(如「放行一条真钱链路……由 tests/… 的」)。
            # **只取第一个**:同一行后面可能还有别的「N 条」,全取会误报
            # (codex 第 2 轮的 Low-2)。
            match = re.search(r"(\d+)\s*条", line)
            if match is None:
                continue
            counted = True
            if int(match.group(1)) != actual:
                stale.append(f"{name}: 写的是 {match.group(1)} 条,实际 {actual} 条")
        if not counted:
            # 条数被挪到不含文件名的行 → 守护会静默失效,这里拦住
            stale.append(f"{name}: 没有任何一行同时提到本文件与条数(守护会静默失效)")
    assert not stale, (
        "文档里的测试条数与实际不符(改了用例数要同步文档):\n  " + "\n  ".join(stale)
    )


def _collect_count() -> list[str]:
    """收集本文件的用例 id 列表(用 `--collect-only` 数,与 pytest 报告一致)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_check_domain.py",
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    ids = [line.strip() for line in proc.stdout.splitlines() if "::" in line]
    if not ids:
        raise AssertionError(f"pytest --collect-only 没收集到用例:\n{proc.stdout[-500:]}")
    return ids


@pytest.mark.parametrize("import_style", [
    "import walk_forward_optimizer as wf\ndef rd():\n    return wf.DATA_FILE\n",
    "import walk_forward_optimizer\ndef rd():\n    return walk_forward_optimizer.DATA_FILE\n",
    "from walk_forward_optimizer import DATA_FILE\ndef rd():\n    return DATA_FILE\n",
])
def test_data_flow_probe_sees_every_import_style(import_style: str) -> None:
    """数据流探针不得因 **import 写法**不同而失明(codex 第 2 轮的 Medium-1)。

    `from X import DATA_FILE` 这种写法读的是**裸名字**而不是属性,
    早期版本直接 `continue` 掉了——于是同一份代码换个 import 写法
    就绕过了整条数据流探针,只剩人写的那条断言还在拦。
    """
    # 路径必须**每次不同**:verdict_graph 按路径缓存模块信息,
    # 复用同一个路径会读到上一轮的缓存(参数化三种写法会互相干扰)。
    probe = ROOT / "scripts" / f"zz_probe_import_{abs(hash(import_style))}.py"
    probe.write_text(
        "from risk.promotion_gate import check_gate\n"
        "def go(c):\n    return check_gate(c)\n" + import_style,
        encoding="utf-8",
    )
    try:
        providers = vg.gate_input_providers(probe)
        assert "scripts/walk_forward_optimizer.py" in providers, (
            f"该 import 写法下探针失明了,providers={sorted(providers)}"
        )
    finally:
        probe.unlink(missing_ok=True)


def test_data_flow_probe_ignores_incidental_reads() -> None:
    """数据流探针不得**过度包含**(codex 第 2 轮的 Medium-2)。

    「读了一个模块的属性」太宽:读一个跟门槛毫无关系的 helper
    (如控制台编码 helper `reconfigure_stdio`)不应把那个模块拉进保护面。
    否则一旦拥有判定点的文件读了某个**研究文件**的属性,
    那个研究文件会被强制升级为不可降级 —— **研究档被侵蚀**,
    与本 ADR 的方向相反。

    收窄判据:被读的值(或其调用结果)**流进了另一个调用的实参**。
    """
    probe = ROOT / "scripts" / "zz_probe_incidental.py"  # 独立路径,避免缓存串扰
    probe.write_text(
        "from risk.promotion_gate import check_gate\n"
        "def go(c):\n    return check_gate(c)\n"
        "import research_console as rconsole\n"
        "def setup():\n"
        "    rconsole.reconfigure_stdio()   # 纯副作用,结果没人用\n",
        encoding="utf-8",
    )
    try:
        providers = vg.gate_input_providers(probe)
        assert "scripts/research_console.py" not in providers, (
            f"无关的 helper 读取被算成了门槛输入: {sorted(providers)}"
        )
    finally:
        probe.unlink(missing_ok=True)
