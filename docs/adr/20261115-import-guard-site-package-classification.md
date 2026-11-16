# ADR: 导入守卫按「是不是本仓库的代码」分类,而不是按「物理上是否在仓库根之下」

- 状态:已采纳(2026-11-15)
- 关联:Issue [#47](https://github.com/laiyinyizao007/invest/issues/47)
- 相关:`docs/adr/20261112-research-sandbox-groundwork.md`(研究沙盒)、`docs/adr/20261113-evolution-engine.md`(不可变宪法)

## 背景

沙盒的导入守卫 `scripts/research_sandbox.py::RepoImportGuard` 按**路径**裁决候选能不能导入某个模块。
它的类 docstring 写了三条规则:

1. 在仓库根**之下**、但不在允许目录里 → 拒绝(框架、风控、回测模块一律不许碰)
2. 在允许目录里 → 还必须「候选开始运行之前就存在、且此后没被改过」
3. 在仓库根**之外**(标准库、站点包)→ 也必须「候选开始运行之前就存在」

第三条的**理由**(第 24 轮 codex 记的)写得很清楚:站点包在宿主机上通常可写,
候选往里面写一个辅助模块就能绕开仓库内的路径规则。

**但第三条是按「物理上是否在仓库根之下」实现的。** 本仓库的 venv 恰好在仓库根之内
(`.venv/`),于是站点包里的模块**落进第一条**被直接拒掉——第三条那条更宽松、也更正确的规则
(带「预先存在」检查)**从来没被应用过**。

## 现象

在主 checkout 里用仓库自带的 venv 跑研究侧测试,4 条红:

```
tests/test_research_api.py::test_a_stale_pyc_cannot_replace_the_hashed_source
tests/test_research_api.py::test_evaluate_child_really_runs_the_evaluation
tests/test_research_api.py::test_a_candidate_file_with_a_bom_still_loads
tests/test_research_api.py::test_a_candidate_that_rewrites_a_framework_object_at_import_time_is_refused
```

候选侧的报错是 `禁止导入仓库内模块 vnpy_ctastrategy:它不在允许的目录 ['strategies'] 内`——
`vnpy_ctastrategy` 是站点包,不是本仓库的代码。

**CI 永远看不到**:CI 在 runner 上现装依赖,站点包在 checkout 之外,走的是第三条。
这条路径只在本地主 checkout 里出现。

## 决策

**判据改成「这是不是本仓库自己的代码」,而不是「物理上是否在仓库根之下」。**

1. 新增 `research_sandbox.site_package_roots()`:算出**当前解释器**的站点包根,
   并且**只保留真的是站点包目录的**(basename ∈ {site-packages, dist-packages} 且存在)。
   (Windows 上 `site.getsitepackages()` 返回的是 prefix 本身,照单全收等于把整个
   venv / 整个 Python 安装目录当成信任区。)
2. 新增 `research_sandbox.installed_site_files(roots)`:从各发行版的
   `*.dist-info/RECORD` 读出**安装记录**(路径 → sha256 + 大小),按站点包根**模块级缓存**
   (索引一次要扫 8.5 万个条目,逐个 `Path.resolve()` 太贵;不做 resolve,只做字符串拼接)。
3. 守卫构造时取一次(`self.site_roots` / `self.site_installed`)。
4. 新增 `_is_site_package(resolved)` 与 `_outside_repo(resolved)`:
   **仓库之外 = 物理上不在仓库根之下 _或_ 它是「站点包里安装好的第三方库文件」**。
   「安装好的」= 在安装记录里 **且** 内容(sha256 + 大小)没被改过。
5. **四处**分类判定(模块、祖先包、包位置、通配符)统一改用 `_outside_repo()`。

**为什么必须用「安装记录」而不是「mtime 早于本轮起点」**(第 4 轮 codex 复核的 H1):
守卫是**每个子进程重建一次**的(`research_api.py` 每轮刷新 `started_at_ns`),
所以「mtime 早于本轮起点」**挡不住跨轮次**:候选第 1 轮写进站点包的辅助模块,
第 2 轮就能导入它并被执行(实测:白名单里的真实包 `numpy`,辅助模块拿到真内建)。
仓库自己早就证过「只比 mtime 不可靠」(`_changed()` 的注释:
「改写内容再把 mtime 还原」能骗过),受信目录正因如此补了 sha256 + 仓库提交基准。
**安装记录就是站点包的「提交基准」等价物**:
不在记录里 = 安装之后才出现的文件 → 不算第三方库;在记录里但哈希变了 → 被改写过 → 不算。

**这是收窄分类,不是放宽防线。** 仓库自己的代码
(`risk/`、`scripts/`、`trading/`、`strategies/` 等)仍然落进「仓库之内 → 只许在允许目录里」
那一支,一道检查都没松。

## 必须保持不变的

- 候选往站点包里写辅助模块 → 仍被拒(**跨轮次也拒**:文件不在安装记录里)
- 装好的站点包文件被改写 → 仍被拒(内容与安装记录对不上)
- 仓库内的框架/风控/回测模块 → 仍一律被拒
- 受信目录(`strategies/`)的「与仓库提交核对」不受影响
- **判不出站点包时**(取不到根 / 装记录取不到)→ 退回原来的字面口径(fail-closed)

## 验证

新增 `tests/test_import_guard_venv_site_packages.py`(9 条),**放行与拦截各配一条**,
并按仓库自己的标准写成**动态探针**(真的写文件、真的 import、真的看结果):

| 用例 | 方向 |
|---|---|
| 仓库内、装好的站点包 → 允许导入 | 放行 |
| 同上,但**走真实导入路径**(`safe_builtins` + 导入门) | 放行 |
| 仓库外的站点包 → 仍允许(回归) | 放行 |
| 仓库自己的 `risk/evil`、`scripts/framework` → 仍被拒 | 拦截 |
| 候选往站点包里**写出来**的模块 → 仍被拒 | 拦截 |
| **跨轮次**:第 1 轮写的,第 2 轮(新守卫)导 → 仍被拒 | 拦截 |
| 装好的站点包文件**被改写** → 仍被拒 | 拦截 |
| `site_package_roots()` 真的能取到根(API 存在性探针) | — |

**同一份用例在修复前是红的**(3 failed / 6 passed),而且红的理由正是那道误分类
(`禁止导入仓库内模块 thirdparty:它不在允许的目录 ['strategies'] 内`),
不是「用例跑不起来」。

另有一处实测复现:在临时 checkout 里造一个 `.venv/Lib/site-packages/`,
把 `vnpy_ctastrategy` 连同它的 `dist-info/RECORD` 一起复制进去,让站点包
**真的解析到 checkout 根之下**——修复前那 4 条用例红,修复后 98 全绿。

跨轮次那条的实测(在那个布局里直接探守卫):
第 1 轮往站点包写辅助模块 → 第 2 轮**新守卫**导入 → 被拒;
改写一个装好的包文件 → 被拒;没动过的装好的包 → 放行。

## 已知局限

1. **站点包根取自当前解释器**。若候选跑在**另一个**解释器(不同 venv)下,
   那个解释器的站点包不在本守卫的根清单里 → 按字面口径判成「仓库内模块」→ 拒。
   这是**fail-closed 方向**(宁可多拒),但仍可能误杀。当前架构下守卫与候选同解释器,
   所以碰不到。
2. **站点包若被装进仓库的某个允许目录**(如 `strategies/` 下的第三方包),
   仍按「允许目录」那一支处理——多了一道「与仓库提交核对」,比站点包那一支更严。
3. 本次只改**分类**;`check_package_wildcard()` 里「站点包也要过『预先存在』」
   那条原本就有,保持不变。
4. **站点包在仓库之外时的跨轮次逃逸,本次没有动**(那是 CI 布局,原本就存在):
   那一支仍只比 mtime。候选若在**站点包(仓库外)**里写辅助模块,下一轮仍可能导进来
   (第 4 轮 codex 实测:origin/main 上即如此)。本 PR 只保证**仓库内**的站点包
   不再把这条洞打开——安装记录这道基准同时覆盖了跨轮次与跨会话。
   **登记为独立 Issue 跟进**,不在本 PR 范围内(改那一支会动到 CI 布局的既有行为)。
