# 计划:研究沙盒自动批准(独立账本)+ 尝试记录(Issue #67)

- 档位:执行域;风险等级:中(研究域代码执行;实盘链路不动);回滚:`auto_approve.enabled=false`
- 授权:用户 2026-11-23「是」(确认块:授权执行=是,风险=中,方案=独立账方案,回滚=可用);不设次数/时间上限为用户明确选择
- 决议与风险登记:`docs/adr/20261123-research-auto-approval.md`

## 任务
- [x] T1 配置:`auto_approve.enabled`(默认 false)、`paths.auto_approvals_file`、`paths.attempts_file`,路径结构固定
- [x] T2 沙盒:`_record_row`(共用底层)、`record_approval` 拒收 auto 通道、`record_auto_approval`、`is_approved_sha_any`
- [x] T3 研究侧读取点改用 `is_approved_sha_any`(`research_api.check_approval` / `child_approved`);运行侧不动
- [x] T4 MCP 服务端:开关判断 → 先落尝试记录 → 写自动账;尝试记录接入 start/筛查拒绝/批准/推进/失败
- [x] T5 `scripts/research_attempts.py`(哈希链追加)
- [x] T6 测试 `tests/test_auto_approval.py`;边界测试写入者作用域改为 `_record_row`
- [x] T7 CI:ruff 清单与套件清单接入新文件
- [x] T8 文档:ADR、CHANGELOG、PROJECTWIKI
- [x] T9 G8:第一轮 zen 被配额阻塞(429)、第二轮 codex 已做并修正(见 ADR「G8 复核结果与处置」)
- [ ] T10 PR、CI 全绿、合并;开关保持默认关,**由用户在本机打开**

## 复核历史
- 第一轮(zen codereview gemini-2.5-pro):429 配额耗尽,且返回混入 #59 旧记录 ⇒ 视为未做,待补。
- 第二轮(codex):无 Critical;2 High/5 Medium/5 Low,已修 H1/H2/M1/M4/L1-L5,M2/M3/M5 部分如实登记。修后 CI 闸门模拟与相关套件通过。
