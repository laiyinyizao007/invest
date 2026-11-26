"""候选进程的宿主脚本:候选代码**只**在这里跑(不在框架进程里)。

由父进程这样起(ADR `20261115-candidate-process-isolation.md` 第 4.3 节):

    python -E -P -B -c "<本文件的源码文本>"

**必须按源码文本(`-c`)起,不能按路径起**:按路径起时,候选能从 `sys.orig_argv` 与自己调用栈里
宿主帧的 `co_filename` 读到本文件的绝对路径,进而推出仓库根(A2 动态探针实测抓到,
见 `tests/test_candidate_process_isolation.py::test_a2_*`)。`-c` 之下两处都只剩 `<string>` / 源码文本。

- `-E`:忽略 `PYTHONPATH` / `PYTHONHOME` / `PYTHONIOENCODING` 等环境注入(实测:`-E` 下
  `PYTHONIOENCODING=utf-8` 无效,所以本脚本自己把 stdout/stderr 重配成 utf-8);
- `-P`:**不把脚本所在目录(仓库的 `scripts/`)与当前目录加进 `sys.path`**
  ⇒ `import research_grid` / `import risk` / `import scripts.backtest_lib` 必然 ImportError(验收标准 A1);
- `-B`:不写 `.pyc`。

**`-I` 不能用**:它会连用户 site-packages 一起去掉,`talib` 就没了(本机实测)。

候选侧运行时包(`research/candidate/`)由父进程复制到一个**临时目录**,路径经环境变量
`CANDIDATE_RUNTIME_DIR` 交给本脚本 ⇒ 候选进程的 `sys.path` 里**没有任何仓库路径**。
候选源码与参数放在同一临时目录的 `request.json` 里(ADR 第 4.3 节第 2 点允许"临时目录下的只读副本");
**行情切片走 stdin 的长度前缀二进制帧**。

本脚本只依赖标准库(候选侧运行时包另外依赖 numpy + ta-lib)。
"""
from __future__ import annotations

import json
import os
import random
import sys
import traceback
from pathlib import Path

RUNTIME_ENV = "CANDIDATE_RUNTIME_DIR"
REQUEST_FILE = "request.json"


def _scrub_host_paths() -> None:
    """抹掉宿主脚本自己的仓库路径。

    没有这一步,候选可以从 `__main__.__file__` / `sys.argv[0]` 推出仓库根,
    然后 `open()` 到仓库里的行情 CSV 与账本 —— 隔离就只剩"礼貌"了。
    """
    sys.argv = ["<candidate-host>"]
    main_module = sys.modules.get("__main__")
    if main_module is not None:
        try:
            main_module.__file__ = "<candidate-host>"
        except Exception:                                    # noqa: BLE001 - 抹不掉也不能因此崩
            pass


def _reconfigure_stdio() -> None:
    """`-E` 会忽略 `PYTHONIOENCODING`,所以 stdout/stderr 的编码得自己钉成 utf-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                    # noqa: BLE001 - 不是 TextIOWrapper 就跳过
            pass


def _die_with_parent() -> None:
    """Linux:父进程(框架子进程)一死,内核就杀掉本进程(`prctl(PR_SET_PDEATHSIG, SIGKILL)`)。

    为什么需要:启动器让宿主自己当会话组长(`start_new_session=True`),好整组杀;代价是外层 `kill_tree`
    在 POSIX 只 `proc.kill()` 框架子进程时,宿主不在它的进程组里,会成孤儿——候选若是死循环就永久占 CPU。
    容器(Linux)正是这条路径;Windows 上 `taskkill /T` 杀整棵树,不需要。
    **不是安全边界**:恶意候选自己也能 prctl 掉它(OS 级限制属 S4);这里防的是失控/有 bug 的候选。"""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, 9, 0, 0, 0)        # PR_SET_PDEATHSIG = 1, SIGKILL = 9
    except Exception:                                    # noqa: BLE001 - 设不上也不能因此起不来
        pass
    # 竞态:父进程若在 prctl **之前**就死了(解释器启动、模块导入期间),内核不会再发死亡信号,宿主就成了孤儿。
    # 启动器经环境变量告知自己的 pid;prctl 之后发现真实的父进程不是它,就自己退出。
    # 只在 Linux:Windows 上 venv 的 python.exe 是启动器,真实 python 的父进程是那个壳,pid 对不上是正常的。
    expected = os.environ.get("CANDIDATE_PARENT_PID", "")
    if expected.isdigit() and os.getppid() != int(expected):
        os._exit(1)


def _write_raw_frame(payload: bytes) -> None:
    """协议模块还没能用时的兜底写帧(与 `candidate.protocol.encode_frame` 同一算法)。"""
    sys.stdout.buffer.write(len(payload).to_bytes(4, "big") + payload)
    sys.stdout.buffer.flush()


def _write_raw_error(message: str) -> None:
    body = json.dumps({"magic": "CANDIDATE_SIGNALS_V1", "status": "error", "error": message[:500]},
                      ensure_ascii=False).encode("utf-8")
    try:
        _write_raw_frame(body)
    except Exception:                                        # noqa: BLE001 - 连兜底都失败就没办法了
        pass


def _load_runtime() -> Path:
    raw = os.environ.get(RUNTIME_ENV)
    if not raw:
        raise RuntimeError(f"缺少环境变量 {RUNTIME_ENV}(父进程没有把候选运行时包交过来)")
    runtime_dir = Path(raw)
    if not (runtime_dir / "candidate" / "protocol.py").is_file():
        raise RuntimeError("候选运行时包不完整(缺 candidate/protocol.py)")
    return runtime_dir


def _exec_candidate(source: str, class_name: str, params: dict, bars: list):
    """在**本进程**里执行候选源码,返回它的信号序列。

    这里没有任何框架对象可碰:仓库不在 `sys.path` 上,风控代码不在本进程里。
    候选的 `print` 被重定向到 stderr,免得污染 stdout 上的协议帧。
    """
    import types

    from candidate.protocol import pack_signals

    module = types.ModuleType("candidate_code")
    module.__dict__["__file__"] = "<candidate-source>"
    module.__dict__["__name__"] = "candidate_code"

    stdout = sys.stdout
    try:
        sys.stdout = sys.stderr                     # 候选的 print 走 stderr,协议通道保持干净
        code = compile(source, "<candidate-source>", "exec")
        exec(code, module.__dict__)                 # noqa: S102 - 这里就是"执行候选"的本意
        cls = getattr(module, class_name, None)
        if cls is None:
            raise RuntimeError(f"候选源码里没有类 {class_name}")
        instance = cls(params)
        signals = instance.run(bars)
    finally:
        sys.stdout = stdout

    payload = pack_signals(signals)
    return payload


def main() -> int:
    _die_with_parent()
    _scrub_host_paths()
    _reconfigure_stdio()
    random.seed(0)                                  # 确定性:候选用了未固定的随机数时仍可复现
    try:
        import numpy as np
        np.random.seed(0)
    except Exception:                                # noqa: BLE001 - numpy 不在就不设种子
        pass

    try:
        runtime_dir = _load_runtime()
    except Exception as exc:                        # noqa: BLE001
        _write_raw_error(f"候选运行时不可用: {exc}")
        return 0

    sys.path.insert(0, str(runtime_dir))
    try:
        from candidate.protocol import (MAGIC_RESPONSE, encode_json, read_frame,
                                        unpack_bars, write_frame)
    except Exception as exc:                        # noqa: BLE001
        _write_raw_error(f"候选协议模块不可用: {exc}")
        return 0

    try:
        request = json.loads((runtime_dir / REQUEST_FILE).read_text(encoding="utf-8"))
        class_name = request["class"]
        params = request.get("params") or {}
        interval = request.get("interval") or ""
        n_bars = int(request["n_bars"])
        source = request["source"]

        payload = read_frame(sys.stdin.buffer)
        bars = unpack_bars(payload, n_bars, interval)
        if len(bars) != n_bars:
            raise RuntimeError(f"K 线根数不符(期望 {n_bars},实得 {len(bars)})")

        signals_payload = _exec_candidate(source, class_name, params, bars)

        write_frame(sys.stdout.buffer, encode_json({
            "magic": MAGIC_RESPONSE, "status": "ok", "n_bars": n_bars, "error": "",
        }))
        write_frame(sys.stdout.buffer, signals_payload)
    except SystemExit as exc:                       # 候选自己 exit():按失败处理
        _write_raw_error(f"候选进程自行退出: {exc}")
        return 0
    except BaseException as exc:                    # noqa: BLE001 - 候选的任何异常都要变成结构化失败
        detail = "".join(ch for ch in str(exc) if ch.isprintable())[:400]
        traceback.print_exc(file=sys.stderr)
        _write_raw_error(f"{type(exc).__name__}: {detail}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
