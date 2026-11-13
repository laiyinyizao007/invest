"""样例必须自洽。

曾出现过:批量把 `as_of` 改成 `now - age_seconds` 时,把 `trading-stale` 的 as_of 改成了与它自己的
`data.heartbeat.last_beat_at` 矛盾的值(12:10 vs 22:10)——**真实实现保证 as_of 就是心跳时间戳**,
所以那个组合服务器根本产生不出来。而 `trading-stale` 正是 Lovable 指引里指定的验收用例:
前端同事照着它开发,会学到一条不存在的规则。

所以这里断言两件事:
1. 所有样例的 `as_of + age_seconds` 落在**同一个参照 now** 上;
2. trading 类样例的 `as_of` 等于它自己的心跳时间戳。
"""
import datetime
import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
FIX = WEB / "fixtures"

# 样例集的参照 now(契约 §3 的示例也是它)
NOW = datetime.datetime.fromisoformat("2026-11-13T08:30:00+08:00")


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


def test_the_recording_fixtures_as_of_matches_their_own_heartbeat():
    drifted = []
    for name, body in _section_fixtures():
        data = body.get("data") or {}
        hb = data.get("heartbeat_at")
        if not hb or body.get("as_of") is None:
            continue
        parsed = datetime.datetime.fromisoformat(hb)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        if abs((parsed - datetime.datetime.fromisoformat(body["as_of"])).total_seconds()) > 2:
            drifted.append((name, body["as_of"], hb))
    assert not drifted, drifted
