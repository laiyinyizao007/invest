#!/usr/bin/env python
"""后果域分档器:判定一批变更文件属于「研究域」还是「执行域」。

**为什么要有这个脚本**:项目级 CLAUDE.md 里若只写「研究类改动走轻流程」,
那是一句无法执行的口号——人和代理都能给自己的改动找到「这算研究」的理由。
本脚本把判据变成**一次调用**:路径落在显式研究白名单里 → 研究域;其余一律执行域
(默认拒绝,fail-closed)。白名单之外的东西不需要理由,需要理由的是白名单里的东西。

**分档的判据不是「这个文件看起来像不像研究代码」,而是「改它能不能改变真钱链路的结论」。**
仓库里已经有好几道机器可校验的闸门,它们各自守住一段链:
晋级门槛(`check_gate`)→ 实盘准入(`check_live_admission`)→ 批准账本(`record_approval`)→
候选静态筛查(`screen_source`)→ 风控底线运行期核对(`check_instance_state`)。
**凡是把结论喂进这些闸门的模块,都属于执行域**——哪怕它住在 `scripts/` 下、
名字里带 research。

几个反直觉的例子(本仓库真实存在的):
- `scripts/research_sandbox.py` 名字里有 research,却是**批准账本的唯一写入者**。
- `scripts/research_grid.py` 是"选参",但它算出的 `on_plateau` 是晋级门槛第 8 条。
- `scripts/backtest_lib.py` 是"回测引擎",但它跑完会核对风控底线有没有被改写。
- `risk/dsr.py` / `risk/plateau.py` / `risk/trial_ledger.py` 只是纯函数,
  但它们的输出分别是门槛第 9 条、第 8 条和第 9 条的输入。

三档输出:
- 研究域     —— 改动只影响研究产物(候选代码、选参、数据准备、报告、文档);
- 不可降级   —— 改它能改变真钱链路的结论(判定/授权/隔离/风控/凭据/部署/CI);
- 执行域     —— 其余一切(默认拒绝:不在研究白名单里的都算这里)。

退出码:
- 0 全部为研究域;
- 1 含不可降级或执行域文件(必须走完整流程);
- 2 用法错误 / 环境不可用 —— 同样是失败:**判不了等于不放行**。

只用标准库,不 import bootstrap(不装交易环境也能跑,与 tools/check_constitution_history.py 同一条纪律)。
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 不可降级清单:改它能改变真钱链路的结论。
#
# 判据一句话:**这个文件的内容(或它的缺席)会不会改变一道判定/授权/隔离/风控的结果。**
# 会 → 不可降级。
#
# 注意这里包含若干「长得像研究代码」的文件,理由逐条写在注释里——
# 这不是保守,是实测:这些模块的输出被下游闸门直接消费(见 ADR 的「实测」一节)。
# --------------------------------------------------------------------------

NEVER_DOWNGRADE = (
    # --- 晋级门槛的判定与输入 ---
    # 阈值与判定本体;放宽 = 让不合格候选过关
    "risk/promotion_gate.py",
    # 组装门槛输入并调用 check_gate
    "risk/candidate_eval.py",
    # DSR 值 = 门槛第 9 条的输入
    "risk/dsr.py",
    # parameter_on_plateau = 门槛第 8 条的输入
    "risk/plateau.py",
    # 试验次数 = DSR 去通胀的输入;少记等于放水
    "risk/trial_ledger.py",

    # --- 实盘准入与批准链 ---
    "risk/live_admission.py",       # 生产 API 前置检查
    "scripts/record_promotion.py",  # 实盘准入批准文件的唯一合法写出者
    "scripts/research_sandbox.py",        # 批准账本唯一写入者(record_approval)
    "scripts/research_api.py",            # approve/review CLI 入口 + 候选装载
    "scripts/research_approval_server.py",# 带外批准页(人输入 sha256 才写账本)
    "scripts/research_console.py",        # 同一批 CLI 入口的薄壳
    "scripts/research_gateway.py",        # 模型→内核的唯一网络通道(端点白名单)
    "scripts/research_config.py",         # 沙盒配置加载器(硬边界、只能追加的禁用清单)

    # --- 门槛评估的编排层 ---
    # 名字像"研究",但算出的东西直接喂进 check_gate
    "scripts/research_gate.py",
    "scripts/research_grid.py",

    # --- 回测引擎:跑完要核对风控底线有没有被改写 ---
    "scripts/backtest_lib.py",

    # --- 不可变宪法:决定候选策略「能碰什么」 ---
    "risk/constitution.py",
    "config/immutable_constitution.json",

    # --- 沙盒与看板的边界配置 ---
    "config/research_sandbox.json",
    "config/risk_manager_setting.json",
    "config/status_dashboard.json",

    # --- 方法学闸门:决定「哪些数据可以再看一次」 ---
    "scripts/segment_windows.py",
    "scripts/segment_ledger.py",
    "config/test_segment_usage.json",

    # --- 风控底线 ---
    "risk/risk_guard.py",
    "risk/account_guard.py",
    "risk/order_policy.py",
    "risk/positions.py",

    # --- 人类读的判定呈现面 ---
    # 报告里的「结论: 通过全部门槛」是给人看的判定表述,被 dashboard 消费
    "risk/report.py",
    "risk/diagnosis.py",

    # --- 运行时与柜台 ---
    "trading/",
    "ctp_tools/",
    "strategies/",              # 被实际交易的模板与基线
    "notify/",                  # 外发告警 + 凭据脱敏
    "dashboard/",               # 反映交易实况的面板(含控制面)
    "scripts/run_headless.py",
    "scripts/run_gui.py",
    "scripts/pause_trading.py",
    "scripts/unlock_account.py",
    "scripts/use_env.py",
    "scripts/simnow_guard.py",
    "scripts/penetration_check.py",
    "scripts/collect_reject_texts.py",
    "scripts/collect_terminal_info.py",
    "scripts/record_market_data.py",
    "scripts/record_market_data_wrapper.py",
    "scripts/record_market_incremental.py",
    "scripts/broker_evidence.py",
    "scripts/broker_report.py",
    "scripts/broker_test.py",
    "scripts/drill.py",
    "scripts/drill_trace.py",
    "scripts/status_server.py",
    "scripts/unattended_report.py",
    "scripts/bootstrap.py",     # 导入顺序与时区:改它会影响所有脚本的运行环境
    "config/risk_manager_setting.json",

    # --- 模型可见通道:工具目录恰为三项是锁死的不变量 ---
    "tools/research_mcp/",

    # --- 凭据与部署 ---
    # 根目录的 .env* 一网打尽:.env / .env.guoxin / .env.simnow 等档案全部 gitignore、
    # 不入库,所以下面 test_never_downgrade_entries_all_exist 不能用"入库文件"校验它们
    # (见该测试里的例外)。凭据文件不入库,恰恰是它们不在 git ls-files 里的原因——
    # 放行它们等于放行真钱。
    ".env*",
    "config/.env*",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.research.yml",
    "docker/",
    "requirements.txt",
    "requirements-research.txt",
    "requirements-dashboard.txt",
    "pyproject.toml",

    # --- CI 检出能力:改测试 = 改「缺陷能不能被发现」 ---
    ".github/workflows/",
    "tools/check_constitution_history.py",
    # 防线不变量与门槛相关的用例:改断言 = 削弱检出能力
    "tests/test_research_boundary.py",
    "tests/test_research_config.py",
    "tests/test_research_sandbox.py",
    "tests/test_research_gateway.py",
    "tests/test_research_approval_server.py",
    "tests/test_constitution.py",
    "tests/test_constitution_round11.py",
    "tests/test_constitution_round12.py",
    "tests/test_constitution_round25.py",
    "tests/test_merge_main_sandbox_union.py",
    "tests/test_candidate_escape_production_path.py",
    "tests/test_helper_module_escape.py",
    "tests/test_live_admission.py",
    "tests/test_candidate_eval.py",
    "tests/test_diagnosis.py",
    "tests/test_dsr.py",
    "tests/test_discipline.py",
    "tests/test_guard_integration.py",
    "tests/test_dashboard_bypass_matrix.py",
    "tests/test_research_api.py",
    "tests/test_research_gate.py",
    "tests/test_research_grid.py",
    "tests/test_segment_ledger.py",
    "tests/test_optimize.py",
    "tests/test_walk_forward_optimizer.py",
    # 用例里直接调用判定点的:改断言 = 削弱检出能力
    "tests/test_promotion_gate.py",
    "tests/test_plateau.py",
    "tests/test_sandbox_module_view.py",
    "tests/test_sandbox_round25_rules.py",
    "tests/test_sandbox_round26_rules.py",
)

# --------------------------------------------------------------------------
# 研究域白名单:改这里只影响研究产物的内容与结论,碰不到任何判定/授权/隔离。
#
# 判据:**这个文件的输出会不会被真钱链路上的闸门消费?**
# 会 → 不在白名单(见不可降级清单,那里逐条写了实测理由)。
# --------------------------------------------------------------------------

# 目录前缀(以 / 结尾,含子目录)
RESEARCH_DIRS = (
    "pending_strategies/",       # 候选策略代码:改了就换了 sha256,已有批准自动失效
    "tools/bisheng/",            # 毕昇工作流构建脚本(只生成工作流定义,不碰沙盒)
    "evidence/",                 # 离线证据分析
    "data/research/",            # 研究运行产物
    "data/research-control/",    # 研究批准账本(只读快照;真正的账本写入在上面)
    "data/results/",             # 研究结果
    "docs/adr/",                 # 决策记录:研究方法学的 ADR 恰恰是研究产物
    "docs/plans/",               # 方案文档
)

# 单文件
RESEARCH_FILES = (
    # 选参与优化(只产出候选参数,不判定门槛是否通过)
    "scripts/optimize.py",
    "scripts/optimize_15m.py",
    "scripts/walk_forward_optimizer.py",
    # 数据准备(离线)
    "scripts/build_continuous.py",
    "scripts/calc_ladder_eta.py",
    "scripts/fetch_data.py",
    "scripts/import_minute.py",
    # 文档与本文件自身
    "CLAUDE.md",
    "CHANGELOG.md",
    "PROJECTWIKI.md",
    "README.md",
)

# 白名单里的例外:docs/web/ 是状态面板的前端与契约,面板能反映交易实况 → 执行域。
RESEARCH_DIR_EXCLUSIONS = (
    "docs/web/",
)

# 执行域里其余的敏感面:不是「不可降级」(改它们不必都算削弱防线),
# 但它们是交易/部署面,默认落在执行域即可,单独列出仅为报告可读性。
#
# 注意 `docs/adr/`、`docs/plans/` **不在**这里:研究方法学的 ADR 恰恰是研究产物,
# 把它们判执行域等于让研究档在最需要留下决策记录的地方失效。
# 「文档可能声明一条没接线的防线」这一风险,由 CLAUDE.md §4 的
# **文档主题升档规则**兜住(内容判据,机器判不了——见 ADR 的「已知局限」)。
EXECUTION_NOTES = (
    "tools/webgen/",
    "tools/audit_frontend_export.py",
    "tools/build_static_manifest.py",
    "tools/repro_logout_crash.py",
    "tools/simnow_gui.ps1",
    "docs/live-readiness.md",
    "docs/runbook-simnow.md",
    "docs/runbook-market-recording.md",
    "docs/broker-filing.md",
    "docs/architecture.md",
    "docs/modules.md",
    "docs/methodology-mapping.md",
    "docs/roadmap.md",
)


def _norm(path: str) -> str:
    """统一成仓库内相对路径、正斜杠、无前导 ./。"""
    p = path.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _matches(rel: str, pattern: str) -> bool:
    """匹配一个清单条目。

    两种语义:
    - 以 / 结尾 = 目录前缀(该目录下的一切);
    - 否则 = fnmatch 全串匹配(支持 `*`,例如凭据文件 `.env*` 一网打尽)。

    fnmatch 的 `*` 会跨 `/`,但整串匹配的前提使得 `config/.env.example` 不会被
    根目录的 `.env*` 误伤——这正是我们要的:两个位置要分别管。
    """
    if pattern.endswith("/"):
        return rel.startswith(pattern)
    return fnmatch.fnmatchcase(rel, pattern)


def classify(path: str) -> tuple[str, str]:
    """返回 (档位, 理由)。档位 ∈ {research, never_downgrade, execution}。"""
    rel = _norm(path)
    if not rel:
        return "execution", "空路径"

    for prefix in NEVER_DOWNGRADE:
        if _matches(rel, prefix):
            return "never_downgrade", f"真钱链路:{prefix}"

    for prefix in RESEARCH_DIR_EXCLUSIONS:
        if _matches(rel, prefix):
            return "execution", f"研究白名单例外:{prefix}"

    for prefix in RESEARCH_DIRS:
        if _matches(rel, prefix):
            return "research", f"研究目录:{prefix}"

    if rel in RESEARCH_FILES:
        return "research", "研究白名单单文件"

    for prefix in EXECUTION_NOTES:
        if _matches(rel, prefix):
            return "execution", f"交易/部署面:{prefix}"

    return "execution", "默认拒绝(不在研究白名单)"


def classify_all(paths: list[str]) -> list[tuple[str, str, str]]:
    """返回 [(原路径, 档位, 理由)]。"""
    return [(p, *classify(p)) for p in paths]


def verdict(entries: list[tuple[str, str, str]]) -> str:
    if not entries:
        return "execution"
    for _, tier, _ in entries:
        if tier != "research":
            return "execution"
    return "research"


def git_changed_paths(base: str) -> list[str]:
    """列出相对 base 的改动文件(含未跟踪)。失败抛 RuntimeError(fail-closed)。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"无法读取 git 改动(base={base!r}): {exc!r}") from exc

    paths = list(out)
    for line in status:
        #  porcelain:XY path ; 重命名形如 "R  old -> new"
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        raw = raw.strip('"')
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按后果域给变更文件分档(研究域 / 不可降级 / 执行域)",
    )
    parser.add_argument("paths", nargs="*", help="变更文件路径(仓库相对)")
    parser.add_argument(
        "--diff", metavar="BASE",
        help="改为取 git 相对 BASE 的改动(含未跟踪);BASE 形如 origin/main",
    )
    args = parser.parse_args(argv)

    if args.diff:
        try:
            paths = git_changed_paths(args.diff)
        except RuntimeError as exc:
            print(f"环境不可用(fail-closed): {exc}", file=sys.stderr)
            return 2
    else:
        paths = list(args.paths)

    if not paths:
        print("用法错误:没有待判定的路径(给位置参数,或用 --diff BASE)", file=sys.stderr)
        return 2

    entries = classify_all(paths)
    overall = verdict(entries)

    for path, tier, reason in entries:
        label = {"research": "研究域", "never_downgrade": "不可降级", "execution": "执行域"}[tier]
        print(f"{label:<6} {path}    ({reason})")

    counts = {"research": 0, "never_downgrade": 0, "execution": 0}
    for _, tier, _ in entries:
        counts[tier] += 1
    print(f"\n合计 {len(entries)} 个文件:研究域 {counts['research']} / "
          f"不可降级 {counts['never_downgrade']} / 执行域 {counts['execution']}")
    print("总判定:", "研究域(走轻流程)" if overall == "research" else "执行域(走完整流程)")

    return 0 if overall == "research" else 1


if __name__ == "__main__":
    sys.exit(main())
