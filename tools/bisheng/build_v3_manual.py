"""工作流 v3-手工版（不依赖任何 LLM 的联调工作流）。

与 v3 完全相同的「提交 → 本机批准 → 选参 + 晋级门槛评估 → 结果」链路，只是把两个 LLM 节点（起名、写策略）
换成**人输入**：文件名、源码由人粘贴。用途：
  1. 证伪「含花括号 / 引号 / 多行的源码经毕昇变量替换传给工具会不会被篡改」（run_v3_manual.py 比对 sha256）；
  2. 在 LLM 没有额度时验证提交、批准、评估、报告、迭代上限整条链路；
  3. 结果节点用 view=text，直接显示纯文本中文报告。
用法（在含 templates.json 的目录）：python build_v3_manual.py [--update <flow_id>]
"""
import sys

import bs
from bsflow import chain, flow_node, graph, now, ref, research_tool, save_flow, set_param, tool_tpl

IDS_FILE = "v3m_ids.json"


def build(tool: dict) -> dict:
    child = {c["name"]: c for c in tool["children"]}
    start = flow_node("start_m", "start", 0, 0, "开始")

    name_in = flow_node("input_name", "input", 320, 0, "候选文件名")
    name_in["data"]["tab"]["value"] = "dialog_input"
    src_in = flow_node("input_src", "input", 640, 0, "候选源码")
    src_in["data"]["tab"]["value"] = "dialog_input"

    submit = flow_node("tool_submit", tool_tpl(child["submit_candidate"]), 960, 0, "提交候选")
    set_param(submit, "name", ref("input_name", "user_input"))
    set_param(submit, "source", ref("input_src", "user_input"))

    tip = flow_node("output_tip", "output", 1280, 0, "请本机审阅批准")
    set_param(tip, "message", {"msg": "候选已提交（提交结果：" + ref("tool_submit", "output") + "）。\n请在本机先看代码再批准：\n"
                               "python scripts/research_api.py review --strategy " + ref("input_name", "user_input") + ".py\n"
                               "批准后在下方输入「继续」。批准绑定代码哈希，代码改动后需重新批准。", "files": []})

    wait = flow_node("input_wait", "input", 1600, 0, "等待批准")
    wait["data"]["tab"]["value"] = "dialog_input"

    ev = flow_node("tool_eval", tool_tpl(child["run_evaluate"]), 1920, 0, "选参+晋级门槛评估")
    set_param(ev, "strategy", ref("input_name", "user_input") + ".py")        # 提交接口会自动补 .py，评估接口不会
    set_param(ev, "class", "Candidate")
    set_param(ev, "view", "text")            # 纯文本中文报告；出错时网关仍返回 JSON，原因可读

    res = flow_node("output_result", "output", 2240, 0, "评估报告")
    set_param(res, "message", {"msg": ref("tool_eval", "output"), "files": []})

    nodes = [start, name_in, src_in, submit, tip, wait, ev, res]
    edges = chain("start_m", "input_name", "input_src", "tool_submit", "output_tip", "input_wait", "tool_eval", "output_result",
                  loop_to="input_name")
    return graph(nodes, edges)


def main() -> None:
    token = bs.login()
    data = build(research_tool(token))
    import json
    open("v3m_flow.json", "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
    update = sys.argv[sys.argv.index("--update") + 1] if "--update" in sys.argv else None
    save_flow(token, "invest 研究工作流 v3-手工(无 LLM 联调)", "人输入文件名与源码 -> 提交 -> 本机批准 -> 选参+晋级门槛评估 -> 文本报告",
              data, IDS_FILE, update)
    print(now(), "done")


if __name__ == "__main__":
    main()
