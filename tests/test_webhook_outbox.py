"""通知器的落盘待补发队列(store-and-forward):网络中断期间产生的告警不能丢,恢复后补发。

起因:夜盘真实切断网络 240 秒的演练里,行情假死/断线告警在断网期间产生,恢复后也没有补发
(通知器只重试 3 次、约十几秒就放弃),手机永远收不到。
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import notify.webhook as wh
from notify.webhook import WebhookNotifier


class Net:
    """假网络:up=False 时 requests.post 抛连接错误;记录每次发出的 payload。"""

    def __init__(self):
        self.up = True
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        if not self.up:
            raise ConnectionError("network is down")
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})


@pytest.fixture
def net(monkeypatch):
    fake = Net()
    monkeypatch.setattr(wh.requests, "post", fake.post)
    return fake


def make(tmp_path, **kw):
    args = dict(kind="telegram", chat_id="123", retry_delays=(0.0,), sleep=lambda s: None, outbox_path=tmp_path / "outbox.jsonl",
                redeliver_interval=0.0, host="h", dedup_window=0.0, now=lambda: datetime(2026, 11, 12, 23, 0, 0))
    args.update(kw)
    return WebhookNotifier("http://x/bot/send", **args)


def item(level="ERROR", title="t", message="m", when="2026-11-12T22:46:24"):
    return {"level": level, "title": title, "message": message, "extra": {}, "time": when}


def texts(net):
    return [c["text"] for c in net.calls]


def lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------- 暂存 ----------

def test_failed_delivery_is_deferred_to_the_outbox_instead_of_lost(tmp_path, net):
    n = make(tmp_path)
    net.up = False

    n._deliver(item(title="网关连接断开"))

    assert n.deferred == 1 and n.failed == 0 and n.pending_outbox == 1
    assert lines(tmp_path / "outbox.jsonl")[0]["title"] == "网关连接断开"


def test_without_an_outbox_failures_are_still_counted_as_failed(tmp_path, net):
    n = make(tmp_path, outbox_path=None)
    net.up = False

    n._deliver(item())

    assert n.failed == 1 and n.deferred == 0 and n.pending_outbox == 0


def test_outbox_never_stores_secrets(tmp_path, net):
    n = make(tmp_path, secrets=["hunter2-secret"])
    net.up = False
    assert n.send("ERROR", "登录失败", "密码 hunter2-secret 错误") is True

    n.close(drain_timeout=5.0)

    assert "hunter2-secret" not in (tmp_path / "outbox.jsonl").read_text(encoding="utf-8")


# ---------- 补发 ----------

def test_redelivery_sends_deferred_alerts_once_the_network_is_back_and_marks_them(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    n._deliver(item(level="ERROR", title="网关连接断开", when="2026-11-12T22:46:24"))
    net.up, net.calls = True, []

    sent = n.process_outbox_once()

    assert sent == 1 and n.redelivered == 1 and n.pending_outbox == 0
    assert "【补发】" in texts(net)[0] and "网关连接断开" in texts(net)[0] and "2026-11-12T22:46:24" in texts(net)[0]
    assert lines(tmp_path / "outbox.jsonl") == []


def test_redelivery_stops_at_the_first_failure_and_keeps_everything(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    for i in range(3):
        n._deliver(item(level="ERROR", title=f"e{i}"))
    net.calls.clear()

    sent = n.process_outbox_once()                      # 网络仍然断着

    assert sent == 0 and len(net.calls) == 1            # 只试一次,不对每条都等超时
    assert n.pending_outbox == 3


def test_fatal_and_error_alerts_are_redelivered_individually_oldest_first(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    n._deliver(item(level="ERROR", title="第一条", when="2026-11-12T22:00:00"))
    n._deliver(item(level="FATAL", title="第二条", when="2026-11-12T22:01:00"))
    net.up, net.calls = True, []

    n.process_outbox_once()

    assert "第一条" in texts(net)[0] and "第二条" in texts(net)[1]


def test_many_info_alerts_are_merged_into_one_digest(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    for i in range(5):
        n._deliver(item(level="INFO", title=f"网关已连接{i}", when=f"2026-11-12T22:0{i}:00"))
    net.up, net.calls = True, []

    n.process_outbox_once()

    assert len(net.calls) == 1 and "摘要" in texts(net)[0]
    assert all(f"网关已连接{i}" in texts(net)[0] for i in range(5))
    assert n.pending_outbox == 0 and n.redelivered == 5


def test_few_info_alerts_are_sent_individually(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    n._deliver(item(level="INFO", title="a"))
    n._deliver(item(level="INFO", title="b"))
    net.up, net.calls = True, []

    n.process_outbox_once()

    assert len(net.calls) == 2


def test_digest_is_capped_so_a_long_outage_cannot_flood_the_phone(tmp_path, net):
    n = make(tmp_path, outbox_max=500)
    net.up = False
    for i in range(60):
        n._deliver(item(level="INFO", title=f"i{i}"))
    net.up, net.calls = True, []

    n.process_outbox_once()

    body = texts(net)[0]
    assert body.count("\n") <= wh.DIGEST_MAX_LINES + 6 and "其余" in body


def test_failed_digest_is_kept_for_the_next_attempt(tmp_path, net):
    n = make(tmp_path)
    net.up = False
    for i in range(4):
        n._deliver(item(level="INFO", title=f"i{i}"))

    assert n.process_outbox_once() == 0 and n.pending_outbox == 4


# ---------- 上限与过期 ----------

def test_alerts_older_than_the_max_age_are_expired_not_sent(tmp_path, net):
    now = datetime(2026, 11, 13, 22, 0, 0)
    n = make(tmp_path, outbox_max_age=3600.0, now=lambda: now)
    net.up = False
    n._deliver(item(level="ERROR", title="旧", when=(now - timedelta(hours=5)).isoformat(timespec="seconds")))
    n._deliver(item(level="ERROR", title="新", when=(now - timedelta(minutes=5)).isoformat(timespec="seconds")))
    net.up, net.calls = True, []

    n.process_outbox_once()

    assert n.expired == 1 and len(net.calls) == 1 and "新" in texts(net)[0]


def test_outbox_cap_drops_the_oldest_non_fatal_first(tmp_path, net):
    n = make(tmp_path, outbox_max=3)
    net.up = False
    n._deliver(item(level="FATAL", title="致命", when="2026-11-12T22:00:00"))
    for i in range(4):
        n._deliver(item(level="INFO", title=f"i{i}", when=f"2026-11-12T22:0{i + 1}:00"))

    titles = [e["title"] for e in lines(tmp_path / "outbox.jsonl")]

    assert "致命" in titles and len(titles) == 3 and "i0" not in titles
    assert n.dropped >= 2


# ---------- 持久化 ----------

def test_a_new_notifier_picks_up_alerts_left_by_a_previous_process(tmp_path, net):
    first = make(tmp_path)
    net.up = False
    first._deliver(item(title="上次进程留下的"))
    net.calls.clear()

    import time
    second = make(tmp_path, redeliver_interval=3600.0)      # 启动时立刻试一次;网络仍断着,失败后保留
    deadline = time.time() + 5
    while not net.calls and time.time() < deadline:
        time.sleep(0.02)
    assert second.pending_outbox == 1 and net.calls          # 新进程确实主动尝试补发了上次的遗留
    net.up, net.calls = True, []

    assert second.process_outbox_once() == 1
    assert "上次进程留下的" in texts(net)[0]
    second.close(drain_timeout=2.0)


def test_corrupt_outbox_lines_are_ignored(tmp_path, net):
    (tmp_path / "outbox.jsonl").write_text('{broken\n' + json.dumps(item(title="好的")) + "\n\n[1,2]\n", encoding="utf-8")

    net.up = False                                         # 避免启动时的后台补发把它发掉

    n = make(tmp_path, redeliver_interval=3600.0)

    assert n.pending_outbox == 1
    n.close(drain_timeout=2.0)


def test_close_persists_alerts_still_waiting_in_the_queue(tmp_path, net):
    n = make(tmp_path)
    n._queue.put_nowait(item(level="FATAL", title="退出时还没发的"))

    n._persist_pending_queue()

    assert [e["title"] for e in lines(tmp_path / "outbox.jsonl")] == ["退出时还没发的"]


# ---------- 已知断网时不再逐条走完整重试 ----------

def test_when_the_network_is_known_down_new_alerts_make_a_single_attempt(tmp_path, net):
    sleeps = []
    n = make(tmp_path, retry_delays=(1.0, 3.0, 5.0), sleep=sleeps.append, redeliver_interval=30.0)
    net.up = False
    n._deliver(item(title="第一条"))                     # 完整重试 4 次后暂存
    assert len(net.calls) == 4
    net.calls.clear()
    sleeps.clear()

    n._deliver(item(title="第二条"))

    assert len(net.calls) == 1 and sleeps == [] and n.pending_outbox == 2


# ---------- 后台线程端到端 ----------

def test_background_worker_redelivers_after_the_network_recovers(tmp_path, net):
    import time
    n = make(tmp_path, redeliver_interval=0.05)
    net.up = False
    assert n.send("ERROR", "网关连接断开", "交易服务器:原因8193") is True
    deadline = time.time() + 5
    while n.pending_outbox == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert n.pending_outbox == 1

    net.up = True
    deadline = time.time() + 5
    while n.pending_outbox and time.time() < deadline:
        time.sleep(0.02)
    n.close(drain_timeout=5.0)

    assert n.pending_outbox == 0 and n.redelivered == 1
    assert any("【补发】" in t and "网关连接断开" in t for t in texts(net))


def test_enabled_notifier_without_outbox_keeps_the_legacy_behaviour(tmp_path, net):
    n = make(tmp_path, outbox_path=None)

    assert n.send("INFO", "x", "y") is True
    n.close(drain_timeout=5.0)

    assert n.sent == 1 and n.pending_outbox == 0


# ---------- 真实 HTTP 往返(不打桩):端口无人监听 -> 暂存;服务器起来 -> 自动补发 ----------

def test_real_http_outage_then_recovery_redelivers_with_a_real_server(tmp_path):
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                          # 先让这个端口没人监听 -> 连接被拒,等价于断网
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    n = WebhookNotifier(f"http://127.0.0.1:{port}/bot/send", kind="telegram", chat_id="1", retry_delays=(0.0,), timeout=1.0,
                        outbox_path=tmp_path / "outbox.jsonl", redeliver_interval=0.1, host="h", dedup_window=0.0)
    assert n.send("ERROR", "网关连接断开", "交易服务器:原因8193") is True
    deadline = time.time() + 5
    while n.pending_outbox == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert n.pending_outbox == 1 and received == []

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)           # 「网络恢复」
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        deadline = time.time() + 8
        while n.pending_outbox and time.time() < deadline:
            time.sleep(0.05)
        n.close(drain_timeout=3.0)
    finally:
        server.shutdown()

    assert n.pending_outbox == 0 and n.redelivered == 1
    assert len(received) == 1 and "【补发】" in received[0]["text"] and "网关连接断开" in received[0]["text"]
