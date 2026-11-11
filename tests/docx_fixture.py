"""测试用的迷你国信模板:结构仿照真实模板(表头文字、Wingdings 勾选符号、锚点段落、国信填写区),
**不含任何国信测试环境信息**——真实模板不入库。"""
import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def sym(char: str) -> str:
    return f'<w:r><w:sym w:font="Wingdings 2" w:char="{char}"/></w:r>'


def run(text: str) -> str:
    return f'<w:r><w:rPr><w:color w:val="999999"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def para(*parts: str) -> str:
    return "<w:p>" + "".join(p if p.startswith("<w:r") else run(p) for p in parts) + "</w:p>"


def cell(*parts: str) -> str:
    return "<w:tc>" + (para(*parts) if parts else "<w:p/>") + "</w:tc>"


def row(*cells: str) -> str:
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def table(*rows: str) -> str:
    return "<w:tbl>" + "".join(rows) + "</w:tbl>"


def opts(selected_char: str | None, *labels: str, checked: tuple[str, ...] = ()) -> list[str]:
    return [p for lb in labels for p in (sym("0052" if lb in checked else "00A3"), run(lb))]


GX_TABLE = table(
    row(cell("完整性"), cell("是否能完整采集相应的信息"), cell(*opts(None, "是", "否：", checked=("是",)))),
)


def build_body() -> str:
    parts = [
        para("致尊敬的客户:"),
        # ---------- 穿透报告 ----------
        para("穿透式测试报告"), para("——XXXX系统"), para("日期：XXXX年XX月XX日"),
        table(
            row(cell("测试账户"), cell("（建议使用自有仿真账户进行测试）")),
            row(cell("测试系统名称"), cell(" ")),
            row(cell("AppID/RelayAppID"), cell("（实盘AppID必须与测试AppID保持一致）")),
            row(cell("AppID类型"), cell(*opts(None, "直连的交易者", "为每个交易者都创建连接的中继", checked=("为每个交易者都创建连接的中继",)))),
            row(cell("测试接入系统名称"), cell(*opts(None, "CTP", "miniCTP", "CTPⅡ mini", "盛立"))),
            row(cell("软件功能"), cell("□自动开平仓     □止盈止损     □批量下单   ", sym("00A3"), run("多账户管理     "), "□交易模型编写     □二次开发")),
        ),
        table(row(cell("测试人签字"), cell("")), row(cell("联系方式"), cell(""))),
        table(
            row(cell("功能性测试"), cell("①登录"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell(""), cell("②行情"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell(""), cell("③委托（开/平仓）"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell(""), cell("④撤单"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell(""), cell("⑤成交"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell("安全性测试"), cell("⑥具备流量控制功能"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
            row(cell(""), cell("⑦具备自动退出功能"), cell(*opts(None, "正常")), cell(*opts(None, "异常："))),
        ),
        para("（请按序号附截图，①③④⑤必须）"), para(""),
        para("************以下部分由国信期货信息科技中心填写************"),
        GX_TABLE,
        # ---------- 程序化报告 ----------
        para("国信期货有限责任公司外部接入系统"), para("程序化测试报告"), para("——XXXX系统"), para("日期：XXXX年XX月XX日"),
        table(
            row(cell("测试系统名称"), cell(""), cell("版本号"), cell("")),
            row(cell("AppID/RelayAppID"), cell("")),
            row(cell("连接模式"), cell(*opts(None, "直连的交易者", "为每个交易者都创建连接的中继"))),
            row(cell("提测方"), cell("名称"), cell("")),
            row(cell(""), cell("联系人"), cell("")),
            row(cell(""), cell("电话"), cell(""), cell("邮箱"), cell("")),
            row(cell("测试方"), cell("名称"), cell("国信期货有限责任公司")),
            row(cell("送测样本"), cell(*opts(None, "测试软件", "技术文档", "其他（ 截图 ）", checked=("其他（ 截图 ）",)))),
            row(cell("测试时间"), cell("")),
            row(cell("测试账号"), cell("")),
            row(cell("交易信息系统名称"), cell(*opts(None, "CTP", "miniCTP", "CTPⅡ mini"))),
            row(cell("程序化交易系统"), cell("测试机型号： 操作系统： CPU：")),
        ),
        table(
            row(cell("测试结果")),
            row(cell("测试项"), cell("测试指标"), cell("测试结果")),
            row(cell("1.接口适应性测试"), cell("1.1.连通性"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell(""), cell("1.2.基础交易功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell("2.异常监测"), cell("2.1.系统连接状态异常监测功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell(""), cell("2.2.报撤单笔数监测功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell(""), cell("2.3.重复报单监测功能（选测）"), cell(*opts(None, "通过", "不通过", "未测试", checked=("未测试",)))),
            row(cell("3.阈值管理"), cell("3.1.阈值设置及预警功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell("4.错误防范"), cell("4.1.交易指令检查功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell(""), cell("4.2.错误提示功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell("5.应急处置"), cell("5.1.暂停交易功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell(""), cell("5.2.批量撤单功能（选测）"), cell(*opts(None, "通过", "不通过", "未测试", checked=("未测试",)))),
            row(cell("6.日志记录"), cell("6.1日志记录功能"), cell(*opts(None, "通过", "不通过", checked=("通过",)))),
            row(cell("测试结论"), cell(*opts(None, "通过", checked=("通过",)))),
            row(cell("测试人员(国信期货)"), cell("")),
        ),
        para("提测方保证测试总结报告真实"), para("提测方（签字/盖章(加盖骑缝章)）："),
        para("测试总结报告证明材料（截图要求请参阅：测试系统功能测试指引）"),
        para("一、接口适应性"),
        para("1.1.连通性（系统登录）"), para("（截图材料）"), para(""),
        para("1.2.基础交易功能（包含开仓、平仓、撤单）"), para("（截图材料：若开平撤在一个界面体现，可合并为一图）"),
        para("开仓"), para(""), para("平仓"), para("撤单"), para(""),
        para("二、异常监测"),
        para("2.1.系统连接状态异常监测功能（系统连接断开提示）"), para("（截图材料）"), para(""),
        para("2.2.报撤单笔数监测功能（统计报单笔数、撤单笔数）"), para("（截图材料：若报单笔数、撤单笔数在一个界面体现，可合并为一图）"),
        para(""), para("报单笔数统计"), para(""), para("撤单笔数统计"), para(""),
        para("2.3.重复报单监测功能（选测）"), para("（截图材料）"), para(""),
        para("三、阈值管理"),
        para("3.1.阈值设置及预警功能（包含报单总笔数、撤单总笔数、重复报单总笔数等指标阈值并预警）"), para("（截图材料）"),
        para("报单总笔数阈值设置+预警"), para(""), para("撤单总笔数阈值设置+预警"), para(""), para("重复报单总笔数阈值设置+预警（选测）"), para(""),
        para("四、错误防范"),
        para("4.1.程序交易指令检查功能（包含但不限于合约代码、合约最小变动价位、单笔委托最大手数）"), para("（截图材料）"), para(""),
        para("合约代码（报单+指令检查 截图一并提供）"), para("合约最小变动价位（报单+指令检查 截图一并提供）"), para("单笔委托最大手数（报单+指令检查 截图一并提供）"), para(""),
        para("4.2.接收柜台错误提示功能（如无足够资金开仓、无仓位可平、市场状态不允许等）"), para("（截图材料）"),
        para("1）无足够资金开仓（报单+错误提示 截图一并提供）"), para("2）无仓位可平（报单+错误提示 截图一并提供）"), para("3）市场状态不允许（报单+错误提示 截图一并提供）"), para(""),
        para("五、应急处置"),
        para("5.1.暂停交易功能（提供系统暂停交易功能截图）"), para("（截图材料）"), para(""),
        para("5.2.批量撤单功能（选测）"), para("（截图材料）"), para(""),
        para("六、日志记录"),
        para("6.1.日志记录功能（包括但不限于交易日志、系统运行记录、监测记录、错误提示记录）"),
        para("（截图材料）1)交易日志2)系统运行记录"), para("3)监测记录4)错误提示记录"),
        para("测试系统功能测试指引:摘自中国期货业协会"),
    ]
    return "".join(parts)


def build_template(path: Path) -> Path:
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>'
                f'{build_body()}<w:sectPr/></w:body></w:document>')
    rels = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{REL}"/>'
    content_types = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{CT}">'
                     '<Default Extension="xml" ContentType="application/xml"/></Types>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", rels)
    return path
