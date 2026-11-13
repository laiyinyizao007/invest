"""生成 / 校验前端构建产物清单 manifest.json。

用法:
    python tools/build_static_manifest.py dashboard/static            # 生成(覆盖已有 manifest.json)
    python tools/build_static_manifest.py dashboard/static --check    # 只校验,不写文件(可放进前端 CI)

格式(状态页后端 dashboard/app.py::StaticSite 读取的就是它):
    {"files": {"index.html": "<sha256>", "assets/app-1a2b.js": "<sha256>", ...}}
- 键是相对静态目录的路径,一律正斜杠;值是文件内容的 SHA-256(十六进制小写)。
- manifest.json 不列自己。后端**只服务清单内的文件**,而且每次请求都会重新校验哈希:
  清单之外的文件、哈希对不上的文件、被换成符号链接的文件,一律 404。
- 目录里有符号链接、没有 index.html、是空目录,都拒绝生成。

注意:这个 manifest.json 与 docs/web/fixtures/manifest.json(示例数据清单)同名但完全无关。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_NAME = "manifest.json"


class ManifestError(ValueError):
    pass


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ManifestError(f"拒绝符号链接: {p.relative_to(root).as_posix()}(清单里的文件必须是真实文件,符号链接的目标可能被别人改)")
        if p.is_file() and p.relative_to(root).as_posix() != MANIFEST_NAME:
            out.append(p)
    return out


def build_manifest(root: Path) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ManifestError(f"不是目录: {root}")
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in _files(root)}
    if not files:
        raise ManifestError("目录是空的:没有可登记的构建产物")
    if "index.html" not in files:
        raise ManifestError("构建产物里没有 index.html")
    return {"files": dict(sorted(files.items()))}


def write_manifest(root: Path) -> dict:
    manifest = build_manifest(root)
    (Path(root) / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def check_manifest(root: Path) -> list[str]:
    """返回问题清单(空 = 一致)。"""
    root = Path(root).resolve()
    try:
        recorded = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError, AttributeError):
        return [f"{MANIFEST_NAME} 不存在或不是合法 JSON"]
    actual = build_manifest(root)["files"]
    problems = []
    for rel, digest in recorded.items():
        if rel not in actual:
            problems.append(f"{rel}: 清单里有,目录里没有")
        elif actual[rel] != digest:
            problems.append(f"{rel}: 内容与清单里的哈希不符")
    for rel in actual:
        if rel not in recorded:
            problems.append(f"{rel}: 目录里有,清单里没有(后端不会托管它)")
    return problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory", type=Path, help="前端构建产物目录(如 dashboard/static)")
    ap.add_argument("--check", action="store_true", help="只校验现有清单与目录是否一致,不写文件")
    args = ap.parse_args(argv)
    try:
        if args.check:
            problems = check_manifest(args.directory)
            for p in problems:
                print(p)
            print("清单与目录一致。" if not problems else f"{len(problems)} 个问题。")
            return 1 if problems else 0
        manifest = write_manifest(args.directory)
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"已写入 {args.directory / MANIFEST_NAME}:{len(manifest['files'])} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
