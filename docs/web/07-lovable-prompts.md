# 07 Lovable 提示词包

按顺序一条一条复制进 Lovable。**每条做完先核对『验收点』,再发下一条。**
使用前先读 [`05-lovable-guide.md`](05-lovable-guide.md)(尤其 §1 风险与 §3.3 项目约束)。视觉细节在 [`06-design-system.md`](06-design-system.md)。

> 提示词用中文写,技术名词保持英文。若 Lovable 对某条理解偏差,把该条拆成更小的两条重发。
> 文中 `〔粘贴〕` 表示需要把仓库里对应文件的内容贴进去;文件位置都在 `docs/web/`。

| 步骤 | 目标 | 阶段 |
|---|---|---|
| P0 | 项目开局与约束 | — |
| P1 | 设计令牌与应用外壳 | — |
| P2 | 类型、API 客户端、示例数据、示例切换器 | — |
| P3 | 令牌输入与连接状态 | — |
| P4 | 通用状态组件 | — |
| P5 | 总览页 | ① |
| P6 | 交易与风控页 | ① |
| P7 | 告警与事件、无人值守 | ① |
| P8 | 行情录制、研究沙盒、证据文档 | ① |
| P9 | 无障碍、响应式、主题 | ① |
| P10 | 质量收尾与导出 | ① |
| P11–P14 | 后续阶段页面(先用示例数据) | ②–⑤ |

---

## P0 项目开局与约束

**发给 Lovable:**

```
创建一个新项目:"invest 状态页",用于监控一个自动交易系统的运行状态。技术栈:React + TypeScript + Tailwind CSS + shadcn/ui + React Router。

〔粘贴 05-lovable-guide.md §3.3 的"项目约束"整段〕

请先做这些,不要做别的:
1. 搭好项目骨架和路由,路由占位:/ (总览)、/trading、/alerts、/days、/recording、/research、/docs/:key。
2. 建立目录:src/api/、src/components/、src/pages/、src/i18n/、src/theme/。
3. 不要接入 Supabase,不要添加登录页、数据库、边缘函数。
4. 检查 index.html:删除所有外部 <script> 和 <link>(包括任何用于在线编辑的脚本),不使用 Google Fonts。
```

**验收点**
- [ ] 项目能跑,七个路由都有占位页
- [ ] `index.html` 里没有任何 `http(s)://` 脚本或样式链接
- [ ] `package.json` 里没有 supabase 相关依赖
- [ ] 没有生成登录页

---

## P1 设计令牌与应用外壳

**发给 Lovable:**

```
实现设计令牌和应用外壳。规范如下(来自设计系统文档):

〔粘贴 06-design-system.md 的 §2 设计令牌(深色与浅色两张表)、§3 玻璃拟态规则与降级 CSS、§4 状态视觉表达表〕

要求:
1. 用 CSS 变量定义全部令牌,Tailwind 通过 theme.extend.colors 引用;组件里不得出现十六进制颜色。
2. 深色为默认主题,同时提供浅色;跟随系统并可手动切换,偏好存 localStorage(只存主题)。
3. 字体用系统字体栈,不引入网络字体。数字使用 tabular-nums。
4. 实现 .glass 样式及三条降级规则(不支持 backdrop-filter、prefers-reduced-transparency、手机宽度<768px)。
5. 应用外壳:顶部"环境横幅"(常驻,不随滚动消失)+"总状态条"+ 侧边导航(手机上变底部或抽屉导航)+ 主内容区。环境横幅和总状态条先用写死的占位数据,后续接 API。
6. 环境横幅四种外观:simnow / guoxin-test 中性蓝灰;live 用 critical 色底并加粗"实盘"字样;unknown 用 warn 色并写"环境未知,请勿据此判断"。
7. 导航分组:监控(总览、交易与风控、告警与事件)、验收(无人值守、行情录制、证据文档)、研究(研究沙盒)。
```

**验收点**
- [ ] 全站没有写死的十六进制颜色(`grep -rE "#[0-9a-fA-F]{6}" src/components src/pages` 为空)
- [ ] 深色与浅色切换正常
- [ ] 手机宽度下玻璃效果关闭、导航可用
- [ ] 四种环境横幅外观明显不同

---

## P2 类型、API 客户端、示例数据、示例切换器

**发给 Lovable:**

```
接下来建立数据层。请严格使用我提供的类型与示例数据,不要自己发明字段。

1. 创建 src/api/types.ts,内容如下(原样,不要改动):
〔粘贴 lovable-seed/api-types.ts〕

2. 创建 src/api/mock-data.phase1.ts 与 src/api/mock-data.later.ts,内容分别如下(原样):
〔粘贴 lovable-seed/mock-data.phase1.ts〕
〔粘贴 lovable-seed/mock-data.later.ts〕

3. 创建 src/api/client.ts —— 整个应用里唯一发起网络请求的地方。它有两种实现,由构建时环境变量 VITE_API_MODE 选择,默认 "mock":
   - mock:根据请求路径返回示例数据。当前"选中的示例"由一个小型状态存储决定(例如选中 "trading-stale" 时,GET /api/trading 返回该示例)。每个接口默认返回它的"正常"示例。
   - http:请求同源的相对路径(/api/...),带 Authorization: Bearer <令牌> 头;令牌从 sessionStorage 读取;处理 401(清除令牌并通知 UI)、429(读 Retry-After)、5xx(抛出可识别的"服务器故障"错误,与"数据缺失"区分开)。
   两种实现对外接口一致,返回类型用 types.ts 里的类型。client.ts 之外的任何文件不得出现 fetch。

4. 创建一个"示例切换器":仅开发模式可见(生产构建里必须不包含它,用 import.meta.env.DEV 条件),右下角抽屉,列出 manifest 里的所有示例(按接口分组,显示说明),点击即切换当前选中的示例并触发页面刷新。示例清单在 lovable-seed/mock-manifest.json:
〔粘贴 lovable-seed/mock-manifest.json〕

5. 创建轮询 Hook:间隔取 GET /api/meta 返回的 polling_hint_seconds;标签页不可见时暂停,重新可见时立即补一次;请求失败时指数退避(2→4→8…上限 60 秒);同一端点同一时刻只有一个在途请求;连续 3 次失败标记"连接中断"。
```

**验收点**
- [ ] `grep -rn "fetch(" src | grep -v src/api` 为空
- [ ] `api-types.ts` 与仓库里的文件一致(逐字节比较)
- [ ] 示例切换器在开发模式出现,生产构建(`npm run build`)的产物里搜不到切换器相关代码
- [ ] 切换示例后,对应接口立刻返回新示例
- [ ] 标签页切到后台后不再发请求(看开发者工具 Network)

---

## P3 令牌输入与连接状态

**发给 Lovable:**

```
实现令牌输入与连接状态:
1. TokenGate:应用启动时若 sessionStorage 里没有令牌,显示一个居中的输入层(玻璃卡片),输入框 type="password"、autocomplete="off"。mock 模式下任意非空字符串都接受;提示文字说明"令牌只保存在本标签页,关闭即清除"。
2. 收到 401:清除令牌,回到输入层,显示"令牌无效或已失效"。不自动重试。
3. 令牌绝不出现在 URL、localStorage、console 输出里。
4. ConnectionBanner:连续 3 次请求失败时,在外壳顶部(环境横幅下方)显示"与状态服务的连接中断,以下数据可能已过期",并让所有"N 秒前"改为"最后更新于 HH:mm:ss"(不再走动)。恢复后自动消失。
5. 429:按 Retry-After 暂停轮询,并在状态条显示"请求过于频繁,稍后自动恢复"。
```

**验收点**
- [ ] 清掉 sessionStorage 后刷新,看到输入层
- [ ] 令牌在 URL、localStorage、控制台里都搜不到
- [ ] 在 mock 里让某接口连续失败(可在客户端加一个开发用故障注入开关),横幅出现且时间不再走动

---

## P4 通用状态组件

**发给 Lovable:**

```
实现下列通用组件(src/components/),每个都要有 Storybook 风格的演示页或在示例切换器里可见:

〔粘贴 06-design-system.md §6 组件清单表〕

关键规则(请严格遵守):
1. StateBadge:state 取值 ok / stale / missing / corrupt / unknown;收到任何未知取值一律按 unknown 渲染,绝不当作 ok。每个状态都是"图标 + 颜色 + 文字"三件套(按设计文档 §4 的表)。
2. SectionCard:接收一个 Section<T>(包含 state、as_of、age_seconds、message、data)。
   - state=ok:正常渲染内容。
   - state=stale:内容保留、整体 60% 不透明度、卡片头显示"数据已过期"徽标,不用绿色。
   - state=missing:不渲染内容,显示"未找到数据来源"和 message。
   - state=corrupt:不渲染内容,显示"数据来源损坏或正在被写入"和 message。
   - 其它:按 unknown。
   卡片头右侧显示 AgeLabel。
3. AgeLabel:显示服务器给的 age_seconds(如"4 秒前");悬停显示绝对时间(北京时间为主,浏览器本地时间在括号内,两者相同则省略)。age_seconds 为 null 时显示"—"。
4. Money/Pnl:金额千分位 2 位小数加"元";盈亏必须带正负号;null 显示"—"而不是 0。盈亏颜色使用独立令牌 gain/loss,不得复用 ok/critical。
5. PlainText:用于报告、日志、源码。保留换行,等宽字体,使用文本节点渲染,绝不解析 HTML。
6. MarkdownView:使用禁用原始 HTML 的 Markdown 渲染器,链接不允许出站(只保留站内锚点)。附一个单元测试:输入 <script>alert(1)</script> 与 <img src=x onerror=alert(1)> 时输出的是文字而不是元素。
```

**验收点**
- [ ] 把示例的 `state` 手改为 `"future_state"`,渲染为 unknown(灰色、问号、"无法判断")
- [ ] 把 stale/missing/corrupt/ok 四种放一起,灰度截图后仍能区分
- [ ] `PlainText` 与 `MarkdownView` 的注入测试通过
- [ ] `null` 金额显示 `—`

---

## P5 总览页

**发给 Lovable:**

```
实现总览页(/),数据来自 GET /api/overview(外壳里的总状态条也用同一份数据)和 GET /api/meta(环境横幅)。

规格:
1. 外壳的环境横幅取 meta.environment;总状态条显示 overview.level(ok→"一切正常"、warn→"需要关注"、critical→"严重"、unknown→"无法判断")和最多 3 条 reasons,点击某条跳转到对应页面。level 由服务器给出,前端不得自行计算;未知取值按 unknown。
2. 五张摘要卡(交易与风控、告警、无人值守、行情录制、研究沙盒),每张显示 headline、StateBadge 和"N 秒前"(用 overview.server_time 即可),点击进入对应页。
3. 卡片排序:critical → warn → unknown → ok;同级别按固定顺序。
4. 布局按设计文档 §5.2:桌面 12 列 Bento 网格,手机单列。
5. 自动刷新用 P2 的轮询 Hook。
```

**验收点**(用示例切换器依次选 `overview-ok`、`overview-warn-unknown`、`overview-critical`)
- [ ] `overview-warn-unknown`:总状态条显示"无法判断"(灰色问号),**不是** ok
- [ ] `overview-critical`:总状态条显示"严重",原因可点击跳转;critical 卡片排最前
- [ ] 手机宽度下总览可读

---

## P6 交易与风控页

**发给 Lovable:**

```
实现 /trading 页,数据来自 GET /api/trading(Section<TradingData>)。完整规格:

〔粘贴 01-product-spec.md §6.2 的内容〕

补充:
1. 进程区:heartbeat.process=running 显示绿色"运行中";stopped 显示中性警示"已停止 N 小时"(不是红色,因为进程可能本来就没开);unknown 显示"无法判断"。
2. 当日盈亏进度条:已亏 / limits.daily_loss_yuan;回撤进度条:drawdown_from_peak / limits.hard_drawdown_yuan;达到 80% 变警示色。limits 里为 null 时不画进度条,只显示数字。
3. hard_locked=true:页面顶部整幅横条"账户已硬锁定",显示 hard_reason 原文(纯文本)。
4. daily_locked_day 等于今天(用 guard.day 比较,不要用浏览器日期):显示"今日已熔断:禁止新开仓"(严重)。
5. pause.paused=true:"已手动暂停开仓,平仓照常"。
6. live_admission.approved:文字写"已存在实盘准入批准文件",并注明"不等于正在实盘"。
7. 账号只显示 account_masked,不做任何还原尝试。
8. 本页没有任何写操作按钮。
```

**验收点**(依次选 `trading-ok`、`trading-stale`、`trading-missing`、`trading-corrupt`、`trading-hard-locked`、`trading-paused`)
- [ ] `trading-stale`:数字置灰并带"数据已过期",**没有"运行中"**
- [ ] `trading-missing` / `trading-corrupt`:不渲染数字,显示对应说明
- [ ] `trading-hard-locked`:顶部整幅硬锁定横条,进度条接近满
- [ ] 页面上找不到任何"下单/撤单/平仓"字样

---

## P7 告警与事件、无人值守

**发给 Lovable:**

```
实现 /alerts 与 /days 两页。完整规格:

〔粘贴 01-product-spec.md §6.3 与 §6.4 的内容〕

补充:
1. /alerts:待补发队列 pending>0 时显示警告"有告警因断网等原因尚未送达手机",并显示最老一条的年龄;recent 事件按日期倒序、severe 在前,级别用徽标(严重/警告)。pending=0 且 recent 为空时显示"无待补发,近期无异常事件"(这是空态,与 missing 不同)。
2. /days:顶部进度"longest_clean_streak / required_streak 天";每日表列:日期、启动次数、成交笔数(0 显示"—")、事件徽标列表(无事件写"无异常")、心跳;has_severe 的行整行加严重标记。
3. 验收清单:checked 为 true/false/null,null 显示"需人工核对"而不是未勾选。
4. missing_weekdays 非空时显示"以下工作日没有日志:…"。
5. first_ts / last_ts 是北京时间的无时区字符串,按北京时间解释并标注。
```

**验收点**(`alerts-empty`、`alerts-events`、`days-sample`)
- [ ] `alerts-empty` 显示的是"无…"而不是缺失态
- [ ] `checked: null` 的清单项显示"需人工核对"
- [ ] 严重事件行视觉上明显区别于警告行,且不只靠颜色

---

## P8 行情录制、研究沙盒、证据文档

**发给 Lovable:**

```
实现 /recording、/research、/docs/:key 三页。完整规格:

〔粘贴 01-product-spec.md §6.5、§6.6、§6.7 的内容〕

补充:
1. /recording:status=stalled 显示警告"已连接但没有订阅到任何合约";last_tick 为 null 在非 stalled 时不要标红(非交易时段是正常的)。
2. /research:显示迭代额度 used/limit(满额时显示"约 N 小时后释放",用 next_release_in_seconds);待审候选列表只显示名称、哈希前 8 位、是否已批准——本页没有任何"批准"按钮;最近运行点击后进入详情(GET /api/research/runs/{run_id})。
3. 运行详情:report 用 PlainText 原样显示(保留换行),下方列 failures;metrics/diagnosis/selection 等对象只做键值表。passed=false 写"未通过",null 写"未判定"。
4. /docs/:key:key 只允许 unattended-log 与 simnow-drills,其它显示"不存在";内容用 MarkdownView(禁用 HTML,链接不出站)。
```

**验收点**(`recording-running`/`-stalled`/`-missing`、`research-sample`、`run-evaluate`、`doc-unattended`)
- [ ] 研究页找不到"批准/拒绝"类按钮
- [ ] 运行详情里 `report` 的换行被保留,且 `<` `>` 之类字符按文字显示
- [ ] `/docs/..%2f.env` 之类 key 显示"不存在"

---

## P9 无障碍、响应式、主题

**发给 Lovable:**

```
做一轮无障碍与响应式检查并修复:
1. 键盘:所有可操作元素可用 Tab 到达,焦点环可见(用 accent 色);弹层有焦点陷阱且 Esc 关闭。
2. 屏幕阅读器:StateBadge 有可读文字;表格有表头关联;自动刷新区域用 aria-live="polite",当总体级别从 ok/warn 变为 critical 时改用 assertive 播报一次。
3. 对比度:深浅两套主题都满足 WCAG AA(正文 ≥4.5:1);状态不得只靠颜色。
4. 尊重 prefers-reduced-motion 与 prefers-reduced-transparency。
5. 响应式:≥1280 桌面完整布局;768–1279 平板单栏、表格可横向滚动;<768 手机,总览与告警页必须可用,顶部固定环境横幅与总状态条。
6. 首屏 JS 体积 ≤ 300 KB(gzip):路由级懒加载,大组件(图表等)延迟加载。
报告你发现并修复了哪些问题。
```

**验收点**
- [ ] 只用键盘能走完所有页面
- [ ] 灰度截图后各状态仍可区分
- [ ] `npm run build` 的体积报告里首屏 JS ≤ 300 KB(gzip)
- [ ] 手机宽度总览与告警页可读

---

## P10 质量收尾与导出

**发给 Lovable:**

```
收尾:
1. 补单元/组件测试(vitest + testing-library):StateBadge 对未知取值回退 unknown;SectionCard 四种 state 的渲染;Money/Pnl 的 null 与符号;PlainText/MarkdownView 的注入测试;轮询 Hook 的退避与暂停。
2. 清理:删除未使用的依赖、组件、示例页;确认 index.html 无外部脚本/样式;确认没有 console.log 输出令牌或响应。
3. 写 README.md:如何开发(mock 模式)、如何构建、环境变量 VITE_API_MODE、如何更新种子文件(api-types.ts / mock-data)。
4. 同步到 GitHub。
```

**验收点 / 导出后动作**(在本机,不在 Lovable)
- [ ] `npm ci && npm test && npm run build` 通过
- [ ] 运行审计:`python tools/audit_frontend_export.py --dist dist --src src --api-dir src/api --strict`,**error 为 0**,warn 逐条人工确认
- [ ] 把产物放进 `dashboard/static/`,源码放进 `dashboard/frontend/`,按 `03-frontend-handoff.md` §7 逐条验收

---

## 后续阶段页面(先用示例数据,后端上线前不显示入口)

以下页面可以提前用 `mock-data.later.ts` 做出来,但导航入口必须由 `meta.capabilities` 控制:未启用就不渲染入口,也不渲染灰色占位按钮。

### P11 阶段②:实时与权益曲线
```
实现 /live 与 /equity(capabilities.live / capabilities.equity 为真时才出现在导航)。规格:
〔粘贴 01-product-spec.md §7〕
重点:快照过期(live-stale)时所有数字置灰并整页顶部显示"实时数据已过期 N 秒";表格行没有任何操作按钮;权益曲线纵轴不得截断到误导,无数据段显示断点不连线,叠加 day_start_equity 基线。
```

### P12 阶段③:控制与审计(仅暂停开仓)
```
实现 /control 与 /audit(capabilities.control 为真且角色为 operator 以上才出现)。规格:
〔粘贴 01-product-spec.md §8 与 02-api-contract.md §9〕
重点:唯一操作是"暂停新开仓",两步确认(必填原因 + 输入确认词 PAUSE),带 X-CSRF-Token;解除暂停默认不在网页开放,只显示"请在交易机上执行 pause_trading.py 解除"。页面上不得出现任何其它写操作。
```

### P13 阶段④:研究与数据视图
```
实现 /research/candidates/:name、/research/runs、/data、/config(capabilities.data_views 为真时出现)。规格:
〔粘贴 01-product-spec.md §9〕
重点:候选审阅是带行号的只读源码视图(PlainText),只显示可复制的 review_command,没有批准按钮;测试段"已消费"要显眼;配置页是只读展示,不是编辑器。
```

### P14 阶段⑤:节点与角色
```
实现 /nodes(capabilities.nodes 为真时出现),并让导航与按钮按 GET /api/whoami 的 role 与 capabilities 双重过滤。规格:
〔粘贴 01-product-spec.md §10〕
重点:reachable=false 的节点显示"失联"并计入 unknown,不得显示为正常。
```

---

## 附:当 Lovable 偏离约束时的纠正话术

| 情况 | 回复 |
|---|---|
| 它建议接 Supabase / 加登录 | "本项目不使用任何后端服务。登录就是一个令牌输入框,令牌放 sessionStorage。请撤销该改动。" |
| 它在组件里直接 `fetch` | "所有网络请求必须只在 src/api/client.ts 里。请把它挪过去。" |
| 它引入网络字体或外链图片 | "不允许任何外部资源。请改用系统字体栈,删除外链。" |
| 它给数据缺失的卡片显示了绿色/正常 | "数据缺失、过期、损坏必须显示为灰色的'未知/过期',绝不能显示正常。请按 StateBadge 规则修正。" |
| 它加了"下单""平仓""紧急制动"按钮 | "这是监控页,不是交易终端。请删除所有交易类控件。" |
| 它自己发明了接口或字段 | "接口与字段以 src/api/types.ts 为准,不得自行发明。请改回。" |
| 它改了 api-types.ts | "api-types.ts 由契约生成,不要手改。请恢复原样。" |
