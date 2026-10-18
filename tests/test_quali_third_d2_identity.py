"""D2 controls: source microsecond precision, real reducer and warning lifecycle."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wodata.live import FastF1LiveTimingSource, LiveTimingMessage
from wodata.live.quali_models import QualiPositionObservation, QualiSpeedObservation
from woplanner.quali.service import QualiLiveService
from woplanner.quali.traffic_finalisation import TelemetryDelayMonitor

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
OFFSET = EPOCH.timestamp()
STAMP = 105.26


def adapter(keys):
    return SimpleNamespace(
        completion_bounds=lambda key: (100., 110.),
        completion_coordinate=lambda key: "session",
        completed_lap_for_sample=lambda number, stamp, coordinate=None:
            keys.get(number) if coordinate in (None, "session") and 100 < stamp <= 110 else None,
    )


def raw(now, stamps, coordinate, *, offset=OFFSET, kind="position"):
    rows = tuple(QualiPositionObservation(n, t, "OnTrack", acquisition_coordinate=coordinate)
                 for n, t in stamps)
    speeds = tuple(QualiSpeedObservation(n, t, 360., acquisition_coordinate=coordinate)
                   for n, t in stamps)
    return SimpleNamespace(receive_time_trusted=True, traffic_clock_time_s=now,
                           speed_coordinate_offset_s=offset,
                           position_observations=rows if kind == "position" else (),
                           speed_observations=speeds if kind == "speed" else ())


@pytest.mark.parametrize("first_coordinate", ["session", "utc"])
@pytest.mark.parametrize("kind", ["position", "speed"])
def test_fractional_retransmission_both_directions_preserves_first_receipt(first_coordinate, kind):
    monitor = TelemetryDelayMonitor()
    keys = {n: ("Q1", n, 2, 110.) for n in "ABC"}
    physical = adapter(keys)
    first = STAMP if first_coordinate == "session" else OFFSET + STAMP
    other = "utc" if first_coordinate == "session" else "session"
    # Session value emitted by real reducer after subtracting the UTC epoch.
    second = OFFSET + STAMP if other == "utc" else (OFFSET + STAMP) - OFFSET
    assert second != STAMP if other == "session" else True
    monitor.observe(raw(105.5, [(n, first) for n in keys], first_coordinate, kind=kind), physical)
    for key in keys.values():
        monitor.completed(key, 110., True)
    for receipt in (113., 113.5, 114.):
        monitor.observe(raw(receipt, [(n, second) for n in keys], other, kind=kind), physical)
        monitor.observe(raw(receipt, [(n, first) for n in keys], first_coordinate, kind=kind), physical)
    assert monitor.observed_late_samples == monitor.count == 0
    assert monitor.warning == ""
    assert set(monitor.first_receipts.values()) == {105.5}


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("first_coordinate", ["utc", "session"])
def test_real_reducer_service_fractional_mapping_switch_does_not_warn(native, first_coordinate):
    service = QualiLiveService()
    if native:
        service.controller._source = FastF1LiveTimingSource()
    store = service.controller.store

    def packet(topic, payload, receipt, session=None, source=None):
        msg = LiveTimingMessage(topic, payload,
            EPOCH + timedelta(seconds=source) if source is not None else None,
            EPOCH + timedelta(seconds=receipt), source="signalr_live" if native else "replay",
            received_at_is_trusted=True, session_time_seconds=session,
            received_at_monotonic_s=receipt if native else None)
        snapshot = store.apply(msg)
        service.consume(snapshot)
        return snapshot

    packet("DriverList", {n: {"Tla": n} for n in "ABC"}, 90., 90., 90.)
    packet("TimingData", {"Lines": {n: {"NumberOfLaps": 1, "InPit": False}
                                    for n in "ABC"}}, 100., 100., 100.)
    payload = {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=STAMP)).isoformat(),
                            "Entries": {n: {"X": 1, "Y": 2, "Status": "OnTrack"}
                                        for n in "ABC"}}]}
    initial = packet("Position.z", payload, 105.5,
                     STAMP if first_coordinate == "session" else None,
                     STAMP if first_coordinate == "session" else None)
    assert initial.position_observations[0].acquisition_coordinate == first_coordinate
    if first_coordinate == "utc":
        packet("ExtrapolatedClock", {"Utc": (EPOCH + timedelta(seconds=106.)).isoformat(),
                                     "Remaining": "10:00"}, 106., 106.)
    packet("TimingData", {"Lines": {n: {"NumberOfLaps": 2,
                                       "LastLapTime": {"Value": "0:10.000"}}
                                    for n in "ABC"}}, 110., 110., 110.)
    repeated = packet("Position.z", payload, 113.)
    assert repeated.position_observations[0].acquisition_coordinate != first_coordinate
    assert repeated.position_observations[0].session_time_s != STAMP
    assert service.telemetry_delays.count == service.telemetry_delays.observed_late_samples == 0
    assert service.snapshot.telemetry_delay_warning == ""
    fresh = {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=STAMP + .000001)).isoformat(),
                          "Entries": {n: {"X": 1, "Y": 2, "Status": "OnTrack"}
                                      for n in "ABC"}}]}
    packet("Position.z", fresh, 113.1)
    assert service.telemetry_delays.count == 3
    assert service.telemetry_delays.observed_late_samples == 3
    assert "3 laps exceeded 2s" in service.snapshot.telemetry_delay_warning


def test_valid_mapping_after_first_receipt_and_mapping_loss_keep_proven_alias():
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    monitor.observe(raw(105.5, [("A", OFFSET + STAMP)], "utc", offset=None), physical)
    monitor.completed(key, 110., True)
    monitor.observe(raw(113., [("A", (OFFSET + STAMP) - OFFSET)], "session"), physical)
    assert set(monitor.first_receipts.values()) == {105.5}
    monitor.observe(raw(114., [("A", STAMP)], "session", offset=None), physical)
    assert monitor.count == 0
    # An unavailable/contradictory mapping cannot establish a fresh alias.
    monitor.observe(raw(114., [("A", STAMP + .000001)], "session", offset=None), physical)
    assert monitor.count == 1


@pytest.mark.parametrize("offset", [None, float("nan"), float("inf")])
def test_invalid_mapping_does_not_create_unsupported_alias(offset):
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    monitor.observe(raw(105.5, [("A", OFFSET + STAMP)], "utc", offset=offset), physical)
    monitor.completed(key, 110., True)
    monitor.observe(raw(113., [("A", STAMP)], "session", offset=offset), physical)
    assert monitor.count == 1
    assert set(monitor.first_receipts.values()) == {105.5, 113.}


@pytest.mark.parametrize("coordinate", ["session", "utc"])
def test_distinct_microsecond_samples_remain_distinct(coordinate):
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    start = STAMP if coordinate == "session" else OFFSET + STAMP
    monitor.observe(raw(105.5, [("A", start)], coordinate), physical)
    monitor.completed(key, 110., True)
    monitor.observe(raw(113., [("A", start + .000001)], coordinate), physical)
    assert monitor.observed_late_samples == monitor.count == 1
    assert len(monitor.first_receipts) == 2


def test_coordinate_alias_survives_bounded_retention_and_completion_withdrawal(monkeypatch):
    monkeypatch.setattr("woplanner.quali.traffic_finalisation.MAX_RECEIPT_IDENTITIES", 4)
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    monitor.observe(raw(105.5, [("A", STAMP)], "session"), physical)
    monitor.completed(key, 110., True)
    for index in range(6):
        monitor.observe(raw(111. + index / 10, [("Z", 200. + index)], "session"), physical)
    monitor.observe(raw(113., [("A", OFFSET + STAMP)], "utc"), physical)
    assert monitor.count == 0
    assert len(monitor.first_receipts) <= 4
    assert len(monitor._receipt_expiry_order) <= 8
    monitor.revoke(key)
    corrected = ("Q1", "A", 2, 110.1)
    physical = adapter({"A": corrected})
    monitor.completed(corrected, 113.2, True)
    monitor.observe(raw(116., [("A", STAMP)], "session"), physical)
    assert monitor.count == 0
    monitor.observe(raw(116.1, [("A", STAMP + .000001)], "session"), physical)
    assert monitor.count == 1
    monitor.revoke(corrected)
    assert monitor.count == 0


@pytest.mark.parametrize("offset_fraction", [.000001, .123456, .999999])
@pytest.mark.parametrize("first_coordinate", ["session", "utc"])
def test_fractional_verified_bridge_preserves_microsecond_identity(offset_fraction, first_coordinate):
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    offset = OFFSET + offset_fraction
    utc = OFFSET + offset_fraction + STAMP
    first = STAMP if first_coordinate == "session" else utc
    other = "utc" if first_coordinate == "session" else "session"
    second = utc if other == "utc" else utc - offset
    monitor.observe(raw(105.5, [("A", first)], first_coordinate, offset=offset), physical)
    monitor.completed(key, 110., True)
    monitor.observe(raw(113., [("A", second)], other, offset=offset), physical)
    assert monitor.count == 0
    assert set(monitor.first_receipts.values()) == {105.5}
    # A whole source tick is not a precision alias.
    newer = utc + .000001 if other == "utc" else STAMP + .000001
    monitor.observe(raw(113.1, [("A", newer)], other, offset=offset), physical)
    assert monitor.count == 1


def test_real_reducer_contradictory_mapping_is_not_used_for_aliases():
    service = QualiLiveService(telemetry_delay_lap_threshold=1)
    store = service.controller.store

    def packet(topic, payload, receipt, session=None, source=None):
        message = LiveTimingMessage(topic, payload,
            EPOCH + timedelta(seconds=source) if source is not None else None,
            EPOCH + timedelta(seconds=receipt), source="replay", received_at_is_trusted=True,
            session_time_seconds=session)
        snapshot = store.apply(message)
        service.consume(snapshot)
        return snapshot

    packet("DriverList", {"A": {"Tla": "A"}}, 90., 90., 90.)
    packet("TimingData", {"Lines": {"A": {"NumberOfLaps": 1, "InPit": False}}}, 100., 100., 100.)
    initial = {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=STAMP)).isoformat(),
                            "Entries": {"A": {"X": 1, "Y": 2, "Status": "OnTrack"}}}]}
    first = packet("Position.z", initial, 105.5, STAMP, STAMP)
    assert first.speed_coordinate_offset_s == OFFSET
    packet("TimingData", {"Lines": {"A": {"NumberOfLaps": 2,
                                            "LastLapTime": {"Value": "0:10.000"}}}}, 110., 110., 110.)
    # Contradicting paired coordinates disable the independent mapping.
    broken = packet("Position.z", {"Position": []}, 112., 111., 112.)
    assert broken.speed_coordinate_offset_s is None
    assert service.telemetry_delays._coordinate_offset_s is None
    repeated = packet("Position.z", initial, 113.)
    assert repeated.position_observations[0].acquisition_coordinate == "utc"
    assert service.telemetry_delays.count == 0  # Unknown ownership cannot warn.
    assert set(service.telemetry_delays.first_receipts.values()) == {OFFSET + 105.5, OFFSET + 113.}


def test_source_timestamp_precision_survives_millisecond_warning_boundary():
    monitor = TelemetryDelayMonitor(threshold=1)
    key = ("Q1", "A", 2, 110.)
    physical = adapter({"A": key})
    monitor.completed(key, 110., True)
    monitor.observe(raw(112., [("A", STAMP)], "session"), physical)
    monitor.observe(raw(112.000001, [("A", STAMP + .000001)], "session"), physical)
    assert monitor.count == 1
    assert monitor.maximum_delay_s == pytest.approx(2.000001)
    # Unlike duplicate identities, a genuinely new timestamp can measure delay.
    monitor.observe(raw(120., [("A", STAMP + .000001)], "session"), physical)
    assert monitor.observed_late_samples == 1
