"""实盘准入:连接生产 API 之前必须有"通过晋级门槛"的批准记录(纯 Python,不依赖 vn.py)。

为什么需要:`risk/promotion_gate.py::check_gate` 只是一个函数,以前没有任何运行时代码消费它,
"策略未通过样本外门槛就不得上实盘"只存在于文档里。这里把它变成启动前的硬检查:
- 目标是**生产 API**(`CTP_ENVIRONMENT=实盘` 且交易前置不是 SimNow)时,必须存在批准文件;
- 批准文件只能由 `scripts/record_promotion.py` 在 `check_gate` 返回空列表时写出;
- SimNow 与期货公司看穿式测试环境(`测试`)不受限——它们是用来验证管道的。

这是防"误操作"的闸门,不是防"恶意"的:有写权限的人可以手写批准文件。
决策与局限见 docs/adr/20261111-discipline-enforcement.md。
"""
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from risk.promotion_gate import CandidateMetrics, check_gate

APPROVAL_RELATIVE = Path("data") / "state" / "promotion_approval.json"
# **装载门**的批准记录:与准入门是**两份独立的文件**(Issue #37)。
#
# 为什么拆开:两道门问的不是一件事——
#   准入门(上面那份):「这个账号/这套系统能不能连生产 API」;
#   装载门(这份):      「这份**代码**配不配被装载执行」。
# 共用一份记录的话,「过了一次门槛」会同时满足两件事,而且没法单独撤销其中一件。
# 这份额外绑定**品种 / 字节 / 那一次评估**,所以它只能授权它被评估过的那一份代码。
LOAD_APPROVAL_RELATIVE = Path("data") / "state" / "approved_promotion.json"


def check_live_admission(environment: str, on_simnow: bool, approval_path: Path) -> tuple[bool, str]:
    """返回 (是否放行, 说明)。on_simnow 由调用方用 scripts/simnow_guard 判断。"""
    if environment != "实盘":
        return True, f"柜台环境={environment!r}(非生产 API),不需要实盘准入批准。"
    if on_simnow:
        return True, "目标是 SimNow 模拟盘,不需要实盘准入批准。"
    if not approval_path.exists():
        return False, (f"目标是生产 API,但没有实盘准入批准文件 {approval_path}。"
                       "候选策略必须先通过晋级门槛:python scripts/record_promotion.py --metrics <指标.json> "
                       "--strategy <名称> --approver <批准人>(未全部通过时不会写出文件)。")
    try:
        record = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"实盘准入批准文件无法读取({exc!r}),按未批准处理。"
    if not isinstance(record, dict) or record.get("gate_failures") != [] or not record.get("approved_at") \
            or not str(record.get("approver", "")).strip() or not str(record.get("strategy", "")).strip():
        return False, "实盘准入批准文件内容不完整或记录了未通过的门槛项,按未批准处理。"
    return True, f"实盘准入批准:策略 {record['strategy']},批准人 {record['approver']},时间 {record['approved_at']}。"


def write_approval(path: Path, strategy: str, metrics: CandidateMetrics, approver: str, now: datetime,
                   sha256: str | None = None) -> list[str]:
    """跑晋级门槛;只有全部通过才写**准入门**的批准文件。返回未通过的条目(空列表 = 已写出)。

    `sha256`:这份批准对应**哪一段字节**。给了它,装载门才能核对
    「批准通过记录」与「实际要跑的那份字节」是同一份(见 `write_load_approval`)。
    """
    failures = check_gate(metrics)
    if failures:
        return failures
    record = {
        "strategy": strategy,
        "approver": approver,
        "approved_at": now.isoformat(timespec="seconds"),
        "gate_failures": [],
        "metrics": asdict(metrics),
    }
    if sha256:
        record["sha256"] = str(sha256).strip().lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return []


def write_load_approval(path: Path, strategy: str, key: str, sha256: str, run_id: str,
                        metrics: CandidateMetrics, approver: str, now: datetime) -> list[str]:
    """写**装载门**的批准记录(独立于准入门)。

    三个绑定缺一不可(Issue #37):
    - `key`:这份通过是**哪个品种**的(C15 / CADJ / MAADJ …)。同一份代码换个品种不通用。
    - `sha256`:**哪一段字节**。换个文件不能蹭别人的通过记录。
    - `run_id`:**哪一次评估**。数据一更新,同一次通过的含义就变了;记下 run_id 才追得回来。

    门槛**再跑一遍**:写这份记录时也必须全过(不允许「先过门槛、再补记录」)。
    """
    failures = check_gate(metrics)
    if failures:
        return failures
    for name, value in (("key", key), ("sha256", sha256), ("run_id", run_id)):
        if not str(value).strip():
            return [f"装载门的批准记录缺少 {name}:它绑定的是「哪份字节、哪个品种、哪一次评估」"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(sha256).strip().lower()):
        return [f"sha256 格式不对(应为 64 位小写十六进制):{sha256!r}"]
    record = {
        "strategy": strategy,
        "key": str(key).strip(),
        "sha256": str(sha256).strip().lower(),
        "run_id": str(run_id).strip(),
        "approver": approver,
        "approved_at": now.isoformat(timespec="seconds"),
        "gate_failures": [],
        "metrics": asdict(metrics),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return []
