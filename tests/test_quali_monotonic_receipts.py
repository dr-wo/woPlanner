"""Live receipt progression is immune to UTC corrections and processing delay."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from queue import Queue
from types import SimpleNamespace

import pytest
from wodata.live import FastF1LiveTimingSource, LiveTimingMessage
from wodata.live.controller import LiveTimingController
from wodata.live.quali_reducer import QualiSessionStore
from woplanner.quali.traffic_finalisation import PendingTraffic
from test_quali_late_telemetry import scheduled, KEY


def receipt(service, mono, utc, *, points=()):
    raw = replace(service.raw_snapshot, message_source="signalr_live", receive_time_trusted=True,
                  traffic_clock_time_s=utc, traffic_clock_kind="receive", position_observations=points)
    # Works on the saved defective dataclass as well; old service ignores it.
    object.__setattr__(raw, "receive_monotonic_s", mono)
    return service.consume(raw)


def live():
    service = scheduled()
    service._pending_traffic.clear()
    service._traffic_clock_kind = None
    service._traffic_now = None
    service.controller._source = FastF1LiveTimingSource()
    service.session_is_qualifying = True
    receipt(service, 100., 100.)
    service._pending_traffic[KEY] = PendingTraffic(KEY, "AAA", 3, 1, 100., service._traffic_clock_kind)
    return service


@pytest.mark.parametrize("utc", [160., 40.])
def test_utc_jump_cannot_accelerate_or_delay_live_deadlines(utc):
    service = live()
    receipt(service, 101.1, utc)
    assert service._traffic_now == pytest.approx(101.1)
    assert KEY in service._pending_traffic
    assert not service._physical_traffic.calls
    receipt(service, 102., utc + .9)
    assert service._physical_traffic.calls[0][0] == 102.
    assert KEY in service._pending_traffic
    receipt(service, 104., utc + 2.9)
    assert KEY not in service._pending_traffic
    count = len(service._physical_traffic.calls)
    receipt(service, 104.1, utc + 3.)
    assert len(service._physical_traffic.calls) == count


def test_sparse_callback_distinguishes_cutoff_and_publication_time(monkeypatch):
    now = [103.5]
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: now[0])
    service = live()
    receipt(service, 101., 160.)
    service.poll()
    assert service._physical_traffic.calls[0][0] == 102.
    record = service.traffic_finalisation_records[-1]
    assert record["evidence_deadline_s"] == 102.
    assert record["publication_clock_s"] == 103.5
    assert record["publication_delay_s"] == 3.5
    assert KEY in service._pending_traffic
    now[0] = 104.
    service.poll()
    assert KEY not in service._pending_traffic


def test_queue_freeze_waits_for_receipts_not_processing_time(monkeypatch):
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: 160.)
    service = live()
    receipt(service, 101., 101.)
    service.controller._messages.put("already received")
    result = SimpleNamespace(snapshot=service.raw_snapshot, changed=False, statuses=())
    monkeypatch.setattr(service.controller, "drain", lambda: result)
    service.poll()
    assert KEY in service._pending_traffic
    assert not service._physical_traffic.calls
    service.controller._messages.get_nowait()
    receipt(service, 103., 160.)
    assert service._physical_traffic.calls[0][0] == 102.
    assert KEY in service._pending_traffic
    service.poll()
    assert KEY not in service._pending_traffic


def test_controller_captures_live_receipt_before_queueing_and_preserves_it(monkeypatch):
    controller = LiveTimingController(store=QualiSessionStore())
    controller._source = FastF1LiveTimingSource()
    wall = [100.]
    monkeypatch.setattr("wodata.live.controller.time.monotonic", lambda: wall[0])
    received = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller._enqueue_message(LiveTimingMessage("Heartbeat", {}, received, received))
    queued = controller._messages.queue[0]
    assert queued.received_at_monotonic_s == 100.
    wall[0] = 160.
    controller.drain()
    assert controller.store.snapshot().receive_monotonic_s == 100.
    # Execution monotonic must not be manufactured for replay.
    controller._source = object()
    controller._enqueue_message(LiveTimingMessage("Heartbeat", {}, received, received,
                                                   source="f1_archive_replay", received_at_is_trusted=True))
    assert controller._messages.queue[0].received_at_monotonic_s is None
