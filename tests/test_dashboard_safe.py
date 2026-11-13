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
