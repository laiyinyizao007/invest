"""参数热力图:写出去的必须面板读取侧读得进,而且轴不能错位。纯逻辑 + tmp_path。"""
import json
import pytest

import research_api as api
from dashboard.evolution import EvolutionError, read_heatmap
from risk.heatmap import HeatmapError, build_heatmap, save_heatmap, validate_heatmap

NEG_INF = float("-inf")


def selection(declared=("slow_window", "fast_window", "atr_mult"), best=None):
    """按**声明顺序**(故意不是名字排序的顺序)造一个 select_params 的输出。得分是各轴取值的确定性函数,
    这样读回来之后能逐格核对有没有错位。"""
    values = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 90], "atr_mult": [1.0, 1.5, 2.0, 2.5]}
    axes = {name: values[name] for name in declared}

    def score(params):
        return params["fast_window"] * 0.01 + params["slow_window"] * 0.001 + params["atr_mult"] * 0.1

    rows = []
    for i, a in enumerate(axes[declared[0]]):
        for j, b in enumerate(axes[declared[1]]):
            for k, c in enumerate(axes[declared[2]]):
                params = {declared[0]: a, declared[1]: b, declared[2]: c}
                rows.append([i, j, k, score(params)])
    return {"axes": axes, "window": ["2020-07-22", "2022-06-30"], "best_cell": best, "on_plateau": True,
            "plateau": {"cell": best, "scores": rows}}, score


def test_axes_and_indices_are_reordered_to_the_name_sorted_order_the_reader_expects(tmp_path):
    """读取侧把三个轴按名字排序后当 x/y/z。声明顺序(slow, fast, atr)与排序顺序(atr, fast, slow)不同,
    不换的话面板上的热力图是错位的——这条用真正的读取函数逐格核对。"""
    sel, score = selection(best=[2, 1, 3])               # 声明顺序下:slow=90, fast=20, atr=2.5
    body = build_heatmap(sel)
    assert list(body["axes"]) == ["atr_mult", "fast_window", "slow_window"]
    assert body["best_cell"] == [3, 1, 2]                 # 同一个格子,换成排序顺序的下标
    save_heatmap(tmp_path, "run1", body)

    view = read_heatmap(tmp_path, "run1")
    assert view["axes"]["x"]["name"] == "atr_mult" and view["axes"]["y"]["name"] == "fast_window"
    assert view["axes"]["z"]["name"] == "slow_window" and view["axes"]["z"]["selected"] == 2
    x_values, y_values = view["axes"]["x"]["values"], view["axes"]["y"]["values"]
    third_value = view["axes"]["z"]["values"][view["axes"]["z"]["selected"]]
    for yi, fast in enumerate(y_values):
        for xi, atr in enumerate(x_values):
            expected = score({"fast_window": fast, "slow_window": third_value, "atr_mult": atr})
            assert view["grid"][yi][xi] == pytest.approx(expected), (fast, atr)


@pytest.mark.parametrize("declared", [
    ("atr_mult", "fast_window", "slow_window"),           # 恰好已经是排序顺序
    ("slow_window", "fast_window", "atr_mult"),
    ("fast_window", "slow_window", "atr_mult"),
    ("slow_window", "atr_mult", "fast_window"),
])
def test_every_declaration_order_reads_back_the_same_values(tmp_path, declared):
    sel, score = selection(declared=declared, best=[1, 1, 1])
    save_heatmap(tmp_path, "r", build_heatmap(sel))
    view = read_heatmap(tmp_path, "r")
    zi = view["axes"]["z"]["selected"]
    third = view["axes"]["z"]["values"][zi]
    names = {view["axes"][k]["name"]: k for k in "xyz"}
    for yi, y in enumerate(view["axes"]["y"]["values"]):
        for xi, x in enumerate(view["axes"]["x"]["values"]):
            params = {view["axes"]["x"]["name"]: x, view["axes"]["y"]["name"]: y, view["axes"]["z"]["name"]: third}
            assert view["grid"][yi][xi] == pytest.approx(score(params))
    assert set(names) == {"atr_mult", "fast_window", "slow_window"}


def test_invalid_cells_are_written_as_the_string_minus_inf_never_as_json_infinity(tmp_path):
    sel, _ = selection(best=[0, 0, 0])
    sel["plateau"]["scores"][0][3] = "-inf"                              # select_params 自己写的形态
    sel["plateau"]["scores"][1][3] = NEG_INF                             # 万一漏过来的浮点 -inf
    sel["plateau"]["scores"][2][3] = float("nan")
    sel["plateau"]["scores"][3][3] = None
    body = build_heatmap(sel)
    assert sum(1 for row in body["scores"] if row[3] == "-inf") == 4
    path = save_heatmap(tmp_path, "r", body)
    text = path.read_text(encoding="utf-8")
    assert "Infinity" not in text and "NaN" not in text and '"-inf"' in text
    view = read_heatmap(tmp_path, "r")
    assert view["valid_cells"] == view["n_cells"] - 4                    # 读取侧把它们读成"空",不是 0


def test_the_note_tells_the_reader_the_numbers_are_not_out_of_sample(tmp_path):
    sel, _ = selection(best=[0, 0, 0])
    save_heatmap(tmp_path, "r", build_heatmap(sel))
    view = read_heatmap(tmp_path, "r")
    assert "不是样本外" in view["note"] and view["on_plateau"] is True
    assert view["source_window"] == ["2020-07-22", "2022-06-30"]


def test_a_heatmap_without_a_best_cell_is_allowed(tmp_path):
    sel, _ = selection(best=None)
    sel["on_plateau"] = False
    body = build_heatmap(sel)
    assert body["best_cell"] is None and body["on_plateau"] is False
    save_heatmap(tmp_path, "r", body)
    assert read_heatmap(tmp_path, "r")["best_cell"] is None


# ---------- 校验 ----------

def good():
    return build_heatmap(selection(best=[0, 0, 0])[0])


def test_the_built_heatmap_validates():
    validate_heatmap(good())


@pytest.mark.parametrize("mutate", [
    lambda b: b["scores"].append(b["scores"][0]),                                   # 重复格子
    lambda b: b["scores"].pop(),                                                    # 网格不完整
    lambda b: b["scores"][0].__setitem__(0, 99),                                    # 下标越界
    lambda b: b["scores"][0].__setitem__(0, True),                                  # 布尔下标
    lambda b: b["scores"][0].__setitem__(3, float("nan")),                          # 非有限得分
    lambda b: b["scores"][0].__setitem__(3, "bad"),
    lambda b: b.__setitem__("axes", dict(reversed(list(b["axes"].items())))),       # 没按名字排序
    lambda b: b["axes"].__setitem__("fast_window", []),
    lambda b: b["axes"].__setitem__("fast_window", [10, True, 30]),
    lambda b: b.__setitem__("best_cell", [9, 0, 0]),
    lambda b: b.__setitem__("best_cell", [0, 0]),
    lambda b: b.__setitem__("on_plateau", "yes"),
    lambda b: b.__setitem__("scores", "nope"),
])
def test_malformed_heatmaps_are_rejected(mutate):
    body = good()
    mutate(body)
    with pytest.raises(HeatmapError):
        validate_heatmap(body)


def test_a_non_three_dimensional_selection_is_refused():
    sel, _ = selection()
    sel["axes"].pop("atr_mult")
    with pytest.raises(HeatmapError):
        build_heatmap(sel)


@pytest.mark.parametrize("bad_row", [[0, 0, 0], [0, 0, 0, 1.0, 2.0], "x", None])
def test_a_malformed_score_row_in_the_selection_is_refused(bad_row):
    sel, _ = selection(best=[0, 0, 0])
    sel["plateau"]["scores"][0] = bad_row
    with pytest.raises(HeatmapError):
        build_heatmap(sel)


# ---------- 落盘 ----------

@pytest.mark.parametrize("run_id", ["", "../x", "a/b", "a\\b", "a b", ".hidden", "x" * 65, None, 5])
def test_unsafe_run_ids_are_refused_and_nothing_is_written(tmp_path, run_id):
    with pytest.raises(HeatmapError):
        save_heatmap(tmp_path / "hm", run_id, good())
    assert not (tmp_path / "hm").exists() or not list((tmp_path / "hm").iterdir())


def test_an_existing_heatmap_is_never_overwritten(tmp_path):
    save_heatmap(tmp_path, "r1", good())
    before = (tmp_path / "r1.json").read_bytes()
    other = good()
    other["on_plateau"] = False
    with pytest.raises(HeatmapError):
        save_heatmap(tmp_path, "r1", other)
    assert (tmp_path / "r1.json").read_bytes() == before


def test_the_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    save_heatmap(tmp_path, "r1", good())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r1.json"]


def test_a_failed_write_raises_and_leaves_no_partial_file(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("risk.heatmap.os.replace", boom)
    with pytest.raises(HeatmapError):
        save_heatmap(tmp_path, "r1", good())
    assert not (tmp_path / "r1.json").exists()


def test_an_oversized_heatmap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("risk.heatmap.MAX_HEATMAP_BYTES", 100)
    with pytest.raises(HeatmapError):
        save_heatmap(tmp_path, "r1", good())


def test_the_reader_rejects_what_the_writer_would_refuse_so_the_two_stay_in_step(tmp_path):
    """读取侧才是消费者:凡是写端校验不过的,读取侧也不该接受(口径同源)。"""
    body = good()
    body["scores"].pop()
    (tmp_path / "bad.json").write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(EvolutionError):
        read_heatmap(tmp_path, "bad")
    with pytest.raises(HeatmapError):
        validate_heatmap(body)


# ---------- 评估接线 ----------

# ---------- 作业结果里的热力图(评估接线的断言在 test_research_gate.py) ----------


def test_take_heatmap_removes_it_from_the_job_body_and_validates_it():
    body = {"heatmap": good(), "passed": True}
    heatmap, note = api.take_heatmap(body)
    assert "heatmap" not in body and note is None and heatmap is not None


def test_a_garbage_heatmap_is_dropped_with_a_note_instead_of_failing_the_job():
    body = {"heatmap": {"axes": "garbage"}}
    heatmap, note = api.take_heatmap(body)
    assert heatmap is None and "丢弃" in note and "heatmap" not in body


def test_no_heatmap_is_fine():
    assert api.take_heatmap({"passed": True}) == (None, None)


def test_saving_the_job_heatmap_writes_next_to_the_results(tmp_path):
    paths = {"results_dir": str(tmp_path)}
    api.save_job_heatmap(paths, "run42", good())
    assert (tmp_path / "heatmaps" / "run42.json").exists()


def test_a_failed_heatmap_write_fails_loudly_after_the_result_is_saved(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise HeatmapError("disk full")

    monkeypatch.setattr(api, "save_heatmap", boom)
    with pytest.raises(api.JobError) as exc:
        api.save_job_heatmap({"results_dir": str(tmp_path)}, "run42", good())
    assert exc.value.code == api.EXIT_FAILED and "run42" in exc.value.message
