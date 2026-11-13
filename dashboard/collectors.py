"""各数据来源的采集器:读文件,判定状态,产出区块(Section)与摘要(级别、标题、原因)。

纯逻辑、只读、不抛异常给调用方(单个来源出错只影响它自己的区块)。时间一律由调用方传入 `now`。
**缺失、过期、损坏永远不是 ok**,总体级别取最差(unknown 比 warn 更糟)。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dashboard import models as m
from dashboard.config import PAUSE_FLAG_NAME, Settings
from dashboard.safe import mask_account
from evidence import logstats as ls


@dataclass
class Result:
    section: dict
    level: str
    headline: str
    reasons: list[dict] = field(default_factory=list)


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _reason(section: str, level: str, text: str) -> dict:
    return {"section": section, "level": level, "text": text}


# ---------------------------------------------------------------- 交易与风控

HEARTBEAT_MAX_BYTES = 1024           # 心跳文件就是一个数字;更大的一定是异常
CLOCK_SKEW_TOLERANCE = m.CLOCK_SKEW_TOLERANCE   # 定义在 models.py(共用的 freshness() 也要用)
# 风控状态落后多久算陈旧。依据(已核对源码):trading/guard_rule.py::on_timer 每秒调用 guard.update(),
# risk/account_guard.py::update 在『状态变化』或『距上次落盘 >=60 秒』时 _save();留 3 倍余量。
# 前提:账户对象已到位(on_timer 里 `if account is not None`)。账户资金还没回报或断线时文件确实不再更新——
# 此时判 stale 是对的(当前权益确实不是最新的)。改动上述两处的人请同步这个阈值;tests/test_dashboard_config.py 里有断言守着这个耦合。
GUARD_STALE_AFTER = 180.0


def _read_heartbeat(path: Path) -> tuple[float | None, str | None]:
    """心跳文件内容是 epoch 秒。返回 (时间戳, 错误类别),错误类别:None / "missing" / "corrupt"。

    内容不合法就是 corrupt,不退回文件 mtime(G8 第 2 轮审查:曾经退回 mtime,刚写入的垃圾文件会被显示成『运行中』)。
    """
    try:
        text = m.read_text_limited(path, HEARTBEAT_MAX_BYTES).strip()
    except FileNotFoundError:
        return None, "missing"
    except (OSError, m.Untrusted):
        return None, "corrupt"
    try:
        value = float(text)
    except ValueError:
        return None, "corrupt"
    if not math.isfinite(value) or value <= 0:
        return None, "corrupt"
    return value, None


def _has_offset(text: str) -> bool:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _validate_guard(raw: Any, limits: dict) -> dict | None:
    """校验 account_guard.json 的形状;任何不符合返回 None(调用方按 corrupt 处理)。"""
    if not isinstance(raw, dict):
        return None
    if not all(_num(raw.get(k)) for k in ("last_equity", "peak_equity", "day_start_equity")):
        return None
    if not isinstance(raw.get("day"), str) or not isinstance(raw.get("hard_locked"), bool):
        return None
    locked_day = raw.get("daily_locked_day")
    if locked_day is not None and not isinstance(locked_day, str):
        return None
    reason = raw.get("hard_reason")
    if reason is not None and not isinstance(reason, str):
        return None
    sampled = raw.get("sampled_at")
    sampled_ts = None
    if sampled is not None:
        # 带时区的 ISO 字符串。无时区的会被 parse_iso 当成 UTC——风控进程写的是本地时间,会差 8 小时,所以直接拒绝
        if not isinstance(sampled, str) or not _has_offset(sampled):
            return None
        sampled_ts = m.parse_iso(sampled)
        if sampled_ts is None:
            return None
    last, peak, start = float(raw["last_equity"]), float(raw["peak_equity"]), float(raw["day_start_equity"])
    return {
        "_sampled_ts": sampled_ts,
        "account_masked": mask_account(raw.get("account_id")),
        "day": raw["day"], "day_start_equity": start, "last_equity": last, "peak_equity": peak,
        "daily_pnl": round(last - start, 2), "drawdown_from_peak": round(max(peak - last, 0.0), 2),
        "daily_locked_day": locked_day or None, "hard_locked": raw["hard_locked"], "hard_reason": reason or None,
        "limits": dict(limits),
    }


def collect_trading(settings: Settings, now: float) -> Result:
    state_dir = settings.paths["state_dir"]
    stale_after = settings.stale_after("trading")
    hb_ts, hb_err = _read_heartbeat(state_dir / "heartbeat")
    guard_path = state_dir / "account_guard.json"
    raw, g_err = m.read_json(guard_path)
    guard = _validate_guard(raw, settings.limits) if g_err is None else None
    if g_err is None and guard is None:
        g_err = "corrupt"

    # ---- 心跳:时间戳不能是未来(超出容忍的时钟误差)——否则时钟回拨或文件异常会让『0 秒新鲜』长期成立
    heartbeat = {"process": "unknown", "last_beat_at": None, "age_seconds": None}
    hb_future = False
    if hb_ts is not None:
        if hb_ts - now > CLOCK_SKEW_TOLERANCE:
            hb_future, hb_err, hb_ts = True, "corrupt", None
        else:
            age = round(max(now - hb_ts, 0.0), 1)
            heartbeat = {"process": "running" if age <= stale_after else "stopped", "last_beat_at": m.iso(hb_ts), "age_seconds": age}

    # ---- 风控状态:有自己的年龄。心跳新鲜不代表权益/盈亏/锁定状态是当前值
    # 年龄按『权益被采样的时刻』算,而不是文件写入时间:网关断开后 vn.py 仍缓存旧账户对象,风控进程照常每分钟落盘,
    # 文件 mtime 因此一直是新的,权益却已不再刷新(codex 复核 #4)。旧格式文件没有 sampled_at → 无法确认,不显示为新鲜。
    guard_mtime = m.mtime(guard_path)
    sampled_ts = guard.pop("_sampled_ts") if guard is not None else None
    guard_future = (guard_mtime is not None and guard_mtime - now > CLOCK_SKEW_TOLERANCE) or (sampled_ts is not None and sampled_ts - now > CLOCK_SKEW_TOLERANCE)
    guard_unverifiable = guard is not None and sampled_ts is None
    basis = [t for t in (sampled_ts, guard_mtime) if t is not None]
    guard_age = None if guard is None or sampled_ts is None else round(max(now - min(basis), 0.0), 1)
    if guard is not None:
        guard["as_of"] = None if sampled_ts is None else m.iso(sampled_ts)
        guard["age_seconds"] = guard_age
    guard_stale = guard is not None and (guard_future or guard_unverifiable or (guard_age is not None and guard_age > GUARD_STALE_AFTER))

    flag = state_dir / PAUSE_FLAG_NAME
    paused_at = m.mtime(flag)
    data = {
        "heartbeat": heartbeat,
        "guard": guard,
        "pause": {"paused": paused_at is not None, "since": None if paused_at is None else m.iso(paused_at)},
        "live_admission": {"approved": (state_dir / "promotion_approval.json").exists()},
    }
    as_of = hb_ts if hb_ts is not None else (sampled_ts if sampled_ts is not None else guard_mtime)

    # ---- 区块状态:取最差。顺序即优先级
    if hb_err == "corrupt" or g_err == "corrupt":
        what = "心跳文件内容无法解析或时间戳在未来(时钟回拨或文件异常)" if hb_err == "corrupt" else "account_guard.json 损坏、被占用或格式不符(已重试一次)"
        state, message = "corrupt", what
    elif hb_err == "missing" and g_err == "missing":
        state, message = "missing", "未找到心跳和风控状态文件(交易进程未启动,或状态页不在交易机上)"
    elif hb_err == "missing":
        state, message = "unknown", "未找到心跳文件:无法确认交易进程是否在运行"
    elif g_err == "missing":
        state, message = "unknown", "未找到风控状态文件:无法确认风控状态(运行时可能没有配置 state_file)"
    elif heartbeat["process"] == "stopped":
        state, message = "stale", f"心跳已停 {heartbeat['age_seconds'] / 3600:.1f} 小时:交易进程可能已退出"
    elif guard_stale:
        state = "stale"
        message = ("风控状态文件的时间戳在未来(时钟回拨或文件异常)" if guard_future else
                   "风控状态文件没有记录权益采样时间(旧格式,需交易进程重启后才会写入):无法确认当前权益、盈亏是否仍在刷新" if guard_unverifiable else
                   f"权益已 {guard_age / 60:.0f} 分钟没有被刷新(风控进程每分钟落盘,但账户回报停了——网关可能已断开):当前权益、盈亏、锁定状态可能不是最新")
    else:
        state, message = "ok", None

    reasons: list[dict] = []
    if guard and guard["hard_locked"]:
        # 锁定状态不会因为文件旧了就失效:陈旧的『已硬锁定』同样报 critical
        level, headline = "critical", "账户已硬锁定"
        reasons.append(_reason("trading", "critical", f"账户已硬锁定:{guard['hard_reason'] or '原因未记录'}"))
    elif guard and guard["daily_locked_day"] and guard["daily_locked_day"] == guard["day"]:
        level, headline = "critical", "当日熔断:禁止新开仓"
        reasons.append(_reason("trading", "critical", "当日熔断已触发:今日禁止新开仓"))
    elif state != "ok":
        level = "unknown"
        if state == "stale" and heartbeat["process"] == "stopped":
            headline = f"心跳已停 {heartbeat['age_seconds'] / 3600:.1f} 小时"
        elif state == "stale":
            headline = "风控状态已过期"
        else:
            headline = {"missing": "无状态文件", "corrupt": "状态文件损坏"}.get(state, "无法确认进程状态")
        reasons.append(_reason("trading", "unknown", f"无法判断交易状态:{message}"))
    elif data["pause"]["paused"]:
        level, headline = "warn", "已暂停开仓"
        reasons.append(_reason("trading", "warn", "已手动暂停开仓(平仓与持仓管理照常)"))
    else:
        level = "ok"
        headline = f"运行中 · 当日 {guard['daily_pnl']:+.1f} 元" if guard else "运行中"

    section = m.section(state, data, as_of=as_of, now=now, stale_after=stale_after, message=message)
    return Result(section, level, headline, reasons)


# ---------------------------------------------------------------- 无人值守每日表 / 告警


_SEVERITY = {"严重": "severe", "警告": "warning"}
REQUIRED_STREAK = 5
FUTURE_LOG_MESSAGE = "日志文件的修改时间在未来(时钟回拨或文件异常):不能据此判断日志是否在更新"


def _log_files(settings: Settings) -> list[Path]:
    """可信的每日日志:非空,且至少含一行能解析出时间戳的内容。

    **空文件不算**:别的进程新建的 0 字节日志既不是活动证据(曾让交易进程停了几十小时的页面显示『刚更新』),也不能代表『这一天无异常』。
    **垃圾内容也不算**(G8 第 2 轮审查):任何新鲜的非空文件都被解释成『干净的一天』并给 alerts 提供『数据仍新鲜』的证据。
    可信度看内容,新鲜度看文件 mtime(日志时间戳是机器本地时间,容器与宿主时区不同,不能直接当北京时间)。
    解析结果有缓存,这里的判断几乎没有额外开销。
    """
    files: list[Path] = []
    for d in settings.paths["log_dirs"]:
        if d.is_dir():
            files += [f for f in sorted(d.glob("vt_*.log")) if _nonempty(f) and _parse_cached(f).trusted]
    return files


def _unreadable_logs(settings: Settings) -> int:
    """非空、但没有任何可识别时间戳的日志文件数(格式变了或被写坏)。与『一个日志都没找到』是两回事。"""
    n = 0
    for d in settings.paths["log_dirs"]:
        if d.is_dir():
            n += sum(1 for f in d.glob("vt_*.log") if _nonempty(f) and not _parse_cached(f).trusted)
    return n


def _nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


PARSE_CACHE_MAX = 64
_PARSE_LOCK = threading.Lock()


@dataclass
class _ParseState:
    parser: ls.DayLogParser
    offset: int                 # 已喂给解析器的字节数
    hasher: "hashlib._Hash"     # 已读前缀 [0, offset) 的 SHA-256 状态:『文件自上次读取以来只是被追加』的证明;追加时增量 update,不重读
    mtime_ns: int
    size: int


_PARSE_CACHE: dict[str, _ParseState] = {}


CHUNK_BYTES = 1 << 20


def _read_range(path: Path, start: int, end: int | None = None) -> bytes:
    """喂给解析器的数据读取(只用于『新增部分』)。单独成函数,便于测试断言读了多少、从哪里开始读。"""
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read() if end is None else fh.read(max(end - start, 0))


def _prefix_sha(path: Path, length: int) -> str | None:
    """文件前 length 字节的 SHA-256(分块读取,内存恒定)。读到的字节不足 length(文件变短了)返回 None。

    这是『只追加』的**证明**:头尾抽样不是(G8 复核实测:保持开头与末尾字节不变、原地改写中间同长度的内容,
    抽样指纹不变,缓存继续用旧结果,严重事件永久不显示)。代价可以接受——实测 60 MB 约 80 毫秒、5 MB 约 9 毫秒;
    日志没变(mtime 与大小都没变)时完全跳过。"""
    h = hashlib.sha256()
    remaining = length
    with path.open("rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                return None
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _full_parse(path: Path, key: str, st) -> _ParseState:
    """全量解析:分块读取,边读边喂解析器、边更新前缀哈希——解析与哈希来自**同一次读取**。
    以实际读到的字节数为准(文件在读的过程中可能还在增长,多出来的留给下一次增量)。"""
    parser = ls.DayLogParser(ls._day_from_filename(path))
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            parser.feed(chunk)
            h.update(chunk)
            total += len(chunk)
    return _ParseState(parser, total, h, st.st_mtime_ns, st.st_size)


def _parse_cached(path: Path) -> ls.DayStats:
    """增量解析并缓存单个日志文件。

    为什么需要(G8 审查实测):5 MB 的单日日志整份解析要约 0.9 秒,20 MB 约 4 秒,60 MB 约 12 秒;总览里 days 与 alerts
    各解析一遍,前端每 8 秒轮询一次——日志一大请求就会堆积、耗尽线程池,且恰恰发生在日志刷屏、最需要看状态的时候。
    当天的日志一直在增长,所以光按 (mtime, size) 缓存没用;这里记住解析器和已读偏移,只处理新增的字节。

    **何时可以信任缓存**(宁可慢也不能错):
    - (mtime, size) 都没变:直接用,零成本。
    - 否则重新计算已读前缀 [0, offset) 的 SHA-256:与保存的相同 = 前面的内容确实没动,只读新增部分继续喂;
      不同、或文件变短 = 被改写/截断/替换,**全量重解析**。头尾抽样不够(同长度的中间改写能骗过它)。
    返回的是快照副本。整个『校验 + 读取 + 喂入』在锁内完成,避免两个请求同时推进同一个解析器。
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return ls.parse_day_log(path)
    with _PARSE_LOCK:
        state = _PARSE_CACHE.get(key)
        try:
            if state is not None and (st.st_mtime_ns, st.st_size) == (state.mtime_ns, state.size):
                pass                                                          # 没变:直接用
            elif state is not None and st.st_size >= state.offset and _prefix_sha(path, state.offset) == state.hasher.hexdigest():
                if st.st_size > state.offset:                                 # 前缀没动:只读新增部分,哈希对象增量 update(不重读整个前缀)
                    fresh = _read_range(path, state.offset, st.st_size)
                    state.parser.feed(fresh)
                    state.hasher.update(fresh)
                    state.offset += len(fresh)
                state.mtime_ns, state.size = st.st_mtime_ns, st.st_size
            else:                                                             # 首次 / 被截断 / 被改写 / 被替换:全量
                state = _full_parse(path, key, st)
                if len(_PARSE_CACHE) >= PARSE_CACHE_MAX and key not in _PARSE_CACHE:
                    _PARSE_CACHE.clear()
                _PARSE_CACHE[key] = state
        except OSError:
            _PARSE_CACHE.pop(key, None)
            return ls.parse_day_log(path)
        snap = state.parser.snapshot(key)
    return snap


def warm_up(settings: Settings) -> None:
    """启动时预热日志解析缓存,让用户第一次打开页面时已经是热的(60 MB 日志冷解析约 11 秒)。
    预热只是优化:任何异常都吞掉,绝不能阻止服务启动。"""
    try:
        _days(settings)
    except Exception:  # noqa: BLE001
        pass


def _days(settings: Settings) -> list[ls.DayStats]:
    """按天汇总,只统计有内容的日志(与 _log_files 同一口径)。"""
    merged: dict[str, ls.DayStats] = {}
    for f in _log_files(settings):
        stats = _parse_cached(f)
        target = merged.setdefault(stats.day, ls.DayStats(day=stats.day))
        target.started += stats.started
        target.trades += stats.trades
        target.events.update(stats.events)
        target.first_ts = target.first_ts or stats.first_ts
        if stats.last_ts:
            target.last_ts = stats.last_ts
        target.log_path = stats.log_path
    return [merged[d] for d in sorted(merged)]


def _events(stats: ls.DayStats) -> list[dict]:
    rows = [{"level": _SEVERITY.get(level, "warning"), "name": name, "count": count} for (level, name), count in stats.events.items()]
    rows.sort(key=lambda r: (r["level"] != "severe", r["name"]))                     # 严重在前
    return rows


def _checklist_item(text: str) -> str:
    return text.removeprefix("- [ ] ").replace("`", "").replace("**", "").strip()


def _heartbeat_day_label(settings: Settings, now: float) -> tuple[str, str] | None:
    """(心跳所属日, 文字)。心跳只挂在当天那一行;用注入的 now 计算,不读真实时钟。"""
    ts, err = _read_heartbeat(settings.paths["state_dir"] / "heartbeat")
    if err is not None or ts is None or ts - now > CLOCK_SKEW_TOLERANCE:       # 损坏或在未来的心跳不当作证据
        return None
    age = max(now - ts, 0.0)
    day = datetime.fromtimestamp(ts, m.BEIJING).strftime("%Y-%m-%d")
    return day, (f"{age:.0f} 秒前" if age <= settings.stale_after("trading") else f"已停 {age / 3600:.1f} 小时")


def collect_days(settings: Settings, now: float, limit: int | None = None) -> Result:
    limit = limit or settings.raw["days_default"]
    stale_after = settings.stale_after("days")
    days = _days(settings)
    if not days:
        bad = _unreadable_logs(settings)
        if bad:
            msg = f"有 {bad} 个日志文件,但内容无法识别(格式变了或被写坏),不能据此判断无人值守情况"
            return Result(m.section("corrupt", None, now=now, stale_after=stale_after, message=msg), "unknown", "日志无法识别",
                          [_reason("days", "unknown", msg)])
        sec = m.section("missing", None, now=now, stale_after=stale_after, message="未找到每日日志(交易进程未运行过,或日志目录配置不对)")
        return Result(sec, "unknown", "无日志", [_reason("days", "unknown", "未找到每日日志:无法判断无人值守情况")])

    newest = max((t for t in (m.mtime(f) for f in _log_files(settings)) if t is not None), default=None)
    streak = ls.longest_clean_streak(days)
    missing_days = ls.missing_weekdays(days)
    hb = _heartbeat_day_label(settings, now)
    rows = []
    for stats in reversed(days[-limit:]):
        rows.append({
            "day": stats.day, "started": stats.started, "trades": stats.trades, "clean": stats.clean, "has_severe": stats.has_severe,
            "events": _events(stats),
            "first_ts": f"{stats.day} {stats.first_ts}" if stats.first_ts else None,
            "last_ts": f"{stats.day} {stats.last_ts}" if stats.last_ts else None,
            "heartbeat": hb[1] if hb and hb[0] == stats.day else None,
        })
    raw_items = ls.acceptance_checklist(days)
    checklist = [{"text": _checklist_item(t), "checked": (streak >= REQUIRED_STREAK) if i == 0 else None} for i, t in enumerate(raw_items)]
    data = {"days": rows, "checklist": checklist, "longest_clean_streak": streak, "required_streak": REQUIRED_STREAK,
            "missing_weekdays": missing_days}

    state = m.freshness(newest, now, stale_after)
    latest = days[-1]
    bad_logs = _unreadable_logs(settings)
    bad_msg = f"有 {bad_logs} 个日志文件内容无法识别(格式变了、被写坏或日期与文件名不符),这些天不能算作『无异常』"
    reasons: list[dict] = []
    if latest.has_severe:
        level, headline = "critical", f"{latest.day} 有严重事件"
        reasons.append(_reason("days", "critical", f"{latest.day} 日志里有严重事件:" + "、".join(n for (lv, n) in latest.events if lv == "严重")))
    elif bad_logs:
        # 有日志文件但内容无法识别——哪怕另一天是好的。曾经只在『一个好日志都没有』时才报,
        # 于是『昨天干净、今天的日志坏了』显示成一切正常。坏掉的那天是『无法判断』,不是『无异常』。
        level, headline = "unknown", "有日志无法识别"
        reasons.append(_reason("days", "unknown", bad_msg))
    elif state == "corrupt":
        # 日志文件的修改时间在未来(时钟回拨或文件异常):不能据此判断新鲜度。严重事件是日志内容,仍照常报 critical(上一分支)
        level, headline = "unknown", "日志时间异常"
        reasons.append(_reason("days", "unknown", FUTURE_LOG_MESSAGE))
    elif state == "stale":
        level, headline = "warn", "日志长时间未更新"
        reasons.append(_reason("days", "warn", "每日日志长时间没有更新(交易进程可能已停止)"))
    elif latest.events or missing_days:
        level, headline = "warn", f"连续无异常 {streak}/{REQUIRED_STREAK} 天"
        reasons.append(_reason("days", "warn", "最新一天有警告级事件" if latest.events else f"有 {len(missing_days)} 个工作日没有日志"))
    else:
        level, headline = "ok", f"连续无异常 {streak}/{REQUIRED_STREAK} 天"
    notes = [x for x in ({"stale": "每日日志长时间未更新", "corrupt": FUTURE_LOG_MESSAGE}.get(state), bad_msg if bad_logs else None) if x]
    return Result(m.section(state, data, as_of=newest, now=now, stale_after=stale_after, message=";".join(notes) or None), level, headline, reasons)


def _outbox(settings: Settings, now: float) -> tuple[int, float | None, str]:
    """(待补发条数, 最老一条的年龄, 状态)。状态:ok / missing。无法解析的非空行也计入条数(宁可多报,不能漏报)。"""
    path = settings.paths["state_dir"] / "webhook_outbox.jsonl"
    try:
        lines = m.read_text_limited(path, MAX_OUTBOX_BYTES).splitlines()
    except FileNotFoundError:
        return 0, None, "missing"
    except (OSError, m.Untrusted):
        return 0, None, "corrupt"                 # 读不了或异常增长 ≠ 不存在;不能把它说成『没有积压』
    pending, oldest = 0, None
    for line in lines:
        if not line.strip():
            continue
        pending += 1
        try:
            row = m.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and isinstance(row.get("time"), str):
            try:
                ts = datetime.fromisoformat(row["time"]).timestamp()        # 队列里是本机本地时间(无时区),按本机时区解释
            except ValueError:
                continue
            age = max(now - ts, 0.0)
            oldest = age if oldest is None else max(oldest, age)
    return pending, None if oldest is None else round(oldest, 1), "ok"


def collect_alerts(settings: Settings, now: float) -> Result:
    stale_after = settings.stale_after("alerts")
    pending, oldest, outbox_state = _outbox(settings, now)
    days = _days(settings)
    if outbox_state == "corrupt":
        # 待补发队列是『有告警没送到手机』的唯一证据;读不了或异常增长时,不能说没有积压
        msg = "待补发告警队列无法读取或异常增长:无法确认有没有告警尚未送达手机"
        return Result(m.section("corrupt", None, as_of=m.mtime(settings.paths["state_dir"] / "webhook_outbox.jsonl"), now=now,
                                stale_after=stale_after, message=msg), "unknown", "告警队列异常", [_reason("alerts", "unknown", msg)])
    bad_logs = _unreadable_logs(settings)
    if bad_logs and not days:
        msg = f"有 {bad_logs} 个日志文件,但内容无法识别(格式变了或被写坏),不能据此确认没有新告警"
        return Result(m.section("corrupt", None, now=now, stale_after=stale_after, message=msg), "unknown", "日志无法识别",
                      [_reason("alerts", "unknown", msg)])
    bad_msg = f"有 {bad_logs} 个日志文件内容无法识别,不能据此确认这些天没有新告警"
    if not days and outbox_state == "missing":
        sec = m.section("missing", None, now=now, stale_after=stale_after, message="未找到告警队列和每日日志")
        return Result(sec, "unknown", "无数据来源", [_reason("alerts", "unknown", "未找到告警队列和日志:无法判断告警情况")])

    recent: list[dict] = []
    for stats in reversed(days[-3:]):                                        # 最近三天的事件
        for e in _events(stats):
            recent.append({"day": stats.day, **e})
    recent.sort(key=lambda r: (r["day"], r["level"] == "severe"), reverse=True)
    recent.sort(key=lambda r: r["level"] != "severe")                        # 严重在前(稳定排序保留日期倒序)
    data = {"outbox": {"pending": pending, "oldest_age_seconds": oldest}, "recent": recent}

    newest = max((t for t in (m.mtime(f) for f in _log_files(settings)) if t is not None), default=None)
    # 没有任何非空日志 = 没有『进程在运行』的证据;空队列不能被解读成『一切正常』
    state = m.freshness(newest, now, stale_after) if newest is not None else "unknown"
    latest = days[-1] if days else None
    reasons: list[dict] = []
    if latest and latest.has_severe:
        level, headline = "critical", f"{latest.day} 有严重事件"
        reasons.append(_reason("alerts", "critical", f"{latest.day} 有严重事件,请查看告警与事件"))
    elif pending:
        level, headline = "warn", f"待补发 {pending} 条"
        reasons.append(_reason("alerts", "warn", f"有 {pending} 条告警尚未送达手机(断网等原因暂存)"))
    elif bad_logs:
        # 有日志读不懂——哪怕另一天是好的。坏掉的那天不能被当成『没有告警』
        level, headline = "unknown", "有日志无法识别"
        reasons.append(_reason("alerts", "unknown", bad_msg))
    elif latest and latest.events:
        level, headline = "warn", "近期有警告事件"
        reasons.append(_reason("alerts", "warn", f"{latest.day} 有警告级事件"))
    elif state in ("stale", "unknown", "corrupt"):
        level, headline = "unknown", {"stale": "数据已过期", "corrupt": "日志时间异常"}.get(state, "无法判断")
        text = FUTURE_LOG_MESSAGE if state == "corrupt" else "告警数据已过期,无法确认当前没有新告警"
        reasons.append(_reason("alerts", "unknown", text))
    else:
        level, headline = "ok", "无待补发"
    notes = [x for x in (FUTURE_LOG_MESSAGE if state == "corrupt" else None, bad_msg if bad_logs else None) if x]
    message = ";".join(notes) or None
    return Result(m.section(state, data, as_of=newest, now=now, stale_after=stale_after, message=message), level, headline, reasons)


# ---------------------------------------------------------------- 行情录制

_REC_OK = {"starting", "connecting", "connected", "recording", "stopping", "stopped"}
_REC_HEADLINE = {"recording": "录制中", "starting": "启动中", "connecting": "连接中", "connected": "已连接", "stopping": "停止中", "stopped": "已停止"}


def _utc_to_bj(value: Any) -> str | None:
    ts = m.parse_iso(value)
    return None if ts is None else m.iso(ts)


def collect_recording(settings: Settings, now: float) -> Result:
    stale_after = settings.stale_after("recording")
    path = settings.paths["recording_dir"] / "status.json"
    raw, err = m.read_json(path)
    if err == "missing":
        sec = m.section("missing", None, now=now, stale_after=stale_after, message="未找到录制状态文件(录制未启动,或不在本机)")
        return Result(sec, "unknown", "未启动或不在本机", [_reason("recording", "unknown", "未找到录制状态文件")])
    if err or not isinstance(raw, dict):
        sec = m.section("corrupt", None, as_of=m.mtime(path), now=now, stale_after=stale_after, message="录制状态文件损坏、被占用或格式不符(已重试一次)")
        return Result(sec, "unknown", "状态文件损坏", [_reason("recording", "unknown", "录制状态文件损坏,无法判断录制情况")])

    status = raw.get("status") if isinstance(raw.get("status"), str) else "unknown"
    hb = m.parse_iso(raw.get("heartbeat_at"))
    started = m.parse_iso(raw.get("started_at"))
    # 时间关系必须自洽:心跳不能在未来(超出容忍的时钟误差),开始时间不能晚于心跳。矛盾 = 文件不可信,而不是『新鲜』
    if hb is not None and (hb - now > CLOCK_SKEW_TOLERANCE or (started is not None and started - hb > CLOCK_SKEW_TOLERANCE)):
        msg = "录制状态里的时间自相矛盾或在未来(时钟回拨或文件异常),不能据此判断录制情况"
        return Result(m.section("corrupt", None, as_of=m.mtime(path), now=now, stale_after=stale_after, message=msg), "unknown",
                      "状态文件时间异常", [_reason("recording", "unknown", msg)])
    age = None if hb is None else max(now - hb, 0.0)
    fresh = age is not None and age <= stale_after
    stopped = status == "stopped"

    def text(key: str) -> str | None:
        return raw.get(key) if isinstance(raw.get(key), str) else None

    data = {
        "wrapper_running": bool(fresh and not stopped),
        "status": status,
        "profile": text("profile"),
        "symbols": [s for s in raw["symbols"] if isinstance(s, str)] if isinstance(raw.get("symbols"), list) else [],
        "started_at": _utc_to_bj(raw.get("started_at")),
        "heartbeat_at": None if hb is None else m.iso(hb),
        "last_tick": text("last_tick"), "last_bar": text("last_bar"),
        "last_error": text("last_error"), "data_warning": text("data_warning"),
    }
    reasons: list[dict] = []
    if hb is None:
        # 包括 status=stopped:正常停止应当留下终态时间;没有它就无法证明这是一次正常停止(G8 第 2 轮审查:
        # 曾经 {"status": "stopped"} 单独一行就返回 ok、as_of 为空)
        state, message = "unknown", "录制状态里没有心跳时间:无法判断录制进程是否存活或是否正常停止"
    elif stopped:
        state, message = "ok", None
    elif not fresh:
        state, message = "stale", f"录制进程心跳已停 {age / 60:.0f} 分钟:进程可能已退出"
    else:
        state, message = "ok", None

    if state == "stale":
        level, headline = "warn", "录制进程心跳已停"
        reasons.append(_reason("recording", "warn", message))
    elif state == "unknown":
        level, headline = "unknown", "无法判断"
        reasons.append(_reason("recording", "unknown", message))
    elif status == "stalled":
        detail = f"({data['last_error']})" if data["last_error"] else ""
        level, headline = "warn", "录制停滞(已连接却不订阅)"
        reasons.append(_reason("recording", "warn", "录制进程停滞:已连接但没有订阅到任何合约" + detail))
    elif status == "degraded" or data["data_warning"]:
        level, headline = "warn", "录制降级"
        reasons.append(_reason("recording", "warn", data["data_warning"] or "录制处于降级状态"))
    elif status in _REC_OK:
        level, headline = "ok", _REC_HEADLINE.get(status, status)
    else:
        level, headline = "unknown", f"未知状态 {status}"                 # 不认识的取值绝不当作正常
        reasons.append(_reason("recording", "unknown", f"录制状态取值无法识别:{status}"))
    return Result(m.section(state, data, as_of=hb, now=now, stale_after=stale_after, message=message), level, headline, reasons)


# ---------------------------------------------------------------- 研究沙盒

RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{6}$")
MAX_JSON_BYTES = 2_000_000           # 单个运行结果
MAX_SOURCE_BYTES = 256_000           # 候选策略源码(沙盒配置里的提交上限是 64 KB,留余量)
MAX_LEDGER_BYTES = 2_000_000         # 提交账本:每天最多几十条
MAX_OUTBOX_BYTES = 5_000_000         # 待补发队列:最多 200 条,每条几 KB
MAX_PENDING_LISTED = 50              # 待审候选最多列出多少个


def _research_cfg(settings: Settings) -> tuple[dict, str | None]:
    """沙盒配置里的额度与路径。配置文件缺失用默认值(沙盒按默认运行);损坏返回错误。"""
    defaults = {"iteration": {"max_iterations": 5, "window_hours": 24},
                "paths": {"pending_dir": "pending_strategies", "results_dir": "data/research", "approvals_file": "data/research-control/approvals.json"}}
    raw, err = m.read_json(settings.paths["research_config"])
    if err == "missing":
        return defaults, None
    if err or not isinstance(raw, dict):
        return defaults, "corrupt"
    it = raw.get("iteration") if isinstance(raw.get("iteration"), dict) else {}
    pa = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    return {"iteration": {**defaults["iteration"], **it}, "paths": {**defaults["paths"], **pa}}, None


def _inside(root: Path, rel: Any) -> Path | None:
    if not isinstance(rel, str):
        return None
    path = (root / rel).resolve()
    return path if root == path or root in path.parents else None


def _sha256(path: Path) -> str | None:
    """文件内容的 SHA-256;读不了或超过上限返回 None(调用方要把它计为『不可读』,不能静默丢弃)。

    **不缓存**(G8 第 2 轮审查):曾按 (路径, mtime, size) 缓存,同长度改动并恢复 mtime 后,已修改的候选会继续显示『已批准』。
    『已批准』是安全相关的状态,宁可每次请求重新哈希——候选最多 50 个、每个 ≤256 KB,开销可以忽略。
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_SOURCE_BYTES:
        return None
    return hashlib.sha256(raw).hexdigest()


def _headline_of(result: dict) -> str:
    report = result.get("report")
    if isinstance(report, str):
        lines = [l.strip() for l in report.splitlines() if l.strip()]
        for line in lines:
            if line.startswith("结论"):
                return line[:120]
        if lines:
            return lines[0][:120]
    return f"{result.get('kind', '运行')} · {result.get('status', '状态未知')}"


RUN_KINDS = ("backtest", "gate", "evaluate")
MAX_RUNS_SCANNED = 200               # 最多校验多少个运行结果文件(防止结果目录无限增长拖慢请求);超出的在提示里说明


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _valid_run(run_id: str, body: Any) -> bool:
    """运行结果的结构(按 scripts/research_api.py 的真实产出定):不符合就当作无法读取,而不是『尽量显示』。

    G8 第 3 轮 #8:曾经 `{}` 被显示成正常的 backtest;第二次复核时只有 kind/strategy_file/passed 的残缺文件仍能显示成『通过』。
    网关保存结果时一定写入 run_id、strategy_sha256、report、ran_at;策略报错时写 error,否则 gate/evaluate 写 failures、backtest 写 stats。
    """
    if not isinstance(body, dict) or body.get("kind") not in RUN_KINDS:
        return False
    if body.get("run_id") != run_id:                                     # 必须存在且等于文件名(文件被改名、复制或手写都会不符)
        return False
    name = body.get("strategy_file")
    if not isinstance(name, str) or not name.endswith(".py") or "/" in name or "\\" in name or name in (".py", "..py"):
        return False
    sha = body.get("strategy_sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        return False
    if not isinstance(body.get("report"), str) or not body["report"].strip() or m.parse_iso(body.get("ran_at")) is None:
        return False
    status = body.get("status")
    if status not in (None, "strategy_error"):                          # 真实产出里只有这两种;其他取值(含 "ok"/"complete")是手写或损坏的
        return False
    errored = status == "strategy_error"
    if errored:
        err = body.get("error")
        if not (isinstance(err, dict) and isinstance(err.get("type"), str) and isinstance(err.get("message"), str)):
            return False
    if body["kind"] in ("gate", "evaluate"):
        passed, failures = body.get("passed"), body.get("failures")
        if not isinstance(passed, bool):
            return False
        if errored:
            return passed is False                                      # 策略报错不可能同时『通过』
        if not (isinstance(failures, list) and all(isinstance(x, str) for x in failures)):
            return False
        return (not failures) if passed else bool(failures)             # 通过 ⇔ 没有失败项(risk/candidate_eval.py:passed = not failures)
    return errored or (isinstance(body.get("stats"), dict) and bool(body["stats"]))


def _brief_run(run_id: str, result: dict) -> dict:
    kind = result["kind"]                                               # 调用方已用 _valid_run 校验
    passed = result.get("passed")
    return {"run_id": run_id, "kind": kind, "status": result.get("status") if isinstance(result.get("status"), str) else None,
            "passed": passed if isinstance(passed, bool) else None,
            "strategy_file": result.get("strategy_file") if isinstance(result.get("strategy_file"), str) else None,
            "headline": _headline_of(result)}


def _really_inside(path: Path, directory: Path) -> bool:
    """文件解析后的真实位置必须直接位于 directory 内。符号链接/junction 指向目录外的一律不算。"""
    try:
        return path.resolve(strict=True).parent == directory
    except OSError:
        return False


def _run_files(results_dir: Path) -> list[Path]:
    """结果目录里的运行结果文件。**在源头过滤**:后面所有读取都只会拿到真实位于目录内的文件,不依赖每处调用都记得检查。"""
    if not results_dir.is_dir():
        return []
    return sorted((p for p in results_dir.glob("*.json") if RUN_ID.fullmatch(p.stem) and _really_inside(p, results_dir)), reverse=True)


def collect_research(settings: Settings, now: float) -> Result:
    cfg, cfg_err = _research_cfg(settings)
    root = settings.root
    results_dir = _inside(root, cfg["paths"]["results_dir"])
    pending_dir = _inside(root, cfg["paths"]["pending_dir"])
    approvals_file = _inside(root, cfg["paths"]["approvals_file"])
    if cfg_err or None in (results_dir, pending_dir, approvals_file):
        sec = m.section("corrupt", None, now=now, message="研究沙盒配置损坏或路径越界")
        return Result(sec, "unknown", "配置损坏", [_reason("research", "unknown", "研究沙盒配置损坏,无法读取状态")])

    ledger_path = results_dir / "submissions.jsonl"
    runs = _run_files(results_dir)
    pending_files = sorted(f for f in pending_dir.glob("*.py") if _really_inside(f, pending_dir)) if pending_dir.is_dir() else []
    if not (results_dir.is_dir() or pending_dir.is_dir() or ledger_path.exists()):
        sec = m.section("missing", None, now=now, message="未找到研究沙盒目录(沙盒未部署或尚未使用)")
        return Result(sec, "unknown", "沙盒未部署", [_reason("research", "unknown", "未找到研究沙盒目录")])

    try:
        window = float(cfg["iteration"]["window_hours"]) * 3600.0
        limit = int(cfg["iteration"]["max_iterations"])
    except (TypeError, ValueError):
        sec = m.section("corrupt", None, now=now, message="研究沙盒配置里的额度取值非法")
        return Result(sec, "unknown", "配置损坏", [_reason("research", "unknown", "研究沙盒配置里的额度取值非法")])
    used, oldest_in_window = 0, None
    try:
        text = m.read_text_limited(ledger_path, MAX_LEDGER_BYTES) if ledger_path.exists() else ""
        for line in text.splitlines():
            if not line.strip():
                continue
            row = m.loads(line)
            at = row["at"] if isinstance(row, dict) else None
            if not _num(at):
                raise ValueError("记录缺少时间戳")
            if now - at < window:
                used += 1
                oldest_in_window = at if oldest_in_window is None else min(oldest_in_window, at)
    except (ValueError, KeyError, OSError, RecursionError, m.Untrusted):
        sec = m.section("corrupt", None, as_of=m.mtime(ledger_path), now=now, message="提交账本损坏或异常增长:网关会拒绝所有新提交(fail-closed)")
        return Result(sec, "unknown", "提交账本损坏", [_reason("research", "unknown", "提交账本损坏:研究网关将拒绝新提交,需要人工修复")])

    # 批准记录:文件不存在 = 没有任何批准(合法);文件存在却读不了 = 『已批准』标记不可信,整个区块判损坏
    # (G8 第 2 轮审查:曾把损坏当成『0 个批准』,用户无法区分『确实没批准』与『批准文件坏了』)
    approvals, approvals_err = m.read_json(approvals_file)
    if approvals_err == "corrupt" or (approvals_err is None and not isinstance(approvals, dict)):
        sec = m.section("corrupt", None, as_of=m.mtime(approvals_file), now=now, message="批准记录文件损坏或格式不符:无法判断哪些候选已批准")
        return Result(sec, "unknown", "批准记录损坏", [_reason("research", "unknown", "批准记录损坏:无法判断哪些候选已批准,请人工检查")])
    approved_hashes = set(approvals) if isinstance(approvals, dict) else set()

    pending, unreadable_pending = [], 0
    for p in pending_files[:MAX_PENDING_LISTED]:
        sha = _sha256(p)
        if sha is None:
            unreadable_pending += 1                        # 读不了或超过上限:计数并暴露,不静默消失
            continue
        pending.append({"name": p.name, "sha256": sha, "approved": sha in approved_hashes})
    unreadable_pending += max(len(pending_files) - MAX_PENDING_LISTED, 0)

    recent, unreadable_runs = [], 0
    shown = settings.raw["recent_runs"]
    for i, p in enumerate(runs[:MAX_RUNS_SCANNED]):                     # 不只看最近 N 个:更早的损坏文件也要被发现
        body, e = m.read_json(p, retries=0)
        if e is None and _valid_run(p.stem, body):
            if i < shown:
                recent.append(_brief_run(p.stem, body))
        else:
            unreadable_runs += 1
    unscanned = max(len(runs) - MAX_RUNS_SCANNED, 0)
    data = {"quota": {"used": used, "limit": limit, "window_hours": int(cfg["iteration"]["window_hours"]),
                      "next_release_in_seconds": None if oldest_in_window is None else int(round(oldest_in_window + window - now))},
            "approvals_count": len(approved_hashes), "pending": pending, "recent_runs": recent}

    problems, notes = [], []
    if unreadable_runs:
        problems.append(f"{unreadable_runs} 个运行结果文件损坏或无法读取,未显示")
    if unscanned:
        notes.append(f"更早的 {unscanned} 个运行结果未检查")
    if unreadable_pending:
        problems.append(f"{unreadable_pending} 个待审候选无法读取(损坏或超过大小上限),未显示")
    reasons = [_reason("research", "warn", "研究沙盒有数据无法显示:" + ";".join(problems))] if problems else []
    if problems:
        level, headline = "warn", "有数据无法显示"
    elif used >= limit:
        level, headline = "warn", f"额度已用尽 {used}/{limit}"
        reasons = [_reason("research", "warn", f"迭代额度已用尽({used}/{limit}),新候选会被网关拒绝")]
    else:
        level, headline = "ok", f"额度 {used}/{limit}"
    # 研究数据是每次请求现读的快照,不依赖周期性写入的进程,不判过期;as_of 取最近一次活动
    activity = [t for t in (m.mtime(ledger_path), m.mtime(runs[0]) if runs else None) if t is not None]
    return Result(m.section("ok", data, as_of=max(activity) if activity else None, now=now, message=";".join(problems + notes) or None),
                  level, headline, reasons)


def get_run(settings: Settings, run_id: str) -> dict | None:
    """单次运行详情。run_id 必须严格匹配格式才会碰文件系统(路径穿越在这里被挡住)。"""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return None
    cfg, err = _research_cfg(settings)
    results_dir = None if err else _inside(settings.root, cfg["paths"]["results_dir"])
    if results_dir is None:
        return None
    path = results_dir / f"{run_id}.json"
    if not _really_inside(path, results_dir):          # 符号链接/junction 指向结果目录之外:拒绝
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
    except OSError:
        return None
    body, e = m.read_json(path, retries=0)
    if e or not _valid_run(run_id, body):
        return None
    brief = _brief_run(run_id, body)

    def strings(key: str) -> list[str]:
        return [x for x in body[key] if isinstance(x, str)] if isinstance(body.get(key), list) else []

    def obj(key: str) -> dict | None:
        return body.get(key) if isinstance(body.get(key), dict) else None

    return {**{k: brief[k] for k in ("run_id", "kind", "status", "passed", "strategy_file")},
            "strategy_sha256": body.get("strategy_sha256") if isinstance(body.get("strategy_sha256"), str) else None,
            "report": body.get("report") if isinstance(body.get("report"), str) else None,
            "failures": strings("failures"), "params": obj("params"), "metrics": obj("metrics"),
            "diagnosis": obj("diagnosis"), "selection": obj("selection"),
            "windows": body.get("windows") if isinstance(body.get("windows"), list) else None, "error": obj("error")}


# ---------------------------------------------------------------- 证据文档

MAX_DOC_BYTES = 500_000


def _read_limited(path: Path, limit: int) -> str | None:
    try:
        with path.open("rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return None
    return raw[:limit].decode("utf-8", errors="replace")


def get_doc(settings: Settings, key: str) -> dict | None:
    """按键取文档。键必须精确等于白名单里的键,不接受任何路径。"""
    if not isinstance(key, str) or key not in settings.paths["docs"]:
        return None
    text = _read_limited(settings.paths["docs"][key], MAX_DOC_BYTES)
    if text is None:
        return None
    return {"key": key, "title": settings.raw["docs_titles"][key], "markdown": text}


# ---------------------------------------------------------------- 总览

CORE = ("trading", "alerts", "days")
OPTIONAL = ("recording", "research")                 # 没部署时只显示『缺失』,不拖低总体级别
_ORDER = {"critical": 0, "unknown": 1, "warn": 2, "ok": 3}


def _collect_safely(name: str, func, settings: Settings, now: float) -> Result:
    """单个来源出任何异常都只影响它自己的区块,且异常细节不外泄。"""
    try:
        return func(settings, now)
    except Exception:  # noqa: BLE001
        sec = m.section("unknown", None, now=now, message=f"{name} 采集失败(详见服务端日志)")
        return Result(sec, "unknown", "采集失败", [_reason(name, "unknown", f"{name} 采集失败,无法判断")])


def collect_all(settings: Settings, now: float) -> dict[str, Result]:
    # 按名字在模块里现查(而不是在定义处绑定函数对象),这样测试可以替换单个 collector 来验证隔离性
    return {name: _collect_safely(name, globals()[f"collect_{name}"], settings, now)
            for name in ("trading", "alerts", "days", "recording", "research")}


def collect_overview(settings: Settings, now: float) -> dict:
    results = collect_all(settings, now)
    counted = {n: r for n, r in results.items() if n in CORE or r.section["state"] != "missing"}
    reasons = sorted((x for r in counted.values() for x in r.reasons), key=lambda x: _ORDER.get(x["level"], 1))
    return {
        "server_time": m.iso(now),
        "level": m.worst([r.level for r in counted.values()]),
        "reasons": reasons[:10],
        "sections": {n: {"state": r.section["state"], "level": r.level, "headline": r.headline} for n, r in results.items()},
    }
