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
