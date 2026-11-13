"""已知敏感值的文本脱敏。**脱敏的唯一出处**。

从 notify/webhook.py 抽出(2026-11-12):状态页(dashboard/)要复用它,但 webhook.py 在模块顶部 import requests,
只装了 Web 依赖的环境会因此导入失败。这里只依赖标准库。webhook.py 重新导出了 `redact`,既有调用不受影响。
"""
from collections.abc import Iterable


def redact(text: str, secrets: Iterable[str]) -> str:
    """替换已知敏感值；长度不足 3 的值不替换，避免误伤普通文本。

    长值优先替换，避免短密钥是长密钥前缀时留下后缀。
    """
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text
