# ADR: Bisheng 策略进化引擎(分阶段,试验记账与 DSR 先行)

- 状态:已采纳(2026-11-13 用户确认,DSR 阈值 0.95 自此锁定)
- 日期:2026-11-13
- 跟踪:Issue [#15](https://github.com/laiyinyizao007/invest/issues/15)(阶段1),总 Issue [#1](https://github.com/laiyinyizao007/invest/issues/1)
- 关联:`20261109-promotion-gate`、`20261111-risk-floor-immutability`、`20261110-bisheng-ai-researcher`、`20261112-research-sandbox-config-and-approval`

## 背景
目标是用 Bisheng 搭"生成 → 沙盒回测 → 评估 → 变异"的闭环,自动寻找策略。勘察结论:网关、sha256 人工审批、迭代上限、
晋级门槛 8 项检查、select/validate 分窗、参数平原、容器隔离**均已存在**,不重建。缺口只有:

1. 试验次数记账与 DSR(进化会放大多重检验;此前 `methodology-mapping` #20/#33 以"31 组网格惩罚无统计意义"为由留作规划位,进化引擎使该前提不再成立)
2. 机器可校验的不可变参数清单
3. 父子谱系、精英库、变异反馈
4. 可提交入库的 flow 定义,以及门槛通过到 `record_promotion` 的 metrics 衔接

## 决策
### 分期
| 阶段 | 内容 |
|---|---|
| 1 | `risk/trial_ledger.py` + `risk/dsr.py`,晋级门槛新增第 9 条 DSR |
| 2 | `config/immutable_constitution.json` + 沙盒 AST 筛查拒绝覆盖 |
| 3 | `risk/lineage.py` 谱系与精英库,网关只读 `/elite`,`/feedback` 附结构化诊断 |
| 4 | `tools/bisheng/build_v4.py`、flow JSON 入库、`scripts/make_promotion_metrics.py` |

### 试验记账规则
- 每个 `param_grid` 格子计 1 次试验;每次候选提交计 1 次试验;按谱系家族累计,**只增不减**。
- 账本追加写入 `data/research/trials.jsonl`;损坏即 fail-closed(拒绝评估),与 submissions 账本一致。
- 阶段 1 的实现口径(落地时确定):
  - **全局累计**,不分家族;按家族划分随阶段 3 的谱系再引入(全局累计更保守,不会少记)。
  - 记账点(**先记账再跑,失败不退**,codex 复核后改为此口径):网关 `/candidates` 成功提交计 1;`evaluate` 启动子进程前按最小网格
    预扣 27(3 个参数 × 每个 ≥3 个取值),子进程回报的格子数更大时补记差额,超时/崩溃/策略报错也已预扣;`gate` 启动前记 1 次 `gate_run`
    并以新的累计数作为 DSR 的 N。代价:声明有误的网格(如 GridError)也会扣 27,属于保守方向。
  - 账本上限:单条 count ≤ 10⁹,累计 ≤ 10¹²,超限按损坏处理——否则 `1-1/N` 舍入为 1.0 会让 DSR 永久抛错。
  - 非有限值一律 fail-closed:`deflated_sharpe` 对 NaN/inf/溢出返回 0.0;`check_gate` 要求 `dsr` 为 [0,1] 内的有限数、`n_trials` 为 ≥1 的 int(拒绝 bool/float/NaN)。
  - 账本由可信的父进程读写,评估子进程只收到 `--prior-trials`;同进程内用锁串行,**跨进程不加锁**——并发 CLI 可能漏记极少量(偏向少记),列入已知限制。
  - 父进程对子进程回报的 `grid_cells` 只做下限加固(≥ 3³ = 27,合法网格的最小值)。这**不能**防伪造整份结果:候选代码本就在子进程内执行,
    能伪造格子数的候选同样能伪造 DSR 本身;该风险由「人读过代码才批准」和既有威胁模型(`research_sandbox.py` 文件头:纵深防御而非沙盒)承担。
  - 网关用 `_busy` 锁串行执行作业,网关内不会有两个 evaluate 读到同一个 `prior`;CLI 与网关并发才可能,列入已知限制。
  - DSR 的 V 取 `1/(T-1)`(零假设下夏普估计量方差),T = 样本外拼接日收益个数;N 取累计试验数(含本次格子)。
- 被淘汰的候选同样计数——幸存者偏差正是 DSR 要惩罚的对象。

### DSR 门槛(在此锁定,看到任何候选之前)
- **第 9 条:DSR ≥ 0.95**(Bailey & López de Prado 2014;以家族累计试验数 N 去通胀)。
- 一旦本 ADR 转"已采纳",该阈值**不得为让候选通过而放宽**;调整须先新 ADR,且只能在不查看新候选结果的前提下进行。
- 夏普估计方差取自样本外拼接日收益的偏度/峰度修正式,不另设经验系数。

### PBO 延后
- 理由:单品种(玉米)、3 个滚动窗口,且 `risk-floor-immutability` §2 已排除 CPCV;在该数据上 CSCV 组合数过少,PBO 估计噪声大于信号。
- 重启触发条件(任一):接入第二个品种;滚动窗口 ≥ 8;出现新的未消耗测试段。重启须新 ADR。

### 沿用的硬约束
- 风控底线不参与优化,进化只允许重写白名单里的成员:`entry_signal`、`param_grid`、`parameters` 与可搜索参数(`fast_window`/`slow_window`/`atr_mult`/`take_profit_r`)。白名单以 `BUILTIN_ALLOWED_OVERRIDES` 为准。
- 迭代上限(5 次 / 24 小时)、`max_source_bytes`、网关路由白名单不放宽。
- 流程内任何节点不得写 `approved/`、`promotion_approval.json` 或实盘配置;上线仍须"人工批准 → `record_promotion.py` → SimNow → 另行授权实盘"。
- 测试段(2024-01-01 起)已消耗一次,不得用于选择或进化反馈。

## 阶段 3 修订:反馈按信息来源分层(用户 2026-11-13 同意),并吸收状态页会话的裁决

背景:原计划把精英档案/父代码回给 LLM,与『硬记忆预算』冲突。问题的本质不是反馈条数,而是反馈携带多少**验证窗口**的信息:
验证窗口一旦被 LLM 反复看到,就变成训练集;DSR 只惩罚试验次数,不惩罚每次试验带回的信息量;测试段已消耗一次,目前**没有干净的留出集**。

| 信息来源 | 给 LLM / 种群选择用什么 |
|---|---|
| 选参窗口(2020-07 ~ 2022-06) | 放开:结构化指标、热力图、谱系、诊断。在这里过拟合不影响闸门 |
| 滚动验证窗口(闸门) | **梯子式量化**(`risk/ladder.py`,阶段 3):仅当超过历史最佳至少一个步长才汇报,汇报值取整到步长,否则重复上次值;另设总查询预算。状态(最佳值、已用查询数)持久化,损坏即 fail-closed |
| 最终留出(新数据 / SimNow 前向) | 永远不进入进化回路,只给人看,最终确认一次 |

与状态页会话的裁决(`20261113-evolution-dashboard.md`「变异输入与适应度」)对齐的部分,**采纳其更严格的一侧**:
- 反思节点只输出程序校验过的结构化字段(枚举 + 0–1 数值),自由文本只存档给人看,不进变异提示词(不再设「评论者 LLM 写自由文本」);
- 父代码不原样回灌,只回灌程序从代码抽取的可验证描述(用到的指标、入场条件个数、参数所在网格单元);
- 种群用简化 MAP-Elites 分格(持仓周期、交易频率等),格子从小做起;加权只用于同格内选父代;
- 变异输入仍受『最近 3 条结构化 Trace』约束;`GET /elite` 不做。

台账新增字段(名称已定,对方读取侧照此实现):`cell`(所在格子,字符串)、`diagnosis`(对象,由程序产出;内部枚举在阶段 3 定稿后通知)。

**阶段 3 范围追加(状态页会话提出,2026-11-13,待阶段 3 开工时实现):** 评估时另存 `data/research/heatmaps/<run_id>.json`,内容 `axes`、`scores`
(`[i,j,k,夏普|"-inf"]`,无效格用字符串 `"-inf"`,不写 JSON 的 `-Infinity`)、`best_cell`、`on_plateau`。得分表**不并入**网关给外层的响应(现状:`run_evaluation` 只保留摘要,
这条保持)。数据来自选参窗口,面板只读该文件。

**用户裁决(2026-11-13;状态页会话先转述,用户随后在本会话确认,来源见 `20261113-evolution-dashboard.md` 第 1 条):**
PBO **不**作晋级硬条件(延后,与本 ADR「PBO 延后」一致);DSR **不**用于选父代,选父代只用选参窗口适应度;`dsr`、`n_trials` 仍记录并展示给人看,不参与选择。
重评条件:多品种复权连续合约到位并能给出 PBO 置信区间,或出现从不进回路的独立留出集。
**代价(须如实承认):** 选参窗口内的过拟合只能靠质量多样性、结构化反馈和既有门槛(含参数高原)约束,这一层没有多重检验校正。
下面第 1、2 项**已确认**(PBO 延后、DSR 不选父代);第 3 项(出处)已在上一节核实。阶段 3 的前置因此解除,但仍待:梯子步长的定法(见上节更正)、DSR 的 V 改经验方差。

**原待确认项(留档):**
1. **「PBO 阈值作晋级硬条件」与本 ADR 的 PBO 延后冲突。** 当前复权 15 分钟连续数据只有玉米一个品种、3 个滚动窗口,PBO 估计噪声大于信号(见上文「PBO 延后」)。
   若要保留该条件,前置是扩数据:其他 DCE 品种有原始分钟线(`DCE_大连商品交易所/<品种>/00_主力连续`),但**未生成复权连续合约**,跨度未核实;需另立 ADR 并先做数据构建。
2. **适应度的来源。** 「用 DSR 打折的夏普做适应度」中的 DSR 来自验证窗口,若直接用于选父代,验证信息会经由选择回到种群,抵消梯子的保护。
   候选做法:选父代用选参窗口的适应度;验证窗口的结果只作闸门,或只以梯子量化后的值参与。
3. **方法出处未联网核实。** 梯子(Blum & Hardt 2015)、Thresholdout(Dwork 等 2015)、MAP-Elites(Mouret & Clune 2015)、FunSearch / AlphaEvolve 的评估器打分均来自记忆,落地前须核对原文再定细节。

## 出处与数据核实(2026-11-13,上节「待确认」第 3 项的结果)

**论文(读了 arXiv 全文或 ar5iv 版,非摘要):**
| 方法 | 核实结果 | 对本项目的含义 |
|---|---|---|
| Ladder(Blum & Hardt 2015,arXiv 1502.04585) | 仅当新成绩比历史最佳好出至少步长 η 才更新,更新值取整到 η 的倍数,否则重复上次值;Theorem 3.1:误差随提交次数 k 只按对数增长。另有无参变体:用配对差的标准差 s 做阈值 s/√n | 保证针对 **i.i.d. 样本上的损失**。我们是自相关的日收益序列、样本量只有约 1.5 年,保证不能直接套用,只能当设计依据 |
| Thresholdout(Dwork 等,arXiv **1506.02629**《Generalization in Adaptive Data Analysis and Holdout Reuse》) | 训练集与留出集的估计差超过带噪阈值才用留出集作答(加 Laplace 噪声),留出集作答次数有预算 B,用尽即停止回答 | 之前我误记在 1411.2664 里,那篇摘要没有 Thresholdout。同样是 i.i.d. 假设 |
| MAP-Elites(Mouret & Clune 2015,arXiv 1504.04909) | 格子粒度可先粗后细;新个体仅在格子为空或优于占位者时入格;**父代从已占格子中均匀随机选** | 状态页 ADR 的「加权只用于同格内选父代」是对原算法的有意偏离,不是原文做法 |
| AlphaEvolve(arXiv 2506.13131) | 提示里**确实包含**历史程序及其评分;程序库受 MAP-Elites 与岛屿模型启发;代码改动用 SEARCH/REPLACE 差分块 | 它的评估器是对固定问题的确定性函数,没有「留出集会被适应」的问题,**不能当作**把含噪验证指标回灌 LLM 的先例 |
| FunSearch(Nature 2023) | **未核实** | — |

**对前文建议的更正:**
- 我之前提的梯子步长 0.1 只是占位,量级不对。夏普的标准误在 3 年样本上约 0.84,验证窗口约 1.5 年则更大(≈1.2),步长应与标准误同量级;阶段 3 实现前须按配对日盈亏差的标准差定步长(参考无参变体),并在看到任何候选之前登记。
- 与 Ladder 不同,我们是「越大越好」的夏普,更新条件要取反(新成绩比历史最佳高出至少 η)。

**DSR 公式(阶段 1 已实现)核对:** 与多个公开资料一致——SR0 = √V·[(1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(Ne))],DSR = Φ((SR−SR0)·√(T−1)/√(1−skew·SR+(kurt−1)/4·SR²)),夏普为**非年化**逐期值,峰度为**原始**峰度(正态=3);
N=100 时括号项约 2.53,与实现一致。核对对象是二手资料,未读 Bailey & López de Prado 2014 原文。
**一处偏离:** 公开资料里 V 是「N 次试验各自夏普的方差」(经验值);阶段 1 用了零假设下的理论值 1/(T−1)。真实试验的夏普分散度通常更大,所以实现比原文**偏宽松**。
改进方向:账本在记次数之外同时记录每次试验的夏普,用经验方差。这需要新 ADR 或在阶段 3 一并做,**阈值不得因此放宽**,只能更严。

**数据(本机核实):**
| 数据 | 事实 |
|---|---|
| 复权 15 分钟连续合约 | 仅玉米:2020-07-22 ~ 2026-06-15,32540 根(`derived/C_15m_adj.csv`) |
| 原始 1 分钟主力连续(`KQ.m`,未复权) | 2016-01-04/05 起,至 2026-06-12,品种多(C、PP、L、M、A、J、I、V 等,EB 自 2019-09-26 起);价格在换月处拼接,不能直接用 |
| 逐合约 1 分钟(`月份连续/`) | 玉米自 2020-01 起;现有构建方法依赖逐合约数据,所以复权序列只能到 2020 年 |
| 因此 | 2016–2020 的玉米没有逐合约数据,复权只能从 `KQ.m` 自身推断换月点,属近似;其他品种逐合约数据是否同为 2020 起,**只核实了玉米** |

结论:换品种做留出集可行(PP、L、M 等有 10 年原始分钟线,逐合约数据若同为 2020 起则复权后约 5.9 年,与玉米同量级),但需要先为该品种构建复权连续合约;
这是 PBO 重启(第二个品种)的前置工作,仍须单独立项和 ADR。

## 阶段 3 前置:DSR 的 V 与梯子步长 η 的定法(登记于**任何候选生成之前**)

这两个量和晋级阈值 0.95 是同一类东西:**看到结果之前定死的量**。看到候选之后再改它们,性质上等于放宽阈值。
因此本节的登记必须在阶段 3 产出第一份候选之前完成。

### DSR 的方差 V:改为经验方差,且只能更严
- **公式**:`V = max(V_经验, 1/(T-1))`,其中 `V_经验` 是各次试验夏普估计量的样本方差(与理论值同一口径:非年化逐期夏普、同一 OOS 窗口、同一指标定义)。
- **取 max 而不是直接替换**:这次改动只可能让 DSR 更严,不会更松。这样"阈值不得放宽"从口头承诺变成机制——以后也**没人能靠挑一个更松的 V 来放水**。
- **样本不足时退回理论值**:试验数 < 20 时 `V_经验` 不可靠,退回 `1/(T-1)`,并把"样本不足"这一事实本身记录下来(不得静默)。
- **落地**:账本(`risk/trial_ledger.py`)在记次数之外同时记录每次试验的夏普。这与阶段 3 的谱系账本是同一份数据,一起做。
- **对搜索无影响**:DSR 不用于选父代(见上),所以 V 变严只会让"晋级"更难,不会扭曲搜索过程本身。

### 梯子步长 η:按配对日盈亏差的标准差定
- **方法**:参考 Blum & Hardt 无参变体,用**配对差**的标准差估计噪声尺度——两条候选在同一验证窗口上的逐日盈亏之差,标准差比各自标准差小得多(共同的市场波动互相抵消)。取 `η ≈ 1.5 × SE(配对差)`,并设下限 **0.25 夏普**,避免在异常安静的窗口里算出 0.03 这种没意义的步长。
- **为什么不用 0.1**:3 年样本的夏普标准误约 0.84,1.5 年验证窗口约 1.2;步长 0.1 只有噪声的约 1/12,梯子会不停汇报,等于没有。
- **数值何时算出来**:需要 CADJ/MAADJ 的复权连续日线序列。**本开发环境没有该序列**(`~/.vntrader` 下无数据库,`data/` 下只有分钟原始数据,复权日线由 `scripts/build_continuous.py --symbol C --save` 写入 vn.py 数据库,在有数据的环境里构建)。因此 η 的**数值**必须在有该数据的环境里算出,并在生成第一份候选**之前**写进本 ADR;**方法**现在就定死。
- **取整方向**:我们是"越大越好"的夏普,更新条件取反(新成绩比历史最佳高出至少 η 才汇报,汇报值取整到 η 的倍数)。

### 第二个品种:数据准备现在做,PBO 仍然延后
- **数据流水线现在做**:构建第二个品种的复权连续序列是纯数据工程,不涉及策略决策,在看到候选之前做完**不引入偏差**(反而避免后面忙乱时改数据口径)。毕昇模型配额卡着,这是现成的空闲时间。
- **PBO 仍延后**:1 个品种 × 3 个窗口能切出的 CSCV 组合太少,估出来的 PBO 基本是噪声,拿它当判据比不看更糟。重启触发条件见本 ADR 的 PBO 章节。
- **注意**:第二个品种的复权序列同时是上面 η 定量的前置,两件事共用同一份数据工作。

## 封存前向留出集(用户 2026-11-13 确认)
2026-06-15 之后的新数据(`C_15m_adj.csv` 最后一根为 2026-06-15 14:45)声明为**封存前向留出集**:进化回路(选参、验证、反馈、梯子)永远不可见,只在最终确认时由人触发使用一次。
结构性保证已存在:`research_gate.slice_bars` 拒绝任何 ≥ `TEST_SEGMENT_START`(2024-01-01)的 K 线,新数据天然落在该范围之后,评估无法触及。
尚未做(后续项):专门的前向留出登记(一次性使用账本)与最终确认脚本;持续录入新数据由用户负责。

## 阶段 2:不可变宪法(Issue #16)

清单文件 `config/immutable_constitution.json`,内置清单在 `risk/constitution.py`。**清单只能追加,不能删减内置项**;
文件缺失或非法一律 fail-closed(拒绝执行),不做"默认放行"。

三层防线:
| 层 | 位置 | 拦什么 |
|---|---|---|
| 静态(提交时) | `research_sandbox.screen_source` → `constitution.screen_source` | 类体覆盖受保护成员、重写受保护方法、实例上改写受保护属性、改写受保护的风控模块函数、下单/撤单接口、禁用内建函数的**引用**、**框架的"按名 setter"**、受保护参数作为**关键字参数**或字典键出现、**受禁 dunder 的定义与引用**(见下)、自定义 metaclass、类装饰器 |
| 运行时(执行前) | `research_api.import_candidate` → `constitution.check_class` | 候选必须继承模板家族;不得重写白名单以外的**模板成员**——覆盖候选类体、中间基类、类创建后动态打的补丁三类;不得定义受禁 dunder;不得定义按名 setter;不得自定义 metaclass |
| 参数(请求时) | `research_api.check_params_allowed` → `constitution.check_params` | 调用方不得在设置字典里指定受保护参数或成员(此前 `backtest`/`gate` 不校验参数键名),**大小写不敏感**比对;子进程入口(`_child`/`_gate_child`/`_evaluate_child`)自己再查一遍——子入口是独立的信任边界 |

要点:
- **家族而非模板**:要求继承"模板所属家族"(MRO 里最靠近 object 的**应用层**基类),不要求继承 15 分钟模板本身——研究与沙盒也跑日线族。家族根必须排除 vnpy 框架基类与 `abc.ABC`;早先按"祖先名字/模块"猜测会把 `abc.ABC` 当成家族根,连带把模板自身成员误判为"候选重写",导致**每个真实候选都被拒**。
- **静态层不判断"是否重写了模板成员"**:模板成员名单要到运行时才知道,那一层由 `check_class` 负责;静态层只管受保护项与下单接口。候选**新增**成员(新指标、新辅助方法)允许。
- 白名单 `allowed_overrides` 默认只有 `entry_signal`、`param_grid`、`parameters` 与可搜索参数;试图把受保护项加进白名单一律拒绝(不静默过滤,以免掩盖配置错误)。白名单与受保护清单的冲突在**合并后**的集合上校验,冲突即报错。
- `research_grid.FORBIDDEN_AXES` 改为从清单派生,避免两处各写一份而漂移。
- **清单的 `template` 只能取内置白名单内的类**:换成任意可导入的类(如 vnpy 的 `CtaTemplate`)会让继承约束形同虚设;要换模板必须改代码、走代码评审。
- **"只能追加"靠版本控制保证**:`load()` 只与代码里的内置集合合并,察觉不到文件里的删除。清单相对上一次提交只增不减,由 CI 检查(`test_constitution_history_is_append_only`)逐个历史版本比对;**内置**保护项写在代码常量里,那个比对看不见,另由 `test_builtin_protections_are_never_shrunk` 从历史版本里把常量读出来比对(否则把 `BUILTIN_PROTECTED_PARAMETERS` 里的 `risk_budget` 删掉,其它检查一样全绿)。两个检查都需要**完整历史**:CI 必须 `fetch-depth: 0`,浅克隆下检查直接失败而不是跳过——查不了等于没查,不能让它冒充通过。
  - 第 5 轮复核发现这条检查**曾经形同虚设**:提取器只认 `tuple`/`list` 字面量,而 `BUILTIN_PROTECTED_NAMES` 的定义是 `(...) + BUILTIN_PROTECTED_PARAMETERS`(一个 BinOp),于是这个最大的一组受保护成员从来没进过比对——删掉 `on_disconnect` 之后 Constitution Guard 照样全绿(两轮外部复核都独立复现了)。现已改为受控的常量表达式求值(支持字面量、常量之间的 `+`、同文件常量引用),并**fail-closed**:注册表里的常量解析不出来就报错,而不是静默跳过。
  - 内置保护的范围也扩了:除了 `risk/constitution.py` 的七个常量,沙盒层的 `FORBIDDEN_MODULES`/`FORBIDDEN_CALLS`/`FORBIDDEN_ATTRS` 一并纳入历史比对(它们同样挡着模块导入与反射入口)。
  - 历史列表用 `git log --full-history`:默认的历史简化会把"与某个父提交内容相同"的合并提交剪掉(那种提交不比也不会漏内容,但列表更直白)。
  - 清单文件至今只有一个历史版本,此时"平凡通过"而不是 `skip`——CI 输出里的 `1 skipped` 与 pass 长得一样,让人以为检查跑过了。

### 拦"引用"而不是拦"调用"
`f = object.__setattr__` 之后 `f(self, "risk_budget", 1e9)` 的调用点是裸名字,AST 看不见被调的是谁;实测这个写法**真的能击穿风控底线**(`within_budget(100.0, 10.0, inst.risk_budget)` 在改写后返回 True)。因此静态层一律拦**引用**:
- 受禁 dunder(`__setattr__`/`__delattr__`/`__getattribute__`/`__getattr__`/`__init_subclass__`/`__set_name__`)的任何 `ast.Attribute` 引用与裸名字引用;
- 下单/撤单接口(`buy`/`sell`/`send_order`/`cancel_all` …)的任何 `ast.Attribute` 引用与裸名字引用;
- 禁用内建函数(`getattr`/`setattr`/`delattr`/`vars`/`globals`/`locals`/`eval`/`exec`/`compile`/`open`/`__import__`/`input`/`type`)的任何名字引用。

同样地,`operator.attrgetter('__dict__')(self)` 把属性名当字符串用,绕开了"禁止访问 `__dict__`"的属性写法检查——`operator` 模块整体禁用,受禁属性名作为**字符串常量**出现也一律拒绝。

**关于字符串常量的准确口径(第 5 轮更正)**:沙盒层对 `FORBIDDEN_ATTRS`(`__dict__`、`__class__` 等)的检查是**完全相等**匹配,不是子串匹配。所以 `X = "__dict__"` 会被拒,而文档字符串里"提到"某个名字(比如 `"""we use __dict__ here"""`)**不会**被拒——早先这份 ADR 写的是"文档字符串里提到也会被拒",说过头了,以本段为准。

### 框架的"按名 setter":不给"按键名改写"留任何入口

第 5 轮复核实测出的一条活的绕过:`CtaTemplate.update_setting(setting)` 会把字典里的键**按键名** `setattr` 到实例上,
而模板的 `parameters` 列表里正好有四个受保护参数(`risk_budget`/`max_daily_losses`/`atr_window`/`history_vt_symbol`)。
于是候选只要写

```python
self.update_setting(dict(risk_budget=1e12))
```

就能把单笔风险预算从 300 改成 1e12——而这个名字在源码里既不是属性赋值也不是字典键(是 `ast.keyword.arg`),
按语法位置筛查看不见它。实测改之前:静态层返回空列表、运行时类校验返回空列表、候选被接受,
`within_budget(100, 10, 1e12)` 返回 True(真实底线 300 时返回 False)。

处理:
- 新增 `BUILTIN_FORBIDDEN_BY_NAME_SETTERS`(`update_setting`),**任何引用**都拦(不只调用),取别名同样拦;
- 候选自己定义 `update_setting` 也拦(引擎初始化时会调用它),类体里写成赋值(`update_setting = 3`)同样拦;
- 受保护参数作为**关键字参数**出现也拦(`dict(risk_budget=…)`),按大小写不敏感比对,与参数层同一把尺子。

### 第 7 轮补记:另一条同类入口——`__builtins__['setattr']`

第 6 轮复核实测出**同一个后果**的另一条路径,静态层与沙盒层当时都返回空列表:

```python
__builtins__['setattr'](self, 'risk_budget', 1e12)     # 底线 300 → 1e12,within_budget 随之放行
```

`exec(code, module.__dict__)` 会往候选的 globals 里注入一个 `__builtins__` **字典**,
里面就有 `setattr`。此前只有 `x.__builtins__` 这种**属性写法**被拦,裸名与字符串形式都漏了。
处理(在沙盒层,因为它本来就是"禁用内建与反射入口"那一层):
- 受禁属性名(`__builtins__`/`__dict__`/`__class__`/`__mro__` 等)的**裸名**引用一律拒;
- 这些名字以及 `setattr`/`getattr`/`eval`/`exec`/`globals`/`locals`/`vars`/`__import__`/`compile`/`input`
  作为**字符串常量**出现也拒;
- `import builtins` 早就在模块禁用名单里(`FORBIDDEN_MODULES`),不另开口子。

**为什么不把 `open`/`type` 也按字符串拒**:交易数据里 `"status": "open"` 这种字段值很常见,
按字符串一律拒会把正常候选整片拒掉——误报面远大于收益。真正危险的是"按名字改写/反射"这一类。

### 执行时的能力隔离:不给,而不是再禁一批名字

第 7 轮复核(`gpt-5.6-sol`)指出:前面这些**静态**规则封住的是**已知写法**,不是能力本身。
实测两条等价后果的路径,静态层与沙盒层都返回空列表:

```python
e.__traceback__.tb_frame.f_builtins.setattr(self, 'risk_budget', 1e12)   # 经异常回溯拿到内建字典
from unittest import mock; mock.patch.object(self, 'risk_budget', 1e12)  # 能改写任意对象属性的模块
```

继续枚举反射路径是枚举不完的(每加一批名字都要配测试,而且总有下一个)。所以改成**把能力收走**:
候选执行时(`exec(code, module.__dict__)`)显式给一份**最小内建集合**
(`scripts/research_sandbox.py::safe_builtins`):

- `setattr`/`delattr`/`getattr`/`eval`/`exec`/`compile`/`open`/`globals`/`locals`/`vars`/`type`/`object` 等一律**不给**;
- `__import__` 换成**受限导入器**:只放行不在禁用名单里的模块(`builtins`、`unittest.mock` 等已加进名单);
- 其余内建(`len`/`range`/`min`/`max`/`sum`/`sorted`/`isinstance`/`hasattr`/`Exception` 等)照常给。

这样一来:无论候选怎么绕,**它能拿到的内建字典里就没有 `setattr`**——
连 `frame.f_builtins` 也只会拿到这份受限字典(帧的 `f_builtins` 取自模块 globals 的 `__builtins__`)。
`unittest.mock` 那条路则由受限导入器直接关上。

代价:候选用到这些名字会得到 `NameError`/`AttributeError`(而不是静默放行),由人在批准前看出来;
`type(...)` 这种常见写法也会失效。这是本项目一贯的 fail-closed 取舍。

**这不是安全边界的终点**:容器隔离(`docker-compose.research.yml`:无网络、只读挂载、`cap_drop ALL`)
与"运行前由人读过代码"仍然是真正的边界。能力隔离只是把"静态黑名单挡不住的那部分"又收了一层。

### 第 8 轮补记:风控执行点、整条属性路径、回测后的实例状态

第 8 轮(codex CLI 深度分析,**36/50,未通过**;与第 7 轮并行,审查期间工作区被并发修改)
报出两条 **Critical**。两条都是**实测复现**过的(真实 15 分钟玉米数据,不是推测),
而且当时**三层校验全绿**——静态层、沙盒层、运行时类校验全部返回空列表。

#### Critical 1:候选可以**调用**模板的风控执行点

```python
class Evil(TrendAtr15mStrategy):
    def entry_signal(self) -> int:
        signal = super().entry_signal()
        if signal:
            self._send_entry(signal, float(self.atr_value) * 50.0, float(self.am.close[-1]))
        return 0
```

`BUILTIN_PROTECTED_NAMES` 只防**重定义**(`check_class` 比对类字典),静态层只拦三类**引用**
(受禁 dunder / 按名 setter / 下单接口)。受保护的**方法名**不在引用黑名单里,
于是候选借到了模板自己的下单机器——而 `within_budget(dist, self.get_size(), self.risk_budget)`
与 `self.guard.can_open(...)` **都在被绕过的那个函数体内**(`_try_enter`)。
实测(`atr_mult=50`,窗口 2022-07-01~2023-06-30,3000 根 15 分钟 K 线):

| | 成交笔数 | 单笔止损距离 | 被预算拦下 |
|---|---|---|---|
| 诚实模板 | 0 | — | 157 |
| 上面这段候选 | 2 | 508.7 | 0 |

508.7 元 ≈ 17 倍的 300 元单笔预算。

**处理**:新增 `BUILTIN_PROTECTED_ENTRY_POINTS`(`_try_enter`/`_send_entry`/`_manage_position`/
`_place_protective_stop`/`_place_pending_entry`/`_process_daily_bar`/`_ensure_protective_stop`/
`_stop_order_price`/`_complete_stale_daily_bar`),静态层对这些名字的**引用**一律拒——
**不只拦调用**:`f = self._send_entry` 之后的 `f(...)` 调用点已经看不出是谁了。

#### Critical 2:静态层只看赋值的**末段属性名**,不看整条属性路径

```python
self.guard.max_losses = 999      # 当日连亏熔断失效
self.guard._losses = 0
self.cta_engine.size = 0.001     # 合约乘数改小 → 风险金额接近 0
```

`guard`/`trail`/`am`/`cta_engine` 是**实例属性**:既不在 `vars(candidate)` 里(`check_class` 只比类字典),
也不在受保护名清单里(受保护的是 `guard` 这个**名字**,不是它指向的对象)。
实测:改 `guard` 后成交 120 → 149 笔(熔断形同虚设);改 `cta_engine.size` 后回撤从
-4272 元 / -39.97% 缩成 **-0 元 / -0.00%**,`promotion_gate` 的 2000 元与 20% 两道门槛**同时失效**
(夏普是比值,不受影响,所以门槛看起来"过了")。

**处理**(三条):
1. 静态层改为解析**整条属性路径**(`self.guard.max_losses` 整条路径入库),命中即拒;
2. 新增 `BUILTIN_PROTECTED_STATE_OBJECTS`(`guard`/`trail`/`cta_engine`):对这些对象**连引用都禁**
   ——只禁"写"会被别名绕过去(`g = self.guard` 之后的 `g.max_losses = 999` 按属性路径查也查不到);
   `am`/`bg` 不在此列(候选要读行情 `self.am.close[-1]`,那是它的正当用途),只受"不得写入"约束;
   另把 `am.array` 列为受保护路径(整条路径不得出现在源码里);
3. 新增**运行时第三层** `constitution.check_instance_state()`:回测**跑完之后**核对实例上的
   `risk_budget`/`atr_window`/`max_daily_losses`/`guard.max_losses`/`cta_engine.size`,
   不一致即 fail-closed 抛错(`scripts/backtest_lib.py` 的两条入口都接上)。
   这层**不依赖候选写得老实**——静态层拦写法、类校验拦类字典,这一层拦的是**回测途中**才发生的改写。
   比对基准是**回测开始前的快照**,不是模板常量:研究脚本会用更大的 `risk_budget` 做敏感性实验
   (`tests/test_strategy_backtest.py` 里就是),那是合法的;被改写的是"回测途中"。

#### 第 8 轮之后自查又发现的两个缺口(自己写的新规则自己复查时抓到的)

**缺口 1:写受保护成员只看 `self.` 开头的路径,`s = self` 之后绕过去。**
上面那条"按整条属性路径判定"最初只认 `self.` 根,于是这三种写法静态层**全返回空列表**:

```python
s = self;  s.risk_budget = 1e12      # 给 self 取别名之后改底线
o = object();  o.risk_budget = 1e12  # 根不是 self
s = self;  s.am.close = [1.0]
```

第三条还算**回归**:早先的规则是按赋值的**末段**属性名判定的(`target.attr`),
`o.risk_budget = ...` 那种写法本来就被拒;换成"只看 self 根的整条路径"之后反而放行了。
现改成:**属性路径里任何一段命中受保护成员就算越权**(不限根)。

**缺口 2:行情数组可以被原地改写——那等于伪造回测数据。**

```python
self.am.close[0] = 999.0        # 下标赋值:改的是被取到的那个对象
arr = self.am.close;  arr[0] = 999.0   # 取别名之后原地改
```

vn.py 的 `ArrayManager` 里 `close` 是 **property**,返回的就是 `close_array` 本身(不是副本),
所以"取出来再改"与"直接改"是一回事。候选改掉已经走过的价格,就能凭空造出一段行情。
现补三条:**下标赋值**也按整条路径判定(`self.am.close[0] = …` 命中 `am`);
新增"**受保护容器不得取别名**"(`arr = self.am.close` 拒——带下标的读 `x = self.am.close[-1]`
与带调用的读 `x = self.am.atr(14)` 放行,别把正常用法一起误伤)。

**顺带更正本 ADR 早先的一句过头话**:原先写"`numpy 标量(self.am.close)`是取不出来的副本,不在此列"——
**不成立**,`self.am.close` 就是那个数组本体。另外 `BUILTIN_PROTECTED_PATHS` 里的 `am.array`
在 vn.py 的 ArrayManager 里**根本没有这个属性**(真实的是 `close_array`/`open_array`/…),
这一条几乎匹配不到东西;真正护住行情数组的是上面的"容器不得取别名"。该条目留着是因为
历史守卫对内置清单只查"只增不减",删条目会被判违规——这一点也写在这里,免得以后有人以为它有用。

**新守卫第一次实战生效**:改写 `_write_violation` 时顺手把 `c.protected_names` 的直接引用
换成了 `_is_protected(...)`,引用次数从 10 降到 9,**守卫当场报了出来**
("常量还在但底线的执行点被删了")。这条规则不是摆设。

#### 其余发现(同轮)

- **High 3——历史守卫只守常量,不守常量的执行点**:删掉 `if name in BUILTIN_FORBIDDEN_DUNDERS:` 整块、
  窄化该判断、删掉 `forbidden_order_calls` 分支、`_is_protected` 恒返回 `False`,
  这四种改法下守卫退出码**都是 0**(全绿)。守卫比对的是**常量集合**,不看常量**被怎么用**;
  "常量还在、底线的执行没了"正是它挡不住的那条路。
  **处理**:改为按 AST 统计受保护常量的**引用次数**(Load 语境,定义处不算),引用次数减少即报错;
  `BUILTIN_ALLOWED_OVERRIDES` 另定为**只减不增**(白名单加一项 = 多放开一点底线,
  与"清单只能追加"的规则正好相反)。
- **Medium 4——网格的受保护轴检查与 `check_params` 口径不一**:前者大小写敏感且只覆盖
  `protected_parameters`,后者大小写不敏感且含 names。现收敛到 `constitution.protected_lower_all()`
  这一把尺子,两边共用。
- **Medium 5——`check_params` 只查顶层**:嵌套字典/列表里的受保护键漏检。现改为递归。
- **Low 6——受禁 dunder 名单漏了反射型的那批**:`__del__`/`__reduce__`/`__reduce_ex__`/`__mro_entries__`/
  `__dict__`/`__class__`/`__mro__`/`__globals__`/`__code__` 等。现已补齐(见上面的保留字表)。
- **Low 7——`__slots__` 里藏受保护名能过静态层**(运行时层兜住了)。现静态层也扫 `__slots__` 元素。
- **Low 8——子入口的 TOCTOU**:子入口先查审批(读一次文件),再由 `import_candidate` **重新读一次**
  并校验**调用方传入的** `--sha256`,两次读之间文件若被换掉就会跑未审批内容。
  已改为按**审批命中的哈希**核对(并行的那一路会话完成,提交 `f90807d`)。

#### 本轮之后仍然存在的残余缺口(如实记录)

1. 静态层是**纵深防御,不是安全边界**——真正的边界仍是容器隔离与"运行前由人读过代码";
2. `am` 的**子对象**按"容器"整体保护(取别名即拒),没有逐个字段建模;
   静态层不做作用域解析,`self` 的别名之外的路径靠"路径里任何一段命中受保护成员"这条规则兜着,
   但它挡不住**同名但无关**的对象(比如候选自己造一个叫 `guard` 的字段——那种写法会被误伤,属 fail-closed 的有意取舍);
3. 晋级门槛缺**逐笔风险预算审计**:能通过门槛的候选,逐笔风险金额仍可能大于 300 元
   (只是没被改写而已)——这是下一步要补的,不是本轮解决的;
4. 守卫现在守"引用次数",不是守"语义"——把一次 `if name in BUILTIN_FORBIDDEN_DUNDERS:`
   改成 `if name in BUILTIN_FORBIDDEN_DUNDERS or True:` 之类的同义改写仍可能躲开计数。
   语义级的守卫需要把执行点本身登记成受保护常量(下一步)。

### 第 9 轮补记:模块级改写、`__new__`、以及运行期第四层(身份指纹)

第 9 轮(codex CLI 深度分析,**34/50,需改进,不建议发布**)报出一条 **Critical** 与若干 High/Medium/Low。
与前几轮不同的是:这一条**四层防线全部放行**,而且绕开的是"整条属性路径判定"本身——

```python
import risk.risk_guard as rg
rg.risk_per_trade = lambda *a, **k: 0.0     # 单笔风险恒为 0 → 300 元预算形同虚设
```

第 8 轮刚加的"整条属性路径"规则在这里**不起作用**,原因有两个:
其一,静态层判的是"候选**自己**的源码里出现了受保护成员的路径",
而这里被改的是**模块对象上的属性**——路径是 `rg.risk_per_trade`,
`rg` 是候选自己起的别名,静态层无从知道它指向 `risk.risk_guard`;
其二,`risk_per_trade` 这个名字本身**不在**任何受保护名单里(受保护的是方法与状态对象),
第 8 轮新增的 `BUILTIN_PROTECTED_MODULE_ATTRS` 正是为这条补上的。

即便补了名单,写法一换就又漏:`rg.risk_per_trade = …`、`import risk.risk_guard as rg` 后赋值、
`type(cls).risk_budget = …`(路径的根是**调用表达式**,路径解析在那一段断掉)、
`sys.modules["risk.risk_guard"].risk_per_trade = …`……
**枚举写法是枚举不完的**。这与第 7 轮的结论一致:**黑名单封的是"已知写法",封不住"能力"**。

#### Critical 1 的处理:运行期第四层——受保护对象的身份指纹

不再枚举写法,改为**事后比对身份**:

```python
PROTECTED_RUNTIME_PACKAGES = ("risk", "strategies", "vnpy_ctastrategy")

before = snapshot_protected_state()   # 候选执行前
exec(candidate_code, module.__dict__)
after  = snapshot_protected_state()   # 候选执行后
tampered = check_protected_state(before, after)   # 被替换/被删除/被新增 → fail-closed
```

指纹覆盖**三个受保护包下所有已加载模块的公开属性**,以及其中**每个类的成员**;
容器(list/dict/set)取**副本**比对,函数与类比**身份**。
于是不管候选用哪种写法(`rg` 直接改、别名改、`type(cls)` 改、`sys.modules` 改),
只要换了对象就对不上——**写法不再重要,结果才重要**。

接点有三处:
- `scripts/research_api.py::import_candidate`:候选**导入期**前后各拍一张;
- `scripts/backtest_lib.py`:两条回测入口(`run_engine` / `run_engine_bars`)在
  `engine.add_strategy(...)` **之后**拍基线,回测**结束后**再拍一张——
  回测途中才发生的改写(第 8 轮第三层看的是实例状态,这一层看的是框架对象)同样能抓住。

**快照必须覆盖到模板模块**:模板是候选自己 import 的,
若在候选 import 之后才拍快照,快照里根本没有它,候选在导入期把它改写了也拍不到。
所以 `_preload_protected_modules()` 在拍快照**之前**先导入
`vnpy_ctastrategy`(`template`/`backtesting`/`engine`)与模板模块本身。
这一条是 codex 实测出来的:不预加载时,候选改写模板类**完全不被记录**。

#### High 3:`__new__` 早于快照,构造期改写结构性不可见

`__new__` 在**实例构造时**执行,早于回测前的快照——
它里面的改写会被当成"基线",运行期第三层(比对实例状态)**结构性地看不见**。
第 9 轮 codex 实测:候选定义 `__new__` 并在里面改 `risk_budget`,三层防线全部放行。
候选没有任何正当理由自定义 `__new__`(模板也没有),所以直接**禁掉**:
`__new__` 加入 `BUILTIN_FORBIDDEN_DUNDERS`(不得定义、引用或调用)。

#### 其余发现(同轮)

- **High 3——ADR 保留字表漏项**:表里没列"受保护模块级成员""受保护容器"
  "运行期受保护包",`__new__` 也漏了,而表是候选作者**唯一的查处**——
  漏了就等于没有。已补齐,并把 `tests/test_research_sandbox.py` 里的
  `listed()` 扩展到**全部**宪法名单(`BUILTIN_PROTECTED_NAMES` 整组、
  `BUILTIN_PROTECTED_MODULE_ATTRS`、`BUILTIN_PROTECTED_CONTAINERS`、
  `BUILTIN_PROTECTED_PATHS`、`BUILTIN_PROTECTED_ENTRY_POINTS`、
  `BUILTIN_PROTECTED_STATE_OBJECTS`、`PROTECTED_RUNTIME_PACKAGES`),
  以后代码里加任何一个名字而表里没写,那条用例会当场红。
- **Medium 5——候选能 import 风控包**:`risk` 整个包加入 `FORBIDDEN_MODULES`
  (静态层与受限导入器同时生效)。候选没有正当理由 import 风控模块——
  风控由基类固定,候选只写信号逻辑。
  注意**不能**把 `strategies` 一并禁掉:候选本来就要
  `from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy` 继承基类。
- **Low 6——历史守卫重复解析**:守卫此前对**每个**历史版本都重新解析一遍当前源码
  (引用次数统计),历史一长就是平方级的浪费。改为**当前源码只数一遍**,
  新增 `usage_regressions_from_counts(current_counts, older_source, names)`,
  `usage_regressions()` 保留为包装(它有测试在用)。
- **新增登记**:第四层的覆盖范围(`PROTECTED_RUNTIME_PACKAGES`)与预加载清单
  (`_PROTECTED_PRELOAD_MODULES`)已登记进 `tools/check_constitution_history.py` 的
  `BUILTIN_SETS`——少一个包 = 那一个包不再受指纹保护,历史守卫会报出来。

#### 第 9 轮之后仍然存在的残余缺口(如实记录)

1. **指纹不是"内存证明"**:它比对的是**已加载模块**的属性与类成员。
   候选若通过 `importlib`/`ctypes` 之类手段在别的命名空间里造一个"影子"对象,
   指纹看不见——但那类模块本来就被禁用清单挡着(`importlib`/`ctypes` 均在 `FORBIDDEN_MODULES` 里),
   且**它改不到真框架对象**,所以对底线的威胁有限。
2. **指纹只覆盖三个包**:别的第三方库(数据源、日志、指标)不在范围内。
   它们不承载风控判定,所以当前不构成底线风险;若将来引入承载风控的新包,
   必须同时加进 `PROTECTED_RUNTIME_PACKAGES`(历史守卫会强制这一点)。
3. **静态层仍然是纵深防御,不是安全边界**——真正的边界仍是容器隔离
   (`docker-compose.research.yml`:无网络、只读挂载、`cap_drop ALL`)与"运行前由人读过代码"。
4. **守卫守的是"引用次数"与"集合内容",不是语义**:把一次检查改写成同义形式仍可能躲开计数;
   `PROTECTED_RUNTIME_PACKAGES` 这种"多一项更严"的常量已登记,但语义级守卫仍未做。
5. 晋级门槛仍缺**逐笔风险预算审计**(第 8 轮登记的遗留项,本轮未动)。

### 第 10 轮补记:函数对象的**状态**、实例上的**遮蔽**,以及"判定逻辑本身"可被改写

第 9 轮引入的运行期第四层(受保护对象的**身份指纹**)把"换个写法改写框架对象"堵住了,
但它比的是**身份**(`("id", value)`)。第 10 轮 codex 复核与本地自查各抓到一条 Critical,
两条都**四层防线全部放行**,且都不是"写法"的问题:

#### Critical 1(codex):改的是函数**对象的状态**,不是它的名字

    from strategies.trend_atr_strategy import within_budget
    within_budget.__defaults__ = (0.0,)        # volume 的默认值变成 0

`within_budget` 还是**那个函数对象**,身份没变,只比身份完全看不见;
而改完之后单笔风险金额恒为 0,300 元预算形同虚设。
实测:`within_budget(500, 10, 300)` 由 `False` 变成 `True`。

处理(纵深,三处各自独立):

- **静态层**:把 `__defaults__`/`__kwdefaults__`/`__annotations__`/`__wrapped__`
  加进 `BUILTIN_FORBIDDEN_DUNDERS`,沙盒的 `FORBIDDEN_ATTRS`/`FORBIDDEN_STRINGS` 同步;
  候选没有任何正当理由碰这些 dunder。
- **静态层(读)**:新增 `BUILTIN_PROTECTED_READ_ATTRS`
  (`within_budget`/`stop_distance`/`risk_per_trade`/`DailyLossGuard`/`TrailingStop`/`trading_day`)——
  候选**不得读**风控对象。风控由基类固定,候选只写信号逻辑;
  拿到定义就多一条改写它的路。只收**不会误伤**的名字:泛用词
  (`reset`/`can_open`/`on_trade_closed`/`ceil_to`/`floor_to`)不在其中。
- **第四层**:指纹对函数不再只比身份,而是比
  `(函数对象, __code__ 的 id, __defaults__, __kwdefaults__, 闭包单元格内容)`。
  就地改写默认参数、换掉代码对象、改闭包里捕获的变量,都会被报出来。

#### Critical 2(自查):在**实例**上遮蔽模板方法

    def entry_signal(self):
        self.get_size = lambda: 0.001        # 真实合约乘数 10
        return 1

`within_budget(dist, self.get_size(), self.risk_budget)` 读到的是 0.001,
300 元预算算成 0.003 元,任何止损距离都"在预算内"。
类字典里没有这个赋值(`check_class` 比不出来),实例字段快照只盯三个底线值,
指纹只覆盖模块属性与类成员——所以四层全放行。

处理:

- **静态层**:`get_size` 进 `BUILTIN_PROTECTED_NAMES`(它是"决定风险金额"的框架方法),
  `self.get_size = ...` 按名字被拦。
- **第三层**:新增"实例上不许遮蔽模板方法"的核对——模板 MRO 里的**可调用**成员,
  实例字典里不许出现这些名字(白名单里的 `entry_signal` 等除外)。
  这里只查"实例上有没有遮蔽",不比"实现是不是同一个":同一个家族里日线模板与
  15 分钟模板本来就有各自的实现,按"必须等于某个模板的实现"去比会误伤合法的日线策略。

#### 其余发现(同轮)

- **High 1——`trading_day` 没被保护**:熔断器按它分日("今天亏了几笔"),
  改掉它就能把昨天的亏损算到今天,当日连亏熔断随之失效。
  已进 `BUILTIN_PROTECTED_MODULE_ATTRS` 与 `BUILTIN_PROTECTED_READ_ATTRS`。
- **High 2——指纹"跳过所有 dunder"**:于是改写类的 `__getattribute__`
  (让 `self.get_size()` 返回假的)或 `__instancecheck__`(把 isinstance 判定废掉)都拍不到。
  改成**显式忽略清单**(`_FINGERPRINT_IGNORED_DUNDERS`,只放过纯元数据),
  其余 dunder 一律进指纹;`__instancecheck__`/`__subclasscheck__`/`__subclasshook__`/`__prepare__`
  同时进受禁 dunder 名单。
- **High 2 的另一半——家族判定本身可被改写**:第三层原来用
  `isinstance(strategy, family_root(template))` 判"这是不是模板家族的策略",
  而 `isinstance` 的判定逻辑正是 `ABCMeta.__instancecheck__`——把它换掉,
  第三层直接判成"不适用"而放行。改成按 **MRO** 判定(结构判定,没有可改写的入口)。
- **Medium 1——受保护模块成员"只禁写不禁读"**:读一次就多一条改写的路,
  由上面 `BUILTIN_PROTECTED_READ_ATTRS` 补上(只收不会误伤的名字)。
- **Medium 3——第四层的"回测前"基准拍晚了**:`backtest_lib` 里基准是在
  `engine.add_strategy()` **之后**拍的,构造期的改写会被当成"基线"。
  基准前移到构造之前(`run_engine` 与 `run_engine_bars` 两处)。
- **Medium 4——第四层的**执行点**没人守**:删掉 `check_protected_state(...)` 那一行,
  常量集合没变、清单文件也没变,第四层就此失效。
  `tools/check_constitution_history.py` 新增 `CALL_SITE_REGISTRY`,
  按"引用次数只许增不许减"守 `snapshot_protected_state`/`check_protected_state`
  在 `backtest_lib` 与 `research_api` 两处的引用次数(实测:删掉调用后守卫退出码 1)。
- **Low 1——泛用名按名字一刀切**:`self.indicator.reset = 1` 这种普通写法会被
  `reset`(TrailingStop 的方法名)误拒。带主的名字改成**主人也在路径里**才算命中
  (`BUILTIN_MODULE_ATTR_OWNERS`),路径断在调用表达式上时只认底线成员。
- **Low 3——比对可能抛异常**:numpy 数组的 `==` 返回数组,放进 `bool()` 会抛 ValueError,
  原实现会把它当成"被改写"(误报)。指纹改用 `_safe_token`:能按值比的按值比
  (浮点用 `repr` 解决 nan),不能的一律退化成身份标记,**比对永不抛异常**。

#### 第 10 轮之后仍然存在的残余缺口(如实记录)

1. **第四层仍是"前后差分"**:候选若在回测途中"改→用→还原",差分看不出来。
   这一类要靠**能力收走**来堵(见"执行时的能力隔离":候选的内建里没有
   `setattr`/`getattr`/`type`,`sys`/`importlib`/`ctypes` 等模块导不进来),
   静态层则保证**源码里可见的**改写一律按名字被拦。
   两层叠加之后,"纯动态取得受保护对象再改"的路已经堵死,但差分本身的盲区仍在。
2. **指纹覆盖三个包、约 3400 个键**:一次快照约 6 毫秒(第 9 轮是 2.5 毫秒,
   多出来的是 dunder 覆盖与函数状态)。每次回测拍两张,代价可接受。
3. **静态层仍然是纵深防御,不是安全边界**——真正的边界仍是容器隔离与"运行前由人读过代码"。
4. 晋级门槛仍缺**逐笔风险预算审计**(第 8 轮登记的遗留项,本轮未动)。

### 第 11 轮补记:容器上的**变更型方法**、执行点的**调用形态**,以及指纹范围的**显式化**

第 11 轮 codex CLI 深度分析(**43.0/50,需改进,不建议发布**)逐条复现了第 9/10 轮的 10 条修复:
**9 条确认真堵住**(函数对象状态改写、实例遮蔽 `get_size`、`trading_day`、指纹 dunder、
MRO 家族判定、受保护成员禁读、基准前移、`reset` 带主机制、`_safe_token`),
**1 条只半堵**(第 10 轮的"执行点注册表"只数名字不看语义)。本轮报出 2 条 High / 3 条 Medium / 2 条 Low,
逐条处理如下。

#### High 1 —— 受保护容器只挡"写/取别名",不挡"变更型方法调用"

`self.am.update_bar(fake_bar)` 在静态层与沙盒层都返回 `[]`,第 3/4 层也不覆盖活实例:
codex 端到端实测**四层全绿**而回测结果被完全改写(`skipped_by_budget` 从 0 变成 3928)。
这与 `BUILTIN_PROTECTED_CONTAINERS` 的立意(防止伪造价格 -> 伪造收益)直接矛盾:
写数组被拦、插数组不拦。

修法:新增 `BUILTIN_PROTECTED_CONTAINER_SCOPE`(`am`/`bg`)与 `BUILTIN_PROTECTED_CONTAINER_MUTATORS`
(`update_bar`/`update_tick`/`append`/…/`byteswap`)。凡是"路径经过受保护容器"且"调用了变更型方法"
的一律拒;容器上的**只读**调用(`self.am.sma(...)`)照常放行。
配套降误报:裸局部名 `am`/`bg` 降级为**路径保护**(只保护 `self.am.xxx`,不再按裸名一刀切),
类体里定义 `am` 仍然拒。

#### High 2 —— 第四层执行点守卫只数名字,不看语义

codex 在独立 clone 上实测四种改法,都能让 `tools/check_constitution_history.py` **exit 0** 而第四层已失效:
(a) 本地同名影子 `def check_protected_state(b, a): return []` + 改为裸名调用;
(b) `const.check_protected_state(protected_before, protected_before)`——自己比自己,比对必然通过;
(c) 把调用挪进死分支(`if False:`),名字引用数不变;
(d) 新增一个执行候选却完全不调第三/四层的脚本——注册表是手工维护的,没有完备性检查。

修法:守卫从"名字出现次数"升级为**调用形态**(`check_call_site_shape`):必须有**活调用**(死分支不算)、
不得有同名影子、`check_protected_state` 的两个实参必须不同;再加**完备性检查**
(`check_executor_completeness`):按仓库实际内容找出所有执行候选的模块(裸 `exec(...)` / 沙盒执行入口 /
候选导入入口),不在"走第四层"的传递闭包里的执行入口一律报出来。
(裸 `exec(...)` 才算执行候选:`qapp.exec()` 是 Qt 事件循环,按属性名一刀切会误报。)

#### Medium 1 —— `self.trading_day` 的新误报

第 10 轮把 `trading_day` 放进模块级成员名单之后,`self.trading_day = bar.datetime.date()`
被误判成"改写了受保护的风控函数"——"每天只开一次仓"这类常规候选会被整片拒掉。
修法:模块级成员名只在**根不是 `self`** 时才算命中(实例属性与同名候选方法一律无害);
模块级的 `rg.trading_day = ...` 仍然拦。

#### Medium 2 —— 指纹只覆盖"快照时已加载"的受保护模块

候选在运行期 import 一个此前没加载过的受保护子模块再改写它,指纹**完全看不见**
(此前"新增成员"只在父键已存在时才报)。修法:快照为每个受保护模块留一个**模块标记键**,
比对时发现"运行期新加载的受保护模块"就报出来。

#### Medium 3 —— vnpy 核心对象只靠"巧合"被覆盖

`vnpy.trader.utility.ArrayManager` 此前只因为模板**按名 import** 它才恰好出现在指纹里;
模板哪天改成 `import vnpy.trader.utility as util`,这层保护会**静默消失**,而历史守卫看不出来
(它只守常量集合,不管语义)。修法:`PROTECTED_RUNTIME_PACKAGES` 支持**点号路径前缀**,显式纳入
`vnpy.trader.constant` / `vnpy.trader.object` / `vnpy.trader.utility`——覆盖不再依赖"模板怎么 import"。

`vnpy.trader.database` **故意不在范围内**:它的模块级 `database` 全局在第一次 `get_database()` 时
会被**合法**地替换(离线取数脚本会这么做),纳入范围会造成运行期虚假 diff;
而研究链路的数据来自 CSV(`load_bars_csv`),数据库根本不在研究数据路径上,篡改它没有意义。

#### Medium 4 —— numpy 数组退化成身份标记

`_safe_token` 把 numpy 数组变成 `("id", id(x))`,`arr[0] = 5` 这种原地改写在指纹里看不见。
修法:numpy 数组按**内容**比(`tolist()` 后逐项取指纹);内容没变就不报(否则每次回测刷出一堆虚假 diff)。

#### Low 1 / Low 3

- **Low 1**:`am`/`bg` 这类容器名降级为路径保护(见 High 1)。
- **Low 3**:第三层只查**实例字典**,看不见候选在运行期给自己的类打补丁(`Cand.get_size = ...`)。
  新增 `_runtime_class_dict_violations`:回测结束后再比一次类字典(自动生成的 dunder 除外),
  白名单里的 `entry_signal` 等仍然允许。

#### 第 11 轮之后仍然存在的残余缺口(如实记录)

1. **第四层仍是"前后差分"**:候选若在回测途中"改 -> 用 -> 还原",差分看不出来(与第 10 轮同)。
2. **指纹覆盖六个包段、约 4600 个键**:一次快照约 8 毫秒(实测 7.3-10.1 毫秒,中位 8.2;首张约 490 毫秒,含模块预载);
   真实回测与候选导入前后均**零虚假 diff**(已实测)。
3. **`vnpy.trader.database` 不在指纹范围内**:研究链路不用数据库取数,当前没有暴露路径;
   若将来研究链路改从数据库读数据,这一条必须重新评估。
4. **候选 import 一个新的 vnpy 子模块并改写它**:静态层按名字看不见(该模块不在受保护名单里),
   指纹也只在"运行期新加载的受保护模块"这一层报——已登记为后续项。
5. **PBO/CSCV 仍然延后**,与本轮无关。

### 第 12 轮补记:容器的**视图与别名**、执行点守卫的**数据流**,以及运行期第五层(行情数据完整性)

第 12 轮 codex CLI 深度分析(**38.5/50,需改进,不建议发布**)端到端实测复现了第 11 轮的修复,
并报出 2 条 High / 2 条 Medium / 2 条 Low,逐条处理如下。

#### High 1 —— 容器规则只挡"写/取别名",不挡"视图 / dunder 写入口 / bg 别名"

codex 在 120 根合成 15 分钟 K 线上端到端实测:`self.am.close[:]`(numpy 视图)、
`self.am.close.__setitem__(...)`(dunder 写入口)、`x = self.bg` 再 `x.update_bar(fake)`
(别名),静态层与沙盒层都返回 `[]`,第 3/4 层也不覆盖活实例——**四层防线全绿**,
而 ATR 被从 40 压到 2.86、`skipped_by_budget` 由 1 变 0,诚实模板 0 成交而候选成交 2 笔:
**¥300 单笔风险预算被架空**。

根因:"按名字禁止变更型方法"只覆盖了**直接调用**。numpy 的**视图**与 dunder 写入口可以在
不改写容器本身的前提下改写它的内容;而别名一旦被取走,调用点已经看不出被调的是谁。

修法分两层:
- **静态层**:新增 `BUILTIN_PROTECTED_CONTAINER_VIEWS`(`view`/`reshape`/`ravel`/`squeeze`/`transpose`),
  这些方法返回的还是同一块内存;变更型方法名单补上 dunder 写入口
  (`__setitem__`/`__delitem__`/`__iadd__`/`__imul__`/`__ior__`);
  并新增"**交出引用**"检查(`_container_escape_findings`):把容器存进名字(赋值 / 带注解 / 海象 / 元组解包)、
  传给出调用、返回出去、装进容器、作为默认参数——凡是让引用**流出去**的动作一律拒。
  容器只许**就地**使用(读 `self.am.close[-1]`、读调用 `self.am.sma(5)`)。
- **运行期第五层**(`check_market_data_integrity`):回测结束后把行情容器与引擎**真正喂过的** K 线
  逐值比对——回放根数不得多于喂入根数(凭空多出来的 K 线),容器里的每个价格都必须**真实出现过**
  (基准里已有的 + 引擎喂过的),回放根数与喂入根数一致时再逐值比一遍尾部(被改写的行情)。
  这一层**不依赖静态分析**,所以尚未被枚举出来的写路径也一并兜住。

  **基准拍在 `on_init` 之前,预热的合法根数只认引擎交出的那些**:日线族的 `on_init` 会先经
  `engine.load_bar` 加载一段历史日线做预热,那些根数同样记在 `am.count` 里——把"累计根数"直接当成
  "回放根数",就会把**预热**误判成"凭空多出来的 K 线",整个日线研究链路都会在回测结束时
  fail-closed(本轮修复时真的踩到了:14 条既有测试同时变红)。第一版的补法是把基准拍在预热之后,
  但**这又开了一个洞**(第 13 轮自查实测):候选在 `on_init` 里往容器塞 40 根假 K 线,它们成了
  "基线",`am.count=160` 而引擎只喂了 120,第五层照样放行。现在的口径:基准拍在构造完成时
  (`on_init` 之前,容器为空),回测脚本包住 `engine.load_bar` 记下**引擎实际交出的**预热 K 线,
  与回放 K 线一起算"合法喂入"。

#### High 2 —— 第四层执行点守卫仍能被"形态"绕过

codex 在独立 clone 上实测五种改法,守卫都能 **exit 0** 而第四层已经失效:
把比对挪进**永不被调用的函数**;第二个实参写成语义相同但语法不同的
`dict(const.snapshot_protected_state())`;新执行入口只在**注释**里写上那两个名字;
新执行入口用 `eval(compile(...))` 跑候选;新执行入口只 `import` 一个已覆盖的模块。

修法:守卫从"名字与计数"升级为**数据流 + 作用域 + 可达性 + AST 覆盖**:
- 第二个实参必须**就是**一次 `snapshot_protected_state()` 调用(不能是变量,不能包一层);
- 快照调用与比对调用必须在**同一个函数作用域**内;
- 所在函数必须在仓库里**被调用过**(死函数不算);
- 执行入口的识别改为 **AST 为准**(注释里的名字不算数),并覆盖裸 `exec`/`eval`/`compile`
  与属性形式的 `import_module`/`run_path`/`run_module`;
- 新增 `check_constitution_module_is_clean`:宪法模块自身不得出现裸 `exec`/`eval`/`compile`。

#### Medium 1 —— 裸局部名 `am.append(...)` 被误拒

第 11 轮承诺"裸局部名 `am`/`bg` 不再按裸名一刀切",但只放宽了**赋值**路径,变更型方法的
**调用**路径照旧按裸名拒——`am = []` 再 `am.append(x)` 会被整片拒掉,常规候选会被误伤。
修法:受保护容器的判定改为**路径级**——只有作用域段出现在接收者路径的第 2 段及以后
(`self.am.update_bar` ✓、`s.am.update_bar` ✓)才算命中;裸名 `am.append` 放行。

#### Medium 2 —— 伪造 `__module__` 能买"仓库自己的类"豁免

运行期类字典检查给"仓库自己的策略类"开了豁免(它们合法地重写内部方法)。候选只要在类体里写
`__module__ = "strategies.trend_atr_15m_strategy"`,字符串就与仓库里的模块名对上了,豁免生效——
越权的 `get_size` 改写被放过。
修法:豁免的判定必须是"这个类**确实定义在**它声称的模块里"。

判据不能是"`vars(module)` 里有没有这个名字"——**函数里就地定义的类**本来就不在模块字典里
(`tests/test_x.py` 里的 `Chatty` 就是),那样判会把仓库自己的代码也拒掉(实测 14 条既有测试变红)。
也不能是 `inspect.getfile(cls)`——它对类是循环的(按 `cls.__module__` 查模块文件),正好被伪造的
`__module__` 骗过。真正的判据是**类体里自己的代码落在哪个文件**:自己写的函数/lambda 的
`__code__.co_filename` 才是定义位置。于是:
- 仓库里就地定义的替身:代码文件 == 模块文件 → 豁免;
- 候选伪造 `__module__ = "strategies.xxx"`:代码文件是候选文件,模块文件在 `strategies/` → 不同 → 不豁免;
- 类体里没有自己的函数(只继承)时,退回"模块字典里就有这个类"来判断。

顺带修正:待审目录不再写死,而是从 `RESEARCH_SANDBOX_CONFIG` 指向的配置文件里读 `pending_dir`
(默认 `pending_strategies`)。

#### Low 1 / Low 2 —— 文档与实测不符

- **Low 1**:保留字表少了两组名字(容器视图型方法、dunder 写入口),已补到上表
  (该表仍是单一事实来源,由 `tests/test_research_sandbox.py::test_the_reserved_word_table_matches_the_code` 逐个核对)。
- **Low 2**:三处与实测不符的断言——①"伪造 `__module__` 能骗过豁免"那是**漏洞**不是保证(已在 Medium 2 修掉);
  ②"裸局部名 `am.append` 一律拒"是第 11 轮的半成品(已在 Medium 1 修掉);
  ③"一次快照约 5 毫秒"实测为 7.3-10.1 毫秒(中位 8.2),已改。

#### 第 12 轮之后仍然存在的残余缺口(如实记录)

1. 第五层只覆盖**行情容器**(价格数组与 K 线根数);候选若改写的是别的引擎状态(例如持仓或委托的内部字段),
   第五层看不见——但那些字段落在第三层(受保护成员)与第四层(身份指纹)范围内。
1b. 第五层的"逐值比尾部"只在**一根 K 线对应一次容器更新**时成立(当前的日线族与 15 分钟族都是);
   若将来出现"一根 K 线不更新容器"的家族,那一段只能靠"价格必须真实出现过"这条兜底。
2. 第五层比对的是"喂入的 K 线 vs 容器里的价格";若引擎本身喂错(不是候选造成的),它也会报出来——**故意的**:宁可多报。
3. 第四层仍是"前后差分":候选若在回测途中"改 -> 用 -> 还原",差分看不出来(与第 9/10/11 轮同)。
4. 容器静态规则是**语法级**的:候选若用反射拿到容器——但 `getattr` 在执行期根本不提供
   (`safe_builtins` 已移除),静态层也把 `getattr` 列为禁用内建。
5. PBO/CSCV 仍然延后,与本轮无关。

### 关于"vnpy 全家只有一处按名 setter"这句话的更正

早先这里写的是"已核过 vnpy 全家,按名 setter 只有 `update_setting` 一处"——**说过头了**。
更准确的说法:`update_setting` 是**候选能直接调用**的那一处;此外还有
`vnpy_ctastrategy/engine.py` 里 CTA 引擎按 `strategy.variables` 恢复变量的路径,
以及 `vnpy_spreadtrading/template.py` 的对应实现。后两处当前无害:
`variables` 是模板成员、候选改不了(运行时层会拦),而且引擎恢复的值来自引擎自己的状态,不是调用方给的。
但这是"白名单恰好不含它"的巧合,不是文档承诺的不变量——所以给 `BUILTIN_ALLOWED_OVERRIDES`
加新名字之前,必须先确认它不在 `variables`/`parameters` 里。

### 保留标识符与误报(有意为之)

静态层按 AST 里的**名字**拦,不做作用域解析,所以下列写法一律被拒(第 5、7 轮复核都实测确认):
局部变量或函数参数恰好叫 `cover`/`short`/`buy`/`sell`/`type`/`open`/`vars` 等;
与交易无关的对象上恰好有个叫 `buy` 的属性;文档字符串里提到 `__dict__` 这类 token。
`cover`/`short` 在期货策略里是极普通的词("回补空头"),LLM 生成的候选会常规踩中。

这是 **fail-closed 的有意取舍**:宁可误伤让人来看,不可放过一个真绕过。代价与范围已登记在
"已知残余风险"一节。静态层不是安全边界,为减少误报而做不可靠的局部数据流推断会把防线改薄,所以不做。

**保留字清单(候选作者一侧要查的就是这份)**:清单的单一事实来源是代码里的常量,
下面这份是从代码抄出来的快照;改动常量时要同步改这里,并跑
`tests/test_research_sandbox.py::test_the_reserved_word_table_matches_the_code`
(它逐个核对本表里的名字与代码常量是否一致)。

| 类别 | 名字 | 出处 |
|---|---|---|
| 受保护参数(不得出现在设置字典、网格、字典键、关键字参数里) | `risk_budget`、`max_daily_losses`、`atr_window`、`history_vt_symbol` | `BUILTIN_PROTECTED_PARAMETERS` |
| 受保护成员(不得在类体里重写) | `guard`、`trail`、`am`、`bg`、`pending_dist`、`pending_entry`、`_stop_armed`、`atr_value`、`stop_price`、`entry_price`、`entry_dist`、`prev_pos`、`skipped_by_budget`、`trail_side`、`trail_extreme`、`trail_stop`、`is_live`、`state_consistent`、`cta_engine`、`get_size`、`__init__`、`on_bar`、`on_tick`、`on_init`、`on_start`、`on_stop`、`on_trade`、`on_order`、`on_stop_order`、`on_account_status`、`on_position`、`on_cancel_order`、`on_disconnect`、`_process_daily_bar`、`_manage_position`、`_try_enter`、`_send_entry`、`_place_protective_stop`、`_place_pending_entry`、`_stop_order_price`、`_ensure_protective_stop`、`_complete_stale_daily_bar`、`risk_budget`、`max_daily_losses`、`atr_window`、`history_vt_symbol` | `BUILTIN_PROTECTED_NAMES` |
| 受禁 dunder(不得定义、引用或调用) | `__setattr__`、`__delattr__`、`__getattribute__`、`__getattr__`、`__init_subclass__`、`__set_name__`、`__dict__`、`__class__`、`__mro__`、`__bases__`、`__subclasses__`、`__globals__`、`__code__`、`__func__`、`__self__`、`__closure__`、`__reduce__`、`__reduce_ex__`、`__getnewargs__`、`__getstate__`、`__setstate__`、`__del__`、`__mro_entries__`、`__new__`、`__defaults__`、`__kwdefaults__`、`__annotations__`、`__wrapped__`、`__instancecheck__`、`__subclasscheck__`、`__subclasshook__`、`__prepare__` | `BUILTIN_FORBIDDEN_DUNDERS` |
| 受保护的**风控执行点**(不得引用,连取别名都不许) | `_try_enter`、`_send_entry`、`_manage_position`、`_place_protective_stop`、`_place_pending_entry`、`_process_daily_bar`、`_ensure_protective_stop`、`_stop_order_price`、`_complete_stale_daily_bar` | `BUILTIN_PROTECTED_ENTRY_POINTS` |
| 受保护的**风控状态对象**(不得引用,连读都不许) | `guard`、`trail`、`cta_engine` | `BUILTIN_PROTECTED_STATE_OBJECTS` |
| 受保护的**属性路径**(整条路径不得出现在源码里) | `am.array` | `BUILTIN_PROTECTED_PATHS` |
| 受保护的**模块级成员**(不得引用:改写它们等于改掉风控计算本身) | `within_budget`、`stop_distance`、`risk_per_trade`、`ceil_to`、`floor_to`、`DailyLossGuard`、`TrailingStop`、`can_open`、`on_trade_closed`、`reset`、`trading_day` | `BUILTIN_PROTECTED_MODULE_ATTRS` |
| 受保护的**风控对象**(不得**读**:拿到定义就多一条改写它的路) | `within_budget`、`stop_distance`、`risk_per_trade`、`DailyLossGuard`、`TrailingStop`、`trading_day` | `BUILTIN_PROTECTED_READ_ATTRS` |
| 受保护的**容器**(不得取别名另存) | `am` | `BUILTIN_PROTECTED_CONTAINERS` |
| 受保护容器的**范围**(这些名字上的容器受保护) | `am`、`bg` | `BUILTIN_PROTECTED_CONTAINER_SCOPE` |
| 受保护容器上的**变更型方法**(不得调用:改行情 = 伪造收益) | `update_bar`、`update_tick`、`append`、`extend`、`insert`、`remove`、`pop`、`popitem`、`clear`、`update`、`setdefault`、`sort`、`reverse`、`discard`、`fill`、`itemset`、`put`、`resize`、`copyto`、`setfield`、`setflags`、`byteswap`、`__setitem__`、`__delitem__`、`__iadd__`、`__imul__`、`__ior__` | `BUILTIN_PROTECTED_CONTAINER_MUTATORS` |
| 受保护容器上的**视图型方法**(返回的还是同一块内存,改它等于改容器) | `view`、`reshape`、`ravel`、`squeeze`、`transpose` | `BUILTIN_PROTECTED_CONTAINER_VIEWS` |
| 运行期**受保护包**(第四层身份指纹覆盖范围:整包模块与类成员;支持点号路径前缀) | `risk`、`strategies`、`vnpy_ctastrategy`、`vnpy.trader.constant`、`vnpy.trader.object`、`vnpy.trader.utility` | `PROTECTED_RUNTIME_PACKAGES` |
| 按名 setter(不得引用或定义) | `update_setting` | `BUILTIN_FORBIDDEN_BY_NAME_SETTERS` |
| 下单/撤单接口(不得引用) | `buy`、`sell`、`short`、`cover`、`send_order`、`send_stop_order`、`send_local_stop_order`、`cancel_order`、`cancel_all`、`cancel_all_orders` | `BUILTIN_FORBIDDEN_ORDER_CALLS` |
| 禁用内建(不得引用) | `exec`、`eval`、`compile`、`open`、`__import__`、`getattr`、`setattr`、`delattr`、`globals`、`locals`、`vars`、`input`、`type` | `research_sandbox.FORBIDDEN_CALLS` |
| 禁用属性(裸名与字符串都拒) | `__bases__`、`__subclasses__`、`__globals__`、`__builtins__`、`__dict__`、`__code__`、`__mro__`、`__class__`、`__defaults__`、`__kwdefaults__`、`__annotations__`、`__wrapped__`、`__instancecheck__`、`__subclasscheck__`、`__subclasshook__`、`__prepare__` | `research_sandbox.FORBIDDEN_ATTRS` |
| 禁用字符串(同上,再加上按名改写/反射的内建名) | 上面那组,外加 `setattr`、`delattr`、`getattr`、`eval`、`exec`、`compile`、`globals`、`locals`、`vars`、`input`、`__import__`、`__getattribute__`、`__setattr__`、`__delattr__` | `research_sandbox.FORBIDDEN_STRINGS` |
| 执行时**不提供**的内建 | `setattr`、`delattr`、`getattr`、`eval`、`exec`、`compile`、`open`、`globals`、`locals`、`vars`、`breakpoint`、`input`、`type`、`object`、`help`、`exit`、`quit`、`license`、`credits` | `research_sandbox.UNSAFE_BUILTINS` |
| 执行时**禁止导入**的模块(除上面那组外) | `unittest`、`unittest.mock`、`mock` | `research_sandbox.EXTRA_FORBIDDEN_MODULES` |
| 禁用模块(不得 import) | `os`、`sys`、`subprocess`、`socket`、`ctypes`、`shutil`、`pathlib`、`importlib`、`pickle`、`marshal`、`requests`、`urllib`、`http`、`ftplib`、`smtplib`、`asyncio`、`multiprocessing`、`threading`、`signal`、`builtins`、`inspect`、`gc`、`tempfile`、`glob`、`io`、`operator`、`risk` | `research_sandbox.FORBIDDEN_MODULES` |

### 哈希、筛查、编译、执行必须是同一份字节
早先 `import_candidate` 用 `spec.loader.exec_module()` 载入候选:Python 默认的 `.pyc` 只比对源文件的 mtime 与大小,不确认字节码对应当前内容,于"人工批准的、算过哈希的、过了静态筛查的源码"和"实际执行的字节码"可以不是一回事;分开打开文件还留下 TOCTOU 窗口。现在只读一次字节,对同一份内容做完哈希、筛查、编译、执行(不再走 `SourceFileLoader`),`__pycache__` 里的旧字节码没有机会生效。

第 5 轮补记、第 7 轮更正后的细节:
- **模块元数据**:用 `types.ModuleType` 建壳,再手工挂上 `__spec__`/`__loader__`/`__package__`
  (取自 `spec_from_file_location`,但**不调用** `exec_module`)——执行的仍是上面编译好的、经过校验的那份字节。
  相对导入前后都不可用(候选是顶层模块,不在包里),这不是本次改造引入的回归。
  注意 `__loader__` 仍指向 `SourceFileLoader`:候选若自己去调 `__loader__.get_code(__name__)`,
  仍能取到 `__pycache__` 里的字节码缓存——那属于候选自伤,不改变"执行的是校验过的那份字节"这一事实,
  但结论要写准:不是"字节码没有机会生效",而是"**我们执行的不是**字节码缓存"。
- **BOM**:自己读字节之后,UTF-8 BOM 会留在源码里让 `compile` 报 `invalid non-printable character U+FEFF`,
  而旧的 `SourceFileLoader` 会自动剥掉——这是"同一份字节"带来的行为差异。现在用 `utf-8-sig` 解码(哈希仍对原始字节算),
  并且**父入口与子入口走同一个读取器** `read_candidate()`——早先只有子入口改了口径,
  于是同一份带 BOM 的候选在父入口被拒、在子入口却能过(第 6 轮 codex 实测)。
- **审计锚点用原始字节的哈希**:结果里记的 `strategy_sha256` 早先是从 `read_text()` 的文本再哈希,
  而文本模式会做通用换行转换(CRLF → LF),本仓库工作区里的 `.py` 又都是 CRLF——
  于是记录里的哈希与批准记录里的哈希**必然不同**,审计链断在换行上。现在统一用 `rs.file_sha256(path)`。
- **导入期异常走结构化回传**:候选在 import 期报错(缺模块、模块级副作用出错、`raise SystemExit()`)
  不再带 traceback 退出 1,而是按 `{"error": ...}` 回传给父进程,LLM 那条反馈通道才拿得到诊断信息(此前只报"回测失败")。

### 受禁 dunder 与元类
`object.__setattr__(self, "risk_budget", 1e9)` 能在运行时直接改写实例上的风控底线,而属性形式的 `__setattr__` 不在静态筛查原有的赋值检查覆盖范围内,运行时又因"所有 dunder 一律忽略"而漏掉——两层防线同时失效。因此:
- 静态层拒绝**定义、引用或调用** `__setattr__`/`__delattr__`/`__getattribute__`/`__getattr__`/`__init_subclass__`/`__set_name__`,拒绝候选自定义这些 dunder,拒绝自定义 metaclass 与类装饰器;
- 运行时层拒绝候选定义这些 dunder,拒绝候选的 metaclass 与模板不一致。
- `super().__init__()` 不在受禁之列(`__init__` 在受保护成员名单里,单独处理)。

**静态层不是安全边界**:AST 黑名单无法封闭 Python 的动态能力(运行时构造的属性名、字节码、导入期副作用都绕得开)。它的定位是纵深防御的一层,真正的边界是容器隔离(`docker-compose.research.yml`:无网络、只读挂载、`cap_drop ALL`)与"运行前由人读过代码"这两条前提。

**已知残余风险**:
1. 候选若在模块导入期用 `import` 副作用改写模板模块的成员,静态层未必拦得住。
2. 第 5 轮实测的 `update_setting` 绕过、第 7 轮实测的 `__builtins__['setattr']` 绕过、第 7 轮实测的
   `frame.f_builtins.setattr` 与 `unittest.mock.patch.object` 绕过,静态层已**拦截已知写法**,
   执行时也**不再给候选改写属性的能力**(见"执行时的能力隔离")——但它们暴露的是一类更一般的问题:
   **按名字改写实例属性的入口**。彻底解决要把风控底线移出候选实例属性、
   存到候选够不着的引擎侧对象里(模板的 `parameters` 列表里之所以有 `risk_budget`,就是因为底线现在是实例属性)。
   已登记为后续项,不在阶段 2 范围内。
3. 静态层按名字拦,不做作用域解析,保留标识符误伤普通写法(见"保留标识符与误报")。这是有意的取舍。
4. **隐藏子入口仍然信任调用方给的 `--prior-trials`**:子入口只查"非负",不读试验账本,
   所以直接调用 `_evaluate_child --prior-trials 0` 会让 DSR 用比真实累计更小的 N——**放松晋级门槛**;
   而且子入口**自己不写账本**,直接调用可以无限次白嫖评估而不增加试验次数。
   彻底解决要么让子入口以账本为准(而不是信调用方给的数字),要么让子入口自己也记账。
   已登记为后续项(第 7 轮 codex 提出)。
5. **本仓库没有跑全量测试的 CI**:`.github/workflows/` 里只有宪法守卫一条与代码相关的作业,
   全量测试(2249+ 条)只在本地跑。宪法守卫那条作业此前恒红(见下一节),所以"CI 绿"这件事在本仓库里
   从来没有成立过。已登记为后续项:要么加一条装好环境的测试作业,要么明确承认"测试靠本地跑"并写进 README。

## 隐藏子入口的信任模型

`research_api.py` 里的 `_child`/`_gate_child`/`_evaluate_child` 是**隐藏入口**:能被直接调用,
所以不能假设父进程已经查过什么。第 5 轮复核把这条边界补齐了——现在每个子入口自己复查:

| 输入 | 谁负责 | 拒了会怎样 |
|---|---|---|
| `--params` 设置字典 | `child_params()` | 受保护参数(含大小写变体)被拒,退出码 2 |
| `--random-runs` | `child_int_args()` | 超出配置边界 `rc.INT_BOUNDS[("gate","random_runs")]` 被拒(与父入口同一把尺子) |
| `--prior-trials` | `child_int_args()` | 负数被拒——负数会让 evaluate 路径的 DSR 用更小的 N,**放松晋级门槛** |
| `--key` / `--segment` | parser 的 `choices` + `run_child` 里的 `segment_window` | 非法值退出码 2(此前是未捕获异常的 1) |
| `--on-plateau` | parser 的 `choices` | 只接受 `0`/`1` |
| `--file` | `child_file()` | 文件必须在 `pending_strategies/` 内 |
| 审批记录 | `child_approved()` | 未被人审阅过的候选不许跑——`--sha256` 只证明"文件没变",不证明"人看过" |

早先这里只复查了设置字典,其余输入仍依赖父入口——"独立信任边界"这句话当时是说过头的。
子入口的"拒绝"(退出码 2)现在也**原样传到父层**:此前一律被压成"回测失败",
原因只进了本机 stderr,进不了返回给调用方/LLM 的结果里,而那条通道正是为"把诊断送回去"设计的。

### 宪法守卫为什么是一个独立脚本(`tools/check_constitution_history.py`)

守卫本体只用标准库 + git,**不依赖 pytest,也不依赖 vnpy**。早先它寄生在 pytest 用例里,
CI 跑 `pytest tests/test_constitution.py`——而根 `tests/conftest.py` 会 import `segment_ledger` →
`bootstrap` → `vnpy.trader.setting`,在只装 pytest 的 CI 上**收集阶段就失败**(退出码 4),
守卫于是在 CI 上一次都没跑过,却看起来"配好了"(第 6 轮 codex 实测;第 7 轮我本地复现了同样的失败)。
现在 CI 直接跑那个脚本,退出码契约是:0 通过 / 1 发现问题 / 2 环境不可用(浅克隆、git 不可用——同样是失败)。

守卫还多做了两件事:
- **注册表完整性**:源码里新增一个字符串集合型的内置保护常量而没登记进历史比对,守卫会报出来
  (当年 `BUILTIN_PROTECTED_NAMES` 正是这样漏掉的);
- **换模板要同时改白名单**:清单里的 `template` 改了而内置白名单 `BUILTIN_ALLOWED_TEMPLATES` 没跟着改,守卫会报出来。

## 明确不做
无人值守连续迭代(4c,须单独评审)、仪表盘审批按钮、PBO/CPCV、多品种扩展、新增测试段。

## 影响
- 评估结果多出 `n_trials`、`dsr`;已有 8 项门槛语义不变。
- 进化跑得越多,同一家族的 DSR 越难通过——这是预期行为,不是缺陷。
- Bisheng 可被 Dify/LangGraph 替换的条款不变:所有判定逻辑在仓库内的纯函数,flow 只做编排。

## 验证
- 阶段 1:`tests/test_trial_ledger.py`(只增不减、损坏 fail-closed)、`tests/test_dsr.py`(N 增大 DSR 单调下降、已知数值对照)、
  `tests/test_promotion_gate.py`(第 9 条独立触发)。
- 阶段 2:`tests/test_constitution.py`(静态筛查、运行时类校验、清单只增不减、内置保护只增不减——含"删掉一个受保护成员必须被发现"的 mutation 自检)、
  `tests/test_research_sandbox.py`(沙盒路径与筛查)、`tests/test_research_api.py`(三层校验的入口行为、隐藏入口的信任边界、
  `.pyc` 绕过、导入期错误结构化回传)。CI 另有 `.github/workflows/constitution-guard.yml`,直接跑独立守卫脚本
  `tools/check_constitution_history.py`(不装 pytest、更不装 vnpy,`fetch-depth: 0`)。
- 外部复核的原始记录:第 5 轮 codex(`gpt-5.6-sol`)**40.5/50,未通过**;第 5 轮另一轮独立复核(`MiniMax-M3`)**33/50,未通过**;
  第 6 轮独立复核(`MiniMax-M3`)**32/50,未通过**(报出第 5 轮两轮都没报出的 `__builtins__['setattr']` 绕过,
  以及"宪法守卫在 CI 上收集即失败"这一事实);第 7 轮(codex `gpt-5.6-sol` 与独立复核各一轮,结论不一致,
  报出的是"静态黑名单封的是已知写法而不是能力");第 8 轮(codex CLI 深度分析)**36/50,未通过**
  (两条 Critical 都是**实测复现**过的绕过:调用风控执行点、改写受保护对象的内部状态)。
  各轮的发现均已逐条复现后修复并各配了回归测试。
- 第 8 轮新增的回归用例:执行点引用、受保护对象写入与别名、`am.array` 路径、
  运行时第三层(实例状态核对)、`check_params` 嵌套、受禁 dunder、`__slots__`、
  历史守卫的"引用次数减少"与"白名单只减不增"(`tests/test_constitution.py`),
  以及运行时第三层的接线用例(`tests/test_baseline_15m.py`:改 `cta_engine.size` / `guard.max_losses`
  必须抛 `RuntimeError`,不改则正常跑完)。

## 未完成
阶段 1 已实现(Issue #15);阶段 2 已实现(Issue #16,第 5–8 轮复核的阻塞项已修,第 8 轮结果与残余缺口见 `CHANGELOG.md`);
阶段 3–4 未实现;阶段 4 另受 Bisheng 模型配额阻塞(见 `PROJECTWIKI.md`)。
