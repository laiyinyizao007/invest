"""自定义事件类型。数据均为简单 dict/dataclass,便于 Webhook 与运行时订阅。"""

# 账户风控告警:data = {"level": "INFO|WARN|ERROR|FATAL", "title": str, "message": str}
EVENT_GUARD_ALERT = "eGuardAlert"

# 累计死线触发且已确认空仓(或平仓彻底失败):运行时据此停止策略并退出。data = {"flat": bool, "reason": str}
EVENT_GUARD_HARD_LOCK = "eGuardHardLock"
