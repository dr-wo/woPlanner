"""Duplicate diagnostics survive both completion windows and bounded eviction."""
from types import SimpleNamespace

from wodata.live.quali_models import QualiPositionObservation
from woplanner.quali.traffic_finalisation import TelemetryDelayMonitor

KEY = ("Q1", "1", 2, 90.)


def packet(now, samples=()):
    return SimpleNamespace(
        receive_time_trusted=True, traffic_clock_time_s=now,
        position_observations=tuple(QualiPositionObservation(number, stamp, "OnTrack")
                                    for number, stamp in samples), speed_observations=(),
    )


def adapter(*, known=True):
    return SimpleNamespace(
        completion_bounds=lambda key: (0., 90.) if known and key == KEY else None,
        completed_lap_for_sample=lambda number, stamp: KEY if known and number == "1" and 0 < stamp <= 90 else None,
    )


def test_on_time_early_receipt_survives_active_completion_window():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.observe(packet(89., (("1", 50.),)), adapter(known=False))
    monitor.completed(KEY, 100., True)
    monitor.observe(packet(100.), adapter())
    monitor.observe(packet(699., (("1", 50.),)), adapter())
    assert monitor.first_receipts[("position", "1", 50.)] == 89.
    assert monitor.count == 0
    assert monitor.warning == ""
    assert monitor.observed_late_samples == 0
    # A genuinely new late acquisition sample remains measurable.
    monitor.observe(packet(699., (("1", 51.),)), adapter())
    assert monitor.count == 1
    assert monitor.maximum_delay_s == 599.


def test_evicted_receipt_cannot_be_reclassified_as_new_late_sample(monkeypatch):
    monkeypatch.setattr("woplanner.quali.traffic_finalisation.MAX_RECEIPT_IDENTITIES", 3)
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.observe(packet(89., (("1", 50.),)), adapter(known=False))
    monitor.completed(KEY, 100., True)
    monitor.observe(packet(100.), adapter())
    monitor.observe(packet(101., (("2", 1.), ("2", 2.), ("2", 3.))), adapter())
    assert ("position", "1", 50.) not in monitor.first_receipts
    monitor.observe(packet(103., (("1", 50.),)), adapter())
    assert monitor.count == 0
    assert monitor.warning == ""
    monitor.observe(packet(103., (("1", 51.),)), adapter())
    assert monitor.count == 1
    assert len(monitor.first_receipts) <= 3
    assert len(monitor._receipt_anchors) <= 3
    assert len(monitor._associated_receipts) <= 3
    assert sum(map(len, monitor._receipt_ids_by_driver.values())) <= 3
    assert len(monitor._receipt_expiry_order) <= 6


def test_time_eviction_without_available_bounds_is_conservative():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.observe(packet(89., (("1", 50.),)), adapter(known=False))
    monitor.completed(KEY, 100., True)
    # Completion ownership is not yet reliable, so expiry cannot be extended.
    monitor.observe(packet(100.), adapter(known=False))
    monitor.observe(packet(699., (("1", 50.),)), adapter())
    assert monitor.count == 0
    assert monitor.warning == ""


def test_retention_expires_and_cardinality_index_clears():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.observe(packet(89., (("1", 50.),)), adapter(known=False))
    monitor.completed(KEY, 100., True)
    monitor.observe(packet(100.), adapter())
    monitor.advance(701.)
    assert not monitor.first_receipts
    assert not monitor._receipt_anchors
    assert not monitor._receipt_ids_by_driver
    assert not monitor._associated_receipts
    assert not monitor._receipt_expiry_order
    assert not monitor.completions
    assert not monitor._forgotten_acquisition_floor
