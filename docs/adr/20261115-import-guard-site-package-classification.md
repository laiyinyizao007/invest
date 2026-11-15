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

1. 新增 `research_sandbox.site_package_roots()`:算出**当前解释器**的站点包根
   (`sysconfig.get_paths()` 的 purelib/platlib + `site.getsitepackages()` /
   `getusersitepackages()` + 按 `sys.prefix` 拼的兜底)。
2. 守卫构造时取一次(`self.site_roots`)。
3. 新增 `_is_site_package(resolved)` 与 `_outside_repo(resolved)`:
   **仓库之外 = 物理上不在仓库根之下 _或_ 它是站点包**。
4. 五处分类判定(模块、祖先包、包位置、通配符)统一改用 `_outside_repo()`。

**这是收窄分类,不是放宽防线。** 站点包那一支本来就带「预先存在且此后没被改过」的检查
(第三条的理由),修复只是让分类**对得上 docstring**;仓库自己的代码
(`risk/`、`scripts/`、`trading/`、`strategies/` 等)仍然落进「仓库之内 → 只许在允许目录里」
那一支,一道检查都没松。

## 必须保持不变的

- 候选往站点包里写辅助模块 → 仍被「预先存在」检查挡住(第 24 轮 codex 的原始理由)
- 仓库内的框架/风控/回测模块 → 仍一律被拒
- 受信目录(`strategies/`)的「与仓库提交核对」不受影响
- **判不出站点包时**(取不到根)→ 退回原来的字面口径(fail-closed)

## 验证

新增 `tests/test_import_guard_venv_site_packages.py`,**放行与拦截各配一条**:

| 用例 | 方向 |
|---|---|
| 仓库内的站点包(预先存在)→ 允许导入 | 放行 |
| 仓库外的站点包 → 仍允许(回归) | 放行 |
| 仓库自己的 `risk/evil` → 仍被拒 | 拦截 |
| 候选运行期间往站点包写的模块 → 仍被拒 | 拦截 |
| `site_package_roots()` 真的能取到根 | — |

**同一份用例在修复前是红的**,而且红的理由正是那道误分类
(`禁止导入仓库内模块 thirdparty:它不在允许的目录 ['strategies'] 内`),
不是「用例跑不起来」。

另有一处实测复现:在临时 checkout 里造一个 `.venv/Lib/site-packages/`,
把 `vnpy_ctastrategy` 复制进去,让站点包**真的解析到 checkout 根之下**——
修复前那 4 条用例红,修复后 98 全绿。

## 已知局限

1. **站点包根取自当前解释器**。若候选跑在**另一个**解释器(不同 venv)下,
   那个解释器的站点包不在本守卫的根清单里 → 按字面口径判成「仓库内模块」→ 拒。
   这是**fail-closed 方向**(宁可多拒),但仍可能误杀。当前架构下守卫与候选同解释器,
   所以碰不到。
2. **站点包若被装进仓库的某个允许目录**(如 `strategies/` 下的第三方包),
   仍按「允许目录」那一支处理——多了一道「与仓库提交核对」,比站点包那一支更严。
3. 本次只改**分类**;`check_package_wildcard()` 里「站点包也要过『运行之前就存在』」
   那条原本就有,保持不变。
