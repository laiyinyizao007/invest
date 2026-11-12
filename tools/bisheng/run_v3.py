"""运行 v1 工作流:自动回答「研究想法」输入;在等待批准节点处暂停,按需在本机执行 approve(模拟人读过代码后的动作)后再继续。
用法: python run_v3.py <flow_id> "<想法1>" ["<想法2>" ...] [--auto-approve]  (想法用完即停止,验证多轮回环)"""
import asyncio
import json
import re
import subprocess
import sys

import websockets

import bs

AUTO_APPROVE = "--auto-approve" in sys.argv


def send_input(flow_id, node_id, text):
    return json.dumps({"action": "input", "flow_id": flow_id, "data": {node_id: {
        "data": {"user_input": text, "dialog_files_content": []}, "message": text, "message_id": None,
        "category": "question", "extra": None, "source": None}}})


async def main(flow_id, ideas):
    token = bs.login()
    data = json.load(open("v3_flow.json", encoding="utf-8"))
    url = f"ws://127.0.0.1:7860/api/v1/workflow/chat/{flow_id}"
    cand_name = None
    async with websockets.connect(url, additional_headers={"Cookie": f"access_token_cookie={token}"}, open_timeout=15, max_size=None) as ws:
        await ws.send(json.dumps({"action": "init_data", "flow_id": flow_id, "data": data}))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=240)
            except asyncio.TimeoutError:
                print("(240s 无新事件,结束)")
                return
            except websockets.ConnectionClosed as exc:
                print("连接关闭:", exc.code, exc.reason)
                return
            msg = json.loads(raw)
            cat = msg.get("category")
            body = msg.get("message")
            text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
            if cat == "user_input" or (isinstance(body, dict) and body.get("node_id", "").startswith("input_") and cat in ("input",)):
                node_id = body.get("node_id") if isinstance(body, dict) else None
                print(f"[等待输入] {node_id}")
                if node_id == "input_idea":
                    if not ideas:
                        print("想法已用完,结束(多轮回环验证完成)")
                        return
                    await ws.send(send_input(flow_id, node_id, ideas.pop(0)))
                elif node_id == "input_wait":
                    if not AUTO_APPROVE:
                        print("需要人工批准,未带 --auto-approve,停止。")
                        return
                    out = subprocess.run([sys.executable, r"C:\Dev\projects\invest\scripts\research_api.py", "approve", "--strategy", cand_name + ".py"],
                                         capture_output=True, text=True, encoding="utf-8", cwd=r"C:\Dev\projects\invest")
                    print("[本机 approve]", out.returncode, out.stdout.strip().replace("\n", " ")[:120], out.stderr.strip()[:200])
                    await ws.send(send_input(flow_id, node_id, "继续"))
                continue
            if cat == "node_run" and isinstance(body, dict):
                if body.get("reason"):
                    print(f"[node_run 失败] {body.get('name')}: {body.get('reason')}")
                for block in body.get("log_data") or []:
                    for item in block:
                        if item.get("key", "").endswith(".output") and body.get("node_id") == "llm_name":
                            cand_name = str(item.get("value")).strip()
                            print(f"[起名] {cand_name!r}")
                        if item.get("key", "").endswith(".output") and body.get("node_id") in ("tool_submit", "tool_eval"):
                            print(f"[{body.get('name')}] {str(item.get('value'))[:300]}")
                continue
            if cat in ("output_msg", "stream_msg", "stream_over", "error", "end_cover"):
                if cat == "output_msg" or cat == "error":
                    print(f"[{cat}] {text[:600]}")
            if cat == "processing" or msg.get("type") == "close":
                if msg.get("status") == "end" or msg.get("type") == "close":
                    print("流程结束")
                    return


asyncio.run(main(sys.argv[1], [a for a in sys.argv[2:] if not a.startswith("--")]))
