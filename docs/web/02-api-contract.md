# 02 API 契约:通用约定

具体字段以 [`status-api.openapi.json`](status-api.openapi.json) 为准;本文说明 OpenAPI 表达不了的**行为规则**。
示例响应在 [`fixtures/`](fixtures/),由 `tests/test_web_contract.py` 保证与模型一致。

## 1. 基本约定

| 项 | 约定 |
|---|---|
| 地址 | 阶段①只监听 `http://127.0.0.1:<端口>`,默认 8770(可配置)。经局域网访问属于阶段⑤,需另写 ADR |
| 格式 | 请求与响应均为 UTF-8 JSON;静态页面和资源为 HTML/JS/CSS |
| 方法 | 阶段①只有 `GET`(及 `HEAD`)。其它方法一律 405 |
| 版本 | `meta.version`(语义化版本)。**加字段不算破坏性变更;删字段、改类型、改含义才是**,需要升主版本并通知前端 |
| 向前兼容 | 前端必须**忽略未知字段**,并把**未知的 `state` / `level` 取值按 `unknown` 处理**。模型里 `additionalProperties` 默认开放,就是为此 |
| 时间 | 所有时间字段为 ISO 8601 带时区偏移(如 `2026-11-13T08:30:00+08:00`)。例外:来自日志的 `first_ts` / `last_ts` 是北京时间的无时区字符串 `2026-11-12 21:00:03`,按北京时间解释 |
| 金额 | 数字,单位元,浮点;前端只做格式化,不做会累积误差的运算 |
| 空值 | 字段不可得时为 `null`,不省略字段(便于类型生成) |

## 2. 鉴权

- 读接口:`Authorization: Bearer <只读令牌>`。令牌由部署者生成(`data/dashboard/status.token`),**不入版本库**。
- 静态页面(`/`、`/assets/*`)**不需要令牌**(它们不含数据),页面加载后弹出令牌输入框。
- 令牌由前端放在 `sessionStorage`,每次请求通过 Authorization 头发送。**不得**放 URL、`localStorage`、Cookie;不得写进日志或错误上报。
- 401 响应:清除内存中的令牌并回到输入框;**响应体不含任何关于正确令牌的信息**(不提示长度、前缀)。
- 阶段③起写操作使用**另一个**写令牌(`writeToken`)与角色 `operator`;只读令牌即使被拿到也不能写。

## 3. 区块模型(Section)

多数数据接口返回同一种外壳,**把『数据状态』和『数据内容』分开**:

```json
{
  "state": "ok",
  "as_of": "2026-11-13T08:29:56+08:00",
  "age_seconds": 4.0,
  "stale_after_seconds": 60.0,
  "message": null,
  "data": { }
}
```

| 字段 | 说明 |
|---|---|
| `state` | `ok` / `stale` / `missing` / `corrupt` / `unknown`,见产品规格 §5 |
| `as_of` | **数据自身**的时间(文件修改时间或内容里的时间),不是服务器当前时间。缺失时为 `null` |
| `age_seconds` | 服务器计算的 `now - as_of`。前端直接显示,**不要用浏览器时钟重算**(两台机器时钟可能不同) |
| `stale_after_seconds` | 超过它 `state` 变 `stale` |
| `message` | 给人看的说明,非 `ok` 时通常有值 |
| `data` | 内容;`missing` / `corrupt` 时为 `null`,`stale` 时通常仍有值 |

**判断规则(前端)**:先看 `state`,再决定是否渲染 `data`。`state` 非 `ok` 时,绝不能用绿色表示『正常』。

`GET /api/overview` 和 `GET /api/meta` 不是区块,没有这层外壳。

## 4. 总体级别(Overview)

`overview.level ∈ {ok, warn, critical, unknown}`,**由服务器计算**,前端不得自行重算。规则(服务器实现,此处供前端理解):

| level | 触发(任一) |
|---|---|
| `critical` | 账户硬锁定;当日熔断生效;最新一天日志里有严重事件(紧急平仓失败、对账失败、持仓漂移等) |
| `unknown` | 心跳或风控状态文件缺失/损坏/过期,无法判断交易是否在运行或是否安全 |
| `warn` | 待补发告警>0;录制 `stalled`;今日有警告级事件;已手动暂停开仓;额度用尽 |
| `ok` | 所有关键区块 `state=ok` 且无以上情形 |

**可选子系统**:`recording`(行情录制)和 `research`(研究沙盒)是可选的。它们**未部署(区块 `state=missing`)时不计入总体级别**——否则没部署录制的机器总体永远是 `unknown`,警示会被习惯性忽略。但它们的卡片仍显示『缺失』,且一旦存在却 `stale` / `corrupt` / 告警,就照常计入。核心区块 `trading` / `alerts` / `days` 缺失一律计入。

`reasons[]` 是按严重程度排序的原因列表,每项 `{section, level, text}`,供总状态条展示。

> 『心跳过期』归为 `unknown` 而不是 `critical`,因为状态页无法知道交易进程**本来是不是应该在运行**(例如周末、夜盘间隙、尚未启动)。措辞要中性:『无法判断,最近一次心跳是 N 小时前』。

## 5. 错误

HTTP 状态码 + JSON:

```json
{"error": "未授权", "code": "unauthorized"}
```

| 状态码 | `code` | 场景 | 前端处理 |
|---|---|---|---|
| 400 | `bad_request` / `csrf` | 参数不合法;写操作缺 CSRF 或确认词不匹配 | 显示 `error`,不自动重试 |
| 401 | `unauthorized` | 令牌缺失或错误 | 清除令牌,回到输入框 |
| 403 | `forbidden` | 角色不足或能力未开放 | 隐藏入口并提示 |
| 404 | `not_found` | 键/ID 不存在,或不在白名单(含路径穿越企图) | 显示『不存在』 |
| 405 | — | 方法不允许 | 视为前端 bug |
| 429 | `rate_limited` | 超过每分钟请求上限 | 按 `Retry-After` 退避 |
| 500/503 | `internal` / `unavailable` | 服务器自身故障 | 显示连接中断横幅;**不得**把错误当作『数据为空』 |

**区别两类失败**:『数据来源缺失/损坏』是 **HTTP 200** + `state=missing/corrupt`(状态页本身是好的,只是看到的东西有问题);『状态页自己坏了』才是 5xx。前端两种提示要不同。

## 6. 轮询

- 阶段①②**只用轮询**,不用 WebSocket / SSE(契约里没有流式接口;`capabilities.stream` 恒为 `false`,将来若加入再启用)。
- 间隔取 `meta.polling_hint_seconds`;标签页不可见时暂停,重新可见时立即补一次。
- 单端点单在途请求;失败指数退避(2→4→8…上限 60 秒);连续 3 次失败显示『连接中断』横幅。
- 总览页每轮请求 `/api/overview`;进入某个详情页再请求该页的端点。**不要并发拉全部端点。**
- 服务器有每分钟请求上限(默认 120)。按 8 秒间隔一页最多 8 次/分钟,留有余量;不要为『更实时』缩短到秒级——需要实时数据请等阶段②。

## 7. 缓存

- 所有 `/api/*` 响应带 `Cache-Control: no-store`。
- 静态资源文件名带内容哈希,可长期缓存;`index.html` 不缓存。

## 8. 响应头与安全

服务器对所有响应加:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store            (仅 /api/*)
```

含义对前端的约束:**不允许内联脚本、`eval`、外部域名的脚本/样式/字体/图片/请求**;内联样式暂允许(框架常用)。CSP 违规在测试里即视为缺陷。
无 CORS:页面与 API 同源,前端不需要也不得依赖跨域。

## 9. 写操作规则(阶段③)

仅有两个端点:`POST /api/control/pause`(暂停新开仓)、`POST /api/control/resume`(解除,默认关闭)。

1. **独立写令牌**:`Authorization: Bearer <写令牌>`,与只读令牌不同。
2. **CSRF**:请求头 `X-CSRF-Token` 必带。令牌在用户成功输入写令牌后,由 `GET /api/meta` 的响应头 `X-CSRF-Token` 下发(只在写能力开启时存在),前端保存在内存中;缺失或错误 → 400 `csrf`。
3. **确认词**:请求体 `confirm` 必须等于 `"PAUSE"`(或 `"RESUME"`),`reason` 必填(≤200 字)。
4. **幂等**:已暂停时再次暂停返回 200 与当前状态,不报错;但每次调用都写审计。
5. **审计**:每次调用(含被拒绝)写一条,`GET /api/audit` 可见,不可删改。
6. **语义**:暂停只拒绝新开仓,平仓与持仓管理照常。网页**不能**撤销交易进程已经做出的任何动作。
7. **不可扩展**:不得以此为模板新增其它写操作;契约测试断言写操作集合恰好是这两个。

## 10. 各端点的取数与过期阈值

供前端理解『为什么这块会变成 stale』(阈值由服务器配置,真实值在 `stale_after_seconds`):

| 端点 | 数据来源 | 默认过期阈值 |
|---|---|---|
| `/api/trading` | 心跳文件修改时间(进程每 10 秒刷新)+ 风控状态 JSON | 60 秒 |
| `/api/alerts` | 待补发队列文件 + 当日日志 | 300 秒 |
| `/api/days` | 每日日志解析。**只统计有内容的日志**:0 字节的日志文件既不是活动证据也不代表『无异常』;新鲜度取最新非空日志的修改时间 | 600 秒 |
| `/api/recording` | 录制进程的 `status.json`(每 5 秒心跳) | 120 秒 |
| `/api/research` | 提交账本 + 运行结果目录 | **不判过期**(每次请求现读,不依赖周期性写入的进程;`state` 要么 ok 要么 missing / corrupt) |
| `/api/live`(②) | 运行时写出的快照文件 | 15 秒 |
| `/api/equity`(②) | 状态页自己追加的权益序列 | 600 秒 |

## 11. 变更流程

1. 提出变更 → 先改 `status-api.openapi.json` 与对应 `fixtures/`。
2. 运行 `pytest tests/test_web_contract.py`,保证示例与模型一致、阶段①仍只读、没有被禁止的路径。
3. 前端与后端据此各自实现;新增字段前端先忽略,即可平滑上线。
4. 破坏性变更:升 `info.version` 主版本,在本文件末尾记录,并给前端至少一个迭代的过渡期(新旧并存)。

## 变更记录

- 2026-11-12(后端阶段①实现后)写回三项实现决定:可选子系统缺失不计入总体级别;研究区块不判过期;每日日志只统计非空文件(真实数据验证发现:交易进程停 71 小时后别的进程新建了 0 字节日志,曾让页面显示『刚更新』)。

- 2026-11-12 初版(覆盖阶段①到⑤)。
