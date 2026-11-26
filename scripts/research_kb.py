"""研究知识库(毕昇 5 号库「invest-研究资料」)的清单与过期检测(Issue #94 PR 2)。

原则:知识库 = 可信、稳定、只读、**单一出处是仓库文档**。库里的文档必须与仓库当前内容一致,否则模型会读到旧口径
(曾出现:库里的晋级门槛 ADR 早于第 9 条 DSR 门槛)。所以:
- 白名单(`WHITELIST`)决定哪些仓库文档进库;结果、尝试记录、反馈、任何账本**永不进库**;
- 同步后写清单(manifest):每个文档的来源路径、内容哈希、仓库 commit、库内文件 id;
- 研究启动前调用 `staleness()`:清单缺失/损坏、白名单变了、文件缺失、哈希不一致 ⇒ 返回问题列表,**调用方必须停止**(fail-closed)。

本模块只做本地、离线的纯逻辑(不连毕昇),便于单测;联网同步在 `tools/bisheng/sync_kb.py`。
哈希口径:换行统一成 `\\n` 后的 sha256(与毕昇返回的 md5 字段同口径,同步脚本上传的也是这份规范化字节),
这样 Windows(CRLF)与 Linux 检出不会互相报"过期"。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

KNOWLEDGE_ID = 5
KB_NAME = "invest-研究资料"
MANIFEST_VERSION = 1

# 进库的仓库文档(相对仓库根)。增删条目 = 改变模型可见的"可信知识",属于执行域改动。
WHITELIST: tuple[str, ...] = (
    "docs/adr/20261109-promotion-gate.md",
    "docs/methodology-mapping.md",
    "docs/roadmap.md",
    "docs/architecture.md",
    "docs/live-readiness.md",
    "docs/adr/20261123-research-round-1-preregistration.md",
)


def kb_name(rel: str) -> str:
    """库里的文件名 = 仓库路径的文件名部分(白名单内必须唯一,见 `check_whitelist`)。"""
    return rel.rsplit("/", 1)[-1]


def check_whitelist(whitelist=WHITELIST) -> None:
    names = [kb_name(r) for r in whitelist]
    if len(set(names)) != len(names):
        raise ValueError(f"白名单里有重名文件,库内无法区分: {names}")
    for r in whitelist:
        if r.startswith("/") or ".." in r.split("/") or "\\" in r:
            raise ValueError(f"白名单只能是仓库内的相对路径: {r}")


def normalized_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(normalized_bytes(path)).hexdigest()


def default_manifest_path(root: Path) -> Path:
    return root / "data" / "research" / "kb_manifest.json"


def build_manifest(root: Path, files: list[dict], commit: str, synced_at: str, whitelist=WHITELIST) -> dict:
    """`files` = [{path, kb_file_id}];哈希在这里按仓库当前内容现算,不信调用方传的。"""
    by_path = {f["path"]: f for f in files}
    entries = []
    for rel in whitelist:
        entries.append({"path": rel, "name": kb_name(rel), "sha256": sha256_of(root / rel),
                        "kb_file_id": by_path.get(rel, {}).get("kb_file_id")})
    return {"version": MANIFEST_VERSION, "knowledge_id": KNOWLEDGE_ID, "kb_name": KB_NAME,
            "repo_commit": commit, "synced_at": synced_at, "files": entries}


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def staleness(root: Path, manifest_path: Path | None = None, whitelist=WHITELIST) -> list[str]:
    """返回问题列表;空 = 知识库与仓库一致。任何读不了/对不上都算问题(fail-closed)。"""
    mp = manifest_path or default_manifest_path(root)
    if not mp.is_file():
        return [f"知识库清单不存在({mp.name}):还没同步过,请先运行 `python tools/bisheng/sync_kb.py --apply`"]
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["知识库清单读不了或已损坏,请重新同步"]
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION \
            or manifest.get("knowledge_id") != KNOWLEDGE_ID or not isinstance(manifest.get("files"), list):
        return ["知识库清单格式不对或指向别的库,请重新同步"]
    recorded = {}
    for f in manifest["files"]:
        if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str):
            recorded[f["path"]] = f["sha256"]
    problems: list[str] = []
    for rel in whitelist:
        if rel not in recorded:
            problems.append(f"白名单文档还没同步进知识库: {rel}")
            continue
        p = root / rel
        try:
            current = sha256_of(p)
        except OSError:
            problems.append(f"仓库里找不到白名单文档: {rel}")
            continue
        if current != recorded[rel]:
            problems.append(f"知识库里的 {rel} 已过期(仓库内容已变)")
    extra = sorted(set(recorded) - set(whitelist))
    if extra:
        problems.append(f"清单里有白名单外的文档(白名单已变): {extra}")
    return problems


def stale_message(problems: list[str]) -> str:
    return ("研究知识库与仓库文档不一致,已停止启动研究(模型会读到旧口径):\n- " + "\n- ".join(problems)
            + "\n请在宿主机运行 `python tools/bisheng/sync_kb.py --apply` 重新同步后再试。")
