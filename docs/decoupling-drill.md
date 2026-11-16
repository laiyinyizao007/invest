# 断耦演练记录

外层(毕昇 / 研究网关 / DSH)宕机时,内层交易不受影响。

<!-- BEGIN DECOUPLING DRILLS -->

### 2026-11-15T10:14:54 — 全部通过
- **PASS** 内层不依赖研究侧模块
  - trading/ 下没有任何研究侧 import
  - risk/ 下没有任何研究侧 import
  - strategies/ 下没有任何研究侧 import
- **PASS** 外层容器不持有交易凭据
  - invest-research-gateway 的环境里没有 CTP_* 变量
- **PASS** 外层宕机时内层照跑
  - 探针外层容器: 已起
  - 探针外层容器已停
  - 内层心跳文件: 在刷新
  - 内层策略仍在运行: True
  - 内层进程从不连外层:外层的容器/网络变化与它无关

> 本演练不连真 CTP:证明的是「外层宕机不阻断内层」,不是实盘可用性。

<!-- END DECOUPLING DRILLS -->
