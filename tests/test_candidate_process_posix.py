"""候选宿主随父进程消失而消失(POSIX / 容器部署路径;G8 codex 复核的 M1)。

第一条真的杀进程,只在 Linux 跑(Windows 跳过);第二条只看调用点(AST),哪里都能跑。

背景:`research_api.kill_tree` 在 POSIX 只 `proc.kill()` 框架子进程;而候选宿主为了能被启动器整组杀掉,
是自己会话的组长(`start_new_session=True`),不在框架子进程的进程组里。于是外层超时杀掉框架子进程时,
正在跑的候选宿主**不会被杀**——候选若是死循环就永久占 CPU。Windows 上 `taskkill /T` 恰好覆盖到整棵树,
所以这条只有 Linux(也就是容器里)才会出现。

解法:宿主启动时用 `prctl(PR_SET_PDEATHSIG, SIGKILL)` 让内核在父进程死亡时杀掉它。
这条测试**不需要 numpy/talib**,研究侧 CI(Linux)能真跑;Windows 上跳过。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "scripts" / "research_candidate_host.py"

requires_linux = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                    reason="PR_SET_PDEATHSIG 只有 Linux 有;Windows 靠 taskkill /T 杀整棵树")


def _is_dead(pid: int) -> bool:
    """进程已不存在,或只剩僵尸(容器里 PID 1 不一定回收孤儿)。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def _wait_dead(pid: int, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _is_dead(pid):
            return True
        time.sleep(0.1)
    return _is_dead(pid)


# 「框架子进程」:起一个按**宿主源码**初始化的孙进程(自己会话组长,与启动器同形),打印孙进程 pid,然后睡
PARENT = textwrap.dedent('''
    import subprocess, sys, time
    host_src = open(sys.argv[1], encoding="utf-8").read()
    child_code = (
        "ns = {'__name__': 'host_under_test'}\\n"
        "exec(compile(" + repr(host_src) + ", '<host>', 'exec'), ns)\\n"
        "fn = ns.get('_die_with_parent')\\n"
        "fn and fn()\\n"
        "import time\\n"
        "while True:\\n"
        "    time.sleep(0.5)\\n"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code], start_new_session=True)
    print(child.pid, flush=True)
    time.sleep(120)
''')


@requires_linux
def test_host_dies_when_its_parent_is_killed_even_though_it_leads_its_own_session():
    parent = subprocess.Popen([sys.executable, "-c", PARENT, str(HOST)], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline().strip())
        time.sleep(0.8)                                   # 让孙进程跑到 `_die_with_parent()` 之后
        assert not _is_dead(child_pid), "探针前提:孙进程应当还活着"
        os.kill(parent.pid, signal.SIGKILL)               # 模拟 research_api.kill_tree 的 proc.kill()
        parent.wait(timeout=10)
        assert _wait_dead(child_pid), "框架子进程被杀后,候选宿主成了孤儿(会永久占 CPU)"
    finally:
        if parent.poll() is None:
            parent.kill()
        if child_pid is not None and not _is_dead(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_the_host_calls_it_before_reading_anything_from_stdin():
    """调用点也要钉住:`_die_with_parent()` 在 main() 里先于读 stdin / 载入候选之前。"""
    import ast
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_die_with_parent" in calls
    first_read = next(i for i, n in enumerate(ast.walk(main)) if isinstance(n, ast.Name) and n.id == "_load_runtime")
    first_die = next(i for i, n in enumerate(ast.walk(main)) if isinstance(n, ast.Name) and n.id == "_die_with_parent")
    assert first_die < first_read


CHILD_CODE = textwrap.dedent('''
    import sys, time
    host_src = open(sys.argv[1], encoding="utf-8").read()
    ns = {"__name__": "host_under_test"}
    exec(compile(host_src, "<host>", "exec"), ns)
    ns["_die_with_parent"]()
    print("alive", flush=True)
    while True:
        time.sleep(0.2)
''')


@requires_linux
def test_host_exits_at_once_when_its_parent_was_already_dead_before_prctl():
    """竞态:父进程在 prctl 之前就死了,内核不会再发死亡信号。启动器把自己的 pid 经 CANDIDATE_PARENT_PID 告诉宿主,
    宿主 prctl 之后发现 getppid() 对不上就自己退出(G8 终轮复核指出的窗口:解释器启动与模块导入期间)。"""
    env = dict(os.environ, CANDIDATE_PARENT_PID="999999")        # 与真实父进程(本测试进程)对不上
    child = subprocess.Popen([sys.executable, "-c", CHILD_CODE, str(HOST)], env=env, stdout=subprocess.PIPE, text=True)
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        pytest.fail("父 pid 对不上,宿主却没有退出")
    assert child.returncode == 1


@requires_linux
def test_host_keeps_running_when_the_expected_parent_pid_matches():
    """放行侧:告知的父 pid 与真实父进程一致时,宿主照常活着。"""
    env = dict(os.environ, CANDIDATE_PARENT_PID=str(os.getpid()))
    child = subprocess.Popen([sys.executable, "-c", CHILD_CODE, str(HOST)], env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "alive"
        time.sleep(0.5)
        assert child.poll() is None
    finally:
        child.kill()
