# ADR: 研究沙盒自动批准(独立账本)与尝试记录

- 状态:已采纳(2026-11-23);Issue #67
- 档位:**执行域**(改批准链:`research_sandbox.py` / `research_api.py` / `research_config.py` / `tools/research_mcp/server.py` 均在不可降级清单)
- 取代:`20261123-research-round-1-preregistration` 的 **§2「最多再提交 3 次」**(其余条款不变)。
- 关联:`20261120-inchat-approval-confirmation`(会话内哈希回贴,本文在其之上再放宽一档)

## 用户授权与风险接受(原话)

用户 2026-11-23:「我不想要手动贴,我要的就是自动跑,这个你应该改规则」「完全不限,但是我知道它试了很多次,所以我也可以在实盘前有所考量」。
选择:自动批准**不设次数与时间上限**;尝试记录放 `data/research/attempts.jsonl` + 报告。

## 决定

1. **独立账本**:新增 `data/research-control/auto_approvals.json`(同哈希链/锚点/独占锁,`via=auto-research`)。
   - 写入口 `research_sandbox.record_auto_approval`:目标文件名必须是 `auto_approvals.json`;与人工账共用底层 `_record_row`。
   - `record_approval`(人工账写入者)**拒收** `via=auto-research`,两本账不会串。
   - 研究侧读取 `is_approved_sha_any`(人工账 或 自动账命中);两本账**各自核链、各自 fail-closed**。
2. **运行侧不读自动账**:`trading/`、`risk/approval_ledger.py`、`risk/approved_guard.py`、收菜 `research_collect.py`、准入/晋级相关文件
   一律只认人工账——自动批准的候选没有人读过源码,**不得借此进入装载链路**。有 AST 测试锁住(`tests/test_auto_approval.py`)。
3. **开关**:`config/research_sandbox.json` 的 `auto_approve.enabled`,**默认 false**;读不到/非布尔一律当关(fail-closed)。
   开关是配置文件而不是工具参数,模型不能改;关闭即回到会话内手动贴哈希。
4. **保留的防线**:静态筛查必须先过;哈希与盘上文件内容绑定(代码改一个字节批准即失效);候选执行窗口的审计钩子不放行写控制目录;链被改/账本读不了 ⇒ 拒绝采信。
5. **尝试记录** `data/research/attempts.jsonl`(`scripts/research_attempts.py`,哈希链追加):
   `start` / `screening_rejected` / `auto_approved` / `manual_approved` / `advanced` / `failure`,含被拒与失败的尝试。
   **自动批准前先落记录,记不下就不批**。

## 已知风险(如实登记,用户已接受)

- **模型生成的代码不经人读就在本机研究沙盒里执行。** 静态筛查是黑名单,不是隔离;候选窗口的审计钩子也「不是安全边界」(见 `research_sandbox.py` 的 #31 注释:同进程候选无法被彻底隔离,#29 的独立进程/容器才是闭合方案)。
  本改动**没有**加强隔离。
- **不设上限 ⇒ 多重检验**:试验次数继续涨(DSR 分母),门槛只会更难过;出现「过关」候选时必须按「试了很多次」读。
  `config` 里网关的滚动提交上限(`iteration.max_iterations=5 / window_hours=24`)**未改动**,仍是实际的速率闸。
- 运行侧不读自动账,所以自动批准的候选**不能**直接收菜装载;上线仍需人工批准、装载门、准入门等既有步骤,这些不受本文影响。

## 回滚

把 `auto_approve.enabled` 改回 `false`(或删掉该配置项)。已写入自动账的批准继续有效(与盘上哈希绑定),但不再产生新的;要作废可人工重置账本。

## 验证

`tests/test_auto_approval.py`(18+ 条):独立账本、拒绝写错文件、改文件即失效、读侧 fail-closed、伪造条目不被采信、候选窗口写不了自动账、
运行侧无引用(AST)、唯一调用点且同函数内先判开关、开关关/开两条服务端路径、记不下记录就不批、筛查拒绝被记录、尝试记录防篡改。
