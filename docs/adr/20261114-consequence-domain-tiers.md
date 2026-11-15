# ADR: 按后果域分档——研究档与执行档

- 状态:已采纳(2026-11-14)
- 关联:Issue [#35](https://github.com/laiyinyizao007/invest/issues/35)
- 相关:`docs/adr/20261111-discipline-enforcement.md`(纪律机制补缺)、`docs/adr/20261112-research-sandbox-groundwork.md`、`docs/adr/20261113-evolution-engine.md`(不可变宪法)

## 背景

全局 `~/.claude/CLAUDE.md` 的流程闸门按**改动规模**定档:改动 >10 行或跨多个文件 → 建 Issue、
开 worktree、生成 plan.md、双轮 codex 复核、代码与文档同一原子提交。

这套规格对**生产代码**是对的:改 `trading/runtime.py` 和改一个回测脚本,行数一样,
但坏掉的后果差着几个数量级。**规模不是风险的代理指标**。

策略研究恰恰撞在这条缝里。研究改动(写候选、调参、跑回测、出诊断报告)的规模与生产代码
一样,却付了同一份流程税;而它的产物**可以重跑、失败只影响结论、不动钱**。
用户提的原话是「做策略研究的时候为什么不是实盘也要这么多批准」——这个成本是真实的。

## 决策

**按「改完最坏能出什么事」分档,而不是按改动规模。**

### 1. 两档

| 档位 | 判据 |
|---|---|
| **研究域** | 只影响研究产物(候选代码、回测结果、诊断、报告、编排) |
| **执行域** | 可能影响真实委托/持仓/资金、风控底线、实盘准入批准链、凭据或部署 |

### 2. 判据由代码执行,不靠措辞

新增 `tools/check_domain.py`:按路径清单把每个文件判成 **研究域 / 不可降级 / 执行域**。
退出码 `0` 全研究域 / `1` 含执行域 / `2` 环境不可用(fail-closed:判不了等于不放行)。

**默认拒绝**:不在研究白名单里的路径一律判执行域。把新文件放进研究域必须显式改
`tools/check_domain.py`——那是需要 codex 复核的代码改动,不是随手一句话。

清单按**能力**编,不按目录划。判据一句话:**这个文件的内容(或它的缺席)会不会改变一道
真钱链路上的判定/授权/隔离/风控的结果?** 会 → 不可降级。

### 3. 实测:哪些文件真的在真钱链路上

清单不是拍脑袋写的,是**实测**出来的。做法:用 AST 算出仓库里每个文件
「自己定义或直接调用了哪些判定点」(`tools/verdict_graph.py`),
判定点 = `check_gate`(晋级门槛)、`check_live_admission`(实盘准入)、
`write_approval`(批准文件写出)、`record_approval`(批准账本写入)、
`screen_source`(静态筛查)、`check_instance_state`(风控底线运行期核对)、
`check_class`(候选类结构核对)。

实测结果推翻了几处「看起来是研究代码」的直觉:

| 文件 | 名字/位置像 | 实测它拥有 |
|---|---|---|
| `scripts/research_sandbox.py` | 研究沙盒 | **批准账本的唯一写入者** + 静态筛查 |
| `scripts/research_api.py` | 研究 API | 批准入口 + 筛查 + 类核对 |
| `scripts/research_approval_server.py` | 批准页 | 账本写入 + 筛查 |
| `scripts/research_gateway.py` | 网关 | 筛查(模型可见通道) |
| `risk/candidate_eval.py` | 评估聚合 | **直接调 `check_gate`** |
| `scripts/backtest_lib.py` | 回测引擎 | 回测跑完**核对风控底线** |
| `scripts/record_promotion.py` | 记录批准 | 调 `write_approval`(内部 `check_gate`) |

而**数据流**上的耦合 AST 看不见,必须靠人工清单补:
`risk/dsr.py`(门槛第 9 条 DSR 的计算)、`risk/plateau.py`(第 8 条 `on_plateau`)、
`risk/trial_ledger.py`(第 9 条的试验次数)、`scripts/research_grid.py`(算出的
`on_plateau` 进门槛)、`scripts/segment_windows.py` / `segment_ledger.py`
(测试段边界与「只看一次」登记)。

**两层都要有:图找得到的归图,图找不到的归清单。**

### 4. 研究域省什么、保留什么

省的是**审批仪式**,不是**质量闸门**:

| 环节 | 研究域 |
|---|---|
| P2 plan.md + plan-down | 跳过 |
| 结构化确认块 + 6/6 完备性清单 | 跳过 |
| Issue + worktree | 不强制 |
| G8 双轮 codex | **第一轮照做**;第二轮仅在碰 `risk/` 判定逻辑、沙盒代码或依赖变更时补回 |
| G1 文档原子提交 | 降级:CHANGELOG 记一笔;PROJECTWIKI 只在模块职责/接口变化时更新 |
| G9 覆盖率 | 沿用最低 70% |
| G14 副作用登记 | 只登记越出研究域的部分 |
| ADR 记录 | **保留** |
| 相关既有测试 | **照跑** |

### 5. 两档都不适用的规则(防的是「错」,不是「贵」)

G3 意图边界、G5 安全合规、G7 脱敏、G4 一致性、G8「两轮都做」、G12 学习闭环,
以及**代码内的全部实盘防线**(不可变宪法、`live_admission`、`promotion_gate`、
账户风控、沙盒静态筛查)。CI 闸门对所有 PR 生效,与分档无关。

**本决策不削弱任何一道代码内的防线,也不改它们的阈值。**

### 6. 升档规则

默认拒绝;不可降级 > 研究白名单 > 默认拒绝;清单查不到 → 执行域;中途越界立刻升档;
意图不改变档位;清单本身是代码(增删条目 = 执行域改动)。

## 为什么不直接改全局 CLAUDE.md

因为分档是**本仓库特有的**:别的仓库没有「不可变宪法 + 实盘准入 + 批准账本 + 测试段登记」
这套结构,「研究域」在这里有精确含义,在那里没有。而且全局文件是跨项目共享的,
不该被单个仓库的特例污染。

Claude Code 加载用户级规则在前、项目级在后,**两者冲突时都可能采纳,没有硬优先级**
(官方文档明确说明)。所以本仓库的 CLAUDE.md 不写「覆盖全局」,而是**逐条列出哪些全局规则
按哪一档执行**——不列出的部分沿用全局。这比赌优先级更可靠。

## 已知局限

1. **路径是后果域的代理指标,不是它本身。** 一个研究文件若被交易运行时 import,
   放行它就等于放行一条真钱链路。这条由 `tests/test_check_domain.py` 的
   **动态探针**兜住:测试真的算出生产入口的 import 闭包,断言它与研究白名单**不相交**。
   依赖图不会随仓库演进而失准,手写清单会。

2. **判定点分析认得出调用关系与「直接数据流」,认不出更绕的耦合。**
   - **调用关系**:文件自己定义或直接调用判定点 → `owned_sinks` 认得。
   - **直接数据流**:拥有判定点的文件从某个 import 进来的模块上**读属性**
     (如 `research_api.py` 读 `wf.DATA_FILE`)→ `all_gate_input_providers` 认得。
     这一支是第 1 轮 codex 的 H1 逼出来的。
   - **认不出的**:值经**第三个**模块中转才流进判定点(被拥有判定点的文件不直接读它);
     以及 `risk/dsr.py` / `risk/plateau.py` / `risk/trial_ledger.py` 这类
     **纯函数**——它们不调判定点,输出却是门槛第 9/8 条的输入,靠人工清单兜住。
   缓解:人工清单里逐条写了理由;`CLAUDE.md` §4 规则 7/8 兜住「文档声明防线」
   与「削弱研究脚本里的防线调用」两类内容判据;G8 第一轮 codex 复核是第三道眼力。
   **残余**:新引入的、绕一道中转的数据流耦合,不会被自动发现。

3. **「削弱型改动」这条规则只能部分机械化。** 路径判据抓得住「改了防线文件」,
   抓不住「在一个研究文件里悄悄放宽某项检查」。例如在 `risk/diagnosis.py` 里把某个
   阈值调松,路径上它仍是研究域。
   缓解:G8 第一轮 codex 复核照做(这是唯一能发现语义削弱的一关),
   且 §4 规定碰 `risk/` 判定逻辑时必须补回第二轮。
   **残余**:一个刻意改语义、且第一轮 codex 没看出来的削弱,仍可能漏过。

4. **文档主题升档规则靠人判断。** 分档器只认路径,不认文档写了什么。ADR/方案文档判研究域,
   但如果某次文档改动**声明或修改了一条防线的口径**,必须由人或代理识别出来并升档
   （CLAUDE.md §4 规则 7）。这是内容判据,机器判不了。
   这条不是装饰:本仓库已经吃过一次亏——ADR 里写着「已加固」,代码里其实没接线,
   外部审查便报「无问题」。所以规则 7 明确写了「只改文档不改代码 = 升档」。

5. **清单会过时。** 新的研究文件要进白名单必须改 `tools/check_domain.py`。
   这有一道好处(改清单 = 执行域改动 = 要复核),也有一个坏处(新增研究文件多一步手续)。
   动态探针会持续检查白名单里的**单文件**是否真实存在,防止清单里躺着已删除的文件——
   否则新建同名文件会自动获得研究域待遇。
   **目录前缀**(`RESEARCH_DIRS`)不做存在性校验:其中 `pending_strategies/`、
   `data/research/`、`data/research-control/`、`data/results/` 是 **gitignore 的
   运行时目录**,在干净的 worktree 里本来就不存在(已实测确认),校验它们会误报。
   代价:一个已删除的目录前缀会静默留着,直到有人往里放文件——但那属于
   「新建研究文件」,按上面那条本来就要改清单。

6. **本决策只管流程,不管代码。** 它减少的是改动的**审批成本**,不是缺陷的**检出能力**。
   代码内的防线与 CI 闸门一个都没动。

## 实现状态

- `tools/check_domain.py`:仅标准库,不 import bootstrap(不装交易环境也能跑)。
  打印前把 stdout/stderr 重配成 utf-8 + `errors="replace"`——Windows 控制台默认
  cp1252,不改会直接抛 `UnicodeEncodeError`(codex 第 1 轮的 M5)。
- `tools/verdict_graph.py`:判定点的**函数级** AST 分析(纯标准库)。
  判据是**直接归属**(自己定义或直接调用),不是传递可达——
  传递可达会把助手的判定算到调用者头上(误伤),而**误伤的探针等于没有探针**。
  另有一支**数据流**分析(见下「已知局限」第 2 条)。
- `tests/test_check_domain.py`:**90 条**,含双向探针(放行 + 拦截)、
  全仓不变量(调用关系 + 数据流两支)、探针自身的准确性夹具。
- `CLAUDE.md`:本仓库的分档规则与两档流程对照表。
- CI:`research-guard.yml` 的 ruff 清单与测试清单**已**纳入这三个新文件;
  另加一步**分档器冒烟**(用 `PYTHONIOENCODING=cp1252` 跑真实 CLI,验退出码语义)
  ——只有单元测试的话,CLI 那条路径(--diff 读 git、打印中文、按退出码表态)
  在 CI 上从没被跑过(codex 第 1 轮的 M4)。

### 探针的变异验证

探针本身会被证伪,否则它只是一段好看的断言。已做的变异(每次都被抓住,
条数由 `pytest` 的 FAILED 行数实测,不是估的):

| 变异 | 变红的测试条数 |
|---|---|
| 删掉 `trading/` 的不可降级条目 | 1 |
| 把 `risk/live_admission.py` 挪进研究白名单 | 5 |
| 把 `risk/candidate_eval.py` 挪进研究白名单 | 3 |
| 把 `scripts/research_sandbox.py` 移出不可降级 | 4 |
| 把 `scripts/backtest_lib.py` 移出不可降级 | 3 |
| 把 `scripts/walk_forward_optimizer.py` 移出不可降级 | 2 |
| 把整个 `scripts/` 放进研究白名单 | 6 |
| 把 `walk_forward_optimizer.py` 挪进研究白名单(H1 类:数据流) | 2 |
| 把 `optimize.py` 挪进研究白名单 | 6 |
| 新建一个直接调 `check_gate` 的文件(不入库) | 1(`test_every_sink_owning_file_is_never_downgrade`) |

最后一条尤其重要:它证明这条不变量对**尚未入库的新文件**也生效
(扫描含 `git status --porcelain -uall` 的未跟踪文件,而不只是 `git ls-files`)。
**`-uall` 不可省**:不带它,git 会把未跟踪的整个目录折叠成一行 `?? newpkg/`,
目录里的文件就看不见了(codex 第 1 轮的 M1)。

## 第 1 轮 codex 复核的发现与处置

本 ADR 与实现经 codex CLI 第 1 轮复核(2026-11-14)。发现 2 条 High、5 条 Medium、
6 条 Low。**逐条独立复现后**的处置:

| 编号 | 问题 | 处置 |
|---|---|---|
| **H1** | `scripts/walk_forward_optimizer.py` 判研究域,但它的 `DATA_FILE` / `build_windows()` 经 `research_api.py:887-890` → `run_gate` → `check_gate` **决定门槛的评估窗口与数据** | 已移入不可降级;并新增**数据流探针**(见下)自动覆盖这一类 |
| **H2** | `verdict_graph` 对 `from X import f as g` 后调 `g(...)` 的形态**漏报** | 已修(裸名字分支查别名表);补了三种调用形态的夹具 |
| **M1** | 未跟踪**目录**里的文件不被扫描(`git status` 缺 `-uall`) | 已改 `-uall`(两处) |
| **M2** | 「生产闭包不相交」那条的模块解析器不认 `scripts/` 裸模块导入,`scripts/bootstrap.py` 漏在闭包外 | 已统一口径;闭包从 17 项变 18 项 |
| **M3** | 文档三处不实(变异条数、白名单存在性校验范围、点名了没有该调用的文件) | 已逐条订正 |
| **M4** | 分档判定在 CI 里没有任何机器强制点 | 已加分档器冒烟关卡 |
| **M5** | Windows cp1252 控制台上 CLI 直接崩 | 已加 stdout/stderr 重配;补了 `PYTHONIOENCODING=cp1252` 的回归用例 |

### H1 引出的能力升级:数据流探针

H1 暴露的是**一整类**耦合:模块**不调用**判定点,但它的**值**被喂进判定点。
只认调用关系永远抓不到这一类。

于是新增 `verdict_graph.all_gate_input_providers`:在**拥有判定点**的文件里,
找出「从 import 进来的模块上读取的属性」(如 `wf.DATA_FILE`),被读到的那些模块
就是判定输入的提供者,必须与判定点同级受保护。

实测结果(生产文件,10 个):`walk_forward_optimizer.py`、`research_api.py`、
`research_sandbox.py`、`research_config.py`、`research_console.py`、
`research_gateway.py`、`segment_windows.py`、`segment_ledger.py`、
`constitution.py`、`bootstrap.py`、`dashboard/safe.py`。
新增不变量 `test_every_gate_input_provider_is_never_downgrade` 自动覆盖这一类——
**不再依赖人手写一条断言去钉某个文件**。

**这一支仍然有边界**:它只看「拥有判定点的文件」读到的属性。
若某个值经**第三个**模块中转才流进判定点(而不是被拥有判定点的文件直接读到),
这一支看不见。实测 `research_api.py` 是直接读 `wf.DATA_FILE` 的,所以抓到了。

