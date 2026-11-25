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

    def evaluate_job(self, **kw):
        self.seen = kw
        return {"kind": "evaluate", "passed": False, "status": "ok"}


def _gateway_evaluate(data):
    app = gw.GatewayApp.__new__(gw.GatewayApp)
    app.jobs = _Jobs()
    app._run_job = lambda func, **kw: func(**kw)
    app._shape = lambda result, view: (200, result)
    return app, app._evaluate(data, {})


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
