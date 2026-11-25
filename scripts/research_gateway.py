"""研究沙盒 HTTP 网关(阶段四 4b):毕昇与确定性内核之间唯一的网络通道。

只做适配,不写任何研究逻辑——全部转调 research_api 的作业函数。用标准库实现(无新增依赖),
因为这是仅本机可达、低并发的薄网关;依赖越少,供应链与攻击面越小。

端点即白名单(ROUTES);其余一律 404/405。**不存在**:下单、查账户、改风控、连 CTP、访问测试段、
批准(approve)、写 approved/。批准只能由本机人工触发——带外批准页(`scripts/research_approval_server.py`)
或本机 CLI(`research_api.py approve`);请求体里的 confirm 字段会被忽略。
启动前自检:进程环境里不得有任何 CTP_* 变量(部署层 fail-closed,见 ADR bisheng-ai-researcher 安全约束 5)。
令牌:独立文件(路径来自配置,默认 data/research/gateway.token),`init-token` 生成,永不覆盖。
决议见 docs/adr/20261112-research-sandbox-config-and-approval.md 与 docs/plans/20261112-bisheng-integration-spec.md。
"""
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import research_config as rc
import research_console as rconsole
import research_sandbox as rs
from risk.trial_ledger import TrialLedgerError, record_trials

ROUTES = {
    ("GET", "/research/data"),
    ("POST", "/research/candidates"),
    ("POST", "/research/backtest"),
    ("POST", "/research/gate"),
    ("POST", "/research/evaluate"),
    ("GET", "/research/feedback"),
    ("GET", "/research/results/{run_id}"),
}
CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")
MIN_TOKEN_LENGTH = 32
MAX_TRACE_FAILURES = 8
MAX_TRACE_TEXT = 300
MAX_PENDING_NAMES = 50


class GatewayError(RuntimeError):
    pass


class LedgerError(RuntimeError):
    """提交账本无法读取:fail-closed,拒绝提交而不是悄悄放行。"""


def assert_clean_environment(env) -> None:
    leaked = sorted(k for k in env if k.upper().startswith("CTP_"))
    if leaked:
        raise GatewayError("网关进程环境里不得有 CTP 段变量(部署层隔离),发现: " + ", ".join(leaked))


def init_token(path: Path) -> str:
    path = Path(path)
    if path.exists():
        raise GatewayError(f"令牌文件已存在,拒绝覆盖: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + chr(10), encoding="utf-8")
    return token


def load_token(path: Path) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GatewayError(f"令牌文件无法读取: {exc}") from exc
    if len(token) < MIN_TOKEN_LENGTH:
        raise GatewayError(f"令牌过短(<{MIN_TOKEN_LENGTH}),请用 init-token 重新生成")
    return token


def strip_code_fence(source: str) -> str:
    """LLM 常把代码包在 ```python ... ``` 里:只剥最外层围栏,内容原样保留;没有围栏则不改动。"""
    text = source.strip()
    if text.startswith("```") and text.endswith("```") and text.count("```") == 2:
        first_newline = text.find(chr(10))
        if first_newline != -1:
            return text[first_newline + 1:-3].strip(chr(10)) + chr(10)
    return source


def _lenient_object(value, name: str) -> dict | None:
    """毕昇工具节点把所有参数都当字符串传:接受「JSON 字符串形式的对象」;空串视为未提供;其余非对象一律拒绝。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise rs.SandboxError(f"{name} 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise rs.SandboxError(f"{name} 必须是 JSON 对象")
    return value


def _lenient_int(value, name: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise rs.SandboxError(f"{name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise rs.SandboxError(f"{name} 必须是整数") from exc


class PlainText(str):
    """标记「按 text/plain 原样输出」的响应体:毕昇工具节点的输出是整段响应文本,JSON 里的换行会显示成 \\n,人没法读。"""


def _view(data: dict) -> str | None:
    """可选的 view 字段:缺省/空串 = 完整结果;"report" = 只回精炼报告(JSON);"text" = 纯文本报告;其它一律拒绝(在运行任何作业之前)。"""
    value = data.get("view")
    if value is None or value == "":
        return None
    if value not in ("report", "text"):
        raise rs.SandboxError('view 只能是 "report" 或 "text"(或不填表示完整结果)')
    return value


def _compact(result: dict) -> dict:
    return {k: result[k] for k in ("run_id", "passed", "status", "report") if k in result}


def _strip_digests(payload):
    """把响应里的**内容哈希**去掉:网关的响应会进毕昇工作流的输出,再由工作流回给模型。

    候选哈希一旦进了模型通道,「人核对完整 64 位哈希」这道仪式就退化成橡皮章
    (模型可以在聊天里替人念哈希)。所以哈希压根不进模型可见的响应——
    正则脱敏只是兜底,不是承重墙(第 5 轮 codex 的 M3)。
    磁盘上的结果文件不受影响:那里留的是**内部账本**,人要核对时去批准页看。
    """
    if isinstance(payload, dict):
        return {k: _strip_digests(v) for k, v in payload.items() if "sha256" not in k.lower()}
    if isinstance(payload, list):
        return [_strip_digests(v) for v in payload]
    return payload


def _scrub(payload, secrets: tuple[str, ...] = ()):
    """**模型可见通道的统一清洗**:剥哈希键 + 盖掉字符串里的本机绝对路径与 64 位哈希。

    `secrets` 是**按值**盖掉的凭据(网关自己的鉴权令牌 + 毕昇开放 API 密钥):
    清洗规则是按**名字**认的(`CTP_PASSWORD` / `Authorization: Bearer` 之类),
    凭据一旦被候选读了原文再塞进异常文本,名字那一层就认不出来——按值盖掉这一条
    是兜底,不是替代(第 10 轮 codex 的 Medium 5)。

    「按值」这一层**委托给 `rs.scrub_text`**:网关原先自己手写了一遍替换循环,
    **没有长度阈值**,而共享内核有(`len(secret) >= 8`)——两条通道对「多短的串才算
    凭据」的口径不一样,漂移的那一条就是漏的那一条(第 14 轮 codex 的 L10)。

    为什么要有「统一」这一层,以及为什么放在 `handle` 的**唯一出口**:
    清洗散落在各端点就会漏。网关有五条模型可见的出口(`_data` / `_candidates` /
    `_shape` 的三种 view / `_results` / `_feedback`),漏一条就等于边界没守住——
    结果文件里的 `error.message`、失败 Trace 的 `failures`、作业的 `report` 里
    都可能夹着本机路径(策略 raise 出来的文本、vnpy 异常的 detail)或候选自己的哈希。
    收在 `handle` 出口就没有「漏掉某个端点」这回事了。

    哈希的脱敏口径与 MCP 侧共用 `rs.redact_digests`:两边规则不一样,
    等于「哪条通道漏了就是漏了」(第 6 轮 codex 的 H1)。
    """
    if isinstance(payload, PlainText):
        # PlainText 是 str 的子类:保住这个标记,否则响应会退化成 JSON(换行变成 \n),
        # 毕昇工具节点里人就没法读了。
        return PlainText(_scrub(str(payload), secrets))
    payload = _strip_digests(payload)
    if isinstance(payload, dict):
        return {k: _scrub(v, secrets) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_scrub(v, secrets) for v in payload]
    if isinstance(payload, str):
        return rs.scrub_text(payload, secrets=secrets)
    return payload


def _default_jobs() -> SimpleNamespace:
    import research_api as api
    return SimpleNamespace(data_report=api.data_report, backtest_job=api.backtest_job, gate_job=api.gate_job, evaluate_job=api.evaluate_job,
                           evaluate_prepare=api.evaluate_prepare, evaluate_run=api.evaluate_run,
                           plateau_from_data=api.plateau_from_data)


class GatewayApp:
    """纯逻辑的请求处理(不涉及 socket),便于单测。"""

    def __init__(self, cfg: dict, paths: dict, token: str, jobs=None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, secrets: tuple[str, ...] = ()):
        self.cfg, self.paths, self.token = cfg, paths, token
        self.jobs = jobs if jobs is not None else _default_jobs()
        self.clock = clock
        self.wall = wall                      # 挂钟时间(秒):迭代上限的滚动窗口用它;clock 是单调钟,只用于限流
        # **按值**要盖掉的凭据清单:与 MCP / CLI 两条通道共用同一份
        # (`research_config.known_secret_values`,第 14 轮 codex 的 M3/L10)。
        # 网关手里已经握着鉴权令牌,不必再读一次令牌文件——但**毕昇开放 API 密钥**
        # 也要在这一层盖掉:原先网关只盖令牌,候选把密钥写进异常文本就原样出去了。
        # 清单由 `main()` 传入(读真机上那份密钥文件只该在真机做一次);
        # 单测可以直接传自己的清单,不去读真机的密钥文件。
        # 网关自己的鉴权令牌**永远**在清单里(它就是这条通道的凭据);
        # 外部传进来的清单只是补充,不能把它挤掉。
        self.secrets = tuple(dict.fromkeys((token,) + tuple(secrets)))
        self._ledger_lock = threading.Lock()
        self._hits: deque[float] = deque()
        self._busy = threading.Lock()
        self._lock = threading.Lock()

    # ---- 通用关口 ----
    def _rate_limited(self) -> bool:
        now = self.clock()
        limit = self.cfg["gateway"]["rate_limit_per_minute"]
        with self._lock:
            while self._hits and now - self._hits[0] >= 60.0:
                self._hits.popleft()
            if len(self._hits) >= limit:
                return True
            self._hits.append(now)
            return False

    def _authorised(self, headers: dict) -> bool:
        value = headers.get("authorization", "")
        scheme, _, supplied = value.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        headers = {k.lower(): v for k, v in headers.items()}
        if self._rate_limited():
            return 429, {"error": "请求过于频繁"}
        if not self._authorised(headers):
            return 401, {"error": "未授权"}          # 先鉴权再路由:不向未授权者泄露路由是否存在
        if len(body) > self.cfg["gateway"]["max_body_bytes"]:
            return 413, {"error": "请求体过大"}

        route, params = self._match(method, path)
        if route is None:
            known_paths = {p.split("{")[0].rstrip("/") for _, p in ROUTES}
            return (405, {"error": "方法不允许"}) if path.rstrip("/") in known_paths else (404, {"error": "不存在"})
        data: dict = {}
        if method == "POST":
            try:
                data = json.loads(body.decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return 400, {"error": "请求体不是合法 JSON"}
            if not isinstance(data, dict):
                return 400, {"error": "请求体必须是 JSON 对象"}
        try:
            status, payload = getattr(self, "_" + route)(data, params)
        except rs.SandboxError as exc:
            # 错误消息会经毕昇工作流回到模型:**走统一清洗**再出去
            # (模型看到本机目录结构没有用处,却多了一条指纹识别信息)。
            # 这条分支曾经只做 redact_paths,哈希/批准命令/凭据都漏——清洗必须只有一个出口
            # (第 7 轮 codex 的 F4)。
            return 400, _scrub({"error": str(exc)}, self.secrets)
        except Exception as exc:                                    # 兜底:不回传异常细节(可能含本机路径)
            print(f"网关内部错误: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 500, {"error": "内部错误"}
        # **模型可见通道的唯一出口**:剥哈希键 + 盖掉本机绝对路径。
        # 收在这里而不是散在各端点:散着就会漏(网关有五条模型可见的出口)。
        return status, _scrub(payload, self.secrets)

    @staticmethod
    def _match(method: str, path: str):
        path = path.split("?", 1)[0]
        if (method, path) in ROUTES:
            return path.rsplit("/", 1)[-1], {}
        prefix = "/research/results/"
        if method == "GET" and path.startswith(prefix) and "/" not in path[len(prefix):] and path[len(prefix):]:
            return "results", {"run_id": path[len(prefix):]}
        return None, {}

    # ---- 提交账本:滚动窗口内最多提交 N 个候选(服务端强制,LLM 无法绕过) ----
    def _ledger_path(self) -> Path:
        return self.paths["results_dir"] / "submissions.jsonl"

    def _ledger_entries(self) -> list[dict]:
        path = self._ledger_path()
        if not path.exists():
            return []
        entries: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if not isinstance(data, dict) or isinstance(data.get("at"), bool) or not isinstance(data.get("at"), (int, float)):
                    raise ValueError("记录缺少时间戳")
                entries.append(data)
        except (OSError, ValueError) as exc:
            raise LedgerError(f"提交账本损坏或无法读取,拒绝提交(fail-closed): {exc}") from exc
        return entries

    def _iteration_status(self, entries: list[dict], now: float) -> dict:
        cfg = self.cfg["iteration"]
        window = cfg["window_hours"] * 3600.0
        recent = [e for e in entries if now - e["at"] < window]
        release = None if not recent else int(round(min(e["at"] for e in recent) + window - now))
        return {"used": len(recent), "limit": cfg["max_iterations"], "window_hours": cfg["window_hours"],
                "next_release_in_seconds": release}

    def _write_ledger(self, entries: list[dict]) -> None:
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as stream:
            for entry in entries:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    def _iteration_snapshot(self) -> dict:
        try:
            with self._ledger_lock:
                return self._iteration_status(self._ledger_entries(), self.wall())
        except LedgerError as exc:
            return {"error": str(exc)}

    # ---- 端点 ----
    def _data(self, _data, _params):
        return 200, {"data": self.jobs.data_report(), "iteration": self._iteration_snapshot()}

    def _candidates(self, data, _params):
        name, source = data.get("name"), data.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            return 400, {"error": "name 与 source 必须是字符串"}
        name = name.strip()
        if name and not name.endswith(".py"):
            name += ".py"                                           # 工作流里常把「名字」与「.py」分开给
        source = strip_code_fence(source)
        if not re.fullmatch(self.cfg["candidates"]["name_pattern"], name):
            return 400, {"error": "文件名不符合命名规则"}
        raw = source.encode("utf-8")
        if len(raw) > self.cfg["candidates"]["max_source_bytes"]:
            return 400, {"error": f"源码超过大小上限 {self.cfg['candidates']['max_source_bytes']} 字节"}
        screening = self.cfg["screening"]
        findings = rs.screen_source(source, screening["extra_forbidden_modules"], screening["extra_forbidden_calls"],
                                    screening.get("allowed_modules", ()))
        if findings:
            return 400, {"error": "静态筛查未通过: " + "; ".join(findings)}
        target = self.paths["pending_dir"] / name
        self.paths["pending_dir"].mkdir(parents=True, exist_ok=True)
        with self._ledger_lock:                                     # 额度检查、写文件、记账必须是一个整体,否则并发能多交
            try:
                entries = self._ledger_entries()
            except LedgerError as exc:
                return 500, {"error": str(exc)}
            now = self.wall()
            status = self._iteration_status(entries, now)
            if status["used"] >= status["limit"]:
                hours = (status["next_release_in_seconds"] or 0) / 3600.0
                return 429, {"error": f"滚动 {status['window_hours']} 小时内最多提交 {status['limit']} 个候选,"
                                      f"已用 {status['used']}/{status['limit']};最早一条约 {hours:.1f} 小时后释放", "iteration": status}
            if target.exists():
                return 409, {"error": "同名候选已存在,请换名"}              # 重名不占额度
            sha = hashlib.sha256(raw).hexdigest()
            window = self.cfg["iteration"]["window_hours"] * 3600.0
            kept = [e for e in entries if now - e["at"] < window]
            try:                                                    # 试验账本最先写:损坏则整个提交失败、什么都没占用;
                record_trials(self.paths["results_dir"] / "trials.jsonl", "submission", 1, at=now)   # 之后任何一步失败,多记的试验也保留(只增不减)
            except TrialLedgerError as exc:
                return 500, {"error": str(exc)}
            try:                                                    # 先记账再落文件:崩溃只会多扣一个额度,不会多放一个
                self._write_ledger(kept + [{"at": now, "name": name, "sha256": sha}])
            except OSError as exc:
                return 500, {"error": f"提交账本写入失败,未保存候选: {exc}"}
            try:
                with open(target, "xb") as stream:                  # x:绝不覆盖已有文件
                    stream.write(raw)
            except (FileExistsError, OSError) as exc:
                try:
                    self._write_ledger(kept)                        # 文件没落成就把刚记的账退回
                except OSError:
                    pass
                if isinstance(exc, FileExistsError):
                    return 409, {"error": "同名候选已存在,请换名"}
                return 500, {"error": f"候选文件写入失败: {exc}"}
        # **响应里不带 sha256**:网关的响应会进毕昇工作流的输出,再由工作流回给模型。
        # 候选哈希进了模型通道,「人核对完整 64 位哈希」这道仪式就退化成橡皮章
        # (第 8 轮 codex 的 C1)。哈希记在账本里(内部用),不对外。
        return 200, {"name": name}

    @staticmethod
    def _shape(outcome: tuple, view: str | None) -> tuple:
        status, payload = outcome
        if status != 200 or not isinstance(payload, dict):
            return outcome                                   # 错误一律保持 JSON,调用方才能读到原因
        if view == "report":
            return status, _compact(payload)
        if view == "text":
            return status, PlainText(f"{payload.get('report', '')}\n\n(run_id: {payload.get('run_id', '?')})")
        return status, payload

    @staticmethod
    def _job_error(exc) -> tuple[int, dict]:
        import research_api as api
        # 作业消息也可能带本机路径(批准记录位置之类),同样盖掉再出去。
        message = rs.redact_paths(exc.message)
        if exc.code == api.EXIT_TIMEOUT:
            return 504, {"error": message}
        if exc.code == api.EXIT_FAILED:
            return 500, {"error": message}
        return (403 if exc.message == api.NEEDS_APPROVAL else 400), {"error": message}

    def _run_job(self, func, **kwargs):
        import research_api as api
        if not self._busy.acquire(blocking=False):
            return 409, {"error": "已有作业在运行,请稍后重试"}
        try:
            return 200, func(**kwargs)
        except api.JobError as exc:
            return self._job_error(exc)
        finally:
            self._busy.release()

    # ---- 异步评估(Issue #84) ----
    # 毕昇工具节点对网关的读超时硬编码 30 秒,而评估要数分钟。所以 evaluate 改成「启动即返回」:
    # 前半段(入参、策略名、静态筛查、**批准检查**、试验预扣)在**请求线程里**同步做完,任何拒绝都以原状态码在启动前返回;
    # 通过后才起后台线程跑慢的后半段。线程不做任何检查,也就**不可能绕过**检查。
    # 返回码用 **200**(不是 202):毕昇的 API 工具把任何非 200 都当异常抛。
    # 运行状态写在结果目录的 `running/` 子目录(见 research_sandbox.RUNNING_DIRNAME),最终结果与同步评估同形、写顶层。
    def _start_evaluate(self, strategy: str, cls: str, random_runs, product: str, view):
        import research_api as api
        results_dir = self.paths["results_dir"]
        if not self._busy.acquire(blocking=False):
            return 409, {"error": "已有作业在运行,请稍后重试"}
        started = False
        run_id = None
        try:
            try:
                ctx = self.jobs.evaluate_prepare(strategy=strategy, cls=cls, random_runs=random_runs, confirm=False,
                                                 product=product)
            except api.JobError as exc:
                return self._job_error(exc)
            run_id = rs.new_run_id()
            try:
                rs.write_running(results_dir, run_id, {
                    "kind": "evaluate", "status": "running", "key": product, "strategy_file": ctx.get("strategy_file"),
                    "started_at": datetime.fromtimestamp(self.wall()).isoformat(timespec="seconds")})
            except OSError as exc:
                print(f"网关内部错误: 运行状态无法登记: {exc}", file=sys.stderr)
                return 500, {"error": "运行状态无法登记,评估未启动(试验次数已预扣,不退还)"}
            try:
                worker = threading.Thread(target=self._evaluate_worker, args=(ctx, run_id), name=f"evaluate-{run_id}",
                                          daemon=True)
                worker.start()
            except RuntimeError as exc:
                rs.clear_running(results_dir, run_id)
                print(f"网关内部错误: 无法启动评估线程: {exc}", file=sys.stderr)
                return 500, {"error": "无法启动评估线程(试验次数已预扣,不退还)"}
            started = True                                              # 从这里起 _busy 由线程释放
        finally:
            if not started:
                self._busy.release()
        poll = f"稍后用 get_result(run_id={run_id}) 取结果;status=running 表示还在跑(通常几分钟),别重复提交。"
        if view == "text":
            return 200, PlainText(f"评估已启动(run_id: {run_id})。{poll}")
        return 200, {"run_id": run_id, "status": "running", "message": poll}

    def _evaluate_worker(self, ctx: dict, run_id: str) -> None:
        import research_api as api
        results_dir = self.paths["results_dir"]
        try:
            self.jobs.evaluate_run(ctx, run_id=run_id)                  # 最终结果落顶层 <run_id>.json
            rs.clear_running(results_dir, run_id)
        except api.JobError as exc:
            self._finish_running(run_id, rs.redact_paths(exc.message), exc.code)
        except Exception as exc:                                        # 兜底:任何异常都落成 error,绝不留下永久 running
            print(f"网关内部错误(评估线程): {type(exc).__name__}: {exc}", file=sys.stderr)
            self._finish_running(run_id, "内部错误", None)
        finally:
            self._busy.release()

    def _finish_running(self, run_id: str, message: str, code) -> None:
        try:
            state = rs.load_result(run_id, self.paths["results_dir"])
        except (rs.SandboxError, ValueError, OSError):
            state = {"kind": "evaluate"}
        try:
            rs.write_running(self.paths["results_dir"], run_id, {**state, "status": "error", "error": message, "code": code})
        except OSError as exc:
            print(f"网关内部错误: 无法记录评估失败: {exc}", file=sys.stderr)

    def recover_interrupted(self) -> list[str]:
        """启动时调用:上个进程留下的 running 改标 interrupted(那些线程已随进程消失)。"""
        return rs.mark_interrupted(self.paths["results_dir"])

    def _backtest(self, data, _params):
        strategy, cls, key, segment = (data.get(k) for k in ("strategy", "class", "key", "segment"))
        if not all(isinstance(x, str) for x in (strategy, cls, key, segment)) or not CLASS_NAME.match(cls):
            return 400, {"error": "strategy/class/key/segment 必须是字符串,class 必须是合法标识符"}
        if key not in rs.KEYS or segment not in rs.SEGMENTS:
            return 400, {"error": f"key 只能是 {list(rs.KEYS)},segment 只能是 {list(rs.SEGMENTS)}"}
        params = _lenient_object(data.get("params"), "params") or {}
        view = _view(data)
        # confirm 固定为 False:批准只能由本机 CLI 触发,请求体里的同名字段被忽略
        return self._shape(self._run_job(self.jobs.backtest_job, strategy=strategy, cls=cls, key=key, segment=segment,
                                         params=params, confirm=False), view)

    def _gate(self, data, _params):
        strategy, cls = data.get("strategy"), data.get("class")
        if not isinstance(strategy, str) or not isinstance(cls, str) or not CLASS_NAME.match(cls):
            return 400, {"error": "strategy/class 必须是字符串,class 必须是合法标识符"}
        params = _lenient_object(data.get("params"), "params") or {}
        random_runs = _lenient_int(data.get("random_runs"), "random_runs")
        plateau_data = _lenient_object(data.get("plateau"), "plateau")   # 只接受内联网格得分,绝不读取调用方给的路径
        plateau = (False, "未提供 plateau:参数高原未评估,按未通过处理")
        if plateau_data is not None:
            plateau = self.jobs.plateau_from_data(plateau_data)
        view = _view(data)
        return self._shape(self._run_job(self.jobs.gate_job, strategy=strategy, cls=cls, params=params, plateau=plateau,
                                         random_runs=random_runs, confirm=False), view)

    def _evaluate(self, data, _params):
        strategy, cls = data.get("strategy"), data.get("class")
        if not isinstance(strategy, str) or not isinstance(cls, str) or not CLASS_NAME.match(cls):
            return 400, {"error": "strategy/class 必须是字符串,class 必须是合法标识符"}
        random_runs = _lenient_int(data.get("random_runs"), "random_runs")
        # 参数网格由候选类的 param_grid 声明,不经请求体传入:请求体里不接受任何影响选参的字段
        # product(Issue #69)只是白名单里的一个键,缺省 C15;非法值由 evaluate_job 拒绝
        product = data.get("product")
        if product is None or product == "":
            product = "C15"
        if not isinstance(product, str) or product not in rs.PRODUCTS:
            return 400, {"error": f"product 只能是 {list(rs.PRODUCTS)}"}
        return self._start_evaluate(strategy, cls, random_runs, product, _view(data))

    def _results(self, _data, params):
        run_id = params["run_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            return 400, {"error": "非法的 run_id"}
        try:
            return 200, rs.load_result(run_id, self.paths["results_dir"])
        except rs.SandboxError:
            return 404, {"error": "没有这条结果"}

    def _feedback(self, _data, _params):
        """最近 N 条失败的结构化 Trace(N 来自配置)。只含固定字段,不回传源码、路径或调用方自由文本。

        另外回一份**已有候选的文件名**:起名那一步要避开它们。
        少了这一段,同一个研究想法重跑会撞名 → 网关 409 → 整条工作流中止,
        「多轮研究循环」实际退化成单轮(第 6 轮实测:同一个想法第二跑死在 409 上)。
        只给文件名(且只给符合命名规则的),不给路径、不给源码、不给内容。"""
        limit = self.cfg["feedback"]["limit"]
        traces = []
        for run_id in reversed(rs.list_results(self.paths["results_dir"])):
            try:
                result = rs.load_result(run_id, self.paths["results_dir"])
            except rs.SandboxError:
                continue
            failures = [str(f)[:MAX_TRACE_TEXT] for f in (result.get("failures") or [])][:MAX_TRACE_FAILURES]
            diagnosis = result.get("diagnosis") or {}
            weakest = diagnosis.get("weakest") if isinstance(diagnosis, dict) else None
            error = result.get("error")
            if result.get("status") == "strategy_error" and isinstance(error, dict):
                failures = [f"策略运行报错: {str(error.get('type', ''))[:60]}: {str(error.get('message', ''))[:MAX_TRACE_TEXT]}"]
            failed = bool(failures) if (result.get("kind") in ("gate", "evaluate") or failures) else weakest is not None
            if not failed:
                continue
            cls = result.get("class")
            traces.append({
                "run_id": run_id, "kind": result.get("kind"), "key": result.get("key"),
                "class": cls if isinstance(cls, str) and CLASS_NAME.match(cls) else None,
                "weakest": weakest if isinstance(weakest, str) else None, "failures": failures,
                "summary": str(diagnosis.get("summary", ""))[:MAX_TRACE_TEXT] if isinstance(diagnosis, dict) else "",
            })
            if len(traces) >= limit:
                break
        return 200, {"limit": limit, "pending_candidates": self._pending_names(), "traces": traces}

    def _pending_names(self) -> list[str]:
        """已有候选的文件名(新到旧)。只回文件名,且只回符合命名规则的那些。

        名字本身不是秘密(工作流本来就把候选名告诉模型),但它是**调用方自由文本**:
        所以按命名规则过滤一遍,免得目录里别的文件混进来。"""
        pattern = self.cfg["candidates"]["name_pattern"]
        pending = self.paths["pending_dir"]
        try:
            files = list(pending.glob("*.py"))
        except OSError:
            return []
        stamped = []
        for path in files:
            if not re.fullmatch(pattern, path.name):
                continue
            try:                                    # 逐个取时间戳:中途有文件被删不该让整份名单作废
                stamped.append((path.stat().st_mtime, path.name))
            except OSError:
                continue
        stamped.sort(reverse=True)                # 新到旧
        return [name for _, name in stamped[:MAX_PENDING_NAMES]]


def build_server(app: GatewayApp, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "research-gateway"
        sys_version = ""

        def _serve(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            limit = app.cfg["gateway"]["max_body_bytes"]
            body = self.rfile.read(length) if 0 < length <= limit else (b"x" * (limit + 1) if length > limit else b"")
            status, payload = app.handle(method, self.path, dict(self.headers.items()), body)
            is_text = isinstance(payload, PlainText)
            raw = (str(payload) if is_text else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ("text/plain" if is_text else "application/json") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                # 调用方(毕昇工具节点)超时或提前断开:这是**正常的**收尾,
                # 不是网关故障。不接住的话 socketserver 会把整段 traceback 打进日志,
                # 真正的错误反而被淹没(实测本机日志里就有这么一大段)。
                self.close_connection = True

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_PUT(self):
            self._serve("PUT")

        def do_DELETE(self):
            self._serve("DELETE")

        def log_message(self, fmt, *args):                          # 访问日志不含请求体与令牌
            print(f"{self.address_string()} {fmt % args}", file=sys.stderr)

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split(chr(10))[0])
    parser.add_argument("command", choices=("serve", "init-token"))
    args = parser.parse_args(argv)
    try:
        cfg = rc.load()
        paths = rc.resolve_paths(cfg, rc.ROOT)
        if args.command == "init-token":
            init_token(paths["token_file"])
            print(f"令牌已生成: {paths['token_file']}(内容不在此打印;把它配置到毕昇工具的鉴权头里)")
            return 0
        assert_clean_environment(os.environ)
        token = load_token(paths["token_file"])
    except (rc.ConfigError, GatewayError) as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 2
    app = GatewayApp(cfg, paths, token, secrets=rc.known_secret_values(
        cfg=cfg, root=rc.ROOT, label="research-gateway", token=token))
    recovered = app.recover_interrupted()
    if recovered:
        print(f"上次未跑完的评估已改标 interrupted: {recovered}", file=sys.stderr)
    server = build_server(app, cfg["gateway"]["host"], cfg["gateway"]["port"])
    print(f"研究沙盒网关监听 {cfg['gateway']['host']}:{cfg['gateway']['port']}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    rconsole.reconfigure_stdio()
    sys.exit(main())
