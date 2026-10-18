"""D1 historical calibration and callback scheduling, through the real pipeline."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from wodata.live import FastF1LiveTimingSource, LiveTimingMessage
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.service import QualiLiveService

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def send(service, topic, payload, acquired, receipt=None):
    receipt = acquired if receipt is None else receipt
    live = isinstance(service.controller._source, FastF1LiveTimingSource)
    message = LiveTimingMessage(topic, payload, EPOCH + timedelta(seconds=acquired),
        EPOCH + timedelta(seconds=receipt), source="signalr_live" if live else "f1_archive_replay",
        session_time_seconds=acquired, playback_time_seconds=receipt,
        received_at_is_trusted=live, received_at_monotonic_s=receipt if live else None)
    service.consume(service.controller.store.apply(message))


def timing(service, number, patch, acquired, receipt=None):
    send(service, "TimingData", {"Lines": {number: patch}}, acquired, receipt)


def calibrated_field(live=False, *, b_start=97.1, b_sector=6):
    service = QualiLiveService()
    if live:
        service.controller._source = FastF1LiveTimingSource()
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    send(service, "DriverList", {n: {"Tla": n} for n in "ABC"}, 70.)
    events = []
    def add(topic, payload, acquired):
        events.append((acquired, len(events), topic, payload))
    for n, start, speed, sector in (("C", 80., 432., 3), ("B", b_start, 360., b_sector), ("A", 100., 360., 3)):
        add("TimingData", {"Lines": {n: {"NumberOfLaps": 1, "InPit": False}}}, start)
        for step in range(21):
            t = start + step / 2
            utc = (EPOCH + timedelta(seconds=t)).isoformat()
            add("CarData.z", {"Entries": [{"Utc": utc, "Cars": {n: {"Channels": {"2": speed}}}}]}, t)
            add("Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                n: {"Status": "OnTrack", "X": 1, "Y": 1}}}]}, t)
            if step == 6:
                add("TimingData", {"Lines": {n: {"Sectors": {"0": {"Value": f"0:0{sector}.000"}}}}}, t)
        add("TimingData", {"Lines": {n: {"NumberOfLaps": 2, "LastLapTime": {"Value": "0:10.000"}}}}, start + 10)
        if n != "A":
            add("TimingData", {"Lines": {n: {"InPit": True}}}, start + 10.01)
    for acquired, _, topic, payload in sorted(events):
        send(service, topic, payload, acquired)
    assert service._physical_traffic._lap_spans == (1200.,)
    assert target(service).traffic_status == "pending"
    return service


def target(service):
    return next(sample for sample in service._track_samples if sample.driver == "A")


def business(service):
    x = target(service)
    return (x.traffic_status, x.traffic_circuit_length_m, x.physical_mean_ahead_gap_s,
            x.ahead_coverage, x.clean_air_at_completion, x.traffic_target_samples,
            x.traffic_ahead_samples)


def withdraw_old(service, receipt, conflict=False):
    patch = {"NumberOfLaps": 3, "LastLapTime": {"Value": "0:10.000"}} if conflict else {
        "NumberOfLaps": 1}
    timing(service, "C", patch, 90., receipt)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
def test_original_114_1_withdrawal_cannot_change_historical_calibration(live, conflict):
    service = calibrated_field(live)
    withdraw_old(service, 114.1, conflict)
    result = target(service)
    assert result.traffic_circuit_length_m == pytest.approx(1100.)
    assert result.physical_mean_ahead_gap_s == pytest.approx(3.19)
    assert result.ahead_coverage == pytest.approx(15 / 21)
    assert result.clean_air_at_completion is True
    assert not service._pending_traffic
    historical = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=112., clock_kind=service._traffic_clock_kind)
    assert historical.circuit_length_m == pytest.approx(1100.)
    assert historical.mean_ahead_gap_s == pytest.approx(3.19)
    current = service._physical_traffic.finalize("A", 2, admit_span=False)
    assert current.circuit_length_m == pytest.approx(1000.)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("receipt", [111., 112., 113., 114., 114.1])
def test_same_causal_evidence_has_same_locked_business_result_under_callback_schedules(live, receipt):
    prompt, delayed = calibrated_field(live), calibrated_field(live)
    if receipt > 112.:
        prompt.advance_traffic_clock(112.)
    if receipt > 114.:
        prompt.advance_traffic_clock(114.)
    withdraw_old(prompt, receipt)
    withdraw_old(delayed, receipt)
    prompt.advance_traffic_clock(115.)
    delayed.advance_traffic_clock(115.)
    assert business(delayed) == business(prompt)
    assert tuple(delayed._physical_traffic._span_history.items()) == tuple(prompt._physical_traffic._span_history.items())
    assert not delayed._pending_traffic and not prompt._pending_traffic
    result = target(delayed)
    assert result.traffic_circuit_length_m == pytest.approx(1000. if receipt <= 114. else 1100.)
    assert result.clean_air_at_completion is (receipt > 114.)


def test_duplicate_withdrawal_preserves_first_retirement_and_historical_view():
    service = calibrated_field()
    service.advance_traffic_clock(112.)
    old_key = next(key for key in service._physical_traffic._span_history if key[1] == "C")
    withdraw_old(service, 113.)
    first = service._physical_traffic._revocation_clocks[old_key]
    withdraw_old(service, 113.5)
    assert service._physical_traffic._revocation_clocks[old_key] == first
    old = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=112., clock_kind=service._traffic_clock_kind)
    new = service._physical_traffic.finalize("A", 2, admit_span=False,
        evidence_deadline_s=113.5, clock_kind=service._traffic_clock_kind)
    assert old.circuit_length_m == pytest.approx(1100.)
    assert new.circuit_length_m == pytest.approx(1000.)
    assert target(service).clean_air_at_completion is False


@pytest.mark.parametrize("live", [False, True])
def test_acquisition_retention_jump_after_deadline_cannot_erase_pending_normal_result(live):
    prompt, delayed = calibrated_field(live), calibrated_field(live)
    prompt.advance_traffic_clock(112.)
    prompt.advance_traffic_clock(114.)
    for service in (prompt, delayed):
        utc = (EPOCH + timedelta(seconds=1000.)).isoformat()
        send(service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {"A": {"Channels": {"2": 360.}}}}]}, 1000., 115.)
        send(service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
            "A": {"Status": "OnTrack", "X": 1, "Y": 1}}}]}, 1000., 115.)
    assert business(delayed) == business(prompt)
    assert target(delayed).clean_air_at_completion is True


def test_update_retention_compaction_preserves_current_and_retained_calibration(monkeypatch):
    from test_quali_c1_review_corrections import field, heartbeat
    store, service, key = field()
    heartbeat(store, service, 112.)
    monkeypatch.setattr("woplanner.quali.physical_traffic.MAX_BOUNDARY_RECORDS", 2)
    for number, stamp, receipt in (("A", 105.25, 113.), ("B", 105.75, 113.25), ("A", 106.25, 113.5)):
        utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
        send(service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
            number: {"Channels": {"2": 720.}}}}]}, stamp, receipt - .1)
        send(service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
            number: {"Status": "OnTrack", "X": 1, "Y": 1}}}]}, stamp, receipt)
    adapter = service._physical_traffic
    # Protected pending cutoffs retain all three causal revisions, even
    # when the usual history budget is reduced to two for this test.
    assert sum(len(record.versions) - 1 for record in adapter._span_versions.values()) == 3
    normal = adapter.finalize("A", 2, admit_span=False, evidence_deadline_s=112.,
                              clock_kind=service._traffic_clock_kind)
    assert normal.circuit_length_m == pytest.approx(1000.)
    assert len(adapter._span_history) == 2
    earlier = adapter.finalize("A", 2, admit_span=False, evidence_deadline_s=113.1,
                               clock_kind=service._traffic_clock_kind)
    current = adapter.finalize("A", 2, admit_span=False, evidence_deadline_s=113.5,
                               clock_kind=service._traffic_clock_kind)
    assert earlier.circuit_length_m == pytest.approx(1012.5)
    assert current.circuit_length_m == pytest.approx(1037.5)
    assert target(service).traffic_circuit_length_m == current.circuit_length_m


def test_estimator_capacity_keeps_prior_contribution_window_until_retirement_cutoff():
    from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
    from test_quali_third_adversarial import _complete_trace
    adapter = RollingQualiPhysicalTraffic()
    evidence, completions = (), ()
    prior_cutoff = None
    prior_keys = None
    for index in range(33):
        start = 80. + index * 10.
        raw, event = _complete_trace(str(index), start, 360., start + 10., evidence, completions)
        adapter.ingest(raw)
        adapter.finalize(str(index), 2, evidence_deadline_s=start + 12., clock_kind="receive")
        evidence, completions = raw.traffic_evidence_sequence, raw.completed_lap_sequence
        if index == 31:
            prior_cutoff, prior_keys = start + 12., tuple(adapter._span_history)
    assert len(adapter._span_history) == 32
    assert prior_keys[0] not in adapter._span_history
    assert tuple(adapter._spans_at_deadline(prior_cutoff, "receive")) == prior_keys
    assert len(adapter._spans_at_deadline(start + 12., "receive")) == 32
