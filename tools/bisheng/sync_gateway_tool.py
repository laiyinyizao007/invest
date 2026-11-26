"""把仓库 docs/research-gateway.openapi.json 同步进毕昇里登记的网关工具(「invest 研究沙盒网关」)。

用法:
  python sync_gateway_tool.py            # 只预演:列出哪些子工具的 extra/api_params/desc 与仓库不一致
  python sync_gateway_tool.py --apply    # 先备份到 %TEMP%\\bisheng-backup,再 PUT;之后核对子工具 id 未变
  python sync_gateway_tool.py --probe    # (只读)用非法品种打 get_recent_failures,确认 product 真的抵达网关(网关回 400)

只在本机自有毕昇实例上使用;不打印任何密钥。子工具按名字就地更新(id 不变),不新增、不删除子工具。
"""
import json
import os
import sys
import time
from pathlib import Path

import bs
import gateway_tool_plan as gtp

SPEC = Path(__file__).resolve().parents[2] / "docs" / "research-gateway.openapi.json"


def _tool(token):
    return next(x for x in bs._req("GET", "/tool?is_preset=0", token=token)["data"] if x["name"].startswith("invest"))


def _parsed(token, spec_text):
    r = bs._req("POST", "/tool/schema", {"file_content": spec_text}, token=token)
    if r.get("status_code") != 200:
        raise SystemExit(f"解析 openapi 失败: {r.get('status_code')} {r.get('status_message')}")
    return r["data"]


def probe(token, tool):
    child = next(c for c in tool["children"] if c["name"] == "get_recent_failures")
    r = bs._req("POST", "/tool/test", {
        "server_host": tool["server_host"], "openapi_schema": tool["openapi_schema"], "extra": child["extra"],
        "auth_method": tool["auth_method"], "auth_type": tool["auth_type"], "api_key": tool["api_key"],
        "api_location": tool.get("api_location") or "", "parameter_name": tool.get("parameter_name") or "",
        "request_params": {"product": "ZZ99"}}, token=token)
    print("探测(非法品种,应得到网关 400 的 product 白名单提示):", str(r.get("data"))[:200])


def main(argv):
    token = bs.login()
    tool = _tool(token)
    spec_text = SPEC.read_text(encoding="utf-8")
    parsed = _parsed(token, spec_text)
    steps = gtp.plan(tool["children"], parsed["children"])
    print("差异:", json.dumps(steps, ensure_ascii=False) if steps else "无(登记的工具与仓库一致)")
    if "--probe" in argv:
        probe(token, tool)
    if "--apply" not in argv or not steps:
        return
    backup = os.path.join(os.environ.get("TEMP", "."), "bisheng-backup")
    os.makedirs(backup, exist_ok=True)
    with open(os.path.join(backup, f"tool-before-sync-{int(time.time())}.json"), "w", encoding="utf-8") as fh:
        json.dump(tool, fh, ensure_ascii=False)
    body = gtp.apply_plan(tool, parsed, spec_text)
    r = bs._req("PUT", "/tool", body, token=token)
    print("PUT", r.get("status_code"), r.get("status_message"))
    after = _tool(token)
    ids_before = {c["name"]: c["id"] for c in tool["children"]}
    ids_after = {c["name"]: c["id"] for c in after["children"]}
    print("子工具 id 不变:", ids_before == ids_after, "| 数量", len(ids_after))
    left = gtp.plan(after["children"], parsed["children"])
    print("同步后仍有差异:", left or "无")
    if ids_before != ids_after or left:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
