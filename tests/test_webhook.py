"""notify/webhook.py 测试:用本机 HTTP 服务器做真实的网络往返,不访问互联网。"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


from notify.webhook import WebhookNotifier, redact, redact_obj  # noqa: E402
import notify.webhook as webhook_module  # noqa: E402


@pytest.fixture
def server():
    """本机 HTTP 服务器:记录收到的 JSON;可通过 state['statuses'] 依次指定返回码(用完后返回 200)。

    响应体按飞书机器人格式给 `{"code":0,"msg":"success"}` —— 真实的飞书永远回 JSON,
    响应体是判"是否真的送达"的重要依据(失败也是 HTTP 200)。
    """
    state = {"received": [], "statuses": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["received"].append(json.loads(body))
            status = state["statuses"].pop(0) if state["statuses"] else 200
            self.send_response(status)
            if status < 300:
                payload = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                # 错误响应不写 body(也不声明 Content-Length),否则客户端会等一个不存在的响应体
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


def test_three_character_secret_is_redacted_through_webhook_path():
    assert redact("auth=abc", ["abc"]) == "auth=***"


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
    n.close(drain_timeout=0.1)


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
    n.close(drain_timeout=5.0)
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


# ---------- 飞书响应体:失败也返回 HTTP 200 ----------

def test_feishu_body_failure_is_not_counted_as_success(server):
    """飞书机器人失败时也返回 HTTP 200,失败信息在 JSON 体里。

    只看状态码会把"根本没送达"当成发送成功 → 告警静默丢失,
    而运行手册的验收标准恰恰是"告警全部到达手机"。
    """
    class BodyHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.dumps({"code": 19001, "msg": "invalid token"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BodyHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        n = WebhookNotifier(f"http://127.0.0.1:{httpd.server_address[1]}/hook", kind="feishu",
                            sleep=lambda s: None)
        n.send("FATAL", "硬锁定", "触发")
        n.close(drain_timeout=5)
        assert n.sent == 0 and n.failed == 1
        assert "19001" in n.last_error
    finally:
        httpd.shutdown()


def test_feishu_body_success_is_counted_as_success(server):
    n = make(server, kind="feishu")
    n.send("INFO", "心跳", "ok")
    assert wait_for(lambda: n.sent == 1)
    assert n.failed == 0


def test_feishu_body_code_as_a_string_is_not_mistaken_for_a_failure():
    """对端把业务码写成字符串 "0" 时仍是成功,不能被当成失败。"""
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": "0", "msg": "success"}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    assert n._body_error(_Resp()) is None
    assert n._body_error(type("R", (), {"json": lambda self: {"StatusCode": "0"}})()) is None


def test_feishu_body_code_as_a_nonzero_string_is_a_failure():
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": "19001", "msg": "invalid token"}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    problem = n._body_error(_Resp())
    assert problem is not None and "19001" in problem


def test_a_non_feishu_webhook_ignores_the_response_body():
    """generic / telegram 没有统一的响应体语义,只看状态码。"""
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 19001}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="generic")
    assert n._body_error(_Resp()) is None


def test_a_feishu_response_without_a_business_code_is_not_counted_as_success():
    """飞书一定会给 code(或旧版的 StatusCode)。

    一个没有业务码的 JSON 体(例如代理网关回 {"error": "..."})无法证明消息
    真的被飞书接收 —— 按成功处理就是静默丢告警。
    """
    class _NoCode:
        status_code = 200

        def json(self):
            return {"error": "not found"}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    problem = n._body_error(_NoCode())
    assert problem is not None and "业务码" in problem


def test_a_feishu_response_that_is_not_json_is_not_counted_as_success():
    """代理/网关可能把请求吞掉并回一个 HTML 2xx 页面。

    真实的飞书一定回 JSON;回的不是 JSON 就说明这条消息**没有**被飞书接收,
    按成功处理就是静默丢告警 —— 而验收标准恰恰是"告警全部到达手机"。
    """
    class _HtmlResp:
        status_code = 200

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    problem = n._body_error(_HtmlResp())
    assert problem is not None and "JSON" in problem


def test_a_feishu_response_that_is_a_json_array_is_not_counted_as_success():
    class _ArrayResp:
        status_code = 200

        def json(self):
            return [{"code": 0}]

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    problem = n._body_error(_ArrayResp())
    assert problem is not None and "JSON" in problem


def test_a_feishu_html_page_is_retried_and_then_reported_as_failed():
    """端到端:回 HTML 的 2xx 端点必须算失败(而不是静默当成已送达)。"""
    class HtmlHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = b"<html><body>gateway</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HtmlHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        n = WebhookNotifier(f"http://127.0.0.1:{httpd.server_address[1]}/hook", kind="feishu",
                            sleep=lambda s: None, retry_delays=(0.0,))
        n.send("FATAL", "硬锁定", "触发")
        n.close(drain_timeout=5)
        assert n.sent == 0 and n.failed == 1
        assert "JSON" in n.last_error
    finally:
        httpd.shutdown()


def test_the_counters_are_updated_under_a_lock():
    """发送线程与后台线程会同时改计数器,不加锁会丢数字。"""
    n = WebhookNotifier("http://127.0.0.1:1/hook", sleep=lambda s: None, retry_delays=(0.0,))
    n._ensure_started = lambda: None
    n.sent = 0
    n.failed = 0
    import threading as _t

    def bump():
        for _ in range(200):
            n._bump("sent", 1)
            n._bump("failed", 1)

    threads = [_t.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert n.sent == 800 and n.failed == 800


# ---------- 脱敏(结构级) ----------

def test_the_extra_is_redacted_inside_nested_structures():
    """extra 里的密钥可能在 dict / list 的任意深度,不能只脱敏顶层。"""
    extra = {"ctx": {"pwd": "sup3rsecret"}, "items": ["sup3rsecret", "普通文本"]}
    safe = redact_obj(extra, ["sup3rsecret"])
    assert safe["ctx"]["pwd"] == "***"
    assert safe["items"][0] == "***"
    assert safe["items"][1] == "普通文本"


def test_a_secret_containing_json_syntax_does_not_break_the_payload():
    """密钥里有引号时,先序列化再替换会把整条 JSON 弄坏。"""
    secret = 'pa"ss'
    safe = redact_obj({"pwd": secret, "n": 1}, [secret])
    assert safe["pwd"] == "***"
    json.dumps(safe)          # 不能抛异常


def test_the_extra_that_reaches_the_server_is_redacted(server):
    """端到端:extra 里的密钥不能原样发出去。"""
    n = make(server, secrets=["sup3rsecret"])
    n.send("ERROR", "当日熔断", "亏了 700 元", extra={"ctx": {"pwd": "sup3rsecret"}})
    assert wait_for(lambda: n.sent == 1)
    body = server["received"][0]
    assert body["extra"]["ctx"]["pwd"] == "***"
    n.close()


def test_a_non_string_value_in_the_extra_is_kept_as_is():
    """数字、布尔、None 不该被脱敏函数改动(脱敏只针对字符串)。"""
    safe = redact_obj({"n": 1, "ok": True, "none": None}, ["x"])
    assert safe == {"n": 1, "ok": True, "none": None}


def test_the_key_of_a_dict_is_redacted_too():
    """键名里带密码时,只脱敏值等于没脱敏 —— 密钥照样会原样发出去。"""
    safe = redact_obj({"sup3rsecret": 1}, ["sup3rsecret"])
    assert safe == {"***": 1}
    assert "sup3rsecret" not in json.dumps(safe)


def test_a_non_string_key_is_kept_as_it_is():
    """JSON 对象的键可以是数字/布尔/None —— 保持原样,别把 int 变成字符串。"""
    safe = redact_obj({1: "a", None: "b", True: "c"}, ["x"])
    assert safe == {1: "a", None: "b", True: "c"}


def test_a_key_that_cannot_be_turned_into_a_string_is_replaced():
    """键的 __str__ 抛异常时不能把整条告警丢掉 —— 换成占位符。"""
    class _BadKey:
        def __str__(self):
            raise RuntimeError("boom")

    safe = redact_obj({_BadKey(): 1}, ["x"])
    assert list(safe.values()) == [1]
    json.dumps(safe)                      # 键已经变成字符串,能正常序列化


def test_a_non_json_value_is_redacted_after_it_becomes_a_string():
    """自定义对象最终会被 json.dumps(default=str) 转成字符串;
    str(obj) 里带着密码时,必须先转成字符串再脱敏,否则原样发出去。"""
    class _Secretive:
        def __str__(self):
            return "登录密码是 sup3rsecret"

    safe = redact_obj({"ctx": _Secretive()}, ["sup3rsecret"])
    assert safe == {"ctx": "登录密码是 ***"}


def test_the_extra_that_reaches_the_server_has_no_secret_in_the_key(server):
    """端到端:键名里的密钥也不能发出去。"""
    n = make(server, secrets=["sup3rsecret"])
    n.send("ERROR", "当日熔断", "亏了 700 元", extra={"sup3rsecret": {"pwd": "sup3rsecret"}})
    assert wait_for(lambda: n.sent == 1)
    body = server["received"][0]
    assert "sup3rsecret" not in json.dumps(body, ensure_ascii=False)
    n.close()


# ---------- 业务码的"无法确认"必须判失败 ----------

def test_a_null_business_code_is_not_counted_as_success():
    """code 为 null 时无法确认送达 —— 按成功处理就是静默丢告警。"""
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": None, "msg": ""}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    problem = n._body_error(_Resp())
    assert problem is not None and "None" in problem


def test_an_empty_business_code_is_not_counted_as_success():
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": "", "msg": ""}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    assert n._body_error(_Resp()) is not None


def test_a_non_numeric_business_code_is_not_counted_as_success():
    class _Resp:
        status_code = 200

        def json(self):
            return {"code": "ok", "msg": ""}

    n = WebhookNotifier("http://127.0.0.1:1/hook", kind="feishu")
    assert n._body_error(_Resp()) is not None


# ---------- 排空队列的等待时间 ----------

def test_the_drain_timeout_covers_the_whole_queue():
    """队列里排着 N 条时,close() 的等待必须够 N 倍 —— 否则最后几条会被丢掉。"""
    n = WebhookNotifier("http://127.0.0.1:1/hook", timeout=1.0, retry_delays=(1.0,))
    # 不启动后台线程:消息留在队列里,才能稳定地测"队列里有 N 条"这个状态
    n._ensure_started = lambda: None
    assert n.drain_timeout == n.flush_timeout
    for i in range(3):
        n.send("INFO", "心跳", f"第 {i} 条")
    assert n.drain_timeout == pytest.approx(n.flush_timeout * 3)


def test_close_waits_for_the_queue_to_be_drained(server):
    """close() 返回时,队列里的消息必须已经发出去(sent 计数要够)。"""
    n = make(server)
    for i in range(5):
        n.send("INFO", "心跳", f"第 {i} 条")
    n.close(drain_timeout=10)
    assert n.sent == 5


# ---------- 正在发送中的那条消息也要算进排空预算 ----------

def _hanging_server(release: threading.Event, entered: threading.Event):
    """本机服务器:接收请求后卡住不响应,用来制造"正在发送中"的状态。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            entered.set()
            release.wait(10)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/hook"


def test_the_drain_timeout_counts_the_message_that_is_being_sent():
    """worker 已经取走、但还没发完的那条不在 qsize() 里。

    漏掉它 → close() 少等一条 → 退出时最后那条 FATAL 告警随进程一起消失,
    而这恰恰是本轮想兜底的场景。
    """
    release, entered = threading.Event(), threading.Event()
    httpd, url = _hanging_server(release, entered)
    try:
        n = WebhookNotifier(url, timeout=5.0, retry_delays=())
        n.send("FATAL", "还有持仓", "退出前告警")
        assert entered.wait(5), "后台线程没有开始发送"
        assert n._queue.qsize() == 0, "消息已被 worker 取走,队列应当是空的"
        # 排空预算必须把它算进去
        assert n.drain_timeout == pytest.approx(n.flush_timeout)
        n.send("INFO", "心跳", "第二条还排在队列里")
        assert n.drain_timeout == pytest.approx(n.flush_timeout * 2)
        release.set()
        n.close(drain_timeout=10)
    finally:
        release.set()
        httpd.shutdown()


def test_the_fatal_message_is_moved_to_the_front_of_the_queue():
    """退出时最重要的告警("还有持仓")不该排在几十条 INFO 后面。"""
    n = WebhookNotifier("http://127.0.0.1:1/hook")
    n._ensure_started = lambda: None          # 不启动后台线程,让队列保持可控
    n.send("INFO", "普通", "第 1 条")
    n.send("FATAL", "还有持仓", "退出前告警")
    assert n._queue.queue[0]["level"] == "INFO"
    n._prioritize_fatal()
    assert n._queue.queue[0]["level"] == "FATAL"
    assert n._queue.queue[-1]["level"] == "INFO"


def test_close_does_not_wait_forever_when_the_endpoint_hangs(monkeypatch):
    """排空预算有上限:对端一直不响应时,不能让退出流程被吊住。"""
    monkeypatch.setattr(webhook_module, "MAX_DRAIN_TIMEOUT", 0.3)
    release, entered = threading.Event(), threading.Event()
    httpd, url = _hanging_server(release, entered)
    try:
        n = WebhookNotifier(url, timeout=5.0, retry_delays=())
        n.send("INFO", "心跳", "会卡住")
        assert entered.wait(5)
        started = time.monotonic()
        n.close(drain_timeout=9999)
        assert time.monotonic() - started < 3, "close() 没有按上限返回"
    finally:
        release.set()
        httpd.shutdown()


def test_close_stops_the_worker_even_when_the_queue_is_full():
    """队列满的时候停止哨兵放不进去。

    只靠哨兵停线程的话,worker 永远收不到停止信号 → close() 只能干等到预算耗尽,
    交易进程被吊在退出阶段。所以另外用独立的"已关闭"标志,排空后自行退出。
    """
    release, entered = threading.Event(), threading.Event()
    httpd, url = _hanging_server(release, entered)
    try:
        n = WebhookNotifier(url, timeout=5.0, retry_delays=(), max_queue=2)
        n.send("INFO", "心跳", "第 1 条")      # 被 worker 取走,卡在发送里
        assert entered.wait(5)
        n.send("INFO", "心跳", "第 2 条")      # 队列里 1 条
        n.send("INFO", "心跳", "第 3 条")      # 队列满(2 条)
        assert n._queue.qsize() == 2
        release.set()
        n.close(drain_timeout=5)
        assert n._thread is not None and not n._thread.is_alive(), "后台线程没有停下来"
    finally:
        release.set()
        httpd.shutdown()


def test_send_after_close_is_refused():
    """关闭之后再收的消息只会躺在队列里没人发 —— 必须明确拒绝,不能让调用方以为收下了。"""
    n = WebhookNotifier("http://127.0.0.1:1/hook")
    n._ensure_started = lambda: None
    n.close()
    assert n.send("FATAL", "还有持仓", "关闭之后才发的") is False


def test_a_numeric_key_equal_to_the_secret_is_redacted():
    """数字键在 `json.dumps` 里会变成字符串("12345")——
    键恰好等于密钥时,原样保留就等于把它发出去了。"""
    obj = redact_obj({12345: "值"}, ["12345"])
    assert "12345" not in json.dumps(obj, ensure_ascii=False)
