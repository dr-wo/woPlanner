import pytest

from woplanner.quali.planning import (
    DEFAULT_SESSION_DURATIONS_S,
    QualiArrangement, QualiPlanModel, QualiRunPlan, QualiTimingValues,
    calculate_arrangement_timing, calculate_double_yellow_latest_safe_release,
    run_duration,
)


T = QualiTimingValues(100, 100, 110, 90)


def test_one_run_prep_and_multiple_push_formula():
    assert run_duration(QualiRunPlan(False, 1), T) == 290
    assert run_duration(QualiRunPlan(True, 2), T) == 600


def test_turnaround_only_between_runs_and_margin_uses_final_push_start():
    arrangement = QualiArrangement((QualiRunPlan(False, 1), QualiRunPlan(True, 2)), 120, 30)
    result = calculate_arrangement_timing(arrangement, T, 1080)
    assert result.duration_s == 1010  # 290 + 120 + 600
    assert result.final_push_start_offset_s == 820  # first run + turnaround + out/prep/push/cool
    assert result.latest_start_s == 230
    # Final push completion is intentionally after the margin point and may pass session zero.
    assert result.latest_start_s + result.final_push_start_offset_s == 1050


def test_negative_latest_start_is_not_clamped():
    result = calculate_arrangement_timing(
        QualiArrangement((QualiRunPlan(True, 5),), margin_s=30), T, 600
    )
    assert result.latest_start_s < 0
    assert not result.feasible
    assert result.segments[0].start_s == result.latest_start_s


def test_multiple_independent_arrangements_and_manual_reset():
    model = QualiPlanModel(analysis_timings=T, timings=QualiTimingValues())
    model.arrangements["Q1"].extend([
        QualiArrangement((QualiRunPlan(False, 1),)),
        QualiArrangement((QualiRunPlan(True, 2),)),
    ])
    assert len(model.arrangements["Q1"]) == 2
    model.reset_timings()
    assert model.timings == T


def test_invalid_run_and_empty_arrangement_rejected():
    with pytest.raises(ValueError): QualiRunPlan(number_of_push_laps=0)
    with pytest.raises(ValueError): QualiArrangement(())


def test_sprint_qualifying_default_session_durations():
    model = QualiPlanModel()

    assert DEFAULT_SESSION_DURATIONS_S["SQ1"] == 12 * 60
    assert DEFAULT_SESSION_DURATIONS_S["SQ2"] == 10 * 60
    assert DEFAULT_SESSION_DURATIONS_S["SQ3"] == 8 * 60
    assert model.session_durations_s["SQ1"] == 12 * 60
    assert set(model.arrangements).issuperset({"SQ1", "SQ2", "SQ3"})


def test_double_yellow_latest_safe_release_lists_each_non_overlapping_component():
    result = calculate_double_yellow_latest_safe_release(
        T, turnaround_s=120, margin_s=30,
        timing_sources={"cool_lap_s": "FP-derived (n=4)"},
    )
    assert result.invalidated_attempt_s == 300  # OUT + PREP + invalidated PUSH
    assert result.latest_safe_release_remaining_s == 760
    assert dict(result.sources)["cool_lap_s"] == "FP-derived (n=4)"
