"""notify/webhook.py 测试:用本机 HTTP 服务器做真实的网络往返,不访问互联网。"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notify.webhook import WebhookNotifier, redact  # noqa: E402


@pytest.fixture
def server():
    """本机 HTTP 服务器:记录收到的 JSON;可通过 state['statuses'] 依次指定返回码(用完后返回 200)。"""
    state = {"received": [], "statuses": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["received"].append(json.loads(body))
            status = state["statuses"].pop(0) if state["statuses"] else 200
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/hook"
    yield state
    httpd.shutdown()


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def make(server, **kw):
    kw.setdefault("sleep", lambda s: None)
    return WebhookNotifier(server["url"], **kw)


# ---------- 格式 ----------

def test_generic_payload_round_trip(server):
    n = make(server, host="vps1")
    assert n.send("ERROR", "当日熔断", "亏损 700 元", extra={"equity": 9300})
    assert wait_for(lambda: n.sent == 1)
    body = server["received"][0]
    assert (body["level"], body["title"], body["message"], body["host"]) == ("ERROR", "当日熔断", "亏损 700 元", "vps1")
    assert body["extra"] == {"equity": 9300} and body["time"]
    n.close()


def test_feishu_payload(server):
    n = make(server, kind="feishu", host="vps1")
    n.send("FATAL", "累计死线", "回撤 2100")
    assert wait_for(lambda: n.sent == 1)
    body = server["received"][0]
    assert body["msg_type"] == "text" and "[FATAL] 累计死线" in body["content"]["text"] and "@vps1" in body["content"]["text"]
    n.close()


def test_telegram_payload(server):
    n = make(server, kind="telegram", chat_id="12345")
    n.send("INFO", "成交", "买开 1 手")
    assert wait_for(lambda: n.sent == 1)
    body = server["received"][0]
    assert body["chat_id"] == "12345" and "[INFO] 成交" in body["text"]
    n.close()


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        WebhookNotifier("http://x", kind="slack")
    with pytest.raises(ValueError):
        WebhookNotifier("http://x", kind="telegram")             # 缺 chat_id
    with pytest.raises(ValueError):
        WebhookNotifier("http://x", min_level="LOUD")


# ---------- 过滤与去重 ----------

def test_disabled_without_url():
    n = WebhookNotifier("")
    assert not n.enabled and n.send("FATAL", "x", "y") is False


def test_level_filter(server):
    n = make(server, min_level="ERROR")
    assert n.send("INFO", "心跳", "ok") is False
    assert n.send("WARN", "注意", "ok") is False
    assert n.send("ERROR", "出错", "ok") is True
    assert wait_for(lambda: n.sent == 1)
    n.close()


def test_duplicate_messages_are_suppressed_but_fatal_is_not(server):
    n = make(server)
    assert n.send("ERROR", "委托被拒", "同一条") is True
    assert n.send("ERROR", "委托被拒", "同一条") is False
    assert n.send("ERROR", "委托被拒", "另一条") is True
    assert n.send("FATAL", "累计死线", "同一条") is True
    assert n.send("FATAL", "累计死线", "同一条") is True          # FATAL 不去重
    assert wait_for(lambda: n.sent == 4)
    assert n.dropped == 1
    n.close()


def test_duplicate_window_expires(server):
    now = [0.0]
    n = make(server, dedup_window=60, clock=lambda: now[0])
    assert n.send("ERROR", "t", "m") and not n.send("ERROR", "t", "m")
    now[0] = 61.0
    assert n.send("ERROR", "t", "m") is True
    n.close()


# ---------- 脱敏 ----------

def test_secrets_are_redacted_everywhere(server):
    n = make(server, secrets=["SuperSecret99", "tok_abcdef"])
    n.send("ERROR", "登录失败 SuperSecret99", "密码 SuperSecret99 token=tok_abcdef", extra={"auth": "tok_abcdef", "n": 1})
    assert wait_for(lambda: n.sent == 1)
    raw = json.dumps(server["received"][0], ensure_ascii=False)
    assert "SuperSecret99" not in raw and "tok_abcdef" not in raw and "***" in raw
    n.close()


def test_short_secrets_are_not_replaced():
    assert redact("a 12 b", ["12"]) == "a 12 b"                    # 太短,替换会误伤正常文本
    assert redact("pw=abcd", ["abcd"]) == "pw=***"


def test_extra_with_non_json_types_does_not_break_sending(server):
    from datetime import datetime
    n = make(server)
    assert n.send("INFO", "t", "m", extra={"when": datetime(2026, 11, 9, 10, 0)})
    assert wait_for(lambda: n.sent == 1)
    n.close()


# ---------- 重试与失败 ----------

def test_retries_with_backoff_then_succeeds(server):
    server["statuses"] = [500, 502]
    sleeps = []
    n = make(server, sleep=sleeps.append)
    n.send("ERROR", "t", "m")
    assert wait_for(lambda: n.sent == 1)
    assert sleeps == [1.0, 3.0] and len(server["received"]) == 3     # 失败 2 次后第 3 次成功
    n.close()


def test_gives_up_after_all_retries_without_raising(server):
    server["statuses"] = [500] * 10
    sleeps = []
    n = make(server, sleep=sleeps.append)
    n.send("ERROR", "t", "m")
    assert wait_for(lambda: n.failed == 1)
    assert sleeps == [1.0, 3.0, 5.0] and n.sent == 0 and n.last_error == "HTTP 500"
    n.close()


def test_unreachable_server_never_raises_and_hides_the_url():
    token_url = "http://127.0.0.1:1/bot-secret-token-123/send"          # 端口 1:连接必然被拒绝
    n = WebhookNotifier(token_url, sleep=lambda s: None, retry_delays=(0.0,), timeout=1.0)
    assert n.send("ERROR", "t", "m") is True                              # 入队成功,调用方不受影响
    assert wait_for(lambda: n.failed == 1)
    assert "bot-secret-token-123" not in n.last_error                     # 异常文本里的 URL(含 token)被脱敏
    n.close()


def test_send_never_blocks_the_caller_even_if_server_is_slow(server):
    started = threading.Event()

    def slow_sleep(seconds):
        started.set()
        time.sleep(0.5)

    server["statuses"] = [500] * 10
    n = make(server, sleep=slow_sleep, retry_delays=(5.0,))
    t0 = time.time()
    n.send("ERROR", "t", "m")
    assert time.time() - t0 < 0.3                                         # 发送(含重试等待)都在后台线程
    n.close(flush_timeout=0.1)


# ---------- 队列 ----------

def test_full_queue_drops_non_fatal_and_keeps_fatal(server):
    n = make(server, max_queue=2)
    n._ensure_started = lambda: None                                      # 不启动后台线程:队列无人消费
    assert n.send("ERROR", "a", "1") and n.send("ERROR", "b", "2")
    assert n.send("ERROR", "c", "3") is False and n.dropped == 1          # 满了:非 FATAL 被丢弃
    assert n.send("FATAL", "d", "4") is True                              # FATAL 挤掉最旧的一条
    titles = [n._queue.get_nowait()["title"] for _ in range(2)]
    assert titles == ["b", "d"]


def test_fatal_evicts_oldest_non_fatal_first_and_only_then_oldest_fatal(server):
    n = make(server, max_queue=3)
    n._ensure_started = lambda: None
    n.send("FATAL", "f1", "1"), n.send("ERROR", "e1", "2"), n.send("ERROR", "e2", "3")
    assert n.send("FATAL", "f2", "4") is True                                  # 挤掉最旧的非 FATAL(e1),而不是 f1
    assert [m["title"] for m in list(n._queue.queue)] == ["f1", "e2", "f2"]
    assert n.send("FATAL", "f3", "5") is True                                  # 再挤掉 e2
    assert [m["title"] for m in list(n._queue.queue)] == ["f1", "f2", "f3"]
    assert n.send("FATAL", "f4", "6") is True                                  # 全是 FATAL 时才挤掉最旧的 FATAL
    assert [m["title"] for m in list(n._queue.queue)] == ["f2", "f3", "f4"]


def test_close_flushes_pending_messages(server):
    n = make(server)
    for i in range(5):
        n.send("ERROR", "t", f"m{i}")
    n.close(flush_timeout=5.0)
    assert len(server["received"]) == 5


def test_close_without_start_is_safe():
    WebhookNotifier("http://x").close()


def test_eviction_never_removes_the_stop_sentinel(server):
    from notify.webhook import _STOP
    n = make(server, max_queue=2)
    n._ensure_started = lambda: None
    n._queue.put_nowait(_STOP)
    n._queue.put_nowait({"level": "FATAL", "title": "f1", "message": "1", "extra": {}, "time": "t"})
    assert n.send("FATAL", "f2", "2") is True
    items = list(n._queue.queue)
    assert items[0] is _STOP and [m["title"] for m in items[1:]] == ["f2"]         # 挤掉的是最旧的 FATAL,停止哨兵保留


def test_eviction_refuses_when_only_the_sentinel_is_in_a_full_queue(server):
    from notify.webhook import _STOP
    n = make(server, max_queue=1)
    n._ensure_started = lambda: None
    n._queue.put_nowait(_STOP)
    assert n.send("FATAL", "f", "1") is False and list(n._queue.queue) == [_STOP]
