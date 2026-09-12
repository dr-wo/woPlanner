"""Live qualifying application service."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wodata.live import (
    LiveConfig,
    LiveTimingController,
    PitLaneGeometry,
    QualiPitState,
    QualiRawDriverState,
    QualiRawSnapshot,
    QualiSessionStore,
    ReplayConfig,
)

from ..common.session_info import parse_session_identity
from .models import (
    CutoffProjection,
    FinishedReason,
    QualiDriverState,
    QualiSessionSnapshot,
)
from .planning import SPRINT_QUALI_PARTS, STANDARD_QUALI_PARTS, QualiTimingValues


QUALI_PARTS = (*STANDARD_QUALI_PARTS, *SPRINT_QUALI_PARTS)


class QualiLiveService:
    def __init__(
        self,
        controller: LiveTimingController | None = None,
        *,
        eliminated_counts: dict[str, int] | None = None,
        out_lap_s: float = 100.0,
        cool_lap_s: float = QualiTimingValues().cool_lap_s,
        track_rate_s_per_field_push: float = 0.0,
        push_threshold_fraction: float = 0.02,
        pit_lane_geometry: PitLaneGeometry | None = None,
    ) -> None:
        self.controller = controller or LiveTimingController(
            store=QualiSessionStore(pit_lane_geometry=pit_lane_geometry)
        )
        self.eliminated_counts = {part: 6 for part in QUALI_PARTS}
        self.eliminated_counts.update(eliminated_counts or {})
        self.operational_timings = QualiTimingValues(
            out_lap_s=float(out_lap_s), cool_lap_s=float(cool_lap_s)
        )
        self.operational_timing_sources = {
            name: "unavailable" for name in vars(self.operational_timings)
        }
        self._fp_timing_values: dict[str, float] = {}
        self._fp_timing_sources: dict[str, str] = {}
        self._manual_timing_values: dict[str, float] = {}
        self._manual_timing_sources: dict[str, str] = {}
        if cool_lap_s != QualiTimingValues().cool_lap_s:
            self._manual_timing_values["cool_lap_s"] = float(cool_lap_s)
            self._manual_timing_sources["cool_lap_s"] = "manual override"
            self.operational_timing_sources["cool_lap_s"] = "manual override"
        self._operational_artifact_identity: tuple[int, int] | None = None
        self._message_driven = isinstance(self.controller, LiveTimingController)
        if self._message_driven:
            self.controller.on_message_snapshot = self._consume_message_snapshot
        self._fp_track_rate_s_per_field_push = float(track_rate_s_per_field_push)
        self._automatic_track_rate_s_per_field_push = (
            self._fp_track_rate_s_per_field_push
        )
        self._automatic_track_source = "FP prior"
        self._automatic_track_part: str | None = None
        self._track_override_s_per_field_push: float | None = None
        self.track_rate_s_per_field_push = self._automatic_track_rate_s_per_field_push
        self.push_threshold_fraction = float(push_threshold_fraction)
        self.track_source = "FP prior"
        self.raw_snapshot = QualiRawSnapshot.empty()
        self.session_part = "Q1"
        self._states: dict[str, QualiDriverState] = {}
        self._completed_laps: dict[str, int] = {}
        self._reference_total_s: float | None = None
        self._reference_checkpoints: dict[tuple[str, int], float] = {}
        self._actual_cutoffs: dict[str, float | None] = {}
        self._part_push_laps: dict[str, list[float]] = {
            part: [] for part in QUALI_PARTS
        }
        self._track_estimates: dict[str, tuple[float | None, int, str]] = {}
        self._latest_raw: dict[str, QualiRawDriverState] = {}
        self._event_geometry_identity = None
        self.received_session_info = self.raw_snapshot.session_info
        self.received_session_name = ""
        self.session_is_qualifying: bool | None = None
        self.snapshot = self._build_snapshot()

    def start_live(self, config: LiveConfig) -> None:
        self.reset()
        self._geometry_data_root = config.data_root
        self._load_geometry(config.year, config.round_number, config.data_root)
        self.controller.start_live(config)

    def start_replay(self, config: ReplayConfig) -> None:
        self.reset()
        self.controller.start_replay(config)

    def _load_geometry(
        self, year: int, round_number: int, data_root, *, force: bool = False
    ) -> None:
        store = getattr(self.controller, "store", None)
        if not isinstance(store, QualiSessionStore):
            return
        if store.pit_lane_geometry is not None and not force:
            return
        if force:
            store.pit_lane_geometry = None
        try:
            from wodata.artifacts import read_pit_lane_geometry

            geometry = read_pit_lane_geometry(
                year=year, round_number=round_number, data_root=data_root
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return
        store.pit_lane_geometry = PitLaneGeometry(
            tuple(tuple(point) for point in geometry["centreline"]),
            float(geometry.get("maximum_distance", 400.0)),
        )

    def configure_event_geometry(
        self, year: int, round_number: int, data_root: str | Path | None = None
    ) -> None:
        self._geometry_data_root = data_root
        self._load_geometry(year, round_number, data_root, force=True)

    def reset(self) -> None:
        self.raw_snapshot = QualiRawSnapshot.empty()
        self.session_part = "Q1"
        self._states.clear()
        self._completed_laps.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()
        self._actual_cutoffs.clear()
        self._part_push_laps = {part: [] for part in QUALI_PARTS}
        self._track_estimates.clear()
        self._automatic_track_rate_s_per_field_push = (
            self._fp_track_rate_s_per_field_push
        )
        self._automatic_track_source = "FP prior"
        self._automatic_track_part = None
        self._track_override_s_per_field_push = None
        self.track_rate_s_per_field_push = self._automatic_track_rate_s_per_field_push
        self.track_source = self._automatic_track_source
        self._latest_raw.clear()
        self._event_geometry_identity = None
        self.received_session_info = self.raw_snapshot.session_info
        self.received_session_name = ""
        self.session_is_qualifying = None
        self.snapshot = self._build_snapshot()

    def set_fp_track_rate(self, value: float) -> None:
        self._fp_track_rate_s_per_field_push = float(value)
        if any(estimate[0] is not None for estimate in self._track_estimates.values()):
            return
        self._automatic_track_rate_s_per_field_push = float(value)
        self._automatic_track_source = "FP prior"
        self._automatic_track_part = None
        if self._track_override_s_per_field_push is None:
            self.track_rate_s_per_field_push = float(value)
            self.track_source = self._automatic_track_source

    def set_track_override(self, value: float | None) -> None:
        self._track_override_s_per_field_push = (
            None if value is None else float(value)
        )
        if value is None:
            self.track_rate_s_per_field_push = (
                self._automatic_track_rate_s_per_field_push
            )
            self.track_source = self._automatic_track_source
        else:
            self.track_rate_s_per_field_push = float(value)
            self.track_source = "Manual override"

    @property
    def automatic_track_estimate(
        self,
    ) -> tuple[float | None, int, str] | None:
        if self._automatic_track_part is None:
            return None
        return self._track_estimates.get(self._automatic_track_part)

    @property
    def out_lap_s(self) -> float:
        return self.operational_timings.out_lap_s

    @property
    def cool_lap_s(self) -> float:
        return self.operational_timings.cool_lap_s

    def set_operational_timings(self, timings: QualiTimingValues, *, source: str) -> None:
        """Compatibility entry point for an explicit manual timing policy."""
        self.operational_timings = timings
        self._manual_timing_values["cool_lap_s"] = timings.cool_lap_s
        self._manual_timing_sources["cool_lap_s"] = source
        self.operational_timing_sources["cool_lap_s"] = source

    def set_manual_operational_timing(self, category: str, value: float) -> None:
        if category not in vars(self.operational_timings):
            raise ValueError(f"Unknown operational timing category: {category}")
        value = float(value)
        self._manual_timing_values[category] = value
        self._manual_timing_sources[category] = "manual override"
        self.operational_timings = replace(self.operational_timings, **{category: value})
        self.operational_timing_sources[category] = "manual override"

    def clear_manual_operational_timings(self) -> None:
        self._manual_timing_values.clear()
        self._manual_timing_sources.clear()
        self._apply_operational_sources()

    def load_operational_analysis(self, result) -> None:
        self._fp_timing_values.clear()
        self._fp_timing_sources.clear()
        for _, row in result.durations.iterrows():
            value = row["selected_default_s"]
            category = str(row["category"])
            if value == value and category in vars(self.operational_timings):
                self._fp_timing_values[category] = float(value)
                self._fp_timing_sources[category] = (
                    f"FP-derived (n={int(row['selected_count'])})"
                )
        self._apply_operational_sources()

    def _apply_operational_sources(self) -> None:
        values = vars(QualiTimingValues()).copy()
        sources = {name: "unavailable" for name in values}
        for category, value in self._fp_timing_values.items():
            values[category] = value
            sources[category] = self._fp_timing_sources[category]
        for category, value in self._manual_timing_values.items():
            values[category] = value
            sources[category] = self._manual_timing_sources[category]
        self.operational_timings = QualiTimingValues(**values)
        self.operational_timing_sources = sources

    def _effective_cool_timing(self, current_fastest_s: float | None) -> tuple[float | None, str]:
        if "cool_lap_s" in self._manual_timing_values:
            return (
                self._manual_timing_values["cool_lap_s"],
                self._manual_timing_sources["cool_lap_s"],
            )
        if "cool_lap_s" in self._fp_timing_values:
            return (
                self._fp_timing_values["cool_lap_s"],
                self._fp_timing_sources["cool_lap_s"],
            )
        if current_fastest_s is None:
            return None, "unavailable"
        return current_fastest_s * 1.30, "fastest × 1.30"

    def _load_operational_artifact(self, year: int, round_number: int) -> None:
        identity = (year, round_number)
        if identity == self._operational_artifact_identity:
            return
        self._operational_artifact_identity = identity
        try:
            from wodata.artifacts import read_pre_quali_analysis

            _, durations, _ = read_pre_quali_analysis(
                year=year,
                round_number=round_number,
                data_root=getattr(self, "_geometry_data_root", None),
            )
        except (FileNotFoundError, ValueError, OSError):
            self._fp_timing_values.clear()
            self._fp_timing_sources.clear()
            self._apply_operational_sources()
            return
        self.load_operational_analysis(type("Analysis", (), {"durations": durations})())

    def _consume_message_snapshot(self, raw):
        self.received_session_info = raw.session_info
        identity = parse_session_identity(raw.session_info)
        self.received_session_name = identity.session_name
        event_key = (identity.year, identity.round_number)
        if all(event_key) and event_key != self._event_geometry_identity:
            self._event_geometry_identity = event_key
            self._load_geometry(*event_key, getattr(self, "_geometry_data_root", None), force=True)
            self._load_operational_artifact(*event_key)
        if identity.is_qualifying is False:
            if self.session_is_qualifying is not False:
                self.reset()
                self.received_session_info = raw.session_info
                self.received_session_name = identity.session_name
            self.session_is_qualifying = False
            return
        if identity.is_qualifying is None:
            return
        self.session_is_qualifying = True
        self.consume(raw)

    def poll(self):
        result = self.controller.drain()
        # Real controllers deliver every intermediate message state, not just
        # the final state of a drain. Snapshot-only test adapters remain usable.
        if not self._message_driven:
            self._consume_message_snapshot(result.snapshot)
        return self.snapshot, result.statuses, result.changed and self.session_is_qualifying is True

    def consume(self, raw: QualiRawSnapshot) -> QualiSessionSnapshot:
        previous_part = self.session_part
        self.raw_snapshot = raw
        if raw.session_part != previous_part:
            if not self._states:
                self.session_part = raw.session_part
            else:
                self.transition_to(raw.session_part)
        for driver in raw.drivers:
            self._consume_driver(driver)
        for number, driver in self._latest_raw.items():
            state = self._states.get(number)
            if state is not None and state.active:
                projected = self._project(driver)
                self._states[number] = replace(state, projected_lap_s=projected)
        self._rank_and_project()
        self.snapshot = self._build_snapshot()
        return self.snapshot

    def _consume_driver(self, raw: QualiRawDriverState) -> None:
        existing = self._states.get(raw.driver_number)
        if existing is not None and not existing.active:
            self._states[raw.driver_number] = replace(existing, historical_results=raw.historical_results)
            return
        previous_laps = self._completed_laps.get(raw.driver_number)
        completed_lap = (
            previous_laps is not None
            and raw.completed_laps > previous_laps
            and raw.last_lap_s is not None
        )
        fastest_before = min(
            (
                state.best_lap_s
                for state in self._states.values()
                if state.active and state.best_lap_s is not None
            ),
            default=raw.best_lap_s,
        )
        completed_push = (
            completed_lap
            and fastest_before is not None
            and raw.last_lap_s <= fastest_before * (1.0 + self.push_threshold_fraction)
        )
        self._latest_raw[raw.driver_number] = raw
        if completed_push:
            self._part_push_laps[self.session_part].append(float(raw.last_lap_s))
            self._update_track_from_part(self.session_part, live=True)
        self._completed_laps[raw.driver_number] = raw.completed_laps
        projected = self._project(raw)
        finished = existing.finished if existing else False
        reason = existing.finished_reason if existing else None
        remaining = self.raw_snapshot.remaining_time_s
        effective = existing.effective_finished if existing else False
        effective_remaining = existing.effective_finish_remaining_s if existing else None
        recovery = existing.effective_finish_recovery_s if existing else None
        timing_source = existing.effective_finish_timing_source if existing else None
        current_fastest = min(
            (value for value in (
                raw.best_lap_s,
                raw.last_lap_s if completed_push else None,
                *(state.best_lap_s for state in self._states.values() if state.active),
            ) if value is not None),
            default=None,
        )
        cool_requirement, cool_source = self._effective_cool_timing(current_fastest)
        if (not effective and completed_push and remaining is not None
                and cool_requirement is not None and remaining < cool_requirement):
            effective = True
            effective_remaining, recovery = remaining, cool_requirement
            timing_source = cool_source
        if remaining is not None and not finished:
            if completed_lap and (remaining <= 0 or self.raw_snapshot.normal_finished):
                finished, reason = True, FinishedReason.FINAL_LAP_COMPLETED
            elif raw.in_pit and remaining < self.out_lap_s:
                finished, reason = True, FinishedReason.IN_PIT_NO_OUTLAP_TIME
            elif completed_push and cool_requirement is not None and remaining < cool_requirement:
                finished, reason = True, FinishedReason.NO_TIME_FOR_NEXT_PUSH
        best = raw.best_lap_s
        if existing and existing.best_lap_s is not None:
            best = (
                existing.best_lap_s
                if best is None
                else min(best, existing.best_lap_s)
            )
        state = QualiDriverState(
            raw.driver_number,
            raw.driver_code,
            raw.team,
            sectors_s=raw.sectors_s,
            last_lap_s=raw.last_lap_s,
            best_lap_s=best,
            projected_lap_s=projected,
            finished=finished,
            finished_reason=reason,
            pit_state=raw.pit_state,
            pit_lane_progress=raw.pit_lane_progress,
            tyre_history=raw.tyre_history,
            historical_results=raw.historical_results,
            effective_finished=effective,
            effective_finish_remaining_s=effective_remaining,
            effective_finish_recovery_s=recovery,
            effective_finish_timing_source=timing_source,
        )
        self._states[raw.driver_number] = state
        if best is not None and (
            self._reference_total_s is None or best < self._reference_total_s
        ):
            self._reference_total_s = best
            checkpoints = _checkpoint_elapsed(raw)
            if checkpoints:
                self._reference_checkpoints = checkpoints

    def _project(self, raw: QualiRawDriverState) -> float | None:
        if self._reference_total_s is None:
            return None
        checkpoints = _checkpoint_elapsed(raw)
        common = [key for key in checkpoints if key in self._reference_checkpoints]
        if not common:
            return raw.last_lap_s
        priority = lambda key: (key[0] == "segment", key[1])
        key = max(common, key=priority)
        reference_elapsed = self._reference_checkpoints[key]
        if reference_elapsed <= 0:
            return None
        return checkpoints[key] * self._reference_total_s / reference_elapsed

    def _rank_and_project(self) -> None:
        active = [state for state in self._states.values() if state.active]
        ranked = sorted(
            active,
            key=lambda state: (
                state.best_lap_s is None,
                state.best_lap_s or float("inf"),
                state.driver,
            ),
        )
        leader = next(
            (
                state.best_lap_s
                for state in ranked
                if state.best_lap_s is not None
            ),
            None,
        )
        previous = None
        for rank, state in enumerate(ranked, start=1):
            gap = (
                state.best_lap_s - leader
                if state.best_lap_s is not None and leader is not None
                else None
            )
            interval = (
                state.best_lap_s - previous
                if state.best_lap_s is not None and previous is not None
                else None
            )
            projected_gap = (
                state.projected_lap_s - leader
                if state.projected_lap_s is not None and leader is not None
                else None
            )
            self._states[state.driver_number] = replace(
                state,
                rank=rank,
                gap_s=gap,
                interval_s=interval,
                projected_gap_s=projected_gap,
                status=(
                    "EFF. FINISH" if state.effective_finished else "FINISHED"
                    if state.finished
                    else state.pit_state.value.replace("_", " ")
                ),
            )
            if state.best_lap_s is not None:
                previous = state.best_lap_s

    def transition_to(self, new_part: str) -> None:
        new_part = new_part.upper()
        if new_part == self.session_part:
            return
        self._update_track_from_part(self.session_part)
        current = [state for state in self._states.values() if state.active]
        current.sort(
            key=lambda state: (
                state.best_lap_s is None,
                state.best_lap_s or float("inf"),
            )
        )
        eliminated_count = min(
            self.eliminated_counts.get(self.session_part, 0), len(current)
        )
        advancing_count = len(current) - eliminated_count
        cutoff = (
            current[advancing_count - 1].best_lap_s
            if advancing_count > 0
            else None
        )
        self._actual_cutoffs[self.session_part] = cutoff
        eliminated_numbers = {state.driver_number for state in current[advancing_count:]}
        for number, state in tuple(self._states.items()):
            if number in eliminated_numbers:
                self._states[number] = replace(
                    state,
                    active=False,
                    finished=True,
                    finished_reason=FinishedReason.ELIMINATED,
                    frozen_part=self.session_part,
                    best_lap_s=None, last_lap_s=None,
                    sectors_s=(None, None, None), projected_lap_s=None,
                    gap_s=None, interval_s=None, projected_gap_s=None,
                    status="ELIMINATED",
                    effective_finished=False, effective_finish_remaining_s=None,
                    effective_finish_recovery_s=None, effective_finish_timing_source=None,
                )
            elif state.active:
                self._states[number] = QualiDriverState(
                    state.driver_number,
                    state.driver,
                    state.team,
                    pit_state=state.pit_state,
                    pit_lane_progress=state.pit_lane_progress,
                    tyre_history=state.tyre_history,
                )
        self.session_part = new_part
        self._completed_laps.clear()
        self._latest_raw.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()
        self.snapshot = self._build_snapshot()

    def _update_track_from_part(self, part: str, *, live: bool = False) -> None:
        values = self._part_push_laps.get(part, [])
        try:
            from wostrategy.analysis.live_qualifying import (
                calculate_live_qualifying_track_evolution,
            )

            estimate = calculate_live_qualifying_track_evolution(values)
            rate = estimate.rate_s_per_field_push
        except ValueError:
            self._track_estimates[part] = (None, len(values), "insufficient_support")
            return
        self._track_estimates[part] = (rate, len(values), "estimated")
        self._automatic_track_rate_s_per_field_push = rate
        self._automatic_track_source = (
            f"{part} live estimate" if live else f"{part} estimate"
        )
        self._automatic_track_part = part
        if self._track_override_s_per_field_push is None:
            self.track_rate_s_per_field_push = rate
            self.track_source = self._automatic_track_source

    def finalise(self) -> None:
        final_part = "SQ3" if self.session_part.startswith("SQ") else "Q3"
        self._update_track_from_part(final_part)
        for number, state in tuple(self._states.items()):
            if state.active:
                self._states[number] = replace(
                    state,
                    finished=True,
                    finished_reason=FinishedReason.SESSION_FINALISED,
                    frozen_part=final_part,
                )
        self._actual_cutoffs.setdefault(final_part, None)
        self.snapshot = self._build_snapshot()

    def pit_status_groups(self) -> dict[str, tuple[str, ...]]:
        fired = sorted(
            state.driver
            for state in self._states.values()
            if state.pit_state == QualiPitState.FIRED_UP
        )
        queue = sorted(
            (
                state
                for state in self._states.values()
                if state.pit_state == QualiPitState.QUEUE
            ),
            key=lambda state: -(
                state.pit_lane_progress
                if state.pit_lane_progress is not None
                else -1
            ),
        )
        on_track = sorted(
            state.driver
            for state in self._states.values()
            if state.pit_state == QualiPitState.ON_TRACK
        )
        return {
            "Fired-up": tuple(fired),
            "Queue": tuple(state.driver for state in queue),
            "On-track": tuple(on_track),
        }

    @property
    def recording_path(self) -> Path | None:
        return self.controller.recording_path

    def stop(self) -> None:
        self.controller.stop()

    def _build_snapshot(self) -> QualiSessionSnapshot:
        active = sorted(
            (state for state in self._states.values() if state.active),
            key=lambda state: state.rank or 999,
        )
        count = len(active)
        p1_target = self.session_part == "Q3"
        cutoff_rank = 1 if p1_target and count else max(
            count - self.eliminated_counts.get(self.session_part, 0), 0
        )
        cutoff_state = active[cutoff_rank - 1] if cutoff_rank else None
        unfinished = sum(not state.finished for state in active)
        correction = -self.track_rate_s_per_field_push * unfinished
        current = cutoff_state.best_lap_s if cutoff_state else None
        cutoff = CutoffProjection(
            "P1" if p1_target else "CUTOFF",
            cutoff_rank,
            cutoff_state.driver if cutoff_state else None,
            current,
            unfinished,
            self.track_rate_s_per_field_push,
            correction,
            current + correction if current is not None else None,
        )
        rows = sorted(
            self._states.values(),
            key=lambda state: (
                state.active is False,
                state.rank is None,
                state.rank or 999,
                state.driver,
            ),
        )
        return QualiSessionSnapshot(
            self.session_part,
            self.raw_snapshot.remaining_time_s,
            self.raw_snapshot.session_status,
            tuple(rows),
            cutoff,
            tuple(self._actual_cutoffs.items()),
            self.track_source,
            self.raw_snapshot.normal_finished,
            self.raw_snapshot.normal_finish_source,
            clock_running=self.raw_snapshot.clock_running,
            clock_anchor_session_time_s=self.raw_snapshot.clock_anchor_session_time_s,
        )

    @staticmethod
    def _previous_part(part: str) -> str:
        return {
            "Q2": "Q1",
            "Q3": "Q2",
            "SQ2": "SQ1",
            "SQ3": "SQ2",
        }.get(part, "FP")


def _checkpoint_elapsed(raw: QualiRawDriverState) -> dict[tuple[str, int], float]:
    checkpoints = {(item.kind, item.index): item.elapsed_s for item in raw.segments}
    elapsed = 0.0
    for index, sector in enumerate(raw.sectors_s, start=1):
        if sector is None:
            break
        elapsed += sector
        checkpoints[("sector", index)] = elapsed
    return checkpoints
