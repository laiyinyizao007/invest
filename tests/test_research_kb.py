"""研究知识库清单 / 过期检测 / 同步计划(Issue #94 PR 2)。不连毕昇。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools" / "bisheng"))
sys.path.insert(0, str(ROOT / "tools" / "research_mcp"))

import research_kb as rk       # noqa: E402
import sync_kb                 # noqa: E402

WL = ("docs/a.md", "docs/adr/b.md")


@pytest.fixture
def repo(tmp_path):
    for rel, text in (("docs/a.md", "# A\r\n正文\r\n"), ("docs/adr/b.md", "# B\n")):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return tmp_path


def _synced(repo):
    m = rk.build_manifest(repo, [{"path": r, "kb_file_id": i} for i, r in enumerate(WL)], "abc", "2026-11-25T00:00:00+00:00", WL)
    rk.write_manifest(rk.default_manifest_path(repo), m)
    return m


def test_real_whitelist_is_valid_and_every_file_exists():
    rk.check_whitelist()
    for rel in rk.WHITELIST:
        assert (ROOT / rel).is_file(), rel


def test_whitelist_never_contains_results_or_ledgers():
    for rel in rk.WHITELIST:
        assert rel.startswith("docs/") and rel.endswith(".md"), rel


@pytest.mark.parametrize("bad", [("docs/x/a.md", "docs/y/a.md"), ("/abs.md",), ("../up.md",), ("docs\\a.md",)])
def test_check_whitelist_rejects_duplicates_and_escapes(bad):
    with pytest.raises(ValueError):
        rk.check_whitelist(bad)


def test_in_sync_is_clean_and_crlf_does_not_matter(repo):
    _synced(repo)
    assert rk.staleness(repo, whitelist=WL) == []
    (repo / "docs/a.md").write_bytes("# A\n正文\n".encode())          # 同内容、LF 换行
    assert rk.staleness(repo, whitelist=WL) == []


def test_changed_document_is_stale(repo):
    _synced(repo)
    (repo / "docs/a.md").write_text("# A\n改了\n", encoding="utf-8")
    problems = rk.staleness(repo, whitelist=WL)
    assert problems and "docs/a.md" in problems[0] and "过期" in problems[0]


@pytest.mark.parametrize("breakage", ["no_manifest", "corrupt", "wrong_kb", "bad_version", "files_not_list"])
def test_unreadable_or_foreign_manifest_fails_closed(repo, breakage):
    m = _synced(repo)
    path = rk.default_manifest_path(repo)
    if breakage == "no_manifest":
        path.unlink()
    elif breakage == "corrupt":
        path.write_text("{not json", encoding="utf-8")
    else:
        m.update({"wrong_kb": {"knowledge_id": 6}, "bad_version": {"version": 99}, "files_not_list": {"files": "x"}}[breakage])
        path.write_text(json.dumps(m), encoding="utf-8")
    assert rk.staleness(repo, whitelist=WL)


def test_whitelist_changes_and_missing_files_are_reported(repo):
    _synced(repo)
    assert any("还没同步" in p for p in rk.staleness(repo, whitelist=WL + ("docs/new.md",)))
    assert any("白名单已变" in p for p in rk.staleness(repo, whitelist=WL[:1]))
    (repo / "docs/a.md").unlink()
    assert any("找不到" in p for p in rk.staleness(repo, whitelist=WL))


def test_manifest_hash_is_computed_from_the_repo_not_from_the_caller(repo):
    m = rk.build_manifest(repo, [{"path": "docs/a.md", "kb_file_id": 7, "sha256": "forged"}], "c", "t", WL)
    assert m["files"][0]["sha256"] == rk.sha256_of(repo / "docs/a.md") != "forged"
    assert m["files"][1]["kb_file_id"] is None


def test_sync_plan_add_replace_keep_and_extra(repo):
    live = [{"id": 1, "file_name": "a.md", "md5": rk.sha256_of(repo / "docs/a.md")},      # 一致 ⇒ 保留
            {"id": 2, "file_name": "b.md", "md5": "old"},                                  # 不一致 ⇒ 替换
            {"id": 3, "file_name": "旧复盘.md", "md5": "x"}]                                # 白名单外 ⇒ 只报告
    plan = sync_kb.make_plan(repo, live, WL)
    assert [i["name"] for i in plan["keep"]] == ["a.md"]
    assert plan["replace"] == [{"path": "docs/adr/b.md", "name": "b.md", "old_ids": [2]}]
    assert plan["add"] == [] and plan["extra"] == [{"id": 3, "name": "旧复盘.md"}]
    assert [i["name"] for i in sync_kb.make_plan(repo, [], WL)["add"]] == ["a.md", "b.md"]


def test_case_variant_and_duplicate_copies_are_replaced_not_left_behind(repo):
    h = rk.sha256_of(repo / "docs/a.md")
    plan = sync_kb.make_plan(repo, [{"id": 1, "file_name": "A.MD", "md5": h}], WL)
    assert plan["replace"][0]["old_ids"] == [1] and plan["extra"] == []


def test_symlink_or_duplicate_manifest_entries_are_stale(repo, tmp_path_factory):
    m = _synced(repo)
    m["files"].append(dict(m["files"][0]))
    rk.write_manifest(rk.default_manifest_path(repo), m)
    assert any("重复" in p for p in rk.staleness(repo, whitelist=WL))
    outside = tmp_path_factory.mktemp("out") / "a.md"
    outside.write_text("x", encoding="utf-8")
    (repo / "docs/a.md").unlink()
    try:
        (repo / "docs/a.md").symlink_to(outside)
    except OSError:
        pytest.skip("本机不允许建软链接")
    _synced(repo)
    assert any("仓库内" in p for p in rk.staleness(repo, whitelist=WL))


def test_sync_plan_replaces_duplicate_live_copies(repo):
    h = rk.sha256_of(repo / "docs/a.md")
    live = [{"id": 1, "file_name": "a.md", "md5": h}, {"id": 5, "file_name": "a.md", "md5": h}]
    plan = sync_kb.make_plan(repo, live, WL)
    assert plan["replace"][0]["old_ids"] == [1, 5]                      # 库里有重复副本也不当作一致


def test_mcp_start_research_refuses_when_the_kb_is_stale(monkeypatch, tmp_path):
    import server as mcp_server
    monkeypatch.setattr(mcp_server, "_kb_problems", lambda: ["知识库里的 docs/roadmap.md 已过期(仓库内容已变)"])
    called = []
    monkeypatch.setattr(mcp_server, "invoke", lambda **k: called.append(k))
    out = mcp_server._start_research("某个想法", provenance="user")
    assert "已停止启动研究" in out and "sync_kb.py" in out and not called


def test_mcp_kb_check_failure_is_fail_closed(monkeypatch):
    import server as mcp_server
    monkeypatch.setattr(mcp_server.rkb, "staleness", lambda root, manifest_path=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mcp_server._kb_problems() and "出错" in mcp_server._kb_problems()[0]


def test_a_failed_apply_invalidates_the_manifest_instead_of_leaving_a_false_all_clear(repo, monkeypatch, tmp_path):
    """codex 复核 Critical:先删后传时上传失败,旧清单还在 ⇒ 库缺文档却被判一致。现在改动库之前先作废清单。"""
    manifest = tmp_path / "kb_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    deleted = []

    class FakeBs:
        @staticmethod
        def login():
            return "t"

        @staticmethod
        def _req(method, path, body=None, token=None, **kw):
            if method == "GET":
                return {"data": {"data": [{"id": 2, "file_name": "b.md", "md5": "old", "status": 2}]}}
            deleted.append(path)
            return {"status_code": 200}

        @staticmethod
        def multipart(path, files, token):
            return {"status_code": 500, "status_message": "boom"}

    monkeypatch.setitem(sys.modules, "bs", FakeBs)
    orig = sync_kb.make_plan
    monkeypatch.setattr(sync_kb, "make_plan", lambda root, live: orig(root, live, WL))
    monkeypatch.setattr(sync_kb.rk, "WHITELIST", WL)
    monkeypatch.setattr(sync_kb, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(sync_kb.rk, "default_manifest_path", lambda root: manifest)
    with pytest.raises(SystemExit):
        sync_kb.apply(repo, prune=False)
    assert deleted and not manifest.exists()                      # 旧版已删、上传失败 ⇒ 清单必须已被作废
