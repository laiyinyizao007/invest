"""样例必须自洽。

曾出现过:批量把 `as_of` 改成 `now - age_seconds` 时,把 `trading-stale` 的 as_of 改成了与它自己的
`data.heartbeat.last_beat_at` 矛盾的值(12:10 vs 22:10)——**真实实现保证 as_of 就是心跳时间戳**,
所以那个组合服务器根本产生不出来。而 `trading-stale` 正是 Lovable 指引里指定的验收用例:
前端同事照着它开发,会学到一条不存在的规则。

录制那一侧也是同一类:样例写的是 **UTC** 的心跳,而生产里服务端给的是 +08:00(北京时间)——
前端照着写会把时间格式化差 8 小时。

所以这里断言三件事:
1. 所有样例的 `as_of + age_seconds` 落在**同一个参照 now** 上;
2. trading / recording 类样例的 `as_of` **逐字等于**自己的心跳时间戳(不容差);
3. 录制的时间字段带 +08:00,不是 UTC。
"""
import datetime
import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
FIX = WEB / "fixtures"

# 样例集的参照 now(契约 §3 的示例也是它)
NOW = datetime.datetime.fromisoformat("2026-11-13T08:30:00+08:00")
BEIJING = datetime.timedelta(hours=8)


def _section_fixtures():
    for f in sorted(FIX.glob("*.json")):
        if f.stem == "manifest":
            continue
        body = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(body, dict) and "state" in body and "as_of" in body:
            yield f.stem, body


def test_every_section_fixture_shares_one_reference_now():
    drifted = []
    for name, body in _section_fixtures():
        if body.get("as_of") is None or body.get("age_seconds") is None:
            continue
        ts = datetime.datetime.fromisoformat(body["as_of"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        implied = ts + datetime.timedelta(seconds=body["age_seconds"])
        if implied != NOW:
            drifted.append((name, implied.isoformat()))
    assert not drifted, drifted


def test_the_trading_fixtures_as_of_matches_their_own_heartbeat():
    """真实实现:区块的 as_of 就是心跳时间戳(collect_trading 的 as_of = hb_ts)。"""
    drifted = []
    for name, body in _section_fixtures():
        heartbeat = ((body.get("data") or {}).get("heartbeat") or {})
        last_beat = heartbeat.get("last_beat_at")
        if not last_beat or body.get("as_of") is None:
            continue
        if body["as_of"] != last_beat:
            drifted.append((name, body["as_of"], last_beat))
    assert not drifted, drifted


def test_the_recording_fixtures_as_of_is_their_own_heartbeat_verbatim():
    """真实实现:`as_of` 与 `data["heartbeat_at"]` 是**同一个字符串**,不容差。

    之前的容差(> 2 秒)恰好盖住了样例里的偏差(2.0 秒),而且根本没断言时区写法——
    样例写的是 UTC 的心跳,生产里是 +08:00,前端照着写会差 8 小时。
    """
    drifted = []
    for name, body in _section_fixtures():
        data = body.get("data") or {}
        heartbeat_at = data.get("heartbeat_at")
        if not heartbeat_at or body.get("as_of") is None:
            continue
        if body["as_of"] != heartbeat_at:
            drifted.append((name, body["as_of"], heartbeat_at))
    assert not drifted, drifted


def test_the_recording_time_fields_use_the_exchange_timezone_not_utc():
    """服务端渲染的录制时间带 +08:00(北京时间)。样例写 UTC 会让前端格式化差 8 小时。"""
    drifted = []
    for name, body in _section_fixtures():
        data = body.get("data") or {}
        for field in ("heartbeat_at", "started_at"):
            value = data.get(field)
            if not isinstance(value, str) or "T" not in value:
                continue
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.utcoffset() != BEIJING:
                drifted.append((name, field, value))
    assert not drifted, drifted
