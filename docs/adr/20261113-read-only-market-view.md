# ADR: 候选在 entry_signal 里只拿行情容器的只读副本视图(收能力)

- 状态:已采纳(2026-11-13)。**这是对模板与既有宪法决策的修订**(G6:变更既有 ADR 须用户确认)——
  用户在 PR #17 合并时选择了"接受现状合并",并把"收能力"列为遗留项;随后以 `/goal 剩下这些都要做完` 授权执行。
- 日期:2026-11-13
- 跟踪:Issue [#23](https://github.com/laiyinyizao007/invest/issues/23)
- 关联:`20261113-evolution-engine`(第 12–14 轮补记、「趋势与判断」)、`20261111-risk-floor-immutability`

## 背景
宪法 G8 复核的 codex 分数:第 12 轮 38.5、第 13 轮 38.0、第 14 轮 38.5(要求 ≥45)。
每一轮修的都是**写法**——numpy 视图、dunder 写入口、别名、`[:, None]`、改 `count`/`size`、预热期塞假 K 线、
跨 K 线的"改 → 用 → 改回"——而下一轮 codex 总能找到下一个。这与第 7/9 轮"黑名单封不住能力"的结论一致,
并在第 14 轮补记里被明确写成:**继续逐条堵写法,没有理由认为能收敛到 ≥45**。

这些攻击有一个共同前提:候选手里拿着**真实的行情容器**(`ArrayManager` / `BarGenerator`)。
所以问题不在"写法认不全",而在"不该让它拿到"。

## 决策
模板 `TrendAtrStrategy` 在调用 `self.entry_signal()` 的那一刻(`_call_entry_signal`):

1. 把 `self.am` 换成**只读视图**(`risk/market_view.py::read_only_view`);
2. 把 `self.bg`(分钟线合成器,与 `am` 同一类容器)换成**禁止访问**的哨兵 `NO_ACCESS`;
3. 调用结束——**含异常**——立刻还原(`try/finally`)。

候选只覆盖 `entry_signal`(其余成员受保护),所以这是它碰得到行情容器的**唯一**时机;
模板里只有 `_call_entry_signal` 调 `entry_signal`(有一条测试用 AST 守着这一点)。

### 视图的性质
- **没有任何变更入口**:`update_bar` 不存在;
- **所有数组与指标结果都是拷贝**(`close`/`high`/…、`*_array`、`sma`/`atr`/`macd`/`boll`……,元组/列表里的数组逐个拷贝);
- **视图本身不可变**:`__slots__ = ()`(没有实例字典可藏东西),`setattr`/`delattr` 一律拒绝;
- **真实容器的引用不在属性里**,只在方法的闭包里。

### 为什么是"拷贝",而不是"只读视图"(`flags.writeable = False`)
这是实现时踩到、并写进测试的一点:**numpy 允许对"基础数组可写"的视图重新把可写打开**——
`v = a.view(); v.flags.writeable = False; v.flags.writeable = True` 成功,随后写 `v` 就改到了 `a`。
所以"返回只读视图"挡不住一个会重新打开可写标志的候选;返回拷贝则没有这个问题——写了也只是改自己的副本。
代价是每次访问数组都拷贝一份(窗口约 70 根 × 7 个数组):实测 6000 根 15 分钟线上耗时无可见差异(噪声以内)。

### 不在本次范围
- **`self.guard` / `self.trail` / `self.cta_engine` 没有换成视图**。它们受静态层"引用即拦"与运行期第三、四层保护;
  而 `write_log`、`get_size` 等候选可以合法调用的模板方法要用到 `cta_engine`,在 `entry_signal` 期间隐藏它会误伤诚实候选。
  这一层专门解决 rounds 12–14 反复出现的那一类:**行情容器**。
- **不放宽任何已有防线**:静态层的容器规则、运行期第五层(逐根核对)、守卫全部保留,是这一层之外的纵深防御。

## 对既有决策的修订
| 项 | 修订 |
|---|---|
| 模板 `TrendAtrStrategy` | 新增 `_call_entry_signal`、`_am_view`;`_try_enter` 改走 `_call_entry_signal` |
| 受保护成员 `BUILTIN_PROTECTED_NAMES` | 加 `_call_entry_signal`、`_am_view`(只增不减,守卫登记) |
| 受保护执行点 `BUILTIN_PROTECTED_ENTRY_POINTS` | 加 `_call_entry_signal`:候选**引用**它就拦(覆盖它就能绕开视图) |
| 第 14 轮补记里的"残余 1:运行期三层与静态层并不完全独立" | **缓解**:候选在 `entry_signal` 里不再拿得到真实容器,第五层的强核对不再依赖静态层去拦 `am.count` 的写入——但这条残余**没有被消除**(见下) |

## 自查发现:栈帧内省——先于本修订、在已合并的宪法里就存在的洞
给外部模型做 G8 第一轮之前,我先用真实回测引擎(不经静态筛查)攻击自己的设计,发现**视图的承诺只在栈帧走不通时才成立**:

- **沿栈帧能到可信代码的局部变量**。`e.__traceback__.tb_frame.f_back.f_locals`(或 `gen.gi_frame.f_back`)一路走上去,
  先是 `_call_entry_signal` 的局部变量(最初写成 `real_am, real_bg = self.am, self.bg`——等于把真容器放在候选一步就走得到的地方),
  再往上是回测引擎(`history_data`、`cta_engine`)、`run_evaluation` 的局部变量(含**整份**行情,包括验证窗口)。
  实测:候选取回了真实 `ArrayManager` 并改写了它;另一条走到了 `BacktestingEngine` 并改写 `history_data`。
  两条都被第五层抓到(纵深防御起了作用),但靠的是"首根 K 线时存了一份缓冲快照"这个碰巧的设计。
- **这与 `self.cta_engine`(被"引用即拦")是同一个能力,却没有被同样对待**。原来的禁用清单里没有 `__traceback__`、`tb_frame`、`f_back`、
  `f_locals`、`gi_frame` 这些名字;**更省事的路**是受限导入器放行了 `traceback`:`traceback.walk_stack(None)` 不需要写任何被禁的属性名。
  第 7 轮的注释(`research_sandbox.py`)写过 `e.__traceback__.tb_frame.f_builtins.setattr` 这条路径,当时的补法是把 `setattr` 从候选的内建里拿掉,
  **没有拿掉"沿栈帧走到可信代码"这个能力本身**。
- 受限导入器同时放行了 `linecache`(能读任意文件)、`code`、`pdb`、`dis`、`runpy`、`faulthandler`、`tracemalloc`。

### 这次做了什么
1. **静态层**:`research_sandbox.FORBIDDEN_ATTRS` 加栈帧/回溯相关的 19 个名字(`__traceback__`、`tb_frame`、`tb_next`、`f_back`、`f_locals`、`f_globals`、
   `f_builtins`、`f_code`、`f_trace`、`gi_*`、`cr_*`、`ag_*`、`with_traceback`),它们作为属性名与字符串常量都拒;
   `FORBIDDEN_MODULES` 加 `traceback`、`linecache`、`code`、`codeop`、`pdb`、`bdb`、`dis`、`faulthandler`、`tracemalloc`、`runpy`、`pkgutil`、`zipimport`;
   宪法层 `BUILTIN_FORBIDDEN_DUNDERS` 加 `__traceback__`。受限导入器在运行期同样拒绝这些模块。
2. **运行期纵深防御**:`_call_entry_signal` 不再把真容器放在局部变量里(存进 `market_view` 的模块级弱引用表),
   所以即使有人绕过静态层真的沿栈帧往上走,调用链上没有任何一个栈帧的局部变量里放着真实的 `ArrayManager`/`BarGenerator`(有测试逐帧检查)。
3. 变异验证:真容器放回局部变量(2 条变红)、放行 `traceback`(2 条)、不再禁 `f_back`/`f_locals`(4 条)。

### 如实记录:这不是封死
- **运行期没有办法完全封死栈帧**:进程内的 Python 里,只要绕过静态层,沿栈帧仍然走得到引擎(实测攻击 B 在去掉静态筛查之后依旧能走到 `BacktestingEngine`)。
  所以**静态层是这道口子的门**,与 `self.cta_engine` 被"引用即拦"是同一个层次的保护——本修订把栈帧内省**补进了同一道门**,而不是造了一堵墙。
- **没有处理的同类缺口**:受限导入器仍放行 `gzip`、`zipfile`、`tarfile`、`bz2`、`lzma`、`shelve`、`dbm`、`sqlite3`、`fileinput`、`mmap` 这类能按路径读写文件的模块,
  以及 `numpy` 自身的 `fromfile`/`load`(`load(allow_pickle=True)` 能执行 pickle)。模块黑名单永远是黑名单;
  真正的边界仍是**只运行已被人读过并批准的代码**与**容器隔离**(与 `research_sandbox.py` 文件头一致)。这一条要让用户知道,而不是假装没有。

## 验证
- 单元:每个数组都是拷贝(含重新打开可写标志、`[:, None]`、`reshape`/`view`/`T`、`__setitem__`)、没有 `update_bar`、不可变、
  真实容器不在任何属性里、`NO_ACCESS` 拒绝一切访问;
- **端到端,用真实回测引擎、不经过静态筛查**:第 12–14 轮 codex 的攻击——写回类、ATR 塌陷(跨 K 线改写再还原)、
  `update_bar`(喂一根**合法的**假 K 线)、`count -= 1`、`size = 5`、`del`、挂新属性、`bg.update_bar`——
  要么抛 `ReadOnlyViewError`(回测报"策略抛出异常"),要么写到副本上、**结果与诚实模板逐笔一致**;
- **对诚实候选完全透明**:有真实成交的行情上,有视图与无视图逐笔一致、ATR 逐位一致;既有的全部回测/策略测试不变;
- **变异验证**:让视图返回真数组(19 条用例变红)、让模板跳过换入(10 条)、放开 `update_bar`(2 条)、让视图可写(7 条)——都被抓到。
  其中放开 `update_bar` 最初只有 1 条变红:端到端那条喂的是 `None`,真容器收到 `None` 也会抛错,所以变异后它照样"通过";
  改成喂一根**合法的**假 K 线之后才有判别力;
- 第五层仍然有效:用一个覆盖了换入点的类模拟"视图被绕过",`count -= 1` 依旧被逐根核对抓到(原来那条用例改成了这个形态)。

## 如实记录:残余与没验证的
1. **这仍是纵深防御,不是沙盒**。视图把真实容器放在闭包里;要取出它需要 `__closure__` / `__globals__` / `__code__`,或者沿栈帧走(`f_back`/`f_locals`)——
   这些是受禁名字,静态层现在都拦(栈帧这一批是本修订自查时**新补**的,见上节)。但静态层是黑名单,所以**候选若能绕过静态层,
   这一层的"拿不到"就不成立**。收能力把"必须认全所有写法"降成了"必须拦住取闭包这一个口子",口子少得多,但不是零。
2. **没有重新跑 G8 复核**。本 ADR 写成时 codex 额度/Gemini 配额的情况见 CHANGELOG;**这次修订没有得到新的外部分数**,
   所以不能声称"分数提高了"。可以说的只有:攻击面从"容器的写法"收窄成了"取闭包",且有端到端用例与变异验证守着。
3. `self.guard`/`self.trail`/`self.cta_engine` 仍靠静态层与运行期检测(见"不在本次范围")。
4. 视图只覆盖 `entry_signal` 这一个时机。模板自己的代码路径(`on_bar`、`_process_daily_bar` 等)仍直接用真实容器——
   它们是受保护成员,候选改不了;但它们若将来新增"调用候选代码"的入口,必须同样走视图(AST 用例守着 `entry_signal` 的唯一调用点)。
5. 候选仍可以通过 `entry_signal` 的返回值与自己的实例属性影响行为——那是它的正当用途,不是漏洞。
