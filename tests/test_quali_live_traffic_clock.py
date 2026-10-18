"""Sparse live polling advances deadlines; replay polling cannot invent time."""
from dataclasses import replace
from queue import Queue
from types import SimpleNamespace

import pytest
from wodata.live.quali_models import QualiPositionObservation

from test_quali_late_telemetry import packet, scheduled
from test_quali_monotonic_receipts import live, receipt


def live_points(service, mono, stamps):
    return receipt(service, mono, 10000. + mono, points=tuple(
        QualiPositionObservation("1", stamp, "OnTrack") for stamp in stamps))


def test_sparse_live_poll_publishes_at_normal_deadline_without_timing_change(monkeypatch):
    wall = [101.]
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: wall[0])
    service = live()
    live_points(service, 101., (20., 21.))
    before_drivers = service.snapshot.drivers
    before_cutoff = service.snapshot.cutoff
    wall[0] = 101.999
    service.poll()
    assert not service._physical_traffic.calls
    wall[0] = 102.
    snapshot, _, changed = service.poll()
    assert changed
    assert snapshot.drivers == before_drivers
    assert len(service._physical_traffic.calls) == 1
    assert service._physical_traffic.calls[0][0] == pytest.approx(102.)
    assert service._track_samples[1].traffic_status == "ok"
    # A supported fit can change the track estimate, while current cutoff
    # identity/current timing still belong to ordinary qualifying state.
    assert snapshot.cutoff.current_cutoff_s == before_cutoff.current_cutoff_s
    assert snapshot.cutoff.driver == before_cutoff.driver
    wall[0] = 104.
    service.poll()
    assert not service._pending_traffic


def test_replay_poll_never_advances_captured_receive_clock(monkeypatch):
    wall = [1000.]
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: wall[0])
    service = scheduled()
    service.controller._source = type("ReplayPlayer", (), {})()
    packet(service, 101., (20., 21.))
    service.consume(replace(service.raw_snapshot, message_source="replay", position_observations=()))
    wall[0] += 100.
    service.poll()
    assert service._traffic_now == 101.
    assert not service._physical_traffic.calls
    packet(service, 102.)
    assert service._physical_traffic.calls[0][0] == 102.


def test_delayed_processing_uses_actual_receive_deadline(monkeypatch):
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: 103.5)
    service = live()
    live_points(service, 101., (20., 21.))
    service.poll()
    assert service._traffic_now == 103.5
    assert service._physical_traffic.calls[0] == (102., 2)
    assert all(count == 2 for _, count in service._physical_traffic.calls)
    assert service._pending_traffic  # Grace ends at receipt 104, not processing + 4.


@pytest.mark.parametrize("ended", [False, True])
def test_queued_receipts_are_reduced_before_wall_clock_or_end_freeze(monkeypatch, ended):
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: 105.)
    service = live()
    live_points(service, 101., (20.,))
    queue = Queue()
    queue.put("already received evidence")
    service.controller._messages = queue
    result = SimpleNamespace(snapshot=service.raw_snapshot, changed=False,
                             statuses=(SimpleNamespace(state="disconnected"),) if ended else ())
    monkeypatch.setattr(service.controller, "drain", lambda: result)
    service.poll()
    assert service._traffic_now == 101.
    assert not service._physical_traffic.calls
    # The next capped drain delivers the receipt that was still queued.
    queue.get_nowait()
    live_points(service, 103., (21.,))
    result.statuses = ()
    service.poll()
    assert service._physical_traffic.calls[:2] == [(102., 1), (103., 2)]
    assert service._track_samples[1].traffic_status == "ok"
    assert not service._pending_traffic


def test_stop_reduces_received_backlog_before_finalising(monkeypatch):
    service = live()
    live_points(service, 101., (20.,))
    queue = Queue()
    queue.put("already received evidence")
    service.controller._messages = queue
    monkeypatch.setattr(service.controller, "stop", lambda: None)

    def drain():
        queue.get_nowait()
        live_points(service, 103., (21.,))

    monkeypatch.setattr(service.controller, "drain", drain)
    service.stop()
    assert service._physical_traffic.calls == [(102., 1), (103., 2)]
    assert not service._pending_traffic
