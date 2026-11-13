"""国信测试报告 docx 填写器与证据模型/渲染的离线测试(用现场构造的迷你模板,真实模板不入库)。"""
import struct
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parent.parent

import broker_evidence as be  # noqa: E402
import broker_report as br  # noqa: E402
from docx_fixture import build_template  # noqa: E402

NS = {"w": br.W_NS}


def evidences(**overrides):
    base = {}
    for item in be.ITEMS:
        base[item] = be.Evidence(item, be.PASS, "", "2026-11-11 10:00:00", [be.Shot("", ["line"])])
    base["P1.3"] = be.Evidence("P1.3", be.PASS, "", "2026-11-11 10:00:00", [be.Shot("200笔", ["line"])], {"orders": 200, "elapsed_seconds": 37.4})
    base.update(overrides)
    return base


def ctx(**kw):
    defaults = dict(system_name="invest", version="1.0", app_id="client_invest_1.0", account="12345678",
                    submitter_name="测试员乙", contact_name="联系人乙", contact_phone="13800000000", contact_email="a@b.c",
                    machine="Dell XPS", os_name="Windows 11", cpu="Ultra 9", memory="32 GB", disk="NVMe 954GB",
                    test_time="2026-11-11", report_date=datetime(2026, 11, 11))
    defaults.update(kw)
    return br.ReportContext(**defaults)


def render_images(tmp_path, evs):
    out = tmp_path / "evidence"
    be.render_all(list(evs.values()), out)
    be.save_evidence(out, list(evs.values()))
    return out


def build(tmp_path, evs, **ctx_kw):
    template = build_template(tmp_path / "template.docx")
    out_dir = render_images(tmp_path, evs)
    outcome = br.build_report(template, out_dir, tmp_path / "out.docx", ctx(**ctx_kw), evs)
    return outcome, br.Docx(outcome.path)


def table_with(doc, label):
    for tbl in doc.body.findall(br.TBL):
        for tr in tbl.findall(br.TR):
            if any(br.text_of(tc).strip().startswith(label) for tc in tr.findall(br.TC)):
                return tbl
    raise AssertionError(label)


def checked_labels(tc):
    """单元格里被勾选的选项文字(sym 0052 或字面 ☑)。"""
    opts, current = [], None
    for el in tc.iter():
        if el.tag == br.SYM:
            current = {"on": el.get(br.w("char")) == br.CHECKED, "label": ""}
            opts.append(current)
        elif el.tag == br.T and current is not None:
            current["label"] += el.text or ""
    return [o["label"].strip() for o in opts if o["on"]]


# ---------- set_checks ----------

def make_cell(xml_inner):
    return etree.fromstring(f'<w:tc xmlns:w="{br.W_NS}">{xml_inner}</w:tc>')


def test_set_checks_exact_match_beats_prefix():
    from docx_fixture import opts
    tc = make_cell("<w:p>" + "".join(opts(None, "CTP", "miniCTP", "CTPⅡ mini")) + "</w:p>")
    labels = br.set_checks(tc, {"CTP"})
    assert labels == ["CTP", "miniCTP", "CTPⅡ mini"] and checked_labels(tc) == ["CTP"]


def test_set_checks_resets_preticked_and_handles_literal_boxes():
    from docx_fixture import run, sym
    tc = make_cell("<w:p>" + run("□自动开平仓     □止盈止损   ") + sym("0052") + run("多账户管理   ") + run("□二次开发") + "</w:p>")
    br.set_checks(tc, {"自动开平仓", "二次开发"})
    text = br.text_of(tc)
    assert "☑自动开平仓" in text and "□止盈止损" in text and "☑二次开发" in text
    assert checked_labels(tc) == []                                  # 预勾选的符号选项已被重置


def test_set_checks_prefix_fallback_for_labels_with_suffix():
    from docx_fixture import opts
    tc = make_cell("<w:p>" + "".join(opts(None, "测试软件", "其他（ 截图 ）")) + "</w:p>")
    br.set_checks(tc, {"其他"})
    assert checked_labels(tc) == ["其他（ 截图 ）"]


# ---------- 整份报告 ----------

def conclusion_rows(doc):
    """结果表里的两个「测试结论」行:(外部接入, 程序化补充)。"""
    results = table_with(doc, "1.1")
    rows = [tr.findall(br.TC) for tr in results.findall(br.TR) if br.text_of(tr.findall(br.TC)[0]).strip().startswith("测试结论")]
    assert len(rows) == 2
    return rows


def test_report_fills_fields_and_checks_results(tmp_path):
    outcome, doc = build(tmp_path, evidences())
    text = br.text_of(doc.body)
    assert "——invest系统" in text and "2026年11月11日" in text and "XXXX" not in text
    summary = [t for t in doc.body.findall(br.TBL) if br.find_row(t, "提测方")][0]
    assert br.text_of(br.find_row(summary, "测试系统名称")[1][1]) == "invest" and br.text_of(br.find_row(summary, "测试系统名称")[1][3]) == "1.0"
    assert br.text_of(br.find_row(summary, "AppID/RelayAppID")[1][1]) == "client_invest_1.0"
    assert br.text_of(br.find_row(summary, "测试账号")[1][1]) == "12345678"
    assert checked_labels(br.find_row(summary, "连接模式")[1][1]) == ["直连的交易者"]
    assert checked_labels(br.find_row(summary, "交易信息系统名称")[1][1]) == ["CTP"]
    functions = br.text_of(br.find_row(summary, "测试系统功能")[1][1])
    assert "☑止盈止损" in functions and "□批量下单" in functions
    assert br.text_of(br.find_row(summary, "联系人")[1][2]) == "联系人乙"
    phone_row = br.find_row(summary, "电话")[1]
    assert br.text_of(phone_row[2]) == "13800000000" and br.text_of(phone_row[4]) == "a@b.c"
    assert br.text_of(br.find_row(summary, "测试系统设备")[1][1]).startswith("测试机型号:Dell XPS")
    results = table_with(doc, "1.1")
    for prefix in ("1.1", "1.2", "2.1", "3.1", "3.2", "4.1", "5.1", "5.2", "6.1", "7.1"):
        assert checked_labels(br.find_row(results, prefix)[1][2]) == ["通过"], prefix
    assert checked_labels(br.find_row(results, "3.3")[1][2]) == ["通过"]      # 选测项取证为 PASS 则勾通过
    external, programmatic = conclusion_rows(doc)
    assert checked_labels(external[1]) == ["通过"] and checked_labels(programmatic[1]) == ["通过"]
    assert outcome.warnings == []


def test_new_numbering_maps_each_item_to_its_own_row_not_the_old_numbers(tmp_path):
    """旧版把「连接监测」编成 2.1;新模板 2.1 是「稳定运行」、连接监测是 3.1——只有 P2.1(连接监测)失败时,必须是 3.1 行变「不通过」,2.1 不受影响。"""
    evs = evidences(**{"P2.1": be.Evidence("P2.1", be.FAIL, "没有检测到断开", "t", [be.Shot("", ["x"])])})
    _, doc = build(tmp_path, evs)
    results = table_with(doc, "1.1")
    assert checked_labels(br.find_row(results, "3.1")[1][2]) == ["不通过"]
    assert checked_labels(br.find_row(results, "2.1")[1][2]) == ["通过"]
    external, programmatic = conclusion_rows(doc)
    assert checked_labels(external[1]) == ["通过"] and checked_labels(programmatic[1]) == []


def test_stability_seconds_are_filled_from_metrics_and_image_follows_the_200_line(tmp_path):
    _, doc = build(tmp_path, evidences())
    paras = [el for el in doc.body if el.tag == br.P]
    texts = ["DRAW" if el.find(f".//{{{br.W_NS}}}drawing") is not None else br.text_of(el).strip() for el in paras]
    assert any(t.startswith("2）持续下单200笔，耗时约 37.4 秒") for t in texts)
    assert any(t.startswith("1）持续下单50笔，耗时约     秒") for t in texts)      # 50 笔是非程序化系统的项,不填
    i = next(n for n, t in enumerate(texts) if t.startswith("2）持续下单200笔"))
    assert "DRAW" in texts[i + 1:i + 3]


def test_stability_without_metrics_is_reported_not_guessed(tmp_path):
    evs = evidences()
    evs["P1.3"] = be.Evidence("P1.3", be.PASS, "", "t", [be.Shot("200笔", ["x"])])
    outcome, doc = build(tmp_path, evs)
    assert "耗时约 N 秒" in "\n".join(outcome.missing)
    assert any(br.text_of(el).startswith("2）持续下单200笔，耗时约     秒") for el in doc.body if el.tag == br.P)


def test_gray_hint_color_is_not_copied_into_filled_text(tmp_path):
    _, doc = build(tmp_path, evidences())
    summary = [t for t in doc.body.findall(br.TBL) if br.find_row(t, "提测方")][0]
    cell = br.find_row(summary, "AppID/RelayAppID")[1][1]
    assert cell.find(".//w:color", NS) is None and br.text_of(cell) == "client_invest_1.0"


def test_never_defaults_to_pass_failed_or_skipped_mandatory_items_stay_unchecked(tmp_path):
    evs = evidences(**{
        "P1.1": be.Evidence("P1.1", be.FAIL, "授权失败", "t", [be.Shot("", ["x"])]),
        "P3.1": be.Evidence("P3.1", be.SKIP, "需交易时段", "t", [be.Shot("", ["x"])]),
        "P2.3": be.Evidence("P2.3", be.SKIP, "选测", "t", [be.Shot("", ["x"])]),
        "P5.2": be.Evidence("P5.2", be.SKIP, "未实现", "t", [be.Shot("", ["x"])]),
    })
    del evs["P4.2"]                                                         # 完全没有取证结果
    outcome, doc = build(tmp_path, evs)
    results = table_with(doc, "1.1")
    assert checked_labels(br.find_row(results, "1.1")[1][2]) == ["不通过"]
    assert checked_labels(br.find_row(results, "4.1")[1][2]) == []         # 必测项 SKIP:不勾,模板预勾选的「通过」被重置
    assert checked_labels(br.find_row(results, "5.2")[1][2]) == []
    assert checked_labels(br.find_row(results, "3.3")[1][2]) == ["未测试"]  # 选测项 SKIP → 未测试
    assert checked_labels(br.find_row(results, "6.2")[1][2]) == ["未测试"]
    external, programmatic = conclusion_rows(doc)
    assert checked_labels(external[1]) == [] and checked_labels(programmatic[1]) == []     # 必测项未全过 → 不勾结论
    joined = "\n".join(outcome.missing)
    assert "1.1 连通性" in joined and "4.1 阈值设置" in joined and "5.2 接收柜台错误提示" in joined and "必测项未全部通过" in joined
    assert "签字/盖章" in joined


def test_template_preticked_collection_rows_are_reset_not_defaulted_to_pass(tmp_path):
    """「看穿式采集」两行模板预勾了「通过」:是否通过由国信评估,脚本一律重置并在缺失清单里提示。"""
    outcome, doc = build(tmp_path, evidences())
    results = table_with(doc, "1.1")
    assert checked_labels(br.find_row(results, "采集信息的完整性")[1][2]) == []
    assert checked_labels(br.find_row(results, "采集信息的正确性")[1][2]) == []
    assert "看穿式采集" in "\n".join(outcome.missing)


def test_guosen_filled_areas_are_untouched(tmp_path):
    template = build_template(tmp_path / "t.docx")
    before = br.Docx(template)
    gx_before = etree.tostring(table_with(before, "技术评估意见"))
    gx_final_before = [etree.tostring(tr) for tr in table_with(before, "测试结论").findall(br.TR)
                       if br.text_of(tr).startswith("测试人员(国信期货)")]
    _, doc = build(tmp_path, evidences())
    assert etree.tostring(table_with(doc, "技术评估意见")) == gx_before
    assert [etree.tostring(tr) for tr in table_with(doc, "测试结论").findall(br.TR)
            if br.text_of(tr).startswith("测试人员(国信期货)")] == gx_final_before


def test_images_are_embedded_with_relationships_and_content_type(tmp_path):
    outcome, doc = build(tmp_path, evidences())
    with zipfile.ZipFile(outcome.path) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        content_types = z.read("[Content_Types].xml").decode("utf-8")
        assert z.testzip() is None
    assert media and all(f"media/{Path(n).name}" in rels for n in media)
    assert 'Extension="png"' in content_types
    assert len(doc.body.findall(f".//{{{br.W_NS}}}drawing")) == len(media)


def test_images_follow_their_anchor_labels(tmp_path):
    evs = evidences()
    evs["P1.2"] = be.Evidence("P1.2", be.PASS, "", "t", [be.Shot("开仓", ["o"]), be.Shot("平仓", ["c"]), be.Shot("撤单", ["x"])])
    _, doc = build(tmp_path, evs)
    paras = [el for el in doc.body if el.tag == br.P]
    texts = ["DRAW" if el.find(f".//{{{br.W_NS}}}drawing") is not None else br.text_of(el).strip() for el in paras]
    start = texts.index("1.2.基础交易功能（包含开仓、平仓、撤单）")
    window = texts[start:start + 10]
    assert window.index("1）开仓") + 1 == window.index("DRAW")                  # 图紧跟各自的小标题之后
    assert window.index("2）平仓") < window.index("3）撤单")
    assert window.count("DRAW") == 3


def test_every_item_with_a_shot_lands_in_its_own_section(tmp_path):
    """每个测试项的图都落在模板里自己编号的小节下(旧编号映射会把图插进别的小节)。"""
    evs = {k: be.Evidence(k, be.PASS, "", "t", [be.Shot("", [k])], {"orders": 200, "elapsed_seconds": 1}) for k in br.TEMPLATE_NO}
    _, doc = build(tmp_path, evs)
    paras = [el for el in doc.body if el.tag == br.P]
    current, seen = None, {}
    for el in paras:
        t = br.text_of(el).strip()
        m = br.HEADING.match(t)
        if m and f"{m.group(1)}.{m.group(2)}" in br.SECTION_NO:
            current = f"{m.group(1)}.{m.group(2)}"
        if el.find(f".//{{{br.W_NS}}}drawing") is not None and current:
            seen[current] = seen.get(current, 0) + 1
    assert seen == {no: 1 for no in br.TEMPLATE_NO.values()}


def test_missing_context_values_are_reported_not_silently_blank(tmp_path):
    outcome, _ = build(tmp_path, evidences(), app_id="", contact_phone="")
    joined = "\n".join(outcome.missing)
    assert "AppID 没有提供" in joined and "联系电话 没有提供" in joined


def test_unrecognized_template_fails_loudly(tmp_path):
    bad = tmp_path / "bad.docx"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", f'<w:document xmlns:w="{br.W_NS}"><w:body><w:p/></w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    with pytest.raises(ValueError, match="程序化补充测试报告"):
        br.build_report(bad, tmp_path, tmp_path / "o.docx", ctx(), evidences())


def test_old_layout_template_is_rejected_instead_of_filled_with_new_numbers(tmp_path):
    """旧版模板(第二封面叫「程序化测试报告」、编号不同)必须明确拒绝,不能按新编号错填。"""
    old = build_template(tmp_path / "old.docx")
    with zipfile.ZipFile(old) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["word/document.xml"] = parts["word/document.xml"].replace("程序化补充测试报告".encode(), "程序化测试报告".encode())
    with zipfile.ZipFile(old, "w") as z:
        for n, data in parts.items():
            z.writestr(n, data)
    with pytest.raises(ValueError, match="20260717"):
        br.build_report(old, tmp_path, tmp_path / "o.docx", ctx(), evidences())


def test_report_cli_needs_template_and_evidence(tmp_path, capsys):
    assert br.main(["--evidence", str(tmp_path), "--template", str(tmp_path / "nope.docx")]) == 2
    template = build_template(tmp_path / "t.docx")
    assert br.main(["--evidence", str(tmp_path), "--template", str(template)]) == 2
    assert "evidence.json" in capsys.readouterr().err


def test_report_cli_end_to_end(tmp_path, capsys, monkeypatch):
    template = build_template(tmp_path / "t.docx")
    out_dir = render_images(tmp_path, evidences())
    monkeypatch.setenv("BROKER_TEST_SYSTEM_NAME", "invest")
    monkeypatch.setenv("CTP_APP_ID", "client_invest_1.0")
    out = tmp_path / "final.docx"
    assert br.main(["--evidence", str(out_dir), "--template", str(template), "--out", str(out), "--no-terminal-info"]) == 0
    assert out.exists() and "报告里还缺什么" in capsys.readouterr().out


def test_context_from_env_reads_broker_test_settings():
    env = {"BROKER_TEST_SYSTEM_NAME": "invest", "BROKER_TEST_VERSION": "1.0", "CTP_APP_ID": "client_invest_1.0", "CTP_USERNAME": "u",
           "BROKER_TEST_FUNCTIONS": "自动开平仓, 止盈止损"}
    info = {"machine": {"manufacturer": "Dell", "model": "XPS", "memory_gb": 32}, "cpu": [{"name": "Ultra 9"}],
            "disks": [{"model": "NVMe", "size_gb": 954}], "os": {"name": "Windows 11", "version": "10.0"}}
    c = br.context_from_env(env, info)
    assert c.app_id == "client_invest_1.0" and c.functions == ("自动开平仓", "止盈止损") and c.memory == "32 GB"
    assert c.machine == "Dell XPS" and c.cpu == "Ultra 9" and "954GB" in c.disk


# ---------- 证据模型与渲染 ----------

def test_render_shot_produces_a_valid_png_even_without_cjk_font(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "FONT_CANDIDATES", ())
    monkeypatch.delenv("BROKER_REPORT_FONT", raising=False)
    ev = be.Evidence("T1", be.PASS, "", "2026-11-11 10:00:00")
    path = tmp_path / "a.png"
    cjk = be.render_shot(ev, be.Shot("", ["login ok " * 40, "line two"]), path)
    data = path.read_bytes()
    assert cjk is False and data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == be.IMAGE_WIDTH and height > 100


def test_render_shot_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "FONT_CANDIDATES", ())
    seen = []
    monkeypatch.setattr(be, "wrap_lines", lambda lines, font, w: seen.extend(lines) or lines)
    be.render_shot(be.Evidence("T1", be.PASS), be.Shot("", ["token abcdef123 here"]), tmp_path / "a.png", secrets=("abcdef123",))
    assert seen == ["token *** here"]


def test_save_load_roundtrip_and_merge_semantics(tmp_path):
    old = {"T1": be.Evidence("T1", be.PASS, "", "t1", [be.Shot("", ["a"])]), "T3": be.Evidence("T3", be.FAIL, "x", "t1")}
    be.save_evidence(tmp_path, list(old.values()))
    loaded = be.load_evidence(tmp_path)
    assert loaded["T1"].shots[0].lines == ["a"] and loaded["T3"].status == be.FAIL
    new = [be.Evidence("T1", be.SKIP, "非交易时段"), be.Evidence("T3", be.PASS), be.Evidence("T2", be.PASS)]
    merged = {e.item: e for e in be.merge_evidence(loaded, new)}
    assert merged["T1"].status == be.PASS                    # 新的 SKIP 不抹掉旧的 PASS
    assert merged["T3"].status == be.PASS                    # 新的结果覆盖旧的 FAIL
    assert list(merged) == ["T1", "T2", "T3"]                # 按目录顺序输出
    assert be.load_evidence(tmp_path / "nope") == {}


def test_optional_item_fail_blocks_the_conclusion_check(tmp_path):
    """选测项(如 P2.3=模板 3.3)取证为 FAIL 时不勾程序化补充的「测试结论:通过」,并在缺失清单里点名;外部接入部分不受影响。"""
    evs = evidences(**{"P2.3": be.Evidence("P2.3", be.FAIL, "重复报单未被拦截", "t", [be.Shot("", ["x"])])})
    outcome, doc = build(tmp_path, evs)
    results = table_with(doc, "1.1")
    assert checked_labels(br.find_row(results, "3.3")[1][2]) == ["不通过"]
    external, programmatic = conclusion_rows(doc)
    assert checked_labels(external[1]) == ["通过"] and checked_labels(programmatic[1]) == []
    assert "P2.3" in "\n".join(outcome.missing) and "选测项" in "\n".join(outcome.missing)


def test_missing_evidence_image_file_is_reported_not_silently_dropped(tmp_path):
    """evidence.json 里登记了图、磁盘上文件没了:插不了图,但要在 warnings/missing 里点名,不静默跳过。"""
    evs = evidences()
    out_dir = render_images(tmp_path, evs)
    victim = next(out_dir.glob("images/*.png"))
    victim.unlink()
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    joined = "\n".join(outcome.missing)
    assert "证据图缺失" in joined and victim.name in joined
    assert any("证据图缺失" in w for w in outcome.warnings)


def build_from_dir(tmp_path, evs, out_dir):
    """用已渲染好的证据目录(而不是现场再渲染)构造报告,供删除图片文件等场景使用。"""
    template = build_template(tmp_path / "template2.docx")
    outcome = br.build_report(template, out_dir, tmp_path / "out2.docx", ctx(), evs)
    return outcome, br.Docx(outcome.path)


def test_fill_covers_prints_submitter_name_beside_signature_line(tmp_path):
    doc = br.Docx(build_template(tmp_path / "template.docx"))
    p = etree.SubElement(doc.body, br.P)
    for text in ("提测方（签字/盖章(", "加盖骑缝章", ")）：      "):
        r = etree.SubElement(p, br.R)
        etree.SubElement(r, br.T).text = text
    br.fill_covers(doc, ctx(submitter_name="测试员甲"))
    assert "加盖骑缝章)）：（提测方姓名：测试员甲" in br.text_of(p)
    p2 = etree.SubElement(doc.body, br.P)
    etree.SubElement(etree.SubElement(p2, br.R), br.T).text = "提测方（签字/盖章(加盖骑缝章)）：  "
    br.fill_covers(doc, ctx(submitter_name=""))
    assert br.text_of(p2) == "提测方（签字/盖章(加盖骑缝章)）：  "


def test_missing_mandatory_result_row_makes_the_conclusion_undecidable(tmp_path):
    """必测项的结果行在模板里找不到时,不能因为「没发现失败」就勾「通过」。"""
    template = build_template(tmp_path / "t.docx")
    with zipfile.ZipFile(template) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["word/document.xml"] = parts["word/document.xml"].replace("5.2.错误提示功能".encode(), "5-2.错误提示功能".encode())
    with zipfile.ZipFile(template, "w") as z:
        for n, data in parts.items():
            z.writestr(n, data)
    evs = evidences()
    out_dir = render_images(tmp_path, evs)
    outcome = br.build_report(template, out_dir, tmp_path / "o.docx", ctx(), evs)
    doc = br.Docx(outcome.path)
    external, programmatic = conclusion_rows(doc)
    assert checked_labels(external[1]) == ["通过"] and checked_labels(programmatic[1]) == []
    assert any("5.2" in w for w in outcome.warnings) and any("结论不可判定" in m for m in outcome.missing)


def test_section_numbers_are_matched_as_whole_tokens_not_prefixes():
    assert br.HEADING.match("1.10 其他") is None and br.HEADING.match("1.1.连通性").group(2) == "1"
    from docx_fixture import cell, row, table
    tbl = etree.fromstring(f'<root xmlns:w="{br.W_NS}">' + table(row(cell("1.10.其他"), cell("x")), row(cell("1.1.连通性"), cell("y"))) + "</root>")[0]
    hit = br.find_numbered_row(tbl, "1.1")
    assert br.text_of(hit[1][0]) == "1.1.连通性"
