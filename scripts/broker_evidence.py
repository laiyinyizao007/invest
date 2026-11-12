"""国信测试取证:证据数据模型、持久化与证据图片渲染(Pillow)。

国信《外部接入系统技术测试报告》要求每个测试项附截图。本项目的"截图"是**证据文本渲染成的图片**:
内容取自真实运行(脱敏后的日志、计数、回报),带时间与状态标题栏,可追溯;它**不是**真实界面截图,
是否被国信接受需向国信确认(见 docs/adr/20261111-broker-test-compliance.md)。

一个测试项(Evidence)可以有多张图(Shot):例如 1.2 基础交易要「开仓/平仓/撤单」三张,4.2 柜台错误要三类各一张。
"""
import json
import os
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path

from notify.webhook import redact

PASS, FAIL, SKIP, MANUAL = "PASS", "FAIL", "SKIP", "MANUAL"
STATUS_LABEL = {PASS: "通过", FAIL: "未通过", SKIP: "未测试", MANUAL: "待人工"}
STATUS_COLOR = {PASS: (34, 139, 34), FAIL: (200, 30, 30), SKIP: (120, 120, 120), MANUAL: (210, 120, 0)}

# 测试项目录:穿透式测试 T1–T7,程序化测试 P*(与国信模板的编号一致)
ITEMS: dict[str, str] = {
    "T1": "①登录", "T2": "②行情", "T3": "③委托(开/平仓)", "T4": "④撤单", "T5": "⑤成交",
    "T6": "⑥具备流量控制功能", "T7": "⑦具备自动退出功能(异常时)",
    "P1.1": "1.1 连通性(系统登录)", "P1.2": "1.2 基础交易功能(开仓/平仓/撤单)",
    "P2.1": "2.1 系统连接状态异常监测功能", "P2.2": "2.2 报撤单笔数监测功能", "P2.3": "2.3 重复报单监测功能(选测)",
    "P3.1": "3.1 阈值设置及预警功能",
    "P4.1": "4.1 交易指令检查功能", "P4.2": "4.2 接收柜台错误提示功能",
    "P5.1": "5.1 暂停交易功能", "P5.2": "5.2 批量撤单功能(选测)",
    "P6.1": "6.1 日志记录功能",
}
OPTIONAL_ITEMS = frozenset({"P2.3", "P5.2"})      # 国信模板里的选测项:可以「未测试」

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/PingFang.ttc",
)
IMAGE_WIDTH = 1200
PADDING = 24
FONT_SIZE = 18


@dataclass
class Shot:
    """一张证据图:label 对应国信模板里的截图位名称(如「开仓」「报单笔数统计」),空串 = 该项唯一的图。"""

    label: str = ""
    lines: list[str] = field(default_factory=list)
    image: str = ""         # 渲染后的 PNG 路径(相对证据目录)


@dataclass
class Evidence:
    item: str
    status: str
    summary: str = ""
    started_at: str = ""
    shots: list[Shot] = field(default_factory=list)

    @property
    def title(self) -> str:
        return ITEMS.get(self.item, self.item)


def load_font(size: int = FONT_SIZE):
    """加载支持中文的字体。返回 (字体, 是否为 CJK 字体);找不到时退回 Pillow 默认字体(中文会显示成方框)。"""
    from PIL import ImageFont
    candidates = [os.environ.get("BROKER_REPORT_FONT", "")] + list(FONT_CANDIDATES)
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    return ImageFont.load_default(), False


def wrap_lines(lines: list[str], font, max_width: int) -> list[str]:
    """按像素宽度折行(逐字符贪心,中文无空格也能折)。"""
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    out: list[str] = []
    for line in lines:
        if not line:
            out.append("")
            continue
        current = ""
        for ch in line:
            if draw.textlength(current + ch, font=font) > max_width and current:
                out.append(current)
                current = ch
            else:
                current += ch
        out.append(current)
    return out


def render_shot(ev: Evidence, shot: Shot, path: Path, secrets: tuple[str, ...] = ()) -> bool:
    """把一张证据图渲染成 PNG。返回是否使用了 CJK 字体(False = 中文会显示为方框,调用方应告警)。"""
    from PIL import Image, ImageDraw
    font, cjk = load_font()
    title_font, _ = load_font(FONT_SIZE + 2)
    inner = IMAGE_WIDTH - 2 * PADDING
    header = f"{ev.item}  {ev.title}" + (f" · {shot.label}" if shot.label else "")
    sub = f"状态:{STATUS_LABEL.get(ev.status, ev.status)}    时间:{ev.started_at or '-'}"
    body = wrap_lines([_redact(line, secrets) for line in shot.lines] or ["(无明细)"], font, inner)
    footer = "证据由 scripts/broker_test.py 自动生成 · 内容已脱敏"
    line_h = FONT_SIZE + 8
    bar_h = 2 * line_h + PADDING
    height = bar_h + PADDING + len(body) * line_h + PADDING + line_h
    img = Image.new("RGB", (IMAGE_WIDTH, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, IMAGE_WIDTH, bar_h], fill=STATUS_COLOR.get(ev.status, (90, 90, 90)))
    draw.text((PADDING, PADDING // 2), header, font=title_font, fill=(255, 255, 255))
    draw.text((PADDING, PADDING // 2 + line_h), sub, font=font, fill=(255, 255, 255))
    y = bar_h + PADDING
    for line in body:
        draw.text((PADDING, y), line, font=font, fill=(30, 30, 30))
        y += line_h
    draw.text((PADDING, y + PADDING // 2), footer, font=font, fill=(130, 130, 130))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return cjk


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """兼容旧内部调用；实际脱敏统一由 notify.webhook.redact 执行。"""
    return redact(text, secrets)


def render_all(evidences: list[Evidence], out_dir: Path, secrets: tuple[str, ...] = ()) -> bool:
    """渲染全部证据图并回填 shot.image。返回是否全程使用 CJK 字体。"""
    all_cjk = True
    for ev in evidences:
        for n, shot in enumerate(ev.shots, 1):
            rel = Path("images") / f"{ev.item.replace('.', '_')}_{n}.png"
            all_cjk &= render_shot(ev, shot, out_dir / rel, secrets)
            shot.image = rel.as_posix()
    return all_cjk


def save_evidence(out_dir: Path, evidences: list[Evidence]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "evidence.json"
    path.write_text(json.dumps([asdict(e) for e in evidences], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_evidence(out_dir: Path) -> dict[str, Evidence]:
    path = out_dir / "evidence.json"
    if not path.exists():
        return {}
    result: dict[str, Evidence] = {}
    for raw in json.loads(path.read_text(encoding="utf-8")):
        shots = [Shot(**s) for s in raw.get("shots", [])]
        result[raw["item"]] = Evidence(raw["item"], raw["status"], raw.get("summary", ""), raw.get("started_at", ""), shots)
    return result


def merge_evidence(old: dict[str, Evidence], new: list[Evidence]) -> list[Evidence]:
    """增量合并(--resume):新结果覆盖旧结果,但新的 SKIP 不覆盖旧的 PASS/FAIL(非交易时段补测不应抹掉白天的证据)。"""
    merged = dict(old)
    for ev in new:
        previous = merged.get(ev.item)
        if ev.status in (SKIP, MANUAL) and previous is not None and previous.status in (PASS, FAIL):
            continue
        merged[ev.item] = ev
    return [merged[k] for k in ITEMS if k in merged]


def wrap_detail(text: str, width: int = 90) -> list[str]:
    """把一段长文本拆成多行明细(给没有字体度量的场景用,例如测试)。"""
    return textwrap.wrap(text, width=width) or [""]
