"""Pure qualifying run-plan and timeline calculations."""

from __future__ import annotations

from dataclasses import dataclass, field


from wodata.live.quali_clock import (
    STANDARD_QUALI_PARTS, SPRINT_QUALI_PARTS, DEFAULT_SESSION_DURATIONS_S,
)

@dataclass(frozen=True)
class QualiTimingValues:
    out_lap_s: float = 100.0
    in_lap_s: float = 100.0
    cool_lap_s: float | None = None
    push_lap_s: float = 90.0


@dataclass(frozen=True)
class QualiRunPlan:
    prep_required: bool = False
    number_of_push_laps: int = 1

    def __post_init__(self) -> None:
        if self.number_of_push_laps < 1:
            raise ValueError("number_of_push_laps must be at least one")


@dataclass(frozen=True)
class QualiArrangement:
    runs: tuple[QualiRunPlan, ...] = (QualiRunPlan(),)
    turnaround_s: float = 120.0
    margin_s: float = 30.0
    name: str = "Arrangement"

    def __post_init__(self) -> None:
        if not self.runs:
            raise ValueError("An arrangement must contain at least one run")
        if self.turnaround_s < 0 or self.margin_s < 0:
            raise ValueError("turnaround and margin must be non-negative")


@dataclass(frozen=True)
class TimelineSegment:
    label: str
    start_s: float
    end_s: float
    run_index: int | None = None


@dataclass(frozen=True)
class ArrangementTiming:
    duration_s: float
    final_push_start_offset_s: float
    latest_start_s: float
    feasible: bool
    segments: tuple[TimelineSegment, ...]


@dataclass(frozen=True)
class DoubleYellowReleaseTiming:
    """Countdown required for one replacement PUSH after a local yellow.

    This models a continuing qualifying clock.  A red flag is deliberately not
    represented because its paused duration is unknowable.
    """

    invalidated_attempt_s: float
    recovery_in_s: float
    turnaround_s: float
    replacement_out_s: float
    replacement_prep_s: float
    next_push_margin_s: float
    latest_safe_release_remaining_s: float
    sources: tuple[tuple[str, str], ...]


@dataclass
class QualiPlanModel:
    analysis_timings: QualiTimingValues = field(default_factory=QualiTimingValues)
    timings: QualiTimingValues = field(default_factory=QualiTimingValues)
    session_durations_s: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SESSION_DURATIONS_S)
    )
    arrangements: dict[str, list[QualiArrangement]] = field(
        default_factory=lambda: {
            part: [] for part in (*STANDARD_QUALI_PARTS, *SPRINT_QUALI_PARTS)
        }
    )

    def reset_timings(self) -> None:
        self.timings = self.analysis_timings

    def timing(self, part: str, arrangement: QualiArrangement) -> ArrangementTiming:
        return calculate_arrangement_timing(
            arrangement, self.timings, self.session_durations_s[part.upper()]
        )


def run_duration(run: QualiRunPlan, timings: QualiTimingValues) -> float:
    if timings.cool_lap_s is None:
        raise ValueError("Cool / Prep timing unavailable; manual override required")
    pushes = run.number_of_push_laps
    return (
        timings.out_lap_s
        + (timings.cool_lap_s if run.prep_required else 0.0)
        + pushes * timings.push_lap_s
        + (pushes - 1) * timings.cool_lap_s
        + timings.in_lap_s
    )


def final_push_start_in_run(run: QualiRunPlan, timings: QualiTimingValues) -> float:
    if timings.cool_lap_s is None:
        raise ValueError("Cool / Prep timing unavailable; manual override required")
    return (
        timings.out_lap_s
        + (timings.cool_lap_s if run.prep_required else 0.0)
        + (run.number_of_push_laps - 1) * (timings.push_lap_s + timings.cool_lap_s)
    )


def calculate_arrangement_timing(
    arrangement: QualiArrangement,
    timings: QualiTimingValues,
    session_duration_s: float,
) -> ArrangementTiming:
    prior = sum(run_duration(run, timings) for run in arrangement.runs[:-1])
    prior += arrangement.turnaround_s * max(len(arrangement.runs) - 1, 0)
    final_push_offset = prior + final_push_start_in_run(arrangement.runs[-1], timings)
    duration = sum(run_duration(run, timings) for run in arrangement.runs)
    duration += arrangement.turnaround_s * max(len(arrangement.runs) - 1, 0)
    latest_start = float(session_duration_s) - arrangement.margin_s - final_push_offset
    segments = _timeline_segments(arrangement, timings, latest_start)
    return ArrangementTiming(
        duration,
        final_push_offset,
        latest_start,
        latest_start >= 0,
        segments,
    )


def calculate_double_yellow_latest_safe_release(
    timings: QualiTimingValues,
    *,
    turnaround_s: float,
    margin_s: float,
    timing_sources: dict[str, str] | None = None,
    prep_required: bool = True,
) -> DoubleYellowReleaseTiming:
    """Calculate the release countdown for the stated double-yellow scenario.

    The invalidated first attempt consumes OUT, optional PREP, and PUSH.  Its
    return path is then IN, pit turnaround, OUT, optional PREP, and the margin
    needed to begin the replacement PUSH.  Each component occurs once.
    """
    if turnaround_s < 0 or margin_s < 0:
        raise ValueError("turnaround and margin must be non-negative")
    if timings.cool_lap_s is None and prep_required:
        raise ValueError("Cool / Prep timing unavailable; manual override required")
    prep = timings.cool_lap_s if prep_required else 0.0
    invalidated = timings.out_lap_s + prep + timings.push_lap_s
    components = (
        invalidated,
        timings.in_lap_s,
        float(turnaround_s),
        timings.out_lap_s,
        prep,
        float(margin_s),
    )
    sources = timing_sources or {}
    return DoubleYellowReleaseTiming(
        invalidated_attempt_s=invalidated,
        recovery_in_s=timings.in_lap_s,
        turnaround_s=float(turnaround_s),
        replacement_out_s=timings.out_lap_s,
        replacement_prep_s=prep,
        next_push_margin_s=float(margin_s),
        latest_safe_release_remaining_s=sum(components),
        sources=tuple((key, sources.get(key, "built-in fallback")) for key in (
            "out_lap_s", "in_lap_s", "cool_lap_s", "push_lap_s",
        )),
    )


def _timeline_segments(
    arrangement: QualiArrangement, timings: QualiTimingValues, start_s: float
) -> tuple[TimelineSegment, ...]:
    cursor = start_s
    output: list[TimelineSegment] = []
    for run_index, run in enumerate(arrangement.runs, start=1):
        output.append(TimelineSegment("PIT EXIT", cursor, cursor, run_index))
        cursor = _append(output, "OUT", cursor, timings.out_lap_s, run_index)
        if run.prep_required:
            cursor = _append(output, "PREP", cursor, timings.cool_lap_s, run_index)
        for push_index in range(run.number_of_push_laps):
            cursor = _append(output, "PUSH", cursor, timings.push_lap_s, run_index)
            if push_index < run.number_of_push_laps - 1:
                cursor = _append(output, "COOL", cursor, timings.cool_lap_s, run_index)
        cursor = _append(output, "IN", cursor, timings.in_lap_s, run_index)
        if run_index < len(arrangement.runs):
            cursor = _append(output, "TURNAROUND", cursor, arrangement.turnaround_s, None)
    return tuple(output)


def _append(
    output: list[TimelineSegment],
    label: str,
    start: float,
    duration: float,
    run_index: int | None,
) -> float:
    end = start + duration
    output.append(TimelineSegment(label, start, end, run_index))
    return end
