from __future__ import annotations

import math

import pandas as pd

from ..common.timing import collapse_cumulative_time_gaps
from .models import PlannerRaceSession

NORMALIZED_LAP_COLUMNS = [
    "driver",
    "team",
    "lap",
    "lap_time_s",
    "sector_1_time_s",
    "sector_2_time_s",
    "sector_3_time_s",
    "cumulative_time_s",
    "compound",
    "stint",
    "tyre_age",
    "pit_in",
    "pit_out",
    "is_valid",
    "track_status",
    "track_status_s1",
    "track_status_s2",
    "track_status_s3",
    "physical_track_status_s1",
    "physical_track_status_s2",
    "physical_track_status_s3",
    "position",
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
    laps["sector_1_time_s"] = _seconds(_column(raw_laps, "Sector1Time"))
    laps["sector_2_time_s"] = _seconds(_column(raw_laps, "Sector2Time"))
    laps["sector_3_time_s"] = _seconds(_column(raw_laps, "Sector3Time"))
    sector_lap_time_s = laps[
        ["sector_1_time_s", "sector_2_time_s", "sector_3_time_s"]
    ].sum(axis=1, min_count=3)
    laps["lap_time_s"] = laps["lap_time_s"].combine_first(sector_lap_time_s)
    elapsed_lap_time_s = _elapsed_seconds(
        _column(raw_laps, "LapStartTime"),
        _column(raw_laps, "Time"),
    )
    laps["lap_time_s"] = laps["lap_time_s"].combine_first(elapsed_lap_time_s)
    laps["compound"] = _column(raw_laps, "Compound", default=pd.NA)
    laps["stint"] = pd.to_numeric(_column(raw_laps, "Stint"), errors="coerce").astype("Int64")
    laps["tyre_age"] = pd.to_numeric(_column(raw_laps, "TyreLife"), errors="coerce")
    laps["pit_in"] = _notna(_column(raw_laps, "PitInTime"))
    laps["pit_out"] = _notna(_column(raw_laps, "PitOutTime"))
    laps["is_valid"] = _valid_lap_mask(raw_laps, laps)
    laps["track_status"] = _track_status(raw_laps)
    laps["track_status_s1"] = _sector_track_status(raw_laps, "Sector1TrackStatus")
    laps["track_status_s2"] = _sector_track_status(raw_laps, "Sector2TrackStatus")
    laps["track_status_s3"] = _sector_track_status(raw_laps, "Sector3TrackStatus")
    laps["physical_track_status_s1"] = laps["track_status_s1"]
    laps["physical_track_status_s2"] = laps["track_status_s2"]
    laps["physical_track_status_s3"] = laps["track_status_s3"]
    laps["position"] = pd.to_numeric(_column(raw_laps, "Position"), errors="coerce")
    laps = _align_sector_track_status_to_leader(laps)
    laps["result_rank"] = pd.to_numeric(
        _column(raw_laps, "SessionResultRank"),
        errors="coerce",
    )
    laps["start_position"] = pd.to_numeric(
        _column(raw_laps, "SessionStartPosition"),
        errors="coerce",
    )

    laps["_lap_end_time_s"] = _seconds(_column(raw_laps, "Time"))
    race_start_time_s = pd.to_numeric(_seconds(_column(raw_laps, "LapStartTime")), errors="coerce").min()

    laps = laps.loc[laps["driver"].notna() & (laps["driver"].astype(str) != "")]
    laps = laps.sort_values(["driver", "lap"], kind="mergesort").reset_index(drop=True)
    cumulative_from_lap_times = (
        laps.groupby("driver", sort=False)["lap_time_s"].cumsum().where(laps["lap_time_s"].notna())
    )
    if pd.notna(race_start_time_s):
        cumulative_from_absolute_time = pd.to_numeric(
            laps["_lap_end_time_s"], errors="coerce"
        ) - float(race_start_time_s)
        cumulative_from_absolute_time = cumulative_from_absolute_time.where(
            cumulative_from_absolute_time.notna() & (cumulative_from_absolute_time >= 0.0)
        )
        laps["cumulative_time_s"] = cumulative_from_absolute_time.combine_first(
            cumulative_from_lap_times
        )
        previous_lap_had_red_flag = (
            laps.groupby("driver", sort=False)["track_status"]
            .shift()
            .astype("string")
            .str.contains("5", regex=False, na=False)
        )
        laps["cumulative_time_s"] = collapse_cumulative_time_gaps(
            laps,
            driver_column="driver",
            order_column="lap",
            cumulative_column="cumulative_time_s",
            duration_column="lap_time_s",
            eligible_gap=previous_lap_had_red_flag,
        )
    else:
        laps["cumulative_time_s"] = cumulative_from_lap_times
    laps = laps.drop(columns=["_lap_end_time_s"])
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
    if pd.api.types.is_datetime64_any_dtype(values):
        return pd.Series(float("nan"), index=values.index)
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_numeric(values, errors="coerce")
    return pd.to_timedelta(values, errors="coerce").dt.total_seconds()


def _elapsed_seconds(start_values: pd.Series, end_values: pd.Series) -> pd.Series:
    start = _seconds(start_values)
    end = _seconds(end_values)
    elapsed = end - start
    return elapsed.where(elapsed > 0)


def _notna(values: pd.Series) -> pd.Series:
    return values.notna().astype(bool)


def _track_status(raw_laps: pd.DataFrame) -> pd.Series:
    for column in ("TrackStatus", "TrackStatusCode", "track_status"):
        if column in raw_laps.columns:
            return raw_laps[column]
    return pd.Series(pd.NA, index=raw_laps.index)


def _sector_track_status(raw_laps: pd.DataFrame, sector_column: str) -> pd.Series:
    if sector_column in raw_laps.columns:
        return raw_laps[sector_column]
    return _track_status(raw_laps)


def _align_sector_track_status_to_leader(laps: pd.DataFrame) -> pd.DataFrame:
    required = {"lap", "position", "track_status_s1", "track_status_s2", "track_status_s3"}
    if required.difference(laps.columns):
        return laps
    leader_rows = (
        laps.dropna(subset=["lap", "position"])
        .sort_values(["lap", "position"], kind="mergesort")
        .drop_duplicates(subset=["lap"], keep="first")
        .loc[:, ["lap", "track_status_s1", "track_status_s2", "track_status_s3"]]
    )
    if leader_rows.empty:
        return laps
    leader_status = leader_rows.set_index("lap")
    output = laps.copy()
    for column in ("track_status_s1", "track_status_s2", "track_status_s3"):
        mapped = output["lap"].map(leader_status[column])
        output[column] = mapped.combine_first(output[column])
    return output


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
