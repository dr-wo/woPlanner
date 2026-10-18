"""Independent C1 counterexamples: real reducer, service and physical evaluator."""
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import FastF1LiveTimingSource, LiveTimingMessage, QualiSessionStore
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.service import QualiLiveService


EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def packet(store, service, topic, payload, acquired, received=None):
    received = acquired if received is None else received
    live = getattr(service, "_test_live_receipts", False)
    message = LiveTimingMessage(
        topic, payload, EPOCH + timedelta(seconds=acquired),
        EPOCH + timedelta(seconds=received), source="signalr_live" if live else "f1_archive_replay",
        session_time_seconds=acquired, playback_time_seconds=received,
        received_at_is_trusted=live,
    )
    if live:
        # Also exercise the frozen runtime which predates the typed field.
        object.__setattr__(message, "received_at_monotonic_s", received)
    raw = store.apply(message)
    service.consume(raw)
    return raw


def timing(store, service, number, patch, acquired, received=None):
    return packet(store, service, "TimingData", {"Lines": {number: patch}}, acquired, received)


def field(*, b_complete=True, live=False):
    service = QualiLiveService()
    store = service.controller.store
    if live:
        service.controller._source = FastF1LiveTimingSource()
        service._test_live_receipts = True
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    packet(store, service, "DriverList", {number: {"Tla": number} for number in "AB"}, 90.)
    for half in range(200, 223):
        stamp = half / 2.
        for number, start in (("A", 100.), ("B", 101.)):
            if stamp == start:
                timing(store, service, number, {"NumberOfLaps": 1, "InPit": False}, stamp)
            if not start <= stamp <= start + 10:
                continue
            utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
            packet(store, service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
                number: {"Channels": {"2": 360., "0": 12000}},
            }}]}, stamp)
            packet(store, service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                number: {"X": 100, "Y": 100, "Status": "OnTrack"},
            }}]}, stamp)
            if stamp == start + 3:
                timing(store, service, number, {"Sectors": {"0": {"Value": "0:03.000"}}}, stamp)
            if stamp == start + 10 and (number != "B" or b_complete):
                timing(store, service, number, {"NumberOfLaps": 2,
                       "LastLapTime": {"Value": "0:10.000"}}, stamp)
    assert len(service._track_samples) == (2 if b_complete else 1)
    key = next(key for key in service._pending_traffic if key[1] == "A")
    return store, service, key


def heartbeat(store, service, stamp):
    return packet(store, service, "Heartbeat", {}, stamp)


def target(service):
    return next(sample for sample in service._track_samples if sample.driver == "A")


def publications(service, key):
    return [record for record in service.traffic_finalisation_records if record["key"] == key]


def test_missing_competitor_end_anchor_revises_same_sample_and_cache():
    store, service, key = field(b_complete=False)
    heartbeat(store, service, 112.)
    assert target(service).traffic_status != "ok"
    before_cache = service.track_evolution_diagnostic()
    before_count, before_revision = len(service._track_samples), service._track_samples_revision
    timing(store, service, "B", {"NumberOfLaps": 2,
                               "LastLapTime": {"Value": "0:10.000"}}, 111., 113.)
    direct = service._physical_traffic.finalize("A", 2)
    assert direct.available
    assert direct.mean_ahead_gap_s == pytest.approx(9.)
    assert direct.ahead_coverage == pytest.approx(19 / 21)
    assert target(service).traffic_status == "ok"
    assert target(service).physical_mean_ahead_gap_s == pytest.approx(9.)
    assert service._track_samples[0].driver == "A"
    assert len(service._track_samples) == before_count + 1  # B's own valid completion.
    assert service._track_samples_revision > before_revision
    assert service.track_evolution_diagnostic() is not before_cache
    assert publications(service, key)[-1]["reason"] == "exceptional grace"


@pytest.mark.parametrize("number", ["A", "B"])
def test_explicit_completion_revocation_removes_previously_supported_result(number):
    store, service, key = field()
    heartbeat(store, service, 112.)
    assert target(service).traffic_status == "ok"
    assert target(service).clean_air_at_completion is True
    before_length = len(service._track_samples)
    raw = timing(store, service, number, {"NumberOfLaps": 2, "LastLapTime": {"Value": ""}},
                 110. if number == "A" else 111., 113.)
    revoked = [event.completion_key for event in raw.traffic_evidence_sequence
               if event.kind == "completion_invalidated"]
    assert revoked
    if number == "A":
        assert service._physical_traffic.completion_bounds(key) is None
    assert not service._physical_traffic.finalize("A", 2).available
    assert target(service).traffic_status != "ok"
    assert target(service).clean_air_at_completion is None
    assert len(service._track_samples) == before_length


def test_repeated_boundary_and_unrelated_driver_patch_do_not_republish_target():
    store, service, key = field()
    heartbeat(store, service, 112.)
    heartbeat(store, service, 113.)  # Also publish B's independent normal deadline.
    records = len(publications(service, key))
    revision = service._track_samples_revision
    timing(store, service, "B", {"NumberOfLaps": 2,
                               "LastLapTime": {"Value": "0:10.000"}}, 111., 113.1)
    timing(store, service, "D", {"NumberOfLaps": 1, "InPit": True}, 500., 113.2)
    assert len(publications(service, key)) == records
    assert service._track_samples_revision == revision


def test_conflicting_competitor_boundary_withdraws_support():
    store, service, key = field()
    heartbeat(store, service, 112.)
    assert target(service).traffic_status == "ok"
    timing(store, service, "B", {"NumberOfLaps": 3,
                               "LastLapTime": {"Value": "0:10.000"}}, 111., 113.)
    assert not service._physical_traffic.finalize("A", 2).available
    assert target(service).traffic_status != "ok"
    assert publications(service, key)[-1]["reason"] == "exceptional grace"


@pytest.mark.parametrize("received", [114.001, 200.])
def test_completion_revocation_after_hard_lock_preserves_published_sample(received):
    store, service, key = field()
    heartbeat(store, service, 112.)
    heartbeat(store, service, 114.)
    assert key not in service._pending_traffic
    before = target(service)
    timing(store, service, "B", {"NumberOfLaps": 2, "LastLapTime": {"Value": ""}}, 111., received)
    assert not service._physical_traffic.finalize("A", 2).available
    assert target(service) == before


@pytest.mark.parametrize("number", ["A", "B"])
def test_sparse_negative_evidence_preserves_normal_cutoff_then_revises_in_grace(number):
    store, service, key = field()
    assert target(service).traffic_status == "pending"
    timing(store, service, number, {"NumberOfLaps": 2, "LastLapTime": {"Value": ""}},
           110. if number == "A" else 111., 113.)
    records = publications(service, key)
    assert records[0]["delay_s"] == 2
    assert records[0]["status"] == "ok"  # +3 revocation did not exist at +2.
    assert records[1]["delay_s"] == 3
    assert records[1]["status"] != "ok"
    assert target(service).traffic_status != "ok"
