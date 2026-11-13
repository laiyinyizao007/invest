"""防线:压缩包与大二进制文件不得进版本库。

起因(2026-11-13):券商发来的 API 文件包(两个共约 40 MB 的 `.rar`)被批量 `git add -A docs` 误带入库,
并随两个远端分支发了出去。`.gitignore` 里的 `docs/API--*/` 带斜杠,只匹配目录,不匹配文件;
而 `tests/test_no_personal_data.py` 为了跳过二进制,**不检查大于 2 MB 的文件**——于是两道防线都没拦住。

这里补上那个洞,且不依赖文件内容:
1. 被跟踪的文件里不得有压缩包(券商文件、导出物都不该以压缩包形式进库);
2. 被跟踪的文件体积不得超过上限——超过的几乎一定是误入库的二进制,要入库须在这里显式登记并写明理由。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SUFFIXES = (".rar", ".zip", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz")
MAX_BYTES = 5_000_000
# 确需入库的大文件在这里登记:{相对路径: 理由}。目前没有。
SIZE_ALLOWLIST: dict[str, str] = {}


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("不在 git 仓库内")
    return [name for name in out.decode("utf-8", errors="replace").split("\0") if name]


def test_no_archive_is_tracked():
    archives = [n for n in _tracked() if n.lower().endswith(ARCHIVE_SUFFIXES)]
    assert not archives, (
        "压缩包不得进版本库(券商文件/导出物请放在本机,并用 .gitignore 忽略):" + ", ".join(archives)
    )


def test_no_oversized_file_is_tracked():
    too_big = []
    for name in _tracked():
        path = ROOT / name
        if name in SIZE_ALLOWLIST or not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_BYTES:
            too_big.append(f"{name} ({size / 1_000_000:.1f} MB)")
    assert not too_big, f"被跟踪的文件超过 {MAX_BYTES // 1_000_000} MB(多半是误入库的二进制):" + ", ".join(too_big)


def test_the_gitignore_covers_archives_in_docs():
    """规则要真能挡住:`docs/` 下的压缩包文件必须被忽略(原先的 `docs/API--*/` 只匹配目录)。"""
    probe = "docs/API--probe-archive.rar"
    result = subprocess.run(["git", "check-ignore", "-q", probe], cwd=ROOT, capture_output=True, timeout=30)
    assert result.returncode == 0, f"{probe} 没有被 .gitignore 忽略"
