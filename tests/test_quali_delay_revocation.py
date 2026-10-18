"""C5: explicit withdrawal changes warning truth without reopening traffic."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wodata.live import LiveTimingMessage, QualiSessionStore
from wodata.live.quali_models import QualiPositionObservation
from woplanner.quali.service import QualiLiveService
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.traffic_finalisation import TelemetryDelayMonitor

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEYS = [("Q1", str(n), 2, 110.) for n in range(1, 4)]


def packet(now, samples=(), coordinate=None):
    return SimpleNamespace(receive_time_trusted=True, traffic_clock_time_s=now,
        position_observations=tuple(QualiPositionObservation(number, stamp, "OnTrack",
            acquisition_coordinate=coordinate) for number, stamp in samples), speed_observations=())


def adapter_for(keys):
    return SimpleNamespace(completion_bounds=lambda key: (100., key[3]),
        completion_coordinate=lambda key: "session",
        completed_lap_for_sample=lambda number, stamp, coordinate=None: keys.get(number))


def late_monitor():
    monitor = TelemetryDelayMonitor()
    for key in KEYS:
        monitor.completed(key, 110., True)
    adapter = adapter_for({key[1]: key for key in KEYS})
    monitor.observe(packet(113., [(key[1], 105.) for key in KEYS]), adapter)
    assert monitor.count == 3
    assert monitor.warning
    return monitor, adapter


def test_revoke_clears_warning_preserves_dedup_and_blocks_retransmission():
    monitor, adapter = late_monitor()
    identities = dict(monitor.first_receipts)
    monitor.revoke(KEYS[0])
    assert KEYS[0] not in monitor.completions
    assert KEYS[0] not in monitor.late_laps
    assert monitor.count == 2
    assert not monitor.warning
    assert monitor.first_receipts == identities
    for now in (114., 115., 116.):
        monitor.revoke(KEYS[0])
        monitor.completed(KEYS[0], now, True)
        monitor.observe(packet(now + 3, [("1", 105.), ("1", 106.)]), adapter)
    assert monitor.count == 2
    assert KEYS[0] not in monitor.completions
    assert monitor.observed_late_samples == 3


def test_corrected_different_completion_key_accepts_only_new_observations():
    monitor, adapter = late_monitor()
    monitor.revoke(KEYS[0])
    corrected = (*KEYS[0][:3], 110.1)
    monitor.completed(corrected, 114., True)
    adapter.completed_lap_for_sample = lambda number, *_: corrected if number == "1" else KEYS[int(number)-1]
    monitor.observe(packet(117., [("1", 105.)]), adapter)
    assert monitor.count == 2  # Original receipt cannot become new latency.
    monitor.observe(packet(117., [("1", 106.)]), adapter)
    assert monitor.count == 3
    assert corrected in monitor.late_laps
    assert KEYS[0] not in monitor.late_laps
    assert monitor.warning


def test_routine_completion_retention_does_not_withdraw_recent_late_warning():
    monitor, _ = late_monitor()
    # Completion at 110 has expired, but its observed delay at 113 has not.
    monitor.advance(710.1)
    assert not monitor.completions
    assert monitor.count == 3
    assert monitor.warning
    monitor.advance(713.)
    assert monitor.count == 3  # Inclusive ten-minute observed window.
    monitor.advance(713.001)
    assert monitor.count == 0
    assert not monitor.warning


def test_bounded_revocations_do_not_reanimate_evicted_identity(monkeypatch):
    import woplanner.quali.traffic_finalisation as module
    monkeypatch.setattr(module, "MAX_COMPLETION_RECORDS", 3)
    monitor = TelemetryDelayMonitor(threshold=1)
    for lap in range(2, 12):
        key = ("Q1", "1", lap, float(lap * 10))
        monitor.completed(key, float(lap), True)
        monitor.revoke(key)
    assert len(monitor._revoked_completions) <= 3
    oldest = ("Q1", "1", 2, 20.)
    monitor.completed(oldest, 100., True)
    monitor.observe(packet(103., [("1", 15.)]), adapter_for({"1": oldest}))
    assert not monitor.count
    assert oldest not in monitor.completions
    # Recent legitimate corrections remain separately identifiable.
    corrected = ("Q1", "1", 11, 110.1)
    monitor.completed(corrected, 104., True)
    monitor.observe(packet(107., [("1", 109.)]), adapter_for({"1": corrected}))
    assert monitor.count == 1
    assert len(monitor._forgotten_revocation_lap_floor) <= module.MAX_DEDUP_STREAMS


def test_coordinate_specific_receipt_identity_and_eviction_floor(monkeypatch):
    import woplanner.quali.traffic_finalisation as module
    monkeypatch.setattr(module, "MAX_RECEIPT_IDENTITIES", 1)
    monitor = TelemetryDelayMonitor(threshold=1)
    key = KEYS[0]
    monitor.completed(key, 110., True)
    adapter = adapter_for({"1": key})
    # An unrelated UTC stream must neither deduplicate nor poison session time.
    monitor.observe(packet(111., [("1", 1767225701.)], "utc"), adapter_for({}))
    monitor.observe(packet(113., [("1", 105.)], "session"), adapter)
    assert monitor.count == 1
    assert ("position", "1", 105., "session") in monitor.first_receipts
    assert ("position", "1", "utc") in monitor._forgotten_acquisition_floor


def test_early_receipt_association_respects_acquisition_domain():
    monitor = TelemetryDelayMonitor(threshold=1)
    adapter = adapter_for({})
    monitor.observe(packet(109., [("1", 105.)], "utc"), adapter)
    monitor.completed(KEYS[0], 110., True)
    adapter = adapter_for({"1": KEYS[0]})
    monitor.observe(packet(110.), adapter)
    assert not monitor._associated_receipts


def test_actual_reducer_service_revocation_after_hard_lock():
    store, service = QualiSessionStore(), QualiLiveService()
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    def send(topic, payload, acquisition, receipt):
        message = LiveTimingMessage(topic, payload,
            EPOCH + timedelta(seconds=acquisition), EPOCH + timedelta(seconds=receipt),
            source="synthetic_test", session_time_seconds=acquisition,
            received_at_is_trusted=True)
        raw = store.apply(message)
        service.consume(raw)
        return raw
    send("DriverList", {n: {"Tla": "D" + n} for n in "123"}, 90., 90.)
    for n in "123":
        send("TimingData", {"Lines": {n: {"NumberOfLaps": 1, "InPit": False}}}, 100., 100.)
    for n in "123":
        send("TimingData", {"Lines": {n: {"Sectors": {"0": {"Value": "0:03.000"}}}}}, 103., 103.)
    for n in "123":
        send("TimingData", {"Lines": {n: {"NumberOfLaps": 2,
             "LastLapTime": {"Value": "0:10.000"}}}}, 110., 110.)
    assert len(service._track_samples) == 3
    assert len(service._pending_traffic) == 3
    send("Position.z", {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=105.)).isoformat(),
        "Entries": {n: {"X": 100, "Y": 100, "Status": "OnTrack"} for n in "123"}}]}, 113., 113.)
    assert service.telemetry_delays.count == 3
    assert service.snapshot.telemetry_delay_warning
    service.advance_traffic_clock(EPOCH.timestamp() + 114.)
    assert not service._pending_traffic
    before_samples = tuple(service._track_samples)
    raw = send("TimingData", {"Lines": {"1": {"NumberOfLaps": 2,
        "LastLapTime": {"Value": ""}}}}, 110.1, 115.)
    revoked = next(event.completion_key for event in raw.traffic_evidence_sequence
                   if event.kind == "completion_invalidated")
    assert service._physical_traffic.completion_bounds(revoked) is None
    assert service.telemetry_delays.count == 2
    assert not service.snapshot.telemetry_delay_warning
    assert revoked not in service.telemetry_delays.completions
    assert tuple(service._track_samples) == before_samples
    send("Heartbeat", {}, 116., 116.)
    assert service.telemetry_delays.count == 2


def test_revocation_before_completion_or_after_expiry_still_blocks_identity():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.revoke(KEYS[0])
    monitor.completed(KEYS[0], 110., True)
    monitor.observe(packet(113., [("1", 105.)]), adapter_for({"1": KEYS[0]}))
    assert not monitor.count
    monitor.advance(1000.)
    assert not monitor.first_receipts
    monitor.completed(KEYS[0], 1000., True)
    monitor.observe(packet(1003., [("1", 105.), ("1", 106.)]), adapter_for({"1": KEYS[0]}))
    assert not monitor.completions
    assert not monitor.count


def test_explicit_withdrawal_removes_warning_even_after_completion_retention():
    monitor, _ = late_monitor()
    monitor.advance(711.)
    assert not monitor.completions
    assert monitor.count == 3
    monitor.revoke(KEYS[0])
    assert monitor.count == 2
    assert not monitor.warning


def test_revocation_stream_overflow_is_bounded_and_conservative(monkeypatch):
    import woplanner.quali.traffic_finalisation as module
    monkeypatch.setattr(module, "MAX_COMPLETION_RECORDS", 2)
    monkeypatch.setattr(module, "MAX_DEDUP_STREAMS", 2)
    monitor = TelemetryDelayMonitor(threshold=1)
    for number in range(20):
        monitor.revoke(("Q1", str(number), 2, 110.))
    assert len(monitor._revoked_completions) == 2
    assert len(monitor._forgotten_revocation_lap_floor) == 2
    assert monitor._revocation_stream_capacity_exceeded
    monitor.completed(KEYS[0], 110., True)
    monitor.observe(packet(113., [("1", 105.)]), adapter_for({"1": KEYS[0]}))
    assert not monitor.count


def test_validated_mapping_alias_keeps_first_receipt_even_if_mapping_is_later_unavailable():
    monitor = TelemetryDelayMonitor(threshold=1)
    adapter = adapter_for({})
    utc_stamp = EPOCH.timestamp() + 105.
    monitor.observe(packet(109., [("1", utc_stamp)], "utc"), adapter)
    monitor.completed(KEYS[0], 110., True)
    adapter = adapter_for({"1": KEYS[0]})
    mapped = packet(110.)
    mapped.speed_coordinate_offset_s = EPOCH.timestamp()
    monitor.observe(mapped, adapter)
    assert ("position", "1", utc_stamp, "utc") in monitor._associated_receipts
    mapped = packet(113., [("1", 105.)], "session")
    mapped.speed_coordinate_offset_s = EPOCH.timestamp()
    monitor.observe(mapped, adapter)
    assert monitor.first_receipts[("position", "1", 105., "session")] == 109.
    assert not monitor.count
    monitor.observe(packet(114., [("1", 105.)], "session"), adapter)
    assert not monitor.count
    monitor.observe(packet(114.1, [("1", 106.)], "session"), adapter)
    assert monitor.count == 1


def test_validated_mapping_respects_forgotten_alias_acquisition_floor(monkeypatch):
    import woplanner.quali.traffic_finalisation as module
    monkeypatch.setattr(module, "MAX_RECEIPT_IDENTITIES", 1)
    monitor = TelemetryDelayMonitor(threshold=1)
    utc_stamp = EPOCH.timestamp() + 105.
    monitor.observe(packet(109., [("1", utc_stamp), ("1", utc_stamp + 1)], "utc"), adapter_for({}))
    monitor.completed(KEYS[0], 110., True)
    mapped = packet(113., [("1", 105.)], "session")
    mapped.speed_coordinate_offset_s = EPOCH.timestamp()
    monitor.observe(mapped, adapter_for({"1": KEYS[0]}))
    assert not monitor.count
    assert len(monitor.first_receipts) <= 1
    assert len(monitor._receipt_expiry_order) <= 2


def test_new_late_observation_uses_only_validated_unique_coordinate_bridge():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.completed(KEYS[0], 110., True)
    adapter = SimpleNamespace(completion_bounds=lambda key: (100., 110.),
        completion_coordinate=lambda key: "session",
        completed_lap_for_sample=lambda number, stamp, coordinate:
            KEYS[0] if coordinate == "session" and 100. < stamp <= 110. else None)
    raw = packet(113., [("1", EPOCH.timestamp() + 105.)], "utc")
    # Unmapped domains cannot create a measured delay.
    monitor.observe(raw, adapter)
    assert not monitor.count
    raw = packet(113.1, [("1", EPOCH.timestamp() + 106.)], "utc")
    raw.speed_coordinate_offset_s = EPOCH.timestamp()
    monitor.observe(raw, adapter)
    assert monitor.count == 1
    assert abs(monitor.maximum_delay_s - 3.1) < 1e-9


def test_coordinate_bridge_with_two_different_completion_owners_is_unmeasured():
    monitor = TelemetryDelayMonitor(threshold=1)
    other = (*KEYS[0][:3], EPOCH.timestamp() + 110.)
    for key in (KEYS[0], other):
        monitor.completed(key, 110., True)
    adapter = SimpleNamespace(completion_bounds=lambda key: (100., 110.),
        completion_coordinate=lambda key: "session",
        completed_lap_for_sample=lambda number, stamp, coordinate:
            KEYS[0] if coordinate == "session" else other)
    raw = packet(113., [("1", EPOCH.timestamp() + 105.)], "utc")
    raw.speed_coordinate_offset_s = EPOCH.timestamp()
    monitor.observe(raw, adapter)
    assert not monitor.count
