"""Optional operational resource/output separation; legacy callers stay unchanged."""
from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wodata.artifacts import pre_quali_root, read_pre_quali_analysis

RESOURCE_SETTING = "WOPLANNER_QUALI_RESOURCE_ROOT"
FP_FILES = ("lap_roles.csv", "duration_summary.csv", "track_progression.csv", "manifest.json")


class ArtifactError(ValueError):
    """A present FP artifact set is incomplete, invalid or outside its root."""


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlink paths are not allowed in isolated runtime roots: {part}")


def _absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute (independent of cwd): {path}")
    _no_links(path)
    return path.resolve()


def _overlap(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def source_worktrees() -> tuple[Path, ...]:
    """Protect imported repositories and all their registered source worktrees."""
    roots = set()
    for name in ("wodata", "woplanner", "wostrategy"):
        module = importlib.import_module(name)
        location = Path(module.__file__).resolve()
        root = next((p for p in location.parents if (p / "pyproject.toml").is_file()), location.parents[2])
        roots.add(root)
        try:
            listing = subprocess.check_output(
                ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            if (root / ".git").exists():
                raise ValueError(f"Cannot enumerate protected source worktrees: {root}") from exc
            continue  # Installed distributions still protect their actual package root.
        roots.update(Path(line[9:]).resolve() for line in listing.splitlines() if line.startswith("worktree "))
    return tuple(sorted(roots))


@dataclass(frozen=True)
class RuntimePaths:
    resource_root: Path | None
    output_root: Path | None
    isolated: bool = False

    def checked(self, root: Path, path: Path) -> Path:
        """Check descendants too: an existing nested symlink must not redirect I/O."""
        _no_links(root)
        _no_links(path)
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError(f"Hard-linked runtime file is not allowed: {path}")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Runtime path escapes configured root: {path}")
        return path

    def check_output(self) -> Path:
        assert self.output_root is not None
        root = self.output_root
        self.checked(root, root)
        for ancestor in (root, *root.parents):
            if ancestor.exists() and not ancestor.is_dir():
                raise ValueError(f"Output Root path is not a directory: {ancestor}")
        if root.exists():
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"Symlink in Output Root: {path}")
                if path.is_file() and path.stat().st_nlink != 1:
                    raise ValueError(f"Hard-linked file in Output Root: {path}")
        return root

    def read_fp(self, year: int, round_number: int):
        if not self.isolated:
            return read_pre_quali_analysis(year=year, round_number=round_number, data_root=self.resource_root), self.resource_root
        for root in (self.output_root, self.resource_root):
            directory = pre_quali_root(year, round_number, root)
            paths = [self.checked(root, directory / name) for name in FP_FILES]
            if not any(p.exists() for p in paths):
                continue
            try:
                if not all(p.is_file() for p in paths):
                    raise ValueError("incomplete FP artifact set (all four files are required)")
                manifest = json.loads(paths[-1].read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or (
                    manifest.get("schema_version") != "schema_v1"
                    or manifest.get("year") != year or manifest.get("round") != round_number
                ):
                    raise ValueError("invalid FP manifest schema/event identity")
                threshold = manifest["push_threshold_fraction"]
                if isinstance(threshold, bool) or not math.isfinite(float(threshold)) or float(threshold) < 0:
                    raise ValueError("invalid FP manifest push threshold")
                tables = read_pre_quali_analysis(year=year, round_number=round_number, data_root=root)
                count = manifest["lap_count"]
                if type(count) is not int or count != len(tables[0]):
                    raise ValueError("invalid FP manifest lap_count")
                required = ({"driver", "raw_lap_time_s"}, {"category", "selected_count", "selected_default_s"}, {"rate_s_per_field_push", "sample_count"})
                for table, columns in zip(tables, required):
                    if not columns.issubset(table.columns):
                        raise ValueError(f"invalid FP table columns: missing {sorted(columns - set(table.columns))}")
                import pandas as pd
                for table, columns in zip(tables, ({"raw_lap_time_s"}, {"selected_count", "selected_default_s"}, {"rate_s_per_field_push", "sample_count"})):
                    for column in columns & set(table.columns):
                        values = pd.to_numeric(table[column], errors="raise")
                        if column in {"selected_count", "sample_count"} and (
                            values.isna().any() or any(float(v) < 0 or not float(v).is_integer() for v in values.dropna())
                        ):
                            raise ValueError(f"invalid FP sample counts in {column}")
                        if any(not math.isfinite(float(v)) for v in values.dropna()):
                            raise ValueError(f"non-finite FP values in {column}")
                laps = tables[0]
                if "is_driver_fp_reference" in laps:
                    flags = laps["is_driver_fp_reference"].dropna()
                    if len(flags) and not pd.api.types.is_bool_dtype(flags.dtype):
                        raise ValueError("invalid FP reference boolean values")
                return tables, root
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ArtifactError(f"Invalid FP artifacts at {directory}: {exc}") from exc
        raise FileNotFoundError(f"No saved FP artifacts for {year}/R{round_number} in Output or Resource Root")


def resolve_runtime_paths(legacy_root: str | Path | None = None) -> RuntimePaths:
    if RESOURCE_SETTING not in os.environ:
        legacy = Path(legacy_root) if legacy_root is not None else None
        return RuntimePaths(legacy, legacy)
    resource = _absolute(os.environ[RESOURCE_SETTING], RESOURCE_SETTING)
    output_value = os.environ.get("WODATA_ROOT")
    if not output_value:
        raise ValueError(f"{RESOURCE_SETTING} requires an explicit WODATA_ROOT Output Root")
    output = _absolute(output_value, "WODATA_ROOT")
    if not resource.is_dir():
        raise ValueError(f"Resource Root does not exist: {resource}")
    if _overlap(resource, output):
        raise ValueError("Resource and Output Roots must not overlap")
    for source in source_worktrees():
        if _overlap(output, source) or _overlap(resource, source):
            raise ValueError(f"Resource/Output Roots must not overlap source/shared data worktree: {source}")
    paths = RuntimePaths(resource, output, True)
    paths.check_output()
    return paths
