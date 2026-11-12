# ADR: 提前部署毕昇,并先落地研究沙盒的纯逻辑底座(诊断书、参数高原)

- 状态:已采纳(2026-11-12,用户确认)
- 关联:`20261110-bisheng-ai-researcher.md`(本 ADR 对其时序作局部调整,边界与安全约束不变)、`20261109-promotion-gate.md`、`docs/roadmap.md` §2.4 阶段三/四、§2.5、§2.6

## 背景

原定时序是「阶段三 API 固化 → 阶段四接入毕昇」,其理由是:没有固化的白名单端点,边界铁律无法在部署层强制。
但本机毕昇 v3.0.0-beta5 早已部署(2026-10-28,见 `bisheng-rs/bisheng/docker/DEPLOY-LOCAL.md`),开发机资源(22 线程 / 63 GB)满足要求,
用户希望尽早让「研究 → 反馈 → 改进」的飞轮开始转。

## 决策

1. **允许毕昇先于 API 固化部署,但只作为知识库问答使用**:
   - 对外端口只绑 `127.0.0.1`(已落实,bisheng 仓库 `2507a3c522`);Code 节点保持关闭(`workflow.code_node_enabled` 默认 false);版本 ≥ 2.6.0。
   - 部署与交易 compose 分离,不共享网络与 `.env`;毕昇**不接任何工具**,不读 invest 的运行时状态。
   - 知识库 `invest-研究资料` 只含 ADR / 方法论 / 路线图 / 复盘要点(无凭据)。入库时文本会发给所配置的 embedding(SiliconFlow)与摘要(MiniMax)模型,这是已知并接受的外发面。
2. **接工具(阶段四 4b)的前置不变**:研究沙盒 API 白名单端点必须先固化;pending/approved 目录约定与人工批准不变;装载永远由人执行。
3. **先落地不依赖毕昇的纯逻辑底座**(内层,确定性、可单测、不碰实盘路径):
   - `risk/diagnosis.py`:回撤 / 次数 / 连亏 / 夏普四维诊断书,阈值与晋级门槛同源。
   - `risk/plateau.py`:`parameter_on_plateau` 的计算。

## 与原 ADR 的偏离(需要记录)

- **参数高原比 promotion-gate 第 8 条原文更严**:原文只要求「邻域平均得分同样为正」。实现额外要求邻域内至少一半格子自身得分为正,
  否则一个巨大离群点即可把均值拉正。这只会让判定更保守(多拒绝,不会多放行)。
- 登记表 §2.6「`parameter_on_plateau` 的计算来源」由此项提前落地;「参数平原卷积 / PBO / DSR」仍按原触发条件保留。

## 尚未做、且不在本 ADR 范围

- 把晋级门槛接进优化脚本:门槛需要样本外夏普、随机入场分位数、成本翻倍夏普等输入,而现有优化器只产出训练/验证段结果。
  接线要先定义「候选评估」的数据流(哪一段算样本外、随机对照如何跑),单独设计。
- `approved/` 目录与批准流程、`gate` 子命令、15 分钟品种的沙盒回测:待门槛接线设计之后。

## 补充:`scripts/research_api.py`(2026-11-12 已落地 `data` / `backtest` / `results`)

- `backtest` 只接受 `pending_strategies/`(已加入 .gitignore)内的 .py,只能跑训练/验证段(测试段在解析器与代码层双重不可达),必须带 `--confirm-reviewed`。
- 策略在**子进程**中运行:环境变量走白名单(`CTP_*`、`WEBHOOK_*`、`LIVE_*` 等一律不传),静态筛查(禁导入 os/subprocess/socket 等、禁 exec/eval/open/getattr 等)在父子进程各做一次,超时杀整棵进程树。
- **这是纵深防御,不是沙盒**:策略仍以当前用户权限运行,能被刻意绕过。因此 4a 的前提是代码运行前由人读过;结果里记录代码 sha256。LLM 产出在无人读过时运行,需要容器级隔离,是 4b 的前置。
- 结果存 `data/research/<run_id>.json`(含诊断书);`results` 读取。

## 验证

- `tests/test_diagnosis.py`、`tests/test_plateau.py`;诊断书对相同输入恒得相同输出且可 JSON 序列化。
- 毕昇端口绑定以 `docker compose config` 逐端口核对为 `127.0.0.1`。
