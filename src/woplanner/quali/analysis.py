"""Pre-qualifying analysis orchestration for the planner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
import traceback
from uuid import uuid4

import pandas as pd

from wodata.artifacts import write_pre_quali_analysis
from wostrategy.analysis.pre_quali import PreQualiAnalysisResult, analyze_pre_quali

from .runtime_paths import RuntimePaths, resolve_runtime_paths
from .fp_access import (
    access_recovery_message, archive_summary, authentication_status, observe_archive_responses,
    observe_parsed_cache_usage,
)

_LOGGER = logging.getLogger(__name__)


class QualiAnalysisService:
    def __init__(self, data_root: str | Path | None = None, *, runtime_paths: RuntimePaths | None = None) -> None:
        self.runtime_paths = runtime_paths or resolve_runtime_paths(data_root)
        self.data_root = self.runtime_paths.output_root
        self.loaded_artifact_root: Path | None = None
        self.result: PreQualiAnalysisResult | None = None
        self.last_diagnostics: dict = {}
        self.diagnostic_path: Path | None = None
        self._diagnostic_output_validated = False

    def analyze(
        self,
        year: int,
        round_number: int,
        *,
        push_threshold_fraction: float = 0.02,
        laps: pd.DataFrame | None = None,
    ) -> PreQualiAnalysisResult:
        self._begin_diagnostics(year, round_number, supplied_laps=laps is not None)
        try:
            if self.runtime_paths.isolated:
                self.runtime_paths.check_output()
            self._diagnostic_output_validated = True
            if laps is None:
                laps = self._load_fp_laps(year, round_number)
            self._phase("calculation")
            result = analyze_pre_quali(
                laps,
                year=year,
                round_number=round_number,
                push_threshold_fraction=push_threshold_fraction,
            )
            self._phase("artifact_write")
            write_pre_quali_analysis(
                result.laps,
                result.durations,
                result.track_progression,
                year=year,
                round_number=round_number,
                push_threshold_fraction=push_threshold_fraction,
                data_root=self.data_root,
            )
            self.loaded_artifact_root = self.runtime_paths.output_root
            self.result = result
            self.last_diagnostics.update(status="success", lap_count=len(result.laps))
            self._phase("complete")
            return result
        except Exception as exc:
            self.last_diagnostics.update(status="failed", error=_exception_record(exc))
            if self.last_diagnostics["phase"] == "session_laps":
                self.last_diagnostics["sessions"][self.last_diagnostics["session"]] = {
                    "status": "failed", "phase": "session_laps", "error": _exception_record(exc)
                }
            _LOGGER.exception(
                "FP analysis failed for %s (phase=%s, session=%s, cache=%s, output=%s)",
                self.last_diagnostics["event_id"], self.last_diagnostics["phase"],
                self.last_diagnostics["session"], self.last_diagnostics["roots"]["cache"],
                self.last_diagnostics["roots"]["output"],
            )
            raise
        finally:
            self._persist_diagnostics()

    def _begin_diagnostics(self, year: int, round_number: int, *, supplied_laps: bool) -> None:
        from wodata import get_data_root

        self.diagnostic_path = None
        self._diagnostic_output_validated = False
        self.last_diagnostics = {
            "schema_version": 1,
            "event_id": f"{year}-R{round_number}", "year": int(year), "round": int(round_number),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "status": "running", "phase": "output_validation", "session": None,
            "input": "supplied_laps" if supplied_laps else "practice_sessions",
            "roots": {
                "resource": str(self.runtime_paths.resource_root) if self.runtime_paths.resource_root is not None else None,
                "output": str(get_data_root(self.data_root)), "cache": None,
            },
            "sessions": {name: {"status": "not_requested"} for name in ("FP1", "FP2", "FP3")},
            "warnings": [],
            "authentication_before": authentication_status(),
        }

    def _phase(self, phase: str, session: str | None = None) -> None:
        if self.last_diagnostics:
            self.last_diagnostics.update(phase=phase, session=session)

    def _persist_diagnostics(self) -> None:
        if not self._diagnostic_output_validated:
            # Failed root safety checks must never be followed by a write there.
            self.last_diagnostics["persistence_error"] = {
                "type": "OutputRootNotValidated", "message": "Diagnostic output root did not pass validation."
            }
            return
        root = Path(self.last_diagnostics["roots"]["output"])
        directory = (root / "woplanner" / "quali_analysis_diagnostics"
                     / f"year={self.last_diagnostics['year']}" / f"round={self.last_diagnostics['round']}")
        path = directory / f"analysis-{uuid4().hex}.json"
        contents = json.dumps(self.last_diagnostics, indent=2, ensure_ascii=False) + "\n"
        try:
            if self.runtime_paths.isolated:
                self.runtime_paths.checked(root, path)
            directory.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves every previous attempt and dataset.
            with path.open("x", encoding="utf-8") as handle:
                handle.write(contents)
            self.diagnostic_path = path
        except (OSError, ValueError) as exc:
            # ValueError is the existing isolated-root safety check, not a
            # catch-all for calculation/serialization errors.
            self.last_diagnostics["persistence_error"] = _exception_record(exc)
            _LOGGER.warning("Unable to save FP analysis diagnostics at %s: %s", path, exc, exc_info=True)

    def failure_message(self, exc: Exception) -> str:
        """Context for the GUI while leaving the original exception untouched."""
        diagnostic = self.last_diagnostics
        roots = diagnostic.get("roots", {})
        location = str(self.diagnostic_path) if self.diagnostic_path is not None else "unavailable"
        if self.diagnostic_path is None and diagnostic.get("persistence_error"):
            error = diagnostic["persistence_error"]
            location += f" ({error['type']}: {error['message']})"
        recovery = access_recovery_message(diagnostic)
        return (f"{type(exc).__name__}: {exc}\n"
                + (f"{recovery}\n" if recovery else "")
                +
                f"Event: {diagnostic.get('event_id', 'unknown')}\n"
                f"Phase: {diagnostic.get('phase', 'unknown')}"
                f"; session: {diagnostic.get('session') or 'all / not session-specific'}\n"
                f"Cache: {roots.get('cache') or 'not initialized'}\n"
                f"Resources: {roots.get('resource') or 'default workspace'}\n"
                f"Output: {roots.get('output', 'unknown')}\n"
                f"Diagnostics: {location}")

    def load(self, year: int, round_number: int) -> PreQualiAnalysisResult:
        (laps, durations, progression), self.loaded_artifact_root = self.runtime_paths.read_fp(year, round_number)
        self.result = PreQualiAnalysisResult(laps, durations, progression, 0.02)
        return self.result

    def _load_fp_laps(self, year: int, round_number: int) -> pd.DataFrame:
        import fastf1

        from wodata import get_fastf1_raw_cache_dir
        # The GUI invokes Session directly; without enabling the workspace
        # cache it ignores the already available FP session downloads.
        self._phase("cache_setup")
        cache = get_fastf1_raw_cache_dir(
            self.data_root if self.runtime_paths.isolated else None
        )
        if self.last_diagnostics:
            self.last_diagnostics["roots"]["cache"] = str(cache)
        if self.runtime_paths.isolated:
            self.runtime_paths.check_output()
            self.runtime_paths.checked(self.runtime_paths.output_root, cache)
        cache.mkdir(parents=True, exist_ok=True)
        # Opening and flushing a new temporary file verifies actual access;
        # permission bits and os.access cannot detect every filesystem denial.
        with tempfile.TemporaryFile(mode="w+b", dir=cache) as probe:
            probe.write(b"woplanner cache write check\n")
            probe.flush()
        configure = getattr(fastf1.Cache, "configure", None)
        if configure is not None:
            configure(cache_dir=str(cache))
        else:  # Older-runtime tests; fresh authenticated access requires 3.9.
            fastf1.Cache.enable_cache(str(cache))

        with observe_archive_responses(fastf1.Cache) as responses, observe_parsed_cache_usage() as hits:
            try:
                return self._load_practice_sessions(year, round_number)
            finally:
                self.last_diagnostics["archive_access"] = archive_summary(responses, hits)
                self.last_diagnostics["authentication_after"] = authentication_status()

    def _load_practice_sessions(self, year: int, round_number: int) -> pd.DataFrame:
        from wostrategy.core.session import Session

        sessions = []
        loaded_sessions = {}
        errors = []
        for name in ("FP1", "FP2", "FP3"):
            self._phase("session_load", name)
            try:
                loaded = Session(year, round_number, name)
            except Exception as exc:
                if self.last_diagnostics:
                    self.last_diagnostics["sessions"][name] = {
                        "status": "failed", "phase": "session_load", "error": _exception_record(exc)
                    }
                errors.append(f"{name}: {exc}")
                continue
            self._phase("session_laps", name)
            frame = loaded.laps.copy()
            frame["SessionName"] = name
            sessions.append(frame)
            loaded_sessions[name] = loaded
            if self.last_diagnostics:
                self.last_diagnostics["sessions"][name] = {"status": "success", "lap_count": len(frame)}
        self._phase("practice_loading")
        if not sessions:
            raise RuntimeError("Unable to load FP1/FP2/FP3: " + "; ".join(errors))
        laps = pd.concat(sessions, ignore_index=True)
        self._phase("reference_enrichment")
        return self._add_fastest_driver_lap_references(laps, loaded_sessions, diagnostics=self.last_diagnostics)

    @staticmethod
    def _add_fastest_driver_lap_references(laps: pd.DataFrame, sessions: dict, *, diagnostics: dict | None = None) -> pd.DataFrame:
        """Attach fastest-valid-FP checkpoint intervals without inventing sectors."""
        output = laps.copy()
        from fastf1.exceptions import DataNotLoadedError
        output["IsDriverFpReference"] = False
        for column in ("FpStartTo5PctS", "FpStartToS1S", "FpS1ToS2S"):
            output[column] = float("nan")
        valid = output["LapTime"].notna() & output["PitOutTime"].isna() & output["PitInTime"].isna()
        if "Deleted" in output:
            valid &= ~output["Deleted"].fillna(False).astype(bool)
        for column in ("IsAccurate", "IsValid"):
            if column in output:
                valid &= output[column].isna() | output[column].astype(bool)
        # One common benchmark: the fastest valid driver/lap over FP1–FP3.
        # It makes the operational 7% threshold comparable across the field.
        selected = [output.loc[valid, "LapTime"].idxmin()] if valid.any() else []
        for index in selected:
            row = output.loc[index]
            if diagnostics:
                diagnostics.update(phase="reference_enrichment", session=str(row["SessionName"]))
            loaded = sessions.get(str(row["SessionName"]))
            if loaded is None:
                continue
            matches = loaded.data.laps.loc[
                loaded.data.laps["Driver"].eq(row["Driver"])
                & loaded.data.laps["LapNumber"].eq(row["LapNumber"])
            ]
            if matches.empty:
                continue
            lap = next(matches.iterlaps())[1]
            output.loc[index, "IsDriverFpReference"] = True
            output.loc[index, "FpStartToS1S"] = _seconds(row.get("Sector1Time"))
            output.loc[index, "FpS1ToS2S"] = _seconds(row.get("Sector2Time"))
            try:
                telemetry = lap.get_telemetry()
                distance = pd.to_numeric(telemetry["Distance"], errors="coerce")
                times = telemetry["Time"]
                finish_distance = distance.max()
                first = distance[distance >= finish_distance * 0.05].index[0]
                output.loc[index, "FpStartTo5PctS"] = _seconds(times.loc[first] - times.iloc[0])
            except DataNotLoadedError as exc:
                warning = {
                    "phase": "reference_telemetry", "session": str(row["SessionName"]),
                    "driver": str(row["Driver"]), "lap_number": int(row["LapNumber"]),
                    "error": _exception_record(exc),
                }
                if diagnostics:
                    diagnostics["warnings"].append(warning)
                _LOGGER.warning("FP %s %s lap %s has no optional reference telemetry: %s",
                                row["SessionName"], row["Driver"], row["LapNumber"], exc)
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        return output

    def effective_fp_track_rate(self) -> tuple[float, str, int]:
        if self.result is None or self.result.track_progression.empty:
            return 0.0, "No usable FP estimate", 0
        progression = self.result.track_progression
        if "driver" in progression:
            progression = progression.loc[progression["driver"].eq("aggregate")]
        usable = progression.dropna(subset=["rate_s_per_field_push"])
        if usable.empty:
            return 0.0, "Insufficient FP support", 0
        weights = usable["sample_count"].clip(lower=1)
        weighted_rate = (usable["rate_s_per_field_push"] * weights).sum()
        rate = float(weighted_rate / weights.sum())
        return (
            rate,
            "FP1/FP2/FP3 existing linear track-evolution fit",
            int(weights.sum()),
        )


def _seconds(value) -> float:
    if value is None or pd.isna(value):
        return float("nan")
    return float(value.total_seconds() if hasattr(value, "total_seconds") else value)


def _exception_record(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}
