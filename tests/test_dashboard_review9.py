"""TTL 缓存的容量与预热(codex 第十二次复核的 High:超过 32 个战役缓存就完全失效)。

曾把两类键混在一个 dict 里(内容哈希键 + 路径 TTL 键),每个战役占两个条目,
上限 64 → 有效容量只有 32;淘汰又是 clear() 全部清空。于是越过 32 个战役后
每次写入都清空缓存,TTL 永远不命中,总览每 8 秒轮询就重读一次全部台账
(实测 50 个战役 = 300 MB ≈ 9 秒/请求,面板与交易系统同机)。
"""
import json
import time

import pytest

from dashboard import collectors as col
from dashboard import config as appconfig

NOW = 1_800_000_000.0


def _ledger(root, name, records=50):
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(records):
        rows.append(json.dumps({"campaign_id": name, "generation": i + 1, "candidate_sha256": f"{i:064x}",
                                "parent_sha256": None, "run_id": "20261112-210000-000000", "status": "evaluated",
                                "sharpe": 1.0, "total_return": 0.1, "max_drawdown": 0.05, "dsr": 0.5, "n_trials": 2,
                                "fail_reason": None, "reflection": None, "created_at": "2026-11-12T21:00:00",
                                "cell": "c", "diagnosis": None}, ensure_ascii=False))
    (d / f"{name}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _settings(root):
    (root / "config").mkdir(parents=True, exist_ok=True)
    cfg = root / "config" / "status_dashboard.json"
    cfg.write_text(json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    return appconfig.load(path=cfg, root=root)


def test_the_summary_cache_holds_more_than_thirty_two_campaigns(tmp_path):
    """上限必须大于一次统计的战役数(50),否则缓存对大部分部署形同虚设。"""
    root = tmp_path / "repo"
    for i in range(col.MAX_CAMPAIGNS_SUMMARIZED):
        _ledger(root, f"camp{i:03d}")
    settings = _settings(root)
    col.collect_evolution(settings, NOW)                       # 冷:填缓存
    assert len(col._EVOLUTION_TTL_CACHE) >= col.MAX_CAMPAIGNS_SUMMARIZED, \
        f"TTL 缓存只装了 {len(col._EVOLUTION_TTL_CACHE)} 个(需要至少 {col.MAX_CAMPAIGNS_SUMMARIZED})"
    assert len(col._EVOLUTION_SUMMARY_CACHE) >= col.MAX_CAMPAIGNS_SUMMARIZED, \
        f"内容缓存只装了 {len(col._EVOLUTION_SUMMARY_CACHE)} 个(需要至少 {col.MAX_CAMPAIGNS_SUMMARIZED})"


def test_a_second_request_within_the_ttl_does_not_reread_the_ledgers(tmp_path, monkeypatch):
    """TTL 内重复请求不得再读盘——这是这次修复的全部意义。"""
    root = tmp_path / "repo"
    for i in range(col.MAX_CAMPAIGNS_SUMMARIZED):
        _ledger(root, f"camp{i:03d}")
    settings = _settings(root)
    col.collect_evolution(settings, NOW)
    reads = {"n": 0}
    real = col.ev.read_campaign

    def counting(directory, name):
        reads["n"] += 1
        return real(directory, name)

    monkeypatch.setattr(col.ev, "read_campaign", counting)
    col.collect_evolution(settings, NOW)
    assert reads["n"] == 0, f"TTL 内又读了 {reads['n']} 次台账"


def test_a_broken_ledger_is_also_cached(tmp_path, monkeypatch):
    """损坏的台账也要进 TTL 缓存:否则每个请求都重读并重新哈希整个文件(929 MB 的超限台账 ≈ 每请求 1 秒)。"""
    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.jsonl").write_text("{broken\n", encoding="utf-8")
    settings = _settings(root)
    calls = {"n": 0}
    real = col.ev.read_campaign

    def counting(directory, name):
        calls["n"] += 1
        return real(directory, name)

    monkeypatch.setattr(col.ev, "read_campaign", counting)
    for _ in range(3):
        col.collect_evolution(settings, NOW)
    assert calls["n"] == 1, f"损坏的台账被读了 {calls['n']} 次(应只读一次)"


def test_the_startup_warmup_also_warms_the_evolution_summaries(tmp_path, monkeypatch):
    """总览每 8 秒轮询一次;冷启动后第一个请求要 11 秒就足够拖慢页面。"""
    root = tmp_path / "repo"
    _ledger(root, "camp")
    settings = _settings(root)
    calls = {"n": 0}
    real = col.ev.read_campaign

    def counting(directory, name):
        calls["n"] += 1
        return real(directory, name)

    monkeypatch.setattr(col.ev, "read_campaign", counting)
    col.warm_up(settings)
    assert calls["n"] >= 1, "warm_up 没有预热进化摘要"


def test_an_oversized_ledger_is_not_hashed_in_full(tmp_path, monkeypatch):
    """超限的台账不必整份哈希:read_campaign 自己会按体积上限拒掉。"""
    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    (d / "huge.jsonl").write_bytes(b"x" * (col.ev.MAX_LEDGER_BYTES + 1024))
    settings = _settings(root)
    col.collect_evolution(settings, NOW)                        # 冷:坏台账
    digests = {"n": 0}
    real = col._file_digest

    def counting(path):
        digests["n"] += 1
        return real(path)

    monkeypatch.setattr(col, "_file_digest", counting)
    settings.raw["evolution"]["summary_ttl"] = 0.0             # TTL 置 0:让缓存真的过期,
    col.collect_evolution(settings, NOW)                        # 否则第二次命中 TTL,走不到哈希,测试守不住
    assert digests["n"] == 0, f"超限台账被哈希了 {digests['n']} 次"


def test_the_cached_corrupt_message_stays_the_same_across_requests(tmp_path):
    """损坏摘要要连同原始错误消息一起缓存:否则第一次『台账超过大小上限』、第二次变笼统,文案会跳。"""
    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    (d / "huge.jsonl").write_bytes(b"x" * (col.ev.MAX_LEDGER_BYTES + 1024))
    settings = _settings(root)
    first = col.collect_evolution(settings, NOW).section["data"]["campaigns"][0]["message"]
    second = col.collect_evolution(settings, NOW).section["data"]["campaigns"][0]["message"]
    assert first == second, (first, second)
    assert "上限" in first or "损坏" in first


def test_the_seed_files_are_byte_identical_to_what_the_generator_produces():
    """种子是前端真正拿去用的 mock。**用生成器逐字节断言**:改样例忘改种子就会红,
    而且不依赖任何解析方式(格式变了也不怕)。"""
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    web = repo / "docs" / "web"
    spec = importlib.util.spec_from_file_location("gen_lovable_seed", repo / "tools" / "webgen" / "gen_lovable_seed.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for name, content in gen.mock_files().items():
        assert (web / "lovable-seed" / name).read_text(encoding="utf-8") == content, name


def test_the_log_parse_cache_does_not_wipe_itself_when_it_fills_up(tmp_path):
    """日志文件每天多一个。超过上限后如果每次新增都 clear() 全部缓存,
    所有日志会被反复重解析(面板跑几个月就会踩到)。逐出最久没用的那个即可。"""
    from dashboard import collectors as col

    root = tmp_path / "repo"
    d = root / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True)
    for i in range(col.PARSE_CACHE_MAX + 5):
        (d / f"vt_2026010{i % 9 + 1}.log").write_text(
            f"2026-01-0{i % 9 + 1} 09:00:00 | INFO | R | 已启动\n", encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    col._PARSE_CACHE.clear()
    col._PARSE_CACHE_USED.clear()
    col.collect_days(settings, NOW)
    assert len(col._PARSE_CACHE) <= col.PARSE_CACHE_MAX
    # 缓存里应当还留着大部分文件,而不是每次新增都清空
    assert len(col._PARSE_CACHE) > 1, "缓存被整体清空了(每次新增都会重解析所有日志)"


def test_a_deeply_nested_json_line_in_a_ledger_is_reported_not_crashing(tmp_path):
    """深层嵌套的 JSON 会抛 RecursionError(不是 ValueError)——不该漏成未处理异常。"""
    from dashboard import evolution as ev

    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    (d / "camp.jsonl").write_text("[" * 100_000 + "]" * 100_000 + "\n", encoding="utf-8")
    with pytest.raises(ev.EvolutionError):
        ev.read_campaign(d, "camp")
