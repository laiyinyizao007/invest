"""从 `approved/` 装载策略:批准账本是**唯一**授权来源,任何一项校验不过就拒绝装载。

这道门回答的是整个系统里最要紧的一个问题:**这份代码凭什么有权被运行时执行?**
答案只有一个:人读过它、批准了它,而批准**绑定到字节**(sha256)。

四道校验(缺一不可,顺序即依赖):
  1. **位置**:文件必须在 `approved/` 目录内(解析后的父目录就是它,防 `..` 与符号链接逃逸);
  2. **授权**:这份字节的 sha256 必须在批准账本里——`approved/` 里放一个没批准过的文件,直接拒绝;
  3. **合规**:静态筛查(宪法)必须通过,设置字典里不得夹带受保护参数;
  4. **同一性**:哈希、筛查、编译、执行必须基于**同一份字节**。

第 4 条与研究侧 `scripts/research_api.py::import_candidate` 同一口径:
  - 分开打开文件会留下 TOCTOU 窗口(算哈希的那份与执行的那份可能不是同一份);
  - 用 `spec.loader.exec_module()` 载入时,Python 可能从 `__pycache__` 取时间戳有效的旧 `.pyc`,
    于是「人批准的、算过哈希的、过了筛查的源码」与「实际执行的字节码」不是一回事。
  所以这里**只读一次字节**,编译并执行的就是那一份,`.pyc` 没有机会生效。

失败一律抛 `ApprovedLoadError`,调用方**不得**回退到默认策略——那是 fail-closed 的全部意义。
"""
from __future__ import annotations

import hashlib
import re
import types
from pathlib import Path

from risk import approval_ledger
from risk import constitution as const

# 批准目录里的文件名与类名都必须是「普通名字」:不含路径分隔符、不含 `..`
SAFE_FILE_NAME = re.compile(r"^[a-z0-9_]{1,40}\.py$")
SAFE_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ApprovedLoadError(Exception):
    """装载被拒。调用方必须拒绝启动,不得降级。"""


def _read_approved_bytes(approved_dir: Path, file_name: str) -> tuple[Path, bytes]:
    """定位并读取 `approved/` 里的一个文件(只读一次字节)。"""
    if not isinstance(file_name, str) or not SAFE_FILE_NAME.fullmatch(file_name):
        raise ApprovedLoadError(f"批准目录里的文件名不合法: {file_name!r}")
    approved_dir = Path(approved_dir)
    path = (approved_dir / file_name).resolve()
    # 解析后的父目录必须就是 approved/:`..`、绝对路径与符号链接都会在这里露馅
    if path.parent != approved_dir.resolve():
        raise ApprovedLoadError(f"策略文件不在批准目录内: {file_name}")
    if not path.is_file():
        raise ApprovedLoadError(f"批准目录里没有这个文件: {file_name}")
    return path, path.read_bytes()


def load_approved_strategy(
    *,
    approved_dir: Path | str,
    approvals_file: Path | str,
    file_name: str,
    class_name: str = "Candidate",
    setting: dict | None = None,
    template: type | None = None,
) -> type:
    """装载并返回策略类。任何一项校验不过 → `ApprovedLoadError`。

    `setting` 是要传给策略的设置字典:一并做受保护参数校验(风控底线不得由调用方指定)。
    """
    if not isinstance(class_name, str) or not SAFE_CLASS_NAME.fullmatch(class_name):
        raise ApprovedLoadError(f"策略类名不合法: {class_name!r}")

    path, raw = _read_approved_bytes(Path(approved_dir), file_name)
    sha = hashlib.sha256(raw).hexdigest()

    # ② 授权:这份字节必须在批准账本里
    try:
        ledger = approval_ledger.load_approvals(approvals_file)
    except approval_ledger.ApprovalLedgerError as exc:
        raise ApprovedLoadError(f"批准账本不可读,拒绝装载(fail-closed): {exc}") from exc
    if sha not in ledger:
        raise ApprovedLoadError(
            f"{file_name} 的当前内容没有人工批准记录,拒绝装载"
            f"(批准绑定的是字节;改过代码就要重新批准并重新收菜)")

    # ③ 合规:静态筛查 + 设置字典校验
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApprovedLoadError(f"策略文件不是合法 UTF-8: {exc}") from exc
    findings = const.screen_source(source)
    if findings:
        raise ApprovedLoadError("静态筛查未通过,拒绝装载:\n  " + "\n  ".join(findings))
    if setting is not None:
        param_findings = const.check_params(setting)
        if param_findings:
            raise ApprovedLoadError("策略设置未通过校验,拒绝装载:\n  " + "\n  ".join(param_findings))

    # 模板:宪法指定的那一个(人维护的骨架)
    if template is None:
        try:
            template = const.load_template()
        except const.ConstitutionError as exc:
            raise ApprovedLoadError(f"策略骨架模板不可用,拒绝装载(fail-closed): {exc}") from exc

    # ④ 同一性:编译并执行的就是上面那一份字节
    try:
        code = compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise ApprovedLoadError(f"策略文件语法错误: {exc}") from exc
    module = types.ModuleType("approved_strategy")
    module.__file__ = str(path)
    module.__package__ = ""
    # 第四层(受保护对象的身份指纹):执行**之前**拍基准,执行**之后**重新拍并比对。
    # 候选在模块体里改写框架对象(给模板类挂成员、换掉风控函数)在这一层露馅。
    # 快照必须在执行之前拍——拍在之后就成了「自己跟自己比」,必然通过。
    protected_before = const.snapshot_protected_state()
    try:
        exec(code, module.__dict__)                       # noqa: S102 - 这里就是要执行批准过的字节
    except Exception as exc:                              # noqa: BLE001 - 候选自己抛错也要变成「拒绝」而不是裸异常
        raise ApprovedLoadError(f"策略文件执行失败: {exc!r}") from exc
    tampered = const.check_protected_state(protected_before, const.snapshot_protected_state())
    if tampered:
        raise ApprovedLoadError("策略文件在装载期间改写了受保护对象,拒绝装载:\n  "
                                + "\n  ".join(tampered))

    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        raise ApprovedLoadError(f"策略文件里没有类 {class_name}: {file_name}")
    if not issubclass(cls, template):
        raise ApprovedLoadError(
            f"类 {class_name} 不是策略骨架({template.__name__})的子类,拒绝装载")
    return cls


def check_approved_consistency(
    *,
    approved_dir: Path | str,
    approvals_file: Path | str,
) -> list[str]:
    """核对 `approved/` 里每个文件与批准账本是否仍然一致(运行中/收菜后自检用)。

    返回人类可读的问题列表;空列表表示一致。
    """
    problems: list[str] = []
    approved_dir = Path(approved_dir)
    if not approved_dir.is_dir():
        return problems
    try:
        ledger = approval_ledger.load_approvals(approvals_file)
    except approval_ledger.ApprovalLedgerError as exc:
        return [f"批准账本不可读(fail-closed): {exc}"]
    for path in sorted(approved_dir.glob("*.py")):
        sha = approval_ledger.file_sha256(path)
        if sha not in ledger:
            problems.append(f"{path.name}:当前内容不在批准账本里(被改过或从未批准)")
    return problems
