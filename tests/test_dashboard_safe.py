"""dashboard/safe.py:所有响应序列化前的唯一净化出口。账号、令牌、本机路径、环境变量、非有限数都不得漏出。"""
import json
import math

import pytest

from dashboard import safe

ACCOUNT = "66170099"            # 虚构账号(个人信息只放 .env;测试数据一律虚构)
TOKEN = "tok_9f8e7d6c5b4a39281706"


def test_mask_account_keeps_head_and_tail_only_and_hides_length():
    assert safe.mask_account(ACCOUNT) == "66****99"
    assert safe.mask_account("123456789012345") == "12****45"          # 长度不同,星号数不变
    assert safe.mask_account("abc") == "****"
    assert safe.mask_account("") == "****"
    assert safe.mask_account(None) == "****"


@pytest.mark.parametrize("text,leak", [
    (f"Authorization: Bearer {TOKEN}", TOKEN),
    ("bearer abcdefgh12345678", "abcdefgh12345678"),
    (r"日志来源: C:\Dev\projects\invest\runtime\vntrader\log", r"C:\Dev"),
    ("读取 /home/avery/invest/data/state/x.json 失败", "/home/avery"),
    ("CTP_PASSWORD=hunter2hunter2", "hunter2"),
    ("export CTP_AUTH_CODE: ABCDEF0123456789", "ABCDEF0123456789"),
])
def test_scrub_text_removes_known_leak_shapes(text, leak):
    out = safe.scrub_text(text)
    assert leak not in out and out


def test_scrub_text_redacts_explicit_secrets_via_the_single_source_redact():
    assert TOKEN not in safe.scrub_text(f"x {TOKEN} y", secrets=[TOKEN])
    assert ACCOUNT not in safe.scrub_text(f"账户 {ACCOUNT} 已锁定", secrets=[ACCOUNT])


def test_scrub_text_keeps_ordinary_text_and_newlines():
    text = "候选评估(类 Candidate)\n结论: 未通过\n夏普 -1.84,往返 156 笔"
    assert safe.scrub_text(text) == text


def test_scrub_text_clips_long_values():
    out = safe.scrub_text("x" * 5000, limit=300)
    assert len(out) <= 301 and out.endswith("…")


def test_sanitize_masks_account_keys_and_hides_sensitive_keys():
    out = safe.sanitize({"account_id": ACCOUNT, "password": "p", "CTP_AUTH_CODE": "a", "api_key": "k", "last_equity": 9870.5})
    assert out["account_id"] == "66****99"
    assert out["password"] == "***" and out["CTP_AUTH_CODE"] == "***" and out["api_key"] == "***"
    assert out["last_equity"] == 9870.5


def test_sanitize_walks_nested_structures_and_scrubs_every_string():
    data = {"a": [{"msg": f"token {TOKEN}", "path": r"D:\secret\x.txt"}], "b": ("x", 1)}
    blob = json.dumps(safe.sanitize(data, secrets=[TOKEN]), ensure_ascii=False)
    assert TOKEN not in blob and r"D:\\secret" not in blob and "D:" not in blob


def test_sanitize_makes_output_json_safe():
    out = safe.sanitize({"nan": math.nan, "inf": math.inf, "n": None, "t": True, "i": 3, "p": __import__("pathlib").Path("/x")})
    json.dumps(out, allow_nan=False)                    # 不抛异常即可
    assert out["nan"] is None and out["inf"] is None and out["n"] is None and out["t"] is True and out["i"] == 3


def test_sanitize_caps_list_length_and_depth():
    assert len(safe.sanitize(list(range(1000)), max_items=200)) == 200
    deep = cur = {}
    for _ in range(40):
        cur["x"] = {}
        cur = cur["x"]
    assert "too deep" in json.dumps(safe.sanitize(deep))


def test_sanitize_does_not_mutate_its_input():
    src = {"account_id": ACCOUNT, "items": [1, 2, 3]}
    safe.sanitize(src)
    assert src == {"account_id": ACCOUNT, "items": [1, 2, 3]}


def test_property_no_input_string_keeps_a_registered_secret():
    """属性式检查:无论秘密出现在哪、被什么包围,脱敏后都不存在。"""
    import random
    rnd = random.Random(7)
    alphabet = "abc XYZ-_:=/\\\n中文 \"'"
    for _ in range(300):
        pre = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 20)))
        post = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 20)))
        assert TOKEN not in safe.scrub_text(pre + TOKEN + post, secrets=[TOKEN])


@pytest.mark.parametrize("key", ["passed", "bypass", "compass", "pass_rate", "passes", "passing"])
def test_ordinary_keys_that_merely_contain_pass_are_not_hidden(key):
    """回归:『passed』曾被当成密码键整个替换成 ***,研究页的通过状态因此永远显示星号。"""
    assert safe.sanitize({key: False})[key] is False


@pytest.mark.parametrize("key", ["password", "PASSWORD", "db_password", "passwd", "pass", "user-pass", "api_key", "apiKey", "secret_x",
                                 "auth_token", "CTP_AUTH_CODE", "app_id", "private_key", "credentials"])
def test_genuinely_sensitive_keys_are_still_hidden(key):
    assert safe.sanitize({key: "value"})[key] == "***"


def test_every_contract_fixture_survives_sanitize_unchanged():
    """净化层不能把契约里的合法数据改掉:全部示例经 sanitize 后必须与原样相等。"""
    import json
    from pathlib import Path
    fx = Path(__file__).resolve().parents[1] / "docs" / "web" / "fixtures"
    for p in sorted(fx.glob("*.json")):
        if p.stem == "manifest":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        assert safe.sanitize(data) == data, p.stem


# ---------------------------------------------------------------- G8 第 2 轮(codex #8):净化层的具体泄露输入

def test_a_numeric_account_id_is_masked_too_not_only_strings():
    """曾经:ACCOUNT_KEYS 命中后仍要求值是 str,{"account_id": 66170099} 原样返回。"""
    assert safe.sanitize({"account_id": 66170099})["account_id"] == "66****99"
    assert safe.sanitize({"account_id": 66170099.0})["account_id"] != 66170099.0


@pytest.mark.parametrize("key", ["account_no", "accountNo", "account_number", "AccountNumber", "broker_account", "login_id", "loginId",
                                 "investor_id", "InvestorID", "user_name", "UserName", "user-id", "client_account"])
def test_account_like_keys_are_masked_regardless_of_spelling(key):
    out = safe.sanitize({key: "66170099"})[key]
    assert "66170099" not in str(out) and out != "66170099"


@pytest.mark.parametrize("key", ["pwd", "PWD", "passwd", "authorization", "Authorization", "cookie", "Set-Cookie", "session", "session_id", "sessionid",
                                 "x-api-key", "access_token", "refresh_token", "client_secret", "credentials", "private-key"])
def test_credential_like_keys_are_hidden_regardless_of_spelling(key):
    assert safe.sanitize({key: "s3cr3t-value"})[key] == "***"


@pytest.mark.parametrize("text,leak", [
    ("PASSWORD=hunter2", "hunter2"), ("password: hunter2 ok", "hunter2"), ("pwd=hunter2", "hunter2"), ("passwd = hunter2", "hunter2"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"), ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Cookie: sid=abc123def456; theme=dark", "abc123def456"), ("api_key=AKIA1234567890", "AKIA1234567890"), ("token=abcdef123456", "abcdef123456"),
    ("secret: s3cr3tvalue", "s3cr3tvalue"), ("AUTH_CODE=ABCDEF0123456789", "ABCDEF0123456789"),
    (r"UNC=\\server\share\secret\file.txt", "server"), (r"见 \\?\C:\Users\me\.env", "Users"), (r"\\fileserver\finance\2026.xlsx", "fileserver"),
    (r"\\.\pipe\trading-secret", "trading-secret"), (r"路径 \\nas01\backup\accounts.csv 不可读", "nas01"),
    ("读取 C%3A%5CUsers%5Cme%5C.env 失败", "Users%5Cme"), ("path=%2Fhome%2Favery%2Finvest%2F.env", "avery"), ("see c%3a%5cdev%5cprojects", "dev%5cprojects"),
    ("打开 /private/var/folders/xx/secret.txt", "/private/var"), ("in /workspace/invest/data/state/x.json", "/workspace/invest"),
    ("x /Volumes/Backup/accounts.csv", "/Volumes/Backup"), ("/usr/local/lib/python3.13/site-packages/x.py", "/usr/local"),
])
def test_text_leaks_found_by_the_second_reviewer_are_scrubbed(text, leak):
    out = safe.scrub_text(text)
    assert leak not in out, out


@pytest.mark.parametrize("text", [
    "候选评估(类 Candidate)\n结论: 未通过\n夏普 -1.84,往返 156 笔", "passed 3 of 5 checks", "bypass flag is off", "pass rate 0.5",
    "token bucket 容量 8", "2026/11/12 09:00:01", "比例 1/2/3", "sha256 9f2c5a1d0b7e4c33a1f0e8d6b2c4a7f1", "c2701.DCE 多开 1@2400",
    "请求 /api/trading 返回 401", "文档在 docs/web/README.md", "key=value 但不是敏感词: name=demo", "version 1.0.0",
])
def test_ordinary_text_with_similar_words_is_left_alone(text):
    assert safe.scrub_text(text) == text


def test_scrub_cannot_be_defeated_by_unicode_lookalikes_of_the_key_names():
    """全角/变体写法:先做 NFKC 规范化再判断键名。"""
    assert safe.sanitize({"ｐａｓｓｗｏｒｄ": "x", "ＰＷＤ": "y"}) == {"ｐａｓｓｗｏｒｄ": "***", "ＰＷＤ": "***"}


def test_an_account_key_holding_an_object_is_recursed_into_not_stringified():
    """回归(契约示例暴露):live.data.account 是资金对象 {balance, ...},键名恰好叫 account。
    账号掩码只对标量(字符串/数字)生效;容器必须继续递归,其中真正的账号键仍会被各自掩码。"""
    out = safe.sanitize({"account": {"balance": 9870.5, "available": 8900.0, "account_id": "66170099"}})
    assert out["account"]["balance"] == 9870.5 and out["account"]["available"] == 8900.0
    assert out["account"]["account_id"] == "66****99"
    assert safe.sanitize({"account": ["a", "b"]})["account"] == ["a", "b"]


def test_a_secret_key_holding_an_object_is_hidden_entirely():
    assert safe.sanitize({"credentials": {"user": "x", "pass": "y"}}) == {"credentials": "***"}


# ---- G8 第 3 轮 #9:编码形式的路径(单层 / 双层百分号编码) ----
@pytest.mark.parametrize("text,leak", [
    ("读取 %5C%5Cfileserver%5Cfinance%5C2026.xlsx 失败", "fileserver"),
    ("读取 %255C%255Cfileserver%255Cfinance 失败", "fileserver"),
    ("path=%252Fhome%252Favery%252Finvest%252F.env", "avery"),
    ("path=%2Fusr%2Flocal%2Flib%2Fx.py", "usr"),
    ("file=C%253A%255CUsers%255Cme%255C.env", "Users"),
    ("url=https://x/?p=%255C%255Cnas01%255Cbackup", "nas01"),
    ("see %2FVolumes%2FBackup%2Faccounts.csv", "Backup"),
])
def test_encoded_unc_and_double_encoded_paths_are_scrubbed(text, leak):
    out = safe.scrub_text(text)
    assert leak not in out, out


@pytest.mark.parametrize("text", [
    "进度 100%25 完成", "a%20b%20c", "https://example.com/a%2Fb?q=1%262", "折扣 50% off", "%E4%B8%AD%E6%96%87 (中文)", "sha256=9f2c%5a1d", "比例 3%/4%",
])
def test_ordinary_percent_text_is_left_alone(text):
    assert safe.scrub_text(text) == text


def test_urls_are_not_mistaken_for_drive_letter_paths():
    """曾经 `s://` 被当成盘符 `s:/`,https://example.com 会变成 http<path>。"""
    assert safe.scrub_text("见 https://example.com/docs 与 ftp://host/x") == "见 https://example.com/docs 与 ftp://host/x"
    assert "Users" not in safe.scrub_text("file:///C:/Users/me/.env")
