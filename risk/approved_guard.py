"""从 `approved/` 装载策略:批准账本是**唯一**授权来源,任何一项校验不过就拒绝装载。

这道门回答的是整个系统里最要紧的一个问题:**这份代码凭什么有权被运行时执行?**
答案只有一个:人读过它、批准了它,而批准**绑定到字节**(sha256)。

五道校验(缺一不可,顺序即依赖):
  1. **位置**:文件必须在 `approved/` 目录内(解析后的父目录就是它,防 `..` 与符号链接逃逸);
  2. **授权**:这份字节的 sha256 必须在批准账本里——`approved/` 里放一个没批准过的文件,直接拒绝;
  3. **合规**:静态筛查(宪法)必须通过;**类级校验**(候选不得重写白名单以外的模板成员)必须通过;
     设置字典里不得夹带**风控底线**参数;
  4. **同一性**:哈希、筛查、编译、执行必须基于**同一份字节**;
  5. **第四层**:执行前后各拍一次受保护对象的身份指纹并比对。

第 4 条与研究侧 `scripts/research_api.py::import_candidate` 同一口径:
  - 分开打开文件会留下 TOCTOU 窗口(算哈希的那份与执行的那份可能不是同一份);
  - 用 `spec.loader.exec_module()` 载入时,Python 可能从 `__pycache__` 取时间戳有效的旧 `.pyc`,
    于是「人批准的、算过哈希的、过了筛查的源码」与「实际执行的字节码」不是一回事。
  所以这里**只读一次字节**,编译并执行的就是那一份,`.pyc` 没有机会生效。

第 5 条的**绑定方式**是关键(前三轮 codex 复核实测出三条越过的绕过,逐条堵上):
  ① 闸门函数必须在 exec **之前**绑成局部引用,装载时调的是「导入时那个真函数」,
     不是「调用时模块上的那个属性」——否则候选可以自己
     `import risk.constitution as _c; _c.check_protected_state = lambda *a, **k: []`
     把第四层换成空函数(结论恒为「没被改写」),甚至换成
     「**被调用时把原函数装回去**」的假函数(连事后身份核对都骗得过);
  ② exec **之后**还要逐一核对「模块上的属性还是不是原来那几个」;
  ③ 静态层先封:候选只要写闸门函数的名字,或用 `setattr(obj, "名字", …)` 按字符串写,
     就在 exec **之前**被拒(见 `constitution.BUILTIN_PROTECTED_GATE_ATTRS`)。

⚠️ **但这不等于「运行侧已安全」**:在同进程、完整真实内建的环境里执行任意 Python 代码,
候选可以用 `sys._getframe(1).f_locals[...]` 直接改装载器的局部变量(Python 3.13 的 PEP 667 写穿)。
真正的边界是**人工审批 + 隔离**,而隔离目前**只在研究侧**(受限内建 + 容器)——
运行侧没有。详见 ADR `20261115-approved-sandbox-closure`「已知取舍」第 8 条。

失败一律抛 `ApprovedLoadError`,调用方**不得**回退到默认策略——那是 fail-closed 的全部意义。
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import threading
import types
from pathlib import Path

from risk import approval_ledger
from risk import constitution as const

# 批准目录里的文件名与类名都必须是「普通名字」:不含路径分隔符、不含 `..`
# (与 `scripts/research_config.py` 的 `candidates.name_pattern` 是同一条规则,
#  两边共用这一份字面量——三处各写一遍迟早漂移;`tests/test_approval_ledger.py` 锁住一致性)
SAFE_FILE_NAME = re.compile(r"^[a-z0-9_]{1,40}\.py$")
SAFE_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# 设置字典里**具名**豁免的操作参数。
#
# 为什么豁免:宪法的 `BUILTIN_PROTECTED_PARAMETERS` 里还有 `history_vt_symbol`,
# 它只是「预热用哪个历史合约」的操作参数,不是风控底线——运行时会从 `--history` 传它,
# 若一并禁掉,实盘正常配置(带历史预热)就永远装不上(第 1 轮 codex 复核实测)。
#
# 为什么是**具名豁免**而不是「只校验底线那几项」:后者是 fail-open——
# 将来新加的受保护参数会被**静默丢弃**而不是被拒(第 3 轮 codex 的 N4)。
SETTING_EXEMPT_PARAMS = ("history_vt_symbol",)


class ApprovedLoadError(Exception):
    """装载被拒。调用方必须拒绝启动,不得降级。"""


# ---- 闸门函数:在**模块导入时**绑成常量 ----
#
# 为什么不能等到装载时再去 `const.` 上取:候选改写的是**模块属性**,
# 而我们在装载时取到的就已经是它换上去的那个了(第 1 轮 codex 实测的绕过:
# `const.check_protected_state = lambda *a, **k: []` → 第四层恒为「没被改写」)。
#
# 绑成常量之后:① 我们用的始终是「导入时那个真函数」;
# ② 装载前后逐一核对「模块上的属性还是不是那个真函数」——
# 候选换掉了就当场发现,并把整个进程标记为**已中毒**(下面 `_POISONED`)。
#
# 清单:不只第四层那两个,筛查、类级校验、参数校验、账本读取**全部**在内——
# 候选能把它们一个个换掉,而它们正是「拒绝」这件事的执行者。
# 清单用**不可变的 tuple**(而不是 dict):dict 是可变对象,候选一句
# `import risk.approved_guard as _g; _g._CANONICAL_GATES["x"] = fake` 就能改它。
# tuple 改不了元素(整体替换由下面的默认参数绑定挡)。
_CANONICAL_GATES: tuple[tuple[str, object], ...] = (
    ("snapshot_protected_state", const.snapshot_protected_state),
    ("check_protected_state", const.check_protected_state),
    ("screen_source", const.screen_source),
    ("check_class", const.check_class),
    ("check_params", const.check_params),
    ("load", const.load),                    # 宪法的清单装载器:换掉它 = 下一份候选的筛查被清空
    ("load_template", const.load_template),  # 骨架装载器:换掉它 = 换一个不受白名单约束的骨架
    ("BUILTIN_ALLOWED_TEMPLATES", const.BUILTIN_ALLOWED_TEMPLATES),
)
_CANONICAL_LEDGER: tuple[tuple[str, object], ...] = (
    ("load_approvals", approval_ledger.load_approvals),
    ("file_sha256", approval_ledger.file_sha256),
    ("is_approved_sha", approval_ledger.is_approved_sha),
)
# 进程级中毒标记:一次装载被拒之后,本进程**不再允许**装载任何候选。
# 理由:被拒的候选可能已经改写了进程里的东西(改写发生在 exec 期间,拒绝发生在比对之后),
# 那次改写**留在进程里**。若允许第二次装载,第二次就会绑到已被换掉的闸门上。
# 运行时是「一进程一次装载」,失败即退出;这里把这条约定**变成代码强制**。
_POISONED = False


def _gates_intact(gates: tuple[tuple[str, object], ...] | None = None,
                  ledger: tuple[tuple[str, object], ...] | None = None) -> list[str]:
    """核对所有闸门函数还是导入时那批(候选有没有换掉它们)。

    清单用**默认参数**绑死:写成模块全局的话,候选一句
    `import risk.approved_guard as _g; _g._CANONICAL_GATES = {...}` 就把它换掉了,
    而核对会拿新清单去比——等于没核对(第 3 轮 codex 指出)。
    """
    gates = _CANONICAL_GATES if gates is None else gates
    ledger = _CANONICAL_LEDGER if ledger is None else ledger
    broken: list[str] = []
    for name, canonical in gates:
        if getattr(const, name, None) is not canonical:
            broken.append(f"risk.constitution.{name}")
    for name, canonical in ledger:
        if getattr(approval_ledger, name, None) is not canonical:
            broken.append(f"risk.approval_ledger.{name}")
    return broken


def _refuse_poisoned(reason: str) -> None:
    """记下「本进程已中毒」并拒绝。"""
    global _POISONED
    _POISONED = True
    raise ApprovedLoadError(reason)


def _read_approved_bytes(approved_dir: Path, file_name: str) -> tuple[Path, bytes]:
    """定位并读取 `approved/` 里的一个文件(只读一次字节)。"""
    if not isinstance(file_name, str) or not SAFE_FILE_NAME.fullmatch(file_name):
        raise ApprovedLoadError(f"批准目录里的文件名不合法: {file_name!r}")
    approved_dir = Path(approved_dir)
    path = (approved_dir / file_name).resolve()
    # `resolve()` 会跟随符号链接并消掉 `..`:解析后的**父目录**必须就是 approved/。
    # 于是「指向 approved/ 外面」的符号链接(以及 `..`、绝对路径)都会在这里露馅;
    # 指向 approved/ **内部**另一文件的符号链接会被放行——读的是目标文件的字节,
    # 哈希按目标内容算,账本命中才放行,没有绕过。
    if path.parent != approved_dir.resolve():
        raise ApprovedLoadError(f"策略文件不在批准目录内: {file_name}")
    if not path.is_file():
        raise ApprovedLoadError(f"批准目录里没有这个文件: {file_name}")
    return path, path.read_bytes()


def _load_constitution() -> const.Constitution:
    """读宪法的**清单文件**(不是内置默认值)——两侧用同一份清单,免得漂移。"""
    try:
        return const.load()
    except const.ConstitutionError as exc:
        raise ApprovedLoadError(f"宪法清单不可用,拒绝装载(fail-closed): {exc}") from exc


def load_template_named(name: str) -> type:
    """按 `模块:类` 取一个骨架;名字必须在宪法的**内置白名单**里。

    为什么要有这个:宪法指定的默认模板是 `strategies.trend_atr_15m_strategy:TrendAtr15mStrategy`,
    而那个骨架**仅支持研究回测**(`on_tick` 直接抛异常)。要在交易运行时里装载候选,
    就得能选一个**实盘可用**的骨架——目前是日线的 `strategies.trend_atr_strategy:TrendAtrStrategy`。
    名字仍然走白名单,不能随便指定一个类(否则继承约束形同虚设)。

    导入动作交给 `constitution.load_template` 做(它是宪法自己的入口):
    本模块因此**不出现** `importlib.import_module`——导入也是执行代码,
    宪法历史守卫会把「自己 import 的模块」判成一个需要走第四层的执行入口。
    """
    if not isinstance(name, str) or name not in const.BUILTIN_ALLOWED_TEMPLATES:
        raise ApprovedLoadError(
            f"骨架不在宪法的内置白名单里: {name!r}(可选:{', '.join(const.BUILTIN_ALLOWED_TEMPLATES)})")
    constitution = _load_constitution()
    try:
        return const.load_template(dataclasses.replace(constitution, template=name))
    except const.ConstitutionError as exc:
        raise ApprovedLoadError(f"骨架无法载入(fail-closed): {name}: {exc}") from exc


def _exec_in_thread(code, namespace: dict, snapshot_protected_state, check_protected_state,
                    box: dict) -> None:
    """执行候选的字节,并在**同一个作用域**里走完第四层。

    为什么在独立线程里跑(第 4 轮 codex 实测):在同线程里跑,候选可以用
    `getattr(sys._getframe(1), 'f_' + 'locals')['check_protected_state'] = fake`
    **直接改装载器的局部变量**(Python 3.13 的 PEP 667 写穿)——
    连「exec 之前绑好的局部引用」都拦不住,第四层能被完全中和。

    独立线程里 `sys._getframe(1)` 拿到的是**那个线程**的帧,不是装载器的帧:
    主线程的局部变量够不着。实测:线程里的候选改不到主线程的局部变量。

    线程不是安全边界:候选仍然能改写**共享的**模块状态(例如 `risk.risk_guard`),
    那一路由第四层的指纹比对兜着(比对用的是绑定好的真函数,候选劫持不了)。
    帧劫持、闭包单元、`__code__` 改写这一类「够到装载器自身」的招,线程隔离能挡。

    异常(含 `SystemExit`)原样带回去重抛——失败模式因此只有一种。
    """
    before = snapshot_protected_state()
    try:
        exec(code, namespace)                              # noqa: S102 - 这里就是要执行批准过的字节
    except BaseException as exc:                          # noqa: BLE001 - 原样带回调用方重抛
        box["error"] = exc
    else:
        tampered = check_protected_state(before, snapshot_protected_state())
        box["tampered"] = tampered
        # 结论**必须在这里被读掉**(宪法历史守卫按这条核对执行点):
        # 「比对做了却把结论丢掉」等于没做。
        if tampered:
            box["tampering"] = True


def load_approved_strategy(
    *,
    approved_dir: Path | str,
    approvals_file: Path | str,
    file_name: str,
    class_name: str = "Candidate",
    setting: dict | None = None,
    template_name: str | None = None,
    collected_file: Path | str | None,
    promotion_file: Path | str | None,
    expected_key: str | None = None,
) -> tuple[type, str]:
    """装载并返回 `(策略类, 这份字节的 sha256)`。任何一项校验不过 → `ApprovedLoadError`。

    返回哈希是为了让调用方能把它派生进**状态命名空间**(`strategy_name`):
    换批准版本就换命名空间,旧版本的持久化状态不会被新版本继承。

    `setting` 是要传给策略的设置字典:对**整个** setting 做受保护参数校验,
    只对 `SETTING_EXEMPT_PARAMS` 里那一个具名的操作参数豁免。

    `expected_key`:本次要跑的品种(如 C15)。给了就与装载门记录里的 key 核对——
    记录只授权它被评估过的那个品种。

    `collected_file` / `promotion_file`:第三、第四道门——
    「这段字节**经过了收菜那道关口**」(收菜时会把源码打给人看)与
    「这个策略**通过了晋级门槛**」。两者都是独立于「人批准过」的记录。

    ⚠️ 这两个参数是**必填**的(可以显式传 `None` 表示「本调用不做这道检查」):
    给它们默认值就等于「忘了传就悄悄跳过一道门」——那是 fail-open。
    运行时永远传真实的路径;只有低层测试会显式传 `None`。

    **被投毒之后不再装载**:一次被拒的候选可能已经把进程改坏了(改写发生在 exec 期间,
    拒绝发生在比对之后),那次改写留在进程里。所以一旦发现闸门或受保护对象被改写,
    本进程就被标记为**已中毒**,之后任何装载一律拒绝(要装别的候选就起新进程)。
    正常情况下(没有投毒)可以多次装载。
    """
    if _POISONED:
        raise ApprovedLoadError(
            "本进程已经尝试装载过一次候选,不再允许第二次"
            "(上一次可能已改写进程状态;请起新进程)")
    broken = _gates_intact()
    if broken:
        _refuse_poisoned("装载闸门本身已被改写,拒绝装载(本进程已中毒):" + ", ".join(broken))

    # 闸门函数**在 exec 之前**绑成局部引用:候选执行完再经模块属性去取,
    # 取到的可能已经是候选换上去的那个(第 1/3 轮 codex 实测的两条绕过:
    # ① 换成空函数 → 结论恒为「没被改写」;
    # ② 换成「调用时把原函数装回去」的假函数 → 事后身份核对也看不出来)。
    # 局部引用在 exec **之前**就绑好了,候选的代码在另一个 module dict 里,改不到它。
    load_constitution = _CANONICAL_GATES[5][1]
    load_template = _CANONICAL_GATES[6][1]
    screen_source = _CANONICAL_GATES[2][1]
    check_params = _CANONICAL_GATES[4][1]
    check_class = _CANONICAL_GATES[3][1]
    snapshot_protected_state = _CANONICAL_GATES[0][1]
    check_protected_state = _CANONICAL_GATES[1][1]
    load_approvals = _CANONICAL_LEDGER[0][1]

    try:
        if not isinstance(class_name, str) or not SAFE_CLASS_NAME.fullmatch(class_name):
            raise ApprovedLoadError(f"策略类名不合法: {class_name!r}")

        path, raw = _read_approved_bytes(Path(approved_dir), file_name)
        sha = hashlib.sha256(raw).hexdigest()

        # ② 授权:这份字节必须在批准账本里
        try:
            ledger = load_approvals(approvals_file)
        except approval_ledger.ApprovalLedgerError as exc:
            raise ApprovedLoadError(f"批准账本不可读,拒绝装载(fail-closed): {exc}") from exc
        if sha not in ledger:
            raise ApprovedLoadError(
                f"{file_name} 的当前内容没有人工批准记录,拒绝装载"
                f"(批准绑定的是字节;改过代码就要重新批准并重新收菜)")

        # ②b 「已收菜」记录:收菜那道关口会把源码打给人看、并要求粘贴完整哈希。
        # 人工 `cp` 把文件拷进 approved/ 就**没有**这条记录 → 拒绝。
        if collected_file is not None:
            collected = approval_ledger.load_collected(collected_file)
            if sha not in collected:
                raise ApprovedLoadError(
                    f"{file_name} 没有「已收菜」记录,拒绝装载"
                    f"(收菜是那道把源码打给人看的关口;人工拷贝绕过不了它)")

        # ②c 晋级门槛通过记录(装载门):与「人批准」独立的第四道门。
        # 人工审阅挡得住「代码里有坏东西」,挡不住「这策略没有统计优势」。
        # 这份记录绑定**品种 / 字节 / 那一次评估**(Issue #37):它只授权
        # 它被评估过的那一份代码、那个品种。
        if promotion_file is not None:
            promotion = approval_ledger.load_promotion(promotion_file)
            if not isinstance(promotion, dict):
                raise ApprovedLoadError("晋级门槛通过记录格式不对,拒绝装载")
            record_sha = str(promotion.get("sha256") or "")
            record_key = str(promotion.get("key") or "").strip()
            record_run = str(promotion.get("run_id") or "").strip()
            if not record_sha:
                raise ApprovedLoadError(
                    f"{file_name} 没有晋级门槛通过记录,拒绝装载"
                    f"(门槛没过就不是能上实盘的策略;先跑 scripts/record_promotion.py)")
            if record_sha != sha:
                raise ApprovedLoadError(
                    f"晋级门槛通过记录对应的是另一份字节,拒绝装载"
                    f"(记录 {record_sha[:12]}… ≠ 本文件 {sha[:12]}…)")
            if not record_key:
                raise ApprovedLoadError("晋级门槛通过记录没有品种(key),拒绝装载")
            if not record_run:
                raise ApprovedLoadError("晋级门槛通过记录没有 run_id(哪一次评估),拒绝装载")
            if expected_key and expected_key != record_key:
                raise ApprovedLoadError(
                    f"晋级门槛通过记录对应的是品种 {record_key},"
                    f"而本次要跑的是 {expected_key},拒绝装载")
            if promotion.get("gate_failures") != []:
                raise ApprovedLoadError("晋级门槛通过记录里记录了未通过的门槛项,拒绝装载")

        # ③ 合规:静态筛查 + 参数校验
        # **用清单文件里的那一份**(const.load()),不是内置默认值:
        # 清单文件的设计就是「只能追加」,将来往 JSON 里加保护项,运行侧也得认。
        constitution = load_constitution()
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApprovedLoadError(f"策略文件不是合法 UTF-8: {exc}") from exc
        findings = screen_source(source, constitution)
        if findings:
            raise ApprovedLoadError("静态筛查未通过,拒绝装载:\n  " + "\n  ".join(findings))
        if setting is not None:
            # **对整个 setting 校验**,只对 `history_vt_symbol` 这一个**具名**操作参数豁免。
            # (早先的写法是「只留风控底线那三项再校验」——宪法是「只能追加」的,
            #  将来新加的受保护参数会被**静默丢弃**而不是被拒,那是 fail-open。)
            payload = {k: v for k, v in setting.items() if k not in SETTING_EXEMPT_PARAMS}
            param_findings = check_params(payload, constitution)
            if param_findings:
                raise ApprovedLoadError("策略设置未通过校验,拒绝装载:\n  "
                                        + "\n  ".join(param_findings))

        # 模板:人维护的骨架。默认取宪法指定的那一个;
        # `template_name` 用来选**实盘可用**的骨架(默认那个是仅研究回测的 15m 骨架)。
        # 骨架只能按**名字**选(名字走宪法的内置白名单):没有「直接传一个类进来」的口子。
        if template_name:
            template = load_template_named(template_name)
        else:
            try:
                template = load_template(constitution)
            except const.ConstitutionError as exc:
                raise ApprovedLoadError(f"策略骨架模板不可用,拒绝装载(fail-closed): {exc}") from exc
        # 骨架自报「仅研究」→ 拒绝在**交易运行时**装载:这类骨架的 on_tick 直接抛异常
        # (见 strategies/trend_atr_15m_strategy.py),策略会在实盘第一根 tick 上静默死掉——
        # 失败方向安全(不下单),但错误信息极具误导性,不如在装载时就拒绝并说清楚。
        if getattr(template, "research_only", False):
            raise ApprovedLoadError(
                f"策略骨架 {template.__name__} 仅支持研究回测(它的 on_tick 直接抛异常),"
                f"不能在交易运行时装载;实盘需要一个人维护的实盘骨架")

        # ④ 同一性:编译并执行的就是上面那一份字节
        try:
            code = compile(source, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:
            raise ApprovedLoadError(f"策略文件语法错误: {exc}") from exc
        module = types.ModuleType("approved_strategy")
        module.__file__ = str(path)
        module.__package__ = ""
        # 第四层(受保护对象的身份指纹):执行**之前**拍基准,执行**之后**重新拍并比对。
        # 两次调用用的都是上面绑好的**真函数**,候选换不掉它们。
        box: dict = {}
        thread = threading.Thread(
            target=_exec_in_thread,
            args=(code, module.__dict__, snapshot_protected_state, check_protected_state, box),
            name="approved-strategy-exec", daemon=True)
        thread.start()
        thread.join()
        if "error" in box:
            exc = box["error"]
            # **必须兜住 SystemExit / KeyboardInterrupt**:候选 `raise SystemExit(0)` 若逃出去,
            # 交易进程会以**退出码 0** 结束——`restart: on-failure` 不重启、监控看着像「正常停止」,
            # 而且零告警(第 2 轮 codex 实测)。研究侧写的是 `except (Exception, SystemExit)`,同一条口径。
            raise ApprovedLoadError(f"策略文件执行失败: {exc!r}") from exc
        tampered = box.get("tampered", [])
        # 核对**所有**闸门有没有被拆:筛查、类级校验、参数校验、账本读取、第四层——
        # 候选能把它们一个个换掉,而它们正是「拒绝」这件事的执行者。
        broken = _gates_intact()
        if broken:
            _refuse_poisoned("策略文件在装载期间改写了装载闸门本身,拒绝装载(本进程已中毒):"
                             + ", ".join(broken))
        if tampered:
            _refuse_poisoned("策略文件在装载期间改写了受保护对象,拒绝装载(本进程已中毒):\n  "
                             + "\n  ".join(tampered))

        cls = getattr(module, class_name, None)
        if not isinstance(cls, type):
            raise ApprovedLoadError(f"策略文件里没有类 {class_name}: {file_name}")
        if not issubclass(cls, template):
            raise ApprovedLoadError(
                f"类 {class_name} 不是策略骨架({template.__name__})的子类,拒绝装载")
        # ③(补)类级校验:静态层不判断「是否重写了模板成员」——模板成员名单要到运行时才知道,
        # 那一层由 `check_class` 负责。研究侧有这层(`research_api.import_candidate`),
        # 运行侧原先漏了 → 研究侧拒的候选运行侧会收(第 1 轮 codex 复核发现)。
        class_findings = check_class(cls, template, constitution)
        if class_findings:
            raise ApprovedLoadError("候选重写了不允许重写的模板成员,拒绝装载:\n  "
                                    + "\n  ".join(class_findings))
        return cls, sha
    except ApprovedLoadError:
        raise
    except BaseException as exc:                          # noqa: BLE001
        # 闸门 API 的失败模式**只有一种**:任何别的异常(属性被删→AttributeError、
        # 属性被置 None→TypeError、候选自己抛的怪东西)都变成「拒绝」,
        # 而不是裸异常逃出去(第 3 轮 codex 实测)。
        raise ApprovedLoadError(f"策略装载异常: {exc!r}") from exc


def check_approved_consistency(
    *,
    approved_dir: Path | str,
    approvals_file: Path | str,
) -> list[str]:
    """核对 `approved/` 里每个文件与批准账本是否仍然一致(收菜后与启动时自检)。

    返回人类可读的问题列表;空列表表示一致。
    """
    problems: list[str] = []
    broken = _gates_intact()
    if broken:
        return ["装载闸门已被改写(fail-closed):" + ", ".join(broken)]
    approved_dir = Path(approved_dir)
    if not approved_dir.is_dir():
        return problems
    try:
        ledger = _CANONICAL_LEDGER[0][1](approvals_file)
    except approval_ledger.ApprovalLedgerError as exc:
        return [f"批准账本不可读(fail-closed): {exc}"]
    for path in sorted(approved_dir.glob("*.py")):
        sha = _CANONICAL_LEDGER[1][1](path)
        if sha not in ledger:
            problems.append(f"{path.name}:当前内容不在批准账本里(被改过或从未批准)")
    return problems
