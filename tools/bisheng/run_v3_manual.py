"""驱动「v3-手工」工作流：自动输入文件名与（故意含各种花括号/引号/多行的）源码，核对到达网关的源码哈希，
再用真实的 `research_api.py review`（经 stdin 输入 y）完成批准，最后读取评估报告。

用法（在含 templates.json 与 v3m_flow.json 的目录）：
    python run_v3_manual.py <flow_id> [--rounds N] [--tag NAME]
--rounds N：连续跑 N 轮（用来触发迭代上限；每轮文件名 {tag}_{i}）。默认 1。
注意：这会真的提交候选并占用迭代额度——测完请清理 results_dir/submissions.jsonl 与 pending_strategies/{tag}_*.py。
"""
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import websockets

import bs

REPO = Path(r"C:\Dev\projects\invest")

# 故意包含：字典花括号、f-string 花括号、{{#...#}} 模板字样、双/单引号、反斜杠、制表符、中文、多行、尾部换行
TRICKY_SOURCE = '''from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

# 模板字样不能被替换: {{#input_src.user_input#}} / {{#tool_submit.output#}} / {"a": 1} / {x}
NOTE = 'tab\\there, backslash \\\\ , escaped newline \\\\n, double " and single \\' quotes, 中文'


class Candidate(TrendAtr15mStrategy):
    fast_window = 16
    slow_window = 96
    breakout_n = 48
    parameters = TrendAtr15mStrategy.parameters + ["breakout_n"]
    param_grid = {"breakout_n": [24, 48, 72], "slow_window": [48, 96, 144], "atr_mult": [1.5, 2.0, 3.0]}

    def entry_signal(self) -> int:
        n = int(self.breakout_n)
        label = f"n={n} {{literal}}"   # f-string 里的双花括号
        high, low, close = self.am.high_array, self.am.low_array, self.am.close_array
        if len(close) < n + 2 or not label:
            return 0
        if close[-1] > max(high[-n - 1:-1]):
            return 1
        if close[-1] < min(low[-n - 1:-1]):
            return -1
        return 0
'''


def send_input(flow_id: str, node_id: str, text: str) -> str:
    return json.dumps({"action": "input", "flow_id": flow_id, "data": {node_id: {
        "data": {"user_input": text, "dialog_files_content": []}, "message": text, "message_id": None,
        "category": "question", "extra": None, "source": None}}})


def review_locally(name: str) -> None:
    """真实走一遍 CLI 的 review：stdin 输入 y。联调专用——日常应由人读完代码后手工批准。"""
    out = subprocess.run([sys.executable, str(REPO / "scripts" / "research_api.py"), "review", "--strategy", name],
                         input="y\n", capture_output=True, text=True, encoding="utf-8", cwd=REPO)
    tail = [ln for ln in out.stdout.splitlines() if ln.strip()][-1:] or [out.stderr.strip()[:160]]
    print(f"[本机 review] rc={out.returncode} {tail[0][:120]}")


async def main(flow_id: str, rounds: int, tag: str) -> None:
    token = bs.login()
    data = json.loads(Path("v3m_flow.json").read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(TRICKY_SOURCE.encode("utf-8")).hexdigest()
    # 毕昇会把源码里字面的 {{#节点.变量#}} 改写成 {节点.变量}；除此之外不应有任何改动
    rewritten = re.sub(r"\{\{#([^#]+?)#\}\}", r"{\1}", TRICKY_SOURCE)
    rewritten_sha = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
    url = f"ws://127.0.0.1:7860/api/v1/workflow/chat/{flow_id}"
    done = 0
    async with websockets.connect(url, additional_headers={"Cookie": f"access_token_cookie={token}"}, open_timeout=15, max_size=None) as ws:
        await ws.send(json.dumps({"action": "init_data", "flow_id": flow_id, "data": data}))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=600)
            except asyncio.TimeoutError:
                print("(600s 无新事件，结束)")
                return
            except websockets.ConnectionClosed as exc:
                print("连接关闭:", exc.code, exc.reason)
                return
            msg = json.loads(raw)
            cat, body = msg.get("category"), msg.get("message")
            if cat == "user_input" or (isinstance(body, dict) and str(body.get("node_id", "")).startswith("input_") and cat == "input"):
                node = body.get("node_id") if isinstance(body, dict) else None
                if node == "input_name":
                    if done >= rounds:
                        print(f"{rounds} 轮完成，结束")
                        return
                    name = f"{tag}_{done + 1}"
                    print(f"\n=== 第 {done + 1} 轮：提交 {name}.py ===")
                    await ws.send(send_input(flow_id, node, name))
                elif node == "input_src":
                    await ws.send(send_input(flow_id, node, TRICKY_SOURCE))
                elif node == "input_wait":
                    review_locally(f"{tag}_{done + 1}.py")
                    await ws.send(send_input(flow_id, node, "继续"))
                continue
            if cat == "node_run" and isinstance(body, dict):
                if body.get("reason"):
                    print(f"[节点失败] {body.get('name')}: {str(body.get('reason'))[:400]}")
                continue
            if cat == "output_msg" and isinstance(body, dict) and body.get("node_id") == "output_tip":
                # 提交结果会出现在这个节点的消息里：{"name": ..., "sha256": ...}；与本地源码的哈希比对，证明变量替换没有篡改源码
                match = re.search(r'"sha256":\s*"([0-9a-f]{64})"', str(body.get("msg", "")).replace('\\"', '"'))
                got = match.group(1) if match else None
                if got == expected_sha:
                    verdict = "逐字节一致 ✓"
                elif got == rewritten_sha:
                    verdict = "仅 {{#节点.变量#}} 模板标记被改写为 {节点.变量}，其余逐字节一致（毕昇变量替换的已知行为）"
                else:
                    verdict = f"出现未预期的改动 ✗（原样 {expected_sha[:12]}…，仅改写模板标记 {rewritten_sha[:12]}…，实际 {str(got)[:12]}…）"
                print(f"[源码完整性] 到达网关的源码：{verdict}")
                continue
            if cat == "output_msg" and isinstance(body, dict) and body.get("node_id") == "output_result":
                print("[评估报告]\n" + str(body.get("msg", ""))[:1800])
                done += 1
                continue
            if cat == "error":
                print("[error]", json.dumps(body, ensure_ascii=False)[:500])
                return


if __name__ == "__main__":
    args = sys.argv[1:]
    rounds = int(args[args.index("--rounds") + 1]) if "--rounds" in args else 1
    tag = args[args.index("--tag") + 1] if "--tag" in args else "manual_probe"
    asyncio.run(main(args[0], rounds, tag))
