"""Deadline/revision/warning controls; synthetic latency is not field evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from wodata.live import LiveConnectionStatus, QualiRawSnapshot
from wodata.live.quali_models import QualiPositionObservation
from wodata.live.replay import ReplayPlayer
from woplanner.quali.physical_traffic import PhysicalLapTraffic, QUALIFYING_TRAFFIC_POLICY
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution import TrackEvolutionSample
from woplanner.quali.traffic_finalisation import PendingTraffic, TelemetryDelayMonitor


KEY = ("Q1", "1", 3, 90.)
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TrafficControl:
    """Separate deadline scheduling tests from shared physical mathematics."""
    policy = QUALIFYING_TRAFFIC_POLICY

    def __init__(self):
        self.rows = {}
        self.revision = 0
        self.changed_sample_times = self.changed_state_times = ()
        self.calls = []

    def ingest(self, raw):
        changed = []
        for item in raw.position_observations:
            key = (item.driver_number, item.session_time_s)
            if key in self.rows:
                continue
            self.rows[key] = raw.traffic_clock_time_s
            changed.append(key)
            self.revision += 1
        self.changed_sample_times = tuple(changed)

    def completion_bounds(self, key):
        return (0., 90.) if key == KEY else None

    def completed_lap_for_sample(self, number, stamp):
        return KEY if number == "1" and 0 < stamp <= 90 else None

    def finalize(self, driver, lap, *, evidence_deadline_s=None, clock_kind=None):
        count = sum(receipt <= evidence_deadline_s for receipt in self.rows.values())
        self.calls.append((evidence_deadline_s, count))
        return PhysicalLapTraffic(4., .8, "ok", count) if count >= 2 else PhysicalLapTraffic(target_samples=count)


def sample(lap, index, install, *, gap=None, status="pending"):
    return TrackEvolutionSample("Q1", "AAA", "Team", lap, 92. - lap,
                                index, "SOFT", 0, True, gap,
                                .8 if gap is not None else 0., status,
                                tyre_install_index=install)


def scheduled():
    service = QualiLiveService()
    service._physical_traffic = TrafficControl()
    service._track_samples = [sample(2, 1, 0, gap=4., status="ok"), sample(3, 2, 1)]
    service.track_evolution_policy.samples = {
        ('Q1', '1', 2, 0.): service._track_samples[0], KEY: service._track_samples[1],
    }
    service.track_evolution_policy._dirty = True
    service._pending_traffic[KEY] = PendingTraffic(KEY, "AAA", 3, 1, 100., "receive")
    service._traffic_now = 100.
    service._traffic_clock_kind = "receive"
    service.raw_snapshot = replace(QualiRawSnapshot.empty(), session_part_established=True)
    service.telemetry_delays.completed(KEY, 100., True)
    service.snapshot = service._build_snapshot()
    return service


def packet(service, now, times=(), *, part="Q1", trusted=True, kind="receive"):
    return service.consume(replace(
        QualiRawSnapshot.empty(), session_part=part, session_part_established=True,
        traffic_clock_time_s=now, traffic_clock_kind=kind,
        receive_time_trusted=trusted, message_source="synthetic_test",
        position_observations=tuple(QualiPositionObservation("1", stamp, "OnTrack") for stamp in times),
        last_message_at_utc=EPOCH + timedelta(seconds=now),
    ))


def test_normal_deadline_does_not_delay_timing_or_wait_four_seconds():
    service = scheduled()
    packet(service, 101.9, (20., 21.))
    assert not service._physical_traffic.calls
    assert service._track_samples[1].traffic_status == "pending"
    assert service.advance_traffic_clock(102.)
    assert service._track_samples[1].traffic_status == "ok"
    assert service.snapshot.track_evolution_diagnostic.driver_count == 1
    assert service.traffic_finalisation_records[-1]["delay_s"] == 2.
    assert len(service._pending_traffic) == 1  # Published; exceptional window remains open.


def test_grace_replaces_metadata_without_vetoing_supported_fit():
    service = scheduled()
    packet(service, 101., (20.,))
    service.advance_traffic_clock(102.)
    assert service.snapshot.track_evolution_diagnostic.driver_count == 1
    prior_rate = service.snapshot.cutoff.track_rate_s_per_field_push
    packet(service, 103., (21.,))
    assert service.snapshot.cutoff.track_rate_s_per_field_push == prior_rate
    assert len(service._track_samples) == 2
    assert service.snapshot.track_evolution_diagnostic.driver_count == 1
    assert service.snapshot.track_evolution_diagnostic.lap_count == 2
    assert service.traffic_finalisation_records[-1]["reason"] == "exceptional grace"
    count = len(service._physical_traffic.calls)
    packet(service, 103.2, (21.,))
    assert len(service._physical_traffic.calls) == count
    assert len(service._track_samples) == 2


def test_sparse_message_can_trigger_normal_then_grace_with_separate_cutoffs():
    service = scheduled()
    packet(service, 101., (20.,))
    packet(service, 103., (21.,))
    assert service._physical_traffic.calls == [(102., 1), (103., 2)]
    assert service._track_samples[1].traffic_status == "ok"


@pytest.mark.parametrize("arrival", [104.001, 105., 200.])
def test_after_hard_limit_cannot_change_fit_but_is_diagnosed(arrival):
    service = scheduled()
    packet(service, 101., (20.,))
    service.advance_traffic_clock(104.)
    before = service._track_samples[1]
    packet(service, arrival, (21.,))
    assert service._track_samples[1] == before
    assert not service._pending_traffic
    assert service.telemetry_delays.count == 1
    assert service.telemetry_delays.maximum_delay_s == pytest.approx(arrival - 100.)


def test_hard_limit_is_inclusive_and_not_reopened():
    service = scheduled()
    packet(service, 101., (20.,))
    service.advance_traffic_clock(102.)
    packet(service, 104., (21.,))
    assert service._track_samples[1].traffic_status == "ok"
    assert not service._pending_traffic
    calls = len(service._physical_traffic.calls)
    packet(service, 104.1, (22.,))
    assert len(service._physical_traffic.calls) == calls


def test_segment_transition_resolves_pending_without_waiting():
    service = scheduled()
    packet(service, 101., (20.,))
    packet(service, 101.5, part="Q2")
    assert not service._pending_traffic
    assert service._track_samples[1].traffic_status == "unknown"
    assert service.traffic_finalisation_records[-1]["reason"] == "segment transition"


def test_source_end_and_interruption_flush_pending():
    service = scheduled()
    service.controller._statuses.put(LiveConnectionStatus("disconnected", "Replay completed"))
    service.poll()
    assert not service._pending_traffic
    assert service.traffic_finalisation_records[-1]["reason"] == "source ended"
    other = scheduled()
    other.stop()
    assert not other._pending_traffic


def test_direct_segment_transition_resolves_pending_without_feed_packet():
    service = scheduled()
    packet(service, 101., (20.,))
    service.transition_to("Q2")
    assert not service._pending_traffic
    assert service._track_samples[1].traffic_status == "unknown"
    assert service.traffic_finalisation_records[-1]["reason"] == "segment transition"


def monitor_packet(now, samples, *, trusted=True):
    return SimpleNamespace(receive_time_trusted=trusted, traffic_clock_time_s=now,
                           position_observations=tuple(QualiPositionObservation(number, stamp, "OnTrack")
                                                       for number, stamp in samples), speed_observations=())


def test_warning_counts_driver_laps_not_packets_and_clears():
    monitor = TelemetryDelayMonitor(threshold=3, window_s=600.)
    keys = {number: ("Q1", number, 2, 100.) for number in "123"}
    adapter = SimpleNamespace(completed_lap_for_sample=lambda number, stamp: keys.get(number))
    for key in keys.values():
        monitor.completed(key, 100., True)
    monitor.observe(monitor_packet(102., [("1", 50.)]), adapter)
    assert monitor.count == 0  # Exactly two seconds is not confirmed >2s.
    monitor.observe(monitor_packet(102.1, [("1", 50.)]), adapter)
    assert monitor.count == 0  # Resending previously on-time data is not lateness.
    monitor.observe(monitor_packet(103., [("1", 51.), ("1", 52.), ("2", 51.)]), adapter)
    assert monitor.count == 2
    assert monitor.warning == ""
    monitor.observe(monitor_packet(103., [("3", 51.)]), adapter)
    assert monitor.count == 3
    assert "3 laps exceeded 2s" in monitor.warning
    monitor.observe(monitor_packet(104., [("3", 51.), ("3", 52.)]), adapter)
    assert monitor.count == 3
    monitor.advance(703.001)
    assert monitor.count == 0
    assert monitor.warning == ""


def test_unknown_receipts_and_unattributable_samples_do_not_warn():
    monitor = TelemetryDelayMonitor(threshold=1)
    monitor.completed(KEY, 100., True)
    adapter = SimpleNamespace(completed_lap_for_sample=lambda *_: None)
    monitor.observe(monitor_packet(110., [("1", 50.)]), adapter)
    assert monitor.count == 0
    adapter.completed_lap_for_sample = lambda *_: KEY
    monitor.observe(monitor_packet(111., [("1", 51.)], trusted=False), adapter)
    assert monitor.count == 0
    assert monitor.warning == ""


def test_warning_is_independent_of_supported_fit_and_configurable():
    service = scheduled()
    service.telemetry_delays.threshold = 1
    packet(service, 105., (20.,))
    assert service.snapshot.track_evolution_diagnostic.driver_count == 1
    assert service.snapshot.telemetry_delayed_lap_count == 1
    assert "1 laps exceeded 2s" in service.snapshot.telemetry_delay_warning


@pytest.mark.parametrize("recorded", [True, False])
def test_accelerated_replay_uses_provenance_and_no_sleep(tmp_path, monkeypatch, recorded):
    path = tmp_path / "clock.jsonl"
    if recorded:
        rows = [{"recording_schema_version": 2, "topic": "Heartbeat", "payload": {},
                 "source": "signalr_live", "sequence": index + 1,
                 "source_timestamp_utc": (EPOCH + timedelta(seconds=stamp)).isoformat(),
                 "received_at_utc": (EPOCH + timedelta(seconds=stamp + 3.)).isoformat(),
                 "session_time_seconds": None} for index, stamp in enumerate((0., 10.))]
    else:
        rows = [{"schema_version": 1, "topic": "Heartbeat", "payload": {},
                 "source": "f1_archive_replay", "sequence": index + 1,
                 "session_time_seconds": stamp} for index, stamp in enumerate((0., 10.))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("Accelerated replay must not sleep"))
    service = QualiLiveService()
    clocks = []

    def consume(message):
        raw = service.controller.store.apply(message)
        service.consume(raw)
        clocks.append((raw.traffic_clock_time_s, raw.traffic_clock_kind, raw.receive_time_trusted))

    assert ReplayPlayer(path, speed=0).play(consume).status == "completed"
    assert clocks[1][0] - clocks[0][0] == 10.
    assert all(kind == ("receive" if recorded else "playback") and trusted == recorded
               for _, kind, trusted in clocks)
    assert service.telemetry_delays.warning == ""
