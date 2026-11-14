"""对外揭示层:研究网关交给外层(LLM/进化回路)的一切,都从这里过。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:按信息来源分层——
- **选参窗口**(2020-07 ~ 2022-06):放开。结构化指标、热力图、行为格子、结构化诊断、适应度都可以给;
- **滚动验证窗口**(闸门):只能经**梯子**(risk/ladder.py)量化后给出;
- 最终留出:永远不进回路。

落盘的结果(`data/research/<run_id>.json`)是给**人**看的完整记录(验证窗口的夏普、逐窗口成绩、带数字的中文失败原因、
DSR、成本翻倍夏普……);外层看到的是它的**净化视图**,由本模块生成:

- **只有白名单字段**:数字都是有限数,名字都在封闭集合里,没有任何会进变异提示词的自由文本——
  失败原因是门槛检查项的稳定 ID(`risk.promotion_gate.CHECK_IDS`),不是带验证窗口数字的句子;
  候选自己起的参数名/类名之外的字符串一律不进视图(类名与策略错误类型限长、限字符集);
- **验证窗口的夏普只经梯子**:一个 run 第一次向外层揭示验证窗口的数字时,才消耗一次梯子查询并把当时的视图存下来
  (`<results_dir>/outward/<run_id>.json`);**之后重复读取只回放当时的视图**,不再查询、也不会读到更新的值——
  否则反复读同一个 run 就是在白嫖查询,或者在梯子上升之后读到不一致的值;
- 没有验证窗口数字的 run(选参失败、策略报错、训练段回测)不消耗查询;
- **从未揭示过的 run 不会被偷偷揭示**:`allow_query=False` 时(给 `/feedback` 用)既没有存档、梯子里也没有这个 run 就返回 None,
  因为 `/feedback` 一次要扫很多 run,不能让一次读取悄悄烧掉一批查询;
- **存档写失败后重试是安全的**(第 15 轮 codex 的 High):梯子的提交按 `run_id` 幂等,"扣了查询、存档没写成"的 run 重试时
  只回放当时的汇报值,不重复扣查询;
- 存档损坏就 fail-closed(抛 OutwardError),不重新查询——重新查询会掩盖篡改。

被拒绝的路径同样重要:验证段的回测(`/backtest` 的 `segment=valid`)由网关直接拒绝(见 research_gateway.py),
因为它会绕过梯子直接给出验证窗口的完整统计。
"""
from __future__ import annotations

import builtins
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from risk import structured_diagnosis as sd
from risk.behavior import CELL_PATTERN
from risk.describe import SEARCHABLE_PARAMS
from risk.ladder import Ladder
from risk.promotion_gate import CHECK_IDS

VIEW_VERSION = 1
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KINDS = ("evaluate", "gate", "backtest")
MAX_ERROR_MESSAGE = 300
PHASES = ("declaration", "selection", "validation")
# 报错原文**只在这些阶段放出**:声明期(我们自己写的消息)与选参窗口回放(进化回路放开的那一层)。
# 验证窗口回放里的报错原文不出去:候选的普通 bug(`assert pnl > 0, f"pnl {pnl}"`)就会把验证窗口的原始数字
# 塞进消息,而梯子一次都不扣(第 15 轮 codex 的 Critical,端到端复现过)。没有阶段标记的按验证窗口处理(fail-closed)。
OPEN_PHASES = ("declaration", "selection")
# 异常类型的封闭集合:内建异常的名字 + 我们自己的声明期错误。候选自己定义的异常类名是自由文本,不放行。
STANDARD_ERROR_TYPES = frozenset(
    name for name in dir(builtins) if isinstance(getattr(builtins, name), type)
    and issubclass(getattr(builtins, name), BaseException)) | {"GridError", "SandboxError", "ConstitutionError"}
MAX_STORED_BYTES = 64 * 1024
# 训练段回测给外层的统计(选参那一层,放开)。不含验证/测试段的任何东西。
BACKTEST_STATS = ("return_pct", "maxdd_pct", "maxdd_yuan", "sharpe", "round_trips", "end_balance", "net_pnl", "total_cost")
_lock = threading.Lock()


class OutwardError(Exception):
    """对外揭示的存档损坏或写入失败(fail-closed)。"""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def validation_sharpe(result: dict) -> tuple[bool, Any]:
    """这个结果里有没有**验证窗口**的夏普。返回 (有, 值)。有就意味着向外揭示它必须经过梯子。

    - evaluate / gate:门槛评估的样本外夏普 `metrics.oos_sharpe`(策略报错的没有);
    - backtest 的 valid 段(命令行跑出来、事后被网关读到):`stats.sharpe`。"""
    kind = result.get("kind")
    if result.get("status") == "strategy_error":
        return False, None
    if kind in ("evaluate", "gate"):
        metrics = result.get("metrics")
        if isinstance(metrics, dict):
            return True, metrics.get("oos_sharpe")
        return False, None
    if kind == "backtest" and result.get("segment") == "valid":
        stats = result.get("stats")
        return True, stats.get("sharpe") if isinstance(stats, dict) else None
    return False, None


def _selection_view(selection: Any) -> dict | None:
    if not isinstance(selection, dict):
        return None
    out: dict[str, Any] = {}
    params = selection.get("best_params")
    if isinstance(params, dict):
        # 参数名来自候选自己写的 param_grid:只放行可搜索参数的封闭集合,其余只计数(名字是自由文本通道)
        out["best_params"] = {k: float(v) for k, v in params.items() if k in SEARCHABLE_PARAMS and _finite(v)}
        out["other_params"] = sum(1 for k in params if k not in SEARCHABLE_PARAMS)
    for key in ("best_sharpe", "best_smoothed"):
        out[key] = _num(selection.get(key))
    plateau = selection.get("on_plateau")
    out["on_plateau"] = plateau if isinstance(plateau, bool) else None
    for key in ("n_cells", "valid_cells"):
        out[key] = _int(selection.get(key))
    window = selection.get("window")
    out["window"] = (list(window) if isinstance(window, list) and len(window) == 2
                     and all(isinstance(d, str) and DATE.fullmatch(d) for d in window) else None)
    return out


def _behavior_view(behavior: Any) -> dict | None:
    if not isinstance(behavior, dict):
        return None
    out = {key: _num(behavior.get(key)) for key in ("avg_hold_bars", "trips_per_100_bars")}
    out.update({key: _int(behavior.get(key)) for key in ("round_trips", "n_bars")})
    cell = behavior.get("cell")
    out["cell"] = cell if isinstance(cell, str) and CELL_PATTERN.fullmatch(cell) else None
    return out


def build_outward(result: dict) -> dict:
    """由落盘结果生成净化视图(不含梯子汇报值)。纯函数:同一个结果恒得同一个视图。"""
    kind = result.get("kind")
    run_id = result.get("run_id")
    cls = result.get("class")
    view: dict[str, Any] = {
        "version": VIEW_VERSION,
        "run_id": run_id if isinstance(run_id, str) and RUN_ID.fullmatch(run_id) else None,
        "kind": kind if kind in KINDS else None,
        "status": "strategy_error" if result.get("status") == "strategy_error" else "ok",
        "class": cls if isinstance(cls, str) and CLASS_NAME.fullmatch(cls) else None,
    }
    if view["status"] == "strategy_error":
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        etype, message, phase = error.get("type"), error.get("message"), error.get("phase")
        phase = phase if phase in PHASES else None
        # 训练段回测整段都是放开的那一层;其余看阶段标记
        message_is_open = phase in OPEN_PHASES or (kind == "backtest" and result.get("segment") == "train")
        view["error"] = {"type": etype if isinstance(etype, str) and etype in STANDARD_ERROR_TYPES else None,
                         "phase": phase,
                         "message": message[:MAX_ERROR_MESSAGE] if message_is_open and isinstance(message, str) else None}
    if kind in ("evaluate", "gate"):
        if result.get("kind_detail") == "selection-only":
            # 只做了选参、没有跑验证窗口:这时的"通过/失败项"不含验证窗口的任何信息,统一成固定值
            # (伪造的 selection-only 结果也带不出比特:第 16 轮 codex 的 M2)
            view["passed"], view["failed_checks"] = False, None
        else:
            view["passed"] = result.get("passed") is True
            ids = result.get("failed_check_ids")
            view["failed_checks"] = ([i for i in ids if i in CHECK_IDS] if isinstance(ids, list) else None)
    if kind == "evaluate":
        view["selection"] = _selection_view(result.get("selection"))
        view["behavior"] = _behavior_view(result.get("behavior"))
        view["cell"] = view["behavior"]["cell"] if view["behavior"] else None
        diagnosis = result.get("selection_diagnosis")
        view["selection_diagnosis"] = diagnosis if sd.validate(diagnosis) else None
        view["fitness"] = _num(result.get("fitness"))
        trials = result.get("trials")
        view["trials_total"] = _int(trials.get("total")) if isinstance(trials, dict) else None
    if kind == "backtest" and result.get("segment") == "train":
        stats = result.get("stats")
        view["stats"] = {k: _num(stats.get(k)) for k in BACKTEST_STATS} if isinstance(stats, dict) else None
    view["report"] = outward_report(view)
    return view


def outward_report(view: dict) -> str:
    """净化视图的确定性中文报告。只用视图里的字段,所以报告里不可能出现视图之外的数字。"""
    lines = [f"run {view.get('run_id')}:{view.get('kind')}"]
    if view.get("status") == "strategy_error":
        error = view.get("error") or {}
        fallback = "训练段回测" if view.get("kind") == "backtest" and error.get("message") else "验证窗口回放"
        where = {"declaration": "声明期", "selection": "选参窗口回放", "validation": "验证窗口回放"}.get(error.get("phase"), fallback)
        detail = error.get("message") or "报错原文仅人可见(验证窗口回放中的报错原文不外传:消息里可能带着验证窗口的数字)"
        lines.append(f"策略运行报错({error.get('type') or '自定义异常'},{where}):{detail}")
        return chr(10).join(lines)
    if "passed" in view:
        lines.append("门槛:通过" if view["passed"] else "门槛:未通过")
        if view.get("failed_checks"):
            lines.append("未通过的检查项:" + "、".join(view["failed_checks"]))
    ladder = view.get("ladder")
    if ladder:
        lines.append(f"验证窗口夏普(梯子量化,步长 {ladder['step']}):{ladder['reported_sharpe']};剩余查询 {ladder['queries_left']} 次")
    selection = view.get("selection")
    if selection:
        lines.append(f"选参窗口:最佳夏普 {selection.get('best_sharpe')},平滑 {selection.get('best_smoothed')},"
                     f"参数高原 {selection.get('on_plateau')},有效格子 {selection.get('valid_cells')}/{selection.get('n_cells')}")
        if selection.get("best_params"):
            lines.append("选出的参数:" + ",".join(f"{k}={v}" for k, v in sorted(selection["best_params"].items())))
    if view.get("cell"):
        lines.append(f"行为格子:{view['cell']}")
    diagnosis = view.get("selection_diagnosis")
    if diagnosis and diagnosis.get("weakest"):
        lines.append(f"选参窗口最弱维度:{diagnosis['weakest']}")
    if view.get("stats"):
        lines.append("训练段统计:" + ",".join(f"{k}={v}" for k, v in view["stats"].items() if v is not None))
    return chr(10).join(lines)


# ---------------- 存档 ----------------

_ALLOWED = {"version", "run_id", "kind", "status", "class", "error", "passed", "failed_checks", "selection", "behavior",
            "cell", "selection_diagnosis", "fitness", "trials_total", "stats", "ladder", "report"}


def validate_view(view: Any) -> bool:
    """存档读回来之后的校验:键都在白名单里、梯子块是有限数。不合法就当损坏。"""
    if not isinstance(view, dict) or not set(view) <= _ALLOWED or view.get("version") != VIEW_VERSION:
        return False
    error = view.get("error")
    if error is not None and (not isinstance(error, dict) or not set(error) <= {"type", "phase", "message"}
                              or error.get("phase") not in (None, *PHASES)
                              or (error.get("message") is not None and error.get("phase") not in OPEN_PHASES
                                  and view.get("kind") != "backtest")):
        return False
    ladder = view.get("ladder")
    if ladder is not None:
        if (not isinstance(ladder, dict) or set(ladder) != {"reported_sharpe", "step", "queries_left"}
                or not all(_finite(v) for v in ladder.values())):
            return False
    if view.get("failed_checks") is not None and not (
            isinstance(view["failed_checks"], list) and all(i in CHECK_IDS for i in view["failed_checks"])):
        return False
    diagnosis = view.get("selection_diagnosis")
    if diagnosis is not None and not sd.validate(diagnosis):
        return False
    return isinstance(view.get("report"), str)


class RevealStore:
    """已向外层揭示过的视图存档:每个 run 至多一份,写入后不再改写。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise OutwardError("run_id 不合法")
        return self.directory / f"{run_id}.json"

    def get(self, run_id: str) -> dict | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            if path.stat().st_size > MAX_STORED_BYTES:
                raise OutwardError("揭示存档超过大小上限")
            view = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutwardError(f"揭示存档损坏(fail-closed): {type(exc).__name__ if isinstance(exc, OSError) else exc}") from exc
        if not validate_view(view):
            raise OutwardError("揭示存档不合法(fail-closed)")
        return view

    def put(self, run_id: str, view: dict) -> None:
        path = self._path(run_id)
        if not validate_view(view):
            raise OutwardError("要存的视图不合法")
        try:
            text = json.dumps(view, ensure_ascii=False, allow_nan=False)
            self.directory.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise OutwardError("该 run 已有揭示存档,不覆盖")
            tmp = self.directory / f".{run_id}.json.tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except (OSError, ValueError) as exc:
            raise OutwardError(f"揭示存档写入失败: {type(exc).__name__ if isinstance(exc, OSError) else exc}") from exc


def _replay(stored: dict, view: dict, result: dict, sharpe: Any, run_id: str, ladder: Ladder) -> dict:
    """回放存档前先**对账**(第 16 轮 codex 的 H1:存档只校验形状,改一个文件就能让外层读到任意精度的数字与自由文本)。

    存档必须同时满足:梯子日志里有这个 run;汇报值与梯子(带哈希链)回放的一致;除梯子块与报告外,与由落盘结果
    重新生成的视图逐字段相等;报告就是视图的确定性重推导。任何一条对不上都当作被改过 fail-closed。
    返回的是**当前**的剩余查询数(第 16 轮 L1:不回放旧的 `queries_left`)。"""
    if not ladder.has(run_id):
        raise OutwardError("揭示存档在梯子日志里没有对应的查询记录(fail-closed):存档被伪造或梯子被回滚")
    plan = ladder.preview(sharpe, run_id=run_id)
    block = stored.get("ladder")
    if not isinstance(block, dict) or block.get("reported_sharpe") != plan["reported"] or block.get("step") != plan["step"]:
        raise OutwardError("揭示存档的汇报值与梯子对不上(fail-closed)")
    body = {k: v for k, v in stored.items() if k not in ("ladder", "report")}
    expected = {k: v for k, v in view.items() if k not in ("ladder", "report")}
    if body != expected:
        raise OutwardError("揭示存档与落盘结果对不上(fail-closed)")
    if stored.get("report") != outward_report(stored):
        raise OutwardError("揭示存档的报告不是视图的确定性推导(fail-closed)")
    fresh = dict(stored)
    fresh["ladder"] = {"reported_sharpe": plan["reported"], "step": plan["step"], "queries_left": plan["queries_left"]}
    fresh["report"] = outward_report(fresh)
    return fresh


def reveal(result: dict, *, ladder: Ladder, store: RevealStore, allow_query: bool = True) -> dict | None:
    """把一个落盘结果揭示给外层,返回净化视图。

    - 结果里没有验证窗口数字:直接给视图,不消耗查询;
    - 有:已揭示过就**回放存档**;没揭示过就消耗一次梯子查询、存档、再给视图;
    - `allow_query=False` 且需要查询时返回 None(从未揭示过的 run 不会被偷偷揭示);
    - 梯子预算用尽抛 `LadderExhausted`,状态损坏抛 `LadderError`——都不放出任何验证窗口信息。"""
    view = build_outward(result)
    has, sharpe = validation_sharpe(result)
    if not has:
        # 第 16 轮 codex 的 M2:是否扣查询由 **kind** 决定,不由"metrics 是否存在"决定。
        # evaluate/gate 的 `passed` 与失败检查项本身就是验证窗口的比特;缺 metrics 却带着它们的结果是伪造/损坏,fail-closed。
        if (result.get("kind") in ("evaluate", "gate") and result.get("status") != "strategy_error"
                and result.get("kind_detail") != "selection-only"):
            raise OutwardError("门槛结果缺少 metrics 却带着通过/失败信息(fail-closed:不放出,也不绕过梯子)")
        return view
    run_id = view["run_id"]
    if run_id is None:
        raise OutwardError("没有合法的 run_id,无法揭示验证窗口的数字")
    with _lock:
        stored = store.get(run_id)
        if stored is not None:
            return _replay(stored, view, result, sharpe, run_id, ladder)
        # 没有存档,但梯子日志里有这个 run:上一次揭示在"扣了查询、存档没写成"处失败了(第 15 轮 codex 的 High)。
        # 它已经被扣过一次查询,重建视图只是回放当时的汇报值——不是"偷偷揭示",也不会再扣。
        if not allow_query and not ladder.has(run_id):
            return None
        # 梯子的提交按 run_id **幂等**:同一个 run 重复提交只回放当时的汇报值、不再扣查询。
        # 所以存档写失败之后重试是安全的:不会重复扣、也没有新的信息泄漏(重试本来就拿得到同样的值)。
        reported = ladder.submit(sharpe, run_id=run_id)
        view["ladder"] = {"reported_sharpe": reported["reported"], "step": reported["step"],
                          "queries_left": reported["queries_left"]}
        view["report"] = outward_report(view)
        store.put(run_id, view)
        return view
