from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from wodata.live import load_recording
from wodata.live.models import LiveTimingState
from woplanner.live.presentation import guard_provisional_order


def current_rows():
    return pd.DataFrame([
        {"driver": "AAA", "segment_index": 4, "source_time_s": 120.0,
         "cumulative_time_s": 125.0, "reference_cumulative_time_s": 120.0,
         "delta_s": 5.0, "timing_state": "provisional", "timing_available": True},
        {"driver": "BBB", "segment_index": 4, "source_time_s": 120.5,
         "cumulative_time_s": 122.0, "reference_cumulative_time_s": 120.0,
         "delta_s": 2.0, "timing_state": "provisional", "timing_available": True},
    ])


def fresh_states():
    return (
        LiveTimingState("1", "AAA", gap_to_leader_s=1.0,
                        gap_source_time_s=122.0, latest_source_time_s=123.0),
        LiveTimingState("2", "BBB", gap_to_leader_s=2.0,
                        gap_source_time_s=122.5, latest_source_time_s=123.0),
    )


def test_fresher_official_order_suppresses_conflicting_provisional_points_only():
    rows = current_rows()
    rows.loc[0, "timing_state"] = "committed"
    original = rows.copy(deep=True)
    result = guard_provisional_order(rows, fresh_states())
    assert result.loc[0, "delta_s"] == 5.0
    assert result.loc[0, "timing_available"]
    assert pd.isna(result.loc[1, "delta_s"])
    assert not result.loc[1, "timing_available"]
    assert result.loc[1, "timing_state"] == "provisional"
    assert result.loc[1, "display_reason"] == "fresh_official_order_conflict"
    pd.testing.assert_frame_equal(rows, original)


@pytest.mark.parametrize("change", [
    {"latest_source_time_s": 130.0},
    {"gap_source_time_s": 119.0},
    {"gap_source_time_s": 125.0, "latest_source_time_s": 125.0},
    {"gap_source_time_s": None},
    {"gap_to_leader_s": None},
    {"gap_to_leader_s": float("nan")},
])
def test_stale_unordered_or_unavailable_official_gap_cannot_override_display(change):
    left, right = fresh_states()
    result = guard_provisional_order(current_rows(), (replace(left, **change), right))
    assert result["delta_s"].tolist() == [5.0, 2.0]
    assert result["timing_available"].all()


def test_order_guard_does_not_compare_different_sector_coordinates_or_old_points():
    rows = current_rows()
    newer = rows.iloc[[0]].copy()
    newer["segment_index"] = 5
    newer["source_time_s"] = 123.0
    rows = pd.concat([rows, newer], ignore_index=True)
    result = guard_provisional_order(rows, fresh_states())
    assert result["delta_s"].tolist() == [5.0, 2.0, 5.0]


def test_current_progress_can_use_a_provisional_reference_before_pace_is_available():
    rows = current_rows()
    rows["cumulative_time_s"] = [121.0, 122.0]
    rows["delta_s"] = float("nan")
    rows["reference_cumulative_time_s"] = float("nan")
    rows["timing_available"] = False
    result = guard_provisional_order(rows, fresh_states(), reference_driver="AAA")
    assert result["delta_s"].tolist() == [0.0, 1.0]
    assert result["timing_available"].all()
    assert result["display_reason"].eq("provisional_reference").all()
    assert result["reference_cumulative_time_s"].isna().all()
    assert result["cumulative_time_s"].tolist() == [121.0, 122.0]


@pytest.mark.parametrize("reference", [None, float("nan")])
def test_missing_provisional_reference_does_not_create_finite_timing(reference):
    rows = current_rows()
    rows.loc[0, "cumulative_time_s"] = reference
    rows["delta_s"] = float("nan")
    rows["timing_available"] = False
    result = guard_provisional_order(
        rows, fresh_states(), reference_driver="AAA"
    )
    assert result["delta_s"].isna().all()
    assert not result["timing_available"].any()


def test_provisional_reference_does_not_create_a_missing_cumulative_origin():
    rows = current_rows()
    rows["delta_s"] = float("nan")
    rows["cumulative_time_s"] = float("nan")
    rows["timing_available"] = False
    result = guard_provisional_order(rows, fresh_states(), reference_driver="AAA")
    assert result["delta_s"].isna().all()
    assert not result["timing_available"].any()


@pytest.mark.parametrize("cut,ahead,behind", [(3314, "16", "1"), (3334, "6", "3")])
def test_r15_fresh_official_pairs_reject_the_original_raw_s1_order(cut, ahead, behind):
    fixture = Path(__file__).parents[2] / "woData/tests/fixtures/r15_first_laps.jsonl"
    observations, gaps = {}, {}
    for recorded in load_recording(fixture):
        message = recorded.message
        if message.sequence > cut:
            break
        if message.topic != "TimingData":
            continue
        for driver, patch in message.payload["Lines"].items():
            if driver not in {ahead, behind}:
                continue
            stamp = message.source_timestamp_utc.timestamp()
            value = patch.get("Sectors", {}).get("0", {}).get("Value")
            if value:
                observations[driver] = float(value), stamp
            if "GapToLeader" in patch:
                gaps[driver] = float(patch["GapToLeader"]), stamp
    assert observations[ahead][0] > observations[behind][0]
    assert gaps[ahead][0] < gaps[behind][0]
    now = max(item[1] for item in gaps.values())
    rows = pd.DataFrame([
        {"driver": driver, "segment_index": 4, "source_time_s": stamp,
         "cumulative_time_s": duration, "delta_s": duration,
         "timing_state": "provisional", "timing_available": True}
        for driver, (duration, stamp) in observations.items()
    ])
    states = tuple(
        LiveTimingState(driver, driver, gap_to_leader_s=gap,
                        gap_source_time_s=stamp, latest_source_time_s=now)
        for driver, (gap, stamp) in gaps.items()
    )
    result = guard_provisional_order(rows, states)
    assert result["delta_s"].isna().all()
    assert not result["timing_available"].any()
    assert result["display_reason"].eq("fresh_official_order_conflict").all()


@pytest.mark.parametrize("cut,ahead,behind", [(3314, "LEC", "NOR"), (3334, "HAD", "VER")])
def test_r15_early_live_plot_progresses_with_correct_provisional_pair_order(cut, ahead, behind):
    from wodata.live import LiveSessionStore
    from woplanner.live.service import LivePlannerService

    fixture = Path(__file__).parents[2] / "woData/tests/fixtures/r15_first_laps.jsonl"
    store = LiveSessionStore()
    for recorded in load_recording(fixture):
        if recorded.message.sequence > cut:
            break
        store.reduce(recorded.message)
    service = LivePlannerService()
    service.snapshot = store.snapshot()
    try:
        plot = service.build_plot_data("RUS", include_provisional=True)
        pair = plot.loc[plot["driver"].isin([ahead, behind])].set_index("driver")
        assert pair["timing_state"].eq("provisional").all()
        assert pair["timing_available"].all()
        assert pair["display_reason"].eq("provisional_reference").all()
        assert pair.loc[ahead, "delta_s"] < pair.loc[behind, "delta_s"]
        assert pair["reference_cumulative_time_s"].isna().all()
    finally:
        service.shutdown()
