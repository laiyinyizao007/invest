"""候选代码的**可验证描述**:由程序从源码里抽取,不来自候选自报、更不来自 LLM。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:父代码不原样回灌,只回灌程序从代码抽取的
可验证描述(用到的指标、入场条件个数、参数所在网格单元)。原样回灌父代码有两个问题:
一是代码里的注释/字符串/变量名是自由文本,可以夹带任何东西进变异提示词;二是它携带的信息远多于
变异真正需要的。这里只输出**封闭集合里的名字与小整数**:

- `indicators`:调用了 ArrayManager 的哪些指标方法(`self.am.sma(...)` 之类;名字必须在已知集合里);
- `entry_conditions`:`entry_signal` 里的比较表达式个数(0–99);
- `grid_axes`:`param_grid` 字典字面量里属于**可搜索参数封闭集合**的键;集合之外的键只计入 `other_axes`(个数),
  名字不进描述——候选自己起的名字是自由文本;
- `has_entry_signal`:有没有 `entry_signal` 方法。

纯 `ast`,不执行任何代码。解析失败时返回"什么都没有"的描述(不抛错、不猜)。
"""
from __future__ import annotations

import ast
from typing import Any

# vn.py ArrayManager 的指标方法(只读)。不在这个集合里的名字一律不进描述——描述里不会出现候选自己起的名字。
INDICATORS = frozenset({
    "sma", "ema", "kama", "wma", "apo", "cmo", "mom", "ppo", "roc", "rocr", "rocp", "rocr_100", "trix", "std",
    "obv", "cci", "atr", "natr", "rsi", "macd", "adx", "adxr", "dx", "minus_di", "plus_di", "willr", "ultosc",
    "trange", "boll", "keltner", "donchian", "aroon", "aroonosc", "minus_dm", "plus_dm", "mfi", "ad", "adosc",
    "bop", "stoch", "sar",
})
MAX_CONDITIONS = 99
MAX_AXES = 3
# 可搜索参数(ADR「沿用的硬约束」):描述里的网格轴名**只能**来自这个封闭集合。候选自己起的参数名
# (`param_grid` 的键是候选写的字符串)一律不进描述、只计数——否则它就是一条 40 字符的自由文本通道。
SEARCHABLE_PARAMS = frozenset({"fast_window", "slow_window", "atr_mult", "take_profit_r"})
_KEYS = {"indicators", "entry_conditions", "grid_axes", "other_axes", "has_entry_signal"}


def _empty() -> dict:
    return {"indicators": [], "entry_conditions": 0, "grid_axes": [], "other_axes": 0, "has_entry_signal": False}


def _chain(node: ast.AST) -> list[str]:
    """属性链的名字,如 `self.am.sma` → ["self", "am", "sma"];不是纯属性链返回 []。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return []


def describe_candidate(source: str) -> dict:
    """从候选源码抽取可验证描述。解析失败返回空描述。"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _empty()
    out = _empty()
    indicators: set[str] = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "entry_signal":
                out["has_entry_signal"] = True
                out["entry_conditions"] = min(MAX_CONDITIONS, out["entry_conditions"]
                                              + sum(isinstance(n, ast.Compare) for n in ast.walk(item)))
                for node in ast.walk(item):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        chain = _chain(node.func)
                        if len(chain) >= 2 and "am" in chain[:-1] and chain[-1] in INDICATORS:
                            indicators.add(chain[-1])
            elif isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "param_grid"
                                                      for t in item.targets) and isinstance(item.value, ast.Dict):
                keys = [k.value for k in item.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                out["grid_axes"] = sorted({k for k in keys if k in SEARCHABLE_PARAMS})[:MAX_AXES]
                out["other_axes"] = min(MAX_AXES, sum(1 for k in keys if k not in SEARCHABLE_PARAMS))
    out["indicators"] = sorted(indicators)
    return out


def validate(obj: Any) -> bool:
    """这是不是一份合法的描述:键集精确、指标在已知集合里、数值在范围内。描述会写进台账,写出前必须过这一关。"""
    if not isinstance(obj, dict) or set(obj) != _KEYS:
        return False
    indicators = obj["indicators"]
    if not isinstance(indicators, list) or len(indicators) > len(INDICATORS) or indicators != sorted(set(indicators)):
        return False
    if not all(isinstance(x, str) and x in INDICATORS for x in indicators):
        return False
    count = obj["entry_conditions"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_CONDITIONS:
        return False
    axes = obj["grid_axes"]
    if not isinstance(axes, list) or len(axes) > MAX_AXES or axes != sorted(set(axes)):
        return False
    if not all(isinstance(x, str) and x in SEARCHABLE_PARAMS for x in axes):
        return False
    other = obj["other_axes"]
    if isinstance(other, bool) or not isinstance(other, int) or not 0 <= other <= MAX_AXES:
        return False
    return isinstance(obj["has_entry_signal"], bool)
