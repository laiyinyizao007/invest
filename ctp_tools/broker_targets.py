"""国信期货测试环境与 SimNow 预演环境的「允许连的前置地址表」。

单独放一个模块,是为了让**同一个白名单**同时被这几处使用:
- scripts/guoxin_oneclick.py(一键流程的档案校验);
- scripts/broker_test.py(运行期校验,BROKER_TEST_ALLOWED_TD/MD);
- scripts/broker_report.py(生成正式报告名前核对证据里的连接信息)。

只要这些地方各写一份,迟早会漂移;漂移的后果是「校验通过但连到别处」。

地址来源:
- 国信:客户提供的《CTP仿真互联网.txt》(brokerid 8030,电信/联通两套前置);
- SimNow:官方公布的前置机地址(SIMNOW_TD_ADDRESSES 见 ctp_tools/simnow.py)。

**白名单只能收窄,不能放宽**
--------------------------------
`BROKER_TEST_ALLOWED_TD` 与 `CTP_TD_ADDRESS` 都来自同一个 `.env` 文件。
如果直接采信前者,谁改一下 `.env` 就能让脚本把**账号密码发给任意机器**
(改地址 + 改白名单,两个键都在同一个文件里,闸门自己给自己发通行证)。
所以这里有一张**代码里写死的可信地址全集**(`KNOWN_TD` / `KNOWN_MD`),
用户给的白名单只能与它求交集:`resolve_allowed()`。
交集为空 = 拒绝连接;不在全集里的地址没有强制放行的开关。
"""
from __future__ import annotations

from ctp_tools.simnow import SIMNOW_MD_ADDRESSES, SIMNOW_TD_ADDRESSES, normalize_address

# 国信期货看穿式测试环境
GUOXIN_TEST_BROKER_ID = "8030"
GUOXIN_TEST_TD = frozenset({"121.13.218.19:41285", "120.86.114.57:41285"})
GUOXIN_TEST_MD = frozenset({"121.13.218.19:41293", "120.86.114.57:41293"})

# SimNow 预演环境(模拟资金,不是国信环境)
SIMNOW_BROKER_ID = "9999"
SIMNOW_TD = SIMNOW_TD_ADDRESSES
SIMNOW_MD = SIMNOW_MD_ADDRESSES

# 可信地址全集:用户白名单的上限。生产 API 的地址一定不在这里。
KNOWN_TD = frozenset(GUOXIN_TEST_TD | set(SIMNOW_TD))
KNOWN_MD = frozenset(GUOXIN_TEST_MD | set(SIMNOW_MD))


def allowed_for_broker(broker_id: str) -> tuple[frozenset[str], frozenset[str]]:
    """按**经纪商代码**返回该环境唯一允许的前置地址集。

    为什么要按 BrokerID 绑定:可信地址全集是「国信 ∪ SimNow」。如果白名单
    可以同时涵盖两个环境,那么「国信档案 + SimNow 前置」也能通过校验——
    而这两个环境是**不同的账户、不同的柜台**。清场工具按哪个账户连、
    取证运行器按哪个账户连,一旦不一致,平掉的就不是要取证的那个仓位。
    把地址集与 BrokerID 绑死,这种混搭在第一步就被拒。

    未知 BrokerID → 返回两个空集(调用方必须拒绝连接,没有「默认放行」)。
    """
    if broker_id == GUOXIN_TEST_BROKER_ID:
        return GUOXIN_TEST_TD, GUOXIN_TEST_MD
    if broker_id == SIMNOW_BROKER_ID:
        return frozenset(SIMNOW_TD), frozenset(SIMNOW_MD)
    return frozenset(), frozenset()


def resolve_allowed_for_broker(broker_id: str, raw_td: str, raw_md: str
                               ) -> tuple[frozenset[str], frozenset[str], list[str], list[str]]:
    """按经纪商代码绑定解析白名单。返回 (允许的交易前置, 允许的行情前置, 被丢弃的交易地址, 被丢弃的行情地址)。

    未知 BrokerID → 两张表都是空(调用方必须拒绝连接)。
    """
    known_td, known_md = allowed_for_broker(broker_id)
    if not known_td:
        return frozenset(), frozenset(), [], [f"未知的经纪商代码 {broker_id!r}"]
    allowed_td, dropped_td = resolve_allowed(raw_td, known_td)
    allowed_md, dropped_md = resolve_allowed(raw_md, known_md)
    return allowed_td, allowed_md, dropped_td, dropped_md


def resolve_allowed(raw: str, known: frozenset[str]) -> tuple[frozenset[str], list[str]]:
    """把用户给的前置地址白名单解析成「真正允许连接的地址集」。

    返回 (允许连接的地址, 被丢弃的地址)。

    规则:**只能收窄,不能放宽**。
    - 用户给了白名单 → 只保留同时出现在可信地址全集里的那些;
      不在全集里的地址被丢弃(并由调用方大声报出来),因为白名单和
      `CTP_TD_ADDRESS` 同源,采信它等于自证清白。
    - 用户**没给**白名单 → 放行集合为空:「操作员明确说出连哪台机器」
      这一步不能省(ADR §5),代码不能替他决定连哪台。
    """
    wanted = {normalize_address(x) for x in (raw or "").replace(";", ",").split(",") if x.strip()}
    if not wanted:
        return frozenset(), []
    dropped = sorted(wanted - set(known))
    return frozenset(wanted & set(known)), dropped
