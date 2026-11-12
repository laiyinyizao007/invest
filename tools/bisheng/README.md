# 毕昇工作流运维脚本

本目录是**运维工具**,不是交易/研究内核的一部分;不含任何密钥(登录凭据运行时从 `bisheng-rs/bisheng/docker/.env` 读取,验证码答案取自本机后端日志,仅适用于本机自有实例)。

| 文件 | 作用 |
|---|---|
| `bs.py` | 本机毕昇 API 小工具:登录、请求、multipart 上传 |
| `extract_templates.js` | 从毕昇前端源码 `controllers/API/workflow.ts` 取出节点模板数组 → `templates.json`(`node extract_templates.js templates.json`) |
| `build_v1.py` | 生成并创建工作流 v1(想法→LLM 起名→LLM 写策略→提交→等待本机批准→沙盒回测→输出);`--update <flow_id>` 更新现有版本 |
| `run_v1.py` | 通过 websocket 真实运行 v1:自动回答研究想法;`--auto-approve` 会**代替人**在本机执行 `approve`(仅用于联调,日常应由人读完代码后手工批准) |

在本目录运行(脚本用相对路径读 `templates.json`):
```
node extract_templates.js templates.json
python build_v1.py
python run_v1.py <flow_id> "研究想法"
```
威胁模型与批准机制见 `docs/adr/20261112-research-sandbox-config-and-approval.md`。

## v3 与无 LLM 联调（2026-11-12 新增）

| 文件 | 作用 |
|---|---|
| `bsflow.py` | 节点构造共用 helper（`build_v1/v2/v3.py` 模块末尾直接调 `main()`，不可导入，新脚本统一用它） |
| `build_v3.py` | 生成/更新 v3 工作流（想法→读失败反馈→检索研究资料→LLM 起名→LLM 写候选→提交→本机批准→选参+门槛评估）；`--update <flow_id>`，`--model-id <id>` 换模型 |
| `build_v3_manual.py` / `run_v3_manual.py` | 不用 LLM 的联调版：人输入文件名与源码，`--rounds N` 连跑 N 轮（用来触发迭代上限）；运行脚本会核对到达网关的源码哈希，并用真实的 `research_api.py review`（stdin 输入 y）批准——**仅联调用**，日常应由人读完代码后手工批准 |
| `check_retrieval.py` | 知识库检索自检（不用对话 LLM） |

联调会占用迭代额度并留下候选与结果：测完请清理 `data/research/submissions.jsonl`、`data/research/*.json`、`data/research-control/approvals.json`（**保留 `gateway.token`**）与 `pending_strategies/` 下的测试候选——否则下一轮 LLM 会读到测试垃圾作为「最近失败反馈」。
