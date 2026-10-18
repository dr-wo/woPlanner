"""Verify evidence-audit clock separation; these are not implementation tests."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/audit_q17_03_cross_round.py"
SPEC = importlib.util.spec_from_file_location("q17_03_audit", SOURCE)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def utc(seconds):
    return (EPOCH + timedelta(seconds=seconds)).isoformat()


def capture(tmp_path, *, late_delay=.7):
    records = []

    def append(topic, payload, sample_clock, arrival_clock):
        records.append({"recording_schema_version": 2, "message_schema_version": 1,
                        "topic": topic, "payload": payload, "source": "signalr_live",
                        "source_timestamp_utc": utc(sample_clock),
                        "received_at_utc": utc(arrival_clock), "sequence": len(records) + 1,
                        "session_time_seconds": sample_clock})

    def positions(times):
        return {"Position": [{"Timestamp": utc(stamp), "Entries": {
            "A": {"X": 10, "Y": 10, "Status": "OnTrack"},
        }} for stamp in times]}

    append("DriverList", {"A": {"Tla": "AAA"}}, 99., 479.)
    append("TimingData", {"Lines": {"A": {"NumberOfLaps": 1, "InPit": False}}}, 100., 480.)
    append("CarData.z", {"Entries": [{"Utc": utc(stamp), "Cars": {
        "A": {"Channels": {"2": 200., "0": 12000}},
    }} for stamp in (100.5, 101.5, 102.)]}, 101., 490.)
    append("Position.z", positions((100.5, 101.)), 101., 491.)
    append("TimingData", {"Lines": {"A": {"NumberOfLaps": 2,
           "LastLapTime": {"Value": "0:02.000"}}}}, 102., 500.)
    append("Position.z", positions((101.5, 102.)), 102., 500. + late_delay)
    path = tmp_path / "capture.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_batched_earlier_speed_is_already_received_not_future(tmp_path):
    result = AUDIT.audit(0, capture(tmp_path))
    sync = result["synchronisation"]
    assert sync["positions"] == 4
    assert sync["legacy_temporal_matches"] == 1
    assert sync["future_speed_rejections"] == 3
    assert sync["future_rejections_with_received_earlier_speed"] == 3
    assert sync["causal_temporal_matches"] == 4


def test_actual_arrival_delta_and_deadline_completeness(tmp_path):
    result = AUDIT.audit(0, capture(tmp_path))
    delays = result["position_after_completion_arrival_s"]
    assert delays["count"] == 2
    assert delays["max"] == pytest.approx(.7, abs=1e-6)
    assert result["source_gap_summary"]["anchored_laps"] == 1
    windows = result["window_temporal_completeness"]
    assert windows["0.0"]["causal_speed_temporally_complete_laps"] == 0
    assert windows["0.5"]["causal_speed_temporally_complete_laps"] == 0
    assert windows["1.0"]["causal_speed_temporally_complete_laps"] == 1
    assert windows["2.0"]["causal_speed_temporally_complete_laps"] == 1


def test_two_seconds_does_not_capture_later_observations(tmp_path):
    result = AUDIT.audit(0, capture(tmp_path, late_delay=2.1))
    assert result["late_position_two_second_coverage"] == 0
    assert result["window_temporal_completeness"]["2.0"]["causal_speed_temporally_complete_laps"] == 0
    assert result["window_temporal_completeness"]["5.0"]["causal_speed_temporally_complete_laps"] == 1


def test_archive_publication_is_not_live_arrival_evidence(tmp_path):
    path = capture(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        record["source"] = "f1_archive_replay"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = AUDIT.audit(0, path)
    assert result["real_arrival_timestamps"] is False
    assert result["position_after_completion_arrival_s"] is None
    assert result["window_temporal_completeness"] is None


def test_source_gap_and_boundary_limits_are_unchanged():
    assert AUDIT.temporal_complete([(0.5,), (1.5,), (2.,)], 0., 2.)
    assert not AUDIT.temporal_complete([(0.5,), (1.500001,), (2.,)], 0., 2.)
    assert not AUDIT.temporal_complete([(0.500001,), (1.,), (2.,)], 0., 2.)


def test_speed_received_after_position_cannot_retroactively_match(tmp_path):
    path = capture(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[2], records[3] = records[3], records[2]
    # Delivery order is authoritative for causal availability, even when the
    # packet's sample clock is older and both channels subsequently exist.
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = AUDIT.audit(0, path)
    assert result["synchronisation"]["causal_temporal_matches"] == 2
    assert result["synchronisation"]["missing_or_stale_speed_rejections"] == 2


def test_late_duplicate_does_not_expand_useful_arrival_distribution(tmp_path):
    path = capture(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    duplicate = dict(records[-1], received_at_utc=utc(510.), sequence=7)
    records.append(duplicate)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = AUDIT.audit(0, path)
    assert result["position_after_completion_arrival_s"]["max"] == 10.
    useful = result["unique_causal_position_after_completion_arrival_s"]
    assert useful["count"] == 2
    assert useful["max"] == pytest.approx(.7, abs=1e-6)
    assert result["unique_causal_late_position_two_second_coverage"] == 1.


def test_archive_without_utc_offset_has_incomparable_lap_clock(tmp_path):
    path = capture(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        record["source"] = "f1_archive_replay"
        record["source_timestamp_utc"] = None
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = AUDIT.audit(0, path)
    assert result["source_gap_summary"]["incomparable_sample_boundary_clock_laps"] == 1
    assert "eventual_position_temporally_complete_laps" not in result["source_gap_summary"]


def test_deadline_is_inclusive_for_evidence_audit(tmp_path):
    result = AUDIT.audit(0, capture(tmp_path, late_delay=2.))
    assert result["unique_causal_late_position_two_second_coverage"] == 1.
    assert result["window_temporal_completeness"]["2.0"]["causal_speed_temporally_complete_laps"] == 1
