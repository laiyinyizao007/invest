"""候选宿主里的**最小运行期围栏**(G8 终轮复核:S1 的候选在宿主进程里没有任何运行期围栏)。

旧路径里候选执行窗口 `rs.candidate_window()` 会用审计钩子拒起子进程、联网、写目录之外的路径;
新路径把候选搬进了宿主进程,这层围栏随之消失(宿主只依赖标准库,`research_sandbox` 不在它的 sys.path 上)。
这里给宿主装一个**自带的、最小的**审计钩子:只在候选执行窗口内生效,拒起进程 / 联网 / 在临时工作目录之外写或改文件。

**如实说明它不是什么**:不是安全边界。恶意候选能用 ctypes 之类绕过,也能找到宿主的状态字典把它关掉;
真正的限制属于 S4(Job Object / 容器只读挂载 / 网络隔离)。这一层防的是**失控或有 bug 的候选**,
以及把"候选能起进程/联网/写盘"从"默认可以"变成"需要主动绕过"。

探针不依赖 numpy/talib:在一个子进程里把宿主源码 exec 进命名空间、上膛围栏,再逐个尝试危险动作。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "scripts" / "research_candidate_host.py"

HARNESS = textwrap.dedent('''
    import json, os, socket, subprocess, sys
    host_src = open(sys.argv[1], encoding="utf-8").read()
    ns = {"__name__": "host_under_test"}
    exec(compile(host_src, "<host>", "exec"), ns)
    work, outside, moved = sys.argv[2], sys.argv[3], sys.argv[4]
    os.chdir(work)
    ns["_install_fence"]()
    ns["_arm_fence"](work)
    out = {}

    def attempt(label, fn):
        try:
            fn()
            out[label] = "allowed"
        except PermissionError:
            out[label] = "denied"
        except Exception as exc:
            out[label] = "other:" + type(exc).__name__

    attempt("popen", lambda: subprocess.Popen([sys.executable, "-c", "pass"]).wait())
    attempt("system", lambda: os.system("echo hi"))
    attempt("socket_connect", lambda: socket.socket().connect(("127.0.0.1", 9)))
    attempt("write_outside", lambda: open(outside, "w").write("x"))
    attempt("append_outside", lambda: open(outside, "a").write("x"))
    attempt("remove_outside", lambda: os.remove(outside))
    attempt("rename_outside", lambda: os.rename(outside, moved))
    attempt("mkdir_outside", lambda: os.mkdir(outside + ".dir"))
    attempt("write_inside", lambda: open(os.path.join(work, "ok.txt"), "w").write("x"))
    attempt("mkdir_inside", lambda: os.mkdir(os.path.join(work, "sub")))
    attempt("read_outside", lambda: open(sys.argv[1], "rb").read(1))
    ns["_disarm_fence"]()
    attempt("popen_after_disarm", lambda: subprocess.Popen([sys.executable, "-c", "pass"]).wait())
    out["outside_intact"] = os.path.exists(outside) and open(outside).read() == "keep"
    print(json.dumps(out))
''')


@pytest.fixture
def probe(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    done = subprocess.run([sys.executable, "-c", HARNESS, str(HOST), str(work), str(outside), str(tmp_path / "moved.txt")],
                          capture_output=True, text=True, timeout=90)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("action", ["popen", "system", "socket_connect", "write_outside", "append_outside",
                                    "remove_outside", "rename_outside", "mkdir_outside"])
def test_dangerous_actions_are_denied_while_the_fence_is_armed(probe, action):
    assert probe[action] == "denied", probe


@pytest.mark.parametrize("action", ["write_inside", "mkdir_inside", "read_outside", "popen_after_disarm"])
def test_ordinary_actions_still_work(probe, action):
    """放行侧:临时工作目录里照常读写、读目录之外的文件照常、没上膛时(宿主自己的 I/O 阶段)一切照常。"""
    assert probe[action] == "allowed", probe


def test_the_denied_actions_left_the_outside_file_untouched(probe):
    assert probe["outside_intact"] is True


# ---------------------------------------------------------------- 调用点:围栏只在候选执行窗口里上膛,且一定下膛


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse(HOST.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def test_the_fence_is_armed_before_the_candidate_runs_and_disarmed_in_a_finally():
    func = _func("_exec_candidate")
    arm, disarm = _calls(func, "_arm_fence"), _calls(func, "_disarm_fence")
    assert arm and disarm
    first_exec = min(n.lineno for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "exec")
    assert min(c.lineno for c in arm) < first_exec, "围栏必须在 exec 候选源码之前上膛"
    tries = [n for n in ast.walk(func) if isinstance(n, ast.Try)]
    assert any(_calls(ast.Module(body=t.finalbody, type_ignores=[]), "_disarm_fence") for t in tries), "下膛必须在 finally 里"


def test_main_installs_the_fence_before_running_the_candidate():
    main = _func("main")
    install = _calls(main, "_install_fence")
    run = _calls(main, "_exec_candidate")
    assert install and run and min(c.lineno for c in install) < min(c.lineno for c in run)
