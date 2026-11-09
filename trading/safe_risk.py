"""安全风控引擎:在 vnpy_riskmanager 的基础上做两处改动。

1. 平仓单跳过"计数类"规则(每日上限、活动委托数、重复报单)。这些规则不区分开平,触顶后会把保护性平仓也拦下,
   而持仓还在——最危险的情况。平仓仍要通过"合约存在 / 价格是最小变动价位整数倍 / 单笔大小"等合法性规则。
2. 加载账户风控规则 AccountGuardRule,并以项目 config/risk_manager_setting.json 为默认参数
   (用户在 .vntrader 下的同名文件可按规则覆盖)。
"""
import json
from pathlib import Path

from vnpy.trader.object import OrderRequest
from vnpy_riskmanager import RiskEngine, RiskManagerApp

from trading.guard_rule import CLOSING_OFFSETS, AccountGuardRule

# 平仓单不受这些规则约束(它们按计数限流,会把平仓一起拦掉)
COUNTING_RULES = {"每日上限检查", "活动委托检查", "重复报单检查"}


class SafeRiskEngine(RiskEngine):
    """"""

    def load_rules(self) -> None:
        project_setting = Path(__file__).resolve().parent.parent / "config" / "risk_manager_setting.json"
        base = json.loads(project_setting.read_text(encoding="utf-8")) if project_setting.exists() else {}
        # 项目配置为默认值,.vntrader 下已有的同名配置按规则覆盖
        self.setting = {name: {**base.get(name, {}), **self.setting.get(name, {})} for name in {*base, *self.setting}}
        super().load_rules()
        self.add_rule(AccountGuardRule)

    def check_allowed(self, req: OrderRequest, gateway_name: str) -> bool:
        closing = req.offset in CLOSING_OFFSETS
        for rule in self.rules.values():
            if not rule.active:
                continue
            if closing and rule.name in COUNTING_RULES:
                continue
            if not rule.check_allowed(req, gateway_name):
                return False
        return True


class SafeRiskManagerApp(RiskManagerApp):
    """"""

    engine_class = SafeRiskEngine


__all__ = ["SafeRiskEngine", "SafeRiskManagerApp"]
