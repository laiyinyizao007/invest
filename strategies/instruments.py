"""品种参数与成本假设。

注意:手续费与滑点是**假设值**,实盘前必须换成期货公司的真实费率:
- fee_per_lot:单边手续费(元/手),这里统一假设 3 元,偏保守
- 滑点:假设每边 1 个最小变动价位
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    vt_symbol: str       # 新浪主力连续,如 C0.DCE
    size: int            # 每手吨数
    pricetick: float     # 最小变动价位(元/吨)
    fee_per_lot: float = 3.0  # 单边手续费假设(元/手)

    @property
    def slippage_points(self) -> float:
        """回测用的"滑点"(价格点数):1 个 tick + 固定手续费折算。

        vn.py 回测的滑点成本 = 手数 × 每手吨数 × slippage,与成交额无关,
        恰好可以精确表达"每手固定手续费",所以把手续费折成点数并入,同时把按比例的 rate 设为 0。
        """
        return self.pricetick + self.fee_per_lot / self.size


INSTRUMENTS: dict[str, Instrument] = {
    "C": Instrument("C0.DCE", size=10, pricetick=1.0),     # 玉米,主测品种
    "MA": Instrument("MA0.CZCE", size=10, pricetick=1.0),  # 甲醇,对照品种
    # 复权主力连续合约(scripts/build_continuous.py --save 写入),数据自 2018-05 起;回测与实盘预热应使用它们而不是 C0/MA0
    "CADJ": Instrument("CADJ.DCE", size=10, pricetick=1.0),
    "MAADJ": Instrument("MAADJ.CZCE", size=10, pricetick=1.0),
}
