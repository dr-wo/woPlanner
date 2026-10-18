"""Independent live-clock adversity on real reducer/service/controller paths."""
from datetime import timedelta

import pytest

from wodata.live import LiveTimingMessage
from test_quali_c1_review_corrections import EPOCH, field, publications, target


def receipt(store, service, monotonic, wall, *, enqueue=False):
    message = LiveTimingMessage(
        "Heartbeat", {}, EPOCH + timedelta(seconds=111.),
        EPOCH + timedelta(seconds=wall), source="signalr_live",
        received_at_is_trusted=True,
    )
    object.__setattr__(message, "received_at_monotonic_s", monotonic)
    if enqueue:
        service.controller._enqueue_message(message)
    else:
        service.consume(store.apply(message))


@pytest.mark.parametrize("wall", [10000., -10000.])
def test_real_live_utc_jumps_do_not_shorten_or_restart_deadlines(wall):
    store, service, key = field(live=True)
    receipt(store, service, 111.1, wall)
    assert publications(service, key) == []
    assert key in service._pending_traffic
    assert service._pending_traffic[key].completed_at == 110.
    receipt(store, service, 112., wall + 1000.)
    assert target(service).traffic_status == "ok"
    assert publications(service, key)[0]["delay_s"] == 2
    assert key in service._pending_traffic
    receipt(store, service, 114., wall - 1000.)
    assert key not in service._pending_traffic


def test_actual_controller_processes_queued_receipts_before_delayed_live_poll(monkeypatch):
    store, service, key = field(live=True)
    # Explicitly wire real reducer callbacks to the service. This isolates
    # traffic from unrelated identity discovery in this synthetic session.
    service.controller.on_message_snapshot = service.consume
    receipt(store, service, 111.5, 10000., enqueue=True)
    receipt(store, service, 112.5, -10000., enqueue=True)
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: 200.)
    service.poll()
    record = publications(service, key)[0]
    assert record["evidence_deadline_s"] == 112.
    assert record["publication_clock_s"] == 200.
    assert record["publication_evidence_progress_s"] == 112.5
    assert record["delay_s"] == 2.
    assert record["publication_delay_s"] == 90.
    assert target(service).traffic_status == "ok"
    assert key not in service._pending_traffic  # Sparse live callback occurs at 200.


def test_missing_original_live_monotonic_receipt_cannot_certify_new_live_deadline():
    store, service, key = field(live=True)
    message = LiveTimingMessage("Heartbeat", {}, None, EPOCH + timedelta(seconds=10000.),
                               source="signalr_live", received_at_is_trusted=True)
    service.consume(store.apply(message))
    assert service.raw_snapshot.receive_time_trusted is False
    assert service.raw_snapshot.traffic_clock_time_s is None
    assert key in service._pending_traffic
    assert publications(service, key) == []
