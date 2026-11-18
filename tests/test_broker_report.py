"""国信测试报告 docx 填写器与证据模型/渲染的离线测试(用现场构造的迷你模板,真实模板不入库)。"""
import struct
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
    # 配置解析走 ctp_tools.profile;这里只关心报告生成,不读真实项目 .env(里面是别的 AppID)
    import os

    from ctp_tools.profile import Profile

    monkeypatch.setattr(br, "load_profile", lambda profile="", root=None: Profile(env=dict(os.environ), hard_conflicts=[], notes=[]))
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


def test_the_report_test_date_is_beijing_time():
    """报告表头「测试时间」必须与证据链同一基准(北京时间)。

    2026-11-16 codex 第八轮复核 L5:证据目录、条目时间、连接记录都已是北京时间,
    报告头却是本机本地时间 ⇒ 夜盘时段出的报告日期比证据目录早一天。

    北京时间 00:00 前后跨零点时,前后两次取时钟可能落在不同日期 ⇒ 断言
    「落在前后两次取样之间」,而不是「等于某一次取样」(2026-11-16 codex 第九轮复核 L6:
    这个用例会在北京时间零点整前后随机挂)。
    """
    from risk import account_guard as ag

    before = ag.beijing_now().strftime("%Y-%m-%d")
    c = br.context_from_env({}, {})
    after = ag.beijing_now().strftime("%Y-%m-%d")
    assert c.test_time in {before, after}


def test_the_report_cover_date_is_beijing_time(monkeypatch):
    """封面「报告日期」也必须是北京时间。

    2026-11-16 codex 第九轮复核 M2:报告头的日期用本机本地时间(datetime.now),
    而表头「测试时间」用北京时间 ⇒ 同一份报告上可能出现两个日期(宿主机 UTC-8 时差一天)。

    2026-11-16 codex 第十轮复核 L-5:原用例断言「落在前后两次 `beijing_now()` 之间」——
    只要**实现**也用 `beijing_now()`,它必然成立 ⇒ 在「本机==北京」的环境里是假绿,
    抓不住被修的缺陷。这里改成**注入固定时钟**:固定一个时刻(北京 2026-11-16 21:30:45),
    再断言封面日期就是那个北京时间值 —— 实现若用本机本地时间,在任何非北京时区的机器上
    都会红(北京时区的机器上两者本来就相同,没有可区分的缺陷)。
    """
    from datetime import datetime as real_datetime, timezone

    from risk import account_guard as ag

    # 2026-11-16 13:30:45 UTC == 北京 2026-11-16 21:30:45
    instant = real_datetime(2026, 11, 16, 13, 30, 45, tzinfo=timezone.utc)

    class FixedDatetime(real_datetime):
        """把「现在」钉死成上面那个时刻;按传入的时区换算 ⇒ 与本机时区无关。"""

        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.astimezone()

    monkeypatch.setattr(ag, "datetime", FixedDatetime)
    c = br.context_from_env({}, {})
    assert c.report_date == real_datetime(2026, 11, 16, 21, 30, 45), c.report_date
    # 表头「测试时间」同一基准(两个日期必须同一天)
    assert c.test_time == "2026-11-16", c.test_time


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


def test_watermark_banner_is_inserted_at_the_top_of_the_body(tmp_path):
    """预演报告必须一眼看出「不是国信环境」——模板正文里预填的是国信的 BrokerID/前置。"""
    evs = evidences()
    template = build_template(tmp_path / "template.docx")
    out_dir = render_images(tmp_path, evs)
    outcome = br.build_report(template, out_dir, tmp_path / "out.docx", ctx(), evs,
                              watermark="SimNow 预演 · 非国信测试环境 · 不可提交国信")
    doc = br.Docx(outcome.path)
    first_paragraph = doc.body[0]
    assert br.text_of(first_paragraph) == "SimNow 预演 · 非国信测试环境 · 不可提交国信"


def test_official_report_name_is_refused_for_non_guosin_evidence(tmp_path):
    """证据连的不是国信环境时,不能用正式报告文件名(否则会产出一份「自称国信环境」的文件)。"""
    evs = evidences()
    out_dir = render_images(tmp_path, evs)
    (out_dir / "connection.json").write_text(
        '[{"at": "2026-11-13T21:05:01", "environment": "实盘", "trade_front": "182.254.243.31:30001",'
        ' "market_front": "182.254.243.31:30011", "broker_id": "9999", "app_id": "simnow_client_test"}]',
        encoding="utf-8")
    assert br.non_guoxin_connections(out_dir) == ["BrokerID=9999 交易前置=182.254.243.31:30001"]
    (out_dir / "connection.json").write_text(
        '[{"at": "2026-11-13T21:05:01", "environment": "测试", "trade_front": "121.13.218.19:41285",'
        ' "market_front": "121.13.218.19:41293", "broker_id": "8030", "app_id": "guoxin_client"}]',
        encoding="utf-8")
    assert br.non_guoxin_connections(out_dir) == []


# ---------- 真实运行截图(证据目录 manual/) ----------

def _png(tmp_path, name="real.png"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (40, 30), "black").save(p)
    return p.read_bytes()


def _para_texts(doc):
    paras = [el for el in doc.body if el.tag == br.P]
    return ["DRAW" if el.find(f".//{{{br.W_NS}}}drawing") is not None else br.text_of(el).strip() for el in paras]


def test_real_screenshots_follow_the_matching_card(tmp_path):
    """manual/P1.2_平仓.png 紧跟「平仓」那张证据卡片之后,带说明标题;卡片本身保留。"""
    evs = evidences()
    evs["P1.2"] = be.Evidence("P1.2", be.PASS, "", "t", [be.Shot("开仓", ["o"]), be.Shot("平仓", ["c"]), be.Shot("撤单", ["x"])])
    out_dir = render_images(tmp_path, evs)
    (out_dir / "manual").mkdir()
    (out_dir / "manual" / "P1.2_平仓.png").write_bytes(_png(tmp_path))
    outcome, doc = build_from_dir(tmp_path, evs, out_dir)
    texts = _para_texts(doc)
    i = texts.index("2）平仓")
    assert texts[i + 1] == "DRAW"                                   # 生成的证据卡片
    assert texts[i + 2] == br.REAL_SHOT_CAPTION and texts[i + 3] == "DRAW"   # 紧跟真实截图
    assert texts.index("3）撤单") > i + 3
    assert not outcome.warnings


def test_item_level_real_screenshots_go_after_the_items_last_card_in_name_order(tmp_path):
    evs = evidences()
    out_dir = render_images(tmp_path, evs)
    (out_dir / "manual").mkdir()
    for n in ("P1.3_2.png", "P1.3.png"):
        (out_dir / "manual" / n).write_bytes(_png(tmp_path, n))
    outcome, doc = build_from_dir(tmp_path, evs, out_dir)
    texts = _para_texts(doc)
    i = next(k for k, t in enumerate(texts) if t.startswith("2）持续下单200笔"))
    assert texts[i + 1: i + 6] == ["DRAW", br.REAL_SHOT_CAPTION, "DRAW", br.REAL_SHOT_CAPTION, "DRAW"]
    assert outcome.real_shots == ["P1.3.png", "P1.3_2.png"]


def test_unmatched_real_screenshot_is_reported_not_dropped(tmp_path):
    evs = evidences()
    out_dir = render_images(tmp_path, evs)
    (out_dir / "manual").mkdir()
    (out_dir / "manual" / "P9.9_开仓.png").write_bytes(_png(tmp_path))
    (out_dir / "manual" / "notes.txt").write_text("x", encoding="utf-8")
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    assert any("P9.9_开仓.png" in w for w in outcome.warnings)
    assert not any("notes.txt" in w for w in outcome.warnings)        # 只认 png


def test_no_manual_dir_changes_nothing(tmp_path):
    outcome, doc = build(tmp_path, evidences())
    assert br.REAL_SHOT_CAPTION not in _para_texts(doc) and outcome.real_shots == []


def _manual(tmp_path, evs, files: dict):
    out_dir = render_images(tmp_path, evs)
    (out_dir / "manual").mkdir()
    for name, data in files.items():
        target = out_dir / "manual" / name
        if data is None:
            target.mkdir()
        else:
            target.write_bytes(data)
    return out_dir


def test_xml_special_characters_in_names_do_not_break_the_report(tmp_path):
    """文件名/小标题里的 & 必须转义进 XML(否则 etree 解析失败、整份报告生成不了)。"""
    evs = evidences()
    evs["P1.2"] = be.Evidence("P1.2", be.PASS, "", "t", [be.Shot("开仓", ["o"]), be.Shot("A&B", ["c"])])
    out_dir = _manual(tmp_path, evs, {"P1.2_A&B.png": _png(tmp_path)})
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    assert outcome.real_shots == ["P1.2_A&B.png"]
    names = [d.get("name") for d in br.Docx(outcome.path).doc.iter(f"{{{br.WP_NS}}}docPr")]
    assert "P1.2-real-P1.2_A&B" in names                    # 转义后再解析回来是原文


def test_invalid_or_oversized_pngs_are_warned_and_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "MAX_IMAGE_BYTES", 5000)
    evs = evidences()
    big = _png(tmp_path) + b"\0" * 6000
    out_dir = _manual(tmp_path, evs, {"P1.1.png": b"not a png at all, definitely", "P1.2.png": big, "P1.3.png": _png(tmp_path)})
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    assert outcome.real_shots == ["P1.3.png"]
    assert any("P1.1.png" in w and "不是有效的 PNG" in w for w in outcome.warnings)
    assert any("P1.2.png" in w and "过大" in w for w in outcome.warnings)


def test_zero_sized_png_header_is_rejected():
    import zlib
    chunk = b"IHDR" + struct.pack(">IIBBBBB", 0, 16, 8, 2, 0, 0, 0)     # 宽 0、高 16,校验和正确
    header = br.PNG_MAGIC + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))
    assert "尺寸异常" in br.png_problem(header)


def test_uppercase_extension_counts_and_directories_named_png_are_ignored(tmp_path):
    evs = evidences()
    out_dir = _manual(tmp_path, evs, {"P1.3.PNG": _png(tmp_path), "P1.1.png": None})
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    assert outcome.real_shots == ["P1.3.PNG"]


def test_new_image_ids_do_not_collide_with_the_template(tmp_path):
    template = build_template(tmp_path / "t.docx")
    d = br.Docx(template)
    rel = br.etree.SubElement(d.rels, f"{{{br.REL_NS}}}Relationship")
    rel.set("Id", "rIdBroker1")
    rel.set("Type", br.IMAGE_REL_TYPE)
    rel.set("Target", "media/broker_1.png")
    d.members["word/media/broker_1.png"] = b"old"
    d.names.append("word/media/broker_1.png")
    para = br.picture_paragraph(d, _png(tmp_path), "x", d.next_pic_id())
    d.body.append(para)
    d.body.append(br.picture_paragraph(d, _png(tmp_path), "y", d.next_pic_id()))
    rids = [r.get("Id") for r in d.rels]
    assert len(rids) == len(set(rids)) and d.members["word/media/broker_1.png"] == b"old"
    ids = [p.get("id") for p in d.doc.iter(f"{{{br.WP_NS}}}docPr")]
    assert len(ids) == len(set(ids))


def test_real_shots_are_named_in_warnings_when_proof_sections_are_missing(tmp_path):
    template = build_template(tmp_path / "t.docx")
    d = br.Docx(template)
    for p in [el for el in d.body if el.tag == br.P and br.text_of(el).strip().startswith("测试总结报告证明材料")]:
        d.body.remove(p)
    d.save(tmp_path / "nohead.docx")
    evs = evidences()
    out_dir = _manual(tmp_path, evs, {"P1.3.png": _png(tmp_path)})
    outcome = br.build_report(tmp_path / "nohead.docx", out_dir, tmp_path / "o.docx", ctx(), evs)
    assert any("P1.3.png" in w for w in outcome.warnings)


def test_non_numeric_elapsed_seconds_is_reported_not_crashing(tmp_path):
    evs = evidences()
    evs["P1.3"] = be.Evidence("P1.3", be.PASS, "", "t", [be.Shot("200笔", ["l"])], {"orders": 200, "elapsed_seconds": "about 20"})
    outcome, _ = build(tmp_path, evs)
    assert any("耗时不是数字" in m for m in outcome.missing)


def test_numeric_string_elapsed_seconds_is_still_filled(tmp_path):
    evs = evidences()
    evs["P1.3"] = be.Evidence("P1.3", be.PASS, "", "t", [be.Shot("200笔", ["l"])], {"orders": 200, "elapsed_seconds": "20.6"})
    _, doc = build(tmp_path, evs)
    assert any("耗时约 20.6 秒" in br.text_of(p) for p in doc.body if p.tag == br.P)


def test_tall_images_are_capped_in_height(tmp_path):
    from PIL import Image
    p = tmp_path / "tall.png"
    Image.new("RGB", (10, 5000), "black").save(p)
    d = br.Docx(build_template(tmp_path / "t.docx"))
    para = br.picture_paragraph(d, p.read_bytes(), "tall", d.next_pic_id())
    ext = para.find(f".//{{{br.WP_NS}}}extent")
    assert int(ext.get("cy")) <= br.MAX_IMAGE_HEIGHT_EMU and int(ext.get("cx")) >= 1


def test_png_with_a_corrupt_header_checksum_is_rejected(tmp_path):
    good = bytearray(_png(tmp_path))
    assert br.png_problem(bytes(good)) == ""
    good[30] ^= 0xFF                                          # 破坏 IHDR 的 CRC
    assert "校验和" in br.png_problem(bytes(good))


def test_total_embedded_media_is_capped(tmp_path, monkeypatch):
    evs = evidences()
    out_dir = _manual(tmp_path, evs, {"P1.3.png": _png(tmp_path), "P1.3_2.png": _png(tmp_path, "b.png")})
    monkeypatch.setattr(br, "MAX_TOTAL_MEDIA_BYTES", 0)       # 任何图都会超出总量
    outcome, _ = build_from_dir(tmp_path, evs, out_dir)
    assert outcome.real_shots == [] and any("总量" in w for w in outcome.warnings)


def test_truncated_or_corrupted_png_body_is_rejected(tmp_path):
    good = _png(tmp_path)
    assert "截断" in br.png_problem(good[:-12])               # 去掉 IEND
    assert "截断" in br.png_problem(good[:40])
    bad = bytearray(good)
    idat = bytes(bad).index(b"IDAT")
    bad[idat + 6] ^= 0xFF                                      # 改坏 IDAT 数据
    assert "IDAT" in br.png_problem(bytes(bad))
