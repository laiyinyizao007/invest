# ADR: 复用官方/成熟组件评估——结论与待办

- 状态:已记录(2026-11-16);**登记待办,不排期、不改代码**
- 关联:`docs/adr/20261109-use-vnpy.md`(选 vn.py)、`docs/adr/20261113-evolution-engine.md`(DSR)、`docs/roadmap.md` §2.5
- 档位:研究域(`tools/check_domain.py` 判定 `docs/adr/` 为研究目录)。**本文只记录评估与待办,不声明任何防线的状态**

## 背景

提出的问题:仓库里手写的部分,有多少能换成 vn.py 官方组件或社区已验证的成熟实现?动机是不信任自己写的代码。
评估对象是**手写部分与官方/社区组件的重叠**,不是换框架——vn.py 已经是底座。

## 结论

**现在不动代码,先做策略。** 理由:两个基线策略回测均为负,晋级门槛 fail-closed,
**尚无候选接近通过线**,手写门槛即使有错也暂无后果。下面的待办在「有候选接近阈值」时才变成前置步骤。

## 待办(触发条件驱动;登记不等于排期)

| # | 事项 | 做法 | 触发条件 |
|---|---|---|---|
| 1 | DSR 参考实现对照测试 | 用独立实现作测试里的参考答案,核对 `risk/dsr.py` 数值与论文勘误;**不引入运行时依赖**(保持纯标准库、异常输入返回 0.0) | 任一候选 DSR/夏普接近阈值(DSR→0.95)**之前必须先做** |
| 2 | 夏普口径核对 | 对比 vn.py `BacktestingEngine.calculate_statistics` 与门槛使用的「日收益、未年化」口径;一致则沿用 vn.py 自带,不一致写 ADR | 同 1 |
| 3 | 无头启动流程对读 | `scripts/run_headless.py` + `trading/runtime.py` 对读官方 `examples/no_ui/run.py`(父进程守护 + 交易时段 + 定时拉起),找漏项;纯读代码 | 阶段一收尾 / 阶段二启动前 |
| 4 | 通知库评估(低优先) | 先读 `notify/webhook.py` 列出行为清单(尤其「落盘待补发队列」),再判断 Apprise 能否覆盖 | 维护 `webhook.py` 的成本明显变高时 |

DSR 参考实现候选(只作测试参考,**均为个人小仓库,维护与测试质量未核实**):
`Kodezilla0725/deflated-sharpe`(自称复现两篇论文到四位小数并列 5 处勘误)、`WatchTree-19/overfit`(MIT)、
`CH4RL3I/backtest-overfitting`(仅依赖 numpy/pandas/scipy)。`quantskills/skill-backtest-overfit` 为 GPL-3.0,**不得引入**。
引用前须逐个核对许可证与实现。

## 已评估、明确不做

| 组件 | 不做的理由(2026-11-16 查证) |
|---|---|
| `vnpy_akshare`(社区) | 2 星 / 11 次提交 / 无发布版本;文档自相矛盾(支持列表有期货,使用说明又写不支持期货);仅明确日线;缺依赖或网络失败时**静默返回空结果**,与本仓库 fail-closed 原则相反 |
| `empyrical-reloaded` | 维护者声明已进入维护模式,新功能转到别的项目,不建议新引入 |
| QuantStats | 依赖过重(matplotlib / seaborn / yfinance),不适合进交易环境;现有口径没有缺口需要它 |
| `vnpy_datamanager`(官方) | 下载必须另配数据服务(文档示例为 RQData),不处理连续合约;剩下 CSV 导入与缺口检查,收益有限 |
| PBO | 沿用 `docs/roadmap.md` §2.5 既有「延后」;出现需求时用 CSCV 参考实现做 spike,不自写 |

## 必须保留自有(vn.py 与社区均无对应)

`risk/trial_ledger.py`、`scripts/segment_ledger.py`、`risk/constitution.py`、`risk/live_admission.py`、
`scripts/walk_forward_optimizer.py`(官方无滚动窗口分析)、`scripts/research_sandbox.py`
(静态 AST 筛查是最弱一层,真正的隔离靠容器 `docker-compose.research.yml`)。

## 原则

- 通用件(通知、数据导入、绩效指标)可以换成熟库;**门槛类不盲目换成小众第三方库**——
  用独立实现做**对照**,比替换更可信,也不扩大门槛的依赖面。
- 这些待办一旦涉及 `risk/`、`trading/`、沙盒或依赖变更,按执行域走完整流程(见 `CLAUDE.md` §4)。
- 官方组件盘点(未核实项见下)只是线索,采用前仍要逐项实测:
  `vnpy_riskmanager` 4.2 内置规则只含流控类(委托数/流速/撤单/重复报单),**没有单日亏损或最大回撤**,这两条仍需自写;
  `vnpy_portfoliomanager` / `vnpy_paperaccount` 只看过一行描述,**未读细节**。

## 来源

- vn.py 组织页与模块清单:https://github.com/vnpy
- vn.py 官方无界面示例:https://github.com/vnpy/vnpy/blob/master/examples/no_ui/run.py
- vnpy_riskmanager 4.2 重构:https://www.vnpy.com/forum/topic/34297-veighnafa-bu-v4-2-0-riskmanagerfeng-kong-mo-kuai-zhong-gou-sheng-ji
- 社区 `vnpy_akshare`:https://github.com/lstwzd/vnpy_akshare
- `empyrical-reloaded`:https://github.com/stefan-jansen/empyrical-reloaded
- QuantStats:https://github.com/ranaroussi/quantstats
- Apprise:https://github.com/caronc/apprise
