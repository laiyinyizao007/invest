# ADR: 研究沙盒参数全部配置化,人工批准绑定到代码哈希

- 状态:已采纳(2026-11-12,用户确认全部四项并要求「全部参数化,放在配置文件里」)
- 设计过程:`docs/plans/20261112-bisheng-integration-spec.md`
- 关联:`20261110-bisheng-ai-researcher.md`、`20261112-research-sandbox-groundwork.md`、`20261112-candidate-evaluation.md`

## 决策

1. **人工批准 = 本机 CLI + 内容哈希**。`research_api.py approve --strategy F` 在人读过代码后执行,把文件 sha256 记入批准记录;
   `backtest` / `gate` 只运行哈希已记录的代码。代码一改哈希即变,批准自动失效;批准的是内容而不是文件名。
   外层(毕昇)没有任何端点能写批准记录。`--confirm-reviewed` 保留为「现在读过并批准」的快捷方式,同样落哈希记录。
2. **失败反馈由服务端限制条数**(默认最近 3 条),不依赖工作流提示词约束。
3. **工作流最大迭代轮数**默认 5,作为配置项与引擎 `recursion_limit` 共同限制。
4. **无人值守的连续迭代(4c)之前,必须先做容器级隔离**(无网络、数据只读挂载、无 `.env`、资源限额)。在此之前不做任何无人值守的连续迭代。
5. **全部可调参数集中在 `config/research_sandbox.json`**(随仓库、不含密钥),由 `scripts/research_config.py` 加载:
   - 文件缺失 → 用内置默认;文件存在但非法(未知键、类型错、越界、路径越出项目根)→ **拒绝运行**,绝不悄悄回退默认。
   - 安全相关数值有**硬边界**,配置能调参但不能把边界调没:`feedback.limit` ∈ [1,10]、`iteration.max_iterations` ∈ [1,50]、
     `gate.random_runs` ∈ [20,2000]、`gateway.host` 不得为 `0.0.0.0`/`::`、`candidates.name_pattern` 不得放开路径分隔符、
     `paths.pending_dir` 不得指向 `strategies`/`approved`/`risk` 等受保护目录、`gateway.token_file` 不得借用 `.env`。
   - `screening.extra_forbidden_*` 只能**追加**禁用项,内置清单不能被删减。
   - 令牌本身不进配置文件,配置里只有令牌**文件路径**(位于被 .gitignore 的 `data/` 下)。
   - 命令行参数(`--timeout`、`--random-runs`)可覆盖配置默认,但同样受硬边界约束。

## 实现状态(2026-11-12)

- 网关 `scripts/research_gateway.py` 已实现(标准库,无新增依赖)并经真实 socket 实测;`confirm` 字段在 HTTP 层被忽略。
- **待决:容器到宿主的连通方式。** 网关监听 `127.0.0.1` 时毕昇容器访问不到(实测 `host.docker.internal` 解析到 `172.20.10.5`,与当前网络 `10.32.7.121` 不符,连接超时)。
  选项:(a) 绑定到某个宿主网卡 IP——会暴露到该网络,仅靠令牌保护,不建议;(b) **把网关与沙盒一起放进容器**(与毕昇同一 compose 网络、仅只读挂载数据、无 `.env`),这同时满足决策 4 的容器隔离前置,是推荐方向;(c) 经 Tailscale 等私网地址。需要用户选择。

## 未决/后续
- `iteration.max_iterations`、`feedback.limit` 已在配置里,但要等网关与毕昇工作流才会被消费。
- 容器隔离(决策 4)的具体方案另立 ADR。
