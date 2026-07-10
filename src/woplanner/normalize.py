from __future__ import annotations

import math

import pandas as pd

from .model import PlannerRaceSession

NORMALIZED_LAP_COLUMNS = [
    "driver",
    "team",
    "lap",
    "lap_time_s",
    "cumulative_time_s",
    "compound",
    "stint",
    "tyre_age",
    "pit_in",
    "pit_out",
    "is_valid",
    "result_rank",
    "start_position",
]


def normalize_race_laps(
    raw_laps: pd.DataFrame,
    *,
    year: int,
    round_number: int,
    session_name: str,
) -> PlannerRaceSession:
    """Map FastF1/woStrategy lap columns to the planner-safe schema."""
    laps = pd.DataFrame(index=raw_laps.index)
    laps["driver"] = _column(raw_laps, "Driver", default="")
    laps["team"] = _column(raw_laps, "Team", default="")
    laps["lap"] = pd.to_numeric(_column(raw_laps, "LapNumber"), errors="coerce").astype("Int64")
    laps["lap_time_s"] = _seconds(_column(raw_laps, "LapTime"))
    laps["compound"] = _column(raw_laps, "Compound", default=pd.NA)
    laps["stint"] = pd.to_numeric(_column(raw_laps, "Stint"), errors="coerce").astype("Int64")
    laps["tyre_age"] = pd.to_numeric(_column(raw_laps, "TyreLife"), errors="coerce")
    laps["pit_in"] = _notna(_column(raw_laps, "PitInTime"))
    laps["pit_out"] = _notna(_column(raw_laps, "PitOutTime"))
    laps["is_valid"] = _valid_lap_mask(raw_laps, laps)
    laps["result_rank"] = pd.to_numeric(
        _column(raw_laps, "SessionResultRank"),
        errors="coerce",
    )
    laps["start_position"] = pd.to_numeric(
        _column(raw_laps, "SessionStartPosition"),
        errors="coerce",
    )

    laps = laps.loc[laps["driver"].notna() & (laps["driver"].astype(str) != "")]
    laps = laps.sort_values(["driver", "lap"], kind="mergesort").reset_index(drop=True)
    laps["cumulative_time_s"] = (
        laps.groupby("driver", sort=False)["lap_time_s"].cumsum().where(laps["lap_time_s"].notna())
    )
    laps = laps.loc[:, NORMALIZED_LAP_COLUMNS]

    drivers = _build_drivers(laps)
    event_key = _event_key(raw_laps, year=year, round_number=round_number, session_name=session_name)
    return PlannerRaceSession(
        event_key=event_key,
        year=year,
        round_number=round_number,
        session_name=session_name,
        laps=laps,
        drivers=drivers,
    )


def _column(data: pd.DataFrame, name: str, default: object = pd.NA) -> pd.Series:
    if name in data.columns:
        return data[name]
    return pd.Series(default, index=data.index)


def _seconds(values: pd.Series) -> pd.Series:
    if pd.api.types.is_timedelta64_dtype(values):
        return values.dt.total_seconds()
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_numeric(values, errors="coerce")
    return pd.to_timedelta(values, errors="coerce").dt.total_seconds()


def _notna(values: pd.Series) -> pd.Series:
    return values.notna().astype(bool)


def _valid_lap_mask(raw_laps: pd.DataFrame, laps: pd.DataFrame) -> pd.Series:
    if "Deleted" in raw_laps.columns:
        deleted = raw_laps["Deleted"].fillna(False).astype(bool)
    elif "IsAccurate" in raw_laps.columns:
        deleted = ~raw_laps["IsAccurate"].fillna(False).astype(bool)
    else:
        deleted = pd.Series(False, index=raw_laps.index)

    return (
        laps["lap"].notna()
        & laps["lap_time_s"].map(lambda value: isinstance(value, (int, float)) and math.isfinite(value))
        & (laps["lap_time_s"] > 0)
        & ~deleted
    )


def _build_drivers(laps: pd.DataFrame) -> pd.DataFrame:
    drivers = (
        laps.loc[:, ["driver", "team"]]
        .dropna(subset=["driver"])
        .drop_duplicates(subset=["driver"], keep="first")
        .sort_values(["team", "driver"], kind="mergesort")
        .reset_index(drop=True)
    )
    return drivers


def _event_key(
    raw_laps: pd.DataFrame,
    *,
    year: int,
    round_number: int,
    session_name: str,
) -> str:
    if "EventName" in raw_laps.columns and raw_laps["EventName"].notna().any():
        event = str(raw_laps["EventName"].dropna().iloc[0]).strip().replace(" ", "_")
        return f"{year}_{round_number}_{session_name}_{event}"
    return f"{year}_{round_number}_{session_name}"
