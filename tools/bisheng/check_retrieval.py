"""知识库检索自检（不用任何对话 LLM）：开始 -> 输入问题 -> 知识库检索 -> 输出检索结果。
验证 build_v3.py 里检索节点的参数形状是否被毕昇接受，以及 bge-m3 嵌入是否可用。
用法（在含 templates.json 的目录）：python check_retrieval.py "<问题>"
"""
import asyncio
import json
import sys
from pathlib import Path

import websockets

import bs
from bsflow import chain, flow_node, graph, ref, save_flow, set_param

KNOWLEDGE_ID = 5


def build() -> dict:
    start = flow_node("start_c", "start", 0, 0, "开始")
    question = flow_node("input_q", "input", 320, 0, "问题")
    question["data"]["tab"]["value"] = "dialog_input"
    kr = flow_node("kr_kb", "knowledge_retriever", 640, 0, "检索研究资料")
    set_param(kr, "user_question", ["input_q.user_input"])
    set_param(kr, "knowledge", {"type": "knowledge", "value": [{"key": KNOWLEDGE_ID, "label": "invest-研究资料"}]})
    set_param(kr, "retrieved_result", [{"key": "retrieved", "label": "检索结果"}])
    out = flow_node("output_r", "output", 960, 0, "检索结果")
    set_param(out, "message", {"msg": "检索到:\n" + ref("kr_kb", "retrieved"), "files": []})
    end = flow_node("end_c", "end", 1280, 0, "结束")          # 输出节点必须有后继，否则毕昇构建节点时报 NoneType
    return graph([start, question, kr, out, end], chain("start_c", "input_q", "kr_kb", "output_r", "end_c"))


async def run(flow_id: str, data: dict, text: str) -> None:
    token = bs.login()
    url = f"ws://127.0.0.1:7860/api/v1/workflow/chat/{flow_id}"
    async with websockets.connect(url, additional_headers={"Cookie": f"access_token_cookie={token}"}, open_timeout=15, max_size=None) as ws:
        await ws.send(json.dumps({"action": "init_data", "flow_id": flow_id, "data": data}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            cat, body = msg.get("category"), msg.get("message")
            if cat == "user_input" or (isinstance(body, dict) and str(body.get("node_id", "")).startswith("input_") and cat == "input"):
                await ws.send(json.dumps({"action": "input", "flow_id": flow_id, "data": {"input_q": {
                    "data": {"user_input": text, "dialog_files_content": []}, "message": text, "message_id": None,
                    "category": "question", "extra": None, "source": None}}}))
            elif cat == "node_run" and isinstance(body, dict) and body.get("reason"):
                print("[节点失败]", body.get("name"), str(body.get("reason"))[:300])
            elif cat == "output_msg":
                print("[输出]", str(body.get("msg", ""))[:1200])
                return
            elif cat == "error":
                print("[error]", json.dumps(body, ensure_ascii=False)[:400])
                return


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "晋级门槛有哪几条？"
    token = bs.login()
    data = build()
    ids = Path("check_ids.json")
    existing = json.loads(ids.read_text(encoding="utf-8"))["flow_id"] if ids.exists() else None      # 已创建过就更新，毕昇不允许同名重复创建
    flow_id = save_flow(token, "invest 检索自检", "开始->输入问题->知识库检索->输出（不用 LLM）", data, "check_ids.json", existing)
    asyncio.run(run(flow_id, data, question))
