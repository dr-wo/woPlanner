"""Message-driven operational lap-state classification for qualifying."""

from __future__ import annotations

from dataclasses import dataclass

from wodata.live import QualiPitState, QualiRawDriverState


LAP_STATES = (
    "UNKNOWN", "NOT ACTIVE", "FIRE-UP", "IN-PIT", "PIT-LANE",
    "QUEUE", "OUT", "SLOW", "FAST", "IN",
)


@dataclass(frozen=True)
class LapPaceReference:
    start_to_5pct_s: float | None = None
    start_to_s1_s: float | None = None
    s1_to_s2_s: float | None = None
    source: str = "unavailable"


def classify_lap_state(
    raw: QualiRawDriverState,
    *,
    previous_state: str | None,
    new_lap: bool,
    reference: LapPaceReference | None,
    slow_threshold_fraction: float = 0.07,
) -> str:
    """Classify only from the current reducer snapshot and FP reference.

    Physical pit evidence wins.  A new lap alternates the FAST/SLOW prior, then
    the 5%-distance and S1 checks may correct it; S2 can only degrade FAST.
    """
    physical = _physical_state(raw)
    if physical is not None:
        return physical
    if raw.out_lap_confirmed:
        return "OUT"
    if not raw.timed_lap_eligible:
        return "UNKNOWN"
    state = previous_state if previous_state in {"FAST", "SLOW"} else "UNKNOWN"
    if new_lap:
        state = "FAST" if previous_state == "SLOW" else "SLOW" if previous_state == "FAST" else "UNKNOWN"
    if reference is None:
        return state
    first_5 = next(
        (point.elapsed_s for point in raw.segments
         if point.kind == "distance" and point.index == 5),
        None,
    )
    if first_5 is not None and reference.start_to_5pct_s is not None:
        state = _pace_state(first_5, reference.start_to_5pct_s, state, allow_recovery=True,
                            threshold=slow_threshold_fraction)
    s1 = raw.sectors_s[0]
    if s1 is not None and reference.start_to_s1_s is not None:
        state = _pace_state(s1, reference.start_to_s1_s, state, allow_recovery=True,
                            threshold=slow_threshold_fraction)
    s2 = raw.sectors_s[1]
    if s2 is not None and reference.s1_to_s2_s is not None:
        state = _pace_state(s2, reference.s1_to_s2_s, state, allow_recovery=False,
                            threshold=slow_threshold_fraction)
    return state


def _physical_state(raw: QualiRawDriverState) -> str | None:
    state = raw.structural_state if raw.structural_state != QualiPitState.UNKNOWN else raw.pit_state
    if state == QualiPitState.IN_PIT:
        return "IN-PIT"
    if state in {QualiPitState.NOT_ACTIVE, QualiPitState.INACTIVE}:
        return "NOT ACTIVE"
    if state in {QualiPitState.FIRE_UP, QualiPitState.FIRED_UP}:
        return "FIRE-UP"
    if state == QualiPitState.PIT_LANE:
        return "PIT-LANE"
    if state == QualiPitState.QUEUE:
        return "QUEUE"
    if state == QualiPitState.IN_LAP:
        return "IN"
    return None


def _pace_state(
    observed: float, reference: float, current: str, *, allow_recovery: bool,
    threshold: float,
) -> str:
    if observed > reference * (1.0 + threshold):
        return "SLOW"
    return "FAST" if allow_recovery else current
