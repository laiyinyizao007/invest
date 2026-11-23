#!/usr/bin/env python
"""变异验证脚手架:对 `tools/check_domain.py` 的清单做定点变异,断言每个变异都被抓住。

**为什么要有这个脚本**:ADR 里那句「N 种变异全部被抓住,条数实测」——
如果靠人手记,数字会过期(第 1 轮 codex 复核就发现 ADR 表里三行数字对不上:
测试文件从 82 条长到 90 条,红的用例集自然变了)。本脚本把这件事变成**可复算**的:
每次改清单后跑一遍,数字当场出来。

**三条防「假绿」的要求**(第 3 轮 codex 复核的 Critical-2 与 High-1):
1. **变异必须真的改变判定**:每个变异都带一个「见证路径」(witness),
   变异前后它的**分档结果必须不同**。否则「测试变红」可能只是别的原因,
   而某些变异是**空操作**(例:把 `"scripts/"` 塞进 `RESEARCH_FILES` —— 那个清单用
   精确成员判断,目录前缀语义只在 `RESEARCH_DIRS` 里生效,于是这一行等于
   「把 optimize.py 移出白名单」,与上一行重复)。
2. **`--verify` 必须真的核对**:按**去掉反引号后的文本**匹配 ADR 行,
   并断言**行数齐全**。早期版本拿脚本里的裸标签去匹配 ADR 的 Markdown 行,
   11 个标签**全部匹配不上** → 恒打印「一致」→ 纯假绿灯。
3. **退出码要分清**「有变异没被抓住」(1)与「环境不可用」(2)。

**为什么在副本里做**:变异要改的是**工作树里的文件**。在真工作树上改来改去
风险太大(中途出错就留下半个改坏的清单)。所以先复制一份仓库到临时目录,
在副本上做变异,真工作树全程不动。

用法:
    python tools/check_mutations.py            # 跑全部变异,打印条数
    python tools/check_mutations.py --verify   # 额外核对 ADR 表格里的数字

退出码:
    0 全部变异都被抓住(且 --verify 时 ADR 数字对得上);
    1 有变异没被抓住 / 变异是空操作 / ADR 数字对不上;
    2 环境不可用(fail-closed)。
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CD_REL = "tools/check_domain.py"
TEST_REL = "tests/test_check_domain.py"

# pytest 子进程的超时秒数(**两个脚手架共用这一份**)。
# 第 55 轮之二十(codex 第 2 轮 I1):子解释器有超时,pytest 却没有 ⇒
# pytest 挂死时脚手架会**永久挂起**(不是失败,是不动了)⇒ CI 只能等到
# 自己的 job 超时。超时按**环境不可用**报,fail-loud
# (第 55 轮之二十二:超时用**带外**的哨兵退出码 `TIMEOUT_RETURNCODE`,
# 不用 2 —— pytest 自己的「收集阶段失败」也是 2 ⇒ 用 2 会把两件事混为一谈)。
PYTEST_TIMEOUT_SECONDS = 900

# 变异规格:
#   label    —— 人类可读的说明(也要出现在 ADR 表里,--verify 靠它匹配)
#   rel      —— 要改的文件
#   token    —— 在该文件里定位这一行的**引号内路径 token**(不含注释),
#               这样改注释不会让脚本找不到锚点(第 3 轮的 Low-1)
#   new_line —— 替换成的那一行
#   witness  —— 见证路径:变异前后它的分档结果必须不同(否则这一变异是空操作)
#   before   —— 变异**前**见证路径应有的档位(写进规格,免得测试只能靠猜)
Mutation = tuple[str, str, str, str, str, str]

MUTATIONS: tuple[Mutation, ...] = (
    ("删掉 trading/ 的不可降级条目",
     CD_REL, '"trading/",', '    # (moved by mutation)', "trading/runtime.py", "never_downgrade"),
    ("把 risk/live_admission.py 移出不可降级",
     CD_REL, '"risk/live_admission.py",', '    # (moved by mutation)',
     "risk/live_admission.py", "never_downgrade"),
    ("把 risk/candidate_eval.py 移出不可降级",
     CD_REL, '"risk/candidate_eval.py",', '    # (moved by mutation)',
     "risk/candidate_eval.py", "never_downgrade"),
    ("把 scripts/research_sandbox.py 移出不可降级",
     CD_REL, '"scripts/research_sandbox.py",', '    # (moved by mutation)',
     "scripts/research_sandbox.py", "never_downgrade"),
    ("把 scripts/backtest_lib.py 移出不可降级",
     CD_REL, '"scripts/backtest_lib.py",', '    # (moved by mutation)',
     "scripts/backtest_lib.py", "never_downgrade"),
    ("把 scripts/walk_forward_optimizer.py 移出不可降级",
     CD_REL, '"scripts/walk_forward_optimizer.py",', '    # (moved by mutation)',
     "scripts/walk_forward_optimizer.py", "never_downgrade"),
    ("把 scripts/research_grid.py 移出不可降级",
     CD_REL, '"scripts/research_grid.py",', '    # (moved by mutation)',
     "scripts/research_grid.py", "never_downgrade"),
    ("把 scripts/research_api.py 移出不可降级",
     CD_REL, '"scripts/research_api.py",', '    # (moved by mutation)',
     "scripts/research_api.py", "never_downgrade"),
    ("把 risk/ledger_chain.py 移出不可降级",
     CD_REL, '"risk/ledger_chain.py",', '    # (moved by mutation)',
     "risk/ledger_chain.py", "never_downgrade"),
    ("把 risk/ledger_admin.py 移出不可降级",
     CD_REL, '"risk/ledger_admin.py",', '    # (moved by mutation)',
     "risk/ledger_admin.py", "never_downgrade"),
    ("把 scripts/research_ledger.py 移出不可降级",
     CD_REL, '"scripts/research_ledger.py",', '    # (moved by mutation)',
     "scripts/research_ledger.py", "never_downgrade"),
    ("把 tests/test_fs_capability_closure.py 移出不可降级",
     CD_REL, '"tests/test_fs_capability_closure.py",', '    # (moved by mutation)',
     "tests/test_fs_capability_closure.py", "never_downgrade"),
    ("把 tests/test_research_mcp.py 移出不可降级",
     CD_REL, '"tests/test_research_mcp.py",', '    # (moved by mutation)',
     "tests/test_research_mcp.py", "never_downgrade"),
    # 第 48 轮:分档器自身与 CI 侧「真的跑了吗」闸门这一族。
    # 它们共同决定「一条防线没跑」会不会被发现——把它们降级到研究域,
    # 就等于让"削弱检出能力"的改动免走完整流程。
    ("把 tools/check_domain.py 移出不可降级",
     CD_REL, '"tools/check_domain.py",', '    # (moved by mutation)',
     "tools/check_domain.py", "never_downgrade"),
    ("把 tools/check_ci_ran.py 移出不可降级",
     CD_REL, '"tools/check_ci_ran.py",', '    # (moved by mutation)',
     "tools/check_ci_ran.py", "never_downgrade"),
    ("把 tools/check_guard_mutations.py 移出不可降级",
     CD_REL, '"tools/check_guard_mutations.py",', '    # (moved by mutation)',
     "tools/check_guard_mutations.py", "never_downgrade"),
    ("把 tests/guard_roster.json 移出不可降级",
     CD_REL, '"tests/guard_roster.json",', '    # (moved by mutation)',
     "tests/guard_roster.json", "never_downgrade"),
    ("把 tests/test_check_ci_ran.py 移出不可降级",
     CD_REL, '"tests/test_check_ci_ran.py",', '    # (moved by mutation)',
     "tests/test_check_ci_ran.py", "never_downgrade"),
    ("把 tests/test_check_guard_mutations.py 移出不可降级",
     CD_REL, '"tests/test_check_guard_mutations.py",', '    # (moved by mutation)',
     "tests/test_check_guard_mutations.py", "never_downgrade"),
    # Issue #57:research profile 白名单检查器自己的用例——它钉死的是
    # 「模型看得见的工具边界」这个不变式(降级 = 改断言 = 绕开完整流程)。
    ("把 tests/test_research_profile_check.py 移出不可降级",
     CD_REL, '"tests/test_research_profile_check.py",', '    # (moved by mutation)',
     "tests/test_research_profile_check.py", "never_downgrade"),
    ("把 scripts/optimize.py 移出研究白名单(落回默认拒绝)",
     CD_REL, '"scripts/optimize.py",', '    # (moved by mutation)',
     "scripts/optimize.py", "research"),
    # 这一条必须动 RESEARCH_DIRS(目录前缀语义在那儿):
    # RESEARCH_FILES 用的是**精确成员判断**,往里塞 "scripts/" 是空操作。
    ("把整个 scripts/ 放进研究目录白名单",
     CD_REL, '"evidence/",', '    "scripts/",\n    "evidence/",',
     "scripts/some_brand_new_thing.py", "execution"),
)

# 额外场景:新建一个拥有判定点的文件(未入库)。它不在任何清单里,
# 默认拒绝会让它落进「执行域」——但全仓不变量要求它必须被明确列为不可降级,
# 所以这条场景期望**测试变红**(即探针有反应)。
EXTRA_PROBE_LABEL = "新建一个直接调 check_gate 的文件(不入库)"
EXTRA_PROBE = '''from risk.promotion_gate import check_gate


def go(metrics):
    return check_gate(metrics)
'''


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 设成 utf-8 + errors="replace"。

    **为什么必须做**:Windows 控制台默认 cp1252,打印中文会抛
    `UnicodeEncodeError`——而本仓库的主力开发环境就是 Windows
    (CLAUDE.md §6 把这个脚本写成可跑命令)。
    这是第 1 轮 codex 的 M5 在**新脚本**上的原样复发:同样的缺陷,
    在 `check_domain.py` 里修过,新写的脚本里又带进来一次。
    口径与 `tools/check_domain.py`、`scripts/research_console.py` 一致。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def cleanup_temp_dir(path: Path) -> None:
    """删掉临时副本(第 50 轮:与纪律守卫那套**共用**这一份)。

    此前是 `shutil.rmtree(tmp, ignore_errors=True)`——**忽略错误**等于"删不掉就算了":
    Windows 上文件被占用(索引器/杀软/git)时会**留下仓库副本**在临时目录里,越攒越多。
    现在:重试几次 ⇒ 仍不行就**把路径报出来**(让人能自己删)。
    """
    for attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.5 * (attempt + 1))

    def _force(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        try:
            shutil.rmtree(path, onerror=_force)
        except TypeError:          # Python 3.12+ 把 onerror 改名成 onexc
            shutil.rmtree(path, onexc=lambda f, t, e: _force(f, t, e))
    except OSError as exc:
        print("[提醒] 临时副本没删干净(Windows 上常见:文件被占用):%s(%s)" % (path, exc),
              file=sys.stderr)


# **本脚手架自己的「超时」信号**(第 55 轮之二十二):pytest 的退出码只在 0..5,
# 所以用一个**越界**的哨兵值就不会与 pytest 的任何退出码相撞。
#
# **为什么必须挪到带外**(第 55 轮之二十二):第 55 轮之二十一曾把
# 「退出码 2」直接判成「环境不可用」⇒ 而 pytest 自己的**收集阶段失败**
# (收集到语法错误的文件)**也是退出码 2** ⇒ 「变异把文件改到收集阶段就炸」
# 被**误诊**成「环境不可用」⇒ 第 50 轮定下的口径被顶掉了
# ⇒ `tests/test_check_domain.py` 的两条老用例当场变红(**实测**发现的)。
TIMEOUT_RETURNCODE = -1


def run_outcome(failed: int, returncode: int, output: str = "") -> str:
    """把一次 pytest 的结果分类(第 50 轮):'ok' / 'spec_broken' / 'env_unavailable'。

    **为什么要分这一类**:pytest 的退出码分不出「用例失败」与「收集阶段就炸了」。
    收集阶段炸掉时**一条 FAILED 都没有**(失败数 = 0)⇒ 此前会把它算成
    「探针没抓住」(守卫有洞)。其实那是**变异规格写错了**(把文件改成语法错误),
    两者在「红了」这件事上无法区分 ⇒ 现在分开:spec_broken。

    - 退出码 0 且没有失败 ⇒ ok(绿);
    - 退出码 1 且**有** FAILED ⇒ ok(红,是探针在工作);
    - 退出码 1 但**没有** FAILED ⇒ spec_broken(收集阶段炸了);
    - 退出码 **2** ⇒ **看输出**:
        - 输出里有 `KeyboardInterrupt` ⇒ **env_unavailable**(真·被打断);
        - 否则 ⇒ **spec_broken**(收集阶段失败)。
      **为什么必须看输出**(第 55 轮之二十四,claude 第 1 轮复核 ⑤ +
      **本地实测**):pytest 的 `ExitCode.INTERRUPTED`(= 2)这个**枚举名**
      会让人以为「2 只可能是被打断」⇒ 复核者据此建议「2 一律 env_unavailable」。
      ⇒ **实测**(pytest 8.4.2,见 `%TEMP%/r278_exitcodes.py`):
      收集阶段失败(语法错误 / 导入时抛异常)**也是退出码 2**,
      输出里是 `Interrupted: N error during collection`;
      而 `KeyboardInterrupt` 也是退出码 2,输出里是 `KeyboardInterrupt`。
      ⇒ 两者**只能靠输出文本**分开 ⇒ 若按复核者的建议把 2 一律归
      env_unavailable,「变异把文件改到收集阶段就炸」会被**误诊**成
      「环境不可用」⇒ 变异规格写错了却说成环境坏了 ⇒ **诊断是错的**。
      ⇒ 所以这里**采纳「2 要分开」,不采纳「2 一律 env_unavailable」**;
      另配一条变异见证把这条分界线**钉住**(见 ADR 第 55 轮之二十四)。
    - 退出码 **TIMEOUT_RETURNCODE(-1)** ⇒ **env_unavailable**
      (第 55 轮之二十一,codex 第 2 轮 F5:超时是**环境不可用**,
      **不是**「变异规格写错了」⇒ 两者混为一谈 ⇒ 打印的诊断是**错的**;
      第 55 轮之二十二把超时挪到**带外**的哨兵退出码,免得与 pytest 的 2 相撞);
    - 退出码 ≥ 3 ⇒ spec_broken(用法错误/内部错误)。
    - **退出码 < 0**(第 55 轮之二十三,codex 第 2 轮 Low-2)⇒ env_unavailable:
      POSIX 上 pytest 被信号杀死(OOM-kill 等)时 returncode 是**负数**
      ⇒ 上一版把它归成 spec_broken ⇒ 打印的诊断是**错的**
      (「变异把文件改到收集阶段就报错了」与事实不符)
      ⇒ 实测 `run_outcome(0, -9)` 原先返回 `spec_broken`。
      ⇒ 现在:**任何**负退出码都归 env_unavailable(含超时哨兵 -1)。
    """
    if returncode == TIMEOUT_RETURNCODE:
        return "env_unavailable"
    if returncode < 0:
        # **被信号杀死**(不是本脚手架的超时哨兵)⇒ 仍然是「环境不可用」
        return "env_unavailable"
    if returncode == 2:
        # **退出码 2 有两种含义**(实测,见上面 docstring):
        # 收集阶段失败(spec_broken)与真·被打断(env_unavailable)
        # ⇒ 靠**输出文本**分开。
        if "KeyboardInterrupt" in output:
            return "env_unavailable"
        return "spec_broken"
    if returncode not in (0, 1):
        return "spec_broken"
    if returncode == 1 and failed == 0:
        return "spec_broken"
    return "ok"


def baseline_failure_reason(outcome: str, returncode: int) -> str | None:
    """基线(未变异的副本)有问题时的**诊断**;没问题 ⇒ `None`。

    **为什么要抽成一份**(第 55 轮之二十四,claude 第 1 轮复核 ⑤):
    此前这段判定写在**两个**脚手架的 `main()` 里(各写一份)⇒
    「基线挂死了」这件事**没有任何办法被钉住**⇒ 挂死会被说成「测试失败」
    ⇒ **诊断是错的**(仓库教训:**诊断代码本身也会是死代码 ⇒ 诊断必须自己验**)。
    ⇒ 抽成**一份共享实现** ⇒ 一条用例就能把它钉住
    (`tests/test_check_guard_mutations.py::test_the_baseline_diagnosis_tells_a_hang_apart_from_a_failure`)。

    口径:
      - `env_unavailable`(超时哨兵 / 被信号杀死 / 真·被打断)⇒ **环境不可用**;
      - `spec_broken`(收集阶段就炸)⇒ **规格写错**;
      - `ok` ⇒ `None`(基线没问题)。
    """
    if outcome == "env_unavailable":
        return ("pytest 退出码 %s ⇒ **环境不可用**(超时或被打断),"
                "变异验证无从谈起(这**不是**「测试失败」)" % returncode)
    if outcome == "spec_broken":
        return ("pytest 退出码 %s ⇒ 收集阶段就炸了,变异验证无从谈起:" % returncode)
    return None


def _purge_bytecode_cache(root: Path) -> int:
    """删掉副本里的所有字节码缓存(第 55 轮之十四之四)。返回删掉几个。

    **为什么必须删**:Python 的 `.pyc` 按 (源文件 mtime 的**整秒**, 源文件大小)
    校验新鲜度。本脚手架的变异多是**同大小**的小改动
    (`if X:` → `if False and X:`),若两次变异落在**同一整秒**,
    第二次变异后的源文件与上一次编译出的 `.pyc` 头部**完全吻合**
    ⇒ Python 直接复用**旧字节码** ⇒ 变异**根本没生效**
    ⇒ 见证保持绿 ⇒ 看起来像「守卫有洞」,实际是**变异空转**。

    **pytest 的改写缓存也是同一套机制**(第 55 轮之十四之五,codex 第 2 轮 M1):
    `_pytest/assertion/rewrite.py` 的 `_read_pyc` 比的是
    `int(st.st_mtime)` 与 `size`(写成 `mtime & 0xFFFFFFFF` / `size & 0xFFFFFFFF`),
    `_write_pyc` 写的也是这两个值 ⇒ **与 CPython 的时间戳型 `.pyc` 完全是同一套**
    ⇒ **测试文件一样会中招**。
    **两个版本都核对过**:CI 用的 **8.3.4** 与本机的 **8.4.2** —— 且 8.3.4 的
    `_read_pyc` 见到 `flags != 0` 会直接判「invalid pyc (unsupported flags)」
    ⇒ **pytest 的改写缓存只认时间戳型**,不存在 hash 型那条路。
    (此前本文件写「pytest 的改写缓存是 hash-based ⇒ 只有那 17 条会中招」
    是**事实错误**,已按实测改正。)

    **实测(第 55 轮之十四之四/五)**:触发要**同时**满足两条——
    (a) 变异前后**字节数相同**;(b) 两次落在**同一整秒**。
    当时那 158 条里**31 条**满足 (a)(= **CI 报错那时的集合**;
    29 条在 `tools/check_ci_wiring.py`、1 条在 pytest 用例文件、1 条在 YAML——
    **YAML 不是 Python,没有字节码缓存,天然不中招**)。
    CI 上**恰好 17 条**同时满足 (b) ⇒
    **CI 报错的 17 条全部落在这 31 条之内,且这 31 条之外无一条报错**
    ⇒ 机制被实测坐实(不是推测)。
    **第 55 轮之二十复测(实测,不靠人记)**:纪律那套现在是 **174** 条,
    撞字节数的**只剩 4 条**(`check_ci_ran.py` 1 / `check_ci_wiring.py` 2 /
    `ci_gate.py` 1)——第 55 轮把接线那一族**整族换掉**时,锚点从「等长 token
    改写」变成了「整行替换」⇒ 长度变了 ⇒ Python 必定重编
    ⇒ **那个陷阱在现在的套件里几乎不成立**;但**清缓存这条规则仍然必须留着**
    (它有自己的变异与见证,而且下一轮只要有人再写一条等长改写就会回来)。
    **只在本机的 CI 上犯**:一次变异循环只要 0.3~0.8 秒 ⇒ 常常落进同一整秒;
    本机 Windows 上 pytest 启动慢(>1 秒)⇒ 整秒总是错开 ⇒ 撞不上。

    **两个脚手架共用这一份实现**(仓库教训:同一个问题两个答案 ⇒ 迟早只改一边)。
    """
    removed = 0
    found = 0
    try:
        caches = list(root.rglob("__pycache__"))
    except OSError as exc:            # 目录不可读 ⇒ 不能当成「没有缓存」
        print("[提醒] 遍历字节码缓存目录时出错:%s ⇒ 本次清理**不完整**" % exc,
              file=sys.stderr)
        caches = []
    for cache in caches:
        for pyc in cache.glob("*.pyc"):
            found += 1
            try:
                pyc.unlink()
                removed += 1
            except OSError as exc:    # 删不掉 ⇒ 防线可能**无声失效** ⇒ 必须出声
                print("[提醒] 删不掉字节码缓存:%s(%s)⇒ 本轮可能仍被陈旧字节码影响"
                      % (pyc, exc), file=sys.stderr)
    if found != removed:
        print("[提醒] 找到 %d 个字节码缓存,只删掉 %d 个 ⇒ 清理不完整"
              % (found, removed), file=sys.stderr)
    return removed


def _run_tests(cwd: Path) -> tuple[int, str, int]:
    """跑本文件的测试,返回 (FAILED 行数, 完整输出)。

    **必须显式 encoding="utf-8"**:子进程被 PYTHONUTF8=1 逼着吐 UTF-8,
    而 `text=True` 默认按 locale(cp1252)解码 —— 解码失败时 stdout 变 None,
    紧接着 `.splitlines()` 就崩(第 3 轮的 Critical-1 第二处)。

    **关于「subprocess 实参不是静态字符串」**:这里的实参全部来自**本地常量**
    (`sys.executable` + 固定的测试路径),且**不经 shell**(`shell=False` 是默认),
    外部输入进不来。`_tier_of` 里的路径经 `%r` 转成 Python 字面量后作为 `-c`
    的参数传入,同样不经 shell。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # **不能带着 `PYTHONDONTWRITEBYTECODE` 跑**(第 55 轮之十四之五,codex L3):
    # 一旦这个变量(或 `-B`)在环境里,解释器**根本不写 `.pyc`**
    # ⇒ 拦截侧探针的「第一次跑会写出 `.pyc`」前提不成立 ⇒ 探针假红
    # ⇒ 把「环境配置」误报成「变异被吃」。这里**显式摘掉**它。
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    # **也要摘掉 `PYTHONPYCACHEPREFIX`**(第 55 轮之二十,codex 第 2 轮 M1):
    # 这个变量让解释器把 `.pyc` **写到前缀目录**去 ⇒ 副本里压根没有 `__pycache__`
    # ⇒ `_purge_bytecode_cache` 删 0 个**也不报错** ⇒「同秒 + 同大小」的变异
    # 照样被陈旧字节码吃掉 ⇒ 脚手架会得出「守卫有洞」的错误结论
    # —— 正是这套脚手架要根治的故障,换了个环境开关就原样回归。
    # 与 `PYTHONDONTWRITEBYTECODE` 同类处置:**显式摘掉**,并**出声**(不许无声)。
    if env.pop("PYTHONPYCACHEPREFIX", None) is not None:
        print("[提醒] 环境里有 `PYTHONPYCACHEPREFIX` ⇒ 已摘掉:否则字节码缓存写到"
              "副本外 ⇒ 清缓存清不到它 ⇒ 同秒 + 同大小的变异会被陈旧字节码吃掉",
              file=sys.stderr)
    # **先清字节码缓存**(第 55 轮之十四之四):否则「同秒 + 同大小」的变异
    # 会被**陈旧的 .pyc** 吃掉 ⇒ 变异空转 ⇒ 见证保持绿(假绿)。
    _purge_bytecode_cache(cwd)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", TEST_REL, "-q", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=str(cwd), env=env,
            encoding="utf-8", errors="replace",
            # **pytest 子进程也要有超时**(第 55 轮之二十,codex 第 2 轮 I1):
            # 挂死的 pytest 会让整个脚手架永久挂起(连失败都不是,只是不动了)。
            # 超时按**环境不可用**报(哨兵退出码 `TIMEOUT_RETURNCODE`),
            # fail-loud,不许装作「没抓住」。
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print("[FAIL] pytest 超过 %d 秒还没结束 ⇒ 环境不可用(挂死),变异验证无从谈起"
              % PYTEST_TIMEOUT_SECONDS, file=sys.stderr)
        # **哨兵退出码**(第 55 轮之二十二):不是 2 —— pytest 的「收集阶段失败」
        # 也是 2 ⇒ 用 2 会把「规格写错」误诊成「环境不可用」。
        return 0, "", TIMEOUT_RETURNCODE
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    # **退出码也要返回**(第 50 轮):此前只看 FAILED 行数 ⇒
    # pytest 在**收集阶段**就炸时(一条 FAILED 都没有)会被当成"基线全绿"。
    return len(failed), proc.stdout, proc.returncode


def copy_repo(dest: Path) -> None:
    """复制一份够用的仓库副本。

    **必须建一个 git 仓库**:测试里有几条探针是走 git 的
    (`git ls-files` / `git status --porcelain -uall`),
    没有 .git 时它们会直接报错——那不是探针在工作,是环境坏了,
    两者在「红了」这件事上无法区分。所以这里 git init + 全量 add + commit。
    """
    for rel in ("tools", "tests", "risk", "scripts", "config", "docs", "strategies",
                "trading", "ctp_tools", "notify", "dashboard", "evidence",
                "pending_strategies", "pyproject.toml",
                # `.github/` 也要复制(第 52 轮):守卫里有探针**读**这个 workflow
                # (接线守卫)。缺了它,那条守卫会**因为读不到文件而报错**——
                # 看起来像「变异被抓住」,其实是环境坏了(假绿)。
                # 此前只有纪律守卫那套脚手架复制了它,分档这套没有 ⇒ 两套口径不一致。
                ".github",
                # 清单里点名的这些也必须复制过去,否则存在性探针会误报
                "Dockerfile", "docker-compose.yml", "docker-compose.research.yml",
                "requirements.txt", "requirements-research.txt", "requirements-dashboard.txt",
                "CLAUDE.md", "CHANGELOG.md", "PROJECTWIKI.md", "README.md",
                # .gitignore 必须一起复制:目录前缀的「是 gitignore 的运行时目录吗」
                # 这条判据靠它(缺了它,pending_strategies/ 就既不存在也不被忽略)
                ".gitignore"):
        src = ROOT / rel
        if not src.exists():
            continue
        dst = dest / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(cmd, cwd=str(dest), check=True, env=env,
                       capture_output=True)


def _apply(repo: Path, rel: str, token: str, new_line: str) -> str:
    """把含 `token` 的那一行替换成 `new_line`;返回 'ok' 或错误说明。

    定位靠**引号内的路径 token**,不靠整行相等——所以改行尾注释不会让脚本
    找不到锚点(第 3 轮的 Low-1:早期版本整行匹配,注释改一个字就 CI 变红,
    报错信息还 cryptic)。

    **行尾会被归一化成 LF**(第 55 轮之二十,codex 第 2 轮 I2 的如实说明):
    本函数按 `\\n` 切分再写回 ⇒ 副本里的 CRLF 文件一律变成 LF ⇒
    **文件大小会变小**。后果:「同秒 + 同大小」这个陷阱在这套脚手架里
    **几乎不成立**(长度必然变了 ⇒ Python 会重编)⇒ 这里的清缓存是**冗余保险**,
    不是主力。**纪律那套**( `tools/check_guard_mutations.py::_apply` )**逐字节保
    真行尾** ⇒ 「同字节数」的统计(现测 4 条)**只对纪律那套成立**,别照着错误前提推演。
    """
    path = repo / rel
    if not path.exists():
        return f"文件不存在:{rel}"
    lines = path.read_text(encoding="utf-8").split("\n")
    hits = [i for i, line in enumerate(lines) if token in line]
    if len(hits) != 1:
        return f"锚点 {token!r} 命中 {len(hits)} 次(应为 1)"
    lines[hits[0]] = new_line
    path.write_text("\n".join(lines), encoding="utf-8")
    return "ok"


def _tier_of(repo: Path, rel: str) -> str:
    """问副本里的分档器:这个路径现在判成什么档。

    **必须先清字节码缓存**(第 55 轮之十四之四):这个判定是**靠 import**
    得到的 ⇒ Python 若发现 `.pyc` 头记的 (整秒 mtime, size) 与源文件
    **一致**,就直接复用**旧字节码** ⇒ 若变异前后的这两个值相同,
    `after` 会读到**变异前**的判定 ⇒ 被误报成「空操作变异」(退出码 1)。
    现有 22 条变异碰巧都改了文件长度(size 不同 ⇒ 会重编),
    但那是**运气**,不是保证 ⇒ 这里把缓存清掉,让判定**只**取决于源文件。
    """
    _purge_bytecode_cache(repo)
    code = (
        "import sys; sys.path.insert(0, 'tools'); "
        "import check_domain as cd; print(cd.classify(%r)[0])" % rel
    )
    # **同样要摘掉 `PYTHONPYCACHEPREFIX`**(第 55 轮之二十,codex 第 2 轮 M1):
    # 这个判定是**靠 import** 得到的 ⇒ 解释器若把 `.pyc` 写到前缀目录,
    # 副本里的 `_purge_bytecode_cache` **清不到它** ⇒ 复用的仍是旧字节码
    # ⇒ 变异前后的判定可能读的是**同一份**旧代码 ⇒ 空操作变异被误报。
    env = {**os.environ, "PYTHONUTF8": "1"}
    env.pop("PYTHONPYCACHEPREFIX", None)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(repo), env=env,
                          encoding="utf-8", errors="replace")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="变异验证:每个清单改动都必须被抓住")
    parser.add_argument("--verify", action="store_true",
                        help="额外核对 ADR 表格里的数字与实测是否一致")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="domain_mutations_"))
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    try:
        copy_repo(repo)

        baseline, out, base_rc = _run_tests(repo)
        # **基线也要分三类**(第 55 轮之二十四,claude 第 1 轮复核 ⑤):
        # 超时(哨兵退出码 `TIMEOUT_RETURNCODE`)、收集阶段失败、真·被打断
        # 三者的**诊断不一样** ⇒ 上一版一句「基线就有测试失败」把它们**混成一句**
        # ⇒ 挂死的 pytest 会被说成「测试失败」⇒ **诊断是错的**。
        base_outcome = run_outcome(baseline, base_rc, out)
        # **基线的诊断只有一份实现**(第 55 轮之二十四):此前这段判定在
        # **两个**脚手架里各写一份 ⇒ 「基线挂死了」没有任何办法被钉住
        # ⇒ 挂死会被说成「测试失败」⇒ **诊断是错的**。
        reason = baseline_failure_reason(base_outcome, base_rc)
        if reason:
            print("基线(未变异的副本):" + reason, file=sys.stderr)
            print(out[-1500:], file=sys.stderr)
            return 2
        if baseline != 0:
            print("基线就有测试失败,变异验证无从谈起:", file=sys.stderr)
            print(out[-1500:], file=sys.stderr)
            return 2
        print("基线(未变异):全绿")
        print()

        results: list[tuple[str, int]] = []
        for label, rel, token, new_line, witness, expected_before in MUTATIONS:
            orig = (repo / rel).read_text(encoding="utf-8")
            before = _tier_of(repo, witness)
            if before != expected_before:
                print(f"  [跳过] {label}:见证路径 {witness} 现在的档位是 {before},"
                      f"规格写的是 {expected_before}(规格与仓库现状对不上)", file=sys.stderr)
                return 2
            msg = _apply(repo, rel, token, new_line)
            if msg != "ok":
                (repo / rel).write_text(orig, encoding="utf-8")
                print(f"  [跳过] {label}:{msg}", file=sys.stderr)
                return 2
            after = _tier_of(repo, witness)
            n, out, rc = _run_tests(repo)
            (repo / rel).write_text(orig, encoding="utf-8")

            outcome = run_outcome(n, rc, out)
            if outcome == "env_unavailable":
                # **环境不可用**(超时/中断)—— 与「变异规格写错」**不是一回事**
                # (第 55 轮之二十一,codex 第 2 轮 F5)⇒ 分开报,fail-closed。
                print(f"  [跳过] {label}:pytest 退出码 {rc} ⇒ **环境不可用**"
                      f"(超时或被中断)⇒ 变异验证无从谈起"
                      f"(这**不是**「变异规格写错了」)", file=sys.stderr)
                return 2
            if outcome == "spec_broken":
                # **收集阶段就炸**:pytest 退出码非 0 且一条 FAILED 都没有
                # ⇒ 不是"探针没抓住"(那是守卫有洞),而是**变异规格写错了**
                # ⇒ 退出码 2(fail-closed,与纪律守卫那套同一口径)。
                print(f"  [跳过] {label}:pytest 退出码 {rc} 且没有 FAILED ⇒ "
                      f"变异把文件改到收集阶段就报错了(变异规格写错了)", file=sys.stderr)
                return 2

            if before == after:
                # **空操作变异**:判定结果没变 → 这一行等于没测。
                # (第 3 轮的 High-1:ADR 表里有一行把 `"scripts/"` 塞进
                #  RESEARCH_FILES,而那个清单用精确成员判断 → 整行是空操作)
                print(f"  [!! 空操作] {label}:见证路径 {witness} 的分档前后都是 {before}")
                return 1
            mark = "OK " if n > 0 else "!! "
            print(f"  [{mark}{n:2d} 条红] {label}"
                  f"(见证 {witness}:{before} → {after})")
            results.append((label, n))

        # 额外场景:新建一个拥有判定点的文件(未入库)
        new_file = repo / "risk" / "zz_new_gate_probe.py"
        new_file.write_text(EXTRA_PROBE, encoding="utf-8")
        n, out, rc = _run_tests(repo)
        new_file.unlink()
        if run_outcome(n, rc, out) == "env_unavailable":
            print(f"  [跳过] {EXTRA_PROBE_LABEL}:pytest 退出码 {rc} ⇒ "
                  f"**环境不可用**(超时或被中断)⇒ 探针场景无从谈起", file=sys.stderr)
            return 2
        if run_outcome(n, rc, out) == "spec_broken":
            print(f"  [跳过] {EXTRA_PROBE_LABEL}:pytest 退出码 {rc} 且没有 FAILED ⇒ "
                  f"探针场景本身有问题(环境不可用)", file=sys.stderr)
            return 2
        results.append((EXTRA_PROBE_LABEL, n))
        print(f"  [{'OK ' if n > 0 else '!! '}{n:2d} 条红] {EXTRA_PROBE_LABEL}")

        uncaught = [label for label, n in results if n == 0]
        print()
        if uncaught:
            print("以下变异**没有被抓住**(探针有洞):", file=sys.stderr)
            for label in uncaught:
                print(f"  - {label}", file=sys.stderr)
            return 1
        print(f"{len(results)} 种变异全部被抓住。")

        if args.verify:
            return _verify_adr(results)
        return 0
    finally:
        cleanup_temp_dir(tmp)


# --------------------------------------------------------------------------
# ADR 变异表的核对(两个脚本**共用**这一份口径)
# --------------------------------------------------------------------------

def normalize_cell(cell: str) -> str:
    """**逐格**归一化:去掉反引号与成对的 `**`。

    **不去掉单个 `*`**——`except*` 里那个星号是**语法的一部分**,
    去掉它两侧就再也对不上(第 48 轮实测:全局去星号 ⇒ `except*` 被削成 `except`,
    于是标签只能改写成不含星号的说法 ⇒ **掩盖了归一化的缺陷**,
    还把它固化成了「标签里不许有星号」这条守卫——codex 第 2 轮 M1)。

    同理**不能**去掉下划线:`risk/live_admission.py` 这类路径里有它,
    去掉就把路径改了。
    """
    return cell.replace("`", "").replace("**", "").strip()


def table_rows_in_section(adr_text: str, section_anchor: str) -> list[str]:
    """取出某一节里所有表格的**第一格**(跳过表头与分隔行)。

    **只看那一节**(此前扫的是整份 ADR ⇒ 别的表里的一行也可能顶替本轮标签,
    匹配过松);**跳过表头**(markdown 表格的第一行是表头,不是变异)。
    """
    lines = adr_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") and section_anchor in line:
            start = index
            break
    if start is None:
        return []
    first_cells: list[str] = []
    group: list[list[str]] = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith("#"):
            break
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and not all(set(cell) <= set("-: ") for cell in cells):
                group.append(cells)
            continue
        if group:                      # 表格结束 ⇒ 结算这一组
            first_cells.extend(cell[0] for cell in group[1:] if cell)
            group = []
    if group:
        first_cells.extend(cell[0] for cell in group[1:] if cell)
    return first_cells


def verify_mutation_table(labels, adr_path: Path, section_anchor: str) -> int:
    """核对 ADR 里那张变异表与脚本的变异集合(**双向**)。

    **双向**(codex 第 2 轮 H2):此前只查「脚本里的每一条都在表里」,
    ADR **多写**一行(文档夸大)没人发现 ⇒ 现在两个方向都查,
    并打印**两边各多少行**。

    只核对**集合**,不核对「每种变异红了几条」——那个数字随用例数变化,
    写进文档必然过期。

    退出码:0 一致 / 1 不一致 / 2 环境不可用(ADR 读不到、锚点找不到)。
    """
    if not adr_path.exists():
        print("[FAIL] ADR 不存在:%s" % adr_path, file=sys.stderr)
        return 2
    adr_text = adr_path.read_text(encoding="utf-8")
    cells = table_rows_in_section(adr_text, section_anchor)
    if not cells:
        print("[FAIL] ADR 里找不到「%s」那一节(或那一节里没有表格):%s"
              % (section_anchor, adr_path), file=sys.stderr)
        return 2
    listed = {normalize_cell(cell) for cell in cells}
    wanted = {normalize_cell(label) for label in labels}
    bad: list[str] = []
    for label in sorted(wanted - listed):
        bad.append("脚本里有、ADR 表里没有:「%s」" % label)
    for cell in sorted(listed - wanted):
        bad.append("ADR 表里有、脚本里没有:「%s」(文档多写/陈旧 ⇒ 会夸大)" % cell)
    if bad:
        print("ADR 里的变异表与脚本不一致:", file=sys.stderr)
        for line in bad:
            print("  - %s" % line, file=sys.stderr)
        print("  (ADR 表 %d 行 vs 脚本 %d 条)" % (len(listed), len(wanted)), file=sys.stderr)
        return 1
    print("ADR 里列出的变异集合与脚本一致(两边各 %d 行,逐条对上)。" % len(wanted))
    return 0


def _verify_adr(results: list[tuple[str, int]], adr_path: Path | None = None) -> int:
    """核对 ADR 里的**变异集合**与实测是否一致(第 49 轮:口径搬到共用函数)。

    **双向**(script → ADR 与 ADR → script 两个方向都查)、
    **只看那一节**的表格、**逐格**归一化(不去掉单个 `*`)——
    见 `verify_mutation_table` 的说明(第 49 轮 codex H2/M1)。
    """
    adr = adr_path or (ROOT / "docs" / "adr" / "20261114-consequence-domain-tiers.md")
    labels = [label for label, _n in results]
    return verify_mutation_table(labels, adr, "探针的变异验证")


if __name__ == "__main__":
    sys.exit(main())
