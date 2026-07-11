from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from .model import PitStopScenario, PlannerRaceSession, ScenarioTrajectory
from .normalize import normalize_race_laps
from .strategy_model import WoStrategyRaceModelAdapter

TEAM_COLOURS = {
    "Red Bull Racing": "#3671C6",
    "Mercedes": "#27F4D2",
    "Ferrari": "#E80020",
    "McLaren": "#FF8000",
    "Aston Martin": "#229971",
    "Alpine": "#0093CC",
    "Williams": "#64C4FF",
    "Racing Bulls": "#6692FF",
    "Kick Sauber": "#52E252",
    "Haas F1 Team": "#B6BABD",
}
FALLBACK_COLOURS = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]


class RacePlannerService:
    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = data_root
        self.session: PlannerRaceSession | None = None
        self._last_plot_data: pd.DataFrame | None = None
        self.scenarios = ScenarioManager()
        self.strategy_model = WoStrategyRaceModelAdapter(data_root=data_root)

    def load_race(
        self,
        year: int,
        round_number: int,
        session_name: str = "R",
    ) -> PlannerRaceSession:
        from wostrategy import load_race_laps_for_planner

        raw_laps = load_race_laps_for_planner(
            year=year,
            round_number=round_number,
            session=session_name,
            data_root=self.data_root,
        )
        self.session = normalize_race_laps(
            raw_laps,
            year=year,
            round_number=round_number,
            session_name=session_name,
        )
        self.session.drivers = assign_driver_styles(self.session.drivers)
        self.session.drivers = add_driver_ranks(self.session.laps, self.session.drivers)
        self.scenarios.clear_all()
        self._last_plot_data = None
        return self.session

    def load_latest_race(self, session_name: str = "R") -> PlannerRaceSession:
        year, round_number = latest_completed_race()
        return self.load_race(year=year, round_number=round_number, session_name=session_name)

    def build_plot_data(self, reference_driver: str) -> pd.DataFrame:
        session = self._require_session()
        base_plot_data = build_reference_delta_data(
            session.laps,
            reference_driver=reference_driver,
        )
        self._last_plot_data = merge_scenario_plot_data(
            base_plot_data,
            session.laps,
            self.scenarios.active_trajectories(),
            reference_driver=reference_driver,
        )
        return self._last_plot_data

    def default_reference_driver(self) -> str:
        session = self._require_session()
        return default_reference_driver(session.laps)

    def build_pit_window_data(
        self,
        selected_driver: str,
        normal_pit_loss_s: float,
        sc_vsc_pit_loss_s: float,
    ) -> pd.DataFrame:
        self._require_session()
        plot_data = (
            self._last_plot_data
            if self._last_plot_data is not None
            else self.build_plot_data(selected_driver)
        )
        selected = plot_data.loc[plot_data["driver"] == selected_driver].copy()
        if "plot_role" in selected.columns:
            selected = selected.loc[selected["plot_role"] == "actual"].copy()
        return build_pit_window_data(
            selected,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
        )

    def build_tyre_event_data(self) -> pd.DataFrame:
        self._require_session()
        if self._last_plot_data is None:
            return _empty_tyre_event_data()
        return build_tyre_event_data(self._last_plot_data)

    def create_pit_stop_simulation(
        self,
        *,
        driver: str,
        stop_after_lap: int,
        compound: str,
        initial_tyre_age: int,
        pit_loss_s: float,
    ) -> PitStopScenario:
        session = self._require_session()
        driver_laps = session.laps.loc[session.laps["driver"] == driver]
        if driver_laps.empty:
            raise ValueError(f"Driver not found in loaded session: {driver}")
        max_lap = int(pd.to_numeric(driver_laps["lap"], errors="coerce").max())
        stop_after_lap = int(stop_after_lap)
        first_simulated_lap = stop_after_lap + 1
        if first_simulated_lap > max_lap:
            raise ValueError("Simulation must start before the driver's final recorded lap")
        team = str(driver_laps["team"].dropna().iloc[0]) if driver_laps["team"].notna().any() else ""

        predicted = self.strategy_model.predict_future_laps(
            year=session.year,
            round_number=session.round_number,
            session_name=session.session_name,
            driver=driver,
            team=team,
            start_lap=first_simulated_lap,
            end_lap=max_lap,
            compound=compound,
            initial_tyre_age=int(initial_tyre_age),
        )
        simulated = predicted.rename(
            columns={"predicted_clean_lap_time_s": "lap_time_s"}
        ).copy()
        simulated.loc[simulated.index[0], "lap_time_s"] += float(pit_loss_s)

        scenario = PitStopScenario(
            scenario_id=f"{driver}-{uuid4().hex[:8]}",
            driver=driver,
            stop_after_lap=stop_after_lap,
            first_simulated_lap=first_simulated_lap,
            compound=str(compound).upper(),
            initial_tyre_age=int(initial_tyre_age),
            pit_loss_s=float(pit_loss_s),
        )
        trajectory = ScenarioTrajectory(
            scenario_id=scenario.scenario_id,
            driver=driver,
            laps=simulated.loc[:, ["driver", "team", "lap", "lap_time_s", "compound", "tyre_age"]],
        )
        self.scenarios.upsert(scenario, trajectory)
        self._last_plot_data = None
        return scenario

    def clear_simulation(self, scenario_id: str) -> None:
        self.scenarios.clear(scenario_id)
        self._last_plot_data = None

    def clear_driver_simulation(self, driver: str) -> None:
        self.scenarios.clear_driver(driver)
        self._last_plot_data = None

    def clear_all_simulations(self) -> None:
        self.scenarios.clear_all()
        self._last_plot_data = None

    def build_lap_delta_guide_data(
        self,
        reference_driver: str,
        y_min: float | None = None,
        y_max: float | None = None,
    ) -> pd.DataFrame:
        session = self._require_session()
        plot_data = self._last_plot_data
        if plot_data is None:
            plot_data = self.build_plot_data(reference_driver)
        return build_lap_delta_guide_data(
            plot_data,
            session.laps,
            reference_driver=reference_driver,
            y_min=y_min,
            y_max=y_max,
        )

    def _require_session(self) -> PlannerRaceSession:
        if self.session is None:
            raise RuntimeError("No race session loaded")
        return self.session


class ScenarioManager:
    def __init__(self) -> None:
        self._scenarios: dict[str, PitStopScenario] = {}
        self._trajectories: dict[str, ScenarioTrajectory] = {}

    def active_scenarios(self) -> list[PitStopScenario]:
        return [scenario for scenario in self._scenarios.values() if scenario.is_active]

    def active_trajectories(self) -> list[ScenarioTrajectory]:
        active_ids = {scenario.scenario_id for scenario in self.active_scenarios()}
        return [
            trajectory
            for scenario_id, trajectory in self._trajectories.items()
            if scenario_id in active_ids
        ]

    def upsert(self, scenario: PitStopScenario, trajectory: ScenarioTrajectory) -> None:
        self.clear_driver(scenario.driver)
        self._scenarios[scenario.scenario_id] = scenario
        self._trajectories[scenario.scenario_id] = trajectory

    def clear(self, scenario_id: str) -> None:
        self._scenarios.pop(scenario_id, None)
        self._trajectories.pop(scenario_id, None)

    def clear_driver(self, driver: str) -> None:
        for scenario in list(self._scenarios.values()):
            if scenario.driver == driver:
                self.clear(scenario.scenario_id)

    def clear_all(self) -> None:
        self._scenarios.clear()
        self._trajectories.clear()


def build_reference_delta_data(laps: pd.DataFrame, *, reference_driver: str) -> pd.DataFrame:
    required = {"driver", "lap", "cumulative_time_s"}
    missing = required.difference(laps.columns)
    if missing:
        raise ValueError(f"laps is missing required columns: {', '.join(sorted(missing))}")

    reference = (
        laps.loc[laps["driver"] == reference_driver, ["lap", "cumulative_time_s"]]
        .dropna(subset=["lap", "cumulative_time_s"])
        .rename(columns={"cumulative_time_s": "reference_cumulative_time_s"})
    )
    if reference.empty:
        raise ValueError(f"Reference driver not found or has no valid laps: {reference_driver}")

    plot_data = laps.merge(reference, on="lap", how="inner")
    plot_data["delta_s"] = (
        plot_data["cumulative_time_s"] - plot_data["reference_cumulative_time_s"]
    )
    plot_data["plot_role"] = "actual"
    plot_data["scenario_id"] = pd.NA
    plot_data["is_simulated"] = False
    plot_data["is_faded"] = False
    return plot_data.sort_values(["driver", "lap"], kind="mergesort").reset_index(drop=True)


def merge_scenario_plot_data(
    base_plot_data: pd.DataFrame,
    base_laps: pd.DataFrame,
    trajectories: list[ScenarioTrajectory],
    *,
    reference_driver: str,
) -> pd.DataFrame:
    if not trajectories:
        return base_plot_data

    reference = (
        base_laps.loc[base_laps["driver"] == reference_driver, ["lap", "cumulative_time_s"]]
        .dropna(subset=["lap", "cumulative_time_s"])
        .rename(columns={"cumulative_time_s": "reference_cumulative_time_s"})
    )
    output_frames = [base_plot_data.copy()]
    faded_drivers = set()
    for trajectory in trajectories:
        scenario_laps = trajectory.laps.copy()
        if scenario_laps.empty:
            continue
        base_driver_laps = base_laps.loc[base_laps["driver"] == trajectory.driver]
        stop_after_lap = int(pd.to_numeric(scenario_laps["lap"], errors="coerce").min()) - 1
        base_before_stop = base_driver_laps.loc[
            pd.to_numeric(base_driver_laps["lap"], errors="coerce") == stop_after_lap,
            "cumulative_time_s",
        ]
        if base_before_stop.empty or pd.isna(base_before_stop.iloc[0]):
            continue
        scenario_laps["cumulative_time_s"] = (
            float(base_before_stop.iloc[0])
            + pd.to_numeric(scenario_laps["lap_time_s"], errors="coerce").cumsum()
        )
        scenario_laps["stint"] = _next_driver_stint(base_driver_laps)
        scenario_laps["pit_in"] = False
        scenario_laps["pit_out"] = False
        scenario_laps.loc[scenario_laps.index[0], "pit_out"] = True
        scenario_plot = scenario_laps.merge(reference, on="lap", how="inner")
        scenario_plot["delta_s"] = (
            scenario_plot["cumulative_time_s"] - scenario_plot["reference_cumulative_time_s"]
        )
        scenario_plot["plot_role"] = "simulation"
        scenario_plot["scenario_id"] = trajectory.scenario_id
        scenario_plot["is_simulated"] = True
        scenario_plot["is_faded"] = False
        anchor = base_driver_laps.loc[
            pd.to_numeric(base_driver_laps["lap"], errors="coerce") == stop_after_lap
        ].merge(reference, on="lap", how="inner")
        if not anchor.empty:
            anchor = anchor.iloc[[0]].copy()
            anchor["delta_s"] = (
                anchor["cumulative_time_s"] - anchor["reference_cumulative_time_s"]
            )
            anchor["plot_role"] = "simulation"
            anchor["scenario_id"] = trajectory.scenario_id
            anchor["is_simulated"] = False
            anchor["is_faded"] = False
            scenario_plot = pd.concat([anchor, scenario_plot], ignore_index=True, sort=False)
        output_frames.append(scenario_plot)
        faded_drivers.add((trajectory.driver, stop_after_lap))

    output = pd.concat(output_frames, ignore_index=True, sort=False)
    faded_anchor_frames = []
    for driver, stop_after_lap in faded_drivers:
        mask = (
            (output["driver"] == driver)
            & (output["plot_role"] == "actual")
            & (pd.to_numeric(output["lap"], errors="coerce") > stop_after_lap)
        )
        output.loc[mask, "is_faded"] = True
        anchor = output.loc[
            (output["driver"] == driver)
            & (output["plot_role"] == "actual")
            & (pd.to_numeric(output["lap"], errors="coerce") == stop_after_lap)
            & (~output["is_faded"].fillna(False).astype(bool))
        ]
        if not anchor.empty:
            anchor = anchor.iloc[[0]].copy()
            anchor["is_faded"] = True
            faded_anchor_frames.append(anchor)
    if faded_anchor_frames:
        output = pd.concat([output, *faded_anchor_frames], ignore_index=True, sort=False)
    return output.sort_values(["driver", "plot_role", "lap"], kind="mergesort").reset_index(drop=True)


def _next_driver_stint(driver_laps: pd.DataFrame) -> int:
    if "stint" not in driver_laps.columns:
        return 1
    stints = pd.to_numeric(driver_laps["stint"], errors="coerce").dropna()
    return int(stints.max()) + 1 if not stints.empty else 1


def default_reference_driver(laps: pd.DataFrame) -> str:
    ranked = (
        laps.loc[:, ["driver", "result_rank"]]
        .dropna(subset=["driver", "result_rank"])
        .sort_values(["result_rank", "driver"], kind="mergesort")
    )
    if not ranked.empty:
        return str(ranked.iloc[0]["driver"])

    latest = latest_driver_order(laps)
    if latest.empty:
        raise ValueError("No drivers available for reference selection")
    return str(latest.iloc[0]["driver"])


def latest_completed_race(now: datetime | None = None) -> tuple[int, int]:
    import fastf1

    current_time = now or datetime.now(timezone.utc)
    year = current_time.year
    schedule = fastf1.get_event_schedule(year, include_testing=False)
    completed = _completed_races_from_schedule(schedule, current_time)
    if completed.empty and year > 1950:
        year -= 1
        schedule = fastf1.get_event_schedule(year, include_testing=False)
        completed = _completed_races_from_schedule(schedule, current_time)
    if completed.empty:
        raise ValueError("No completed race found in the FastF1 schedule")

    latest = completed.sort_values("Session5DateUtc").iloc[-1]
    return int(year), int(latest["RoundNumber"])


def _completed_races_from_schedule(schedule: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if "Session5DateUtc" not in schedule.columns:
        return pd.DataFrame()
    dates = pd.to_datetime(schedule["Session5DateUtc"], utc=True, errors="coerce")
    return schedule.loc[dates.notna() & (dates <= pd.Timestamp(now))]


def add_driver_ranks(laps: pd.DataFrame, drivers: pd.DataFrame) -> pd.DataFrame:
    output = drivers.copy()
    start_order = starting_driver_order(laps)
    latest_order = latest_driver_order(laps)
    start_tyres = starting_driver_tyre_state(laps)
    latest_tyres = latest_driver_tyre_state(laps)
    output = output.merge(
        start_order.rename(columns={"rank": "start_rank"}),
        on="driver",
        how="left",
    )
    output = output.merge(
        latest_order.rename(columns={"rank": "latest_rank"}),
        on="driver",
        how="left",
    )
    output = output.merge(
        start_tyres.add_prefix("start_").rename(columns={"start_driver": "driver"}),
        on="driver",
        how="left",
    )
    output = output.merge(
        latest_tyres.add_prefix("latest_").rename(columns={"latest_driver": "driver"}),
        on="driver",
        how="left",
    )
    return output


def starting_driver_order(laps: pd.DataFrame) -> pd.DataFrame:
    start_position_order = _start_position_order(laps)
    if not start_position_order.empty:
        return start_position_order

    first_laps = laps.dropna(subset=["driver", "lap", "cumulative_time_s"]).copy()
    if first_laps.empty:
        return pd.DataFrame(columns=["driver", "rank"])
    first_laps = first_laps.sort_values(["driver", "lap"], kind="mergesort")
    first_laps = first_laps.groupby("driver", as_index=False, sort=False).first()
    return _rank_by_cumulative_time(first_laps)


def latest_driver_order(laps: pd.DataFrame) -> pd.DataFrame:
    ranked = _result_rank_order(laps)
    if not ranked.empty:
        return ranked

    latest_laps = laps.dropna(subset=["driver", "lap", "cumulative_time_s"]).copy()
    if latest_laps.empty:
        return pd.DataFrame(columns=["driver", "rank"])
    latest_laps = latest_laps.sort_values(["driver", "lap"], kind="mergesort")
    latest_laps = latest_laps.groupby("driver", as_index=False, sort=False).last()
    return _rank_by_cumulative_time(latest_laps)


def starting_driver_tyre_state(laps: pd.DataFrame) -> pd.DataFrame:
    first_laps = _first_driver_laps(laps)
    if first_laps.empty:
        return pd.DataFrame(columns=["driver", "tyre_compound", "tyre_age_label"])
    return _driver_tyre_state(first_laps)


def latest_driver_tyre_state(laps: pd.DataFrame) -> pd.DataFrame:
    latest_laps = _latest_driver_laps(laps)
    if latest_laps.empty:
        return pd.DataFrame(columns=["driver", "tyre_compound", "tyre_age_label"])
    return _driver_tyre_state(latest_laps)


def _first_driver_laps(laps: pd.DataFrame) -> pd.DataFrame:
    required = {"driver", "lap"}
    if required.difference(laps.columns):
        return pd.DataFrame()
    first_laps = laps.dropna(subset=["driver", "lap"]).copy()
    if first_laps.empty:
        return pd.DataFrame()
    first_laps = first_laps.sort_values(["driver", "lap"], kind="mergesort")
    return first_laps.groupby("driver", as_index=False, sort=False).first()


def _latest_driver_laps(laps: pd.DataFrame) -> pd.DataFrame:
    required = {"driver", "lap"}
    if required.difference(laps.columns):
        return pd.DataFrame()
    latest_laps = laps.dropna(subset=["driver", "lap"]).copy()
    if latest_laps.empty:
        return pd.DataFrame()
    latest_laps = latest_laps.sort_values(["driver", "lap"], kind="mergesort")
    return latest_laps.groupby("driver", as_index=False, sort=False).last()


def _driver_tyre_state(driver_laps: pd.DataFrame) -> pd.DataFrame:
    output = driver_laps.loc[:, ["driver"]].copy()
    output["tyre_compound"] = (
        driver_laps["compound"] if "compound" in driver_laps.columns else pd.NA
    )
    output["tyre_age_label"] = (
        driver_laps["tyre_age"].map(_tyre_age_label)
        if "tyre_age" in driver_laps.columns
        else "?"
    )
    return output.loc[:, ["driver", "tyre_compound", "tyre_age_label"]]


def _result_rank_order(laps: pd.DataFrame) -> pd.DataFrame:
    if "result_rank" not in laps.columns:
        return pd.DataFrame(columns=["driver", "rank"])
    ranked = (
        laps.loc[:, ["driver", "result_rank"]]
        .dropna(subset=["driver", "result_rank"])
        .sort_values(["result_rank", "driver"], kind="mergesort")
        .drop_duplicates(subset=["driver"], keep="first")
    )
    if ranked.empty:
        return pd.DataFrame(columns=["driver", "rank"])
    ranked["rank"] = np.arange(1, len(ranked) + 1)
    return ranked.loc[:, ["driver", "rank"]]


def _start_position_order(laps: pd.DataFrame) -> pd.DataFrame:
    if "start_position" not in laps.columns:
        return pd.DataFrame(columns=["driver", "rank"])
    ranked = (
        laps.loc[:, ["driver", "start_position"]]
        .dropna(subset=["driver", "start_position"])
        .sort_values(["start_position", "driver"], kind="mergesort")
        .drop_duplicates(subset=["driver"], keep="first")
    )
    if ranked.empty:
        return pd.DataFrame(columns=["driver", "rank"])
    ranked["rank"] = np.arange(1, len(ranked) + 1)
    return ranked.loc[:, ["driver", "rank"]]


def _rank_by_cumulative_time(laps: pd.DataFrame) -> pd.DataFrame:
    ranked = laps.sort_values(["cumulative_time_s", "driver"], kind="mergesort").copy()
    ranked["rank"] = np.arange(1, len(ranked) + 1)
    return ranked.loc[:, ["driver", "rank"]]


def build_pit_window_data(
    selected_driver_delta: pd.DataFrame,
    *,
    normal_pit_loss_s: float,
    sc_vsc_pit_loss_s: float,
) -> pd.DataFrame:
    if selected_driver_delta.empty:
        return pd.DataFrame(
            columns=[
                "lap",
                "normal_lower_boundary_s",
                "vsc_lower_boundary_s",
                "vsc_upper_boundary_s",
                "normal_upper_boundary_s",
            ]
        )

    output = selected_driver_delta.loc[:, ["lap", "delta_s"]].copy()
    output["normal_lower_boundary_s"] = output["delta_s"] - float(normal_pit_loss_s)
    output["vsc_lower_boundary_s"] = output["delta_s"] - float(sc_vsc_pit_loss_s)
    output["vsc_upper_boundary_s"] = output["delta_s"] + float(sc_vsc_pit_loss_s)
    output["normal_upper_boundary_s"] = output["delta_s"] + float(normal_pit_loss_s)
    return output.loc[
        :,
        [
            "lap",
            "normal_lower_boundary_s",
            "vsc_lower_boundary_s",
            "vsc_upper_boundary_s",
            "normal_upper_boundary_s",
        ],
    ]


def build_tyre_event_data(plot_data: pd.DataFrame) -> pd.DataFrame:
    required = {"driver", "lap", "delta_s", "compound", "stint", "tyre_age"}
    missing = required.difference(plot_data.columns)
    if missing:
        return _empty_tyre_event_data()

    events: list[pd.DataFrame] = []
    is_simulated = (
        plot_data["is_simulated"].fillna(False).astype(bool)
        if "is_simulated" in plot_data.columns
        else pd.Series(False, index=plot_data.index)
    )
    plot_role = (
        plot_data["plot_role"].astype(str)
        if "plot_role" in plot_data.columns
        else pd.Series("actual", index=plot_data.index)
    )
    actual_data = plot_data.loc[~is_simulated & plot_role.ne("simulation")].copy()
    sorted_data = actual_data.sort_values(["driver", "lap"], kind="mergesort")
    for _, driver_laps in sorted_data.groupby("driver", sort=False):
        driver_laps = driver_laps.dropna(subset=["lap", "delta_s"]).copy()
        if driver_laps.empty:
            continue
        previous_stint = driver_laps["stint"].shift(1)
        previous_compound = driver_laps["compound"].shift(1)
        event_mask = (
            driver_laps["stint"].ne(previous_stint)
            | driver_laps["compound"].ne(previous_compound)
        )
        event_mask = event_mask & previous_stint.notna()
        if "pit_out" in driver_laps.columns:
            event_mask = event_mask | (
                driver_laps["pit_out"].fillna(False).astype(bool) & previous_stint.notna()
            )
        events.append(driver_laps.loc[event_mask])

    simulated_data = plot_data.loc[is_simulated].copy()
    if not simulated_data.empty and "scenario_id" in simulated_data.columns:
        simulated_data = simulated_data.dropna(subset=["lap", "delta_s"])
        simulated_data = simulated_data.sort_values(
            ["scenario_id", "driver", "lap"],
            kind="mergesort",
        )
        events.append(simulated_data.groupby("scenario_id", as_index=False, sort=False).first())

    if not events:
        return _empty_tyre_event_data()

    output = pd.concat(events, ignore_index=True)
    output = output.dropna(subset=["lap", "delta_s"])
    if output.empty:
        return _empty_tyre_event_data()
    output["tyre_age_label"] = output["tyre_age"].map(_tyre_age_label)
    if "scenario_id" not in output.columns:
        output["scenario_id"] = pd.NA
    if "is_simulated" not in output.columns:
        output["is_simulated"] = False
    if "is_faded" not in output.columns:
        output["is_faded"] = False
    return output.loc[
        :,
        [
            "driver",
            "lap",
            "delta_s",
            "compound",
            "tyre_age_label",
            "scenario_id",
            "is_simulated",
            "is_faded",
        ],
    ]


def _empty_tyre_event_data() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "driver",
            "lap",
            "delta_s",
            "compound",
            "tyre_age_label",
            "scenario_id",
            "is_simulated",
            "is_faded",
        ]
    )


def build_lap_delta_guide_data(
    plot_data: pd.DataFrame,
    laps: pd.DataFrame,
    *,
    reference_driver: str,
    y_min: float | None = None,
    y_max: float | None = None,
) -> pd.DataFrame:
    leader_lap_time = _reference_lap_time(laps, reference_driver)
    if leader_lap_time is None or leader_lap_time <= 0:
        return pd.DataFrame(columns=["lap_delta", "y", "label"])

    delta_values = pd.to_numeric(plot_data.get("delta_s"), errors="coerce").dropna()
    if delta_values.empty:
        return pd.DataFrame(columns=["lap_delta", "y", "label"])

    visible_min = float(delta_values.min()) if y_min is None else float(y_min)
    visible_max = float(delta_values.max()) if y_max is None else float(y_max)
    lower = min(visible_min, visible_max)
    upper = max(visible_min, visible_max)

    first_lap = int(np.floor(lower / leader_lap_time))
    last_lap = int(np.ceil(upper / leader_lap_time))
    rows = []
    for lap_delta in range(first_lap, last_lap + 1):
        if lap_delta == 0:
            continue
        y = lap_delta * leader_lap_time
        if lower <= y <= upper:
            rows.append(
                {
                    "lap_delta": lap_delta,
                    "y": y,
                    "label": f"{lap_delta:+d} lap" if abs(lap_delta) == 1 else f"{lap_delta:+d} laps",
                }
            )
    return pd.DataFrame(rows, columns=["lap_delta", "y", "label"])


def _reference_lap_time(laps: pd.DataFrame, reference_driver: str) -> float | None:
    required = {"driver", "lap_time_s"}
    if required.difference(laps.columns):
        return None
    reference_laps = laps.loc[laps["driver"] == reference_driver, "lap_time_s"]
    reference_laps = pd.to_numeric(reference_laps, errors="coerce")
    reference_laps = reference_laps.loc[reference_laps > 0].dropna()
    if reference_laps.empty:
        return None
    return float(reference_laps.median())


def _tyre_age_label(value: object) -> str:
    age = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(age):
        return "?"
    return str(max(int(round(float(age))) - 1, 0))


def assign_driver_styles(drivers: pd.DataFrame) -> pd.DataFrame:
    output = drivers.copy()
    team_colour_lookup: dict[str, str] = {}
    fallback_index = 0
    for team in output["team"].fillna("").astype(str).unique():
        if team in TEAM_COLOURS:
            team_colour_lookup[team] = TEAM_COLOURS[team]
        else:
            team_colour_lookup[team] = FALLBACK_COLOURS[fallback_index % len(FALLBACK_COLOURS)]
            fallback_index += 1

    output["colour"] = output["team"].fillna("").astype(str).map(team_colour_lookup)
    output["line_style"] = "solid"
    for _, team_rows in output.groupby("team", sort=False, dropna=False):
        for offset, row_index in enumerate(team_rows.index):
            output.loc[row_index, "line_style"] = "solid" if offset == 0 else "dash"
    return output


def finite_xy(data: pd.DataFrame, *, y_column: str) -> tuple[np.ndarray, np.ndarray]:
    x = pd.to_numeric(data["lap"], errors="coerce")
    y = pd.to_numeric(data[y_column], errors="coerce")
    valid = x.notna() & y.notna()
    return x.loc[valid].to_numpy(dtype="float64"), y.loc[valid].to_numpy(dtype="float64")
