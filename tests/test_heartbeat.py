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


def test_failure_waits_only_the_retry_backoff_and_success_resets_counter():
    calls, clock = {"n": 0}, Clock()

    def flaky(url, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("once")
    s = make(flaky, clock=clock)
    assert s.beat({}) is False
    clock.t += hb.RETRY_SECONDS
    assert s.beat({}) is True and s.consecutive_failures == 0      # 失败后只需等退避时间,不必等满间隔


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


# ---------------------------------------------------------------- G8 第 2 轮(codex 39/50)复现后补的回归

def test_unserializable_status_never_raises():
    """High:payload 的脱敏/序列化原先在 try 之外,循环引用会把 RecursionError 抛给交易进程。"""
    s = make(lambda url, body: None)
    status = {}
    status["self"] = status
    assert s.beat(status) is False and s.consecutive_failures == 1


def test_non_string_and_numeric_secrets_are_redacted():
    """Medium:数字型敏感值(如账户号)在值里是整数时原先不脱敏;非字符串 secret 原先会让 redact 抛 TypeError。"""
    from notify.redact import redact, redact_obj
    assert redact("账户 12345678", [12345678]) == "账户 ***"
    assert redact_obj({"account": 12345678, "ratio": 1.5, "ok": True}, ["12345678"]) == {"account": "***", "ratio": 1.5, "ok": True}
    sent = []
    s = hb.HeartbeatSender("https://hb.example.com/ping", secrets=[12345678], post=lambda u, b: sent.append(b))
    assert s.beat({"account": 12345678}) is True and b"12345678" not in sent[0]


def test_redirects_are_not_followed():
    """Medium:默认 urlopen 会跟随重定向,构造时的 https/回环限制就失效了。"""
    import urllib.request
    opener = hb._build_opener()
    assert not any(isinstance(h, urllib.request.HTTPRedirectHandler) and type(h) is urllib.request.HTTPRedirectHandler
                   for h in opener.handlers)
    req = urllib.request.Request("https://a.example.com/x", data=b"{}", method="POST")
    redirect = hb._NoRedirect()
    with pytest.raises(Exception):
        redirect.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/")


def test_failed_send_backs_off_before_retrying():
    """Low:失败后原先下一次调用立即重试,高频调用会形成重试风暴;现在至少隔 RETRY_SECONDS。"""
    clock = Clock()

    def boom(url, body):
        raise OSError("down")
    s = make(boom, clock=clock)
    assert s.beat({}) is False and s.consecutive_failures == 1
    clock.t += 1
    assert s.beat({}) is False and s.consecutive_failures == 1      # 退避期内不发送
    clock.t += hb.RETRY_SECONDS
    assert s.beat({}) is False and s.consecutive_failures == 2
