"""测试段使用登记:`--test` 只许消费一次。

登记文件 `config/test_segment_usage.json` 入库,使「测试段已被看过」成为可审计的事实而不只是人工纪律。
先登记、后运行(fail-closed):即使随后脚本崩溃,该测试段也算已使用,因为结果可能已被看到。
没有"强制重用"开关——开关会让登记形同虚设;确需重置(例如数据扩充后出现全新的样本外段)
必须手工编辑登记文件并在 ADR 中说明,这个动作会留在 git 历史里。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# **不 import bootstrap**:bootstrap 会把 vnpy 交易栈与 .env 拖进来,
# 于是连 pytest 的全局 conftest(它 import 本模块)都要求装交易环境——
# CI 上的守卫因此在**收集阶段**就中止,一次都没跑过(见 docs/adr 与
# tools/check_constitution_history.py 的注释)。这里自己算仓库根:
# 与 bootstrap.ROOT 同一个目录(scripts/ 的上一级)。
ROOT = Path(__file__).resolve().parent.parent

LEDGER = ROOT / "config" / "test_segment_usage.json"


class SegmentAlreadyUsed(RuntimeError):
    pass


class LedgerError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"测试段登记文件无法读取,拒绝继续(fail-closed): {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"测试段登记文件格式错误(应为对象): {path}")
    return data


def check(key: str, path: Path | None = None) -> None:
    path = path or LEDGER  # 运行时取值,便于测试替换
    entry = _load(path).get(key)
    if entry is not None:
        raise SegmentAlreadyUsed(
            f"测试段 {key} 已被使用({entry.get('at', '?')}: {entry.get('note', '')});"
            "再次使用会产生多重检验偏差。如确有全新样本外数据,请手工编辑登记文件并补 ADR。"
        )


def record(key: str, path: Path | None = None, note: str = "") -> None:
    path = path or LEDGER
    check(key, path)
    data = _load(path)
    data[key] = {"at": datetime.now().isoformat(timespec="seconds"), "note": note}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
