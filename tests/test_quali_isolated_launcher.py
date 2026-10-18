"""Launcher refusals use disposable source/resource fixtures, no live network."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/q17_quali_isolated.py"
spec = importlib.util.spec_from_file_location("q17_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    sources = {}
    for package, name in launcher.PACKAGES.items():
        root = tmp_path / name; (root / "src" / package).mkdir(parents=True)
        (root / "pyproject.toml").write_text("fixture")
        (root / "src" / package / "__init__.py").write_text("# pinned fixture")
        files = {str(p.relative_to(root)): launcher.digest(p) for p in root.rglob("*") if p.is_file()}
        sources[name] = {"root": str(root), "commit": "a"*40, "files": files}
    def git(root, *args):
        if args[0] == "rev-parse": return "a"*40
        if args[0] == "ls-tree": return "\n".join(sources[Path(root).name]["files"])
        if args[0] == "status": return ""
        if args[0] == "worktree": return f"worktree {root}"
        raise AssertionError(args)
    monkeypatch.setattr(launcher, "git", git)
    return {"manifest_version": 1, "python_version": [3,11,15],
        "python_executable": sys.executable, "python_prefix": sys.prefix,
        "dependencies": {}, "sources": sources}


def test_sources_verified_and_wrong_commit_refused(manifest, monkeypatch):
    assert set(launcher.verify_sources(manifest)) == set(launcher.PACKAGES)
    original = launcher.git
    monkeypatch.setattr(launcher, "git", lambda root,*args: "b"*40 if args[0] == "rev-parse" else original(root,*args))
    with pytest.raises(ValueError, match="commit mismatch"): launcher.verify_sources(manifest)


def test_changed_source_hash_refused(manifest):
    root = Path(manifest["sources"]["woPlanner"]["root"])
    (root / "src/woplanner/__init__.py").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"): launcher.verify_sources(manifest)


def test_untracked_lazy_python_file_refused(manifest):
    root = Path(manifest["sources"]["woPlanner"]["root"])
    (root / "src/woplanner/injected.py").write_text("pass")
    with pytest.raises(ValueError, match="Unexpected Python"): launcher.verify_sources(manifest)


def test_dirty_worktree_refused(manifest, monkeypatch):
    original = launcher.git
    monkeypatch.setattr(launcher, "git", lambda root,*args: " M src/file.py" if args[0] == "status" else original(root,*args))
    with pytest.raises(ValueError, match="dirty"): launcher.verify_sources(manifest)


def test_dependency_mismatch_refused(manifest):
    manifest["dependencies"] = {"pandas": "wrong"}
    with pytest.raises(ValueError, match="Dependency version mismatch"): launcher.verify_sources(manifest)


def test_resource_hash_and_extra_files_refused(tmp_path):
    root = tmp_path / "resource"; root.mkdir()
    data = root / "allowed.csv"; data.write_text("fixture")
    path = root / "resource-manifest.json"
    path.write_text(json.dumps({"resource_manifest_version":1, "files":[{
        "destination":"allowed.csv", "sha256":launcher.digest(data), "size":data.stat().st_size}]}))
    manifest = {"resources":{"root":str(root), "manifest_sha256":launcher.digest(path)}}
    assert launcher.verify_resources(manifest) == root
    (root / "extra.csv").write_text("extra")
    with pytest.raises(ValueError, match="Unexpected or missing"): launcher.verify_resources(manifest)
    (root / "extra.csv").unlink(); data.write_text("changed")
    with pytest.raises(ValueError, match="size/hash mismatch"): launcher.verify_resources(manifest)


def test_import_guard_refuses_shared_fallback(tmp_path, monkeypatch):
    outside = tmp_path / "shared"; (outside / "woplanner").mkdir(parents=True)
    (outside / "woplanner/__init__.py").write_text("pass")
    monkeypatch.setattr(sys, "path", [str(outside)])
    guard = launcher.PinnedImports({"woplanner":tmp_path / "pinned/woplanner"}, {})
    with pytest.raises(ImportError, match="Refusing unpinned"): guard.find_spec("woplanner")


def test_state_overlap_refused_before_any_mkdir(manifest, tmp_path, monkeypatch):
    root = Path(manifest["sources"]["woPlanner"]["root"])
    resource = tmp_path / "resource"; resource.mkdir()
    monkeypatch.setattr(launcher, "verify_resources", lambda m:resource)
    manifest.update(runtime_root=str(root / "logs"), output_root=str(tmp_path / "out"))
    with pytest.raises(ValueError, match="overlaps"): launcher.configure(manifest)
    assert not (root / "logs").exists()


def test_import_guard_refuses_uninventoried_native_origin(tmp_path, monkeypatch):
    import importlib.machinery
    root = tmp_path / "woplanner"; root.mkdir()
    binary = root / "unchecked.so"; binary.write_bytes(b"not a real extension")
    spec = importlib.machinery.ModuleSpec("woplanner.unchecked", None, origin=str(binary))
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *a:spec)
    guard = launcher.PinnedImports({"woplanner":root}, {})
    with pytest.raises(ImportError, match="unchecked project"): guard.find_spec("woplanner.unchecked")
