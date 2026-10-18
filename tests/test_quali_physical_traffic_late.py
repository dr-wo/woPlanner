"""Acquisition ownership and causal traffic evidence, independent of scheduler."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import QualiPitState, QualiRawDriverState, QualiRawSnapshot
from wodata.live.models import PublicationTime
from wodata.live.quali_models import (
    QualiCompletedLap, QualiPositionObservation, QualiSpeedObservation,
    QualiTrafficEvidence,
)
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
START = PublicationTime("session", 100.)
END = PublicationTime("session", 110.)
COMPLETION = QualiCompletedLap("Q1", "1", 2, 10., 110.)
START_EVIDENCE = (QualiTrafficEvidence("1", "lap_counter", START, 1),)
COMPLETE_EVIDENCE = START_EVIDENCE + (
    QualiTrafficEvidence("1", "lap_counter", END, 2),
    QualiTrafficEvidence("1", "completion", END, 2, COMPLETION.event_key),
)


def raw(stamps=(), *, speeds=None, evidence=START_EVIDENCE, completed=(), clock=1.,
        out_lap=False, freshness=5.):
    speeds = speeds if speeds is not None else tuple(
        QualiSpeedObservation("1", stamp, 400., EPOCH) for stamp in stamps
    )
    driver = QualiRawDriverState(
        "1", "AAA", "Team", current_lap_identity=3, structural_state=QualiPitState.ON_TRACK,
        latest_position_time_s=max(stamps, default=110.), latest_telemetry_time_s=110.,
        speed_kph=400., out_lap_confirmed=out_lap,
    )
    return replace(
        QualiRawSnapshot.empty(), drivers=(driver,),
        position_observations=tuple(QualiPositionObservation("1", stamp, "OnTrack") for stamp in stamps),
        speed_history={"1": speeds}, traffic_evidence_sequence=evidence,
        completed_lap_sequence=completed, traffic_clock_time_s=clock,
        traffic_clock_kind="receive", receive_time_trusted=True,
        last_message_at_utc=EPOCH + timedelta(seconds=clock), speed_freshness_s=freshness,
    )


def test_batch_positions_match_each_causal_speed_instead_of_latest_register():
    adapter = RollingQualiPhysicalTraffic()
    speeds = tuple(QualiSpeedObservation("1", stamp, speed, EPOCH)
                   for stamp, speed in [(100., 100.), (101., 200.), (102., 300.)])
    adapter.ingest(raw((100., 100.5, 101., 101.5, 102.), speeds=speeds))
    assert [row["Speed"] for row in adapter._rows["1"]] == [100., 100., 200., 200., 300.]


@pytest.mark.parametrize("speeds", [
    (QualiSpeedObservation("1", 101., 200., EPOCH),),
    (QualiSpeedObservation("1", 100., 200., EPOCH + timedelta(seconds=2)),),
    (QualiSpeedObservation("1", 94.9, 200., EPOCH),),
    (),
])
def test_future_unreceived_stale_and_missing_speed_cannot_create_rows(speeds):
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((100.,), speeds=speeds))
    assert adapter.row_count == 0


def test_configured_freshness_limit_and_history_contract_prevent_reprocessing():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((100.,), speeds=(QualiSpeedObservation("1", 98., 200., EPOCH),), freshness=1.))
    assert adapter.row_count == 0
    adapter.ingest(raw((), speeds=(QualiSpeedObservation("1", 110., 200., EPOCH),)))
    assert adapter.row_count == 0


def test_delayed_end_sample_owns_completed_lap_and_reattaches_end_anchor():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((100., 105., 109.5)))
    adapter.ingest(raw(evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,)))
    adapter.ingest(raw((110.5, 110., 104., 110.), evidence=COMPLETE_EVIDENCE,
                       completed=(COMPLETION,), clock=3., out_lap=True))
    rows = adapter._rows["1"]
    assert [row["SessionTime"] for row in rows] == [100., 104., 105., 109.5, 110., 110.5]
    assert [row["LapNumber"] for row in rows] == [2, 2, 2, 2, 2, 3]
    assert rows[0]["LapStartTime"] == 100.
    assert rows[-2]["LapEndTime"] == 110.
    assert rows[-3]["LapEndTime"] is None
    assert rows[-2]["PhaseRouteValid"] is True
    assert rows[-1]["PhaseRouteValid"] is False
    assert adapter.completed_lap_for_sample("1", 110.) == COMPLETION.event_key
    assert adapter.completed_lap_for_sample("1", 110.5) is None
    assert adapter.completion_bounds(COMPLETION.event_key) == (100., 110.)
    revision = adapter.revision
    adapter.ingest(raw((110.,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,), clock=3.5))
    assert adapter.revision == revision
    assert not adapter.changed_sample_times


def test_no_current_counter_guess_for_samples_before_initial_boundary():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((99.,)))
    assert adapter._rows["1"][0]["LapNumber"] is None
    assert adapter._rows["1"][0]["LapStartTime"] is None
    assert adapter.lap_at_sample("1", 99.) is None


def test_completion_bounds_require_independent_start_and_consistent_duration():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((110.,), evidence=COMPLETE_EVIDENCE[1:], completed=(COMPLETION,)))
    assert adapter.completion_bounds(COMPLETION.event_key) is None
    assert adapter.completed_lap_for_sample("1", 109.) is None
    inconsistent = replace(COMPLETION, lap_time_s=20.)
    other = RollingQualiPhysicalTraffic()
    other.ingest(raw((105.,), evidence=COMPLETE_EVIDENCE, completed=(inconsistent,)))
    assert other.completion_bounds(COMPLETION.event_key) is None


def test_deadline_filter_reconstructs_anchors_and_excludes_later_rows():
    adapter = RollingQualiPhysicalTraffic()
    stamps = tuple(100. + index / 2 for index in range(20))
    adapter.ingest(raw(stamps))
    adapter.ingest(raw(evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,), clock=1.5))
    adapter.ingest(raw((110.,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,), clock=3.))
    normal = adapter.finalize("AAA", 2, evidence_deadline_s=2., clock_kind="receive")
    assert normal.target_samples == 20
    assert normal.circuit_length_m == pytest.approx(9.5 * 400. / 3.6)
    history = dict(adapter._span_history)
    grace = adapter.finalize("AAA", 2, admit_span=False, evidence_deadline_s=4., clock_kind="receive")
    assert grace.target_samples == 21
    assert adapter._span_history == history
    # A later receipt cannot sneak into a caller's already fixed deadline.
    adapter.ingest(raw((109.75,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,), clock=5.))
    assert adapter.finalize("AAA", 2, admit_span=False, evidence_deadline_s=4., clock_kind="receive") == grace


def test_duplicate_affirmative_states_do_not_grow_receipt_versions():
    adapter = RollingQualiPhysicalTraffic()
    adapter._record_state("1", 105., False, 1., "receive")
    adapter._record_state("1", 105., True, 3., "receive")
    revision = adapter.revision
    adapter._record_state("1", 105., True, 4., "receive")
    assert len(adapter._candidate_states["1"]) == 2
    assert adapter.revision == revision


def test_boundary_records_remain_for_diagnostics_after_traffic_rows_age_out():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((100., 110.), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,)))
    adapter.ingest(raw((400.,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,)))
    assert adapter.completed_lap_for_sample("1", 109.) == COMPLETION.event_key
    adapter.ingest(raw((800.,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,)))
    assert adapter.completed_lap_for_sample("1", 109.) is None


def test_exact_counter_boundary_owns_ending_lap_before_completion_publication():
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((110.,), evidence=COMPLETE_EVIDENCE[:-1]))
    assert adapter._rows["1"][0]["LapNumber"] == 2
    assert adapter._rows["1"][0]["LapEndTime"] is None
    assert adapter.completed_lap_for_sample("1", 110.) is None


def test_evicted_start_marker_cannot_be_restored_by_a_later_sample(monkeypatch):
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw((100., 100.5, 101., 102.), evidence=START_EVIDENCE))
    monkeypatch.setattr("woplanner.quali.physical_traffic.ROLLING_TRAFFIC_SECONDS", 1.5)
    adapter.ingest(raw((102.5,), evidence=START_EVIDENCE))
    assert adapter._rows["1"][0]["SessionTime"] == 101.
    assert adapter._rows["1"][0]["LapStartTime"] is None
    assert all(row["LapStartTime"] is None for row in adapter._rows["1"])


def test_missing_speed_still_bounds_state_storage(monkeypatch):
    monkeypatch.setattr("woplanner.quali.physical_traffic.MAX_ROWS_PER_DRIVER", 8)
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(raw(tuple(100. + index for index in range(20)), speeds=()))
    assert adapter.row_count == 0
    assert len(adapter._candidate_states["1"]) == 8


def test_explicit_acquisition_coordinates_cannot_borrow_other_domain_boundaries():
    adapter = RollingQualiPhysicalTraffic()
    snapshot = raw((105.,), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,))
    snapshot = replace(snapshot, position_observations=tuple(
        replace(item, acquisition_coordinate="utc") for item in snapshot.position_observations))
    adapter.ingest(snapshot)
    assert adapter._rows["1"][0]["LapNumber"] is None
    assert adapter.completed_lap_for_sample("1", 105., "utc") is None
    assert adapter.completed_lap_for_sample("1", 105., "session") == COMPLETION.event_key
    assert adapter.lap_at_sample("1", 105., "utc") is None


def test_initial_utc_samples_do_not_poison_later_mapped_session_watermark():
    adapter = RollingQualiPhysicalTraffic()
    initial = raw((1_700_000_000.,), evidence=START_EVIDENCE)
    initial = replace(initial, position_observations=tuple(
        replace(item, acquisition_coordinate="utc") for item in initial.position_observations))
    adapter.ingest(initial)
    mapped = raw((100., 105., 110.), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,))
    mapped = replace(mapped, position_observations=tuple(
        replace(item, acquisition_coordinate="session") for item in mapped.position_observations))
    adapter.ingest(mapped)
    valid = [row for row in adapter._rows["1"] if row["_AcquisitionCoordinate"] == "session"]
    assert [row["SessionTime"] for row in valid] == [100., 105., 110.]
    assert [row["LapNumber"] for row in valid] == [2, 2, 2]
    assert valid[0]["LapStartTime"] == 100.
    assert valid[-1]["LapEndTime"] == 110.
    assert adapter.completed_lap_for_sample("1", 1_700_000_000., "utc") is None
    assert adapter.completion_bounds(COMPLETION.event_key) == (100., 110.)


def test_coordinate_transition_capacity_keeps_recent_mapped_samples(monkeypatch):
    monkeypatch.setattr("woplanner.quali.physical_traffic.MAX_ROWS_PER_DRIVER", 3)
    adapter = RollingQualiPhysicalTraffic()
    initial = raw((1_700_000_000., 1_700_000_001., 1_700_000_002.))
    initial = replace(initial, position_observations=tuple(
        replace(item, acquisition_coordinate="utc") for item in initial.position_observations))
    adapter.ingest(initial)
    mapped = raw((100., 105., 110.), evidence=COMPLETE_EVIDENCE, completed=(COMPLETION,))
    mapped = replace(mapped, position_observations=tuple(
        replace(item, acquisition_coordinate="session") for item in mapped.position_observations))
    adapter.ingest(mapped)
    assert [row["SessionTime"] for row in adapter._rows["1"]] == [100., 105., 110.]
    assert all(row["_AcquisitionCoordinate"] == "session" for row in adapter._rows["1"])
    assert [state["SessionTime"] for state in adapter._candidate_states["1"]] == [100., 105., 110.]


def test_mixed_coordinate_time_expiry_inspects_rows_beyond_numeric_front(monkeypatch):
    monkeypatch.setattr("woplanner.quali.physical_traffic.ROLLING_TRAFFIC_SECONDS", 10.)
    adapter = RollingQualiPhysicalTraffic()
    utc = raw((1000., 1001., 1002.))
    utc = replace(utc, position_observations=tuple(
        replace(item, acquisition_coordinate="utc") for item in utc.position_observations))
    adapter.ingest(utc)
    session = raw((100., 101.))
    session = replace(session, position_observations=tuple(
        replace(item, acquisition_coordinate="session") for item in session.position_observations))
    adapter.ingest(session)
    # A different driver advances UTC without advancing the session domain.
    newer = raw((1020.,))
    newer = replace(newer, drivers=(replace(newer.drivers[0], driver_number="2", driver_code="BBB"),),
                    position_observations=(QualiPositionObservation("2", 1020., "OnTrack", acquisition_coordinate="utc"),),
                    speed_history={"2": (QualiSpeedObservation("2", 1020., 400., EPOCH),)})
    adapter.ingest(newer)
    assert [row["SessionTime"] for row in adapter._rows["1"]] == [100., 101., 1002.]
    assert [state["SessionTime"] for state in adapter._candidate_states["1"]] == [100., 101., 1002.]
