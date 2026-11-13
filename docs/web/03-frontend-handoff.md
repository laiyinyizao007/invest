# 03 前端交接说明

写给接手前端的同事。目标:**不读交易代码、不连交易机,也能把前端做完并通过验收。**

## 1. 你要交付什么

| 交付物 | 说明 |
|---|---|
| 前端源码 | 放 `dashboard/frontend/`(新目录),含依赖锁文件 |
| 构建产物 | 静态文件,输出到 `dashboard/static/`;后端直接托管,**部署时不需要 Node** |
| 单元与组件测试 | 覆盖状态语义、格式化、脱敏相关渲染 |
| 契约测试 | 用 `fixtures/` 校验前端对每种状态的渲染(见 §6) |
| 一页说明 | 如何开发、构建、更新示例数据 |

技术栈**已确定为 React + TypeScript + Tailwind CSS**,用 Lovable 作为开发环境:做法与风险见 [`05-lovable-guide.md`](05-lovable-guide.md),提示词见 [`07-lovable-prompts.md`](07-lovable-prompts.md)。无论用什么工具,都必须满足 §2 的全部约束;导出物入库前必须通过 `tools/audit_frontend_export.py`。

## 2. 硬性约束(验收时逐条检查)

### 2.1 零外部依赖
- 构建产物里**不得出现任何外部 URL**:无 CDN、无 Google Fonts、无统计/埋点脚本、无外链图片。
- 字体用系统字体栈,或随产物自带(woff2)。
- 验收检查:`grep -rE "https?://" dashboard/static/` 除了命名空间 URI(如 `http://www.w3.org/2000/svg`)外必须为空。

### 2.2 与服务器 CSP 兼容
服务器发送的 CSP(见契约 §8):`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; ...`

- **不得有内联 `<script>`、`eval`、`new Function`、`javascript:` URL。** 注意某些构建工具/库默认注入内联脚本或使用 `eval`(如部分 sourcemap 配置、旧版模板编译器),请使用生产构建并确认不依赖它们。
- 样式允许内联(`style-src 'unsafe-inline'`),但优先用 CSS 文件。
- 验收检查:用生产构建在带 CSP 的服务器下打开,控制台**零** CSP 违规。

### 2.3 不得把服务器文本当 HTML
日志行、告警文本、研究报告、策略源码、Markdown 文档里都可能含对端返回的字符串。

- **禁止** `innerHTML`、`v-html`、`dangerouslySetInnerHTML`、`{@html}`、`document.write`。
- 文本一律用框架的默认文本插值(它们会转义),或 `textContent`。
- Markdown 文档页:必须使用**禁用原始 HTML** 的渲染器(如 markdown-it `html:false`、marked + 严格配置),并保证链接不能出站。需要一条测试:输入 `<img src=x onerror=alert(1)>` 与 `<script>` 时,输出里是**文字**而不是元素。
- 验收检查:静态扫描源码无上述 API;运行上面的注入测试。

### 2.4 令牌处理
- 只放 `sessionStorage`(关闭标签页即清除)和 `Authorization` 头。
- 不放 URL(含查询参数、hash)、不放 `localStorage`/Cookie、不 `console.log`、不进错误上报。
- 401 → 清除并回到输入框;不自动用旧令牌重试。
- 令牌输入框用 `type="password"`、`autocomplete="off"`。

### 2.5 只调用契约里的接口
- 请求目标只能是 `status-api.openapi.json` 里的 `paths`;封装在**一个** API 客户端模块里,不要在组件里直接 `fetch`。
- 推荐用 `openapi-typescript` 之类从 OpenAPI 生成类型,类型不匹配时构建失败。
- 验收检查:代码里 `fetch/XMLHttpRequest/axios` 的调用点只有 API 客户端一处。

### 2.6 没有交易类控件
任何阶段都不得出现:下单、撤单、平仓、**一键全平/紧急制动**、连接、订阅、改风控参数、解锁账户、批准/拒绝/**回滚**策略的按钮、表单、快捷键、隐藏菜单。唯一写操作是阶段③的『暂停开仓』,按产品规格 §8 的两步确认实现。

### 2.7 兼容性
- 浏览器:Chrome / Edge / Firefox / Safari 最近两个主版本。
- 不依赖 ServiceWorker、WebSocket(阶段①②),不要求安装为 PWA。

## 3. 状态与数据展示的正确性(最容易出错的部分)

这些规则比样式重要。**评审时优先检查它们。**

1. **先看 `state`,再渲染 `data`。** `data` 为 `null` 是正常的(`missing/corrupt`),不能当作『空列表』。
2. **未知取值按 unknown。** `state`/`level` 出现不认识的值 → 灰色『无法判断』。禁止 `default` 分支落到 ok。
3. **年龄用 `age_seconds`。** 不用浏览器时钟减时间戳。两个例外:『距上次刷新』这种前端自己的计时。
4. **`unknown` ≠ `ok`。** 总状态条、徽标都要有独立外观。
5. **空态 ≠ 缺失。** `state=ok` 且列表为空 → 『无』;`state=missing` → 『未找到数据来源』。
6. **`null` 显示 `—`,不是 0。** 尤其是 `daily_pnl`、`peak_equity` 这类金额。
7. **不重算服务器算好的值**:`level`、`reasons`、`daily_pnl`、`drawdown_from_peak`。
8. **过期时降低对比度而不是隐藏**,并明确写出『已过期 N 秒』。
9. **连接中断横幅出现时**,所有『N 秒前』停止走动或改为『最后更新于 …』,避免给人数据仍在更新的错觉。

## 4. 目录与构建建议

```
dashboard/
  frontend/                  # 你的源码(此目录你说了算)
    src/
      api/                   # 唯一的请求出口 + 由 OpenAPI 生成的类型
      i18n/zh-CN.ts          # 全部界面文案
      components/            # 区块头、状态徽标、过期条、横幅…
      pages/                 # 每个页面一个目录
      theme/                 # 深色/浅色
    package.json  lockfile
    vite.config.* 等
  static/                    # 构建产物(提交到仓库;后端托管)
```

- 构建产物**提交进仓库**,这样部署机不需要 Node。`npm run build` 把产物写到 `dashboard/static/`,然后运行清单工具:

  ```bash
  python tools/build_static_manifest.py dashboard/static            # 生成 manifest.json
  python tools/build_static_manifest.py dashboard/static --check    # 只校验(可放进前端 CI)
  ```

  清单格式是**前后端的硬约定**(不要自己手写):`{"files": {"index.html": "<sha256>", "assets/app-1a2b.js": "<sha256>", ...}}`,键是相对路径(正斜杠),值是文件内容的 SHA-256。
  后端**只服务清单里的文件,而且每次请求都重新校验哈希**:清单之外的文件、构建后又被改动的文件、被换成符号链接的文件,一律 404。所以**改了任何产物文件都必须重新生成清单**,否则那个文件会 404。
  工具会拒绝符号链接、空目录、没有 `index.html` 的目录。`tests/test_build_static_manifest.py` 断言后端接受这个工具写出的一切。
  注意:它与 `docs/web/fixtures/manifest.json`(示例数据清单)同名但完全无关。
- 前端路由(如 `/trading`、`/docs/unattended-log`)刷新时由后端回退到 `index.html`,但**只限白名单里的页面路径**(见 `dashboard/app.py::FRONTEND_ROUTES`)。新增页面路由时需要在那里登记,否则刷新会 404——请在需求里告诉后端同事。
- 资源文件名带内容哈希;`index.html` 引用它们。
- 构建要**可复现**:锁依赖版本,CI 里 `npm ci`。

## 5. 联调流程

```mermaid
flowchart LR
  A[契约冻结 OpenAPI 加示例] --> B[前端用示例数据开发]
  B --> C[后端阶段1 实现]
  C --> D[用真实后端联调]
  D --> E[验收]
```

1. **M0 契约冻结**(已完成初版):你读 `docs/web/`,有疑问提 Issue,不要自己改契约。
2. **M1 用示例数据开发**:搭一个本地 mock 服务,按 `fixtures/manifest.json` 返回示例(`manifest.json` 列出了接口、状态码、示例文件和说明)。可以用 [Prism](https://github.com/stoplightio/prism) 直接基于 OpenAPI 起 mock,或自己写十几行的静态服务。**为了测试所有状态,请让 mock 支持通过查询参数或头切换示例**(如 `?fixture=trading-hard-locked`——仅限开发 mock,不属于真实契约)。
3. **M2 联调**:后端阶段①完成后,在同一台机器上启动真实服务,令牌文件由后端生成。
4. **M3 验收**:见 §7。

## 6. 契约测试(前端侧)

对 `manifest.json` 里的每个示例:

1. 用它作为 API 响应渲染对应页面;
2. 断言:状态徽标、关键数字、过期/缺失/损坏的文案、未知取值回退;
3. 额外构造:把示例的 `state` 改成 `"future_state"`,断言渲染为 unknown。

后端有对应的契约测试(`tests/test_web_contract.py`),保证示例与模型一致;所以前端只需要保证**对所有示例都渲染正确**。

## 7. 验收清单

### 功能
- [ ] 阶段①七个页面(总览、交易与风控、告警与事件、无人值守、行情录制、研究沙盒、证据文档)完整可用
- [ ] 对每个示例(尤其 `*-stale`、`*-missing`、`*-corrupt`、`*-hard-locked`、`overview-warn-unknown`、`recording-stalled`、`live-stale`)渲染符合产品规格
- [ ] 环境横幅常驻,`live` 与 `simnow` 外观明显不同
- [ ] 总状态条:`unknown` 与 `ok` 不同外观
- [ ] 时间同时显示北京时间与本地时间
- [ ] 连接中断横幅、429 退避、401 回到输入框
- [ ] 标签页隐藏时暂停轮询

### 安全
- [ ] §2.1–2.6 全部通过(外链扫描、CSP 零违规、无危险 API、令牌只在 sessionStorage、仅一个 API 出口、无交易控件)
- [ ] HTML 注入测试通过(日志文本、报告、Markdown、源码都按文字显示)
- [ ] 令牌不出现在 URL、控制台、本地存储

### 质量
- [ ] 无障碍:键盘可达、焦点可见、状态不只靠颜色、对比度达标(给出检查报告)
- [ ] 手机宽度下总览与告警页可用
- [ ] 首屏 JS ≤ 300 KB(gzip)
- [ ] 单元/组件测试通过;构建可复现
- [ ] 一页开发说明

## 8. 后端能提供什么、不能提供什么

| 你可能想要 | 状态 |
|---|---|
| 更多示例数据/新增状态示例 | 提 Issue,我们加到 `fixtures/` 并过契约测试 |
| 新字段 | 提 Issue 说明用途;加字段不破坏兼容,通常很快 |
| WebSocket/推送 | 阶段①②不提供;需要实时数据请等阶段② |
| 修改/批准/下单类接口 | **不会提供**,见 ADR 的『明确不做』 |
| 跨域(CORS) | 不提供;页面与 API 同源 |
| 在页面里展示完整账号/路径 | 不会提供;服务器脱敏,前端不得尝试还原 |

## 8b. 能力开关(`GET /api/meta` 的 `capabilities`)

**哪些页面显示,由 `capabilities` 决定,不是由你猜**。启动时取一次 `/api/meta`,按下面的开关决定导航与入口:

| 开关 | 为真时 | 现在 |
|---|---|---|
| `live` | 显示「实时」页(`/api/live`) | 假(阶段②未上线) |
| `equity` | 显示「权益曲线」页(`/api/equity`) | 假 |
| `control` | 显示「控制」页(`/api/control/*`);还需 `whoami.role` 为 `operator` 以上 | 假 |
| `stream` | 允许用 WebSocket/SSE | **恒为假**:阶段①②只用轮询 |
| `research_detail` | 可点进单次运行的详情(`/api/research/runs/{run_id}`) | 真 |
| `data_views` | 显示候选审阅、运行列表、数据与演练、配置等阶段④页面 | 假 |
| `nodes` | 显示「节点」页(`/api/nodes`) | 假 |
| `evolution` | 显示进化引擎三视图(`/api/research/evolution*`) | 假(阶段④未上线;读取侧已就绪,等引擎侧产出数据) |

**开关为假时**:不显示入口,也**不要**直接调那个接口——阶段②③④的接口在对应阶段上线前**不存在**,调用会得到 404。

## 9. 沟通与变更

- 契约问题、示例缺失、阶段范围疑问:在仓库提 Issue,标签 `web`。
- 任何会改变上面『硬性约束』的想法(例如想引入某个外部字体、某个统计服务),先问,不要先做。
- 本目录文档若与 OpenAPI 文件冲突,**以 OpenAPI 文件为准**,并请指出,我们修文档。
