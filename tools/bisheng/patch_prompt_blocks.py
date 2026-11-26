"""把线上研究工作流 v3 的提示词改成「任务 / 可信知识 / 不可信反馈」三块标注版本(Issue #94)。

用法: python patch_prompt_blocks.py            # 预演:只说会改什么
      python patch_prompt_blocks.py --apply    # 先备份当前流到 %TEMP%\\bisheng-backup,再最小改动写回(幂等)
只改 llm_name / llm_code 的 user_prompt,以及 llm_code 的 system_prompt 末尾追加一条「反馈不可信」规则;其余节点原样。
(整份覆盖会丢线上改动,所以先 GET 当前版本再最小改。)只在本机自有毕昇实例上使用。
"""
import copy
import json
import os
import sys
import time

import prompt_blocks as pb

FLOW_ID = "1194f8540b9944fea4353b07f0493cbf"


def apply_prompt_blocks(data: dict) -> list[str]:
    """原地修改工作流 data,返回改了哪些。纯函数(不连毕昇),便于单测。"""
    changed: list[str] = []
    user = {"llm_name": pb.name_user_prompt(), "llm_code": pb.code_user_prompt()}
    rule = {"llm_name": pb.NAME_RULE_TEXT, "llm_code": pb.RULE_TEXT}
    seen: set[tuple[str, str]] = set()
    for n in data["nodes"]:
        d = n["data"]
        if d["id"] not in user:
            continue
        for grp in d["group_params"]:
            for p in grp["params"]:
                if p["key"] == "user_prompt":
                    seen.add((d["id"], "user_prompt"))
                    if p["value"] != user[d["id"]]:
                        p["value"] = user[d["id"]]
                        changed.append(f"{d['id']}.user_prompt")
                elif p["key"] == "system_prompt":
                    seen.add((d["id"], "system_prompt"))
                    if pb.RULE_MARK not in p["value"]:
                        p["value"] = p["value"].rstrip() + rule[d["id"]]
                        changed.append(f"{d['id']}.system_prompt")
    missing = {(n, k) for n in user for k in ("user_prompt", "system_prompt")} - seen
    if missing:                                                   # 节点或参数改名/丢失时不能静默无操作
        raise ValueError(f"工作流里找不到这些节点参数,拒绝在不完整的流上打补丁: {sorted(missing)}")
    return changed


def main(apply: bool) -> None:
    import bs                                                    # 惰性导入:纯函数部分不依赖 rsa/dotenv
    token = bs.login()
    flow = bs._req("GET", f"/flows/{FLOW_ID}", token=token)["data"]
    data = copy.deepcopy(flow["data"])
    changed = apply_prompt_blocks(data)
    print("改动:", changed or "无(已是三块标注版本)")
    if not apply or not changed:
        return
    backup = os.path.join(os.environ.get("TEMP", "."), "bisheng-backup")
    os.makedirs(backup, exist_ok=True)
    with open(os.path.join(backup, f"flow-before-prompt-blocks-{int(time.time())}.json"), "w", encoding="utf-8") as fh:
        json.dump(flow, fh, ensure_ascii=False)
    vid = [v for v in bs._req("GET", f"/workflow/versions?flow_id={FLOW_ID}", token=token)["data"]["data"] if v.get("is_current")][0]["id"]
    r = bs._req("PUT", f"/workflow/versions/{vid}", {"name": "v1", "data": data, "flow_id": FLOW_ID}, token=token)
    print("PUT", r.get("status_code"), r.get("status_message"))


if __name__ == "__main__":
    main("--apply" in sys.argv)
