"""确定性中文报告:把 backtest / gate / evaluate 的结果压成几行人能读的文字。

纯函数,不依赖 vn.py,同一输入恒得同一输出。**不做策略建议**——「下一步」只复述失败条目与诊断书的最弱维度,
避免把确定性工具变成另一个会胡说的来源。输入可以残缺,永远不抛异常(报告出错不能挡住真正的结果)。
"""
from __future__ import annotations

from typing import Any

from risk.promotion_gate import GateThresholds

MAX_FAILURES = 10
MAX_LINE = 240


def _clip(text: Any, limit: int = MAX_LINE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _num(value: Any, spec: str = ".2f") -> str:
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "?"


def _params(params: Any) -> str:
    if not isinstance(params, dict) or not params:
        return "无"
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _failures(failures: Any) -> list[str]:
    if not isinstance(failures, list) or not failures:
        return []
    lines = [f"  - {_clip(f)}" for f in failures[:MAX_FAILURES]]
    if len(failures) > MAX_FAILURES:
        lines.append(f"  - 其余 {len(failures) - MAX_FAILURES} 项未列出")
    return lines


def _diagnosis_line(diagnosis: Any) -> str | None:
    if isinstance(diagnosis, dict) and diagnosis.get("summary"):
        return f"诊断: {_clip(diagnosis['summary'])}"
    return None


def _selection_lines(selection: Any, params: Any) -> list[str]:
    if not isinstance(selection, dict):
        return []
    valid, total = selection.get("valid_cells"), selection.get("n_cells")
    lines = [f"选参: 在 {'~'.join(str(x) for x in (selection.get('window') or ['?', '?']))} 扫描 {valid}/{total} 个有效格子"
             + (f",选出 {_params(params)}" if params else ""),
             "参数高原: " + ("在高原上" if selection.get("on_plateau") else "不在高原上")]
    if selection.get("note"):
        lines.append(f"  {_clip(selection['note'])}")
    return lines


def render_report(result: Any) -> str:
    try:
        return _render(result if isinstance(result, dict) else {})
    except Exception as exc:        # noqa: BLE001  报告是辅助信息,绝不能因为它出错而丢掉真正的结果
        return f"(报告生成失败: {type(exc).__name__})"


def _render(r: dict) -> str:
    kind = r.get("kind")
    if r.get("status") == "strategy_error":
        err = r.get("error") if isinstance(r.get("error"), dict) else {}
        return f"策略运行报错: {_clip(err.get('type', '?'), 60)}: {_clip(err.get('message', ''))}\n请根据报错修正候选代码后重新提交。"

    if kind == "backtest":
        stats = r.get("stats") if isinstance(r.get("stats"), dict) else {}
        lines = [f"沙盒回测 {r.get('key', '?')} {r.get('segment', '?')} 段(类 {r.get('class', '?')})",
                 f"收益 {_num(stats.get('return_pct'), '.1f')}%,夏普 {_num(stats.get('sharpe'))},"
                 f"最大回撤 {_num(stats.get('maxdd_pct'), '.1f')}%({_num(stats.get('maxdd_yuan'), '.0f')} 元),往返 {stats.get('round_trips', '?')} 笔"]
        line = _diagnosis_line(r.get("diagnosis"))
        return "\n".join(lines + ([line] if line else []))

    metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
    failures = r.get("failures") if isinstance(r.get("failures"), list) else []
    title = "选参 + 晋级门槛评估" if kind == "evaluate" else "晋级门槛评估"
    if r.get("passed") is True:
        verdict = "结论: 通过全部门槛。通过只代表有资格进入下一阶段接受检验,不代表策略有效。"
    else:
        verdict = f"结论: 未通过({len(failures)} 项未过)" if failures else "结论: 未通过"
    lines = [f"{title}(类 {r.get('class', '?')})", verdict]
    if kind == "evaluate":
        lines += _selection_lines(r.get("selection"), r.get("params"))
    if metrics:
        lines.append(f"样本外: 夏普 {_num(metrics.get('oos_sharpe'))},往返 {metrics.get('round_trips', '?')} 笔,"
                     f"最大回撤 {_num(metrics.get('max_drawdown_pct'), '.1f')}%({_num(metrics.get('max_drawdown_yuan'), '.0f')} 元)")
        lines.append(f"对照: 超过随机入场的 {_num(metrics.get('random_entry_percentile'), '.1f')}%(门槛 {GateThresholds.min_random_percentile:.0f}%),"
                     f"成本翻倍后夏普 {_num(metrics.get('stress_sharpe'))}")
    if failures:
        lines.append("未通过的门槛:")
        lines += _failures(failures)
    line = _diagnosis_line(r.get("diagnosis"))
    if line:
        lines.append(line)
    return "\n".join(lines)
