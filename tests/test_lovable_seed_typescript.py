"""前端种子(类型 + 示例数据)必须能被 TypeScript 严格模式编译通过。

文档里写着『经 TypeScript 严格模式编译通过』,但仓库里没有任何东西验证这句话——
TypeScript 不在 Python 依赖里,所以这个测试在没装 tsc 的环境里会跳过(理由写在 skip 里,
不会静默消失)。装了 tsc 的环境(前端同事的机器、CI)会真的编译一遍。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "docs" / "web"
SEED = WEB / "lovable-seed"
TSCONFIG = {
    "compilerOptions": {
        "strict": True, "noEmit": True, "target": "ES2020",
        "module": "ESNext", "moduleResolution": "bundler", "skipLibCheck": True,
    },
    "include": ["*.ts"],
}


def _tsc() -> str | None:
    """找 tsc:项目本地 node_modules 优先,再找 PATH 上的。找不到返回 None。"""
    local = Path(__file__).resolve().parents[1] / "node_modules" / ".bin" / "tsc"
    if local.exists():
        return str(local)
    found = shutil.which("tsc")
    return found


def test_the_seed_compiles_under_typescript_strict_mode(tmp_path):
    tsc = _tsc()
    if not tsc:
        pytest.skip("没有 tsc(TypeScript 不在 Python 依赖里);前端同事或 CI 上会跑这条")
    project = tmp_path / "project"
    project.mkdir()
    for f in SEED.glob("*.ts"):
        shutil.copy(f, project / f.name)
    (project / "tsconfig.json").write_text(json.dumps(TSCONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    result = subprocess.run([tsc, "-p", str(project / "tsconfig.json")], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]


def test_every_fixture_has_a_mock_entry_in_the_seed():
    """每个样例都要在种子里(前端 mock 模式用得上)。"""
    import re

    keys = set()
    for f in ("mock-data.phase1.ts", "mock-data.later.ts"):
        keys |= set(re.findall(r'"([a-z0-9-]+)":\s*\{', (SEED / f).read_text(encoding="utf-8")))
    fixtures = {p.stem for p in (WEB / "fixtures").glob("*.json") if p.stem != "manifest"}
    assert fixtures <= keys, sorted(fixtures - keys)
