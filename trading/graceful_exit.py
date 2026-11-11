"""优雅登出:退出前向 CTP 交易与行情前置各发一次 ReqUserLogout。

为什么需要:`vnpy_ctp` 的网关 `close()` 只调用 `exit()`——释放 API、直接断开连接,**从不发登出请求**。
前置机看到的是"连接突然断开"而不是"用户退出登录"。国信看穿式测试会检查「登录后连接的会话数是否正确」与
「退出后是否正常注销」,没有登出请求时会话要等前置的超时才清理,可能被判为未正常注销。
这里在关闭引擎之前补发登出请求并给前置一点处理时间。

注意:
- `TdApi/MdApi` 暴露了 `reqUserLogout`,但网关没有覆盖 `onRspUserLogout`,所以拿不到登出回报——只能发请求后固定等待一小段时间;
- 任何一步失败都**不得**打断退出流程(尤其是运行时 shutdown 里"退出时仍有持仓"的 FATAL 告警必须照常发出);
- 没有 `td_api/md_api` 的网关(测试用假网关、非 CTP 网关)直接跳过。
"""
import time
from collections.abc import Callable

LOGOUT_SETTLE_SECONDS = 1.5      # 发出登出请求后等待前置处理的时间(无回报可等)


def logout_ctp(main_engine, gateway_name: str = "CTP", settle: float = LOGOUT_SETTLE_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """向已登录的交易/行情通道各发一次登出请求。返回每个通道的处理结果(给日志/报告用),不抛异常。"""
    try:
        gateway = main_engine.get_gateway(gateway_name)
    except Exception as exc:     # noqa: BLE001  引擎已关闭或网关不存在
        return [f"取网关失败,跳过登出: {exc!r}"]
    results: list[str] = []
    sent = False
    for label, attr in (("交易", "td_api"), ("行情", "md_api")):
        api = getattr(gateway, attr, None)
        if api is None or not hasattr(api, "reqUserLogout"):
            results.append(f"{label}: 无该通道,跳过")
            continue
        if not getattr(api, "login_status", False):
            results.append(f"{label}: 未登录,跳过")
            continue
        try:
            api.reqid += 1
            api.reqUserLogout({"BrokerID": api.brokerid, "UserID": api.userid}, api.reqid)
            results.append(f"{label}: 已发送登出请求")
            sent = True
        except Exception as exc:     # noqa: BLE001  一个通道失败不能影响另一个,更不能打断退出
            results.append(f"{label}: 登出请求失败 {exc!r}")
    if sent and settle > 0:
        sleep(settle)
    return results
