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


# ---------- 受保护目录/凭据文件:大小写不敏感,且三个路径都要查(第 6 轮 codex 的 M3) ----------

@pytest.mark.parametrize("key,value", [
    ("pending_dir", "Strategies"),              # 正式策略目录
    ("results_dir", "strategies"),
    ("approvals_file", "Strategies/approvals.json"),
    ("pending_dir", "data"),                    # data 本身不是受保护目录,这条是反向对照
])
def test_paths_must_not_point_at_protected_directories(tmp_path, key, value):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"paths": {key: value}}), encoding="utf-8")

    if value == "data":                          # 反向对照:普通目录应当放行
        assert rc.load(path)["paths"][key] == value
        return
    with pytest.raises(rc.ConfigError):
        rc.load(path)


def test_token_file_must_not_borrow_a_credential_file(tmp_path):
    """`token_file="data/.ENV"` 在 Windows 上就是 .env:网关令牌会覆盖 CTP 凭据文件。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"gateway": {"token_file": "data/.ENV"}}), encoding="utf-8")

    with pytest.raises(rc.ConfigError):
        rc.load(path)


def test_approvals_file_must_not_borrow_a_credential_file(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"paths": {"approvals_file": "data/.env"}}), encoding="utf-8")

    with pytest.raises(rc.ConfigError):
        rc.load(path)


# ---------- 控制目录与文件名是**钉死**的:改了路径就必须被拒(第 10 轮 codex 的 M7) ----------

@pytest.mark.parametrize("key,value", [
    ("approvals_file", "data/other/approvals.json"),              # 换个目录
    ("approvals_file", "data/research-control/approvals.json.bak"),  # 换个文件名
    ("approvals_file", "../data/research-control/approvals.json"),   # 越出仓库
    ("approvals_file", "data/research-control/other.json"),          # 目录对、名字不对
])
def test_the_approvals_file_path_is_pinned(tmp_path, key, value):
    """批准记录的位置是**结构约束**,不是"文件名不在黑名单里"。

    钉死它的理由:批准记录是"人批准过"的唯一凭据,位置一变,批准页、
    网关校验与 CLI 三处对不上就是静默失配(第 7 轮 codex 的 F8)。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"paths": {key: value}}), encoding="utf-8")

    with pytest.raises(rc.ConfigError):
        rc.load(path)


@pytest.mark.parametrize("value", [
    "data/other/gateway.token",
    "data/research-control/gateway.token.bak",
    "data/research-control/other.token",
    "../data/research-control/gateway.token",
])
def test_the_gateway_token_path_is_pinned(tmp_path, value):
    """网关令牌同理:固定目录 + 固定文件名,没有借名的余地。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"gateway": {"token_file": value}}), encoding="utf-8")

    with pytest.raises(rc.ConfigError):
        rc.load(path)


def test_grid_section_has_sane_shipped_values():
    cfg = rc.load()

    assert cfg["grid"] == {"min_round_trips": 10, "max_cells": 343, "timeout_seconds": 1800}


@pytest.mark.parametrize("data", [
    {"grid": {"max_cells": 26}},               # 少于一个 3x3x3 邻域,永远选不出参数
    {"grid": {"max_cells": 100000}},           # 失控
    {"grid": {"min_round_trips": 0}},
    {"grid": {"min_round_trips": 100000}},
    {"grid": {"timeout_seconds": 0}},
])
def test_invalid_grid_values_are_rejected(tmp_path, data):
    with pytest.raises(rc.ConfigError):
        rc.load(write(tmp_path, data))


def test_iteration_window_hours_default_and_bounds(tmp_path):
    assert rc.load()["iteration"] == {"max_iterations": 5, "window_hours": 24}
    for bad in (0, 721):
        with pytest.raises(rc.ConfigError):
            rc.load(write(tmp_path, {"iteration": {"window_hours": bad}}))
