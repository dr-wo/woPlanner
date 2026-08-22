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
        from wostrategy.core.session import Session

        sessions = []
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
        if not sessions:
            raise RuntimeError("Unable to load FP1/FP2/FP3: " + "; ".join(errors))
        return pd.concat(sessions, ignore_index=True)

    def effective_fp_track_rate(self) -> tuple[float, str, int]:
        if self.result is None or self.result.track_progression.empty:
            return 0.0, "No usable FP estimate", 0
        usable = self.result.track_progression.dropna(subset=["rate_s_per_field_push"])
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
