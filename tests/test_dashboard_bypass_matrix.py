"""净化层的绕过形态矩阵。

这些形态分散在多轮复核里被逐个发现(零宽字符、%u 编码、无盘符根路径、全角写法……)。
把它们收成一张矩阵当回归测试:以后改净化规则时,一眼能看出有没有漏掉哪种形态。
"""
import pytest

from dashboard import safe

ZW = chr(0x200B)
B = chr(92)

# (名称, 输入, 泄露标记)
BYPASS_CASES = [
    ("盘符路径", r"C:\Users\Alice\secret.env", "Alice"),
    ("盘符+空格目录", r"C:\Users\Alice Smith\secret.env", "Smith"),
    ("无盘符根路径", B + "Users" + B + "Alice" + B + "secret.env", "Alice"),
    ("UNC", B + B + "fileserver" + B + "finance" + B + "secret.xlsx", "fileserver"),
    ("扩展路径", B + B + "?" + B + "C:" + B + "Users" + B + "Alice", "Alice"),
    ("全角盘符+冒号", "Ｃ" + B + "Users" + B + "Alice", "Alice"),
    ("全角冒号", "C：" + B + "Users" + B + "Alice", "Alice"),
    ("全角盘符无冒号", "Ｃ" + B + "Users" + B + "Alice", "Alice"),
    ("零宽夹盘符", "C" + ZW + ":" + B + "Users" + B + "Alice", "Alice"),
    ("零宽夹目录名", B + "Us" + ZW + "ers" + B + "Alice", "Alice"),
    ("零宽夹全角冒号", "C" + ZW + "：" + B + "Users" + B + "Alice", "Alice"),
    ("URL 编码盘符", "%43%3A%5CUsers%5CAlice", "Alice"),
    ("URL 编码根路径", "%5CUsers%5CAlice", "Alice"),
    ("双重编码", "%255CUsers%255CAlice", "Alice"),
    ("三重编码", "%25255CUsers%25255CAlice", "Alice"),
    ("%u 编码", "C%3A%25u005cUsers%25u005cAlice", "Alice"),
    ("POSIX 路径", "/home/alice/secret.env", "alice"),
    ("POSIX 变体", "/Users/alice/secret", "alice"),
    ("小写盘符", r"c:\users\alice\secret.env", "alice"),
    ("带引号", '"' + r"C:\Users\Alice\secret.env" + '"', "Alice"),
    ("混合大小写目录", r"C:\USERS\ALICE\secret.env", "ALICE"),
    ("相对路径带盘符", r"..\C:\Users\Alice\x", "Alice"),
    ("设备路径", r"\\.\pipe\trading-secret", "trading-secret"),
]


@pytest.mark.parametrize("name,text,marker", BYPASS_CASES, ids=[c[0] for c in BYPASS_CASES])
def test_the_path_is_scrubbed_in_every_known_shape(name, text, marker):
    out = safe.scrub_text(text)
    assert marker not in out, (name, out)


@pytest.mark.parametrize("text", [
    "普通中文句子,没有路径",
    "时间 10:30 比例 3:4",
    "https://example.com/a:/b",
    "100%25 complete",
    "%E4%B8%AD%E6%96%87",
    "Ａ：说明",
])
def test_ordinary_text_is_left_alone(text):
    assert safe.scrub_text(text) == text
