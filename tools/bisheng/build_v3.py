"""工作流 v3(15 分钟玉米 + 晋级门槛评估;人工驱动的多轮研究循环):
想法(人输入) -> 读最近失败反馈 -> LLM 起名 -> LLM 写 entry_signal(带反馈) -> 提交候选 -> 提示人本机审阅批准 -> 等人确认 -> 沙盒回测 -> 输出结果 -> 回到「想法」输入(人给下一轮想法,或直接结束对话)。
每一轮都必须经人批准,所以轮数天然由人把控;平台另有 workflow.max_steps 兜底。门槛评估(15 分钟品种)放到后续版本。用法: python build_v2.py"""
import copy
import json
import sys

import bs

T = {t["type"]: t for t in json.load(open("templates.json", encoding="utf-8"))}
MODEL_ID = 7   # MiniMax-M3(毕昇里已配置)

SYSTEM_NAME = "你是命名助手。只输出一个 snake_case 英文名字:仅小写字母、数字、下划线,不超过 24 个字符,不带扩展名,不带引号、空格、换行或任何解释。"
SYSTEM_CODE = """你是量化研究员,为玉米(DCE)期货 **15 分钟 K 线**写一个候选策略(每根 K 线是 15 分钟,窗口参数的单位是「根」,一个交易日约 37 根)。严格遵守:
1. 只输出 Python 源码本身,不要 markdown 围栏,不要任何解释文字。
2. 文件固定结构(只能改 entry_signal 的内部逻辑和类属性参数):
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    # 其余风控与出场逻辑继承自基类,不得修改。可优化参数只有:fast_window / slow_window / atr_mult,以及你自己新增的信号参数
    fast_window = 16
    slow_window = 96
    breakout_n = 48                      # 示例:你自己新增的信号参数(类属性)
    # 新增的参数名必须登记到 parameters,否则引擎不会把它传给策略
    parameters = TrendAtr15mStrategy.parameters + ["breakout_n"]
    # 必须声明 param_grid:恰好三个参数,每个至少 3 个、至多 10 个**严格递增**的数值;不得包含 risk_budget、max_daily_losses、atr_window
    # 服务端会在 2022-06-30 之前的数据上扫描这个网格选参,再在其后的验证窗口评估,所以不要自己挑「最好看」的参数
    param_grid = {"breakout_n": [24, 48, 72, 96], "slow_window": [48, 96, 144], "atr_mult": [1.5, 2.0, 3.0]}

    def entry_signal(self) -> int:
        # 返回 1 做多 / -1 做空 / 0 无信号;只能使用 self.am(ArrayManager)提供的指标:
        # self.am.sma(n, array=True) / ema / rsi / atr / boll / donchian 等,以及 self.am.close_array / high_array / low_array
        return 0
3. 绝对不要导入 os、sys、subprocess、socket、pathlib、requests 等任何系统/网络模块;不要用 open、exec、eval、getattr。
4. 风险控制(3% 止损、300 元风险预算、熔断)由基类固定,不要尝试绕过或修改。
5. 不要使用未来数据:只能用已完成的 K 线;信号在收盘后产生。"""


def flow_node(node_id, tpl, x, y, name):
    data = copy.deepcopy(T[tpl] if isinstance(tpl, str) else tpl)
    data["id"] = node_id
    data["name"] = name
    data["description"] = data.get("description") if data.get("description") not in ("true", None) else ""
    return {"id": node_id, "type": "flowNode", "position": {"x": x, "y": y}, "data": data}


def set_param(node, key, value):
    for group in node["data"]["group_params"]:
        for param in group["params"]:
            if param["key"] == key:
                param["value"] = value
                return
    raise KeyError(f"{node['id']} 没有参数 {key}")


def edge(src, dst):
    return {"animated": True, "id": f"xy-edge__{src}right_handle-{dst}left_handle", "source": src, "sourceHandle": "right_handle",
            "target": dst, "targetHandle": "left_handle", "type": "customEdge"}


def tool_tpl(child):
    return {
        "id": "", "tool_key": child["tool_key"], "type": "tool", "name": child["name"], "description": child["desc"],
        "group_params": [
            {"name": "工具参数", "params": [
                {"key": p["name"], "label": p["name"], "type": "var_textarea", "test": "input", "desc": p.get("description", ""),
                 "required": p.get("required", False), "value": ""} for p in child["api_params"]]},
            {"name": "输出", "params": [{"global": "key", "key": "output", "label": "输出变量", "type": "var", "value": ""}]},
        ],
    }


def ref(node_id, key):
    return "{{#" + f"{node_id}.{key}" + "#}}"


def build(tool):
    child = {c["name"]: c for c in tool["children"]}
    nodes = []
    nodes.append(flow_node("start_v1", "start", 0, 0, "开始"))
    inp = flow_node("input_idea", "input", 320, 0, "研究想法")
    inp["data"]["tab"]["value"] = "dialog_input"
    nodes.append(inp)

    fb = flow_node("tool_feedback", tool_tpl(child["get_recent_failures"]), 480, 0, "读最近失败反馈")
    nodes.append(fb)

    for node_id, name, x, system, user in (
        ("llm_name", "起名", 640, SYSTEM_NAME, "研究想法:" + ref("input_idea", "user_input")),
        ("llm_code", "写策略", 960, SYSTEM_CODE, "研究想法:" + ref("input_idea", "user_input") + "\n策略名:" + ref("llm_name", "output")),
    ):
        n = flow_node(node_id, "llm", x, 0, name)
        n["data"]["tab"]["value"] = "single"
        set_param(n, "model_id", MODEL_ID)
        set_param(n, "temperature", 0.3)
        set_param(n, "system_prompt", system)
        set_param(n, "user_prompt", user)
        set_param(n, "output_user", False)
        nodes.append(n)

    submit = flow_node("tool_submit", tool_tpl(child["submit_candidate"]), 1280, 0, "提交候选")
    set_param(submit, "name", ref("llm_name", "output"))
    set_param(submit, "source", ref("llm_code", "output"))
    nodes.append(submit)

    tip = flow_node("output_tip", "output", 1600, 0, "请本机审阅批准")
    set_param(tip, "message", {"msg": "候选已提交为 " + ref("llm_name", "output") + ".py(提交结果:" + ref("tool_submit", "output") + ")。\n"
                               "请在本机读完代码后执行批准:\npython scripts/research_api.py approve --strategy " + ref("llm_name", "output") + ".py\n"
                               "批准完成后在下方输入「继续」。批准绑定代码哈希,代码改动后需重新批准。", "files": []})
    nodes.append(tip)

    wait = flow_node("input_wait", "input", 1920, 0, "等待批准")
    wait["data"]["tab"]["value"] = "dialog_input"
    nodes.append(wait)

    bt = flow_node("tool_eval", tool_tpl(child["run_evaluate"]), 2240, 0, "选参+晋级门槛评估")
    set_param(bt, "strategy", ref("llm_name", "output") + ".py")
    set_param(bt, "class", "Candidate")
    # random_runs 留空:取服务端配置;参数由候选声明的 param_grid 在服务端选出,不经工作流传递
    nodes.append(bt)

    res = flow_node("output_result", "output", 2560, 0, "门槛评估结果")
    set_param(res, "message", {"msg": "选参 + 晋级门槛评估(选参窗口止于 2022-06-30,验证窗口之后;不触碰测试段):\n" + ref("tool_eval", "output"), "files": []})
    nodes.append(res)
    nodes.append(flow_node("end_v1", "end", 2880, 0, "结束"))

    order = ["start_v1", "input_idea", "tool_feedback", "llm_name", "llm_code", "tool_submit", "output_tip", "input_wait", "tool_eval", "output_result", "end_v1"]
    edges = [edge(a, b) for a, b in zip(order, order[1:])]
    # 回环:输出结果后回到「想法」输入,由人给出下一轮想法;人不再输入即结束(end 节点保留给显式终止)
    edges = [e for e in edges if not (e["source"] == "output_result" and e["target"] == "end_v1")]
    edges.append(edge("output_result", "input_idea"))
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.6}}


def main():
    t = bs.login()
    lst = bs._req("GET", "/tool?is_preset=0", token=t)
    tool = next(x for x in lst["data"] if x["name"].startswith("invest"))
    data = build(tool)
    json.dump(data, open("v3_flow.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if "--update" in sys.argv:
        flow_id = sys.argv[sys.argv.index("--update") + 1]
        ids = json.load(open("v3_ids.json"))
        r = bs._req("PUT", f"/workflow/versions/{ids['version_id']}", {"name": "v1", "data": data, "flow_id": flow_id}, token=t)
        print("update:", r.get("status_code"), r.get("status_message"))
        return
    r = bs._req("POST", "/workflow/create", {"name": "invest 研究工作流 v3(15分钟+门槛)", "description": "人工驱动的多轮研究循环:想法->读失败反馈->LLM 写 15 分钟候选(声明 param_grid)->提交->本机批准->选参+晋级门槛评估->下一轮", "data": data}, token=t)
    print("create:", r.get("status_code"), r.get("status_message"))
    if r.get("status_code") in (200, 201):
        flow = r["data"]
        json.dump({"flow_id": flow["id"], "version_id": flow.get("version_id")}, open("v3_ids.json", "w"))
        print("flow id:", flow["id"], "version:", flow.get("version_id"))


main()
