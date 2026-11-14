"""第 25 轮(codex 的 Medium 8):宪法历史守卫对新的 deny 名单必须**真的在看**。

之前这两头一凑,守卫对三份名单是空的:
- 提取器不认 `frozenset({...})` 写法 -> `CAPABILITY_MODULE_NAMES` 与
  `FORBIDDEN_MODULE_PATHS` **从来没被解析过**;
- 三份名单压根没登记进注册表 -> "漏登记"的检查也不报。

结果:从危险名单里删掉 "ssl",守卫照样全绿——查不了等于没查,却冒充通过。
这一份按 [[static-ban-rules-need-dynamic-probes]] 的规矩来:每条都有放行+拦截双向探针。
"""
from __future__ import annotations

import pytest

from tools.check_constitution_history import (
    SANDBOX_SETS,
    const_string_set,
    removed_protections,
)


# --------------------------------------------------------------- 提取器:认得 frozenset

def test_the_extractor_reads_a_frozenset_of_strings():
    """`frozenset({...})` 必须能被解析成字符串集合。"""
    source = 'CAPS = frozenset({"ctypes", "subprocess"})\n'

    got = const_string_set(source, ("CAPS",))

    assert got.get("CAPS") == {"ctypes", "subprocess"}


def test_the_extractor_reads_a_frozenset_over_a_tuple():
    source = 'CAPS = frozenset(("ctypes", "subprocess"))\n'

    got = const_string_set(source, ("CAPS",))

    assert got.get("CAPS") == {"ctypes", "subprocess"}


def test_the_extractor_still_refuses_a_form_it_cannot_understand():
    """解析不了就**不返回值**——查不了不能冒充通过。"""
    source = "CAPS = frozenset({name for name in ['ctypes']})\n"

    got = const_string_set(source, ("CAPS",))

    assert "CAPS" not in got


# --------------------------------------------------------------- 注册表:三份名单都得登记

@pytest.mark.parametrize("name", ["DANGEROUS_MODULE_NAMES", "FORBIDDEN_MODULE_PATHS",
                                  "CAPABILITY_MODULE_NAMES"])
def test_the_deny_lists_are_registered_in_the_history_guard(name):
    """三份都是"少一项 = 多放开一点能力"的 deny 名单,必须纳入历史比对。"""
    assert name in SANDBOX_SETS, f"{name} 没登记进历史守卫:删掉它的一项照样全绿"


def test_removing_a_dangerous_module_is_reported():
    """拦截:把 "ssl" 从危险名单里删掉,必须被报出来。"""
    older = 'DANGEROUS_MODULE_NAMES = {"ssl", "httpx"}\n'
    current = 'DANGEROUS_MODULE_NAMES = {"httpx"}\n'

    removed = removed_protections(current, older, ("DANGEROUS_MODULE_NAMES",))

    assert removed.get("DANGEROUS_MODULE_NAMES") == {"ssl"}, (
        "危险名单里少了一项,历史守卫没报出来")


def test_adding_a_dangerous_module_is_not_reported():
    """放行:给危险名单**加**一项是收紧,不算问题。"""
    older = 'DANGEROUS_MODULE_NAMES = {"httpx"}\n'
    current = 'DANGEROUS_MODULE_NAMES = {"httpx", "ssl"}\n'

    removed = removed_protections(current, older, ("DANGEROUS_MODULE_NAMES",))

    assert not removed.get("DANGEROUS_MODULE_NAMES"), "收紧名单被当成了问题"


def test_the_real_sandbox_lists_are_all_parseable():
    """真实源码里的三份名单必须都能被解析出来(解析不了 = 比对是空的)。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "research_sandbox.py").read_text(
        encoding="utf-8")

    got = const_string_set(source, SANDBOX_SETS)

    missing = [name for name in SANDBOX_SETS if name not in got]
    assert not missing, f"这些内置保护常量解析不了,历史比对对它们无效:{missing}"
