"""Explicitly pinned opt-in launcher. Never installs packages or follows main."""
from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

DEFAULT_MANIFEST = Path("/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/versions/resource-roots-v1/source-manifest.json")
PACKAGES = {"wodata": "woData", "woplanner": "woPlanner", "wostrategy": "woStrategy"}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def regular(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"Expected an absolute non-symlink path: {path}")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"Expected a regular file with no hard links: {path}")
    return path


def git(root, *args):
    return subprocess.check_output(["/usr/bin/git", "-C", str(root), *args], text=True).strip()


def verify_sources(manifest):
    if manifest["manifest_version"] != 1:
        raise ValueError("Unsupported source manifest version")
    if sys.version_info[:3] != tuple(manifest["python_version"]) or sys.version_info[:3] != (3, 11, 15):
        raise ValueError(f"Python version mismatch: {sys.version}")
    if Path(sys.executable).absolute() != Path(manifest["python_executable"]) or Path(sys.prefix) != Path(manifest["python_prefix"]):
        raise ValueError(f"Unexpected interpreter/environment: {sys.executable}, {sys.prefix}")
    for name, version in manifest["dependencies"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Dependency version mismatch: {name} (expected {version})")
    roots = {}
    for package, name in PACKAGES.items():
        row = manifest["sources"][name]
        root = Path(row["root"])
        regular(root / "pyproject.toml")
        if git(root, "rev-parse", "HEAD") != row["commit"]:
            raise ValueError(f"Pinned commit mismatch: {name} at {root}; expected {row['commit']}")
        tracked = set(git(root, "ls-tree", "-r", "--name-only", row["commit"]).splitlines())
        if tracked != set(row["files"]):
            raise ValueError(f"Source inventory mismatch: {name}")
        for relative, expected in row["files"].items():
            path = regular(root / relative)
            if not path.resolve().is_relative_to(root.resolve()) or digest(path) != expected:
                raise ValueError(f"Pinned source hash mismatch: {path}")
        if git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"Source worktree is dirty: {root}")
        # Ignored Python files could also shadow a lazy import.
        expected_python = {str((root / rel).resolve()) for rel in tracked if rel.startswith("src/") and rel.endswith(".py")}
        actual_python = {str(p.resolve()) for p in (root / "src").rglob("*.py")}
        if expected_python != actual_python:
            raise ValueError(f"Unexpected Python source files: {root / 'src'}")
        roots[package] = root / "src" / package
    return roots


def verify_resources(manifest):
    row = manifest["resources"]
    root = Path(row["root"])
    path = regular(root / "resource-manifest.json")
    if digest(path) != row["manifest_sha256"]:
        raise ValueError(f"Resource manifest hash mismatch: {path}")
    resource = json.loads(path.read_text())
    if resource["resource_manifest_version"] != 1:
        raise ValueError("Unsupported resource manifest version")
    expected = {"resource-manifest.json"}
    for item in resource["files"]:
        path = regular(root / item["destination"])
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Resource path escape: {path}")
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"Resource size/hash mismatch: {path}")
        expected.add(item["destination"])
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Resource symlink: {path}")
        if path.is_file(): actual.add(str(path.relative_to(root)))
    if actual != expected:
        raise ValueError("Unexpected or missing files in resource snapshot")
    return root


class PinnedImports(importlib.abc.MetaPathFinder):
    def __init__(self, roots, files):
        self.roots = roots
        self.files = files

    def find_spec(self, fullname, path=None, target=None):
        package = fullname.split(".")[0]
        if package not in self.roots:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not Path(spec.origin).resolve().is_relative_to(self.roots[package]):
            raise ImportError(f"Refusing unpinned project import: {fullname}")
        origin = Path(spec.origin).resolve()
        if origin.suffix != ".py" or self.files.get(str(origin)) != digest(origin):
            raise ImportError(f"Refusing unchecked project module bytes: {fullname}: {origin}")
        # Ignore any timestamp-valid bytecode adjacent to audited sources.
        spec.loader = importlib.machinery.SourceFileLoader(fullname, str(origin))
        return spec


def imported_paths(roots):
    result = {}
    for name, module in list(sys.modules.items()):
        package = name.split(".")[0]
        if package not in roots or not getattr(module, "__file__", None):
            continue
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(roots[package]):
            raise ImportError(f"Loaded unpinned project module: {name}: {path}")
        result[name] = {"path": str(path), "sha256": digest(path)}
    return result


def configure(manifest):
    roots = verify_sources(manifest)
    resource = verify_resources(manifest)
    base = Path(manifest["runtime_root"])
    if not base.is_absolute() or any(p.is_symlink() for p in (base, *base.parents)):
        raise ValueError(f"Invalid runtime state path: {base}")
    protected = [resource, *(p.parents[1] for p in roots.values())]
    output = Path(manifest["output_root"])
    for writable in (base, output):
        if not writable.is_absolute() or any(p.is_symlink() for p in (writable, *writable.parents)):
            raise ValueError(f"Invalid writable runtime path: {writable}")
        for source in protected:
            if writable.resolve().is_relative_to(source) or source.is_relative_to(writable.resolve()):
                raise ValueError(f"Runtime state/output overlaps source/resources: {writable}")
    for key in list(os.environ):
        if key.startswith(("PYTHON", "QT_", "QML")) or key in {"WOSTRATEGY_DATA_ROOT", "MPLCONFIGDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"}:
            os.environ.pop(key, None)
    run = base / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(run / "cache/pycache")
    sys.path[:0] = [str(root.parent) for root in roots.values()]
    files = {str((Path(row["root"]) / rel).resolve()): sha
             for row in manifest["sources"].values() for rel, sha in row["files"].items()}
    sys.meta_path.insert(0, PinnedImports(roots, files))
    # Validate output selection before creating state directories or importing Qt.
    os.environ.update(WOPLANNER_QUALI_RESOURCE_ROOT=str(resource), WODATA_ROOT=str(output))
    from woplanner.quali.runtime_paths import resolve_runtime_paths, source_worktrees
    paths = resolve_runtime_paths()
    for source in source_worktrees():
        if base.resolve().is_relative_to(source) or source.is_relative_to(base.resolve()):
            raise ValueError(f"Runtime state overlaps protected source/shared data: {base}")
    for name in ("logs", "cache/matplotlib", "config", "data", "tmp", "xdg-runtime"):
        (run / name).mkdir(parents=True, exist_ok=False, mode=0o700)
    os.environ.update(MPLCONFIGDIR=str(run / "cache/matplotlib"), XDG_CACHE_HOME=str(run / "cache"),
        XDG_CONFIG_HOME=str(run / "config"), XDG_DATA_HOME=str(run / "data"),
        XDG_RUNTIME_DIR=str(run / "xdg-runtime"), TMPDIR=str(run / "tmp"),
        TMP=str(run / "tmp"), TEMP=str(run / "tmp"))
    import tempfile
    tempfile.tempdir = str(run / "tmp")
    logging.basicConfig(filename=run / "logs/application.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return roots, paths, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true", help="Verify pins, resources and actual imports without opening a window")
    parser.add_argument("--replay", action="store_true", help="Explicitly select the pinned read-only R17 recording")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(regular(args.manifest).read_text())
        roots, paths, run = configure(manifest)
        from woplanner.quali import gui
        provenance = {"manifest": str(args.manifest), "manifest_sha256": digest(args.manifest),
            "sources": {name: {k:v for k,v in row.items() if k != "files"} for name,row in manifest["sources"].items()},
            "python": sys.version, "dependencies": manifest["dependencies"],
            "resource_root": str(paths.resource_root), "output_root": str(paths.output_root),
            "state_root": str(run), "imports": imported_paths(roots)}
        try:
            _, selected = paths.read_fp(2026, 17)
            provenance["saved_fp_root"] = str(selected)
        except FileNotFoundError as exc:
            provenance["saved_fp_unavailable"] = str(exc)
        (run / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        print(json.dumps({k:v for k,v in provenance.items() if k != "imports"}, indent=2), flush=True)
        if args.check:
            return 0
        replay = None
        if args.replay:
            replay = regular(Path(manifest["replay"]["path"]))
            if digest(replay) != manifest["replay"]["sha256"]:
                raise ValueError(f"Pinned recording hash mismatch: {replay}")
        stderr = (run / "logs/stderr.log").open("a")
        os.dup2(stderr.fileno(), 2)
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["Q17 isolated Qualifying"])
        window = gui.QualiWindow(runtime_paths=paths)
        if replay is not None:
            window.year.setValue(2026); window.round.setValue(17)
            window.replay_path.setText(str(replay))
        window.show()
        # The selected recording starts only when the user clicks Replay.
        code = app.exec()
        provenance["imports_after_gui"] = imported_paths(roots)
        (run / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        return code
    except Exception as exc:
        print(f"Q17 isolated launcher REFUSED: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
