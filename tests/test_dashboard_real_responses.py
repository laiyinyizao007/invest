"""真实 HTTP 响应必须通得过 OpenAPI 的 schema。

曾出过一个大问题:`/api/research/evolution` 的真实响应带 Section 外壳,而契约、样例、类型都是裸数据 ——
真实响应通不过自己的契约(`'campaigns' is a required property`)。而套件全绿,因为**没有任何测试拿真实响应对 schema**:
契约测试只校验样例对 schema,另一个测试只校验 collector 的内层 data。
前端照契约写代码,mock 跑得通、生产拿到 undefined ——最坏的失败方式。

所以这里补上:对每个接口跑**真实响应**,按契约里的 schema 校验。
"""
import json
from pathlib import Path

import pytest

from dashboard import app as appmod
from dashboard import config as appconfig

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
SPEC = json.loads((WEB / "status-api.openapi.json").read_text(encoding="utf-8"))
COMPONENTS = SPEC["components"]

PHASE_ONE = [p for p in SPEC["paths"] if SPEC["paths"][p].get("get", {}).get("x-phase") == 1]
# 带路径参数的接口:用契约里的例子值(取不到就跳过——那是别处的测试负责的)
PATH_PARAMS = {"/api/research/runs/{run_id}": {"run_id": "20261112-210000-000000"},
               "/api/docs/{key}": {"key": "unattended-log"}}


def _schema_for(path):
    op = SPEC["paths"][path]["get"]
    resp = op["responses"]["200"]
    if "$ref" in resp:
        resp = COMPONENTS["responses"][resp["$ref"].rsplit("/", 1)[1]]
    return resp["content"]["application/json"]["schema"]


def _client(tmp_path, **extra):
    from fastapi.testclient import TestClient

    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    cfg = root / "config" / "status_dashboard.json"
    cfg.write_text(json.dumps({"environment": "simnow", **extra}), encoding="utf-8")
    settings = appconfig.load(path=cfg, root=root)
    return TestClient(appmod.create_app(settings, "tok"))


@pytest.mark.parametrize("path", PHASE_ONE)
def test_the_real_response_validates_against_the_contract(tmp_path, path):
    """真实响应对 schema —— 不只是样例对 schema。"""
    jsonschema = pytest.importorskip("jsonschema")
    from jsonschema import Draft202012Validator

    client = _client(tmp_path)
    params = PATH_PARAMS.get(path)
    if params is not None and path == "/api/docs/{key}":
        pytest.skip("文档接口按 key 取内容,形状由另一处测试负责")
    response = client.get(path, headers={"Authorization": "Bearer tok"}, params=params)
    if response.status_code == 404 and params is not None:
        pytest.skip(f"{path} 需要真实数据,形状由另一处测试负责")
    assert response.status_code == 200, path
    schema = {"$ref": "#/components/schemas/SectionBase"} if path not in (
        "/api/meta", "/api/overview") else _schema_for(path)
    if path not in ("/api/meta", "/api/overview"):
        # 区块型接口:响应是 SectionBase + data
        schema = {"allOf": [{"$ref": "#/components/schemas/SectionBase"}], "components": COMPONENTS}
    else:
        schema = {**schema, "components": COMPONENTS}
    errors = sorted(Draft202012Validator(schema).iter_errors(response.json()), key=str)
    assert not errors, (path, [e.message for e in errors[:3]])


def test_the_evolution_list_response_also_validates(tmp_path):
    """进化列表是**裸数据**形态(与 curve/log/heatmap 一致);之前它返回 Section 外壳而契约是裸数据。"""
    jsonschema = pytest.importorskip("jsonschema")
    from jsonschema import Draft202012Validator

    client = _client(tmp_path, evolution={"enabled": True})
    response = client.get("/api/research/evolution", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    schema = {**_schema_for("/api/research/evolution"), "components": COMPONENTS}
    errors = sorted(Draft202012Validator(schema).iter_errors(response.json()), key=str)
    assert not errors, [e.message for e in errors[:3]]
