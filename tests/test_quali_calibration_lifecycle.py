"""Approved calibration lifecycle, through the real reducer/service/evaluator."""
from datetime import timedelta

import pytest

from test_quali_third_historical_evidence import (
    EPOCH, calibrated_field, send, timing, target, business, withdraw_old,
)
from test_quali_c1_review_corrections import field, heartbeat
VALID, INVALIDATED = "VALID", "INVALIDATED"


def position(service, number, stamp, receipt, status="OnTrack", speed=None):
    utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
    if speed is not None:
        send(service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
            number: {"Channels": {"2": speed}}}}]}, stamp, receipt)
    return send(service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
        number: {"Status": status, "X": 1, "Y": 1}}}]}, stamp, receipt)


def key_for(service, number):
    return next(key for key in service._physical_traffic._admitted_completion_keys if key[1] == number)


def spans_at(service, cutoff):
    return service._physical_traffic._spans_at_deadline(cutoff, service._traffic_clock_kind)


def add_later_target(service):
    # The original target is explicitly off track before the future field.
    timing(service, "A", {"InPit": True}, 115.9)
    send(service, "DriverList", {n: {"Tla": n} for n in "DE"}, 116.)
    events = []
    for number, start, sector in (("E", 117.1, 6), ("D", 120., 3)):
        events.append((start, "TimingData", {"Lines": {number: {"NumberOfLaps": 1, "InPit": False}}}))
        for step in range(21):
            stamp = start + step / 2
            utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
            events.extend(((stamp, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {number: {"Channels": {"2": 360.}}}}]}),
                           (stamp, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {number: {"Status": "OnTrack", "X": 1, "Y": 1}}}]})))
            if step == 6:
                events.append((stamp, "TimingData", {"Lines": {number: {"Sectors": {"0": {"Value": f"0:0{sector}.000"}}}}}))
        events.append((start + 10, "TimingData", {"Lines": {number: {"NumberOfLaps": 2, "LastLapTime": {"Value": "0:10.000"}}}}))
        events.append((start + 10.01, "TimingData", {"Lines": {number: {"InPit": True}}}))
    for stamp, topic, payload in sorted(events, key=lambda e: e[0]):
        send(service, topic, payload, stamp)
    service.advance_traffic_clock(135.)
    return next(sample for sample in service._track_samples if sample.driver == "D")


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("stamp", [105., 105.25])
def test_same_key_invalidation_and_corrective_recovery_are_receipt_versioned(live, stamp):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "A")
    normal = target(service)
    position(service, "A", stamp, 113., "OffTrack")
    assert key not in service._physical_traffic._span_history
    assert key in spans_at(service, 112.)
    assert key not in spans_at(service, 113.)
    assert target(service).traffic_status != "ok"
    assert key not in service._physical_traffic._revoked
    position(service, "A", stamp, 113.5)
    assert service._physical_traffic._span_history[key] == pytest.approx(1000.)
    assert key not in spans_at(service, 113.25)
    assert spans_at(service, 113.5)[key] == pytest.approx(1000.)
    assert target(service).traffic_status == normal.traffic_status
    assert target(service).clean_air_at_completion is True
    states = [v.validity for v in service._physical_traffic._span_versions[key].versions]
    assert states == [VALID, INVALIDATED, VALID]
    assert len([k for k in service._physical_traffic._span_history if k == key]) == 1


@pytest.mark.parametrize("live", [False, True])
def test_later_acquisition_return_does_not_correct_original_offtrack(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "A")
    position(service, "A", 105.25, 113., "OffTrack")
    position(service, "A", 105.75, 113.25)
    assert key not in service._physical_traffic._span_history
    position(service, "A", 105.25, 113.5)
    assert key in service._physical_traffic._span_history


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("receipt", [111.5, 112., 113., 114., 114.1])
def test_prompt_and_delayed_callbacks_match_invalidation_and_recovery(live, receipt):
    prompt, delayed = calibrated_field(live), calibrated_field(live)
    if receipt > 112.:
        prompt.advance_traffic_clock(112.)
    if receipt > 114.:
        prompt.advance_traffic_clock(114.)
    for service in (prompt, delayed):
        position(service, "A", 105.25, receipt, "OffTrack")
        position(service, "A", 105.25, receipt + .2)
        service.advance_traffic_clock(115.)
    assert business(prompt) == business(delayed)
    assert prompt._track_samples == delayed._track_samples
    assert prompt._physical_traffic._span_history == delayed._physical_traffic._span_history
    for cutoff in (112., 113., 114., 115.):
        assert spans_at(prompt, cutoff) == spans_at(delayed, cutoff)
    assert not prompt._pending_traffic and not delayed._pending_traffic


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("speed", [0., 10440.])
def test_supported_rejected_revision_removes_old_calibration(live, speed):
    _, service, key = field(live=live)
    service.advance_traffic_clock(114.)
    locked = tuple(service._track_samples)
    position(service, "A", 105.25, 115., speed=speed)
    assert key not in service._physical_traffic._span_history
    assert key in spans_at(service, 112.)
    assert service._physical_traffic._span_versions[key].versions[-1].validity == INVALIDATED
    assert tuple(service._track_samples) == locked


@pytest.mark.parametrize("live", [False, True])
def test_post_lock_valid_revision_changes_calibration_not_fitting(live):
    _, service, key = field(live=live)
    service.advance_traffic_clock(115.)
    locked = tuple(service._track_samples)
    position(service, "A", 105.25, 115.1, speed=720.)
    assert service._physical_traffic._span_history[key] == pytest.approx(1025.)
    assert spans_at(service, 112.)[key] == pytest.approx(1000.)
    assert tuple(service._track_samples) == locked


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_post_lock_invalidation_and_recovery_affect_later_independent_target(live, recover):
    service = calibrated_field(live)
    service.advance_traffic_clock(115.)
    locked = target(service)
    key = key_for(service, "A")
    position(service, "A", 105.25, 115.1, "OffTrack")
    assert key not in service._physical_traffic._span_history
    if recover:
        position(service, "A", 105.25, 115.2)
        assert key in service._physical_traffic._span_history
    later = add_later_target(service)
    assert target(service) == locked
    assert later.traffic_circuit_length_m == pytest.approx(1000. if recover else 1100.)
    assert later.physical_mean_ahead_gap_s == pytest.approx(2.90 if recover else 3.19)
    assert later.ahead_coverage == pytest.approx(15 / 21)
    assert later.clean_air_at_completion is (not recover)


@pytest.mark.parametrize("live", [False, True])
def test_state_only_negative_does_not_manufacture_distance(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    adapter = service._physical_traffic
    key = key_for(service, "A")
    count = adapter.row_count
    # The store has no speed supporting the distinct older 100.25 source
    # coordinate after its speed ring advanced to 110. Its state is still
    # authoritative, while it cannot add an integration row.
    service.controller.store._speed_history.clear()
    position(service, "A", 100.25, 113., "OffTrack")
    assert adapter.row_count == count
    assert key not in adapter._span_history
    position(service, "A", 100.25, 113.5)
    assert adapter.row_count == count
    assert adapter._span_history[key] == pytest.approx(1000.)


@pytest.mark.parametrize("live", [False, True])
def test_duplicate_negative_and_equal_receipt_distinct_corrections(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "A")
    position(service, "A", 105., 113., "OffTrack")
    adapter = service._physical_traffic
    version_count = len(adapter._span_versions[key].versions)
    sample_revision = service._track_samples_revision
    position(service, "A", 105., 113.1, "OffTrack")
    assert len(adapter._span_versions[key].versions) == version_count
    assert service._track_samples_revision == sample_revision
    position(service, "A", 105., 113.1)
    position(service, "A", 105., 113.1, "OffTrack")
    position(service, "A", 105., 113.1)
    assert len(adapter._span_versions[key].versions) == version_count + 3
    assert key in spans_at(service, 113.1)
    assert key not in spans_at(service, 113.)


@pytest.mark.parametrize("live", [False, True])
def test_terminal_withdrawal_cannot_recover_from_positive_status(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "C")
    withdraw_old(service, 113., conflict=True)
    position(service, "C", 85., 113.5)
    assert key not in service._physical_traffic._span_history
    assert service._physical_traffic._span_versions[key].versions[-1].retirement == "ownership"
    assert key in spans_at(service, 112.)


@pytest.mark.parametrize("live", [False, True])
def test_retention_loss_and_mapping_uncertainty_are_not_invalidation(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(115.)
    adapter = service._physical_traffic
    key = key_for(service, "A")
    before = adapter._span_history.copy()
    position(service, "A", 1000., 116., speed=360.)
    assert adapter._span_history == before
    assert adapter._span_versions[key].versions[-1].validity == VALID
    # Negative correction remains effective even beyond the acquisition age
    # gate. A later return cannot recover using the now-missing distance rows.
    position(service, "A", 105., 117., "OffTrack")
    assert key not in adapter._span_history
    position(service, "A", 105., 118.)
    assert key not in adapter._span_history
    assert adapter._span_versions[key].versions[-1].validity == INVALIDATED


@pytest.mark.parametrize("live", [False, True])
def test_pending_cutoff_survives_multiple_versions_under_history_pressure(live, monkeypatch):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "A")
    monkeypatch.setattr("woplanner.quali.physical_traffic.MAX_BOUNDARY_RECORDS", 2)
    for i in range(8):
        position(service, "A", 105., 113. + i / 20, "OffTrack" if i % 2 == 0 else "OnTrack")
    assert spans_at(service, 112.)[key] == pytest.approx(1000.)
    assert key not in spans_at(service, 113.)
    assert spans_at(service, 113.05)[key] == pytest.approx(1000.)
    assert key in service._physical_traffic._span_history
    assert len(service._physical_traffic._span_versions[key].versions) == 9
    assert key in service._pending_traffic


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("speed,correction_speed,expected", [(0., 720., 1000.), (10440., 0., 1337.5)])
def test_supported_range_rejection_can_recover_with_new_causal_rows(live, speed, correction_speed, expected):
    _, service, key = field(live=live)
    service.advance_traffic_clock(115.)
    locked = tuple(service._track_samples)
    position(service, "A", 105.25, 115.1, speed=speed)
    assert key not in service._physical_traffic._span_history
    position(service, "A", 105.125, 115.2, speed=correction_speed)
    assert service._physical_traffic._span_history[key] == pytest.approx(expected)
    assert key not in spans_at(service, 115.15)
    assert spans_at(service, 115.2)[key] == pytest.approx(expected)
    assert tuple(service._track_samples) == locked


@pytest.mark.parametrize("live", [False, True])
def test_nonoverlapping_physical_contributor_notifies_pending_target(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    old = target(service)
    key = key_for(service, "C")
    position(service, "C", 85., 113., "OffTrack")
    assert key not in service._physical_traffic._span_history
    assert target(service).traffic_circuit_length_m == pytest.approx(1000.)
    assert old.traffic_circuit_length_m == pytest.approx(1100.)
    assert spans_at(service, 112.)[key] == pytest.approx(1200.)
    assert (113., service._traffic_clock_kind) in service._physical_traffic.changed_calibration_clocks
    position(service, "C", 85., 113.5)
    assert service._physical_traffic._span_history[key] == pytest.approx(1200.)
    assert target(service).traffic_circuit_length_m == pytest.approx(1100.)


def test_recovery_preserves_original_order_and_capacity_retirement():
    from dataclasses import replace
    from test_quali_third_adversarial import _complete_trace
    from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
    adapter = RollingQualiPhysicalTraffic()
    raw, first = _complete_trace("0", 100., 360., 110.)
    adapter.ingest(raw)
    adapter.finalize("0", 2, evidence_deadline_s=112., clock_kind="receive")
    adapter.protect_calibration_cutoffs(((112., "receive"), (114., "receive")))
    adapter._record_state("0", 105., False, 113., "receive", "session")
    adapter._reconcile_contributions(113., "receive")
    assert first.event_key not in adapter._span_history
    evidence, completed = raw.traffic_evidence_sequence, raw.completed_lap_sequence
    for i in range(1, 33):
        raw, event = _complete_trace(str(i), 100., 360., 113., evidence, completed)
        adapter.ingest(raw)
        adapter.finalize(str(i), 2, evidence_deadline_s=113., clock_kind="receive")
        evidence, completed = raw.traffic_evidence_sequence, raw.completed_lap_sequence
    assert len(adapter._span_history) == 32
    order = adapter._span_versions[first.event_key].order
    adapter._record_state("0", 105., True, 113.5, "receive", "session")
    adapter._reconcile_contributions(113.5, "receive")
    record = adapter._span_versions[first.event_key]
    assert record.order == order
    assert record.versions[-1].retirement == "capacity"
    assert first.event_key not in adapter._span_history
    assert adapter._spans_at_deadline(112., "receive")[first.event_key] == pytest.approx(1000.)
    assert first.event_key not in adapter._spans_at_deadline(113., "receive")
    assert len(adapter._spans_at_deadline(113.5, "receive")) == 32
    adapter._record_state("0", 105., False, 113.6, "receive", "session")
    adapter._record_state("0", 105., True, 113.7, "receive", "session")
    adapter._reconcile_contributions(113.7, "receive")
    assert record.versions[-1].retirement == "capacity"


@pytest.mark.parametrize("live", [False, True])
def test_mapping_loss_without_specific_contradiction_preserves_certification(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    adapter = service._physical_traffic
    before = adapter._span_history.copy()
    # Loss of the independent source/session bridge is uncertainty, not a
    # specific withdrawal of a previously captured speed dependency.
    from wodata.live import LiveTimingMessage
    message = LiveTimingMessage("Position.z", {"Position": []}, EPOCH + timedelta(seconds=116.),
        EPOCH + timedelta(seconds=115.1), source="signalr_live" if live else "f1_archive_replay",
        session_time_seconds=115., playback_time_seconds=115.1,
        received_at_is_trusted=live, received_at_monotonic_s=115.1 if live else None)
    raw = service.controller.store.apply(message)
    service.consume(raw)
    assert raw.speed_coordinate_offset_s is None
    assert adapter._span_history == before
    assert all(record.versions[-1].validity == VALID for record in adapter._span_versions.values()
               if record.versions[-1].retirement is None)


def test_impossible_protected_budget_conflict_is_explicit_not_fifo_loss(monkeypatch):
    service = calibrated_field()
    service.advance_traffic_clock(112.)
    adapter = service._physical_traffic
    key = key_for(service, "A")
    before = spans_at(service, 112.)
    count = sum(len(record.versions) for record in adapter._span_versions.values())
    monkeypatch.setattr("woplanner.quali.physical_traffic.MAX_PROTECTED_SPAN_VERSIONS", count)
    with pytest.raises(OverflowError, match="protected calibration transitions"):
        position(service, "A", 105., 113., "OffTrack")
    assert spans_at(service, 112.) == before
    assert sum(len(record.versions) for record in adapter._span_versions.values()) == count
    assert key in spans_at(service, 112.)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("correction_coordinate", ["utc", "session"])
def test_verified_coordinate_switch_reconciles_same_source_identity(live, correction_coordinate):
    from wodata.live import LiveTimingMessage
    service = calibrated_field(live)
    service.advance_traffic_clock(112.)
    key = key_for(service, "A")
    utc = (EPOCH + timedelta(seconds=105.)).isoformat()
    def correction(receipt, status, coordinate):
        message = LiveTimingMessage("Position.z", {"Position": [{"Timestamp": utc,
            "Entries": {"A": {"Status": status, "X": 1, "Y": 1}}}]},
            EPOCH + timedelta(seconds=105.), EPOCH + timedelta(seconds=receipt),
            source="signalr_live" if live else "f1_archive_replay",
            session_time_seconds=105. if coordinate == "session" else None,
            playback_time_seconds=receipt, received_at_is_trusted=live,
            received_at_monotonic_s=receipt if live else None)
        raw = service.controller.store.apply(message)
        service.consume(raw)
        assert raw.position_observations[0].acquisition_coordinate == coordinate
    correction(113., "OffTrack", "utc")
    assert key not in service._physical_traffic._span_history
    assert key in spans_at(service, 112.)
    assert target(service).traffic_status != "ok"
    correction(113.5, "OnTrack", correction_coordinate)
    assert service._physical_traffic._span_history[key] == pytest.approx(1000.)
    assert key not in spans_at(service, 113.25)
    assert target(service).clean_air_at_completion is True
    assert len(service._physical_traffic._span_versions[key].versions) == 3


def source_packet(service, live, topic, payload, stamp, receipt, *, session=None):
    from wodata.live import LiveTimingMessage
    message = LiveTimingMessage(topic, payload, EPOCH + timedelta(seconds=stamp),
        EPOCH + timedelta(seconds=receipt), source="signalr_live" if live else "f1_archive_replay",
        session_time_seconds=session, playback_time_seconds=receipt,
        received_at_is_trusted=live, received_at_monotonic_s=receipt if live else None)
    raw = service.controller.store.apply(message)
    service.consume(raw)
    return raw


def utc_status(service, live, stamp, receipt, status):
    return source_packet(service, live, "Position.z", {"Position": [{
        "Timestamp": (EPOCH + timedelta(seconds=stamp)).isoformat(),
        "Entries": {"A": {"Status": status, "X": 1, "Y": 1}}}]}, stamp, receipt)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("stamp,expected", [(105., 1000.), (105.25, 1025.)])
def test_verified_bridge_preserves_first_distance_support_or_revises_distinct_row(live, stamp, expected):
    service = calibrated_field(live)
    service.advance_traffic_clock(115.)
    locked = tuple(service._track_samples)
    key = key_for(service, "A")
    source_packet(service, live, "CarData.z", {"Entries": [{
        "Utc": (EPOCH + timedelta(seconds=stamp)).isoformat(),
        "Cars": {"A": {"Channels": {"2": 720.}}}}]}, stamp, 115.1)
    utc_status(service, live, stamp, 115.2, "OnTrack")
    assert service._physical_traffic._span_history[key] == pytest.approx(expected)
    assert spans_at(service, 112.)[key] == pytest.approx(1000.)
    assert tuple(service._track_samples) == locked


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("stamp,has_proven_dependency", [(105., True), (105.25, False)])
def test_mapping_loss_distinguishes_proven_source_dependency_from_unknown_point(live, stamp, has_proven_dependency):
    service = calibrated_field(live)
    service.advance_traffic_clock(115.)
    key = key_for(service, "A")
    raw = source_packet(service, live, "Position.z", {"Position": []}, 116., 115.1, session=115.)
    assert raw.speed_coordinate_offset_s is None
    utc_status(service, live, stamp, 115.2, "OffTrack")
    assert (key not in service._physical_traffic._span_history) is has_proven_dependency
    utc_status(service, live, stamp, 115.3, "OnTrack")
    assert service._physical_traffic._span_history[key] == pytest.approx(1000.)


@pytest.mark.parametrize("live", [False, True])
def test_blocker_dependency_survives_state_retention_without_inventing_distance(live):
    service = calibrated_field(live)
    service.advance_traffic_clock(115.)
    key = key_for(service, "A")
    utc_status(service, live, 105., 115.1, "OffTrack")
    adapter = service._physical_traffic
    assert key not in adapter._span_history
    source_packet(service, live, "Position.z", {"Position": []}, 116., 115.2, session=115.)
    # Exercise state-cache expiry while the independently captured integration
    # rows remain retained; dependency provenance lives on the contribution.
    foreign = [state for state in adapter._candidate_states["A"] if state.get("_AcquisitionCoordinate") == "utc"]
    for state in foreign:
        adapter._forget_state("A", state)
        adapter._candidate_states["A"].remove(state)
    before_rows = adapter.row_count
    utc_status(service, live, 105., 115.3, "OnTrack")
    assert adapter._span_history[key] == pytest.approx(1000.)
    assert adapter.row_count == before_rows
    assert len(adapter._state_latest["A"]) <= 2 * len(adapter._candidate_states["A"])


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("receipt", [113., 114.1])
def test_coordinate_corrections_have_same_locked_business_under_callback_schedules(live, receipt):
    prompt, delayed = calibrated_field(live), calibrated_field(live)
    prompt.advance_traffic_clock(112.)
    if receipt > 114.:
        prompt.advance_traffic_clock(114.)
    for service in (prompt, delayed):
        utc_status(service, live, 105., receipt, "OffTrack")
        position(service, "A", 105., receipt + .2)
        service.advance_traffic_clock(116.)
    assert business(prompt) == business(delayed)
    assert prompt._track_samples == delayed._track_samples
    assert prompt._physical_traffic._span_history == delayed._physical_traffic._span_history
    for cutoff in (112., receipt, receipt + .2, 116.):
        assert spans_at(prompt, cutoff) == spans_at(delayed, cutoff)


@pytest.mark.parametrize("live", [False, True])
def test_negative_before_admission_and_during_grace_give_same_later_independent_result(live):
    early, late = calibrated_field(live), calibrated_field(live)
    early_key = next(key for key in early._pending_traffic if key[1] == "A")
    position(early, "A", 105.25, 111.5, "OffTrack")
    early.advance_traffic_clock(115.)
    assert early_key not in early._physical_traffic._admitted_completion_keys
    late.advance_traffic_clock(112.)
    late_key = key_for(late, "A")
    position(late, "A", 105.25, 113., "OffTrack")
    late.advance_traffic_clock(115.)
    assert late_key not in late._physical_traffic._span_history
    earlier, later = add_later_target(early), add_later_target(late)
    assert earlier == later
    assert later.traffic_circuit_length_m == pytest.approx(1100.)
    assert later.physical_mean_ahead_gap_s == pytest.approx(3.19)
    assert later.ahead_coverage == pytest.approx(15 / 21)
    assert later.clean_air_at_completion is True


@pytest.mark.parametrize("live", [False, True])
def test_shared_calibration_invalidation_reaches_multiple_pending_targets_without_new_samples(live):
    service = calibrated_field(live, b_start=101., b_sector=3)
    service.advance_traffic_clock(112.)
    assert {key[1] for key in service._pending_traffic} == {"A", "B"}
    count = len(service._track_samples)
    position(service, "C", 85., 112.5, "OffTrack")
    assert target(service).traffic_circuit_length_m == pytest.approx(1000.)
    service.advance_traffic_clock(113.)
    b = next(sample for sample in service._track_samples if sample.driver == "B")
    assert b.traffic_circuit_length_m == pytest.approx(1000.)
    position(service, "C", 85., 113.5)
    assert len(service._track_samples) == count
    assert service._physical_traffic._span_history[key_for(service, "C")] == pytest.approx(1200.)
    service.advance_traffic_clock(116.)
    assert not service._pending_traffic
    assert target(service).traffic_circuit_length_m == b.traffic_circuit_length_m == pytest.approx(1000.)


def test_unclocked_legacy_admission_uses_delivery_order_not_acquisition_seconds():
    from test_quali_rolling_phase_validity import ingest_field
    adapter = ingest_field()
    traffic = adapter.finalize("A", 1)
    assert traffic.available
    key = next(iter(adapter._span_history))
    version = adapter._span_versions[key].versions[0]
    assert version.domain == "ingest_order"
    assert version.clock == adapter._legacy_ingest_ordinal
    assert version.clock != adapter._span_versions[key].bounds[1]
    assert key not in adapter._spans_at_deadline(version.clock, "receive")
    assert key not in adapter._spans_at_deadline(version.clock - 1, "ingest_order")
    assert adapter._spans_at_deadline(version.clock, "ingest_order")[key] == pytest.approx(1000.)
