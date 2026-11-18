"""主力换月推导与校验(risk/roll.py)。

- 主力选择规则从 scripts/build_continuous.py 挪到 risk/roll.py(只依赖标准库),build_continuous 改为从这里导入:
  规则只有一份,研究侧与实盘启动校验用的是同一个函数。
- 校验口径:按换月记录推出某日应持有的主力;与实盘 LIVE_VT_SYMBOL 比较,不一致或无法判定都不放行(fail-closed 由调用方执行)。
"""
from datetime import date

import pytest

from risk import roll as r

ROLLS = [
    {"date": date(2026, 3, 18), "from": "C2605", "to": "C2609"},
    {"date": date(2026, 7, 20), "from": "C2609", "to": "C2701"},
]


def test_choose_main_is_the_same_function_build_continuous_uses():
    bc = pytest.importorskip("build_continuous")            # build_continuous 依赖交易栈;没装就跳过
    assert bc._choose_main is r.choose_main and bc.contract_expiry is r.contract_expiry


def test_contract_expiry():
    assert r.contract_expiry("C2509") == 2509 and r.contract_expiry("MA1905") == 1905
    with pytest.raises(ValueError):
        r.contract_expiry("CXX")


def test_choose_main_only_rolls_forward_on_previous_day_open_interest():
    expiry = {"C2605": 2605, "C2609": 2609}
    avail = {"C2605": {"open_interest": 10}, "C2609": {"open_interest": 99}}
    last_day = {"C2605": date(2026, 5, 15), "C2609": date(2026, 9, 14)}
    # 远月当日持仓量更大,但前一日还没反超 → 不换(不用当日统计量,避免前视)
    assert r.choose_main("C2605", avail, {"C2605": 50, "C2609": 40}, expiry, last_day, date(2026, 3, 17)) == "C2605"
    # 前一日反超 → 换
    assert r.choose_main("C2605", avail, {"C2605": 40, "C2609": 50}, expiry, last_day, date(2026, 3, 18)) == "C2609"
    # 不回头
    assert r.choose_main("C2609", avail, {"C2605": 90, "C2609": 50}, expiry, last_day, date(2026, 3, 19)) == "C2609"


@pytest.mark.parametrize("code,ctp", [
    ("C2701", "c2701.DCE"),
    ("MA2701", "MA701.CZCE"),
    ("c2701", "c2701.DCE"),
])
def test_sina_to_ctp(code, ctp):
    assert r.sina_to_ctp(code) == ctp


def test_sina_to_ctp_rejects_unknown_product_and_bad_codes():
    from risk.sessions import UnknownProductError
    with pytest.raises(UnknownProductError):
        r.sina_to_ctp("XX2701")
    with pytest.raises(ValueError):
        r.sina_to_ctp("C27")


@pytest.mark.parametrize("day,expected", [
    (date(2026, 3, 17), "C2605"),     # 第一次换月之前:持有第一条记录的 from
    (date(2026, 3, 18), "C2609"),     # 换月当日起持有新合约
    (date(2026, 7, 19), "C2609"),
    (date(2026, 7, 20), "C2701"),
])
def test_main_as_of(day, expected):
    assert r.main_as_of(ROLLS, day) == expected


def test_main_as_of_without_records_is_unknown():
    assert r.main_as_of([], date(2026, 1, 5)) is None


@pytest.mark.parametrize("live,day,status", [
    ("c2701.DCE", date(2026, 11, 16), r.OK),
    ("C2701.DCE", date(2026, 11, 16), r.MISMATCH),     # CTP 规范是 DCE 小写:大写就是写错了(G8 第 2 轮)
    (" c2701.DCE ", date(2026, 11, 16), r.OK),         # 只容忍首尾空白
    ("c2609.DCE", date(2026, 11, 16), r.MISMATCH),     # 还挂着旧主力
    ("c2701.DCE", date(2026, 7, 1), r.MISMATCH),       # 换月前就切到远月也算不一致
])
def test_validate_roll(live, day, status):
    res = r.validate_roll(live, ROLLS, data_end=date(2026, 11, 13), day=day)
    assert res.status == status
    if status == r.MISMATCH:
        assert res.expected and res.expected in res.reason


def test_validate_roll_is_unknown_when_roll_data_is_stale():
    res = r.validate_roll("c2701.DCE", ROLLS, data_end=date(2026, 10, 1), day=date(2026, 11, 16))
    assert res.status == r.UNKNOWN and "2026-10-01" in res.reason      # 换月记录太旧:不能证明现在还没换月


def test_validate_roll_is_unknown_without_records_or_for_wrong_product():
    assert r.validate_roll("c2701.DCE", [], data_end=date(2026, 11, 13), day=date(2026, 11, 16)).status == r.UNKNOWN
    res = r.validate_roll("MA701.CZCE", ROLLS, data_end=date(2026, 11, 13), day=date(2026, 11, 16))
    assert res.status == r.MISMATCH and "品种" in res.reason


def test_load_rolls_csv(tmp_path):
    p = tmp_path / "C_rolls.csv"
    p.write_text("date,from,to,gap,gap_exact\n2026-03-18,C2605,C2609,12.0,True\n2026-07-20,C2609,C2701,-3.0,False\n", encoding="utf-8")
    assert r.load_rolls_csv(p) == ROLLS


@pytest.mark.parametrize("body", [
    "date,from\n2026-03-18,C2605\n",                                   # 缺列
    "date,from,to,gap,gap_exact\nbad,C2605,C2609,1,True\n",            # 日期坏
    "date,from,to,gap,gap_exact\n2026-07-20,C2609,C2701,1,True\n2026-03-18,C2605,C2609,1,True\n",   # 乱序
    "date,from,to,gap,gap_exact\n2026-03-18,C2605,C2609,1,True\n2026-07-20,C2605,C2701,1,True\n",   # 链条断了
    "date,from,to,gap,gap_exact\n2026-03-18,C2605,MA2609,1,True\n",                                    # 跨品种(G8 第 2 轮)
    "date,from,to,gap,gap_exact\n2026-03-18,C2609,C2605,1,True\n",                                     # 往回换
    "date,from,to,gap,gap_exact\n2026-03-18,C2605,,1,True\n",                                          # 空合约
    "date,from,to,gap,gap_exact\n2026-03-18,XX2605,XX2609,1,True\n",                                   # 时段表外品种
])
def test_load_rolls_csv_rejects_bad_files(tmp_path, body):
    p = tmp_path / "C_rolls.csv"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(r.RollDataError):
        r.load_rolls_csv(p)
