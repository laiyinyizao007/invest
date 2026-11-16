"""`tools/check_guard_mutations.py` **自己的**守卫用例。

**为什么要有这个文件**:本仓库的教训是「新写的守卫工具自己也要有人守」
(`new-guard-tools-need-their-own-guards`)。`tools/check_guard_mutations.py`
是第 48 轮新增的守卫脚本——它决定「纪律守卫有没有失守」;
如果它自己判错了(假绿 / 假红 / 空转),那整套变异验证就是**假的绿**。

**这些用例必须便宜**:整套变异验证要跑二十多次 pytest(几分钟);
这里的用例只做**静态与局部**检查(不跑 pytest),所以能在 CI 上作为前置的秒级关卡。
真跑变异的那一步仍然由 `.github/workflows/research-guard.yml` 里的独立步骤负责。

**双向探针**(每条规则都配「放行 + 拦截」两侧):
- 锚点必须唯一命中 → 不唯一必须报错(拦截);唯一必须能落地(放行);
- 跨行锚点必须能落地 → 这正是脚本第一版的洞(按行匹配 ⇒ 跨行锚点永远命中 0 次);
- 行尾符必须原样保留 → CRLF 保持 CRLF、LF 保持 LF;
- ADR 集合核对 → 缺行必须红、**多写一行也必须红**(第 49 轮 codex H2:双向)、
  标签对不上必须红、markdown 强调号不得影响匹配;
- **归一化是逐格的,只去反引号与成对的 `**`**(第 49 轮 codex M1):标签**可以**带
  单个 `*`(例如 `except*` 就是 Python 语法的一部分)——此前全局去星号把它削成
  `except`,逼得标签绕开写(「try-star 语法」),反而把归一化的缺陷固化成了规矩。
- **每条"应该变红"的变异都必须钉一个见证用例**(第 49 轮 codex M3/M4):
  只数「红了几条」抓不住「走错原因的红」。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_guard_mutations as cgm  # noqa: E402
import check_mutations as cm  # noqa: E402


# --------------------------------------------------------------------------
# 变异清单本身:每条规格都必须能落地
# --------------------------------------------------------------------------

def test_every_mutation_label_is_unique() -> None:
    """标签重复 ⇒ ADR 表里两行同字 ⇒ 核对时无法分辨是哪一条;输出也会混淆。"""
    labels = [spec["label"] for spec in cgm.MUTATIONS]
    assert len(set(labels)) == len(labels), "变异标签不能重复"


def test_mutation_labels_may_contain_a_single_star() -> None:
    """标签**可以**带单个 `*`——`except*` 里的星号是语法的一部分。

    第 48 轮的旧规矩是「标签里不许有 `*` 或反引号」,起因是当时的归一化**全局**
    去星号 ⇒ `except*` 被削成 `except` ⇒ 标签只能绕开写(「try-star 语法」),
    **掩盖了归一化的缺陷**,还把它固化成了规矩(第 49 轮 codex M1)。

    现在归一化是**逐格**的:只去反引号与成对的 `**`,不去掉单个 `*`。
    这条用例把「标签里确实有带星号的真实标签」钉住——哪天归一化又退回全局去星号,
    它会因为「找不到带星号的标签」而变红。
    """
    starred = [spec["label"] for spec in cgm.MUTATIONS if "*" in spec["label"]]
    assert starred, (
        "一条带 `*` 的标签都没有 ⇒ 要么归一化又被改回全局去星号,"
        "要么那条 except* 变异被绕开写掉了")
    assert any("except*" in label for label in starred), (
        "标签应当写回真实的 except*(而不是绕开写):%s" % starred)


def test_every_mutation_label_matches_its_adr_row_after_normalization() -> None:
    """**两侧归一化后必须相等**——这才是那条规则真正要保证的东西。

    旧写法禁的是「标签里出现某些字符」,那是**手段**;真正要保证的是
    「脚本侧的标签与 ADR 表里那一格,归一化之后是同一个字符串」。
    手段会误伤(单星号是合法语法),目的不会。
    """
    adr = ROOT / "docs" / "adr" / "20261113-evolution-engine.md"
    cells = cm.table_rows_in_section(adr.read_text(encoding="utf-8"), "纪律守卫的变异表")
    listed = {cm.normalize_cell(cell) for cell in cells}
    for spec in cgm.MUTATIONS:
        normalized = cm.normalize_cell(spec["label"])
        assert normalized in listed, (
            "标签归一化后不在 ADR 表里:%r(ADR 表 %d 行)" % (normalized, len(cells)))


def test_every_mutation_anchor_hits_exactly_once() -> None:
    """每条变异的锚点在**当前仓库**里必须唯一命中。

    锚点漂移(改过守卫、但忘了改变异规格)会让那条变异变成**空操作**;
    空操作比没有变异更坏(它看起来「验证过了」)。所以这里静态核对一遍。
    """
    for spec in cgm.MUTATIONS:
        rel = spec["rel"]
        target = ROOT / rel
        assert target.exists(), "%s:目标文件不存在 %s" % (spec["label"], rel)
        # **按 CRLF/LF 归一后再数**(脚手架的 `_apply` 也是这么做的):
        # 锚点里含换行,而仓库里 CRLF 与 LF 都有 ⇒ 不归一就数不中。
        text = cgm._read_text(target)[0]
        for token, _new in spec["edits"]:
            hits = text.count(token)
            assert hits == 1, (
                "%s:锚点在 %s 里命中 %d 次(应为 1):%r" % (spec["label"], rel, hits, token[:80])
            )


def test_every_mutation_has_at_least_one_edit_and_a_target() -> None:
    """空编辑的变异是空操作;没有见证目标的变异不知道该跑谁。"""
    for spec in cgm.MUTATIONS:
        assert spec["edits"], "%s:变异没有编辑内容" % spec["label"]
        assert spec["targets"], "%s:变异没有见证目标" % spec["label"]
        assert spec["expect"] in ("red", "green"), "%s:expect 只能是 red/green" % spec["label"]


def test_there_is_at_least_one_harmless_control_mutation() -> None:
    """必须存在**期望保持绿**的反向对照变异。

    少了它,整套变异验证就可能「一律报红」——那等于没有判据
    (脚本自己写的文档里第 2 条防假绿要求)。
    """
    greens = [s["label"] for s in cgm.MUTATIONS if s["expect"] == "green"]
    assert greens, "必须至少有一条「期望保持绿」的反向对照变异"


# --------------------------------------------------------------------------
# `_apply`:锚点定位
# --------------------------------------------------------------------------

def test_apply_replaces_a_uniquely_matching_anchor(tmp_path: Path) -> None:
    """放行侧:锚点唯一命中 ⇒ 文件被改掉。"""
    target = tmp_path / "a.py"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    msg = cgm._apply(tmp_path, "a.py", [("beta", "BETA")])
    assert msg == "ok", msg
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\n"


def test_apply_refuses_an_anchor_that_is_missing(tmp_path: Path) -> None:
    """拦截侧:锚点命中 0 次 ⇒ 报错(而不是「猜一个改」)。

    猜一个改 = 变异变成空操作 = 空转的见证 ⇒ 比没有变异更坏。
    """
    target = tmp_path / "a.py"
    target.write_text("alpha\n", encoding="utf-8")
    msg = cgm._apply(tmp_path, "a.py", [("gamma", "GAMMA")])
    assert msg != "ok", "锚点不存在时必须报错"
    assert target.read_text(encoding="utf-8") == "alpha\n", "失败时不得改动文件"


def test_apply_refuses_an_anchor_that_is_not_unique(tmp_path: Path) -> None:
    """拦截侧:锚点命中多次 ⇒ 报错。

    命中多次时「改哪一个」是任意的 ⇒ 变异可能根本没改到想改的地方。
    """
    target = tmp_path / "a.py"
    target.write_text("alpha\nalpha\n", encoding="utf-8")
    msg = cgm._apply(tmp_path, "a.py", [("alpha", "A")])
    assert msg != "ok", "锚点不唯一时必须报错"
    assert target.read_text(encoding="utf-8") == "alpha\nalpha\n", "失败时不得改动文件"


def test_apply_supports_cross_line_anchors(tmp_path: Path) -> None:
    """放行侧:**跨行锚点**必须能落地。

    这是脚本第一版的洞:早先按**行**匹配,一个含换行的字符串永远不可能是某一行
    的子串 ⇒ 跨行锚点恒命中 0 次 ⇒ 那条变异被误判成「规格写错了」。
    本用例把「跨行也能改」钉住。
    """
    target = tmp_path / "a.py"
    target.write_text("head\nmid\ntail\n", encoding="utf-8")
    msg = cgm._apply(tmp_path, "a.py", [("mid\ntail", "MID\nTAIL")])
    assert msg == "ok", msg
    assert target.read_text(encoding="utf-8") == "head\nMID\nTAIL\n"


def test_apply_reports_a_missing_file_instead_of_raising(tmp_path: Path) -> None:
    """目标文件不存在 ⇒ 返回错误说明(不抛异常):脚本据此退出码 2(fail-closed)。"""
    msg = cgm._apply(tmp_path, "nope.py", [("x", "y")])
    assert msg != "ok"
    assert "nope.py" in msg


# --------------------------------------------------------------------------
# 行尾符:仓库里 CRLF 与 LF 都有,改写必须原样保留
# --------------------------------------------------------------------------

def test_crlf_files_stay_crlf(tmp_path: Path) -> None:
    """CRLF 文件改完之后还是 CRLF(否则 diff 全是行尾噪声,评审会淹掉真正的改动)。"""
    target = tmp_path / "a.py"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    msg = cgm._apply(tmp_path, "a.py", [("beta", "BETA")])
    assert msg == "ok", msg
    assert target.read_bytes() == b"alpha\r\nBETA\r\n"


def test_lf_files_stay_lf(tmp_path: Path) -> None:
    """LF 文件改完之后还是 LF(不许被顺手统一成 CRLF)。"""
    target = tmp_path / "a.py"
    target.write_bytes(b"alpha\nbeta\n")
    msg = cgm._apply(tmp_path, "a.py", [("beta", "BETA")])
    assert msg == "ok", msg
    assert target.read_bytes() == b"alpha\nBETA\n"


# --------------------------------------------------------------------------
# ADR 集合核对
# --------------------------------------------------------------------------

def _adr(rows: list[str], anchor: str = "纪律守卫的变异表") -> str:
    """造一份带**小节标题**的 ADR(核对只看那一节)。"""
    return ("#### 4. %s\n\n| 变异 | 改的是哪个文件 |\n|---|---|\n%s\n"
            % (anchor, "\n".join(rows)))


def test_verify_adr_accepts_a_matching_table(tmp_path: Path) -> None:
    """放行侧:表里有这一行 ⇒ 通过。"""
    adr = tmp_path / "adr.md"
    adr.write_text(_adr(["| 某条变异 | `risk/x.py` |"]), encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 0


def test_verify_adr_rejects_a_missing_row(tmp_path: Path) -> None:
    """拦截侧:表里缺这一行 ⇒ 返回 1(不能「静默跳过 ⇒ 假绿灯」)。"""
    adr = tmp_path / "adr.md"
    adr.write_text(_adr(["| 别的行 | `risk/x.py` |"]), encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 1


def test_verify_adr_rejects_an_extra_row(tmp_path: Path) -> None:
    """拦截侧:**ADR 多写一行** ⇒ 返回 1(第 49 轮 codex H2)。

    此前只查「脚本里的每一条都在表里」⇒ ADR **多写**(文档夸大)没人发现。
    文档比实现乐观,是最容易骗过后来人的一种偏差。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(_adr(["| 某条变异 | `risk/x.py` |",
                         "| 表里多出来的一行 | `risk/y.py` |"]), encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 1


def test_verify_adr_only_looks_at_its_own_section(tmp_path: Path) -> None:
    """**别的节**里的同名行不能顶替本轮的行(第 49 轮 codex H2)。

    此前扫的是整份 ADR ⇒ 另一张表里恰好有一行同字 ⇒ 核对会以为「表里有」。
    """
    adr = tmp_path / "adr.md"
    adr.write_text("#### 9. 别的节\n\n| 变异 | 说明 |\n|---|---|\n"
                    "| 某条变异 | `risk/x.py` |\n\n" + _adr(["| 别的行 | 说明 |"]),
                    encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 1


def test_verify_adr_ignores_markdown_emphasis(tmp_path: Path) -> None:
    """markdown 的强调号(成对的 `**`)不得影响匹配——ADR 表里就有 `**目录**` 这种写法。"""
    adr = tmp_path / "adr.md"
    adr.write_text(_adr(["| 某条**变异** | `risk/x.py` |"]), encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 0


def test_verify_adr_keeps_a_single_star(tmp_path: Path) -> None:
    """**单个 `*` 不得被去掉**(第 49 轮 codex M1)。

    `except*` 里的星号是 Python 语法的一部分:去掉它两侧就再也对不上。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(_adr(["| 让判定不认 except* 语法 | 说明 |"]), encoding="utf-8")
    assert cgm._verify_adr([("让判定不认 except* 语法", "red")], adr) == 0


def test_verify_adr_fails_when_the_file_is_missing(tmp_path: Path) -> None:
    """ADR 读不到 ⇒ 退出码 2(fail-closed),不是 0。"""
    assert cgm._verify_adr([("某条变异", "red")], tmp_path / "nope.md") == 2


def test_verify_adr_fails_when_the_section_is_missing(tmp_path: Path) -> None:
    """**小节找不到** ⇒ 退出码 2(fail-closed),不是 0。

    小节名改了(比如加了「第 N 轮」前缀)而脚本没跟着改 ⇒ 核对必须红,
    不能「扫不到就当通过」。
    """
    adr = tmp_path / "adr.md"
    adr.write_text("#### 4. 别的标题\n\n| 变异 | 说明 |\n|---|---|\n| 某条变异 | 说明 |\n",
                    encoding="utf-8")
    assert cgm._verify_adr([("某条变异", "red")], adr) == 2


def test_normalize_cell_only_strips_backticks_and_paired_stars() -> None:
    """归一化只去**反引号**与**成对的 `**`**;单个 `*` 与下划线都要留着。

    下划线也留着:`risk/live_admission.py` 这类路径里有它,去掉就把路径改了。
    """
    assert cm.normalize_cell("`except*`") == "except*"
    assert cm.normalize_cell("**加粗**") == "加粗"
    assert cm.normalize_cell("risk/live_admission.py") == "risk/live_admission.py"


def test_every_red_mutation_has_a_witness() -> None:
    """每条"应该变红"的变异都必须钉一个**见证用例**(第 49 轮 codex M3)。

    没有见证 ⇒ 脚本只能数「红了几条」⇒ **走错原因的红**(别的用例红了、
    或者名单过期让守卫用例本来就红)也会被算成「抓住了」——那是假绿。
    """
    for spec in cgm.MUTATIONS:
        if spec["expect"] != "red":
            continue
        assert spec["label"] in cgm.WITNESSES, (
            "这条变异没有钉见证用例:%s" % spec["label"])


def test_every_witness_points_at_a_test_that_exists() -> None:
    """见证用例必须**真的存在**——否则改名后脚手架会一直红着而无从解释。"""
    for label, witness in cgm.WITNESSES.items():
        rel, _, name = witness.partition("::")
        assert name, "见证用例的 nodeid 不完整:%s" % witness
        path = ROOT / rel
        assert path.exists(), "见证用例指向的文件不存在:%s" % witness
        source = path.read_bytes().decode("utf-8")
        assert ("def %s(" % name) in source, (
            "见证用例指向的函数不存在(改名了?):%s" % witness)


def test_the_real_adr_table_matches_the_scripts_labels() -> None:
    """端到端(但**不跑变异**):真实 ADR 表必须与脚本里的标签**逐条对上**。

    这条用例是整套流程里最便宜的一道关卡:整套变异跑完要好几分钟,
    而「标签与 ADR 对不上」在**第 0 秒**就能发现——第 48 轮那次
    (`except*` 被削成 `except`)就是这么拖到跑完全部变异才暴露的。
    """
    results = [(spec["label"], spec["expect"]) for spec in cgm.MUTATIONS]
    assert cgm._verify_adr(results) == 0, (
        "ADR 里的变异表与脚本的标签对不上——改脚本里的标签时必须同步改 ADR 那一行"
    )


# --------------------------------------------------------------------------
# 第 52 轮(codex 第 2 轮):脚手架自己的判定路径要有覆盖 + 两套脚手架共用一份实现
# --------------------------------------------------------------------------

def test_both_harnesses_share_one_outcome_classifier() -> None:
    """两个脚手架必须共用**同一份**「这次 pytest 算不算抓住」的判定。

    此前纪律守卫那套**自己写了一套**关键词启发式
    (`line.startswith("ERROR") or " errors" in line and "error" in line.lower()`),
    与分档那套的 `_run_outcome` **同名不同义**——同一份 pytest 输出,
    两个脚手架可能给出**相反**的结论。这正是仓库的教训:「同一个问题两个答案」。
    """
    assert cgm.run_outcome is cm.run_outcome


def test_both_harnesses_share_one_copy_repo() -> None:
    """仓库复制也必须**只有一份实现**。

    此前各写一份:分档那套**不复制** `.github/`,纪律守卫那套复制。
    ⇒ 同一件事两个答案 ⇒ 迟早只改一边(而 `.github` 恰恰是接线守卫要读的东西)。
    """
    assert cgm.copy_repo is cm.copy_repo


def test_the_shared_copy_repo_carries_the_ci_workflow(tmp_path: Path, monkeypatch) -> None:
    """**动态探针**:共用那份复制实现必须把 `.github/` 也带过去。

    接线守卫(`tests/test_research_boundary.py` 里那条)会**读**
    `.github/workflows/research-guard.yml`。副本里缺了它,那条守卫会
    **因为读不到文件而报错**——看起来像「变异被抓住」,其实是环境坏了(假绿)。
    """
    fake_root = tmp_path / "fake_root"
    (fake_root / ".github" / "workflows").mkdir(parents=True)
    (fake_root / ".github" / "workflows" / "research-guard.yml").write_text(
        "name: fake\n", encoding="utf-8")
    (fake_root / "tools").mkdir()
    (fake_root / "tools" / "t.py").write_text("x = 1\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(cm, "ROOT", fake_root)

    cm.copy_repo(dest)

    assert (dest / ".github" / "workflows" / "research-guard.yml").is_file(), (
        "副本里没有 CI workflow ⇒ 接线守卫读不到文件,会假绿")
    assert (dest / "tools" / "t.py").is_file()


# --------------------------------------------------------------------------
# `main()` 的判定路径(codex 第 2 轮 H2:此前**零覆盖**)
#
# 此前 `tests/test_check_guard_mutations.py` 从不调 `cgm.main()`——
# 「红/绿判定」这条路径**一次都没被跑过**。codex 的 H2 说得直接:
# 唯一的绿对照抓不住它声称要抓的退化,而没人知道,因为那条路径没人测。
#
# 这里用**极小的假仓库**(两份只有一条用例的套件)把 `main()` 真跑一遍:
#   拦截侧:明知抓不住的变异 ⇒ 必须返回 **1**(不是 0)
#   放行侧:确实把见证用例打红的变异 ⇒ 必须返回 **0**
# 两头都要有,否则这条用例自己也是空转的。
# --------------------------------------------------------------------------

def _fake_repo(dest: Path) -> None:
    """造一个**极小**的仓库副本:两份只有一条用例的套件 + 一个被改的文件。"""
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    # 钉住 rootdir,免得 pytest 往上找到别的配置文件
    (dest / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (dest / "tests" / "test_fake_boundary.py").write_text(
        "def test_witness():\n    assert True\n", encoding="utf-8")
    (dest / "tests" / "test_fake_tool.py").write_text(
        "def test_tool():\n    assert True\n", encoding="utf-8")
    (dest / "guard_target.py").write_text("VALUE = 1\n", encoding="utf-8")


def _run_main_with(monkeypatch, mutations, witnesses) -> int:
    """把脚手架的「输入」换成一组极小的假规格,再真跑一遍 `main()`。"""
    monkeypatch.setattr(cgm, "MUTATIONS", mutations)
    monkeypatch.setattr(cgm, "WITNESSES", witnesses)
    monkeypatch.setattr(cgm, "BOUNDARY", "tests/test_fake_boundary.py")
    monkeypatch.setattr(cgm, "TOOL_TEST", "tests/test_fake_tool.py")
    monkeypatch.setattr(cgm, "copy_repo", _fake_repo)
    return cgm.main([])


def test_main_returns_1_when_a_mutation_is_not_caught(monkeypatch, capsys) -> None:
    """**拦截侧**:明知抓不住的变异 ⇒ `main()` 必须返回 **1**(不是 0)。

    这条变异改的是 `guard_target.py`(一个跟见证用例无关的文件)⇒
    见证用例照常绿 ⇒ 脚手架**没有**抓住它 ⇒ 必须报"守卫有洞"并返回 1。
    """
    mutations = [{
        "label": "抓不住的变异",
        "rel": "guard_target.py",
        "edits": [("VALUE = 1", "VALUE = 2")],
        "targets": ["guard_target.py"],
        "expect": "red",
    }]
    witnesses = {"抓不住的变异": "tests/test_fake_boundary.py::test_witness"}

    assert _run_main_with(monkeypatch, mutations, witnesses) == 1
    capsys.readouterr()


def test_main_returns_0_when_the_witness_goes_red(monkeypatch, capsys) -> None:
    """**放行侧**:变异确实把见证用例打红 ⇒ `main()` 必须返回 **0**。

    没有这一条,上面那条就可能是「`main()` 一律返回 1」的假红——
    那本身也是一种空转。
    """
    mutations = [{
        "label": "抓得住的变异",
        "rel": "tests/test_fake_boundary.py",
        "edits": [("assert True", "assert False")],
        "targets": ["tests/test_fake_boundary.py"],
        "expect": "red",
    }]
    witnesses = {"抓得住的变异": "tests/test_fake_boundary.py::test_witness"}

    assert _run_main_with(monkeypatch, mutations, witnesses) == 0
    capsys.readouterr()


def test_main_refuses_when_a_red_mutation_has_no_witness(monkeypatch, capsys) -> None:
    """**放行侧(另一头)**:「应该变红」的变异没钉见证 ⇒ 退出码 **2**(环境不可用)。

    没有见证就不知道该跑谁 ⇒ 只能数「红了几条」⇒ 走错原因的红会被算成抓住了。
    这条路径此前也没被覆盖过。
    """
    mutations = [{
        "label": "没钉见证的变异",
        "rel": "guard_target.py",
        "edits": [("VALUE = 1", "VALUE = 2")],
        "targets": ["guard_target.py"],
        "expect": "red",
    }]
    monkeypatch.setattr(cgm, "MUTATIONS", mutations)
    monkeypatch.setattr(cgm, "WITNESSES", {})

    assert cgm.main([]) == 2
    capsys.readouterr()


def test_main_returns_0_when_the_green_control_stays_green(monkeypatch, capsys) -> None:
    """**绿对照侧(保持绿)**:无害改动没有弄红套件 ⇒ `main()` 必须返回 **0**。

    这是绿对照判定的**放行侧**。没有这一条,「绿对照一律判成不合格」
    (比如把 `ok` 写死成 `False`)的脚本照样能返回 0 —— 脚手架就变成了
    「一律报红」,而没人知道,因为那条路径没人测(第 52 轮 codex H2)。
    """
    mutations = [{
        "label": "无害改动(套件仍绿)",
        "rel": "guard_target.py",
        "edits": [("VALUE = 1", "VALUE = 2")],
        "targets": ["tests/test_fake_tool.py"],
        "expect": "green",
    }]

    assert _run_main_with(monkeypatch, mutations, {}) == 0
    capsys.readouterr()


def test_main_returns_1_when_the_green_control_goes_red(monkeypatch, capsys) -> None:
    """**绿对照侧(变红)**:本该无害的改动竟然弄红了套件 ⇒ `main()` 必须返回 **1**。

    这是绿对照判定的**拦截侧**。没有这一条,「绿对照一律判成合格」
    (比如把 `ok` 写死成 `True`)的脚本会把「无害改动弄红了套件」也说成通过。
    两头都要有,这条用例自己才不是空转的。
    """
    mutations = [{
        "label": "本该无害的改动(竟然弄红)",
        "rel": "tests/test_fake_tool.py",
        "edits": [("assert True", "assert False")],
        "targets": ["tests/test_fake_tool.py"],
        "expect": "green",
    }]

    assert _run_main_with(monkeypatch, mutations, {}) == 1
    capsys.readouterr()


def test_the_green_control_cannot_be_vacuous() -> None:
    """绿对照的「与基线一致」不许在**读不出通过数**时成立(第 52 轮 codex L1)。

    `_passed_count` 读不出时返回 `None`(此前返回 `-1`)⇒ `-1 == -1` 恒真
    ⇒ 「什么都没跑」会被算成「与基线一致」⇒ 绿对照什么也没证明。
    """
    assert cgm._passed_count("") is None
    assert cgm._passed_count("no summary here") is None
    assert cgm._passed_count("1 passed, 2 warnings in 0.10s") == 1
    # 旧行为返回 -1:两个 -1 相等 ⇒ 恒真
    assert -1 == -1

