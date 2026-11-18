"""已知敏感值的文本脱敏。**脱敏的唯一出处**。

从 notify/webhook.py 抽出(2026-11-12):状态页(dashboard/)要复用它,但 webhook.py 在模块顶部 import requests,
只装了 Web 依赖的环境会因此导入失败。这里只依赖标准库。webhook.py 重新导出了 `redact`,既有调用不受影响。
结构化脱敏 `redact_obj`(含 dict 键)于 2026-11-17 一并移入(Issue #54:外部心跳发送端要用,且研究侧 CI 不装 requests)。
"""
import json
from collections.abc import Iterable


def redact(text: str, secrets: Iterable[str]) -> str:
    """替换已知敏感值；长度不足 3 的值不替换，避免误伤普通文本。

    长值优先替换，避免短密钥是长密钥前缀时留下后缀。
    """
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _redact_key(key, secrets: Iterable[str]):
    """dict 的**键**也要脱敏 —— 键名里带密码时,只脱敏值等于没脱敏。

    JSON 对象的键必须是字符串,非字符串键(如 int)由 json 负责转换,
    这里只保证"转换前的原值"不会把密钥带出去。
    两个不同的键脱敏后可能撞成同一个(键名里带密钥时才会发生),
    撞上的那个字段会丢失 —— 这是刻意的取舍:宁可丢字段,也不能把密钥发出去。
    """
    if isinstance(key, str):
        return redact(key, secrets)
    if key is None or isinstance(key, (int, float, bool)):
        # 数字/布尔键在 `json.dumps` 里会变成字符串("12345"、"true")——
        # 直接原样保留就等于把"键恰好等于密钥"的场景漏出去。
        # 所以按 JSON 形态先脱敏:命中就返回脱敏后的字符串(键类型变了,但不会泄密)。
        try:
            canonical = json.dumps(key)
        except Exception:
            canonical = str(key)
        safe = redact(canonical, secrets)
        return safe if safe != canonical else key
    try:
        return redact(str(key), secrets)
    except Exception:
        return "<key>"


def redact_obj(value, secrets: Iterable[str]):
    """对任意 JSON 结构做脱敏(dict / list / 标量)。

    不能先把结构序列化成 JSON 字符串再替换:序列化成字符串时引号、换行等
    已经被转义,`str.replace` 找不到原样的密钥 → 密钥留在 extra 里发出去;
    而如果密钥本身含 JSON 语法字符,替换还有可能把整条 JSON 弄坏。
    在**结构上**逐个字符串脱敏,两个问题都没有。

    三处容易漏的地方一起处理:
    - dict 的**键**也可能含密钥(_redact_key);
    - 非 JSON 标量(datetime、Decimal、自定义对象…)最终会被 `json.dumps(default=str)`
      转成字符串 —— 必须**先转成字符串再脱敏**,否则 `str(obj)` 里带的密码会原样发出去;
    - 数字、布尔、None 原样保留(转成字符串会改变语义,也可能引入无意义的信息泄漏)。
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {_redact_key(k, secrets): redact_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, secrets) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    # 其它类型最终会被 json.dumps(default=str) 转成字符串:先转再脱敏,顺序不能反。
    try:
        text = str(value)
    except Exception:       # 个别对象的 __str__ 会抛异常,不能因此把整条告警丢掉
        return f"<{type(value).__name__}>"
    return redact(text, secrets)
