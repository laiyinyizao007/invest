"""risk/account_guard.py 与 risk/order_policy.py 单元测试(纯 Python)。"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest


from risk.account_guard import AccountGuard, AccountLimits, trading_day  # noqa: E402
from risk.order_policy import (  # noqa: E402
    CTP_ERROR_KIND,
    RejectKind,
    RejectMonitor,
    RetryPolicy,
    classify_reject,
    extract_ctp_error_code,
    find_stale_orders,
)

MON_10AM = datetime(2026, 11, 9, 10, 0)       # 2026-11-09 是周一
TUE_10AM = datetime(2026, 11, 10, 10, 0)


# ---------- 交易日 ----------

@pytest.mark.parametrize(
    "dt, expected",
    [
        (datetime(2026, 11, 9, 9, 30), date(2026, 11, 9)),      # 周一日盘
        (datetime(2026, 11, 9, 14, 59), date(2026, 11, 9)),
        (datetime(2026, 11, 9, 21, 5), date(2026, 11, 10)),     # 周一夜盘 → 周二交易日
        (datetime(2026, 11, 10, 0, 30), date(2026, 11, 10)),    # 周二凌晨(周一夜盘的跨午夜部分)
        (datetime(2026, 11, 13, 21, 5), date(2026, 11, 16)),    # 周五夜盘 → 下周一
        (datetime(2026, 11, 14, 1, 0), date(2026, 11, 16)),     # 周六凌晨(周五夜盘) → 下周一
        (datetime(2026, 11, 15, 12, 0), date(2026, 11, 16)),    # 周日 → 周一
    ],
)
def test_trading_day(dt, expected):
    assert trading_day(dt) == expected


# ---------- 阈值校验 ----------

def test_limits_validation():
    with pytest.raises(ValueError):
        AccountLimits(daily_loss_limit=0)
    with pytest.raises(ValueError):
        AccountLimits(daily_loss_limit=3000, max_drawdown=2000)


# ---------- 当日熔断 ----------

def test_daily_breaker_triggers_once_at_threshold():
    g = AccountGuard(AccountLimits(600, 2000))
    assert g.update(10_000, MON_10AM) == []                       # 当日开盘权益 10000
    assert g.update(9_500, MON_10AM) == []                        # 亏 500,未到 600
    events = g.update(9_400, MON_10AM)                            # 亏 600,恰好触发
    assert [e.level for e in events] == ["DAILY"]
    assert not g.can_open(date(2026, 11, 9))
    assert g.update(9_300, MON_10AM) == []                        # 继续亏不重复报警(边沿触发)


def test_daily_lock_expires_next_trading_day():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    g.update(9_400, MON_10AM)
    assert not g.can_open(date(2026, 11, 9))
    g.update(9_400, TUE_10AM)                                     # 次日第一次采样作为新的开盘权益
    assert g.can_open(date(2026, 11, 10))


def test_night_session_belongs_to_next_trading_day():
    """周一夜盘属于周二交易日:不应沿用周一的当日熔断。"""
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    g.update(9_400, MON_10AM)                                     # 周一熔断
    g.update(9_400, datetime(2026, 11, 9, 21, 5))                 # 周一夜盘 = 周二交易日,新的开盘权益 9400
    assert g.can_open(date(2026, 11, 10))


def test_floating_loss_counts_toward_daily_breaker():
    """口径用权益,含浮动盈亏:持仓浮亏同样触发,不必等平仓。"""
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    assert g.update(9_390, MON_10AM)[0].level == "DAILY"


# ---------- 累计死线 ----------

def test_hard_lock_from_peak_not_from_initial():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    g.update(11_000, MON_10AM)                                    # 创新高 → 底线抬到 9000
    events = g.update(9_100, MON_10AM)                            # 距最高点回撤 1900 < 2000:只触发当日熔断(较开盘 -900)
    assert [e.level for e in events] == ["DAILY"] and not g.hard_locked
    events = g.update(9_000, MON_10AM)                            # 距最高点回撤 2000:触发硬锁定(而不是跌破 8000)
    assert [e.level for e in events] == ["HARD"]
    assert g.hard_locked and not g.can_open(date(2026, 11, 9))


def test_hard_lock_is_permanent_across_days():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    g.update(7_900, MON_10AM)
    assert g.hard_locked
    assert not g.can_open(date(2026, 11, 20))                     # 过了很多天仍然锁定


def test_both_levels_can_fire_on_same_update():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    events = g.update(7_000, MON_10AM)
    assert {e.level for e in events} == {"HARD", "DAILY"}


def test_manual_unlock_resets_peak_to_avoid_instant_relock():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    g.update(7_900, MON_10AM)
    g.unlock_hard()
    assert not g.hard_locked
    assert g.update(7_900, TUE_10AM) == []                        # 最高点已重置,不会马上再次触发
    assert g.peak_equity == 7_900


# ---------- 无效采样 ----------

@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), None])
def test_invalid_equity_samples_are_ignored(bad):
    """CTP 启动/查询瞬间常报 0 权益:不能因此误熔断,也不能污染最高点。"""
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    assert g.update(bad, MON_10AM) == []
    assert g.can_open(date(2026, 11, 9)) and g.peak_equity == 10_000


# ---------- 外部触发的当日锁定 ----------

def test_external_daily_lock_and_idempotence():
    g = AccountGuard()
    assert g.lock_daily(MON_10AM, "废单风暴").level == "DAILY"
    assert g.lock_daily(MON_10AM, "再次") is None
    assert not g.can_open(date(2026, 11, 9)) and g.can_open(date(2026, 11, 10))


def test_external_daily_lock_ignored_when_hard_locked():
    g = AccountGuard()
    g.update(10_000, MON_10AM)
    g.update(7_000, MON_10AM)
    assert g.lock_daily(MON_10AM, "x") is None


# ---------- 持久化 ----------

def test_state_survives_restart(tmp_path):
    path = tmp_path / "state" / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    g.update(9_400, MON_10AM)
    g2 = AccountGuard(AccountLimits(600, 2000), path)             # 模拟进程重启
    assert not g2.can_open(date(2026, 11, 9))
    assert g2.peak_equity == 10_000 and g2.day_start_equity == 10_000


def test_hard_lock_survives_restart_and_requires_manual_unlock(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    g.update(7_900, MON_10AM)
    g2 = AccountGuard(AccountLimits(600, 2000), path)
    assert g2.hard_locked and "累计死线" in g2.hard_reason
    g2.unlock_hard()
    assert not AccountGuard(AccountLimits(600, 2000), path).hard_locked


def test_corrupted_state_file_fails_safe(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text("{ not json", encoding="utf-8")
    g = AccountGuard(AccountLimits(), path)
    assert g.hard_locked and "损坏" in g.hard_reason          # 宁可停手,不当作"没有锁"


def test_missing_keys_in_state_file_fails_safe(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({"peak_equity": 1}), encoding="utf-8")
    assert AccountGuard(AccountLimits(), path).hard_locked


def test_state_write_is_atomic(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(), path)
    g.update(10_000, MON_10AM)
    assert [p.name for p in tmp_path.iterdir()] == ["guard.json"]


def test_status_strings():
    g = AccountGuard(AccountLimits(600, 2000))
    d = date(2026, 11, 9)
    g.update(10_000, MON_10AM)
    assert g.status(d) == "OK"
    g.update(9_400, MON_10AM)
    assert g.status(d) == "DAILY_LOCKED"
    g.update(7_900, MON_10AM)
    assert g.status(d) == "HARD_LOCKED"


# ---------- 盘中重启:当日开盘基准 ----------

def test_new_day_baseline_is_previous_equity_not_first_sample_of_the_day(tmp_path):
    """进程在已经亏损后的盘中重启:开盘基准取上次采样(前一日收盘附近),而不是新交易日第一次采样。"""
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)                                    # 周一一直运行到收盘,最后权益 10000
    restarted = AccountGuard(AccountLimits(600, 2000), path)      # 周二盘中才重启,此时权益已亏到 9300
    events = restarted.update(9_300, TUE_10AM)
    assert restarted.day_start_equity == 10_000
    assert [e.level for e in events] == ["DAILY"]                 # 当日亏损 700 ≥ 600,立刻触发而不是被抬高基准而漏掉


def test_first_ever_sample_is_used_when_no_history():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    assert g.day_start_equity == 10_000 and g.last_equity == 10_000


def test_last_equity_is_persisted_at_least_every_minute(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    g.update(9_990, datetime(2026, 11, 9, 10, 0, 30))             # 30 秒内、没有任何状态变化(未创新高):不落盘
    assert json.loads(path.read_text(encoding="utf-8"))["last_equity"] == 10_000
    g.update(9_980, datetime(2026, 11, 9, 10, 1, 1))              # 超过 60 秒:落盘最新权益
    assert json.loads(path.read_text(encoding="utf-8"))["last_equity"] == 9_980


def test_old_state_file_without_last_equity_still_loads(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({
        "peak_equity": 10_000, "day": "2026-11-09", "day_start_equity": 10_000,
        "daily_locked_day": None, "hard_locked": False, "hard_reason": "",
    }), encoding="utf-8")
    g = AccountGuard(AccountLimits(), path)
    assert not g.hard_locked and g.last_equity == 0.0


# ---------- 写盘失败不抛异常 ----------

def test_save_failure_is_recorded_not_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                                       # 目录位置被文件占用,mkdir 必然失败
    g = AccountGuard(AccountLimits(600, 2000), blocker / "guard.json")
    events = g.update(10_000, MON_10AM)                           # 不抛异常
    assert events == [] and g.save_error
    assert g.update(9_400, MON_10AM)[0].level == "DAILY"          # 内存中的熔断照常工作


def test_save_error_clears_after_a_successful_write(tmp_path):
    g = AccountGuard(AccountLimits(600, 2000), tmp_path / "guard.json")
    g.save_error = "old"
    g.update(10_000, MON_10AM)
    assert g.save_error == ""


# ---------- 账户绑定 ----------

def test_first_binding_keeps_existing_baseline_and_records_account():
    g = AccountGuard(AccountLimits(600, 2000))
    g.update(10_000, MON_10AM)
    assert g.bind_account("A") is None and g.account_id == "A" and g.peak_equity == 10_000


def test_same_account_does_nothing():
    g = AccountGuard(AccountLimits(600, 2000))
    g.bind_account("A")
    g.update(10_000, MON_10AM)
    assert g.bind_account("A") is None and g.peak_equity == 10_000


def test_switching_account_resets_baselines_so_a_big_sim_balance_cannot_hard_lock_a_small_real_account():
    """SimNow 的 2000 万权益被记成最高点后换成 1 万元账户:不重置会立刻被判回撤 1999 万而硬锁定。"""
    g = AccountGuard(AccountLimits(600, 2000))
    g.bind_account("SIM")
    g.update(20_000_000, MON_10AM)
    note = g.bind_account("REAL")
    assert note and "SIM" in note and "REAL" in note
    assert g.update(10_000, MON_10AM) == [] and not g.hard_locked
    assert g.peak_equity == 10_000 and g.day_start_equity == 10_000


def test_switching_account_clears_the_daily_lock_but_never_the_hard_lock():
    g = AccountGuard(AccountLimits(600, 2000))
    g.bind_account("A")
    g.update(10_000, MON_10AM)
    g.update(9_300, MON_10AM)
    assert not g.can_open(date(2026, 11, 9))
    g.bind_account("B")
    assert g.can_open(date(2026, 11, 9))                                      # 当日熔断属于旧账户
    g.update(10_000, MON_10AM)
    g.update(7_000, MON_10AM)
    assert g.hard_locked
    note = g.bind_account("C")
    assert g.hard_locked and "人工解锁" in note                                # 硬锁定只能人工解除


def test_account_binding_survives_restart(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.bind_account("SIM")
    g.update(20_000_000, MON_10AM)
    g2 = AccountGuard(AccountLimits(600, 2000), path)
    assert g2.account_id == "SIM" and g2.peak_equity == 20_000_000
    assert g2.bind_account("REAL") is not None and g2.peak_equity == 0.0


def test_empty_account_id_is_ignored():
    g = AccountGuard(AccountLimits(600, 2000))
    g.bind_account("A")
    assert g.bind_account("") is None and g.account_id == "A"


# ---------- 废单分类 ----------

@pytest.mark.parametrize("text", ["CTP:资金不足", "合约不存在", "平仓量超过持仓量", "不在交易时段", "报单价格不在涨跌停板内"])
def test_known_hard_rejects(text):
    assert classify_reject(text) is RejectKind.HARD


def test_real_simnow_non_trading_hour_reject_is_hard():
    """2026-11-10 SimNow 实测文本:非交易时段下发的委托被拒。

    这句里含"连接",会被 SOFT_KEYWORDS 命中;但它是硬拒绝(时段问题,重试没用),
    必须按硬拒绝处理,否则会无谓地重试平仓单。
    """
    text = "交易委托失败，代码：21，信息：CTP:该交易席位未连接到交易所"
    assert classify_reject(text) is RejectKind.HARD


@pytest.mark.parametrize("text", ["交易服务器连接断开", "请求超时", "网络异常,正在重连"])
def test_known_soft_rejects(text):
    assert classify_reject(text) is RejectKind.SOFT


@pytest.mark.parametrize("text", [None, "", "某个从未见过的错误", "连接断开且资金不足"])
def test_unknown_or_mixed_rejects_are_hard(text):
    """未知文本、同时含软硬关键词:一律按硬拒绝(宁可停手也不重试)。"""
    assert classify_reject(text) is RejectKind.HARD


# ---------- CTP 数字错误码(三级分类:码表 → 关键词 → fail-closed) ----------

@pytest.mark.parametrize("text, expected", [
    ("交易委托失败，代码：21，信息：CTP:资金不足", 21),       # 全角冒号
    ("交易委托失败,代码:81,信息:CTP:报单价格超出涨跌停板", 81),  # 半角冒号
    ("委托 1 状态更新，代码：33，信息：CTP:合约交易状态不允许", 33),
    ("没有错误码的纯文本", None),
    (None, None),
    ("", None),
])
def test_extract_ctp_error_code(text, expected):
    assert extract_ctp_error_code(text) == expected


def test_extract_code_anchors_on_keyword_not_any_number():
    """断线原因里的数字不能被当成错误码:必须锚定「代码」前缀。"""
    assert extract_ctp_error_code("交易服务器连接断开，原因0x1001") is None
    # 「代码」后第一个数字才是错误码,同一行后面的其他数字不影响
    assert extract_ctp_error_code("交易委托失败，代码：21，信息：错误 4097") == 21


@pytest.mark.parametrize("code", sorted(CTP_ERROR_KIND))
def test_every_listed_ctp_error_code_is_classified_by_table(code):
    text = f"交易委托失败，代码：{code}，信息：CTP:某原因"
    assert classify_reject(text) is CTP_ERROR_KIND[code]


def test_error_code_overrides_soft_keyword():
    """实测文本:代码 21 的句子里含软关键词「连接」,码表优先,必须判硬拒绝。"""
    text = "交易委托失败，代码：21，信息：CTP:该交易席位未连接到交易所"
    assert extract_ctp_error_code(text) == 21
    assert classify_reject(text) is RejectKind.HARD


def test_unknown_code_falls_back_to_keywords():
    """码未收录时回退关键词:软关键词文本判软拒绝,给 SimNow 实测补表留出路。"""
    assert classify_reject("交易委托失败，代码：9999，信息：网络连接超时,正在重连") is RejectKind.SOFT
    assert classify_reject("交易委托失败，代码：9999，信息：CTP:资金不足") is RejectKind.HARD


def test_unknown_code_with_unknown_text_is_hard():
    """码未收录且关键词也认不出:fail-closed 硬拒绝。"""
    assert classify_reject("交易委托失败，代码：9999，信息：CTP:某种没见过的错误") is RejectKind.HARD


# ---------- 重试节奏 ----------

def test_retry_policy_delays_and_escalation():
    p = RetryPolicy()
    assert [p.delay_for(i) for i in (1, 2, 3)] == [1.0, 3.0, 5.0]
    assert p.delay_for(4) is None and p.delay_for(0) is None      # 超过 3 次升级为硬故障
    assert p.max_attempts == 3


# ---------- 废单风暴 ----------

def test_reject_storm_within_window():
    m = RejectMonitor(limit=3, window_seconds=600)
    assert [m.add(t) for t in (0, 100, 200)] == [False, False, True]


def test_old_rejects_fall_out_of_window():
    m = RejectMonitor(limit=3, window_seconds=600)
    m.add(0), m.add(100)
    assert m.add(1000) is False and m.count == 1


def test_reject_monitor_validation():
    with pytest.raises(ValueError):
        RejectMonitor(limit=0)


# ---------- 超时委托 ----------

def test_only_stale_open_orders_are_returned():
    orders = [("a", 0.0, True), ("b", 100.0, True), ("c", 0.0, False)]
    assert find_stale_orders(orders, now_ts=130.0, timeout=120.0) == ["a"]     # 平仓单 c 永不因超时被撤
    assert find_stale_orders(orders, now_ts=130.0, timeout=10.0) == ["a", "b"]


# ---- G8 第 3 轮 #4:权益『采样时间』与文件写入时间分开 ----
def test_state_file_records_when_the_equity_was_sampled_with_a_timezone(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    sampled = datetime(2026, 11, 9, 9, 59, 12)
    g.update(10_000, MON_10AM, sampled_at=sampled)
    saved = json.loads(path.read_text(encoding="utf-8"))["sampled_at"]
    parsed = datetime.fromisoformat(saved)
    assert parsed.tzinfo is not None                                     # 带时区:面板不会把本地时间当 UTC 解析
    assert parsed.replace(tzinfo=None) == sampled.astimezone().replace(tzinfo=None)


def test_sampled_at_is_none_when_the_caller_never_supplies_one(tmp_path):
    """调用方没有采样时间就不记录——绝不用 now 顶替(见重启场景的测试)。需要『现在』的调用方自己传 now。"""
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    assert json.loads(path.read_text(encoding="utf-8"))["sampled_at"] is None


def test_an_invalid_sample_does_not_advance_sampled_at(tmp_path):
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    g.update(float("nan"), datetime(2026, 11, 9, 11, 0))
    g.update(10_100, datetime(2026, 11, 9, 11, 1), sampled_at=MON_10AM)       # 保存会发生(创新高),采样时间仍是旧的
    assert datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["sampled_at"]).replace(tzinfo=None) == MON_10AM


def test_old_state_file_without_sampled_at_still_loads_and_saves_without_inventing_one(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({
        "peak_equity": 10_000, "day": "2026-11-09", "day_start_equity": 10_000,
        "daily_locked_day": None, "hard_locked": False, "hard_reason": "",
    }), encoding="utf-8")
    g = AccountGuard(AccountLimits(), path)
    g._save()
    assert json.loads(path.read_text(encoding="utf-8"))["sampled_at"] is None       # 没有采样就不编造


# ---- codex 第 3 轮复核 #4:重启与缓存账户不能被写成『刚采样』 ----
def test_sampled_at_is_restored_on_load_and_not_replaced_by_now_when_the_caller_has_no_sample_time(tmp_path):
    """重启后 vn.py 若仍能给出缓存的旧账户对象,定时器会调用 update(..., sampled_at=None)。
    此时绝不能把 now 当成采样时间——那会把旧权益写成刚采样。"""
    path = tmp_path / "guard.json"
    old = datetime(2026, 11, 8, 15, 0)
    first = AccountGuard(AccountLimits(600, 2000), path)
    first.update(10_000, old, sampled_at=old)
    restarted = AccountGuard(AccountLimits(600, 2000), path)
    assert restarted.last_sampled_at is not None and restarted.last_sampled_at.replace(tzinfo=None) == old
    restarted.update(10_000, MON_10AM, sampled_at=None)
    restarted._save()
    saved = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["sampled_at"])
    assert saved.replace(tzinfo=None) == old


def test_a_corrupt_sampled_at_in_the_state_file_is_dropped_not_invented(tmp_path):
    path = tmp_path / "guard.json"
    AccountGuard(AccountLimits(600, 2000), path).update(10_000, MON_10AM, sampled_at=MON_10AM)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["sampled_at"] = "not-a-time"
    path.write_text(json.dumps(state), encoding="utf-8")
    g = AccountGuard(AccountLimits(600, 2000), path)
    assert g.last_sampled_at is None and g.peak_equity == 10_000          # 其余状态照常加载
