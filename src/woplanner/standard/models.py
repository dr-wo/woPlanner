"""State contracts for the standard race planner."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

COMPARISON_MODE_MODELLED_REAL_STRATEGY = "modelled_real_strategy"
COMPARISON_MODE_OBSERVED_REAL_DATA = "observed_real_data"
COMPARISON_MODE_PURE_SIM = "pure_sim"

COMPARISON_MODE_LABELS = {
    COMPARISON_MODE_PURE_SIM: "Pure sim - model lap times",
    COMPARISON_MODE_MODELLED_REAL_STRATEGY: "Real-race adjusted - add observed issues",
    COMPARISON_MODE_OBSERVED_REAL_DATA: "Observed real data",
}


@dataclass
class PlannerRaceSession:
    event_key: str
    year: int
    round_number: int
    session_name: str
    laps: pd.DataFrame
    drivers: pd.DataFrame


@dataclass
class StrategyEdit:
    edit_id: str
    edit_type: str
    stop_after_lap: int
    compound: str | None
    initial_tyre_age: int | None
    pit_loss_s: float | None
    pit_in_loss_s: float | None = None
    pit_out_loss_s: float | None = None
    pit_loss_source: str | None = None


@dataclass
class DriverStrategyScenario:
    scenario_id: str
    driver: str
    edits: list[StrategyEdit]
    comparison_mode: str = COMPARISON_MODE_MODELLED_REAL_STRATEGY
    is_active: bool = True
    use_team_model_data: bool = False
    green_pit_loss_s: float | None = None
    sc_pit_loss_s: float | None = None
    vsc_pit_loss_s: float | None = None


PitStopScenario = DriverStrategyScenario


@dataclass
class ScenarioTrajectory:
    scenario_id: str
    driver: str
    laps: pd.DataFrame


@dataclass
class StintSegment:
    start_lap: int
    end_lap: int
    compound: str
    initial_tyre_age: int


@dataclass
class PlannedPitStop:
    stop_after_lap: int
    first_lap_after_stop: int
    pit_loss_s: float
    pit_in_loss_s: float | None = None
    pit_out_loss_s: float | None = None


@dataclass
class StrategyPlan:
    driver: str
    team: str
    start_after_lap: int
    end_lap: int
    segments: list[StintSegment]
    pit_stops: list[PlannedPitStop]


@dataclass
class StrategyComparison:
    scenario_id: str
    driver: str
    comparison_mode: str
    what_if_laps: pd.DataFrame
    comparison_laps: pd.DataFrame
    warnings: list[str] | None = None
