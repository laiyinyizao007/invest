// 从前端源码里取出工作流节点模板数组(它是个 JS 字面量),导出为 JSON
const fs = require("fs");
const src = fs.readFileSync("C:/Dev/projects/bisheng-rs/bisheng/src/frontend/platform/src/controllers/API/workflow.ts", "utf-8");
const start = src.indexOf("const workflowTemplate = [");
if (start < 0) throw new Error("workflowTemplate not found");
let i = src.indexOf("[", start);
let depth = 0, inStr = null, esc = false, end = -1;
for (let j = i; j < src.length; j++) {
  const c = src[j];
  if (inStr) {
    if (esc) esc = false;
    else if (c === "\\") esc = true;
    else if (c === inStr) inStr = null;
    continue;
  }
  if (c === '"' || c === "'" || c === "`") { inStr = c; continue; }
  if (c === "[") depth++;
  if (c === "]") { depth--; if (depth === 0) { end = j; break; } }
}
const literal = src.slice(i, end + 1);
const arr = new Function("return " + literal)();
fs.writeFileSync(process.argv[2], JSON.stringify(arr, null, 1), "utf-8");
console.log("templates:", arr.map(t => t.type).join(", "));
