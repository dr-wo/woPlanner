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

from .models import (
    CutoffProjection,
    FinishedReason,
    QualiDriverState,
    QualiSessionSnapshot,
)


class QualiLiveService:
    def __init__(
        self,
        controller: LiveTimingController | None = None,
        *,
        eliminated_counts: dict[str, int] | None = None,
        out_lap_s: float = 100.0,
        cool_lap_s: float = 100.0,
        track_rate_s_per_field_push: float = 0.0,
        push_threshold_fraction: float = 0.02,
        pit_lane_geometry: PitLaneGeometry | None = None,
    ) -> None:
        self.controller = controller or LiveTimingController(
            store=QualiSessionStore(pit_lane_geometry=pit_lane_geometry)
        )
        self.eliminated_counts = {"Q1": 6, "Q2": 6, "Q3": 6}
        self.eliminated_counts.update(eliminated_counts or {})
        self.out_lap_s = float(out_lap_s)
        self.cool_lap_s = float(cool_lap_s)
        self.track_rate_s_per_field_push = float(track_rate_s_per_field_push)
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
            "Q1": [],
            "Q2": [],
            "Q3": [],
        }
        self._track_estimates: dict[str, tuple[float | None, int, str]] = {}
        self._latest_raw: dict[str, QualiRawDriverState] = {}
        self.snapshot = self._build_snapshot()

    def start_live(self, config: LiveConfig) -> None:
        self.reset()
        self._load_geometry(config.year, config.round_number, config.data_root)
        self.controller.start_live(config)

    def start_replay(self, config: ReplayConfig) -> None:
        self.reset()
        self.controller.start_replay(config)

    def _load_geometry(self, year: int, round_number: int, data_root) -> None:
        store = getattr(self.controller, "store", None)
        if (
            not isinstance(store, QualiSessionStore)
            or store.pit_lane_geometry is not None
        ):
            return
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
        self._load_geometry(year, round_number, data_root)

    def reset(self) -> None:
        self.raw_snapshot = QualiRawSnapshot.empty()
        self.session_part = "Q1"
        self._states.clear()
        self._completed_laps.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()
        self._actual_cutoffs.clear()
        self._part_push_laps = {"Q1": [], "Q2": [], "Q3": []}
        self._track_estimates.clear()
        self._latest_raw.clear()

    def poll(self):
        result = self.controller.drain()
        self.consume(result.snapshot)
        return self.snapshot, result.statuses, result.changed

    def consume(self, raw: QualiRawSnapshot) -> QualiSessionSnapshot:
        previous_part = self.session_part
        self.raw_snapshot = raw
        if raw.session_part != previous_part:
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
        self._completed_laps[raw.driver_number] = raw.completed_laps
        projected = self._project(raw)
        finished = existing.finished if existing else False
        reason = existing.finished_reason if existing else None
        remaining = self.raw_snapshot.remaining_time_s
        if remaining is not None and not finished:
            if raw.in_pit and remaining < self.out_lap_s:
                finished, reason = True, FinishedReason.IN_PIT_NO_OUTLAP_TIME
            elif completed_push and remaining <= 0:
                finished, reason = True, FinishedReason.FINAL_LAP_COMPLETED
            elif completed_push and remaining < self.cool_lap_s:
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
                    "FINISHED"
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
        eliminated_count = min(self.eliminated_counts[self.session_part], len(current))
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
                )
            elif state.active:
                self._states[number] = QualiDriverState(
                    state.driver_number,
                    state.driver,
                    state.team,
                    pit_state=state.pit_state,
                    pit_lane_progress=state.pit_lane_progress,
                )
        self.session_part = new_part
        self.track_source = f"{self._previous_part(new_part)} estimate"
        self._completed_laps.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()

    def _update_track_from_part(self, part: str) -> None:
        values = self._part_push_laps.get(part, [])
        try:
            from wostrategy.analysis.pre_quali import (
                estimate_push_sequence_track_progression,
            )

            rate = estimate_push_sequence_track_progression(values)
        except ValueError:
            self._track_estimates[part] = (None, len(values), "insufficient_support")
            return
        self.track_rate_s_per_field_push = rate
        self._track_estimates[part] = (rate, len(values), "estimated")

    def finalise(self) -> None:
        self._update_track_from_part("Q3")
        for number, state in tuple(self._states.items()):
            if state.active:
                self._states[number] = replace(
                    state,
                    finished=True,
                    finished_reason=FinishedReason.SESSION_FINALISED,
                    frozen_part="Q3",
                )
        self._actual_cutoffs.setdefault("Q3", None)
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
        cutoff_rank = max(count - self.eliminated_counts.get(self.session_part, 0), 0)
        cutoff_state = active[cutoff_rank - 1] if cutoff_rank else None
        unfinished = sum(not state.finished for state in active)
        correction = self.track_rate_s_per_field_push * unfinished
        current = cutoff_state.best_lap_s if cutoff_state else None
        cutoff = CutoffProjection(
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
        )

    @staticmethod
    def _previous_part(part: str) -> str:
        return {"Q2": "Q1", "Q3": "Q2"}.get(part, "FP")


def _checkpoint_elapsed(raw: QualiRawDriverState) -> dict[tuple[str, int], float]:
    checkpoints = {(item.kind, item.index): item.elapsed_s for item in raw.segments}
    elapsed = 0.0
    for index, sector in enumerate(raw.sectors_s, start=1):
        if sector is None:
            break
        elapsed += sector
        checkpoints[("sector", index)] = elapsed
    return checkpoints
