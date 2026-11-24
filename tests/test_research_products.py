"""Issue #69:评估按品种选数据与规格(原先写死玉米 C15)。

覆盖:白名单与规格表一致(取自 20261116 候选池核实表);缺省 C15 与旧行为一致;
非法 product 在父进程与子进程两处都被拒;网关缺省 C15、把 product 原样交给作业;
子进程真的用所选品种的数据文件与规格键。
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
import walk_forward_optimizer as wf  # noqa: E402
from strategies.instruments import INSTRUMENTS  # noqa: E402

# 乘数、最小变动价位、国信单边费率(元/手):docs/adr/20261116-candidate-product-pool.md 核实表
SPECS = {"V15": (5, 1.0, 1.01), "EB15": (5, 1.0, 1.01), "EG15": (10, 1.0, 3.01),
         "PP15": (5, 1.0, 1.01), "M15": (10, 1.0, 1.51)}


def test_whitelist_is_exactly_corn_plus_the_five_pool_products():
    assert set(wf.PRODUCTS) == {"C15", *SPECS}


@pytest.mark.parametrize("key", sorted(SPECS))
def test_instrument_specs_match_the_verified_table(key):
    size, tick, fee = SPECS[key]
    inst = INSTRUMENTS[key]
    assert (inst.size, inst.pricetick, inst.fee_per_lot) == (size, tick, fee)
    assert inst.vt_symbol.endswith(".DCE")


@pytest.mark.parametrize("key", sorted(wf.PRODUCTS))
def test_every_product_has_a_spec_and_a_symbol_that_matches_it(key):
    data_file, symbol = wf.PRODUCTS[key]
    assert key in INSTRUMENTS
    assert INSTRUMENTS[key].vt_symbol == f"{symbol}.DCE"
    assert Path(data_file).name.endswith("_15m_adj.csv")


def test_corn_is_unchanged():
    assert wf.PRODUCTS["C15"] == (wf.DATA_FILE, wf.SYMBOL)
    assert wf.KEY == "C15"


def test_other_products_do_not_follow_the_corn_env_override():
    derived = Path(r"C:\Dev\data\invest\derived")
    for key in SPECS:
        assert Path(wf.PRODUCTS[key][0]).parent == derived


@pytest.mark.parametrize("bad", ["", "C", "v15", "V15 ", "../x", None, 15, ["V15"]])
def test_check_product_refuses_anything_outside_the_whitelist(bad):
    with pytest.raises(api.JobError):
        api.check_product(bad)


@pytest.mark.parametrize("good", sorted(wf.PRODUCTS))
def test_check_product_accepts_the_whitelist(good):
    api.check_product(good)


def test_evaluate_job_refuses_a_bad_product_before_doing_anything():
    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", product="XX")


def test_cli_default_product_is_corn():
    args = api.build_parser().parse_args(["evaluate", "--strategy", "s.py", "--class", "S"])
    assert args.product == "C15"
    args = api.build_parser().parse_args(["evaluate", "--strategy", "s.py", "--class", "S", "--product", "V15"])
    assert args.product == "V15"


def test_child_refuses_a_bad_product(capsys):
    args = api.build_parser().parse_args(["_evaluate_child", "--file", "x", "--cls", "S", "--random-runs", "20",
                                          "--sha256", "0" * 64, "--product", "XX"])
    code = api.run_evaluate_child(args)
    assert code != api.EXIT_OK


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


def test_gateway_rejects_a_non_string_product():
    _, (status, body) = _gateway_evaluate({"strategy": "s.py", "class": "S", "product": 3})
    assert status == 400


def test_openapi_enum_equals_the_whitelist():
    spec = json.loads((ROOT / "docs" / "research-gateway.openapi.json").read_text(encoding="utf-8"))
    schema = spec["paths"]["/research/evaluate"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]["product"]["enum"]) == set(wf.PRODUCTS)
