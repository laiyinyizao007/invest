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


# ---------------------------------------------------------------- G8 第 2 轮(codex #12)

def test_concurrent_first_start_all_end_up_with_the_same_token_and_none_crashes(tmp_path):
    """两个进程同时首次启动:O_EXCL 保证只有一个创建成功,输家必须读取赢家的文件,而不是抛 FileExistsError(带本机路径的 traceback)。"""
    import threading
    path = tmp_path / "dash" / "status.token"
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            results.append(auth.load_or_create_token(path))
        except Exception as exc:                                  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len({tok for tok, _ in results}) == 1                  # 所有人拿到同一个令牌
    assert sum(1 for _, created in results if created) == 1       # 恰好一个是创建者


def test_the_loser_of_the_race_reads_a_token_that_is_not_yet_fully_written(tmp_path, monkeypatch):
    """赢家创建了文件但还没写完内容时,输家读到空/半截内容——应短暂重试,而不是当作『令牌过短』报错。"""
    path = tmp_path / "t"
    real_exists = type(path).exists
    state = {"n": 0}

    def flaky_read(self, *a, **k):
        state["n"] += 1
        return "" if state["n"] <= 2 else "k" * 43

    monkeypatch.setattr(type(path), "read_text", flaky_read)
    path.write_text  # noqa: B018
    # 直接构造:文件已存在(赢家已创建),内容前两次读到空
    monkeypatch.setattr(type(path), "exists", lambda self: True if self == path else real_exists(self))
    tok, created = auth.load_or_create_token(path)
    assert tok == "k" * 43 and created is False


@pytest.mark.parametrize("exc", [PermissionError(r"denied C:\Users\alice\secret\t"), OSError("disk error /home/alice/x"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])
def test_every_failure_is_wrapped_in_a_token_error_without_leaking_the_path(tmp_path, monkeypatch, exc):
    path = tmp_path / "t"
    path.write_text("x" * 40, encoding="utf-8")
    monkeypatch.setattr(type(path), "read_text", lambda self, *a, **k: (_ for _ in ()).throw(exc))
    with pytest.raises(auth.TokenError) as info:
        auth.load_or_create_token(path)
    assert "alice" not in str(info.value) and str(tmp_path) not in str(info.value)


def test_creation_failure_on_a_read_only_location_is_a_token_error_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(r"no write access to C:\secret")))
    with pytest.raises(auth.TokenError) as info:
        auth.load_or_create_token(tmp_path / "new" / "t")
    assert "secret" not in str(info.value)


def test_the_token_file_is_never_observable_in_a_half_written_state(tmp_path):
    """核心性质:并发创建期间,不停地观察令牌文件——只会看到『不存在』或『内容完整』,绝不会看到空/半截。
    (之前用 O_EXCL 创建再写入,存在文件已创建但仍为空的窗口。)"""
    import threading
    path = tmp_path / "dash" / "status.token"
    seen_bad, stop = [], threading.Event()

    def observer():
        while not stop.is_set():
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if len(text.strip()) < auth.MIN_TOKEN_LENGTH:
                seen_bad.append(text)

    obs = threading.Thread(target=observer)
    obs.start()
    for i in range(30):
        p = tmp_path / f"d{i}" / "status.token"
        path = p
        threads = [threading.Thread(target=auth.load_or_create_token, args=(p,)) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    stop.set()
    obs.join()
    assert seen_bad == []


def test_no_temp_files_are_left_behind(tmp_path):
    p = tmp_path / "dash" / "status.token"
    auth.load_or_create_token(p)
    auth.load_or_create_token(p)
    assert sorted(x.name for x in p.parent.iterdir()) == ["status.token"]


# ---- G8 第 3 轮 #5:回退路径与临时文件 ----
def _leftovers(directory):
    return sorted(p.name for p in directory.iterdir())


def test_fsync_failure_leaves_no_temp_file_containing_the_token(tmp_path, monkeypatch):
    path = tmp_path / "dash" / "status.token"
    monkeypatch.setattr(auth.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(auth.TokenError):
        auth.load_or_create_token(path)
    assert not path.exists()
    assert _leftovers(path.parent) == []                 # 含令牌的临时文件必须被清掉


def test_a_filesystem_without_hard_links_fails_closed_instead_of_publishing_an_empty_file(tmp_path, monkeypatch):
    path = tmp_path / "dash" / "status.token"
    monkeypatch.setattr(auth.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(1, "not supported")))
    with pytest.raises(auth.TokenError) as info:
        auth.load_or_create_token(path)
    assert not path.exists()                              # 不能留下空文件或半成品:否则下次启动会永久遇到『内容过短』
    assert _leftovers(path.parent) == []
    assert str(tmp_path) not in str(info.value) and "硬链接" in str(info.value)
