from __future__ import annotations

from pathlib import Path

import pandas as pd

from wodata import get_cache_path, get_race_performance_review_event_dir
from .models import StrategyPlan, StintSegment

MODEL_VERSION = "strategy_sim_mvp_v3"
SCHEMA = "schema_v3"
SUMMARY_SUFFIXES = {
    "baseline": "summary_baseline_pace",
    "fuel": "summary_fuel_rate",
    "track": "summary_track_rate",
    "compound": "summary_compound_delta",
    "team_degradation": "summary_team_compound_degradation",
    "clean_laps": "clean_laps",
    "sample_parameters": "sample_parameters",
    "pit_loss_summary": "pit_loss_summary",
}


class MissingStrategyModelError(RuntimeError):
    def __init__(self, missing: list[Path], expected_dir: Path | str) -> None:
        self.missing = missing
        self.expected_dir = expected_dir
        missing_text = "\n".join(f"- {path}" for path in missing)
        super().__init__(
            "Missing woStrategy race-performance-review cache files.\n"
            f"Expected directory: {expected_dir}\n"
            f"{missing_text}\n"
            "Run woStrategy race_performance_review manually first."
        )


class MissingDriverStrategyModelError(ValueError):
    def __init__(
        self,
        *,
        driver: str,
        team: str,
        compound: str,
        team_model_driver: str | None = None,
    ) -> None:
        self.driver = str(driver)
        self.team = str(team)
        self.compound = str(compound).upper()
        self.team_model_driver = (
            None if team_model_driver is None else str(team_model_driver)
        )
        message = f"No strategy model row for {self.driver} on {self.compound}"
        if self.team_model_driver is not None:
            message += (
                f". {self.team}'s data is available from "
                f"{self.team_model_driver}"
            )
        super().__init__(message)


class WoStrategyRaceModelAdapter:
    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        source_cache_dir: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root) if data_root is not None else None
        self.source_cache_dir = Path(source_cache_dir) if source_cache_dir is not None else None

    def load_model(self, *, year: int, round_number: int, session_name: str) -> pd.DataFrame:
        normalized_path = self._normalized_cache_path(year, round_number, session_name)
        source_paths, missing, expected_dir = self._locate_source_paths(
            year,
            round_number,
            session_name,
            optional=("pit_loss_summary",),
        )
        if missing:
            raise MissingStrategyModelError(missing, expected_dir)

        required_sources = [
            path for name, path in source_paths.items() if name != "pit_loss_summary"
        ]
        if normalized_path.exists() and _is_newer_than_sources(normalized_path, required_sources):
            return pd.read_csv(normalized_path)

        artifact = self._read_source_model(
            year=year,
            round_number=round_number,
            session_name=session_name,
            source_paths=source_paths,
        )
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        artifact.to_csv(normalized_path, index=False)
        return artifact

    def predict_future_laps(
        self,
        *,
        year: int,
        round_number: int,
        session_name: str,
        driver: str,
        team: str,
        start_lap: int,
        end_lap: int,
        compound: str,
        initial_tyre_age: int,
    ) -> pd.DataFrame:
        strategy_plan = StrategyPlan(
            driver=str(driver),
            team=str(team),
            start_after_lap=int(start_lap) - 1,
            end_lap=int(end_lap),
            segments=[
                StintSegment(
                    start_lap=int(start_lap),
                    end_lap=int(end_lap),
                    compound=str(compound).upper(),
                    initial_tyre_age=int(initial_tyre_age),
                )
            ],
            pit_stops=[],
        )
        return self.predict_strategy_plan(
            year=year,
            round_number=round_number,
            session_name=session_name,
            strategy_plan=strategy_plan,
        ).drop(columns=["pit_loss_s", "predicted_total_lap_time_s"])

    def predict_strategy_plan(
        self,
        *,
        year: int,
        round_number: int,
        session_name: str,
        strategy_plan: StrategyPlan,
        use_team_data: bool = False,
    ) -> pd.DataFrame:
        model = self.load_model(year=year, round_number=round_number, session_name=session_name)
        frames = []
        for segment in strategy_plan.segments:
            compound = str(segment.compound).upper()
            driver_rows = model.loc[
                (model["driver"].astype(str) == str(strategy_plan.driver))
                & (model["compound"].astype(str).str.upper() == compound)
            ]
            if driver_rows.empty:
                team_rows = model.loc[
                    (model["team"].astype(str) == str(strategy_plan.team))
                    & (model["compound"].astype(str).str.upper() == compound)
                    & (model["driver"].astype(str) != str(strategy_plan.driver))
                ].sort_values("driver", kind="mergesort")
                if not use_team_data or team_rows.empty:
                    raise MissingDriverStrategyModelError(
                        driver=strategy_plan.driver,
                        team=strategy_plan.team,
                        compound=compound,
                        team_model_driver=(
                            None if team_rows.empty else str(team_rows.iloc[0]["driver"])
                        ),
                    )
                driver_rows = team_rows
            row = driver_rows.iloc[0]
            laps = pd.Series(
                range(int(segment.start_lap), int(segment.end_lap) + 1),
                dtype="int64",
            )
            if laps.empty:
                continue
            tyre_age = laps - int(segment.start_lap) + int(segment.initial_tyre_age)
            fuel_proxy_remaining = float(row["fuel_total_laps"]) - laps.astype(float)
            predicted = (
                float(row["baseline_pace_s"])
                + float(row["compound_delta_s"])
                + float(row["team_compound_degradation_s_per_lap"])
                * (tyre_age.astype(float) - float(row["tyre_age_reference"]))
                + float(row["fuel_rate_s_per_lap"])
                * (fuel_proxy_remaining - float(row["fuel_reference"]))
                + float(row["track_rate_s_per_lap"])
                * (laps.astype(float) - float(row["reference_lap"]))
            )
            frames.append(
                pd.DataFrame(
                    {
                        "driver": str(strategy_plan.driver),
                        "team": str(strategy_plan.team),
                        "lap": laps,
                        "compound": compound,
                        "tyre_age": tyre_age,
                        "predicted_clean_lap_time_s": predicted,
                    }
                )
            )

        if frames:
            output = pd.concat(frames, ignore_index=True).sort_values(
                "lap",
                kind="mergesort",
            )
        else:
            output = pd.DataFrame(
                columns=[
                    "driver",
                    "team",
                    "lap",
                    "compound",
                    "tyre_age",
                    "predicted_clean_lap_time_s",
                ]
            )
        output["pit_loss_s"] = 0.0
        output["pit_in_loss_s"] = 0.0
        output["pit_out_loss_s"] = 0.0
        pit_losses = {}
        pit_in_losses = {}
        pit_out_losses = {}
        for pit_stop in strategy_plan.pit_stops:
            first_lap = int(pit_stop.first_lap_after_stop)
            total = float(pit_stop.pit_loss_s)
            pit_losses[first_lap] = total
            pit_in_losses[int(pit_stop.stop_after_lap)] = float(
                pit_stop.pit_in_loss_s if pit_stop.pit_in_loss_s is not None else total / 2.0
            )
            pit_out_losses[first_lap] = float(
                pit_stop.pit_out_loss_s if pit_stop.pit_out_loss_s is not None else total / 2.0
            )
        if pit_losses and not output.empty:
            output["pit_loss_s"] = output["lap"].astype(int).map(pit_losses).fillna(0.0)
            output["pit_in_loss_s"] = output["lap"].astype(int).map(pit_in_losses).fillna(0.0)
            output["pit_out_loss_s"] = output["lap"].astype(int).map(pit_out_losses).fillna(0.0)
        output["predicted_total_lap_time_s"] = (
            pd.to_numeric(output["predicted_clean_lap_time_s"], errors="coerce")
            + pd.to_numeric(output["pit_in_loss_s"], errors="coerce").fillna(0.0)
            + pd.to_numeric(output["pit_out_loss_s"], errors="coerce").fillna(0.0)
        )
        return output.loc[
            :,
            [
                "driver",
                "team",
                "lap",
                "compound",
                "tyre_age",
                "predicted_clean_lap_time_s",
                "pit_loss_s",
                "pit_in_loss_s",
                "pit_out_loss_s",
                "predicted_total_lap_time_s",
            ],
        ].reset_index(drop=True)

    def pit_loss_split(
        self,
        *,
        year: int,
        round_number: int,
        session_name: str,
        track_status_type: str = "normal",
    ) -> tuple[float, float] | None:
        source_paths, missing, expected_dir = self._locate_source_paths(
            year,
            round_number,
            session_name,
            optional=("pit_loss_summary",),
        )
        path = source_paths.get("pit_loss_summary")
        if path is None or path in missing or not path.exists():
            return None
        summary = pd.read_csv(path)
        if summary.empty:
            return None
        status = str(track_status_type or "normal")
        rows = summary.loc[summary["TrackStatusType"].astype(str) == status]
        if rows.empty:
            return None
        row = rows.iloc[0]
        pit_in = pd.to_numeric(
            pd.Series([row.get("PitInS3LossMedianSeconds")]),
            errors="coerce",
        ).iloc[0]
        pit_out = pd.to_numeric(
            pd.Series([row.get("PitOutS1LossMedianSeconds")]),
            errors="coerce",
        ).iloc[0]
        if pd.isna(pit_in) or pd.isna(pit_out):
            return None
        return float(pit_in), float(pit_out)

    def _locate_source_paths(
        self,
        year: int,
        round_number: int,
        session_name: str,
        *,
        optional: tuple[str, ...] = (),
    ) -> tuple[dict[str, Path], list[Path], Path | str]:
        candidates = (
            [self.source_cache_dir]
            if self.source_cache_dir is not None
            else _source_cache_dir_candidates(
                data_root=self.data_root,
                year=year,
                round_number=round_number,
                session_name=session_name,
            )
        )
        best_paths: dict[str, Path] | None = None
        best_missing: list[Path] | None = None
        for candidate in candidates:
            paths = _source_paths(candidate, year, round_number, session_name)
            missing = [
                path
                for name, path in paths.items()
                if name not in optional and not path.exists()
            ]
            if not missing:
                self.source_cache_dir = candidate
                return paths, [], candidate
            if best_missing is None or len(missing) < len(best_missing):
                best_paths = paths
                best_missing = missing

        expected = " or ".join(str(path) for path in candidates)
        return best_paths or {}, best_missing or [], expected

    def _source_paths(self, year: int, round_number: int, session_name: str) -> dict[str, Path]:
        source_cache_dir = self.source_cache_dir or _source_cache_dir_candidates(
            data_root=self.data_root,
            year=year,
            round_number=round_number,
            session_name=session_name,
        )[0]
        return _source_paths(source_cache_dir, year, round_number, session_name)

    def _source_cache_path_label(self) -> str:
        if self.source_cache_dir is not None:
            return str(self.source_cache_dir)
        return "woData/wostrategy/race_performance_review/schema_v1"

    def _normalized_cache_path(self, year: int, round_number: int, session_name: str) -> Path:
        return get_cache_path(
            namespace="woplanner",
            dataset="strategy_model",
            schema=SCHEMA,
            year=year,
            round_number=round_number,
            session=session_name,
            filename="race_model.csv",
            data_root=self.data_root,
        )

    def _read_source_model(
        self,
        *,
        year: int,
        round_number: int,
        session_name: str,
        source_paths: dict[str, Path],
    ) -> pd.DataFrame:
        baseline = _read_required(source_paths["baseline"], {"Team", "Median"})
        fuel = _scalar_median(source_paths["fuel"])
        track = _scalar_median(source_paths["track"])
        compound_delta = _read_required(source_paths["compound"], {"Compound", "Median"})
        degradation = _read_required(source_paths["team_degradation"], {"Team", "Compound", "Median"})
        clean_laps = _read_required(
            source_paths["clean_laps"],
            {"Driver", "Team", "LapNumber", "TyreAgeLaps", "FuelProxyLapsRemaining"},
        )
        sample_parameters = _read_required(
            source_paths["sample_parameters"],
            {"FuelRef", "RaceLapRef", "TyreAgeRef"},
        )
        baseline = _expand_team_baseline_to_drivers(baseline, clean_laps)

        reference_lap = _reference_value(
            sample_parameters,
            "RaceLapRef",
            fallback=float(pd.to_numeric(clean_laps["LapNumber"], errors="coerce").mean()),
        )
        fuel_reference = _reference_value(sample_parameters, "FuelRef", fallback=0.0)
        tyre_age_reference = _reference_value(sample_parameters, "TyreAgeRef", fallback=0.0)
        fuel_total_laps = float(
            (
                pd.to_numeric(clean_laps["LapNumber"], errors="coerce")
                + pd.to_numeric(clean_laps["FuelProxyLapsRemaining"], errors="coerce")
            )
            .dropna()
            .median()
        )

        rows = []
        for _, driver_row in baseline.iterrows():
            team = str(driver_row["Team"])
            driver = str(driver_row["Driver"])
            for _, compound_row in compound_delta.iterrows():
                compound = str(compound_row["Compound"]).upper()
                deg_match = degradation.loc[
                    (degradation["Team"].astype(str) == team)
                    & (degradation["Compound"].astype(str).str.upper() == compound)
                ]
                degradation_s = 0.0 if deg_match.empty else float(deg_match.iloc[0]["Median"])
                rows.append(
                    {
                        "year": year,
                        "round_number": round_number,
                        "session_name": session_name,
                        "driver": driver,
                        "team": team,
                        "baseline_pace_s": float(driver_row["Median"]),
                        "fuel_rate_s_per_lap": fuel,
                        "track_rate_s_per_lap": track,
                        "compound": compound,
                        "compound_delta_s": float(compound_row["Median"]),
                        "team_compound_degradation_s_per_lap": degradation_s,
                        "reference_lap": reference_lap,
                        "fuel_reference": fuel_reference,
                        "fuel_total_laps": fuel_total_laps,
                        "tyre_age_reference": tyre_age_reference,
                        "source_cache_path": self._source_cache_path_label(),
                        "model_version": MODEL_VERSION,
                    }
                )
        return pd.DataFrame(rows)


def _source_paths(
    source_cache_dir: Path,
    year: int,
    round_number: int,
    session_name: str,
) -> dict[str, Path]:
    return {
        name: source_cache_dir
        / f"race_performance_{year}_{round_number}_{session_name}_{suffix}.csv"
        for name, suffix in SUMMARY_SUFFIXES.items()
    }


def _source_cache_dir_candidates(
    *, data_root: Path | None, year: int, round_number: int, session_name: str
) -> list[Path]:
    workspace_root = Path(__file__).resolve().parents[4]
    return [
        get_race_performance_review_event_dir(
            year=year,
            round_number=round_number,
            session=session_name,
            data_root=data_root,
        ),
        workspace_root / "cache" / "race_performance_review",
    ]


def _read_required(path: Path, columns: set[str]) -> pd.DataFrame:
    data = pd.read_csv(path)
    missing = columns.difference(data.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(sorted(missing))}")
    return data


def _reference_value(data: pd.DataFrame, column: str, *, fallback: float) -> float:
    if column not in data.columns:
        return float(fallback)
    values = pd.to_numeric(data[column], errors="coerce").dropna()
    if values.empty:
        return float(fallback)
    return float(values.iloc[0])


def _expand_team_baseline_to_drivers(
    baseline: pd.DataFrame,
    clean_laps: pd.DataFrame,
) -> pd.DataFrame:
    if "Driver" in baseline.columns:
        return baseline

    driver_teams = (
        clean_laps.loc[:, ["Driver", "Team"]]
        .dropna(subset=["Driver", "Team"])
        .drop_duplicates(subset=["Driver"], keep="first")
    )
    expanded = driver_teams.merge(baseline, on="Team", how="inner")
    if expanded.empty:
        raise ValueError("Could not map team baseline pace rows to clean-lap drivers")
    return expanded


def _scalar_median(path: Path) -> float:
    data = _read_required(path, {"Median"})
    if data.empty:
        raise ValueError(f"{path} does not contain any model rows")
    return float(data.iloc[0]["Median"])


def _is_newer_than_sources(path: Path, sources) -> bool:
    if not path.exists():
        return False
    try:
        output_mtime = path.stat().st_mtime
        return all(source.exists() and output_mtime >= source.stat().st_mtime for source in sources)
    except OSError:
        return False
