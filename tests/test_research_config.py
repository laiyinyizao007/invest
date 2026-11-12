"""研究沙盒配置:全部可调参数集中在 config/research_sandbox.json,加载时严格校验,无效即拒绝(fail-closed)。"""
import json

import pytest

import research_config as rc


def write(tmp_path, data):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_shipped_config_file_loads_and_matches_defaults():
    cfg = rc.load()

    assert cfg["iteration"]["max_iterations"] == 5
    assert cfg["feedback"]["limit"] == 3
    assert cfg["gate"]["random_runs"] == 200
    assert cfg["gateway"]["host"] == "127.0.0.1"
    assert cfg == rc.load(None)                                           # 默认路径即随仓库的配置文件


def test_missing_file_falls_back_to_defaults(tmp_path):
    assert rc.load(tmp_path / "nope.json") == rc.DEFAULTS


def test_partial_file_overrides_only_what_it_names(tmp_path):
    cfg = rc.load(write(tmp_path, {"gate": {"random_runs": 500}}))

    assert cfg["gate"]["random_runs"] == 500
    assert cfg["gate"]["timeout_seconds"] == rc.DEFAULTS["gate"]["timeout_seconds"]
    assert cfg["feedback"]["limit"] == 3


@pytest.mark.parametrize("data", [
    {"gate": {"random_runs": 5}},                  # 太少,分位数没有意义
    {"gate": {"random_runs": 100000}},             # 失控
    {"feedback": {"limit": 0}},                    # 0 条等于关闭反馈
    {"feedback": {"limit": 11}},                   # 违反「硬记忆预算」精神
    {"iteration": {"max_iterations": 0}},
    {"iteration": {"max_iterations": 1000}},
    {"backtest": {"timeout_seconds": 0}},
    {"candidates": {"max_source_bytes": 0}},
    {"candidates": {"name_pattern": "("}},         # 非法正则
    {"candidates": {"name_pattern": ".*"}},        # 放开任意文件名(含路径分隔符)
    {"gateway": {"host": "0.0.0.0"}},              # 暴露到所有网卡
    {"gateway": {"host": "::"}},
    {"gateway": {"port": 0}},
    {"gateway": {"port": 70000}},
    {"gateway": {"rate_limit_per_minute": 0}},
    {"paths": {"pending_dir": "../outside"}},
    {"paths": {"approvals_file": "C:/abs/approvals.json"}},
    {"paths": {"pending_dir": "strategies"}},      # 不得指向正式策略目录
    {"paths": {"pending_dir": "approved"}},
    {"gateway": {"token_file": ".env"}},           # 不得借用交易 .env
])
def test_invalid_values_are_rejected(tmp_path, data):
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, data))


@pytest.mark.parametrize("data", [{"typo": {}}, {"gate": {"random_run": 200}}, {"gate": 5}])
def test_unknown_keys_and_wrong_shapes_are_rejected(tmp_path, data):
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, data))


def test_corrupt_json_is_rejected(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(rc.ConfigError):
        rc.load(path)


def test_wrong_types_are_rejected(tmp_path):
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, {"gate": {"random_runs": "200"}}))
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, {"gate": {"random_runs": True}}))


def test_screening_lists_can_only_extend_never_shrink(tmp_path):
    cfg = rc.load(write(tmp_path, {"screening": {"extra_forbidden_modules": ["numpy"], "extra_forbidden_calls": ["print"]}}))

    assert cfg["screening"]["extra_forbidden_modules"] == ["numpy"]
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, {"screening": {"extra_forbidden_modules": [1]}}))


def test_resolved_paths_stay_inside_project_root(tmp_path):
    cfg = rc.load()
    paths = rc.resolve_paths(cfg, tmp_path)

    assert all(tmp_path in p.parents for p in paths.values())
    assert set(paths) == {"pending_dir", "results_dir", "approvals_file", "token_file"}


def test_config_path_can_be_overridden_by_environment(tmp_path, monkeypatch):
    path = write(tmp_path, {"gate": {"random_runs": 321}})
    monkeypatch.setenv("RESEARCH_SANDBOX_CONFIG", str(path))

    assert rc.load()["gate"]["random_runs"] == 321


def test_explicit_path_beats_environment(tmp_path, monkeypatch):
    env_path = write(tmp_path, {"gate": {"random_runs": 321}})
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"gate": {"random_runs": 111}}), encoding="utf-8")
    monkeypatch.setenv("RESEARCH_SANDBOX_CONFIG", str(env_path))

    assert rc.load(other)["gate"]["random_runs"] == 111


def test_shipped_paths_keep_approvals_and_token_in_the_control_directory():
    cfg = rc.load()

    assert cfg["paths"]["approvals_file"].startswith("data/research-control/")
    assert cfg["gateway"]["token_file"].startswith("data/research-control/")
    assert not cfg["paths"]["results_dir"].startswith("data/research-control")      # 结果目录(容器可写)与控制目录(容器只读)分开
