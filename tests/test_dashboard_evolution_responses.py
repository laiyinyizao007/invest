"""进化引擎的**真实响应**必须通得过契约的 schema。

此前只有战役列表那一个被真实请求校验过;曲线、日志、热力图、源码、差异五个都只校验过 collector 的返回值。
『真实响应对 schema』这条纪律是踩过坑才有的(响应形态与契约分叉,前端 mock 跑得通、生产拿到 undefined),
所以六个都要走一遍真实请求。
"""
import json
from pathlib import Path

import pytest

from dashboard import app as appmod
from dashboard import config as appconfig

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
SPEC = json.loads((WEB / "status-api.openapi.json").read_text(encoding="utf-8"))
COMPONENTS = SPEC["components"]
CAMPAIGN = "camp-2026-11"
RUN_ID = "20261112-210000-000000"
SHA_PARENT = "a" * 64
SHA_CHILD = "b" * 64

RECORD = {"campaign_id": CAMPAIGN, "generation": 1, "candidate_sha256": SHA_CHILD, "parent_sha256": SHA_PARENT,
          "run_id": RUN_ID, "status": "evaluated", "sharpe": 1.4, "total_return": 0.12, "max_drawdown": 0.08,
          "dsr": 0.9, "n_trials": 3, "fail_reason": None, "reflection": "反思文本",
          "created_at": "2026-11-12T21:00:00", "cell": "fast-mid", "diagnosis": {"weakest": "drawdown"}}


def _client(tmp_path):
    from fastapi.testclient import TestClient

    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "status_dashboard.json").write_text(
        json.dumps({"environment": "simnow", "evolution": {"enabled": True}}), encoding="utf-8")
    evo = root / "data" / "research" / "evolution"
    evo.mkdir(parents=True)
    (evo / f"{CAMPAIGN}.jsonl").write_text(json.dumps(RECORD, ensure_ascii=False) + "\n", encoding="utf-8")
    heat = root / "data" / "research" / "heatmaps"
    heat.mkdir(parents=True)
    (heat / f"{RUN_ID}.json").write_text(json.dumps({
        "axes": {"breakout_n": [48, 72], "atr_mult": [1.5, 2.0], "slow_window": [96]},
        "scores": [[0, 0, 0, 1.1], [1, 0, 0, 0.9], [0, 1, 0, "-inf"], [1, 1, 0, 0.3]],
        "best_cell": [0, 0, 0], "on_plateau": False,
        "window": ["2020-07-22", "2022-06-30"], "source": "selection"}), encoding="utf-8")
    pend = root / "pending_strategies"
    pend.mkdir(parents=True)
    (pend / "gen1.py").write_bytes(b"class C:\n    x = 1\n")
    (pend / "gen2.py").write_bytes(b"class C:\n    x = 2\n")
    settings = appconfig.load(path=root / "config" / "status_dashboard.json", root=root)
    return TestClient(appmod.create_app(settings, "tok"))


def _schema(path):
    resp = SPEC["paths"][path]["get"]["responses"]["200"]
    return {**resp["content"]["application/json"]["schema"], "components": COMPONENTS}


@pytest.mark.parametrize("path", [
    "/api/research/evolution",
    "/api/research/evolution/{campaign_id}/curve",
    "/api/research/evolution/{campaign_id}/log",
    "/api/research/runs/{run_id}/heatmap",
    "/api/research/candidates/{sha256}/code",
    "/api/research/candidates/{sha256}/diff",
])
def test_every_evolution_endpoint_returns_something_that_matches_the_contract(tmp_path, path):
    jsonschema = pytest.importorskip("jsonschema", reason="这个用例需要 jsonschema(见 requirements.txt)")
    from jsonschema import Draft202012Validator

    client = _client(tmp_path)
    concrete = (path.replace("{campaign_id}", CAMPAIGN).replace("{run_id}", RUN_ID)
                .replace("{sha256}", SHA_CHILD))
    params = {"parent": SHA_PARENT} if "diff" in path else None   # 差异接口必须带父代哈希(契约里是必填)
    response = client.get(concrete, headers={"Authorization": "Bearer tok"}, params=params)
    assert response.status_code == 200, (concrete, response.text[:200])
    errors = sorted(Draft202012Validator(_schema(path)).iter_errors(response.json()), key=str)
    assert not errors, (path, [e.message for e in errors[:3]])
