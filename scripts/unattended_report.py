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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from notify.webhook import WebhookNotifier

BEGIN_MARK = "<!-- BEGIN UNATTENDED -->"
END_MARK = "<!-- END UNATTENDED -->"

from evidence.logstats import (  # noqa: E402,F401  纯逻辑已抽到 evidence/,此处重新导出以保持原有接口
    EXCHANGE_TZ,
    EVENT_MARKERS,
    TRADE_MARKER,
    DayStats,
    _parse_ts,
    _day_from_filename,
    parse_day_log,
    collect_days,
    heartbeat_state,
    heartbeat_day,
    render_table,
    longest_clean_streak,
    missing_weekdays,
    acceptance_checklist,
)


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
