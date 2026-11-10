"""委托异常处理策略的纯逻辑:废单分类、紧急平仓重试节奏、废单风暴检测、超时委托识别。

原则:
- **开仓单永不自动重试**:策略下一根 K 线会自己重新判断;重发开仓单有重复下单的风险。
- 网络抖动时订单状态可能不同步(订单其实已被接受),所以**任何重试前必须确认原订单已是终态且未成交**。
- 仅紧急平仓单允许有限次重试(间隔 1s/3s/5s),超过后升级为硬故障并告警。
- 废单分类采用**错误码优先、关键词回退、未知 fail-closed** 三级决策:
  1. 文本里带 CTP 数字错误码(措辞「代码：21」)且码在 CTP_ERROR_KIND 表内 → 直接按码表定论;
  2. 无码或码未收录 → 回退文本关键词分类;
  3. 仍无法识别 → 一律按硬拒绝(宁可停手也不重试)。
  码表只收录有 SimNow 实测或官方文档背书的错误码,不凭记忆写码;新错误码经
  scripts/collect_reject_texts.py 采集后再补表。
"""
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

HARD_KEYWORDS = (
    "资金不足", "保证金", "平仓量", "平今", "平昨", "持仓不足", "合约不存在", "不在交易时段", "非交易",
    "停牌", "涨跌停", "价格", "手数", "权限", "未授权", "禁止", "不被支持", "报单类型", "重复",
    # 2026-11-10 SimNow 实测:非交易时段下发的委托,CTP 回"该交易席位未连接到交易所"(代码 21)。
    # 这句里含"连接",会被 SOFT_KEYWORDS 误判成网络抖动而允许重试;实际是硬拒绝(重试无用,
    # 开盘前不会变成可成交),所以必须显式列为硬关键词。
    "该交易席位未连接到交易所",
)
SOFT_KEYWORDS = ("连接", "断开", "超时", "网络", "重连", "未连接", "未登录")


class RejectKind(Enum):
    HARD = "HARD"   # 不重试,告警并锁定
    SOFT = "SOFT"   # 可在确认原单终态后有限重试(仅平仓单)


# CTP 错误码 → 分类。只收录有 SimNow 实测(docs/reject-texts-simnow.md)或官方错误码表背书的码。
# 网络抖动类的**软错误码暂不收录**:没有实测样本前不凭记忆写码,软拒绝仍由 SOFT_KEYWORDS 文本路径识别;
# 采集到真实软错误码后再补入本表(值为 RejectKind.SOFT)。
CTP_ERROR_KIND: dict[int, RejectKind] = {
    21: RejectKind.HARD,   # 资金不足/保证金不足;SimNow 实测非交易时段也回 21(该交易席位未连接到交易所)
    81: RejectKind.HARD,   # 价格超出涨跌停板/价格非法
    63: RejectKind.HARD,   # 权限不足/未授权
    32: RejectKind.HARD,   # 报单字段非法
    33: RejectKind.HARD,   # 合约交易状态不允许(非交易时段/停牌)
    14: RejectKind.HARD,   # 合约不存在
    30: RejectKind.HARD,   # 该报单已撤销
    31: RejectKind.HARD,   # 该报单已成交
}

# 锚定「代码」前缀取第一个数字码,兼容全角「：」与半角「:」。
# 不能直接搜文本里的第一个整数:断线日志「连接断开，原因{reason}」等也含数字,会误匹配。
_ERROR_CODE_RE = re.compile(r"代码[:：]\s*(\d+)")


def extract_ctp_error_code(text: str | None) -> int | None:
    """从 CTP 错误文本中提取数字错误码(如「交易委托失败，代码：21，信息：…」→ 21);无码返回 None。"""
    if not text:
        return None
    match = _ERROR_CODE_RE.search(text)
    return int(match.group(1)) if match else None


def classify_reject(text: str | None) -> RejectKind:
    """三级分类:错误码表命中 → 关键词回退 → 未知按硬拒绝。

    文本为空、同时含软硬关键词或无法识别时,一律按硬拒绝。
    码表优先是为了纠正关键词的歧义:实测文本「代码：21…该交易席位未连接到交易所」含软关键词
    「连接」,但错误码 21 明确是硬拒绝(重试不会变成可成交)。
    """
    if not text:
        return RejectKind.HARD
    code = extract_ctp_error_code(text)
    if code is not None and code in CTP_ERROR_KIND:
        return CTP_ERROR_KIND[code]
    hard = any(k in text for k in HARD_KEYWORDS)
    soft = any(k in text for k in SOFT_KEYWORDS)
    return RejectKind.SOFT if soft and not hard else RejectKind.HARD


@dataclass(frozen=True)
class RetryPolicy:
    delays: tuple[float, ...] = (1.0, 3.0, 5.0)   # 第 n 次重试前的等待秒数

    @property
    def max_attempts(self) -> int:
        return len(self.delays)

    def delay_for(self, attempt: int) -> float | None:
        """attempt 从 1 开始;超过最大次数返回 None(升级为硬故障)。"""
        if 1 <= attempt <= len(self.delays):
            return self.delays[attempt - 1]
        return None


class RejectMonitor:
    """滚动窗口内的废单计数:窗口内达到阈值即判定为"废单风暴"。"""

    def __init__(self, limit: int = 3, window_seconds: float = 600.0) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit 必须 >= 1 且 window_seconds > 0")
        self.limit = limit
        self.window = window_seconds
        self._times: deque[float] = deque()

    def add(self, now_ts: float) -> bool:
        """记录一次废单,返回是否达到风暴阈值。"""
        self._times.append(now_ts)
        while self._times and now_ts - self._times[0] > self.window:
            self._times.popleft()
        return len(self._times) >= self.limit

    @property
    def count(self) -> int:
        return len(self._times)


def find_stale_orders(orders: Iterable[tuple[str, float, bool]], now_ts: float, timeout: float) -> list[str]:
    """orders: (订单号, 创建时间戳, 是否开仓单)。返回应撤销的超时**开仓**委托;平仓单永不因超时被撤。"""
    return [oid for oid, created, is_open in orders if is_open and now_ts - created >= timeout]
