"""候选进程侧的运行时包。

**候选进程只能看到本包**(宿主把 `research/` 目录加进 `sys.path`,仓库根不在路径上)。

模块清单:
- `bars`:候选侧的 K 线载体(与 vn.py 的 `BarData` 字段对齐,不含任何框架行为)。
- `indicators`:`ArrayManager` 的等价实现(numpy + ta-lib),**与框架侧同一套口径**。
- `signal_template`:候选可继承的基类(`SignalTemplate`)。**不含风控计算**。
- `protocol`:父子进程之间的帧协议(控制面 JSON + 数据面长度前缀二进制帧)。

见 ADR `docs/adr/20261115-candidate-process-isolation.md` 第 3、4.3、5.3 节。
"""
