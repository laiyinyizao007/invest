"""闸门函数名的「任何写入形态都要拒」——本分支(#23)并入 main 时补的动态探针。

背景(为什么这条要单独测):
main 在 PR #34 里加了 `BUILTIN_PROTECTED_GATE_ATTRS` 与 `_gate_attr_violations()`:
**闸门函数**(`screen_source` / `check_class` / `check_protected_state` / …)是
「拒绝」这件事的执行者,改写它们 = 把筛查换成一个听话的版本,于是
「静态筛查: 通过」「没有改写受保护对象」这两个给人看的信号都能变成谎言。

但 main 那边**没有**为这七种写入形态配探针(仓库规矩:每条新规则都要有
放行+拦截双向探针,静态推理必漏)。本文件补上:

- 拦截侧:七种形态(赋值/带注解赋值/复合赋值/元组解包/del/setattr/delattr/
  vars()[...])逐一被拒,且**断言是哪条规则拒的**(不然测试只证明「有拒绝」,
  不证明「拒对了东西」——把规则名删掉这条测试也得绿);
- 放行侧:普通候选代码**不**被误伤(否则这条规则会把诚实候选整片拒掉,
  进化循环因此被闲置——第 4 轮 G8 复核的 F2 就是这么发生的)。

覆盖方式:直接喂给 `screen_source`,不依赖任何候选模板。
"""
from __future__ import annotations

import pytest

import risk.constitution as const


GATE = "screen_source"


# --------------------------------------------------------------- 拦截侧:七种写入形态

@pytest.mark.parametrize(
    "source, expected",
    [
        ("x.screen_source = 1", "改写了闸门函数"),
        ("x.screen_source: int = 1", "改写了闸门函数"),
        ("x.screen_source += 1", "改写了闸门函数"),
        ("a, x.screen_source = v", "改写了闸门函数"),
        ("del x.screen_source", "删除了闸门函数"),
        ('setattr(o, "screen_source", 1)', "按名改写了闸门函数"),
        ('delattr(o, "screen_source")', "按名改写了闸门函数"),
        ('vars(o)["screen_source"] = 1', "通过 vars() 改写了闸门函数"),
    ],
    ids=["assign", "annassign", "augassign", "tuple-unpack", "del", "setattr", "delattr", "vars-item"],
)
def test_gate_attr_writes_are_denied_in_every_form(source, expected):
    """七种写入形态逐一被拒,且要报出**是哪一类**拒绝。"""
    findings = const.screen_source(source)

    assert findings, f"没有被拒:{source}"
    assert any(expected in f for f in findings), f"{source} -> {findings}"


def test_the_denial_names_the_gate_function():
    """拒绝信息里必须带上**被改写的那个名字**,否则无法判断拒对了没有。"""
    findings = const.screen_source("x.check_class = 1")

    assert any("check_class" in f for f in findings), findings


def test_gate_attr_rule_covers_the_whole_blacklist():
    """黑名单里**每一个**名字都要被这条规则挡住,不能只挡住清单里的头几个。"""
    missed = [
        name for name in const.BUILTIN_PROTECTED_GATE_ATTRS
        if not const.screen_source(f"x.{name} = 1")
    ]

    assert not missed, f"这些闸门名没被挡住:{missed}"


# --------------------------------------------------------------- 放行侧:不误伤

def test_reading_a_gate_name_is_not_a_write():
    """**读**闸门名不是改写它——放行(否则候选连看一眼都不行了)。"""
    assert const.screen_source("y = x.screen_source") == []


def test_ordinary_attribute_writes_are_not_denied():
    """普通属性写入不受这条规则影响。"""
    assert const.screen_source("x.foo = 1") == []
    assert const.screen_source("self.indicator = 2") == []


def test_setattr_with_an_ordinary_name_is_not_denied():
    """按名 setattr 一个**普通**名字不该被这条规则拒。"""
    assert const.screen_source('setattr(o, "foo", 1)') == []
