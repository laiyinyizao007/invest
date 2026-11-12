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
