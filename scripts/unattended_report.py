"""无人值守运行的验收证据:每日报告 + webhook 连通性自检。

对应运行手册第 6 节"连续 ≥ 5 个交易日无人值守、告警全部到达手机"。
这两件事都不需要人工盯着,但都需要**可核对的证据**,所以做成脚本:

1. 每日报告(--report):解析 vn.py 的每日日志(`vt_YYYYMMDD.log`)与心跳文件,
   统计当天的启动次数、异常事件、成交笔数、心跳新鲜度,生成一张可核对的表。
   只统计日志里真实出现的内容,不推断"没问题"。
2. webhook 自检(--probe-webhook):
   - 配置了 WEBHOOK_URL → 用真实 WebhookNotifier 发一条测试消息,报告对端是否接受;
   - 没配置 → 起一个本地 HTTP 接收端,验证"发送 → 到达 → 失败可辨识"整条链路,
     并明确指出"手机端尚未验证"。

用法(项目根目录):
    python scripts/unattended_report.py --report
    python scripts/unattended_report.py --probe-webhook
    python scripts/unattended_report.py --report --probe-webhook

注意:本脚本会**真实发送**一条 webhook 消息(配置了地址时)。测试消息标题带
"【连通性自检】"前缀,便于在手机上识别并删除。
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from notify.webhook import WebhookNotifier

BEGIN_MARK = "<!-- BEGIN UNATTENDED -->"
END_MARK = "<!-- END UNATTENDED -->"

# 交易所时区:所有"属于哪一天"的判断都用它,不用本机时区。
# 宿主机(UTC-8)与容器(Asia/Shanghai)差 16 小时,按本机时区算会把日期算错。
EXCHANGE_TZ = ZoneInfo("Asia/Shanghai")

# 日志里表示"异常事件"的措辞 → 报告里的人话。
# 键是日志里出现的片段,值是 (严重级别, 展示名)。
# 注意"紧急平仓完成"是**正常**的收尾,不列入异常;只有"紧急平仓失败"才是异常。
# 一行日志可能同时命中多个标记(措辞有重叠),"异常事件"列的计数因此可能偏大 ——
# 这只影响展示:判断"这一天有没有异常"只看有没有事件,多算几次不改变结论。
EVENT_MARKERS: dict[str, tuple[str, str]] = {
    "对账失败": ("严重", "对账失败"),
    "硬锁定": ("严重", "硬锁定"),
    "退出时仍有持仓": ("严重", "退出时仍有持仓"),
    "持仓漂移": ("严重", "持仓漂移"),
    "策略已停止运行": ("严重", "策略被异常停止"),
    "紧急平仓失败": ("严重", "紧急平仓失败"),
    "启动失败": ("严重", "启动失败"),
    "网关连接失败": ("严重", "网关连接失败"),
    "连接断开": ("警告", "连接断开"),
    "行情停更": ("警告", "行情停更"),
    "网关错误": ("警告", "网关错误"),
    # 风控熔断类:守卫(RiskManager)与运行时(Runtime)的告警都会落到日志文件里。
    # 这些词是**告警标题**的一部分,缺了就会把熔断日误判成"干净的一天"。
    "当日熔断": ("严重", "当日熔断"),
    "累计死线触发": ("严重", "累计死线触发"),
    "当日禁止开仓": ("警告", "当日禁止开仓"),
    "账户变更": ("警告", "账户变更"),
    "委托被拒": ("警告", "委托被拒"),
    "平仓委托频繁被拒": ("严重", "平仓委托频繁被拒"),
    # 人工干预(非 CTA 策略、非紧急平仓发出的委托):验收要求"人工干预次数 = 0",出现即让当天不算干净
    "检测到非策略委托": ("严重", "非策略委托"),
    "风控状态写盘失败": ("严重", "风控状态写盘失败"),
    "平仓缺少行情": ("严重", "平仓缺少行情"),
    "平仓请求被拆分器拒绝": ("严重", "平仓请求被拆分器拒绝"),
    "策略初始化失败": ("严重", "策略初始化失败"),
    # 紧急平仓本身(FATAL)是异常事件;而"紧急平仓完成"是**正常**收尾,不能算异常。
    # 这里用带级别前缀和冒号的写法把两者区分开:只匹配 "[FATAL] 紧急平仓:",
    # 不会误伤 "[INFO] 紧急平仓完成",也不会和 "紧急平仓失败" 重复计数。
    "[FATAL] 紧急平仓:": ("严重", "触发紧急平仓"),
}
# 成交是"业务事件",不是异常,单独计数。
# 运行时写的格式是 "[INFO] 成交: c2701.DCE 多开 1@2400";心跳里的"策略持仓 0"不算成交。
TRADE_MARKER = "] 成交:"


@dataclass
class DayStats:
    """一天的日志统计。"""

    day: str
    started: int = 0
    trades: int = 0
    events: Counter = field(default_factory=Counter)
    first_ts: str = ""
    last_ts: str = ""
    log_path: str = ""

    @property
    def clean(self) -> bool:
        return not self.events

    @property
    def has_severe(self) -> bool:
        """这一天有没有"严重"级别的事件(熔断、紧急平仓、硬锁定、对账失败…)。

        周末的日志里如果出现严重事件,连续段一样要断 —— 夜盘跨零点的事故
        不能因为落在周六就被放过;警告级的空闲期抖动才不影响。
        """
        return any(level == "严重" for level, _ in self.events)

    def as_row(self, heartbeat: str = "") -> str:
        if self.events:
            detail = "、".join(f"{name}×{count}" for (_, name), count in sorted(self.events.items()))
        else:
            detail = "无异常"
        trades = str(self.trades) if self.trades else "-"
        return (f"| {self.day} | {self.started} | {trades} | {detail} | {heartbeat or '-'} |")


def _parse_ts(line: str) -> str:
    """从 vn.py 日志行里取出时间戳。

    真实日志行首形如 `2026-11-10 15:36:11.123 | INFO | ...`:带毫秒,
    直接按 "%Y-%m-%d %H:%M:%S" 解析会失败(小数点后多一段),所以先砍掉小数部分。
    """
    head = line[:19]
    try:
        return datetime.strptime(head.strip(), "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
    except ValueError:
        return ""


def _day_from_filename(path: Path) -> str:
    """`vt_20261110.log` -> `2026-11-10`。文件名不是这个格式就原样返回。"""
    stem = path.stem.removeprefix("vt_")
    if len(stem) == 8 and stem.isdigit():
        return f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
    return stem


def parse_day_log(path: Path) -> DayStats:
    """解析一天的 vn.py 日志,统计启动次数、成交笔数与异常事件。"""
    stats = DayStats(day=_day_from_filename(path), log_path=str(path))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return stats
    started_ok = 0
    start_attempts = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        ts = _parse_ts(line)
        if ts:
            if not stats.first_ts:
                stats.first_ts = ts
            stats.last_ts = ts
        # "已启动"与"启动耗时"每次成功启动各出现一次,取较大者,避免同一次启动被数成两次
        if "已启动" in line:
            started_ok += 1
        elif "启动耗时" in line:
            start_attempts += 1
        if TRADE_MARKER in line:
            stats.trades += 1
        for marker, (level, name) in EVENT_MARKERS.items():
            if marker in line:
                stats.events[(level, name)] += 1
    stats.started = max(started_ok, start_attempts)
    return stats


def collect_days(log_dirs: list[Path], day_filter: str = "") -> list[DayStats]:
    """扫描日志目录,按天汇总(同一天有多个目录时合并计数)。"""
    merged: dict[str, DayStats] = {}
    for directory in log_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("vt_*.log")):
            stats = parse_day_log(path)
            if day_filter and stats.day != day_filter:
                continue
            target = merged.setdefault(stats.day, DayStats(day=stats.day))
            target.started += stats.started
            target.trades += stats.trades
            target.events.update(stats.events)
            target.first_ts = target.first_ts or stats.first_ts
            if stats.last_ts:
                target.last_ts = stats.last_ts
            target.log_path = stats.log_path
    return [merged[d] for d in sorted(merged)]


def heartbeat_state(heartbeat_file: Path, max_age: float = 60.0) -> str:
    """心跳文件的新鲜度:进程每 10 秒刷新一次,超过 max_age 没刷新说明进程已经死了。"""
    try:
        ts = float(heartbeat_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "无心跳"
    age = max(time.time() - ts, 0.0)
    if age <= max_age:
        return f"{age:.0f}s 前"
    return f"⚠ 已停 {age / 3600:.1f} 小时"


def heartbeat_day(heartbeat_file: Path) -> str:
    """心跳属于哪一天:取文件最后修改时间所在的日期。

    心跳是"当前进程"的状态,只能挂在当天那一行;挂到历史日期上会误导人。
    用文件 mtime 而不是本机日期 —— 容器里是 Asia/Shanghai,宿主机是另一个时区。

    **按交易所时区换算**:mtime 是 epoch 秒,`datetime.fromtimestamp` 会用**本机**时区换算。
    宿主机是 UTC-8 时,容器里 Asia/Shanghai 的 08:00 会被算成前一天的 16:00 →
    心跳挂到错误的交易日那一行(或者哪一行都挂不上)。
    交易日的口径本来就必须是交易所时区,所以这里显式指定 Asia/Shanghai。
    """
    try:
        stamp = datetime.fromtimestamp(heartbeat_file.stat().st_mtime, EXCHANGE_TZ)
    except OSError:
        return ""
    return stamp.strftime("%Y-%m-%d")


def render_table(days: list[DayStats], heartbeat: str = "", heartbeat_on: str = "") -> str:
    """生成每日证据表。

    心跳是"当前进程"的状态,只属于**今天**那一行;挂到历史日期上会误导人。
    但心跳"没有落在任何一行"时(文件根本不存在、或最后刷新那天没有日志),
    必须挂在**最后一行**上 —— 否则"进程已经死了"这条最重要的证据会在表里消失,
    而验收项"没有进程静默死亡"恰恰是看这一列的。

    形参叫 `heartbeat_on` 而不是 `heartbeat_day`:后者是本模块里那个算日期的
    函数名,同名形参会把它遮住,读代码时容易以为这里调用了那个函数。
    """
    lines = [
        "| 交易日 | 启动次数 | 成交笔数 | 异常事件 | 心跳 |",
        "|---|---|---|---|---|",
    ]
    show_on = heartbeat_on
    if heartbeat and not show_on and days:
        # 心跳不知道属于哪一天 → 挂到最后一行(最近的一天),总比不挂好
        show_on = days[-1].day
    for day in days:
        show = heartbeat if (heartbeat and day.day == show_on) else ""
        lines.append(day.as_row(show))
    return "\n".join(lines)


def longest_clean_streak(days: list[DayStats]) -> int:
    """最长的一段"连续无异常"**交易日**天数。

    验收要求的是"连续 ≥ 5 个交易日",所以必须算**连续**段,
    不能拿"总共干净了几天"顶替(中间夹一个有异常的日子,连续性就断了)。

    还要看**缺天**:进程静默死亡的那天根本不会有日志文件,只按"日志里有的天"
    遍历会把缺天悄悄跳过,连续段被拉长 —— 验收结论被虚高,恰恰是最要命的。
    所以按日历逐天走:工作日(周一~周五)必须有日志且无异常才算一天;
    缺日志的工作日**打断**连续段。

    周末不是交易日:周末**有日志也不计入**连续段。容器是常驻的,周末照样有日志文件
    (周五夜盘跨过零点还会写进周六的文件),把它们当成交易日,验收标准会凭空多出两天。
    但周末**严重**级别的事件(紧急平仓、熔断、硬锁定、对账失败…)仍然**打断**连续段 ——
    夜盘跨零点的事故不能因为落在周六就被放过;周末的**警告**(空闲期的连接抖动)
    则不打断,否则"连续 5 个交易日"永远达不成。

    节假日无法从本地数据判断 —— 法定假日会被当成"缺天",让连续段偏短。
    这是保守方向(宁可少报不可多报),符合"证据不足就不算数"的原则。
    """
    by_day = {d.day: d for d in days}
    if not by_day:
        return 0
    try:
        start = date.fromisoformat(min(by_day))
        end = date.fromisoformat(max(by_day))
    except ValueError:
        # 日期格式不是 ISO(理论上不会发生)→ 退回"只按日志里的天算"
        best = current = 0
        for day in days:
            current = current + 1 if day.clean else 0
            best = max(best, current)
        return best

    best = current = 0
    cursor = start
    while cursor <= end:
        stats = by_day.get(cursor.isoformat())
        if cursor.weekday() >= 5:
            # 周末不是交易日:既不计入也不打断(除非有严重事件)。
            if stats is not None and stats.has_severe:
                current = 0
        elif stats is not None:
            # 工作日有日志:干净就计入连续段,有异常就断
            current = current + 1 if stats.clean else 0
        else:
            # 工作日却没有任何日志 → 进程静默死亡(那天的日志文件根本没生成)。
            # 只按"日志里有的天"遍历会把这一天悄悄跳过,连续段被拉长,验收结论虚高。
            current = 0
        best = max(best, current)
        cursor += timedelta(days=1)
    return best


def missing_weekdays(days: list[DayStats]) -> list[str]:
    """有日志的首末两天之间,**缺少日志的工作日**(不含首末两天本身)。

    这些天进程要么根本没跑,要么死了没留下日志 —— 静默死亡没有任何告警,
    只能靠"该有却没有"的缺口发现。法定节假日无法从本地数据判断,会被一并算进来,
    所以这份清单是"待人工核对",不是"已确认缺勤"。
    """
    by_day = {d.day: d for d in days}
    if not by_day:
        return []
    try:
        start = date.fromisoformat(min(by_day))
        end = date.fromisoformat(max(by_day))
    except ValueError:
        return []
    missing = []
    cursor = start + timedelta(days=1)
    while cursor < end:
        if cursor.weekday() < 5 and cursor.isoformat() not in by_day:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return missing


def acceptance_checklist(days: list[DayStats]) -> list[str]:
    """按运行手册第 6 节列出验收项与当前状态。"""
    total = len(days)
    streak = longest_clean_streak(days)
    missing = missing_weekdays(days)
    checklist = [
        f"- [ ] 连续 ≥ 5 个交易日无人值守:已记录 {total} 天"
        + (f"(最长连续无异常 {streak} 天)" if streak else ""),
        f"- [ ] 告警全部到达手机:需配合 `--probe-webhook` 的自检结果",
        f"- [ ] 没有重复下单:成交笔数与委托笔数需人工抽查(日志里没有重复下单的直接证据)",
        f"- [ ] 没有止损未触发 / 持仓与策略不一致:见上表异常事件列",
        f"- [ ] 人工干预次数 = 0:见上表异常事件列里的「非策略委托」(有则该天作废、连续天数重新计)",
        f"- [ ] 没有进程静默死亡:见心跳列"
        + (f"(**缺 {len(missing)} 个工作日没有日志,需人工核对是否节假日:{', '.join(missing)}**)"
           if missing else ""),
    ]
    return checklist


def replace_section(text: str, begin: str, end: str, body: str) -> str:
    """替换文档里 begin/end 标记之间的内容;没有标记就原样返回(不覆盖人工维护的文档)。"""
    if begin not in text or end not in text:
        return text
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body}\n{end}{tail}"


# ---------------- webhook 自检 ----------------


class _LocalReceiver(BaseHTTPRequestHandler):
    """本地接收端:记录收到的请求,按预设返回码应答。"""

    received: list[dict] = []
    reply_code: int = 0          # 飞书风格的业务码;0 = 成功
    reply_status: int = 200

    def do_POST(self) -> None:      # noqa: N802  (BaseHTTPRequestHandler 的命名约定)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        type(self).received.append({"path": self.path, "body": body})
        payload = json.dumps({"code": type(self).reply_code, "msg": "test"}).encode("utf-8")
        self.send_response(type(self).reply_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args) -> None:   # 静音默认的 stderr 日志
        pass


def _flush_timeout(timeout: float, retry_delays: tuple[float, ...]) -> float:
    """等待后台线程把消息发完所需的上限。

    与 `WebhookNotifier.flush_timeout` 同一套算法(通知器自己知道自己的重试预算,
    那是权威口径;这里保留一个函数形式方便单测直接验证公式)。
    """
    return timeout * (1 + len(retry_delays)) + sum(retry_delays) + 5.0


def probe_webhook_local(timeout: float = 10.0) -> tuple[bool, str]:
    """离线自检:起一个本地接收端,验证"发出 → 到达 → 业务失败可辨识"整条链路。

    返回 (是否通过, 说明文字)。
    """
    _LocalReceiver.received = []
    _LocalReceiver.reply_code = 0          # 复位:上一次调用可能把它留在失败分支
    _LocalReceiver.reply_status = 200
    server = HTTPServer(("127.0.0.1", 0), _LocalReceiver)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/hook"
        delays = (0.1,)
        notifier = WebhookNotifier(url, kind="feishu", retry_delays=delays, timeout=timeout)
        notifier.send("INFO", "【连通性自检】本地接收端", "这是一条离线自检消息")
        notifier.close(drain_timeout=notifier.drain_timeout)
        ok = notifier.sent == 1 and notifier.failed == 0 and len(_LocalReceiver.received) == 1

        # 再验证"业务失败"能被识别:飞书失败时也返回 HTTP 200,只看状态码会当成成功
        _LocalReceiver.reply_code = 19001
        bad = WebhookNotifier(url, kind="feishu", retry_delays=delays, timeout=timeout)
        bad.send("INFO", "【连通性自检】失败分支", "这条应当被判为失败")
        bad.close(drain_timeout=bad.drain_timeout)
        fail_detected = bad.sent == 0 and bad.failed == 1

        if ok and fail_detected:
            return True, "本地链路通过:消息送达且飞书式业务失败可被识别(未涉及真实 webhook)"
        if not ok:
            return False, f"本地链路失败:sent={notifier.sent} failed={notifier.failed} 收到={len(_LocalReceiver.received)}"
        return False, f"业务失败未被识别:sent={bad.sent} failed={bad.failed}(会把没送达的告警当成成功)"
    finally:
        server.shutdown()
        server.server_close()


def probe_webhook_remote(url: str, kind: str, chat_id: str, secrets: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    """在线自检:用真实地址发一条测试消息,报告对端是否接受。

    返回 (是否通过, 说明文字)。**会真实发送一条消息。**
    """
    delays = (1.0, 3.0)
    notifier = WebhookNotifier(url, kind=kind, chat_id=chat_id, timeout=timeout, retry_delays=delays,
                               secrets=secrets)
    if not notifier.enabled:
        return False, "WEBHOOK_URL 为空,没有可用的地址"
    accepted = notifier.send("INFO", "【连通性自检】invest", "这是一条连通性测试消息,收到即可删除。")
    if not accepted:
        return False, "消息未被接受(可能被去重或队列已满)"
    # 等够重试+退避的最长时间再判断,否则慢端会被误判成"没送达"
    notifier.close(drain_timeout=notifier.drain_timeout)
    if notifier.sent:
        return True, f"对端已接受(类型 {kind})"
    return False, f"对端未接受:最后错误 {notifier.last_error or '(无)'}"


# ---------------- 主流程 ----------------


def _setup_stdout() -> None:
    """Windows 控制台默认编码可能是 GBK/cp1252,打印中文与 ✅ 会抛 UnicodeEncodeError。

    文件已经写好了,但进程会带着异常退出(退出码 1),看起来像"脚本坏了"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load_env() -> dict[str, str]:
    """读取 .env(不在 .env 里就退回进程环境变量)。"""
    env = dict(os.environ)
    env_file = bootstrap.ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def main(argv: list[str] | None = None) -> int:
    # 必须**先**修好 stdout 编码再解析参数:argparse 的 --help / 错误信息里含中文,
    # 在 cp1252 控制台上打印会抛 UnicodeEncodeError,连用法说明都看不到。
    _setup_stdout()
    parser = argparse.ArgumentParser(description="无人值守验收:每日报告 + webhook 自检")
    parser.add_argument("--report", action="store_true", help="生成每日证据表")
    parser.add_argument("--probe-webhook", action="store_true", help="做 webhook 连通性自检")
    parser.add_argument("--out", default=str(bootstrap.ROOT / "docs" / "unattended-log.md"),
                        help="每日报告写入的文件")
    parser.add_argument("--runtime-dir", default=str(bootstrap.ROOT / "runtime"),
                        help="容器运行目录(日志与状态)")
    parser.add_argument("--log-dir", action="append", default=[],
                        help="额外的日志目录,可重复指定;默认只统计容器目录")
    parser.add_argument("--include-host", action="store_true",
                        help="同时统计宿主机 .vntrader/log(注意:本机跑单元测试也会写进那个目录,数字会虚高)")
    parser.add_argument("--day", default="", help="只统计某一天(YYYY-MM-DD)")
    args = parser.parse_args(argv)

    if not args.report and not args.probe_webhook:
        parser.print_help()
        return 2

    runtime_dir = Path(args.runtime_dir)
    # 默认只统计容器目录:宿主机上跑单元测试时,vn.py 会把测试日志写进 .vntrader/log,
    # 混进去会让"启动 771 次"这种荒唐数字出现。宿主机日志要用 --include-host 显式加上。
    log_dirs = [runtime_dir / "vntrader" / "log"]
    log_dirs += [Path(d) for d in args.log_dir]
    if args.include_host:
        log_dirs.append(bootstrap.ROOT / ".vntrader" / "log")
    exit_code = 0

    if args.report:
        days = collect_days(log_dirs, args.day)
        if not days:
            # 一份"空的证据表"不是证据:日志目录不存在或没有 vt_*.log,说明容器没跑起来或路径写错了。
            # 静默生成一张空表并退出 0,会让人误以为"今天没问题"。
            print(f"⚠ 没有找到任何日志文件(查过:{'、'.join(str(d) for d in log_dirs)}),"
                  f"证据表是空的 —— 不能据此认为当天正常。", file=sys.stderr)
            exit_code = exit_code or 1
        hb_file = runtime_dir / "data" / "state" / "heartbeat"
        heartbeat = heartbeat_state(hb_file)
        body = "\n\n".join([
            f"> 生成时间:{datetime.now().isoformat(timespec='seconds')}(机器本地时间)",
            f"> 日志来源:{'、'.join(str(d) for d in log_dirs)}",
            render_table(days, heartbeat, heartbeat_on=heartbeat_day(hb_file)),
            "\n".join(acceptance_checklist(days)),
        ])
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            text = out.read_text(encoding="utf-8")
            new_text = replace_section(text, BEGIN_MARK, END_MARK, body)
            if new_text == text and BEGIN_MARK not in text:
                print(f"报告: {out} 里没有 {BEGIN_MARK} 标记,未写入(避免覆盖人工内容)", file=sys.stderr)
                exit_code = 2
            else:
                out.write_text(new_text, encoding="utf-8")
                print(f"已写入每日报告:{out}")
        else:
            title = "# 无人值守运行记录\n\n> 本文件由 `scripts/unattended_report.py --report` 生成,标记之间的内容会被覆盖。\n\n"
            out.write_text(f"{title}{BEGIN_MARK}\n{body}\n{END_MARK}\n", encoding="utf-8")
            print(f"已创建每日报告:{out}")
        print(body)

    if args.probe_webhook:
        env = load_env()
        url = env.get("WEBHOOK_URL", "")
        if url:
            ok, msg = probe_webhook_remote(
                url,
                env.get("WEBHOOK_KIND", "generic"),
                env.get("WEBHOOK_TELEGRAM_CHAT_ID", ""),
                [env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", "")],
            )
        else:
            ok, msg = probe_webhook_local()
        print(("✅ " if ok else "❌ ") + msg)
        if not ok:
            exit_code = exit_code or 1

    return exit_code


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
