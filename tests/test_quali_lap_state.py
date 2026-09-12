from wodata.live import QualiCheckpoint, QualiPitState, QualiRawDriverState

from woplanner.quali.lap_state import LapPaceReference, classify_lap_state


REF = LapPaceReference(5.0, 30.0, 31.0, "FP fastest valid lap")


def raw(*, laps=1, sectors=(None, None, None), checkpoints=(), pit=QualiPitState.ON_TRACK, eligible=True, out=False):
    return QualiRawDriverState(
        "1", "AAA", "Team", completed_laps=laps, sectors_s=sectors,
        segments=checkpoints, pit_state=pit, timed_lap_eligible=eligible,
        out_lap_confirmed=out,
    )


def test_alternation_and_5_percent_correction():
    assert classify_lap_state(raw(), previous_state="SLOW", new_lap=True, reference=REF) == "FAST"
    assert classify_lap_state(
        raw(checkpoints=(QualiCheckpoint("distance", 5, 5.6),)),
        previous_state="FAST", new_lap=False, reference=REF,
    ) == "SLOW"


def test_seven_percent_threshold_is_slow_evidence():
    assert classify_lap_state(
        raw(sectors=(32.11, None, None)), previous_state="FAST", new_lap=False, reference=REF,
    ) == "SLOW"


def test_s1_can_recover_but_s2_cannot_rescue_a_slow_lap():
    assert classify_lap_state(
        raw(sectors=(29.9, None, None)), previous_state="SLOW", new_lap=False, reference=REF,
    ) == "FAST"
    assert classify_lap_state(
        raw(sectors=(34.0, 29.0, None)), previous_state="FAST", new_lap=False, reference=REF,
    ) == "SLOW"


def test_pit_queue_and_inlap_take_precedence_over_pace_state():
    assert classify_lap_state(raw(pit=QualiPitState.QUEUE), previous_state="FAST", new_lap=False, reference=REF) == "QUEUE"
    assert classify_lap_state(raw(pit=QualiPitState.IN_LAP), previous_state="FAST", new_lap=False, reference=REF) == "IN"
    assert classify_lap_state(raw(laps=0, out=True, eligible=False), previous_state="FAST", new_lap=False, reference=REF) == "OUT"
