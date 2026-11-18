"""外部心跳发送端(notify/heartbeat.py)。

路线图:交易机定期向外部端点推送**脱敏**心跳,外部连续 ≥3 分钟收不到就告警。**只告警,不接管、不平仓**——
所以发送端只发不收:不解析对端回包,也没有任何"按回包执行动作"的入口。
- 间隔必须 ≤60 秒(外部 3 分钟阈值内至少能漏两次才告警,避免偶发丢包误报);
- 外部端点必须是 https(本机回环地址除外,便于测试与本地中继);
- 发送失败不抛异常、不阻塞调用方,只累计连续失败次数。
"""
import json

import pytest

from notify import heartbeat as hb


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(post, clock=None, **kw):
    return hb.HeartbeatSender("https://hb.example.com/ping", interval_seconds=30, secrets=["S3cr3tPass"],
                              host="trader-01", clock=clock or Clock(), post=post, **kw)


def test_payload_is_redacted_and_marked_as_heartbeat():
    sent = []
    s = make(lambda url, body: sent.append((url, json.loads(body))))
    assert s.beat({"equity": 10000, "note": "password=S3cr3tPass", "locked": False}) is True
    url, payload = sent[0]
    assert url == "https://hb.example.com/ping" and payload["kind"] == "heartbeat" and payload["host"] == "trader-01"
    assert "S3cr3tPass" not in json.dumps(payload, ensure_ascii=False)
    assert payload["status"]["equity"] == 10000 and "ts" in payload


def test_throttled_within_interval():
    sent, clock = [], Clock()
    s = make(lambda url, body: sent.append(body), clock=clock)
    assert s.beat({}) is True
    clock.t += 10
    assert s.beat({}) is False                      # 间隔内不重复发送
    clock.t += 25
    assert s.beat({}) is True and len(sent) == 2


def test_failures_never_raise_and_are_counted():
    def boom(url, body):
        raise OSError("network down")
    clock = Clock()
    s = make(boom, clock=clock)
    assert s.beat({}) is False and s.consecutive_failures == 1
    clock.t += 31
    assert s.beat({}) is False and s.consecutive_failures == 2


def test_failure_does_not_consume_the_interval_and_success_resets_counter():
    calls, clock = {"n": 0}, Clock()

    def flaky(url, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("once")
    s = make(flaky, clock=clock)
    assert s.beat({}) is False
    clock.t += 1
    assert s.beat({}) is True and s.consecutive_failures == 0      # 失败后不必等满间隔再重试


def test_response_body_is_never_interpreted():
    s = make(lambda url, body: b'{"action": "flatten"}')         # 对端回包里写什么都不会被执行
    assert s.beat({}) is True
    assert not any(name for name in dir(s) if "flatten" in name or "command" in name)


@pytest.mark.parametrize("kwargs", [
    dict(url="http://hb.example.com/ping"),            # 外部端点必须 https
    dict(interval_seconds=0),
    dict(interval_seconds=61),                         # 超过 60 秒:外部 3 分钟阈值前漏不了两次
    dict(url="ftp://x"),
])
def test_rejects_unsafe_configuration(kwargs):
    base = dict(url="https://hb.example.com/ping", interval_seconds=30)
    base.update(kwargs)
    with pytest.raises(ValueError):
        hb.HeartbeatSender(base.pop("url"), post=lambda u, b: None, **base)


def test_loopback_http_is_allowed_for_local_relay():
    hb.HeartbeatSender("http://127.0.0.1:9000/ping", interval_seconds=30, post=lambda u, b: None)
