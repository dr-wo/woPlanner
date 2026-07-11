from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from .model import (
    COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    COMPARISON_MODE_OBSERVED_REAL_DATA,
    DriverStrategyScenario,
    PlannedPitStop,
    PlannerRaceSession,
    ScenarioTrajectory,
    StintSegment,
    StrategyEdit,
    StrategyComparison,
    StrategyPlan,
)
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
TRACK_STATUS_SC = "sc"
TRACK_STATUS_VSC = "vsc"


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
            self.scenarios.active_comparisons(),
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
        events = build_tyre_event_data(self._last_plot_data)
        return annotate_removed_observed_pit_events(events, self.scenarios.active_scenarios())

    def build_track_status_event_data(self) -> pd.DataFrame:
        session = self._require_session()
        return build_track_status_event_data(session.laps)

    def pit_loss_default_for_stop(
        self,
        *,
        stop_after_lap: int,
        normal_pit_loss_s: float,
        sc_vsc_pit_loss_s: float,
    ) -> float:
        session = self._require_session()
        first_lap_after_stop = int(stop_after_lap) + 1
        status_events = build_track_status_event_data(session.laps)
        status = status_events.loc[
            pd.to_numeric(status_events["lap"], errors="coerce") == first_lap_after_stop,
            "track_status_type",
        ]
        if not status.empty and str(status.iloc[0]) in {TRACK_STATUS_SC, TRACK_STATUS_VSC}:
            return float(sc_vsc_pit_loss_s)
        return float(normal_pit_loss_s)

    def create_pit_stop_simulation(
        self,
        *,
        driver: str,
        stop_after_lap: int,
        compound: str,
        initial_tyre_age: int,
        pit_loss_s: float,
        comparison_mode: str = COMPARISON_MODE_MODELLED_REAL_STRATEGY,
        normal_pit_loss_s: float | None = None,
        sc_vsc_pit_loss_s: float | None = None,
    ) -> DriverStrategyScenario:
        session = self._require_session()
        comparison_mode = _normalize_comparison_mode(comparison_mode)
        normal_pit_loss_s = float(pit_loss_s if normal_pit_loss_s is None else normal_pit_loss_s)
        sc_vsc_pit_loss_s = float(
            normal_pit_loss_s if sc_vsc_pit_loss_s is None else sc_vsc_pit_loss_s
        )
        driver_laps = session.laps.loc[session.laps["driver"] == driver]
        if driver_laps.empty:
            raise ValueError(f"Driver not found in loaded session: {driver}")
        max_lap = int(pd.to_numeric(driver_laps["lap"], errors="coerce").max())
        stop_after_lap = int(stop_after_lap)
        first_simulated_lap = stop_after_lap + 1
        if first_simulated_lap > max_lap:
            raise ValueError("Simulation must start before the driver's final recorded lap")
        team = str(driver_laps["team"].dropna().iloc[0]) if driver_laps["team"].notna().any() else ""

        existing_scenario = self.scenarios.scenario_for_driver(driver)
        if existing_scenario is None:
            scenario = DriverStrategyScenario(
                scenario_id=f"{driver}-{uuid4().hex[:8]}",
                driver=driver,
                edits=[],
                comparison_mode=comparison_mode,
            )
        else:
            scenario = DriverStrategyScenario(
                scenario_id=existing_scenario.scenario_id,
                driver=existing_scenario.driver,
                edits=list(existing_scenario.edits),
                comparison_mode=comparison_mode,
                is_active=existing_scenario.is_active,
            )

        edit = StrategyEdit(
            edit_id=f"{driver}-lap-{stop_after_lap}",
            edit_type="add_pit",
            stop_after_lap=stop_after_lap,
            compound=str(compound).upper(),
            initial_tyre_age=int(initial_tyre_age),
            pit_loss_s=float(pit_loss_s),
            pit_loss_source="manual",
        )
        scenario.edits = _upsert_strategy_edit(scenario.edits, edit)
        actual_pit_loss = estimate_actual_pit_loss_by_lap(
            session.laps,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
            warnings=[],
        )
        what_if_plan = build_what_if_strategy_plan(
            scenario,
            driver_laps=driver_laps,
            team=team,
            end_lap=max_lap,
            actual_pit_loss_by_lap=actual_pit_loss,
        )
        warnings: list[str] = []
        actual_pit_loss = estimate_actual_pit_loss_by_lap(
            session.laps,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
            warnings=warnings,
        )
        what_if_laps = self._predict_strategy_plan(what_if_plan)
        comparison = self._build_strategy_comparison(
            scenario,
            what_if_plan=what_if_plan,
            what_if_laps=what_if_laps,
            driver_laps=driver_laps,
            team=team,
            actual_pit_loss_by_lap=actual_pit_loss,
            warnings=warnings,
        )
        comparison.comparison_laps = annotate_simulated_pit_markers(
            comparison.comparison_laps,
            scenario.edits,
        )
        display_laps = comparison.comparison_laps.rename(
            columns={"sim_display_lap_time_s": "lap_time_s"}
        )
        trajectory = ScenarioTrajectory(
            scenario_id=scenario.scenario_id,
            driver=driver,
            laps=display_laps.loc[
                :,
                [
                    "driver",
                    "team",
                    "lap",
                    "lap_time_s",
                    "compound",
                    "tyre_age",
                    "pit_out",
                    "edit_id",
                    "stop_after_lap",
                ],
            ],
        )
        self.scenarios.upsert(scenario, trajectory, comparison)
        self._last_plot_data = None
        return scenario

    def create_remove_observed_pit_simulation(
        self,
        *,
        driver: str,
        first_lap_after_stop: int,
        pit_loss_s: float | None = None,
        pit_loss_source: str | None = None,
        comparison_mode: str = COMPARISON_MODE_MODELLED_REAL_STRATEGY,
        normal_pit_loss_s: float | None = None,
        sc_vsc_pit_loss_s: float | None = None,
    ) -> DriverStrategyScenario:
        session = self._require_session()
        comparison_mode = _normalize_comparison_mode(comparison_mode)
        normal_pit_loss_s = float(20.0 if normal_pit_loss_s is None else normal_pit_loss_s)
        sc_vsc_pit_loss_s = float(10.0 if sc_vsc_pit_loss_s is None else sc_vsc_pit_loss_s)
        driver_laps = session.laps.loc[session.laps["driver"] == driver].copy()
        if driver_laps.empty:
            raise ValueError(f"Driver not found in loaded session: {driver}")
        driver_laps = driver_laps.sort_values("lap", kind="mergesort")
        first_lap_after_stop = int(first_lap_after_stop)
        stop_after_lap = first_lap_after_stop - 1
        max_lap = int(pd.to_numeric(driver_laps["lap"], errors="coerce").max())
        if first_lap_after_stop > max_lap:
            raise ValueError("Cannot remove a pit after the driver's final recorded lap")
        pit_lap = driver_laps.loc[pd.to_numeric(driver_laps["lap"], errors="coerce") == first_lap_after_stop]
        if pit_lap.empty or not bool(pit_lap.iloc[0].get("pit_out", False)):
            raise ValueError(f"No observed pit-out lap for {driver} at lap {first_lap_after_stop}")
        previous_lap = driver_laps.loc[pd.to_numeric(driver_laps["lap"], errors="coerce") == stop_after_lap]
        if previous_lap.empty:
            raise ValueError("Cannot remove a pit without a previous lap to continue from")

        warnings: list[str] = []
        actual_pit_loss = estimate_actual_pit_loss_by_lap(
            session.laps,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
            warnings=warnings,
        )
        estimated_loss, estimated_source = _removed_pit_loss_estimate(
            actual_pit_loss,
            driver=driver,
            first_lap_after_stop=first_lap_after_stop,
        )
        if pit_loss_s is None:
            pit_loss_s = estimated_loss
            pit_loss_source = estimated_source
        else:
            pit_loss_source = pit_loss_source or "manual"

        existing_scenario = self.scenarios.scenario_for_driver(driver)
        if existing_scenario is None:
            scenario = DriverStrategyScenario(
                scenario_id=f"{driver}-{uuid4().hex[:8]}",
                driver=driver,
                edits=[],
                comparison_mode=comparison_mode,
            )
        else:
            scenario = DriverStrategyScenario(
                scenario_id=existing_scenario.scenario_id,
                driver=existing_scenario.driver,
                edits=list(existing_scenario.edits),
                comparison_mode=comparison_mode,
                is_active=existing_scenario.is_active,
            )

        continued_compound = str(previous_lap.iloc[0].get("compound", "")).upper()
        continued_age = _int_or_default(previous_lap.iloc[0].get("tyre_age"), 0) + 1
        edit = StrategyEdit(
            edit_id=f"{driver}-remove-lap-{stop_after_lap}",
            edit_type="remove_observed_pit",
            stop_after_lap=stop_after_lap,
            compound=continued_compound,
            initial_tyre_age=continued_age,
            pit_loss_s=float(pit_loss_s),
            pit_loss_source=str(pit_loss_source or "estimated"),
        )
        scenario.edits = _upsert_strategy_edit(scenario.edits, edit)
        actual_pit_loss = _actual_pit_loss_with_remove_edit_overrides(actual_pit_loss, scenario)
        team = str(driver_laps["team"].dropna().iloc[0]) if driver_laps["team"].notna().any() else ""
        what_if_plan = build_what_if_strategy_plan(
            scenario,
            driver_laps=driver_laps,
            team=team,
            end_lap=max_lap,
            actual_pit_loss_by_lap=actual_pit_loss,
        )
        what_if_laps = self._predict_strategy_plan(what_if_plan)
        comparison = self._build_strategy_comparison(
            scenario,
            what_if_plan=what_if_plan,
            what_if_laps=what_if_laps,
            driver_laps=driver_laps,
            team=team,
            actual_pit_loss_by_lap=actual_pit_loss,
            warnings=warnings,
        )
        comparison.comparison_laps = annotate_simulated_pit_markers(
            comparison.comparison_laps,
            scenario.edits,
        )
        display_laps = comparison.comparison_laps.rename(
            columns={"sim_display_lap_time_s": "lap_time_s"}
        )
        trajectory = ScenarioTrajectory(
            scenario_id=scenario.scenario_id,
            driver=driver,
            laps=display_laps.loc[
                :,
                [
                    "driver",
                    "team",
                    "lap",
                    "lap_time_s",
                    "compound",
                    "tyre_age",
                    "pit_out",
                    "edit_id",
                    "stop_after_lap",
                ],
            ],
        )
        self.scenarios.upsert(scenario, trajectory, comparison)
        self._last_plot_data = None
        return scenario

    def build_strategy_delta_data(self) -> pd.DataFrame:
        comparisons = self.scenarios.active_comparisons()
        if not comparisons:
            return _empty_strategy_delta_data()
        frames = []
        for comparison in comparisons:
            data = comparison.comparison_laps.copy()
            if data.empty:
                continue
            data["scenario_id"] = comparison.scenario_id
            data["driver"] = comparison.driver
            data["comparison_mode"] = comparison.comparison_mode
            frames.append(data)
        if not frames:
            return _empty_strategy_delta_data()
        return pd.concat(frames, ignore_index=True, sort=False)

    def clear_simulation(self, scenario_id: str) -> None:
        self.scenarios.clear(scenario_id)
        self._last_plot_data = None

    def remove_strategy_edit(self, scenario_id: str, stop_after_lap: int) -> None:
        session = self._require_session()
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            return
        remaining_edits = [
            edit for edit in scenario.edits if int(edit.stop_after_lap) != int(stop_after_lap)
        ]
        if not remaining_edits:
            self.scenarios.clear(scenario_id)
            self._last_plot_data = None
            return
        rebuilt = DriverStrategyScenario(
            scenario_id=scenario.scenario_id,
            driver=scenario.driver,
            edits=remaining_edits,
            comparison_mode=scenario.comparison_mode,
            is_active=scenario.is_active,
        )
        self._rebuild_scenario(session, rebuilt)
        self._last_plot_data = None

    def remove_strategy_edit_by_id(self, scenario_id: str, edit_id: str) -> None:
        session = self._require_session()
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            return
        remaining_edits = [edit for edit in scenario.edits if str(edit.edit_id) != str(edit_id)]
        if not remaining_edits:
            self.scenarios.clear(scenario_id)
            self._last_plot_data = None
            return
        rebuilt = DriverStrategyScenario(
            scenario_id=scenario.scenario_id,
            driver=scenario.driver,
            edits=remaining_edits,
            comparison_mode=scenario.comparison_mode,
            is_active=scenario.is_active,
        )
        self._rebuild_scenario(session, rebuilt)
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

    def _predict_strategy_plan(self, strategy_plan: StrategyPlan) -> pd.DataFrame:
        session = self._require_session()
        if hasattr(self.strategy_model, "predict_strategy_plan"):
            return self.strategy_model.predict_strategy_plan(
                year=session.year,
                round_number=session.round_number,
                session_name=session.session_name,
                strategy_plan=strategy_plan,
            )
        if len(strategy_plan.segments) != 1:
            raise RuntimeError("The configured strategy model cannot predict multi-segment plans")
        segment = strategy_plan.segments[0]
        predicted = self.strategy_model.predict_future_laps(
            year=session.year,
            round_number=session.round_number,
            session_name=session.session_name,
            driver=strategy_plan.driver,
            team=strategy_plan.team,
            start_lap=segment.start_lap,
            end_lap=segment.end_lap,
            compound=segment.compound,
            initial_tyre_age=segment.initial_tyre_age,
        ).copy()
        predicted["pit_loss_s"] = 0.0
        pit_losses = {
            int(pit_stop.first_lap_after_stop): float(pit_stop.pit_loss_s)
            for pit_stop in strategy_plan.pit_stops
        }
        predicted["pit_loss_s"] = predicted["lap"].astype(int).map(pit_losses).fillna(0.0)
        predicted["predicted_total_lap_time_s"] = (
            pd.to_numeric(predicted["predicted_clean_lap_time_s"], errors="coerce")
            + predicted["pit_loss_s"]
        )
        return predicted

    def _build_strategy_comparison(
        self,
        scenario: DriverStrategyScenario,
        *,
        what_if_plan: StrategyPlan,
        what_if_laps: pd.DataFrame,
        driver_laps: pd.DataFrame,
        team: str,
        actual_pit_loss_by_lap: pd.DataFrame,
        warnings: list[str],
    ) -> StrategyComparison:
        first_stop_after_lap = _earliest_stop_after_lap(scenario)
        default_pit_loss = _first_edit_pit_loss(scenario)
        actual_pit_loss_by_lap = _actual_pit_loss_with_remove_edit_overrides(
            actual_pit_loss_by_lap,
            scenario,
        )
        baseline_plan = build_modelled_real_strategy_plan(
            driver_laps,
            driver=scenario.driver,
            team=team,
            stop_after_lap=first_stop_after_lap,
            default_pit_loss_s=default_pit_loss,
            actual_pit_loss_by_lap=actual_pit_loss_by_lap,
        )
        baseline_laps = self._predict_strategy_plan(baseline_plan)
        baseline_laps = apply_actual_pit_loss_distribution(
            baseline_laps,
            actual_pit_loss_by_lap,
            driver=scenario.driver,
        )
        observed_laps = observed_real_strategy_laps(
            driver_laps,
            first_stop_after_lap,
            actual_pit_loss_by_lap=actual_pit_loss_by_lap,
            driver=scenario.driver,
        )
        comparison_laps = build_strategy_comparison_laps(
            what_if_laps,
            baseline_laps,
            observed_laps,
            comparison_mode=scenario.comparison_mode,
            stop_after_lap=first_stop_after_lap,
        )
        return StrategyComparison(
            scenario_id=scenario.scenario_id,
            driver=scenario.driver,
            comparison_mode=scenario.comparison_mode,
            what_if_laps=what_if_laps,
            comparison_laps=comparison_laps,
            warnings=warnings,
        )

    def _rebuild_scenario(
        self,
        session: PlannerRaceSession,
        scenario: DriverStrategyScenario,
    ) -> None:
        driver_laps = session.laps.loc[session.laps["driver"] == scenario.driver]
        if driver_laps.empty:
            self.scenarios.clear(scenario.scenario_id)
            return
        max_lap = int(pd.to_numeric(driver_laps["lap"], errors="coerce").max())
        team = str(driver_laps["team"].dropna().iloc[0]) if driver_laps["team"].notna().any() else ""
        warnings: list[str] = []
        actual_pit_loss = estimate_actual_pit_loss_by_lap(
            session.laps,
            normal_pit_loss_s=_first_edit_pit_loss(scenario),
            sc_vsc_pit_loss_s=_first_edit_pit_loss(scenario),
            warnings=warnings,
        )
        actual_pit_loss = _actual_pit_loss_with_remove_edit_overrides(actual_pit_loss, scenario)
        what_if_plan = build_what_if_strategy_plan(
            scenario,
            driver_laps=driver_laps,
            team=team,
            end_lap=max_lap,
            actual_pit_loss_by_lap=actual_pit_loss,
        )
        what_if_laps = self._predict_strategy_plan(what_if_plan)
        comparison = self._build_strategy_comparison(
            scenario,
            what_if_plan=what_if_plan,
            what_if_laps=what_if_laps,
            driver_laps=driver_laps,
            team=team,
            actual_pit_loss_by_lap=actual_pit_loss,
            warnings=warnings,
        )
        comparison.comparison_laps = annotate_simulated_pit_markers(
            comparison.comparison_laps,
            scenario.edits,
        )
        display_laps = comparison.comparison_laps.rename(
            columns={"sim_display_lap_time_s": "lap_time_s"}
        )
        trajectory = ScenarioTrajectory(
            scenario_id=scenario.scenario_id,
            driver=scenario.driver,
            laps=display_laps.loc[
                :,
                [
                    "driver",
                    "team",
                    "lap",
                    "lap_time_s",
                    "compound",
                    "tyre_age",
                    "pit_out",
                    "edit_id",
                    "stop_after_lap",
                ],
            ],
        )
        self.scenarios.upsert(scenario, trajectory, comparison)

class ScenarioManager:
    def __init__(self) -> None:
        self._scenarios: dict[str, DriverStrategyScenario] = {}
        self._trajectories: dict[str, ScenarioTrajectory] = {}
        self._comparisons: dict[str, StrategyComparison] = {}

    def active_scenarios(self) -> list[DriverStrategyScenario]:
        return [scenario for scenario in self._scenarios.values() if scenario.is_active]

    def get(self, scenario_id: str) -> DriverStrategyScenario | None:
        return self._scenarios.get(scenario_id)

    def scenario_for_driver(self, driver: str) -> DriverStrategyScenario | None:
        for scenario in self.active_scenarios():
            if scenario.driver == driver:
                return scenario
        return None

    def active_trajectories(self) -> list[ScenarioTrajectory]:
        active_ids = {scenario.scenario_id for scenario in self.active_scenarios()}
        return [
            trajectory
            for scenario_id, trajectory in self._trajectories.items()
            if scenario_id in active_ids
        ]

    def active_comparisons(self) -> list[StrategyComparison]:
        active_ids = {scenario.scenario_id for scenario in self.active_scenarios()}
        return [
            comparison
            for scenario_id, comparison in self._comparisons.items()
            if scenario_id in active_ids
        ]

    def upsert(
        self,
        scenario: DriverStrategyScenario,
        trajectory: ScenarioTrajectory,
        comparison: StrategyComparison,
    ) -> None:
        self._scenarios[scenario.scenario_id] = scenario
        self._trajectories[scenario.scenario_id] = trajectory
        self._comparisons[scenario.scenario_id] = comparison

    def clear(self, scenario_id: str) -> None:
        self._scenarios.pop(scenario_id, None)
        self._trajectories.pop(scenario_id, None)
        self._comparisons.pop(scenario_id, None)

    def clear_driver(self, driver: str) -> None:
        for scenario in list(self._scenarios.values()):
            if scenario.driver == driver:
                self.clear(scenario.scenario_id)

    def clear_all(self) -> None:
        self._scenarios.clear()
        self._trajectories.clear()
        self._comparisons.clear()


def _upsert_strategy_edit(
    edits: list[StrategyEdit],
    new_edit: StrategyEdit,
) -> list[StrategyEdit]:
    output = [
        edit for edit in edits if int(edit.stop_after_lap) != int(new_edit.stop_after_lap)
    ]
    output.append(new_edit)
    return sorted(output, key=lambda edit: int(edit.stop_after_lap))


def _earliest_stop_after_lap(scenario: DriverStrategyScenario) -> int:
    if not scenario.edits:
        raise ValueError("Scenario has no strategy edits")
    return min(int(edit.stop_after_lap) for edit in scenario.edits)


def _first_edit_pit_loss(scenario: DriverStrategyScenario) -> float:
    if not scenario.edits:
        return 0.0
    first = min(scenario.edits, key=lambda edit: int(edit.stop_after_lap))
    return float(first.pit_loss_s or 0.0)


def build_what_if_strategy_plan(
    scenario: DriverStrategyScenario,
    *,
    driver_laps: pd.DataFrame,
    team: str,
    end_lap: int,
    actual_pit_loss_by_lap: pd.DataFrame | None = None,
) -> StrategyPlan:
    edits = sorted(scenario.edits, key=lambda edit: int(edit.stop_after_lap))
    if not edits:
        raise ValueError("Scenario has no strategy edits")

    start_after_lap = _earliest_stop_after_lap(scenario)
    future = driver_laps.loc[
        (pd.to_numeric(driver_laps["lap"], errors="coerce") > start_after_lap)
        & (pd.to_numeric(driver_laps["lap"], errors="coerce") <= int(end_lap))
    ].copy()
    future = future.sort_values("lap", kind="mergesort")
    if future.empty:
        return StrategyPlan(
            driver=scenario.driver,
            team=team,
            start_after_lap=start_after_lap,
            end_lap=int(end_lap),
            segments=[],
            pit_stops=[],
        )

    edits_by_stop = {int(edit.stop_after_lap): edit for edit in edits}
    removed_stops = {
        int(edit.stop_after_lap)
        for edit in edits
        if edit.edit_type == "remove_observed_pit"
    }
    simulated_rows: list[dict] = []
    pit_stops: list[PlannedPitStop] = []
    previous_row = driver_laps.loc[
        pd.to_numeric(driver_laps["lap"], errors="coerce") == start_after_lap
    ]
    current_compound = (
        str(previous_row.iloc[0].get("compound", "")).upper() if not previous_row.empty else ""
    )
    current_age = (
        _int_or_default(previous_row.iloc[0].get("tyre_age"), 0) if not previous_row.empty else -1
    )

    for _, row in future.iterrows():
        lap = int(row["lap"])
        stop_after_lap = lap - 1
        edit = edits_by_stop.get(stop_after_lap)
        pit_loss_s = 0.0
        is_actual_pit = bool(row.get("pit_out", False))

        if edit is not None and edit.edit_type == "add_pit":
            current_compound = str(edit.compound or "").upper()
            current_age = int(edit.initial_tyre_age or 0)
            pit_loss_s = float(edit.pit_loss_s or 0.0)
            pit_stops.append(
                PlannedPitStop(
                    stop_after_lap=stop_after_lap,
                    first_lap_after_stop=lap,
                    pit_loss_s=pit_loss_s,
                )
            )
        elif edit is not None and edit.edit_type == "remove_observed_pit":
            if not current_compound:
                current_compound = str(edit.compound or row.get("compound", "")).upper()
            current_age = current_age + 1 if current_age >= 0 else int(edit.initial_tyre_age or 0)
        elif edit is not None:
            raise ValueError(f"Unsupported strategy edit type: {edit.edit_type}")
        elif is_actual_pit and stop_after_lap not in removed_stops:
            current_compound = str(row.get("compound", "")).upper()
            current_age = _int_or_default(row.get("tyre_age"), 0)
            pit_loss_s = _actual_pit_loss_for_lap(
                actual_pit_loss_by_lap,
                driver=scenario.driver,
                lap=lap,
                default=0.0,
            )
            pit_stops.append(
                PlannedPitStop(
                    stop_after_lap=stop_after_lap,
                    first_lap_after_stop=lap,
                    pit_loss_s=pit_loss_s,
                )
            )
        else:
            if not current_compound:
                current_compound = str(row.get("compound", "")).upper()
                current_age = _int_or_default(row.get("tyre_age"), 0)
            else:
                current_age += 1

        simulated_rows.append(
            {
                "lap": lap,
                "compound": current_compound,
                "tyre_age": current_age,
            }
        )

    segments = []
    simulated = pd.DataFrame(simulated_rows)
    groups = _future_stint_groups(simulated)
    for _, group in simulated.groupby(groups, sort=False):
        first = group.iloc[0]
        segments.append(
            StintSegment(
                start_lap=int(first["lap"]),
                end_lap=int(group["lap"].max()),
                compound=str(first["compound"]).upper(),
                initial_tyre_age=_int_or_default(first["tyre_age"], 0),
            )
        )

    return StrategyPlan(
        driver=scenario.driver,
        team=team,
        start_after_lap=start_after_lap,
        end_lap=int(end_lap),
        segments=segments,
        pit_stops=pit_stops,
    )


def _actual_pit_loss_for_lap(
    actual_pit_loss_by_lap: pd.DataFrame | None,
    *,
    driver: str,
    lap: int,
    default: float,
) -> float:
    if actual_pit_loss_by_lap is None or actual_pit_loss_by_lap.empty:
        return float(default)
    pit_loss = actual_pit_loss_by_lap.loc[
        (actual_pit_loss_by_lap["driver"].astype(str) == str(driver))
        & (pd.to_numeric(actual_pit_loss_by_lap["lap"], errors="coerce") == int(lap)),
        "actual_pit_loss_s",
    ]
    if pit_loss.empty or pd.isna(pit_loss.iloc[0]):
        return float(default)
    return float(pit_loss.iloc[0])


def _removed_pit_loss_estimate(
    actual_pit_loss_by_lap: pd.DataFrame,
    *,
    driver: str,
    first_lap_after_stop: int,
) -> tuple[float, str]:
    if actual_pit_loss_by_lap.empty:
        return 0.0, "estimated"
    lap_numbers = {int(first_lap_after_stop), int(first_lap_after_stop) - 1}
    rows = actual_pit_loss_by_lap.loc[
        (actual_pit_loss_by_lap["driver"].astype(str) == str(driver))
        & (pd.to_numeric(actual_pit_loss_by_lap["lap"], errors="coerce").isin(lap_numbers))
    ].copy()
    if rows.empty:
        return 0.0, "estimated"
    loss = float(pd.to_numeric(rows["actual_pit_loss_s"], errors="coerce").fillna(0.0).sum())
    if "actual_pit_loss_source" in rows.columns:
        sources = [
            str(value)
            for value in rows["actual_pit_loss_source"].dropna().tolist()
            if str(value) and str(value) != "none"
        ]
        if sources:
            return loss, sources[-1]
    return loss, "estimated"


def _actual_pit_loss_with_remove_edit_overrides(
    actual_pit_loss_by_lap: pd.DataFrame,
    scenario: DriverStrategyScenario,
) -> pd.DataFrame:
    output = actual_pit_loss_by_lap.copy()
    if "actual_pit_loss_source" not in output.columns:
        output["actual_pit_loss_source"] = "none"
    for edit in scenario.edits:
        if edit.edit_type != "remove_observed_pit":
            continue
        first_lap = int(edit.stop_after_lap) + 1
        driver_mask = output["driver"].astype(str) == str(scenario.driver)
        lap_values = pd.to_numeric(output["lap"], errors="coerce")
        output.loc[driver_mask & (lap_values == int(edit.stop_after_lap)), "actual_pit_loss_s"] = 0.0
        output.loc[
            driver_mask & (lap_values == first_lap),
            ["actual_pit_loss_s", "actual_pit_loss_source"],
        ] = [float(edit.pit_loss_s or 0.0), str(edit.pit_loss_source or "manual")]
    return output


def annotate_removed_observed_pit_events(
    tyre_events: pd.DataFrame,
    scenarios: list[DriverStrategyScenario],
) -> pd.DataFrame:
    if tyre_events.empty:
        return tyre_events
    output = tyre_events.copy()
    output["is_removed_observed_pit"] = False
    output["removed_pit_edit_id"] = pd.NA
    output["removed_pit_loss_s"] = pd.NA
    output["removed_pit_loss_source"] = pd.NA
    output["removed_pit_scenario_id"] = pd.NA
    output["continued_compound"] = pd.NA
    output["continued_tyre_age"] = pd.NA
    for scenario in scenarios:
        for edit in scenario.edits:
            if edit.edit_type != "remove_observed_pit":
                continue
            first_lap = int(edit.stop_after_lap) + 1
            mask = (
                (output["driver"].astype(str) == str(scenario.driver))
                & (pd.to_numeric(output["lap"], errors="coerce") == first_lap)
                & (~output["is_simulated"].fillna(False).astype(bool))
            )
            output.loc[mask, "is_removed_observed_pit"] = True
            output.loc[mask, "removed_pit_edit_id"] = edit.edit_id
            output.loc[mask, "removed_pit_loss_s"] = float(edit.pit_loss_s or 0.0)
            output.loc[mask, "removed_pit_loss_source"] = str(edit.pit_loss_source or "")
            output.loc[mask, "removed_pit_scenario_id"] = scenario.scenario_id
            output.loc[mask, "continued_compound"] = edit.compound
            output.loc[mask, "continued_tyre_age"] = edit.initial_tyre_age
            output.loc[mask, "scenario_id"] = scenario.scenario_id
    return output


def annotate_simulated_pit_markers(
    comparison_laps: pd.DataFrame,
    edits: list[StrategyEdit],
) -> pd.DataFrame:
    output = comparison_laps.copy()
    output["pit_out"] = False
    output["edit_id"] = pd.NA
    output["stop_after_lap"] = pd.NA
    for edit in edits:
        if edit.edit_type != "add_pit":
            continue
        first_lap = int(edit.stop_after_lap) + 1
        mask = pd.to_numeric(output["lap"], errors="coerce") == first_lap
        output.loc[mask, "pit_out"] = True
        output.loc[mask, "edit_id"] = edit.edit_id
        output.loc[mask, "stop_after_lap"] = int(edit.stop_after_lap)
    return output


def build_modelled_real_strategy_plan(
    driver_laps: pd.DataFrame,
    *,
    driver: str,
    team: str,
    stop_after_lap: int,
    default_pit_loss_s: float,
    actual_pit_loss_by_lap: pd.DataFrame | None = None,
) -> StrategyPlan:
    future = driver_laps.loc[pd.to_numeric(driver_laps["lap"], errors="coerce") > stop_after_lap].copy()
    future = future.sort_values("lap", kind="mergesort")
    if future.empty:
        return StrategyPlan(
            driver=driver,
            team=team,
            start_after_lap=int(stop_after_lap),
            end_lap=int(stop_after_lap),
            segments=[],
            pit_stops=[],
        )

    groups = _future_stint_groups(future)
    segments = []
    for _, group in future.groupby(groups, sort=False):
        group = group.sort_values("lap", kind="mergesort")
        first = group.iloc[0]
        segments.append(
            StintSegment(
                start_lap=int(first["lap"]),
                end_lap=int(group["lap"].max()),
                compound=str(first.get("compound", "")).upper(),
                initial_tyre_age=_int_or_default(first.get("tyre_age"), 0),
            )
        )

    pit_stops = []
    if "pit_out" in future.columns:
        pit_out_rows = future.loc[future["pit_out"].fillna(False).astype(bool)]
        for _, row in pit_out_rows.iterrows():
            first_lap = int(row["lap"])
            pit_loss_s = float(default_pit_loss_s)
            if actual_pit_loss_by_lap is not None and not actual_pit_loss_by_lap.empty:
                pit_loss = actual_pit_loss_by_lap.loc[
                    (actual_pit_loss_by_lap["driver"].astype(str) == str(driver))
                    & (pd.to_numeric(actual_pit_loss_by_lap["lap"], errors="coerce") == first_lap),
                    "actual_pit_loss_s",
                ]
                if not pit_loss.empty and pd.notna(pit_loss.iloc[0]):
                    pit_loss_s = float(pit_loss.iloc[0])
            pit_stops.append(
                PlannedPitStop(
                    stop_after_lap=first_lap - 1,
                    first_lap_after_stop=first_lap,
                    pit_loss_s=pit_loss_s,
                )
            )

    return StrategyPlan(
        driver=driver,
        team=team,
        start_after_lap=int(stop_after_lap),
        end_lap=int(future["lap"].max()),
        segments=segments,
        pit_stops=pit_stops,
    )


def apply_actual_pit_loss_distribution(
    model_laps: pd.DataFrame,
    actual_pit_loss_by_lap: pd.DataFrame | None,
    *,
    driver: str,
) -> pd.DataFrame:
    output = model_laps.copy()
    if output.empty or actual_pit_loss_by_lap is None or actual_pit_loss_by_lap.empty:
        return output
    if "lap" not in output.columns or "predicted_clean_lap_time_s" not in output.columns:
        return output

    pit_loss = actual_pit_loss_by_lap.copy()
    pit_loss = pit_loss.loc[pit_loss["driver"].astype(str) == str(driver)]
    if pit_loss.empty:
        return output
    pit_loss = pit_loss.loc[:, ["lap", "actual_pit_loss_s"]].rename(
        columns={"actual_pit_loss_s": "distributed_actual_pit_loss_s"}
    )
    output = output.merge(pit_loss, on="lap", how="left")
    output["pit_loss_s"] = pd.to_numeric(
        output["distributed_actual_pit_loss_s"], errors="coerce"
    ).fillna(0.0)
    output["predicted_total_lap_time_s"] = (
        pd.to_numeric(output["predicted_clean_lap_time_s"], errors="coerce")
        + output["pit_loss_s"]
    )
    return output.drop(columns=["distributed_actual_pit_loss_s"])


def observed_real_strategy_laps(
    driver_laps: pd.DataFrame,
    stop_after_lap: int,
    *,
    actual_pit_loss_by_lap: pd.DataFrame | None = None,
    driver: str | None = None,
) -> pd.DataFrame:
    future = driver_laps.loc[pd.to_numeric(driver_laps["lap"], errors="coerce") > stop_after_lap].copy()
    if future.empty:
        return pd.DataFrame(
            columns=[
                "lap",
                "compound",
                "tyre_age",
                "observed_real_lap_time_s",
                "observed_actual_pit_loss_s",
            ]
        )
    future = future.sort_values("lap", kind="mergesort")
    output = future.loc[:, ["lap", "compound", "tyre_age", "lap_time_s"]].rename(
        columns={"lap_time_s": "observed_real_lap_time_s"}
    )
    output["observed_actual_pit_loss_s"] = 0.0
    if actual_pit_loss_by_lap is not None and not actual_pit_loss_by_lap.empty:
        actual_pit_loss = actual_pit_loss_by_lap.copy()
        if driver is not None and "driver" in actual_pit_loss.columns:
            actual_pit_loss = actual_pit_loss.loc[
                actual_pit_loss["driver"].astype(str) == str(driver)
            ]
        actual_pit_loss = actual_pit_loss.loc[:, ["lap", "actual_pit_loss_s"]].rename(
            columns={"actual_pit_loss_s": "observed_actual_pit_loss_s"}
        )
        output = output.drop(columns=["observed_actual_pit_loss_s"]).merge(
            actual_pit_loss,
            on="lap",
            how="left",
        )
        output["observed_actual_pit_loss_s"] = pd.to_numeric(
            output["observed_actual_pit_loss_s"], errors="coerce"
        ).fillna(0.0)
    return output.reset_index(drop=True)


def estimate_actual_pit_loss_by_lap(
    laps: pd.DataFrame,
    *,
    normal_pit_loss_s: float,
    sc_vsc_pit_loss_s: float,
    warnings: list[str] | None = None,
) -> pd.DataFrame:
    output = laps.loc[:, ["driver", "lap"]].copy()
    output["actual_pit_loss_s"] = 0.0
    output["actual_pit_loss_source"] = "none"
    if "pit_out" not in laps.columns and "pit_in" not in laps.columns:
        return output
    pit_out = (
        laps["pit_out"].fillna(False).astype(bool)
        if "pit_out" in laps.columns
        else pd.Series(False, index=laps.index)
    )
    pit_in = (
        laps["pit_in"].fillna(False).astype(bool)
        if "pit_in" in laps.columns
        else pd.Series(False, index=laps.index)
    )
    if not (pit_out.any() or pit_in.any()):
        return output
    missing_status_count = 0
    used_pit_in_indices: set[object] = set()

    for index, row in laps.loc[pit_out].iterrows():
        pit_loss, status_missing, pit_loss_source = _pit_loss_for_row(
            row,
            laps,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
        )
        if status_missing:
            missing_status_count += 1
        driver = row.get("driver")
        lap = pd.to_numeric(pd.Series([row.get("lap")]), errors="coerce").iloc[0]
        previous_pit_in_index = None
        if pd.notna(lap):
            previous = laps.loc[
                (laps["driver"].astype(str) == str(driver))
                & (pd.to_numeric(laps["lap"], errors="coerce") == int(lap) - 1)
                & pit_in
            ]
            if not previous.empty:
                previous_pit_in_index = previous.index[0]
        if previous_pit_in_index is not None:
            output.loc[previous_pit_in_index, "actual_pit_loss_s"] = float(pit_loss) / 2.0
            output.loc[index, "actual_pit_loss_s"] = float(pit_loss) / 2.0
            output.loc[previous_pit_in_index, "actual_pit_loss_source"] = pit_loss_source
            output.loc[index, "actual_pit_loss_source"] = pit_loss_source
            used_pit_in_indices.add(previous_pit_in_index)
        else:
            output.loc[index, "actual_pit_loss_s"] = pit_loss
            output.loc[index, "actual_pit_loss_source"] = pit_loss_source

    standalone_pit_in = pit_in & ~laps.index.isin(used_pit_in_indices)
    for index, row in laps.loc[standalone_pit_in].iterrows():
        driver = row.get("driver")
        lap = pd.to_numeric(pd.Series([row.get("lap")]), errors="coerce").iloc[0]
        has_next_pit_out = False
        if pd.notna(lap):
            has_next_pit_out = bool(
                (
                    (laps["driver"].astype(str) == str(driver))
                    & (pd.to_numeric(laps["lap"], errors="coerce") == int(lap) + 1)
                    & pit_out
                ).any()
            )
        if has_next_pit_out:
            continue
        pit_loss, status_missing, pit_loss_source = _pit_loss_for_row(
            row,
            laps,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
        )
        if status_missing:
            missing_status_count += 1
        output.loc[index, "actual_pit_loss_s"] = pit_loss
        output.loc[index, "actual_pit_loss_source"] = pit_loss_source
    if missing_status_count and warnings is not None:
        warnings.append(
            f"Track status was unavailable for {missing_status_count} actual pit event(s); "
            "normal pit-loss default was used."
        )
    return output


def track_status_type(value: object) -> str | None:
    status = str(value).upper()
    if status in {"", "<NA>", "NAN", "NONE", "1"}:
        return None
    if "VSC" in status or "VIRTUAL" in status:
        return TRACK_STATUS_VSC
    if "SAFETY" in status or " SC" in f" {status}" or status == "SC":
        return TRACK_STATUS_SC
    codes = {char for char in status if char.isdigit()}
    if "4" in codes:
        return TRACK_STATUS_SC
    if codes.intersection({"6", "7"}):
        return TRACK_STATUS_VSC
    return None


def _is_sc_or_vsc_status(value: object) -> bool:
    return track_status_type(value) in {TRACK_STATUS_SC, TRACK_STATUS_VSC}


def _pit_loss_for_row(
    row: pd.Series,
    laps: pd.DataFrame,
    *,
    normal_pit_loss_s: float,
    sc_vsc_pit_loss_s: float,
) -> tuple[float, bool, str]:
    if "track_status" not in laps.columns or pd.isna(row.get("track_status")):
        return float(normal_pit_loss_s), True, "normal default"
    status = track_status_type(row.get("track_status"))
    if status == TRACK_STATUS_SC:
        return float(sc_vsc_pit_loss_s), False, "SC default"
    if status == TRACK_STATUS_VSC:
        return float(sc_vsc_pit_loss_s), False, "VSC default"
    return float(normal_pit_loss_s), False, "normal default"


def build_track_status_event_data(laps: pd.DataFrame) -> pd.DataFrame:
    if "lap" not in laps.columns or "track_status" not in laps.columns:
        return _empty_track_status_event_data()
    data = laps.loc[:, ["lap", "track_status"]].copy()
    data["lap"] = pd.to_numeric(data["lap"], errors="coerce")
    data = data.dropna(subset=["lap"])
    if data.empty:
        return _empty_track_status_event_data()
    data["track_status_type"] = data["track_status"].map(track_status_type)
    data = data.loc[data["track_status_type"].notna()].copy()
    if data.empty:
        return _empty_track_status_event_data()

    priority = {TRACK_STATUS_SC: 2, TRACK_STATUS_VSC: 1}
    data["priority"] = data["track_status_type"].map(priority).fillna(0)
    output = (
        data.sort_values(["lap", "priority"], ascending=[True, False], kind="mergesort")
        .drop_duplicates(subset=["lap"], keep="first")
        .sort_values("lap", kind="mergesort")
        .reset_index(drop=True)
    )
    output["lap"] = output["lap"].astype(int)
    output["label"] = output["track_status_type"].str.upper()
    return output.loc[:, ["lap", "track_status_type", "label"]]


def build_strategy_comparison_laps(
    what_if_laps: pd.DataFrame,
    baseline_model_laps: pd.DataFrame,
    observed_laps: pd.DataFrame,
    *,
    comparison_mode: str,
    stop_after_lap: int,
) -> pd.DataFrame:
    what_if = what_if_laps.rename(
        columns={
            "predicted_total_lap_time_s": "sim_model_lap_time_s",
            "compound": "what_if_compound",
            "tyre_age": "what_if_tyre_age",
            "pit_loss_s": "what_if_pit_loss_s",
        }
    ).copy()
    baseline = baseline_model_laps.rename(
        columns={
            "predicted_total_lap_time_s": "baseline_model_lap_time_s",
            "compound": "baseline_compound",
            "tyre_age": "baseline_tyre_age",
            "pit_loss_s": "baseline_pit_loss_s",
        }
    ).copy()
    observed = observed_laps.rename(
        columns={
            "compound": "observed_compound",
            "tyre_age": "observed_tyre_age",
        }
    ).copy()

    merged = what_if.loc[
        :,
        [
            "lap",
            "driver",
            "team",
            "sim_model_lap_time_s",
            "what_if_compound",
            "what_if_tyre_age",
            "what_if_pit_loss_s",
        ],
    ].merge(
        baseline.loc[
            :,
            [
                "lap",
                "baseline_model_lap_time_s",
                "baseline_compound",
                "baseline_tyre_age",
                "baseline_pit_loss_s",
            ],
        ],
        on="lap",
        how="inner",
    ).merge(
        observed.loc[
            :,
            [
                "lap",
                "observed_real_lap_time_s",
                "observed_actual_pit_loss_s",
                "observed_compound",
                "observed_tyre_age",
            ],
        ],
        on="lap",
        how="inner",
    )
    if merged.empty:
        return _empty_strategy_delta_data().drop(columns=["scenario_id", "driver", "comparison_mode"])
    merged["model_delta_lap_s"] = (
        pd.to_numeric(merged["sim_model_lap_time_s"], errors="coerce")
        - pd.to_numeric(merged["baseline_model_lap_time_s"], errors="coerce")
    )
    merged["cumulative_model_delta_s"] = merged["model_delta_lap_s"].cumsum()
    merged["observed_actual_pit_loss_s"] = pd.to_numeric(
        merged["observed_actual_pit_loss_s"], errors="coerce"
    ).fillna(0.0)
    merged["baseline_pit_loss_s"] = pd.to_numeric(
        merged["baseline_pit_loss_s"], errors="coerce"
    ).fillna(0.0)
    merged["observed_scaffold_lap_time_s"] = (
        pd.to_numeric(merged["observed_real_lap_time_s"], errors="coerce")
        - merged["observed_actual_pit_loss_s"]
    )
    merged["baseline_clean_lap_time_s"] = (
        pd.to_numeric(merged["baseline_model_lap_time_s"], errors="coerce")
        - merged["baseline_pit_loss_s"]
    )
    merged["race_domain_residual_s"] = (
        pd.to_numeric(merged["observed_real_lap_time_s"], errors="coerce")
        - pd.to_numeric(merged["baseline_model_lap_time_s"], errors="coerce")
    )
    merged["sim_display_lap_time_s"] = (
        pd.to_numeric(merged["sim_model_lap_time_s"], errors="coerce")
        + merged["race_domain_residual_s"]
    )
    merged["sim_display_cumulative_time_s"] = (
        float("nan")
        if merged.empty
        else pd.to_numeric(merged["sim_display_lap_time_s"], errors="coerce").cumsum()
    )
    merged["sim_display_cumulative_time_s"] = (
        pd.to_numeric(merged["sim_display_lap_time_s"], errors="coerce").cumsum()
    )
    merged["model_delta_cumulative_s"] = merged["cumulative_model_delta_s"]
    merged["what_if_lap_time_s"] = merged["sim_model_lap_time_s"]
    merged["baseline_lap_time_s"] = (
        merged["observed_real_lap_time_s"]
        if comparison_mode == COMPARISON_MODE_OBSERVED_REAL_DATA
        else merged["baseline_model_lap_time_s"]
    )
    merged["lap_delta_s"] = merged["model_delta_lap_s"]
    merged["cumulative_delta_s"] = merged["cumulative_model_delta_s"]
    merged["compound"] = merged["what_if_compound"]
    merged["tyre_age"] = merged["what_if_tyre_age"]
    merged["pit_loss_s"] = merged["what_if_pit_loss_s"]
    return merged.loc[
        :,
        [
            "lap",
            "driver",
            "team",
            "observed_real_lap_time_s",
            "observed_actual_pit_loss_s",
            "observed_scaffold_lap_time_s",
            "sim_model_lap_time_s",
            "baseline_model_lap_time_s",
            "baseline_clean_lap_time_s",
            "race_domain_residual_s",
            "model_delta_lap_s",
            "cumulative_model_delta_s",
            "sim_display_lap_time_s",
            "sim_display_cumulative_time_s",
            "compound",
            "tyre_age",
            "pit_loss_s",
            "what_if_lap_time_s",
            "baseline_lap_time_s",
            "lap_delta_s",
            "cumulative_delta_s",
            "what_if_compound",
            "baseline_compound",
            "observed_compound",
            "what_if_tyre_age",
            "baseline_tyre_age",
            "observed_tyre_age",
            "what_if_pit_loss_s",
            "baseline_pit_loss_s",
        ],
    ].reset_index(drop=True)


def _future_stint_groups(future: pd.DataFrame) -> pd.Series:
    lap = pd.to_numeric(future["lap"], errors="coerce")
    compound = future["compound"].astype(str).str.upper() if "compound" in future.columns else ""
    tyre_age = pd.to_numeric(future["tyre_age"], errors="coerce")
    previous_lap = lap.shift(1)
    previous_age = tyre_age.shift(1)
    previous_compound = compound.shift(1) if hasattr(compound, "shift") else compound
    new_group = lap.ne(previous_lap + 1).fillna(True) | compound.ne(previous_compound).fillna(True)
    age_break = (
        tyre_age.notna()
        & previous_age.notna()
        & (tyre_age.astype(float) != previous_age.astype(float) + 1.0)
    )
    new_group = new_group | age_break
    if "stint" in future.columns:
        stint = pd.to_numeric(future["stint"], errors="coerce")
        stint_break = stint.notna() & stint.shift(1).notna() & stint.ne(stint.shift(1))
        new_group = new_group | stint_break
    return new_group.fillna(True).cumsum()


def _int_or_default(value: object, default: int) -> int:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return int(default)
    return int(numeric)


def _normalize_comparison_mode(value: str) -> str:
    mode = str(value)
    if mode in {COMPARISON_MODE_MODELLED_REAL_STRATEGY, COMPARISON_MODE_OBSERVED_REAL_DATA}:
        return mode
    raise ValueError(f"Unknown comparison mode: {value}")


def _empty_strategy_delta_data() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "scenario_id",
            "driver",
            "comparison_mode",
            "lap",
            "observed_real_lap_time_s",
            "observed_actual_pit_loss_s",
            "observed_scaffold_lap_time_s",
            "sim_model_lap_time_s",
            "baseline_model_lap_time_s",
            "baseline_clean_lap_time_s",
            "race_domain_residual_s",
            "model_delta_lap_s",
            "cumulative_model_delta_s",
            "sim_display_lap_time_s",
            "sim_display_cumulative_time_s",
            "compound",
            "tyre_age",
            "pit_loss_s",
            "what_if_lap_time_s",
            "baseline_lap_time_s",
            "lap_delta_s",
            "cumulative_delta_s",
            "what_if_compound",
            "baseline_compound",
            "observed_compound",
            "what_if_tyre_age",
            "baseline_tyre_age",
            "observed_tyre_age",
            "what_if_pit_loss_s",
            "baseline_pit_loss_s",
        ]
    )


def _empty_track_status_event_data() -> pd.DataFrame:
    return pd.DataFrame(columns=["lap", "track_status_type", "label"])


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
    comparisons: list[StrategyComparison] | None = None,
    *,
    reference_driver: str,
) -> pd.DataFrame:
    if not trajectories:
        return base_plot_data

    comparison_by_id = {comparison.scenario_id: comparison for comparison in comparisons or []}
    comparison_by_driver = {comparison.driver: comparison for comparison in comparisons or []}
    reference = _display_reference_cumulative_time(
        base_laps,
        reference_driver=reference_driver,
        reference_comparison=comparison_by_driver.get(reference_driver),
    )
    output_frames = [_rebase_plot_data_to_reference(base_plot_data, reference)]
    faded_drivers = set()
    for trajectory in trajectories:
        comparison = comparison_by_id.get(trajectory.scenario_id)
        if comparison is None or comparison.comparison_laps.empty:
            continue
        base_driver_laps = base_laps.loc[base_laps["driver"] == trajectory.driver]
        scenario_laps = _simulated_display_laps(base_driver_laps, comparison.comparison_laps)
        if scenario_laps.empty:
            continue
        stop_after_lap = int(pd.to_numeric(scenario_laps["lap"], errors="coerce").min()) - 1
        scenario_laps["stint"] = _next_driver_stint(base_driver_laps)
        scenario_laps["pit_in"] = False
        if "pit_out" in scenario_laps.columns:
            scenario_laps["pit_out"] = scenario_laps["pit_out"].fillna(False).astype(bool)
        else:
            scenario_laps["pit_out"] = False
            scenario_laps.loc[scenario_laps.index[0], "pit_out"] = True
        if "stop_after_lap" not in scenario_laps.columns:
            scenario_laps["stop_after_lap"] = pd.NA
        marker_mask = scenario_laps["pit_out"].fillna(False).astype(bool)
        scenario_laps.loc[marker_mask, "stop_after_lap"] = (
            pd.to_numeric(scenario_laps.loc[marker_mask, "lap"], errors="coerce") - 1
        )
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


def _display_reference_cumulative_time(
    base_laps: pd.DataFrame,
    *,
    reference_driver: str,
    reference_comparison: StrategyComparison | None,
) -> pd.DataFrame:
    reference = (
        base_laps.loc[base_laps["driver"] == reference_driver, ["lap", "cumulative_time_s"]]
        .dropna(subset=["lap", "cumulative_time_s"])
        .rename(columns={"cumulative_time_s": "reference_cumulative_time_s"})
        .copy()
    )
    if reference_comparison is None:
        return reference
    base_reference_laps = base_laps.loc[base_laps["driver"] == reference_driver]
    simulated_reference = _simulated_display_laps(
        base_reference_laps,
        reference_comparison.comparison_laps,
    )
    if simulated_reference.empty:
        return reference
    simulated_reference = simulated_reference.loc[:, ["lap", "cumulative_time_s"]].rename(
        columns={"cumulative_time_s": "simulated_reference_cumulative_time_s"}
    )
    reference = reference.merge(simulated_reference, on="lap", how="left")
    reference["reference_cumulative_time_s"] = reference[
        "simulated_reference_cumulative_time_s"
    ].combine_first(reference["reference_cumulative_time_s"])
    return reference.drop(columns=["simulated_reference_cumulative_time_s"])


def _rebase_plot_data_to_reference(
    plot_data: pd.DataFrame,
    reference: pd.DataFrame,
) -> pd.DataFrame:
    output = plot_data.drop(columns=["reference_cumulative_time_s"], errors="ignore").merge(
        reference,
        on="lap",
        how="inner",
    )
    output["delta_s"] = (
        pd.to_numeric(output["cumulative_time_s"], errors="coerce")
        - pd.to_numeric(output["reference_cumulative_time_s"], errors="coerce")
    )
    return output


def _simulated_display_laps(
    base_driver_laps: pd.DataFrame,
    comparison_laps: pd.DataFrame,
) -> pd.DataFrame:
    if comparison_laps.empty:
        return pd.DataFrame()
    scenario_laps = comparison_laps.copy()
    scenario_laps = scenario_laps.dropna(subset=["lap"]).sort_values("lap", kind="mergesort")
    if scenario_laps.empty:
        return pd.DataFrame()
    if "sim_display_lap_time_s" not in scenario_laps.columns:
        return pd.DataFrame()
    stop_after_lap = int(pd.to_numeric(scenario_laps["lap"], errors="coerce").min()) - 1
    base_before_stop = base_driver_laps.loc[
        pd.to_numeric(base_driver_laps["lap"], errors="coerce") == stop_after_lap,
        "cumulative_time_s",
    ]
    if base_before_stop.empty or pd.isna(base_before_stop.iloc[0]):
        return pd.DataFrame()
    scenario_laps["cumulative_time_s"] = (
        float(base_before_stop.iloc[0])
        + pd.to_numeric(scenario_laps["sim_display_lap_time_s"], errors="coerce").cumsum()
    )
    scenario_laps["lap_time_s"] = scenario_laps["sim_display_lap_time_s"]
    return scenario_laps


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
        if "pit_out" in simulated_data.columns:
            simulated_events = simulated_data.loc[
                simulated_data["pit_out"].fillna(False).astype(bool)
            ].copy()
        else:
            simulated_events = simulated_data.groupby(
                "scenario_id", as_index=False, sort=False
            ).first()
        if not simulated_events.empty:
            events.append(simulated_events)

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
    if "stop_after_lap" not in output.columns:
        output["stop_after_lap"] = pd.NA
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
            "stop_after_lap",
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
            "stop_after_lap",
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
