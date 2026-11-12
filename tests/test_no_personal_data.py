"""防线:本机 .env 里的个人信息(姓名/电话/邮箱/账号)不得出现在任何受版本控制的文件里。

规则:真实个人信息只放本机 `.env`,不进版本库(含测试数据——测试请用虚构占位)。
没有 `.env`(如 CI)或不在 git 仓库内时跳过。失败信息只给出键名与文件位置,不回显取值。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PERSONAL_KEYS = (
    "BROKER_TEST_SUBMITTER", "BROKER_TEST_CONTACT", "BROKER_TEST_PHONE",
    "BROKER_TEST_EMAIL", "CTP_USERNAME",
)
MAX_BYTES = 2_000_000


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("不在 git 仓库内")
    return [ROOT / name for name in out.decode("utf-8", errors="replace").split("\0") if name]


def _pattern(value: str) -> re.Pattern | None:
    value = value.strip()
    if re.search(r"[一-鿿]", value):
        return re.compile(re.escape(value)) if len(value) >= 2 else None
    if len(value) < 3:
        return None
    # ASCII 值要求前后不是字母,避免 "Lai" 误伤 "Laiyin" 之类更长的单词
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])")


def test_personal_values_from_env_never_appear_in_tracked_files():
    env_file = ROOT / ".env"
    if not env_file.exists():
        pytest.skip("没有本机 .env")
    values = dotenv_values(env_file)
    patterns = {key: _pattern(values[key]) for key in PERSONAL_KEYS if values.get(key)}
    patterns = {key: pat for key, pat in patterns.items() if pat}
    if not patterns:
        pytest.skip(".env 中没有可检查的个人信息")

    hits = []
    for path in _tracked_files():
        try:
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for key, pattern in patterns.items():
                if pattern.search(line):
                    hits.append(f"{key} 出现在 {path.relative_to(ROOT)}:{number}")

    assert not hits, "个人信息进入了版本库(只显示位置,不回显取值):\n" + "\n".join(hits[:20])
