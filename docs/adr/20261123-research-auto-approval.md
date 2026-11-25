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
   - 研究侧读取 `is_approved_sha_any`(人工账 或 自动账命中);两本账**都读、各自核链**,任何一本读不了都 fail-closed(人工账命中也不跳过自动账)。
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
  **更新(2026-11-25,Issue #80,用户指示)**:滚动提交上限已由 5 调到 **20**(`window_hours` 仍 24;硬边界 [1,50] 不变),仍是实际的速率闸,只是更松。这是削弱型改动,走了执行域流程;试验账本与 DSR 不受影响(更多提交只会让门槛更严)。上面「未改动」指本 ADR 写成时。
- 运行侧不读自动账,所以自动批准的候选**不能**直接收菜装载;上线仍需人工批准、装载门、准入门等既有步骤,这些不受本文影响。

## G8 复核结果与处置

- 第一轮(zen codereview,`gemini-2.5-pro`):**被阻塞**——免费档配额耗尽(429,约 18 小时后恢复),且返回内含上一个任务(#59)的旧记录,不可当作本次复核;待配额恢复补跑。
- 第二轮(codex,独立探针,探针在仓库外):无 Critical;**运行侧隔离与候选窗口写保护经实测成立**。报告的 2 High / 5 Medium / 5 Low 已逐条本地核对:
  - H1(筛查与哈希分两次读文件)**已修**:哈希取自被筛查的同一份字节;
  - H2(通道校验只在包装层、`_record_row` 可被直接调用写进人工账)**已修**:通道/文件名配对下沉到 `_record_row`,`record_approval` 也不能写自动账;并补「共用写入者必须自己做配对」的不变式测试(守卫由白名单补成不变式);
  - M1(日志先写已批准后落账)**已修**:`approval_requested` → 落账 → `auto_approved`;落账失败记 `failure`;
  - M4(人工账命中就跳过自动账核链)**已修**:两本账都读再判定;
  - M5 的一半**已修**:`config/research_sandbox.json` 显式写出 `auto_approve.enabled=false`;L1/L2/L4/L5 **已修**(拒写自动账、尝试记录全字段脱敏、工具契约说明例外、并列 mtime 选择确定);L3 加了一致性测试;
  - **未修,如实登记**:
    - M2 自动账与尝试账**没有接入** `scripts/research_ledger.py`(链断后无官方重置工具,只有人工移走文件的出路;提示语已改为指向正确账本);
    - M3 尝试账本沿用试验账本的 `allow_ahead=True`(崩在写锚点之前能自愈,代价是「在账本末尾追加自洽行」不被锚点发现——它是审计旁路记录,不是准入判定;本机写权限者仍可伪造);
    - M5 另一半:开关在 git 跟踪的配置文件里(打开会产生未提交改动),且 MCP 服务端与研究栈在 worktree+`INVEST_ROOT` 部署下读的可能是两份配置——开关只被 MCP 服务端读取,所以以数据根(`INVEST_ROOT`)那份为准;
    - 性能:尝试账每次追加全量重读校验,O(n²),不设上限时长期会变慢。

## 回滚

把 `auto_approve.enabled` 改回 `false`(或删掉该配置项)。已写入自动账的批准继续有效(与盘上哈希绑定),但不再产生新的;要作废可人工重置账本。

## 验证

`tests/test_auto_approval.py`(18+ 条):独立账本、拒绝写错文件、改文件即失效、读侧 fail-closed、伪造条目不被采信、候选窗口写不了自动账、
运行侧无引用(AST)、唯一调用点且同函数内先判开关、开关关/开两条服务端路径、记不下记录就不批、筛查拒绝被记录、尝试记录防篡改。
