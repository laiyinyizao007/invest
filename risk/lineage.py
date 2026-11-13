"""谱系台账:每个战役一份只增不减的台账 `data/research/evolution/<campaign_id>.jsonl`,一行一条候选记录。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」。面板读取侧是 `dashboard/evolution.py`:
**写出去的每一行必须它读得进**——本模块的字段、范围与上限与它同源(读取侧才是消费者)。

台账里**没有任何会进变异提示词的自由文本**:
- `diagnosis` 是结构化诊断(枚举 + 0–1 数值,见 risk/structured_diagnosis.py),写入前校验;
- `cell` 必须是 MAP-Elites 格子名(`H<0-3>F<0-3>`);
- `description` 是程序从代码抽取的可验证描述(risk/describe.py),写入前校验;
- `fail_reason` 是程序给出的短字符串(限长 200),`reflection` 只存档给人看(限长 2000),**外层响应从不回传它们**;
- **未知字段一律拒绝**——否则额外字段就是夹带自由文本的后门。

只增不减:本模块不提供删除/改写接口;追加前先校验整份台账(损坏即 fail-closed,不在已坏的台账上继续累计);
同一战役里 `candidate_sha256` 不得重复(同一份代码被记两次会让进化曲线重复计数);
非根记录的父代必须已在同一战役的台账里,且父代的代号严格小于本条的代号。

字段含义(**选参窗口**那一层,进化回路放开的信息):
- `sharpe` / `total_return` / `max_drawdown`:选参窗口里最佳参数格子的成绩(不是验证窗口);
- `fitness`:选父代用的适应度(选参窗口的邻域平滑夏普)——**DSR 不用于选父代**,`dsr`/`n_trials` 只记录给人看;
- `status`:门槛评估的结局枚举。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from risk import describe as describe_mod
from risk import structured_diagnosis as sd
from risk.behavior import CELL_PATTERN

CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")      # 与读取侧一致
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
STATUSES = ("gate_passed", "gate_failed", "selection_failed", "strategy_error")
MAX_LEDGER_BYTES = 8 * 1024 * 1024                                  # 与读取侧一致:超过它读取侧会拒绝
MAX_LEDGER_LINES = 20_000
MAX_GENERATION = 1_000_000
MAX_FAIL_REASON = 200
MAX_REFLECTION = 2000
FIELDS = ("generation", "candidate_sha256", "parent_sha256", "run_id", "status", "sharpe", "total_return",
          "max_drawdown", "dsr", "n_trials", "fail_reason", "reflection", "created_at", "cell", "diagnosis",
          "fitness", "description")
_lock = threading.Lock()


class LineageError(Exception):
    """台账损坏、记录不合法或写入失败。"""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _opt_number(record: dict, key: str, low: float | None = None, high: float | None = None) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    if not _finite(value) or (low is not None and value < low) or (high is not None and value > high):
        raise LineageError(f"{key} 必须是有限数" + ("" if low is None else f"且在 [{low}, {high}] 内"))
    return float(value)


def _opt_text(record: dict, key: str, limit: int) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise LineageError(f"{key} 必须是不超过 {limit} 字符的字符串")
    return value


def validate_record(record: Any) -> dict:
    """校验一条记录并返回规整后的副本(缺省字段补 None)。不合法抛 LineageError。"""
    if not isinstance(record, dict):
        raise LineageError("记录不是对象")
    extra = set(record) - set(FIELDS)
    if extra:
        raise LineageError(f"记录含未知字段(台账不收自由字段): {sorted(extra)}")
    generation = record.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation <= MAX_GENERATION:
        raise LineageError("generation 必须是非负整数")
    sha = record.get("candidate_sha256")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise LineageError("candidate_sha256 必须是 64 位小写十六进制")
    parent = record.get("parent_sha256")
    if parent is not None and (not isinstance(parent, str) or not SHA256.fullmatch(parent) or parent == sha):
        raise LineageError("parent_sha256 必须是 64 位小写十六进制,且不能是自己")
    run_id = record.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
        raise LineageError("run_id 不合法")
    if record.get("status") not in STATUSES:
        raise LineageError(f"status 必须是 {STATUSES} 之一")
    n_trials = record.get("n_trials")
    if n_trials is not None and (isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 0):
        raise LineageError("n_trials 必须是非负整数")
    cell = record.get("cell")
    if cell is not None and (not isinstance(cell, str) or not CELL_PATTERN.fullmatch(cell)):
        raise LineageError("cell 必须是 MAP-Elites 格子名(H0-3F0-3)")
    diagnosis = record.get("diagnosis")
    if diagnosis is not None and not sd.validate(diagnosis):
        raise LineageError("diagnosis 必须是结构化诊断(枚举 + 0–1 数值,不含自由文本)")
    description = record.get("description")
    if description is not None and not describe_mod.validate(description):
        raise LineageError("description 必须是程序抽取的可验证描述")
    created_at = record.get("created_at")
    if created_at is not None:
        if not isinstance(created_at, str) or len(created_at) > 40:
            raise LineageError("created_at 必须是 ISO 时间字符串")
        try:
            datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise LineageError("created_at 必须是 ISO 时间字符串") from exc
    return {
        "generation": generation, "candidate_sha256": sha, "parent_sha256": parent, "run_id": run_id,
        "status": record["status"], "sharpe": _opt_number(record, "sharpe"),
        "total_return": _opt_number(record, "total_return"), "max_drawdown": _opt_number(record, "max_drawdown"),
        "dsr": _opt_number(record, "dsr", 0.0, 1.0), "n_trials": n_trials,
        "fail_reason": _opt_text(record, "fail_reason", MAX_FAIL_REASON),
        "reflection": _opt_text(record, "reflection", MAX_REFLECTION), "created_at": created_at,
        "cell": cell, "diagnosis": diagnosis, "fitness": _opt_number(record, "fitness"),
        "description": description,
    }


def _short_reason(result: dict) -> str | None:
    """程序给出的失败原因短字符串(不含验证窗口数字):门槛检查项 ID,或固定短语。"""
    if result.get("status") == "strategy_error":
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        etype = error.get("type")
        return ("策略运行报错:" + etype[:60])[:MAX_FAIL_REASON] if isinstance(etype, str) and etype.isidentifier() else "策略运行报错"
    if result.get("kind_detail") == "selection-only":
        return "无法选参"
    ids = result.get("failed_check_ids")
    if isinstance(ids, list) and ids:
        return ",".join(i for i in ids if isinstance(i, str) and re.fullmatch(r"[a-z_]+", i))[:MAX_FAIL_REASON] or None
    return None


def record_from_result(result: dict, source: str, *, parent_sha256: str | None, generation: int) -> dict:
    """由一个 evaluate 落盘结果生成台账记录。**只取进化回路放开的那一层**:

    - `sharpe` / `total_return` / `max_drawdown` / `fitness` / `cell` / `diagnosis`:选参窗口的成绩与行为;
    - `dsr` / `n_trials`:验证窗口派生的数,**只记录给人看**(面板展示),进化回路从不读(见 risk/map_elites.py);
    - `description`:程序从候选源码抽取的可验证描述。
    返回的记录还要过 `validate_record`(append_record 会做)。"""
    selection = result.get("selection") if isinstance(result.get("selection"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    chosen = result.get("selection_metrics") if isinstance(result.get("selection_metrics"), dict) else {}
    if result.get("status") == "strategy_error":
        status = "strategy_error"
    elif result.get("kind_detail") == "selection-only":
        status = "selection_failed"
    else:
        status = "gate_passed" if result.get("passed") is True else "gate_failed"
    n_trials = metrics.get("n_trials")
    return {
        "generation": generation, "candidate_sha256": result.get("strategy_sha256"), "parent_sha256": parent_sha256,
        "run_id": result.get("run_id"), "status": status, "sharpe": selection.get("best_sharpe"),
        "total_return": chosen.get("total_return"), "max_drawdown": chosen.get("max_drawdown"),
        "dsr": metrics.get("dsr"), "n_trials": n_trials if isinstance(n_trials, int) and not isinstance(n_trials, bool) and n_trials >= 0 else None,
        "fail_reason": _short_reason(result), "cell": result.get("cell"),
        "diagnosis": result.get("selection_diagnosis"), "fitness": result.get("fitness"),
        "description": describe_mod.describe_candidate(source),
    }


def ledger_path(directory: Path, campaign_id: str) -> Path:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID.fullmatch(campaign_id):
        raise LineageError("campaign_id 不合法")
    return Path(directory) / f"{campaign_id}.jsonl"


def read_records(directory: Path, campaign_id: str) -> list[dict]:
    """读整份台账并**逐条校验**。文件不存在 = 空;任何一行不合法 = 损坏,抛 LineageError(fail-closed)。"""
    path = ledger_path(directory, campaign_id)
    if not path.exists():
        return []
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            raise LineageError("台账超过大小上限")
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LineageError(f"台账无法读取(fail-closed): {exc}") from exc
    rows: list[dict] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(rows) >= MAX_LEDGER_LINES:
            raise LineageError("台账行数超过上限")
        try:
            row = validate_record(json.loads(line))
        except ValueError as exc:                                    # JSONDecodeError 是 ValueError
            raise LineageError(f"台账第 {number} 行无法解析(fail-closed): {exc}") from exc
        except LineageError as exc:
            raise LineageError(f"台账第 {number} 行不合法(fail-closed): {exc}") from exc
        sha = row["candidate_sha256"]
        if sha in seen:
            raise LineageError(f"台账第 {number} 行的 candidate_sha256 重复(fail-closed)")
        parent = row["parent_sha256"]
        if parent is not None and (parent not in seen or rows[seen[parent]]["generation"] >= row["generation"]):
            raise LineageError(f"台账第 {number} 行的父代不在此前的记录里,或代号不小于本条(fail-closed)")
        seen[sha] = len(rows)
        rows.append(row)
    return rows


def append_record(directory: Path, campaign_id: str, record: dict, *, now: datetime | None = None) -> dict:
    """追加一条记录并返回写入的内容。先校验整份台账与新记录,任何问题都不写。"""
    clean = validate_record(record)
    if clean["created_at"] is None:
        clean["created_at"] = (now or datetime.now()).isoformat(timespec="seconds")
    path = ledger_path(directory, campaign_id)
    with _lock:
        rows = read_records(directory, campaign_id)               # 损坏的台账上不继续累计
        if len(rows) >= MAX_LEDGER_LINES:
            raise LineageError("台账行数已达上限")
        by_sha = {r["candidate_sha256"]: r for r in rows}
        if clean["candidate_sha256"] in by_sha:
            raise LineageError("该候选已经记录过(同一份代码不重复入账)")
        parent = clean["parent_sha256"]
        if parent is not None:
            if parent not in by_sha:
                raise LineageError("父代不在这个战役的台账里")
            if by_sha[parent]["generation"] >= clean["generation"]:
                raise LineageError("父代的代号必须严格小于本条的代号")
        try:
            line = json.dumps(clean, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + chr(10)
        except ValueError as exc:
            raise LineageError(f"记录含非有限数: {exc}") from exc
        current = path.stat().st_size if path.exists() else 0
        if current + len(line.encode("utf-8")) > MAX_LEDGER_BYTES:
            raise LineageError("台账将超过大小上限")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise LineageError(f"台账写入失败(fail-closed): {exc}") from exc
    return clean
