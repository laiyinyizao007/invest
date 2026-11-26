"""候选进程隔离用到的**候选侧**代码容器目录。

**这里放的东西会被候选进程 import 到**——所以本目录下的代码必须满足:
- 不 import 仓库里任何框架模块(`risk` / `scripts` / `strategies` / `tools` 一律不许);
- 不含任何风控计算(风控底线物理上不在候选进程里,见 ADR `20261115-candidate-process-isolation.md` 第 5.3 节);
- 不读 `.env`、不读配置、不知道任何本机路径。

**导入名的约定(重要)**:候选进程启动时只把**本目录**(`research/`)加进 `sys.path`,
**仓库根不在候选进程的路径上** ⇒ 候选里写的是 `from candidate.signal_template import SignalTemplate`,
而 `import risk` / `import research_grid` / `import scripts.backtest_lib` 在候选进程里**必然失败**
(ADR 验收标准 A1)。框架进程侧则用 `from research.candidate...` 导入同一份文件(两个进程,两套导入名,不冲突)。
"""
