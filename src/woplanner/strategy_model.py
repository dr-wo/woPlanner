from __future__ import annotations

from pathlib import Path

import pandas as pd

from wodata import get_cache_path

MODEL_VERSION = "strategy_sim_mvp_v1"
SCHEMA = "schema_v1"
SUMMARY_SUFFIXES = {
    "baseline": "summary_baseline_pace",
    "fuel": "summary_fuel_rate",
    "track": "summary_track_rate",
    "compound": "summary_compound_delta",
    "team_degradation": "summary_team_compound_degradation",
    "clean_laps": "clean_laps",
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
        )
        if missing:
            raise MissingStrategyModelError(missing, expected_dir)

        if normalized_path.exists() and _is_newer_than_sources(normalized_path, source_paths.values()):
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
        model = self.load_model(year=year, round_number=round_number, session_name=session_name)
        driver_rows = model.loc[
            (model["driver"].astype(str) == str(driver))
            & (model["compound"].astype(str).str.upper() == str(compound).upper())
        ]
        if driver_rows.empty:
            raise ValueError(f"No strategy model row for {driver} on {compound}")
        row = driver_rows.iloc[0]

        laps = pd.Series(range(int(start_lap), int(end_lap) + 1), dtype="int64")
        tyre_age = laps - int(start_lap) + int(initial_tyre_age)
        predicted = (
            float(row["baseline_pace_s"])
            + float(row["compound_delta_s"])
            + float(row["team_compound_degradation_s_per_lap"])
            * (tyre_age.astype(float) - float(row["tyre_age_reference"]))
            + float(row["fuel_rate_s_per_lap"])
            * (float(row["fuel_reference"]) - laps.astype(float))
            + float(row["track_rate_s_per_lap"])
            * (laps.astype(float) - float(row["reference_lap"]))
        )
        return pd.DataFrame(
            {
                "driver": str(driver),
                "team": str(team),
                "lap": laps,
                "compound": str(compound).upper(),
                "tyre_age": tyre_age,
                "predicted_clean_lap_time_s": predicted,
            }
        )

    def _locate_source_paths(
        self,
        year: int,
        round_number: int,
        session_name: str,
    ) -> tuple[dict[str, Path], list[Path], Path | str]:
        candidates = (
            [self.source_cache_dir]
            if self.source_cache_dir is not None
            else _source_cache_dir_candidates()
        )
        best_paths: dict[str, Path] | None = None
        best_missing: list[Path] | None = None
        for candidate in candidates:
            paths = _source_paths(candidate, year, round_number, session_name)
            missing = [path for path in paths.values() if not path.exists()]
            if not missing:
                self.source_cache_dir = candidate
                return paths, [], candidate
            if best_missing is None or len(missing) < len(best_missing):
                best_paths = paths
                best_missing = missing

        expected = " or ".join(str(path) for path in candidates)
        return best_paths or {}, best_missing or [], expected

    def _source_paths(self, year: int, round_number: int, session_name: str) -> dict[str, Path]:
        source_cache_dir = self.source_cache_dir or _source_cache_dir_candidates()[0]
        return _source_paths(source_cache_dir, year, round_number, session_name)

    def _source_cache_path_label(self) -> str:
        if self.source_cache_dir is not None:
            return str(self.source_cache_dir)
        return " or ".join(str(path) for path in _source_cache_dir_candidates())

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
            {"Driver", "Team", "LapNumber", "TyreAgeLaps", "FuelLapNumber"},
        )
        baseline = _expand_team_baseline_to_drivers(baseline, clean_laps)

        reference_lap = float(pd.to_numeric(clean_laps["LapNumber"], errors="coerce").median())
        fuel_reference = float(pd.to_numeric(clean_laps["FuelLapNumber"], errors="coerce").median())
        tyre_age_reference = float(pd.to_numeric(clean_laps["TyreAgeLaps"], errors="coerce").median())

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


def _source_cache_dir_candidates() -> list[Path]:
    workspace_root = Path(__file__).resolve().parents[3]
    return [
        workspace_root / "cache" / "race_performance_review",
    ]


def _read_required(path: Path, columns: set[str]) -> pd.DataFrame:
    data = pd.read_csv(path)
    missing = columns.difference(data.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(sorted(missing))}")
    return data


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
