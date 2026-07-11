from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class PlannerRaceSession:
    event_key: str
    year: int
    round_number: int
    session_name: str
    laps: pd.DataFrame
    drivers: pd.DataFrame


@dataclass
class PitStopScenario:
    scenario_id: str
    driver: str
    stop_after_lap: int
    first_simulated_lap: int
    compound: str
    initial_tyre_age: int
    pit_loss_s: float
    is_active: bool = True


@dataclass
class ScenarioTrajectory:
    scenario_id: str
    driver: str
    laps: pd.DataFrame
