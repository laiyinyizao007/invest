"""批准账本(**只读**):「某份字节是否被人批准过」的唯一读法。

为什么把「读」单独抽成一个模块:
  - 批准账本(`data/research-control/approvals.json`)是人工批准的**唯一落点**。
    研究侧(`scripts/research_sandbox.py`)写它;运行侧(`trading/`)在装载策略时必须**复核**它。
  - 两侧不能共用同一个模块:研究侧依赖重(静态筛查、沙盒导入、路径解析),运行侧刻意
    不 import 研究侧的东西(研究侧刻意不 import bootstrap/vnpy,免得把交易凭据拖进研究进程;
    反过来运行侧也不该拖进筛查那一整套)。
  - 所以只把「读」这一半抽到这里。

不变式(有测试锁住,见 `tests/test_approval_ledger.py`):
  - **本模块不含任何写入口**:写账本的入口唯一在 `research_sandbox.record_approval`
    (低层唯一写入者)。收菜动作也只读这里、从不写账本。
  - **读取失败一律 fail-closed**:账本坏了就抛 `ApprovalLedgerError`,绝不回退成
    「没有批准」或「默认批准」。**账本文件不存在**是「空」不是「错」——那是"还没批准过任何东西"。
  - 本模块不 import 研究侧、不 import vnpy、不 import bootstrap:它只是读一个 JSON。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ApprovalLedgerError(ValueError):
    """账本读不了/格式不对。**只读模块里没有别的失败模式**——命中就抛这个。"""


def file_sha256(path: Path | str) -> str:
    """文件内容的 sha256(小写十六进制)。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_approvals(approvals_file: Path | str) -> dict:
    """读批准账本,返回 `{sha256: {"name":..., "at":...}}`。

    - 文件不存在 → `{}`(没有任何批准;这不是错误)
    - 读不了/不是合法 JSON/顶层不是对象 → `ApprovalLedgerError`(fail-closed)
    """
    approvals_file = Path(approvals_file)
    if not approvals_file.exists():
        return {}
    try:
        data = json.loads(approvals_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalLedgerError(f"批准记录无法读取,拒绝继续(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise ApprovalLedgerError("批准记录格式错误(应为对象)")
    return data


def is_approved_sha(sha256: str, approvals_file: Path | str) -> bool:
    """按**哈希**查批准记录(不读被批准的文件)。

    用在"我知道自己手上这份字节的哈希"的地方——比如回测子进程:父进程算好哈希传下来,
    执行前再读一次文件校验「当前内容 == 这个哈希」,于是被执行的字节一定是被批准的那一份,
    不必为了查审批再读一遍文件(两处读之间文件若被改,查的就是另一个版本的内容)。
    """
    return str(sha256).strip().lower() in load_approvals(approvals_file)


def is_approved_path(path: Path | str, approvals_file: Path | str) -> bool:
    """按**文件当前内容**查批准记录(会读一次文件算哈希)。"""
    return file_sha256(path) in load_approvals(approvals_file)


def load_collected(collected_file: Path | str) -> dict:
    """读「已收菜」记录:`{sha256: {"name":..., "collected_at":...}}`。

    为什么单独一个文件:批准账本 `approvals.json` 的**写入口必须唯一**
    (`research_sandbox.record_approval`;有测试用 AST 锁住「没有别的写入者」)。
    收菜是另一件事——它证明的是「这段字节**经过了收菜那道关口**」——
    所以另开一个文件,写入口也只有收菜脚本一个。

    运行时要求这个记录存在:人工 `cp` 把文件拷进 `approved/` 就绕不过收菜那道关口
    (收菜时会把源码打给人看、并要求粘贴完整哈希)。
    """
    path = Path(collected_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalLedgerError(f"收菜记录无法读取,拒绝继续(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise ApprovalLedgerError("收菜记录格式错误(应为对象)")
    return data


def load_promotion(promotion_file: Path | str) -> dict:
    """读晋级门槛的批准文件(`data/state/promotion_approval.json`)。

    这是**第二道**与「人批准」独立的门:人工审阅挡得住「代码里有坏东西」,
    挡不住「这策略没有统计优势」。今天没有任何候选通过过门槛,
    所以这道门实际上把「从 `approved/` 装载」这条路**关着**(见 Issue #37)。
    """
    path = Path(promotion_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalLedgerError(f"晋级批准文件无法读取,拒绝继续(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise ApprovalLedgerError("晋级批准文件格式错误(应为对象)")
    return data
