"""Bounded, causal Qualifying adapter for the shared physical traffic evaluator."""

from __future__ import annotations

from collections import deque, OrderedDict
from bisect import bisect_left
from copy import copy
from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite
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
    validated_complete_lap_span,
    lap_span_within_circuit_range,
)


QUALIFYING_TRAFFIC_POLICY = TrafficCoveragePolicy(
    require_ahead=True, require_behind=False,
    min_ahead_coverage=0.5, min_behind_coverage=0.0,
)
ROLLING_TRAFFIC_SECONDS = 240.0
BOUNDARY_RETENTION_SECONDS = 600.0
MAX_ROWS_PER_DRIVER = 4096
MAX_BOUNDARY_RECORDS = 1024
MAX_CALIBRATION_MEMBERS = 32
# The usual history budget stays small. Pending cutoffs have a finite reserve;
# overflow is explicit rather than silently corrupting a protected view.
MAX_PROTECTED_SPAN_VERSIONS = MAX_BOUNDARY_RECORDS + 2 * MAX_CALIBRATION_MEMBERS * (2 * 1024 + 1)
VALID = "VALID"
INVALIDATED = "INVALIDATED"
NOT_ADMITTED = "NOT_ADMITTED"


@dataclass(frozen=True)
class SpanVersion:
    clock: float | None
    domain: str | None
    validity: str
    value: float | None
    retirement: str | None = None


@dataclass
class SpanContribution:
    order: int
    driver: str
    bounds: tuple[float, float]
    coordinate: str | None
    versions: list[SpanVersion]
    # These are source-observation identities, not a step-function return.
    blockers: set[tuple] = field(default_factory=set)
    blocker_sources: dict[tuple, tuple] = field(default_factory=dict)


QUALIFYING_TRAFFIC_VERSION = "quali-physical-phase-v1"


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
        self._span_history: dict[tuple, float] = {}
        # Successful admission identity lasts for this session adapter, even
        # after its contribution leaves the bounded estimator window.
        self._admitted_completion_keys: set[tuple] = set()
        self._lap_starts: dict[str, tuple[int, object]] = {}
        self._boundary_sources: dict[tuple, int | None] = {}
        self._completion_sources: dict[tuple, tuple | None] = {}
        self._completion_start_sources: dict[tuple, tuple] = {}
        self._revoked: set[tuple] = set()
        self._candidate_states: dict[str, deque[dict]] = {}
        self._state_latest: dict[str, dict[tuple, dict]] = {}
        self._evidence_count = 0
        self._boundary_order: dict[str, list[tuple[float, object]]] = {}
        self._boundary_clocks: dict[tuple, tuple] = {}
        # Each source has one value and at most one permanent ambiguity.
        self._boundary_versions: dict[tuple, tuple] = {}
        self._boundary_ordinals: dict[tuple, int] = {}
        self._revocation_clocks: OrderedDict[tuple, tuple] = OrderedDict()
        self._span_clocks: dict[tuple, tuple] = {}
        # One validity/value history owns the current calibration projection.
        # Retirement is terminal; physical invalidation can recover without
        # changing original admission order.
        self._span_versions: OrderedDict[tuple, SpanContribution] = OrderedDict()
        self._span_order = 0
        self._protected_span_cutoffs = ()
        self._calibration_offset_s = None
        self.changed_calibration_clocks = ()
        self.changed_phase_intervals = ()
        self.changed_revoked_keys = ()
        self.changed_acquisition_points = ()
        self._completion_records: dict[tuple, tuple] = {}
        self._evicted_start_laps: set[tuple] = set()
        self._newest_sample_time: float | None = None
        self._sample_watermarks: dict[str | None, float] = {}
        self._row_coordinates: dict[str, set] = {}
        self._state_coordinates: dict[str, set] = {}
        self.revision = 0
        self.changed_sample_times: tuple[tuple[str, float], ...] = ()
        self.changed_state_times: tuple[tuple[str, float], ...] = ()
        self.last_evaluation_s = 0.0
        self.evaluation_times_s: deque[float] = deque(maxlen=128)
        self.max_rows = 0

    @property
    def _lap_spans(self) -> tuple[float, ...]:
        """Read-only compatibility view; contributions themselves are keyed."""
        return tuple(self._span_history.values())

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
            for rows in (*self._rows.values(), *self._candidate_states.values())
        )

    def ingest(self, snapshot: QualiRawSnapshot) -> None:
        """Acquisition coordinates own laps; delivery clocks bound revisions."""
        self.changed_sample_times = ()
        self.changed_state_times = ()
        self.changed_phase_intervals = ()
        self.changed_revoked_keys = ()
        self.changed_acquisition_points = ()
        self.changed_calibration_clocks = ()
        changed_samples: list[tuple[str, float]] = []
        clock = getattr(snapshot, "traffic_clock_time_s", None)
        clock_kind = getattr(snapshot, "traffic_clock_kind", None)
        if clock is None and clock_kind is None:
            # Unclocked legacy producers still establish delivery order at
            # this ingress. Keep that logical receipt domain distinct from
            # seconds, acquisition coordinates and service deadlines.
            self._legacy_ingest_ordinal = getattr(self, "_legacy_ingest_ordinal", 0) + 1
            clock, clock_kind = self._legacy_ingest_ordinal, "ingest_order"
        self._ingest_clock = (clock, clock_kind)
        offset = getattr(snapshot, "speed_coordinate_offset_s", None)
        self._calibration_offset_s = offset if offset is not None and isfinite(offset) else None
        evidence = getattr(snapshot, "traffic_evidence_sequence", ())
        new_evidence = evidence[self._evidence_count:]
        # Older woData objects lack independent completion provenance. Keep
        # ordinary timing available without inventing traffic support for them.
        completions = {key: event for event in snapshot.completed_lap_sequence
                       if (key := getattr(event, "event_key", None)) is not None}
        changed_boundaries = set()
        for event in new_evidence:
            publication = event.publication_time
            source = (event.driver_number, publication)
            if event.kind == "completion_invalidated":
                self._revoke_completion(event.completion_key)
            elif event.kind == "lap_counter" and event.lap_number is not None:
                if publication is not None:
                    if source not in self._boundary_sources:
                        self._boundary_sources[source] = event.lap_number
                        self._boundary_clocks[source] = (clock, clock_kind)
                        self._boundary_versions[source] = ((event.lap_number, clock, clock_kind),)
                        self._boundary_ordinals[source] = self.revision + 1
                        ordered = self._boundary_order.setdefault(event.driver_number, [])
                        index = bisect_left(ordered, publication.seconds, key=lambda item: item[0])
                        ordered.insert(index, (publication.seconds, publication))
                        changed_boundaries.add(event.driver_number)
                        self._changed_phase(source)
                        self.revision += 1
                    elif self._boundary_sources[source] is not None and self._boundary_sources[source] != event.lap_number:
                        self._boundary_versions[source] += ((None, clock, clock_kind),)
                        self._boundary_sources[source] = None
                        self._changed_phase(source)
                        self._revoke_source(source)
                        changed_boundaries.add(event.driver_number)
                        self.revision += 1
                self._lap_starts[event.driver_number] = (event.lap_number + 1, publication)
            elif event.kind in {"off_track", "on_track"} and publication is not None:
                self._record_state(event.driver_number, publication.seconds,
                                   event.kind == "on_track", clock, clock_kind,
                                   publication.coordinate)
        self._evidence_count = len(evidence)
        # Preserve independent completion publications even when no trace has
        # arrived yet, so anchors can attach to a later first/last sample.
        for evidence_event in new_evidence:
            if evidence_event.kind != "completion":
                continue
            event = completions.get(evidence_event.completion_key)
            publication = evidence_event.publication_time
            if (event is None or event.classification != "normal_timed" or publication is None
                    or event.event_key in self._revoked):
                continue
            source = (event.driver_number, publication)
            owner = self._completion_sources.setdefault(source, event.event_key)
            if owner != event.event_key:
                self._completion_sources[source] = None
                if self._boundary_sources.get(source) is not None:
                    self._boundary_versions[source] = self._boundary_versions.get(source, ()) + ((None, clock, clock_kind),)
                self._boundary_sources[source] = None
                self._changed_phase(source)
                self._revoke_source(source)
                self._revoke_completion(owner)
                self._revoke_completion(event.event_key)
                continue
            if event.event_key not in self._completion_records:
                self._completion_records[event.event_key] = (event, publication, clock, clock_kind)
                self._changed_phase(source)
                changed_boundaries.add(event.driver_number)
                self.revision += 1
        for number in changed_boundaries:
            for row in self._rows.get(number, ()):
                lap = self.lap_at_sample(number, row["SessionTime"], row.get("_AcquisitionCoordinate"))
                if row["LapNumber"] != lap:
                    row["LapNumber"] = lap
                    changed_samples.append((number, row["SessionTime"]))
                    self.revision += 1

        drivers = {driver.driver_number: driver for driver in snapshot.drivers}
        history = getattr(snapshot, "speed_history", None)
        freshness = float(getattr(snapshot, "speed_freshness_s", 5.0)) if history is not None else 5.0
        observations = snapshot.position_observations
        # Production CarData must not reprocess an old position with speed
        # received after that position. Legacy manual snapshots lack history.
        if history is None and not observations:
            observations = tuple(
                (driver.driver_number, driver.latest_position_time_s, None, None)
                for driver in snapshot.drivers if driver.latest_position_time_s is not None
            )
        for observation in observations:
            if isinstance(observation, tuple):
                number, stamp, status, pit_side = observation
                coordinate = None
            else:
                number, stamp, status, pit_side = (
                    observation.driver_number, observation.session_time_s,
                    observation.status, observation.pit_side,
                )
                coordinate = getattr(observation, "acquisition_coordinate", None)
            driver = drivers.get(number)
            if driver is None or stamp is None or not isfinite(stamp):
                continue
            watermark = self._sample_watermarks.get(coordinate)
            received = getattr(observation, "received_at_utc", None) or snapshot.last_message_at_utc
            if pit_side is True or status is not None:
                on_track = pit_side is not True and str(status).lower() in {"ontrack", "0"}
                if on_track or pit_side is True or str(status).lower() in {"offtrack", "1"}:
                    self._record_state(number, stamp, on_track, clock, clock_kind, coordinate)
            if watermark is not None and stamp < watermark - ROLLING_TRAFFIC_SECONDS:
                continue
            self._sample_watermarks[coordinate] = max(stamp, watermark if watermark is not None else stamp)
            self._newest_sample_time = self._sample_watermarks[coordinate]
            if history is None:
                speed_time, speed = driver.latest_telemetry_time_s, driver.speed_kph
            else:
                speed_time, speed = None, None
                offset = getattr(snapshot, "speed_coordinate_offset_s", None)
                receipt_mono = getattr(observation, "received_at_monotonic_s", None)
                for candidate in history.get(number, ()):
                    candidate_time = candidate.session_time_s
                    candidate_coordinate = getattr(candidate, "acquisition_coordinate", None)
                    if (coordinate is not None and candidate_coordinate is not None
                            and coordinate != candidate_coordinate):
                        if offset is None or not isfinite(offset):
                            continue
                        candidate_time += offset if coordinate == "utc" else -offset
                    if not 0 <= stamp - candidate_time <= freshness:
                        continue
                    candidate_mono = getattr(candidate, "received_at_monotonic_s", None)
                    if candidate_mono is not None and receipt_mono is not None:
                        if candidate_mono > receipt_mono:
                            continue
                    elif (candidate.received_at_utc is not None and received is not None
                          and candidate.received_at_utc > received):
                        continue
                    if speed_time is None or candidate_time > speed_time:
                        speed_time, speed = candidate_time, candidate.speed_kph
            lap = self.lap_at_sample(number, stamp, coordinate)
            if lap is None and history is None and coordinate is None:
                lap = driver.current_lap_identity
            if (speed_time is None or speed is None or not isfinite(speed)
                    or speed < 0 or not 0 <= stamp - speed_time <= freshness):
                continue
            rows = self._rows.setdefault(number, deque())
            index = bisect_left(rows, stamp, key=lambda row: row["SessionTime"])
            if any(row["SessionTime"] == stamp and row.get("_AcquisitionCoordinate") == coordinate
                   for row in rows):
                continue
            eligible = (pit_side is not True and
                        (str(status).lower() in {"ontrack", "0"} if status is not None
                         else driver.structural_state == QualiPitState.ON_TRACK))
            rows.insert(index, {
                "Driver": driver.driver_code, "SessionTime": stamp,
                "LapNumber": lap, "Speed": speed,
                "LapStartTime": None, "_StartPublication": None,
                "LapEndTime": None, "_CompletionKey": None,
                "PhaseRouteValid": (not driver.out_lap_confirmed
                                    or (self.completed_lap_for_sample(number, stamp, coordinate) is not None
                                        and driver.current_lap_identity != lap)),
                "_RouteLapAtReception": driver.current_lap_identity,
                "_RouteValidAtReception": not driver.out_lap_confirmed,
                "_AcquisitionCoordinate": coordinate,
                "_CalibrationAliases": self._calibration_aliases(stamp, coordinate),
                "_IngestOrdinal": self.revision + 1,
                "CandidateEligible": eligible,
                "_ReceivedClock": clock, "_ClockKind": clock_kind,
            })
            self._row_coordinates.setdefault(number, set()).add(coordinate)
            self._last_time[number] = max(stamp, self._last_time.get(number, stamp))
            changed_samples.append((number, stamp))
            self.changed_acquisition_points += ((number, coordinate, stamp),)
            self.revision += 1
        # Reconcile affected certified traversals before ordinary retention
        # can remove their integration/anchor support. Fitting locks do not
        # terminate this independent calibration lifecycle.
        for number in set(changed_boundaries) | {number for number, _ in changed_samples}:
            self._refresh_anchors(number)
        self._reconcile_contributions(clock, clock_kind)
        newest = self._newest_sample_time
        if newest is not None:
            def cutoff_for(row):
                coordinate = row.get("_AcquisitionCoordinate")
                watermark = self._sample_watermarks.get(coordinate, self._sample_watermarks.get(None))
                return watermark - ROLLING_TRAFFIC_SECONDS if watermark is not None else -float("inf")

            for number, rows in self._rows.items():
                if len(set(self._row_coordinates.get(number, ())) - {None}) > 1:
                    # UTC epochs and mapped session seconds cannot share a
                    # numeric eviction order. Preserve each domain's final
                    # uncertainty row, then cap by actual ingestion order.
                    last = {row.get("_AcquisitionCoordinate"): row for row in rows}
                    retained = [row for row in rows
                                if row["SessionTime"] >= cutoff_for(row)
                                or row is last[row.get("_AcquisitionCoordinate")]]
                    if len(retained) > MAX_ROWS_PER_DRIVER:
                        kept = sorted(retained, key=lambda row: row.get("_IngestOrdinal", 0))[
                            -MAX_ROWS_PER_DRIVER:]
                        identities = {id(row) for row in kept}
                        retained = [row for row in retained if id(row) in identities]
                    identities = {id(row) for row in retained}
                    for row in rows:
                        if id(row) not in identities and row.get("_StartPublication") is not None:
                            self._evicted_start_laps.add(
                                (number, row["LapNumber"], row["_StartPublication"]))
                    rows.clear()
                    rows.extend(retained)
                    self._row_coordinates[number] = {row.get("_AcquisitionCoordinate") for row in rows}
                    continue
                while len(rows) > 1 and (rows[0]["SessionTime"] < cutoff_for(rows[0])
                                         or len(rows) > MAX_ROWS_PER_DRIVER):
                    if rows[0].get("_StartPublication") is not None:
                        self._evicted_start_laps.add(
                            (number, rows[0]["LapNumber"], rows[0]["_StartPublication"]))
                    rows.popleft()
            for number, states in self._candidate_states.items():
                if len(set(self._state_coordinates.get(number, ())) - {None}) > 1:
                    predecessors = {}
                    for state in states:
                        if state["SessionTime"] < cutoff_for(state):
                            predecessors[state.get("_AcquisitionCoordinate")] = state
                    retained = [state for state in states
                                if state["SessionTime"] >= cutoff_for(state)
                                or state is predecessors.get(state.get("_AcquisitionCoordinate"))]
                    if len(retained) > MAX_ROWS_PER_DRIVER:
                        kept = sorted(retained, key=lambda state: state.get("_IngestOrdinal", 0))[
                            -MAX_ROWS_PER_DRIVER:]
                        identities = {id(state) for state in kept}
                        retained = [state for state in retained if id(state) in identities]
                    retained_ids = {id(state) for state in retained}
                    for state in states:
                        if id(state) not in retained_ids:
                            self._forget_state(number, state)
                    states.clear()
                    states.extend(retained)
                    self._state_coordinates[number] = {
                        state.get("_AcquisitionCoordinate") for state in states}
                    continue
                while len(states) > 1 and (states[1]["SessionTime"] < cutoff_for(states[1])
                                           or len(states) > MAX_ROWS_PER_DRIVER):
                    self._forget_state(number, states.popleft())
            self._trim_boundaries(newest)
        elif self._boundary_order:
            self._trim_boundaries(max(ordered[-1][0] for ordered in self._boundary_order.values()
                                      if ordered))
        for number in set(changed_boundaries) | {number for number, _ in changed_samples}:
            self._refresh_anchors(number)
        self.changed_sample_times = tuple(dict.fromkeys(changed_samples))
        self.max_rows = max(self.max_rows, self.row_count)

    def _start_publication(self, number: str, lap: int, end=None, coordinate=None):
        candidates = [publication for _, publication in self._boundary_order.get(number, ())
                      if self._boundary_sources.get((number, publication)) == lap - 1
                      and (coordinate is None or publication.coordinate == coordinate)
                      and (end is None or (publication.coordinate == end.coordinate
                                           and publication.seconds < end.seconds))]
        if end is None and coordinate is None and len({p.coordinate for p in candidates}) > 1:
            return None
        return candidates[-1] if candidates else None

    def completion_coordinate(self, key):
        record = self._completion_records.get(key)
        return record[1].coordinate if record is not None else None

    def completion_bounds(self, key: tuple) -> tuple[float, float] | None:
        record = self._completion_records.get(key)
        if record is None or key in self._revoked:
            return None
        event, end, _, _ = record
        start = self._start_publication(event.driver_number, event.lap_number, end)
        if (start is None or self._boundary_sources.get((event.driver_number, end)) != event.lap_number
                or abs(end.seconds - start.seconds - event.lap_time_s) > 1.0):
            return None
        return start.seconds, end.seconds

    def completed_lap_for_sample(self, number: str, stamp: float,
                                 coordinate: str | None = None) -> tuple | None:
        """Reliably associate acquisition time, including samples at lap end."""
        candidates = []
        for key in self._completion_records:
            if key[1] != number:
                continue
            if (coordinate is not None
                    and self._completion_records[key][1].coordinate != coordinate):
                continue
            bounds = self.completion_bounds(key)
            if bounds is not None and bounds[0] < stamp <= bounds[1]:
                candidates.append(key)
        return candidates[0] if len(candidates) == 1 else None

    def lap_at_sample(self, number: str, stamp: float,
                      coordinate: str | None = None) -> int | None:
        key = self.completed_lap_for_sample(number, stamp, coordinate)
        if key is not None:
            return key[2]
        ordered = self._boundary_order.get(number, ())
        previous = [publication for time, publication in ordered if time <= stamp
                    and (coordinate is None or publication.coordinate == coordinate)]
        if not previous:
            return None
        # Conflicting publications remain barriers; never resurrect a stale
        # counter by looking past an ambiguous occurrence.
        value = self._boundary_sources.get((number, previous[-1]))
        if (value is not None and previous[-1].seconds == stamp
                and self._start_publication(number, value, previous[-1]) is not None):
            return value
        return None if value is None else value + 1

    def _refresh_anchors(self, number: str) -> None:
        groups: dict[int, list[dict]] = {}
        for row in self._rows.get(number, ()):
            row["LapStartTime"] = row["_StartPublication"] = None
            row["LapEndTime"] = row["_CompletionKey"] = None
            row["_EndReceivedClock"] = row["_EndClockKind"] = None
            if row["LapNumber"] is not None:
                groups.setdefault((row["LapNumber"], row.get("_AcquisitionCoordinate")), []).append(row)
        for (lap, coordinate), rows in groups.items():
            start = self._start_publication(number, lap, coordinate=coordinate)
            if start is not None and (number, lap, start) not in self._evicted_start_laps:
                rows[0]["LapStartTime"] = start.seconds
                rows[0]["_StartPublication"] = start
            for key, record in self._completion_records.items():
                if (key[1:3] != (number, lap) or self.completion_bounds(key) is None
                        or (coordinate is not None and record[1].coordinate != coordinate)):
                    continue
                event, publication, clock, kind = record
                bounds = self.completion_bounds(key)
                for row in rows:
                    if (bounds[0] <= row["SessionTime"] <= bounds[1]
                            and row.get("_RouteLapAtReception") != lap):
                        row["PhaseRouteValid"] = True
                rows[-1]["LapEndTime"] = publication.seconds
                rows[-1]["_CompletionKey"] = key
                rows[-1]["_EndReceivedClock"] = clock
                rows[-1]["_EndClockKind"] = kind
                self._completion_start_sources[key] = (number, start)
                break

    def _trim_boundaries(self, newest: float) -> None:
        publication_watermarks = {}
        for ordered in self._boundary_order.values():
            for time, publication in ordered:
                publication_watermarks[publication.coordinate] = max(
                    time, publication_watermarks.get(publication.coordinate, time))

        def floor_for(publication):
            watermark = self._sample_watermarks.get(
                publication.coordinate, self._sample_watermarks.get(None))
            if watermark is None:
                watermark = publication_watermarks.get(publication.coordinate, newest)
            return watermark - BOUNDARY_RETENTION_SECONDS
        # Preserve one predecessor in each coordinate domain. UTC epochs
        # cannot determine eviction priority for session boundaries; cardinality
        # eviction follows actual ingestion and retains each domain's newest.
        for number, ordered in self._boundary_order.items():
            predecessors, newest_by_domain = {}, {}
            for time, publication in ordered:
                newest_by_domain[publication.coordinate] = publication
                if time < floor_for(publication):
                    predecessors[publication.coordinate] = publication
            retained = [(time, publication) for time, publication in ordered
                        if time >= floor_for(publication)
                        or publication == predecessors.get(publication.coordinate)]
            if len(retained) > 128:
                protected = set(newest_by_domain.values())
                others = sorted((publication for _, publication in retained if publication not in protected),
                                key=lambda publication: self._boundary_ordinals.get((number, publication), 0))
                keep = protected | set(others[-(128 - len(protected)):])
                retained = [(time, publication) for time, publication in retained if publication in keep]
            kept = {publication for _, publication in retained}
            for _, publication in ordered:
                if publication in kept:
                    continue
                source = (number, publication)
                self._boundary_sources.pop(source, None)
                self._boundary_clocks.pop(source, None)
                self._boundary_versions.pop(source, None)
                self._boundary_ordinals.pop(source, None)
                self._completion_sources.pop(source, None)
                self._evicted_start_laps = {item for item in self._evicted_start_laps
                                            if item[0] != number or item[2] != publication}
            ordered[:] = retained
        while len(self._completion_records) > MAX_BOUNDARY_RECORDS:
            key = next(iter(self._completion_records))
            del self._completion_records[key]
            self._completion_start_sources.pop(key, None)
        for key, record in list(self._completion_records.items()):
            if record[1].seconds < floor_for(record[1]):
                del self._completion_records[key]
                self._completion_start_sources.pop(key, None)

    def _record_state(self, number: str, stamp: float, eligible: bool,
                      clock: float | None = None, clock_kind: str | None = None,
                      coordinate: str | None = None) -> None:
        states = self._candidate_states.setdefault(number, deque())
        row = {"SessionTime": stamp, "CandidateEligible": eligible,
               "_ReceivedClock": clock, "_ClockKind": clock_kind,
               "_AcquisitionCoordinate": coordinate,
               "_IngestOrdinal": self.revision + 1}
        index = bisect_left(states, stamp, key=lambda item: item["SessionTime"])
        same = ([] if index == len(states) or states[index]["SessionTime"] != stamp else
                [state for state in states if state["SessionTime"] == stamp
                 and (coordinate is None or state.get("_AcquisitionCoordinate") in {None, coordinate})])
        identities = self._state_latest.setdefault(number, {})
        logical = identities.get(self._state_identity(stamp, coordinate))
        if logical is not None and logical["CandidateEligible"] == eligible:
            return
        aliases = (self._calibration_aliases(stamp, coordinate)
                   or (same[-1].get("_CalibrationAliases", ()) if same else ()))
        if not aliases and logical is not None and logical.get("_AcquisitionCoordinate") != coordinate:
            aliases = ((logical.get("_AcquisitionCoordinate"), logical["SessionTime"]),)
        if not aliases:
            identity = self._state_identity(stamp, coordinate)
            aliases = tuple((point[1], point[0]) for key, record in self._span_versions.items()
                            if key[1] == number and (point := record.blocker_sources.get(identity)) is not None
                            and point[1] != coordinate)
        row["_CalibrationAliases"] = aliases
        if same:
            # Keep receipt versions in delivery order, including withdrawals
            # and corrected returns. The evaluator selects the last version
            # that survives the receipt cutoff.
            end = index
            while end < len(states) and states[end]["SessionTime"] == stamp:
                end += 1
            states.insert(end, row)
        else:
            states.insert(index, row)
        for identity in (self._state_identity(stamp, coordinate),
                         *(self._state_identity(time, domain) for domain, time in aliases)):
            identities[identity] = row
        self._state_coordinates.setdefault(number, set()).add(coordinate)
        self.changed_state_times += ((number, stamp),)
        self.changed_acquisition_points += ((number, coordinate, stamp),)
        self.revision += 1

    def _revoke_completion(self, key: tuple | None) -> None:
        if key is None:
            return
        if key not in self._revoked:
            self.revision += 1
            self.changed_revoked_keys += (key,)
            self._revocation_clocks[key] = getattr(self, "_ingest_clock", (None, None))
            while len(self._revocation_clocks) > MAX_BOUNDARY_RECORDS:
                self._revocation_clocks.popitem(last=False)
            record = self._completion_records.get(key)
            if record is not None:
                self._changed_phase((key[1], record[1]))
        self._revoked.add(key)
        if key in self._span_versions:
            self._retire_span(key, getattr(self, "_ingest_clock", (None, None)), "ownership")
        self._span_history.pop(key, None)
        self._span_clocks.pop(key, None)
        for row in self._rows.get(key[1], ()):
            if row.get("_CompletionKey") == key:
                row["LapEndTime"] = None

    def _revoke_source(self, source: tuple) -> None:
        for key, start_source in self._completion_start_sources.items():
            if start_source == source:
                self._revoke_completion(key)
        invalid_laps = set()
        for row in self._rows.get(source[0], ()):
            if row.get("_StartPublication") == source[1]:
                row["LapStartTime"] = None
                invalid_laps.add(row["LapNumber"])
        for row in self._rows.get(source[0], ()):
            if row["LapNumber"] in invalid_laps:
                self._revoke_completion(row.get("_CompletionKey"))
        self._revoke_completion(self._completion_sources.get(source))

    def _changed_phase(self, source):
        number, publication = source
        if publication is None:
            return
        times = [time for time, point in self._boundary_order.get(number, ())
                 if point.coordinate == publication.coordinate]
        before = [time for time in times if time < publication.seconds]
        after = [time for time in times if time > publication.seconds]
        interval = (number, publication.coordinate,
                    max(before) if before else publication.seconds,
                    min(after) if after else float("inf"))
        if interval not in self.changed_phase_intervals:
            self.changed_phase_intervals += (interval,)

    def protect_calibration_cutoffs(self, cutoffs):
        self._protected_span_cutoffs = tuple(sorted(set(cutoffs), key=lambda item: (str(item[1]), item[0])))
        if len(self._protected_span_cutoffs) > 2 * 1024:
            raise OverflowError("calibration protection exceeds capped pending lifecycle")
        self._compact_span_versions()

    @staticmethod
    def _version_at(record, deadline, kind):
        admission = record.versions[0]
        if deadline is None:
            return record.versions[-1]
        # A corrective version cannot create an admission before its original
        # certification stage, even with out-of-order recorded receipts.
        if admission.clock is not None and (admission.domain != kind or admission.clock > deadline):
            return None
        return next((version for version in reversed(record.versions)
                     if version.clock is None or version.domain == kind and version.clock <= deadline), None)

    def _project_spans(self):
        self._span_history = {
            key: record.versions[-1].value for key, record in self._span_versions.items()
            if record.versions[-1].validity == VALID and record.versions[-1].retirement is None
        }
        self._span_clocks = {key: (self._span_versions[key].versions[-1].clock,
                                  self._span_versions[key].versions[-1].domain)
                             for key in self._span_history}

    def _ensure_span_version_room(self, key=None):
        if sum(len(record.versions) for record in self._span_versions.values()) >= MAX_PROTECTED_SPAN_VERSIONS:
            self._compact_span_versions(keep_keys=(key,) if key is not None else ())
            if sum(len(record.versions) for record in self._span_versions.values()) >= MAX_PROTECTED_SPAN_VERSIONS:
                raise OverflowError("protected calibration transitions exceed finite history reserve")

    def _transition_span(self, key, validity, value, clock, retirement=None):
        record = self._span_versions.get(key)
        if record is None or record.versions[-1].retirement is not None:
            return False
        previous = record.versions[-1]
        if (previous.validity, previous.value, previous.retirement) == (validity, value, retirement):
            return False
        self._ensure_span_version_room(key)
        record.versions.append(SpanVersion(*clock, validity, value, retirement))
        if clock not in self.changed_calibration_clocks:
            self.changed_calibration_clocks += (clock,)
        self.revision += 1
        self._project_spans()
        return True

    def _retire_span(self, key, clock, reason="capacity"):
        record = self._span_versions.get(key)
        if record is not None:
            if self._transition_span(key, record.versions[-1].validity,
                                     record.versions[-1].value, clock, reason):
                # Terminal membership cannot recover; only its compact
                # validity/value history is needed by protected cutoffs.
                record.blockers.clear()
                record.blocker_sources.clear()

    def _enforce_span_capacity(self, clock):
        # Dictionary order is original admission order, never recovery order.
        while len(self._span_history) > MAX_CALIBRATION_MEMBERS:
            self._retire_span(next(iter(self._span_history)), clock)

    def _compact_span_versions(self, keep_keys=()):
        total = sum(len(record.versions) for record in self._span_versions.values())
        if total <= MAX_BOUNDARY_RECORDS and len(self._span_versions) <= MAX_BOUNDARY_RECORDS:
            return
        floors = {}
        for cutoff, kind in self._protected_span_cutoffs:
            floors[kind] = min(cutoff, floors.get(kind, cutoff))
        # Keep admission identity separately from the compacted baseline.
        # Preserve a baseline at/before the earliest protected cutoff in each
        # domain, and every later transition while that work remains pending.
        for record in self._span_versions.values():
            versions = record.versions
            keep = {0, len(versions) - 1}
            for kind, floor in floors.items():
                before = [i for i, v in enumerate(versions)
                          if v.domain == kind and v.clock is not None and v.clock <= floor]
                if before:
                    keep.add(before[-1])
                keep.update(i for i, v in enumerate(versions)
                            if v.domain == kind and v.clock is not None and v.clock > floor)
            if not floors:
                # With no protected work, keep the latest recent versions;
                # ordinary queries remain a bounded diagnostic convenience.
                continue
            record.versions = [v for i, v in enumerate(versions) if i in keep]
        protected_keys = {key for cutoff, kind in self._protected_span_cutoffs
                          for key in self._spans_at_deadline(cutoff, kind)}
        # Recoverable/current records get the existing metadata budget. Older
        # records needed by a pending estimator view use the finite reserve.
        protected_keys.update(keep_keys)
        unprotected = [key for key in self._span_versions if key not in protected_keys
                       and key not in self._span_history]
        while len(self._span_versions) > MAX_BOUNDARY_RECORDS + len(protected_keys) and unprotected:
            del self._span_versions[unprotected.pop(0)]
        if not floors:
            total = sum(len(r.versions) for r in self._span_versions.values())
            for record in self._span_versions.values():
                while total > MAX_BOUNDARY_RECORDS and len(record.versions) > 2:
                    del record.versions[1]
                    total -= 1
        total = sum(len(record.versions) for record in self._span_versions.values())
        if total > MAX_PROTECTED_SPAN_VERSIONS:
            raise OverflowError("protected calibration transitions exceed finite history reserve")
        self._project_spans()

    def _spans_at_deadline(self, deadline, kind):
        if deadline is None:
            return dict(self._span_history)
        return {key: version.value for key, record in self._span_versions.items()
                if (version := self._version_at(record, deadline, kind)) is not None
                and version.validity == VALID and version.retirement is None}

    @staticmethod
    def _state_identity(stamp, coordinate):
        return (round(stamp, 6) if coordinate in {"utc", "session"} else stamp, coordinate)

    def _forget_state(self, number, state):
        identities = self._state_latest.get(number, {})
        keys = (self._state_identity(state["SessionTime"], state.get("_AcquisitionCoordinate")),
                *(self._state_identity(stamp, domain) for domain, stamp in state.get("_CalibrationAliases", ())))
        for key in keys:
            if identities.get(key) is state:
                del identities[key]

    def _calibration_aliases(self, stamp, coordinate):
        # This is dependency provenance for calibration copies, not a change
        # to stored observation identities or the warning's D2 ledger.
        offset = self._calibration_offset_s
        if offset is None or coordinate not in {"utc", "session"}:
            return ()
        converted = Decimal(str(stamp)) + (Decimal(str(offset)) if coordinate == "session" else -Decimal(str(offset)))
        return (("utc" if coordinate == "session" else "session",
                 float(converted.quantize(Decimal("0.000001")))),)

    @staticmethod
    def _mapped_acquisition(row, coordinate):
        if row.get("_AcquisitionCoordinate") == coordinate:
            return row["SessionTime"]
        return next((stamp for domain, stamp in row.get("_CalibrationAliases", ()) if domain == coordinate), None)

    def _state_versions_at(self, number, deadline, kind, coordinate):
        latest = {}
        for state in self._candidate_states.get(number, ()):
            stamp = self._mapped_acquisition(state, coordinate)
            if stamp is None:
                continue
            if (deadline is not None and (state.get("_ClockKind") != kind
                    or state.get("_ReceivedClock") is None or state["_ReceivedClock"] > deadline)):
                continue
            identity = self._state_identity(stamp, coordinate)
            previous = latest.get(identity)
            if previous is None or previous["_IngestOrdinal"] < state["_IngestOrdinal"]:
                latest[identity] = dict(state, SessionTime=stamp, _AcquisitionCoordinate=coordinate)
        return latest

    def _apply_eligibility(self, rows, phase, deadline, kind):
        codes = {driver_rows[-1]["Driver"]: number for number, driver_rows in self._rows.items() if driver_rows}
        groups = {}
        for row in rows:
            groups.setdefault((codes[row["Driver"]], row.get("_AcquisitionCoordinate")), []).append(row)
        for (number, coordinate), group in groups.items():
            states = self._state_versions_at(number, deadline, kind, coordinate)
            for row in group:
                state = states.get(self._state_identity(row["SessionTime"], coordinate))
                if state is not None:
                    row["CandidateEligible"] = state["CandidateEligible"]
            for key, record in phase._completion_records.items():
                if key[1] != number or record[1].coordinate != coordinate:
                    continue
                bounds = phase.completion_bounds(key)
                if bounds is not None and any(not state["CandidateEligible"] and bounds[0] < point[0] <= bounds[1]
                                              for point, state in states.items()):
                    # A status-only publication invalidates eligibility, but
                    # never supplies a distance row or an integration speed.
                    target = next((row for row in group if row["LapNumber"] == key[2]), None)
                    if target is not None:
                        target["CandidateEligible"] = False

    def _reconcile_contributions(self, clock, kind):
        if clock is None or kind is None:
            return
        points = self.changed_acquisition_points
        def mapped_point(number_for_record, record, number, coordinate, stamp):
            if number != number_for_record:
                return None
            if coordinate == record.coordinate:
                return stamp
            alias = next((t for domain, t in self._calibration_aliases(stamp, coordinate)
                          if domain == record.coordinate), None)
            if alias is not None:
                return alias
            identity = self._state_identity(stamp, coordinate)
            prior = record.blocker_sources.get(identity)
            if prior is not None:
                return prior[0]
            state = self._state_latest.get(number, {}).get(identity)
            return self._mapped_acquisition(state, record.coordinate) if state is not None else None
        affected = []
        for key, record in self._span_versions.items():
            if record.versions[-1].retirement is None and any(
                    (stamp := mapped_point(key[1], record, *point)) is not None
                    and record.bounds[0] < stamp <= record.bounds[1] for point in points):
                affected.append(key)
        for key in affected:
            record = self._span_versions[key]
            states = self._state_versions_at(key[1], clock, kind, record.coordinate)
            for point in points:
                stamp = mapped_point(key[1], record, *point)
                if stamp is None or not record.bounds[0] < stamp <= record.bounds[1]:
                    continue
                identity = self._state_identity(stamp, record.coordinate)
                state = states.get(identity)
                if state is not None:
                    if state["CandidateEligible"]:
                        record.blockers.discard(identity)
                        record.blocker_sources = {source: target for source, target in record.blocker_sources.items()
                                                  if target != identity}
                    else:
                        if identity not in record.blockers:
                            if len(record.blockers) >= MAX_ROWS_PER_DRIVER or sum(
                                    len(item.blockers) for item in self._span_versions.values()) >= MAX_PROTECTED_SPAN_VERSIONS:
                                raise OverflowError("calibration eligibility blockers exceed retained evidence budget")
                        source_identity = self._state_identity(point[2], point[1])
                        if source_identity not in record.blocker_sources and len(record.blocker_sources) >= 2 * MAX_ROWS_PER_DRIVER:
                            raise OverflowError("calibration blocker provenance exceeds retained evidence budget")
                        record.blockers.add(identity)
                        record.blocker_sources[source_identity] = identity
            if record.blockers:
                self._transition_span(key, INVALIDATED, None, (clock, kind))
                continue
            rows, phase, _ = self._evaluation_rows(record.driver, key[2], clock, kind, key[1], record.coordinate)
            target = sorted((r for r in rows if r["LapNumber"] == key[2]), key=lambda r: r["SessionTime"])
            spans = self._spans_at_deadline(clock, kind)
            config = self._calibration_config(spans)
            span = validated_complete_lap_span(pd.DataFrame.from_records(target), config=config)
            if span is None:
                # Missing retained rows/anchors/mapping is not negative proof.
                continue
            if self._span_eligible(span, spans, config):
                self._transition_span(key, VALID, span, (clock, kind))
            else:
                self._transition_span(key, INVALIDATED, None, (clock, kind))
        self._enforce_span_capacity((clock, kind))
        self._compact_span_versions()

    def _calibration_config(self, spans):
        return TrafficEvaluationConfig(circuit_length_m=median(spans.values()) if spans else 1000.0,
            coverage_policy=self.policy, require_phase_validity=True, evaluator_version=QUALIFYING_TRAFFIC_VERSION)

    @staticmethod
    def _span_eligible(span, spans, config):
        return span is not None and span >= 1000.0 and (not spans or lap_span_within_circuit_range(span, config.circuit_length_m))

    def _phase_at_deadline(self, deadline, kind):
        # Reconstruct only bounded phase metadata; telemetry retains its own
        # first-receipt cutoff. Negative updates have the same causal rules.
        view = copy(self)
        def timely(clock):
            return clock[1] == kind and clock[0] is not None and clock[0] <= deadline
        view._boundary_sources = {}
        for source, versions in self._boundary_versions.items():
            valid = [value for value, clock, domain in versions if timely((clock, domain))]
            if valid:
                view._boundary_sources[source] = valid[-1]
        view._boundary_order = {number: [(time, point) for time, point in ordered
                                        if (number, point) in view._boundary_sources]
                                for number, ordered in self._boundary_order.items()}
        view._completion_records = {key: record for key, record in self._completion_records.items()
                                    if timely(record[2:4])}
        view._revoked = {key for key in self._revoked
                         if key not in self._revocation_clocks or timely(self._revocation_clocks[key])}
        return view

    def _evaluation_rows(self, driver_code, lap_number, evidence_deadline_s, clock_kind,
                         only_number=None, certified_coordinate=None):
        def within_deadline(row):
            if evidence_deadline_s is None:
                return True
            received = row.get("_ReceivedClock")
            return (row.get("_ClockKind") == clock_kind and received is not None
                    and received <= evidence_deadline_s)

        phase = (self if evidence_deadline_s is None else
                 self._phase_at_deadline(evidence_deadline_s, clock_kind))
        rows = [dict(row) for number, driver_rows in self._rows.items() if only_number is None or number == only_number for row in driver_rows
                if within_deadline(row)]
        # Certified ownership chooses the physical coordinate. Map only rows
        # carrying an independently proven bridge from their own reception.
        numbers = {driver_rows[-1]["Driver"]: number for number, driver_rows in self._rows.items() if driver_rows}
        target_number = numbers.get(driver_code)
        owned = {record[1].coordinate for key, record in phase._completion_records.items()
                 if key[1:3] == (target_number, lap_number) and phase.completion_bounds(key) is not None}
        chosen = certified_coordinate if certified_coordinate is not None else (next(iter(owned)) if len(owned) == 1 else None)
        if chosen is not None and any(row["Driver"] == driver_code and row.get("_AcquisitionCoordinate") is not None for row in rows):
            mapped = {}
            for row in rows:
                stamp = self._mapped_acquisition(row, chosen)
                if stamp is None:
                    if row.get("_AcquisitionCoordinate") is not None:
                        continue
                    # Preserve the existing coordinate-less producer contract;
                    # it does not establish a new cross-domain alias.
                else:
                    row["SessionTime"] = round(stamp, 6)
                    row["_AcquisitionCoordinate"] = chosen
                identity = (row["Driver"], row["SessionTime"], row.get("_AcquisitionCoordinate"))
                previous = mapped.get(identity)
                if previous is None or row.get("_IngestOrdinal", 0) < previous.get("_IngestOrdinal", 0):
                    mapped[identity] = row
            rows = sorted(mapped.values(), key=lambda row: row["SessionTime"])
        if evidence_deadline_s is not None:
            codes = {number: driver_rows[-1]["Driver"] for number, driver_rows in self._rows.items() if driver_rows}
            numbers_by_code = {code: number for number, code in codes.items()}
            for row in rows:
                number = numbers_by_code[row["Driver"]]
                row["LapNumber"] = phase.lap_at_sample(number, row["SessionTime"], row.get("_AcquisitionCoordinate"))
                key = phase.completed_lap_for_sample(number, row["SessionTime"], row.get("_AcquisitionCoordinate"))
                row["PhaseRouteValid"] = (row.get("_RouteValidAtReception", row["PhaseRouteValid"])
                                          or key is not None and row.get("_RouteLapAtReception") != row["LapNumber"])
        target_coordinates = {row.get("_AcquisitionCoordinate") for row in rows
                              if row["Driver"] == driver_code and row["LapNumber"] == lap_number
                              and row.get("_AcquisitionCoordinate") is not None}
        if len(target_coordinates) > 1:
            return [], phase, None
        coordinate = next(iter(target_coordinates), None)
        if coordinate is not None:
            rows = [row for row in rows if row.get("_AcquisitionCoordinate") in {None, coordinate}]
        if evidence_deadline_s is not None:
            # A later row may have taken ownership of the end marker.
            # Reattach independently certified anchors on the filtered
            # trace, rather than filtering away the anchor with that row.
            groups = {}
            for row in rows:
                row["LapStartTime"] = row["_StartPublication"] = None
                row["LapEndTime"] = row["_CompletionKey"] = None
                groups.setdefault((row["Driver"], row["LapNumber"]), []).append(row)
            numbers = {driver_rows[-1]["Driver"]: number
                       for number, driver_rows in self._rows.items() if driver_rows}
            for (code, lap), group in groups.items():
                if lap is None:
                    continue
                number = numbers[code]
                publication = phase._start_publication(number, lap, coordinate=coordinate)
                source_clock, source_kind = self._boundary_clocks.get(
                    (number, publication), (None, None))
                if (publication is not None and source_clock is not None
                        and source_kind == clock_kind and source_clock <= evidence_deadline_s
                        and (number, lap, publication) not in self._evicted_start_laps):
                    group[0]["LapStartTime"] = publication.seconds
                    group[0]["_StartPublication"] = publication
                for key, record in phase._completion_records.items():
                    if (key[1:3] != (number, lap) or phase.completion_bounds(key) is None
                            or (coordinate is not None and record[1].coordinate != coordinate)):
                        continue
                    _, end, received, kind = record
                    if received is not None and kind == clock_kind and received <= evidence_deadline_s:
                        group[-1]["LapEndTime"] = end.seconds
                        group[-1]["_CompletionKey"] = key
        self._apply_eligibility(rows, phase, evidence_deadline_s, clock_kind)
        return rows, phase, coordinate

    def finalize(self, driver_code: str, lap_number: int, *, admit_span: bool = True,
                 evidence_deadline_s: float | None = None,
                 clock_kind: str | None = None) -> PhysicalLapTraffic:
        start = perf_counter()
        try:
            def within_deadline(row):
                return (evidence_deadline_s is None or row.get("_ClockKind") == clock_kind
                        and row.get("_ReceivedClock") is not None and row["_ReceivedClock"] <= evidence_deadline_s)
            rows, phase, coordinate = self._evaluation_rows(driver_code, lap_number, evidence_deadline_s, clock_kind)
            target = sorted(
                (row for row in rows if row["Driver"] == driver_code
                 and row["LapNumber"] == lap_number),
                key=lambda row: row["SessionTime"],
            )
            if len(target) < 2:
                return PhysicalLapTraffic()
            historical_spans = self._spans_at_deadline(evidence_deadline_s, clock_kind)
            config = self._calibration_config(historical_spans)
            span = validated_complete_lap_span(pd.DataFrame.from_records(target), config=config)
            key = target[-1].get("_CompletionKey")
            if (admit_span and key is not None and key not in phase._revoked
                    and key not in self._admitted_completion_keys
                    and self._span_eligible(span, historical_spans, config)):
                clock = ((evidence_deadline_s, clock_kind) if evidence_deadline_s is not None
                         else getattr(self, "_ingest_clock", (None, None)))
                if clock[0] is None or clock[1] is None:
                    return PhysicalLapTraffic(target_samples=len(target))
                self._ensure_span_version_room()
                self._span_order += 1
                number = key[1]
                bounds = phase.completion_bounds(key)
                self._span_versions[key] = SpanContribution(self._span_order, driver_code, bounds, coordinate,
                    [SpanVersion(*clock, VALID, span)])
                self._admitted_completion_keys.add(key)
                if clock not in self.changed_calibration_clocks:
                    self.changed_calibration_clocks += (clock,)
                self.revision += 1
                self._project_spans()
                if key in self._revoked:
                    self._retire_span(key, self._revocation_clocks.get(key, clock), "ownership")
                self._enforce_span_capacity(clock)
                self._compact_span_versions()
                historical_spans = self._spans_at_deadline(evidence_deadline_s, clock_kind)
            spans = list(historical_spans.values())
            if not spans and span is not None and span >= 1000.0:
                spans = [span]
            if not spans:
                return PhysicalLapTraffic(target_samples=len(target))
            circuit_length = median(spans)
            states = [dict(state, Driver=driver_rows[-1]["Driver"])
                      for number, driver_rows in self._rows.items() if driver_rows
                      for state in self._candidate_states.get(number, ())
                      if within_deadline(state)
                      and (coordinate is None or state.get("_AcquisitionCoordinate") in {None, coordinate})]
            result = evaluate_physical_traffic(
                pd.DataFrame.from_records(rows),
                config=TrafficEvaluationConfig(
                    circuit_length_m=circuit_length, coverage_policy=self.policy,
                    require_phase_validity=True, evaluator_version=QUALIFYING_TRAFFIC_VERSION,
                ),
                target_lap=(driver_code, lap_number),
                candidate_states=pd.DataFrame.from_records(states),
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
