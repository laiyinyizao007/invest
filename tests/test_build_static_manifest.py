"""tools/build_static_manifest.py:前端构建产物清单的生成与校验。后端 StaticSite 读取的就是这个格式。"""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_static_manifest", ROOT / "tools" / "build_static_manifest.py")
b = importlib.util.module_from_spec(SPEC)
sys.modules["build_static_manifest"] = b
SPEC.loader.exec_module(b)


def build_dir(tmp_path, files):
    d = tmp_path / "static"
    for rel, body in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(body)
    return d


def test_manifest_lists_every_file_with_its_sha256_using_forward_slashes(tmp_path):
    d = build_dir(tmp_path, {"index.html": b"<html>", "assets/app-1a2b.js": b"x=1", "assets/sub/font.woff2": b"\x00\x01"})
    m = b.build_manifest(d)
    assert m == {"files": {"assets/app-1a2b.js": hashlib.sha256(b"x=1").hexdigest(), "assets/sub/font.woff2": hashlib.sha256(b"\x00\x01").hexdigest(),
                           "index.html": hashlib.sha256(b"<html>").hexdigest()}}


def test_the_manifest_never_lists_itself(tmp_path):
    d = build_dir(tmp_path, {"index.html": b"x", "manifest.json": b"{}"})
    assert "manifest.json" not in b.build_manifest(d)["files"]


def test_output_is_deterministic(tmp_path):
    d = build_dir(tmp_path, {"b.js": b"2", "a.js": b"1", "index.html": b"i"})
    assert json.dumps(b.build_manifest(d)) == json.dumps(b.build_manifest(d))
    assert list(b.build_manifest(d)["files"]) == sorted(b.build_manifest(d)["files"])


def test_symlinks_are_refused(tmp_path):
    d = build_dir(tmp_path, {"index.html": b"x"})
    outside = tmp_path / "outside.js"
    outside.write_bytes(b"secret")
    try:
        os.symlink(outside, d / "link.js")
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    with pytest.raises(b.ManifestError, match="符号链接"):
        b.build_manifest(d)


def test_an_empty_or_index_less_build_is_refused(tmp_path):
    with pytest.raises(b.ManifestError, match="index.html"):
        b.build_manifest(build_dir(tmp_path, {"assets/a.js": b"1"}))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(b.ManifestError):
        b.build_manifest(empty)


def test_write_then_check_roundtrip_and_detects_tampering(tmp_path):
    d = build_dir(tmp_path, {"index.html": b"<html>", "assets/a.js": b"1"})
    b.write_manifest(d)
    assert b.check_manifest(d) == []
    (d / "assets" / "a.js").write_bytes(b"2")
    problems = b.check_manifest(d)
    assert problems and "assets/a.js" in problems[0]
    (d / "assets" / "extra.js").write_bytes(b"not in manifest")
    assert any("extra.js" in p for p in b.check_manifest(d))


def test_the_backend_accepts_exactly_what_this_tool_writes(tmp_path):
    """前后端的约定:本工具写出的清单,后端 StaticSite 必须全部接受(没有被 rejected 的文件),并能逐个读到。"""
    pytest.importorskip("fastapi")
    from dashboard.app import StaticSite
    d = build_dir(tmp_path, {"index.html": b"<title>app</title>", "assets/app-1a2b.js": b"console.log(1)", "assets/app-1a2b.css": b"b{}"})
    b.write_manifest(d)
    site = StaticSite(d)
    assert site.rejected == []
    for rel in ("index.html", "assets/app-1a2b.js", "assets/app-1a2b.css"):
        assert site.read(rel) is not None, rel
    assert site.has_index


def test_cli_exit_codes(tmp_path, capsys):
    d = build_dir(tmp_path, {"index.html": b"<html>"})
    assert b.main([str(d)]) == 0 and (d / "manifest.json").exists()
    assert b.main([str(d), "--check"]) == 0
    (d / "index.html").write_bytes(b"changed")
    assert b.main([str(d), "--check"]) == 1
    assert b.main([str(tmp_path / "nope")]) == 2
