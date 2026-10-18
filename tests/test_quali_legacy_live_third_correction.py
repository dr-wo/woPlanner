"""D3 schema-2/public legacy equivalence through the actual traffic pipeline."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live.replay import ReplayPlayer
from wodata.live.replay.faults import ReplayFaultConfig
from wodata.live.replay.schema import ReplayEvent
from woplanner.quali.service import QualiLiveService
from woplanner.quali.lap_state import LapPaceReference


EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def recording(tmp_path, format_name, *, explicit_false=False):
    observations = [
        ("DriverList", {"A": {"Tla": "A"}}, 90., 0.),
        ("TimingData", {"Lines": {"A": {"NumberOfLaps": 1, "InPit": False}}}, 100., .1),
        ("TimingData", {"Lines": {"A": {"Sectors": {"0": {"Value": "0:03.000"}}}}}, 103., .2),
        ("TimingData", {"Lines": {"A": {"NumberOfLaps": 2,
            "LastLapTime": {"Value": "0:10.000"}}}}, 110., .3),
        ("Heartbeat", {}, 120., 1.3),
    ]
    records = []
    for sequence, (topic, payload, acquired, playback) in enumerate(observations, 1):
        received = (EPOCH + timedelta(seconds=acquired)).isoformat()
        if format_name == "schema2":
            row = dict(recording_schema_version=2, message_schema_version=1,
                topic=topic, payload=payload, sequence=sequence, source="signalr_live",
                received_at_utc=received, source_timestamp_utc=received,
                session_time_seconds=acquired, playback_time_seconds=playback)
        else:
            row = ReplayEvent(topic, payload, playback, sequence, source="signalr_live",
                original_timestamp=received, original_received_at=received,
                message_session_time_seconds=acquired,
                preserve_message_session_time=True).to_dict()
        if explicit_false:
            row["received_at_is_trusted"] = False
        records.append(row)
    path = tmp_path / f"{format_name}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def run(path, monkeypatch, *, speed, wall_offset, faults):
    service = QualiLiveService()
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    trace = []
    wall = [wall_offset]

    def wait(self, seconds, external):
        wall[0] += seconds
        return True

    def ingress(message):
        raw = service.controller.store.apply(message)
        service.consume(raw)
        trace.append((raw.traffic_clock_kind, raw.receive_time_trusted,
                      raw.traffic_clock_time_s, tuple(service._pending_traffic)))

    monkeypatch.setattr(ReplayPlayer, "_wait", wait)
    monkeypatch.setattr("time.monotonic", lambda: wall[0])
    assert ReplayPlayer(path, speed=speed, fault_config=faults).play(ingress).status == "completed"
    return service, trace


@pytest.mark.parametrize("speed", [0., 10., 10000.])
@pytest.mark.parametrize("faults", [ReplayFaultConfig(), ReplayFaultConfig(duplicate_rate=1.,
    topic_delay_seconds={"Heartbeat": 10.})])
def test_schema2_and_public_legacy_same_genuine_receipts_lock_identically(tmp_path, monkeypatch, speed, faults):
    current, current_trace = run(recording(tmp_path, "schema2"), monkeypatch,
        speed=speed, wall_offset=10., faults=faults)
    legacy, legacy_trace = run(recording(tmp_path, "legacy"), monkeypatch,
        speed=speed, wall_offset=100000000., faults=faults)
    assert current_trace == legacy_trace
    assert all(kind == "receive" and trusted for kind, trusted, _, _ in legacy_trace)
    # The completion and heartbeat have original receipts ten seconds apart;
    # their playback coordinates remain one second apart before fault scheduling.
    assert legacy_trace[-1][2] == EPOCH.timestamp() + 120.
    assert not current._pending_traffic and not legacy._pending_traffic
    assert current._track_samples == legacy._track_samples
    assert current.traffic_finalisation_records == legacy.traffic_finalisation_records
    assert len(legacy._track_samples) == 1
    assert legacy._track_samples[0].traffic_status == "unknown"
    assert len(legacy.traffic_finalisation_records) == 1
    record = legacy.traffic_finalisation_records[0]
    assert record["evidence_deadline_s"] == EPOCH.timestamp() + 112.
    assert record["delay_s"] == 2.
    assert record["reason"] == "deadline"


def test_explicit_false_in_equivalent_live_formats_uses_same_untrusted_playback(tmp_path, monkeypatch):
    current, current_trace = run(recording(tmp_path, "schema2", explicit_false=True), monkeypatch,
        speed=0., wall_offset=10., faults=ReplayFaultConfig())
    legacy, legacy_trace = run(recording(tmp_path, "legacy", explicit_false=True), monkeypatch,
        speed=0., wall_offset=1e8, faults=ReplayFaultConfig())
    assert current_trace == legacy_trace
    assert all(kind == "playback" and not trusted for kind, trusted, _, _ in legacy_trace)
    assert current._pending_traffic and legacy._pending_traffic
    assert current._track_samples == legacy._track_samples
    assert not current.traffic_finalisation_records and not legacy.traffic_finalisation_records
