"""Bounded, causal Qualifying adapter for the shared physical traffic evaluator."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from sys import getsizeof
from time import perf_counter

import pandas as pd

from wodata.live import QualiPitState, QualiRawSnapshot
from wostrategy.analysis.traffic import (
    PHYSICAL_CIRCULAR,
    TRAFFIC_STATUS_OK,
    TrafficCoveragePolicy,
    TrafficEvaluationConfig,
    evaluate_physical_traffic,
)


QUALIFYING_TRAFFIC_POLICY = TrafficCoveragePolicy(
    require_ahead=True, require_behind=False,
    min_ahead_coverage=0.5, min_behind_coverage=0.0,
)
ROLLING_TRAFFIC_SECONDS = 240.0


@dataclass(frozen=True)
class PhysicalLapTraffic:
    mean_ahead_gap_s: float | None = None
    ahead_coverage: float = 0.0
    status: str = "unknown"
    target_samples: int = 0
    ahead_samples: int = 0
    circuit_length_m: float | None = None
    method: str = PHYSICAL_CIRCULAR
    evaluator_version: str | None = None

    @property
    def available(self) -> bool:
        return self.status == TRAFFIC_STATUS_OK and self.mean_ahead_gap_s is not None


class RollingQualiPhysicalTraffic:
    """Retain recent field telemetry and finalize only confirmed completed laps."""

    def __init__(self, *, policy: TrafficCoveragePolicy = QUALIFYING_TRAFFIC_POLICY) -> None:
        self.policy = policy
        self._rows: dict[str, deque[dict]] = {}
        self._last_time: dict[str, float] = {}
        self._lap_spans: deque[float] = deque(maxlen=32)
        self.last_evaluation_s = 0.0
        self.evaluation_times_s: deque[float] = deque(maxlen=128)
        self.max_rows = 0

    @property
    def row_count(self) -> int:
        return sum(len(rows) for rows in self._rows.values())

    @property
    def estimated_buffer_bytes(self) -> int:
        """Approximate resident row storage, excluding shared dict keys."""
        return sum(
            getsizeof(rows) + sum(getsizeof(row) + sum(getsizeof(value)
                                                    for value in row.values())
                                      for row in rows)
            for rows in self._rows.values()
        )

    def ingest(self, snapshot: QualiRawSnapshot) -> None:
        newest = None
        drivers = {driver.driver_number: driver for driver in snapshot.drivers}
        observations = snapshot.position_observations or tuple(
            (driver.driver_number, driver.latest_position_time_s, None, None)
            for driver in snapshot.drivers if driver.latest_position_time_s is not None
        )
        for observation in observations:
            if isinstance(observation, tuple):
                number, stamp, status, pit_side = observation
            else:
                number, stamp, status, pit_side = (
                    observation.driver_number, observation.session_time_s,
                    observation.status, observation.pit_side,
                )
            driver = drivers.get(number)
            if driver is None:
                continue
            speed_time = driver.latest_telemetry_time_s
            speed = driver.speed_kph
            lap = driver.current_lap_identity
            if (stamp is None or speed_time is None or speed is None or lap is None
                    or speed < 0 or not 0 <= stamp - speed_time <= 5.0):
                continue
            if stamp <= self._last_time.get(driver.driver_number, float("-inf")):
                continue
            self._last_time[driver.driver_number] = stamp
            state = driver.structural_state
            self._rows.setdefault(driver.driver_number, deque()).append({
                "Driver": driver.driver_code,
                "SessionTime": stamp,
                "LapNumber": lap,
                "Speed": speed,
                # Unknown is insufficient candidate evidence; OUT is on track.
                "CandidateEligible": state == QualiPitState.ON_TRACK
                and pit_side is not True
                and (status is None or status.lower() in {"ontrack", "0"}),
            })
            newest = stamp if newest is None else max(newest, stamp)
        if newest is not None:
            cutoff = newest - ROLLING_TRAFFIC_SECONDS
            for rows in self._rows.values():
                while rows and rows[0]["SessionTime"] < cutoff:
                    rows.popleft()
        self.max_rows = max(self.max_rows, self.row_count)

    def finalize(self, driver_code: str, lap_number: int) -> PhysicalLapTraffic:
        start = perf_counter()
        try:
            rows = [row for driver_rows in self._rows.values() for row in driver_rows]
            target = sorted(
                (row for row in rows if row["Driver"] == driver_code
                 and row["LapNumber"] == lap_number),
                key=lambda row: row["SessionTime"],
            )
            if len(target) < 2:
                return PhysicalLapTraffic()
            span = sum(
                max(0.0, right["SessionTime"] - left["SessionTime"])
                * right["Speed"] / 3.6
                for left, right in zip(target, target[1:])
            )
            if span >= 1000.0:
                self._lap_spans.append(span)
            if not self._lap_spans:
                return PhysicalLapTraffic(target_samples=len(target))
            circuit_length = median(self._lap_spans)
            result = evaluate_physical_traffic(
                pd.DataFrame.from_records(rows),
                config=TrafficEvaluationConfig(
                    circuit_length_m=circuit_length, coverage_policy=self.policy,
                ),
                target_lap=(driver_code, lap_number),
            )
            if result.laps.empty:
                return PhysicalLapTraffic(target_samples=len(target), circuit_length_m=circuit_length)
            lap = result.laps.iloc[0]
            gap = lap["PhysicalMeanTimeDeltaToDriverAhead"]
            return PhysicalLapTraffic(
                mean_ahead_gap_s=float(gap) if pd.notna(gap) else None,
                ahead_coverage=float(lap["AheadCoverageFraction"]),
                status=str(lap["TrafficStatus"]),
                target_samples=int(lap["TargetSampleCount"]),
                ahead_samples=int(lap["AheadMatchedSampleCount"]),
                circuit_length_m=circuit_length,
                evaluator_version=str(lap["TrafficEvaluatorVersion"]),
            )
        finally:
            self.last_evaluation_s = perf_counter() - start
            self.evaluation_times_s.append(self.last_evaluation_s)
