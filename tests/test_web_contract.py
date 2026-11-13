"""状态页前后端契约测试:docs/web/status-api.openapi.json 与 docs/web/fixtures/ 必须互相一致。

前端用这些示例做 mock 联调,后端实现完成后用同一批断言验证真实响应——两边对着同一份契约。
"""
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("jsonschema", reason="契约测试需要 jsonschema(见 requirements.txt)")
from jsonschema import Draft202012Validator  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
SPEC = json.loads((WEB / "status-api.openapi.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((WEB / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
COMPONENTS = SPEC["components"]


def _resolve(node):
    """把 $ref 展开成可用 jsonschema 校验的独立 schema(带 components 定义)。"""
    return {"$defs": {}, **node, "components": COMPONENTS}


def _schema_for(method: str, path: str, status: int) -> dict:
    op = SPEC["paths"][path][method.lower()]
    resp = op["responses"][str(status)]
    if "$ref" in resp:
        resp = COMPONENTS["responses"][resp["$ref"].rsplit("/", 1)[1]]
    return resp["content"]["application/json"]["schema"]


def _validator(schema: dict) -> Draft202012Validator:
    # $ref 形如 #/components/schemas/X,根文档需要有 components 键
    return Draft202012Validator({**schema, "components": COMPONENTS})


@pytest.mark.parametrize("entry", MANIFEST, ids=[m["fixture"] for m in MANIFEST])
def test_every_fixture_matches_its_declared_response_schema(entry):
    payload = json.loads((WEB / "fixtures" / f"{entry['fixture']}.json").read_text(encoding="utf-8"))
    errors = sorted(_validator(_schema_for(entry["method"], entry["path"], entry["status"])).iter_errors(payload), key=str)
    assert not errors, "; ".join(e.message for e in errors[:3])


def test_every_fixture_file_is_listed_in_the_manifest_and_vice_versa():
    on_disk = {p.stem for p in (WEB / "fixtures").glob("*.json")} - {"manifest"}
    assert on_disk == {m["fixture"] for m in MANIFEST}


def test_every_operation_has_a_phase_and_a_role():
    for path, methods in SPEC["paths"].items():
        for method, op in methods.items():
            assert op["x-phase"] in {1, 2, 3, 4, 5}, (path, method)
            assert op["x-role"] in {"viewer", "operator", "admin"}, (path, method)


def test_phase_one_is_read_only():
    """阶段①的全部接口只能是 GET:这是『只读状态页』边界在契约层的可执行形式。"""
    for path, methods in SPEC["paths"].items():
        for method, op in methods.items():
            if op["x-phase"] == 1:
                assert method == "get", (path, method)


def test_write_operations_exist_only_in_phase_three_and_only_for_pause_and_resume():
    writes = {(p, m) for p, ms in SPEC["paths"].items() for m in ms if m != "get"}
    assert writes == {("/api/control/pause", "post"), ("/api/control/resume", "post")}
    for p, m in writes:
        assert SPEC["paths"][p][m]["x-phase"] == 3 and SPEC["paths"][p][m]["x-role"] == "operator"


def test_no_endpoint_can_place_cancel_connect_or_change_risk():
    """无论哪个阶段,契约里都不得出现下单/撤单/连接/改风控/解锁/批准类路径。"""
    forbidden = re.compile(r"order|cancel|connect|subscribe|unlock|approve|risk|strategy/(start|stop)|candidates/.*/(approve|run)", re.I)
    offenders = [p for p in SPEC["paths"] if forbidden.search(p)]
    assert not offenders, offenders


def test_sections_never_hide_a_missing_state_behind_ok():
    """缺失/损坏的示例必须没有 data,且 state 不能是 ok。"""
    for entry in MANIFEST:
        payload = json.loads((WEB / "fixtures" / f"{entry['fixture']}.json").read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("state") in {"missing", "corrupt"}:
            assert payload["data"] is None, entry["fixture"]


def test_fixtures_contain_no_real_looking_secrets_or_absolute_paths():
    blob = "\n".join(p.read_text(encoding="utf-8") for p in (WEB / "fixtures").glob("*.json"))
    assert not re.search(r"[A-Za-z]:\\\\|/Users/|/home/", blob)
    assert not re.search(r"password|auth_?code|secret", blob, re.I)
    assert "66170" not in blob                        # 真实账号前缀不得入库


def test_manifest_paths_exist_in_the_spec():
    for entry in MANIFEST:
        assert entry["path"] in SPEC["paths"], entry
        assert entry["method"].lower() in SPEC["paths"][entry["path"]], entry
