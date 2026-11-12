"""确定性中文报告:把 backtest / gate / evaluate 的结果压成几行人能读的文字。纯函数,不编造策略建议。"""
import json

from risk.report import render_report

METRICS = {"oos_sharpe": -1.84, "oos_years": 1.53, "max_drawdown_pct": -29.1, "max_drawdown_yuan": -3012.0, "round_trips": 156,
           "random_entry_percentile": 65.5, "stress_sharpe": -3.72, "data_adjusted": True, "parameter_on_plateau": False, "random_runs": 200}
FAILURES = ["样本外夏普 -1.84 < 1.5", "最大回撤 29.1% ≥ 20.0%", "最优参数不在参数高原:邻域表现不稳定,疑似过拟合"]
DIAG = {"weakest": "drawdown", "summary": "最弱维度: drawdown(最大回撤占上限 145%)", "dimensions": {}}


def evaluate_result(**over):
    base = {"kind": "evaluate", "passed": False, "failures": FAILURES, "metrics": METRICS, "diagnosis": DIAG, "class": "Candidate",
            "params": {"breakout_n": 72, "slow_window": 96, "atr_mult": 2.0},
            "selection": {"n_cells": 36, "valid_cells": 15, "on_plateau": False, "best_sharpe": -3.5, "window": ["2020-07-22", "2022-06-30"], "note": ""},
            "windows": [{"name": "W1", "round_trips": 57, "sharpe": -3.9}], "run_id": "20261112-210000-000000"}
    base.update(over)
    return base


def test_failed_evaluation_lists_verdict_params_metrics_and_every_failure():
    text = render_report(evaluate_result())

    assert "未通过" in text and "3 项" in text
    assert "breakout_n=72" in text and "不在高原" in text and "15/36" in text
    assert "-1.84" in text and "156" in text and "65.5" in text and "-3.72" in text
    for failure in FAILURES:
        assert failure in text
    assert "drawdown" in text                                       # 诊断书最弱维度


def test_passed_evaluation_says_so_and_does_not_oversell():
    text = render_report(evaluate_result(passed=True, failures=[]))

    assert "通过" in text and "未通过" not in text
    assert "不代表策略有效" in text


def test_selection_only_failure_explains_no_params_could_be_chosen():
    text = render_report({"kind": "evaluate", "passed": False, "params": None, "failures": ["无法选参: 没有任何参数组合同时满足最少往返笔数与完整邻域,无法选参"],
                          "selection": {"n_cells": 27, "valid_cells": 0, "on_plateau": False, "best_params": None,
                                        "note": "没有任何参数组合同时满足最少往返笔数与完整邻域,无法选参"}})

    assert "无法选参" in text and "0/27" in text


def test_strategy_error_is_reported_with_type_and_message():
    text = render_report({"kind": "evaluate", "status": "strategy_error", "passed": False,
                          "error": {"type": "GridError", "message": "param_grid 必须恰好 3 个参数"}})

    assert "策略运行报错" in text and "GridError" in text and "param_grid 必须恰好 3 个参数" in text


def test_backtest_report_shows_stats_and_weakest_dimension():
    text = render_report({"kind": "backtest", "key": "CADJ", "segment": "train", "class": "S",
                          "stats": {"sharpe": 0.44, "maxdd_pct": -6.3, "maxdd_yuan": -695.0, "round_trips": 6, "return_pct": 7.2, "net_pnl": 724.0},
                          "diagnosis": {"weakest": "trades", "summary": "最弱维度: trades(往返 6 笔 < 150)"}})

    assert "回测" in text and "0.44" in text and "6" in text and "trades" in text and "CADJ" in text


def test_gate_report_works_without_selection():
    text = render_report({"kind": "gate", "passed": False, "failures": FAILURES, "metrics": METRICS, "diagnosis": DIAG})

    assert "晋级门槛" in text and "未通过" in text and "选参" not in text


def test_report_is_short_plain_text_without_json():
    text = render_report(evaluate_result())

    assert len(text.splitlines()) <= 24
    assert "{" not in text and "}" not in text                       # 参数写成 a=1 而不是字典
    assert not text.lstrip().startswith("{")


def test_long_failure_lists_and_texts_are_truncated():
    many = [f"失败项{i}:" + "x" * 500 for i in range(30)]

    text = render_report(evaluate_result(failures=many))

    assert text.count("失败项") <= 12 and "其余" in text
    assert max(len(line) for line in text.splitlines()) <= 260


def test_missing_or_malformed_input_never_raises():
    for bad in ({}, {"kind": "evaluate"}, {"kind": "gate", "metrics": "oops"}, {"kind": "backtest", "stats": None},
                {"kind": "evaluate", "selection": [1], "failures": "not a list"}):
        assert isinstance(render_report(bad), str)


def test_report_is_deterministic_and_json_safe():
    a, b = render_report(evaluate_result()), render_report(evaluate_result())

    assert a == b and json.loads(json.dumps({"report": a}, ensure_ascii=False))["report"] == a
