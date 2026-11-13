"""dashboard/auth.py:令牌文件、常量时间比较、限流。"""
import os
import stat
import sys

import pytest

from dashboard import auth


def test_creates_a_random_token_file_once_and_reuses_it(tmp_path):
    p = tmp_path / "state" / "status.token"
    t1, created1 = auth.load_or_create_token(p)
    t2, created2 = auth.load_or_create_token(p)
    assert created1 is True and created2 is False and t1 == t2 and len(t1) >= 40
    assert p.read_text(encoding="utf-8").strip() == t1


def test_two_fresh_tokens_differ(tmp_path):
    a, _ = auth.load_or_create_token(tmp_path / "a")
    b, _ = auth.load_or_create_token(tmp_path / "b")
    assert a != b


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不支持 POSIX 权限位")
def test_new_token_file_is_owner_only(tmp_path):
    p = tmp_path / "t"
    auth.load_or_create_token(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


@pytest.mark.parametrize("content", ["", "short", "x" * 23 + "\n"])
def test_weak_existing_token_is_refused_not_silently_replaced(tmp_path, content):
    p = tmp_path / "t"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(auth.TokenError):
        auth.load_or_create_token(p)
    assert p.read_text(encoding="utf-8") == content


TOKEN = "t" * 40


@pytest.mark.parametrize("header,ok", [
    (f"Bearer {TOKEN}", True), (f"bearer {TOKEN}", True), (f"BEARER   {TOKEN}  ", True),
    (None, False), ("", False), (TOKEN, False), (f"Basic {TOKEN}", False), ("Bearer ", False),
    (f"Bearer {TOKEN}x", False), (f"Bearer {TOKEN[:-1]}", False), ("Bearer " + "é" * 40, False),
])
def test_token_matching(header, ok):
    assert auth.token_matches(TOKEN, header) is ok


def test_token_comparison_uses_constant_time_compare(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: (calls.append(1), real(a, b))[1])
    auth.token_matches(TOKEN, f"Bearer {TOKEN}")
    assert calls


def test_rate_limiter_allows_up_to_limit_then_blocks_then_recovers():
    now = [1000.0]
    rl = auth.RateLimiter(3, lambda: now[0])
    assert [rl.check() for _ in range(3)] == [None, None, None]
    wait = rl.check()
    assert wait is not None and 1.0 <= wait <= 60.0
    now[0] += 61
    assert rl.check() is None


def test_rate_limiter_window_slides_not_resets():
    now = [0.0]
    rl = auth.RateLimiter(2, lambda: now[0])
    rl.check()
    now[0] = 30
    rl.check()
    now[0] = 59
    assert rl.check() is not None           # 前两次都还在窗口里
    now[0] = 61
    assert rl.check() is None               # 第一次已滑出窗口
