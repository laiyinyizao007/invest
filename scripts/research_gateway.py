"""研究沙盒 HTTP 网关(阶段四 4b):毕昇与确定性内核之间唯一的网络通道。

只做适配,不写任何研究逻辑——全部转调 research_api 的作业函数。用标准库实现(无新增依赖),
因为这是仅本机可达、低并发的薄网关;依赖越少,供应链与攻击面越小。

端点即白名单(ROUTES);其余一律 404/405。**不存在**:下单、查账户、改风控、连 CTP、访问测试段、
批准(approve)、写 approved/。批准只能由本机 CLI 触发(research_api.py approve),请求体里的 confirm 字段会被忽略。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import bootstrap
import research_config as rc
import research_sandbox as rs
from risk.ladder import Ladder, LadderError, LadderExhausted, anchor_path_for
from risk.outward import OutwardError, OutwardTampered, RevealStore, reveal
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
LADDER_EXHAUSTED = ("验证窗口的梯子查询预算已用尽:不再给出任何验证窗口的信息,需要人工续期"
                    "(选参窗口的信息、/research/feedback 里已揭示过的结果不受影响)")
LADDER_UNAVAILABLE = "梯子状态或揭示存档不可用(fail-closed):拒绝给出任何验证窗口的信息,请人工检查"
PHASE_LABELS = {"declaration": "声明期", "selection": "选参窗口回放", "validation": "验证窗口回放"}
RESULT_KINDS = ("evaluate", "gate", "backtest")      # 只有这几种 JSON 是"运行结果";results 目录里别的 *.json 不是
VALID_SEGMENT_REFUSED = ("验证段的回测不能经网关直接读取:它会绕过梯子直接给出验证窗口的完整统计。"
                         "验证窗口的成绩只经 /research/evaluate(或 /research/gate)按梯子量化后给出")


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


def _default_jobs() -> SimpleNamespace:
    import research_api as api
    return SimpleNamespace(data_report=api.data_report, backtest_job=api.backtest_job, gate_job=api.gate_job, evaluate_job=api.evaluate_job,
                           plateau_from_data=api.plateau_from_data)


class GatewayApp:
    """纯逻辑的请求处理(不涉及 socket),便于单测。"""

    def __init__(self, cfg: dict, paths: dict, token: str, jobs=None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.cfg, self.paths, self.token = cfg, paths, token
        self.jobs = jobs if jobs is not None else _default_jobs()
        self.clock = clock
        self.wall = wall                      # 挂钟时间(秒):迭代上限的滚动窗口用它;clock 是单调钟,只用于限流
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
            return getattr(self, "_" + route)(data, params)
        except rs.SandboxError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:                                    # 兜底:不回传异常细节(可能含本机路径)
            print(f"网关内部错误: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 500, {"error": "内部错误"}

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
        findings = rs.screen_source(source, screening["extra_forbidden_modules"], screening["extra_forbidden_calls"])
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
        return 200, {"name": name, "sha256": sha}

    @staticmethod
    def _shape(outcome: tuple, view: str | None) -> tuple:
        status, payload = outcome
        if status != 200 or not isinstance(payload, dict):
            return outcome                                   # 错误一律保持 JSON,调用方才能读到原因
        if view == "report":
            return status, _compact(payload)
        if view == "text":
            return status, PlainText(f"{payload.get('report', '')}\n\n(run_id: {payload.get('run_id', '?')})")
        return outcome

    # ---- 对外揭示(risk/outward.py):外层看到的一切都从这里过 ----
    def _ladder(self) -> Ladder:
        # 放进子目录:`list_results` 按 `*.json` 列 run,梯子状态与结果混放会被当成一个"结果"(自查发现)
        return Ladder(Path(self.paths["results_dir"]) / "ladder" / "ladder.json",
                      anchor=anchor_path_for(self.paths["results_dir"]))

    def _reveal_store(self) -> RevealStore:
        return RevealStore(Path(self.paths["results_dir"]) / "outward")

    def _ladder_precheck(self):
        """跑会产生验证窗口数字的作业**之前**先看梯子还有没有查询:用尽了就不必白跑一次(也不白耗试验次数)。"""
        try:
            left = self._ladder().status()["queries_left"]
        except LadderError:
            return 500, {"error": LADDER_UNAVAILABLE}
        return (429, {"error": LADDER_EXHAUSTED}) if left <= 0 else None

    def _reveal(self, result: dict, *, allow_query: bool = True, skip_bad_result: bool = False,
                ladder=None, store=None):
        """落盘结果 → 净化视图。梯子用尽 429;梯子/存档损坏 500(fail-closed,不放出任何验证窗口信息)。

        `skip_bad_result`(批量接口 `/feedback` 用,第 17 轮 M2):**单个**结果/存档不合法只跳过这一个(返回 None),
        梯子本身不可用仍然整体 500——一份坏结果文件不该让进化回路唯一的诊断通道整体下线。

        **但跳过必须有信号**(第 19 轮 codex 的 H1):跳过时在 stderr 记一行,并把原因分类返回给调用方。
        分两类:
        - `OutwardTampered`(揭示存档被改过):**篡改信号**,必须计数上报,不能和"没有失败"长得一样;
        - `OutwardError`(结果本身不合法):这一条没法揭示,跳过它,通道继续。
        两者都不再与"从未揭示过"(None)共用同一个语义。

        `ladder`/`store` 可由调用方传入**已读好的快照**(第 19 轮 L3):批量接口一次只读一次梯子。"""
        try:
            view = reveal(result, ladder=ladder if ladder is not None else self._ladder(),
                          store=store if store is not None else self._reveal_store(), allow_query=allow_query)
        except LadderExhausted:
            return 429, {"error": LADDER_EXHAUSTED}, None
        except LadderError:
            return 500, {"error": LADDER_UNAVAILABLE}, None
        except OutwardTampered as exc:
            print(f"揭示存档被篡改,已跳过并计数: {exc}", file=sys.stderr)
            if not skip_bad_result:
                return 500, {"error": LADDER_UNAVAILABLE}, None
            return 200, None, "tampered"
        except OutwardError as exc:
            print(f"结果不合法,已跳过: {exc}", file=sys.stderr)
            if not skip_bad_result:
                return 500, {"error": LADDER_UNAVAILABLE}, None
            return 200, None, "invalid_result"
        return 200, view, None

    def _reveal_outcome(self, outcome: tuple):
        status, payload = outcome
        if status != 200 or not isinstance(payload, dict):
            return outcome                                   # 作业层的错误原样返回(不含验证窗口信息)
        status, view, _ = self._reveal(payload)
        return status, view

    def _run_job(self, func, **kwargs):
        import research_api as api
        if not self._busy.acquire(blocking=False):
            return 409, {"error": "已有作业在运行,请稍后重试"}
        try:
            return 200, func(**kwargs)
        except api.JobError as exc:
            if exc.code == api.EXIT_TIMEOUT:
                return 504, {"error": exc.message}
            if exc.code == api.EXIT_FAILED:
                return 500, {"error": exc.message}
            return (403 if exc.message == api.NEEDS_APPROVAL else 400), {"error": exc.message}
        finally:
            self._busy.release()

    def _backtest(self, data, _params):
        strategy, cls, key, segment = (data.get(k) for k in ("strategy", "class", "key", "segment"))
        if not all(isinstance(x, str) for x in (strategy, cls, key, segment)) or not CLASS_NAME.match(cls):
            return 400, {"error": "strategy/class/key/segment 必须是字符串,class 必须是合法标识符"}
        if key not in rs.KEYS or segment not in rs.SEGMENTS:
            return 400, {"error": f"key 只能是 {list(rs.KEYS)},segment 只能是 {list(rs.SEGMENTS)}"}
        if segment == "valid":
            return 403, {"error": VALID_SEGMENT_REFUSED}
        params = _lenient_object(data.get("params"), "params") or {}
        view = _view(data)
        # confirm 固定为 False:批准只能由本机 CLI 触发,请求体里的同名字段被忽略
        return self._shape(self._reveal_outcome(self._run_job(
            self.jobs.backtest_job, strategy=strategy, cls=cls, key=key, segment=segment, params=params, confirm=False)), view)

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
        refused = self._ladder_precheck()
        if refused:
            return refused
        return self._shape(self._reveal_outcome(self._run_job(
            self.jobs.gate_job, strategy=strategy, cls=cls, params=params, plateau=plateau,
            random_runs=random_runs, confirm=False)), view)

    def _evaluate(self, data, _params):
        strategy, cls = data.get("strategy"), data.get("class")
        if not isinstance(strategy, str) or not isinstance(cls, str) or not CLASS_NAME.match(cls):
            return 400, {"error": "strategy/class 必须是字符串,class 必须是合法标识符"}
        random_runs = _lenient_int(data.get("random_runs"), "random_runs")
        # 参数网格由候选类的 param_grid 声明,不经请求体传入:请求体里不接受任何影响选参的字段
        view = _view(data)
        extra = {}
        if data.get("campaign") not in (None, ""):
            # 战役信息(谱系台账用):{campaign_id, parent_sha256, generation}。形状与取值由作业层严格校验;
            # 这里只负责把"JSON 字符串形式的对象"(毕昇工具节点把所有参数当字符串传)还原成对象
            campaign = _lenient_object(data.get("campaign"), "campaign")
            if campaign is None:
                return 400, {"error": "campaign 必须是对象或其 JSON 字符串"}
            if isinstance(campaign.get("generation"), str):
                campaign["generation"] = _lenient_int(campaign["generation"], "campaign.generation")
            extra["campaign"] = campaign
        refused = self._ladder_precheck()
        if refused:
            return refused
        return self._shape(self._reveal_outcome(self._run_job(
            self.jobs.evaluate_job, strategy=strategy, cls=cls, random_runs=random_runs, confirm=False, **extra)), view)

    def _results(self, _data, params):
        run_id = params["run_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            return 400, {"error": "非法的 run_id"}
        try:
            result = rs.load_result(run_id, self.paths["results_dir"])
        except rs.SandboxError:
            return 404, {"error": "没有这条结果"}
        if not isinstance(result, dict) or result.get("kind") not in RESULT_KINDS:
            return 404, {"error": "没有这条结果"}            # results 目录里不是运行结果的 *.json(审批记录等)不外露
        # 落盘的是给人看的完整记录;外层只看净化视图。第一次读到带验证窗口数字的 run 才消耗一次梯子查询,之后回放
        status, view, _ = self._reveal(result)
        return status, view

    def _feedback(self, _data, _params):
        """最近 N 条失败的结构化 Trace(N 来自配置)。只含固定字段,不回传源码、路径或调用方自由文本。

        只取**已经向外层揭示过**的 run(或根本没有验证窗口数字的 run):一次 `/feedback` 要扫很多 run,
        不能让一次读取悄悄烧掉一批梯子查询,也不能把从未揭示过的验证窗口数字带出去。
        失败原因是门槛检查项的稳定 ID,不是带验证窗口数字的中文句子。"""
        limit = self.cfg["feedback"]["limit"]
        traces = []
        # 跳过必须**看得见**(第 19 轮 codex 的 H1):此前跳过坏结果/坏存档时响应里没有任何提示,
        # 外层与运维分不清"没有失败"和"失败被藏了"。现在计数并分类上报(只给计数与分类,不给内容)。
        skipped = {"tampered": 0, "invalid_result": 0, "unreadable": 0, "orphaned_by_reset": 0}
        # 一次只读一次梯子(第 19 轮 L3):此前每扫一个结果就重读并重校验整条日志。
        try:
            ladder = self._ladder().snapshot()
        except LadderError:
            return 500, {"error": LADDER_UNAVAILABLE}
        store = self._reveal_store()
        try:
            orphaned = set(self._ladder().orphaned_run_ids())
        except LadderError:
            orphaned = set()
        for run_id in reversed(rs.list_results(self.paths["results_dir"])):
            try:
                result = rs.load_result(run_id, self.paths["results_dir"])
            except rs.SandboxError:
                skipped["unreadable"] += 1
                continue
            if not isinstance(result, dict) or result.get("kind") not in RESULT_KINDS:
                continue
            status, view, reason = self._reveal(result, allow_query=False, skip_bad_result=True,
                                                ladder=ladder, store=store)
            if status != 200:
                return status, view
            if view is None:                                    # 跳过了这个 run:必须说清为什么
                # "被 reset 孤立"既不是"从未揭示过",也不是"被篡改"——reset 把梯子清空了,
                # 历史 run 因此永远读不出来(回放时梯子里查无此 run,若当成篡改就是**误报**)。
                # 这三种情况必须分开计数,否则 reset 一执行,诊断通道就静默变空,
                # 或者一次正常的人工重置就被记成安全事件(第 19 轮 codex 的 H1)。
                if run_id in orphaned:
                    skipped["orphaned_by_reset"] += 1
                elif reason is not None:
                    skipped[reason] += 1
                continue
            if reason is not None:
                skipped[reason] += 1
            error = view.get("error") if isinstance(view.get("error"), dict) else None
            if view.get("status") == "strategy_error" and error:
                detail = str(error.get("message") or "报错原文仅人可见")[:MAX_TRACE_TEXT]      # 验证窗口回放里的原文已在视图里去掉
                where = PHASE_LABELS.get(error.get("phase")) or (
                    "训练段回测" if view.get("kind") == "backtest" and error.get("message") else "验证窗口回放")
                failures = [f"策略运行报错({error.get('type') or '自定义异常'},{where}): {detail}"]
            else:
                failures = list(view.get("failed_checks") or [])[:MAX_TRACE_FAILURES]
            failed = view.get("status") == "strategy_error" or (view.get("kind") in ("gate", "evaluate") and not view.get("passed"))
            if not failed:
                continue
            diagnosis = view.get("selection_diagnosis") if isinstance(view.get("selection_diagnosis"), dict) else {}
            traces.append({
                "run_id": run_id, "kind": view.get("kind"), "class": view.get("class"),
                "weakest": diagnosis.get("weakest"), "failures": failures,
                "summary": str(view.get("report", ""))[:MAX_TRACE_TEXT * 2],
            })
            if len(traces) >= limit:
                break
        return 200, {"limit": limit, "traces": traces, "skipped": skipped,
                     "skipped_total": sum(skipped.values())}


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
            self.wfile.write(raw)

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
        paths = rc.resolve_paths(cfg, bootstrap.ROOT)
        if args.command == "init-token":
            init_token(paths["token_file"])
            print(f"令牌已生成: {paths['token_file']}(内容不在此打印;把它配置到毕昇工具的鉴权头里)")
            return 0
        assert_clean_environment(os.environ)
        token = load_token(paths["token_file"])
    except (rc.ConfigError, GatewayError) as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 2
    app = GatewayApp(cfg, paths, token)
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
    sys.exit(main())
