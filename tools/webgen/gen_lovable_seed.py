"""从 OpenAPI 生成 TypeScript 类型与示例数据模块,供粘贴进 Lovable。重新生成:python tools/webgen/gen_lovable_seed.py;产物在 docs/web/lovable-seed/。"""
import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "docs" / "web"
OUT = WEB / "lovable-seed"
spec = json.loads((WEB / "status-api.openapi.json").read_text(encoding="utf-8"))
S = spec["components"]["schemas"]


def ts(node, depth=0):
    if "$ref" in node:
        n = node["$ref"].rsplit("/", 1)[1]
        return "ApiError" if n == "Error" else n
    if "const" in node:
        return json.dumps(node["const"], ensure_ascii=False)
    if "enum" in node:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in node["enum"])
    if "oneOf" in node:
        return " | ".join(ts(n, depth) for n in node["oneOf"])
    if "allOf" in node:
        return " & ".join(ts(n, depth) for n in node["allOf"])
    t = node.get("type")
    if isinstance(t, list):
        return " | ".join(ts({**node, "type": x}, depth) for x in t)
    if t == "string": return "string"
    if t in ("number", "integer"): return "number"
    if t == "boolean": return "boolean"
    if t == "null": return "null"
    if t == "array": return f"Array<{ts(node.get('items', {}), depth)}>" if "items" in node else "unknown[]"
    if t == "object":
        props = node.get("properties")
        if not props:
            return "Record<string, unknown>"
        req = set(node.get("required", []))
        pad = "  " * (depth + 1)
        lines = []
        for k, v in props.items():
            key = k if k.isidentifier() else json.dumps(k)
            lines.append(f"{pad}{key}{'' if k in req else '?'}: {ts(v, depth + 1)};")
        return "{\n" + "\n".join(lines) + "\n" + "  " * depth + "}"
    return "unknown"


def types_file():
    out = ["// 由 docs/web/status-api.openapi.json 生成,请勿手改;契约变更后重新生成。",
           "// 前端必须容忍未知字段,并把未知的 state / level 取值按 \"unknown\" 处理。", ""]
    for name, node in S.items():
        name = "ApiError" if name == "Error" else name
        desc = node.get("description")
        if desc:
            out.append(f"/** {desc.strip()} */")
        body = ts(node)
        if body.startswith("{"):
            out.append(f"export interface {name} {body}\n")
        else:
            out.append(f"export type {name} = {body};\n")
    out += ["/** 区块外壳:先看 state,再决定是否渲染 data。 */",
            "export type Section<T> = Omit<SectionBase, never> & { data: T | null };", ""]
    # 路径表:便于封装唯一的 API 客户端
    out.append("/** 契约里的全部接口(阶段与最低角色)。API 客户端只允许请求这里列出的路径。 */")
    out.append("export const ENDPOINTS = {")
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            out.append(f"  {op['operationId']}: {{ method: {json.dumps(method.upper())}, path: {json.dumps(path)}, phase: {op['x-phase']}, role: {json.dumps(op['x-role'])} }},")
    out.append("} as const;")
    out.append("export type EndpointName = keyof typeof ENDPOINTS;")
    return "\n".join(out) + "\n"


def mock_file(names, header):
    fx = WEB / "fixtures"
    out = [header, "", "export const mocks: Record<string, unknown> = {"]
    for n in names:
        data = json.loads((fx / f"{n}.json").read_text(encoding="utf-8"))
        out.append(f"  {json.dumps(n)}: {json.dumps(data, ensure_ascii=False)},")
    out.append("};")
    return "\n".join(out) + "\n"


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "api-types.ts").write_text(types_file(), encoding="utf-8")
    manifest = json.loads((WEB / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    phase1 = [m["fixture"] for m in manifest if m["fixture"].startswith(("meta-phase1", "overview", "trading", "alerts", "days", "recording-r", "recording-s", "recording-m", "research", "run-", "doc-", "error-unauthorized", "error-not-found", "error-rate"))]
    later = [m["fixture"] for m in manifest if m["fixture"] not in phase1]
    (OUT / "mock-data.phase1.ts").write_text(mock_file(phase1, "// 阶段①示例数据(全部虚构)。键 = fixtures 文件名;对应接口见 docs/web/fixtures/manifest.json。"), encoding="utf-8")
    (OUT / "mock-data.later.ts").write_text(mock_file(later, "// 阶段②到⑤的示例数据(全部虚构)。"), encoding="utf-8")
    (OUT / "mock-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("phase1:", len(phase1), "later:", len(later))


if __name__ == "__main__":
    main()
