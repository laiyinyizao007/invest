"""把取证结果填进国信《外部接入系统技术测试报告》模板(docx),生成「外部接入系统测试 + 程序化补充测试」报告(国信 20260717 版模板)。

用法(项目根目录):
    python scripts/broker_report.py --template <国信模板.docx> --evidence data/broker_test/<时间戳>
    python scripts/broker_report.py --template <模板> --evidence <目录> --out <输出.docx>

做法:直接用 lxml 改 docx 的 XML,**不引入新依赖**;以单元格文字定位表格行(不写死下标),模板改版时更稳。
- 填写:封面占位、测试接入信息、两部分的测试结果(1.1/1.2/2.1 与 3.1–7.1)、2.1 稳定运行的「耗时约 N 秒」、各项证据图;
- 只支持 20260717 版模板(单文档两部分,无穿透专章);旧版模板会明确报错,不会按旧编号错填;
- **绝不默认勾通过**:模板里预勾选的结果一律重置,只有取证结果为 PASS 才勾「通过/正常」;选测项未测勾「未测试」;
  必测项没有 PASS 时留空并在终端列出"报告里还缺什么";
- **不碰国信填写区**:「信息科技中心评估意见」「测试评估」「测试人员(国信期货)」等表格原样保留;
- 签字/盖章栏留空:需要人工签字盖章。
模板含国信测试环境的 IP/端口,**不入库**,用 --template 传路径(或环境变量 BROKER_REPORT_TEMPLATE)。
"""
import argparse
import os
import re
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401

from lxml import etree

from broker_evidence import FAIL, ITEMS, MANUAL, OPTIONAL_ITEMS, PASS, SKIP, TEMPLATE_NO, Evidence, load_evidence

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


P, TBL, TR, TC, R, T, SYM, RPR, PPR = (w(x) for x in ("p", "tbl", "tr", "tc", "r", "t", "sym", "rPr", "pPr"))
CHECKED, UNCHECKED = "0052", "00A3"      # Wingdings 2:☑ / □
MAX_IMAGE_EMU = 5_600_000                  # 约 15.5 cm,不超过版心
SKIP_MARK_PREFIX = "（截图材料"


# ======================= docx 读写 =======================

class Docx:
    """内存中的 docx 包:保留所有成员,只改 document.xml、rels、content types。"""

    def __init__(self, path: Path) -> None:
        with zipfile.ZipFile(path) as z:
            self.names = z.namelist()
            self.members = {n: z.read(n) for n in self.names}
        self.doc = etree.fromstring(self.members["word/document.xml"])
        self.body = self.doc.find(w("body"))
        self.rels = etree.fromstring(self.members["word/_rels/document.xml.rels"])
        self.content_types = etree.fromstring(self.members["[Content_Types].xml"])
        self._image_count = 0
        self.missing_images: list[str] = []      # 证据里登记了、但磁盘上找不到的图

    def add_image(self, png: bytes) -> str:
        """加入一张 PNG,返回关系 ID。"""
        self._image_count += 1
        name = f"word/media/broker_{self._image_count}.png"
        self.members[name] = png
        if name not in self.names:
            self.names.append(name)
        rid = f"rIdBroker{self._image_count}"
        rel = etree.SubElement(self.rels, f"{{{REL_NS}}}Relationship")
        rel.set("Id", rid)
        rel.set("Type", IMAGE_REL_TYPE)
        rel.set("Target", f"media/broker_{self._image_count}.png")
        if not any(d.get("Extension", "").lower() == "png" for d in self.content_types.findall(f"{{{CT_NS}}}Default")):
            default = etree.SubElement(self.content_types, f"{{{CT_NS}}}Default")
            default.set("Extension", "png")
            default.set("ContentType", "image/png")
        return rid

    def save(self, path: Path) -> None:
        self.members["word/document.xml"] = etree.tostring(self.doc, xml_declaration=True, encoding="UTF-8", standalone=True)
        self.members["word/_rels/document.xml.rels"] = etree.tostring(self.rels, xml_declaration=True, encoding="UTF-8", standalone=True)
        self.members["[Content_Types].xml"] = etree.tostring(self.content_types, xml_declaration=True, encoding="UTF-8", standalone=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self.names:
                z.writestr(name, self.members[name])


# ======================= 文本与单元格 =======================

def text_of(el) -> str:
    return "".join((n.text or "") for n in el.iter(T))


def set_paragraph_text(p, text: str) -> None:
    """把整个段落的文字换成 text,保留第一个 run 的格式(去掉颜色,避免提示文字的灰色被带过来)。"""
    runs = p.findall(R)
    rpr = None
    if runs and runs[0].find(RPR) is not None:
        rpr = _clean_rpr(runs[0].find(RPR))
    for run in runs:
        p.remove(run)
    run = etree.SubElement(p, R)
    if rpr is not None:
        run.append(rpr)
    t = etree.SubElement(run, T)
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _clean_rpr(rpr):
    import copy
    clean = copy.deepcopy(rpr)
    for tag in ("color", "highlight", "shd"):
        for el in clean.findall(w(tag)):
            clean.remove(el)
    return clean


def set_cell_text(tc, text: str) -> None:
    paras = tc.findall(P)
    if not paras:
        paras = [etree.SubElement(tc, P)]
    for extra in paras[1:]:
        tc.remove(extra)
    set_paragraph_text(paras[0], text)


def append_text(tc, text: str) -> None:
    """在单元格第一段末尾追加文字(例如「异常:」后面写原因)。"""
    p = tc.find(P)
    runs = p.findall(R)
    run = etree.SubElement(p, R)
    if runs and runs[-1].find(RPR) is not None:
        run.append(_clean_rpr(runs[-1].find(RPR)))
    t = etree.SubElement(run, T)
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def set_checks(tc, selected: set[str]) -> list[str]:
    """重置单元格内全部勾选框,只勾 selected 里的选项。返回识别到的选项名(便于测试/排错)。

    选项有两种写法:Wingdings 符号 `w:sym`(char 0052=☑ / 00A3=□)后接文字,或文字里的字面 `□选项`。
    匹配先精确(去空白后相等)再前缀:避免勾「CTP」时连「CTPⅡ mini」一起勾上。
    """
    options: list[dict] = []
    current = None
    for el in tc.iter():
        if el.tag == SYM:
            current = {"sym": el, "label": ""}
            options.append(current)
        elif el.tag == T:
            parts = (el.text or "").split("□")
            if current is not None:
                current["label"] += parts[0]
            if len(parts) > 1:
                for k in range(1, len(parts)):
                    options.append({"t": el, "k": k, "label": parts[k]})
                current = None
    chosen: set[int] = set()
    for sel in selected:
        exact = [i for i, o in enumerate(options) if o["label"].strip() == sel]
        if exact:
            chosen.add(exact[0])
            continue
        prefixed = [i for i, o in enumerate(options) if o["label"].strip().startswith(sel)]
        if prefixed:
            chosen.add(prefixed[0])
    literal_by_t: dict = {}
    for i, o in enumerate(options):
        if "sym" in o:
            o["sym"].set(w("char"), CHECKED if i in chosen else UNCHECKED)
        else:
            literal_by_t.setdefault(o["t"], []).append((o["k"], i))
    for t_el, items in literal_by_t.items():
        parts = (t_el.text or "").split("□")
        rebuilt = parts[0]
        by_k = dict(items)
        for k in range(1, len(parts)):
            rebuilt += ("☑" if by_k.get(k) in chosen else "□") + parts[k]
        t_el.text = rebuilt
    return [o["label"].strip() for o in options]


def replace_in_paragraphs(root, old: str, new: str) -> int:
    """把包含 old 的段落整段改写(跨 run 的占位符也能替换;这类封面短句不需要保留混合格式)。"""
    count = 0
    for p in root.iter(P):
        text = text_of(p)
        if old in text:
            set_paragraph_text(p, text.replace(old, new))
            count += 1
    return count


# ======================= 表格定位 =======================

def children(body) -> list:
    return list(body)


def tables_in(body, start: int, end: int) -> list:
    return [el for el in list(body)[start:end] if el.tag == TBL]


def find_row(table, label: str):
    """按单元格文字找行:返回 (tr, cells, 命中列号) 或 None。文字以 label 开头即命中。"""
    for tr in table.findall(TR):
        cells = tr.findall(TC)
        for idx, tc in enumerate(cells):
            if text_of(tc).strip().startswith(label):
                return tr, cells, idx
    return None


def find_row_in_tables(tables: list, label: str):
    for table in tables:
        hit = find_row(table, label)
        if hit:
            return hit
    return None


# ======================= 图片 =======================

def png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])


def inline_picture(rid: str, cx: int, cy: int, pic_id: int, name: str):
    xml = (
        f'<w:drawing xmlns:w="{W_NS}" xmlns:wp="{WP_NS}" xmlns:a="{A_NS}" xmlns:pic="{PIC_NS}" xmlns:r="{R_NS}">'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="{pic_id}" name="{name}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="{PIC_NS}"><pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
    )
    return etree.fromstring(xml)


def picture_paragraph(doc: Docx, png: bytes, name: str, pic_id: int):
    rid = doc.add_image(png)
    width, height = png_size(png)
    cx = min(MAX_IMAGE_EMU, width * 9525)          # 96 dpi:1 px = 9525 EMU
    cy = int(cx * height / width)
    p = etree.Element(P)
    ppr = etree.SubElement(p, PPR)
    jc = etree.SubElement(ppr, w("jc"))
    jc.set(w("val"), "center")
    run = etree.SubElement(p, R)
    run.append(inline_picture(rid, cx, cy, pic_id, name))
    return p


def caption_paragraph(text: str):
    p = etree.Element(P)
    run = etree.SubElement(p, R)
    t = etree.SubElement(run, T)
    t.text = text
    return p


# ======================= 填写 =======================

@dataclass
class ReportContext:
    system_name: str = ""
    version: str = ""
    app_id: str = ""
    account: str = ""
    submitter_name: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    machine: str = ""                 # 测试机型号
    os_name: str = ""
    cpu: str = ""
    memory: str = ""
    disk: str = ""
    test_time: str = ""
    access_system: str = "CTP"
    functions: tuple[str, ...] = ("自动开平仓", "止盈止损", "交易模型编写", "二次开发")
    report_date: datetime = field(default_factory=datetime.now)


@dataclass
class Outcome:
    path: Path
    missing: list[str] = field(default_factory=list)       # 报告里还缺什么(必测项没通过、需人工补的栏)
    warnings: list[str] = field(default_factory=list)


COVER2 = "程序化补充测试报告"
EXTERNAL_ITEMS = ("P1.1", "P1.2", "P1.3")              # 「外部接入系统测试」部分(模板 1.1/1.2/2.1);其余为程序化补充测试
SECTION_NO = {no: item for item, no in TEMPLATE_NO.items()}
HEADING = re.compile(r"^([1-7])\.([1-9])(?!\d)")


def split_parts(doc: Docx) -> tuple[int, int, int]:
    """返回 (外部接入部分起点, 程序化补充部分起点, 文末):以第二封面「程序化补充测试报告」为界。"""
    kids = children(doc.body)
    boundary = next((i for i, el in enumerate(kids) if el.tag == P and text_of(el).strip() == COVER2), None)
    if boundary is None:
        raise ValueError(f"模板里找不到「{COVER2}」封面,无法区分两部分——本脚本只支持国信 20260717 版模板"
                         "(外部接入 1.1/1.2/2.1 + 程序化补充 3.1–7.1),旧版模板的章节编号不同,不能按新编号填")
    return 0, boundary, len(kids)


def is_mandatory(item: str) -> bool:
    return item not in OPTIONAL_ITEMS


def result_choice(ev: Evidence | None, item: str, *, three_way: bool) -> str | None:
    """取证结果 → 勾选项。None = 不勾(必测项没通过,留给人工)。"""
    if ev is not None and ev.status == PASS:
        return "通过"
    if ev is not None and ev.status == FAIL:
        return "不通过"
    if three_way and not is_mandatory(item):
        return "未测试"
    return None


def _status_note(ev: Evidence | None) -> str:
    if ev is None:
        return "没有取证结果"
    return f"{ev.status}({ev.summary})" if ev.summary else ev.status


_PIC_ID = [9000]


def _insert_shot(doc: Docx, after, ev: Evidence, shot, caption: str = ""):
    """在 after 元素之后插入一张图(可带标题段),返回最后插入的元素,供连续插入。"""
    base = getattr(doc, "evidence_dir", None)
    if not shot.image or base is None:
        return after
    path = Path(base) / shot.image
    if not path.exists():
        doc.missing_images.append(f"{ev.item} {shot.label}".strip() + f":{shot.image}")
        return after
    png = path.read_bytes()
    last = after
    if caption:
        cap = caption_paragraph(caption)
        last.addnext(cap)
        last = cap
    _PIC_ID[0] += 1
    pic = picture_paragraph(doc, png, f"{ev.item}-{shot.label or 'shot'}", _PIC_ID[0])
    last.addnext(pic)
    return pic


def find_numbered_row(table, number: str):
    """按章节编号找行:编号后不能紧跟数字(「1.1」不能命中「1.10」)。"""
    pattern = re.compile(rf"^{re.escape(number)}(?!\d)")
    for tr in table.findall(TR):
        cells = tr.findall(TC)
        for idx, tc in enumerate(cells):
            if pattern.match(text_of(tc).strip()):
                return tr, cells, idx
    return None


def _results_table(tables: list):
    for table in tables:
        if find_row(table, "1.1") and find_row(table, "测试结论"):
            return table
    return None


def fill_report(doc: Docx, ctx: ReportContext, evs: dict[str, Evidence], outcome: Outcome, boundary: int, end: int) -> None:
    tables = tables_in(doc.body, 0, end)

    def cell_at(label: str, col_offset: int = 1):
        hit = find_row_in_tables(tables, label)
        if not hit:
            outcome.warnings.append(f"报告:找不到「{label}」行")
            return None
        return hit[1][hit[2] + col_offset] if hit[2] + col_offset < len(hit[1]) else None

    hit = find_row_in_tables(tables, "测试系统名称")
    if hit:
        cells = hit[1]
        set_cell_text(cells[1], ctx.system_name)
        if len(cells) >= 4:
            set_cell_text(cells[3], ctx.version)
    for label, value in (("AppID/RelayAppID", ctx.app_id), ("测试时间", ctx.test_time), ("测试账号", ctx.account)):
        cell = cell_at(label)
        if cell is not None:
            set_cell_text(cell, value)
    cell = cell_at("连接模式")
    if cell is not None:
        set_checks(cell, {"直连的交易者"})
    cell = cell_at("送测样本")
    if cell is not None:
        set_checks(cell, {"其他"})
    cell = cell_at("交易信息系统名称")
    if cell is not None:
        set_checks(cell, {ctx.access_system})
    cell = cell_at("测试系统功能")
    if cell is not None:
        set_checks(cell, set(ctx.functions))
    for label, value in (("名称", ctx.submitter_name), ("联系人", ctx.contact_name)):
        hit = find_row_in_tables(tables, label)
        if hit and hit[2] + 1 < len(hit[1]) and text_of(hit[1][hit[2] + 1]).strip() == "":
            set_cell_text(hit[1][hit[2] + 1], value)
    hit = find_row_in_tables(tables, "电话")
    if hit:
        cells = hit[1]
        set_cell_text(cells[hit[2] + 1], ctx.contact_phone)
        if hit[2] + 3 < len(cells):
            set_cell_text(cells[hit[2] + 3], ctx.contact_email)
    cell = cell_at("测试系统设备")
    if cell is not None:
        set_cell_text(cell, f"测试机型号:{ctx.machine} 操作系统:{ctx.os_name} CPU:{ctx.cpu} 内存:{ctx.memory} 硬盘:{ctx.disk}")

    # 测试结果:按模板编号(1.1…7.1)在结果表里定位,内部 ID → 编号见 TEMPLATE_NO
    results = _results_table(tables)
    if results is None:
        outcome.warnings.append("报告:找不到测试结果表(含「1.1」与「测试结论」行)")
    else:
        passed = {"external": True, "programmatic": True}
        for item, number in TEMPLATE_NO.items():
            part = "external" if item in EXTERNAL_ITEMS else "programmatic"
            hit = find_numbered_row(results, number)
            if not hit:
                outcome.warnings.append(f"报告:找不到「{number}」结果行")
                if is_mandatory(item):                 # 必测项的行都找不到:结论不可判定,绝不能勾「通过」
                    passed[part] = False
                    outcome.missing.append(f"{ITEMS[item]}:模板里找不到「{number}」结果行,结论不可判定")
                continue
            cells, col = hit[1], hit[2]
            ev = evs.get(item)
            choice = result_choice(ev, item, three_way=not is_mandatory(item))
            set_checks(cells[col + 1], {choice} if choice else set())
            if is_mandatory(item) and (ev is None or ev.status != PASS):
                passed[part] = False
                outcome.missing.append(f"{ITEMS[item]}:{_status_note(ev)}")
        optional_failed = sorted(i for i in OPTIONAL_ITEMS if evs.get(i) is not None and evs[i].status == FAIL)
        if optional_failed:
            passed["programmatic"] = False
        conclusions = [tr.findall(TC) for tr in results.findall(TR) if text_of(tr.findall(TC)[0]).strip().startswith("测试结论")]
        if len(conclusions) != 2:
            outcome.warnings.append(f"报告:应有 2 个「测试结论」行(外部接入、程序化补充),实际 {len(conclusions)} 个,结论栏未勾")
        else:
            for cells, part in zip(conclusions, ("external", "programmatic")):
                set_checks(cells[1], {"通过"} if passed[part] else set())
        if not passed["external"]:
            outcome.missing.append("外部接入 测试结论:必测项未全部通过,未勾「通过」")
        if not passed["programmatic"]:
            outcome.missing.append("程序化补充 测试结论:必测项未全部通过,未勾「通过」")
        if optional_failed:
            outcome.missing.append(f"程序化补充 测试结论:选测项 {', '.join(optional_failed)} 未通过,未勾「通过」,需人工判断")
        for label in ("采集信息的完整性", "采集信息的正确性"):       # 模板预勾选了「通过」:是否通过由国信评估,本脚本一律重置、不代勾
            hit = find_row(results, label)
            if hit:
                set_checks(hit[1][hit[2] + 1], set())
        outcome.missing.append("看穿式采集(完整性/正确性):不由本脚本勾选,以国信对采集结果的评估为准(模板预勾选的「通过」已重置)")

    # 证据图:两部分各自从「测试总结报告证明材料」标题起,按各项小标题锚点插入
    kids = children(doc.body)
    proofs = [i for i, el in enumerate(kids) if el.tag == P and text_of(el).strip().startswith("测试总结报告证明材料")]
    first = next((i for i in proofs if i < boundary), None)
    second = next((i for i in proofs if i > boundary), None)
    if first is None or second is None:
        outcome.warnings.append("报告:找不到两处「测试总结报告证明材料」标题,证据图未插入")
        return
    # 第二处标题自己也含「测试系统功能测试指引」字样,所以从下一段起找、且要求以它开头(正文里那段「测试系统功能测试指引:摘自…」)
    guide = next((i for i in range(second + 1, end) if kids[i].tag == P and text_of(kids[i]).strip().startswith("测试系统功能测试指引")), end)
    regions: dict[str, list] = {}
    for lo, hi in ((first, boundary), (second, guide)):
        current = None
        for el in kids[lo:hi]:
            if el.tag != P:
                continue
            m = HEADING.match(text_of(el).strip())
            if m and f"{m.group(1)}.{m.group(2)}" in SECTION_NO:
                current = SECTION_NO[f"{m.group(1)}.{m.group(2)}"]
                regions[current] = [el]
            elif current:
                regions[current].append(el)
    for item, paras in regions.items():
        ev = evs.get(item)
        if ev is None:
            continue
        last_after: dict[int, object] = {}
        default_anchor = next((p for p in paras if text_of(p).strip().startswith(SKIP_MARK_PREFIX)), paras[0])
        for shot in ev.shots:
            anchor = next((p for p in paras[1:] if shot.label and shot.label in text_of(p)), default_anchor)
            base = last_after.get(id(anchor), anchor)
            last_after[id(anchor)] = _insert_shot(doc, base, ev, shot)
    _fill_stability_seconds(evs.get("P1.3"), regions.get("P1.3"), outcome)


def _fill_stability_seconds(ev: Evidence | None, paras: list | None, outcome: Outcome) -> None:
    """2.1 稳定运行:把「持续下单200笔,耗时约     秒」的空格换成取证实测的秒数(没有实测值就保持空白并报缺)。"""
    if ev is None or ev.status not in (PASS, FAIL):
        return
    orders, seconds = ev.metrics.get("orders"), ev.metrics.get("elapsed_seconds")
    if not orders or seconds is None:
        outcome.missing.append("2.1 稳定运行:证据里没有下单笔数/耗时,「耗时约 N 秒」需人工填写")
        return
    target = next((p for p in (paras or []) if f"持续下单{orders}笔" in re.sub(r"\s", "", text_of(p))), None)
    if target is None:
        outcome.warnings.append(f"报告:2.1 里找不到「持续下单{orders}笔」段落,耗时未填")
        return
    text = text_of(target)
    filled, n = re.subn(r"(耗时约)\s*(秒)", rf"\1 {seconds:g} \2", text, count=1)
    if n == 0:
        outcome.warnings.append("报告:2.1 段落里找不到「耗时约 秒」填空,耗时未填")
        return
    set_paragraph_text(target, filled)


def fill_covers(doc: Docx, ctx: ReportContext) -> None:
    replace_in_paragraphs(doc.body, "XXXX系统", f"{ctx.system_name}系统")
    replace_in_paragraphs(doc.body, "XXXX年XX月XX日", f"{ctx.report_date:%Y年%m月%d日}")
    # 签字栏只打印提测方姓名方便对位,不代替手写签字/盖章
    if ctx.submitter_name:
        replace_in_paragraphs(doc.body, "加盖骑缝章)）：", f"加盖骑缝章)）：（提测方姓名：{ctx.submitter_name}，请在此处手写签名）")


def build_report(template: Path, evidence_dir: Path, out_path: Path, ctx: ReportContext,
                 evidences: dict[str, Evidence] | None = None) -> Outcome:
    doc = Docx(template)
    doc.evidence_dir = evidence_dir
    evs = evidences if evidences is not None else load_evidence(evidence_dir)
    outcome = Outcome(path=out_path)
    _, boundary, end = split_parts(doc)
    fill_report(doc, ctx, evs, outcome, boundary, end)
    fill_covers(doc, ctx)
    for missing_image in doc.missing_images:
        outcome.warnings.append(f"证据图缺失,未插入:{missing_image}")
        outcome.missing.append(f"证据图缺失:{missing_image}")
    for label, value in (("系统名称", ctx.system_name), ("AppID", ctx.app_id), ("测试账号", ctx.account), ("提测方名称", ctx.submitter_name),
                         ("联系人", ctx.contact_name), ("联系电话", ctx.contact_phone)):
        if not value:
            outcome.missing.append(f"{label} 没有提供,对应栏留空")
    outcome.missing.append("签字/盖章栏(第五页提测方签字、法人户盖章四处:第五页、两份报告首页、骑缝章)需人工签字盖章")
    doc.save(out_path)
    return outcome


def context_from_env(env, info: dict | None = None) -> ReportContext:
    """从环境变量(BROKER_TEST_*、CTP_*)与终端信息(collect_terminal_info.collect 的结果)组装报告上下文。"""
    info = info or {}
    machine = info.get("machine") or {}
    cpus = info.get("cpu") or []
    disks = info.get("disks") or []
    os_info = info.get("os") or {}
    functions = tuple(x.strip() for x in env.get("BROKER_TEST_FUNCTIONS", "").split(",") if x.strip()) or ReportContext.functions
    return ReportContext(
        system_name=env.get("BROKER_TEST_SYSTEM_NAME", ""), version=env.get("BROKER_TEST_VERSION", ""),
        app_id=env.get("CTP_APP_ID", ""), account=env.get("CTP_USERNAME", ""),
        submitter_name=env.get("BROKER_TEST_SUBMITTER", ""), contact_name=env.get("BROKER_TEST_CONTACT", ""),
        contact_phone=env.get("BROKER_TEST_PHONE", ""), contact_email=env.get("BROKER_TEST_EMAIL", ""),
        machine=f"{machine.get('manufacturer', '')} {machine.get('model', '')}".strip(),
        os_name=f"{os_info.get('name', '')} {os_info.get('version', '')}".strip(),
        cpu=(cpus[0]["name"] if cpus else ""), memory=(f"{machine.get('memory_gb')} GB" if machine.get("memory_gb") else ""),
        disk="; ".join(f"{d['model']} {d['size_gb']}GB" for d in disks[:2]),
        test_time=datetime.now().strftime("%Y-%m-%d"), functions=functions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--template", type=Path, default=os.environ.get("BROKER_REPORT_TEMPLATE") or None,
                        help="国信模板 docx(不入库;也可用环境变量 BROKER_REPORT_TEMPLATE)")
    parser.add_argument("--evidence", type=Path, required=True, help="broker_test.py 生成的证据目录")
    parser.add_argument("--out", type=Path, default=None, help="输出 docx(默认在证据目录里)")
    parser.add_argument("--no-terminal-info", action="store_true", help="不采集本机终端信息(机型/CPU/内存/硬盘栏留空)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if args.template is None or not Path(args.template).exists():
        print("找不到国信模板:请用 --template 或环境变量 BROKER_REPORT_TEMPLATE 指定(模板含国信测试环境信息,不入库)。", file=sys.stderr)
        return 2
    if not (args.evidence / "evidence.json").exists():
        print(f"证据目录里没有 evidence.json:{args.evidence}。请先运行 scripts/broker_test.py。", file=sys.stderr)
        return 2
    bootstrap.load_env()
    info = None
    if not args.no_terminal_info and sys.platform == "win32":
        import collect_terminal_info
        info = collect_terminal_info.collect()
    ctx = context_from_env(os.environ, info)
    out = args.out or (args.evidence / f"国信外部接入系统技术测试报告-{ctx.app_id or 'noappid'}.docx")
    outcome = build_report(Path(args.template), args.evidence, out, ctx)
    print(f"报告已生成:{outcome.path}")
    for line in outcome.warnings:
        print(f"  ⚠ {line}", file=sys.stderr)
    print("报告里还缺什么:")
    for line in outcome.missing:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
