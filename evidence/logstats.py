"""无人值守日志统计的纯逻辑:解析 vn.py 每日日志、心跳新鲜度、连续无异常天数、验收清单。

从 `scripts/unattended_report.py` 抽出(2026-11-12),只依赖标准库:状态页(`dashboard/`)与该脚本共用同一份实现,
状态页不能经 `scripts/bootstrap.py` 间接加载 vnpy。`unattended_report.py` 重新导出了这里的全部名字,既有调用与测试不受影响。
"""
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
    # 这份日志是否像一份真实的 vn.py 日志(状态页用它决定能不能把『没有事件』当成『这一天无异常』)。
    # 默认 True:unattended_report 等既有使用者不关心这个,行为完全不变。判据见 DayLogParser.trusted。
    trusted: bool = True
    # 尾部不完整:最后一行没有换行结尾,且它有可能是某个事件关键字的前缀(『硬锁定』被截成『硬』)。
    # 这与『日志损坏』不同:文件可能只是正在写。两者都要保守(不把这一天算作无异常),但提示文案要分开。
    tail_uncertain: bool = False

    @property
    def clean(self) -> bool:
        """这一天能不能算作『无异常』。

        **必须同时可信**:日志内容不可信(损坏、日期对不上、尾部被截断而可能藏着严重事件)时,
        『没看到事件』不等于『没有事件』。无人值守验收表与状态页都靠这个判断。
        """
        return not self.events and self.trusted

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
        elif not self.trusted:
            detail = "无法确认(日志不可信)"        # 不能写『无异常』:没看到事件 ≠ 没有事件
        else:
            detail = "无异常"
        trades = str(self.trades) if self.trades else "-"
        return (f"| {self.day} | {self.started} | {trades} | {detail} | {heartbeat or '-'} |")


# 一条真实的日志记录:`日期 时间[.毫秒] | 级别 | 来源 | 内容`(取自 runtime/vntrader/log 的真实样子)。
# 比 _parse_ts 严格得多:_parse_ts 只看前 19 个字符,『2026-11-12 09:00:00 加任意垃圾』也算有时间戳。
# 状态页用它判断一份日志是否可信;_parse_ts 的行为不变(unattended_report 和它的 46 个测试依赖它)。
RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}(?:\.\d+)? \| (DEBUG|INFO|WARNING|ERROR|CRITICAL|SUCCESS|TRACE) \| [^|]*\| ?")
MIN_RECORD_RATIO = 0.2          # 记录至少占非空行的这个比例;容忍 traceback 续行,挡住『一行合规 + 大量垃圾』


def _message_part(line: str) -> str:
    """取记录的消息部分(『时间 | 级别 | 来源 | 内容』里的最后一段)。

    前缀检测只看消息:时间戳与来源里出现的字不该被当成关键字的前缀。
    """
    parts = line.split("|")
    return parts[-1] if len(parts) >= 4 else line


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


class DayLogParser:
    """可增量喂入的单日日志解析器:feed(新增字节) 可以反复调用,snapshot() 随时取当前结果。

    为什么需要:当天的日志一直在增长,状态页每次轮询都重新解析整份文件,60 MB 要约 12 秒。增量解析只处理新增的部分。
    等价性靠构造保证:只处理到缓冲里最后一个换行符(LF)为止的完整文本,并对这段文本调用 str.splitlines(),
    与一次性全量解析用的是同一个行分割语义(它还把 CR、VT、FF、U+2028 等也当行分隔符),
    所以任意切分方式的结果都与全量解析一致。
    最后一行没写完(没有换行结尾)时留在缓冲里,等下次补全再处理,不会被数两次。
    """

    def __init__(self, day: str) -> None:
        self.day = day
        self.trades = 0
        self.events: Counter = Counter()
        self.first_ts = ""
        self.last_ts = ""
        self._started_ok = 0          # 『已启动』与『启动耗时』各自计数,快照时取较大者——不能只存最终值,否则无法继续累加
        self._start_attempts = 0
        self._pending = b""           # 尚未遇到换行的残余字节(可能切在多字节字符中间,所以按字节缓冲)
        self._lines = 0               # 非空行数
        self._records = 0             # 其中形如『时间 | 级别 | 来源 | 内容』的真实日志记录数
        self._first_record_day = ""   # 第一条记录的日期(应与文件名日期一致)
        self._truncated_tail = False  # 文件末尾停在多字节字符中间(只在 finish() 处理尾行时才可能为真;追加后字符补全则不会再出现)
        self._bad_encoding = False    # 出现过非法 UTF-8:日志不可信(曾用替换字符『修复』,坏字节落在关键字中间时严重事件会被藏起来)
        self._tail_partial = ""       # 没有换行结尾的最后一行(可能只写了一半);用来判断它是否可能是某个关键字的前缀

    def feed(self, data: bytes) -> None:
        buf = self._pending + data
        cut = buf.rfind(b"\n")
        if cut < 0:
            self._pending = buf
            return
        self._pending = buf[cut + 1:]
        self._consume(self._decode(buf[: cut + 1], tail=False))

    def finish(self) -> None:
        """文件到此为止:把没有换行结尾的最后一行也算进去(全量解析就是这个语义)。"""
        if self._pending:
            text, self._pending = self._decode(self._pending, tail=True), b""
            self._tail_partial = text
            self._consume(text)

    def _decode(self, raw: bytes, *, tail: bool) -> str:
        """严格解码。非法字节 → 标记不可信(仍用替换字符继续计数,供展示;可信度由 trusted() 否决)。
        例外:没有换行的**最后一行**以『不完整的多字节字符』结尾——进程崩溃时写到一半,不是编码损坏。"""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if tail and exc.reason == "unexpected end of data" and exc.end == len(raw):
                # 崩溃时写到一半的最后一行。被截掉的字符可能正是『硬锁定』的最后一个字——无法证明它不是关键字的一部分,
                # 所以这一天不能被算作『无异常』:标记不可信(不是永久坏编码,字符补全后重新解析即恢复)。
                self._truncated_tail = True
                return raw[: exc.start].decode("utf-8", errors="replace")
            self._bad_encoding = True
            return raw.decode("utf-8", errors="replace")

    def _consume(self, text: str) -> None:
        for line in text.splitlines():
            if not line.strip():
                continue
            self._lines += 1
            record = RECORD.match(line)
            if record:
                self._records += 1
                self._first_record_day = self._first_record_day or record.group(1)
            ts = _parse_ts(line)
            if ts:
                if not self.first_ts:
                    self.first_ts = ts
                self.last_ts = ts
            # "已启动"与"启动耗时"每次成功启动各出现一次,取较大者,避免同一次启动被数成两次
            if "已启动" in line:
                self._started_ok += 1
            elif "启动耗时" in line:
                self._start_attempts += 1
            if TRADE_MARKER in line:
                self.trades += 1
            for marker, (level, name) in EVENT_MARKERS.items():
                if marker in line:
                    self.events[(level, name)] += 1

    def _clone(self) -> "DayLogParser":
        c = DayLogParser(self.day)
        c.trades, c.events = self.trades, Counter(self.events)
        c.first_ts, c.last_ts = self.first_ts, self.last_ts
        c._started_ok, c._start_attempts, c._pending = self._started_ok, self._start_attempts, self._pending
        c._lines, c._records, c._first_record_day = self._lines, self._records, self._first_record_day
        c._bad_encoding, c._truncated_tail = self._bad_encoding, self._truncated_tail
        c._tail_partial = self._tail_partial
        return c

    def trusted(self) -> bool:
        """这份日志像不像一份真实的 vn.py 日志。三条都要满足:
        1. 至少有一条完整形状的记录(『时间 | 级别 | 来源 | 内容』,不只是以日期开头)。
           (逻辑上被第 2 条隐含——没有记录时首条记录日期为空,必然不等于文件名日期。有意保留,把『必须有记录』这个意图写在代码里;
           变异检查去掉它不会让测试失败,原因就在这里。)
        2. 第一条记录的日期与文件名日期一致(被移植/重命名的日志不能当这一天的证据);
        0. 没有出现过非法 UTF-8(codex 第 3 轮复核:坏字节落在『当日熔断』中间时,替换后关键字不匹配,日志却仍被判为干净)。
        3. 记录占非空行的比例不低于 20%(容忍 traceback 续行,挡住『一行合规 + 大量垃圾』)。
        不可信的日志不能被解读成『这一天无异常』(G8 复核:一行合规加任意垃圾曾被判为干净的一天)。"""
        return (not self._bad_encoding and not self._truncated_tail and not self._tail_may_hide_event()
                and self._records >= 1 and self._first_record_day == self.day
                and self._records >= MIN_RECORD_RATIO * self._lines)

    def _tail_may_hide_event(self) -> bool:
        """没有换行结尾的最后一行,可能只写了一半。

        **截断落在字符边界上**(更常见)不会有任何解码异常信号:『硬锁定』被截成『硬』时解码完全成功,
        但关键字不匹配 → 这一行看起来『没有事件』。只要这行有可能是某个关键字的前缀,这一天就不能算作『无异常』。
        与关键字完全无关的一行(比如正在写的普通 INFO 行)不受影响——否则正在写入的日志会一直显示未知。
        关键字**已经完整出现**的行不算(事件已被识别并计入)。
        """
        if not self._tail_partial:
            return False
        message = _message_part(self._tail_partial)
        for marker in EVENT_MARKERS:
            if marker in message:
                continue                      # 事件已完整出现并被计入,不是『被藏起来』
            for cut in range(1, len(marker)):
                # **只看结尾**:截断只能发生在行尾。整行任意位置搜索会把普通消息
                # (如『当前权益 10000』里的『当』是『当日熔断』的前缀)误判成不可信,
                # 还会让已经完整识别出的严重事件整份日志被排除(严重级别降成未知)。
                if message.endswith(marker[:cut]):
                    return True
        return False

    def snapshot(self, log_path: str = "") -> DayStats:
        """当前已知的全部内容,**包括还没有换行结尾的最后一行**;不改变解析器自己的状态。

        G8 复核 #10:进程在写入严重事件后崩溃、最后一行没有换行,这一行曾一直留在缓冲里,增量路径永远不显示它,
        而全量解析会数它——两者不等价,严重事件被漏报。现在在**副本**上处理尾行:快照展示它,
        解析器自己仍把它留在缓冲里,等真正的换行到来后再正式处理,所以不会数两次。"""
        view = self._clone()
        if view._pending:
            view.finish()
        return DayStats(day=view.day, started=max(view._started_ok, view._start_attempts), trades=view.trades,
                        events=Counter(view.events), first_ts=view.first_ts, last_ts=view.last_ts, log_path=log_path,
                        trusted=view.trusted(), tail_uncertain=view._tail_may_hide_event())


def parse_day_log(path: Path) -> DayStats:
    """解析一天的 vn.py 日志,统计启动次数、成交笔数与异常事件。"""
    parser = DayLogParser(_day_from_filename(path))
    try:
        parser.feed(path.read_bytes())
    except OSError:
        return parser.snapshot(str(path))
    parser.finish()
    return parser.snapshot(str(path))


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
            target.trusted = target.trusted and stats.trusted        # 任一文件不可信 → 那天不可信
            target.tail_uncertain = target.tail_uncertain or stats.tail_uncertain
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
    untrusted = sum(1 for d in days if not d.trusted)
    streak = longest_clean_streak(days)
    missing = missing_weekdays(days)
    checklist = [
        f"- [ ] 连续 ≥ 5 个交易日无人值守:已记录 {total} 天"
        + (f"(最长连续无异常 {streak} 天)" if streak else "")
        + (f"(**其中 {untrusted} 天的日志不可信,不能算作『无异常』,需人工核对**)" if untrusted else ""),
        f"- [ ] 告警全部到达手机:需配合 `--probe-webhook` 的自检结果",
        f"- [ ] 没有重复下单:成交笔数与委托笔数需人工抽查(日志里没有重复下单的直接证据)",
        f"- [ ] 没有止损未触发 / 持仓与策略不一致:见上表异常事件列",
        f"- [ ] 人工干预次数 = 0:见上表异常事件列里的「非策略委托」(有则该天作废、连续天数重新计)",
        f"- [ ] 没有进程静默死亡:见心跳列"
        + (f"(**缺 {len(missing)} 个工作日没有日志,需人工核对是否节假日:{', '.join(missing)}**)"
           if missing else ""),
    ]
    return checklist
