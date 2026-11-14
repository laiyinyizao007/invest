"""不可变宪法:候选策略能改什么、不能改什么的机器可校验清单。

**这里的每一层都是纵深防御,不是安全边界**:AST 黑名单封不住 Python 的动态能力
(运行时构造的名字、字节码、导入期副作用都绕得开)。真正的边界是容器隔离
(`docker-compose.research.yml`:无网络、只读挂载、`cap_drop ALL`)与"运行前由人读过代码"。
本模块单独阅读时请把这句放在心里——它挡的是"明面上的越权写法"。

为什么需要:`risk-floor-immutability` ADR 说"风控底线永不优化",但此前只靠两条软约束保证——
候选继承基类、以及 `research_grid.FORBIDDEN_AXES` 不让风控参数进网格。缺口有三处:
1. 候选可以直接在类体里写 `risk_budget = 99999`,把底线改掉;
2. 调用方可以在请求参数里带 `risk_budget`,而 `backtest`/`gate` 原本不校验参数键名;
3. 候选可以重写 `on_trade`/`_try_enter` 等路径,绕过连亏熔断与预算检查,或直接下单。

设计(见 docs/adr/20261113-evolution-engine.md 阶段 2):
- 内置清单在代码里(BUILTIN_*),清单文件 `config/immutable_constitution.json` **只能追加**,不能删减内置项;
- 静态筛查 `screen_source`(看 AST):拒绝覆盖受保护成员、实例上改写受保护属性、改写受保护模块函数、
  **引用**下单接口或受禁 dunder(不只是调用)、引用框架的"按名 setter"、
  **引用**风控执行点(模板里做风控判定并下单的方法)与受保护的状态对象、
  写入受保护对象的**内部状态**(`self.guard.max_losses = 999` 这一整条属性路径)、
  受保护参数作为关键字参数或字典键出现。**新增**成员不在静态层管(模板成员名单在运行时才知道);
- 运行时 `check_class`(对照模板类的真实成员):候选必须继承模板;不得重写白名单以外的**模板成员**
  (含中间基类与类创建后打的补丁)。候选自己新增的成员(新指标、新辅助方法)允许;
- 运行时 `check_instance_state`(回测跑完后核对受保护对象的实例状态):熔断阈值、合约乘数、
  风险预算必须与回测设置一致——静态层看不见"运行途中才发生的改写";
- 运行时 `check_params`:拒绝调用方在设置字典里指定受保护参数(顶层与嵌套都查);
- 一律 fail-closed:清单文件缺失或非法即抛 ConstitutionError,不做"默认放行"。

清单文件的字段:
  allowed_overrides    候选可重写的模板成员白名单(只能追加)
  protected_names      不可触碰的成员(只能追加)
  protected_parameters 不可出现在设置字典与网格里的参数(只能追加)
  forbidden_order_calls 候选不得调用的下单/撤单接口(只能追加)
  protected_module_attrs 候选不得改写的模块级风控函数/类(只能追加)
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import re
import sys
import types
from pathlib import Path

CONSTITUTION_FILE = Path(__file__).resolve().parents[1] / "config" / "immutable_constitution.json"

# ---- 内置清单(清单文件只能追加,不能删减) ----
BUILTIN_TEMPLATE = "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy"

# 清单文件里 template 只能取这些内置模板之一:模板是"哪些成员受保护"的基准,
# 换成别的类(如 vnpy 的 CtaTemplate)会让继承约束形同虚设。要换模板必须改代码、走代码评审。
BUILTIN_ALLOWED_TEMPLATES = (
    "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy",
    "strategies.trend_atr_strategy:TrendAtrStrategy",
)

_TEMPLATE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")

# 风控底线与固定参数:不得被候选覆盖,不得进设置字典,不得进 param_grid
BUILTIN_PROTECTED_PARAMETERS = (
    "risk_budget",          # 单笔最大亏损预算(元)
    "max_daily_losses",     # 当日连亏熔断笔数
    "atr_window",           # ATR 窗口:固定,不参与优化
    "history_vt_symbol",    # 历史日线代码
)

# 受保护的类成员:候选不得在类体里重新赋值,也不得重写这些方法
BUILTIN_PROTECTED_NAMES = (
    # 风控状态与工具
    "guard", "trail", "am", "bg", "pending_dist", "pending_entry", "_stop_armed",
    "atr_value", "stop_price", "entry_price", "entry_dist", "prev_pos", "skipped_by_budget",
    "trail_side", "trail_extreme", "trail_stop", "is_live", "state_consistent",
    # 引擎本体:合约乘数、手续费、订单通道都在它身上。改它等于改掉风控计算的地基
    # (`self.cta_engine.size = 0.001` 会让止损距离算出来的风险金额接近 0,
    #  两道回撤门槛随之失效——第 7 轮 codex 实测)。
    "cta_engine",
    # 仓位规模(合约乘数):它决定"止损距离 × 乘数"这个**风险金额**。
    # 候选在**实例**上遮蔽它(`self.get_size = lambda: 0.001`)就能让预算判定恒真:
    # 真实乘数 10 被换成 0.001,300 元预算算成 0.003 元,任何止损距离都"在预算内"。
    # 类字典里看不到实例上的赋值(check_class 比不出来),所以这一层由名字兜住;
    # 运行时第三层再核一次"实例上有没有遮蔽模板方法"(见 _shadowed_template_callables)。
    "get_size",
    # 下单与持仓管理路径:重写即绕过预算检查与连亏熔断
    "__init__", "on_bar", "on_tick", "on_init", "on_start", "on_stop", "on_trade", "on_order",
    "on_stop_order", "on_account_status", "on_position", "on_cancel_order", "on_disconnect",
    "_process_daily_bar", "_manage_position", "_try_enter", "_call_entry_signal", "_am_view", "_send_entry", "_place_protective_stop",
    "_place_pending_entry", "_stop_order_price", "_ensure_protective_stop", "_complete_stale_daily_bar",
) + BUILTIN_PROTECTED_PARAMETERS

# 候选不得**引用**的风控执行点:模板里"做风控判定并下单"的那几个方法。
#
# 为什么"不得重定义"不够:候选不重写它,直接**调用**它,就把绕在它里面的
# 预算判定与熔断判定一起绕过去了。`within_budget(dist, self.get_size(), self.risk_budget)`
# 与 `self.guard.can_open(...)` 都在 `_try_enter` 体内,而 `_send_entry` 是它调用的下单机器:
#     def entry_signal(self):
#         signal = super().entry_signal()
#         if signal:
#             self._send_entry(signal, self.atr_value * 50.0, self.am.close[-1])   # 预算/熔断全绕开
#         return 0
# 第 7 轮 codex 实测:该写法三层校验全绿;真实 15 分钟玉米数据上,atr_mult=50 时
# 诚实模板成交 0 笔(157 次被预算拦下),候选成交 2 笔、单笔止损距离 508.7
# (≈17 倍 300 元预算)。**引用**即拦,不只拦调用——`f = self._send_entry` 之后
# `f(...)` 的调用点看不出是谁。
BUILTIN_PROTECTED_ENTRY_POINTS = (
    "_try_enter", "_send_entry", "_manage_position", "_place_protective_stop",
    "_place_pending_entry", "_process_daily_bar", "_ensure_protective_stop",
    "_stop_order_price", "_complete_stale_daily_bar",
    # 只读行情视图的换入点(收能力):候选碰得到行情容器的唯一时机。覆盖它或引用它,就能绕开视图。
    "_call_entry_signal",
)

# 受保护的**状态对象**:模板自己持有的、装着风控判定的那些对象。
# 候选连"读"都不许——只禁"写"会被别名绕过去:
#     g = self.guard          # 取别名
#     g.max_losses = 999      # 这已经不是"对 self.guard 的写入"了,按属性路径查也查不到
# 干脆连引用一起禁掉,别名这条路就不成立。
# (`am`/`bg` 不在此列:候选要读行情 `self.am.close[-1]`,那是它的正当用途;
#  它们只受"不得写入"约束,见 BUILTIN_PROTECTED_NAMES。)
BUILTIN_PROTECTED_STATE_OBJECTS = (
    "guard",      # 当日连亏熔断器
    "trail",      # 移动止损(只收紧不放松)
    "cta_engine", # 引擎:合约乘数、手续费、订单通道
)

# 受保护的**属性路径**:整条路径都不许引用(写或取别名)。
# 注:vnpy 的 ArrayManager 里**没有** `array` 这个属性(真实的是 `close_array`/`open_array`/…),
# 所以这一条本身几乎匹配不到东西;真正把行情数组护住的是下面的"容器不得取别名"规则
# (`arr = self.am.close` 之后 `arr[-1] = 999` 改的是**回测数据本身**)。
# 这条留着是因为历史守卫对内置清单只查"只增不减",删条目会被判为违规。
BUILTIN_PROTECTED_PATHS = (
    "am.array",
)

# 受保护的**容器**:装着可变对象、拿到别名就能原地改写的那些。
# `guard`/`trail`/`cta_engine` 连引用都不许(见 BUILTIN_PROTECTED_STATE_OBJECTS);
# `am` 必须能读(`self.am.close[-1]` 是候选存在的意义),但**不得把它里面的东西取出来另存**。
#
# 注意:`am`/`bg` 在这里只保护**属性路径**(`self.am.xxx`),不保护裸局部名——
# `am` 是 ArrayManager 的常见缩写,LLM 候选写 `am = self.am.sma(5)` 会被误伤(第 11 轮 codex 的 Low 1)。
BUILTIN_PROTECTED_CONTAINERS = (
    "am",
)

# 变更型方法规则的**适用范围**:哪些实例属性算"装着行情/风控数据的容器"。
# `am` 是 ArrayManager(装着 close/open/high/low 数组),`bg` 是 BarGenerator(装着日线/窗口线)。
# 两者都能被**调用**写入:`self.am.update_bar(fake_bar)`、`self.bg.update_bar(bar)`
# 会往回测里插一根假 K 线(第 11 轮 codex 实测:端到端四层防线全绿,
# 而 skipped_by_budget 由 0 变成 3928、成交数由 212 变成 2——回测结果被完全改写)。
BUILTIN_PROTECTED_CONTAINER_SCOPE = BUILTIN_PROTECTED_CONTAINERS + (
    "bg",
)

# numpy 的**自由函数形态**写入口:第一个实参就是被写的目标数组
# (`copyto(dst, src)`、`put(a, ind, v)`……)。变更型方法名单只覆盖 `接收者.方法()` 形态,
# 这一形态原来漏掉了(第 4 轮 codex 复核的 Low-2)。
# 名单里像 `append`/`sort` 这些**不是**自由函数:它们被写的是接收者,
# 第一个实参是读,按第一个实参判会误伤 `x.append(self.am.close)` 这种正当写法。
_MUTATING_FREE_FUNCTIONS = frozenset({"copyto", "put", "putmask", "place"})

# 这些自由函数里**被写的目标**的形参名(numpy 的惯例):`copyto(dst, src)`、
# `put(a, ind, v)`、`putmask(a, mask, v)`、`place(a, mask, v)`。
# 第 6 轮 codex 的 Medium-2:关键字形态(`np.copyto(dst=self.am.close, src=x)`)
# 原来只看位置实参,漏掉了。
_FREE_MUTATOR_TARGET_KEYWORDS = frozenset({"dst", "a", "arr", "array"})

# 受保护容器上的**变更型方法**:对这些方法**调用**就等于原地改写容器里的东西。
#
# 为什么必须单列一份名单:容器规则原来只挡"写"(`arr = self.am.close` 后 `arr[0] = 999`)
# 与"取别名",**挡不住"调用"**——`self.am.close.fill(0.0)`、`self.am.update_bar(fake_bar)`
# 既不是属性赋值也不是取别名,按语法位置筛查看不见。
# 名单只收**确实会改状态**的方法;读方法(`sma`/`atr`/`generate`/`size`)不在其中,
# 候选读行情的正当用途不受影响。
BUILTIN_PROTECTED_CONTAINER_MUTATORS = (
    # vnpy 的写入型接口:往容器里塞数据
    "update_bar", "update_tick",
    # list/dict/set 的写入型方法
    "append", "extend", "insert", "remove", "pop", "popitem", "clear",
    "update", "setdefault", "sort", "reverse", "discard",
    # numpy 数组的写入型方法(原地改写,对象身份不变)
    "fill", "itemset", "put", "resize", "copyto", "setfield", "setflags",
    "byteswap",
    # dunder 写入口(第 12 轮 codex 的 High 1 变体②):
    # `self.am.close.__setitem__(slice(None), self.am.close)` 与按名调用一样
    # 能原地改写行情数组,而 dunder 名字按名是拦不到的(只有 __setattr__ 之类在禁名单里)。
    "__setitem__", "__delitem__", "__iadd__", "__imul__", "__ior__",
)

# 受保护容器上的**视图型方法**:返回的是原数组的视图(与原数组共享内存),
# 拿到它再改,改的还是回测数据本身(第 12 轮 codex 的 High 1 变体①的同一类)。
#
# `flatten`/`copy` 不在此列:它们返回的是**副本**,拿到也改不到回测数据。
BUILTIN_PROTECTED_CONTAINER_VIEWS = (
    "view", "reshape", "ravel", "squeeze", "transpose",
    # 第 13 轮 codex 的 Critical 1:这两个同样返回视图,第 12 轮漏登记
    "swapaxes", "diagonal",
    # 第 4 轮 G8 复核的 F2:规则收窄到"写入"之后,视图型名字只剩下"当写入目标"这一种用法,
    # 名单就得跟着改成"视图型方法 + 视图型属性"。
    # `T` 是属性不是方法(`arr.T[0] = 9` 与 `arr.transpose()[0] = 9` 等价),此前没人管。
    "T",
)

# 候选不得调用的下单/撤单接口
BUILTIN_FORBIDDEN_ORDER_CALLS = (
    "buy", "sell", "short", "cover", "send_order", "send_stop_order", "send_local_stop_order",
    "cancel_order", "cancel_all", "cancel_all_orders",
)

# 候选不得改写的模块级风控函数/类(防止绕过预算与熔断)
#
# 第 9 轮 codex 实测的 Critical 1:`within_budget` 只是把算术**委托**给 `risk_per_trade`,
# 而 `risk_per_trade` 此前根本不在名单里——候选三行就能架空 300 元单笔预算:
#     import risk.risk_guard as rg
#     rg.risk_per_trade = lambda stop_dist, size, volume=1: 0.0
# 实测 skipped_by_budget 326 → 0、单笔止损 2901 元(预算的 9.7 倍),而四层防线全部放行。
# 同样被漏掉的还有熔断器的判定方法(`DailyLossGuard.can_open`)与移动止损的更新方法
# (`TrailingStop.update`):类名在名单里,方法名不在——所以判定必须**按整条属性路径**,
# 不能只看末段(末段是 `can_open`/`update` 这种看不出危险的名字)。
BUILTIN_PROTECTED_MODULE_ATTRS = (
    "within_budget", "stop_distance", "risk_per_trade", "ceil_to", "floor_to",
    "DailyLossGuard", "TrailingStop",
    "can_open", "on_trade_closed", "reset",
    # 交易日归属:熔断器按它分日("今天亏了几笔")。改掉它就能把昨天的亏损算到今天,
    # 当日连亏熔断随之失效(第 10 轮 codex 实测:四层防线全部放行)。
    "trading_day",
)

# 有些模块级成员的名字**太泛**(`reset`/`can_open`/`on_trade_closed`),
# 按名字一刀切会把普通写法(`self.indicator.reset = 1`)整片拒掉(第 10 轮 codex 的 Low 1)。
# 它们各有主人:只有路径里**同时出现主人**时才算命中。
BUILTIN_MODULE_ATTR_OWNERS = {
    "reset": "TrailingStop",
    "can_open": "DailyLossGuard",
    "on_trade_closed": "DailyLossGuard",
}

# 候选**不得引用**的风控对象:风控由基类固定,候选只写信号逻辑。
#
# 为什么"禁止改写"还不够:拿到引用就多一条改写的路,而改写可以绕开一切按名字的检查
# (第 10 轮 codex 实测:`within_budget.__defaults__ = (0.0,)` 把 volume 默认值变成 0,
# 300 元单笔预算恒真,而四层防线全部放行——因为改的是**函数对象的状态**,不是它的名字)。
# 所以这里禁的是"**读**":候选没有正当理由拿到风控函数的定义。
#
# 只收**不会误伤**的名字:泛用词(`reset`/`can_open`/`on_trade_closed`/`ceil_to`/`floor_to`)
# 不在这里——候选代码里出现 `reset` 太常见,按名字一刀切会把正常候选整片拒掉。
BUILTIN_PROTECTED_READ_ATTRS = (
    "within_budget", "stop_distance", "risk_per_trade",
    "DailyLossGuard", "TrailingStop", "trading_day",
    # 只读行情视图的"真容器发放口"(第 2 轮 G8 复核的 Critical 1:候选 `from strategies.trend_atr_strategy import swap_out`
    # 静态层零发现、运行期拿到真 ArrayManager,把 ATR 40.0 压成 2.9,单笔预算闸门被架空)
    "swap_in", "swap_out", "read_only_view", "_REAL", "_SOURCE",
)

# 候选可以重写的模板成员(白名单);不是白名单的**模板成员**一律不许重写
BUILTIN_ALLOWED_OVERRIDES = (
    "entry_signal", "param_grid", "parameters", "fast_window", "slow_window", "atr_mult", "take_profit_r",
)

# 候选不得定义、也不得调用的 dunder:它们能在运行时改写属性,绕开静态筛查与成员比对。
# __init__ 例外(它本来就在受保护名单里,单独处理)。
#
# 两组:
# ① 改写属性型:直接改写实例/类的属性查找与设置行为;
# ② 反射型:能拿到对象的内部字典、类、类层次或代码对象,再从那里绕回来改写状态。
#    `self.guard.__dict__["max_losses"] = 999` 与 `self.guard.__class__.max_losses = 999`
#    都是第 2 组要挡的写法——它们不碰受保护的属性名,只碰"装着它的那个容器"。
BUILTIN_FORBIDDEN_DUNDERS = (
    "__setattr__", "__delattr__", "__getattribute__", "__getattr__",
    "__init_subclass__", "__set_name__",
    # ② 反射型
    "__dict__", "__class__", "__mro__", "__bases__", "__subclasses__",
    "__globals__", "__code__", "__func__", "__self__", "__closure__",
    "__reduce__", "__reduce_ex__", "__getnewargs__", "__getstate__", "__setstate__",
    "__del__", "__mro_entries__",
    # 回溯对象入口(见 research_sandbox.FORBIDDEN_ATTRS 里同一条的说明):经它走得到栈帧、再走到可信代码的局部变量
    "__traceback__",
    # ③ 构造期(`__new__` 在实例构造时执行,**早于**回测前的快照:
    #    它里面的改写会被当成"基线",运行期第三层结构性地看不见。
    #    候选没有正当理由自定义 `__new__`(模板也没有),直接禁掉。
    #    ——第 9 轮 codex 实测:定义 `__new__` 三层全放行。
    "__new__",
    # ④ 函数对象的**状态**(第 10 轮 codex 实测的 Critical):
    #    `within_budget.__defaults__ = (0.0,)` 把 volume 的默认值改成 0,
    #    单笔风险金额恒为 0,300 元预算形同虚设——四层防线全部放行。
    #    改的是函数**对象的状态**而不是它的名字,按名字的黑名单全都看不见。
    #    指纹层已经改成比"函数状态"而不是只比身份,但静态层也要先把这条路堵上:
    #    候选没有正当理由碰这些 dunder。
    "__defaults__", "__kwdefaults__", "__annotations__", "__wrapped__",
    # ⑤ 类型判定入口:改掉 `ABCMeta.__instancecheck__` 之后 isinstance 一律返回 False,
    #    运行时第三层的"家族判定"直接判成"不适用"而放行(第 10 轮 codex 实测)。
    #    第三层已改成按 MRO 判定(结构判定,没有可改写的入口),这里再加一道静态保险。
    "__instancecheck__", "__subclasscheck__", "__subclasshook__", "__prepare__",
    # ⑥ 异常回溯帧(第 11 轮 codex 实测的 Critical):候选在自己代码里
    # `except BaseException as e:` 中顺着 `e.__traceback__.tb_frame.f_globals`
    # 拿回**真 os**(numpy 内部帧的全局命名空间里有 os),于是任意写文件
    # (可伪造 approvals.json)、读文件(.env、网关令牌)、执行命令,
    # 而**两层静态筛查零告警**——这条路上一个禁用名都不出现。
    # 拿到帧**必经**这几个名字,它们是必经之路而不是可选路径。
    "__traceback__", "__context__", "__cause__",
    "tb_frame", "tb_next", "tb_lasti",
    "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "f_lineno",
)

# 框架自带的"按名 setter":这类方法把调用方给的字典**按键名** setattr 到实例上。
# 候选能**直接调用**的只有 CtaTemplate.update_setting 一处:
#   for name in self.parameters: if name in setting: setattr(self, name, setting[name])
# (另有两处按名恢复的路径——CTA 引擎按 strategy.variables 恢复变量、spreadtrading 的对应实现——
#  它们由框架自己调用,当前无害是因为 variables 是模板成员、候选改不了,不是这里的名单保证的。)
# 而模板的 parameters 列表里正好有四个受保护参数(risk_budget / max_daily_losses /
# atr_window / history_vt_symbol),于是候选只要写
#     self.update_setting(dict(risk_budget=1e12))
# 就能把风控底线改掉——而这个名字在源码里既不是属性赋值也不是字典键,
# 按语法位置筛查看不见它(第 5 轮外部复核实测:静态层与运行时层全绿,底线从 300 被改成 1e12)。
# 所以这一类入口**整体禁用**:不是逐个补 API,而是不给"按名改写"留任何入口。
#
# 但要说清楚:静态层封的是**已知写法**,不是能力本身——同一条后果还能从别的路径拿到
# (第 7 轮实测:经异常回溯拿到 frame.f_builtins 里的 setattr;以及 unittest.mock.patch.object)。
# 真正把能力收走的是**执行时不给这些内建**(scripts/research_sandbox.py::safe_builtins):
# 候选的内建字典里没有 setattr,那些路径就都不成立。
#
# **准确口径**(第 22 轮补记,Issue #26):收走的是**候选自己那些帧**里的能力。
# 别人的帧(导入机制、第三方库)里仍然是真实内建——所以这一层是纵深防御,不是边界;
# 真正的边界是人工审批 + 容器隔离。
BUILTIN_FORBIDDEN_BY_NAME_SETTERS = ("update_setting",)

# 由元类自动注入类字典的成员(ABCMeta 会给每个类塞一个 _abc_impl)。
# 它们不是候选"重写"的产物,比对成员时必须忽略,否则每个真实候选都会被误判。
METACLASS_GENERATED = ("_abc_impl",)



class ConstitutionError(Exception):
    """清单缺失、非法或与模板不符(fail-closed)。"""


@dataclasses.dataclass(frozen=True)
class Constitution:
    version: int
    template: str
    allowed_overrides: frozenset[str]
    protected_names: frozenset[str]
    protected_parameters: frozenset[str]
    forbidden_order_calls: frozenset[str]
    protected_module_attrs: frozenset[str]
    protected_entry_points: frozenset[str] = frozenset()
    protected_state_objects: frozenset[str] = frozenset()
    protected_paths: frozenset[str] = frozenset()


def default() -> Constitution:
    """内置清单(不读文件);用于单元测试与"文件尚未落地"的场景。"""
    return Constitution(
        version=1,
        template=BUILTIN_TEMPLATE,
        allowed_overrides=frozenset(BUILTIN_ALLOWED_OVERRIDES),
        protected_names=frozenset(BUILTIN_PROTECTED_NAMES),
        protected_parameters=frozenset(BUILTIN_PROTECTED_PARAMETERS),
        forbidden_order_calls=frozenset(BUILTIN_FORBIDDEN_ORDER_CALLS),
        protected_module_attrs=frozenset(BUILTIN_PROTECTED_MODULE_ATTRS),
        protected_entry_points=frozenset(BUILTIN_PROTECTED_ENTRY_POINTS),
        protected_state_objects=frozenset(BUILTIN_PROTECTED_STATE_OBJECTS),
        protected_paths=frozenset(BUILTIN_PROTECTED_PATHS),
    )


def _require_name_list(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.isidentifier() for v in value):
        raise ConstitutionError(f"清单字段 {field} 必须是标识符字符串列表")
    return frozenset(value)


def _require_path_list(value: object, field: str) -> frozenset[str]:
    """受保护属性路径:点号分隔的标识符(如 `am.array`)。"""
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v and all(part.isidentifier() for part in v.split("."))
        for v in value
    ):
        raise ConstitutionError(f"清单字段 {field} 必须是点号分隔的属性路径列表")
    return frozenset(value)


def load(path: Path | None = None) -> Constitution:
    """载入清单文件;文件缺失或非法一律抛错(fail-closed)。文件只能在内置清单之上追加。"""
    path = Path(path) if path is not None else CONSTITUTION_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConstitutionError(f"不可变宪法清单缺失或不可读(fail-closed): {path.name}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConstitutionError(f"不可变宪法清单不是合法 JSON(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise ConstitutionError("不可变宪法清单必须是 JSON 对象")
    if data.get("version") != 1:
        raise ConstitutionError(f"不可变宪法清单版本不支持: {data.get('version')!r}")
    template = data.get("template")
    if not isinstance(template, str) or not _TEMPLATE_RE.match(template):
        raise ConstitutionError("不可变宪法清单的 template 必须是 '模块路径:类名' 形式")
    # 模板只能取内置白名单里的类:换成任意可导入的类(如 vnpy 的 CtaTemplate)会让继承约束形同虚设。
    # 要换模板必须改代码、走代码评审。
    if template not in BUILTIN_ALLOWED_TEMPLATES:
        raise ConstitutionError(
            f"不可变宪法清单的 template 不在内置白名单内: {template!r};"
            f"可选: {', '.join(BUILTIN_ALLOWED_TEMPLATES)}")
    for field in ("allowed_overrides", "protected_names", "protected_parameters",
                  "forbidden_order_calls", "protected_module_attrs",
                  "protected_entry_points", "protected_state_objects", "protected_paths"):
        if field not in data:
            raise ConstitutionError(f"不可变宪法清单缺少字段 {field}")

    builtin = default()
    allowed = _require_name_list(data["allowed_overrides"], "allowed_overrides")
    protected_names = _require_name_list(data["protected_names"], "protected_names")
    protected_params = _require_name_list(data["protected_parameters"], "protected_parameters")
    order_calls = _require_name_list(data["forbidden_order_calls"], "forbidden_order_calls")
    module_attrs = _require_name_list(data["protected_module_attrs"], "protected_module_attrs")
    entry_points = _require_name_list(data["protected_entry_points"], "protected_entry_points")
    state_objects = _require_name_list(data["protected_state_objects"], "protected_state_objects")
    protected_paths = _require_path_list(data["protected_paths"], "protected_paths")

    if allowed & builtin.protected_names:
        raise ConstitutionError("allowed_overrides 不得包含内置受保护成员(清单只能追加,不能放宽底线)")
    if allowed & builtin.protected_parameters:
        raise ConstitutionError("allowed_overrides 不得包含内置受保护参数(清单只能追加,不能放宽底线)")
    if protected_params & builtin.allowed_overrides:
        raise ConstitutionError("protected_parameters 不得与内置允许重写的成员冲突")
    # 文件自身新增的项之间也不能互相抵消:同一个名字既在白名单又在受保护名单里时,
    # 白名单在运行时优先生效,新增的保护会静默失效——必须报错而不是让它生效。
    # 冲突要在**合并后**的集合上查:文件新增的 protected_names 也可能撞上内置白名单。
    effective_allowed = allowed | builtin.allowed_overrides
    effective_protected = ((protected_names | builtin.protected_names)
                           | (protected_params | builtin.protected_parameters)
                           | (entry_points | builtin.protected_entry_points)
                           | (state_objects | builtin.protected_state_objects))
    if effective_allowed & effective_protected:
        clash = ", ".join(sorted(effective_allowed & effective_protected))
        raise ConstitutionError(f"白名单与受保护清单冲突({clash}):白名单会胜出,保护将失效")
    if protected_names & protected_params:
        raise ConstitutionError("protected_names 与 protected_parameters 有重叠:同一项被登记两次")

    return Constitution(
        version=1,
        template=template,
        allowed_overrides=effective_allowed,
        protected_names=protected_names | builtin.protected_names,
        protected_parameters=protected_params | builtin.protected_parameters,
        forbidden_order_calls=order_calls | builtin.forbidden_order_calls,
        protected_module_attrs=module_attrs | builtin.protected_module_attrs,
        protected_entry_points=entry_points | builtin.protected_entry_points,
        protected_state_objects=state_objects | builtin.protected_state_objects,
        protected_paths=protected_paths | builtin.protected_paths,
    )


def load_template(c: Constitution | None = None) -> type:
    """按 '模块:类' 载入模板类;导入或取属性失败一律抛错(fail-closed)。"""
    c = c or default()
    module_name, _, class_name = c.template.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:                                  # noqa: BLE001 - 任何导入失败都必须拒绝
        raise ConstitutionError(f"模板类无法导入(fail-closed): {c.template}: {exc}") from exc
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        raise ConstitutionError(f"模板类不存在(fail-closed): {c.template}")
    return cls


# ---------- 静态筛查 ----------

def _class_body_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _string_elements(node: ast.AST) -> list[str]:
    """元组/列表/集合字面量里的字符串常量(`__slots__` 用)。"""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [elt.value for elt in node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return []


def _class_body_target_ids(tree: ast.AST) -> set[int]:
    """类体里被赋值的**裸名字**目标节点(id 集合)。

    用来区分"类体里定义 `am`"(覆盖受保护成员,拒绝)与
    "函数体里 `am = self.am.sma(5)`"(普通局部变量,放行)。
    第 11 轮 codex 的 Low 1:`am`/`bg` 是 ArrayManager/BarGenerator 的常见缩写,
    LLM 候选写局部变量时会常规踩中——按裸名一刀切会把正常候选整片拒掉。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        ids.add(id(target))
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                ids.add(id(stmt.target))
    return ids


def _bound_names(target: ast.AST) -> set[str]:
    """赋值目标里绑定的**裸名字**(`x` / `a, b = ...` / `*rest`)。"""
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            names.update(_bound_names(element))
    elif isinstance(target, ast.Starred):
        names.update(_bound_names(target.value))
    return names


def _direct_bindings(node: ast.AST) -> set[str]:
    """一条语句**在本作用域**里绑定的名字。

    收:赋值/注解赋值/复合赋值的目标、`for` 目标、`with ... as`、`except ... as`、
    `match` 的捕获名、`def`/`class` 的名字、海象表达式 `(x := ...)` 的目标。
    **不收**:
    - `import` 绑定的名字(那是框架那个对象,不是候选自己的);
    - 嵌套函数/类体**内部**的绑定(属于它们自己的作用域)。
    """
    names: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        # 只绑**这个名字**;参数与函数体里的绑定属于它自己的作用域。
        return {node.name}
    if isinstance(node, ast.Assign):
        for target in node.targets:
            names.update(_bound_names(target))
    elif isinstance(node, ast.AnnAssign):
        names.update(_bound_names(node.target))
    elif isinstance(node, ast.AugAssign):
        names.update(_bound_names(node.target))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        names.update(_bound_names(node.target))
        names.update(_scope_bindings(node.body))
        names.update(_scope_bindings(node.orelse))
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                names.update(_bound_names(item.optional_vars))
        names.update(_scope_bindings(node.body))
    elif isinstance(node, (ast.If, ast.While)):
        names.update(_scope_bindings(node.body))
        names.update(_scope_bindings(node.orelse))
    elif isinstance(node, ast.Try):
        names.update(_scope_bindings(node.body))
        names.update(_scope_bindings(node.orelse))
        names.update(_scope_bindings(node.finalbody))
        for handler in node.handlers:
            if handler.name:
                names.add(handler.name)            # `except E as e:` 的名字是字符串,不是 ast.Name
            names.update(_scope_bindings(handler.body))
    elif isinstance(node, ast.Match):
        for case in node.cases:
            names.update(_match_bindings(case.pattern))
            names.update(_scope_bindings(case.body))
    elif isinstance(node, ast.Expr):
        names.update(_walrus_names(node.value))
    # 海象表达式可能藏在**任何**位置(`if (x := 1):` 的条件里、`a = (y := 2)` 的值里、
    # 复合语句的体内),所以整条语句扫一遍;嵌套 `def` 里的海象绑在它自己的作用域,跳过。
    names.update(_statement_walrus_names(node))
    return names


def _statement_walrus_names(node: ast.AST) -> set[str]:
    """一条语句里(不含嵌套 `def` 内部)所有海象表达式绑定的名字。"""
    names: set[str] = set()
    stack = list(ast.iter_child_nodes(node))
    while stack:
        sub = stack.pop()
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue                        # 它里面的海象绑在它自己的作用域
        if isinstance(sub, ast.NamedExpr):
            names.update(_bound_names(sub.target))
        stack.extend(ast.iter_child_nodes(sub))
    return names


def _scope_bindings(stmts: list) -> set[str]:
    """一层作用域里**直接**绑定的名字(不含嵌套函数/类体内部的绑定)。"""
    names: set[str] = set()
    for node in stmts:
        names.update(_direct_bindings(node))
    return names


def _walrus_names(node: ast.AST) -> set[str]:
    """海象表达式 `(x := ...)` 绑定的名字。"""
    return {name for sub in ast.walk(node)
            if isinstance(sub, ast.NamedExpr) for name in _bound_names(sub.target)}


def _match_bindings(pattern: ast.AST) -> set[str]:
    """`match` 分支捕获的名字(捕获名是字符串字段,不是 ast.Name)。"""
    names: set[str] = set()
    for sub in ast.walk(pattern):
        if isinstance(sub, ast.MatchAs) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.MatchStar) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            names.add(sub.rest)
    return names


def _function_bindings(node: ast.AST) -> set[str]:
    """一个函数(或 lambda)**自己**作用域里绑定的名字:参数 + 函数体里的绑定。"""
    names: set[str] = set()
    args = node.args                                        # type: ignore[attr-defined]
    for arg in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.update(_scope_bindings(node.body))
    else:                                                   # lambda:函数体是一个表达式
        names.update(_walrus_names(node.body))             # type: ignore[attr-defined]
    return names


class _CandidateScope:
    """候选源码的**作用域表**:判断某个裸名是不是候选**自己**定义的东西。

    第 6 轮 codex 的 High-1:静态层按**名字**拒受保护符号,候选自己写一个
    `def trading_day()`(LLM 候选极常见的辅助名;本仓 `risk/account_guard.py` 就有同名函数)
    就被拒——而运行期那一层已经改成按**身份**核对(候选自己的同名符号放行),
    两层口径相反:静态筛查先执行,运行期那条"放行"在真实流水线里**永远走不到**。
    这正是 F2 要消灭的那一类误伤(把诚实候选整片拒掉,进化循环因此被闲置)。

    第 7 轮 codex 的 Medium-1/2 把上一版的两个漏洞点破了:
    - Medium-1:上一版只收 `def`/`class`/赋值目标,**参数、for/with/except/lambda/海象/match 捕获**
      这些绑定形式全漏了 → 候选把受保护符号当参数传进来会被误拒;
    - Medium-2:上一版把**整个模块**里出现过的名字都当成"候选自己定义的",
      于是某个函数里定义的 `swap_out` 会把**模块级**的 `swap_out()` 调用一起豁免掉。
    现在按**作用域**收:模块级一份,每个函数/类各一份,引用时沿作用域链往上找
    (Python 的规则:模块作用域处处可见;类作用域只在类体里可见,不进方法)。

    **不收 import 绑定的名字**:`from risk.market_view import swap_out` 绑的是框架那个,
    必须照旧拒(那一路由 `ImportFrom` 分支裁决,不受这里的豁免影响)。"""

    def __init__(self, tree: ast.AST) -> None:
        self.module_names = _scope_bindings(getattr(tree, "body", []))
        self.function_names: dict[int, set[str]] = {}
        self.class_names: dict[int, set[str]] = {}
        self.parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self.function_names[id(node)] = _function_bindings(node)
            elif isinstance(node, ast.ClassDef):
                self.class_names[id(node)] = _scope_bindings(node.body)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent

    def is_defined(self, name: str, node: ast.AST) -> bool:
        """这个裸名在 `node` 所在的作用域链上是不是候选自己绑定的。"""
        if name in self.module_names:
            return True
        current = self.parents.get(id(node))
        saw_function = False
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                saw_function = True
                if name in self.function_names.get(id(current), ()):
                    return True
            elif isinstance(current, ast.ClassDef):
                # 类作用域只在**类体**里可见;方法里看不到类属性(Python 的规则)。
                if not saw_function and name in self.class_names.get(id(current), ()):
                    return True
            current = self.parents.get(id(current))
        return False


def _import_bound_names(tree: ast.AST) -> set[str]:
    """被 import 语句**绑定**的名字(模块别名)。

    `import strategies.trend_atr_strategy as self` 把 `self` 绑成了**模块**,
    于是 `self.swap_out(...)` 长得像"实例自己的属性",实则一步拿到真发放口
    (第 7 轮 codex 的 High-1 实测:静态层与运行期同时放行)。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    return names


def _bare_local_allowed(name: str, node_id: int, class_body_targets: set[int]) -> bool:
    """裸局部变量用容器名(`am`/`bg`)是否放行。

    容器名只保护**属性路径**(`self.am.xxx`),不保护裸局部名:
    `am = self.am.sma(5)` 是候选读行情的常规写法,按裸名一刀切会误伤
    (第 11 轮 codex 的 Low 1)。类体里定义 `am` 仍然拒绝——那是覆盖成员,不是局部变量。"""
    return name in BUILTIN_PROTECTED_CONTAINER_SCOPE and node_id not in class_body_targets


def _is_protected(name: str, c: Constitution) -> bool:
    """名字是否受保护。

    两个集合是**各自独立**的:清单文件里两者有交集时 `load()` 会拒绝加载,内置常量里
    `BUILTIN_PROTECTED_NAMES` 已包含 `BUILTIN_PROTECTED_PARAMETERS`。这里仍然查两遍,
    免得"参数只登记在 protected_parameters"时被漏掉。
    """
    return name in c.protected_names or name in c.protected_parameters


def _protected_lower(c: Constitution) -> set[str]:
    """受保护项的小写形式:与 `check_params` 同一口径(大小写不敏感)。

    `Risk_Budget` 与 `risk_budget` 是两个不同的属性,精确匹配会让这条路径绕过参数校验;
    静态层的关键字参数检查用同一把尺子,免得两处口径不一致。"""
    return {name.lower() for name in c.protected_names} | {name.lower() for name in c.protected_parameters}


def protected_lower_all(c: Constitution | None = None) -> set[str]:
    """受保护项的小写全集(成员 ∪ 参数):**给别的模块用的同一把尺子**。

    `research_grid` 原来自己抄了一份 `protected_parameters` 且大小写敏感,
    与 `check_params` 的口径不一致——同一把尺子,两处量纲不同(第 7 轮 codex 实测:
    候选把参数名写成 `Risk_Budget` 并把网格键拼出来,网格层放行、参数层拒绝)。
    """
    c = c or default()
    return {name.lower() for name in c.protected_names} | {name.lower() for name in c.protected_parameters}


def _attr_path(node: ast.AST) -> list[str] | None:
    """把属性表达式解析成路径段列表。

    `self.guard.max_losses` → ["self", "guard", "max_losses"];
    `self.am.close[0]` → ["self", "am", "close"](下标被跳过——写 `self.am.close[0]`
    实际改的是 `self.am.close` 指向的那个对象,路径要一路走到它)。
    不是属性链时返回 None。"""
    segments: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            segments.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Subscript):
            current = current.value
        elif isinstance(current, ast.Name):
            segments.append(current.id)
            segments.reverse()
            return segments
        else:
            return None


def _path_after_self(node: ast.AST) -> list[str] | None:
    """属性路径里 `self.` 之后的部分;根不是 self 时返回 None。

    只认 `self.` 开头的写法:`s = self` 之后的 `s.guard.x = 1` 靠
    "禁止引用状态对象"那条规则拦(见 BUILTIN_PROTECTED_STATE_OBJECTS),不靠这里。"""
    path = _attr_path(node)
    if not path or path[0] != "self":
        return None
    return path[1:]


def _self_instance_attr(node: ast.AST, import_names: set[str] | None = None) -> bool:
    """`self.<name>` 形状:实例**自己**的属性,不是某个模块的成员。

    第 11 轮 codex 的 Medium 1:`trading_day` 进了受保护的模块级成员名单之后,
    `_write_violation` 对**任意路径段**命中模块级成员名 → `self.trading_day = bar.datetime.date()`
    被判成"改写了受保护的风控函数 trading_day",把"每天只开一次仓"这类正常候选整片拒掉
    (第 10 轮放行、第 11 轮误拒,已用旧源码对照验证)。
    根因:模块级成员(`risk.risk_guard.trading_day`)与实例属性(`self.trading_day`)是两回事,
    前者是全局函数,后者改不到它。

    第 7 轮 codex 的 High-1:**光看字面名 `self` 不够**。候选写
    `import strategies.trend_atr_strategy as self`,`self` 绑的是**模块**,
    `self.swap_out(...)` 于是被当成"实例自己的属性"而放行——静态层与运行期同时放行,
    真容器回到候选手里。所以这里额外要求:`self` **不是**被 import 绑定的名字。"""
    path = _attr_path(node)
    if not path or len(path) != 2 or path[0] != "self":
        return False
    if import_names and path[0] in import_names:
        return False            # `self` 是模块别名,不是实例
    return True


def _write_violation(target: ast.AST, c: Constitution, import_names: set[str] | None = None) -> str | None:
    """写入受保护成员(或受保护对象的内部状态)时返回路径说明,否则 None。

    早先只看赋值的**末段**属性名(`target.attr`),于是这两种写法三层全绿:
        self.guard.max_losses = 999     # 解除当日连亏熔断
        self.cta_engine.size = 0.001    # 把止损距离算出来的风险金额压到接近 0
    受保护的对象是**实例属性**,不在类字典里,`check_class` 比不出来(第 7 轮 codex 实测)。

    现在的规则:属性路径里**任何一段**命中受保护成员就算越权。
    - 看整条路径而不是末段:末段只有 `max_losses`/`size` 这种名字,
      而 `guard`/`cta_engine`/`am` 才是"装着风控判定的那些对象"。
    - 不限根:只认 `self.` 开头的话,`s = self` 之后的 `s.risk_budget = 1e12`
      会从静态层溜过去(第 8 轮自查实测)。
    - 受保护的**模块级**名字也算:第 9 轮 codex 实测 `rg.DailyLossGuard.can_open = ...`
      这类改写(路径中间的类名才是要害,末段 `can_open` 看不出来)。
    - 根解析不出来时(`type(cls).risk_budget = 1e12`,路径遇到调用表达式就断了)
      至少按**末段属性名**判一次,别让"根不是名字"变成免死金牌(第 9 轮实测)。
    """
    path = _attr_path(target)
    if path:
        # `self.<name>` 是实例自己的属性:与模块级成员(`risk.risk_guard.<name>`)是两回事。
        # 模块级成员名只在**路径不止两段**时才算命中(第 11 轮 codex 的 Medium 1:
        # `self.trading_day = ...` 曾被误判成"改写了受保护的风控函数")。
        self_attr = len(path) == 2 and path[0] == "self"
        for index, segment in enumerate(path):
            if segment in c.protected_names or segment in c.protected_parameters:
                return ".".join(path[index:])
            if segment in c.protected_module_attrs and _module_attr_owner_ok(segment, path):
                if self_attr and index == len(path) - 1:
                    continue
                return ".".join(path[index:])
        return None
    # 路径断在调用表达式上:退回按末段属性名判。
    # 模块级成员名(`reset` 之类)太泛,这里只在**主人也在路径里**时才算命中。
    if isinstance(target, ast.Attribute):
        attr = target.attr
        if attr in c.protected_names or attr in c.protected_parameters:
            return attr
        if attr in c.protected_module_attrs and _module_attr_owner_ok(attr, _attr_path(target) or []):
            if _self_instance_attr(target, import_names):
                return None
            return attr
    return None


def _module_attr_owner_ok(name: str, path: list[str]) -> bool:
    """模块级成员名**有主**时,只有主人也在路径里才算命中。

    `reset` 是 TrailingStop 的方法名,但候选代码里 `self.indicator.reset = 1` 太常见——
    按名字一刀切会把正常候选整片拒掉(第 10 轮 codex 的 Low 1)。"""
    owner = BUILTIN_MODULE_ATTR_OWNERS.get(name)
    return owner is None or owner in path


def _container_write_findings(tree: ast.AST) -> list[tuple[int, str]]:
    """受保护容器(`am`/`bg`)上的**写入**(第 4 轮 G8 复核的 F2:外流规则从"读取"收窄到"写入")。

    **这里曾经还有一条"读取外流"规则**:容器引用一旦"流出去"(存进名字 / 传参 / 装容器 / 返回)
    就一律拒。理由是当时候选手里拿着的是**真实容器**,拿到别名就能原地改写回测数据。
    但只读行情视图落地之后,`self.am.close`/`self.am.close_array`/指标结果返回的都是**拷贝**,
    别名无害;而这条规则把仓库自己的 LLM 提示词写法
    (`self.am.close[-2:]`、`np.mean(self.am.close)`、`self.am.count`、`self.am.close_array`)
    整片拒掉——进化循环因此被闲置(第 4 轮 codex 实测)。
    所以"读取"一律放行,**写入**仍然拒。

    写入的路径各有各的判据:
    - 属性赋值 / 下标赋值 / 复合赋值:`_write_violation` 按整条属性路径判(路径里出现 `am`/`bg` 即命中);
    - 变更型方法调用(`fill`/`update_bar`/`__setitem__`/…):`_container_mutator_call`;
    - `del self.am.close`:**删除**既不是赋值也不是调用,前面两条都看不见它;
    - **容器别名**上的写入与变更型调用(`a = self.am` 之后 `a.update_bar(fake)`):
      别名让属性路径看不出是谁,`_write_violation`/`_container_mutator_call` 都看不见,
      第 4 轮 codex 复核的 Medium-3 实测(旧规则能拦,收窄后 0 findings)——补上;
    - **自由函数形态**的变更型函数(`np.copyto(self.am.close, x)`):名单里有 `copyto`,
      但规则只匹配 `接收者.方法()` 形态——补上(第 4 轮 codex 复核的 Low-2);
    - **下标删除**(`del self.am.close[0]`):删除的下标形式,属性子节点是 Load 上下文,
      前面的删除规则看不见——补上(第 4 轮 codex 复核的 Low-1)。

    残余(如实记录):静态层不再拦"先取**数组**别名再写"(`arr = self.am.close` 之后 `arr[0] = 1`)。
    只读视图下这写的是拷贝,无害;万一视图被绕开,运行期第五层(逐根核对)仍然会抓到。"""
    findings: list[tuple[int, str]] = []
    aliases = _container_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Del):
            path = _attr_path(node) or []
            if any(segment in BUILTIN_PROTECTED_CONTAINER_SCOPE for segment in path[1:]):
                findings.append((node.lineno, f"不得删除受保护容器上的属性 {'.'.join(path)}"))
        if isinstance(node, ast.Call):
            alias_hit = _alias_mutator_call(node, aliases)
            if alias_hit:
                findings.append((node.lineno, f"不得通过容器别名调用变更型方法 {alias_hit}"))
            free_hit = _free_mutator_call(node)
            if free_hit:
                findings.append((node.lineno, f"不得把受保护容器交给变更型函数 {free_hit}"))
        for target in _write_targets(node):
            if _is_container_binding(node):
                # 这一句**就是**别名的定义(`x = self.bg`),不是"通过别名写"。
                break
            hit = _container_view_write(target)
            if hit:
                findings.append((node.lineno, f"不得把受保护容器的视图型结果当写入目标 {hit}"))
            alias_write = _alias_write(target, aliases)
            if alias_write:
                findings.append((node.lineno, f"不得通过容器别名写入受保护容器 {alias_write}"))
            delete_hit = _container_element_delete(target)
            if delete_hit:
                findings.append((node.lineno, f"不得删除受保护容器上的元素 {delete_hit}"))
    return findings


def _is_container_binding(node: ast.AST) -> bool:
    """这个赋值语句是不是"把容器本身绑到名字上"(`x = self.am` / `x: Any = self.bg`)。

    这**不是**写入——是别名的定义。别名定义里的目标名当然在别名集合里,
    不把它排除掉,`x = self.bg` 这一句本身就会被当成"通过别名写入"而误报。"""
    value = None
    if isinstance(node, ast.Assign):
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        value = node.value
    if value is None:
        return False
    return (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr in BUILTIN_PROTECTED_CONTAINER_SCOPE)


def _root_name(node: ast.AST) -> str | None:
    """表达式最底部的名字(`a.close.reshape(-1)[0]` → `a`)。"""
    current = node
    for _ in range(32):                       # 防御:表达式再深也不会超过这个数
        if isinstance(current, ast.Name):
            return current.id
        if isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Subscript):
            current = current.value
        else:
            return None
    return None


def _container_aliases(tree: ast.AST) -> set[str]:
    """绑定到容器**本身**的局部名(`a = self.am` / `b = self.bg` / `a: Any = self.am`)。

    只认"直接绑定容器本身":`x = self.am.close` 绑的是**数组**(视图下是拷贝),
    写它无害,不进这份名单(那是"如实记录"里的残余)。"""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        value = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = [node.target]
        else:
            continue
        if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id == "self"
                and value.attr in BUILTIN_PROTECTED_CONTAINER_SCOPE):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _alias_mutator_call(node: ast.Call, aliases: set[str]) -> str | None:
    """对**容器别名**调用变更型方法(`a.update_bar(fake)`,`a` 是 `self.am` 的别名)。"""
    if not aliases:
        return None
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in BUILTIN_PROTECTED_CONTAINER_MUTATORS:
        return None
    if isinstance(func.value, ast.Name) and func.value.id in aliases:
        return f"{func.value.id}.{func.attr}"
    return None


def _alias_write(target: ast.AST, aliases: set[str]) -> str | None:
    """写入目标是**容器别名**(`a.close[0] = 1` / `a.count = 5`,`a` 是 `self.am` 的别名)。"""
    if not aliases:
        return None
    root = _root_name(target)
    if root and root in aliases:
        return root
    return None


def _free_mutator_call(node: ast.Call) -> str | None:
    """把受保护容器交给**自由函数形态**的变更型函数(`np.copyto(self.am.close, x)`)。

    numpy 的写入口里,这几个是**自由函数**(第一个实参就是被写的目标数组):
    `copyto(dst, src)`、`put(a, ind, v)`、`putmask(a, mask, v)`、`place(a, mask, v)`。
    变更型方法名单里本来就有 `copyto`,但规则只匹配 `接收者.方法()` 形态,
    自由函数形态漏掉了(第 4 轮 codex 复核的 Low-2)。

    **只认这几个**:名单里的 `append`/`extend`/`sort` 这些是**方法**,它们被写的是接收者,
    第一个实参是**读**(`x.append(self.am.close)` 是把数组放进候选自己的列表,无害),
    按第一个实参判会把这种正当写法误伤。"""
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if name not in _MUTATING_FREE_FUNCTIONS:
        return None
    # 被写的目标既可能是**位置**实参(`np.copyto(self.am.close, x)`),
    # 也可能是**关键字**实参(`np.copyto(dst=self.am.close, src=x)`、`np.put(a=self.am.close, …)`)
    # ——numpy 的 `copyto` 是 ufunc、`put` 是普通函数,两种都接受关键字。
    # 第 6 轮 codex 的 Medium-2:只看位置实参时,关键字形态 0 findings。
    targets: list[ast.AST] = []
    if node.args:
        targets.append(node.args[0])
    for keyword in node.keywords:
        if keyword.arg in _FREE_MUTATOR_TARGET_KEYWORDS:
            targets.append(keyword.value)
    for target in targets:
        path = _attr_path(target) or []
        for index, segment in enumerate(path):
            if index == 0:
                continue
            if segment in BUILTIN_PROTECTED_CONTAINER_SCOPE:
                return f"{name}({'.'.join(path)})"
    return None


def _container_element_delete(target: ast.AST) -> str | None:
    """删除受保护容器上的**元素**(`del self.am.close[0]`)。"""
    if not isinstance(target, ast.Subscript):
        return None
    path = _attr_path(target.value) or []
    for index, segment in enumerate(path):
        if index == 0:
            continue
        if segment in BUILTIN_PROTECTED_CONTAINER_SCOPE:
            return ".".join(path)
    return None


def _write_targets(node: ast.AST) -> list[ast.AST]:
    """这个语句里的**写入目标**(赋值/复合赋值/注解赋值/删除的目标)。"""
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets.extend(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets.append(node.target)
    elif isinstance(node, ast.Delete):
        targets.extend(node.targets)
    return targets


def _container_view_write(target: ast.AST) -> str | None:
    """写入目标里有没有"容器上的视图型方法/属性"(`reshape`/`view`/`swapaxes`/`T`…)。

    第 4 轮 G8 复核的 F2 把"读取外流"规则收窄成"写入"之后,这条路径一度**没人管**:
        self.am.close.reshape(-1)[0] = 9.0
    读(`x = self.am.close.reshape(-1)`)是允许的——视图下 `self.am.close` 是拷贝;
    但把它**当写入目标**就不该放行:静态层在这条上没有别的闸
    (`_write_violation` 的属性路径断在调用表达式上,`_container_mutator_call` 只看变更型方法名)。

    判据:写入目标里出现一个属性访问,它的名字在视图型名单里,而它的接收者路径上有 `am`/`bg`。"""
    for node in ast.walk(target):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in BUILTIN_PROTECTED_CONTAINER_VIEWS:
            continue
        path = _attr_path(node) or []
        if any(segment in BUILTIN_PROTECTED_CONTAINER_SCOPE for segment in path[1:]):
            return ".".join(path)
    return None


def _container_mutator_call(node: ast.Call, c: Constitution) -> str | None:
    """对受保护容器调用**变更型方法**时返回那条路径,否则 None。

    第 11 轮 codex 的 High 1:容器规则原来只挡"写"与"取别名",**挡不住"调用"**——
    `self.am.update_bar(fake_bar)` 既不是属性赋值也不是取别名,四层防线全部放行,
    而回测里被注入了一根假 K 线(实测 skipped_by_budget 由 0 变 3928、成交由 212 变 2)。
    与"写数组被拦、插数组不拦"自相矛盾:容器规则声明的目的正是"防伪造价格→伪造收益"。

    只拦**变更型**方法:读方法(`sma`/`atr`/`generate`/`size`)不在名单里,
    候选读行情的正当用途不受影响。"""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    path = _attr_path(func)
    if not path or len(path) < 2:
        return None
    method = path[-1]
    if method not in BUILTIN_PROTECTED_CONTAINER_MUTATORS:
        return None
    receiver = path[:-1]
    for index, segment in enumerate(receiver):
        if index == 0:
            # 裸名不算容器:`am = []` 之后 `am.append(x)` 改的是候选自己的列表。
            # 第 12 轮 codex 的 Medium 1:第 11 轮的 Low 1 只放宽了赋值路径,
            # 变更型方法调用路径照旧按裸名拒,会把正常候选整片拒掉。
            continue
        if segment in BUILTIN_PROTECTED_CONTAINER_SCOPE:
            return ".".join(path[index:])
    return None


def _path_violation(node: ast.AST, c: Constitution) -> str | None:
    """整条属性路径命中受保护路径(如 `self.am.array`)时返回该路径,否则 None。"""
    rest = _path_after_self(node)
    if not rest:
        return None
    for path in c.protected_paths:
        parts = path.split(".")
        if rest[:len(parts)] == parts:
            return path
    return None


# ---------- 通配符导入(`from X import *`)的裁决 ----------
#
# 第 4 轮 G8 复核的 Critical:候选写 `from strategies.trend_atr_strategy import *`,
# 静态筛查与沙盒筛查两层都是 **0 findings**,却把 `swap_in`/`swap_out`/`read_only_view`
# (连同 `within_budget`/`DailyLossGuard` 等风控函数)一次性绑进候选的命名空间——
# 只读视图的整个前提("候选碰不到换入换出")就这样被绕开。
#
# 为什么按名字逐个拦不住:通配符导入**不经过逐名裁决**。具名导入
# (`from X import swap_out`)会被本文件的 ImportFrom 分支逐个名字判,
# 通配符则是**导入机制自己**按被导入模块的公开名字列表绑定,
# 静态层看不见候选最终拿到了哪些名字。
# 所以这一层改成"先把被导入模块的名字集合解析出来,再决定放不放"。
#
# 判定口径(fail-closed,三条任一命中即拒):
# ① 模块级名字里出现**受保护符号** → 拒,并指出是哪几个;
# ② 模块有**模块级私有符号**(下划线开头) → 拒(模块内部的东西不该外流);
# ③ 被导入模块**解析不出来**(不在仓库内 / 源码读不出来 / 相对导入) → 拒:
#    查不了等于没查。
#
# 名字集合只从**源码**解析(不执行模块):在筛查阶段执行被导入的模块,
# 等于在候选跑起来之前先跑一遍候选的代码。
REPO_ROOT = Path(__file__).resolve().parents[1]

# 模块名集合的缓存:键是 (路径, mtime_ns, 大小),文件一改就失效。
_MODULE_NAMES_CACHE: dict[tuple[str, int, int], tuple[frozenset[str], frozenset[str], bool]] = {}
# 缓存上限:文件一改就多一条,不清会一直涨(每条几十字节,但没有理由让它涨)。
_MODULE_NAMES_CACHE_MAX = 128


def _star_import_blocked_symbols() -> frozenset[str]:
    """通配符导入不得暴露的符号:受保护的风控对象 + 只读视图的发放口 + 状态对象 + 风控执行点。

    放在函数里而不是做成模块级常量:模块级的字符串集合常量受历史守卫的
    "只增不减"比对约束(见 tools/check_constitution_history.py),而这份名单
    是**派生**的(由已有清单算出),登记进去会让守卫把它当成一份要单独维护的历史事实。"""
    return (frozenset(BUILTIN_PROTECTED_READ_ATTRS)
            | frozenset(BUILTIN_PROTECTED_STATE_OBJECTS)
            | frozenset(BUILTIN_PROTECTED_ENTRY_POINTS))


def _module_source_file(module: str) -> Path | None:
    """把模块名解析成**仓库内**的源码文件;解析不到返回 None(只找文件,不执行模块)。

    **包优先**:Python 的 `FileFinder` 先找包(`X/__init__.py`)再找模块(`X.py`)。
    顺序反了会核验**错的文件**——`pkg/__init__.py` 脏、`pkg.py` 干净时,
    静态层按干净的那个放行,而运行时绑进来的是脏包里的名字
    (第 4 轮 codex 复核的 Medium-1 实测)。"""
    if not module:
        return None
    base = REPO_ROOT.joinpath(*module.split("."))
    for path in (base / "__init__.py", base.with_suffix(".py")):
        if path.is_file():
            return path
    return None


def _module_export_info(path: Path) -> tuple[frozenset[str], frozenset[str], bool] | None:
    """被导入模块**实际会被 `import *` 绑走**的名字集合、模块级私有符号,以及"是否不可证"。

    返回 `(exported, private, dynamic)`;源码读不出来或解析失败返回 None。

    - `private`:模块级**私有**符号(下划线开头)。`import *` 本来就不绑它们,
      但"这个模块在藏东西"本身是个信号,所以照样拒(规则:通配符导入不得暴露模块级私有符号)。

    - `names`:`import *` 实际绑定的名字。有 `__all__` 时以 `__all__` 为准
      (Python 就是这么定的);没有 `__all__` 时是**公开**名字(下划线开头的本来就不绑)。
    - `dynamic`:名字集合**不可证**(模块用了动态导出),此时必须 fail-closed 拒掉。
      三种来源:①模块级 `__getattr__`(PEP 562,按需生成名字);
      ②`__all__` 不是字面量列表(算出来的);③模块内部自己用了 `from ... import *`
      (它绑进来的名字我们看不到)。

    收进集合的绑定:模块级函数/类名、赋值目标、带注解的赋值、`import`/`from ... import`,
    以及模块级 `if`/`try`/`for`/`with`/`match` 里的绑定
    (第 4 轮 codex 复核的 Medium-2:漏掉 `for`/`with` 会让真实名字不可见)。

    **带缓存**:筛查是批量跑的,同一个被导入模块的源码要被解析很多次。
    缓存键带上 mtime 与大小,文件一改就重新解析。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _MODULE_NAMES_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    names: set[str] = set()
    dynamic = False
    all_names: set[str] | None = None

    def collect_bindings(body: list) -> None:
        nonlocal dynamic, all_names
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
                if node.name == "__getattr__":
                    dynamic = True            # ①PEP 562:按需生成名字,不可证
            elif isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                        if target.id == "__all__":
                            all_names = _literal_string_set(node.value)
                            if all_names is None:
                                dynamic = True        # ②`__all__` 算出来的
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        names.update(e.id for e in target.elts if isinstance(e, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
                if node.target.id == "__all__" and node.value is not None:
                    all_names = _literal_string_set(node.value)
                    if all_names is None:
                        dynamic = True
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        dynamic = True                # ③模块自己用了通配符:它绑了什么我们看不到
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                for target in ast.walk(node.target):
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                collect_bindings(node.body)
                collect_bindings(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for target in ast.walk(item.optional_vars):
                            if isinstance(target, ast.Name):
                                names.add(target.id)
                collect_bindings(node.body)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    for target in ast.walk(case.pattern):
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                    collect_bindings(case.body)
            elif isinstance(node, ast.If):
                collect_bindings(node.body)
                collect_bindings(node.orelse)
            elif isinstance(node, ast.Try):
                collect_bindings(node.body)
                collect_bindings(node.orelse)
                collect_bindings(node.finalbody)
                for handler in node.handlers:
                    collect_bindings(handler.body)

    collect_bindings(tree.body)
    if all_names is not None:
        exported = set(all_names)
    else:
        # 没有 `__all__`:`import *` 只绑**公开**名字(下划线开头的本来就不绑)。
        exported = {n for n in names if not n.startswith("_")}
    private = {n for n in names
               if n.startswith("_") and not (n.startswith("__") and n.endswith("__"))}
    result = (frozenset(exported), frozenset(private), dynamic)
    if len(_MODULE_NAMES_CACHE) >= _MODULE_NAMES_CACHE_MAX:
        _MODULE_NAMES_CACHE.clear()
    _MODULE_NAMES_CACHE[key] = result
    return result


def _literal_string_set(node: ast.AST) -> set[str] | None:
    """字面量列表/元组/集合里的字符串集合;不是字面量就返回 None(不可证)。"""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    values: set[str] = set()
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.add(element.value)
    return values


def star_import_finding(module: str, level: int = 0) -> str | None:
    """裁决 `from <module> import *`:该放行返回 None,否则返回拒绝理由(含规则名)。

    相对导入(`from . import *`)一律拒:候选文件在仓库里的哪个位置,
    静态层不知道(这里只看得到源码文本),解析不出被导入的模块。"""
    if level or not module:
        return ("通配符导入的模块无法确定(相对导入或缺少模块名):查不了等于没查,拒绝"
                "(规则:通配符导入不得暴露受保护符号)")
    path = _module_source_file(module)
    if path is None:
        return (f"通配符导入 `{module}` 的名字集合无法核验(模块不在仓库内或找不到源码):"
                f"查不了等于没查,拒绝(规则:通配符导入不得暴露受保护符号)")
    info = _module_export_info(path)
    if info is None:
        return (f"通配符导入 `{module}` 的名字集合无法解析:查不了等于没查,拒绝"
                "(规则:通配符导入不得暴露受保护符号)")
    names, private, dynamic = info
    if dynamic:
        return (f"通配符导入 `{module}` 的名字集合不可证(模块有动态导出或内部通配符):"
                f"查不了等于没查,拒绝(规则:通配符导入不得暴露受保护符号)")
    hits = sorted(names & _star_import_blocked_symbols())
    if hits:
        return (f"通配符导入 `{module}` 会把受保护符号 {'、'.join(hits)} 放进候选命名空间"
                f"(拿到定义就能架空风控判定,只读视图的换入换出也会被绕开)"
                f"(规则:通配符导入不得暴露受保护符号)")
    if private:
        # 第 6 轮 codex 的 Medium-3:**文案必须说真话**。没有 `__all__` 时,
        # Python 的 `import *` 本来就**不绑**下划线开头的名字(实测绑定集里没有它);
        # 只有模块自己把私有名写进 `__all__` 时它才会被绑。所以这里不说"会放进候选命名空间",
        # 而说"这个模块在藏东西"——拒它的**理由**是信号,不是那个具体的绑定后果。
        return (f"通配符导入 `{module}` 的模块含模块级私有符号 {'、'.join(sorted(private))}"
                f"(import * 通常不绑下划线名,但该模块在藏东西;"
                f"若它的 __all__ 点名了这些符号则会被绑进来)(规则:通配符导入不得暴露模块级私有符号)")
    return None


def import_module_finding(module: str) -> str | None:
    """裁决 `import <模块>`:该放行返回 None,否则返回拒绝理由(含规则名)。

    第 7 轮 codex 的 High-1:`import strategies.trend_atr_strategy as self` +
    `self.swap_out(strategy)` 在静态层与运行期**同时放行**,真 ArrayManager 回到候选手里
    (实测:`strategy.am is 真 ArrayManager` → True,`close[0] = -12345.0` 真的改了真容器;
    第五层"逐根核对"能发现篡改,所以结果没被伪造,但"候选碰不到换入换出"这个前提被绕开了)。
    根因之二:宪法层有通配符导入的分支,**没有**具名导入整个模块的分支。

    口径:
    - **仓库内**的模块:解析它的名字集合,含受保护符号(或名字集合不可证)就拒;
    - **仓库外**的第三方模块(numpy/pandas 之类):**不管**——它们不含我们的发放口,
      一律拒会把诚实候选整片拒掉(进化循环因此被闲置),那不是纵深防御,那是自伤。
    """
    path = _module_source_file(module)
    if path is None:
        return None                      # 仓库外的第三方模块:不含本仓的发放口
    info = _module_export_info(path)
    if info is None:
        return (f"导入 `{module}` 的名字集合无法解析:查不了等于没查,拒绝"
                f"(规则:候选不得导入含受保护符号的模块)")
    names, _private, dynamic = info
    if dynamic:
        return (f"导入 `{module}` 的名字集合不可证(模块有动态导出):查不了等于没查,拒绝"
                f"(规则:候选不得导入含受保护符号的模块)")
    hits = sorted(names & _star_import_blocked_symbols())
    if hits:
        return (f"导入 `{module}` 会把受保护符号 {'、'.join(hits)} 握在手里"
                f"(拿到定义就能架空风控判定,只读视图的换入换出也会被绕开)"
                f"(规则:候选不得导入含受保护符号的模块)")
    return None


def _protected_runtime_objects() -> dict[int, str]:
    """已加载的受保护模块里,受保护符号的**身份**(id -> 符号名)。

    只看 `vars(mod)`(模块的 `__dict__`),不用 `getattr`:模块级 `__getattr__`(PEP 562)
    会让 getattr 产生副作用,也可能凭空造出名字。

    受保护的包:**仓库自己的**三个包(`risk`/`strategies`/`vnpy_ctastrategy`)——
    它们是 `PROTECTED_RUNTIME_PACKAGES` 里**不带点**的那几条(带点的是 vnpy 的第三方模块,
    受保护对象不住在那儿)。从常量派生而不是另写一份,免得将来加包时两处静默分叉
    (第 6 轮 codex 的 Low-2)。
    """
    objects: dict[int, str] = {}
    names = _star_import_blocked_symbols()
    own_packages = {package for package in PROTECTED_RUNTIME_PACKAGES if "." not in package}
    for modname, module in list(sys.modules.items()):
        if module is None:
            continue
        if modname.split(".")[0] not in own_packages:
            continue
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        # 模块**对象**本身也算"握着受保护东西":候选
        # `import strategies.trend_atr_strategy as t` 之后 `t.swap_out` 一步可达
        # (第 7 轮 codex 的 High-1)。按名字挑模块不够——挑的是**含受保护符号**的那些模块。
        if any(name in namespace for name in names):
            objects.setdefault(id(module), f"模块 {modname}")
        for name in names:
            obj = namespace.get(name)
            if obj is None:
                continue
            # 只记**对象**的 id:标量(整数/字符串/布尔)的 id 会被复用或撞上别的对象,
            # 拿它当"身份"会让候选命名空间里任何一个同值的小整数都被误判成持有受保护对象
            # (第 6 轮 codex 的 Low-3:这种误判会让**整个进化循环被闲置**)。
            if isinstance(obj, (bool, int, float, str, bytes, complex, type(None))):
                continue
            objects.setdefault(id(obj), name)
    return objects


def _safe_getattr(value, name: str):
    """读候选命名空间里某个值的属性,**任何异常都当成"读不到"**。

    合并 main #26 的模块代理(`_GuardedModule`)之后:候选命名空间里的值可能是代理,
    它对 `__class__`/`__module__`/`__name__` 这类 dunder 一律抛 `ImportError`(不是 AttributeError)——
    `isinstance(代理, X)` 的回退路径会读 `__class__`,`getattr(代理, "__module__", None)` 也只吞
    AttributeError,于是本层(运行期第四层之后的命名空间核对)自己被代理炸掉、整个导入被拒。
    所以这里一律用 `type(value)` 判类型、用本函数读属性,不碰对象自己的 `__class__`。"""
    try:
        return getattr(value, name, None)
    except Exception:                                     # noqa: BLE001 - 代理对 dunder 抛 ImportError
        return None


def _is_kind(value, kinds) -> bool:
    """`isinstance` 的不碰 `__class__` 的版本(只看 `type(value)`)。"""
    return issubclass(type(value), kinds)


def _namespace_values(namespace: dict) -> list[tuple[str, object]]:
    """命名空间里的值:**顶层**的,以及**一层**嵌套(列表/元组/集合/字典)里的。

    一层就够挡住最常见的藏法(`_hoard = [swap_out]`);更深层的藏法由运行期第五层兜住
    (它盯的是数据本身,不是名字)。字典的**键**也要看:键同样能把对象持在手里
    (`{swap_out: 1}`,第 6 轮 codex 的 Low-4)。"""
    values: list[tuple[str, object]] = []
    for name, value in namespace.items():
        values.append((name, value))
        if _is_kind(value, (list, tuple, set, frozenset)):
            values.extend((f"{name}[…]", item) for item in value)
        elif _is_kind(value, dict):
            values.extend((f"{name}[…]", item) for item in value.values())
            values.extend((f"{name}{{键}}", key) for key in value.keys())
    return values


def check_candidate_namespace(namespace: dict) -> list[str]:
    """运行期守卫:候选模块的命名空间里**不得持有受保护对象**。

    通配符导入一次性把被导入模块的公开名字绑进候选命名空间,静态层看不见拿到了什么
    (第 4 轮 G8 复核的 F1)。这一层在候选**导入之后**核对。

    **按身份(值)核对,不按名字核对**——第 4 轮 codex 复核的 High-1 实测:
    只查名字的话,候选 `_hoard = swap_out` 再 `del swap_out` 就把名字删干净了,
    这一层放行而候选**手里仍然握着真的 `swap_out`**。
    按身份核对则:
    - 通配符导入 → 命名空间里那个值**就是**框架的 `swap_out` → 抓到;
    - "改名 + 删除" → 值还在(`_hoard`)→ 抓到;
    - 候选**自己**定义了一个叫 `trading_day` 的函数 → 值不是框架那个 → **放行**
      (这正是 F2 要消灭的那类误伤:按名字核对会把诚实候选整片拒掉)。

    残余(如实记录):藏在两层以上的嵌套里、或经 `functools.partial`/闭包间接持有的,
    这一层看不见;框架对象本身由运行期第四层(身份指纹)与第五层(逐根核对)盯着。"""
    protected = _protected_runtime_objects()
    if not protected:
        # 受保护模块还没被加载(正常流程里模板先导入,它们一定在)。
        # 先把模板与框架模块导入进来,再按身份核对:查不了等于没查。
        _preload_protected_modules()
        protected = _protected_runtime_objects()
    if not protected:
        # 还是拿不到身份表:退回按**名字**核对,但**排除候选自己定义**的同名符号
        # (第 7 轮 codex 的 Low-1:按名字一刀切会把诚实的 `def trading_day()` 整片拒掉,
        #  与静态层"候选自己定义的名字放行"的口径相反;判据是**出处**:
        #  值来自本仓的受保护包 → 拒;来自别的模块 → 候选自己的,放行)。
        blocked = _star_import_blocked_symbols()
        findings: list[str] = []
        for name in sorted(set(namespace) & blocked):
            value = namespace.get(name)
            if _is_kind(value, (bool, int, float, str, bytes, complex, type(None))):
                continue                # 标量:与身份表同一口径,不算受保护对象
            origin = _safe_getattr(value, "__module__")
            if not isinstance(origin, str):
                origin = _safe_getattr(value, "__name__")        # 模块对象
            if isinstance(origin, str) and origin.split(".")[0] not in _OWN_PROTECTED_PACKAGES:
                continue                # 候选自己的同名符号:不是框架那个,放行
            findings.append(f"候选命名空间里出现了受保护符号 {name}"
                            f"(规则:候选命名空间不得持有受保护对象)")
        return findings
    findings: list[str] = []
    for holder, value in _namespace_values(namespace):
        symbol = protected.get(id(value))
        if symbol:
            findings.append(f"候选命名空间里持有受保护对象 {symbol}(在 `{holder}` 下)"
                            f"(规则:候选命名空间不得持有受保护对象)")
    return findings


def screen_source(source: str, c: Constitution | None = None) -> list[str]:
    """静态筛查:返回可疑构造的中文说明;空列表表示未发现问题。**不是安全保证**,只是纵深防御的一层。

    检查项:
    - 类体内覆盖受保护成员或方法
    - 类体内定义受禁 dunder(如 `__setattr__`)或"按名 setter"
    - 自定义 metaclass、使用类装饰器
    - 实例上改写或删除受保护属性
    - 改写受保护的风控模块函数
    - 引用受禁 dunder(裸名或别名)、引用下单/撤单接口(裸名或别名)
    - 引用框架的"按名 setter"(`update_setting`):它能把调用方的字典按键名 setattr 到实例上
    - 受保护参数作为关键字参数(`dict(risk_budget=…)`)或字典键出现

    语法错误在这里报(fail-closed):解析不过就等于没筛查,返回空列表会让"没查"看起来像"查过没问题"。
    静态层不判断"是否重写了模板成员"——模板成员名单要到运行时才知道;那一层由 `check_class` 负责。"""
    c = c or default()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"语法错误: {exc.msg} (第 {exc.lineno} 行)"]

    findings: list[str] = []
    # 类体里的赋值目标:裸局部名豁免(`am`/`bg`)只对**函数体**生效,
    # 类体里定义 `am = ...` 仍然算"覆盖受保护成员"(见 _class_body_target_ids)。
    class_body_targets = _class_body_target_ids(tree)
    # 候选**自己**定义的名字:裸名引用这些名字时,指的是候选自己的对象,
    # 不是框架那个受保护对象(第 6 轮 codex 的 High-1)。运行期那一层已经按**身份**核对
    # (候选自己的同名符号放行),这里与它对齐。
    # 第 7 轮 codex 的 Medium-1/2:改成**按作用域**判定(参数/for/with/except/lambda/海象/match
    # 这些绑定形式也算"候选自己定义的";但某个函数里的同名定义不再豁免模块级的引用)。
    scope = _CandidateScope(tree)
    # 被 import 绑定的名字:`import strategies.trend_atr_strategy as self` 把 `self`
    # 绑成了**模块**,`self.swap_out(...)` 于是长得像实例属性(第 7 轮 codex 的 High-1)。
    import_names = _import_bound_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for name in sorted(_class_body_names(node)):
                if _is_protected(name, c):
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 覆盖了受保护成员 {name}")
                elif name in BUILTIN_FORBIDDEN_DUNDERS:
                    # `class C: __setattr__ = print` 这种**赋值**形式的定义,
                    # 与 `def __setattr__` 一样是候选自定义受禁 dunder,静态层也要拦
                    # (运行时层能抓到,但那时已经在跑候选了)。
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 定义了受禁的 {name}(可绕过属性改写检查)")
                elif name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 定义了受禁的按名 setter {name}(可按键名改写实例属性)")
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_protected(stmt.name, c):
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 重写了受保护方法 {stmt.name}")
                    elif stmt.name in BUILTIN_FORBIDDEN_DUNDERS:
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 定义了受禁的 {stmt.name}(可绕过属性改写检查)")
                    elif stmt.name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 定义了受禁的按名 setter {stmt.name}(可按键名改写实例属性)")
                # __slots__ 里藏受保护名:类体扫描只看赋值目标的名字,
                # `__slots__ = ("risk_budget",)` 的字符串元组看不见(第 7 轮 codex 实测:
                # 静态层放行、运行时层兜住)。静态层也扫一遍,少一个漏一个。
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
                        and stmt.targets[0].id == "__slots__":
                    for element in _string_elements(stmt.value):
                        if _is_protected(element, c):
                            findings.append(
                                f"第 {stmt.lineno} 行: 类 {node.name} 的 __slots__ 含受保护成员 {element}")
            # 自定义元类:元类的 __setattr__ 能在类创建时改写属性,绕开成员比对
            for kw in node.keywords:
                if kw.arg == "metaclass":
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 不得自定义 metaclass")
            # 类装饰器:装饰器可以在类创建后改写它
            for dec in node.decorator_list:
                findings.append(f"第 {node.lineno} 行: 类 {node.name} 不得使用类装饰器")
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and _is_protected(target.id, c) \
                    and not _bare_local_allowed(target.id, id(target), class_body_targets):
                findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
            elif isinstance(target, ast.Attribute):
                violation = _write_violation(target, c, import_names)
                if violation:
                    findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
                elif target.attr in c.protected_module_attrs and _module_attr_owner_ok(
                        target.attr, _attr_path(target) or []) and not _self_instance_attr(target, import_names):
                    findings.append(f"第 {node.lineno} 行: 改写了受保护的风控函数 {target.attr}")
            elif isinstance(target, ast.Subscript):
                # `self.am.close[0] = 999`:改的是被下标取到的那个对象(行情数组本体)
                violation = _write_violation(target.value, c, import_names)
                if violation:
                    findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_protected(target.id, c) \
                        and not _bare_local_allowed(target.id, id(target), class_body_targets):
                    findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
                elif isinstance(target, ast.Attribute):
                    violation = _write_violation(target, c, import_names)
                    if violation:
                        findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
                    elif target.attr in c.protected_module_attrs and _module_attr_owner_ok(
                            target.attr, _attr_path(target) or []) and not _self_instance_attr(target, import_names):
                        findings.append(f"第 {node.lineno} 行: 改写了受保护的风控函数 {target.attr}")
                elif isinstance(target, ast.Subscript):
                    violation = _write_violation(target.value, c, import_names)
                    if violation:
                        findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Attribute):
                violation = _write_violation(target, c, import_names)
                if violation:
                    findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
            elif isinstance(target, ast.Subscript):
                violation = _write_violation(target.value, c, import_names)
                if violation:
                    findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {violation}")
            elif isinstance(target, ast.Name) and _is_protected(target.id, c) \
                    and not _bare_local_allowed(target.id, id(target), class_body_targets):
                findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
        elif isinstance(node, ast.Call):
            # 对受保护容器调用**变更型方法**:`self.am.update_bar(fake_bar)` 会往回测里
            # 插一根假 K 线,而它既不是属性赋值也不是取别名(第 11 轮 codex 的 High 1)。
            violation = _container_mutator_call(node, c)
            if violation:
                findings.append(
                    f"第 {node.lineno} 行: 不得对受保护容器调用变更型方法 {violation}"
                    "(可原地改写行情数据,伪造回测结果)")
        elif isinstance(node, ast.Attribute):
            # 受禁 dunder 的**任何引用**都要拦,不只调用:`f = object.__setattr__` 之后
            # `f(self, "risk_budget", 1e9)` 同样能改写实例属性,而调用点已经看不出是谁了。
            if node.attr in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.attr}(可绕过属性改写检查)")
            elif node.attr in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                # 框架的按名 setter:名字出现在这里就说明候选想按键名改写实例属性。
                findings.append(f"第 {node.lineno} 行: 禁止引用按名 setter {node.attr}(可按键名改写实例属性)")
            elif node.attr in c.forbidden_order_calls:
                # 下单接口同理拦**引用**:`f = self.buy` 之后 `f(1, 1)` 的调用点看不出是下单。
                findings.append(f"第 {node.lineno} 行: 禁止引用下单/撤单接口 {node.attr}")
            elif node.attr in c.protected_state_objects:
                # 受保护的状态对象:连读都不许。只禁"写"会被别名绕过去
                # (`g = self.guard` 之后 `g.max_losses = 999` 不再是对 self.guard 的写入)。
                findings.append(f"第 {node.lineno} 行: 禁止引用受保护的风控状态对象 {node.attr}(改它等于改掉风控底线)")
            elif node.attr in c.protected_entry_points:
                # 风控执行点:候选不重写它,直接调用它就把预算判定与熔断判定一起绕过了。
                findings.append(f"第 {node.lineno} 行: 禁止引用风控执行点 {node.attr}(调用它可绕开预算与熔断判定)")
            elif node.attr in BUILTIN_PROTECTED_READ_ATTRS:
                # 拿到风控函数的**定义**就多一条改写它的路(改函数对象的状态,连名字都不用碰)。
                # 例外:`self.<name>` 是实例自己的属性,改不到模块级函数(第 11 轮 codex 的 Medium 1:
                # `self.trading_day = ...` 曾被误判成"拿到了受保护对象的定义")。
                if not _self_instance_attr(node, import_names):
                    findings.append(f"第 {node.lineno} 行: 禁止引用受保护的风控对象 {node.attr}(拿到定义就能架空风控判定)")
            elif isinstance(node.ctx, ast.Del) and _is_protected(node.attr, c):
                findings.append(f"第 {node.lineno} 行: 删除了受保护属性 {node.attr}")
            else:
                hit = _path_violation(node, c)
                if hit:
                    findings.append(f"第 {node.lineno} 行: 禁止引用受保护属性路径 {hit}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # 裸名字的引用同样要拦:`__setattr__(self, ...)`、`buy(...)`、以及先取别名再调用。
            if node.id in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.id}(可绕过属性改写检查)")
            elif node.id in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用按名 setter {node.id}(可按键名改写实例属性)")
            elif node.id in c.forbidden_order_calls:
                findings.append(f"第 {node.lineno} 行: 禁止引用下单/撤单接口 {node.id}")
            elif node.id in c.protected_entry_points:
                findings.append(f"第 {node.lineno} 行: 禁止引用风控执行点 {node.id}(调用它可绕开预算与熔断判定)")
            elif node.id in BUILTIN_PROTECTED_READ_ATTRS:
                # 候选**自己**定义过这个名字 → 引用的是它自己的对象,不是框架那个:
                # 放行(第 6 轮 codex 的 High-1)。注意豁免只对**裸名**有效:
                # `from risk.market_view import swap_out` 绑的是框架那个,由 ImportFrom 分支照旧拒;
                # `x = risk.market_view.swap_out` 的**属性**那一路也照旧拒。
                # 第 7 轮 codex 的 Medium-1/2:按**作用域**判定——参数/for/with/except/lambda/
                # 海象/match 这些绑定形式也算"候选自己定义的";某个函数里的同名定义
                # 不再豁免**模块级**的引用。
                if not scope.is_defined(node.id, node):
                    findings.append(f"第 {node.lineno} 行: 禁止引用受保护的风控对象 {node.id}(拿到定义就能架空风控判定)")
        elif isinstance(node, ast.Import):
            # `import <模块>`:候选把**整个模块对象**握在手里,一步就能走到模块里的发放口。
            # 第 7 轮 codex 的 High-1 实测:`import strategies.trend_atr_strategy as self` +
            # `self.swap_out(strategy)` 在静态层与运行期**同时放行**,真 ArrayManager 回到候选手里
            # (`strategy.am is 真 ArrayManager` → True,`close[0] = -12345.0` 真的改了真容器)。
            # 根因之二:宪法层有通配符导入的分支(`ImportFrom`),**没有**具名导入整个模块的分支。
            # `from X import 名字` 仍由 ImportFrom 分支逐名裁决,不受这条影响。
            for alias in node.names:
                reason = import_module_finding(alias.name)
                if reason:
                    findings.append(f"第 {node.lineno} 行: {reason}")
        elif isinstance(node, ast.ImportFrom):
            # 从受保护模块里**导入**风控对象:候选不需要它,拿到就多一条改写它的路。
            # (`from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy` 不受影响——
            #  模板基类不在这个名单里,候选本来就要继承它。)
            for alias in node.names:
                if alias.name == "*":
                    # 通配符导入:名字由导入机制自己绑定,逐名裁决看不到它拿到了什么。
                    # 这一路改成"先解析被导入模块的名字集合,再决定放不放"(见 star_import_finding)。
                    reason = star_import_finding(node.module or "", node.level)
                    if reason:
                        findings.append(f"第 {node.lineno} 行: {reason}")
                    continue
                if alias.name in BUILTIN_PROTECTED_READ_ATTRS:
                    findings.append(
                        f"第 {node.lineno} 行: 不得导入受保护的风控对象 {alias.name}(拿到定义就能架空风控判定)")
        elif isinstance(node, ast.keyword) and node.arg:
            # 关键字参数是**按名**传参的另一个入口:`dict(risk_budget=1e12)` 里这个名字
            # 是 ast.keyword.arg,既不是属性赋值也不是字典键,按语法位置筛查看不见。
            # 与 check_params 一样按大小写不敏感比对。
            if _is_protected(node.arg, c) or node.arg.lower() in _protected_lower(c):
                findings.append(f"第 {node.lineno} 行: 关键字参数 {node.arg} 属于受保护项,不得由候选指定")
        elif isinstance(node, ast.Dict):
            # 字典字面量的键:与参数层同一把尺子(大小写不敏感,且受保护**成员**同样算数)。
            # 早先这里只精确比对 protected_parameters,与 check_params 的口径不一致。
            protected_lower = _protected_lower(c)
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if _is_protected(key.value, c):
                        findings.append(f"第 {node.lineno} 行: 字典字面量含受保护项 {key.value}")
                    elif key.value.lower() in protected_lower:
                        findings.append(f"第 {node.lineno} 行: 字典字面量含受保护项 {key.value}(仅大小写不同)")
    # 受保护容器上的**写入**:属性/下标/复合赋值由 `_write_violation` 按整条属性路径判,
    # 变更型方法调用由 `_container_mutator_call` 判,删除由下面这条判。
    # **读取**不再拒(第 4 轮 G8 复核的 F2):只读视图下读到的是拷贝,别名无害——
    # 早先那条"引用流出去就拒"的规则把诚实候选整片拒掉,进化循环因此被闲置。
    for lineno, message in _container_write_findings(tree):
        findings.append(f"第 {lineno} 行: {message}(写入受保护容器, 伪造回测结果)")
    return findings


# ---------- 运行时校验 ----------

def check_params(params: object, c: Constitution | None = None) -> list[str]:
    """校验调用方给的设置字典:不得包含受保护参数(否则风控底线可被请求方绕过)。

    受保护参数按**大小写不敏感**比对:vn.py 的 `update_setting` 直接 `setattr(self, name, ...)`,
    而 Python 属性名区分大小写,`Risk_Budget` 与 `risk_budget` 是两个不同的属性——
    候选只要自定义 `__setattr__` 就能把 `Risk_Budget` 重定向到真正的 `risk_budget`。
    精确匹配会让这条路径完全绕过参数校验。

    **嵌套结构也查**:早先只查顶层,而静态层对任何位置的字典字面量都查——两处口径不一致,
    迟早出现"参数层放行、静态层拒绝"的怪事(第 7 轮 codex 实测:嵌套里的受保护键被放行)。
    嵌套今天还不可利用(`update_setting` 只按顶层名字 setattr,且它本身被禁),
    但口径必须一致,否则下一个入口接上就变成真漏洞。"""
    c = c or default()
    if not isinstance(params, dict):
        return ["params 必须是 JSON 对象"]
    findings: list[str] = []
    _check_param_keys(params, c, "", findings)
    return findings


def _check_param_keys(value: object, c: Constitution, path: str, findings: list[str]) -> None:
    """递归检查参数名(顶层与嵌套)。`path` 只用于说明位置。"""
    protected_lower = _protected_lower(c)
    if isinstance(value, dict):
        for key, sub in value.items():
            where = f"{path}/{key}" if path else str(key)
            if not isinstance(key, str):
                findings.append(f"参数名非法: {key!r}")
            elif _is_protected(key, c):
                findings.append(f"参数 {where} 属于受保护项,不得由调用方指定")
            elif key.lower() in protected_lower:
                findings.append(f"参数 {where} 与受保护参数仅大小写不同,不得由调用方指定")
            _check_param_keys(sub, c, where, findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_param_keys(item, c, f"{path}[{index}]", findings)


_DUNDER_OK = {"__init__"}          # 其余 dunder(__doc__、__module__、__qualname__ 等)由解释器自动生成,不算重写


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name not in _DUNDER_OK


def template_members(template: type) -> set[str]:
    """模板(含其基类,不含 object)上的全部成员名,供清单漂移检查使用(不含自动生成的 dunder)。"""
    names: set[str] = set()
    for klass in template.__mro__:
        if klass is object:
            break
        names |= {n for n in vars(klass) if not _is_dunder(n)}
    return names


def _is_framework_class(klass: type) -> bool:
    """框架/标准库基类:不属于策略家族(要排除掉,否则家族根会算成 abc.ABC)。"""
    return (klass.__module__ in ("abc", "builtins")
            or klass.__name__ == "object"
            or klass.__module__.startswith("vnpy")
            or klass.__name__ in ("CtaTemplate", "Template"))


def family_root(template: type) -> type:
    """模板所属策略家族的根:MRO 里最靠近 object 的**应用层**基类。

    要求继承家族根而不是模板本身:研究与沙盒也会跑日线族(TrendAtrStrategy 的子类),
    只认 15 分钟模板会把它们误拒。

    真实继承链:
        TrendAtr15mStrategy -> TrendAtrStrategy -> CtaTemplate -> ABC -> object
    家族根是 TrendAtrStrategy。早先的实现靠"祖先名字不是 CtaTemplate/Template 且模块不以 vnpy 开头"
    来过滤,于是 abc.ABC(模块名 "abc",不在 vnpy 下)被当成了应用层基类,
    导致:①家族根算成 ABC;②模板自身的成员被误判为"候选重写",每一个真实候选都被拒。"""
    application = [k for k in template.__mro__ if not _is_framework_class(k)]
    return application[-1] if application else template


# 运行时第三层要核对的实例字段:它们既是模板的受保护参数,也是底线值
# (单笔风险预算、ATR 窗口、当日连亏熔断笔数)。
INSTANCE_STATE_FIELDS = ("risk_budget", "atr_window", "max_daily_losses")


def snapshot_instance_state(strategy: object) -> dict[str, object]:
    """取一份实例上的风控状态**快照**(回测开始前取一次,跑完再比)。

    为什么要快照而不是"跟模板常量比":底线值不总是模板常量——
    研究脚本会用更大的 risk_budget 做敏感性实验(`tests/test_strategy_backtest.py` 就是),
    拿模板常量去比会把**合法**的回测判成"底线被改写"。
    快照比的是"这次回测开始时的值"与"结束时的值":
    改写发生在回测途中,快照就抓得住;而设置本身给的值一律放行。"""
    snap: dict[str, object] = {}
    for name in INSTANCE_STATE_FIELDS:
        snap[name] = getattr(strategy, name, None)
    guard = getattr(strategy, "guard", None)
    snap["guard.max_losses"] = getattr(guard, "max_losses", None) if guard is not None else None
    engine = getattr(strategy, "cta_engine", None)
    snap["cta_engine.size"] = getattr(engine, "size", None) if engine is not None else None
    return snap


def _shadowed_template_callables(strategy: object, template: type) -> list[str]:
    """实例上**遮蔽**模板方法的检出。

    候选可以在实例上给模板方法塞一个假实现:
        def entry_signal(self):
            self.get_size = lambda: 0.001      # 真实合约乘数 10
            return 1
    类字典里看不到这个赋值(`check_class` 比的是类字典),实例字段快照也只盯
    `risk_budget`/`atr_window`/`max_daily_losses` 三个底线值——所以四层防线全都放行。
    而 `within_budget(dist, self.get_size(), self.risk_budget)` 读到的是 0.001,
    300 元预算算成 0.003 元,任何止损距离都"在预算内"(第 10 轮自查实测)。

    这里从模板的 MRO 里取出所有**可调用**成员,核对一件事:
    实例字典里不许出现这些名字(实例属性遮蔽了模板方法)。
    白名单里的成员(`entry_signal` 等)例外——候选本来就可以重写它们。

    只查"实例上有没有遮蔽",不比"实现是不是同一个":
    同一个家族里日线模板与 15 分钟模板本来就有各自的实现(同一个成员名、两份代码),
    按"必须等于某个模板的实现"去比,会把合法的日线策略判成"实现被换掉"。
    "实现被换掉"由另外两层负责:类字典层(`check_class`)与指纹层(第四层)。"""
    findings: list[str] = []
    try:
        instance_dict = vars(strategy)
    except TypeError:                                       # noqa: BLE001 - 没有 __dict__ 的对象不看
        return findings
    root = family_root(template)
    names = template_members(template) | template_members(root)
    for name in sorted(names):
        if name in BUILTIN_ALLOWED_OVERRIDES:
            continue
        owner_impl = None
        for klass in template.__mro__:
            if name in vars(klass):
                owner_impl = vars(klass)[name]
                break
        if owner_impl is None or not callable(owner_impl):
            continue                                        # 数据成员遮蔽不致命,方法才致命
        if name in instance_dict:
            findings.append(f"实例上遮蔽了模板方法 {name}(在实例属性里被覆盖)")
    return findings


# 仓库根目录与候选目录:只有**不在候选目录里**的仓库内代码才享有
# "可以重写内部方法"的特权(候选受这条规矩约束,仓库内代码不受)。
_REPO_ROOT = Path(__file__).resolve().parent.parent


# 候选目录不得落在这些仓库目录里。与 `scripts/research_config.py` 的 `RESERVED_DIRS` **必须一致**
# (宪法模块不能 import 它:它依赖 bootstrap/vnpy;一致性由
# `tests/test_constitution_round12.py::test_the_pending_dir_reserved_names_match_the_sandbox_config` 守)。
# `.git` 也在内:候选目录指到版本库里,等于让受保护对象躲进"没人会去看的目录"。
_PENDING_DIR_RESERVED = frozenset({"strategies", "approved", "risk", "trading", "scripts", "tests", "config", ".git"})
_DEFAULT_PENDING_DIR = "pending_strategies"


def _repo_pending_dir() -> Path:
    """候选目录的路径(单一事实来源:配置里的 `paths.pending_dir`)。

    第 12 轮 codex 的 Medium 2:候选目录此前在宪法里**写死**成 `pending_strategies`,
    而配置是**可以改**的,两者不一致时豁免会静默扩大。

    第 13 轮 codex 的 Medium 2:改成读配置之后又出现另一个口径裂缝——沙盒的
    `research_config.load()` 会校验并拒绝 `pending_dir="strategies"`,而这里自己 `json.loads`
    却照收,于是宪法认为的"候选目录"与沙盒实际用的路径错位。现在这里做**同一套校验**
    (相对路径、不得越出仓库、首段不得是保留目录);校验不过就退回默认目录——
    沙盒对这样的配置是整个拒绝加载的,宪法不应当比它更宽松。"""
    import os

    name = _DEFAULT_PENDING_DIR
    config_path = os.environ.get("RESEARCH_SANDBOX_CONFIG") or (
        _REPO_ROOT / "config" / "research_sandbox.json")
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        candidate = str(cfg["paths"]["pending_dir"])
        relative = Path(candidate)
        resolved = (_REPO_ROOT / relative).resolve()
        # 大小写不敏感:Windows/macOS 的文件系统不分大小写,`Strategies` 就是真正的 `strategies/`
        # (第 14 轮 codex 的 Medium 3);带盘符的路径(`C:foo`)也拒。
        resolved_first = resolved.relative_to(_REPO_ROOT.resolve()).parts[0] if resolved != _REPO_ROOT.resolve() else ""
        reserved = {item.lower() for item in _PENDING_DIR_RESERVED}
        if (candidate and not relative.is_absolute() and not relative.drive and ".." not in relative.parts
                and resolved.is_relative_to(_REPO_ROOT.resolve())
                and relative.parts and relative.parts[0].lower() not in reserved
                and resolved_first.lower() not in reserved):
            name = candidate
    except Exception:                                           # noqa: BLE001 - 读不到/不合法就退回默认值
        pass
    return _REPO_ROOT / name


def _is_in_repo_strategy_class(klass: type) -> bool:
    """这个类是不是**仓库自己**的代码(模板、演示策略、测试替身等)。

    第 11 轮 codex 的 Low 3 修复曾把"类字典里出现受保护成员"一律判成越权,
    结果把仓库自己的代码也拒了:
    - `strategies/intraday_demo_strategy.py` **合法地**重写了 `_try_enter` /
      `_process_daily_bar` 这类内部方法;
    - `tests/` 里的测试替身(`AlwaysLong`、`Chatty`……)也照抄了这些方法。
    "候选不得重写受保护成员"这条规矩约束的是**候选**,不是人审过的仓库内代码。

    判据只看**类体里自己的代码落在哪个文件**,而且必须同时满足:
    1. 类声称的模块文件在仓库里、不在候选目录里;
    2. 类体里**每一个**自己写的函数(`__code__.co_filename`)都来自**那个模块文件,
       或它的 MRO 父类所在模块的文件**(别的测试给仓库类打补丁再"还原"时,会把**继承来的**
       函数写进子类自己的字典——那个函数的源文件是父类的文件,不是这个模块自己的);
    3. 至少有一个——没有任何自己的函数的类不豁免(候选写个只有数据属性的类,
       `risk_budget = 1e9`,"全部来自"会因为空集而恒成立)。

    **不认**"模块字典里有没有同名对象"(第 13 轮 codex 的 Medium 1):候选可以
    `Evil.__module__ = "scripts.research_sandbox"; sb.Evil = Evil` 自己满足它。
    也不能用 `inspect.getfile(cls)`——它按 `cls.__module__` 查模块文件,是循环的。
    `__code__.co_filename` 是编译时写进字节码的,候选改不了(它没有 `compile`/`exec`)。

    残余:候选若 `import` 一个仓库里的函数并挂到类上,那个函数的文件与伪造的 `__module__`
    指向的文件相同时会通过——但那条赋值在类体里,静态层按受保护名拦下,这里只是纵深防御。"""
    module = sys.modules.get(getattr(klass, "__module__", "") or "")
    if module is None:
        return False
    filename = getattr(module, "__file__", None)
    if not filename:
        return False
    try:
        module_file = Path(filename).resolve()
    except OSError:                                            # noqa: BLE001 - 路径拿不准就当不可信
        return False
    if not module_file.is_relative_to(_REPO_ROOT):
        return False
    if module_file.is_relative_to(_repo_pending_dir()):
        return False
    return _class_code_comes_only_from(klass, module_file)


def _mro_module_files(klass: type) -> set:
    """类的 MRO 里各个父类所在模块的文件(解析后的路径)。"""
    files = set()
    for base in getattr(klass, "__mro__", ())[1:]:
        module = sys.modules.get(getattr(base, "__module__", "") or "")
        filename = getattr(module, "__file__", None) if module is not None else None
        if not filename:
            continue
        try:
            files.add(Path(filename).resolve())
        except OSError:                                        # noqa: BLE001 - 拿不准就不算
            continue
    return files


def _own_code_objects(klass: type, _depth: int = 0) -> list:
    """类体里自己写的函数的代码对象(解开 staticmethod/classmethod/property,并**递归进嵌套类**)。

    第 14 轮 codex 的 Medium 1:只遍历顶层成员时,候选把自己的代码藏进嵌套类里,
    豁免判据完全看不见它——顶层只剩一个仓库函数,判据就放行了。
    只递归**真正嵌套**的类(`__qualname__` 以外层类名开头),类体里引用的外部类不算。"""
    codes = []
    for value in vars(klass).values():
        if (isinstance(value, type) and _depth < 4 and value is not klass
                and getattr(value, "__qualname__", "").startswith(klass.__qualname__ + ".")):
            codes.extend(_own_code_objects(value, _depth + 1))
            continue
        candidates = [value]
        candidates.append(getattr(value, "__func__", None))
        for accessor in ("fget", "fset", "fdel"):
            candidates.append(getattr(value, accessor, None))
        for item in candidates:
            code = getattr(item, "__code__", None)
            if code is not None:
                codes.append(code)
    return codes


def _class_code_comes_only_from(klass: type, module_file: Path) -> bool:
    """类体里自己的每一个函数都来自 `module_file` 或其 MRO 父类的模块文件,且至少有一个。"""
    codes = _own_code_objects(klass)
    if not codes:
        return False
    allowed = {module_file} | _mro_module_files(klass)
    for code in codes:
        try:
            source_file = Path(code.co_filename).resolve()
        except OSError:                                        # noqa: BLE001 - 拿不准就当不可信
            return False
        if source_file not in allowed:
            return False
    return True


def _runtime_class_dict_violations(strategy: object, template: type, c: Constitution) -> list[str]:
    """运行期**类字典**里出现了不该出现的成员。

    第 11 轮 codex 的 Low 3:`_shadowed_template_callables` 只查**实例字典**——
    候选在运行期给自己的类打补丁(`Cand.get_size = lambda: 0.001`)第 3 层看不见,
    此前只靠静态层按名字兜底。这里在回测结束后再比一次类字典:
    类字典是"运行期"的事实,静态层只看得见源码。

    自动生成的 dunder(`__dict__`/`__weakref__`/`__module__`/`__doc__` 等)不算越权,
    它们是解释器塞进去的,不是候选写的。"""
    findings: list[str] = []
    try:
        candidate_type = type(strategy)
    except Exception:                                           # noqa: BLE001 - 取不到类型就不查
        return findings
    if _is_in_repo_strategy_class(candidate_type):
        return findings                                          # 仓库自己的策略:不受"候选规矩"约束
    root = family_root(template)
    stop_at = template if (isinstance(candidate_type, type) and issubclass(candidate_type, template)) else root
    for klass in candidate_type.__mro__:
        if klass is stop_at:
            break
        if _is_in_repo_strategy_class(klass):
            continue                                            # 仓库自己的类:可以合法重写内部方法
        for name in sorted(vars(klass)):
            if name in _FINGERPRINT_IGNORED_DUNDERS:
                continue
            if name in BUILTIN_ALLOWED_OVERRIDES:
                continue
            if name in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"运行期类 {klass.__name__} 的类字典里出现了受禁的 {name}")
            elif _is_protected(name, c):
                findings.append(f"运行期类 {klass.__name__} 的类字典里出现了受保护成员 {name}")
    return findings


def check_instance_state(strategy: object, template: type | None = None, c: Constitution | None = None,
                         expected_size: float | None = None, snapshot: dict | None = None,
                         expected: dict | None = None) -> list[str]:
    """运行时第三层:回测**跑完之后**核对受保护对象的实例状态。

    静态层拦的是"写法",运行时类校验拦的是"类字典"——两者都看不见**运行途中**
    才发生的改写:属性是在 `entry_signal` 里被赋值的,类字典里根本没有它。
    第 7 轮 codex 实测的两条绕过正是这一类(`self.guard.max_losses = 999`、
    `self.cta_engine.size = 0.001`,两层静态校验全绿)。

    这里核对三件在回测结束时**必须**成立的事:
    1. 熔断器的阈值 == 策略自己用的阈值(候选改了一个没改另一个就露馅);
    2. 引擎的合约乘数 == 回测设置给它的乘数(改小它,止损距离算出来的风险金额就接近 0,
       promotion_gate 的两道回撤门槛随之失效);
    3. 风险预算与 ATR 窗口 == 这**一次**回测开始时的值(`snapshot`),或 == 调用方给的底线(`expected`)。

    检查放在**跑完之后**是有意的:构造完成时这些值都还是对的,改写发生在回测途中。

    `snapshot` 与 `expected` 都可以不给:
    - 给了 `snapshot`:比对"回测前"与"回测后",抓途中改写(回测脚本走这条);
    - 给了 `expected`:比对与调用方声明的底线(候选路径走这条);
    - 两个都不给:退回到"与模板常量比"的底线口径(只用于单元测试与人工排查)。"""
    c = c or default()
    if template is None:
        try:
            template = load_template(c)
        except ConstitutionError:
            return []
    # 只管模板家族的策略:别的策略(随机入场对照、演示策略)有各自的参数,不该被拿模板的底线比
    #
    # 家族判定走 **MRO**(结构)而不是 isinstance:
    # isinstance 的判定逻辑本身是可以被改写的——`abc.ABCMeta.__instancecheck__` 被换掉之后,
    # isinstance 一律返回 False,这一层直接判成"不适用"而放行(第 10 轮 codex 实测)。
    # MRO 由解释器给出,没有可改写的判定入口。
    root = family_root(template)
    try:
        if not any(klass is root for klass in type(strategy).__mro__):
            return []
    except Exception:                                       # noqa: BLE001 - 判定失败就当不适用
        return []
    findings: list[str] = []
    findings.extend(_shadowed_template_callables(strategy, template))
    findings.extend(_runtime_class_dict_violations(strategy, template, c))

    current = snapshot_instance_state(strategy)

    if snapshot is not None:
        for key, before in snapshot.items():
            after = current.get(key)
            if before != after:
                findings.append(f"{key} 在回测途中被改写: {before!r} -> {after!r}")

    if expected is not None:
        for key, want in expected.items():
            after = current.get(key)
            if want != after:
                findings.append(f"{key} 与底线不一致: {after!r} != {want!r}")

    if snapshot is None and expected is None:
        budget = current.get("risk_budget")
        floor = getattr(template, "risk_budget", None)
        if isinstance(budget, (int, float)) and isinstance(floor, (int, float)) and budget != floor:
            findings.append(f"单笔风险预算被改写: {budget!r} != 模板底线 {floor!r}")

        atr_window = current.get("atr_window")
        atr_floor = getattr(template, "atr_window", None)
        if isinstance(atr_window, int) and isinstance(atr_floor, int) and atr_window != atr_floor:
            findings.append(f"ATR 窗口被改写: {atr_window!r} != 模板固定值 {atr_floor!r}")

    guard = getattr(strategy, "guard", None)
    breaker = getattr(strategy, "max_daily_losses", None)
    if guard is not None and isinstance(breaker, int):
        threshold = getattr(guard, "max_losses", None)
        if threshold != breaker:
            findings.append(
                f"当日连亏熔断阈值被改写: 熔断器 {threshold!r} != 策略 {breaker!r}")

    engine = getattr(strategy, "cta_engine", None)
    if expected_size is not None and engine is not None:
        size = getattr(engine, "size", None)
        if size != expected_size:
            findings.append(
                f"合约乘数被改写: {size!r} != 回测设置的 {expected_size!r}"
                "(止损距离算出来的风险金额会随之失真)")
    return findings


# ---------- 运行期第四层:受保护模块与模板类的"身份指纹" ----------

# 这些包里的东西都是候选够得着的改写目标:
#   risk.*            —— 风控模块(架空预算与熔断)
#   strategies.*      —— 策略模板(改掉方法、参数表、乘数)
#   vnpy_ctastrategy  —— CTA 框架本身(改掉回测引擎与按名 setter)
#
# 第 9 轮 codex 实测的 Critical 1 正是这一类:候选三行 monkey-patch 就能架空 300 元预算,
# 而静态层/沙盒层/check_class/check_instance_state **四层全部放行**。
# 结论与第 7 轮一样:**黑名单封的是"已知写法",封不住"能力"**。
# 与其继续枚举写法(别名、`type(cls)`、`sys.modules`……),不如**事后比对身份**:
# 不管候选用哪种写法把 `rg.risk_per_trade` 换掉了,指纹就对不上。
# 运行期第四层(身份指纹)的覆盖范围。第 11 轮 codex 的 Medium 3 实测:
# `vnpy.trader.utility.ArrayManager` 之类框架对象**只靠巧合**被覆盖——
# 模板按名 `from vnpy.trader.utility import ArrayManager` 时,类对象经模板模块共享,
# 于是指纹里恰好出现了 `strategies.xxx.ArrayManager` 这类键;模板哪天改成
# `import vnpy.trader.utility as util`,这层保护就**静默消失**,而历史守卫看不出来。
# 所以这里把模板真正用到的三个框架模块**按路径**显式纳入范围:
# 覆盖不依赖"模板怎么 import",改写法也不会掉保护。
#
# 注意:`vnpy.trader.database` **故意不在范围内**——它的模块级 `database` 全局
# 在第一次 `get_database()` 时会被合法地替换成数据库实例(离线取数脚本会这么做),
# 纳入范围会造成运行期虚假 diff;而研究链路的数据来自 CSV(`load_bars_csv`),
# 数据库根本不在研究数据路径上,篡改它没有意义。
PROTECTED_RUNTIME_PACKAGES = (
    "risk",
    "strategies",
    "vnpy_ctastrategy",
    "vnpy.trader.constant",
    "vnpy.trader.object",
    "vnpy.trader.utility",
)

# 本仓自己的那三个包(`PROTECTED_RUNTIME_PACKAGES` 里**不带点**的几条):
# 一个值只要**出处**在这里,它就不是候选自己的东西。
# 用途:运行期命名空间核对在没有身份表时,按**出处**区分"框架的同名符号"与"候选自己的同名符号"
# (第 7 轮 codex 的 Low-1)。带点的那些是 vnpy 的第三方模块,受保护对象不住在那儿。
_OWN_PROTECTED_PACKAGES = frozenset(
    package for package in PROTECTED_RUNTIME_PACKAGES if "." not in package)


def _module_in_scope(name: str, packages: tuple[str, ...]) -> bool:
    """模块名是否落在受保护范围内。

    支持**点号路径**前缀:`vnpy.trader.utility` 命中 `vnpy.trader.utility` 本身,
    也命中它的子模块;但不会把 `vnpy.trader.utility_extra` 误算进来
    (前缀后面必须跟点)。"""
    for package in packages:
        if name == package or name.startswith(package + "."):
            return True
    return False


# 运行期**新加载**的受保护模块要不要报。
#
# `strategies` 排除:候选偶尔会 import 别的策略模块(不参与本次回测,改它无害);
# `risk` 与 `vnpy*` 是风控与框架本体——运行期才加载并改写它们,指纹本来就看不见
# (第 11 轮 codex 的 Medium 2:候选 import 一个此前没加载过的受保护子模块再改写,
#  `check_protected_state` 报 [] ),所以这里显式报出来。
_PROTECTED_NEW_MODULE_PACKAGES = (
    "risk",
    "vnpy.trader.constant",
    "vnpy.trader.object",
    "vnpy.trader.utility",
    "vnpy_ctastrategy",
)

# 候选执行前必须**先**导入的模块:否则模板模块是候选自己 import 的,
# 快照里根本没有它,候选在导入期把它改写了也拍不到(第 9 轮 codex 实测)。
_PROTECTED_PRELOAD_MODULES = (
    "vnpy_ctastrategy",
    "vnpy_ctastrategy.template",
    "vnpy_ctastrategy.backtesting",
    "vnpy_ctastrategy.engine",
)


def _preload_protected_modules() -> None:
    """把模板与框架模块先导入进来(导入失败不阻断,指纹里没有它就说明它没被用)。"""
    modules = list(_PROTECTED_PRELOAD_MODULES)
    for spec in BUILTIN_ALLOWED_TEMPLATES:
        modules.append(spec.split(":")[0])
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:                                       # noqa: BLE001 - 导不进来就不纳入指纹
            continue


def _safe_token(value: object) -> object:
    """把任意值转成"**可以安全比较**"的标记。

    比对必须**永不抛异常**,否则一次比较失败就被当成"被改写"(误报)或者直接崩掉:
    - numpy 数组的 `==` 返回数组,放进 `bool()` 会抛 ValueError(第 10 轮 codex 的 Low 3);
    - 浮点 nan 与自己不相等,按值比会永远判成"变了"。

    所以:能按值比的按值比(用 repr 解决 nan),不能的一律退化成**身份**标记
    (id 变了就是换了对象)。"""
    if value is None or isinstance(value, (bool, int, str, bytes)):
        return value
    if isinstance(value, float):
        return ("f", repr(value))               # nan 按 repr 相等
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_safe_token(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return ("set", frozenset(_safe_token(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple((_safe_token(k), _safe_token(v)) for k, v in value.items()))
    # numpy 数组/标量:能按**内容**比就按内容比,否则原地改写看不见
    # (第 11 轮 codex 的 Medium 4:闭包里装 numpy 数组时 `arr[0] = 5` 检测不到,
    #  因为对象身份没变而退化成了身份标记)。
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return ("arr", _safe_token(tolist()))
        except Exception:                                       # noqa: BLE001 - 取不到内容就退回身份
            return ("id", id(value))
    return ("id", id(value))


def _protected_fingerprint_value(value: object) -> tuple[str, object]:
    """把受保护对象的值转成"可比对的指纹"。

    容器(list/dict/set)取**副本**:候选在**原地**改模板的 `parameters` 列表时,
    对象身份不变,只比身份会漏掉。

    函数比的不只是**身份**,还有**状态**(第 10 轮 codex 实测的 Critical):
        within_budget.__defaults__ = (0.0,)
    把 volume 的默认值改成 0,单笔风险金额恒为 0,300 元预算形同虚设——
    而函数对象**还是那个对象**,只比身份完全看不见。
    所以这里连同 `__code__`、默认参数、关键字默认参数、闭包单元格的内容一起比。"""
    if isinstance(value, (classmethod, staticmethod)):
        return _protected_fingerprint_value(value.__func__)
    if isinstance(value, types.FunctionType):
        cells = value.__closure__ or ()
        return ("func", (
            value,                       # 留住函数对象本身:既保证 id 不会被复用,也比身份更严格
            id(value.__code__),
            _safe_token(value.__defaults__),
            _safe_token(value.__kwdefaults__),
            _safe_token(tuple(_cell_token(cell) for cell in cells)),
        ))
    if isinstance(value, list):
        return ("seq", _safe_token(tuple(value)))
    if isinstance(value, tuple):
        return ("seq", _safe_token(tuple(value)))
    if isinstance(value, dict):
        return ("map", _safe_token(dict(value)))
    if isinstance(value, (set, frozenset)):
        return ("set", _safe_token(set(value)))
    # numpy 数组:比**内容**。对象身份不变、内容变了(原地改写)也算"被改写"
    # (第 11 轮 codex 的 Medium 4:退化成身份标记时 `arr[0] = 5` 完全看不见)。
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return ("arr", _safe_token(tolist()))
        except Exception:                                       # noqa: BLE001 - 取不到内容就退回身份
            return ("id", value)
    return ("id", value)


def _cell_token(cell: object) -> object:
    """闭包单元格的内容:空单元格没有内容,按标记处理。"""
    try:
        return cell.cell_contents                        # type: ignore[attr-defined]
    except ValueError:
        return ("empty",)


def _protected_values_same(before: tuple[str, object], after: tuple[str, object]) -> bool:
    if before[0] != after[0]:
        return False
    if before[0] == "id":
        return before[1] is after[1]
    try:
        return bool(before[1] == after[1])
    except Exception:                                           # noqa: BLE001 - 比不了就当被改过(fail-closed)
        return False


# 指纹里**忽略**的 dunder:纯元数据,框架不会在运行期动它们,收了只是平白多出几千个键。
#
# 其余 dunder 一律纳入指纹。早先的写法是"跳过所有 dunder",于是:
# - 候选改写类的 `__getattribute__`(让 `self.get_size()` 返回假的)拍不到;
# - 候选改写 `__instancecheck__`(把 isinstance 判定废掉)也拍不到
#   (第 10 轮 codex 实测:四层防线全部放行)。
_FINGERPRINT_IGNORED_DUNDERS = frozenset({
    "__dict__", "__weakref__", "__module__", "__qualname__", "__doc__",
    "__builtins__", "__annotations__", "__abstractmethods__",
    "__firstlineno__", "__static_attributes__", "__type_params__", "__parameters__",
})


def _fingerprint_skip(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name in _FINGERPRINT_IGNORED_DUNDERS


def snapshot_protected_state() -> dict[str, tuple[str, object]]:
    """给受保护的模块与类拍一张**身份指纹**。

    覆盖:受保护包下**所有已加载模块**的公开属性,以及其中每个**类**的成员
    (dunder 也在内,只放过 `_FINGERPRINT_IGNORED_DUNDERS` 里的纯元数据)。
    模板类(`TrendAtr15mStrategy.get_size` 等)与风控模块(`risk_per_trade` 等)都在里面。

    每个模块本身也留一个标记键(`"risk.risk_guard"` → 该模块对象):
    没有它就看不出"这个模块是**运行期才加载**的"——第 11 轮 codex 的 Medium 2 实测:
    候选在自己执行期间 import 一个此前没加载过的受保护子模块再改写它,
    指纹**完全看不见**(快照里根本没有那个模块)。"""
    _preload_protected_modules()
    snapshot: dict[str, tuple[str, object]] = {}
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not _module_in_scope(name, PROTECTED_RUNTIME_PACKAGES):
            continue
        snapshot[name] = ("id", module)
        for attr, value in list(vars(module).items()):
            if _fingerprint_skip(attr):
                continue
            snapshot[f"{name}.{attr}"] = _protected_fingerprint_value(value)
            if isinstance(value, type):
                for member, member_value in list(vars(value).items()):
                    if _fingerprint_skip(member):
                        continue
                    snapshot[f"{name}.{attr}.{member}"] = _protected_fingerprint_value(member_value)
    return snapshot


def check_protected_state(before: dict, after: dict) -> list[str]:
    """比对两张身份指纹:被替换、被删除或被塞进新成员的受保护对象一律报出来(fail-closed)。

    这是运行期第四层,补的是"候选在导入期/回测途中改写框架对象"这条路——
    静态层只按名字判断,拦不住别名与"根不是名字"的写法。

    "新增"只对**类成员**报:新导入一个模块不算越权(候选偶尔会 import 别的策略模块),
    但往受保护的类上挂一个新成员(`TrendAtr15mStrategy.get_size = ...` 之前没有这个键)
    正是第 9 轮 codex 实测的改写模板类的手法。"""
    findings: list[str] = []
    for key, old in sorted(before.items()):
        if key not in after:
            findings.append(f"受保护对象 {key} 被删除")
            continue
        if not _protected_values_same(old, after[key]):
            findings.append(f"受保护对象 {key} 被改写")
    for key in sorted(after):
        if key in before:
            continue
        value = after[key][1]
        if isinstance(value, types.ModuleType):
            # 运行期**新加载**的受保护模块(第 11 轮 codex 的 Medium 2):
            # 快照里此前根本没有它,改写它指纹就看不见——这里显式报出来。
            if _module_in_scope(key, _PROTECTED_NEW_MODULE_PACKAGES):
                findings.append(f"受保护模块 {key} 在运行期被加载(此前不在指纹里,可能被改写)")
            continue
        # 只报"往**已存在**的类上挂新成员"(父键在 before 里)。
        # 候选导入一个新模块/新类不算越权(候选偶尔会 import 别的策略模块),
        # 而 `TrendAtr15mStrategy.get_size = ...` 的父键
        # `...TrendAtr15mStrategy` 在 before 里——正是第 9 轮 codex 实测的改写手法。
        parent = key.rsplit(".", 1)[0]
        # parent 还得**本身是类**(before 里它的指纹是 ("id", <class>)):
        # 否则候选导入一个新模块(`strategies.foo`)会被误判成"往受保护对象上挂东西"。
        if parent in before and before[parent][0] == "id" and isinstance(before[parent][1], type):
            findings.append(f"受保护对象 {key} 被新增")
    return findings


def check_class(candidate: type, template: type, c: Constitution | None = None) -> list[str]:
    """运行时校验候选类:必须继承模板所属家族;不得重写白名单以外的**模板成员**。

    覆盖三处:候选自身的类体、候选与模板之间的中间基类、以及类创建后动态打的补丁。"""
    c = c or default()
    findings: list[str] = []
    root = family_root(template)
    if not (isinstance(candidate, type) and issubclass(candidate, root)):
        return [f"候选类必须继承模板家族 {root.__name__}"]

    # 自定义元类:元类的 __setattr__ 能在类创建时改写属性,绕开成员比对
    if type(candidate) is not type(template):
        findings.append(f"候选类不得自定义 metaclass(实际 {type(candidate).__name__})")

    members = template_members(template) | template_members(root)
    # 候选直接继承模板时,模板自身的成员不算"候选重写";候选只继承家族根时,家族根也不算。
    stop_at = template if (isinstance(candidate, type) and issubclass(candidate, template)) else root
    chain: list[type] = []
    for klass in candidate.__mro__:
        if klass is stop_at:
            break
        chain.append(klass)

    for klass in chain:
        for name in sorted(vars(klass)):
            if name in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"候选定义了受禁的 {name}({klass.__name__}):可绕过属性改写检查")
                continue
            if name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                findings.append(f"候选定义了受禁的按名 setter {name}({klass.__name__}):可按键名改写实例属性")
                continue
            # 元类自动注入的成员(ABCMeta 给每个类塞 _abc_impl)不是候选"重写"的产物
            if name in METACLASS_GENERATED:
                continue
            if _is_dunder(name):
                continue
            if name in c.allowed_overrides:
                continue
            if _is_protected(name, c):
                findings.append(f"候选重写了受保护成员 {name}({klass.__name__})")
            elif name in members:
                findings.append(f"候选重写了模板成员 {name},但它不在允许重写的白名单里({klass.__name__})")
    return findings


# ---------- 运行时第五层:行情数据完整性 ----------

# ArrayManager 的四个价格数组 ↔ BarData 的四个价格字段。
#
# 第 13 轮 codex 的 Low 2:成交量/成交额/持仓量数组同样可被改写
# (将来任何用到成交量的指标都会被带偏),所以一并核对。
_PRICE_ARRAY_FIELDS = (
    ("open_array", "open_price"),
    ("high_array", "high_price"),
    ("low_array", "low_price"),
    ("close_array", "close_price"),
    ("volume_array", "volume"),
    ("turnover_array", "turnover"),
    ("open_interest_array", "open_interest"),
)


def snapshot_market_data(strategy: object) -> dict:
    """拍一份行情容器的基准(累计根数 + 容器里价格取值的集合)。

    基准必须在 **`on_init` 之前**(构造完成时)拍:拍在预热之后,候选在 `on_init` 里塞的
    假 K 线就成了"基线"(第 13 轮自查实测)。日线族预热喂进去的真实 K 线,由调用方把
    引擎 `load_bar` 实际交出的那些并入"合法喂入"(见 `scripts/backtest_lib.py`)。"""
    am = getattr(strategy, "am", None)
    if am is None:
        return {}
    count = getattr(am, "count", None)
    size = getattr(am, "size", None)
    if not isinstance(count, int) or not isinstance(size, int):
        return {}
    values: dict[str, set] = {}
    for array_name, _ in _PRICE_ARRAY_FIELDS:
        array = getattr(am, array_name, None)
        if array is None:
            continue
        try:
            values[array_name] = {float(value) for value in array}
        except (TypeError, ValueError):
            values[array_name] = set()
    return {"count": count, "size": size, "values": values}


def check_market_data_integrity(strategy: object, bars, baseline: dict | None = None) -> list[str]:
    """核对策略手里的行情容器与引擎喂过的 K 线一致(不可变宪法的运行时第五层)。

    静态层按名字封,总有封不到的写法:视图(`self.am.close[:]`)、dunder 写入口
    (`self.am.close.__setitem__(...)`)、别名(`x = self.bg`)。
    第 12 轮 codex 的 High 1 实测:这三条都能让候选把 ATR 从 40 压到 2.86,
    ¥300 单笔风险预算当场失效(诚实模板 0 成交,候选成交 2 笔),而四层防线全绿。

    这一层封的是**数据本身**,不是写法:回测跑完之后,容器里的 K 线必须还是
    引擎喂进去的那些。候选只要插一根、改一根、或把价格换成没出现过的数,这里就报出来。

    口径:
    - `baseline` 是 **`on_init` 之前**拍的基准(累计根数与当时容器里的价格取值);
      `bars` 是引擎**合法喂过的全部 K 线**(预热喂入 + 回放喂入);
    - 回放根数 = 回放后的累计根数 - 基准累计根数;多于合法喂入的根数 → 有 K 线是凭空来的;
    - 容器里的每个价格都必须**真实出现过**(基准里已有的 + 引擎喂过的)——改价、插假 K 线都露馅;
    - 回放根数恰好等于喂入根数(一 K 线一更新)时,再逐值比一遍:容器尾部必须与喂入尾部相等。
    """
    findings: list[str] = []
    am = getattr(strategy, "am", None)
    if am is None:
        return findings
    count = getattr(am, "count", None)
    size = getattr(am, "size", None)
    if not isinstance(count, int) or not isinstance(size, int):
        return findings
    fed = list(bars or [])
    base_count = 0
    base_values: dict = {}
    if baseline:
        base_count = int(baseline.get("count", 0) or 0)
        base_values = baseline.get("values") or {}
        base_size = baseline.get("size")
        if isinstance(base_size, int):
            if size != base_size:
                findings.append(f"行情容器的容量在回测途中被改写了(原来 {base_size},现在 {size})")
            size = base_size                               # 比对窗口用拍下来的容量,不读活的
    replayed = count - base_count
    if replayed > len(fed):
        findings.append(
            f"行情容器里回放阶段累计了 {replayed} 根 K 线,而引擎只喂了 {len(fed)} 根(有 K 线是凭空来的)")
        return findings
    pools: dict[str, set[float]] = {}
    for array_name, field in _PRICE_ARRAY_FIELDS:
        pool = set(base_values.get(array_name) or ())
        for bar in fed:
            try:
                pool.add(float(getattr(bar, field, 0.0)))
            except (TypeError, ValueError):
                continue
        pools[array_name] = pool
    window = min(size, len(fed))
    exact = replayed == len(fed) and len(fed) > 0
    for array_name, field in _PRICE_ARRAY_FIELDS:
        array = getattr(am, array_name, None)
        if array is None:
            continue
        try:
            actual = [float(value) for value in array]
        except (TypeError, ValueError):
            continue
        unknown = [value for value in actual if value not in pools[array_name]]
        if unknown:
            findings.append(
                f"行情容器的 {array_name} 里有 {len(unknown)} 个价格不是引擎喂过的"
                "(有人在回测途中改写了行情数据)")
            continue
        if not exact or window <= 0:
            continue
        tail = actual[-window:]
        expected = [float(getattr(bar, field, 0.0)) for bar in fed[-window:]]
        if tail != expected:
            findings.append(
                f"行情容器的 {array_name} 与引擎喂进来的 {field} 对不上"
                "(有人在回测途中改写了行情数据)")
    return findings


class MarketDataWatch:
    """运行时第五层的**逐根**核对:每处理完一根 K 线,就核对一次行情容器。

    第 13 轮 codex 的 Critical 1:只在回测结束时核对末态,于是"改 -> 用 -> 改回"的瞬时改写
    完全看不见——候选把 ATR 从 40.00 压到 5.07、成交 30 笔往返(诚实模板 0 成交),
    结束时三个数组与原始行情逐值相同,五层全绿。

    逐根核对之后,瞬时改写必须**撑过一根 K 线的处理**(它要影响下一次 ATR 计算,
    就得在这根 K 线结束时仍然留在容器里),下一次核对必然露馅。
    (只在**同一次**调用里改了又改回、不跨 K 线的写法,影响不到受保护的 ATR——
    ATR 是在 `entry_signal` 之前算好的;能影响的只有候选自己本来就能决定的信号。)

    口径同终态核对:基准在 `on_init` 之前拍;`warmup` 是引擎 `load_bar` 实际交出的预热 K 线;
    `history` 是回放的 K 线。回放根数(累计根数 - 基准)恰等于合法喂入根数时逐值比尾部,
    否则(分钟族合成日线、日线族丢弃不完整的日线)只核对"每个取值都真实出现过"。
    首次发现问题之后锁存,不再重复报。"""

    def __init__(self, strategy: object, baseline: dict | None, warmup, history) -> None:
        import numpy as np

        self._np = np
        self._strategy = strategy
        self._am = getattr(strategy, "am", None)
        warmup = list(warmup)
        self._k = len(warmup)
        self._dead = self._am is None
        if self._dead:
            return
        feed = warmup + list(history)
        baseline = baseline or {}
        self._base_count = int(baseline.get("count", 0) or 0)
        base_values = baseline.get("values") or {}
        # `size` 在构造时捕获,之后任何改动本身就是 finding(第 14 轮 codex 的 Medium 2:
        # 终态核对读的是活的 am.size,改小它会缩小比对窗口)
        size = baseline.get("size") if baseline.get("size") is not None else getattr(self._am, "size", None)
        self._size = size if isinstance(size, int) else 0
        # 族判定(第 14 轮 codex 的 High 1):
        # 旧口径拿 `replayed == k` 当强/弱模式的**开关**——候选让 `am.count` 落后一根,
        # 强模式永久关闭,而"count 与喂入根数不同步"这件事没有任何一层报告。
        # 现在把"每根 K 线容器恰好更新一次"当成**被核对的契约**,而不是从 count 里猜:
        # - 模板的 `on_bar`(候选不能覆盖它,它受保护)每喂一根就更新一次容器;
        # - 日线族(喂入的是日线)同样每根更新一次。
        # 其余(分钟族合成日线、逐分钟喂入的演示策略)更新节奏不规则,只能核对"取值出现过"。
        # (第一版用"开头连续 3 根增量都是 1"来猜,被 `intraday_demo` 打脸:它前几根都是 +1,
        #  第 60 根却是 0——节奏是**家族的属性**,不能从前几根里推断。)
        self._prev_count = getattr(self._am, "count", None)
        self._family = self._updates_every_bar(strategy, history)
        self._offset: int | None = None
        self._fields: list[str] = []
        self._buf: dict = {}
        self._pool: dict = {}
        for array_name, field in _PRICE_ARRAY_FIELDS:
            if getattr(self._am, array_name, None) is None:
                continue
            values = np.array([float(getattr(bar, field, 0.0)) for bar in feed], dtype=float)
            extra = np.array(sorted(base_values.get(array_name) or ()), dtype=float)
            self._fields.append(array_name)
            self._buf[array_name] = values
            self._pool[array_name] = np.unique(np.concatenate([values, extra]))

    @staticmethod
    def _updates_every_bar(strategy: object, history) -> bool:
        """这个策略是不是"每喂一根 K 线就更新一次行情容器"的家族。

        契约由**模板自己的两个方法**决定:`on_bar`(把 K 线交给谁)与 `_process_daily_bar`
        (真正调用 `am.update_bar` 的地方)。两者都是受保护成员,候选不能覆盖,所以对候选来说
        这两个方法就是模板的那份;而仓库里的演示策略(`IntradayDemoStrategy` 重写了
        `_process_daily_bar`,有些 K 线不更新容器)节奏不同,只能走宽松口径。"""
        try:
            template = load_template()
            root = family_root(template)
            cls = type(strategy)
            if getattr(cls, "_process_daily_bar", None) is not getattr(root, "_process_daily_bar", object()):
                return False
            on_bar = getattr(cls, "on_bar", None)
            if on_bar is not None and on_bar is getattr(template, "on_bar", object()):
                return True
        except Exception:                                       # noqa: BLE001 - 判不出就当不是
            return False
        try:
            from vnpy.trader.constant import Interval

            history = list(history)
            return bool(history) and getattr(history[0], "interval", None) == Interval.DAILY
        except Exception:                                       # noqa: BLE001
            return False

    def step(self) -> list[str]:
        """引擎又处理完一根 K 线之后调用;返回这一次的发现(首次之后恒为空)。"""
        if self._dead:
            return []
        self._k += 1
        try:
            findings = self._check()
        except Exception as exc:                                # noqa: BLE001 - 核对自己出错也是失败
            findings = [f"行情容器的逐根核对自己出错了(fail-closed): {type(exc).__name__}"]
        if findings:
            self._dead = True
        return findings

    def _check(self) -> list[str]:
        np = self._np
        am = getattr(self._strategy, "am", None)
        if am is not self._am:
            return ["行情容器在回测途中被换成了另一个对象"]
        size = getattr(am, "size", None)
        if self._size and size != self._size:
            return [f"行情容器的容量在回测途中被改写了(原来 {self._size},现在 {size})"]
        count = getattr(am, "count", None)
        if not isinstance(count, int):
            return []
        replayed = count - self._base_count
        k = self._k
        if replayed > k:
            return [f"行情容器里回放阶段累计了 {replayed} 根 K 线,而引擎只喂了 {k} 根(有 K 线是凭空来的)"]
        delta = count - self._prev_count if isinstance(self._prev_count, int) else None
        self._prev_count = count
        if self._family and delta is not None:
            if delta != 1:
                return [f"第 {k} 根 K 线之后,行情容器的累计根数这一根变化了 {delta}"
                        "(逐根更新的家族每根 K 线应恰好 +1;有人在回测途中改写了容器的计数)"]
            if self._offset is None:
                self._offset = replayed - k                    # 预热丢弃了不完整日线时不为 0,之后要求恒定
            elif replayed - k != self._offset:
                return [f"第 {k} 根 K 线之后,行情容器的累计根数与引擎喂入根数的偏移变了"
                        "(有人在回测途中改写了容器的计数)"]
        window = min(self._size, k)
        exact = bool(self._family) and self._offset == 0 and replayed == k and window > 0
        findings: list[str] = []
        for array_name in self._fields:
            array = np.asarray(getattr(am, array_name, None), dtype=float)
            if exact:
                if not np.array_equal(array[-window:], self._buf[array_name][k - window:k]):
                    findings.append(
                        f"第 {k} 根 K 线之后,行情容器的 {array_name} 与引擎喂进来的对不上"
                        "(有人在回测途中改写了行情数据)")
            elif not np.isin(array, self._pool[array_name]).all():
                findings.append(
                    f"第 {k} 根 K 线之后,行情容器的 {array_name} 里出现了引擎没喂过的取值"
                    "(有人在回测途中改写了行情数据)")
        return findings
