# 变更日志（Changelog）
所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。
与 [`PROJECTWIKI.md`](PROJECTWIKI.md) 互相链接;相关 Issue:[#1](https://github.com/laiyinyizao007/invest/issues/1)。

## [Unreleased]
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
- **进化引擎三视图(读取侧 + 前端文档)**(2026-11-13):新增 `dashboard/evolution.py` 与 `docs/web/08-evolution-views.md`。契约新增 6 个阶段④只读接口(战役列表/进化曲线/变异日志/参数热力图/候选源码/候选差异)与 11 个数据模型,12 份示例数据,种子重新生成。读取侧校验台账行(坏行即损坏,不做『尽量显示』)、按代聚合精英/均值/P25/P75 并由服务端判定停滞与过拟合提示、热力图三维切二维(`null` 单元保持为空、不画成 0)、源码按哈希找、统一 diff 由服务端算。**面板只读:不注入变异、不批准、不回滚**;进化回路由毕昇工作流承载。路由按仓库约定由配置开关 `evolution.enabled` 控制(阶段④上线才注册,`capabilities.evolution` 告诉前端),台账损坏返回 503 与 404 可区分。**数据还没有**——引擎侧阶段 3 还没开工,线上取不到是如实状态,前端用示例数据开发。
