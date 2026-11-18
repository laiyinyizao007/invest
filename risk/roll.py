"""主力合约推导与换月校验:研究侧(构建复权连续序列)与实盘启动校验共用的唯一规则来源。

主力选择规则(原在 scripts/build_continuous.py,挪到这里以便只依赖标准库的实盘/研究模块复用;build_continuous 改为导入):
  1. 主力 = 持仓量最大的合约,且只向更远月换月(不回头);
  2. 用**前一个交易日**的持仓量决定当日主力(持仓量收盘后才公布,用当日值属于前视);
  3. 当前主力当天无成交但之后还会交易 → 当日跳过;不再有数据(到期)→ 强制换月。

换月校验(路线图「主力换月自动化」;接入运行时启动是 F-b 的工作):
  按换月记录推出某日应持有的主力,与实盘 LIVE_VT_SYMBOL 比较,返回 一致/不一致/未知。
  换月记录过旧(数据截止日距今太久)时判为未知:旧记录无法证明「此后没有发生换月」。调用方对 不一致/未知 都应拒绝启动。

只依赖标准库。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from risk.sessions import SESSIONS, UnknownProductError, product_of

OK, MISMATCH, UNKNOWN = "ok", "mismatch", "unknown"
MAX_STALE_DAYS = 7          # 换月记录的数据截止日距检查日超过这么多自然日,判为未知

_SINA_RE = re.compile(r"^([A-Za-z]+)(\d{4})$")


class RollDataError(ValueError):
    """换月记录文件缺失、格式不对或前后不连贯。"""


def contract_expiry(code: str) -> int:
    """合约代码末四位 YYMM -> 可比较的整数(如 C2509 -> 2509)。"""
    m = re.search(r"(\d{4})$", code)
    if not m:
        raise ValueError(f"无法解析合约月份: {code}")
    return int(m.group(1))


def choose_main(current, avail, prev_oi, expiry, last_day, day):
    """决定 day 日使用哪个合约;返回 None 表示该日跳过(不产生 K 线)。只使用截至前一交易日的持仓量。"""
    cands = [c for c in avail if current is None or expiry[c] >= expiry[current]]
    if not cands:
        return None

    def key(c):
        return (prev_oi.get(c, -1.0), expiry[c])

    if current is None:
        known = [c for c in cands if c in prev_oi]
        if known:
            return max(known, key=key)
        # 序列的第一天没有"前一日"可参考,只能用当日持仓量(仅影响起点)
        return max(cands, key=lambda c: (avail[c]["open_interest"], expiry[c]))

    if current in avail:
        best = max(cands, key=key)
        if expiry[best] > expiry[current] and prev_oi.get(best, -1.0) > prev_oi.get(current, -1.0):
            return best
        return current

    # 当前主力今天没有成交:之后还会有数据 → 停牌/无量,跳过当日;不再有数据(到期) → 强制换月
    if last_day[current] > day:
        return None
    return max(cands, key=key)


def sina_to_ctp(code: str) -> str:
    """新浪合约代码 → CTP vt_symbol:DCE 小写 + 4 位(c2701.DCE),CZCE 大写 + 3 位(MA701.CZCE)。"""
    m = _SINA_RE.match(code.strip())
    if not m:
        raise ValueError(f"不是新浪合约代码(品种 + 4 位年月):{code!r}")
    product, yymm = m.group(1).upper(), m.group(2)
    spec = SESSIONS.get(product)
    if spec is None:
        raise UnknownProductError(product)
    if spec.exchange == "CZCE":
        return f"{product}{yymm[1:]}.CZCE"
    if spec.exchange == "DCE":
        return f"{product.lower()}{yymm}.DCE"
    raise UnknownProductError(f"{product}({spec.exchange} 的合约代码规则未登记)")


def main_as_of(rolls: list[dict], day: date) -> str | None:
    """按换月记录(按日期升序)推出 day 日应持有的主力;没有记录时为 None(未知)。"""
    if not rolls:
        return None
    held = rolls[0]["from"]
    for rec in rolls:
        if rec["date"] <= day:
            held = rec["to"]
        else:
            break
    return held


@dataclass(frozen=True)
class RollCheck:
    status: str
    expected: str | None
    reason: str


def validate_roll(live_vt_symbol: str, rolls: list[dict], *, data_end: date, day: date,
                  max_stale_days: int = MAX_STALE_DAYS) -> RollCheck:
    expected = main_as_of(rolls, day)
    if expected is None:
        return RollCheck(UNKNOWN, None, "没有换月记录,无法判定当前主力")
    if (day - data_end).days > max_stale_days:
        return RollCheck(UNKNOWN, None, f"换月记录的数据只到 {data_end.isoformat()},距 {day.isoformat()} 超过 {max_stale_days} 天:"
                                        "无法证明此后没有换月,请先更新连续合约数据")
    if product_of(live_vt_symbol) != product_of(expected):
        return RollCheck(MISMATCH, expected, f"实盘合约 {live_vt_symbol} 与换月记录的品种不同(记录主力 {expected})")
    want = sina_to_ctp(expected)
    if live_vt_symbol.strip().lower() != want.lower():
        return RollCheck(MISMATCH, expected, f"实盘合约 {live_vt_symbol} 不是当前主力 {expected}({want}):请确认是否已换月")
    return RollCheck(OK, expected, f"实盘合约 {live_vt_symbol} 与当前主力 {expected} 一致")


def load_rolls_csv(path: Path | str) -> list[dict]:
    """读 build_continuous 写出的 {SYM}_rolls.csv(列 date,from,to,gap,gap_exact)。不连贯即报错,不猜。"""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        raise RollDataError(f"读不了换月记录 {path}:{exc}") from exc
    out = []
    for i, row in enumerate(rows, start=2):
        try:
            rec = {"date": date.fromisoformat(row["date"]), "from": row["from"].strip(), "to": row["to"].strip()}
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RollDataError(f"{path} 第 {i} 行格式不对:{row}") from exc
        if out:
            if rec["date"] <= out[-1]["date"]:
                raise RollDataError(f"{path} 第 {i} 行日期没有递增:{rec['date']}")
            if rec["from"] != out[-1]["to"]:
                raise RollDataError(f"{path} 第 {i} 行链条断了:上一次换到 {out[-1]['to']},这一行却从 {rec['from']} 换出")
        out.append(rec)
    return out
