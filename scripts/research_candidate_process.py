"""框架侧:起候选宿主进程、取回信号序列(Issue #29 S1,ADR `20261115-candidate-process-isolation.md` 4.3 节)。

**只有这一个出口**把候选的东西带回框架:一个等长的、取值在 {-1,0,1} 里的信号序列。
候选进程回传的其它任何东西(指标、结果文件、额外字段)——协议里没有位置放,框架也无处可取。

怎么起(每一条都有对应的动态探针,见 `tests/test_candidate_process_isolation.py`):
- `python -E -P -B -c <宿主源码>`:**按源码文本起,不按路径起**。按路径起时候选能从 `sys.orig_argv` 与
  自己调用栈里宿主帧的 `co_filename` 读到仓库路径(A2 探针实测抓到)。`-E` 去环境注入,`-P` 不加脚本/当前
  目录进 `sys.path`,`-B` 不写字节码。**不能用 `-I`**:它连用户 site-packages 一起去掉,talib 就没了。
- cwd = 空临时目录;环境变量走 `rs.sandbox_env` 白名单(无 `CTP_*` / `WEBHOOK_*` / `LIVE_*`)。
- 候选源码、参数经临时目录里的 `request.json` 交给宿主;候选运行时包(`research/candidate`)每次复制一份进临时目录。
- K 线走 stdin 的长度前缀二进制帧(**值**传递:候选进程里没有任何行情文件路径)。
- stdin 由单独线程写、主线程轮询 stdout/stderr 文件大小:Windows 管道缓冲很小,一边写一边读才不会死锁;
  输出超过上限立刻杀(候选不能靠狂写 stdout/stderr 吃光内存或磁盘)。
- 超时杀整棵进程树。

返回值**不可信**:这里只做形状校验(帧、magic、长度、取值域),越界一律整格判失败,不截断、不修补。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import research_config as rc
import research_sandbox as rs
from research.candidate import protocol as proto

HOST_FILE = Path(rc.CODE_ROOT) / "scripts" / "research_candidate_host.py"
RUNTIME_SRC = Path(rc.CODE_ROOT) / "research" / "candidate"
# Windows 的命令行上限约 32767 字符;宿主源码走 `-c`,留足余量。超了就明确报错而不是被截断后跑出怪行为。
MAX_HOST_SOURCE_CHARS = 30_000
# 响应 = 一个控制帧(几十字节)+ 一个信号帧(n_bars 字节)。给控制帧与错误文本留 64 KB 余量。
STDOUT_HEADROOM = 64 * 1024
MAX_STDERR_BYTES = 1024 * 1024
POLL_SECONDS = 0.05


class CandidateProcessError(Exception):
    """候选进程这一次没有给出合格的信号序列。

    kind:
      `candidate_error`  候选自己出错(抛异常、exit、返回非法取值)——属于研究中的正常结果;
                          `detail` 是候选写的文字(**不可信**,进入模型可见通道前必须由调用方净化)。
      `timeout` / `output_limit` / `protocol` / `launch`  框架侧的失败,detail 为空。
    """

    def __init__(self, kind: str, message: str, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


def kill_tree(proc: subprocess.Popen) -> None:
    """与 `research_api.kill_tree` 同一做法(venv 的 python.exe 是启动器,须杀整棵树)。
    不直接 import research_api:它是上层,会形成环。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        # POSIX:候选进程是自己会话的组长(启动时 start_new_session=True),整组一起杀——
        # 只 proc.kill() 杀不到孙进程(G8 codex 审查指出,容器里就是这条路径)。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def _tail(path, size: int) -> bytes:
    """只读文件末尾 `size` 字节,**不把整个文件读进内存**(候选可能把 stderr 写得很大)。"""
    with open(path, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, end - size))
        return fh.read(size)


def _remove_tree(path) -> None:
    """清临时目录;清不掉(Windows 上仍被占用)就在**本机 stderr** 留痕,不静默堆积。只打目录名,不打路径。"""
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists():
        print(f"警告:候选进程的临时目录未能清理(可能仍被占用): {Path(path).name}", file=sys.stderr)


def host_source() -> str:
    text = HOST_FILE.read_text(encoding="utf-8")
    if len(text) > MAX_HOST_SOURCE_CHARS:
        raise CandidateProcessError("launch", f"候选宿主源码过长({len(text)} 字符),超出命令行安全上限")
    return text


def _feed_stdin(proc: subprocess.Popen, payload: bytes) -> None:
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass                                           # 宿主已经退出:由主线程按退出结果判
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


def _parse_response(raw: bytes, n_bars: int) -> list[int]:
    """严格解析响应:`控制帧 [+ 信号帧]`,且**不允许有多余字节**。"""
    import io
    stream = io.BytesIO(raw)
    try:
        head = proto.decode_json(proto.read_frame(stream))
    except proto.ProtocolError as exc:
        raise CandidateProcessError("protocol", f"候选进程响应格式错误: {exc}") from exc
    if head.get("magic") != proto.MAGIC_RESPONSE:
        raise CandidateProcessError("protocol", "候选进程响应的 magic 不符")
    status = head.get("status")
    if status == "error":
        raise CandidateProcessError("candidate_error", "候选进程报告失败", detail=str(head.get("error", "")))
    if status != "ok":
        raise CandidateProcessError("protocol", "候选进程响应的 status 非法")
    # `n_bars` 以框架自己发出去的根数为准,不采信响应里的自报
    try:
        signals = proto.unpack_signals(proto.read_frame(stream), n_bars)
    except proto.ProtocolError as exc:
        raise CandidateProcessError("protocol", f"候选进程返回的信号序列不合格: {exc}") from exc
    if stream.read(1):
        raise CandidateProcessError("protocol", "候选进程响应后面有多余字节")
    return signals


def run_candidate_signals(source: str, class_name: str, params: dict, bars, *, interval: str = "15m",
                          timeout: float = 600.0, kill=kill_tree) -> list[int]:
    """在独立进程里跑一次候选,返回与 `bars` 等长的入场意图序列。失败抛 `CandidateProcessError`。"""
    n_bars = len(bars)
    if n_bars == 0:
        raise CandidateProcessError("protocol", "没有可喂给候选的 K 线")
    try:
        payload = proto.encode_frame(proto.pack_bars(bars))
    except proto.ProtocolError as exc:
        raise CandidateProcessError("protocol", f"K 线无法打包: {exc}") from exc
    code = host_source()

    runtime = Path(tempfile.mkdtemp(prefix="cand-rt-"))
    workdir = Path(tempfile.mkdtemp(prefix="cand-cwd-"))
    out_path, err_path = workdir.parent / (workdir.name + ".out"), workdir.parent / (workdir.name + ".err")
    proc = None
    try:
        shutil.copytree(RUNTIME_SRC, runtime / "candidate", ignore=shutil.ignore_patterns("__pycache__"))
        (runtime / "request.json").write_text(json.dumps({
            "class": class_name, "params": params, "interval": interval,
            "n_bars": n_bars, "source": source}, ensure_ascii=False), encoding="utf-8")
        env = rs.sandbox_env(os.environ)
        env["CANDIDATE_RUNTIME_DIR"] = str(runtime)
        max_stdout = n_bars + STDOUT_HEADROOM
        with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
            try:
                proc = subprocess.Popen([sys.executable, "-E", "-P", "-B", "-c", code], stdin=subprocess.PIPE,
                                        stdout=out_fh, stderr=err_fh, cwd=workdir, env=env,
                                        **({} if sys.platform == "win32" else {"start_new_session": True}))
            except OSError as exc:
                raise CandidateProcessError("launch", f"候选进程无法启动: {exc}") from exc
            writer = threading.Thread(target=_feed_stdin, args=(proc, payload), daemon=True)
            writer.start()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    kill(proc)
                    proc.wait()
                    raise CandidateProcessError("timeout", f"候选进程超过 {timeout:g}s,已终止")
                out_fh.flush()
                err_fh.flush()
                if os.path.getsize(out_path) > max_stdout or os.path.getsize(err_path) > MAX_STDERR_BYTES:
                    kill(proc)
                    proc.wait()
                    raise CandidateProcessError("output_limit", "候选进程输出超过上限,已终止")
                time.sleep(POLL_SECONDS)
            writer.join(timeout=5)
        # 退出后**两个**文件都要再核对一次:候选可以在两次轮询之间写爆 stderr 然后立刻退出(G8 codex 审查发现、本地复现)
        if os.path.getsize(out_path) > max_stdout or os.path.getsize(err_path) > MAX_STDERR_BYTES:
            raise CandidateProcessError("output_limit", "候选进程输出超过上限")
        raw = out_path.read_bytes()
        if not raw:
            # 没有任何响应:详细 stderr 只留本机,不回传(可能含候选写的任意文字与本机路径)
            tail = _tail(err_path, 1500).decode("utf-8", "replace")
            print("候选进程没有响应,stderr 尾部:\n" + tail, file=sys.stderr)
            raise CandidateProcessError("protocol", f"候选进程没有响应(退出码 {proc.returncode})")
        return _parse_response(raw, n_bars)
    finally:
        if proc is not None and proc.poll() is None:
            kill(proc)
            try:
                proc.wait(timeout=10)                 # 杀了要等到它真的退出,否则 cwd 还被占着,下面的清理会失败
            except subprocess.TimeoutExpired:
                print("警告:候选进程在被杀后 10 秒内仍未退出", file=sys.stderr)
        for path in (runtime, workdir):
            _remove_tree(path)
        for path in (out_path, err_path):
            try:
                path.unlink()
            except OSError:
                pass
