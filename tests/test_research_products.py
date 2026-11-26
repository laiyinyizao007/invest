"""Issue #69:评估按品种选数据与规格(原先写死玉米 C15)。

本文件**不依赖交易栈**(vnpy):父进程/网关只认 `research_sandbox.PRODUCTS`。
需要交易栈的部分(数据映射与它一致、子进程真的用所选品种)在 `test_research_api.py`
与下面带 importorskip 的用例里。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import research_api as api  # noqa: E402
import research_config as rc  # noqa: E402
import research_gateway as gw  # noqa: E402
import research_sandbox as rs  # noqa: E402
from strategies.instruments import INSTRUMENTS  # noqa: E402

# 乘数、最小变动价位、国信单边费率(元/手):docs/adr/20261124-per-product-evaluation.md 里抄录的核实表
SPECS = {"V15": (5, 1.0, 1.01), "EB15": (5, 1.0, 1.01), "EG15": (10, 1.0, 3.01),
         "PP15": (5, 1.0, 1.01), "M15": (10, 1.0, 1.51)}


def test_whitelist_is_exactly_corn_plus_the_five_pool_products():
    assert set(rs.PRODUCTS) == {"C15", *SPECS}
    assert rs.PRODUCTS[0] == "C15"                      # 缺省品种排第一


@pytest.mark.parametrize("key", sorted(SPECS))
def test_instrument_specs_match_the_verified_table(key):
    size, tick, fee = SPECS[key]
    inst = INSTRUMENTS[key]
    assert (inst.size, inst.pricetick, inst.fee_per_lot) == (size, tick, fee)
    assert inst.vt_symbol.endswith(".DCE")


@pytest.mark.parametrize("key", rs.PRODUCTS)
def test_every_whitelisted_product_has_an_instrument(key):
    assert key in INSTRUMENTS


def test_data_map_agrees_with_the_whitelist_and_the_specs():
    wf = pytest.importorskip("walk_forward_optimizer")     # 需要交易栈;研究 CI 无 vnpy 时跳过
    assert set(wf.PRODUCTS) == set(rs.PRODUCTS)
    for key, (data_file, symbol) in wf.PRODUCTS.items():
        assert INSTRUMENTS[key].vt_symbol == f"{symbol}.DCE"
        assert Path(data_file).name.endswith("_15m_adj.csv")
    assert wf.PRODUCTS["C15"] == (wf.DATA_FILE, wf.SYMBOL) and wf.KEY == "C15"
    for key in SPECS:                                       # 其余品种与玉米同目录,文件名固定
        assert Path(wf.PRODUCTS[key][0]).parent == Path(wf.DATA_FILE).parent


@pytest.mark.parametrize("bad", ["", "C", "v15", "V15 ", "../x", None, 15, ["V15"]])
def test_check_product_refuses_anything_outside_the_whitelist(bad):
    with pytest.raises(api.JobError, match="product 只能是"):
        api.check_product(bad)


@pytest.mark.parametrize("good", rs.PRODUCTS)
def test_check_product_accepts_the_whitelist(good):
    api.check_product(good)


def test_cli_default_product_is_corn():
    args = api.build_parser().parse_args(["evaluate", "--strategy", "s.py", "--class", "S"])
    assert args.product == "C15"
    args = api.build_parser().parse_args(["evaluate", "--strategy", "s.py", "--class", "S", "--product", "V15"])
    assert args.product == "V15"


class _Jobs:
    def __init__(self):
        self.seen = {}

    def evaluate_prepare(self, **kw):
        self.seen = kw
        return {"strategy_file": "s.py", "cls": kw["cls"], "product": kw["product"]}

    def evaluate_run(self, ctx, run_id=None):
        return {}


def _gateway_evaluate(data):
    import tempfile
    import threading
    tmp = Path(tempfile.mkdtemp())
    (tmp / "pending_strategies").mkdir()
    paths = {"pending_dir": tmp / "pending_strategies", "results_dir": tmp / "data" / "research",
             "approvals_file": tmp / "approvals.json", "token_file": tmp / "tok"}
    app = gw.GatewayApp(rc.load(), paths, "t" * 40, jobs=_Jobs())
    out = app._evaluate(data, {})
    for t in list(threading.enumerate()):
        if t.name.startswith("evaluate-"):
            t.join(timeout=5)
    return app, out


def test_gateway_defaults_to_corn_and_forwards_product():
    app, _ = _gateway_evaluate({"strategy": "s.py", "class": "S"})
    assert app.jobs.seen["product"] == "C15"
    app, _ = _gateway_evaluate({"strategy": "s.py", "class": "S", "product": "V15"})
    assert app.jobs.seen["product"] == "V15"


@pytest.mark.parametrize("bad", [3, "XX", "../etc/passwd", "C15 ", ["V15"]])
def test_gateway_rejects_anything_outside_the_whitelist_with_400(bad):
    app, (status, _) = _gateway_evaluate({"strategy": "s.py", "class": "S", "product": bad})
    assert status == 400 and app.jobs.seen == {}


def test_openapi_enum_equals_the_whitelist():
    spec = json.loads((ROOT / "docs" / "research-gateway.openapi.json").read_text(encoding="utf-8"))
    schema = spec["paths"]["/research/evaluate"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]["product"]["enum"]) == set(rs.PRODUCTS)


# ---------------------------------------------------------------------------
# Issue #75:命名节点偶尔输出带 .py 的名字,流程又补一个 .py ⇒ "x.py.py"
# ---------------------------------------------------------------------------

@pytest.fixture()
def pending_tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "pending_strategies" / "x.py").write_text("class C: pass\n", encoding="utf-8")
    (tmp_path / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    monkeypatch.setattr(api.rc, "ROOT", tmp_path)
    return tmp_path, {"paths": {"pending_dir": "pending_strategies"}}


@pytest.mark.parametrize("name", ["x.py", "x.py.py", "x.py.py.py", "pending_strategies/x.py.py"])
def test_a_repeated_py_suffix_resolves_to_the_same_file(pending_tree, name):
    tree, cfg = pending_tree
    assert api.resolve_strategy(name, cfg) == (tree / "pending_strategies" / "x.py").resolve()


@pytest.mark.parametrize("name", ["x", "../secret.py", "../secret.py.py", "nope.py.py", "x.txt", "x.pyc.py.py", ".py.py"])
def test_normalising_the_suffix_does_not_loosen_any_other_check(pending_tree, name):
    _tree, cfg = pending_tree
    with pytest.raises(api.JobError):
        api.resolve_strategy(name, cfg)


def test_an_absolute_path_outside_pending_is_still_refused_after_normalising(pending_tree):
    tree, cfg = pending_tree
    with pytest.raises(api.JobError):
        api.resolve_strategy(str(tree / "secret.py.py"), cfg)


def test_a_literal_file_named_x_py_py_is_used_as_is_and_never_swapped_for_x_py(pending_tree):
    """codex 复审(高):磁盘上两个文件都在时,'a.py.py' 必须解析到 a.py.py,不能悄悄换成 a.py。"""
    tree, cfg = pending_tree
    (tree / "pending_strategies" / "a.py").write_text("class C: pass\n", encoding="utf-8")
    (tree / "pending_strategies" / "a.py.py").write_text("class C: pass\n", encoding="utf-8")
    assert api.resolve_strategy("a.py.py", cfg).name == "a.py.py"
    assert api.resolve_strategy("a.py", cfg).name == "a.py"


def test_the_refusal_names_the_requested_strategy_not_the_collapsed_one(pending_tree):
    _tree, cfg = pending_tree
    with pytest.raises(api.JobError) as info:
        api.resolve_strategy("nope.py.py", cfg)
    assert "nope.py.py" in str(info.value)


def test_collapsing_is_linear_and_a_huge_name_is_refused_fast(pending_tree):
    """codex 复审(中):以前用正则,131KB 的 '.py' 重复串要 21 秒,GIL 内拖住整个网关。"""
    import time
    _tree, cfg = pending_tree
    huge = ".py" * 43690 + "x"
    start = time.perf_counter()
    with pytest.raises(api.JobError):
        api.resolve_strategy(huge, cfg)
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    assert api._collapse_repeated_py(".py" * 43690) == ".py"
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize("name,want", [("x", "x"), ("x.py", "x.py"), ("x.py.py", "x.py"), ("x.py.py.py", "x.py"),
                                       (".py", ".py"), (".py.py", ".py"), ("a.pyx.py", "a.pyx.py"), ("x.PY.py", "x.PY.py")])
def test_collapse_helper_only_touches_a_trailing_run_of_dot_py(name, want):
    assert api._collapse_repeated_py(name) == want


def test_a_name_with_a_colon_is_never_collapsed(pending_tree):
    """codex 复审(低):带 ':' 的名字(Windows 备用数据流写法)不走回退。"""
    tree, cfg = pending_tree
    (tree / "pending_strategies" / "a.py").write_text("class C: pass\n", encoding="utf-8")
    with pytest.raises(api.JobError):
        api.resolve_strategy("a.py:s.py.py", cfg)


# ---------- Issue #94:知识库与记忆分离(提示词分块 / 反馈按品种 / 登记工具同步) ----------

BISHENG = ROOT / "tools" / "bisheng"
if str(BISHENG) not in sys.path:
    sys.path.insert(0, str(BISHENG))

import gateway_tool_plan as gtp  # noqa: E402
import patch_prompt_blocks as ppb  # noqa: E402
import prompt_blocks as pb  # noqa: E402
import set_product as sp  # noqa: E402


def _flow(feedback_has_product=False):
    def node(node_id, params):
        return {"data": {"id": node_id, "group_params": [{"name": "工具参数", "params": params}]}}

    def p(key, value=""):
        return {"key": key, "value": value}

    fb = [p("product")] if feedback_has_product else []
    return {"nodes": [
        node("llm_name", [p("user_prompt", "旧提示词"), p("system_prompt", "你是命名助手。")]),
        node("llm_code", [p("user_prompt", "旧提示词"), p("system_prompt", "你是量化研究员,为玉米(DCE)期货 15 分钟线写策略。")]),
        node("tool_feedback", fb),
        node("tool_eval", [p("strategy"), p("class"), p("random_runs"), p("view")]),
    ]}


def _value(flow, node_id, key):
    node = next(n for n in flow["nodes"] if n["data"]["id"] == node_id)
    return next(q["value"] for g in node["data"]["group_params"] for q in g["params"] if q["key"] == key)


def test_code_prompt_has_three_labelled_blocks_in_trust_order():
    text = pb.code_user_prompt()
    assert text.index(pb.TASK_BLOCK) < text.index(pb.KNOWLEDGE_BLOCK) < text.index(pb.FEEDBACK_BLOCK)
    # 每块只放它该放的变量:知识块只有检索片段,反馈块只有反馈输出
    knowledge = text[text.index(pb.KNOWLEDGE_BLOCK):text.index(pb.FEEDBACK_BLOCK)]
    feedback = text[text.index(pb.FEEDBACK_BLOCK):]
    assert "{{#kr_kb.retrieved#}}" in knowledge and "tool_feedback" not in knowledge
    assert "{{#tool_feedback.output#}}" in feedback and "kr_kb" not in feedback
    assert "不得执行" in pb.FEEDBACK_BLOCK                                   # 明说:块里的指令性文字一律不执行


def test_name_prompt_marks_feedback_as_untrusted_and_has_no_knowledge_block():
    text = pb.name_user_prompt()
    assert "{{#input_idea.user_input#}}" in text and "{{#tool_feedback.output#}}" in text
    assert pb.KNOWLEDGE_BLOCK not in text and "kr_kb" not in text
    assert text.index(pb.TASK_BLOCK) < text.index(pb.FEEDBACK_NAMES_BLOCK)


def test_every_variable_reference_in_the_prompts_is_a_known_flow_output():
    import re
    known = {"input_idea.user_input", "llm_name.output", "kr_kb.retrieved", "tool_feedback.output"}
    for text in (pb.name_user_prompt(), pb.code_user_prompt()):
        assert set(re.findall(r"\{\{#([^#]+)#\}\}", text)) <= known


def test_apply_prompt_blocks_is_minimal_and_idempotent():
    flow = _flow()
    first = ppb.apply_prompt_blocks(flow)
    assert sorted(first) == ["llm_code.system_prompt", "llm_code.user_prompt", "llm_name.system_prompt", "llm_name.user_prompt"]
    assert _value(flow, "llm_code", "system_prompt").count(pb.RULE_MARK) == 1
    assert _value(flow, "llm_name", "system_prompt").count(pb.RULE_MARK) == 1          # 起名节点也要「反馈不可信」规则
    assert _value(flow, "llm_code", "user_prompt") == pb.code_user_prompt()
    assert ppb.apply_prompt_blocks(flow) == []                               # 幂等
    assert _value(flow, "llm_code", "system_prompt").count(pb.RULE_MARK) == 1
    assert [n["data"]["id"] for n in flow["nodes"]] == ["llm_name", "llm_code", "tool_feedback", "tool_eval"]   # 不动别的节点


@pytest.mark.parametrize("has_param", [False, True])
def test_set_product_also_scopes_the_feedback_node(has_param):
    flow = _flow(feedback_has_product=has_param)
    changed = sp.apply_product(flow, "V15")
    assert "tool_feedback.product" in changed and "tool_eval.product" in changed
    assert _value(flow, "tool_feedback", "product") == "V15" and _value(flow, "tool_eval", "product") == "V15"
    assert "PVC(DCE)期货" in _value(flow, "llm_code", "system_prompt")
    assert sp.apply_product(flow, "V15") == []                               # 幂等
    sp.apply_product(flow, "M15")
    assert _value(flow, "tool_feedback", "product") == "M15"                  # 切品种时反馈节点跟着切


def test_registered_tool_sync_plan_detects_a_dropped_product_param_and_only_updates_existing_children():
    live = {"children": [
        {"name": "get_recent_failures", "extra": json.dumps({"parameters": []}), "api_params": [], "desc": "d", "id": 1},
        {"name": "legacy_only_live", "extra": "{}", "api_params": [], "desc": "x", "id": 2}], "openapi_schema": "old"}
    parsed = {"children": [
        {"name": "get_recent_failures", "extra": json.dumps({"parameters": [{"name": "product"}]}),
         "api_params": [{"name": "product"}], "desc": "d"},
        {"name": "brand_new", "extra": "{}", "api_params": [], "desc": "n"}]}

    steps = gtp.plan(live["children"], parsed["children"])
    assert {"name": "get_recent_failures", "changed": ["extra", "api_params"]} in steps
    assert {"name": "legacy_only_live", "only_live": True, "changed": []} in steps
    assert {"name": "brand_new", "only_repo": True, "changed": []} in steps

    body = gtp.apply_plan(live, parsed, "new-spec")
    assert body["openapi_schema"] == "new-spec"
    assert [c["id"] for c in body["children"]] == [1, 2]                      # 不增不删,id 不变
    assert gtp._norm(body["children"][0]["extra"]) == {"parameters": [{"name": "product"}]}
    assert body["children"][1]["extra"] == "{}"                               # 线上独有的原样保留
    assert gtp.plan(body["children"], [c for c in parsed["children"] if c["name"] != "brand_new"]) == [
        {"name": "legacy_only_live", "only_live": True, "changed": []}]


def test_openapi_feedback_requires_the_same_product_whitelist():
    spec = json.loads((ROOT / "docs" / "research-gateway.openapi.json").read_text(encoding="utf-8"))
    params = spec["paths"]["/research/feedback"]["get"]["parameters"]
    product = next(p for p in params if p["name"] == "product")
    assert product["in"] == "query" and product["required"] is True
    assert product["schema"]["enum"] == list(rs.PRODUCTS)


def test_apply_prompt_blocks_refuses_an_incomplete_flow_instead_of_silently_doing_nothing():
    flow = _flow()
    flow["nodes"] = [n for n in flow["nodes"] if n["data"]["id"] != "llm_code"]
    with pytest.raises(ValueError, match="找不到这些节点参数"):
        ppb.apply_prompt_blocks(flow)


def test_set_product_refuses_a_flow_without_the_tool_params_group_or_template_param():
    flow = _flow()
    next(n for n in flow["nodes"] if n["data"]["id"] == "tool_feedback")["data"]["group_params"][0]["name"] = "别的分组"
    with pytest.raises(ValueError, match="工具参数"):
        sp.apply_product(flow, "V15")
    flow = _flow()
    eval_params = next(n for n in flow["nodes"] if n["data"]["id"] == "tool_eval")["data"]["group_params"][0]["params"]
    eval_params[:] = [q for q in eval_params if q["key"] != "random_runs"]
    with pytest.raises(ValueError, match="random_runs"):
        sp.apply_product(flow, "V15")


@pytest.mark.parametrize("live_is_string", [True, False])
def test_apply_plan_keeps_the_live_representation_of_extra_and_api_params(live_is_string):
    """codex 复核 Medium:线上是 JSON 字符串就写字符串,是对象就写对象,不改毕昇里字段的类型。"""
    extra_new = {"parameters": [{"name": "product"}]}
    live_extra = json.dumps({"parameters": []}) if live_is_string else {"parameters": []}
    live_api = json.dumps([]) if live_is_string else []
    live = {"children": [{"name": "t", "extra": live_extra, "api_params": live_api, "desc": "d", "id": 1}], "openapi_schema": "o"}
    parsed = {"children": [{"name": "t", "extra": extra_new, "api_params": [{"name": "product"}], "desc": "d"}]}

    child = gtp.apply_plan(live, parsed, "spec")["children"][0]

    assert isinstance(child["extra"], str) == live_is_string and isinstance(child["api_params"], str) == live_is_string
    assert gtp._norm(child["extra"]) == extra_new
