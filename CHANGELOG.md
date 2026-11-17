# 变更日志（Changelog）
所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。
与 [`PROJECTWIKI.md`](PROJECTWIKI.md) 互相链接;相关 Issue:[#1](https://github.com/laiyinyizao007/invest/issues/1)。

## [Unreleased]
### Fixed（修复）
- **导入守卫把仓库内 venv 的站点包当成「仓库内模块」误杀**(Issue [#47](https://github.com/laiyinyizao007/invest/issues/47),
  新 ADR `20261115-import-guard-site-package-classification`)。`RepoImportGuard` 的 docstring 写了三条规则,
  其中第三条是「在仓库根**之外**(标准库、站点包)→ 也必须『候选开始运行之前就存在』」,
  而它的理由(第 24 轮 codex 记的)是「站点包在宿主机上通常可写,候选往里写一个辅助模块就能绕开仓库内的路径规则」。
  **但第三条是按「物理上是否在仓库根之下」实现的**——本仓库的 venv 就在仓库根之内(`.venv/`),
  于是站点包里的模块落进第一条被直接拒掉,第三条那条更宽松也更正确的规则**从来没被应用过**。
  - 现象:在主 checkout 里用仓库自带 venv 跑研究侧测试,`tests/test_research_api.py` 有 **4 条红**
    (候选侧报 `禁止导入仓库内模块 vnpy_ctastrategy:它不在允许的目录 ['strategies'] 内`——
    `vnpy_ctastrategy` 是站点包,不是本仓库的代码)。**CI 永远看不到**:CI 现装依赖,
    站点包在 checkout 之外,走的是第三条。
  - 修法:判据改成「**这是不是本仓库自己的代码**」——新增 `site_package_roots()` 算出当前解释器的
    站点包根(**只保留真的是 site-packages/dist-packages 的**;Windows 上
    `site.getsitepackages()` 返回的是 prefix 本身,照单全收等于把整个 venv 当信任区),
    新增 `snapshot_site_files()` 拍一份「路径 → (mtime, size)」快照(只 stat 不哈希:
    一个 venv 的 site-packages 有 8 千多个 .py、几百 MB,逐个哈希实测 >120s,只 stat 是 1.6s);
    `_is_site_package()` + `_outside_repo()` 统一四处分类判定(模块、祖先包、包位置、通配符)。
  - **基准必须来自树外**:快照由**父进程**拍(`research_api.site_snapshot_path()` → 临时文件 →
    环境变量 `RESEARCH_SITE_SNAPSHOT` → 子进程),因为**父进程不是候选执行的**,
    快照因此不在候选的控制之下。守卫是**每个子进程重建一次**的,子进程自己拍的快照会
    **包含上一轮候选写进去的文件**——那就是候选自己的「自证」。实测:候选第 1 轮往站点包
    写辅助模块,第 2 轮就能导入并拿到真内建。仓库对受信目录正因如此才用 **git 提交基准**
    (树外);站点包在树内,唯一树外的基准就是父进程的快照。
  - **踩过的坑(如实记录)**:第一版曾用 **pip 的安装记录**(`*.dist-info/RECORD`)当基准,
    但它同样在**候选可写的树里**——能写模块的攻击者也能改那份给它背书的 RECORD。
    实测:`RECORD` 条目写成 `atk.py,,`(哈希与大小留空)时两个校验全被跳过 → **零校验放行**;
    带正确 sha256 的伪造、以及「改包 + 同步改 RECORD」同样放行——**那道检查是空转的**。
  - **快照文件本身也在候选可写的临时目录里**:父进程因此**每轮起子进程前用内存里的原始
    快照重写一次**(候选上一轮的篡改被抹掉),并把**内容的哈希**经环境变量一起下发;
    子进程**只读一次**文件(同一份 bytes 既算 sha256 又解析 JSON,消除 TOCTOU),
    **哈希缺失或不匹配就当「没有基准」→ fail-closed**(少一个环境变量不能变成「跳过校验」)。
  - 变异验证:五个变异体(恒 False / 恒 True / 去掉 mtime 比较 / 去掉 size 比较 /
    哈希核对改成不核对)逐个施加,**每个都至少让一条用例变红**。原先三条能存活,是因为
    「被篡改」用例同时改了 size 与 mtime、伪造快照用例填的是 `mtime=0`——**断言因错误的
    原因变绿**;补了三条各自只触发一道比较的用例才杀掉。
  - **第 4 轮 codex 复核**:上一轮的三条(父进程现读磁盘算哈希 / 子进程读两次 / 哈希缺失
    就跳过校验)**全部真修好**;新发现两条已处置:①父进程每轮就地重开快照路径会**写穿硬链接**
    (改成写临时文件 + `os.replace()` 原子替换);②子进程拿不到父进程快照时的退路在子进程里是
    **fail-open**(`_child` 是注册了的 CLI 子命令,直接调用就落进去)——子进程入口一律要求
    父进程下发快照,拿不到就拒绝(退出码 2)。
  - 新增 `tests/test_import_guard_venv_site_packages.py`(17 条),按仓库自己的标准写成
    **动态探针**(真的写、真的走 `safe_builtins` 导入路径、真的看结果),覆盖跨轮次与改写两种
    攻击面;并按 codex 的意见做成**不依赖实现里的新 API**(快照文件自己写),
    这样同一份用例在修复前也跑得起来,红的原因是「防线行为」而不是「用例报错」。
  - 新增 `tests/test_import_guard_venv_site_packages.py`(9 条),按仓库自己的标准写成
    **动态探针**(真的写、真的导、真的看结果),覆盖跨轮次与改写两种攻击面。
  - **这是收窄分类,不是放宽防线**:站点包那一支本来就带「预先存在且此后没被改过」的检查,
    修复只是让分类对得上 docstring;仓库自己的代码(`risk/`/`scripts/`/`trading/`/`strategies/`)
    仍然落进「仓库之内 → 只许在允许目录里」那一支,一道检查都没松;
    判不出站点包时退回原来的字面口径(fail-closed)。
  - 验证:新增 `tests/test_import_guard_venv_site_packages.py`,**放行与拦截各配一条**
    (仓库内站点包预先存在→放行;仓库自己的 `risk/evil`→仍拒;候选运行期间往站点包写的模块→仍拒)。
    **同一份用例在修复前是红的**,红的理由正是那道误分类,不是「用例跑不起来」。
    另在临时 checkout 里造出「站点包真的解析到 checkout 根之下」的布局实测:修复前 4 条红,修复后 98 全绿。

### Added（新增）
- **录制已起(2026-11-16 23:11,SimNow 第一套前置)**:`.env` 补上持久化的 `CTP_RECORD_*`
  (profile=simnow、合约 `C2701.DCE`+`MA701.CZCE`、Tick+1 分钟、独立库 `.vntrader_record`)。
  自愈层用**用户级守护进程**(每 10 分钟 `ensure`;Windows 计划任务注册被拒 `0x80070005 Access Denied`),
  实测 `ensure` 正确识别已有进程(`already_running` 事件),不重复拉起。
  **一条必须写进手册的观察**:玉米/甲醇夜盘 21:00–23:00 收盘后,`last_tick` 停在 22:59:59.5、
  与本机差 762 秒 → 超 `filter_window` → 状态必然是 `degraded`、tick 被丢弃。
  **这不是故障,是收盘**;分辨法=先核对本机时钟(本次对百度/微软/Akamai 三个源,误差 <2 秒),
  再看 `last_tick` 是不是整点整秒的收盘时刻。下一个时段(日盘 09:00)自动开录。
  ADR `20261111-market-recording-and-dsh-orchestration.md` 选的 dsh-cron 条目**从未接线**(runbook §5 第 3 步未完成),
  该 ADR 待补一句说明现状。
- **录制常驻化与数据扩充方案**(`docs/plans/20261116-recording-and-data-plan.md`;研究域)。
  把「玉米上不了实盘」的瓶颈**定量**了:晋级门槛里与样本量耦合的三条(往返 ≥150 /
  样本外夏普 ≥1.5 / 夏普 95% CI 下界 >0)中,第三条要求 `夏普 > 1.96/√样本外年数`——
  **现在只有 1.5 年样本外,这条要求的夏普(1.96)已经高于门槛本身的 1.5**,
  两条几乎重合,可行区间窄到 (1.96, 3.0]。样本外年数每翻倍,这条的要求降 √2:
  3 年 → 1.39、4 年 → 1.20、10 年 → 0.76。
  → **目标定为 4 年样本外**(真实夏普 ≥1.0 的策略就有机会过 CI 关);三条路径按杠杆排序:
  ① **补购更长历史**(最高杠杆:不是加长尾巴,而是把验证窗从 1.5 年整体拉到 4 年,
  一次采购几周到几个月见效)② **CTP 自录**(只往前长不快,但它是**唯一能拿到真实成交与
  真实费率**的路径——`docs/live-readiness.md` §A 上这两个格子现在还空着)③ 多品种(第二条腿,
  且 DSR 会因试验次数变多而更严)。
  同时定了「加长时间比加密交易划算」的量化理由:往返数与样本外长度是同一个约束的两种说法
  (基准约 0.4 笔/交易日),加密交易要用成本和过拟合自由度去换。
- **后果域分档:研究档 / 执行档的项目级流程**(Issue [#35](https://github.com/laiyinyizao007/invest/issues/35);
  ADR `20261114-consequence-domain-tiers`)。策略研究的改动规模与生产代码一样,
  却付了同一份流程税(plan.md + plan-down、结构化确认块、6/6 完备性清单、Issue + worktree、
  双轮 codex、文档原子提交),而它的产物**可以重跑、失败只影响结论、不动钱**。
  本次新增仓库根 `CLAUDE.md`,按**后果域**分档,而不是按改动规模分档:
  - **研究域**(只影响研究产物):跳过 P2 plan.md 与 plan-down、结构化确认块、6/6 清单、
    Issue + worktree;G8 只省第二轮(第一轮照做);G1 降级为 CHANGELOG 记一笔;
    G9 沿用最低 70%;G14 只登记越出研究域的部分。**ADR 记录与相关既有测试照做。**
  - **执行域**(可能影响真实委托/持仓/资金、风控底线、实盘准入批准链、凭据或部署):
    全局全套流程照旧,本文件不提供任何减法。
  - 两档都**不适用**的规则(不论档位照做):G3 意图边界、G5 安全合规、G7 脱敏、G4 一致性、
    G8「两轮都做」、G12 学习闭环、**代码内的全部实盘防线**(不改任何阈值)、CI 闸门。
  - **判据由代码执行**(`tools/check_domain.py`,仅标准库):按路径清单把每个文件判成
    研究域 / 不可降级 / 执行域,退出码 `0` 全研究域 / `1` 含执行域 / `2` 环境不可用
    (fail-closed:判不了等于不放行)。**默认拒绝**:不在白名单里的一律执行域。
  - **清单是实测出来的,不是拍脑袋写的**:`tools/verdict_graph.py` 用**函数级** AST 分析
    算出每个文件「自己定义或直接调用了哪些判定点」(`check_gate` / `check_live_admission` /
    `write_approval` / `record_approval` / `screen_source` / `check_instance_state` /
    `check_class`)。实测推翻了直觉:名字带 research 的 `scripts/research_sandbox.py` 是
    **批准账本的唯一写入者**;`risk/candidate_eval.py` **直接调 `check_gate`**;
    `scripts/backtest_lib.py` 回测跑完**核对风控底线**;`scripts/research_api.py` /
    `research_approval_server.py` / `research_gateway.py` 是批准与筛查链的入口。
    另有**数据流**上的耦合 AST 看不见(`risk/dsr.py` / `risk/plateau.py` /
    `risk/trial_ledger.py` 是门槛第 9/8 条的输入),靠人工清单兜住——**图找得到的归图,
    图找不到的归清单**。
  - **清单有动态探针盯着**(`tests/test_check_domain.py`,99 条):两道**全仓不变量**
    ——①**调用关系**:任何自己定义或直接调用判定点的文件必须判为不可降级
    (**含尚未入库的新文件**);②**数据流**:不调用判定点、但**值被喂进**判定点的模块
    同样不可降级(实测例子:`research_api.py` 从 `walk_forward_optimizer` 取
    `wf.DATA_FILE` / `wf.build_windows()`,改它 = 改门槛的评估窗口与数据)。
    另有:研究白名单不得拥有判定点、生产 import 闭包不得与白名单相交、
    以及**探针自身的准确性夹具**(既不漏报也不误报——误伤的探针等于没有探针)。
  - **探针经变异验证**(十一种清单改动,每次都被抓住,条数由脚本实测):删 `trading/` 条目、
    把 `risk/live_admission.py` / `risk/candidate_eval.py` / `research_sandbox.py` /
    `backtest_lib.py` / `walk_forward_optimizer.py` / `research_grid.py` / `research_api.py`
    移出不可降级、把 `scripts/optimize.py` 移出研究白名单、把整个 `scripts/` 放进研究**目录**白名单、
    新建一个直接调 `check_gate` 的文件。变异验证是**可复算的脚本** `tools/check_mutations.py`
    (复制仓库到临时目录、逐个施加变异、数 pytest 的 FAILED 行数);`--verify` 还会核对
    ADR 表格里的数字。
  - CI:`research-guard.yml` 的 ruff 清单与测试清单已纳入这四个新文件,
    另加一步**分档器冒烟**(真实 CLI,`PYTHONIOENCODING=cp1252` + 退出码语义)与
    **变异验证**——只有单元测试的话,CLI 那条路径与变异台架在 CI 上从没被跑过。
  - **第 1 轮 codex 复核的处置**(2 High / 5 Medium / 6 Low,逐条独立复现后):
    ①`walk_forward_optimizer.py` 曾被判研究域,但它提供门槛评估的窗口与数据文件
    ——已移入不可降级,并**新增数据流探针**自动覆盖这一整类,不再依赖人手写断言;
    ②`verdict_graph` 对 `from X import f as g` 后调 `g(...)` 的形态**漏报**——已修
    (裸名字分支查别名表),并补了三种调用形态的夹具;
    ③未跟踪**目录**里的文件不被扫描(`git status` 缺 `-uall`)——已改;
    ④「生产闭包不相交」的模块解析器不认 `scripts/` 裸模块导入,`scripts/bootstrap.py`
    漏在闭包外——已统一口径(闭包 17 → 18 项);
    ⑤文档三处不实(条数、校验范围、点名了没有该调用的文件)——已订正;
    ⑥分档判定在 CI 里没有机器强制点——已加冒烟关卡;
    ⑦Windows cp1252 控制台上 CLI 直接崩(CLAUDE.md 文档的命令在主力开发环境不可用)
    ——已加 stdout/stderr 重配,并补了非 UTF-8 控制台的回归用例。
  - **第 2 轮 codex 复核的处置**(第 1 轮的 12 条经独立复现全部真修;本轮新发现
    1 Critical / 3 Medium / 5 Low,同样逐条独立复现后处置):
    ①**Critical:新加的 CI 冒烟关卡在两条触发路径上都会让 CI 永久变红**——
    `pull_request` 事件下浅克隆没有 `origin/main` → `git diff` rc=128 → 分档器 exit 2;
    push 到 main 时 base==HEAD、diff 为空 → 同样 exit 2。根因是「没有改动」与
    「环境不可用」共用退出码 2。已改为:checkout 加 `fetch-depth: 0`;
    `--diff` 成功但**改动集为空** → 打印说明并 **exit 0**,只有 git 读取失败才 exit 2。
    ②数据流探针对 `from X import DATA_FILE` 这种**函数式 import** 漏报(只认模块别名)
    ——已修,并补了三种 import 写形的夹具;
    ③数据流探针**过度包含**(读一个跟门槛无关的 helper 也被算成门槛输入,
    有把研究文件强制升级、**侵蚀研究档**的风险)——已收窄为排除「语句位置的
    纯副作用调用」,provider 集合 25 → 24 且无关的 `research_console.py` 已不在其中;
    ④ADR 变异表三行数字不可复现、一行**变异描述写反**——变异验证改为**可复算的脚本**
    `tools/check_mutations.py`(复制仓库到临时目录、逐个施加变异、数 FAILED 行数),
    ADR 表格按脚本实测重写,`--verify` 可核对;CI 里也加了这一步;
    ⑤CLI 改吐 UTF-8 后,守护 CLI 契约的用例按 locale 解码 → stdout 变 None → **空跑通过**
    ——已加 `encoding="utf-8"`,并断言 stdout 里确有「研究域」「不可降级」;
    ⑥「文档条数必须是真的」那条断言按行且会误报第二个「N 条」——已收紧,
    并补验「条数写错 / 条数被挪走 / 条数被删」三种情形都变红;
    ⑦目录前缀的「拼写近似」检查对 8 个前缀里 6 个**不可达**——已换成**穷尽**判据
    (要么是真实存在的目录、要么是 gitignore 的运行时目录,两者都不是即写错);
    ⑧ADR 的 provider 清单写「10 个」却列了 11 个名字且含不在集合内的条目——已按实测重写。
  - **第 4 轮 Sourcery 复核的处置**(2 条实质发现):
    ①**fail-open 口子**:`python` 遇到未捕获异常时退出码是 **1**,而 1 在本工具的语义里
    是「判为执行域」(**正常结果**)——于是「分档器自己崩了」与「分档器说这是执行域」
    在 CI 眼里完全一样,崩了也照样放行。已改:CLI 入口把任何未预期异常归到 **2**
    (环境不可用,fail-closed);CI 的冒烟关卡除退出码外还要求输出里有「总判定:」这一行。
    语义自洽了:**退出码 1 = 我确实判出了执行域;退出码 2 = 我没能给出判断**。
    ②「削弱型改动一律升档」只列了具体调用、没有总则——已改为先立总则
    (少做一次检查 / 把「拒绝」改成「告警后继续」/ 阈值调松 / fail-open /
    让不可能变可能),已知易踩处只作示例,并写明「拿不准 → 升档」。
    另有三条 subprocess 安全告警经核为误报(实参全是本地常量、不经 shell),已在代码里写明。
  - **合并到 main 之后才暴露的缺口**:#38 合并后 main 上的 Research Guard 直接变红
    (PR 的 CI 是绿的)。根因:冒烟关卡要求输出里有「总判定:」这一行,但
    **「没有改动」那一支没打印它**——而那一支正是 **push 到 main** 时走的
    (base==HEAD、diff 为空)。PR 路径永远有改动,这条分支**从没被走到**。
    本地推理说「push 到 main → exit 0」没错——**退出码对,输出形状不对**。
    已修:那一支也打印「总判定: 无改动(无需分档)」,让每次成功运行都以结论行收尾;
    并补了回归用例。**「合并前全绿」不等于「合并后全绿」**。
  - **本决策不削弱任何一道代码内的防线,也不改它们的阈值**;它只减少改动的**审批成本**,
    不减少缺陷的**检出能力**。

- **闭合沙盒状态机:`approved/` 目录 + 收菜动作 + 运行时装载 fail-closed**(Issue [#34](https://github.com/laiyinyizao007/invest/issues/34);
  ADR `20261115-approved-sandbox-closure`)。**这轮补的是整条链上最要紧的一处断裂**:
  阶段四的状态机「pending → 人工批准 → `approved/`(runtime 只认)」此前只落了前半段——
  `approved/` 目录根本不存在,批准只往账本写一条记录,**没有任何下游消费它**。
  于是批准过的代码与没批准的代码在运行时受到同样的对待,批准沦为流程仪式而不是授权。
  - **收菜**(`scripts/research_collect.py`,**独立脚本**:网关 import `research_api`,但 import 不到收菜模块,
    端点白名单里也没有它——外层只能请求批准,不能请求收菜、更不能自己批准):
    四道校验缺一不可——① 源文件当前字节的 sha256 **必须在批准账本里**;② 人确认之后**再算一次** sha256
    (防「批准后文件被换掉」的 TOCTOU);③ 写入 `approved/` 之后**复算**目标文件 sha256
    (复制必须逐字节一致,不一致就删掉并拒绝);④ 目标路径固定 `approved/<原文件名>`,不接受任何路径参数。
    写盘用「唯一临时名(pid + uuid)+ `os.replace` + Windows 重试」,与 `record_approval` 同款。
    人的门槛与批准一致:交互式终端 + 粘贴**完整 64 位**哈希(敲 `y` 不算——任何程序都能喂一个 `y`)。
  - **运行时装载**(`risk/approved_guard.py` + `trading/runtime.py`):装载时做四件事——
    位置(必须在 `approved/` 内,解析后的父目录就是它)、授权(账本命中)、合规(宪法静态筛查 + 设置里不得夹带
    受保护参数如 `risk_budget`)、同一性(哈希、筛查、编译、执行基于**同一份字节**;只读一次字节再编译执行,
    `.pyc` 没有机会生效)。任何一项不过 → `EXIT_STARTUP_FAILED`(3)并告警,**绝不降级**到 `strategies/` 里的默认策略
    (回退等于「装不上就换一个照样跑」,那批准就白批了)。装载点选在**连网关之前**:没过批准复核的策略不该登录 CTP。
    `RuntimeConfig.approved_file` 等四项**全为 `None` 时行为与从前完全一致**。
  - **批准账本共享只读模块**(`risk/approval_ledger.py`):研究侧写、运行侧读;写入口仍然唯一在
    `research_sandbox.record_approval`,共享模块里**没有任何写入口**(AST 测试锁住)。
    研究侧的读取改为委托,异常类型与文案**逐字不变**(既有调用点靠它识别拒绝)。
  - **第四层跟着新执行入口走**:宪法历史守卫(`tools/check_constitution_history.py`)要求任何执行候选代码的入口
    必须在**同一个函数体**里自己成对取快照并比对——`approved_guard` 因此在 `exec` 前后各拍一次受保护对象的身份指纹。
    这条不是新规矩,是既有守卫**第一次拦住本 PR 的新代码**后按要求补齐的。
  - **骨架契约独立单测**(`tests/test_strategy_skeleton_contract.py`):roadmap 要求「策略骨架模板由人维护、
    有独立单测」——骨架事实上早已存在(宪法 template 指向它),缺的是这份独立单测。
  - **断耦演练**(`scripts/decoupling_drill.py`,证据 `docs/decoupling-drill.md`):阶段四 4b 的退出条件之一。
    三项全过:内层(`trading/`/`risk/`/`strategies/`)不 import 任何研究侧模块、外层容器环境里没有 `CTP_*`、
    **外层停掉时内层照跑**(心跳继续刷新、策略仍在运行)。演练只停**自己起的一次性探针容器**,
    不碰正在服务别人的网关容器。⚠️ **不连真 CTP**:证明的是「外层宕机不阻断内层」,不是实盘可用性。
  - `approved/` **不入库**(未经审查的代码不进版本库);`docker-compose.yml` 挂载它与只读的批准账本。
  - **如实记录的取舍**:宪法第三层(运行**途中**的实例级复检)在运行时装载里**没有**完整实现——
    静态层已禁止候选引用 `guard`/`trail`/`cta_engine`,本轮只在**装载期**做了完整指纹比对;运行途中的持续核对留待评估。
    另一条更要紧的:**运行时装载与研究侧存在能力不对称**——研究侧执行候选用受限内建 + 按路径的导入裁决,
    而运行时的 `exec` 用完整真实内建、且跑在持有 CTP 凭据与交易会话的进程里(交易进程**不是容器**,
    实盘在 Windows 运行机上直跑)。于是 LLM 产出的候选一旦被批准,权限与人工写的 `strategies/` 代码相同。
    本轮**没有**在运行时复用受限内建(语义是为研究沙盒设计的,且会抹掉研究/交易的分层),
    改为在 ADR 里写明,并把「运行时装载前须有晋级门槛通过记录」列为遗留的下一道门。
  - **收菜默认拒绝覆盖已存在的另一版本**(要换版本须显式 `--replace`,旧版本留成 `<名>.prev`):
    覆盖等于换掉「运行时正在认的策略」,是另一件需要显式授权的事。
    收菜时**把源码打出来**让人真看一眼:批准那一步只给大小+哈希(`review` 才给源码),
    而本轮把批准的效力提升到了「运行时执行权」——批准环节的强度也得跟上。
  - **状态命名空间按批准哈希派生**(`approved_<sha8>`):换批准版本自动换命名空间,
    旧版本的持仓/止损状态不会被新版本继承——不靠操作者记得改 `--strategy-name`。

- **第 1 轮 codex 复核(32/50)实测出四处问题,已逐条修掉**(Issue [#34](https://github.com/laiyinyizao007/invest/issues/34);
  ADR `20261115-approved-sandbox-closure` §10~§14):
  - **[critical] 第四层可被候选自己关掉**:原先在 `exec` **之后**才经 `const.` 取比较器,
    候选可以 `const.check_protected_state = lambda *a, **k: []` 把它换成空函数 →
    结论恒为「没被改写」→ **第四层静默失效**(实测:静态层对这条载荷返回 [],不报)。
    改为:比较器与快照器在 exec **之前**绑定成同名局部引用,用后**再核对**模块上的属性没被换过。
  - **[high] 运行时装载漏了宪法的类级校验 `check_class`**:静态层只按名字查黑名单,
    候选可以重写 `on_daily_bar`(日线处理的唯一入口)/ `variables` / `daily_end`
    这类**不在受保护名单里**的模板成员。研究侧有这层,运行侧原先漏了 → 研究侧拒的候选运行侧会收。
  - **[high] 宪法默认模板是「仅研究回测」的 15m 骨架**(`on_tick` 直接抛异常),
    候选必然继承它 → 实盘第一根 tick 上抛异常并被引擎静默停掉(失败方向安全但信息误导)。
    骨架加 `research_only` 标记,装载器看到就拒绝;实盘骨架用 `--approved-template` 显式选
    (走宪法的内置白名单)。
  - **[high] `--history` 与 `check_params` 冲突**:`history_vt_symbol` 是操作参数(预热用哪个历史合约)
    不是风控底线,却与底线一起被拦掉 → **实盘的正常配置(带历史预热)永远装不上**。
    装载时只校验底线那三个参数。
  - 另修:授权来源钉死(删 `--approvals-file`;`--approved-dir` 必须解析到 `<root>/approved`;
    装载前跑批准目录整体自检);收菜只读一次字节(哈希/筛查/落盘同一份);
    文件名规则与运行时装载共用一份常量;`docker-compose` 的 approved 挂载改只读。

- **第 2 轮 codex 复核(41/50)实测出六条,已逐条修掉**:
  - **[high] 一次被拒的候选能永久毒化闸门,同进程第二次装载完全裸奔**:载荷 #1 关掉五道闸并被拒,
    载荷 #2 直接 LOADED、300 元单笔预算被永久换成恒 0 的函数。
    修法:① 所有闸门函数在**模块导入时**绑成常量,装载时用的始终是「导入时那个真函数」;
    ② 装载前后逐一核对「模块上的属性还是不是那个真函数」——
    **比对结论可以被伪造,但属性身份核对不能**;③ 发现被改写 → 进程标记为**已中毒**,
    之后任何装载一律拒绝。
  - **[medium] `del const.check_protected_state` 让 AttributeError 逃出装载器** →
    运行时崩溃 + 退出码 1 + **零告警**。改成 `getattr(..., None)` 比对,运行时兜一层
    `except BaseException` → 统一「拒绝 + 告警 + 退出码 3」。
  - **[medium] 候选 `raise SystemExit(0)` → 进程以退出码 0 静默结束**:
    `restart: on-failure` 不重启、监控看着像「正常停止」、零告警。exec 的兜底改成 `except BaseException`。
  - **[medium] 一条回归用例是「走过场」的**:载荷不是骨架子类,「不是策略骨架的子类」
    先一步拒绝,断言照样成立。载荷改成真骨架子类,并断言拒绝理由里出现「闸门」或「受保护对象」。
  - **[low] 筛查/参数/类级三道闸退回内置默认宪法** → 清单文件里追加的保护项对**运行侧**不生效。
    改成统一用 `const.load()` 的结果并显式传参。
  - **[low] 公开 API 的 `template=` 参数绕过骨架白名单** → 删掉,骨架只能按名字选(走白名单)。

- **第 3 轮 codex 复核实测出 critical 级绕过,已修,并把「修不掉的部分」如实登记**:
  - **[critical]「换属性 → 调用 → 复原」完整绕过**:候选把 `check_protected_state` 换成
    「**被调用时把原函数装回去**并返回 `[]`」的假函数 → 指纹结论恒为空、事后身份核对看到的是
    「真函数」→ 两条检查**同时**被骗过,装载成功、300 元单笔预算被换掉。
    修法三条一起上:① 闸门函数在 exec **之前**绑成局部引用(候选的代码在另一个 module dict 里,
    改不到);② 装载前后逐一核对属性身份(清单用默认参数绑死,免得清单本身被换);
    ③ **静态层先封**——新开 `BUILTIN_PROTECTED_GATE_ATTRS`,候选写这些名字、
    或用 `setattr(obj, "名字", …)` 按字符串写,在 exec 之前就被拒。
  - **[medium] 闸门被 `del` / 被置 None 时裸异常逃出装载器** → 装载器整体包成「失败模式只有一种」。
  - **[medium] 闸门清单不完整**(漏 `const.load` / `load_template` / `BUILTIN_ALLOWED_TEMPLATES`)→ 补齐。
  - **[low] 参数校验「先过滤再校验」对未来的受保护参数 fail-open** → 改成对**整个** setting 校验,
    只对 `history_vt_symbol` 这一个**具名**操作参数豁免。
  - **ADR 更正**:原先写「第四层候选拆不掉」「属性身份核对不能被伪造」——与代码不符,已改。
    如实写明:第 3 轮 codex 实测 `sys._getframe(1).f_locals['check'] = fake`
    (Python 3.13 的 PEP 667 写穿)能**直接改装载器的局部变量**,
    所以在运行侧(同进程、完整真实内建)执行任意 Python 代码**做不到安全**。
    真正的边界仍是「人工审批 + 隔离」,而**运行侧至今没有隔离**(研究侧有受限内建 + 容器)。

- **外层 LLM 端到端联调已通**(不在本 PR 的代码里,是本机毕昇工作流 v3 的进展):
  模型额度恢复后实跑——想法 → LLM 起名 → LLM 写候选 → 提交 → 静态筛查 → **停在「请本机审阅批准」**。
  中途发现 LLM 连着两轮都写出「把 `am.close` 的切片传给函数调用」这种容器逃逸写法
  (静态筛查正确拒绝:切片是**视图**,拿到之后可以原地改写、伪造回测结果);
  把这条硬约束写进提示词后一次写对。
  → **批准那一步仍然必须由人做**(模型不能代替),这正是本 PR 让批准成为「真正的授权」的意义所在。

- **装载门与准入门拆成两份独立的记录**(Issue #37;ADR §22):
  - 准入门 `data/state/promotion_approval.json` 问「这个账号能不能连生产 API」;
    装载门 `data/state/approved_promotion.json` 问「这份**代码**配不配被装载执行」。
    共用一份记录会让「过了一次门槛」同时满足两件事,而且没法单独撤销其中一件。
  - 装载门那份额外绑定**品种 / 字节 / 那一次评估**(`key` + `sha256` + `run_id`):
    同一份代码换个品种不通用、换个文件不能蹭记录、数据一更新同一次通过的含义就变了。
    品种**从那次评估的结果文件里取**(`data/research/<run_id>.json` 的 `key`),
    人手填的 `--key` 与评估结果不一致时拒绝(fail-closed)。
  - 装载时四样都要对上:记录存在 → `gate_failures == []` → sha256 与要跑的字节一致 →
    key 与运行时声明的品种(可选 `--approved-key`)一致。
  - **今天没有任何候选通过过晋级门槛 → 装载门记录不存在 → `approved/` 装载这条路是关着的。**

- **合并 main 的「后果域分档」并按新分档器补齐不可降级清单**(Issue [#35](https://github.com/laiyinyizao007/invest/issues/35);
  ADR `20261114-consequence-domain-tiers`)。main 上新增了项目级 `CLAUDE.md`(按后果域分档)、
  `tools/check_domain.py`(机器可判的分档器)、`tests/test_check_domain.py`(99 条动态探针)与
  变异验证关卡。本分支的改动按新分档器判定为**执行域**——`risk/live_admission.py` 与
  `scripts/record_promotion.py` 属真钱链路判定点、`trading/` 与 `scripts/run_headless.py` 接真实委托
  ——而这一轮全程走的就是执行域流程(P2 的 plan.md 经 plan-down、结构化确认块、6/6 清单、
  Issue + worktree、G8 四轮 codex、文档同原子提交)。
  → 分档器的探针随即指出:本轮新增的 `risk/approval_ledger.py`、`risk/approved_guard.py`、
  `scripts/research_collect.py` 与三个用例文件**拥有判定点或喂值给判定点**,却没被判为不可降级
  → 已按新 CLAUDE.md §4 规则 6(清单本身是代码)补进不可降级清单。变异验证 11 种变异全部被抓住。

- **第 4 轮 codex 复核(35/50)的发现与处置**:
  - **[critical] 帧劫持**:候选可用 `getattr(sys._getframe(1), 'f_' + 'locals')['check'] = fake`
    直接改装载器的局部变量(PEP 667 写穿),第四层能被完全中和。
    → 修法:**候选的 exec 放进独立线程**——线程里 `sys._getframe(1)` 拿到的是那个线程的帧,
    够不到主线程的局部变量。帧劫持、闭包单元、`__code__` 改写这一类够不到主线程的招被挡住。
  - **[high] 宪法风控底线可被已批准的候选用 `setattr`/`getattr`/`vars` 绕过**(H1)。
    → 处置(第 4 轮 codex 建议的折中):**把「运行时装载候选」这条路默认关着**——
    装载现在要求三道**互相独立**的记录:
    ① 批准账本(人读过代码)、② **已收菜**记录(收菜那道把源码打给人看的关口,
    人工 `cp` 绕不过)、③ **晋级门槛通过**记录(且其中的 sha256 与要跑的那份字节相同)。
    **今天没有任何候选通过过晋级门槛 → 这条路实际关着**(见 Issue #37),
    H1 那个洞因此到不了实盘。要打开它,先做完 #37 与运行侧的能力隔离。
  - **[medium] 静态层「先封」名不副实**:只挡 `x.attr = v`,实测 `AnnAssign`/元组解包/
    `AugAssign`/`del`/`setattr`/`delattr`/`vars()[...]` 全都绕得过。
    → 七种形态一次收进 `_gate_attr_violations()`。
  - **[low] 闸门清单是可变 dict**:改成不可变 tuple + 默认参数绑定(候选一句
    `_g._CANONICAL_GATES = {...}` 就能换掉它)。
  - **[low] 清理**:删死常量 `RISK_FLOOR_PARAMS`、改与实现相反的 docstring、
    修一条空转断言、改一条过期注释。
- **合并 main #22 进 #26 分支:两边沙盒防线取并集**(Issue [#26](https://github.com/laiyinyizao007/invest/issues/26),PR #27;
  ADR `20261113-evolution-engine` 的「合并 main #22 的取舍」一节)。main 的 DSH 研究入口(#22)与本分支都在加固同一个沙盒,
  且各自重写了"导入门"。合并后**两边的防御都在,没有为省事丢掉任何一边**:
  - 导入门 = 黑名单(`DANGEROUS_MODULE_NAMES` 等)→ 按全名禁用 → **按路径裁决**(#26)→ **导入白名单**(main)→ 配置追加模块的**准入审计**(main);
    顺序刻意如此:同一个名字被几条规则同时拒时,报的是**最具体**的那条(路径规则说得出"为什么"),不是笼统的"未列入白名单"。
  - 候选拿到的是 main 的代理 `_GuardedModule`(名字层:下划线/泄漏属性名,抛 `ImportError`),
    它的取属性**同时**走 #26 的对象层判据(危险模块/能力来源,抛 `AttributeError`);两层共用 `_check_object_rules`,不各自漂移。
  - `FORBIDDEN_ATTRS` 取并集(main 的 `__spec__`/`__loader__`/`__file__`/`__path__`/`__package__`/`__reduce__`/`__reduce_ex__`/`__getattr__`
    + #26 的走栈/闭包单元属性);`FORBIDDEN_SUBSTRINGS`/`DANGEROUS_UNDERSCORE_ATTRS`(main)与 `FORBIDDEN_KEY_STRINGS`/按键取语境(#26)并存。
  - `research_api`:`rs.safe_builtins(extra_forbidden, allowed_modules, repo_guard=...)` 合成同一个调用,`verify_trusted_tree` 启动期核对保留;
    仓库根改用 main 引入的 `rc.CODE_ROOT`(代码根;`INVEST_ROOT` 只管数据根——受信目录 `strategies/` 属于代码)。
  - **两边口径打架的地方(如实记录,没有放宽任何一边)**:① `matplotlib`/`psutil`:#26 第 25 轮要求可导,main 白名单默认不含、
    配置追加又过不了准入审计 → 取更严的一侧,两条用例已改写并新增回归;② 同一次属性访问两层都拒时异常类型不同
    (名字层 `ImportError`、对象层 `AttributeError`)→ 名字层先拒,对象层用「名单里没有的名字」另测;③ `logging` 不在白名单,
    原视图用例改用同形的 `enum.sys`。
  - 测试:新增 `tests/test_merge_main_sandbox_union.py`(76 条,放行+拦截双向,拦截断言规则文本);
    生产入口用例改成 `run_candidate(static=False)` 默认只测运行期层(静态层先拒会让用例测到别的层),
    静态层拒绝另在并集用例里逐条断言;4 个测试文件补 `import bootstrap`(main 的 research_api 不再顶层 import 它)。
- **DSH 研究入口与带外批准**(Issue [#22](https://github.com/laiyinyizao007/invest/issues/22),
  ADR `20261113-dsh-research-entry-and-out-of-band-approval`):在 DSH Desktop 里用 `research` Preset 发起研究、查进度、请求人工批准,
  批准在本机 `127.0.0.1` 的一次性网页里由人完成——**模型只能请求,不能授予**。
  - `tools/research_mcp/server.py`:MCP 服务,工具恰为三项(`start_research`/`research_status`/`request_approval`),
    经毕昇开放 API `POST /api/v2/workflow/invoke` 编排;**开放 API 与续跑协议已实测**(必须同时带 `session_id` 与 `message_id`)。
  - `scripts/research_approval_server.py`:**批准记录的两个合法入口之一**(低层写入者只有 `research_sandbox.record_approval` 一个函数;另一个入口是等价 CLI `research_api.py approve`/`review`)。只监听回环、校验 Host/Origin/Sec-Fetch-Site 与一次性 CSRF 令牌,
    展示完整源码(脱敏)、静态筛查结果与父策略 diff,人输入**完整 64 位 sha256** 才写账本,后端自己重算哈希并把**刚核对过的那个哈希**写进去
    (不在写入前再读一次文件),填错可改正重试(上限 5 次),决定后进程立刻退出。
  - `tools/research_mcp/dsh-research-preset.patch.yml`:DSH `research` Preset 模板,只挂 `persona` + `research-mcp`,
    不含任何文件读写/命令/网页/子 Agent 工具,Auto review 不启用;密钥只用 `process.env` 引用。
  - `requirements-research.txt`:MCP SDK 会拖进 starlette/uvicorn/pydantic,**不进交易环境**(交易环境仍只装 `requirements.txt`)。
    **措辞更正(第 4~5 轮评审)**:该文件**不是**完整运行环境——研究环境是「仓库依赖 + 本文件」,
    隔离的真正含义是「mcp 不装进交易 venv / 交易镜像」。**第 5 轮再更正一次**:需要 vnpy 的**只有回测子进程**
    (回测要 import `strategies.*`);筛查(`const.load()` / `const.screen_source()`)是纯 AST 分析,批准/编排/网关/MCP 进程都不 import vnpy。
  - **G8 双轮评审(codex CLI 第 2~16 轮:33/50 → 39/50 → 36/50 → 40/50 → 39 与 37 → 38 与 33 → 37 与 36 → **32 与 35** → **36 与 36** → **40 与 40** → **38 与 38.5** → (15 中断)→ **38 与 40**,均未达 45 的发布线)后按发现逐轮加固**:
    - 第 2 轮最要紧的一条:**批准页地址与 sha256 一律不进工具返回值**(只进本机浏览器)——
      返回里带上它们,一个有本地 HTTP 能力的模型就能自己去 POST 批准页,把「带外人工批准」变成「模型自我批准」。
      其余:批准记录写入加独占锁与唯一临时名(并发写不丢更新);候选名改为以**文件系统增量**为准(模型散文里的文件名只在候选目录里核对存在才算);
      页面把源码标为**不可信数据**并经 `dashboard/safe.scrub_text` 脱敏;批准绑定的哈希与「人核对过的」那一份同源(消除 TOCTOU);
      `research-mcp` 的配置从数据根读取;边界测试补上「直接写批准文件」的扫描。
    - 第 3~4 轮闭合的漏洞:**哈希与批准命令仍会经毕昇 tip 模板漏进模型通道**(网关响应把 sha256 原样回给模型 → 模型可在聊天里替人念哈希,
      「人核对完整 64 位」退化成橡皮章)。现为三层防线:网关响应不再回哈希 → 毕昇 tip 模板不再回显工具输出与批准命令 →
      MCP 输出再脱敏一层(64 位十六进制与 `approve` 命令行替换为占位符)。
      同轮还收紧:批准页 POST 必须带浏览器来源标记(`Origin` 或 `Sec-Fetch-Site`);脱敏模块不可用时**拒绝提供页面**(fail-closed,
      不拿占位符顶上);CSRF 改用字节比对(非 ASCII 令牌被拒而非抛 TypeError);`review` 与 `--confirm-reviewed` **必须在交互式终端**里执行,
      自动化调用不得批准代码;锁文件判陈旧(Windows 下用 `OpenProcess` 探活,不用 `os.kill(pid,0)`——那在 Windows 上是终止进程);
      `os.replace` 加重试;研究侧不再 `import bootstrap`,因此**筛查/批准/编排/网关/MCP 进程都不再 import vnpy、不再加载 `.env` 里的交易凭据**
      (网关是最后一个还在 import 的组件,第 4 轮一并去掉;边界测试新增两条 AST 级断言锁住这条声明)。
      ⚠️ 措辞更正:「研究进程不再 import vnpy」只在**筛查/批准/编排/网关/MCP** 这几个进程成立;
      **回测子进程仍然 import vnpy**(回测要 import `strategies.*`),这一条别被上句话带偏。
    - 第 5 轮闭合的漏洞(40/50,仍未达发布线):
      - **批准页地址彻底不落盘**:批准进程**自己开浏览器**,URL 只活在那一个进程的内存里,MCP 侧既拿不到 URL,
        也没有「写进 `.local/research-mcp/approval-page.txt` 再删掉」这种中间态可依赖——此前那句「地址只在本机文件里」
        依赖的是「模型没有本地文件工具」这个**未经验证**的前提,现已不再需要。MCP 的返回文案改指终端回退路径。
      - **CLI 的 stdout 里不再有哈希**:`approve`/`review` 的返回值去掉 `sha256` 键,哈希只留在 stderr(给人的通道);
        交互提示语也一并改走 stderr(`input(prompt)` 会把提示语写进 stdout,那样 stdout 就不是纯 JSON 了)。
      - **网关响应剥掉哈希**(`_strip_digests`):磁盘结果文件里的哈希是内部账本,但响应会进毕昇工作流输出再回到模型,故出口处剥掉。
      - **MCP 的 fail-closed 原因要透出来**:批准进程拒绝提供页面(比如脱敏模块不可用)时,工具返回要写明原因,
        而不是笼统的「批准进程没起来」;所有 MCP 返回统一过脱敏。
      - **没有会话就不许请求批准**:原先没有会话记录时照常拉起批准进程,模型可以把候选目录里的**任意旧文件**推给人批——
        批准流程本身成了「批任意文件」的后门。现在无会话直接拒绝;人要直接批某个候选走终端里的 `research_api.py review/approve`。
      - **脱敏失败时关掉已绑定的 socket**:构造批准页失败要 fail-closed,同时把已经绑定的监听端口关掉,不留空转端口。
      - **锁探活用 `OpenProcess`**:Windows 上 `os.kill(pid, 0)` 是**终止进程**而不是探活,原实现会把进程杀掉。
      - 事实性更正:原先「Origin/Sec-Fetch-Site 能挡住一切 CSRF」的表述被夸大——它们只挡**跨站浏览器** CSRF,
        挡不住本机程序。真正的屏障是「192 位一次性 URL 令牌 + 页面 CSRF 令牌 + 完整 64 位哈希」三件套。
      - **错误消息里的本机路径脱敏**:网关出口统一 `redact_paths`,只盖本机绝对路径(URL 与候选文件名保留)——
        网关响应会回到模型,本机目录结构对它没用却是一条指纹识别信息。
      - **CLI 的 stdout 整段是纯 JSON**:交互提示语原先进 stdout(`input(prompt)` 的副作用),改走 stderr。
    - 第 6 轮(自查 + codex 复核)补上的几处:
      - **模型可见通道的清洗统一到一个出口**:此前路径脱敏只加在网关的两个异常分支上,
        而网关有**五条**模型可见的出口(`_data`、`_candidates`、作业的三种 `view`、`_results`、`_feedback`)——
        结果文件里的 `error.message`、失败 Trace 的 `failures`、作业的 `report` 都可能夹着本机路径。
        现把清洗**收在 `handle` 的唯一出口**(`_scrub`:剥哈希键 + 盖本机绝对路径):
        散落在各端点就会漏,漏一条等于边界没守住;收在出口就没有「漏掉某个端点」这回事。
      - **MCP 侧同样补上路径脱敏**:`redact_for_model` 原先只脱敏 64 位十六进制与批准命令行,
        现在也盖掉本机绝对路径;两处会带出脚本路径的返回(`找不到批准进程:` / `拉不起批准进程:`)一并改走脱敏。
      - **批准页的头名一律小写化**:`handle` 原先不统一头名大小写,直接调用它的地方
        (绕过 HTTP 传输层的小写化)可以用大写的 `Sec-Fetch-Site: cross-site` 绕过来源检查——
        实测修前该头被判为「允许」、修后判为「拒绝」。
      - **CI 关卡补上 `scripts/research_gate.py`**(此前只在边界测试的清单里,没进 ruff 关卡)。
      - **调用方提前断开不再刷整段 traceback**:毕昇工具节点超时或关页时,网关与批准进程的
        `wfile.write` 会抛 `BrokenPipeError`,socketserver 把整段 traceback 打进日志——真正的错误反而被淹没。
        两处都改成安静收尾(实测本机网关日志里就有这么一大段)。
      - **修掉一个「代码与数据两棵树混用」的隐患**:`research-mcp` 原先把 `ROOT/scripts` 插在 `sys.path` 的**最前面**,
        于是 `INVEST_ROOT` 指向别的树时(worktree + 主检出),**代码**会从那棵树的 `scripts/` 导入——
        跑的是另一份代码。实测表现为「新 `server.py` 配旧 `research_sandbox.py` → `AttributeError`」。
        现改为**代码一律从 `CODE_ROOT` 取**(数据根的切换交给 `research_config` 自己处理),并加了断言锁住这条。
    - 第 6 轮(codex CLI 并行两份复核:A 39/50、B 37/50,均未达 45 的发布线)闭合的发现:
      - **自由文本里的哈希也盖掉**:原先只剥「`sha256` 这种形状的键」,结果文件里的 `error.message`、
        作业 `report`、失败 Trace 里随手写的一句「hash=0123…(64 位)」就能原样回到模型。
        现整段文本过 `redact_digests`(任何 64 位十六进制串 → 占位符),网关 `_scrub` 与 MCP `redact_for_model` 两侧都盖。
      - **批准命令行也不进模型通道**:`research_api.py approve` / `research_approval_server.py` 这类命令行
        出现在文本里,模型就能在聊天里替人念命令。现由 `redact_approve_commands` 盖掉;MCP 的「请求批准」返回
        只说「请让本机的人在本机终端里操作」,**不含命令行**(命令行只进本机 MCP 的 stderr)。
      - **`redact_paths` 补齐三类漏网路径**:UNC(`\\server\share`)、Windows 扩展长度(`\\?\C:\…`)、
        POSIX 单段绝对路径(`/etc`、`/tmp` 这类根目录直属项)——原来只认「至少两段」的 POSIX 路径。
      - **配置的三条路径 + 令牌文件统一查受保护目录/凭据文件**:原先只有 `pending_dir` 查,
        `results_dir`/`approvals_file`/`token_file` 仍可被指到 `strategies/` 或借用 `.env`;
        现全部**大小写不敏感**地查(`data/.ENV` 在 Windows 上就是 `.env`)。
      - **同名候选不再让整条工作流中止**:多轮研究时 LLM 复用上一个候选名 → 网关 409 → 毕昇把非 2xx
        变成 `api_call_fail` 并**中止整个工作流**。修法不是自动改名(改名会让「模型看到的名字」与
        「人批准的文件」脱钩,可能批准错文件),而是**把现存的候选名喂给起名这一步**并要求不得重复使用;
        网关的 `_feedback` 返回现存候选名(不含路径、不含源码)供续跑避让。
      - **边界测试从「按名字匹配」升级为 AST 判定**:写入者识别认得属性访问、裸名、import 别名、`getattr`
        与常量拼接;并按**传递 import 闭包**检查研究侧是否间接 import 了 vnpy/交易栈,
        且区分「导入期闭包」与「回测子进程(唯一允许 vnpy 的地方)」。
      - **CI 真正跑边界测试**:此前 `research-guard.yml` 只跑 compileall + ruff,
        **没有任何 job 会跑那些边界测试**——写进仓库却无闸门。现在同一个 job 里跑研究侧九份测试
        (闸门**分两步**跑:先单跑边界不变量那一份,再跑其余——同一个 pytest 调用里只要有一个文件在收集阶段报错,
        整轮就中止、一个测试都不会跑);ruff 清单也补齐了本 issue 改过的文件。
    - 第 7~8 轮(codex CLI 并行两份复核:A **38/50**、B **33/50**,均未达 45 的发布线)补上的几处:
      - **静态筛查从「模块黑名单」改为「能力白名单」**:黑名单漏了 `zipfile`/`tarfile`/`shelve`/`sqlite3` 等——
        它们自己 import 了 `os` 并挂在模块属性上,候选一句 `import zipfile` 就能拿到 `zipfile.os.system`
        (实测筛查**零告警**)。现只放行候选真正需要的能力,其余一律 `ImportError`;
        筛查层另加「取模块危险属性(`X.os`/`X.shutil`/`X.importlib`)」的静态规则。
        **这是纵深防御,不是沙盒**——候选仍以当前用户权限运行,真正的硬保障是「人读过源码 + 批准带外」。
      - **CLI 的 stdout 统一过清洗**:`emit()` 现在过 `rs.scrub_model_visible`,`strategy_sha256` 之类键不再出现在 stdout
        (stdout 是模型可见通道);交互提示语与哈希只在 stderr。
      - **批准页的 URL 与哈希彻底不进 stdout**:ready 事件只带 `event/strategy/screening_passed/findings`,
        URL 与「不要粘进聊天窗口」的警告走 stderr;另加 `redact_approval_urls` 做纵深防御。
      - **清洗升级为「共享内核 + 唯一出口」**:网关的 `SandboxError` 分支原先绕过统一清洗(现改走 `_scrub`);
        MCP 与 CLI 共用 `rs.scrub_text`,并新增**凭据形态**规则(密钥赋值 / `Authorization:` 头 / `Bearer` / `CTP_*`)。
      - **批准必须「TTY + 粘贴完整匹配的 sha256」**;URL 令牌比对改用常数时间;`record_approval` 自己重算哈希,不一致即拒。
      - **写入者识别补上 `__dict__["record_approval"]`**;MCP 侧改为 AST 判定(不再是子串匹配)。
      - **「直接写批准文件」那条断言的判定器重写**:分三层(明确写方法 / 带模块限定的文件函数 / 有歧义的名字且接收者像路径),
        统一 `open` 家族的 mode 位置、`os.open` 走标志位、`_atomic_replace` 计入写操作、路径名做常量折叠;
        判定器**自己**有了对照用例(必须抓到的绕过写法与不该误报的写法)。
      - **研究栈与交易栈解耦**(窗口常量抽到 `scripts/segment_windows.py`,`scripts/segment_ledger.py` 不再 import bootstrap),
        CI 的研究侧闸门因此能**不装交易环境**真跑起来——此前整轮在收集阶段中止,**一个测试都没执行过**。
      - **并发与超时的负向测试补齐**,并当场逼出两个真 bug:批准记录的临时文件名只带 pid(同进程并发会撞名)、
        锁文件在 Windows 上被别的进程读着时删不掉(删不掉 = 之后所有批准都被锁死)。都已修。
      - **Preset 边界从「文本 grep」升级为「结构断言」**(解析 YAML,断言只挂 persona + research-mcp)。
        **如实说明**:CI 里没有 DSH,「渲染后的整棵配置树」仍只有本机手工实测那一次。
    - 第 9 轮(codex CLI 并行两份复核:A **37/50**、B **36/50**,均未达 45 的发布线)补上的几处:
      - **能力白名单自身泄漏 `os`/`sys`——静态层 + 运行期双防线**(第 9 轮最严重的一条):
        白名单里的模块把 `os`/`sys` 挂在自己的属性上——`statistics.sys`、`typing.sys`、`enum.sys`、
        `dataclasses.sys`/`dataclasses.inspect`、`fractions.sys`、`collections._sys`、`random._os`、
        `numpy.select`、`pandas.io.pickle`、`pandas.io.common.os` 都实测存在。候选一句
        `import statistics` + `statistics.sys.exit()` 就能逃出沙盒,而筛查**零告警**。现在:
        ①静态层的属性链判定改成**任意深度**(`X.os`、`X.y.os` 都算),并新增「`from X import os`」形态;
        ②运行期把白名单模块换成 `_GuardedModule` 代理,取危险属性一律抛 `ImportError`,
        `guarded_scope()` 退出时**恢复原模块**(不留代理);③通过配置额外放行的模块也要过这道审计
        ——原先这条配置项本身就是攻击面。
      - **清洗内核补齐两种凭据形态**:`Authorization: Bearer <token>`(原先只盖普通头形态)、
        以及 JSON 形态的 CTP 凭据(`"CTP_BROKER_ID": "..."`)。清洗顺序固定为**赋值 → Bearer → 头 → CTP**。
      - **删掉死代码**:批准进程里那段「与常量做常数时间比较」的预检(比较对象与令牌无关)已删除,
        令牌比对的每一处都走 `secrets.compare_digest`。
      - **清洗内核收敛**:`research_api._child_reason` 原先另起一套本地正则,现统一走 `rs.scrub_text`;
        `review` 的帮助文案改成实际流程。
      - **CI 关卡**:python 3.11 → **3.12**(3.11 上 `-W error::SyntaxWarning` 对无效转义**只是
        DeprecationWarning**,闸门注释承诺的检查在 CI 上是空操作);ruff 清单补上
        `scripts/optimize.py` 与 `tests/test_segment_ledger.py`;compileall 覆盖
        `scripts tools tests dashboard risk notify strategies trading ctp_tools`(研究闭包不只在 scripts/tools/tests 之下);
        新增 `concurrency` 组(连续推送只跑最新一次)。
      - **MCP 侧的「不许写 approvals」改为 AST 判定**(原先是子串匹配,换个变量名就能写账本),
        并与边界测试**共用同一个判定器**;Preset 结构断言从只扫 key 改成**key 与 value 都扫**。
      - **新增负向用例**:approvals 文件路径与网关令牌文件的**固定路径**各 4 个负向用例
        (指向受保护目录 / 借用 `.env`,含 Windows 大小写不敏感与 `data/.ENV` 变体);
        **常驻研究进程不得 import 交易栈**的 AST 级不变量(回测子进程是唯一允许 vnpy 的地方);
        批准写入者按**引用计数**判定(只有两个「人机交互入口」可以*提到* `record_approval`,别处只字不许提)。
      - **依赖面说明更正**:`requirements-research.txt` 改为「研究运行时 = 标准库 + 本文件;
        `requirements.txt` 是交易环境,研究侧不需要」(此前说法与实测相反)。
    - 第 10 轮(codex CLI 并行两份复核:A **32/50**、B **35/50**,均未达 45 的发布线)补上的几处:
      - **模块代理不再放行 dunder 与私有名**(第 10 轮 A 的 Critical,已实测闭合):
  上一轮修好了「白名单模块把 os/sys 挂在属性上」,但同一类失效模式换了属性名 `__spec__` 活下来——
  `mod.__spec__.loader.get_data(path)` 能**读任意文件**(`.env`、网关令牌),
  `set_data(path, bytes)` 能**写任意文件**(直接改 `approvals.json`,于是「人读过源码才批准」
  这道仪式被候选单方面废掉),而静态筛查**零告警**。现在:
  - 代理改为**默认拒绝**:除少数安全名(`__name__`/`__doc__`/`__all__`/`__version__`/
    `__annotations__`/`__qualname__`)外,凡以 `_` 开头的属性一律取不到;
  - 静态层把 `__spec__`/`__loader__`/`__file__`/`__path__`/`__package__`/`__reduce__`/
    `__reduce_ex__`/`__getattr__` 加进 `FORBIDDEN_ATTRS`(裸名与字符串形态都拒);
  - 代理不再改写 `sys.modules`,改用**代理缓存**(`guarded_scope()` 退出即失效),
    取到的**子模块**也递归包成代理(`pandas.io.common.os` 这条链堵得住);
  - 新增负向用例:`__spec__`/`__loader__`/`__dict__`/`__getattribute__`/`_module`
    在运行期必须抛 `ImportError`,静态层必须报错;另有**对照用例**保证
    numpy/pandas 的正常用法(`arr.mean()`/`.sum()`/DataFrame)没被误伤
    (第 10 轮 A 的一条 High 就是「叶子审计把 numpy 打成不可用」,已一并修掉)。
- **清洗内核的凭据形态与顺序**:Bearer 规则提到**最前**(原先 assign 规则先把 `Bearer` 当值吞掉,
  真令牌留在原地:`token: Bearer abc123def456` → 令牌原样保留);
  POSIX 路径的两条规则不再重复遮盖(同一路径被盖成两个 `<路径>`)。
- **网关按值盖掉自己的鉴权令牌**:清洗规则是按**名字**认凭据的,令牌一旦被读成原文再塞进
  作业消息,名字那一层就认不出来——网关出口(`handle` 的唯一出口)现在额外按**值**替换令牌原文。
- **CI 关卡**:compileall 加 `-f`(强制重编译;否则**已存在的 .pyc** 会让这道闸门变成空操作),
  ruff 清单补 `risk/` 与 `notify/`(整个研究闭包),workflow 注释改成「**预防网**」而不是「已修缺陷」;
  `scripts/segment_windows.py` 补入库(原先未入库,CI 会因找不到路径直接红)。
- **Preset 边界从「名字黑名单」改成「结构白名单」**(第 10 轮 B 的 M1):第 10 轮实测变体——
  只在 persona 插件的 `config` 下加一行 `tools: [tool-exec]`,**六条 Preset 断言全部 PASS**。
  现在:任何能力类键(`tools`/`skills`/`mcpServers`/`hooks`/`agents`/…)一律拒,
  任何以 `tool-`/`dsh-tool-`/`skill-` 开头的字符串值一律拒,
  每个插件的 `config` **逐键**断言「只有已知那几个键」,插件**包名**也只允许那两个。
- **批准写入者的「引用」判定补上字符串常量形态**(第 10 轮 B 的 M2):
  `getattr(rs, "record_approval")` 与 `rs.__dict__["record_approval"]` 原先**整条漏掉**
  (能同时骗过「唯一写入者」与「只字不许提」两条断言),现在算引用;
  并补了对照用例(属性/别名/getattr/`__dict__` 四种写法都要被抓住)。
- **「常驻研究进程不 import 交易栈」从浅不变量改成传递闭包 + 只在子进程入口碰交易栈**
  (第 10 轮 B 的 L6):原先只查那三个文件自己的 import,`research_api` 在函数体里 import 交易栈没人管;
  现在:常驻进程的**导入期闭包**里不许出现交易栈;研究侧文件里的交易栈 import
  只能出现在 `*_child` 子进程入口(或两个登记在案的例外函数,且它们只被 `*_child` 调用)。
  第 11 轮把这条从「文档里的一句话」变成**真正在跑的断言**:原先 `_call_sites` 对整个
  `research_api.py` 返回 0 个调用点,那条断言**空转**(第 11 轮 B 的 F1);现三个 walker 收敛成
  一个带作用域栈的 walker,并各配一条对照用例。
- **文档与测试数字**(**第 11 轮当时的快照**):研究侧九份测试共 **518 项**(CI 模拟:**498 通过 + 20 跳过**)。
    - 第 11 轮(codex CLI 并行两份复核:A **33/50**、B **38/50**,均未达 45 的发布线)补上的几处:
      - **静态层认得「异常回溯帧」这一类逃逸**(本轮最严重的一条,Critical):
        第 10 轮修住了「模块的 dunder」,同一类失效模式换了**属性名**活下来——
        `e.__traceback__.tb_frame.f_globals` 直接指向 numpy 内部帧的**真全局命名空间**(里面有真 `os`),
        `np.load.__globals__['os']` 同理;两条路**一个禁用名都不出现**,上一轮筛查返回 `[]`,
        于是候选能任意写文件(可伪造 `approvals.json`)、读 `.env`/令牌、执行命令。
        现在 `__traceback__`/`__context__`/`__cause__`/`tb_frame`/`tb_next`/`tb_lasti`/
        `f_globals`/`f_locals`/`f_builtins`/`f_back`/`f_code`/`f_lineno`/`__globals__`/
        `__code__`/`__spec__`/`__loader__`/`__mro__`/`__subclasses__`/`__getattribute__`/
        `__dict__`/`__reduce__` 一律进 `FORBIDDEN_ATTRS` 与 `FORBIDDEN_SUBSTRINGS`
        (裸名 / 字符串 / **模板**三种形态都拦),并同步进宪法层的 `BUILTIN_FORBIDDEN_DUNDERS`;
        单下划线的私有属性(`numpy._module`)也按「能力入口」判,静态层与运行期口径终于一致。
        **如实说明**:本轮只闭合**静态**那一半——运行期代理**不包裹**从模块取到的函数与类
        (`np.load.__globals__['os']` 运行期仍畅通),这一条**必须靠容器隔离(4b/4c)兜底**,
        静态筛查的作用是让人**看见告警**,不是拦住。已登记进 ADR 的残留清单。
      - **凭据按值脱敏补上第二条通道**:原先只有网关按**值**盖掉自己的鉴权令牌,MCP 侧没有——
        「网关盖住、MCP 不盖」就是一条漏的通道。现在 MCP 的 `redact_for_model`
        会把**已知的凭据值**(毕昇开放 API 密钥 + 网关令牌)按值替换掉。
      - **`redact_paths` 的路径形态**:JSON/repr 形态的 Windows 路径(`"C:\\Users\\…"` 两个反斜杠)
        原先只盖掉 `Users` 一段,尾部段(含用户名与文件名)留在外面;UNC 规则同时会**误伤正则字面量**。
        现在 Windows 规则先于 UNC 规则、UNC 要求**恰好两个**前导反斜杠且主机名形状合法,
        `\\?\C:\…` 与 `\\server\share\…` 仍然盖掉,而 `re.compile(r"\\d+")` 不动。
      - **模板字符串藏不住禁用名**:静态层原先只判「字符串常量**等于**禁用名」,
        `"{0.__globals__}".format(np.mean)` 这种写法能把名字拆到模板里;现在字符串常量只要**含有**禁用串就报。
      - **批准写入者的判定器**:原先把**任何**含 `record_approval` 的字符串常量都算成「持有写入函数」
        (文档字符串、普通错误提示语都会被误判),而 `chr()`/`join()` 那种**非常量折叠**又完全看不见。
        现在:字符串常量的判定**看上下文**(只有作为调用的实参或下标才算)、文档字符串排除在外,
        并补了一条**能力规则**——凡是以模块别名为接收者的 `getattr`/`vars`/`dir`/`attrgetter` 一律算引用。
      - **边界测试的三个 walker 收敛成一个**(两条 High):`_local_imports`/`_trading_import_sites`/`_call_sites`
        原先各有**一套不同的遍历语义**,于是「例外函数只被 `*_child` 调用」那条断言**空转**
        (`_call_sites` 对整个 `research_api.py` 返回 0 个调用点,`for` 循环体一次都没执行),
        而复合语句(`if/for/try/with`)里的 import 与调用**根本看不见**。现统一成一个带**作用域栈**的 walker
        (函数的默认值与装饰器属外层作用域、函数体属内层作用域;复合语句的 `body`/`orelse`/`finalbody`/`handlers`
        递归下钻),三条断言共用它,并各配一条**对照用例**(合成一个不在 `*_child` 作用域里的调用/import,必须被判出来)。
      - **动态导入也纳入不变量**:`importlib.import_module("vnpy…")` 与 `__import__("vnpy")`
        对只看 `import` 语句的不变量**完全隐形**,而本仓库里**已经有**这种写法
        (`risk/constitution.py` 的模板载入)。现新增断言:动态导入只许出现在回测子进程入口里。
      - **Preset 白名单的两个通用分支补上对照用例**:「能力类键归一」与「字符串值前缀」两个分支
        原先在仓库自带的测试里**一次都没被走到**,可以被无声删掉;现补 7 条**参数化的负向用例**
        (能力键的大小写变体 / 分隔符变体 / 值前缀 / 未知配置键 / 未知插件 / 未知插件包),在内存里构造结构、不落盘。
      - **CI 关卡与工具**:第二步加 `--continue-on-collection-errors`(一份文件收集失败不再把整步的研究侧测试全废掉);
        宪法守卫改为只扫 **git 跟踪集**(`git ls-files --cached --others --exclude-standard`),
        开发者落在仓库里的临时 `.py` 不再把守卫弄红(而报错完全指不到「你的临时文件」);
        `.gitignore` 补 `.pytest_tmp_codex/`;ruff 清单补 `dashboard/__init__.py`。
      - **文档诚实度**:ADR 与本文件里「例外函数只被 `*_child` 调用」原先把一条**空转**断言写成了已生效的约束,
        现已随判定器一并修好并补上对照用例;「全量测试」的失败项补注(round11/12 那 10 项因 vnpy 导入顺序而失败,
        **是本分支引入的**,已按上面的方式修掉——不是环境问题,此前那句「改动前后一致」是错的,现已改正);
        基线日期统一为 2026-11-13(复核时钟为本机时区,原文混用了 +0800 与本地时区两种记法)。
      - **导入顺序回归(本分支引入,已修)**:解耦后 `tests/test_constitution_round11.py` 与 `round12.py` **合跑**会失败 10 项,
        报 `bootstrap 必须先于 vnpy.trader.database 导入`。根因如上(宪法指纹预加载 `vnpy_ctastrategy.backtesting`
        → 连带加载 `vnpy.trader.database`;原先靠 conftest 间接导入 bootstrap 兜底,解耦后兜底消失)。
        修法:两份文件在导入期显式 `import bootstrap`;合跑 **92 项全过**。
    - 第 12 轮(codex CLI 并行两份复核:A **36/50**、B **36/50**,均未达 45 的发布线)补上的几处:
      - **本机路径的清洗改成「扫描式」**(A 的 M1,触及「模型可见通道无本机绝对路径」这条不变量):
        原先三条正则各有一个可复现的漏——盘符前紧贴一个 ASCII 字母时**整条绝对路径原样出去**
        (`openC:\Users\alice\.env`)、用户目录里带空格的显示名只盖掉前半段(**OS 用户名残留**)、
        UNC 主机名不允许空格(`\\My Server\share\…` 整条逃逸)。
        现在:先找路径**起点**(盘符/UNC),再扫描到路径真的结束——**空格算不算路径的一部分,
        看它后面的词是不是紧跟分隔符**(`C:\Users\John Doe\.env` 里的空格在路径里;
        `C:\Users\alice\.env failed` 里的空格是散文,路径到 `.env` 为止,句子照旧保留);
        profile 目录额外多吃一个词(OS 用户名要紧)。UNC 要求「主机名后面必须跟分隔符」,
        所以 repr 里的 `\n` 不会被当成路径起点——否则后面的正文会被整段盖掉(过度遮蔽同样丢诊断信息)。
      - **静态层认得「常量拼接」出来的禁用名**(A 的 M4):「字符串/模板形态都拦」这条规则原先**逐常量**判定,
        `'{0.__gl' + 'obals__}'` 一行拼接即绕过——而这条规则的全部价值就是「人批准前能看见告警」。
        现在先把 `BinOp(Add)` 递归折叠再判;`FORBIDDEN_SUBSTRINGS` 同时补齐成 `FORBIDDEN_ATTRS` 的子集
        (ADR 原来列举的十几个名字只在精确匹配里,模板形态不拦)。
      - **CLI 这条模型可见通道补上按值脱敏**(A 的 M5):三条通道(网关/MCP/CLI)里原先只有网关与 MCP 有
        「按值盖掉已知凭据」这一层,CLI 的 stdout 只有「按名字」认——候选把令牌连名字一起塞进异常文本时,
        网关与 MCP 盖住、CLI 不盖。现在 `scrub_model_visible` 接受 `secrets`,
        CLI 在唯一的 stdout 出口 `emit()` 处读一次网关令牌按值盖掉(读不到就跳过,不让整条脱敏失效)。
      - **私有属性的静态口径与运行期对齐**(A 的 L1):`self._state` / `self._file` 这类**候选自己的**私有字段
        原先被**直接拒掉**(运行期的模块代理只拦模块属性);现在这条规则只对「属性链的根是导入进来的模块」生效
        (`np._module` 照样报)。
      - **清洗规则的三个边界**(A 的 L2/L4/L5):64 位摘要改成「**连续 64 位以上的十六进制一律隐去**」
        (原先要求两侧非十六进制,于是「前缀+摘要+后缀」这种最常见的拼接形态整条逃逸);
        批准页地址的 scheme 改成可选(`127.0.0.1:8765/a/TOKEN` 同样是模型能直接用的地址);
        Cookie 头改成**整条隐去**(原先只隐去第一个值,`;` 后面的键值对原样出去)。
      - **凭据读不到时在 stderr 留痕**(A 的 L3):按值脱敏这一层原先是**静默降级**的
        (令牌文件读不到 → 这一层直接消失,没有任何痕迹);现在 MCP 侧在 **stderr**(不进模型通道)记一行,
        免得「以为它还在」。按值替换的长度下限仍取 8:更短的凭据值会把正文整片糊掉,那是另一种信息损失。
      - **交易栈侧的导入顺序不再被静默吞掉**(B 的 M1):`try/except ImportError: pass` 会把
        「交易栈装了但装坏了」的错误吞掉——bootstrap 于是没被导入,后面报的是「导入顺序错了」,
        真实原因(交易栈没装好)反而看不见,排查方向被带偏。现在用 `importlib.util.find_spec("vnpy")`
        **只判断「在不在」**,不吞异常。
      - **边界测试的模块级 import 收集器走进复合语句**(B 的 L6):`if True: import bootstrap` 这种合法写法
        原先会被误报;现在递归下钻 `if/with/for/while` 体,并补了对照用例。
      - **CI 关卡两处对齐注释**(B 的 L4/L5):`compileall` 的清单补上 `.github`(注释承诺的是「整个仓库都要能编译」);
        依赖改为 `pip install -r requirements-research.txt`(声明的依赖文件原先从不被 CI 安装,
        改了这个文件 CI 却不会发现)。
      - **测试数据里不留本机绝对路径**(A 的 L6):`tests/test_research_mcp.py` 里那条真实目录布局换成明显虚构的路径。
      - **两条判定器问题(A 的 M2/M3)已由 B 轮的等价发现先行修好并复核**:假阳性(错误提示/日志里把
        `record_approval` 当实参传)3/3 消解、假阴性(`__dict__[…]` 下标、动态取模块)2/2 命中——
        按 A 轮的原始输入逐条复核通过。
      - **本轮新增的遗留(如实登记,不是已解决)**:交易栈侧的 92 项宪法回归与 `test_segment_ledger`
        **在任何 CI job 里都不会被执行**(研究侧闸门刻意不装交易环境,装了就破掉解耦保证);
        已实测 `test_segment_ledger.py` 经 `optimize` 依赖 vnpy、在「无交易栈」下 4 项失败,
        所以它**不能**并入研究侧闸门。**缓解**:那条导入顺序约束已被改写成一条**纯 AST 的断言**
        (研究侧闸门**会**跑它),但 92 项行为回归本身仍只在本机跑。
      - **文档与测试数字**(**第 12 轮当时的快照**):研究侧九份测试共 **531 项**(CI 模拟:**511 通过 + 20 跳过**——
        跳过的是回测子进程那些「要 vnpy 才跑得起来」的用例)。
    - 第 13 轮(codex CLI 并行两份复核:A **40/50**、B **40/50**,均未达 45 的发布线;
      **无 Critical / High**,两份报告同为本分支最高分)补上的几处:
      - **路径清洗按「段」推进**(A 的 M1):带空格的 Windows 路径原先在空格处就断,目录名只盖掉前半段、
        **OS 用户名残留**;现在改成一串「空格 + 不含分隔符的词」连续推进,直到遇到真正的终止符
        (句读、冒号、引号、换行)才停;`C:\Users\John Doe\file.py` 这种**空格后面还跟文件段**的形态也一并盖掉。
        另修:新规则曾**多吃一个散文词**(`error at <路径> later`,`retry` 丢失)、`\n` 转义被 UNC 规则当成 UNC 起点。
      - **批准写入者判定器补三条取函数的形态**(A 的 M2):`rs.__dict__.get("record_approval")`、
        `rs.__dict__.items()`、`from … import *` 后**裸用** `record_approval` —— 原先都判不出来,现已全部命中并各配对照用例。
      - **`file://` 形态的本机路径不再原样出去**(A 的 L1):`_URL_RE` 原先把所有 URL 摘出来保护,
        `file:///C:/Users/…` 于是整条原样返回;现在 `file:` 开头的 URL 不走 stash,直接替换成 `<路径>`。
      - **CLI 的按值脱敏降级会留痕**(A 的 L2):读不到网关令牌时原先**静默**退化成「只按名字脱敏」一层,
        现在往 **stderr** 打一行(stderr 不进模型通道),与 MCP 侧口径一致。
      - **常量折叠再折两种拼法**(A 的 L3):`_folded_str` 原先只折 `+`,现多折 `"".join([...])` 与 `bytes` 常量。
        **如实说明**:这一层是**纵深防御**(让「人批准前看得见告警」变厚),不是可利用绕过的封堵。
      - **研究侧闸门不再每个 PR 都红**(B 的 **High**):第 12 轮把 CI 依赖改成「从 `requirements-research.txt` 装」之后,
        该文件漏了测试真正需要的 numpy / pandas(第 13 轮 codex 的 High)——现已补上,并标注**仅测试期需要**。
      - **宪法守卫不再 fail-open**(B 的 Medium①):`tools/check_constitution_history.py` 用 git 视角取跟踪集时,
        原先「取不到」会退化成扫全文件系统,实测会出现「扫不到文件 = 判定没问题」;现在退路是**扫全文件系统**,
        两处口径统一为 fail-closed。
      - **契约测试锁住守卫的「谓词」而不只是「守卫的存在」**(B 的 Low③):
        「vnpy 须在 bootstrap 之后」那半截原先**恒空转**,且只查存在性不查谓词
        (把 `find_spec("vnpy") is not None` 改成 `is None`,断言照样绿);现在断言直接查谓词,
        并补了「函数体里的 import 不算模块级导入」的对照。
      - **模块级 import 收集器补齐 `async with` / `match` / `except*`**(B 的 Low④)。
      - **测试数据里不写本机绝对路径**(A 的 L6):新夹具里的本机目录布局改成明显合成的 `C:\Example\research`。
      - **残留第 9 条被更正**(B 的 Low⑤):原先写「tmp_path 的 PermissionError 本机复现不出来、**未复现**」——
        该结论**已被实测推翻**,现改为「本机可复现,但取决于进程拿到的权限;CI 是 Linux 不受影响」。
      - **未做并如实登记**(B 的 Low⑦):交易栈侧的导入顺序守卫仍散在两个测试文件里,
        彻底的做法是下沉到一个共享 helper,已登记为 ADR 残留第 10 条。
      - **数字口径统一重算**(B 的 Medium②):复核期间工作树被并发改动,六处文档的测试条数当场作废
        (531 → 534)。现已统一重算为 **539 项**(CI 模拟 **519 通过 + 20 跳过**)并在六处回填
        (**第 14 轮再次重算为 559 项 / 539 通过 + 20 跳过**,见上);
        **今后任何改动测试条数都要重跑一次 collect**(数字随树变)。
    - **第 14 轮 codex 复核**(A **38/50**、B **38.5/50**,**均未达 45 的发布线**;无 Critical / High)的边界收紧:
      - **正斜杠路径的多词段也按段推进**(A 的 M1):`Path.as_posix()` 写出来的
        `C:/Documents and Settings/x.txt` 原先只盖掉前半段(目录名与文件名残留);
        现在 `/` 与 `\` 同等对待,并给散文里的相对路径留了判据(前一个词像文件名就不算路径的一部分)。
      - **批准写入者判定器认得「变量中转模块字典」**(A 的 M2):`_D = rs.__dict__` 之后
        `_D["record_approval"](…)` 照样写得成账本,而原判定只认「直接下标 / 直接方法调用」;
        现在先收集「被赋值成模块字典」的变量名(`rs.__dict__` / `.copy()` / `vars(rs)` /
        `dict(rs.__dict__.items())`),并各配一条对照用例。
      - **三条模型可见通道共用同一份「按值凭据清单」**(A 的 M3/L10):原先只有 MCP 侧盖毕昇密钥,
        CLI 与网关只盖网关令牌(ADR 写的是「凭据清单也共享」);现在清单抽成
        `research_config.known_secret_values()`,三处共用,网关侧也改走 `rs.scrub_text`
        (长度门槛 ≥8 三侧一致)。
      - **路径清洗的四个边界**(A 的 L4~L7):中文句末标点进终止符集(不再把「。后面」整句吃掉)、
        `Users` 目录规则不再多吃一个散文词、多词段前瞻上限 4 → 8、POSIX 绝对路径带空格也按段推进。
      - **「直接写批准记录」的判定改成调用图口径**(A 的 L8):原先是**作用域**口径,
        拆成两个函数就能绕过去;现在沿**同文件**的调用链往下追 2 层,
        落点作用域用「提到批准记录的参数」写文件 ⇒ 违规(并配一条不误报的对照用例)。
      - **常量折叠再折三种拼法**(A 的 L9):`f-string` / `%` 格式化 / `str.format` 现在都折得出来;
        **如实说明**:剩下三种折不出(算出来的分隔符 / 模块级常量分隔符 / 切片),
        属纵深防御层、不是可利用绕过,已登记为 ADR 残留第 11 条。
      - **四个 import walker 的语义统一,并钉上对照用例**(B 的 L1):上一轮只把 `except*` / `match`
        补进了三个 walker,`async with` 在 `_module_level_imports` 里直接抛 `AttributeError`
        (`AsyncWith` 没有 `orelse` 分支);新加的对照用例(把三种写法的 import 植入临时文件,
        断言四个 walker 都看得见)当场逮住了它。
      - **研究侧不再依赖进程 CWD,并钉成断言**(B 的 L3):`scripts/segment_ledger.py` 不再 import bootstrap,
        顺带取消了 bootstrap 的 `os.chdir(ROOT)` 副作用(pytest 会话的 CWD 不再被强制切到仓库根);
        研究栈里因此不许出现 `os.chdir` / `os.getcwd`,路径锚点必须 `.resolve()` 成绝对路径。
        **交易侧不受影响**(那边的脚本自己 import bootstrap,`APPROVAL_RELATIVE` 永远与 `bootstrap.ROOT` 一起拼)。
      - **残留第 9 条按实测重写**(B 的 Medium):原结论与实测相反——「整批 setup ERROR」**不是确定现象**
        (本机直接跑 129 项全过),原先给的绕法(`--basetemp` 指向仓库内可列目录)**不成立**
        (pytest 对指定的 `--basetemp` 同样执行 `mkdir(mode=0o700)`),而且「mode 在 Windows 上落成 DACL」
        这条机制解释经实测不成立(本机建出来的目录创建者能正常列)。现已按**本机实测**重写并标明环境。
      - **「仓库里」改成「入库集(git 跟踪集)」**(B 的 L2):评审脚手架目录已 gitignore、不在跟踪集里。
      - **`find_spec` 守卫的爆炸半径如实登记**(B 的 L4):交易栈装了但装坏 ⇒ 模块导入期抛错
        ⇒ pytest 收集阶段中止 ⇒ **整轮一个用例都不跑**;已写进 ADR 残留第 8 条。
      - **数字口径再次重算**:研究侧九份测试共 **566 项**(CI 模拟 **546 通过 + 20 跳过**)。
    - **第 16 轮 codex 复核**(A **38/50**、B **40/50**,**均未达 45 的发布线**;无 Critical,A、B 各一条 High;第 15 轮中途被中断、无报告):
      - **repr/JSON 形态的 UNC 路径不再漏盖**(A 的 High):换行转义只有 2 个反斜杠、UNC 的 repr 形态是 4 个,原判据只看主机名首字符是 `n`,`nas`/`net` 开头的主机整条漏盖(三条模型可见通道同时中招);现按反斜杠个数区分并补回归用例。
      - **`_module_level_imports` 对 module-level `with` 不再抛 `AttributeError`**(B 的 High):第 14 轮只拆了 `AsyncWith`;对照用例补上多上下文管理器的 `with`,「四个 walker 一致」的措辞收紧为「模块级复合语句上一致」。
      - **CWD 断言变强**(B 的 M-4/5/6):认任意接收者与裸名的 `chdir`/`getcwd`/`cwd`,锚点清单补上 MCP 侧;`tools/research_mcp/server.py` 的 `ROOT` 加 `.resolve()`,兜底命令改成绝对路径。
      - **误提交的 512 个 pytest 临时产物已移出入库集**(A 的 M3,无凭据;`87ef82d`),`.gitignore` 补评审脚手架目录;历史里仍留着,合并请 squash。
      - **残留第 9 条再重写**:两个环境(非受限 / 受限评审子进程)对「`mkdir(mode=0o700)` 是否可列」的实测相反,不下统一结论;撤回「与 mode 无关」两句。
      - **研究侧路径清洗复用面板侧的全套形态**(A 的 M1):`dashboard.safe.scrub_path_forms` 抽成公共函数,`redact_paths` 第二遍调用,URL 编码 / 全角 / 零宽 / 无盘符根路径都盖住(UNC 与引号规则过贪婪,不借);另修 `str.format` 关键字实参折叠(L1)与网关 `::0`/`0`/`[::]` 监听地址(L3)。
      - **未修并登记**:其余 Low(L2/L4/L5/L6)、调用图判定器的「内容里提到 approval」误报(残留 14)、条数无自动化核对(残留 15)、其余 Low。
      - **数字口径重算**:研究侧九份测试共 **566 项**(546 通过 + 20 跳过)。

  - **工具目录在协议层实测过**:真的把 `research-mcp` 起起来(stdio),用 MCP 客户端 `initialize` + `list_tools`,
    返回**恰 3 个**工具(`start_research`/`research_status`/`request_approval`)。**仍未验证**的只有「真实 DSH 会话里模型侧看到的工具目录」。
  - **多轮研究的同名冲突已实测修好**:在真实开放 API 上用新想法跑了一轮,工作流的 `tool_feedback` 节点拿到了网关返回的
    `pending_candidates`(已落盘的候选名),起名节点据此起了**新名字**(不在列表里),**没有再撞 409**;
    该轮最终停在**静态筛查**(候选把受保护容器传给 `max()`/`min()`,宪法正常拦人),不是本次改动的缺陷。
  - **毕昇工作流已重建**:本机毕昇 3.0.0-beta5 的工作流 `1194f8540b9944fea4353b07f0493cbf` 已用改后的
    `build_v3.py` 重建(`--update`,2026-11-14 11:04),tip 模板里的候选哈希与 `research_api.py approve` 命令行
    **已不在**工作流里(实测读回 `output_tip` 节点确认)——此前这条是「改了仓库但跑的还是旧工作流」的残留。
  - **`.gitattributes`:`*.sh text eol=lf`**。Windows 上 `core.autocrlf=true` 会把 `docker/research/entrypoint.sh` 检出成 CRLF,
    `docker build` 后容器报 `exec /entrypoint.sh: no such file or directory`(shebang 的 `
` 让解释器找不到)——
    研究网关镜像因此起不来,且报错信息完全指不到「换行符」上。
  - **CI 关卡** `.github/workflows/research-guard.yml`:整个仓库跑 `compileall`(`-W error::SyntaxWarning`,告警即失败)+
    研究侧 `ruff` + **研究侧测试(含边界不变量)**,**不安装交易环境**(与 `constitution-guard.yml`
    同一条理由:根 conftest 依赖 vnpy)。动机是第 8 轮复核期间
    `tools/research_mcp/server.py` 带着一处 SyntaxError(`except/elif` 写在 try 里)进过工作树,当时没有任何 job 会跑 ruff 或 compileall;
    「边界测试没有闸门」是第 7 轮 codex 的 High。
  - 验证:工具目录恰为三项、批准写入者唯一、批准页的 URL/哈希不在返回值里、候选名来源、填错可重试、越权路径全拒、
    脱敏 fail-closed、非 TTY 拒绝批准、陈旧锁不永久锁死——研究侧九份测试文件共 **566 项**(本机有交易栈时全过,
    模拟 CI 无交易栈时 **539 通过 + 20 跳过**——跳过的是带 `requires_trading_stack` 标记的回测子进程用例);本分支全量测试 **2886 项通过**(本机 Python 3.13.7 + pytest 8.4.2,2026-11-13 重跑;CI 是 Python 3.12 + pytest 8.3.4)
    (3 项失败 + 5 项 error 为**既有问题**,本轮已取 `origin/main` 版本逐条比对确认**完全相同**:
    状态页隔离子进程在本机 cp1252 控制台下解码失败、drill 缺 `.env` 的 `LIVE_VT_SYMBOL`;
    另有 `tests/test_constitution_round11.py` 与 `round12.py` 的 **10 项**曾在本机因 `scripts/bootstrap.py` 的
    「bootstrap 必须先于 vnpy.trader.database 导入」顺序检查而失败——**这次失败是本分支引入的,不是环境问题**
    (取 origin/main 版本对比:同样的两个文件在 origin/main 上是 **92 项全过**)。
    根因:这两份用例经 `const.snapshot_protected_state()` 预加载 `vnpy_ctastrategy.backtesting`,连带加载了
    `vnpy.trader.database`;而「bootstrap 一定先于它导入」这条顺序原先是**靠 `conftest → segment_ledger → bootstrap`
    兜住的**,登记文件与交易栈解耦(不再 import bootstrap)后那层兜底没了。
    修法:在 round11/round12 的**导入期**显式 `import bootstrap`(交易栈侧固定时区的既定前置),
    并在研究侧边界测试里钉成一条**可跑在研究 CI 上**的 AST 断言(两份文件必须在模块导入期 import bootstrap,
    且不得在它之前 import vnpy 族模块);
    不把 bootstrap 塞回 `scripts/segment_ledger.py`(那会把交易栈重新拖回研究侧,破掉解耦保证);
    修完这两个文件**合跑 92 项全过**,与 origin/main 一致);
    断耦演练(停掉毕昇全部容器)交易与风控不受影响。
- **第二个品种的复权连续序列已构建并核实**(Issue [#18](https://github.com/laiyinyizao007/invest/issues/18),
  ADR `20261113-evolution-engine`「数据(本机核实)」):除玉米外,**塑料 L** 与**豆粕 M** 的 15 分钟复权连续合约已构建完成,
  文件在 `C:/Dev/data/invest/derived/`(不在仓库内)。
  - L:2020-08-24 21:00 ~ 2026-06-15 14:45,32011 根 / 1406 个交易日,换月 **19 次全部精确**
    (含 2021-12 的 L2201→L2202→L2205 两次连续换月);
  - M:2020-07-31 21:00 ~ 2026-06-15 14:45,32379 根 / 1422 个交易日,换月 **18 次全部精确**;
  - C(基准):32540 根 / 1429 个交易日,换月 28 次全部精确。
  - 这解掉了 PBO 重启(第二个品种)的**数据侧**前置:跨度约 5.8~5.9 年,与玉米同量级。PBO 本身仍须单独立项与 ADR。
  - **⚠️ 记进 ADR 的边界事实**:三个品种的 15 分钟文件都停在 **2026-06-15 14:45**(封存前向留出集的边界),
    但**日线文件是用新浪日线补齐到"今天"的**(C 到 2026-11-09、M/L 到 2026-11-13),**已越过封存边界**。
    目前没有任何研究代码读 `*_1d_adj.csv`(只读 `*_15m_adj.csv`),还没有泄漏;但只要有人改去读日线,
    就会把已封存的前向留出集当可见数据用掉。约定:**进化回路只准用 `*_15m_adj.csv`**。
- **阶段 3 前置:梯子步长 η 的标定与登记**(Issue [#18](https://github.com/laiyinyizao007/invest/issues/18),
  ADR `20261113-evolution-engine`「阶段 3 前置:DSR 的 V 与梯子步长 η 的定法」):η = 1.5 × SE(配对日盈亏差),下限 0.25 夏普——
  **方法**早先已在 ADR 里定死,这次把**数值**算出来并登记:**η = 1.10 夏普**(早于任何候选生成)。
  - **标定脚本** `scripts/calc_ladder_eta.py`(纯数据 + 纯统计,不碰交易接口):在**池化验证段** 2022-07-01 ~ 2023-12-31
    (n=367 个交易日,即阶梯实际汇报成绩的那一段)上,取网格里"相邻一步"的 60 对参数做配对比较,
    对逐日盈亏差做**分块自助**(块长 5 个交易日,2000 次重采样)估 SE(ΔSharpe)——分块而非逐日独立重采样,
    因为日盈亏自相关,逐日重采样会低估方差。SE 中位数 **0.7306**,取中位数而非均值(单对的 SE 受个别参数组合影响很大,
    中位数对离群组合稳健),**η = max(0.25, 1.5 × 0.7306) = 1.096 → 取 1.10**(下限未触发)。
  - **口径对齐流水线**:真实链路里每个验证段是一次独立回测(段与段之间不持仓),标定也按三段分别回放再拼接——
    最初把 2022-07 ~ 2023-12 连着跑一整段,持仓会跨过段边界,标的与流水线就不是同一件事(SE 中位数 0.7297 → 0.7306,登记值不变)。
  - **交叉校验**:解析式 SE(配对差标准差 / 池化波动)与自助值同量级(0.670 vs 0.670、1.063 vs 0.878)。
  - **敏感性**:同一方法在 W1 训练段上得 SE 中位数 0.6731、η = 1.010,与验证段相差 8%——不是单一窗口的偶然;取较大者登记。
  - **纪律**:**测试段(2024-01-01 起)全程未触碰**(该段早已被消耗,不得复用)。
  - **性质**:η 是**汇报阈值**(阶梯何时更新汇报值),**不是晋级门槛**,与 DSR 0.95 互相独立;
    看到候选之后再改它,性质上等于放宽阈值——所以必须先定死。
  - **一处口径不一致已做敏感性对照**:标定脚本的 `setting_of()` 用 `max_daily_losses = 3`,而模板默认是 **2**
    (熔断阈值小会把某些参数对的成交截断、配对差退化)。用同一口径(池化验证段、2000 次重采样、同种子)
    把阈值改成 2 重跑:SE 中位数 **0.7312**(用 3 时 0.7306)、**η = 1.097**(用 3 时 1.096)——
    **都取整为 1.10**。即 **η 对熔断阈值不敏感**(差 0.08%),登记值不变,
    不存在"为了凑一个数去调阈值"的情况。

### Fixed（修复）
- **提交基准不许再用 `git status`(第 26 轮 codex 的 Critical 1)**:第 25 轮把"跨轮次逃逸"的基准
  定成"仓库提交里的那份",实现上用的是 `git status --porcelain` + `git ls-tree`。
  **但 `git status` 会应用 filter**:filter 的 `clean=` 是一条**命令**,由 git 自己去执行,
  而候选能写 `.git/config` 与受信目录下的 `.gitattributes` —— 于是候选只要写下一条 filter 命令,
  **守卫自己跑的那次 `git status` 就替候选执行了它**(实测:标记文件被写了出来,
  静态筛查 **0 findings**——那条命令是 git 执行的,不是候选执行的)。
  守卫从"检查者"变成了"执行面"。
  - 改成三条硬规矩:①**只用不应用 filter 的读法**(`git ls-tree` 读树、`git cat-file` 读对象,
    两者只读对象库、不碰工作区文件);②**调用时关掉一切能执行命令的配置**
    (`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` 指向空设备、
    `HOME`/`XDG_CONFIG_HOME`/`USERPROFILE` 重定向到临时空目录、`-c core.fsmonitor=false`、
    `-c core.pager=cat`、`GIT_OPTIONAL_LOCKS=0`);③**不用 git 去比内容**——
    取**提交里那份 blob**,与工作区内容只比"**去掉 CRLF 之后**是不是一样"
    (第 25 轮自己算 blob 哈希曾把 CRLF/LF 误判成"被改写过",那次的教训是"内容基准要交给内容的主人";
    现在两边都不自己复刻,只比换行,换行之外的任何差异仍判不一致——**不放宽任何已锁定的阈值**)。
  - 守卫在**构造时**(候选运行之前)记下仓库元数据的内容指纹,`check()` 时核一遍,
    **不一致就 fail-closed**(候选改写仓库元数据这件事本身被管住)。
    记哪几份见下面「第 26 轮第二轮 codex 复核之后补修的那几条」(config/HEAD 在哪、第 26 轮 codex
    第二轮实测出 linked worktree 的坑之后又改过一次)。
- **通配符导入(`from 包 import *`)补上提交基准(第 26 轮 Critical 2)**:这一路原来**只有**"快照"这一关,
  没有提交基准 → 第 25 轮堵掉的那条跨轮次逃逸在通配符这条路上**原样可用**
  (上一轮改写 `strategies/base.py`、这一轮 `from strategies import *`)。
  现在通配符路径上的**每一个 `.py`** 都套上与 `check()` 同一把尺子的提交基准核对。
- **相对导入上,视图的"拒绝"被 CPython 的回退绕过 → 拿到真 ctypes(第 26 轮 Critical 3)**:
  CPython 的 `IMPORT_FROM` 回退键是 `<返回模块的 __name__>.<名字>`(绝对名),
  而查表用的是**原始名**——相对导入时原始名是 `""`(`from . import x`)→ **查表永远落空**
  → 视图拒绝之后回退把**真模块**交给候选 → 那个模块 `import ctypes` → `CDLL` 成功(实测)。
  改用**返回模块的 `__name__`** 查表;静态层同时按 `__package__` 把相对 from-import 还原成绝对名再筛。
- **危险名单补漏(第 26 轮 High 1/2)**:`_winapi`(Windows,`CreateProcess` 可达——**实测**)、
  `_ctypes`(**推理**)、`_posixsubprocess`(Linux 对偶,**推理**,本机无法实测)、
  `codeop`(`compile_command` 把字符串编译成代码)、`types`(`FunctionType` 能用真内建造函数)、
  `posixpath`/`ntpath`/`genericpath`(报出那条向量时用的杠杆:`posixpath.join.__globals__` 里
  既有真内建也有 `os`)——全部进 `DANGEROUS_MODULE_NAMES`,导入门与属性层同时生效。
- **提交基准的路径基准统一(第 26 轮 Medium 1)**:原来提交基准是 repo_root 口径、调用方按 toplevel 口径比,
  `repo_root ≠ toplevel` 时**受信导入被整片误杀**(埋着的误杀)。现在统一到 **toplevel 口径**
  (`git ls-tree --full-tree` 从 toplevel 跑),并补了"代码在仓库子目录里"的回归用例。
- **两处 fail-open 改 fail-closed(第 26 轮 Medium 2 / 静态层)**:`check_package_wildcard` 的
  `except OSError: continue` 改成拒;静态层现在与运行期**共用同一份危险名单**
  (`import ssl/codecs/mmap` 原先静态 0 findings,只在运行期才拒——而运行期的拒绝发生在 import **之后**)。
- **静态层筛相对导入(第 26 轮 Low 3)**:按 `__package__` 还原绝对名;还原不出来就**拒**(fail-closed)。
  口径说明:`screen_source()` 新增的 `package` 参数**没有生产调用点传值**(候选永远是不在包里的单文件模块),
  所以生产里的实际行为是"还原不出绝对名 → 直接拒";按 `__package__` 还原这条能力目前只有用例在用。
- **两处缓存(第 26 轮 Low 1/Low 2)**:`git status` 换成 ls-tree/cat-file(内容按 sha 寻址、不可变 → 缓存永远有效),
  gitdir 路径进程内缓存;能力泄漏缓存原来用 `len(vars(module))` 判失效,
  **等量替换**成员会用到旧判定(实测:应为 `ctypes`,实得 `None`)——现在缓存"成员名 → 成员对象",
  复用前逐个用 `is` 核对。
  (目录扫描按 mtime 的那处缓存**后来撤掉了**——见下面「第 26 轮第二轮 codex 复核之后补修的那几条」。)
- **第 26 轮第二轮 codex 复核(34/50)之后补修的那几条**(codex CLI,`mcp__zen__clink`;
  它的两条 HIGH 都自己做了端到端实测,成立):
  - **linked worktree 下 `.git/config` 根本没进指纹(实测)**:指纹原来按
    `git rev-parse --absolute-git-dir` 找,而 linked worktree 里那是 **per-worktree** 的 gitdir,
    `.git/config` 在 **common dir** 里 → 指纹键只有 `['HEAD','index']`,**没有 config**,
    候选改写共用 config 完全不被发现。**本仓库自己就是这个形态**(`invest-26` 是 `invest` 的
    linked worktree)。现在 `config` 与 `info/attributes` 用 `--git-common-dir` 解析,
    `HEAD` 仍取 per-worktree gitdir;回归用例用**真的 linked worktree**(`git worktree add`)来测。
  - **`__pycache__/*.pyc` 盖过已核对过的源码(实测)**:提交基准核对的是源码,而 CPython 导入时
    **优先用 `.pyc`**,`.pyc` 只校验头部里的 mtime+size(候选能伪造)→ 实测守卫放行、导入机制执行的
    是**伪造字节码**(候选代码在真内建下跑出标记文件)。现在核对完源码就把该 `.pyc` **删掉**,
    让导入机制从刚核对过的那份源码重新编译(残余:删除与导入之间的极小窗口、只读挂载那一支不清理)。
  - **`.git/index` 不再进指纹**:提交基准走 `ls-tree HEAD` + `cat-file`,**从不读 index**;
    而 index 会被任何一次 `git add`/`commit` 改写 → 记它只换来"别人提交了一次就全部误杀"。
  - **git 二进制钉死 + 子命令白名单**:git 的绝对路径第一次用到时钉死(`shutil.which`),
    且 `_git_run` **只允许** `rev-parse`/`ls-tree`/`cat-file` 三条只读子命令,别的直接抛错——
    把"守卫永不执行候选写的东西"从**约定**变成**机制**(以后有人加回 `git status` 就复发不了)。
  - **目录扫描的 mtime 缓存撤掉**:粗粒度文件系统(1~2 秒一档)上同一秒内新增的文件会看不见,
    而受信目录只有个位数文件——不值得拿安全换性能。
  - **口径纠正**:"静态层与运行期共用同一份名单"改成"静态层认 `FORBIDDEN_MODULES ∪ DANGEROUS_MODULE_NAMES`,
    运行期再多一份配置追加的名单";ADR 里"容器隔离"从**既有边界**改回**阶段四 4b 的前置(尚未实现)**——
    目前**只有人工审批**这一道闸。
  - **这一轮补修的验证**:用例 33 条 → **37 条**;**变异验证 15 处全部变红**(比上一轮多 3 处:
    linked worktree 指纹、`.pyc` 清理、git 子命令白名单)。
  - **补修时自己写错的一处(如实记录)**:那条"即使指纹不报警也不能跑会应用 filter 的 git 命令"的用例,
    第一版把 filter 写在守卫**构造之后** → 指纹先报警、守卫根本没跑 git → 用例**测不到命令选择**,
    "把提交基准改回 `git status`"的变异因此抓不住。改成"把 filter 写在守卫构造**之前**"之后,
    守卫真的会去跑 git,这时标记文件不出现才说明得了问题。
  - **分数口径**:codex 给的 **34/50** 是**补修之前**的分数;两条 HIGH 与三条 Medium 在这次补修里改了,
    但**补修之后没有再跑一轮 codex** → "补修之后的分数未知",不替它加分也不替它扣分。
- **第 26 轮的验证**:新增 `tests/test_sandbox_round26_rules.py`(先 33 条,补修后 **37 条**),
  每条规则都有**双向**探针
  (放行 + 拦截)并断言"**是哪一条规则**拒的";**变异验证**把这一轮的修复逐一改回旧行为
  (先 12 处,补修后 **15 处**),实测**全部变红**(逐处结果见下)。探针与变异脚本都放在 `%TEMP%`,跑完即删,不进仓库。
  - **本轮自己写错的五处(如实记录)**:①在受限导入器里用了 `getattr`——那个函数的 `f_globals` 是干净的
    (只有受限内建)→ `NameError`,是回归用例立刻变红才发现的,改用闭包里的 `object.__getattribute__`;
    ②元数据指纹拍晚了(先改配置再构造守卫 → 指纹记的是已被改过的配置,测不出东西),
    改成"先构造守卫、再让候选改写";③目录扫描缓存第一版写成"内容哈希"——那等于每次全扫,缓存没有意义;
    ④filter 对照探针第一版"整份套件一起跑时必失败":靶子文件内容没真的改动 → git 走 stat 快路径,
    根本不比对内容、也就不会用 clean filter(单跑时能触发靠的是 git racy-clean 的巧合);
    改成"靶子文件内容真的改掉"+ 命令用"脚本文件 + `sh <脚本>`"(不用 `touch`——
    filter 是 git 交给 sh 执行的,没装 Git Bash 的机器上 `touch` 根本不存在);
    ⑤(第二轮复核补修时)"即使指纹不报警也不能跑会应用 filter 的 git 命令"那条用例第一版把 filter
    写在守卫**构造之后** → 指纹先报警、守卫根本没跑 git → 用例测不到**命令选择**这条规则,
    "把提交基准改回 `git status`"的变异因此抓不住;改成"把 filter 写在守卫构造**之前**"。
  - **改了一处既有测试**:`tests/test_candidate_escape_production_path.py` 的 loader 探针原来用 `import types`
    造模块对象;`types` 进名单后它在静态层就被拒(变成空断言)。已改写成不依赖 `types` 的写法,
    并让它比原来**更强**(原来 `__loader__` 根本不存在,探针恒为 False)。
  - **没有修的一半(如实登记)**:视图仍会递出带 `__globals__`/`__code__` 的 python 函数与类
    (实测 `posixpath.join.__globals__['os']` 可达)。泛化修法(把函数复制成"净化版")会**打断正当用法**:
    `dataclasses` 处理每个字段都要读 `sys.modules`,`enum`/`typing` 的 globals 里也有 `sys`——
    净化掉 `sys` → `@dataclass` 当场失效;保留 `sys` → 那条链原样敞着。
    报出那条向量的杠杆已进名单堵掉,但**泛化问题未修**,真正的边界仍是人工审批 + 容器隔离。
- **跨轮次沙盒逃逸(第 25 轮 codex 第二轮的 Critical 1)**:受信目录(`strategies/`)的完整性基准
  改成**仓库提交里的那份**,不再是"每次运行重拍"的快照(Issue [#26](https://github.com/laiyinyizao007/invest/issues/26),
  ADR `20261113-evolution-engine`「第 25 轮补记」)。
  - **为什么必须换**:快照是每次导入重拍的,挡得住"同一次运行里现写现导",挡不住**"上一轮写、这一轮导"**——
    候选用 `numpy.savetxt` 改写了受信模块,第 1 轮导入被正确拒绝,第 2 轮**重拍**的快照却已经收进了被篡改的文件 →
    放行 → 那个模块在**导入机制**里执行(拿到真实内建)→ 任意命令执行(codex 端到端复现,标记文件确实被写了出来)。
  - **基准取不到就拒**(fail-closed,查不了等于没查);规则按"那个目录**能不能写**"分岔:
    **容器里 `/app` 是只读的**(rootfs `read_only: true`,`strategies/` 烤进镜像)→ 写能力不存在,不需要基准;
    **宿主机跑 CLI** → 目录可写,必须用提交基准核对。
  - **一处自曝的误判(如实记录)**:基准最初是"自己算 git 的 blob 哈希",在 Windows 上会把
    **没被改过**的文件判成"被改写过"(工作区 CRLF vs 提交 LF,`core.autocrlf` 归一化后其实一致)——
    受信目录里的正常模块一律被拒。改用 **git 自己的比较口径**(`git status` + `git ls-tree`)后消失。
    教训:内容基准要交给内容的主人,自己复刻哈希算法只会在换行/编码/过滤器这些细节上出错。
- **视图的"拒绝"被 CPython 的 `import_from` 回退绕过(第 25 轮 High 2)**:视图用"抛 `AttributeError`"表示不给,
  而 `IMPORT_FROM` 在 `getattr` 失败时会**清掉异常、回退到 `sys.modules["pkg.name"]`**——"拒绝"恰好触发了"交出真模块"。
  实测 `from pkgx import sub` 拿到的是**真模块**(候选手里就有一份带 `ctypes` 的真模块)。
  受限导入器现在在 fromlist 这一路**显式查一次 `sys.modules`**,命中真模块就按真模块的规矩裁决。
- **危险名单两层口径不一致(第 25 轮 High 4)**:危险名单原先**只在属性层**生效,导入门放行了一大片——
  实测 `httpx`/`urllib3`/`ssl`/`codecs`/`gzip`/`tarfile`/`zipfile`/`sqlite3`/`mmap`/`atexit` 全部放行,
  网络外传(`httpx.get(...)`)与文件写(`codecs.open(...)`)的能力都还在。导入门现在**复用同一份名单**;
  名单同时补齐了写能力与外传通道(`codecs`/`mmap`/`sqlite3`/`ssl`/`bz2`/`lzma`)——
  静态层只拦**字符串字面量**里的 `"open"`,而 `codecs.open` 是 `ast.Attribute`,"从内建里拿掉 open"没封住写能力。
- **一处误伤,已回退(第 25 轮 High 3)**:导入门曾整块拒绝"会把能力递出来的模块",于是
  `import matplotlib` 直接 `ImportError`(matplotlib 建字体缓存要用 `subprocess`)、`psutil` 同理——
  候选只想画个图却整个模块都用不了。**误伤比漏检更糟**:能力交付改由属性层的模块视图负责,
  导入门只管"危险模块按名单拒"。
- **静态层看不见 fromlist(第 25 轮 Medium 6)**:`from numpy import ctypeslib` 与 `import numpy.ctypeslib`
  是同一件事,但原来只看 `node.module`(numpy)→ 这条写法**放行**;而运行期的拒绝发生在 import **之后**
  (模块已经在导入机制里以真实内建执行过,副作用已经发生)。现在逐个检查 names。
- **四处 fail-open 改成 fail-closed(第 25 轮 Medium 9)**:解析不到来源文件、包位置 `stat` 不出来、
  包内文件 `stat` 不出来、能力来源取不到——原先都是"拿不准就放行",与本文件其它地方的口径相反。
- **能力泄漏判定按模块缓存(第 25 轮 Medium 7)**:原来每次属性访问都全量扫 `vars(module)`
  (实测视图取一个属性 14.7µs、候选热点循环慢 19 倍:20000 次 `np.random.normal()` 沙盒 0.327s vs 普通 0.017s)。
- **视图透传 `__version__`(第 25 轮 Low 11)**:原先只透传 `__all__`/`__name__`/`__doc__`/`__package__`,
  `np.__version__` 直接 `AttributeError`——连"记录本次评估用了哪个库版本"这种正当需求都做不到。
- **宪法守卫对三份新的 deny 名单是空的(第 25 轮 Medium 8)**:提取器**不认 `frozenset({...})` 写法**,
  `DANGEROUS_MODULE_NAMES`/`FORBIDDEN_MODULE_PATHS`/`CAPABILITY_MODULE_NAMES`(=外加 `FORBIDDEN_KEY_STRINGS`)
  既没被解析、也没登记进注册表——两头一凑:从危险名单里删掉 `"ssl"`,守卫照样全绿。
  现在提取器认 `frozenset`/`set` 调用,四份名单登记进注册表;守卫本体已跑通(退出码 0)。
- **本轮的验证方式(新增 26 条用例)**:`tests/test_sandbox_round25_rules.py`(17 条)与
  `tests/test_constitution_round25.py`(9 条),每条规则都有**双向**探针(放行 + 拦截),
  并断言"**是哪一条规则**拒的";**变异验证**把第 25 轮的 6 处修复逐一拿掉,6 处全部被抓到(用例变红)。
- **候选"写辅助模块再 import"这条逃逸,以及另外四条已封的逃逸(Issue [#26](https://github.com/laiyinyizao007/invest/issues/26),
  ADR `20261113-evolution-engine`「第 22 轮补记」):本轮全部结论来自**动态探针**(真的写文件、真的 import、真的看结果),
  不是静态推理——静态推理一定漏(见 memory `static-ban-rules-need-dynamic-probes`)。
  - **实测到的五条逃逸**:①候选用 `numpy.savetxt` 写一个 `.py` 再 import 它(辅助模块在 import 机制里执行,拿到真实内建);
    ②候选模块的 `__loader__.exec_module(m)`(用真实内建把源码再跑一遍);③`e.__traceback__.tb_frame.f_builtins`
    (拿到我们自己的模块帧里的真实内建);④`from <包> import <子模块>`(子模块的 import 在**导入机制内部**完成,
    **不经过**候选那层的守卫;而 `scripts/` 这种没有 `__init__.py` 的目录是**命名空间包**,origin 为 `None`,
    凡“看 origin”的规则都会放行——实测 `from scripts import research_api` 拿到带 `subprocess` 的框架模块,
    等于任意命令执行);⑤`from <包> import *`(通配符):逐项裁决对 `"*"` 无能为力,而**导入机制自己**会按包的
    `__all__`(有的话)去 import 那些子模块,整条路**不经过守卫**——探针实证:候选往带 `__all__` 的包里写一个
    只有一行 `open(...).write("x")` 的模块,`from 包 import *` 之后标记文件被写了出来,
    说明它**确实在导入机制里执行了**。五条都**不经过禁用名单**——名单是黑名单,堵不住机制。
  - **规则改成按路径裁决**(`scripts/research_sandbox.py::RepoImportGuard`):仓库内只允许 `ALLOWED_REPO_IMPORT_DIRS`(`strategies/`)里
    **且在候选开始运行之前就已存在、此后没被改过**的模块;仓库外(标准库、站点包)也必须"运行之前就存在"
    (站点包在宿主机上通常可写,这是第 21 轮遗漏的洞)。定位来源用 `PathFinder.find_spec`——只找文件、**不执行**被检查的模块。
    时间比较按文件系统自身的时间粒度取整后再比(NTFS 100ns、网络盘可达 1s),否则会漏掉"刚写出来就被取整到开始时刻之前"的文件。
  - **逃逸二**:候选模块不再挂 `__spec__`/`__loader__`。**逃逸三**:受限导入器用自己造的一帧(globals 里 `__builtins__` 是受限字典),
    并且**在 except 块之外**抛干净的异常——在 except 里抛,原始异常会挂在 `__context__` 上,候选顺着它一样能摸到 import 机制的帧。
  - **逃逸五(封堵)**:通配符这一路改成**扫目录**(`RepoImportGuard.check_package_wildcard`):
    包内(仓库侧)的每个 `.py` 都必须在快照里、且此后没被改过;只要多出一个文件(候选写的)就拒;
    **仓库之外的包同样要过"运行之前就存在"这一关**(站点包在宿主机上通常可写,而包自己的 `__all__`
    可能正好列着某个现在还不存在的子模块名——候选把它写出来,通配符导入就由导入机制去 import 它)。
    这条**独立于**包位置那条规则——通配符由**导入机制**按 `__all__` 自己拉子模块,守卫连名字都看不到。
    仓库里 `strategies/__init__.py` 目前没有 `__all__`,所以这条在当前仓库里是**潜伏**的;
    探针用**自造的包**把它逼出来,不靠"现在还没事"。
  - **逃逸四(封堵)**:受限导入器在 `fromlist` 非空时**另外裁决**——先裁**包位置**
    (`RepoImportGuard.check_package`,用 `spec.submodule_search_locations`;这条必须**独立存在**:逐项裁决只管**模块**,
    候选 `from <包> import <某个不是模块的名字>` 时逐项裁决看不见任何东西),再**逐个裁 fromlist 里的子模块**
    (于是“候选自己刚写出来、但所在目录合法”的模块同样被拒)。
  - **白名单登记进宪法历史守卫**(`tools/check_constitution_history.py`):`ALLOWED_REPO_IMPORT_DIRS` 与 `BUILTIN_ALLOWED_OVERRIDES` 同等对待,
    **只减不增**,引用次数也进入"只能增不能减"的守望。
  - **如实记录的残余**:第三方模块(如 `numpy`)的帧里**仍然是真实内建**——实测确认。
    所以"进程内执行不受信代码"做不到安全,能力隔离是纵深防御的一层,**不是边界**;
    真正的边界仍是**人工审批 + 容器隔离**。ADR 里"把能力收走"那句话已就地更正:
    收走的是**候选自己那些帧**里的能力,不是整个进程的。
  - **用例**:`tests/test_helper_module_escape.py`(13 条)+ `tests/test_candidate_escape_production_path.py`(35 条,走生产入口 `import_candidate`),
    共 **48 条**。**12 个变异全部被抓住**(还原 loader / 用模块级全局造帧 / 去掉路径裁决 / 去掉时间规则 / 去掉允许目录 /
    去掉快照存在性 / 去掉 mtime 比对 / 去掉包位置裁决 / 去掉 fromlist 逐项裁决 / 去掉通配符目录扫描 /
    去掉通配符对仓库之外包的时间规则 / 去掉执行原语的禁用名单)。
  - **一条方法教训(已写进 ADR)**:变异验证里有个变异**没被抓住**——查下来是那条用例**根本没走到它想走的那条路**
    (没把假仓库插到 `sys.path` 最前面,`strategies` 解析到了**真仓库**那个包,import 因“找不到模块”而失败,断言照样绿)。
    **断言“被拒绝了”不够,还要断言“是被哪一条规则拒绝的”**;修好之后 12 个变异全部变红。
    后半句在第 5 条上又应验了一次:换一条规则来拒绝时,用例也得跟着变红。

- **候选的第六类逃逸:执行原语不经过 `__import__`,按路径裁决对它无效**(Issue [#26](https://github.com/laiyinyizao007/invest/issues/26),
  ADR `20261113-evolution-engine`「第 23 轮补记」):第 22 轮那套"按路径裁决导入"只在**候选调用 import** 的那条路上生效,
  而 `runpy`/`code`/`pkgutil`/`zipimport` 这类**执行原语**根本不经过 `__import__`——于是整套路径规则对它们**完全无效**。
  - **对照实验**(同一个"候选在运行期间写出来的模块",同一次运行,两条路):
    `import 它` → **被拒**("该文件在候选开始运行之后才出现");
    `runpy.run_path(它)` → **执行成功**,辅助模块在**真实内建**下跑起来
    (探针:辅助模块只有一行 `open(...).write("x")`,`open` 在候选自己那些帧里是被收走的,
    只有被导入机制执行才拿得到真内建,所以标记文件一旦出现就说明辅助模块在真实内建下执行了)。
  - 其它实测成立的执行原语:`code.InteractiveInterpreter().runsource(...)`(直接执行字符串,连文件都不用)、
    `zipimport.zipimporter(...).exec_module(...)`(从 zip 里执行模块)、`pkgutil.get_loader(...)`
    (拿到 loader,而 loader 的 `exec_module` 同样能执行任意文件)。
  - **处置**:这一类**不能**再加一条路径规则(它们不走那条路),只能按**名字**堵——
    `runpy`/`code`/`pkgutil`/`zipimport`/`pty`/`platform`/`webbrowser` 进 `FORBIDDEN_MODULES`,
    静态层与运行期层**两道独立的闸**同时生效。误伤面为零:仓库里的策略模板只 import
    random/dataclasses/datetime/vnpy/strategies。
  - **但名单式封堵不收敛(本轮最重要的结论,不能含糊)**:能力会从**别的模块再导出**。
    实测 `numpy.ctypeslib` 把 `ctypes` 整个再导出一遍,而 `numpy` 是候选正当需要的包,于是
    `numpy.ctypeslib.ctypes.CDLL("kernel32").GetCurrentProcessId()` 能直接调原生代码(实测返回 81160)——
    路径裁决、内建限制、受保护对象指纹**全都管不着**。这条路**堵不住**,也不该假装堵得住。
    所以边界说得更硬:**"进程内执行不受信代码"做不到安全**,名单只能拿掉便宜的胜利,
    真正的边界仍是**人工审批 + 容器隔离**。
  - **新增一条纪律**:"规则管得到的那条路"不等于"只有那条路"。第 22 轮的规则全都挂在 `__import__` 上,
    "绕开 `__import__`"就是一条没人看过的路;每加一条规则都要问一句:**还有哪些执行路径不经过它?**
  - **用例**:新增 16 条(运行期层单独拒执行原语 7 条、名字层 7 条、
    "候选不得让运行期写出的模块被执行" 1 条、"能力再导出这条残余确实漏着" 1 条),共 **48 条**;
    **12 个变异全部被抓住**(新增 M12:去掉执行原语的禁用名单)。
    新增用例 `test_the_candidate_cannot_reach_a_native_library_through_an_allowed_package`
    断言的是"**确实漏着**",目的是把残余钉在测试里——哪天架构变了它会变红,提醒人更新 ADR 残余登记。

- **候选的第七类逃逸:守卫自己那一帧的 `f_globals` 里放着**真导入器**(Issue [#26](https://github.com/laiyinyizao007/invest/issues/26),
  ADR `20261113-evolution-engine`「第 24 轮补记」):第 21 轮把受限导入器的 `f_builtins` 换成了受限字典,
  可它的 **`f_globals` 里还放着 `real_import`**。候选顺着 ImportError 的异常链走到守卫那一帧,
  一句 `tb_frame.f_globals["real_import"]` 就把真导入器拿走,接着 `real_import("os").system(...)`
  = **任意命令执行**(探针实测:标记文件被写出来)。
  - **对照**:`tb_frame.f_builtins["setattr"]` 拿到的是**受限**字典(第 21 轮封的);
    `tb_frame.f_globals["real_import"]` 拿到的是**真导入器**(第 24 轮才封)。
  - **处置**:真导入器、禁用名单、路径规则一律搬进**闭包单元**;守卫那一帧的 `f_globals`
    换成一份**干净的新字典**(`types.FunctionType(code, {"__builtins__": 受限内建}, ..., closure)`)。
  - **踩坑留档**:嵌套函数的 `__globals__` 是**定义它的模块**的全局字典,不是外层帧的 locals——
    想"清空外层那一帧"是清不掉的,直接 `clear()` 会把整个模块命名空间抹掉(实测:模块级类全丢)。
  - **新纪律**:**守卫自己那一帧也是攻击面**。凡是准备放进守卫帧里的值,都要问一句:
    "候选摸到这一帧,能不能拿到它?"
- **模块视图:候选拿到的不是模块,是视图**(第 24 轮):codex 实测 `import logging` 之后
  `logging.sys.modules["os"].system(...)` = 任意命令执行,而**四层防线全绿**——
  它们全都只看"名字",而这一条走的是"别的模块把危险模块当属性递出来"。
  - **处置**:候选拿到的模块是 `ModuleView`:属性值是危险模块 → 不给;属性值是会递出能力的模块 →
    整个不给;属性值是别的模块 → 给一份**视图**(递归生效);下划线属性(`__class__`/`__dict__`)一律不给。
- **"名单式封堵不收敛"要更正一半:能力按**对象来源**判定**(第 24 轮):判据从"名字像不像"
  改成"**这个对象是从哪儿定义的**"(`__module__`)——凡是定义在 `ctypes`/`subprocess` 里的对象
  (模块、类、函数都算)一律不给,于是 `subprocess.Popen`、`ctypes.CDLL` 这种"只递出类不递出模块"
  的形态也堵住了,而"换个名字再递一次"骗不过它。`numpy` 本身照常放行(候选正当需要),
  它的三个危险角落按**全名**禁:`numpy.ctypeslib`(递出 `ctypes`)、`numpy.f2py`
  (**import 它就会跑 f2py 的主程序**,实测)、`numpy.distutils`。
- **走栈禁令**(第 24 轮):静态层新增 `__traceback__`/`tb_frame`/`f_globals`/`f_locals`/`f_builtins`/
  `__closure__`/`cell_contents`/`__getattribute__` 一律不得引用(裸名、属性名、字符串三种写法都拒)。
  理由:第三方模块的帧里就是真实内建——实测 numpy 自己的 traceback 里印着
  `numpy/lib/_npyio_impl.py` 第 454 行的 `open(os.fspath(file), "rb")`;
  我们管不了第三方帧的 globals,于是禁掉"顺着异常链走栈"这个**能力本身**。
- **几处 fail-closed 与快照加严**(codex 第二轮复核指出的中危项):`stat` 失败一律按
  "**查不了等于没查**"拒(原先有一支直接放行);快照加 **sha256**(只比 mtime 会被"改写内容再把
  mtime 还原"骗过,粗粒度文件系统上同秒改写 mtime 根本不变),现在比 mtime + size + sha256 三项;
  调用点**显式**传允许目录白名单(靠默认值的话,改默认值就能放开整个 `scripts/`);
  守卫自己的调用一律包 try/except 换成干净的 ImportError——**守卫的帧不进候选看得见的 traceback**。
- **用例**:逃逸相关的三条测试文件共 **102 条**(新增 `tests/test_sandbox_module_view.py` 49 条:
  模块视图、能力判定、按全名禁用的子模块、新名单、快照哈希、fail-closed、调用点显式白名单;
  改写 `tests/test_candidate_escape_production_path.py` 若干条:静态层与运行期**分层测**、
  断言"**是哪一条规则**拒的"、修掉 codex 指出的**假绿**用例(没把假仓库插到 `sys.path` 最前面,
  import 因"找不到模块"而失败,断言照样绿))。
  **18 个变异全部被抓住**(新增 M13 关掉模块视图 / M14 能力判定退回只看模块名 / M15 快照去掉内容哈希 /
  M16 stat 失败改成放行 / M17 去掉按全名禁用的子模块规则 / M18 静态层不再禁走栈属性;
  M2 换成"守卫帧的 globals 里又放回真导入器")。
- **如实记录的残余**(第 24 轮):**能读 `f_locals` 的候选仍能拿到闭包单元**——实测 Python 3.13 的
  `f_locals` 里看得见 `real_import`。运行期这一层**只**保证 `f_globals` 干净;原因是纯 Python 的死结:
  凡是这一帧能拿到的对象,候选拿到这一帧就能拿到它(闭包单元、默认参数、代码常量、塞进容器,全都可达)。
  挡住它的是**静态层的走栈禁令 + 人工审批 + 容器隔离**。新增用例
  `test_the_runtime_layer_still_exposes_the_closure_cells_through_f_locals` 断言的正是"**确实漏着**",
  把这条残余钉在测试里。第三方模块的帧里仍是真实内建(继续成立)。
- **G8 第一轮自查带出的两处小修**(第 24 轮):不是漏洞,但该改,已改。
  - **模块视图缓存原来永不回收**:`_VIEW_CACHE` 是 `WeakKeyDictionary`,可视图对象**强引用**着模块,
    value→key 的强引用把弱键托住,条目永不消失——每个导入过的模块都永久驻留(无界增长)。
    现在视图改用**弱引用**持有模块(`ModuleView._view_module_ref`),模块由 `sys.modules` 持有,视图照常可用,
    缓存条目可被回收。**教训**:用弱引用做缓存时,value 里不能有指向 key 的强引用。
  - **危险模块短名的误伤面收窄**:`FORBIDDEN_STRINGS` 里的 `os`/`sys`/`nt`/`posix` 原本是**全局**禁字符串,
    于是策略里只要出现一个恰好等于这些词的字符串字面量(状态名/列名/打印内容)就被拒。
    现挪进新的 `FORBIDDEN_KEY_STRINGS`,**只按语境拦"按键取"这个动作**(下标键 / 字典取值方法的参数 / 关键字参数名)——
    要封的洞(`tb_frame.f_globals["os"]`)正是"按键取",不是这些字符本身。
    (字符串匹配一直是精确相等,不是子串,所以 `close`/`cost` 从来没被误伤过。)
  - **用例**:新增 9 条(弱引用缓存 1 条 + 视图仍可用 1 条 + 按键取语境 7 条),
    逃逸相关用例共 **111 条**;**变异增加到 20 个,全部被抓住**(新增 M19 视图改回强引用 / M20 去掉按键取规则)。

### Changed（变更）
- 状态页后端方案由『不使用 vnpy_webtrader』改为『fork 并改造』(用户决定,2026-11-12;仅文档):ADR 新增『fork 范围』——上游 MIT,保留版权与许可证全文;逐项处置(保留应用骨架与静态托管模式;删除 JWT 登录与写死密钥、订阅与下单撤单接口、WebSocket 与 RPC、全部 `vnpy` import;`WebEngine` 不装进交易进程);列出 FastAPI 默认行为与契约的冲突及处理(关闭自动文档、统一错误形状、关闭斜杠重定向、安全响应头、强制回环);Web 依赖隔离在 `requirements-dashboard.txt`,不进交易环境。计划 T6 拆为 T6a–T6g。如实记录:改造后上游残留代码很少,价值主要是许可证谱系与起步骨架;若 FastAPI 默认行为的处理成本高于收益,可回退到完全自写后端。
- 路线图与 ADR 评审『另类数据与遗漏模块』愿景清单(仅文档):可行项登记为 `docs/roadmap.md` §2.8(宏观因子只采集存档、特征库、实盘 tick 异常值过滤[先核查]、外部心跳看门狗[仅告警]、策略注册表与本机回滚、盈亏归因、结算单账目核对、组合资金分配、自成交与持仓限额卫士、市场状态识别、Web 热力图与族谱视图);红线冲突项写入『明确不做』并**局部修订**既有的『不做自建前端』:独立进程的只读状态页获准,交易终端式前端与嵌入交易进程的前端仍不做。新增不做:网页一键全平/批准/回滚/改风控阈值、公网穿透、外层实时写入实盘配置、云端接管交易。核对结论:日常结算单确认由 `vnpy_ctp` 自动完成;滑点与成本压力已在回测与晋级门槛中。
- Batch B 脚本层整理(Issue [#12](https://github.com/laiyinyizao007/invest/issues/12)):统一 `notify.webhook.redact` 脱敏实现，新增 `bootstrap.isolate_state()`/`load_env()`，并将 CTP 采集与穿透测试的可复用逻辑真正下沉至 `ctp_tools/`，两个 CLI 现在直接使用共享实现；用 `pyproject.toml` 统一测试导入路径。交易日历仍保留在后续 Batch D。

- PROJECTWIKI 拆分与瘦身(Issue [#7](https://github.com/laiyinyizao007/invest/issues/7)):876 行 → 299 行。
  原 §2.2/§2.4/§2.5 迁至 `docs/roadmap.md`(编号沿用,另增 §2.6 新增规划位与 §2.7 已处理技术债归档);原 §2.3 迁至
  `docs/methodology-mapping.md`(25 行扩充到 34 行,新增「研究文档的矛盾与证据强度」一节给出本项目采纳口径);
  原 §5 整体迁至 `docs/modules.md`(原文未删改),§5 改为模块索引;§4 只留未解决条目并新增 3 条技术债;
  修正过期事实(运行环境改为远程 Windows 直接运行、不再写死测试数、阶段命名统一)。迁移经逐行校验:除有意改写的 19 行外无丢失。
  Bisheng ADR 里的章节引用改指 `docs/roadmap.md`。

### Added（新增）
- **进化引擎阶段 2:不可变宪法**(Issue [#16](https://github.com/laiyinyizao007/invest/issues/16),ADR `20261113-evolution-engine`):
`risk/constitution.py` + `config/immutable_constitution.json`(清单只能追加,文件缺失/非法即 fail-closed)。三层防线:
提交时的静态筛查(覆盖受保护成员、重写受保护方法、实例上改写受保护属性、改写风控模块函数、直接下单撤单、`param_grid` 含受保护参数)、
执行前的运行时类校验(必须继承模板家族;不得重写白名单以外的模板成员,含中间基类与类创建后的补丁)、
请求时的参数校验(`backtest`/`gate` 此前不校验参数键名,`risk_budget` 可被请求方指定)。`research_grid.FORBIDDEN_AXES` 改为从清单派生。
**G8 双轮复核后加固**(codex 第 2 轮发现,逐条复现后修复):`object`/`type.__setattr__()` 可在运行时直接改写实例上的风控底线,
而属性形式的 `__setattr__` 不在原有赋值检查覆盖范围内、运行时又因"所有 dunder 一律忽略"而漏掉,两层防线同时失效——现静态层拦截受禁 dunder 的调用与自定义、
运行时层拒绝候选定义受禁 dunder,另加拒绝自定义 metaclass 与类装饰器;
`run_gate_child()` 此前**完全没有**运行时类校验(整条 gate 路径只剩静态筛查一层,清单 template 配成不可导入的类时仍会运行),
现三个子进程入口统一走 `import_candidate()`,哈希/静态筛查/运行时校验一处不漏;
更严重的是自查发现的 `family_root()` 在真实 MRO 上返回 `abc.ABC`,导致**每个真实候选都因 ABCMeta 自动注入的 `_abc_impl` 被拒**——
阶段 2 的运行时校验对真实代码从未生效过,现改为按"应用层基类"判定家族根并忽略元类自动生成的成员;
受保护参数改为大小写不敏感比对(`Risk_Budget` 与 `risk_budget` 是两个属性,精确匹配会让自定义 `__setattr__` 重定向完全绕过参数校验);
清单的 `template` 改为只能取内置白名单内的类;白名单与受保护清单的冲突改为在合并后的集合上校验(此前 allow 静默胜出,新增的保护形同虚设);
**第 3 轮自查发现的绕过**(codex 连续两轮未报出,系复现绕过代码时自行发现):静态层此前只拦受禁 dunder 的**调用**、不拦**引用**,
于是 `f = object.__setattr__` 之后 `f(self, "risk_budget", 10**12)` 的调用点是裸名字、AST 看不见被调的是谁——实测该写法**真的能击穿风控底线**
(`within_budget(100.0, 10.0, inst.risk_budget)` 在改写后返回 True),现改为受禁 dunder 的任何引用一律拦下;
`operator.attrgetter('__dict__')(self)` 把属性名当字符串用,绕开了"禁止访问 `__dict__`"的属性写法检查,现把受禁属性名作为字符串常量出现时也拦下;
`screen_source` 遇到语法错误时返回空列表(fail-**open**:解析不过就等于没筛查,而空列表看起来跟"查过没问题"一模一样),现改为返回"语法错误"结论(fail-closed);
**codex 第 4 轮复核(31/50,未通过)的发现,逐条复现后修复**:
(1) **Critical——`.pyc` 绕过**:`import_candidate` 走 `spec.loader.exec_module()`,而 Python 默认的 `.pyc` 只比对源文件的 mtime 与大小、不确认字节码对应当前内容,于是"人工批准的、算过哈希的、过了静态筛查的源码"与"实际执行的字节码"可以不是一回事(已实测复现);分开打开文件还留下 TOCTOU 窗口。现只读一次字节,对同一份内容做完哈希、筛查、编译、执行,不再走 `SourceFileLoader`;
(2) **High——别名绕过**:静态层此前只拦"调用",于是"先取别名再调用"(如把 `setattr`/`vars`/`getattr` 存进变量再调)照样能改写风控底线或取到实例字典;下单接口(`self.buy`)同理。现一律拦**引用**;
(3) **High——`operator` 反射通道**:`operator.attrgetter` 能把属性名当字符串传入,绕开属性名检查;`operator` 模块整体禁用,受禁属性名作为字符串常量出现也拒绝(这些 token 按保留字处理,含文档字符串里的提及);
(4) **High——`run_evaluate_child` 的 `cfg` 未定义**:统一入口的重构漏掉了 `cfg = rc.load()`,而使用点包在宽泛的 `except Exception` 里,结果是**每个通过导入的 evaluate 作业都静默变成一条 NameError 的错误结果**;现补回并补了真正跑通子进程路径的行为测试(此前只有"源码里含某个字符串"式的检查,发现不了这种回归);
(5) **Medium——子入口未复查参数**:`_child`/`_gate_child` 能被直接调用,绕过父入口直接传受保护参数就能改写底线;子入口现在自己解析并校验参数(子入口是独立的信任边界);
(6) **Low——ADR 未同步**"受禁 dunder 的任何引用均拒绝"的新语义,并补记静态层不是安全边界、真正的边界是容器隔离。
"只能追加"靠版本控制保证——`load()` 只与代码内置集合合并、察觉不到文件里的删除,现由 CI 检查逐个历史版本比对。
**该检查此前在 CI 里形同虚设**:CI 用 `actions/checkout@v4` + `fetch-depth: 1`(浅克隆)时 `git log` 只看得到一个提交,检查会**静默跳过**、看起来是绿的;现改为对浅克隆 fail-closed(直接失败而不是跳过),并新增 `.github/workflows/constitution-guard.yml`(`fetch-depth: 0`)专门跑这个检查——否则"只能追加"只是一句没人执行的承诺。
**内置保护项也是同样的漏洞**:内置清单写在 `risk/constitution.py` 的常量里,上面那个历史比对看不见它——把 `BUILTIN_PROTECTED_PARAMETERS` 里的 `risk_budget` 删掉,其它检查一样全绿,而候选已经可以改写风控底线了;新增 `test_builtin_protections_are_never_shrunk`,从历史版本里把常量读出来比对(内置保护只能增不能减)。
**G8 第 2 轮(续)**:第 5 轮复核在 codex 额度恢复后跑完,并补跑了第二轮独立外部复核,两轮都**未通过**——逐条复现后修复,阻塞项如下(修复后补跑第 6 轮)。
**第 5 轮 codex(`gpt-5.6-sol`)**:**40.5/50,不建议通过**。Critical 无;High 一条——"内置保护项只增不减"的守卫**恰好漏掉了最重要的那一组**:`BUILTIN_PROTECTED_NAMES` 的定义是 `(...) + BUILTIN_PROTECTED_PARAMETERS`(一个 BinOp),而历史检查的提取器只认 tuple/list 字面量,于是这组受保护成员从来没进过比对——实测删掉 `on_disconnect` 之后 Constitution Guard 依然全绿(94 passed, 1 skipped)。Medium 一条:隐藏子入口只复查了设置字典,`--random-runs`/`--prior-trials`/`--key`/`--segment`/`--on-plateau`/`--file` 都还依赖父进程,`--segment test` 直接调用会以未捕获异常退出 1 而不是拒绝码 2。Low 两条:手工构造的模块缺 `__spec__`/`__loader__`/`__package__`;"拦引用"的误报面(局部变量恰好叫 `buy`/`sell`/`type` 之类)没有写进候选作者能查的文档。
**第 5 轮另一轮独立复核(`MiniMax-M3`)**:**33/50,不建议通过**。它报了 codex 那一轮**没有报出**的一条 **Critical**,我独立复现确认成立。
**Critical——框架的"按名 setter"绕过三层防线(已修)**:vnpy 的 `CtaTemplate.update_setting(setting)` 会把字典里的键**按键名** `setattr` 到实例上,而模板的 `parameters` 列表里正好有四个受保护参数(`risk_budget`/`max_daily_losses`/`atr_window`/`history_vt_symbol`)。候选只要写 `self.update_setting(dict(risk_budget=1e12))` 就能把单笔风险预算从 300 改成 1e12——而这个名字在源码里既不是属性赋值也不是字典键(是 `ast.keyword.arg`),按语法位置筛查看不见它。实测改之前:静态层 `[]`、沙盒层 `[]`、运行时类校验 `[]`、候选被接受,`self.risk_budget` 由 300.0 变成 1e12,`within_budget(100, 10, 1e12)` 返回 True(真实底线 300 时返回 False)——单笔风险预算与当日连亏熔断同时失效。现新增 `BUILTIN_FORBIDDEN_BY_NAME_SETTERS`,**任何引用**都拦(取别名同样拦)、候选自己定义也拦;受保护参数作为**关键字参数**出现也拦(大小写不敏感,与参数层同一把尺子)。已核过 vnpy 全家(`vnpy_ctastrategy` 与 `vnpy.trader`):按名 setter 只有 `update_setting` 一处,所以这是"封一类"而不是"补一个 API"。**根治方向**(把风控底线移出候选实例属性、存到候选够不着的引擎侧对象)已登记为后续项。
**High——内置保护的历史守卫失效(已修)**:提取器改为受控的常量表达式求值(字面量、常量之间的 `+`、同文件常量引用),并**fail-closed**:注册表里的常量解析不出来就报错,而不是静默跳过;范围扩到沙盒层的 `FORBIDDEN_MODULES`/`FORBIDDEN_CALLS`/`FORBIDDEN_ATTRS`;历史列表改用 `git log --full-history`;新增"删掉一个受保护成员必须被发现"的 mutation 自检。mutation 实测:删掉 `on_disconnect` 后守卫失败(改之前是全绿)。
**Medium——隐藏子入口的信任边界(已修)**:`--random-runs` 与 `--prior-trials` 现在由子入口自己复查(`--prior-trials` 为负会放松 DSR 门槛:evaluate 路径先做 `prior + 格子数`,负数让 N 变小,而 DSR 随 N 增大而变严);`--key`/`--segment` 用 parser 的 `choices`;`--on-plateau` 只接受 `0`/`1`;`--file` 必须在 `pending_strategies/` 内;非法 `--segment` 的退出码从 1 改为 2。
**Low(已改)**:`import_candidate` 改用 `spec_from_file_location` 准备模块元数据(保留 `__spec__`/`__loader__`/`__package__`)但仍不调 `exec_module`,字节码绕过不受影响;BOM 候选改用 `utf-8-sig` 解码(此前会报 `invalid non-printable character U+FEFF`);候选导入期异常改走结构化 `{"error": ...}` 回传(此前带 traceback 退出 1,LLM 拿不到诊断);错误提示语"param_grid 含受保护参数"改为"字典字面量含受保护参数"(这条检查的实际范围是任何字典字面量)。
**未采纳/未复现(如实记录)**:另一轮提出的"`git log -- <path>` 会把被合并分支吃掉"一条**复现不成立**——实测特性分支新增保护项的提交在默认 `git log` 里仍在(被剪掉的只有与父提交内容完全相同的合并提交,不比也不会漏内容);已按它的建议改用 `--full-history`,但那是可读性改动,不是漏洞修复。另一轮提出的"文档字符串里提到受禁属性名也会被拒"一条**与实现不符**:沙盒层是精确相等匹配,文档字符串里"提到"不会被拒;ADR 已按实现更正口径。
**第 6 轮复核(MiniMax-M3)**:**32/50,仍不通过**,但阻塞项只剩两条且都很短;它报出了前两轮都没报出的一条活绕过。
**Critical——`__builtins__['setattr']` 绕过(已修,已独立复现)**:第 5 轮修的是 `self.update_setting(...)`,
另一条路径能达成**完全相同的后果**:`exec(code, module.__dict__)` 会往候选的 globals 里注入 `__builtins__` **字典**,
里面就有 `setattr`;此前只拦 `x.__builtins__` 这种**属性写法**,裸名与字符串形式都漏了。
实测改之前:静态层 `[]`、沙盒层 `[]`,`self.risk_budget` 由 300.0 变成 1e12,`within_budget(100,10,1e12)` 返回 True。
现改沙盒层:受禁属性名(`__builtins__`/`__dict__`/`__class__` 等)的**裸名**引用一律拒;
这些名字与 `setattr`/`getattr`/`eval`/`exec`/`globals`/`locals`/`vars`/`__import__`/`compile`/`input`
作为**字符串常量**出现也拒。**故意不含** `open`/`type`:交易数据里 `"status": "open"` 很常见,按字符串一律拒会整片拒掉正常候选。
**High——宪法守卫在 CI 上收集即失败(已修)**:守卫此前寄生在 pytest 用例里,CI 跑 `pytest tests/test_constitution.py`,
而根 `tests/conftest.py` 会 import `segment_ledger` → `bootstrap` → `vnpy.trader.setting`,
在只装 pytest 的 CI 上**收集阶段就失败(退出码 4)**——守卫在 CI 上一次都没跑过,却看起来"配好了"。
本地已复现同样的失败(屏蔽 vnpy 导入后 exit 4)。现把守卫抽成独立脚本 `tools/check_constitution_history.py`
(只用标准库 + git,退出码 0 通过 / 1 有问题 / 2 环境不可用),CI 直接跑它,不装 pytest、更不装 vnpy;
并加了"注册表完整性"检查(源码里新增字符串集合型的保护常量而没登记进历史比对,守卫会报出来——
当年 `BUILTIN_PROTECTED_NAMES` 正是这样漏掉的)与"换模板必须同时改内置白名单"检查。
**本轮还修的(第 6 轮提出)**:BOM 候选在父入口与子入口口径不一致(现在共用 `read_candidate()` 读取器);
结果里记的 `strategy_sha256` 早先是从换行归一后的文本算的,与批准记录的哈希**必然不同**(工作区 `.py` 都是 CRLF),
审计链断在换行上——现统一用原始字节的哈希;候选导入期 `raise SystemExit()` 也走结构化回传;
类体里把按名 setter 写成赋值(`update_setting = 3`)静态层也拦;子入口自己查**审批记录**
(`--sha256` 只证明"文件没变",不证明"人看过");找不到类改为退出码 2(参数错误)而不是 3;子进程的"拒绝"原样传到父层。
**如实记录两条本轮复核提出但本轮不做的**:①子入口仍信任调用方给的 `--prior-trials`(少报会让 DSR 的 N 变小,
放松晋级门槛;且子入口自己不写账本,直接调用可白嫖评估)——根治要么让子入口以账本为准,要么让它自己也记账;
②本仓库**没有任何工作流跑全量测试**,测试只在本地跑。这两条已登记进 ADR 的"已知残余风险"。

**第 7 轮复核(codex `gpt-5.6-sol` 与独立复核 `MiniMax-M3` 各跑一轮)**:两轮结论**不一致**,都未通过,分歧本身有价值:
gpt-5.6-sol 说"上一轮的 Critical 只封住了具体写法、没封住能力"(32/50),MiniMax 说"约 30 条取内建/反射路径逐条探针全部被拦"(达成)。
我按 gpt 说的路径实测:**gpt 是对的**——`e.__traceback__.tb_frame.f_builtins.setattr(self, 'risk_budget', 1e12)`
与 `from unittest import mock; mock.patch.object(self, 'risk_budget', 1e12)` 两条都让静态层与沙盒层返回空列表。
**这说明静态黑名单封的是"已知写法",封不住"能力"**——继续枚举反射路径是枚举不完的。
**改为把能力收走(执行时隔离)**:候选执行时(`exec(code, module.__dict__)`)显式给一份**最小内建集合**
(`scripts/research_sandbox.py::safe_builtins`):`setattr`/`delattr`/`getattr`/`eval`/`exec`/`compile`/`open`/
`globals`/`locals`/`vars`/`type`/`object`/`breakpoint`/`input` 一律不给,`__import__` 换成受限导入器
(`builtins`、`unittest.mock` 等已进禁用名单),其余内建照常给。这样无论候选怎么绕,
**它能拿到的内建字典里就没有 `setattr`**——连 `frame.f_builtins` 也只会拿到这份受限字典。
实测:同一份绕过代码现在抛 `AttributeError`,底线保持 300,`within_budget` 仍为 False。
代价:候选用到这些名字会得到 `NameError`/`AttributeError`(fail-closed,由人在批准前看出来),`type(...)` 这类常见写法也会失效。
**这不是安全边界的终点**:容器隔离与"运行前由人读过代码"仍是真正的边界,能力隔离只是又收了一层。
**同轮修掉的**:守卫的"换模板必须同时改内置白名单"检查在真实历史形状下**静默失效**(白名单一旦在历史上涨过就永远不报——
隔离克隆里先给白名单加一项、再改清单的 template,守卫退出码 0),改为**逐个版本**检查"该版本清单里的 template 必须在该版本的白名单里";
守卫的注册表完整性检查补上 `AnnAssign` 写法;父进程把候选文件收敛为**只读一次**(哈希、筛查、审批、子进程参数、结果记录全部用同一份字节);
回传给调用方的拒绝原因脱敏补上带空格的路径、引号路径与 POSIX 路径;子入口的拒绝消息不再带本机路径;
源码与 ADR 里"vnpy 全家只有 update_setting 一处按名 setter"这句**说过头**的话已更正;
ADR 的保留字清单补齐 `FORBIDDEN_STRINGS`/`UNSAFE_BUILTINS`/`EXTRA_FORBIDDEN_MODULES`,并新增"清单与代码一致"的用例钉住它。

**第 8 轮复核(codex CLI 深度分析,36/50,未通过;与第 7 轮并行,审查期间工作区被并发修改)**:两条 Critical 都是**实测复现**过的(真实 15 分钟玉米数据,窗口 2022-07-01~2023-06-30,3000 根 15 分钟 K 线),而且当时**三层校验全绿**(静态层、沙盒层、运行时类校验都返回空列表):
①**候选可以调用模板的风控执行点**:`self._send_entry(signal, atr*50, close)` 借到了模板自己的下单机器,而 `within_budget()` 与 `guard.can_open()` 恰好都在被绕过的 `_try_enter()` 体内——实测 `atr_mult=50` 时诚实模板成交 0 笔(157 次被预算拦下),候选成交 2 笔、单笔止损距离 508.7(≈17 倍 300 元预算);
②**静态层只看赋值的末段属性名,不看整条属性路径**:`self.guard.max_losses=999` / `self.guard._losses=0` 让当日连亏熔断形同虚设(实测成交 120→149 笔),`self.cta_engine.size=0.001` 把合约乘数改小后回撤从 -4272 元/-39.97% 缩成 **-0 元/-0.00%**,`promotion_gate` 的 2000 元与 20% 两道回撤门槛同时失效(夏普是比值,不受影响,所以门槛看起来"过了")。
**修法**:静态层新增**受保护执行点清单**(`_try_enter`/`_send_entry`/`_manage_position`/`_place_protective_stop`/`_place_pending_entry`/`_process_daily_bar`/`_ensure_protective_stop`/`_stop_order_price`/`_complete_stale_daily_bar`),对这些名字的**引用**一律拒(不只拦调用——`f = self._send_entry` 之后的 `f(...)` 调用点看不出是谁);静态层改为解析**整条属性路径**,并对**受保护状态对象**(`guard`/`trail`/`cta_engine`)**连引用都禁**(否则 `g = self.guard` 之后的 `g.max_losses = 999` 绕过去),另把 `am.array` 列为受保护路径;新增**运行时第三层** `constitution.check_instance_state()`——回测**跑完之后**核对实例上的 `risk_budget`/`atr_window`/`max_daily_losses`/`guard.max_losses`/`cta_engine.size`,不一致即 fail-closed 抛错(`scripts/backtest_lib.py` 两条入口都接上),比对基准是**回测开始前的快照**(研究脚本会用更大的 `risk_budget` 做敏感性实验,那是合法的;被改写的是"回测途中")。
**同轮修掉的其余项**:历史守卫此前只守**常量**不守**执行点**(删掉 `if name in BUILTIN_FORBIDDEN_DUNDERS:` 整块、窄化该判断、删掉 `forbidden_order_calls` 分支、`_is_protected` 恒返回 False,四种改法下守卫退出码**都是 0**),现改为按 AST 统计受保护常量的**引用次数**(Load 语境,定义处不算),引用次数减少即报错;`BUILTIN_ALLOWED_OVERRIDES` 另定为**只减不增**(白名单加一项 = 多放开一点底线,与"清单只能追加"正好相反);选参网格的受保护轴检查与 `check_params` 口径不一(大小写敏感 vs 不敏感、只覆盖参数 vs 含成员名)——收敛到 `constitution.protected_lower_all()` 这一把尺子;`check_params` 改为**递归**查嵌套字典/列表;受禁 dunder 名单补上反射型的那批(`__del__`/`__reduce__`/`__reduce_ex__`/`__mro_entries__`/`__dict__`/`__class__`/`__mro__`/`__globals__`/`__code__` 等);静态层补扫 `__slots__` 元素。**子入口的 TOCTOU(Low 8)由并行的另一路会话修掉**:子入口改为按**审批命中的哈希**核对候选文件,不再读第二遍文件(提交 `f90807d`)。
**自己复查自己写的新规则时又抓到两个缺口(同一轮,一并修掉)**:①写受保护成员的那条新规则最初只认 `self.` 开头的路径,于是 `s = self` 之后的 `s.risk_budget = 1e12`、`o.risk_budget = 1e12`(根不是 self)静态层**全返回空列表**——后者还算**回归**:早先按末段属性名判定的规则本来是拦的;现改成**属性路径里任何一段命中受保护成员就算越权**(不限根)。②**行情数组可以被原地改写**:`self.am.close[0] = 999.0`(下标赋值)与 `arr = self.am.close` 之后的 `arr[0] = 999` 都改的是 vn.py ArrayManager 里的 `close_array` **本体**(`close` 是 property,返回的就是数组本身)——候选改掉已经走过的价格就能凭空造出行情。现补:下标赋值也按整条路径判定;新增**受保护容器(`am`)不得取别名**(`arr = self.am.close` 拒;带下标的读 `x = self.am.close[-1]` 与带调用的读 `x = self.am.atr(14)` 放行)。顺带更正 ADR 里「`self.am.close` 是取不出来的副本」这句**说过头**的话,以及 `BUILTIN_PROTECTED_PATHS` 里的 `am.array` (vn.py 的 ArrayManager 根本没有这个属性,该条目实际几乎匹配不到东西——真正护住行情数组的是「容器不得取别名」这条)。**新守卫第一次实战生效**:改写 `_write_violation` 时把 `c.protected_names` 的直接引用换成了 `_is_protected(...)`,引用次数从 10 降到 9,**守卫当场报了出来**——「只守常量不守执行点」那条规则不是摆设。
**如实记录的残余缺口**:①静态层是**纵深防御不是安全边界**——真正的边界仍是容器隔离与"运行前由人读过代码";②`am` 的**子对象**只建模到 `am.array` 一条路径;③晋级门槛缺**逐笔风险预算审计**(能通过门槛的候选,逐笔风险金额仍可能大于 300 元,只是没被改写);④守卫现在守"引用次数"不守"语义",同义改写(如 `if name in X or True:`)仍可能躲开计数——语义级守卫需要把执行点本身登记成受保护常量。

**第 9 轮复核(codex CLI 深度分析,34/50,需改进,不建议发布)**:一条 **Critical**——候选可以**改写模块对象上的风控函数**:`import risk.risk_guard as rg; rg.risk_per_trade = lambda *a, **k: 0.0` 三行就让 300 元单笔预算形同虚设,而**四层防线全部放行**。第 8 轮刚加的"整条属性路径"规则在这里不起作用:路径 `rg.risk_per_trade` 的根是候选自己起的别名,静态层无从知道它指向 `risk.risk_guard`;而 `risk_per_trade` 这个名字本身也不在任何受保护名单里(受保护的是方法与状态对象)。写法一换又漏(`type(cls)`、`sys.modules`、别名),**枚举写法是枚举不完的**。
**修法——运行期第四层:受保护对象的身份指纹**。不再枚举写法,改为**事后比对身份**:`PROTECTED_RUNTIME_PACKAGES = ("risk", "strategies", "vnpy_ctastrategy")`,候选执行**前后**各拍一张指纹(三个受保护包下所有已加载模块的公开属性 + 其中每个类的成员;容器取副本、函数与类比身份),被替换/被删除/被往已存在的类上新增成员即 fail-closed。接点三处:`research_api.py::import_candidate`(导入期前后)、`backtest_lib.py` 两条回测入口(`add_strategy` 之后拍基线、回测结束后再拍一张)。**快照必须预加载模板模块**——模板是候选自己 import 的,不预加载时快照里根本没有它,候选在导入期改写它完全不被记录(codex 实测)。
**同轮修掉的其余项**:①**`__new__` 早于快照**——`__new__` 在实例构造时执行,早于回测前快照,里面的改写会被当成"基线",运行期第三层结构性看不见(codex 实测:定义 `__new__` 三层全放行),候选没有正当理由自定义它(模板也没有),直接加入 `BUILTIN_FORBIDDEN_DUNDERS` 禁掉;②**ADR 保留字表漏项**——表里没列"受保护模块级成员""受保护容器""运行期受保护包",`__new__` 也漏了,而表是候选作者唯一的查处,已补齐,并把 `listed()` 扩展到**全部**宪法名单,以后代码加名字而表里没写会当场红;③**候选能 import 风控包**——`risk` 整个包加入 `FORBIDDEN_MODULES`(静态层与受限导入器同时生效);**不**包括 `strategies`(候选本来就要 import 模板继承基类);④**历史守卫重复解析**——此前对每个历史版本都重新解析一遍当前源码,改为当前源码只数一遍(新增 `usage_regressions_from_counts`,原 `usage_regressions` 保留为包装);⑤第四层的覆盖范围与预加载清单已登记进历史守卫的 `BUILTIN_SETS`,少一个包即报错。
**如实记录的残余缺口**:①指纹只覆盖**已加载模块**,在别的命名空间里造影子对象看不见(但 `importlib`/`ctypes` 本就在禁用清单里,且改不到真框架对象);②指纹只覆盖三个包,将来引入承载风控的新包必须同时加进 `PROTECTED_RUNTIME_PACKAGES`(历史守卫强制);③静态层仍是纵深防御不是安全边界,真正的边界是容器隔离与"运行前由人读过代码";④晋级门槛仍缺逐笔风险预算审计(第 8 轮登记的遗留项,本轮未动)。

**第 10 轮复核(codex CLI 深度分析,37/50,需改进,不建议发布)**:一条 **Critical**——第 9 轮的"身份指纹"比的是**身份**,而函数对象可以**就地改写**:`from strategies.trend_atr_strategy import within_budget; within_budget.__defaults__ = (0.0,)` 把 volume 的默认值改成 0,单笔风险金额恒为 0,300 元预算就此形同虚设(实测 `within_budget(500, 10, 300)` 由 `False` 变 `True`)——函数**还是那个对象**,四层防线全部放行。本地自查同时抓到**另一条独立 Critical**:候选可以在**实例**上遮蔽模板方法(`self.get_size = lambda: 0.001`,真实合约乘数 10 被换成 0.001,300 元预算算成 0.003 元),类字典里没有这个赋值、实例字段快照不盯它、指纹只覆盖模块属性与类成员——同样四层全放行。
**修法**:①静态层把 `__defaults__`/`__kwdefaults__`/`__annotations__`/`__wrapped__` 加入受禁 dunder(沙盒的 `FORBIDDEN_ATTRS`/`FORBIDDEN_STRINGS` 同步),并新增 `BUILTIN_PROTECTED_READ_ATTRS`——候选**不得读**风控对象(`within_budget`/`stop_distance`/`risk_per_trade`/`DailyLossGuard`/`TrailingStop`/`trading_day`),拿到定义就多一条改写它的路;只收不会误伤的名字,泛用词(`reset`/`can_open`/`on_trade_closed`/`ceil_to`/`floor_to`)不收;②第四层对函数不再只比身份,而是比 `(函数对象, __code__, __defaults__, __kwdefaults__, 闭包单元格内容)`;③`get_size` 进受保护成员名单,第三层新增"实例上不许遮蔽模板方法"的核对;④指纹的 dunder 覆盖从"跳过全部"改为**显式忽略清单**(`_FINGERPRINT_IGNORED_DUNDERS`),类的 `__getattribute__`/`__instancecheck__` 改写从此拍得到;⑤第三层的家族判定从 `isinstance` 改为**按 MRO 判定**——`isinstance` 的判定逻辑正是 `ABCMeta.__instancecheck__`,把它换掉会让第三层直接判成"不适用"而放行;⑥`trading_day`(熔断器按它分日)进受保护名单;⑦第四层的"回测前"基准从 `add_strategy` **之后**前移到**之前**(构造期的改写原先会被当成"基线");⑧历史守卫新增 `CALL_SITE_REGISTRY`,按"引用次数只许增不许减"守第四层的两个接点(实测:删掉 `check_protected_state(...)` 后守卫退出码 1);⑨泛用名 `reset` 改成"主人也在路径里才算命中",`self.indicator.reset = 1` 这种普通写法不再被误拒;⑩指纹比对改用 `_safe_token`,numpy 数组的 `==` 不再抛异常(原实现会把它误判成"被改写")。
**同轮新增回归测试**:静态层拦实例遮蔽 `get_size`/改函数默认参数/读风控函数/`__instancecheck__`;第四层抓函数对象就地改写与类的 `__getattribute__` 改写;第三层抓实例遮蔽模板方法;家族判定在 `ABCMeta.__instancecheck__` 被改写后仍然工作;泛用名不误拒。

**第 11 轮复核(codex CLI 深度分析,43/50,需改进,不建议发布)**:第 9/10 轮的 10 条修复被**逐条独立复核**——**9 条确认真堵住**(函数对象状态改写、实例遮蔽 `get_size`、`trading_day`、指纹 dunder、MRO 家族判定、受保护成员禁读、基准前移、`reset` 带主机制、`_safe_token`),**1 条只半堵**(第 10 轮的"执行点注册表"只数名字、不看语义)。本轮新报出 2 条 High / 3 条 Medium / 2 条 Low,逐条修复:
- **High 1——受保护容器只挡"写/取别名",不挡"变更型方法调用"**:`self.am.update_bar(fake_bar)` 在静态层与沙盒层都返回 `[]`,第 3/4 层也不覆盖活实例,codex 端到端实测**四层全绿**而回测结果被完全改写(`skipped_by_budget` 由 0 变 3928)——写数组被拦、插数组不拦,与容器规则的立意(防伪造价格 → 伪造收益)直接矛盾。新增 `BUILTIN_PROTECTED_CONTAINER_SCOPE`(`am`/`bg`)与 `BUILTIN_PROTECTED_CONTAINER_MUTATORS`(`update_bar`/`update_tick`/`append`/…/`byteswap`):路径经过受保护容器且调用变更型方法一律拒;容器上的**只读**调用(`self.am.sma(...)`)照常放行;裸局部名 `am`/`bg` 降级为**路径保护**(只保护 `self.am.xxx`),类体里定义 `am` 仍然拒。
- **High 2——第四层执行点守卫只数名字、不看语义**:codex 在独立 clone 上实测四种改法都能让 `tools/check_constitution_history.py` **exit 0** 而第四层已失效(同名影子函数、自己比自己、调用挪进死分支、新增不含第三/四层的执行入口)。守卫升级为**调用形态**核对(`check_call_site_shape`:必须有活调用、无同名影子、两个实参必须不同)+ **完备性检查**(`check_executor_completeness`:按仓库实际内容找出所有执行候选的模块,不在"走第四层"传递闭包里的执行入口一律报出来)。
- **Medium 1**——`self.trading_day = …` 被第 10 轮的模块级成员名单误判成"改写风控函数"(第 10 轮新引入的误报,"每天只开一次仓"类候选会被整片拒掉):模块级成员名只在**根不是 `self`** 时才算命中,模块级的 `rg.trading_day = …` 仍然拦。
- **Medium 2**——指纹只覆盖快照时**已加载**的受保护模块,候选在运行期 import 一个此前没加载过的受保护子模块再改写它,指纹完全看不见:快照为每个受保护模块留**模块标记键**,比对时发现"运行期新加载的受保护模块"就报出来。
- **Medium 3**——vnpy 核心对象只靠"模板按名 import"的巧合被覆盖(模板改成 `import vnpy.trader.utility as util` 即静默失效):`PROTECTED_RUNTIME_PACKAGES` 支持**点号路径前缀**,显式纳入 `vnpy.trader.constant`/`vnpy.trader.object`/`vnpy.trader.utility`;`vnpy.trader.database` **故意不在范围内**(模块级 `database` 全局会被合法替换,且研究链路的数据来自 CSV,数据库不在数据路径上)。
- **Medium 4**——numpy 数组退化成身份标记,`arr[0]=5` 这种原地改写在指纹里看不见:指纹对 numpy 按**内容**比,内容没变不报(否则每次回测刷出一堆虚假 diff)。
- **Low 1 / Low 3**——裸局部名 `am`/`bg` 降级为路径保护;第三层新增**类字典**比对(`_runtime_class_dict_violations`)抓运行期给类打的补丁,但**仓库自己的策略**(演示策略、测试替身)不受"候选不得重写受保护成员"这条规矩约束——否则仓库的策略会被自己的宪法拒掉(这是本轮修复时踩到并修掉的误报)。
**同轮新增回归测试**(`tests/test_constitution_round11.py`,22 条):假 K 线注入被拒、只读行情调用放行、`self.trading_day` 不误报、裸局部 `am` 放行而类体 `am` 仍拒、运行期新加载受保护模块被报、框架类按模块路径进指纹且被改写被抓、numpy 原地改写可见、运行期类补丁被抓、仓库自己的策略不被误拒,以及守卫的四种绕过(影子调用 / 自己比自己 / 死分支 / 无第四层的执行入口)全被报出。
**指纹范围扩大后的实测**:键数约 4600,一次快照约 5 毫秒(第 12 轮重测为 7.3–10.1 毫秒、中位 8.2;首张约 490 毫秒,含模块预载),真实回测与候选导入前后**零虚假 diff**。全量 2387 passed / 3 skipped;唯一的 4 failed + 5 errors 与本轮改动无关(dashboard/drill 的环境问题,与第 10 轮基线相同)。

**第 12 轮复核(codex CLI 深度分析,38.5/50,需改进,不建议发布)**:codex 在 120 根合成 15 分钟 K 线上端到端实测,确认第 9/10/11 轮的修复仍然有效,并报出 2 条 High / 2 条 Medium / 2 条 Low,逐条修复:
- **High 1——容器规则只挡"写/取别名",不挡"视图 / dunder 写入口 / bg 别名"**:`self.am.close[:]`(numpy 视图)、`self.am.close.__setitem__(...)`(dunder 写入口)、`x = self.bg` 再 `x.update_bar(fake)`(别名)在静态层与沙盒层都返回 `[]`,第 3/4 层也不覆盖活实例——**四层防线全绿**,而 ATR 被从 40 压到 2.86、`skipped_by_budget` 由 1 变 0,诚实模板 0 成交而候选成交 2 笔,**¥300 单笔风险预算被架空**。修法分两层:静态层新增 `BUILTIN_PROTECTED_CONTAINER_VIEWS`(`view`/`reshape`/`ravel`/`squeeze`/`transpose`,返回的还是同一块内存)、变更型方法名单补上 dunder 写入口(`__setitem__`/`__delitem__`/`__iadd__`/`__imul__`/`__ior__`),并新增"**交出引用**"检查(`_container_escape_findings`:存进名字/带注解/海象/元组解包/传给出调用/返回出去/装进容器/作为默认参数,凡是让引用流出去一律拒,容器只许就地使用);运行期新增**第五层**(`check_market_data_integrity`):回测结束后把行情容器与引擎**真正喂过的** K 线逐值比对(累计根数不得多于喂入根数、四个价格数组必须逐值相等),这一层**不依赖静态分析**,未枚举出的写路径一并兜住。
- **High 2——第四层执行点守卫仍能被"形态"绕过**:codex 在独立 clone 上实测五种改法都能让守卫 **exit 0** 而第四层已失效(比对挪进**永不被调用的函数**;第二个实参写成语义相同的 `dict(const.snapshot_protected_state())`;新执行入口只在**注释**里写名字;新执行入口用 `eval(compile(...))`;新入口只 `import` 已覆盖的模块)。守卫升级为**数据流 + 作用域 + 可达性 + AST 覆盖**:第二个实参必须**就是**一次 `snapshot_protected_state()` 调用、快照与比对必须在同一函数作用域、所在函数必须在仓库里被调用过、执行入口按 AST 识别(注释不算数)并覆盖裸 `exec`/`eval`/`compile` 与属性形式的 `import_module`/`run_path`/`run_module`;新增 `check_constitution_module_is_clean`(宪法模块自身不得出现裸 `exec`/`eval`/`compile`)。
- **Medium 1——裸局部名 `am.append(...)` 被误拒**:第 11 轮承诺"裸局部名不再按名一刀切",但只放宽了赋值路径,变更型方法的调用路径照旧按裸名拒,`am = []` 再 `am.append(x)` 会被整片拒掉。受保护容器的判定改为**路径级**:作用域段必须出现在接收者路径的第 2 段及以后(`self.am.update_bar` ✓、`s.am.update_bar` ✓)才算命中,裸名放行。
- **Medium 2——伪造 `__module__` 能买"仓库自己的类"豁免**:候选在类体里写 `__module__ = "strategies.trend_atr_15m_strategy"` 即可让越权的 `get_size` 改写被豁免放过。豁免的判定改为"这个类**确实由那个模块定义**"(`vars(sys.modules[module]).get(name) is klass`——伪造 `__module__` 只是改了一个字符串);待审目录同时改为从 `RESEARCH_SANDBOX_CONFIG` 指向的配置里读 `pending_dir`,不再写死。
- **Low 1 / Low 2——文档与实测不符**:保留字表补上容器视图型方法与 dunder 写入口(该表仍由 `test_the_reserved_word_table_matches_the_code` 逐个核对);三处不实断言按实现更正(指纹快照耗时的实测值已改为 7.3–10.1 毫秒、中位 8.2)。
**同轮新增回归测试**(`tests/test_constitution_round12.py`,34 条(第 13 轮后增至 56 条)):容器绕过四姿势、取用七姿势、误报七姿势、第五层六例(预热期伪造 K 线被抓 / 诚实回测零发现 / 改写行情被抓 / 凭空多出的 K 线被抓 / 预热不算凭空多出来 / 预热之后仍能抓到凭空多出来的 K 线)、守卫五种绕过(死函数 / 语义同参 / 注释冒充 / eval 入口 / import 闭包)与两个放行例、`__module__` 伪造不再能买豁免、仓库自己的策略仍享豁免、测试函数里就地定义的替身仍享豁免。另跑了 21 条动态探针(放行+拦截双向,不入库):容器绕过三变体、取用七姿势、误报七例、第五层三例。

**第 13 轮复核(codex CLI 深度分析,38.0/50,需改进,不建议发布;基于 `9397821`)**:第 12 轮的 6 条修复大部分确已堵住,codex 还核实了**单笔 ¥300 底线没有被突破**(被预算判定的 `dist` 与写进 `pending_dist` 的是同一个值,`pending_dist` 是受保护名,风险金额与被判定金额结构上同源;损害在统计口径而不在风险底线)。新报 1 Critical / 3 High / 2 Medium / 3 Low,逐条修复:
- **Critical 1——第五层是末态比对,"改→用→改回"的瞬时改写看不见**:codex 最小复现把 ATR 从 40.00 压到 5.07、成交 30 笔往返(诚实模板 0 成交),回测结束时三个数组与原始行情逐值相同,五层全绿;静态层也漏了能表达它的写法(`self.am.close[:, None]`/`[...]`/`[None]`/`.swapaxes()` 都放行,第 12 轮只认 `ast.Slice`)。静态层:`_subscript_makes_a_view` 认出 numpy 全部基本索引视图(含 `slice(...)`),视图方法补 `swapaxes`/`diagonal`,整数下标与高级索引照常放行;运行期:新增 `MarketDataWatch`,回测脚本包住 `engine.new_bar`,**每处理完一根 K 线核对一次**容器,终态比对保留。端到端用例用 monkeypatch 关掉逐根核对来证明用例有判别力(关掉后同一个攻击放行)。
- **High 1/2/3——历史守卫"验形态不验效果"**(独立克隆里改一处,守卫仍 exit 0):`_ = const.check_protected_state(...)` 结论被扔掉;把比对挪进 `lambda`;新执行入口 `exec` 裸奔而那两个名字只放在从不被调用的函数里。现在结论必须被用掉(`_result_is_used`)、lambda 算作用域并直接报、执行入口的每个原语执行调用所在的**同一个函数体**里必须自己成对取快照并比对,且比对调用点与注册表共用同一套形态核对(`_check_call_problems`)。
- **Medium 1——"仓库自己的类"豁免可被候选买断**(`Evil.__module__ = "scripts.research_sandbox"; sb.Evil = Evil` 自己满足"模块字典里有同名对象"):现在只看类体里自己的函数的 `co_filename` 是否**全部**来自类声称的模块文件、且**至少有一个**(没有自己函数的类不豁免,避免空集恒成立)。
- **Medium 2——`pending_dir` 有两个口径**(沙盒会拒绝 `pending_dir="strategies"`,宪法照收):宪法做同一套校验(相对路径、不越出仓库、首段非保留目录),不过就退回默认;保留目录集合是复刻的,由测试守与 `research_config.RESERVED_DIRS` 一致。
- **Low**:第五层扩到成交量/成交额/持仓量数组;ADR 对第五层能力的表述更正为"逐根 + 终态"并写明不覆盖的东西;`_container_escape_findings` 只看最外层表达式(`x = self.am.close + 0` 不命中,numpy 运算出副本,今天无害)**暂不改**,如实登记。
**顺带修好了一个基线上就存在的失败**:`test_walk_forward_optimizer.py::test_real_spawn_*` 在全量套件里一直红(单独跑能过),根因是 `tests/test_research_api.py` 里一个"候选导入期改写模板类"的用例真的执行了 `t.TrendAtr15mStrategy.get_pricetick = lambda self: 10**9` 且**没有还原**——共享的类对象被永久篡改,同一进程里之后的测试(串行、进程内)与新进程(干净)的净盈亏对不上(-572 对 -632)。这不是被测代码的问题,而是测试隔离:`tests/conftest.py` 新增自动还原模板类字典的 fixture。第 13 轮收紧豁免判据后,被篡改的类会被运行期类字典检查**正确地拒掉**(旧的"至少一个来自仓库"判据把它掩盖了),这也是发现这个根因的线索。全量 2312 通过、0 失败(排除 `test_drill`/`test_dashboard_boundaries`,后者缺 `fastapi`)。
**第 14 轮复核(codex CLI 深度分析,38.5/50,需改进,不建议发布;基于 `3ca7b11`)**:第 13 轮的 Critical **真堵住了**(跨 K 线瞬时改写被逐根核对当场抓住、对正常候选零误报;numpy 视图覆盖完整;`co_filename` 判据挡住 `__module__` 伪造;日线预热记账正确 190/190),**无 Critical**,新报 2 High / 3 Medium / 4 Low,三轮分数 38.5 → 38.0 → 38.5 基本持平。逐条处理:
- **High 1——`am.count` 被当成模式开关**:候选让计数落后一根,强模式永久关闭,再用"引擎喂过的取值"调包(ATR 31.54→14.51、成交 0→12 笔,运行期各层全绿);第五层与静态层并不独立,ADR"不依赖静态分析"的说法说过头了(已更正)。改为把"每喂一根 K 线容器恰好更新一次"当成**被核对的契约**:由模板的 `on_bar` 与 `_process_daily_bar`(候选覆盖不了)决定,契约家族每根 `count` 增量必须恰为 1、偏移恒定;`am.size` 在构造时捕获,被改写即 finding。**修复中被 `IntradayDemoStrategy` 打脸两次**(先用"前 3 根增量都是 1"猜家族、再只看 `on_bar`),如实记入 ADR。
- **High 2——守卫仍只验形态**(五种一行改法全部 exit 0:基准挪到回测/exec 之后、结论只被 print、比对挪进同名嵌套死函数、条件表达式吞结论):新增时序检查(执行原语之前必须有基准快照、之后必须有比对;`check_run_call_order` 守 `run_backtesting()` 之前必须取基准)、结论必须流向决策(`if`/`return`/`raise`/`assert`,经过 print/日志不算)、嵌套函数必须在本文件内被调用、静态为假的条件表达式分支不算活调用。守卫对真实代码的第一次报错是误把只生成字节码的 `compile` 当成执行原语,已修。
- **Medium**:嵌套类买断豁免(`_own_code_objects` 递归进真正嵌套的类);`pending_dir` 保留目录检查大小写敏感(`Strategies` 在 Windows 上就是真实的 `strategies/`,**宪法与沙盒配置两边**都放行,现都按小写比对并拒绝带盘符路径);终态核对读活的 `am.size`(现用构造时捕获的)。
- **Low**:`variables` 不在受保护名单(研究/回测路径当前不可利用,**暂不加入**,如实登记);conftest 全局还原 fixture 依赖导入顺序且掩盖真缺陷——同意并**删除**,改为 `test_research_api.py` 那个用例里按用例回滚;守卫硬编码 `_probe_r11` 改为 `_probe` 前缀;ADR 对第五层表述过头已更正。
**结论性判断**:三轮 38.5/38.0/38.5,每轮修的都是**写法**而下一轮总能找到新的,与第 7/9 轮"黑名单封不住能力"一致;继续按这条路修没有理由认为能收敛到 ≥45。真正可能改变趋势的是**收能力**(让候选拿到的 `self.am` 是只读视图/副本,把"改写行情容器"从"检测"变成"做不到"),这会改动模板与既有 ADR 决策(G6 要求经用户确认),**没有擅自做**。
**同轮新增回归测试**(`tests/test_constitution_round12.py` 增至 69 条):计数落后/容量被改被抓、嵌套类买断不成、`pending_dir` 大小写与盘符(宪法与沙盒两边)、守卫的 G1/G2/G3/G6/G6b 与出厂时序放行。全量 2325 通过、0 失败。
**合并决定(用户授权,2026-11-13)**:第 12/13/14 轮 codex 为 38.5 / 38.0 / 38.5(未达 G8 的 ≥45 发布档),G8 第一轮的外部专家验证未完成(Gemini 配额耗尽)。在被告知这些事实与"继续逐条堵没有理由认为能收敛"的判断后,用户**明确选择接受现状合并 PR #17**——这是**风险取舍,不是分数达标**。接受的残余风险见 ADR(运行期三层与静态层并不完全独立:候选若找到静态层的新缺口,可能影响统计口径,但不会突破单笔 ¥300 底线);**不改变任何上线约束**(人工批准 → SimNow → 单独的实盘授权;进化回路不得写 `approved/`/`promotion_approval.json`/实盘配置;DSR ≥ 0.95 已锁定)。后续项:收能力方案、补做 G8 第一轮外部验证。合并时 `main` 与本分支有 6 处冲突(`main` 里是同一项宪法工作的较早版本),宪法相关取较新的一侧、保留 `main` 的 η 标定与统计投影,合并后全量 2489 通过、0 失败。
**本轮的诚实说明**:G8 第一轮的外部专家验证(`gemini-2.5-pro`)因免费档配额耗尽(429)没跑成,只有我自己的核查记录,**不能当作独立意见**;codex 第 13 轮审的是 `9397821`,上面的修复发生在它之后,**需要第 14 轮复核**才能算数。
**同轮新增回归测试**(`tests/test_constitution_round12.py` 增至 56 条):瞬时改写被逐根核对抓到、关掉逐根核对后同一攻击放行(判别力)、视图型下标五种被拒、标量读三种放行、守卫的结论被丢弃/结论存而不读/lambda/执行点挂在别的函数四种改法、伪造模块成员表买不到豁免、只有数据属性的类不豁免、保留目录与沙盒一致、`pending_dir` 五种取值与沙盒口径一致。
**修复过程中自己踩到的两个坑(一并记下)**:①第五层最初把 `am.count` 直接当成"回放根数",而日线族的 `on_init` 会先加载一段历史日线**做预热**(那些根数也记在 `am.count` 里)——于是 14 条既有回测测试同时变红,整个日线研究链路会在回测结束时 fail-closed。现在基准拍在 **`on_init` 之前**(`snapshot_market_data`,构造完成时容器为空),回测脚本包住 `engine.load_bar` 记下引擎**实际交出的**预热 K 线、与回放 K 线一起算合法喂入;**第一版把基准拍在预热之后,第 13 轮自查实测又开了一个洞**(候选在 `on_init` 里塞 40 根假 K 线成了"基线",`am.count=160` 而引擎只喂 120,照样放行),已按上述口径改正并补了回归用例;"凭空多出来的 K 线"仍然抓得到(端到端用例:每根 K 线多插一根 → fail-closed)。②Medium 2 的豁免判据第一版用"模块字典里有没有这个名字",结果把**函数里就地定义的类**(`tests/test_x.py` 里的 `Chatty`)也拒了——仓库自己的代码不该受"候选不得重写受保护成员"这条约束。现判据改为**类体里自己的代码落在哪个文件**(`__code__.co_filename`):就地定义的替身(代码文件 == 模块文件)豁免,伪造 `__module__` 的候选(代码文件在候选目录、模块文件在 `strategies/`)不豁免。

**CI 的实际情况(如实记录)**:本仓库的 GitHub Actions 目前**所有**工作流都在 1–3 秒内失败(`Sync Notion Board`、`Generate Issue Task List`、`Detect Duplicate Issues`、`Constitution Guard` 等一律如此),工作流 YAML 本身解析正常、仓库 Actions 权限是 `enabled`/`all`——不是宪法工作流的写法问题,而是账号/运行器层面的问题(当前 token 缺 `checks:read` 权限,连失败原因都读不到)。**旁证**:该作业的 job 记录里**一个 step 都没有执行**(不是某个 step 失败),这正是运行器/队列/计费层面失败的特征——脚本失败会留下一个 failed step。所以在 Actions 恢复前,守卫在 CI 上的执行仍未被真正验证过;本地已验证:仓库上退出码 0,删掉一个受保护成员后退出码 1。因此 `Constitution Guard` 这条工作流**在 CI 上一次都没有真正跑起来过**,PR #17 的 `mergeStateStatus` 一直是 `UNSTABLE` 也是这个原因。ADR 里"由 CI 检查保证"目前只有本地执行支撑(本地全量 2264 passed / 3 skipped,唯一失败的 `test_drill` 是工作树不带被 gitignore 的 `.env` 所致,与本次改动无关)。这条要等 Actions 恢复后确认,不能当作已经跑过。
**G8 第 2 轮的现状(如实记录)**:codex 连审 5 轮——第 2 轮发现 dunder 绕过与家族根误判、第 4 轮(31/50,未通过)发现 `.pyc` 绕过/别名绕过/`operator` 反射通道/`cfg` 静默回归/子入口未复查参数/ADR 未同步,六项逐条复现后修复并各配了会在修复前失败的测试(对着修复前的提交 `353465e` 跑,8 条新测试会失败)。第 5 轮原先因 codex 账号限流(429)跑不成,额度恢复后于 2026-11-13 03:54 跑完:`gpt-5.6-sol` **40.5/50**、另一轮独立复核(`MiniMax-M3`)**33/50**,两轮都未通过,发现已逐条复现后修复。期间还补过一轮 `gemini-2.5-flash`(**46/50**,无 Critical/High)作参考——它同样不替代 codex 那一轮。修复后已补跑第 6 轮,结果见下方条目。
- **进化引擎三视图(读取侧 + 前端文档)**(2026-11-13):新增 `dashboard/evolution.py` 与 `docs/web/08-evolution-views.md`。契约新增 6 个阶段④只读接口(战役列表/进化曲线/变异日志/参数热力图/候选源码/候选差异)与 11 个数据模型,12 份示例数据,种子重新生成。读取侧校验台账行(坏行即损坏,不做『尽量显示』)、按代聚合精英/均值/P25/P75 并由服务端判定停滞与过拟合提示、热力图三维切二维(`null` 单元保持为空、不画成 0)、源码按哈希找、统一 diff 由服务端算。**面板只读:不注入变异、不批准、不回滚**;进化回路由毕昇工作流承载。路由按仓库约定由配置开关 `evolution.enabled` 控制(阶段④上线才注册,`capabilities.evolution` 告诉前端),台账损坏返回 503 与 404 可区分。**数据还没有**——引擎侧阶段 3 还没开工,线上取不到是如实状态,前端用示例数据开发。
- **进化引擎阶段 1:试验账本 + DSR 门槛**(Issue [#15](https://github.com/laiyinyizao007/invest/issues/15),ADR `20261113-evolution-engine`):`risk/trial_ledger.py`(只增不减、损坏 fail-closed,每个 param_grid 格子与每次成功提交各计 1 次试验,失败的评估同样消耗试验)、`risk/dsr.py`(Bailey & López de Prado 2014,日收益口径,试验间方差取 1/(T-1))、晋级门槛第 9 条 **DSR ≥ 0.95**(阈值已锁定)。`CandidateMetrics` 新增 `dsr`/`n_trials`:缺失即视为未通过(fail-closed),**旧格式的指标 JSON 不再能过 `record_promotion.py`**。账本由可信的父进程读写(先记账再跑,失败不退:evaluate 预扣最小网格 27 并补差额,gate 记 1 次 `gate_run`)、网关 `_candidates` 记提交试验;子进程只拿到 `--prior-trials` 一个数字。NaN/inf 日收益与非法 `dsr`/`n_trials` 一律 fail-closed(G8 复核发现 NaN 会让 `dsr < 0.95` 恒为 False 而放过候选)。PBO 延后。
- `tools/build_static_manifest.py`:生成 / 校验前端构建产物清单 `manifest.json`(`{"files": {相对路径: sha256}}`)——**前后端的硬约定**。后端只服务清单内、哈希匹配的文件,每次请求重新校验;改了产物就必须重新生成。拒绝符号链接、空目录、没有 `index.html` 的目录;`--check` 可放进前端 CI;测试断言后端接受本工具写出的一切。
- 契约:`Guard` 新增 `as_of` / `age_seconds`(风控状态文件自己的年龄);新增示例 `trading-guard-stale`(进程在跑但风控状态陈旧——前端最容易做错的状态);契约文档新增 §10.1 `corrupt` 的完整含义与读取上限、§10.2 `stopped` 录制、`HEAD` 语义。示例共 40 个。
- **状态页后端阶段①**(只读 Web 监控面板,ADR `20261112-status-dashboard.md`):`dashboard/`(派生自 vnpy_webtrader 1.1.0,MIT,保留版权与许可证,见 `dashboard/NOTICE-vnpy_webtrader.md`)+ `scripts/status_server.py` + `requirements-dashboard.txt`。接口 `meta` `overview` `trading` `alerts` `days` `recording` `research` `research/runs/{id}` `docs/{key}`,全部 GET/HEAD;令牌文件 + 常量时间比较 + 滚动窗口限流;统一错误形状且不回显输入;CSP 等安全头;关闭 FastAPI 自动文档与斜杠重定向;静态托管只服务清单内且哈希匹配的文件,SPA 回退限定在前端路由白名单;仅监听回环(配置层强制)。**缺失、过期、损坏永远不是 ok**,总体取最差(unknown 比 warn 更糟);录制与研究是可选子系统,未部署只显示缺失、不拖低总体级别。响应一律经唯一净化出口 `dashboard/safe.py`(账号掩码、令牌/路径/环境变量清理、敏感键名、非有限数)。**前端尚未构建**(显示占位页)。新增 `evidence/logstats.py`:从 `unattended_report.py` 抽出的纯逻辑(302 行原样移动,脚本重新导出,既有 46 个测试不变),因为该脚本经 `bootstrap` 会间接加载 vnpy。边界测试(`tests/test_dashboard_boundaries.py`)把 ADR 承诺变成自动检查:不 import vnpy/trading、不读 `.env`、Web 依赖不进交易环境、上游危险代码不在(基于 AST)、真实进程冒烟(含『杀掉面板不改变状态文件』)。
- 状态页前端(React + Lovable)的计划与文档:`docs/plans/20261112-web-frontend-lovable-plan.md`;`docs/web/05-lovable-guide.md`(真实数据不得进入 Lovable、导出物默认带外部依赖的对策、总流程、可粘贴的项目约束、导出审计闸门)、`06-design-system.md`(深色冷调 Bento + 克制的玻璃拟态;设计令牌对比度经脚本实测深浅两套均达 WCAG AA;玻璃降级规则;状态视觉表达;盈亏色与状态色分离;不用新拟态)、`07-lovable-prompts.md`(P0–P14 提示词,每步带验收点与纠正话术);`docs/web/lovable-seed/`(由 OpenAPI 生成的 `api-types.ts` 与示例数据,经 `tsc --strict` 编译通过,由 `tools/webgen/gen_lovable_seed.py` 重新生成,契约测试断言与契约同步)。`tools/audit_frontend_export.py`:拦截外部脚本(含 Lovable 风格的在线编辑脚本)、内联脚本、Supabase 与统计依赖、`innerHTML`、令牌写入 `localStorage`、API 客户端之外的请求、交易类文案,有 10 条测试。
- 状态页完整 Web 规划(`docs/web/`,供前端同事接手):产品规格(页面、状态语义、无障碍)、API 通用约定、前端交接说明(硬性约束与验收清单)、五阶段规划;机器可读契约 `status-api.openapi.json`(OpenAPI 3.1,22 个接口、37 个模型,每个接口标注阶段与角色)与 39 个示例响应(含缺失、过期、损坏、硬锁定等异常状态)。`tests/test_web_contract.py` 校验示例符合模型,并把边界写成断言:阶段①全部 GET、写操作恰好是暂停/解除、任何阶段都没有下单撤单连接改风控解锁批准类路径。新增测试依赖 `jsonschema`。仅文档与测试,无运行时代码。
- 设计:只读状态页(Web 监控面板)的 ADR `docs/adr/20261112-status-dashboard.md`(待确认)与阶段①计划 `docs/plans/20261112-status-dashboard-plan.md`。结论:独立只读进程、只读文件、仅监听本机回环、前端零外链;**不使用 `vnpy_webtrader`**(核对 1.1.0 安装包:JWT 密钥写死为 `test`、带 `POST /order` 下单与撤单接口、需把 RPC 监听嵌进交易进程、前端仅 104 行 CDN 演示页、也没有我们的熔断/看门狗/录制/研究状态)。实时持仓资金与「暂停开仓」控制分别登记为阶段②③,不在本次范围。仅文档,无代码改动。
- `tools/repro_logout_crash.py`:复现「重连后退出登出段错误(139)」的宿主机循环脚本——每轮在容器里跑 `drill.py disconnect|all --trace`,宿主机真实切断/恢复容器网络(恢复放在 finally),记录退出码与恢复后到退出的秒数,汇总写 `runtime/data/repro/summary.jsonl`。凭据只以 `-e KEY`(不带值)传入、运行时读 `.env.simnow.bak`,不改 `.env`;参数下限防止触发不了断线(切网 ≥130 秒)或被登录限流(轮间隔 ≥300 秒)。有 `--dry-run`。**尚未对真实 SimNow 运行**,待日盘。
- 毕昇研究工作流收尾（不依赖 LLM 的部分；LLM 联调待模型有额度）：
  - **服务端强制迭代上限**：滚动 `iteration.window_hours`（默认 24）小时内最多提交 `iteration.max_iterations`（默认 5）个候选，网关用提交账本 `submissions.jsonl` 计数，超限返回 429 并说明已用额度与何时释放；被 400/409 拒绝的提交不计数；账本损坏 fail-closed；`GET /research/data` 返回剩余额度。此前 `max_iterations` 是没有任何代码使用的空配置。毕昇里实测：第 6 个提交被拒，原因原样出现在节点失败信息里。
  - **确定性中文报告**（`risk/report.py`）：各作业结果落盘带 `report`；网关 `view: "report"`（精炼 JSON）与 `view: "text"`（纯文本，供结果节点直接展示；出错仍返回 JSON）。
  - **先看代码再批准**：`research_api.py review --strategy X` 显示带行号源码与静态筛查结果，只有输入 y 才批准；筛查不过不提问。
  - **无 LLM 联调工作流** `tools/bisheng/build_v3_manual.py` + `run_v3_manual.py`（人输入文件名与源码 → 提交 → 本机 review 批准 → 选参+门槛评估 → 文本报告），以及 `check_retrieval.py`（知识库检索自检）；共用 helper `bsflow.py`。
  - **v3 工作流修正**：写策略提示词此前**没有引用**「读最近失败反馈」节点的输出（v2 起的一次静默失败的替换），现已引用，并接入知识库检索节点（`invest-研究资料`）；提示词要求每个参数 5~7 个取值（3×3×3 只有 1 个内部格子，几乎选不出参数）、禁止 `{{#` 字样；`--model-id` 一键换模型。
- **实测发现——毕昇变量替换只会改写源码里字面的 `{{#节点.变量#}}` 标记**（改成 `{节点.变量}`）；普通花括号（字典、f-string 双花括号）、引号、反斜杠、制表符、中文、多行逐字节不变。批准绑定的是网关实际存下的内容，安全性不受影响，但 LLM 若在代码/注释里写了该标记会被悄悄改写——到网关时已被改，网关拦不了，只能靠提示词禁止。
- 研究网关**提交额度账本**（`scripts/research_gateway.py`，配置 `iteration.window_hours`，默认 24，范围 1–720）：滚动窗口内最多提交 `iteration.max_iterations` 个候选，由服务端强制，LLM 无法绕过；额度检查、写文件、记账在同一把锁内完成，记账失败则撤回候选文件；账本损坏 fail-closed（拒绝提交）；`/research/data` 回报已用额度与最早释放时间。请求可带 `view:"report"` 只取精炼回报（run_id/passed/status/report），非法取值在运行作业前即拒绝。
- 确定性中文报告 `risk/report.py::render_report`：把 backtest/gate/evaluate 结果压成几行文字，纯函数、输入残缺也不抛异常、不做策略建议；每个作业结果附带 `report` 并落盘。此前 `research_api.py` 已导入该模块而文件未入库，现补齐。
- 通知器**落盘待补发队列**（`notify/webhook.py`，环境变量 `WEBHOOK_OUTBOX`，默认 `data/state/webhook_outbox.jsonl`，`off` 关闭）：发送最终失败（如本机断网）的告警不再丢弃，写入 JSONL（临时文件 + fsync + 原子替换），网络恢复后由后台线程补发——FATAL/ERROR/WARN 按发生先后逐条补发并加「【补发】」前缀、保留原始时间；INFO 积压 ≥3 条合并成一条摘要（最多列 20 行）；条数上限（FATAL 优先保留）与存活期（默认 24 小时）；已知断网时新告警只试一次而不是空等约 34 秒；退出时内存队列里没发完的也落盘；进程重启后继续补发。起因：夜盘切断网络 240 秒的演练里，断网期间产生的「行情假死」「网关连接断开」告警恢复后也没有补发。已用真实 Telegram 验证。演练的送达统计改为如实报告实际发送/暂存/放弃/已补发/待补发条数；`drill.py` 场景跑完先落一份结果再进入退出阶段。
- 记录夜盘完整 `all` 的两个新发现：`latency` 在行情清淡时 30 秒没成交（对活跃度敏感）；`disconnect` 重连后退出登出（`logout_ctp`）出现一次原生段错误（139），待复现。
- `disconnect` 演练通过：改为外部真实切断容器网络（`drill.py --external-cut FILE` + 宿主机 `docker network disconnect/connect`），旧版 `td_api.close()` 会释放整个原生 API 导致段错误，已移除；没给 `--external-cut` 时场景自动跳过。实测：切断 240 秒——行情停更看门狗 61 秒告警，CTP 心跳超时约 121 秒报断线（8193），恢复后约 2 秒自动重连；切断 30 秒无断线。至此六个演练场景全部通过。
- 沙盒 `evaluate`（CLI `research_api.py evaluate`、网关 `POST /research/evaluate`、毕昇工具 `run_evaluate`）：候选在类上声明 `param_grid`（恰好三个参数、各 3–10 个严格递增数值，参数名须登记在 `parameters`；`risk_budget`/`max_daily_losses`/`atr_window`/`history_vt_symbol` 不得入网格）→ 服务端在**选参窗口（止于 2022-06-30）**扫网格、邻域平滑选参并判定参数高原 → 用选出的参数在其后的**滚动验证窗口**做完整晋级门槛评估。选参数据与评估数据不重叠，样本外因此由服务端保证；解决「LLM 候选没有参数网格、参数高原恒为未通过」的限制。新增 `scripts/research_grid.py`、`risk/plateau.py::smoothed`（与 `optimize.smoothed` 等价，有对照测试）、配置 `grid.{min_round_trips,max_cells,timeout_seconds}`。实测（毕昇容器 → 沙盒容器，手写候选）：36 格、15 个有效，选出 breakout_n=72/slow_window=96/atr_mult=2.0，不在高原；样本外夏普 -1.84、往返 156、对随机入场 65.5 分位，7 项门槛未过，总耗时 45 秒。毕昇工作流 v3 改用该工具，提示词要求 LLM 声明 `param_grid`。
- SimNow 第一套前置夜盘演练（2026-11-12）：`overnight`、`latency`（0.177 秒）、`tick-stale`、`daily-loss`、`hard-lock` 五个场景通过，`disconnect` 因场景设计缺陷未验证（见 `docs/simnow-drills.md`、`docs/runbook-simnow.md`）。新增 `docs/simnow-drills.md` 合并记录。
- 毕昇工作流 v3（`tools/bisheng/build_v3.py`、`run_v3.py`）：LLM 写继承 `TrendAtr15mStrategy` 的 15 分钟候选，评估节点由日线回测换成 `run_gate`（15 分钟玉米滚动验证段上的晋级门槛评估），解决 v2 里日线候选基类与 15 分钟门槛评估不一致的问题。联调时 MiniMax 返回 429（Token Plan 用量上限），LLM 节点未能实跑；不依赖 LLM 的部分用手写 15 分钟唐奇安候选从毕昇容器一侧实测通过（提交→本机批准→门槛评估，28 秒）：样本外夏普 -5.47、往返 240 笔、对随机入场仅 6 分位，该想法不成立。注意：LLM 候选没有参数网格，`plateau` 恒为空，所以「参数高原」一项永远判未通过——在为候选做参数网格寻优之前，LLM 候选无法通过全部门槛，这是如实的限制而非缺陷。
- `drill.py` 诊断埋点 `--trace FILE` / `--trace-skip-close`（新增 `scripts/drill_trace.py`，默认关闭）：逐行 fsync 的 trace、退出钩子、致命信号转储与周期线程栈转储、释放后 API 调用探针、启动与断线阶段周期快照、距上次登出间隔。用它在夜盘一次运行定位了 `disconnect` 场景静默退出：退出码 139，根因是 `td_api.close()` 释放了整个原生 API，事件引擎定时器线程随后调用 `query_position` 触发段错误；场景本身无法验证「断线与自动重连」，需改为真实切网（`docker network disconnect`），改造前该场景视为未验证。
- 修复 `drill.py` 的 `latency` 场景在真实 SimNow 上必然失败的缺陷：代码读 `tick.ask_price` / `tick.bid_price`，而 vn.py 的 `TickData` 只有 `ask_price_1` / `bid_price_1`。此前单测用手写假 Tick 同样写成 `ask_price`，与代码互相印证，所以 1000+ 个测试全绿却从未发现。测试改用真实 `TickData` 并加一条断言防止假字段回流。夜盘实测 `latency` 通过：成交→持仓回报 0.177 秒，1 手仓已平；`overnight`、`tick-stale` 通过。
- 行情录制「卡住」检测：启动后超时未订阅 → `stalled`，`watchdog` 据此受控重启（忽略上次运行遗留状态）；订阅后长时间无任何 Tick → `data_warning`（只告警）。起因：SimNow 第一套前置夜盘实测，距上次停止仅约 16 秒再启动时，进程连接登录成功却 3 分钟不订阅，状态一直是 `connected`、心跳正常，旧版 watchdog 看不出。
- 策略自身报错作为研究结果回传,而不是网关故障:子进程捕获策略运行异常 → `{type, message}`(取 traceback 末行,抹掉本机路径,限 200 字符)→ 记为 `status: strategy_error` 的结果(HTTP 200)→ `feedback` 的失败 Trace 中给出「策略运行报错: 类型: 信息」;`backtest_lib` 的异常信息现带上 vn.py 随后打印的 traceback(此前只剩一句「触发异常」)。毕昇工作流 v2(多轮循环)实测:布林带想法第 1 轮因 `am.boll` 返回值个数用错而报错,反馈回流后第 2 轮 LLM 自行改写并跑通(夏普 -1.02,该想法不成立)。注意:反馈里的报错文本可能来自已批准代码的异常信息,属进入 LLM 上下文的外部文本,故限长且只保留可打印字符。
- 毕昇工作流 v2 构建与联调脚本 `tools/bisheng/build_v2.py`、`run_v2.py`:想法→读失败反馈→LLM 起名→LLM 写 `entry_signal`(带反馈)→提交→本机批准→回测→输出→回到想法输入(每轮都需人批准,轮数天然由人把控)。
- 研究沙盒容器化 `docker-compose.research.yml` + `docker/research/`:网关与回测在独立容器内运行,内部网络 `research_net`(无外网)、只读根文件系统、丢弃 capability、非 root、资源限额、控制目录只读、数据只读、不含 `.env`/CTP;毕昇后端经该网络调用。镜像约 248 MB(去掉 vnpy 的 GUI 依赖),依赖由宿主机预下载轮子后离线安装(`fetch_wheels.sh`)。子进程运行前重算代码哈希(堵批准后替换文件);批准记录与令牌移入控制目录 `data/research-control/`;`RESEARCH_SANDBOX_CONFIG`、`INVEST_C15_DATA` 环境变量。随附毕昇可导入的接口描述 `docs/research-gateway.openapi.json`(有测试保证与路由白名单一致)。
- 研究沙盒 HTTP 网关 `scripts/research_gateway.py`(标准库实现,无新增依赖):路由白名单(data/candidates/backtest/gate/feedback/results)、Bearer 令牌(独立文件,`init-token` 生成且不覆盖)、先鉴权再路由、全局限流、请求体上限、重作业串行(忙则 409)、提交通道只写 pending 且绝不覆盖并先过静态筛查、请求体里的 `confirm` 被忽略(批准只能由本机 `approve` 触发)、反馈最多返回配置的 N 条且只含固定字段(不回传源码/路径/调用方自由文本)、启动自检拒绝带 CTP_* 环境变量的进程。`research_api` 抽出 CLI 与网关共用的作业函数(JobError)。实测:真实 socket 上 401/404/403 行为正确,提交→本机批准→回测→反馈全链路通;**未解决:毕昇容器访问不到宿主 127.0.0.1 上的网关**(`host.docker.internal` 解析到过期地址 172.20.10.5,连接超时),见 ADR 待决项。
- 研究沙盒参数配置化 `config/research_sandbox.json` + `scripts/research_config.py`:迭代轮数、反馈条数、随机对照次数、各类超时、候选大小/命名规则、网关监听/限流/令牌路径、各目录、筛查追加清单;严格校验(未知键/越界/路径越界即拒绝运行),安全边界不可被配置调没。人工批准绑定代码哈希:新增 `research_api.py approve`,`backtest`/`gate` 只运行已批准的代码(代码一改即失效),`--confirm-reviewed` 变为「现在批准」的快捷方式。ADR `20261112-research-sandbox-config-and-approval`。
- `optimize_15m.py --select-only`:样本外安全选参——训练段止于 2022-06-30(与 Walk-Forward W1 训练段末一致,有测试锁定),不评估验证/测试段,与 `--test` 互斥;输出的参数与 `--plateau-out` 文件交给 `gate`,滚动验证窗口才是真正的样本外。实测该流程选出 16/192/3.0,在 W1~W3 上样本外夏普 0.31(此前用重叠窗口选参的 24/192/3.0 为 1.39,其中含样本内成分),对随机对照 93.5 分位,往返 58 笔,门槛 6 项未过。
- `optimize_15m.py --plateau-out FILE`:把训练段参数网格得分写成沙盒 `gate --plateau-file` 可读的 JSON(只含训练段)。`gate` 结果新增 `protocol_notice`:候选参数若参考过滚动验证窗口(2022-07 ~ 2023-12)就不再是样本外——`optimize_15m.py` 的训练/验证段与这些窗口重叠,用它的输出做 gate 只能算参考(实测其选出的 24/192/3.0 在这些窗口上夏普 1.39、对随机对照 100 分位,但往返仅 48 笔、不在高原,门槛 5 项未过)。
- 沙盒 `gate` 子命令与 `scripts/research_gate.py`:15 分钟玉米滚动验证段(W1~W3,不触碰测试段)上跑候选 / 成本翻倍 / 随机入场对照(新增 `RandomEntry15mStrategy`,入场概率按候选活跃度对齐,`--random-runs` 参数化默认 200),汇总后调用晋级门槛;缺 `--plateau-file` 时参数高原按未通过处理。实测 200 次对照约 23 秒。示例:基准类 fast=14/slow=60 的结果与既有 ADR 一致(样本外夏普 -3.96,门槛 7 项未过)。
- 候选评估纯逻辑 `risk/candidate_eval.py`(ADR `20261112-candidate-evaluation`):滚动验证段拼接成样本外序列、随机入场分位数(种子数参数化,默认 200)、组装 `CandidateMetrics` 并调用 `check_gate`;门槛数值不放宽,无随机对照样本时分位数记 0。尚未接入沙盒 `gate` 子命令。
- 研究沙盒 CLI `scripts/research_api.py`(`data`/`backtest`/`results`)与底座 `scripts/research_sandbox.py`:仅 `pending_strategies/` 内策略、仅训练/验证段、子进程 + 环境白名单 + 静态筛查 + 需 `--confirm-reviewed`;结果附诊断书与代码 sha256。纵深防御而非沙盒,威胁模型写在 `research_sandbox.py` 文件头与 ADR。
- 研究沙盒纯逻辑底座(ADR `20261112-research-sandbox-groundwork`):`risk/diagnosis.py` 确定性诊断书(回撤/次数/连亏/夏普四维,阈值与晋级门槛同源);`risk/plateau.py` 的 `parameter_on_plateau` 计算(比门槛原文更严:邻域内至少一半格子得分为正)。毕昇先于 API 固化部署,仅作知识库问答,不接工具。
- 行情录制 wrapper 的 PID 探测改为三态（alive/dead/unknown）：Windows 访问被拒或查询失败不再被当成「已死」；记录的 PID 状态未知时 `start` 拒绝拉起第二个录制进程。新增 4 组测试，全量 867 通过。
- 测试段使用登记 `scripts/segment_ledger.py` + `config/test_segment_usage.json`:`optimize.py`/`optimize_15m.py` 的 `--test` 先查后登记再运行,重复使用即拒绝(无强制开关,重置须手工改登记文件并补 ADR);15 分钟基准测试段补登记为已使用。`tests/conftest.py` 让所有测试使用临时登记文件。
- 国信取证运行器每次运行向 `connection.json` 追加实际连接的环境、前置地址、BrokerID 与 AppID 掩码(不含账号密码授权码)。
- 行情录制二次加固(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):`wrapper.lock` 改为 OS 文件锁(Windows `msvcrt.locking` / POSIX `flock`),持有进程退出即自动释放,不再依赖 PID 探测与 unlink;`CTP_RECORD_DATABASE_DIR` 必须是项目根下的单层独立目录(拒绝 `.vntrader`/`data`/项目根/多层/绝对路径——codex 审查发现 `archive/.vntrader` 经 `isolate_state(name)` 只取目录名会把 TEMP_DIR 重映射回主库)。新增测试含真实子进程并发持锁,全量 853 通过。G8:codex 轮已审并修复 1 Critical + 2 High;`codereview` 轮待做。
- 个人信息防线 `tests/test_no_personal_data.py`:本机 `.env` 中的姓名/电话/邮箱/账号取值不得出现在任何受版本控制的文件里(失败信息只给位置,不回显取值);测试数据里的姓名一律改为虚构占位。真实个人信息只放本机 `.env`。
- 行情录制 codex 审查后的加固(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):修复 `EventEngine` 重复启动、DCE 小写合约被大写后找不到、关闭时不 flush 丢尾部数据、每 tick 同步写状态文件、旧录制列表不同步;SimNow 要求行情前置主机与已通过白名单的交易前置主机一致;wrapper 子进程不再继承调用方管道、启动后确认存活、`watchdog` 检查心跳;增量任务超时杀整棵进程树、陈旧锁自动恢复、不向下载子进程传递 `CTP_*` 凭证。合约来源问题按用户确认的方案 A 解决:受限连接交易前置,只做认证+查合约,发单/撤单/查资金/查持仓在网关层与底层请求层双重硬性禁止(ADR 已修订边界);国信 profile 新增 `CTP_RECORD_ALLOWED_ADDRESSES` 显式地址允许列表。SimNow 第二套 7x24 前置上已验证认证/登录/合约查询/订阅/tick 到达/优雅停止,以及临时库中 DataRecorder 入库(115 tick + 2 根 1 分钟 K 线);但该环境行情时间不可信(比本机快 417s、郑商所日期停在两天前),DataRecorder 的 60s 保护会丢光 tick,故正式库为 0 行。新增 `data_warning`/`degraded` 状态,时间偏差 ≥60s 时显式告警。**交易时段内的真实录制仍待验收。**
- `scripts/broker_report.py` 在国信报告签字栏旁打印提测方姓名（仅提示，不代替手写签字/盖章；姓名为空时不改动模板）。
- 补齐行情录制与增量调度的有界任务 `scripts/record_market_incremental.py`、离线生命周期测试 `tests/test_recording_tools.py` 和运行手册 `docs/runbook-market-recording.md`；**真实前置上的录制尚未验证**（合约来源已改为受限交易前置连接，见 ADR 修订）；Windows dummy worker 实测已通过并修复 `pid_alive` 在 Windows 上的误判（旧实现使 `ensure`/`watchdog` 重复拉起、`stop` 永远拒绝）；DSH 重启行为与授权行情端到端实测仍待完成（Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)）。
- 行情录制与 DSH 调度生命周期闸门(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):核对 `@goodandready/dsh-cron@0.3.43` 的有限超时、Windows 进程树终止和非 detached spawn 语义，锁定官方 `vnpy_datarecorder` + 独立 `.vntrader_record/database.db` + 短任务 `ensure`/`watchdog` + wrapper 方案；生命周期 ADR 见 [`docs/adr/20261111-market-recording-and-dsh-orchestration.md`](docs/adr/20261111-market-recording-and-dsh-orchestration.md)，本机 dummy worker 实测仍待完成。
- 文档状态对齐(Issue [#11](https://github.com/laiyinyizao007/invest/issues/11)):新增 `docs/architecture.md` §0「目标 vs 现状」逐节点实现矩阵
  (IMPL/PART/PLAN + 证据文件路径 + 实现比例 + 依赖顺序),作为"实现了多少"的单一出处;
  roadmap 目标图的 `DRDOC` 改回规划位(演练从未实跑、该文档尚不存在)、`PG`(晋级门槛)三条边改回规划位(门槛**未接入优化脚本**),
  `BS` 依赖边改回实线;§2.5 换月派生指向改回 `scripts/build_continuous.py`(实际在它那里,不在 `import_minute.py`);
  wiki §2.1 状态列改为区分"代码完成"与"已验证"(`RT/GR` 仅假网关测试、`CTP` 只有启动管道实测、`SG/DR` 从未实跑、`UR` 证据未攒齐),
  图例注明实线只表示代码存在。
- 净持仓方向不明 fail-closed(Issue [#10](https://github.com/laiyinyizao007/invest/issues/10)):新增 `risk/positions.py`(`signed_net`/`undirected`/`describe`);
  `trading/runtime.py` 的启动对账与运行中漂移检测遇到 `Direction.NET` 等方向不明的持仓时拒绝启动/告警,不再按"非多即空"算净额;
  `trading/guard_rule.py` 紧急平仓遇方向不明持仓时**不发任何平仓单**、直接升级人工(FATAL,硬锁定时 flat=False 保持进程存活)。
  此前 NET 会被当空头算反、平反。账户是否真会回报 NET 尚未实测(SimNow/国信测试环境待验证,结论补记进 ADR)。
- 架构总图 `docs/architecture.md`:分层依赖图与允许的 import 方向、磁盘状态归属表、隔离机制摘要、ADR 已知局限台账;
  roadmap §2.6 登记脚本层去重/统一交易时段表/脚本分目录三个规划位;修正 `modules.md` 写死的测试数。
- 国信测试报告的取证与自动生成(Issue [#9](https://github.com/laiyinyizao007/invest/issues/9)):
  `scripts/broker_test.py` 在国信测试环境的同一条真实连接上挂载真实风控,逐项演示 18 个测试项(穿透 T1–T7、程序化 P1.1–P6.1),
  产出脱敏证据 JSON 与渲染成图片的"截图"(`--resume` 增量补测,新的 SKIP 不覆盖旧的 PASS/FAIL);
  `scripts/broker_report.py` 用 lxml 直接填国信原模板 docx(不新增依赖):以文字定位单元格、先精确再前缀勾选、**不默认勾通过**(模板预勾选一律重置)、
  不碰国信填写区、列出"报告里还缺什么";模板含国信测试环境 IP 不入库(`--template`/`BROKER_REPORT_TEMPLATE`)。
  已用真实模板填写验证,Word 可正常打开。`.env.example` 增 `BROKER_TEST_*`;`docs/live-readiness.md` 增 C2b;`docs/broker-filing.md` 增 11 项对照表。
  经 codereview + codex 两轮外部审查后固化的运行约束:发单类测试项前**统一过空仓闸门**(有持仓或查不准持仓则一笔不发、全部记 FAIL)、
  演示委托用**唯一价格**(不同步骤不落进重复报单的同一个价格键)、测试前置地址**由操作者人工确认**(脚本无法用握手判别)、
  暂停/阈值步骤**异常也清场**(finally 解除暂停标志并撤遗留委托)、撤单要等终态确认才算数、日志只取本次运行之后的行；
  报告侧:选测项 FAIL 同样不勾「测试结论:通过」,证据图文件缺失进 warnings 与缺失清单(不静默交缺图报告)。
- 国信程序化测试合规的功能缺口(Issue [#8](https://github.com/laiyinyizao007/invest/issues/8),依据国信《外部接入系统技术测试报告》模板):
  ① 阈值预警——风控规则触达笔数/手数/金额上限时发 WARN「风控阈值触发」告警(走既有 Webhook,按指标 300 秒节流;校验失败类不预警);
  ② 暂停交易——`scripts/pause_trading.py on|off|status` 写/删 `data/state/pause_trading.flag`,标志存在时拒绝新开仓、平仓放行,切换发告警;
  ③ `collect_terminal_info.py` 增整机厂商/型号/内存;`penetration_check.py` 报告记录「关闭连接」步骤(`close_engine` 返回是否限时内关闭)。
  ④ 优雅登出——运行时 `shutdown()` 与脚本共用的 `close_engine` 在关闭引擎前先向已登录的交易/行情通道各发 `ReqUserLogout`(vnpy_ctp 的 `close()` 只断开连接、不登出),失败不打断退出。
  新增 ADR `20261111-broker-test-compliance.md`。
- 纪律机制补缺(Issue [#6](https://github.com/laiyinyizao007/invest/issues/6),依据《期货交易复盘与人性反思》):
  ① 解锁问责——`AccountGuard` 记录硬锁定时间与解锁历史,`unlock_account.py` 增加冷却期(硬锁定当个交易日内拒绝,退出码 3)
  与 `--force`(须交互输入 FORCE、不能与 `--yes` 同用);② 非策略委托检测——运行中发现 reference 不属于 CTA 策略/紧急平仓的新委托,
  告警并按交易日计数(只告警不锁定;启动对账通过后才开始计数),每日证据表记为严重事件、让"人工干预 = 0"可验收;
  ③ 实盘准入——`run_headless` 连接生产 API(`实盘` 且非 SimNow)前要求 `data/state/promotion_approval.json`,
  由新增的 `scripts/record_promotion.py` 在晋级门槛全部通过时才写出(SimNow 与看穿式测试环境豁免)。
  codex 审查后加固:运行时仍在运行(心跳 60 秒内)时拒绝解锁、触发时间未知须 `--force`、写盘失败回滚、`CTP_ENVIRONMENT` 严格校验(只允许「实盘」「测试」)、已计数委托号防回放重复。
  新增 ADR `20261111-discipline-enforcement.md`、`20261111-risk-floor-immutability.md`(风控底线不可优化、明确不做清单、本地止损已接受风险)。
- 穿透式监管接入工具(Issue [#5](https://github.com/laiyinyizao007/invest/issues/5)):新增 `scripts/collect_terminal_info.py`
  (Windows 终端信息采集,标出出网网卡与虚拟网卡)、`scripts/penetration_check.py`(看穿式测试环境演练:
  逐里程碑判定登录全流程、订阅/账户/持仓、可选跌停价买单+撤单,生成脱敏报告;生产 API 无条件拒绝);
  新增 `docs/broker-filing.md`(报备说明草稿,事实取自仓库)、`docs/live-readiness.md` C2/C3(Windows 直接运行的部署方式与要向期货公司确认的问题);
  `.env.example` 补测试环境说明。详见 PROJECTWIKI §5。
- 报备能力面补齐(Issue #4):安装 vn.py 官方模块 `vnpy_algotrading`(算法单/拆单:TWAP、冰山等)与
  `vnpy_spreadtrading`(价差/组合套利),注册到 GUI 入口 `run_gui.py`——平台能力面的演示与手动试验用,
  无人值守路径(`run_headless`/`runtime`)零改动;多账户能力(vn.py `add_gateway` 原生支持多 CTP 网关)
  在 PROJECTWIKI §5 补如实说明(项目默认单账户,`AccountGuardRule` 当前按单账户监控);
  新增日内演示策略 `strategies/intraday_demo_strategy.py`(继承 15 分钟基准,尾盘 14:30 K 线收盘
  发出强平限价单、力争 15:00 前离场,反向跳空未成交时保护性止损接管而非裸奔过夜;夜盘/尾盘不开新仓,
  仅研究/回测)。全量 647 个测试通过,总覆盖率 90%(门禁 85%)。
- 目标架构设计(PROJECTWIKI.md §2.2–2.5,依据两份研究资料):两层架构——
  **内层 = vn.py 流程引擎(确定性内核,现有 trading/risk/strategies/研究脚本/notify/Docker)**,
  **外层 = Bisheng 流程编排(AI 策略研究员,规划位)**;边界铁律 = 外层只经研究沙盒 API
  白名单端点(5 个 `/research/*` 端点)交互,`promotion_gate` 是候选策略唯一闸门,LLM 产出
  只落 `pending_strategies/`、经 walk-forward + gate 全过 + 人工批准才进 `approved/`、
  装载永远人工。附:研究方法论→系统映射表 25 条、四阶段演进路线(SimNow 验收闭环 →
  多品种与调度 → 月度滚动引擎化+研究 API 固化 → Bisheng 接入与 Alpha Factory)、
  技术债去向表、「明确不做」清单(CPCV/每日自动优化/自建前端)、规划位登记表
  (不建空目录,触发条件满足时「新 ADR + 代码同一原子提交」)。
- 新 ADR `docs/adr/20261110-bisheng-ai-researcher.md`:外层 AI 策略研究员采用 Bisheng 编排;
  含备选方案对比(Dify/LangGraph/Coze)、可替换性条款(全部业务逻辑在内层,外层只是编排壳)、
  安全约束(版本 ≥2.6.0 因 CVE-2026-82278 Code 节点 RCE、仅内网、禁用 Code 节点、与交易
  compose 物理分离);未决:部署机器(阶段四 4b 前决定)。
- 研究资料《期货交易的复盘与人性反思》(`docs/期货交易的复盘与人性反思.md`):
  由用户提供 PDF(Gemini 对话导出,51 页)全文忠实转写而来,未改写删减,仅做排版规范化;
  原始 PDF 不入仓库。内容为一次人机协作量化复盘对话——交易纪律反思、1 万元本金约束下的
  系统路线(模拟盘/风控基建先行)、样本内外切分与寻优方法、混沌工程演练等,与本仓库
  既有 ADR 的方法论互相印证。
- 同源要点梳理《期货交易复盘与人性反思-要点梳理》(`docs/期货交易复盘与人性反思-要点梳理.md`):
  按七个正交章节(心性/资金/研究方法论/风控工程/系统基建/AI 自动化/学习资源)逐条归档原文全部
  要点,每条只归一章并以 `(提问 N)` 回溯忠实转写文档,交叉关联用链接不复述。
- Walk-Forward 滚动优化器 `scripts/walk_forward_optimizer.py`(方法学见 ADR
  `docs/adr/20261110-walk-forward-optimizer.md`,在看到结果前写定):
  - 3 个滚动窗口(训练 24 月 / 验证 6 月 / 步长 6 月,验证段互斥),全部止于 2023-12-31;
    fast ∈ {10..30} × slow ∈ {40..90} 二维网格 31 组,`atr_mult=1.5` 与 `risk_budget=300` 固定
    (参数字典不传 risk_budget 键,无法覆盖),按原始夏普确定性多级排序取 Top5 自动外推验证段。
  - 测试段三层隔离护栏:`TEST_SEGMENT_START` 硬断言、API 反例测试、源码扫描测试
    (2024+ 年份字面量只允许出现在常量定义行),不提供任何 `--test` 开关。
  - 多进程:模块级可 pickle worker + 单个 `ProcessPoolExecutor` 贯穿全部窗口(避免每窗口重建池
    导致 lru_cache 失效与重复 spawn),`--workers 1` 串行降级,`--workers` 拒绝 0/负数;
    每进程独立 K 线缓存 + 回测切片后断言不含测试段 K 线;经 `run_engine_bars` 读取
    `skipped_by_budget` 报告预算弃单率,`backtest_lib.py` 零改动;明细输出
    `data/results/walk_forward_C15.csv`(不纳入版本控制)。
  - 一次性演练结果(108 次回测):训练段最优夏普本身全为负,15 组验证参数夏普全部为负、
    正夏普窗口占比 0%,预算弃单率全程 0%(空仓并非预算造成),与基线 ADR 负面结论互证;
    串行/并行两次运行数值完全一致。演练中修复 vn.py 回撤金额带负号导致的两个统计 bug
    (汇总最差回撤被 0 吞掉、排名平局键方向反),复跑确认排名与数值不变。
  - codex 双轮审查(第 1 轮工作流 + 第 2 轮 clink codex CLI 833 秒含真实 spawn 动态探针):
    无 Critical/High;隔离护栏经伪造 2024 窗口实测不可绕过。采纳修复:单池复用、
    真实 spawn 冒烟测试、summarize 非有限夏普防御(当前 vn.py 的 nan_to_num 使其暂不可达,
    属防升级缺口)、Window `__post_init__` 校验、tz-aware 入参归一化、奇数 fills 截断测试、
    空结果不写 CSV;源码扫描层"防误改提示网"定位与弃单率三类误读场景写入 ADR。
  - 新增 `tests/test_walk_forward_optimizer.py` 29 个测试,新模块覆盖率 99%;
    全量 643 个测试通过,总覆盖率 88%(门禁 85%)。
- 极限压力测试前置改造(混沌工程四演练,详见 ADR `docs/adr/20261110-ctp-error-code-and-watchdog-policy.md`):
  - `risk/order_policy.py`:`extract_ctp_error_code()` + `CTP_ERROR_KIND` 码表,废单分类升级为
    「错误码优先 → 关键词回退 → 未知 fail-closed 硬拒绝」三级决策;码表只收录实测/官方背书的 8 个码
    (21/81/63/32/33/14/30/31),软码表留空待 SimNow 实测;模块保持 100% 覆盖率。
  - `trading/guard_rule.py`:开仓单硬拒绝时发 FATAL「严重错误,已停止策略开仓」(带 CTP 错误码与原因);
    新增看门狗撤单唯一公开入口 `cancel_open_orders_on_stale_tick()`(复用 offset 过滤,平仓/紧急平仓单不撤)。
  - `trading/runtime.py`:交易/行情分通道连接状态机(精确措辞驱动、边沿告警、只认「登录成功」为恢复凭据,
    只读属性 `connection_state`);假死看门狗首次越限强撤全部开仓挂单,新 Tick 复位可再次触发,
    `stale_cancel_open_orders` 开关可回退旧的只告警行为。
  - `tests/fakes.py`:`FakeGateway` 新增 `disconnected` 模式(发单本地快速失败返回空串、不产生 OrderData,
    复现 `CtpTdApi.send_order` 断连行为);新增 `tests/test_disconnect_signal.py` 锁定
    「断线信号一次性消费、重连无一窝蜂补发、新信号不受影响」三条语义。
  - `scripts/drill.py`:`disconnect` 场景增加重连后连接状态、窗口内发单失败告警计数、「信号积压补发:无补发」证据项。
  - 测试由 572 个增至 614 个,全量离线通过,总覆盖率 89%(门禁 85%)。
  - G11 偏离说明:Gemini CLI 仍报 `ProjectIdRequiredError`(缺 `GOOGLE_CLOUD_PROJECT`),simple-gemini
    不可用,本 ADR/测试/文档由主模型撰写;plan-down 的方法澄清由 gemini-2.5-flash 与 planner 完成,
    codex CLI 的设计复核经 clink 后台完成(结论与本方案一致:同一组错误码、同样 fail-closed)。
- `README.md`:项目上手入口 —— 说明"这个项目在干嘛、怎么用"。含项目定位与状态提示(策略结论为负面、只用于管道测试、当前不连实盘)、
  Mermaid 架构速览、目录结构、环境准备与 `.env` 配置、按场景分组的命令(数据下载/主力连续/优化/无头运行/Docker/图形界面/
  SimNow 演练/废单采集/无人值守日报)、账户级风控阈值表、测试命令、已知限制表、相关文档索引。
  文档生成偏离说明:按 G11 应由 `simple-gemini` skill(Gemini CLI)生成,但本机 Gemini CLI 报
  `ProjectIdRequiredError`(缺 `GOOGLE_CLOUD_PROJECT` 环境变量),故由主模型撰写;内容已按仓库脚本的 argparse 实际参数逐条核对。
- `scripts/simnow_guard.py`:SimNow 前置机地址白名单(`SIMNOW_TD_ADDRESSES`)+ `check_simnow_address()` + `normalize_address()`。
  `--confirm-simnow` 只是布尔开关,挡不住"配置写错连上真券商";CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,
  **只能靠前置机地址区分**,地址不在表内默认拒绝连接。`collect_reject_texts.py` 与 `drill.py` 共用这一套约束。
- `scripts/drill.py`:把运行手册第 5 节的实测项做成**可重复执行**的演练场景 —— 成交→持仓回报延迟、行情断流看门狗、
  交易连接断开与自动重连、当日熔断链路、累计死线链路、夜盘首根 Tick 的交易日归属。
  演练状态与实盘状态**完全隔离**(独立的 `.vntrader_drill/` 目录与独立的风控状态文件),熔断/硬锁定不会污染实盘;
  熔断场景用"注入权益来源"的方式模拟亏损,只改本进程内的取值,真实账户权益不受影响。
  前提不满足(如收盘后跑看门狗场景)的场景标记为**跳过**而不是判失败。⚠ 必须在容器里跑(时段判断用系统本地时区)。
  非白名单地址上跑会**真的发委托**的场景(`latency`/`daily-loss`/`hard-lock`)需要再加 `--confirm-any-address-trades`。
- `scripts/unattended_report.py`:无人值守验收的**证据**工具 —— `--report` 解析 vn.py 每日日志与心跳文件生成
  "每日证据表"(启动次数/成交笔数/异常事件/心跳新鲜度)+ 验收清单;`--probe-webhook` 做连通性自检
  (有 `WEBHOOK_URL` 就真发一条,没有就起本地接收端离线验证"送达 + 业务失败可辨识")。结果写进 `docs/unattended-log.md`。
- `docs/unattended-log.md`:无人值守运行记录(由脚本生成,标记之间的内容会被覆盖)。
- `scripts/collect_reject_texts.py`:在 SimNow 模拟盘上故意触发几个**必定被拒**的委托,采集 CTP 的真实错误文本,
  用于校准 `risk/order_policy.py` 的 `HARD_KEYWORDS/SOFT_KEYWORDS`;结果记入 `docs/reject-texts-simnow.md`。
  安全约束:必须 `--confirm-simnow`,且交易服务器地址必须在 SimNow 官方地址白名单内;
  交易时段内运行自动改用越界价(不可能成交)并跳过时段场景;
  发单前先查持仓(已有持仓或交易连接断开就中止,退出码 5);
  跑完主动查持仓,发现非零持仓即告警并以非零码退出;所有价格按 `price_tick` 对齐。
- 15 分钟基准策略(`strategies/trend_atr_15m_strategy.py`)、`scripts/optimize_15m.py` 与实验方案 ADR;结果:测试段 -20.2%、夏普 -1.08,成本吞掉全部毛利,**无可延续的优势**(详见 `docs/adr/20261110-baseline-15m.md`)。测试段已被基准使用一次。
- `scripts/import_minute.py`:把购买的分合约 1 分钟线导入为差额复权主力连续合约并合成 15 分钟线(新浪日线补齐缺口、起点成交冷清段裁剪、交易日归属);玉米实测与新浪日线交叉验证通过。
- `scripts/fetch_data.py`:AKShare(新浪源)下载 PP/L/EB 日线与 1/5/15 分钟线,清洗后写入 vn.py 数据库(日线、1 分钟)或 CSV(5、15 分钟);指数退避重试、失败隔离、重复运行幂等。
- `scripts/bootstrap.py`:统一启动入口,固定中国时区与项目内 `.vntrader/` 数据目录。
- `tests/test_fetch_data.py`:32 个单元测试,覆盖率 97%。
- `PROJECTWIKI.md`、`docs/adr/`(vn.py 选型、中国时区两条 ADR)。
- 项目骨架(`0981db3`):`requirements.txt`、`.gitignore`、`config/.env.example`、`strategies/` `risk/` `notify/` `tests/` 空包。
- Phase 2(策略与回测):`risk/risk_guard.py`(风险预算、只收紧的移动止损、连亏熔断)、`risk/promotion_gate.py`(8 条晋级门槛与夏普置信区间)、
  `strategies/trend_atr_strategy.py` 与随机入场对照、`scripts/backtest_lib.py`、`scripts/optimize.py`(三段样本外 + 邻域平滑)。
- `scripts/fetch_data.py` 新增玉米(C)、甲醇(MA)。
- ADR:账户风险预算、负面回测结果、晋级门槛。
- `scripts/build_continuous.py`:分合约日线 → 差价法复权主力连续合约,并量化换月跳空(玉米中位 44 点、甲醇 75 点)。
- 晋级门槛 ADR 增补"样本外年数与置信区间的张力"表;负面结果 ADR 增补"回测结论作废待重做"与样本切分纪律。
- 测试扩充至 320 个(英文/中文 vn.py 语言环境均通过),覆盖率 97%。

- Phase 3 账户级风控:`risk/account_guard.py`(当日 600 / 累计 2000 元双层熔断、原子持久化、日初基准取上次采样)、
  `risk/order_policy.py`(废单分类/重试节奏/风暴检测)、`trading/guard_rule.py`(挂在 vnpy_riskmanager 发单链上的规则:
  拦截开仓放行平仓、紧急平仓状态机)、`trading/safe_risk.py`(平仓单跳过计数类规则)。
- Phase 4 运行时:`trading/runtime.py`(启动对账、看门狗、退出码语义)、`scripts/run_headless.py`、`scripts/unlock_account.py`;
  策略新增实盘模式(Tick→日线、信号存到下一交易时段发单、状态持久化与重启恢复)。
- Phase 5:`notify/webhook.py`(非阻塞通知)、`Dockerfile`、`docker-compose.yml`、健康检查心跳。
- `scripts/build_continuous.py --save` 与 `CADJ/MAADJ` 品种;`optimize.py` 为复权数据使用事先确定的新切分。
- 文档:`docs/runbook-simnow.md`、`docs/live-readiness.md`,ADR 增 3 条(账户风控、部署与实盘模式、负面结果更新)。

### Changed（变更）
- `trading/runtime.py::_on_log`:原合并式 `_login_notified` 单次通知替换为交易/行情两通道各自的边沿状态迁移,
  告警标题(「网关连接断开」/「网关已连接」)与消息子串保持不变,依赖它们的 drill 与无人值守日报不受影响。
- `Dockerfile`:把 `locales` 层挪到 `pip install` **之后**。改动前面的层会让 pip 重新下载全部依赖
  (容器→PyPI 实测 12.6 MB 的 sdist 要 400 秒以上),挪到后面则能命中已有构建缓存,只需重新拉取 `locales` 这一个小包。
- 测试扩充至 417 个,覆盖率 92%(目标 85%)。
- 测试扩充至 543 个,覆盖率 89%(目标 85%);新增用例覆盖:结构级脱敏、飞书业务码 fail-closed、
  队列排空超时、撤单与成交竞态、只平演练自己开的仓、告警"发出/送达"区分、缺天打断连续段、熔断类告警计数。
- 测试扩充至 559 个,覆盖率 89%(目标 85%);新增用例覆盖:周末日志不计入交易日连续段、周末严重事件仍打断连续段、
  键名脱敏、非 JSON 标量脱敏、演练拒绝在锁仓账户上运行、净持仓账户方向单列、平仓价挂在交易的那一侧、
  退出时按通知器排空预算关闭。
- 测试扩充至 565 个,覆盖率 89%(目标 85%);新增用例覆盖:演练结果文件连续两次重写仍更新主记录、
  采集文档的 BEGIN 标记不被丢掉、排空预算计入正在发送中的消息、FATAL 被提到队首、
  对端卡死时 `close()` 按上限返回。
- 测试扩充至 572 个,覆盖率 89%(目标 85%);新增用例覆盖:净持仓账户上平掉演练自己开的仓、
  平仓没成交时撤单、队列满时 `close()` 仍能停住后台线程、关闭后拒绝再收消息、
  数字键等于密钥时被脱敏、通知器关闭抛异常时 shutdown 仍能走完。
- 分钟线时间戳由新浪的"K 线结束时间"前推一个周期,统一为 vn.py 的"开始时间"。
- **文档同步(2026-11-10)**:PROJECTWIKI 补充 15 分钟基准模块、购买数据的派生数据流、GUI 入口、容器内连通 SimNow 未验证等条目;
  `docs/runbook-simnow.md` 与 `docs/live-readiness.md` 刷新 Phase 3–5 的实际状态与剩余实测项;Issue #1 的阶段勾选同步。
- 明确记录:**15 分钟基准的测试段(2024-01 ~ 2026-06)已被使用一次**,新候选策略不能再用它做最终确认。

### Fixed（修复）
- **状态页:净化层的输入上限与脱敏顺序**(2026-11-13):净化**先脱敏再截断**——反过来会让跨过截断点的秘密只露出前缀(实测泄露 9 个字符);截断挡住了 8 轮 percent-decode 的开销(600 KB 的串 300 毫秒 → 1.1 毫秒)。
- **状态页:净化层的零宽字符与输入截断**(2026-11-13):路径规则改为**容忍**零宽/方向控制字符,不再全局删除它们(删除会改动与路径无关的服务器原文);净化输入先截断,600 KB 的百分号串从 300 毫秒降到 1.6 毫秒;另外补上没有盘符的 Windows 根路径。**仍存在的局限已写明**:超过 8 层的百分号编码不处理。
- **状态页:codex 第十四至十六次复核(45.4 → 45.6 → 46.0/50)**(2026-11-13):第十六次无 Critical/High/Medium,codex 明确建议收口。本轮修:录制样例改成真实产出(UTC→+08:00,原先差 8 小时)、样例自洽性测试的容差改成逐字相等、种子一致性改用生成器逐字节断言、契约测试不再被 jsonschema 整体门控(只装了面板依赖的环境里不再静默消失)、契约 §1 的时间例外补上第三族(进化的 created_at/updated_at)。
- **券商 API 压缩包不入库**(推送前核查发现,2026-11-13):`docs/API--整个文件包发送给客户*.rar`(两个,共约 40MB)在 `e7372f8` 里被批量 `git add -A docs` 误带入库,违背 `.gitignore` 的『券商原始文件,不入库』,且已随 `origin/feat/16-constitution`、`origin/feat/16-ladder-eta` 发出。已取消跟踪(本机文件保留)、`.gitignore` 加 `*.rar/*.7z/*.zip`、新增 `tests/test_no_binary_blobs_tracked.py`(压缩包不得入库、文件不得超过 5MB;补的是 `test_no_personal_data` 不检查 >2MB 文件的盲区)。**未改写历史**(blob 已在远端,推送 main 不新增暴露)。压缩包内容未能核实;用户确认其中疑似姓名的 2 字值不是真实姓名。
- **连续段口径改为『只有严重事件才打断』**(用户 2026-11-13 拍板):此前工作日任何事件(含警告)都打断,而周末只有严重事件打断;实盘里 11-10/11-11 只有『连接断开』警告,连续段因此为 0 天。现在警告不打断,但**必须数出来**:页面标题显示『连续无严重事件 N/5 天,期间 M 次警告』,契约新增 `streak_warning_count`。无法判断的日子与缺日志的工作日仍打断。**这是放宽验收标准**——真实数据里连续段由 0 变为 2/5,期间 21 次警告(11-10 一天就有 20 次连接断开)。`委托被拒`/`账户变更`也被一并放过,见计划文档的已知风险。
- **状态页:G8 第一轮(Gemini 侧)确认 45/50 —— G8 两轮均过线**(2026-11-13):新发现的 High/Medium 指向同一个真问题——提示语写着『详见服务端日志』却根本没记日志(顶层异常处理器与 `_collect_safely` 都没记堆栈)。已补 `logging.exception`,堆栈只进服务端日志、不进响应;加了测试。codex 那轮 46.0/50,Gemini 那轮 45/50。
- **状态页:G8 第一轮(Gemini 侧)复核 44/50**(2026-11-13):此前一直因服务商每日配额跑不起来,改用 gemini-2.5-flash 后跑出。三项发现已修:日志解析缓存满后不再 clear 全部(改为逐出最久没用的,否则日志一多就反复重解析)、进化摘要的 LRU 先刷新时间戳再逐出、台账解析捕获 RecursionError。**再次确认被服务商卡住**(各档位 429/503/404),已排定时重试;确认前不宣称 G8 已通过。
- **状态页:codex 第十至十三次复核(44.1/50 → 44.3/50 → 42.9/50 → 45.3/50)**(2026-11-13):第十一轮的 High 是『响应形态与生产行为分叉』——`/api/research/evolution` 的真实响应带 Section 外壳而契约/类型是裸数据,前端照类型写代码、联调时才拿到 undefined;现统一为裸数据,并补上**真实响应对契约**的断言(此前只校验样例)。第十二轮发现我加的 TTL 缓存有容量缺陷(两类键混在一个 dict 里,有效容量只有 32,越过 32 个战役后缓存完全失效、总览每 8 秒重读全部台账)→ 拆成两个 dict 分别计数并按最近命中逐出,补了 4 条在旧设计下会红的测试。第十三轮 **45.3/50,达到 ≥45 可发布档**;剩余缺陷只在样例里(两份样例与自己的心跳字段矛盾),已修并加了样例自洽性测试。
- **状态页:codex 第五次复核(44.6/50)**(2026-11-13):无 Critical/High,2 个 Medium + 2 个 Low 已修——超过上限时未检查的战役会让区块变成 warn 并说明未检查数量(不再显示绿色);零宽字符也能拆开的无盘符根路径与全角路径已覆盖;缓存改用内容哈希、候选源码直接重新哈希;重复定义的常量已合并。**尚未第六次复核,仍未达到『≥45 可发布』。**
- **状态页:codex 第四次复核(43.7/50)**(2026-11-13):无 Critical/High,3 个 Medium 已修——尾行前缀检测只看行尾且只看消息部分(不再让『当前权益』误判、也不再让已识别的严重事件降级);研究结果禁止矛盾字段(成功不许带 error、error 不许为空、backtest 不许带 passed);编码路径检测组合全部五条规则并每轮重做 `%u` 转换。另补 junction 回退分支的测试。**尚未第五次复核,仍未达到『≥45 可发布』。**
- **状态页:codex 第三次复核(42.5/50)的发现**(2026-11-13):(1) 日志最后一行停在多字节字符中间时判**不可信**(被截掉的可能正是『硬锁定』的最后一个字),字符补全后恢复;(2) 运行结果 `status` 只允许缺省或 `strategy_error`,策略报错必须 `passed=false` 且 `error` 有 `type`/`message`,`ran_at` 须是合法时间,通过 ⇔ 无失败项;(3) 路径净化:反斜杠路径目录名允许单个空格、全角盘符字母、零宽字符、解码至稳定最多 8 轮、正斜杠盘符不再误伤 URL 路径里的 `a:/b`;(4) `Path.is_junction` 缺失时退回 reparse point 属性;(5) 令牌创建的检查/创建竞态写明威胁模型。示例数据里 `status` 由 `ok` 改为 `null`(真实产出里没有 `ok`)。**尚未第四次复核,不宣称达到『≥45 可发布』。**
- **状态页:codex 第二次复核(36.5/50)的发现**(2026-11-13):(1) **重启不再把缓存的旧权益写成『刚采样』**——`AccountGuard.update(sampled_at=None)` 保持原值,`_load()` 恢复 `sampled_at`(坏值丢弃且不触发硬锁定),规则按账户号分别记录资金回报时间、换账户清空;(2) 令牌位置按**字面路径**校验并拒绝路径链中的符号链接/junction(`data/dashboard` 曾可被 junction 指向 `data/state`),创建前再验一次;(3) 日志严格 UTF-8:非法字节使日志不可信(坏字节落在『当日熔断』中间曾让日志被判干净);(4) JSON 拒绝 `1e999` 等溢出数并把深层嵌套当作损坏;(5) 运行结果必须有 `run_id`(等于文件名)、sha、`report`、`failures`/`stats`/`error` 等,残缺文件不再显示『通过』;(6) 路径净化补上带空格的引号路径、紧贴字母的盘符、`%u`、多层编码、全角字符。**尚未第三次复核,G8 验收仍未满足。**
- **状态页:codex 复核剩余 4 项**(2026-11-13):(1) **风控文件新增 `sampled_at`(交易侧改动,用户已同意)**:`AccountGuard.update(..., sampled_at)` 记录权益被采样的时刻,`AccountGuardRule` 监听 `EVENT_ACCOUNT` 取『本账户资金回报到达』的时间——网关断开后风控进程仍每分钟落盘、文件 mtime 一直新鲜,采样时间却不再前进;面板按采样时间算年龄,旧格式文件判 `stale`,无时区 / 非法值判 `corrupt`;该字段只供可观测,不参与熔断。(2) 令牌回退发布 fail-closed(不再先发布空文件),临时文件清理覆盖 `fsync` 失败。(3) 研究运行结果按真实产出建立结构校验(`{}`、未知 `kind`、`passed` 非布尔都算不可读),并检查最近 200 个而不是只检查显示的 10 个。(4) 净化层对百分号编码片段做至多 2 轮检测解码;顺带修复盘符正则把 `https://` 当盘符的误伤。10 项均已修改,**尚未再次交 codex 复核**,G8 验收仍未满足。
- **状态页:codex 复核后『只读承诺』边界与严格解码**(2026-11-13):(1) 入口在导入任何项目模块前关闭字节码写入,首次导入不再往 `risk/` 等交易侧包目录写 `__pycache__`(测试在无缓存的源码副本里跑入口,修复前失败);(2) 令牌文件位置由拒绝名单改为白名单——只许 `data/dashboard/` 之下,此前漏掉的 `.vntrader_drill` / `.vntrader_brokertest` / `.vntrader_probe` 现在一并拒绝;(3) 状态文件严格 UTF-8 解码,非法字节为 `corrupt`,不再替换成 U+FFFD 后继续当作正常数据;(4) JSON 拒绝 `NaN` / `Infinity`。剩余未修:风控文件采样时间语义、研究结果结构校验、编码形式 UNC 净化、令牌回退发布路径;修复后尚未再次交由 codex 复核,G8 验收仍未满足。
- **状态页:codex 复核后日志路径 4 项**(2026-11-13):(1) 增量缓存改为校验整个已读前缀的 SHA-256——头尾抽样挡不住『保持头尾不变而同长度改写中间』(codex 复现:把 12 个 ASCII 字节换成同为 12 字节的『启动失败』,严重事件永久不显示);60 MB 日志追加后约 104 毫秒。(2) 日志可信度:记录须匹配完整形状、日期须等于文件名日期、记录占比不低于 20%,有不可识别日志时无论有无其他正常日志都降为 unknown。(3) `freshness()` 统一做未来检查,日志未来 mtime 不再被当作刚更新。(4) `snapshot()` 在副本上处理无换行尾行,崩溃时最后一行的严重事件不再漏报;此前所有等价性测试的数据都以换行结尾、从未覆盖过尾行。6 份真实日志(含 8.7 万行)全部可信。**codex 复核提出的其余 6 项仍未修,修复后尚未再次复核。**
- **状态页 G8 第 2 轮(codex)审查的 12 条发现,全部处理**(2026-11-12/13;1 Critical / 6 High / 3 Medium / 1 Low,每项都做了变异检查):
  - **[Critical] 令牌文件默认写在交易系统的状态目录 `data/state/`**,违反『绝不写入交易系统』。改到专属目录 `data/dashboard/`,`dashboard/config.py` 拒绝令牌位于任何面板读取目录或交易侧目录树;新增对交易侧 6 棵目录树做完整快照(含目录 mtime)的测试。令牌创建改为『先写临时文件再 `os.link` 原子发布』,任何时刻文件要么不存在要么内容完整,并发首次启动安全;所有 I/O 错误包成不含路径的 `TokenError`。
  - **[High] 假安心类**:损坏的心跳曾退回文件 mtime 显示『运行中』(并有测试固化);时间戳在未来曾被当作 0 秒新鲜;新鲜心跳曾掩盖陈旧的风控状态(风控状态现有独立年龄,落后 >180 秒即 `stale`,依据 `on_timer` 每秒调用 `guard.update()`、至少每 60 秒落盘,并有读源码的耦合测试);任意非空垃圾日志曾被当成『干净的一天』(现要求含可解析时间戳,全是垃圾 = `corrupt`,与『没找到』区分);录制 `stopped` 单独一行曾显示 ok(现要求合法终态时间);批准记录损坏曾被当成『0 个批准』;损坏的运行结果曾被静默丢弃;哈希缓存曾使已改动的候选继续显示『已批准』(已删除缓存)。待补发队列读不了曾被说成『不存在』。
  - **[High] 净化层泄露**:数字账号、`account_no` / `login_id` 等键、`pwd` / `authorization` / `cookie` / `session` 键、`PASSWORD=` / `Authorization:` / `Cookie:` 文本、UNC 与 `\\?\` 路径、URL 编码路径、更多 POSIX 根。键名先 NFKC 规范化并去分隔符。账号掩码只对标量,容器继续递归(契约示例 `live.data.account` 是资金对象,曾被整个字符串化)。
  - **[Medium]** 静态文件哈希曾只在启动时校验一次(现每次请求读入字节、校验、发出同一份字节,无 check-then-use);`HEAD` 曾执行完整采集并在 ASGI 层发出 body(现只验证令牌,恒 200);心跳、账本、队列曾无上限读取(现有上限,超限判 `corrupt` 而不截断)。
- 状态页自查(G8 复核时发现):**日志增量解析的指纹只看末尾 64 字节,会被『旧长度处末尾相同、更早内容不同、文件更长』的替换文件骗过**,旧文件的计数混进新文件(对干净的新日志报严重事件)。指纹加上文件开头 64 字节。上一版同名测试没有真正构造出碰撞(在弱实现下也通过),已重写并断言构造成立。符号链接检查里有一个恒为假的冗余子句,已简化。
- 状态页:`dashboard.safe` 曾经 `notify.webhook` 间接 `import requests`,而 `requests` 不在 `requirements-dashboard.txt` 里——只装了 Web 依赖的环境会导入失败。`redact` 抽到无依赖的 `notify/redact.py`(`notify.webhook` 重新导出,既有调用不变);新增测试:面板用到的第三方包必须全部在 `requirements-dashboard.txt` 里声明。
- 状态页:`unattended_report.py` 的纯逻辑抽到 `evidence/logstats.py` 后,容器镜像需要同样复制该包(`Dockerfile` 已由另一会话补上 `COPY evidence`)。状态页本身**不**进交易镜像,有测试守着。
- 状态页性能:当日日志在增长时,5 / 20 / 60 MB 日志下总览请求曾分别需要约 1.9 / 8.3 / 24 秒(前端每 8 秒轮询,日志一大请求就堆积)。新增可增量喂入的 `DayLogParser` 与带前缀指纹的增量缓存,现在与日志大小无关(约 15–20 毫秒),并在启动时预热。等价性靠构造保证:任意切分点、200 种随机切分、特殊换行符(CR / VT / FF / U+2028 等)的结果都与一次性全量解析逐字段相同。
- 测试方法论发现:`httpx` 会把 `//api/trading` 当协议相对 URL 改写成 `/trading`,所以 `TestClient` 测不到服务器对原始双斜杠路径的行为——这类路径改用直接构造 ASGI scope;本环境经 bash heredoc 写入的 `\\` 会被折叠成 `\`,含 UNC 的测试曾因此没有真正测到 UNC,已用 Edit 工具重写。
- 状态页:`dashboard/safe.py` 的敏感键名正则曾把 `passed` / `bypass` / `pass_rate` 当成密码键整个替换成 `***`(研究页的通过状态会永远显示星号);改为 `password`/`passwd`/`passphrase` 任何位置都算,单独的 `pass` 只在整个键或键末尾才算。新增『所有契约示例经净化后原样不变』的回归测试。
- 状态页:每日日志只统计有内容的文件。真实数据验证发现——交易进程停了 71 小时,别的进程新建了 0 字节的当天日志,曾让『日志』与『告警』区块显示『刚更新、ok』。
- `drill.py latency` 开仓限价改为「卖一 + N 跳」(新参数 `--aggress-ticks`,默认 3,0 = 贴卖一):夜盘后段清淡行情里贴着卖一挂的限价买单 30 秒没成交;限价买单按对手价成交,上限只封顶滑点。单测断言挂价。**尚未在真实行情下重测**。
- 研究网关提交改为「先记账、再落文件」(审查发现):进程在两步之间崩溃时,最坏是多扣一个额度,而不是留下未记账的候选而多放额度;重名在记账前判定、不占额度;文件写入失败会把刚记的账退回。各有回归测试。
- 代码审查(2026-11-12)对研究沙盒的两处修复:`research_api.py review` 在批准前重新校验文件哈希,阅读期间文件被换则拒绝(批准必须绑定到用户读过的内容);`risk/report.py` 的随机入场门槛改取 `GateThresholds` 默认值而非写死 95%,`selection.window` 含非字符串元素时不再让整份报告变成「生成失败」。各有回归测试。
- **第八轮 codex 审查(2026-11-10,clink + codex CLI 动态复现)追加修复** —— 1 个 High / 1 个 Medium,
  均为本轮看门狗/废单改造引入或暴露的问题,修复与新增回归测试在同一原子提交内:
  - **[High] 跨交易时段重入时看门狗误报「行情假死」并误撤新挂单**:`_last_tick` 是单调时钟时间戳且从不按
    交易时段边界重新武装,午休/夜盘休市后定时器若先于本时段首 Tick 触发,会把整个休市时间(实测复现 7261s)
    计为静默,对年龄 0 秒的新开仓单也强撤,而策略 `pending_entry` 一次性消费不重发,等于静默丢失入场。
    → 运行时记录上一次定时器的时段状态,在「非交易→交易」边沿把 `_last_tick` 置 None、解除假死边沿,
    等本时段首 Tick 后才开始计 60s 静默;初始基线在 `start()` 记录,首个定时器不被误判为边沿。
  - **[Medium] 已当日锁定后每笔硬拒绝重复发 FATAL,且「已撤销全部挂单」与实际未撤单不符**:FATAL 原在
    `_lock_daily()` 之前无条件发射。→ 仅在锁定状态真实转换(`guard.can_open()` 为真)时发 FATAL,
    已锁定后的后续硬拒绝只保留 ERROR「委托被拒」留痕。
  - **同类既有问题一并修复**:规则自身的 120s 开仓委托超时也按含休市的单调时间计时,午休两小时后旧挂单会在
    开盘瞬间被撤。→ 运行时把交易时段判定注入 `AccountGuardRule.session_active`,休市期间不做超时撤单,
    时段重入时把在跟踪挂单的计时起点拨到开盘时刻(休市时间不计入挂单年龄)。
  - 新增 3 个回归测试:时段重入不误报(运行时)、规则超时剔除午休(运行时夹具)、锁定后重复硬拒绝只发一次 FATAL(集成)。
  - 第二轮 clink codex CLI 复核(动态验证夜盘 21:00 重入、休市中启动、启动失败早退三条路径,无 High/Medium),
    其 4 条 Low 建议全部处置:① 时段边沿的 `_cancel_requested` 清理收窄为只拨开仓单 id,平仓/紧急平仓撤单的
    10s 节流不被休市边沿清掉;② 规则 `_was_in_session` 基线在运行时注入时同步,与运行时 `_in_session` 同一初始认知;
    ③ 补仓库级测试 3 个(夜盘重入、休市中启动、节流清理范围);④ 值班手册说明「日锁后硬拒绝只有 ERROR 属预期」。
    测试由 611 个增至 614 个。
- **第五轮 codex 审查(2026-11-10)追加修复** —— 2 个 Medium / 4 个 Low:
  - **[Medium] 有日志的周末被当成交易日,"连续 5 个交易日"凭空多出两天**:容器是常驻的,周末照样有日志文件
    (周五夜盘跨过零点还会写进周六的文件 —— `trading_day()` 把 20 点后的夜盘归到下一个工作日)。
    `longest_clean_streak()` 原先"有日志就算一天",周末的日志文件于是被算成交易日,验收结论虚高。
    → 周末**既不计入也不打断**;但周末日志里的**严重**事件(紧急平仓、熔断、硬锁定、对账失败…)
    仍然打断连续段(夜盘跨零点的事故不能因为落在周六就被放过),周末的**警告**(空闲期连接抖动)不打断
    (否则验收标准永远达不成)。
  - **[Medium] 演练只看净持仓,多空锁仓(净额 0)时可能误平既有仓**:`latency` 场景进场前只检查净持仓 ≠ 0,
    而多空锁仓(各 1 手)的净持仓是 0 → 演练照跑,收尾平仓时会动到演练之前就存在的仓位。
    → 进场前按**总手数**判断(`gross_position()` > 0 就拒绝演练),进场时的总手数记进 `gross_baseline`,
    收尾用 `at_baseline()` 同时核对净额**与**总手数;`net_position()` 里 `Direction.NET`(净持仓账户)
    单列处理,既不算多也不算空 —— 不能当成空头去平。
  - **[Low] 演练的平仓价一律用买一,平空时永远成交不了**:`_close_position()` 平多(卖出)与平空(买入)
    都用 `bid_price`(买一)。平空是**买入**,挂在买一(比卖一低)上等不到成交 → 演练变成"平仓超时",
    还容易被误判成"平不掉"。→ 平多(卖出)挂**买一** `bid_price`,平空(买入)挂**卖一** `ask_price`。
  - **[Low] `extra` 的脱敏漏了两处,密钥照样能发出去**:① dict 的**键**没脱敏 —— 键名是密码时只脱敏值等于没脱敏;
    ② 非 JSON 标量(datetime、Decimal、自定义对象)最终会被 `json.dumps(default=str)` 转成字符串,
    **先序列化再脱敏就来不及了** —— `str(obj)` 里的密码会原样发出去。
    → 键名同样走 `redact()`;非 JSON 标量在 `redact_obj()` 里**先转成字符串再脱敏**(数字/布尔/None 原样保留)。
  - **[Low] `close()` 的形参名与"单条上限"同名,极易误用**:形参 `flush_timeout` 与 `flush_timeout` 属性
    (单条消息的最坏耗时)同名,但两者含义不同(一个是**排空整队列的总预算**)。
    → 形参统一改名为 `drain_timeout`(与 `drain_timeout` 属性同名同义),并同步 `Notifier` 协议、
    `NullNotifier`、`RecordingNotifier` 与全部调用点。
  - **[Low] 退出时的"仍有持仓" FATAL 可能随进程退出一起丢掉**:`shutdown()` 在关闭通知器前发这条 FATAL,
    而关闭的默认预算是 10 秒 —— 一条消息的最坏耗时是 (1+重试次数) × 单次超时 + 全部退避(默认 34 秒),
    慢一点的对端就收不到这条兜底告警。→ 按通知器自己的 `drain_timeout` 关闭。
  - **[Low] `EVENT_MARKERS` 一行日志可能命中多个标记**(措辞有重叠),"异常事件"列计数偏大 ——
    **只影响展示**,判断"这一天有没有异常"只看有没有事件,多算几次不改变结论。已在代码里注明,不改动行为。
- **第六轮 codex 审查(2026-11-10)追加修复** —— 2 个 Medium / 2 个 Low(主逻辑全部确认正确,以下是审查中新发现的缺陷):
  - **[Medium] 演练结果文件重写一次后丢掉 `END_MARK`,之后再跑就不再更新主记录**:`drill.write_results()`
    重写时只写正文、不写回标记 → 第二次运行 `old.find(END_MARK)` 返回 -1 → 走"没有标记"的分支,
    结果只落到 `simnow-drills-raw.md`,主演练记录停在旧结果上**且不报错**。
    → 重写时把 `BEGIN_MARK` / `END_MARK` 一起写回;补"连续跑两次仍更新主文件"的回归用例。
    同一类缺陷在采集脚本里也存在(`write_collected()` 重写时丢 `BEGIN_MARK`,连"重新采集会替换这一段"这句
    人话一起丢),一并修掉。
  - **[Medium] 排空预算没算"正在发送中的那条"**:`drain_timeout` 只按队列长度(`qsize()`)算,
    而 worker 已经取走、还没发完的那条**不在队列里** → 预算少一条 → 退出时最后那条 FATAL 告警
    随进程一起消失(这正是要兜底的场景)。→ 新增 in-flight 计数,`drain_timeout` 按"队列剩余 + 正在发送"计算。
  - **[Low] 排空预算没有上限**:队列上限 200、单条最坏耗时约 34 秒 → 对端持续超时且队列塞满时,
    排空预算接近 2 小时,交易进程会被吊在退出阶段。→ 等待时间封顶 `MAX_DRAIN_TIMEOUT`(300 秒),
    并在关闭前把 FATAL 提到队首 —— 宁可丢一部分非 FATAL 告警,也不能让退出流程拖住。
  - **[Low] 平仓价的注释把买一/卖一写反了**:vn.py 的 `bid_price` 是**买一**、`ask_price` 是**卖一**。
    行为一直是对的(卖出挂买一、买入挂卖一),但注释与变更日志写成"平多用卖一"会误导后续维护者 → 改正术语。
- **第七轮 codex 审查(2026-11-10)追加修复** —— 2 个 High / 1 个 Medium / 2 个 Low:
  - **[High] 队列满时 `close()` 停不住后台线程**:停止哨兵是**追加**到队列末尾的,
    队列满(`queue.Full`)时根本放不进去,而这个异常被吞掉 → worker 永远收不到停止信号,
    `close()` 只能干等到排空预算耗尽(最坏 200 条 × 单条最坏耗时)。
    → 改用独立的 `_closed` 标志:后台线程取队列超时后检查该标志,排空即退出;关闭后再 `send()` 一律拒绝
    (消息只会躺在队列里没人发,不能让调用方以为收下了)。
  - **[High] 净持仓账户上,演练会把自己的仓位留在账户里**:CTP 的净持仓账户回报的是 `Direction.NET`,
    净额算出来是 0 → `_close_position()` 认为"没有仓位要平",**不发平仓单**,
    留仓手数还被误报成 0 手。演练其实知道自己开了 1 手多仓,却没记下来。
    → 演练记录**自己开出的方向与手数**(`owned_volume` / `owned_direction`),净额看不出来时按它平
    (开多仓就用卖出平);留仓手数改用**总手数**统计,不再按净额报 0。
  - **[Medium] 平仓单没成交时没有撤单**:平仓单还挂在交易所上,过一会儿成交就会留下一笔"晚到的"持仓,
    账户状态与演练结论不一致 → 平仓失败路径补撤单。
  - **[Medium] 通知器关闭抛异常会打断 shutdown**:只捕获 `TypeError`,其它异常会沿着 `finally` 冒出去,
    把退出码与后续清理一起打断 → 兜底捕获并记日志(写日志本身也不再抛)。
  - **[Low] 数字键等于密钥时仍会泄漏**:`{12345: "值"}` 原样保留数字键,
    而 `json.dumps` 会把它变成字符串 `"12345"` 发出去 → 非字符串键按 JSON 形态先脱敏,命中就换成脱敏后的字符串。
- **飞书机器人"失败也返回 HTTP 200",告警会静默丢失**:`notify/webhook.py` 只看 HTTP 状态码,把
  `{"code":19001,"msg":"invalid token"}` 这种"根本没送达"算成发送成功(计入 `sent`)。运行手册的验收标准恰恰是
  "告警全部到达手机",这个缺陷会让验收结论失真。现新增 `_body_error()` 解析响应体,业务码(`code` / 旧版 `StatusCode`)非 0
  一律算失败并走重试,`last_error` 脱敏后保留;业务码可能是字符串 `"0"`,比较时用 `int()` 安全转换,不会把成功误判成失败。
  **响应体不是 JSON(代理/网关回 HTML 2xx)、不是 JSON 对象、或没有业务码 → 同样判为失败**:
  真实的飞书一定回 JSON 并给业务码,拿不到就说明消息没被接收,按成功处理就是静默丢告警。
- **webhook 自检的等待时间短于最坏重试耗时**:慢一点的对端会被误判成"没送达"。等待时间改按
  "重试次数 × 单次超时 + 全部退避"计算(新增 `flush_timeout` 属性)。
- **webhook 计数器在多线程下丢数**:`sent`/`failed`/`dropped` 由发送线程与后台线程同时写,
  `+=` 不是原子操作 → 用独立的 `_counter_lock` 保护。
- **第四轮 codex 审查(2026-11-10)追加修复** —— 2 个 High / 4 个 Medium / 5 个 Low:
  - **[High] 演练的撤单与成交竞态会留下仓位且没有告警**:`scripts/drill.py` 的 `latency` 场景在
    `wait_for_trade()` 超时后直接撤单返回。CTP 的撤单是**异步**的,撤单请求发出前后订单仍可能成交,
    此时仓位留在账户上,却走不到"留仓 FATAL"分支。→ 撤单后用 `wait_flat(close_timeout, baseline)`
    确认净持仓真的回到 baseline;仍有残留(撞车)就平掉它、发 FATAL 告警、场景判失败。
  - **[High] 演练按账户净持仓平仓,会平掉演练之前就有的仓位**:`_close_position()` 用的是**账户级**净持仓,
    目标账户上本来就有同品种持仓的话,演练会连别人的仓一起平掉。→ 进场前记 `baseline = net_position()`,
    平仓量 = **当前净持仓 − baseline**,方向按 delta 正负取反;`latency` 进场前净持仓 ≠ 0 时**直接拒绝演练**
    (分不清哪一手是演练开的,强行平仓就是误伤)。
  - **[Medium] 告警"发出"与"送达"混为一谈**:`RecordingNotifier.send()` 无条件把告警记成已记录,
    底层 `send()` 返回 False(去重/级别过滤/队列满/渠道未配置)时手机端其实没响。→ 每条告警带 `delivered` 标记,
    新增 `delivery_summary()` 区分「发出/底层接受/未送达」,并把**渠道没配**与**送达失败**分开报。
  - **[Medium] 进程静默死亡会让"连续无异常天数"虚高**:`longest_clean_streak()` 只遍历日志里有的天,
    静默死亡那天**根本没有日志文件** → 被悄悄跳过 → 连续段被拉长。→ 按日历逐天走,
    **工作日缺日志就打断连续段**;周末不是交易日,缺日志不打断。新增 `missing_weekdays()` 把缺口列进验收清单。
  - **[Medium] `--help` 在 cp1252 控制台上直接崩**:`_setup_stdout()` 原先在 `parse_args()` 之后调用,
    而 argparse 的用法说明里有中文 → `UnicodeEncodeError`,连用法说明都看不到。→ 提到 `main()` 最前面。
  - **[Medium] `close()` 的等待时间只够一条消息**:`flush_timeout` 是**单条**上限,队列里排着 N 条时
    `close()` 会提前返回,最后几条随进程退出丢掉(`sent` 偏小 → 慢端被误判成"没送达")。→
    拆成 `flush_timeout`(单条)与 `drain_timeout`(单条 × 队列剩余条数),`close()` 的参数改为**总预算**。
  - **[Low] `extra` 的脱敏在错误的层次上做**:先序列化成 JSON 字符串再 `str.replace` ——
    序列化时引号/换行已被转义,`replace` 找不到原样的密钥 → 密钥留在 `extra` 里发出去;
    密钥本身含 JSON 语法字符时,替换还会把整条 JSON 弄坏。→ 新增 `redact_obj()` 在**结构上**逐个字符串脱敏。
  - **[Low] 飞书业务码为 `None` 时被当成成功**:`bool(None)` 是 False,原来的写法把"无法确认"判成"已送达"。→
    业务码解析改为 fail-closed:`None` / 空串 / 非数字一律判失败。
  - **[Low] `EVENT_MARKERS` 漏了熔断类措辞**:守卫与运行时的熔断告警(当日熔断、累计死线触发等)不在表里,
    熔断日会被当成"干净的一天"。→ 补齐措辞;「紧急平仓」用**带级别前缀**的 `"[FATAL] 紧急平仓:"`,
    既不误伤正常的 "[INFO] 紧急平仓完成",也不与 "紧急平仓失败" 重复计数。
  - **[Low] `render_table` 的 `heartbeat_day` 形参遮蔽同名模块函数**;`print(text[:0], end="")` 是死代码。→ 均已清理。
  - **[Low] `risky_scenarios()` 依赖调用方先归一化地址**:调用方忘了归一化,白名单判断就失效
    (同一个地址的两种写法会得到不同结论)→ 地址在函数内部自己归一化。
- **演练的"本次场景"判定有两个方向的错**(2026-11-10):
  - *假阳性*:断线/停更这类告警可能在场景动作之前就发过,不设下界会把旧告警当成本次场景的结果。
    `RecordingNotifier` 新增 `mark()`(记下当前告警条数)与 `find/wait_for(..., since)`。
  - *假阴性*:熔断场景原来是**先注入权益、再打标记**,而守卫的定时器线程随时可能在注入的瞬间就发出告警 ——
    那条告警落在标记之前,被 `since` 过滤掉 → 场景明明触发了熔断却报"没有触发"。已改为**先打标记再注入**。
- **演练开出去的单没成交就撤单**:否则那张单一直挂着,演练结束后才成交,仓位留到下一次运行里(演练污染实盘状态)。
- **演练的平仓单被交易所拒单**:上期所/能源中心要用 `Offset.CLOSETODAY`(演练开的是当日仓),
  用普通平仓会被拒 → 留仓 → 演练"通过"却把仓位留在模拟账户里。现按交易所选 offset,
  并且**平不掉就判失败并发 FATAL 告警**。
- **演练的"硬锁定已落盘"只是文档里的说法**:现在会读风控状态文件确认 `hard_locked` 已写盘,不落盘则场景判失败。
- **演练的时段窗口与运行时不一致**:演练原来硬编码交易时段,而运行时的看门狗用的是策略的 `ENTRY_SESSIONS`。
  两处不一致 → 演练说"在交易时段"而运行时说"不在",看门狗不检查,场景必然假失败。现直接复用同一份。
- **逃生舱只有一道**:地址不在 SimNow 白名单时,`--allow-any-address` 只是"我确认过"的一次声明。
  而 `latency`/`daily-loss`/`hard-lock` **会真的发委托**,后果是真金白银 → 在非白名单地址上跑这类场景
  必须再加 `--confirm-any-address-trades`;用了逃生舱的警告会写进结果文件当审计记录。
- **SimNow 地址白名单按编号规律外推**:`40002`/`40003` 是按第一套的编号规律外推出来的,官网第二套**只公布了 `40001`**,
  没有官方背书 → 已从白名单移除。另外地址先经 `normalize_address()` 归一化(去 `tcp://` 前缀、空格、路径),
  否则"长得不一样"会被误判成非 SimNow,反而逼人用逃生舱。
- **每日证据表的心跳日期算错**:`heartbeat_day()` 用 `datetime.fromtimestamp`(本机时区,宿主机 UTC-8),
  而容器跑的是 Asia/Shanghai,差 16 小时 → 容器 11-10 08:00 的心跳被算成 11-09,挂到错误的交易日那一行。
  现显式用 `ZoneInfo("Asia/Shanghai")` 换算。
- **心跳可能整列从证据表里消失**:心跳文件不存在、或它最后刷新的那天没有日志时,心跳完全不出现 ——
  而"进程静默死亡"这条证据恰恰靠这一列,验收项"没有进程静默死亡"就看它。现改为挂到**最后一行**。
- **日志时间戳带毫秒,按秒解析全部失败**:真实日志行首是 `2026-11-10 15:36:11.123 | INFO | ...`,
  按 `%Y-%m-%d %H:%M:%S` 解析会抛 `ValueError` → 整天的起止时间全丢。现先砍掉小数部分再解析。
- **"总共干净了几天"不能顶替"连续几天"**:验收要求连续 ≥ 5 个交易日,中间夹一个有异常的日子连续性就断了。
  新增 `longest_clean_streak()` 算最长连续段。
- **空的证据表不是证据**:一个 `vt_*.log` 都没有时静默生成空表并退出 0,会让人误以为"当天没问题"。
  现改为警告 + 退出码 1。
- **Windows 控制台编码会让脚本"假死"**:默认 GBK/cp1252 打印中文与 ✅ 会抛 `UnicodeEncodeError`,
  文件已写好但进程带异常退出(码 1)。新增 `_setup_stdout()` 统一按 utf-8 + errors="replace" 重配 stdout/stderr。
- **CTP 的拒单日志是 INFO 级别,原来永远收不到**(2026-11-10 SimNow 实测):`vnpy_ctp` 的 `write_error()` 实际调用
  `write_log()`,所以"交易委托失败,代码：21,信息：…"以 INFO 级别写出。`trading/guard_rule.py` 只认 `log.level >= ERROR`,
  导致废单分类永远拿不到原因(一律按硬拒绝),紧急平仓单的有限重试形同虚设。
  现按**精确措辞** `REJECT_LOG_MARKERS = ("交易委托失败", "交易撤单失败")` 识别,`trading/runtime.py` 的网关错误告警同步修复
  (运行时告警另用宽措辞 `GATEWAY_ERROR_MARKERS`,只给人看、分类错误没有后果);
  `tests/fakes.py` 的假网关也改成 INFO 级别,免得掩盖这个缺陷。措辞收窄的理由:宽泛的"失败"会把
  "行情服务器登录失败""行情订阅失败"也收进来,真废单日志迟到时就拿这些无关文本去分类,把可重试的软拒绝误判成硬拒绝。
- **废单关键词冲突**:`该交易席位未连接到交易所`(SimNow 非交易时段的真实拒单文本)含软关键词"连接",
  被误判成可重试的软拒绝;已显式加入硬关键词表。
- `scripts/collect_reject_texts.py`:① 竞态——CTP 先推订单回报、后写错误日志,脚本拿到拒单回报就翻日志,
  必然抓不到(2026-11-10 首次采集白跑一趟);已加 `wait_error` 等错误日志到达。② `close()` 会 join CTP 原生线程,
  4097 断连风暴下不返回,脚本"跑完不退出";改为限时关闭 + `os._exit` 兜底。
  ③ 交易时段内跑采集时,"非交易时段开仓""超大手数"两个场景用的是合法价格,**可能成交留下持仓**;
  现按 `in_trading_session()` 判定,时段内改用越界价并跳过时段场景。④ 价格未按 `price_tick` 对齐,
  会被 CTP 按"价格非法"拒单,测到的不是想测的场景;已统一 `round_to`。
- **第二轮 codex 审查追加修复**(2026-11-10):
  - 采集脚本只靠 `--confirm-simnow` 布尔开关挡不住"配置写错连上真券商"→ 新增 SimNow 官方地址白名单,
    不在表内直接拒绝连接(`--allow-any-address` 为强制逃生口)。
  - 场景之间的错误会串味(上一个场景的旧错误离本次发单更近)→ `Collector.error_index()` + `since` 参数。
  - **"无持仓时平仓"场景可能真的平掉账户上的持仓** → 发单前 `symbol_has_position()` 查持仓,
    账户上有任何持仓都中止采集(退出码 5);查询必须等满整个窗口(默认 15 秒),
    否则 CTP 异步到达的持仓回报会被漏掉、误判成空仓。交易连接断开同样中止。
  - 早退路径不关引擎 → `main()` 用 `try/finally` 统一走限时 `close_engine()`,采集主体拆到 `_collect()`。
  - 事件线程写、主线程读列表 → `Collector` 加锁,读取走快照。
  - 多网关时错误张冠李戴 → `_errors` 记来源网关,`_error_text_near(ts, gateway_name)` 按网关过滤,
    并优先取废单回报**之后**到达的日志。
- **第三轮 codex 审查追加修复**(2026-11-10):
  - **交易所级废单的原因拿不到**:`REJECT_LOG_MARKERS` 只认"交易委托失败""交易撤单失败",
    但 CTP 的交易所拒单走 `onRtnOrder`(`vnpy_ctp/gateway/ctp_gateway.py:673-684` 把状态映射成 REJECTED),
    原因在 StatusMsg 里、日志措辞是"委托 {委托号} 状态更新，{原因}"(同文件 713-719),
    **不经过** `onRspOrderInsert`。漏掉它 → 交易所拒单原因永远是"未知"→ 一律按硬拒绝 →
    平仓单被提前升级为人工处理、丧失重试机会。现已加入 `"状态更新"` 措辞。
  - 发单前查持仓的等待窗口可能不够 → 查询改为**等满整个窗口且查两轮**(默认 15 秒 + 7.5 秒):
    CTP 的持仓回报是异步的,而且对空账户**不推送任何持仓事件**,
    "没收到回报"既可能是真空仓也可能是回报没到。
  - `Collector` 的 `orders/ticks/contracts` 字典未加锁(与 `errors` 的保护方式不一致)→ 统一加锁,
    读取改走 `contract_of/tick_of/order_of` 快照方法。
  - 交易时段窗口未覆盖 CFFEX 的 13:00 下午盘 → 下午盘起点由 13:30 提前到 13:00(保守取并集)。
  - `--allow-any-address` 的帮助文本与警告改为明确提示"可能连上真实资金账户"。
- **SimNow 第二套对新注册账号不可用**(2026-11-10 实测):连 `182.254.243.31:40001` 时行情登录成功、
  交易登录回 `代码：3,信息：CTP:不合法的登录`;新账号要到第三个交易日才能用第二套。
  采集脚本现在检测到登录失败会**立刻退出(退出码 6)**并说明原因,不再白等 180 秒超时。
- **Docker 容器连不上 SimNow 的根因**:`vnpy_ctp` 的 `toUtf()` 硬编码 `std::locale("zh_CN.GB18030")`,Debian slim 镜像默认无此 locale,CTP 回调线程抛异常导致进程 abort(无 CTP 错误日志)。Dockerfile 已加 `locales` + `localedef -i zh_CN -f GB18030 zh_CN.GB18030`。
- **Docker 容器内完整连上 SimNow**(2026-11-10 实测):连接→授权→登录→结算确认→合约表→策略初始化→订阅→容器 `healthy`→`docker compose stop` 优雅退出(码 0)。
- `scripts/backtest_lib.py`:回测异常检测改用精确哨兵(`Traceback` / `触发异常`),避免普通日志含"异常"二字被误报;`run_engine_bars` 补充统计口径说明(按自然日分组,与日线基准的夏普不可直接比较)。
- `scripts/run_gui.py`:合约事件去重,CTP 重连重发合约表时不会重复订阅;自动订阅的合约缺交易所后缀时给出提示而不是静默不订阅。
- `scripts/optimize_15m.py`:`--data` 换文件后 K 线缓存按路径取键,不会继续用上一份数据。
- `trading/runtime.py`:网关连接只通知一次(行情/交易各报一次"登录成功"不再重复告警)。
- 修正 `DB_TZ` 与系统时区不一致导致 vn.py 读回 K 线整体偏移 16 小时的问题(根因:`vnpy_sqlite` 读取时按系统本地时区解释无时区时间)。
- 数据源 OHLC 自相矛盾的行改为修复而非丢弃(PP 2020-08-06、L 2007-07-31 各 1 行)。
- CSV 改为先写临时文件再原子替换,避免写入中断损坏已积累的分钟历史。
- 邻域平滑选参的偏差:边缘格子邻居更少导致得分偏高、无效邻居被忽略使孤立尖峰胜出、负分区域里按 0 计无效邻居偏乐观;
  现在只有完整 3×3×3 邻域的格子可选,无效邻居按悲观下限计。
- 测试不再依赖 vn.py 枚举的本地化字符串(原先在中文环境下 4 个测试会失败)。
- 策略委托价按 `pricetick` 取整(止损取更紧的一侧),避免实盘因价格不是最小变动价位的整数倍被拒单。
- `backtest_lib` 的异常检测改为精确哨兵,不再因策略日志里出现"异常"二字而误报。

- 复权连续合约经 codex 独立审查与外部模型验证后修复:换月改用前一交易日持仓量(消除前视)、主力停牌不再触发虚假换月、
  无共同交易日时价差近似并告警(原先静默为 0)、取数缓存不再把"暂时取不到"永久缓存成"不存在"。

- 通知脱敏:`requests` 异常文本只含 URL 路径,Telegram token 曾因此可能进入 `last_error`;现在 URL 的路径、查询串和各路径段都参与脱敏。
- 紧急平仓不再依赖陈旧的 OMS 持仓:CTP 持仓回报滞后会导致成交后重复发平仓单;现在按成交量对账,持仓没清零时主动查询并等待,绝不重发。
- 当日熔断的平仓失败后,随后的硬锁定可以重新开一轮平仓(原先会被卡死的状态机吞掉)。

- 优雅停机会误触发紧急平仓:`stop_all_strategies` 把 `trading` 置假,看门狗把它当成"策略崩溃"。现在停机先关看门狗,收到停止请求后看门狗也不再触发。
- 启动安全:启动时撤销遗留委托后等待其终结(否则拒绝启动)并重新确认持仓;持仓回报连续两次一致才对账;状态目录(含风控状态、心跳、CTA 策略数据)不可写时拒绝启动,
  避免硬锁定写不进磁盘、重启后被"洗掉"。CtaEngine 恢复变量前先删除持久化的 `trading/inited`,消除可交易时间窗。
- 新增运行中持仓漂移看门狗:账户持仓与策略持仓不一致持续超过容忍时间 → FATAL 告警并紧急平仓。
- 紧急平仓:没发过平仓单就看到空持仓时先主动查询并等待(启动早期持仓回报可能未到);重开一轮前先撤掉旧的活动平仓单;完成时撤掉残留平仓单;
  硬锁定事件 flat=False 与 flat=True 各发一次(平仓先失败后成功仍能让进程退出)。
- 启动阶段收到停止信号能及时响应并以退出码 0 退出;`wait()` 改为分段等待(Windows 下 Ctrl+C 可中断)。
- 超时类参数加下限(持仓刷新 ≥3s、平仓回报 ≥3s、持仓稳定等待 ≥2s),配成 0 不会让检查形同虚设。
- Webhook 队列满时优先挤掉最旧的非 FATAL 消息,且不会挤掉停止哨兵。

- 风控状态与账户 ID 绑定:连接到不同账户时重置权益基准并告警(SimNow 2000 万的"最高点"不会把 1 万元账户误判为大幅回撤);硬锁定不因换账户而解除。
- `scripts/run_gui.py`:VeighNa Trader 图形界面入口(挂载账户风控,不含对账与看门狗,合约表到位后自动订阅 LIVE_VT_SYMBOL),用于人工观察与试验。
- 启动各阶段耗时(登录/结算确认/合约表)写入日志,启动超时告警里带上已到达的里程碑,便于判断慢在哪一步。
- `connect_timeout` 默认 60 → 300 秒(网关登录后需下载全市场合约表,SimNow 上实测 20 秒~3 分钟以上),超时告警写明原因。
- 首次在真实 SimNow(第一套)上跑通启动管道:连接/授权/登录/结算确认/策略初始化/订阅/优雅停止。

### Security（安全）
- 账号、密码、AuthCode 仅放 `.env`(已 `.gitignore`),仓库只提供 `config/.env.example` 模板。
- **演练脚本与实盘状态彻底隔离**:`scripts/drill.py` 用独立的 `.vntrader_drill/` 目录(策略数据、日志、心跳)与独立的
  `account_guard.json`,演练触发的熔断/硬锁定**不会污染实盘的 `data/state/account_guard.json`**。
- **演练只能在模拟盘上跑**:复用 `scripts/simnow_guard.py` 的官方前置机地址白名单,地址不在表内默认拒绝连接
  (CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,只有地址能区分)。
- **演练的熔断场景不碰真实账户权益**:用"注入权益来源"的方式模拟亏损,只改本进程内的取值,
  守卫判定、撤单、告警、状态落盘照常跑,场景结束即换回真实查询。

<!-- 比对链接 -->
[Unreleased]: https://github.com/laiyinyizao007/invest/compare/0981db3...HEAD

<!-- 归类指引（Conventional Commits → Changelog 分区）
feat: Added（新增）
fix: Fixed（修复）
perf / refactor / style / chore / docs / test: Changed（变更）或按需归类
deprecate: Deprecated（弃用）
remove / breaking: Removed（移除）并标注 BREAKING
security: Security（安全）
-->
