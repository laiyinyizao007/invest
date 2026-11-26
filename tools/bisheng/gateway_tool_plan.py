"""毕昇里登记的网关工具与仓库 openapi 的差异计划(纯函数,不连毕昇,便于单测)。

背景(记忆笔记《毕昇 API 工具的三个硬约束》):请求体/查询串由登记工具子工具的 `extra`(OpenAPI 操作 JSON)决定,
`api_params` 里有参数不等于会发出去;网关接口改了而登记的工具没跟着改,参数会被静默丢掉(曾让 V 的评估跑在玉米上)。
所以每次改网关接口都要把仓库 openapi 解析出的子工具同步到毕昇里。子工具按**名字**就地更新(id 不变,工作流节点引用不受影响)。
"""
from __future__ import annotations

import copy
import json

FIELDS = ("extra", "api_params", "desc")


def _norm(value):
    """extra/api_params 在毕昇里有时是 JSON 字符串、有时是对象:统一成可比较的规范形式。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _like(live, new):
    """把 new 写成与线上 live **相同的表示**:线上是 JSON 字符串就写字符串,是对象就写对象(codex 复核 Medium)。"""
    if isinstance(live, str) and not isinstance(new, str):
        return json.dumps(new, ensure_ascii=False)
    if not isinstance(live, str) and isinstance(new, str):
        try:
            return json.loads(new)
        except ValueError:
            return new
    return new


def plan(live_children: list[dict], parsed_children: list[dict]) -> list[dict]:
    """逐个子工具比较,返回 [{name, changed: [字段...]}];只在线上有/只在仓库有的子工具用 only_live / only_repo 标出。"""
    live = {c["name"]: c for c in live_children}
    new = {c["name"]: c for c in parsed_children}
    out = []
    for name in sorted(set(live) | set(new)):
        if name not in new:
            out.append({"name": name, "only_live": True, "changed": []})
        elif name not in live:
            out.append({"name": name, "only_repo": True, "changed": []})
        else:
            changed = [f for f in FIELDS if _norm(live[name].get(f)) != _norm(new[name].get(f))]
            if changed:
                out.append({"name": name, "changed": changed})
    return out


def apply_plan(tool: dict, parsed: dict, spec_text: str) -> dict:
    """返回新的 PUT 请求体:顶层 openapi_schema 换成仓库版本;**仅对已存在的**子工具按名字更新有差异的字段。
    线上独有的子工具原样保留,仓库新增的子工具**不在这里添加**(那要新增子工具、可能改变 tool_key,须人工确认)。"""
    body = copy.deepcopy(tool)
    body["openapi_schema"] = spec_text
    new = {c["name"]: c for c in parsed["children"]}
    for child in body["children"]:
        src = new.get(child["name"])
        if src is None:
            continue
        for field in FIELDS:
            if _norm(child.get(field)) != _norm(src.get(field)):
                child[field] = _like(child.get(field), src[field])
    return body
