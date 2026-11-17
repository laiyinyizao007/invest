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

import ast
import importlib.util
import marshal
import os
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
    """见证用例必须**真的存在**——否则改名后脚手架会一直红着而无从解释。

    **参数化的见证**(第 55 轮之五):nodeid 可以带参数段
    (`…::test_x[utf8]`)⇒ 存在性只看**函数名**那一段;
    参数 id 写错 ⇒ 脚手架跑那一轮时「见证没有红」⇒ 退出码 1
    (由 `check_guard_mutations.py` 自己兜住,这里不重复实现)。
    """
    for label, witness in cgm.WITNESSES.items():
        rel, _, name = witness.partition("::")
        assert name, "见证用例的 nodeid 不完整:%s" % witness
        path = ROOT / rel
        assert path.exists(), "见证用例指向的文件不存在:%s" % witness
        source = path.read_bytes().decode("utf-8")
        base = name.split("[", 1)[0]
        assert ("def %s(" % base) in source, (
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
    """造一个**极小**的仓库副本:三份只有一条用例的套件 + 一个被改的文件。"""
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    # 钉住 rootdir,免得 pytest 往上找到别的配置文件
    (dest / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (dest / "tests" / "test_fake_boundary.py").write_text(
        "def test_witness():\n    assert True\n", encoding="utf-8")
    (dest / "tests" / "test_fake_tool.py").write_text(
        "def test_tool():\n    assert True\n", encoding="utf-8")
    # 第 55 轮:基线里多了「接线自检自己的用例」这一份(接线那族变异的见证在那儿)
    # ⇒ 假仓库也得有它,否则基线跑不起来 ⇒ `main()` 退出码 2 ⇒ 这几条用例全红。
    (dest / "tests" / "test_fake_wiring.py").write_text(
        "def test_wiring():\n    assert True\n", encoding="utf-8")
    (dest / "guard_target.py").write_text("VALUE = 1\n", encoding="utf-8")


def _run_main_with(monkeypatch, mutations, witnesses) -> int:
    """把脚手架的「输入」换成一组极小的假规格,再真跑一遍 `main()`。"""
    monkeypatch.setattr(cgm, "MUTATIONS", mutations)
    monkeypatch.setattr(cgm, "WITNESSES", witnesses)
    monkeypatch.setattr(cgm, "BOUNDARY", "tests/test_fake_boundary.py")
    monkeypatch.setattr(cgm, "TOOL_TEST", "tests/test_fake_tool.py")
    monkeypatch.setattr(cgm, "WIRING", "tests/test_fake_wiring.py")
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


# --------------------------------------------------------------------------
# 第 55 轮之十四之四:变异被**陈旧的 .pyc** 吃掉(「见证没红」的真正成因)
# --------------------------------------------------------------------------
#
# **这一族问题的来历**:CI 上 17 条变异报了「见证没红」,而本机 155 条全绿。
#
# **机制**:Python 的 `.pyc` 按 (源文件 mtime 的**整秒**, 源文件大小) 校验新鲜度。
# **pytest 的改写缓存是同一套机制**(pytest 8.4.2
# `_pytest/assertion/rewrite.py`:`_read_pyc` 比 `int(st.st_mtime)` 与 `size`,
# `_write_pyc` 写 `int(source_stat.st_mtime) & 0xFFFFFFFF` 与 `size & 0xFFFFFFFF`)
# ⇒ **测试文件一样会中招**。(此前此处写「pytest 的改写缓存是 hash-based」
# 是**事实错误**,已按实测改正——见下。)
# 脚手架的变异多是**同大小**的小改动(`if X:` → `if False and X:`);
# 两次变异落在**同一整秒**时,第二次变异后的源文件与上一次编译出的
# 缓存头部**完全吻合** ⇒ 解释器复用**旧字节码** ⇒ 变异根本没生效
# ⇒ 见证保持绿 ⇒ 看起来像「守卫有洞」,实际是**变异空转**。
#
# **实测坐实(不是推测)**:触发要**同时**满足 (a) 变异前后**字节数相同**
# 与 (b) 两次落在**同一整秒**。158 条变异里**31 条**满足 (a)(29 条在
# `tools/check_ci_wiring.py`、1 条在 pytest 用例文件、1 条在 YAML——**YAML 不是
# Python,没有字节码缓存,天然不中招**);CI 上**恰好 17 条**同时满足 (b)
# ⇒ **CI 报错的 17 条全部落在这 31 条之内,且这 31 条之外无一条报错**。
# CI 一次变异循环只要 0.3~0.8 秒 ⇒ 常常真撞上;本机慢 ⇒ 撞不上。

def _mini_repo_with_a_value(root: Path) -> Path:
    """造一个最小的「模块 + 用例」副本,返回那个模块的路径。

    - `tests/probe_mod.py` 里有个值 `VALUE`;
    - `tests/test_check_domain.py` 断言它是 `"A"`(用例文件名用分档那套的,
      因为那套脚手架只跑固定的那一个路径)。

    脚手架跑这个用例时 `probe_mod` 会被 import ⇒ 写出 `.pyc` ⇒
    后面就能拿它做「同秒 + 同大小」的碰撞。
    """
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "probe_mod.py").write_text('VALUE = "A"\n', encoding="utf-8")
    (tests / "test_check_domain.py").write_text(
        "import probe_mod\n"
        "\n"
        "\n"
        "def test_value():\n"
        "    assert probe_mod.VALUE == \"A\"\n",
        encoding="utf-8",
    )
    return tests / "probe_mod.py"


def _failed_and_output(result) -> tuple[int, str]:
    """两个脚手架的 `_run_tests` 返回**两种形状**(字典 / 元组)⇒ 这里统一成
    (失败条数, 输出文本),免得探针自己分叉成两套口径。"""
    if isinstance(result, dict):
        return int(result["failed"]), str(result["output"])
    return int(result[0]), str(result[1])


def _stamp_of(pyc: Path) -> tuple[int, int]:
    """读 `.pyc` 头里记的 (整秒 mtime, 源文件大小)。"""
    head = pyc.read_bytes()[:16]
    assert len(head) >= 16, "`.pyc` 头不足 16 字节:不是标准的时间戳型缓存"
    flags = int.from_bytes(head[4:8], "little")
    assert flags == 0, "这个解释器用的是 hash 型 `.pyc`(按内容哈希校验)⇒ 本族问题不成立"
    return int.from_bytes(head[8:12], "little"), int.from_bytes(head[12:16], "little")


def test_running_the_suite_clears_the_bytecode_cache_before_it_runs(tmp_path) -> None:
    """**放行侧**:两个脚手架跑用例之前,都必须把副本里的字节码缓存清掉。

    少了这一步,「同秒 + 同大小」的变异会被陈旧的 `.pyc` 顶掉 ⇒ 变异空转。
    这里直接**摆一个假的 `.pyc` 在副本里**,跑一次之后它必须消失。
    """
    planted = tmp_path / "pkg" / "__pycache__" / "planted.cpython-312.pyc"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"\x00" * 16)

    # 分档那套:跑固定的那一个测试路径
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_check_domain.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    cm._run_tests(tmp_path)
    assert not planted.exists(), "分档那套跑用例之前没有清掉字节码缓存"

    # 纪律那套:跑指定的用例
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"\x00" * 16)
    cgm._run_tests(tmp_path, ["tests/test_check_domain.py"])
    assert not planted.exists(), "纪律那套跑用例之前没有清掉字节码缓存"


def _same_second_mutation_must_be_seen(run, root: Path, name: str) -> None:
    """同秒 + 同大小的改动,必须照样被跑用例的那一侧看见。

    这是 CI 上「17 条见证没红」的**原样复现**:
    第一次跑 ⇒ 模块被 import ⇒ 写出 `.pyc`;
    接着把模块改成**同样大小**的内容、mtime 设成**同一整秒**
    ⇒ 若不清缓存,Python 会复用**旧字节码**,用例仍然绿(变异空转);
    ⇒ 所以「第二次跑必须看到那次改动」——用例必须**变红**。
    """
    mod = _mini_repo_with_a_value(root)

    first, first_out = run(root)
    assert first == 0, "%s:第一次跑就红了,前提不成立:%s" % (name, first_out[-500:])

    pycs = list((mod.parent / "__pycache__").glob("probe_mod.*.pyc"))
    assert pycs, ("%s:副本里没写出 `.pyc` ⇒ 这条探针的前提不成立"
                  "(若环境里有 `PYTHONDONTWRITEBYTECODE` 或用了 `-B`,"
                  "解释器压根不写缓存 ⇒ 是**环境配置**问题,不是「变异被吃」)" % name)
    # **不能随便取第一个**(第 55 轮之十四之五,codex L4):
    # 目录里若同时存在时间戳型与 hash 型两个变体,`[0]` 的顺序取决于文件系统
    # ⇒ 可能取到 hash 型那条 ⇒ `flags == 0` 的断言报出与实际情形不符的解释。
    # 这里**显式挑出时间戳型**(flags == 0)的那一条,挑不出就当场报错。
    stamp_pyc = None
    for cand in sorted(pycs):
        head = cand.read_bytes()[:16]
        if len(head) >= 16 and int.from_bytes(head[4:8], "little") == 0:
            stamp_pyc = cand
            break
    assert stamp_pyc is not None, (
        "%s:副本里没有**时间戳型** `.pyc`(只有 %d 个: %s)⇒ 这条探针的前提不成立"
        % (name, len(pycs), [p.name for p in sorted(pycs)]))
    stamp, size = _stamp_of(stamp_pyc)

    # **同样大小** + **同一整秒** ⇒ `.pyc` 会被当成新鲜的
    mod.write_text('VALUE = "B"\n', encoding="utf-8")
    assert mod.stat().st_size == size, "改动没有保持同样大小(那就不是同一个陷阱)"
    os.utime(mod, (stamp, stamp))

    second, second_out = run(root)
    assert second >= 1, (
        "%s:同秒 + 同大小的改动被**陈旧的 `.pyc`** 吃掉了 ⇒ 变异空转 ⇒ "
        "见证会保持绿(假绿)" % name)


def test_the_discipline_harness_sees_a_same_second_mutation(tmp_path) -> None:
    """**拦截侧(纪律那套)**:同秒 + 同大小的改动,纪律脚手架必须看见。

    **与分档那套分成两条用例**(不共用一条):两条变异各有**专属**见证 ⇒
    不会「走错原因的红」(仓库教训:变异见证必须指向唯一那一条规则)。
    """
    root = tmp_path / "纪律"
    root.mkdir()
    _same_second_mutation_must_be_seen(
        lambda r: _failed_and_output(
            cgm._run_tests(r, ["tests/test_check_domain.py"])),
        root, "纪律那套")


def test_the_domain_harness_sees_a_same_second_mutation(tmp_path) -> None:
    """**拦截侧(分档那套)**:同秒 + 同大小的改动,分档脚手架必须看见。

    分档那套的变异**全部**作用在 `tools/check_domain.py`(普通 import
    ⇒ 时间戳型 `.pyc`)⇒ 它才是本轮 17 条中招的那一族。
    """
    root = tmp_path / "分档"
    root.mkdir()
    _same_second_mutation_must_be_seen(
        lambda r: _failed_and_output(cm._run_tests(r)),
        root, "分档那套")


# --------------------------------------------------------------------------
# 第 55 轮之十四之五:codex 第 2 轮 Low-1~Low-4 —— 清理动作本身的健壮性
# --------------------------------------------------------------------------
#
# **为什么这一族也要有探针**:清理字节码缓存是**本轮修复的支点**
# ⇒ 它自己若「静悄悄地失败」(删不掉的没说、目录读不了直接崩、
# 环境变量让解释器压根不写缓存),本轮修的坑就会**以更隐蔽的方式回来**。
# 仓库规矩:**每条新规则都配双向探针 + 一条能变红的变异**。

def test_the_purge_reports_a_cache_file_it_cannot_delete(tmp_path, monkeypatch, capsys) -> None:
    """**删不掉**的字节码缓存必须**出声**(codex Low-1)。

    此前 `except OSError: pass` 静默吞掉 ⇒ 缓存没清掉 ⇒ 那一轮变异可能又被吃
    ⇒ 脚手架把「变异空转」**误报成「守卫有洞」**——正是本轮要根治的故障,
    却以更隐蔽的方式回归。删不掉时**至少要能在日志里看见**。
    """
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    pyc = cache / "x.cpython-312.pyc"
    pyc.write_bytes(b"\x00" * 16)

    real_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if self.name == "x.cpython-312.pyc":
            raise OSError("模拟:文件被别的进程锁住")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    removed = cm._purge_bytecode_cache(tmp_path)
    err = capsys.readouterr().err
    assert removed == 0, "删不掉就不该算成删掉了(返回值必须如实)"
    assert "x.cpython-312.pyc" in err, (
        "删不掉的字节码缓存**没有出声** ⇒ 防线可能无声失效(日志里看不到):%r" % err)


def test_the_purge_survives_an_unreadable_directory(tmp_path, monkeypatch, capsys) -> None:
    """**目录读不了**时,清理不能整体崩(codex Low-2)。

    `rglob` 在迭代中途抛 `PermissionError` ⇒ 脚手架以未处理 traceback 崩掉,
    错误形态与「规格坏了 / 守卫有洞」完全不一样 ⇒ 排障会绕路。
    这里要求:不崩、**出声**、如实返回。
    """
    real_rglob = Path.rglob

    def fake_rglob(self, pattern):
        if self == tmp_path:
            raise OSError("模拟:目录读不了")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", fake_rglob)
    removed = cm._purge_bytecode_cache(tmp_path)
    err = capsys.readouterr().err
    assert removed == 0, "没删掉任何东西时返回数必须是 0"
    assert "不完整" in err, (
        "目录读不了时**没有出声** ⇒ 排障时看不到清理其实没做:%r" % err)


def _bytecode_written_by_the_child(root: Path) -> bool:
    """副本里有没有 `probe_mod` 的字节码缓存(= 子进程真的写了 `.pyc`)。"""
    return bool(list((root / "tests" / "__pycache__").glob("probe_mod.*.pyc")))


def test_the_domain_harness_writes_bytecode_even_when_the_environment_forbids_it(
        tmp_path, monkeypatch) -> None:
    """**分档那套**必须摘掉 `PYTHONDONTWRITEBYTECODE`(codex Low-3)。

    环境里若有这个变量(或用 `-B` 起解释器),解释器**根本不写 `.pyc`**
    ⇒ 拦截侧探针的「第一次跑会写出 `.pyc`」前提不成立 ⇒ 探针**假红**
    ⇒ 把「环境配置」误报成「变异被吃」。
    """
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "分档"
    root.mkdir()
    _mini_repo_with_a_value(root)
    cm._run_tests(root)
    assert _bytecode_written_by_the_child(root), (
        "环境里明明有 `PYTHONDONTWRITEBYTECODE=1`,分档那套却**没有**摘掉它"
        "⇒ 子解释器没写 `.pyc` ⇒ 拦截侧探针会假红")


def test_the_discipline_harness_writes_bytecode_even_when_the_environment_forbids_it(
        tmp_path, monkeypatch) -> None:
    """**纪律那套**必须摘掉 `PYTHONDONTWRITEBYTECODE`(codex Low-3)。

    与分档那套**分成两条用例**:两条变异各有**专属**见证 ⇒ 不会「走错原因的红」。
    """
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "纪律"
    root.mkdir()
    _mini_repo_with_a_value(root)
    cgm._run_tests(root, ["tests/test_check_domain.py"])
    assert _bytecode_written_by_the_child(root), (
        "环境里明明有 `PYTHONDONTWRITEBYTECODE=1`,纪律那套却**没有**摘掉它"
        "⇒ 子解释器没写 `.pyc` ⇒ 拦截侧探针会假红")
# --------------------------------------------------------------------------
# 第 55 轮之十四之六:「字节码缓存陈旧」这条**诊断**自己也要有人守
# --------------------------------------------------------------------------
#
# **为什么这一段是新的**:`_probe_stale_pyc` 与 `_fingerprint` 此前**一条用例都没有**
# ——诊断代码本身也是死代码(仓库教训:诊断必须自己验)。
# 而且它还在第 55 轮之十四之六撞上了宪法层:本模块若直接写 `compile(...)`,
# 宪法层就判它「会执行候选代码却没走第四层」⇒ 已改为**子解释器编译**
# (见 `tools/check_guard_mutations.py::_CHILD_COMPILE_PROGRAM` 的说明)
# ⇒ 这里补上**反向探针**:两个脚手架**一个编译/执行原语都不许持有**。

_EXECUTOR_NAMES = ("exec", "eval", "compile", "safe_builtins", "import_candidate")
_EXECUTOR_ATTRS = ("eval", "compile", "import_module", "run_path", "run_module")
_SAFE_ATTR_RECEIVERS = ("re",)


def _executor_calls(tree: ast.AST) -> list[str]:
    """这个模块里所有「编译/执行原语」的调用点(判据与宪法层一致)。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _EXECUTOR_NAMES:
                found.append("裸名 %s(第 %d 行)" % (func.id, node.lineno))
        elif isinstance(func, ast.Attribute):
            if func.attr not in _EXECUTOR_ATTRS:
                continue
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in _SAFE_ATTR_RECEIVERS:
                continue
            found.append("属性形式 %s(第 %d 行)" % (func.attr, node.lineno))
    return found


def test_the_mutation_harnesses_hold_no_compiler_or_execution_primitive() -> None:
    """**反向探针**:两个变异脚手架**一个编译/执行原语都不许持有**。

    宪法层(`tools/check_constitution_history.py`)按「模块里有没有编译/执行原语」
    判定「谁执行候选代码」⇒ 谁执行候选谁必须走第四层。
    这两个脚手架**从不执行候选代码** ⇒ 若它们持有原语,宪法层会判它们「裸奔」,
    而处置办法只有两条坏路(给不执行候选的工具硬凑第四层 / 去动宪法层的清单)。

    判据与宪法层**同一套**(裸名 exec/eval/compile/safe_builtins/import_candidate
    + 属性形式 eval/compile/import_module/run_path/run_module;`re.compile` 除外)。
    """
    for rel in ("tools/check_guard_mutations.py", "tools/check_mutations.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        calls = _executor_calls(tree)
        assert not calls, (
            "%s 里出现了编译/执行原语 %s ⇒ 宪法层会判它「会执行候选代码却没走第四层」"
            "⇒ 编译请交给一次性子解释器" % (rel, calls))


def test_the_child_compiler_produces_the_same_code_object_as_a_local_compile(tmp_path) -> None:
    """**子解释器编译路径本身要有用**:它给出的代码对象与进程内编译的**等价**。

    否则诊断会**永远**落到「查不了」⇒ 排障时看不出「缓存是新的」还是「旧的」
    ⇒ 等于把诊断废掉(而它是「变异空转 vs 守卫有洞」的判别依据之一)。
    """
    src = tmp_path / "probe_mod.py"
    src.write_text("VALUE = 7\n", encoding="utf-8")
    code, err = cgm._compile_current_source(src)
    assert code is not None, "子解释器编译失败:%s" % err
    assert cgm._fingerprint(code) == cgm._fingerprint(compile(src.read_text(encoding="utf-8"),
                                                             str(src), "exec"))


def test_the_child_compiler_reports_a_broken_source_instead_of_pretending(tmp_path) -> None:
    """源文件编译不了 ⇒ 返回 `(None, 错误说明)`,**不许**装作成功。"""
    src = tmp_path / "broken.py"
    src.write_text("def (\n", encoding="utf-8")
    code, err = cgm._compile_current_source(src)
    assert code is None
    assert err.strip(), "编译失败必须给出原因"


def _write_timestamp_pyc(pyc: Path, source_text: str, mtime: int, size: int) -> None:
    """造一个**时间戳型** `.pyc`:头里写 (mtime, size),正文是 `source_text` 编译出来的。

    头里的 (mtime, size) 与源文件**一致** ⇒ Python 会**复用**它(陷阱的前提)。
    正文来自**另一份源码** ⇒ 复用它就等于「旧字节码被复用」。
    """
    code = compile(source_text, "probe_mod.py", "exec")
    header = (importlib.util.MAGIC_NUMBER
              + (0).to_bytes(4, "little")
              + mtime.to_bytes(4, "little")
              + size.to_bytes(4, "little"))
    pyc.write_bytes(header + marshal.dumps(code))


def _mini_module_with_cache(tmp_path: Path, cached_source: str) -> tuple[Path, Path]:
    """造一个「源文件 + 与它 (mtime, size) 一致的 `.pyc`」的现场。

    源文件与缓存**正文不同**但**字节数相同** ⇒ 这正是「同秒同大小」的陷阱。
    """
    src = tmp_path / "probe_mod.py"
    text = "VALUE = 1\n"
    src.write_text(text, encoding="utf-8")
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    st = src.stat()
    _write_timestamp_pyc(cache_dir / "probe_mod.cpython-313.pyc", cached_source,
                         int(st.st_mtime), st.st_size)
    return src, cache_dir


def test_the_probe_reports_a_reused_stale_bytecode(tmp_path) -> None:
    """**拦截侧**:缓存里的代码与当前源文件**不一致** ⇒ 报「旧字节码被复用」。

    这是「时间戳一致不足以断定缓存是新的」那条判据的正面用例:
    头里的 (mtime, size) 与源文件**完全一致**(陷阱的前提),
    只有**比内容**才分得出「旧的」与「新的」。
    """
    src, _ = _mini_module_with_cache(tmp_path, "VALUE = 2\n")
    out = "\n".join(cgm._probe_stale_pyc(tmp_path, src))
    assert "旧字节码被复用" in out, out
    assert "缓存是新的" not in out, out


def test_the_probe_reports_a_fresh_cache_when_the_code_matches(tmp_path) -> None:
    """**放行侧**:缓存里的代码与当前源文件**一致** ⇒ 报「缓存是新的」。

    没有这一侧,上一条会**恒红**:诊断若只会说「旧字节码被复用」,
    那它在缓存其实是新的场景下就会**误报**(把排障引向错误的成因)。
    """
    src, _ = _mini_module_with_cache(tmp_path, "VALUE = 1\n")
    out = "\n".join(cgm._probe_stale_pyc(tmp_path, src))
    assert "缓存是新的" in out, out
    assert "旧字节码被复用" not in out, out


def test_the_probe_says_the_timestamp_mismatch_is_not_a_trap(tmp_path) -> None:
    """头里的 (mtime, size) 与源文件**对不上** ⇒ Python 会重新编译 ⇒ 不是陷阱。"""
    src, cache_dir = _mini_module_with_cache(tmp_path, "VALUE = 2\n")
    _write_timestamp_pyc(cache_dir / "probe_mod.cpython-313.pyc", "VALUE = 2\n",
                         1, 999999)
    out = "\n".join(cgm._probe_stale_pyc(tmp_path, src))
    assert "时间戳不一致" in out, out
    assert "旧字节码被复用" not in out, out


def test_the_probe_says_it_cannot_tell_when_the_source_cannot_be_compiled(tmp_path) -> None:
    """源文件编译不了 ⇒ 诊断必须说「**查不了**」,**不许**说「缓存是新的」。

    「查不了」与「干净的结论」是两件事:混起来会让排障的人以为已经排除了缓存这条路径。
    """
    src, _ = _mini_module_with_cache(tmp_path, "VALUE = 2\n")
    src.write_text("def (\n", encoding="utf-8")
    out = "\n".join(cgm._probe_stale_pyc(tmp_path, src))
    assert "查不了" in out, out
    assert "缓存是新的" not in out, out


def test_the_probe_says_so_when_there_is_no_cache_at_all(tmp_path) -> None:
    """没有 `__pycache__` ⇒ 说清楚「第一次跑就会重新编译」,而不是安静地什么都不说。"""
    src = tmp_path / "probe_mod.py"
    src.write_text("VALUE = 1\n", encoding="utf-8")
    out = "\n".join(cgm._probe_stale_pyc(tmp_path, src))
    assert "__pycache__" in out, out
