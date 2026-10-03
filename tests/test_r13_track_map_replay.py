from __future__ import annotations

from pathlib import Path
import time

import pytest

from wodata.live.replay import ReplayPlayer
from woplanner.live.telemetry import TelemetryAnalysisWorker


def _r13_recording() -> Path | None:
    relative = Path(
        "woData/wodata/live_timing/schema_v1/year=2026/round=13/session=R/"
        "live_timing_raw.jsonl"
    )
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    return None


def test_real_r13_replay_builds_bound_reference_and_filters_position_ghosts():
    recording = _r13_recording()
    if recording is None:
        pytest.skip("local 2026 R13 recording is unavailable")
    worker = TelemetryAnalysisWorker()
    worker.reset()
    result = ReplayPlayer(recording, speed=0).play(
        worker.enqueue_message, end_time_seconds=600,
    )
    deadline = time.monotonic() + 20.0
    expected_positions = result.topic_counts.get("Position.z", 0)
    while (
        worker.diagnostics().processed_ingress_sequence
        < worker.diagnostics().latest_ingress_sequence
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    snapshot = worker.track_map_snapshot()
    assert result.status == "completed"
    assert worker.diagnostics().position_messages_processed == expected_positions
    assert len(snapshot.drivers) == 22
    assert any(driver.valid for driver in snapshot.drivers)
    assert not {"241", "242", "243"}.intersection(
        driver.driver_id for driver in snapshot.drivers
    )
    assert snapshot.reference is not None
    assert snapshot.reference.driver_id in {
        driver.driver_id for driver in snapshot.drivers
    }
    assert snapshot.reference.driver_code == "RUS"
    assert snapshot.reference.lap_number == 2
    assert snapshot.reference.lap_time_s == pytest.approx(86.651, abs=0.5)
    assert len(snapshot.reference.points) >= 300
    assert snapshot.reference.source == "actual clean-lap Position.z trajectory"
    worker.shutdown()
