"""各数据来源的采集器:读文件,判定状态,产出区块(Section)与摘要(级别、标题、原因)。

纯逻辑、只读、不抛异常给调用方(单个来源出错只影响它自己的区块)。时间一律由调用方传入 `now`。
**缺失、过期、损坏永远不是 ok**,总体级别取最差(unknown 比 warn 更糟)。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
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

def _read_heartbeat(path: Path) -> float | None:
    """心跳文件内容是 epoch 秒;内容不可解析时退回文件修改时间;文件不存在返回 None。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return m.mtime(path)
    try:
        value = float(text)
        if math.isfinite(value) and value > 0:
            return value
    except ValueError:
        pass
    return m.mtime(path)


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
    last, peak, start = float(raw["last_equity"]), float(raw["peak_equity"]), float(raw["day_start_equity"])
    return {
        "account_masked": mask_account(raw.get("account_id")),
        "day": raw["day"], "day_start_equity": start, "last_equity": last, "peak_equity": peak,
        "daily_pnl": round(last - start, 2), "drawdown_from_peak": round(max(peak - last, 0.0), 2),
        "daily_locked_day": locked_day or None, "hard_locked": raw["hard_locked"], "hard_reason": reason or None,
        "limits": dict(limits),
    }


def collect_trading(settings: Settings, now: float) -> Result:
    state_dir = settings.paths["state_dir"]
    stale_after = settings.stale_after("trading")
    hb_ts = _read_heartbeat(state_dir / "heartbeat")
    raw, err = m.read_json(state_dir / "account_guard.json")
    guard = _validate_guard(raw, settings.limits) if err is None else None
    if err is None and guard is None:
        err = "corrupt"

    if hb_ts is None:
        heartbeat = {"process": "unknown", "last_beat_at": None, "age_seconds": None}
    else:
        age = round(max(now - hb_ts, 0.0), 1)
        heartbeat = {"process": "running" if age <= stale_after else "stopped", "last_beat_at": m.iso(hb_ts), "age_seconds": age}

    flag = state_dir / PAUSE_FLAG_NAME
    paused_at = m.mtime(flag)
    data = {
        "heartbeat": heartbeat,
        "guard": guard,
        "pause": {"paused": paused_at is not None, "since": None if paused_at is None else m.iso(paused_at)},
        "live_admission": {"approved": (state_dir / "promotion_approval.json").exists()},
    }
    as_of = hb_ts if hb_ts is not None else m.mtime(state_dir / "account_guard.json")

    reasons: list[dict] = []
    if err == "corrupt":
        state, message = "corrupt", "account_guard.json 损坏、被占用或格式不符(已重试一次)"
    elif hb_ts is None and err == "missing":
        state, message = "missing", "未找到心跳和风控状态文件(交易进程未启动,或状态页不在交易机上)"
    elif hb_ts is None:
        state, message = "unknown", "未找到心跳文件:无法确认交易进程是否在运行"
    elif err == "missing":
        state, message = "unknown", "未找到风控状态文件:无法确认风控状态(运行时可能没有配置 state_file)"
    elif heartbeat["process"] == "stopped":
        state, message = "stale", f"心跳已停 {heartbeat['age_seconds'] / 3600:.1f} 小时:交易进程可能已退出"
    else:
        state, message = "ok", None

    if guard and guard["hard_locked"]:
        level, headline = "critical", "账户已硬锁定"
        reasons.append(_reason("trading", "critical", f"账户已硬锁定:{guard['hard_reason'] or '原因未记录'}"))
    elif guard and guard["daily_locked_day"] and guard["daily_locked_day"] == guard["day"]:
        level, headline = "critical", "当日熔断:禁止新开仓"
        reasons.append(_reason("trading", "critical", "当日熔断已触发:今日禁止新开仓"))
    elif state in ("missing", "corrupt", "unknown", "stale"):
        level = "unknown"
        headline = {"missing": "无状态文件", "corrupt": "状态文件损坏", "unknown": "无法确认进程状态"}.get(state)             or f"心跳已停 {heartbeat['age_seconds'] / 3600:.1f} 小时"          # 只有 stale 才一定有 age_seconds
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


def _log_files(settings: Settings) -> list[Path]:
    """有内容的每日日志。**空文件不算**:别的进程新建的 0 字节日志既不是活动证据(曾让交易进程停了几十小时的页面显示『刚更新』),
    也不能代表『这一天无异常』。"""
    files: list[Path] = []
    for d in settings.paths["log_dirs"]:
        if d.is_dir():
            files += [f for f in sorted(d.glob("vt_*.log")) if _nonempty(f)]
    return files


def _nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _days(settings: Settings) -> list[ls.DayStats]:
    """按天汇总,只统计有内容的日志(与 _log_files 同一口径)。"""
    merged: dict[str, ls.DayStats] = {}
    for f in _log_files(settings):
        stats = ls.parse_day_log(f)
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
    ts = _read_heartbeat(settings.paths["state_dir"] / "heartbeat")
    if ts is None:
        return None
    age = max(now - ts, 0.0)
    day = datetime.fromtimestamp(ts, m.BEIJING).strftime("%Y-%m-%d")
    return day, (f"{age:.0f} 秒前" if age <= settings.stale_after("trading") else f"已停 {age / 3600:.1f} 小时")


def collect_days(settings: Settings, now: float, limit: int | None = None) -> Result:
    limit = limit or settings.raw["days_default"]
    stale_after = settings.stale_after("days")
    days = _days(settings)
    if not days:
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
    reasons: list[dict] = []
    if latest.has_severe:
        level, headline = "critical", f"{latest.day} 有严重事件"
        reasons.append(_reason("days", "critical", f"{latest.day} 日志里有严重事件:" + "、".join(n for (lv, n) in latest.events if lv == "严重")))
    elif state == "stale":
        level, headline = "warn", "日志长时间未更新"
        reasons.append(_reason("days", "warn", "每日日志长时间没有更新(交易进程可能已停止)"))
    elif latest.events or missing_days:
        level, headline = "warn", f"连续无异常 {streak}/{REQUIRED_STREAK} 天"
        reasons.append(_reason("days", "warn", "最新一天有警告级事件" if latest.events else f"有 {len(missing_days)} 个工作日没有日志"))
    else:
        level, headline = "ok", f"连续无异常 {streak}/{REQUIRED_STREAK} 天"
    message = "每日日志长时间未更新" if state == "stale" else None
    return Result(m.section(state, data, as_of=newest, now=now, stale_after=stale_after, message=message), level, headline, reasons)


def _outbox(settings: Settings, now: float) -> tuple[int, float | None, str]:
    """(待补发条数, 最老一条的年龄, 状态)。状态:ok / missing。无法解析的非空行也计入条数(宁可多报,不能漏报)。"""
    path = settings.paths["state_dir"] / "webhook_outbox.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0, None, "missing"
    except OSError:
        return 0, None, "missing"
    pending, oldest = 0, None
    for line in lines:
        if not line.strip():
            continue
        pending += 1
        try:
            row = json.loads(line)
        except ValueError:
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
    elif latest and latest.events:
        level, headline = "warn", "近期有警告事件"
        reasons.append(_reason("alerts", "warn", f"{latest.day} 有警告级事件"))
    elif state in ("stale", "unknown"):
        level, headline = "unknown", "数据已过期" if state == "stale" else "无法判断"
        reasons.append(_reason("alerts", "unknown", "告警数据已过期,无法确认当前没有新告警"))
    else:
        level, headline = "ok", "无待补发"
    return Result(m.section(state, data, as_of=newest, now=now, stale_after=stale_after), level, headline, reasons)


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
    if stopped:
        state, message = "ok", None
    elif hb is None:
        state, message = "unknown", "录制状态里没有心跳时间:无法判断录制进程是否存活"
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
MAX_JSON_BYTES = 2_000_000
MAX_SOURCE_BYTES = 1_000_000
SHA_CACHE_MAX = 256
_SHA_CACHE: dict[tuple[str, float, int], str] = {}


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
    try:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
        if key not in _SHA_CACHE:
            if st.st_size > MAX_SOURCE_BYTES:
                return None
            if len(_SHA_CACHE) >= SHA_CACHE_MAX:
                _SHA_CACHE.clear()                       # 简单的有界缓存:满了就整体清空(条目很便宜,命中率不重要)
            _SHA_CACHE[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return _SHA_CACHE[key]
    except OSError:
        return None


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


def _brief_run(run_id: str, result: dict) -> dict:
    kind = result.get("kind") if result.get("kind") in ("backtest", "gate", "evaluate") else "backtest"
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
        lines = ledger_path.read_text(encoding="utf-8").splitlines() if ledger_path.exists() else []
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            at = row["at"] if isinstance(row, dict) else None
            if not _num(at):
                raise ValueError("记录缺少时间戳")
            if now - at < window:
                used += 1
                oldest_in_window = at if oldest_in_window is None else min(oldest_in_window, at)
    except (ValueError, KeyError, OSError):
        sec = m.section("corrupt", None, as_of=m.mtime(ledger_path), now=now, message="提交账本损坏:网关会拒绝所有新提交(fail-closed)")
        return Result(sec, "unknown", "提交账本损坏", [_reason("research", "unknown", "提交账本损坏:研究网关将拒绝新提交,需要人工修复")])

    approvals, _ = m.read_json(approvals_file)
    approved_hashes = set(approvals) if isinstance(approvals, dict) else set()
    pending = []
    for p in pending_files[:50]:
        sha = _sha256(p)
        if sha:
            pending.append({"name": p.name, "sha256": sha, "approved": sha in approved_hashes})
    recent = []
    for p in runs[: settings.raw["recent_runs"]]:
        body, e = m.read_json(p, retries=0)
        if e is None and isinstance(body, dict):
            recent.append(_brief_run(p.stem, body))
    data = {"quota": {"used": used, "limit": limit, "window_hours": int(cfg["iteration"]["window_hours"]),
                      "next_release_in_seconds": None if oldest_in_window is None else int(round(oldest_in_window + window - now))},
            "approvals_count": len(approved_hashes), "pending": pending, "recent_runs": recent}
    if used >= limit:
        level, headline = "warn", f"额度已用尽 {used}/{limit}"
        reasons = [_reason("research", "warn", f"迭代额度已用尽({used}/{limit}),新候选会被网关拒绝")]
    else:
        level, headline, reasons = "ok", f"额度 {used}/{limit}", []
    # 研究数据是每次请求现读的快照,不依赖周期性写入的进程,不判过期;as_of 取最近一次活动
    activity = [t for t in (m.mtime(ledger_path), m.mtime(runs[0]) if runs else None) if t is not None]
    return Result(m.section("ok", data, as_of=max(activity) if activity else None, now=now), level, headline, reasons)


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
    if e or not isinstance(body, dict):
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
