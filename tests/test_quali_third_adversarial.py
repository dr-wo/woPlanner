"""Independent evidence-lifecycle counterexamples for the third correction round."""
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
from test_quali_c1_review_corrections import field, packet, heartbeat, target

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _complete_trace(number, start, speed, clock, earlier=(), earlier_completions=()):
    end = start + 10.
    event = QualiCompletedLap("Q1", number, 2, 10., end)
    stamps = tuple(start + i / 2 for i in range(21))
    evidence = earlier + (
        QualiTrafficEvidence(number, "lap_counter", PublicationTime("session", start), 1),
        QualiTrafficEvidence(number, "lap_counter", PublicationTime("session", end), 2),
        QualiTrafficEvidence(number, "completion", PublicationTime("session", end), 2, event.event_key),
    )
    raw = replace(
        QualiRawSnapshot.empty(),
        drivers=(QualiRawDriverState(number, number, "Team", current_lap_identity=3,
                 structural_state=QualiPitState.ON_TRACK, speed_kph=speed),),
        position_observations=tuple(QualiPositionObservation(number, stamp, "OnTrack",
                                   acquisition_coordinate="session") for stamp in stamps),
        speed_history={number: tuple(QualiSpeedObservation(number, stamp, speed, EPOCH,
                                    acquisition_coordinate="session") for stamp in stamps)},
        traffic_evidence_sequence=evidence,
        completed_lap_sequence=earlier_completions + (event,),
        traffic_clock_time_s=clock, traffic_clock_kind="receive", receive_time_trusted=True,
    )
    return raw, event


def test_later_admitted_calibration_cannot_admit_earlier_out_of_range_span():
    adapter = RollingQualiPhysicalTraffic()
    original, old = _complete_trace("C", 80., 360., 90.)
    adapter.ingest(original)
    adapter.finalize("C", 2, evidence_deadline_s=92., clock_kind="receive")
    assert adapter._span_history[old.event_key] == pytest.approx(1000.)
    earlier, candidate = _complete_trace("A", 100., 576., 110.,
        original.traffic_evidence_sequence, original.completed_lap_sequence)
    adapter.ingest(earlier)
    future, later = _complete_trace("D", 120., 536.4, 130.,
        earlier.traffic_evidence_sequence, earlier.completed_lap_sequence)
    adapter.ingest(future)
    adapter.finalize("D", 2, evidence_deadline_s=132., clock_kind="receive")
    assert adapter._span_history[later.event_key] == pytest.approx(1490.)
    # At 112 only C's 1000 m contribution exists. A's 1600 m traversal
    # fails the unchanged (< 1.5 * circuit length) admission rule. The
    # future 1490 m contribution must not raise that historical threshold.
    result = adapter.finalize("A", 2, evidence_deadline_s=112., clock_kind="receive")
    assert candidate.event_key not in adapter._span_history
    assert result.circuit_length_m == pytest.approx(1000.)


def test_negative_then_positive_same_acquisition_preserves_cutoff_versions():
    adapter = RollingQualiPhysicalTraffic()
    adapter._record_state("B", 105., False, 111., "receive", "session")
    adapter._record_state("B", 105., True, 113., "receive", "session")
    states = adapter._candidate_states["B"]
    assert [state["CandidateEligible"] for state in states
            if state["_ReceivedClock"] <= 112.] == [False]
    assert [state["CandidateEligible"] for state in states
            if state["_ReceivedClock"] <= 113.] == [False, True]
    revision = adapter.revision
    adapter._record_state("B", 105., True, 113.5, "receive", "session")
    assert adapter.revision == revision


def test_off_track_correction_at_same_acquisition_revises_grace_not_normal():
    store, service, key = field()
    heartbeat(store, service, 112.)
    normal = target(service)
    assert normal.ahead_coverage == pytest.approx(19 / 21)
    # A source correction for an existing acquisition coordinate is negative
    # evidence, not a duplicate affirmative Position packet.
    stamp = 105.
    utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
    packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
        "B": {"X": 100, "Y": 100, "Status": "OffTrack"},
    }}]}, stamp, 113.)
    # This helper's archive path uses playback clocks, inspect the active
    # domain rather than manufacturing a genuine receive clock.
    historical = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=112., clock_kind=service._traffic_clock_kind)
    assert historical.ahead_coverage == normal.ahead_coverage
    assert target(service).ahead_coverage < normal.ahead_coverage


def test_nonoverlapping_old_span_withdrawal_revises_pending_calibration():
    from woplanner.quali.lap_state import LapPaceReference
    from woplanner.quali.service import QualiLiveService
    from test_quali_c1_review_corrections import timing

    service = QualiLiveService()
    store = service.controller.store
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    packet(store, service, "DriverList", {number: {"Tla": number} for number in "ABC"}, 75.)

    def trace(number, stamp, speed):
        utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
        packet(store, service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
            number: {"Channels": {"2": speed, "0": 12000}},
        }}]}, stamp)
        packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
            number: {"X": 100, "Y": 100, "Status": "OnTrack"},
        }}]}, stamp)

    timing(store, service, "C", {"NumberOfLaps": 1, "InPit": False}, 80.)
    for half in range(160, 181):
        stamp = half / 2
        trace("C", stamp, 432.)
        if stamp == 83.:
            timing(store, service, "C", {"Sectors": {"0": {"Value": "0:03.000"}}}, stamp)
    timing(store, service, "C", {"NumberOfLaps": 2,
           "LastLapTime": {"Value": "0:10.000"}}, 90.)
    heartbeat(store, service, 92.)
    assert tuple(service._physical_traffic._lap_spans) == pytest.approx((1200.,))
    # This next boundary prevents C's old revocation phase interval from
    # overlapping A. Its span remains an estimator dependency of A.
    timing(store, service, "C", {"NumberOfLaps": 3}, 95.)
    for half in range(200, 223):
        stamp = half / 2
        for number, start in (("A", 100.), ("B", 101.)):
            if stamp == start:
                timing(store, service, number, {"NumberOfLaps": 1, "InPit": False}, stamp)
            if not start <= stamp <= start + 10.:
                continue
            trace(number, stamp, 360.)
            if number == "A" and stamp == start + 3.:
                timing(store, service, number, {"Sectors": {"0": {"Value": "0:03.000"}}}, stamp)
            if stamp == start + 10.:
                timing(store, service, number, {"NumberOfLaps": 2,
                       "LastLapTime": {"Value": "0:10.000"}}, stamp)
    heartbeat(store, service, 112.)
    normal = target(service)
    assert normal.traffic_circuit_length_m == pytest.approx(1100.)
    assert len(service._track_samples) == 2  # C and A; B is not a pace candidate.
    withdrawn = timing(store, service, "C", {"NumberOfLaps": 1, "LastLapTime": {"Value": ""}}, 90., 113.)
    assert any(event.kind == "completion_invalidated" and event.driver_number == "C"
               for event in withdrawn.traffic_evidence_sequence)
    assert target(service).traffic_circuit_length_m == pytest.approx(1000.)
    earlier = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=112., clock_kind=service._traffic_clock_kind)
    assert earlier.circuit_length_m == normal.traffic_circuit_length_m


def test_new_grace_telemetry_versions_existing_span_contribution():
    store, service, key = field()
    heartbeat(store, service, 112.)
    assert service._physical_traffic._span_history[key] == pytest.approx(1000.)
    stamp = 105.25
    utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
    packet(store, service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
        "A": {"Channels": {"2": 720., "0": 12000}},
    }}]}, stamp, 112.8)
    packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
        "A": {"X": 100, "Y": 100, "Status": "OnTrack"},
    }}]}, stamp, 112.9)
    # The genuinely new 105.25 sample changes right-endpoint integration
    # of the 105.0–105.5 interval from 50 m to 75 m. One completion still
    # contributes once; its evidence version changes only after receipt.
    assert service._physical_traffic._span_history[key] == pytest.approx(1025.)
    normal = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=112., clock_kind=service._traffic_clock_kind)
    assert normal.circuit_length_m == pytest.approx(1000.)
    assert target(service).traffic_circuit_length_m == pytest.approx(1025.)


def test_multiple_same_acquisition_state_flips_keep_delivery_order():
    adapter = RollingQualiPhysicalTraffic()
    deliveries = ((111., True), (113., False), (113.25, True), (113.5, False))
    for clock, eligible in deliveries:
        adapter._record_state("B", 105., eligible, clock, "receive", "session")
    states = adapter._candidate_states["B"]
    for deadline, expected in deliveries:
        timely = [state for state in states if state["_ReceivedClock"] <= deadline]
        assert timely[-1]["CandidateEligible"] is expected
    assert len(states) == 4
    revision = adapter.revision
    adapter._record_state("B", 105., False, 113.75, "receive", "session")
    assert adapter.revision == revision


def test_out_of_order_conflicting_boundary_receipt_does_not_resurrect_ambiguity():
    adapter = RollingQualiPhysicalTraffic()
    start = PublicationTime("session", 100.)
    first = QualiTrafficEvidence("A", "lap_counter", start, 1)
    conflicting = QualiTrafficEvidence("A", "lap_counter", start, 2)
    snapshot = replace(QualiRawSnapshot.empty(), traffic_evidence_sequence=(first,),
        traffic_clock_time_s=113., traffic_clock_kind="receive")
    adapter.ingest(snapshot)
    # A fault-scheduled packet can carry an older original receipt. Once
    # incompatible source ownership is known, neither numerical receipt
    # sorting nor repeated publication can make the source affirmative.
    adapter.ingest(replace(snapshot, traffic_evidence_sequence=(first, conflicting),
                          traffic_clock_time_s=111.))
    assert adapter._phase_at_deadline(114., "receive").lap_at_sample("A", 105., "session") is None
    before = adapter.revision
    adapter.ingest(replace(snapshot, traffic_evidence_sequence=(first, conflicting, first),
                          traffic_clock_time_s=115.))
    assert adapter.revision == before
    assert adapter.lap_at_sample("A", 105., "session") is None


def _dual_calibration_field(*, live=False, b_start=101.):
    from wodata.live import FastF1LiveTimingSource
    from woplanner.quali.lap_state import LapPaceReference
    from woplanner.quali.service import QualiLiveService
    from test_quali_c1_review_corrections import timing
    service = QualiLiveService()
    store = service.controller.store
    if live:
        service.controller._source = FastF1LiveTimingSource()
        service._test_live_receipts = True
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    packet(store, service, "DriverList", {number: {"Tla": number} for number in "AB"}, 90.)
    for half in range(200, int((b_start + 10.) * 2) + 1):
        stamp = half / 2.
        for number, start, speed in (("A", 100., 360.), ("B", b_start, 432.)):
            if stamp == start:
                timing(store, service, number, {"NumberOfLaps": 1, "InPit": False}, stamp)
            if not start <= stamp <= start + 10:
                continue
            utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
            packet(store, service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
                number: {"Channels": {"2": speed, "0": 12000}},
            }}]}, stamp)
            packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                number: {"X": 100, "Y": 100, "Status": "OnTrack"},
            }}]}, stamp)
            if stamp == start + 3:
                timing(store, service, number, {"Sectors": {"0": {"Value": "0:03.000"}}}, stamp)
            if stamp == start + 10:
                timing(store, service, number, {"NumberOfLaps": 2,
                       "LastLapTime": {"Value": "0:10.000"}}, stamp)
    return store, service


@pytest.mark.parametrize("live", [False, True])
def test_multiple_pending_calibrations_lock_equivalently_with_sparse_callbacks(live):
    timely_store, timely = _dual_calibration_field(live=live)
    delayed_store, delayed = _dual_calibration_field(live=live)
    # Both runs use exactly the same feed packets and genuine receipts.
    # Only timer callback scheduling differs before the common final advance.
    timely.advance_traffic_clock(112.)
    assert target(timely).traffic_circuit_length_m == pytest.approx(1000.)
    timely.advance_traffic_clock(113.)
    assert target(timely).traffic_circuit_length_m == pytest.approx(1100.)
    heartbeat(timely_store, timely, 114.1)
    heartbeat(delayed_store, delayed, 114.1)
    a, b = target(timely), target(delayed)
    assert a.traffic_circuit_length_m == b.traffic_circuit_length_m
    assert a.physical_mean_ahead_gap_s == pytest.approx(b.physical_mean_ahead_gap_s)
    assert a.ahead_coverage == b.ahead_coverage
    assert a.clean_air_at_completion == b.clean_air_at_completion
    assert a.traffic_status == b.traffic_status
    assert list(timely._physical_traffic._lap_spans) == list(delayed._physical_traffic._lap_spans)


@pytest.mark.parametrize("live", [False, True])
def test_equal_deadline_receipt_is_ingested_before_other_due_stages_lock(live):
    timely_store, timely = _dual_calibration_field(live=live)
    delayed_store, delayed = _dual_calibration_field(live=live)
    stamp = 110.75
    utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
    for store, service in ((timely_store, timely), (delayed_store, delayed)):
        packet(store, service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
            "B": {"Channels": {"2": 864., "0": 12000}},
        }}]}, stamp, 111.1)
    timely.advance_traffic_clock(112.)
    for store, service in ((timely_store, timely), (delayed_store, delayed)):
        packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
            "B": {"X": 100, "Y": 100, "Status": "OnTrack"},
        }}]}, stamp, 113.)
        heartbeat(store, service, 116.)
    # The new position arrives exactly at B's normal deadline, with speed
    # genuinely received earlier. A's overdue timer cannot publish B early.
    a = next(sample for sample in timely._track_samples if sample.driver == "B")
    b = next(sample for sample in delayed._track_samples if sample.driver == "B")
    assert a.traffic_circuit_length_m == b.traffic_circuit_length_m
    assert a.traffic_target_samples == b.traffic_target_samples


@pytest.mark.parametrize("live", [False, True])
def test_same_cutoff_calibration_admission_precedes_other_target_hard_lock(live):
    store, service = _dual_calibration_field(live=live, b_start=102.)
    # A completes110; B completes112. B's normal deadline and A's hard
    # deadline are both114. All evidence eligible at114 must be reflected
    # before A's inclusive hard-limit revision window closes.
    heartbeat(store, service, 114.)
    a = target(service)
    assert list(service._physical_traffic._lap_spans) == pytest.approx([1000., 1200.])
    assert a.traffic_circuit_length_m == pytest.approx(1100.)
    assert not any(key[1] == "A" for key in service._pending_traffic)
    before = a
    heartbeat(store, service, 114.1)
    assert target(service) == before


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("b_start, expected", [(101.5, 1100.), (102.5, 1000.)])
def test_calibration_admission_before_or_after_hard_cutoff_has_causal_effect(live, b_start, expected):
    store, service = _dual_calibration_field(live=live, b_start=b_start)
    heartbeat(store, service, 114.5)
    before = target(service)
    assert before.traffic_circuit_length_m == pytest.approx(expected)
    assert not any(key[1] == "A" for key in service._pending_traffic)
    heartbeat(store, service, 116.)
    assert target(service) == before


@pytest.mark.parametrize("live", [False, True])
def test_equal_normal_cutoffs_publish_consistent_calibration(live):
    store, service = _dual_calibration_field(live=live, b_start=100.)
    heartbeat(store, service, 112.)
    assert list(service._physical_traffic._lap_spans) == pytest.approx([1000., 1200.])
    for sample in service._track_samples:
        assert sample.traffic_circuit_length_m == pytest.approx(1100.)
    heartbeat(store, service, 114.)
    for sample in service._track_samples:
        assert sample.traffic_circuit_length_m == pytest.approx(1100.)
