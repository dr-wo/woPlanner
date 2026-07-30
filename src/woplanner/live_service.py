from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from wodata.live import LiveConfig, LiveSessionSnapshot, LiveTimingController, ReplayConfig

from .driver_styles import assign_driver_styles

REFERENCE_MODE_ACTUAL_PACE = "actual_pace"
REFERENCE_MODE_AVERAGED_PACE = "averaged_pace"
REFERENCE_MODE_LABELS = {
    REFERENCE_MODE_ACTUAL_PACE: "Actual pace",
    REFERENCE_MODE_AVERAGED_PACE: "Averaged pace",
}


@dataclass(frozen=True)
class LivePlannerUpdate:
    snapshot: LiveSessionSnapshot
    statuses: tuple
    changed: bool


class LivePlannerService:
    def __init__(self, controller: LiveTimingController | None = None) -> None:
        self.controller = controller or LiveTimingController()
        self.snapshot = LiveSessionSnapshot.empty()
        self._start_rank_by_driver: dict[str, int] = {}

    def start_live(self, config: LiveConfig) -> None:
        self._start_rank_by_driver.clear()
        self.controller.start_live(config)

    def start_replay(self, config: ReplayConfig) -> None:
        self._start_rank_by_driver.clear()
        self.controller.start_replay(config)

    def poll(self) -> LivePlannerUpdate:
        result = self.controller.drain()
        self.snapshot = result.snapshot
        for driver in self.snapshot.drivers:
            if driver.driver_code not in self._start_rank_by_driver:
                rank = driver.position or len(self._start_rank_by_driver) + 1
                self._start_rank_by_driver[driver.driver_code] = int(rank)
        return LivePlannerUpdate(self.snapshot, result.statuses, result.changed)

    def stop(self) -> None:
        self.controller.stop()

    def available_drivers(self) -> pd.DataFrame:
        rows = [
            {
                "driver_number": item.driver_number,
                "driver": item.driver_code,
                "team": item.team,
                "full_name": item.full_name,
                "position": item.position,
            }
            for item in self.snapshot.drivers
        ]
        columns = ["driver_number", "driver", "team", "full_name", "position"]
        return assign_driver_styles(pd.DataFrame(rows, columns=columns))

    def default_reference_driver(self) -> str | None:
        drivers = self.available_drivers()
        if drivers.empty:
            return None
        positioned = drivers.dropna(subset=["position"]).sort_values(
            ["position", "driver"], kind="mergesort"
        )
        if not positioned.empty:
            return str(positioned.iloc[0]["driver"])
        return str(drivers.iloc[0]["driver"])

    def build_plot_data(
        self,
        reference_driver: str,
        reference_mode: str = REFERENCE_MODE_AVERAGED_PACE,
    ) -> pd.DataFrame:
        reference_mode = _normalize_reference_mode(reference_mode)
        rows = self._plot_rows()
        columns = [
            "driver", "team", "lap", "sector", "segment_index", "x_lap",
            "lap_time_s", "segment_time_s", "cumulative_time_s", "position", "plot_role",
            "scenario_id", "is_simulated", "is_faded",
        ]
        plot_rows = pd.DataFrame(rows, columns=columns)
        if plot_rows.empty:
            return plot_rows.assign(delta_s=pd.Series(dtype="float64"))
        reference = _reference_cumulative_time(
            plot_rows,
            reference_driver=reference_driver,
            reference_mode=reference_mode,
        )
        output = plot_rows.merge(reference, on="segment_index", how="inner", validate="many_to_one")
        output["delta_s"] = (
            output["cumulative_time_s"] - output["reference_cumulative_time_s"]
        )
        return output.sort_values(["driver", "segment_index"], kind="mergesort").reset_index(drop=True)

    def _plot_rows(self) -> list[dict]:
        if self.snapshot.completed_sectors:
            return [
                {
                    "driver": sector.driver_code,
                    "team": sector.team,
                    "lap": sector.lap_number,
                    "sector": sector.sector,
                    "segment_index": sector.segment_index,
                    "x_lap": _sector_x_for_lap(sector.lap_number, sector.sector),
                    "lap_time_s": pd.NA,
                    "segment_time_s": sector.sector_time_s,
                    "cumulative_time_s": sector.cumulative_time_s,
                    "position": sector.position,
                    "plot_role": "actual",
                    "scenario_id": pd.NA,
                    "is_simulated": False,
                    "is_faded": False,
                }
                for sector in self.snapshot.completed_sectors
            ]
        return [
            {
                "driver": lap.driver_code,
                "team": lap.team,
                "lap": lap.lap_number,
                "sector": "S3",
                "segment_index": lap.lap_number * 3,
                "x_lap": lap.lap_number,
                "lap_time_s": lap.lap_time_s,
                "segment_time_s": lap.lap_time_s,
                "cumulative_time_s": lap.cumulative_time_s,
                "position": lap.position,
                "plot_role": "actual",
                "scenario_id": pd.NA,
                "is_simulated": False,
                "is_faded": False,
            }
            for lap in self.snapshot.completed_laps
        ]

    def build_driver_ranks(self) -> pd.DataFrame:
        drivers = self.available_drivers()
        if drivers.empty:
            return drivers.assign(start_rank=pd.Series(dtype="Int64"), latest_rank=pd.Series(dtype="Int64"))
        drivers["start_rank"] = drivers["driver"].map(self._start_rank_by_driver)
        current = drivers["position"].copy()
        missing = current.isna()
        if missing.any():
            latest_laps = pd.DataFrame(
                [
                    {"driver": lap.driver_code, "lap": lap.lap_number, "time": lap.cumulative_time_s}
                    for lap in self.snapshot.completed_laps
                    if lap.cumulative_time_s is not None
                ]
            )
            if not latest_laps.empty:
                latest = latest_laps.sort_values(["driver", "lap"]).groupby("driver").last()
                fallback_order = latest.sort_values(["lap", "time"], ascending=[False, True])
                fallback_rank = {driver: rank for rank, driver in enumerate(fallback_order.index, 1)}
                current.loc[missing] = drivers.loc[missing, "driver"].map(fallback_rank)
        drivers["latest_rank"] = current
        return drivers


def _normalize_reference_mode(value: str) -> str:
    mode = str(value)
    if mode in {REFERENCE_MODE_ACTUAL_PACE, REFERENCE_MODE_AVERAGED_PACE}:
        return mode
    raise ValueError(f"Unknown reference mode: {value}")


def _reference_cumulative_time(
    plot_rows: pd.DataFrame,
    *,
    reference_driver: str,
    reference_mode: str,
) -> pd.DataFrame:
    reference_rows = plot_rows.loc[plot_rows["driver"].eq(reference_driver)].copy()
    reference_rows["cumulative_time_s"] = pd.to_numeric(
        reference_rows["cumulative_time_s"], errors="coerce"
    )
    reference_rows["segment_time_s"] = pd.to_numeric(
        reference_rows["segment_time_s"], errors="coerce"
    )
    reference_rows = reference_rows.dropna(subset=["cumulative_time_s", "segment_time_s"])
    reference_rows = reference_rows.loc[reference_rows["segment_time_s"] > 0]
    reference_rows = reference_rows.sort_values("segment_index", kind="mergesort")
    if reference_rows.empty:
        return pd.DataFrame(columns=["segment_index", "reference_cumulative_time_s"])
    if reference_mode == REFERENCE_MODE_AVERAGED_PACE:
        sector_averages = reference_rows.groupby("sector")["segment_time_s"].mean()
        reference_rows["reference_segment_time_s"] = reference_rows["sector"].map(sector_averages)
        reference_rows["reference_cumulative_time_s"] = pd.to_numeric(
            reference_rows["reference_segment_time_s"], errors="coerce"
        ).cumsum()
    else:
        reference_rows["reference_cumulative_time_s"] = reference_rows["cumulative_time_s"]
    return reference_rows.loc[:, ["segment_index", "reference_cumulative_time_s"]]


def _sector_x_for_lap(lap: int, sector: str) -> float:
    sector_number = {"S1": 1, "S2": 2, "S3": 3}[str(sector)]
    return int(lap) + (sector_number - 2) / 3.0
