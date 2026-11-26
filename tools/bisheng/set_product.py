"""把线上研究工作流 v3 切到指定品种:改候选生成提示词里的品种名 + 评估节点的 product 参数。

用法: python set_product.py V15        (品种键:C15/V15/EB15/EG15/PP15/M15)
只改这两处,其余节点原样写回(整份覆盖会丢线上改动,所以先 GET 当前版本再最小改)。每次改动前把当前流存到 %TEMP%\bisheng-backup。
不打印任何密钥。仅用于本机自有毕昇实例。"""
import copy
import json
import os
import re
import sys
import time


FLOW_ID = "1194f8540b9944fea4353b07f0493cbf"
NAMES = {"C15": "玉米", "V15": "PVC", "EB15": "苯乙烯", "EG15": "乙二醇", "PP15": "聚丙烯", "M15": "豆粕"}


def _tool_params(d: dict) -> list:
    """节点的「工具参数」分组;找不到就明确报错(不用裸 next:StopIteration 不是给人看的错误,assert 在 -O 下会被去掉)。"""
    for g in d["group_params"]:
        if g["name"] == "工具参数":
            return g["params"]
    raise ValueError(f"节点 {d['id']} 没有「工具参数」分组,拒绝在不完整的流上改动")


def apply_product(data: dict, product: str) -> list[str]:
    """把工作流 data 里与品种相关的三处改成 product(原地修改),返回改了哪些。纯函数,不连毕昇,便于单测。"""
    changed: list[str] = []
    for n in data["nodes"]:
        d = n["data"]
        if d["id"] == "llm_code":
            for grp in d["group_params"]:
                for p in grp["params"]:
                    if p["key"] == "system_prompt":
                        new, k = re.subn(r"为[^()\n]{1,8}\(DCE\)期货", f"为{NAMES[product]}(DCE)期货", p["value"], count=1)
                        if k != 1:
                            raise ValueError("提示词里没找到「为X(DCE)期货」")
                        if new != p["value"]:
                            p["value"] = new
                            changed.append("llm_code.system_prompt")
        if d["id"] == "tool_feedback":
            # Issue #94:失败反馈按品种过滤,**product 必填**(网关不带就 400)——切品种时反馈节点要跟着切
            params = _tool_params(d)
            cur = next((p for p in params if p["key"] == "product"), None)
            if cur is None:
                cur = {"key": "product", "label": "product", "type": "var_textarea", "test": "input",
                       "desc": "评估品种(必填)", "required": True, "value": ""}
                params.append(cur)
            if cur["value"] != product:
                cur["value"] = product
                changed.append("tool_feedback.product")
        if d["id"] == "tool_eval":
            params = _tool_params(d)
            cur = next((p for p in params if p["key"] == "product"), None)
            if cur is None:
                tmpl = next((p for p in params if p["key"] == "random_runs"), None)
                if tmpl is None:
                    raise ValueError("tool_eval 里找不到 random_runs 参数(用它当模板插入 product),拒绝改动")
                cur = copy.deepcopy(tmpl)
                cur.update(key="product", label="product", desc="评估品种", value="")
                params.insert([p["key"] for p in params].index("random_runs") + 1, cur)
            if cur["value"] != product:
                cur["value"] = product
                changed.append("tool_eval.product")
    return changed


def main(product):
    import bs                                      # 惰性导入:apply_product 是纯函数,单测不需要 rsa/dotenv
    if product not in NAMES:
        raise SystemExit(f"品种只能是 {list(NAMES)}")
    t = bs.login()
    flow = bs._req("GET", f"/flows/{FLOW_ID}", token=t)["data"]
    backup = os.path.join(os.environ.get("TEMP", "."), "bisheng-backup")
    os.makedirs(backup, exist_ok=True)
    json.dump(flow, open(os.path.join(backup, f"flow-before-set-{product}-{int(time.time())}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    data = copy.deepcopy(flow["data"])
    changed = apply_product(data, product)
    print("改动:", changed or "无(已是该品种)")
    if not changed:
        return
    vid = [v for v in bs._req("GET", f"/workflow/versions?flow_id={FLOW_ID}", token=t)["data"]["data"] if v.get("is_current")][0]["id"]
    r = bs._req("PUT", f"/workflow/versions/{vid}", {"name": "v1", "data": data, "flow_id": FLOW_ID}, token=t)
    print("PUT", r.get("status_code"), r.get("status_message"))


if __name__ == "__main__":
    main(sys.argv[1])
