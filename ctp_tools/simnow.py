"""SimNow 前置机地址白名单:采集脚本与演练脚本共用同一套"只在模拟盘上跑"的约束。

为什么要有白名单:`--confirm-simnow` 只是一个布尔开关,挡不住"配置写错"——
CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,没法靠环境变量区分两者,
**只能靠前置机地址**。期货公司生产 API 的地址一定不在下面的表里。

地址表只收**官方公布过**的:
- 第一套(仿真,交易时段与实盘一致):交易 30001 / 30002 / 30003,行情 30011;
- 第二套(7x24,仅供 API 测试):交易 40001,行情 40011。
第二套**没有**公布 40002/40003 —— 按第一套的编号规律外推出来的地址没有官方背书,
写进白名单等于"未经确认就放行",所以不收。

第二套的行情前置 40011 的来历:官网没有单独列出它,但**我们自己跑通过** ——
2026-11-12 用第二套(40001/40011)完成认证、登录、合约查询,
`c2701.DCE` 与 `MA701.CZCE` 都能订阅到、tick 持续到达
(见 `docs/runbook-market-recording.md` 第 4 步的记录)。
「实测跑通」就是它的背书;`tools/repro_logout_crash.py` 的 `PORTS` 也用同一对地址。
"""
from urllib.parse import urlparse

# SimNow 官方公布的前置机地址(2026-11 官网 product.action)。
# 第一套与实盘交易时间一致(有夜盘);第二套 7x24 仅有 API 测试用途(新注册账号要等到第三个交易日)。
SIMNOW_TD_ADDRESSES = frozenset({
    "182.254.243.31:30001", "182.254.243.31:30002", "182.254.243.31:30003",
    "182.254.243.31:40001",
})

# SimNow 行情前置(交易 30001 对应行情 30011;第二套 40001 对应 40011)。
# 单独放这里,是为了让「交易前置表」和「行情前置表」只存在于一个模块里——
# 两张表各写一份,迟早漂移(历史上就漂移过一次:broker_targets 与本模块的
# SimNow 交易地址不一致,同一台机器一会儿按新表放行、一会儿按旧表拒绝)。
SIMNOW_MD_ADDRESSES = frozenset({
    "182.254.243.31:30011",
    "182.254.243.31:40011",
})


def normalize_address(td_address: str) -> str:
    """把地址归一化成 `host:port`。

    配置里可能写成 `tcp://182.254.243.31:30001` 或带空格 —— 不归一化就会因为
    "长得不一样"而被判成"不是 SimNow 地址",逼人用逃生舱(--allow-any-address),
    反而削弱了这道防线。
    """
    address = (td_address or "").strip()
    if not address:
        return ""
    if "://" in address:
        address = urlparse(address).netloc or address.split("://", 1)[1]
    # 去掉路径部分(/ctp 之类),只留 host:port
    address = address.split("/", 1)[0]
    return address.strip().lower()   # 主机名大小写无关;与 profile 的白名单比较同一套归一化


def check_simnow_address(td_address: str, allow_any: bool = False) -> tuple[bool, str]:
    """校验交易服务器地址是不是已知的 SimNow 地址。

    返回 (是否放行, 给操作者看的说明)。地址不在表内时默认拒绝连接。
    """
    address = normalize_address(td_address)
    if address in SIMNOW_TD_ADDRESSES:
        return True, f"交易服务器 {address} 在已知 SimNow 地址表内。"
    if allow_any:
        return True, (f"⚠⚠ 危险警告:交易服务器 {address} 不是已知的 SimNow 地址,"
                      f"本次运行由 --allow-any-address 强制放行。"
                      f"若这个地址属于期货公司生产 API,脚本会连上**真实资金账户**并发出委托。")
    return False, (f"交易服务器 {address} 不在已知的 SimNow 地址表里。"
                   f"期货公司生产 API 的地址一定不在表内,因此拒绝连接。"
                   f"确认这是安全的测试环境,可以加 --allow-any-address 强制运行(会再打一次警告)。")


def is_simnow_address(td_address: str) -> bool:
    return normalize_address(td_address) in SIMNOW_TD_ADDRESSES
