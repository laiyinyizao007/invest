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
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 设成 utf-8 + errors="replace"。

    **为什么必须做**:Windows 控制台默认是 cp1252,打印中文档位标签会抛
    `UnicodeEncodeError`——即 CLAUDE.md 文档里那条命令在本仓库主力开发环境
    (Windows)直接崩,退出码也变成 1(与「判为执行域」无法区分)。
    本仓库已有同样的处理(`scripts/research_console.py::reconfigure_stdio`、
    `scripts/bootstrap.py`),这里沿用同一口径。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # 非 TextIO(被 pytest 捕获等)时跳过

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
    # 批准账本的**只读**访问层:研究侧写、运行侧读。
    # 改它 = 改「什么算被批准过」——例如把 is_approved_sha 变成恒真,
    # 任何字节都能过账本这一关。
    "risk/approval_ledger.py",
    # 运行时装载器:approved/ → runtime 的唯一入口。
    # 它消费批准账本与晋级记录,替运行时装载做「拒绝/放行」的判定。
    "risk/approved_guard.py",
    "scripts/research_sandbox.py",        # 批准账本唯一写入者(record_approval)
    "scripts/research_api.py",            # approve/review CLI 入口 + 候选装载
    "scripts/research_approval_server.py",# 带外批准页(人输入 sha256 才写账本)
    "scripts/research_console.py",        # 同一批 CLI 入口的薄壳
    "scripts/research_gateway.py",        # 模型→内核的唯一网络通道(端点白名单)
    "scripts/research_config.py",         # 沙盒配置加载器(硬边界、只能追加的禁用清单)
    "scripts/research_kb.py",             # 知识库白名单与过期检测(MCP 启动研究前的拦截判定;其值被喂进判定链路)
    # 收菜:把候选收进 approved/(运行时唯一认的目录)。它**调用静态筛查**,
    # 并且判定「这份字节能不能进入受信目录」——那是批准链上的一个判定点。
    "scripts/research_collect.py",

    # --- 账本防篡改与人工维护入口(第 47 轮补记)---
    # 试验账本与批准账本的哈希链 + 外置锚点。批准账本的内容**被实盘准入消费**:
    # 链断了却被当成没事发生 = 一条失效的批准被当成有效。改它 = 改批准的效力。
    # **图找得到**:scripts/research_sandbox.py(record_approval 的唯一写入者)import 它,
    # 它的值被喂进判定点 ⇒ verdict_graph 的数据流探针会把它算成 provider。
    "risk/ledger_chain.py",
    # 人工迁移/重置两本账本的入口:**能重写批准账本**(reset 会丢弃链外内容)。
    # **图找不到**(判定点没有 import 它)⇒ 按「图找得到的归图,图找不到的归清单」兜住。
    "risk/ledger_admin.py",
    # 上面那个入口的 CLI 把手(与 research_console.py 同类:本身薄,但它是人操作的入口)。
    "scripts/research_ledger.py",

    # --- 门槛评估的编排层 ---
    # 名字像"研究",但算出的东西直接喂进 check_gate
    "scripts/research_gate.py",
    "scripts/research_grid.py",
    # --- 候选独立进程路径(Issue #29 S1)---
    # 候选进程算出的信号经这几个模块进入评估:信号 → 回放策略 → 引擎 → 日收益 → 门槛输入。
    # 改它们 = 改「候选的信号怎么变成门槛输入」,也就是改候选与框架之间那条唯一的边界。
    # **图找得到**(research_api 的框架侧子入口 import 它们,数据流探针会把它们算成 provider):
    "scripts/research_candidate_process.py",   # 起候选宿主进程、校验响应形状(框架不采信候选的任何自报)
    "scripts/research_candidate_spec.py",      # 框架自己 AST 读 param_grid ⇒ 试验格子数的来源
    "scripts/research_signal_replay.py",        # 信号 → 框架模板回放;受保护参数在这里过检
    # **图找不到**(经子进程边界,没有 import 关系)⇒ 按「图找得到的归图,图找不到的归清单」兜住:
    "scripts/research_candidate_host.py",      # 候选宿主:候选唯一运行的地方,抹路径/收窄能力都在这里
    "research/",                                 # 候选侧运行时包(协议/指标/基类):候选进程里唯一能 import 的东西
    # 滚动优化器:它提供**晋级门槛评估用的窗口与数据文件**
    # (research_api.py:887-890 取 wf.DATA_FILE / wf.build_windows() 后交给 run_gate),
    # 改它 = 改 check_gate 的输入。它还自带一份 TEST_SEGMENT_START(与
    # scripts/segment_windows.py 重复的边界实现),所以同样属于「方法学闸门」面。
    "scripts/walk_forward_optimizer.py",

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
    # 守卫工具自身:它们是「谁拥有判定点」与「清单有没有被测住」的**裁决者**,
    # 改它们 = 改「什么被允许发生」。第 3 轮 codex 的 Medium-1:实测从
    # VERDICT_SINKS 里删掉一个判定点,0 条测试失败——因为守卫工具没被保护、
    # 准确性夹具也没点名全部判定点。
    "tools/verdict_graph.py",
    "tools/check_mutations.py",
    # 分档器**自己**:它拥有「什么算执行域」这个判定点。
    # (默认拒绝已经让它落进执行域,但那样它就还能被塞进研究白名单——
    #  不可降级优先于研究白名单,列在这里才真正堵住那个口子。)
    "tools/check_domain.py",
    # 第 48 轮:CI 侧「真的跑了吗」闸门 + 它的名单 + 守卫自己的用例。
    # 这三样共同决定「一条防线没跑」会不会被发现 = 改它们 = 改检出能力。
    "tools/check_ci_ran.py",
    "tests/guard_roster.json",
    "tests/test_check_ci_ran.py",
    # 第 48 轮:纪律守卫的变异验证脚手架(把"守卫松了"变成可复算的检查)。
    "tools/check_guard_mutations.py",
    # 上面那个脚手架**自己的**守卫用例:守卫工具的守卫也要有人守
    # (仓库教训:新写的守卫工具自己也要有人守)。
    "tests/test_check_guard_mutations.py",
    # 第 55 轮:CI 接线的**规范形**闸门与它的自检。
    # `tools/ci_gate.py` 是 CI 里那一步的**全部**内容(自己跑 pytest、自己核对名单、
    # 用退出码说话)——改它 = 改「闸门判红会不会让 CI 红」。
    "tools/ci_gate.py",
    # `tools/check_ci_wiring.py` 是接线自检的本体:它断言「每个闸门步骤都是规范形」
    # 与「名单里的套件都真的被跑到」——改它 = 改检出能力本身。
    "tools/check_ci_wiring.py",
    # 上面那个自检工具**自己**的用例。
    "tests/test_ci_wiring.py",
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
    # 骨架契约与批准目录装载的用例:它们**直接调用** screen_source / check_class /
    # load_approved_strategy(都是判定点),改断言 = 削弱检出能力。
    "tests/test_strategy_skeleton_contract.py",
    "tests/test_approved_loading.py",
    "tests/test_research_collect.py",
    "tests/test_approval_ledger.py",
    # 自动批准(Issue #67)的隔离不变量:断言「运行侧不读自动账」「自动账只有一处写入点且先判开关」。
    "tests/test_auto_approval.py",
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
    # 第 47 轮:核心防线套件——直接调 screen_source / record_approval
    "tests/test_fs_capability_closure.py",
    # 第 47 轮:批准状态用例改为走唯一的写入者(record_approval)⇒ 它直接调用判定点
    "tests/test_research_mcp.py",
    # Issue #57:research profile 白名单检查器(`tools/research_mcp/profile_check.py`,
    # 上面 `tools/research_mcp/` 那一条已经覆盖它)**自己的用例**。
    # 它是「模型看得见的工具边界」这个不变式的**唯一**钉死处(全局层不得有会向模型注册工具的条目、
    # research Preset 要逐字段等于模板、日常 Desktop profile 判出的问题数要与文档一致)
    # ——改断言 = 削弱检出能力。
    # 它同时在 `tests/guard_roster.json` 的逐条点名名单里(不许被整份/逐条跳过)。
    "tests/test_research_profile_check.py",
    # Issue #29 S1:候选独立进程边界的动态探针与等价性——它们钉死的是「候选拿不到什么」
    # (A1 导入、A2 路径与凭据、A3 写不外溢、A4 新旧等价、A5 丢弃自报指标)与开关只来自配置文件。
    "tests/test_candidate_process_isolation.py",
    "tests/test_candidate_process_runner.py",
    "tests/test_candidate_process_equivalence.py",
    "tests/test_candidate_process_integration.py",
    # 候选声明的 AST 解析必须与运行时生效的一致(重复赋值/块内赋值/__init__/类体外改写一律拒绝):钉的是「框架读到的 = 生效的」。
    "tests/test_candidate_spec.py",
    # 候选宿主随父进程消失(POSIX/容器路径):Linux 上真的杀进程验证,别处只看调用点。
    "tests/test_candidate_process_posix.py",
    # 候选宿主里的最小运行期围栏(拒起进程/联网/写 cwd 之外):不依赖 talib,研究侧 CI 真跑。
    "tests/test_candidate_fence.py",
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
    # 注意:walk_forward_optimizer.py **不在**这里——它提供门槛评估用的窗口与
    # 数据文件(见 NEVER_DOWNGRADE 里的说明)。
    "scripts/optimize.py",
    "scripts/optimize_15m.py",
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

# 说明:`docs/` **不是**研究目录——只有 `docs/adr/` 与 `docs/plans/` 是。
# 所以 `docs/web/`(状态面板的前端与契约)不需要「例外」机制:它本来就落到默认拒绝,
# 即执行域。这里显式列出它,只是为了报告里能说得出**为什么**是执行域。

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
    "docs/web/",                # 状态面板的前端与契约(面板能反映交易实况)
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
    """统一成仓库内相对路径、正斜杠、无前导 ./。

    **必须解析 `..`**:否则 `data/research/../risk/live_admission.py` 会按字面
    匹配到 `data/research/` 前缀而被判成**研究域**——一条绕过所有闸门的路。
    git 不会产出这种路径,但分档器是命令行工具,路径由人给。
    解析后若**逃出仓库**,一律判执行域(默认拒绝)。
    """
    p = path.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return p
    # posixpath.normpath 会消掉 `..` 与重复斜杠
    p = posixpath.normpath(p)
    if p == ".." or p.startswith("../"):
        # 逃出仓库:交回 classify() 的默认拒绝分支
        return p
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


def _safe_git_env() -> dict[str, str]:
    """给 git 用的环境:**关掉一切能执行外部命令的配置**。

    第 26 轮 Critical 1(ADR `docs/adr/20261113-evolution-engine.md`):
    `git status` 会**应用 filter** ⇒ `.git/config` 里 `[filter "x"] clean = <命令>`
    那条命令由 **git 自己去执行** ⇒ 守卫自己变成了**执行面**
    (实测:候选只要写下一条 filter 命令,守卫跑的那次 `git status` 就替它执行了)。

    ⇒ 这里按那条 ADR 的**第 2 条硬规矩**办:
    系统级与全局级配置**全部指空** ⇒ 只有仓库自己的 `.git/config`
    还能定义 filter ⇒ 而仓库里那份**正是候选可写的**
    ⇒ 它的 filter 名字逐个**置空**(见 `_git_safe_args`)。
    """
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    return env


def _repo_filter_names() -> list[str]:
    """当前配置里定义了哪些 **filter 名字**(那些名字背后可能有可执行命令)。

    用 `git config --get-regexp` 读(它会跟随 `include.path`)⇒ 不漏
    「include 进来的那份配置」里的 filter。`git config` **只读配置**,
    不读工作区文件 ⇒ 不应用 filter ⇒ 自身**不会**变成执行面。
    读不到就返回空列表(拿不到 ≠ 有 ⇒ 宁可少中和,也不静悄悄改口径:
    真正兜底的是 `_safe_git_env` 把全局/系统配置指空)。
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(ROOT), "config", "--get-regexp",
             r"^filter\..+\.(clean|smudge|process)$"],
            capture_output=True, text=True, env=_safe_git_env(), check=False,
        )
    except OSError:
        return []
    names: list[str] = []
    for line in res.stdout.splitlines():
        match = re.match(r"^filter\.(.+)\.(clean|smudge|process)\s", line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _git_safe_args() -> list[str]:
    """每次调 git 都要带的那几项「不许执行外部命令」的开关。

    - `core.fsmonitor=false`:`fsmonitor` 本身就是**一条命令**;
    - `core.pager=cat`:不让 pager 插手;
    - 每个 filter 名字:`clean`/`smudge`/`process` **全部置空**
      ⇒ 即使 `.git/config` 里写着命令,**也没有命令可执行**;
      `required=false` ⇒ 属性声明成必需时也不至于报错中断。
    """
    args = ["-c", "core.fsmonitor=false", "-c", "core.pager=cat"]
    for name in _repo_filter_names():
        args += [
            "-c", f"filter.{name}.clean=",
            "-c", f"filter.{name}.smudge=",
            "-c", f"filter.{name}.process=",
            "-c", f"filter.{name}.required=false",
        ]
    return args


def git_changed_paths(base: str) -> list[str]:
    """列出相对 base 的改动文件(含未跟踪)。失败抛 RuntimeError(fail-closed)。

    **不许让 git 执行任何外部命令**(第 55 轮之二十五,codex 第 2 轮 M4):
    `git status` 会应用 filter ⇒ 候选写一条 filter 命令,守卫就替它执行
    ⇒ 与本仓库第 26 轮 Critical 1 的既有 ADR 结论**直接冲突**
    ⇒ 现在按那条结论办:环境关掉全局/系统配置 + 逐个 filter **置空**
    (实测:置空后 `git status` 照样正常列出改动,但**标记文件没有被写出来**)。
    """
    safe = _git_safe_args()
    env = _safe_git_env()
    try:
        out = subprocess.run(
            # `--no-ext-diff`:外部 diff 驱动也是**一条命令**。
            # `base...HEAD` 是**提交对提交**的比对 ⇒ 只读对象库 ⇒ 不碰工作区。
            ["git", "-C", str(ROOT), *safe, "diff", "--no-ext-diff",
             "--name-only", f"{base}...HEAD"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.splitlines()
        status = subprocess.run(
            # `-uall`:不带它,git 会把**未跟踪的整个目录**折叠成一行 `?? newpkg/`,
            # 目录里的文件就看不见了 —— 新建目录里的文件会绕过所有扫描。
            ["git", "-C", str(ROOT), *safe, "status", "--porcelain", "-uall"],
            capture_output=True, text=True, check=True, env=env,
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
    _reconfigure_stdio()
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
        if not paths:
            # **本次没有改动** ≠ 环境不可用。
            # 这两者共用退出码 2 会让 CI 的 fail-closed 判定误伤:push 到 main 时
            # base==HEAD、diff 为空、工作树干净,就是这一支——
            # 那时若也返回 2,CI 会永久变红(codex 第 2 轮的 Critical-1)。
            # 「没有改动」不是失败:返回 0,并说明白。
            #
            # **必须也打印「总判定:」这一行**:CI 的冒烟关卡要求输出里有结论行
            # (否则「分档器崩了」与「分档器说这是执行域」在 CI 眼里一样——
            #  见 ADR 第 4 轮 S-1)。push 到 main 时 base==HEAD、diff 为空,
            # 走的正是这一支——它的输出也必须有结论行,否则关卡会误判成「崩溃」。
            print(f"相对 {args.diff!r} 没有改动(也没有未跟踪文件):无待判定的文件。")
            print("总判定: 无改动(无需分档)")
            return 0
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


def _cli(argv: list[str] | None = None) -> int:
    """CLI 入口:**任何未预期的异常都返回 2(环境不可用),而不是 1。**

    为什么:Sourcery 复核抓到的一个 fail-open 口子——Python 遇到未捕获异常时
    退出码是 **1**,而 1 在本工具的语义里是「判为执行域」(正常结果)。
    于是「分档器自己崩了」与「分档器说这是执行域」在 CI 眼里**完全一样**,
    崩了也照样放行。这里把崩溃归到 2(fail-closed),语义才自洽:
    **退出码 1 = 我确实判出了执行域;退出码 2 = 我没能给出判断。**
    """
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 2
    except Exception as exc:  # noqa: BLE001 — 这里就是要兜住一切
        print(f"分档器异常退出(fail-closed): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
