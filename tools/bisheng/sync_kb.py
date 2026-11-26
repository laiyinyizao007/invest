"""把白名单里的仓库文档同步到毕昇 5 号库「invest-研究资料」,并写清单(Issue #94 PR 2)。

用法(宿主机、本机自有毕昇实例;Windows 请设 PYTHONIOENCODING=utf-8):
  python tools/bisheng/sync_kb.py            # 预演:只说会新增/替换/保留什么,不改库
  python tools/bisheng/sync_kb.py --apply    # 新增/替换有差异的文档,等入库完成,核对库里的哈希后写清单
  python tools/bisheng/sync_kb.py --check    # 只做本地过期检测(退出码 1 = 过期,研究启动前也会做同一检查)
  python tools/bisheng/sync_kb.py --prune    # 与 --apply 合用:同时删掉库里**不在白名单**的文件(删除会改库,须你明确要求)

规则:只上传白名单文件(换行规范化后的字节);只替换**同名**文件;白名单外的文件默认只报告不删;
结果/尝试记录/反馈/账本永不上传;6~9 号库不碰。清单只在库内容与仓库哈希**逐个核对一致**后才写。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import research_kb as rk  # noqa: E402

PAGE = 100
POLL_SECONDS = 180


def make_plan(root: Path, live: list[dict], whitelist=rk.WHITELIST) -> dict:
    """纯函数:live = 库内文件列表[{id,file_name,md5}]。返回 {add, replace, keep, extra}。"""
    rk.check_whitelist(whitelist)
    by_name: dict[str, list[dict]] = {}
    for f in live:
        by_name.setdefault(f.get("file_name"), []).append(f)
    plan = {"add": [], "replace": [], "keep": [], "extra": []}
    for rel in whitelist:
        name, want = rk.kb_name(rel), rk.sha256_of(root / rel)
        found = by_name.get(name, [])
        if len(found) == 1 and found[0].get("md5") == want:
            plan["keep"].append({"path": rel, "name": name, "kb_file_id": found[0]["id"]})
        elif found:
            plan["replace"].append({"path": rel, "name": name, "old_ids": [f["id"] for f in found]})
        else:
            plan["add"].append({"path": rel, "name": name})
    allowed = {rk.kb_name(r) for r in whitelist}
    plan["extra"] = [{"id": f["id"], "name": f.get("file_name")} for f in live if f.get("file_name") not in allowed]
    return plan


def _list_files(bs, token) -> list[dict]:
    out, page = [], 1
    while True:
        r = bs._req("GET", f"/knowledge/file_list/{rk.KNOWLEDGE_ID}?page_size={PAGE}&page_num={page}", token=token)
        data = (r.get("data") or {}).get("data")
        if data is None:
            raise SystemExit(f"读不到知识库文件列表: {r.get('status_code')} {r.get('status_message')}")
        out += data
        if len(data) < PAGE:
            return out
        page += 1


def _upload(bs, token, root: Path, rel: str) -> str:
    name = rk.kb_name(rel)
    r = bs.multipart(f"/knowledge/upload/{rk.KNOWLEDGE_ID}", {"file": (name, rk.normalized_bytes(root / rel), "text/markdown")}, token)
    path = (r.get("data") or {}).get("file_path")
    if not path:
        raise SystemExit(f"上传 {name} 失败: {r.get('status_code')} {r.get('status_message')}")
    return path


def _commit(root: Path) -> str:
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def apply(root: Path, prune: bool) -> int:
    import bs                                                    # 惰性导入:纯函数部分不依赖 rsa/dotenv
    token = bs.login()
    plan = make_plan(root, _list_files(bs, token))
    todo = plan["add"] + plan["replace"]
    for item in plan["replace"]:                                 # 同名旧版先删(只删同名的),再传新版
        for old in item["old_ids"]:
            r = bs._req("DELETE", f"/knowledge/file/{old}", token=token)
            if r.get("status_code") != 200:
                raise SystemExit(f"删除旧版 {item['name']}(id {old}) 失败: {r.get('status_code')} {r.get('status_message')}")
    if prune:
        for item in plan["extra"]:
            r = bs._req("DELETE", f"/knowledge/file/{item['id']}", token=token)
            print("删除白名单外文件", item["name"], r.get("status_code"))
    if todo:
        paths = [_upload(bs, token, root, item["path"]) for item in todo]
        r = bs._req("POST", "/knowledge/process", {"knowledge_id": rk.KNOWLEDGE_ID, "file_list": [{"file_path": p} for p in paths]}, token=token)
        if r.get("status_code") != 200:
            raise SystemExit(f"提交入库失败: {r.get('status_code')} {r.get('status_message')}")
    deadline = time.time() + POLL_SECONDS
    while True:                                                  # 等入库完成,再逐个核对库里的哈希
        live = _list_files(bs, token)
        final = make_plan(root, live)
        pending = [f for f in live if f.get("status") not in (2, 3)]
        if not final["add"] and not final["replace"] and not pending:
            break
        if time.time() > deadline:
            print("入库超时或哈希对不上,未写清单:", {k: [i["name"] for i in v] for k, v in final.items() if k in ("add", "replace")})
            return 1
        time.sleep(3)
    bad = [f["file_name"] for f in live if f.get("status") == 3 and f["file_name"] in {rk.kb_name(r) for r in rk.WHITELIST}]
    if bad:
        print("这些文档入库失败(status=3),未写清单:", bad)
        return 1
    ids = {f["file_name"]: f["id"] for f in live}
    manifest = rk.build_manifest(root, [{"path": r, "kb_file_id": ids.get(rk.kb_name(r))} for r in rk.WHITELIST], _commit(root),
                                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rk.write_manifest(rk.default_manifest_path(root), manifest)
    print("已同步并写清单:", rk.default_manifest_path(root))
    if final["extra"] and not prune:
        print("提示:库里有白名单外的文件(未删,删除需 --prune):", [i["name"] for i in final["extra"]])
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = rk.staleness(ROOT)
        print(rk.stale_message(problems) if problems else "知识库清单与仓库一致")
        return 1 if problems else 0
    if "--apply" in argv:
        return apply(ROOT, prune="--prune" in argv)
    import bs
    plan = make_plan(ROOT, _list_files(bs, bs.login()))
    for key, label in (("add", "新增"), ("replace", "替换"), ("keep", "保留(哈希一致)"), ("extra", "白名单外(默认不动)")):
        print(f"{label}:", [i["name"] for i in plan[key]] or "无")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
