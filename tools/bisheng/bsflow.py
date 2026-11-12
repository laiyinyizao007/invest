"""毕昇工作流节点构造的共用 helper（节点模板来自 templates.json，由 extract_templates.js 从毕昇前端源码取出）。

注意：build_v1/v2/v3.py 是早期脚本，模块末尾直接调用 main()，不能被导入，所以新脚本统一用本模块。
所有脚本都在含 templates.json 的目录下运行（相对路径读取）。
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import bs

TEMPLATES_FILE = Path("templates.json")


def templates() -> dict:
    return {t["type"]: t for t in json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))}


def flow_node(node_id: str, tpl, x: int, y: int, name: str) -> dict:
    data = copy.deepcopy(templates()[tpl] if isinstance(tpl, str) else tpl)
    data["id"] = node_id
    data["name"] = name
    data["description"] = data.get("description") if data.get("description") not in ("true", None) else ""
    return {"id": node_id, "type": "flowNode", "position": {"x": x, "y": y}, "data": data}


def set_param(node: dict, key: str, value) -> None:
    for group in node["data"]["group_params"]:
        for param in group["params"]:
            if param["key"] == key:
                param["value"] = value
                return
    raise KeyError(f"{node['id']} 没有参数 {key}")


def edge(src: str, dst: str) -> dict:
    return {"animated": True, "id": f"xy-edge__{src}right_handle-{dst}left_handle", "source": src, "sourceHandle": "right_handle",
            "target": dst, "targetHandle": "left_handle", "type": "customEdge"}


def ref(node_id: str, key: str) -> str:
    """工作流模板里的变量引用：{{#节点.变量#}}"""
    return "{{#" + f"{node_id}.{key}" + "#}}"


def tool_tpl(child: dict) -> dict:
    """与前端 getToolTree 生成的工具节点一致：参数一律是 var_textarea 字符串，输出变量名固定 output。"""
    return {
        "id": "", "tool_key": child["tool_key"], "type": "tool", "name": child["name"], "description": child["desc"],
        "group_params": [
            {"name": "工具参数", "params": [
                {"key": p["name"], "label": p["name"], "type": "var_textarea", "test": "input", "desc": p.get("description", ""),
                 "required": p.get("required", False), "value": ""} for p in child["api_params"]]},
            {"name": "输出", "params": [{"global": "key", "key": "output", "label": "输出变量", "type": "var", "value": ""}]},
        ],
    }


def research_tool(token: str) -> dict:
    tools = bs._req("GET", "/tool?is_preset=0", token=token)["data"]
    return next(x for x in tools if x["name"].startswith("invest"))


def chain(*ids: str, loop_to: str | None = None) -> list[dict]:
    edges = [edge(a, b) for a, b in zip(ids, ids[1:])]
    if loop_to:
        edges.append(edge(ids[-1], loop_to))
    return edges


def graph(nodes: list[dict], edges: list[dict], zoom: float = 0.6) -> dict:
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": zoom}}


def save_flow(token: str, name: str, description: str, data: dict, ids_file: str, update: str | None = None) -> str:
    """创建或更新工作流，返回 flow_id。ids_file 记录 flow_id/version_id，供下次 --update。"""
    if update:
        ids = json.loads(Path(ids_file).read_text(encoding="utf-8"))
        r = bs._req("PUT", f"/workflow/versions/{ids['version_id']}", {"name": "v1", "data": data, "flow_id": update}, token=token)
        print("update:", r.get("status_code"), r.get("status_message"))
        return update
    r = bs._req("POST", "/workflow/create", {"name": name, "description": description, "data": data}, token=token)
    print("create:", r.get("status_code"), r.get("status_message"))
    flow = r["data"]
    Path(ids_file).write_text(json.dumps({"flow_id": flow["id"], "version_id": flow.get("version_id")}), encoding="utf-8")
    print("flow id:", flow["id"], "version:", flow.get("version_id"))
    return flow["id"]


def now() -> str:
    return time.strftime("%H:%M:%S")
