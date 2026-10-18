"""Independent-review reproductions through the real qualifying reducer."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from wodata.live import LiveTimingMessage, QualiSessionStore
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic, QUALIFYING_TRAFFIC_POLICY
from wostrategy.analysis.traffic import TrafficEvaluationConfig, evaluate_physical_traffic


EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def apply(store, adapter, topic, payload, stamp):
    raw = store.apply(LiveTimingMessage(
        topic, payload, EPOCH + timedelta(seconds=float(stamp)),
        EPOCH + timedelta(seconds=500), session_time_seconds=float(stamp),
        playback_time_seconds=1000 + float(stamp),
    ))
    adapter.ingest(raw)
    return raw


def timing(store, adapter, number, patch, stamp):
    return apply(store, adapter, "TimingData", {"Lines": {number: patch}}, stamp)


def real_field(*, delay=0, unfinished_b=False, b_speed=360., adapter_type=RollingQualiPhysicalTraffic,
               starts=None):
    store, adapter = QualiSessionStore(), adapter_type()
    apply(store, adapter, "DriverList", {c: {"Tla": c} for c in "ABC"}, 90 - delay)
    starts = starts or {"A": 100., "B": 99., "C": 98.}
    for stamp in np.arange(min(starts.values()), max(starts.values()) + 10.5, .5):
        for code, start in starts.items():
            if stamp == start:
                # Delivery has advanced the reducer clock before an old boundary
                # publication arrives. Operational event times remain unchanged.
                apply(store, adapter, "TrackStatus", {"Status": "1"}, stamp)
                timing(store, adapter, code, {"NumberOfLaps": 1, "InPit": False}, stamp - delay)
            if not start <= stamp <= start + 10 or (code == "B" and unfinished_b and stamp > 106):
                continue
            utc = (EPOCH + timedelta(seconds=float(stamp))).isoformat()
            apply(store, adapter, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
                code: {"Channels": {"2": b_speed if code == "B" else 360., "0": 12000}},
            }}]}, stamp)
            apply(store, adapter, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                code: {"X": 100, "Y": 100, "Status": "OnTrack"},
            }}]}, stamp)
            if stamp == start + 10:
                timing(store, adapter, code, {"NumberOfLaps": 2,
                       "LastLapTime": {"Value": "0:10.000"}}, stamp - delay)
    return store, adapter


def samples(adapter):
    rows = [row for values in adapter._rows.values() for row in values]
    states = [dict(state, Driver=number) for number, values in adapter._candidate_states.items()
              for state in values]
    return evaluate_physical_traffic(pd.DataFrame(rows),
        config=TrafficEvaluationConfig(1000., require_phase_validity=True,
                                       coverage_policy=QUALIFYING_TRAFFIC_POLICY),
        target_lap=("A", 2), candidate_states=pd.DataFrame(states)).samples.set_index("SessionTime")


def test_real_reducer_supported_overlap_is_still_one_second():
    _, adapter = real_field()
    result = adapter.finalize("A", 2)
    result_samples = samples(adapter)
    assert (result_samples.loc[100.:108., "PhysicalTimeDeltaToDriverAhead"] == 1.).all()
    # C ends at 108 s and B at 109 s, both still affirmatively on track.
    # The unsupported tail must veto the whole-lap mean despite good coverage.
    assert result_samples.loc[108.5:, "TrafficPhaseUnavailable"].all()
    assert not result.available
    assert result.mean_ahead_gap_s is None
    assert list(adapter._lap_spans) == [1000.]


def test_delayed_original_publications_cannot_validate_shifted_traversal():
    store, adapter = real_field(delay=100)
    event = next(e for e in store.snapshot().completed_lap_sequence if e.driver_number == "A")
    # Reproduce the review: the operational event inherited the advanced clock.
    assert event.session_time_s == 110.
    evidence = next(e for e in store.snapshot().traffic_evidence_sequence
                    if e.kind == "completion" and e.driver_number == "A")
    assert evidence.publication_time.seconds == 10.
    # Acquisition ownership now assigns samples at 100+ to lap 3, after the
    # independently published lap-2 boundary at 10 s. They cannot form lap 2.
    assert adapter._rows["A"][0]["LapNumber"] == 3
    assert adapter._rows["A"][0]["LapStartTime"] == 10.
    assert adapter._rows["A"][-1]["LapEndTime"] is None
    result = adapter.finalize("A", 2)
    assert not result.available
    assert result.mean_ahead_gap_s is None
    assert not adapter._lap_spans


def test_explicit_completion_retraction_revokes_anchor_and_keyed_history():
    store, adapter = real_field(b_speed=432.)
    assert adapter.finalize("A", 2).circuit_length_m == 1000.
    adapter.finalize("B", 2)
    keys = tuple(adapter._span_history)
    assert len(keys) == 2
    raw = timing(store, adapter, "A", {"NumberOfLaps": 2, "LastLapTime": {"Value": ""}}, 110.1)
    assert any(e.kind == "completion_invalidated" and e.completion_key == keys[0]
               for e in raw.traffic_evidence_sequence)
    assert adapter._rows["A"][-1]["LapEndTime"] is None
    assert tuple(adapter._span_history) == (keys[1],)
    assert list(adapter._lap_spans) == [1200.]
    assert adapter.finalize("B", 2).circuit_length_m == 1200.
    assert adapter.finalize("A", 2).mean_ahead_gap_s is None
    assert not adapter.finalize("C", 2).available  # Retracted A remains a competitor.
    assert keys[0] not in adapter._span_history


def test_conflicting_completion_owners_cannot_supply_a_later_start_anchor():
    store, adapter = real_field()
    adapter.finalize("A", 2)
    apply(store, adapter, "TrackStatus", {"Status": "1"}, 111.)
    # A second boundary at the advanced operational clock has a different key but
    # claims the same original publication. Neither can own a phase anchor.
    timing(store, adapter, "A", {"NumberOfLaps": 3,
                               "LastLapTime": {"Value": "0:10.000"}}, 110.)
    apply(store, adapter, "Position.z", {"Position": [{
        "Timestamp": (EPOCH + timedelta(seconds=110.5)).isoformat(),
        "Entries": {"A": {"X": 100, "Y": 100, "Status": "OnTrack"}},
    }]}, 110.5)
    assert adapter._rows["A"][-1]["LapNumber"] is None
    assert adapter._rows["A"][-1]["LapStartTime"] is None
    assert not adapter._lap_spans


@pytest.mark.parametrize("patch", [{"BestLapTime": {"Value": ""}},
                                   {"BestLapTime": {"Value": "0:09.500", "Lap": 1}},
                                   {"Position": 2}])
def test_unrelated_result_register_changes_do_not_retract_traversal(patch):
    store, adapter = real_field()
    before = adapter.finalize("A", 2)
    history = dict(adapter._span_history)
    timing(store, adapter, "A", patch, 110.1)
    assert adapter._rows["A"][-1]["LapEndTime"] == 110.
    assert adapter.finalize("A", 2) == before
    assert adapter._span_history == history  # Re-finalization is idempotent too.


@pytest.mark.parametrize("speed", ["unchanged", "missing", "stale"])
def test_pit_transition_without_usable_telemetry_releases_only_future_veto(speed):
    store, adapter = real_field(unfinished_b=True)
    before = samples(adapter)
    assert pd.isna(before.loc[107., "PhysicalDriverAhead"])
    if speed != "unchanged":
        stamp = 95. if speed == "stale" else 110.
        apply(store, adapter, "CarData.z", {"Entries": [{
            "Utc": (EPOCH + timedelta(seconds=stamp)).isoformat(),
            "Cars": {"B": {"Channels": {"2": None if speed == "missing" else 360.}}},
        }]}, stamp)
    # No new position: the affirmative pit publication itself carries the time.
    timing(store, adapter, "B", {"InPit": True}, 107.)
    after = samples(adapter)
    assert pd.isna(after.loc[106.5, "PhysicalDriverAhead"])
    assert after.loc[107., "PhysicalDriverAhead"] == "C"
    assert after.loc[107., "PhysicalDistanceToDriverAhead"] == pytest.approx(200.)
    assert adapter._rows["B"][-1]["SessionTime"] == 106.


def test_disappearance_without_affirmative_state_keeps_future_veto():
    _, adapter = real_field(unfinished_b=True)
    assert pd.isna(samples(adapter).loc[107., "PhysicalDriverAhead"])
    assert not adapter.finalize("A", 2).available


def test_delayed_pit_publication_does_not_erase_later_on_track_evidence():
    store, adapter = real_field(unfinished_b=True)
    timing(store, adapter, "B", {"InPit": False}, 109.)
    timing(store, adapter, "B", {"InPit": True}, 107.)
    result = samples(adapter)
    assert not result.loc[107., "TrafficPhaseUnavailable"]
    assert result.loc[109., "TrafficPhaseUnavailable"]


def test_delayed_position_cannot_move_current_pit_state_into_the_past():
    store, adapter = real_field(unfinished_b=True)
    timing(store, adapter, "B", {"InPit": True}, 107.)
    # A new-to-the-buffer sample, but physically before the pit transition.
    stamp = 106.5
    apply(store, adapter, "Position.z", {"Position": [{
        "Timestamp": (EPOCH + timedelta(seconds=stamp)).isoformat(),
        "Entries": {"B": {"X": 100, "Y": 100, "Status": "OnTrack"}},
    }]}, stamp)
    result = samples(adapter)
    assert result.loc[106.5, "TrafficPhaseUnavailable"]
    assert not result.loc[107., "TrafficPhaseUnavailable"]


def test_history_candidate_cannot_change_the_estimate_used_to_validate_it():
    _, adapter = real_field(b_speed=900.)  # B integrates to 2,500 m.
    adapter.finalize("A", 2)  # Established 1,000 m bootstrap.
    before = dict(adapter._span_history)
    result = adapter.finalize("B", 2)
    assert adapter._span_history == before
    assert result.circuit_length_m == 1000.
    assert not result.available
    assert result.mean_ahead_gap_s is None


def test_bootstrap_has_no_established_distance_range_to_self_validate_against():
    _, adapter = real_field(b_speed=900.)
    result = adapter.finalize("B", 2)
    assert list(adapter._lap_spans) == [2500.]
    assert result.circuit_length_m == 2500.
