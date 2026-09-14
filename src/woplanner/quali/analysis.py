"""Pre-qualifying analysis orchestration for the planner."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from wodata.artifacts import read_pre_quali_analysis, write_pre_quali_analysis
from wostrategy.analysis.pre_quali import PreQualiAnalysisResult, analyze_pre_quali


class QualiAnalysisService:
    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = data_root
        self.result: PreQualiAnalysisResult | None = None

    def analyze(
        self,
        year: int,
        round_number: int,
        *,
        push_threshold_fraction: float = 0.02,
        laps: pd.DataFrame | None = None,
    ) -> PreQualiAnalysisResult:
        if laps is None:
            laps = self._load_fp_laps(year, round_number)
        result = analyze_pre_quali(
            laps,
            year=year,
            round_number=round_number,
            push_threshold_fraction=push_threshold_fraction,
        )
        write_pre_quali_analysis(
            result.laps,
            result.durations,
            result.track_progression,
            year=year,
            round_number=round_number,
            push_threshold_fraction=push_threshold_fraction,
            data_root=self.data_root,
        )
        self.result = result
        return result

    def load(self, year: int, round_number: int) -> PreQualiAnalysisResult:
        laps, durations, progression = read_pre_quali_analysis(
            year=year, round_number=round_number, data_root=self.data_root
        )
        self.result = PreQualiAnalysisResult(laps, durations, progression, 0.02)
        return self.result

    def _load_fp_laps(self, year: int, round_number: int) -> pd.DataFrame:
        import fastf1

        from wodata import get_fastf1_raw_cache_dir
        from wostrategy.core.session import Session

        # The GUI invokes Session directly; without enabling the workspace
        # cache it ignores the already available FP session downloads.
        fastf1.Cache.enable_cache(str(get_fastf1_raw_cache_dir()))

        sessions = []
        loaded_sessions = {}
        errors = []
        for name in ("FP1", "FP2", "FP3"):
            try:
                loaded = Session(year, round_number, name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            frame = loaded.laps.copy()
            frame["SessionName"] = name
            sessions.append(frame)
            loaded_sessions[name] = loaded
        if not sessions:
            raise RuntimeError("Unable to load FP1/FP2/FP3: " + "; ".join(errors))
        laps = pd.concat(sessions, ignore_index=True)
        return self._add_fastest_driver_lap_references(laps, loaded_sessions)

    @staticmethod
    def _add_fastest_driver_lap_references(laps: pd.DataFrame, sessions: dict) -> pd.DataFrame:
        """Attach fastest-valid-FP checkpoint intervals without inventing sectors."""
        output = laps.copy()
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
