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
- 风控底线不参与优化,进化只允许重写 `entry_signal` 与 `param_grid`。
- 迭代上限(5 次 / 24 小时)、`max_source_bytes`、网关路由白名单不放宽。
- 流程内任何节点不得写 `approved/`、`promotion_approval.json` 或实盘配置;上线仍须"人工批准 → `record_promotion.py` → SimNow → 另行授权实盘"。
- 测试段(2024-01-01 起)已消耗一次,不得用于选择或进化反馈。

## 明确不做
无人值守连续迭代(4c,须单独评审)、仪表盘审批按钮、PBO/CPCV、多品种扩展、新增测试段。

## 影响
- 评估结果多出 `n_trials`、`dsr`;已有 8 项门槛语义不变。
- 进化跑得越多,同一家族的 DSR 越难通过——这是预期行为,不是缺陷。
- Bisheng 可被 Dify/LangGraph 替换的条款不变:所有判定逻辑在仓库内的纯函数,flow 只做编排。

## 验证
`tests/test_trial_ledger.py`(只增不减、损坏 fail-closed)、`tests/test_dsr.py`(N 增大 DSR 单调下降、已知数值对照)、
`tests/test_promotion_gate.py`(第 9 条独立触发)。

## 未完成
阶段 1 已实现(Issue #15);阶段 2–4 未实现;阶段 4 另受 Bisheng 模型配额阻塞(见 `PROJECTWIKI.md`)。
