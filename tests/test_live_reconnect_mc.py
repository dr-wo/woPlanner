"""Reconnect restores timing, tyres and distance into the real MC input worker."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import time

import pytest

from wodata.live.controller import LiveConfig, LiveTimingController
from wodata.live.models import LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder
from woplanner.live.telemetry import TelemetryAnalysisWorker
from woplanner.live.telemetry import (
    CausalAnalysisCut, _CausalSnapshotBuilder, _analysis_store, _apply_analysis_message,
)


def test_recovered_history_can_prepare_mc_without_waiting_for_new_live_lap(tmp_path):
    now = datetime(2026, 8, 23, 13, tzinfo=timezone.utc)
    packets = [
        ("SessionInfo", {"Name": "Race", "StartDate": now.isoformat(), "Meeting": {"RoundNumber": 12}}),
        ("DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"}}),
        ("TimingAppData", {"Lines": {"1": {"Stints": {"0": {
            "Compound": "MEDIUM", "StartLaps": 0, "TotalLaps": 1}}}}}),
        ("LapCount", {"TotalLaps": 57}),
        ("TrackStatus", {"Status": "1"}),
        ("TimingData", {"Lines": {"1": {"Position": "1", "NumberOfLaps": 1,
                                            "LastLapTime": {"Value": "1:30.000"}}}}),
    ]
    recorder = LiveTimingRecorder(tmp_path / "live_timing_raw.jsonl")
    try:
        for sequence, (topic, payload) in enumerate(packets, 1):
            timestamp = now + timedelta(seconds=sequence)
            recorder.write(LiveTimingMessage(topic, payload, timestamp, timestamp,
                                             sequence=sequence, session_time_seconds=float(sequence)))
    finally:
        recorder.close()
    worker = TelemetryAnalysisWorker()
    try:
        controller = LiveTimingController()
        controller.set_telemetry_sink(worker.enqueue_message)
        request = worker.request_analysis_input(leader_lap=1, use_sector_boundary_traffic=False)
        controller._recover_live_history(LiveConfig(2026, 12, "R"), tmp_path)
        deadline = time.monotonic() + 5
        results = []
        while not results and time.monotonic() < deadline:
            results = worker.poll_analysis_inputs()
            time.sleep(0.005)
        assert results, worker.diagnostics()
        prepared = results[0]
        assert prepared.request_id == request
        assert prepared.error is None
        assert prepared.analysis_input.scheduled_total_laps == 57
        assert prepared.analysis_input.drivers == controller.store.snapshot().drivers
        assert prepared.analysis_input.completed_laps == controller.store.snapshot().completed_laps
        assert prepared.cut.leader_lap_number == 1
    finally:
        worker.shutdown()


def retirement_history():
    now = datetime(2026, 8, 23, 13, tzinfo=timezone.utc)
    packets = [
        ("SessionInfo", {"Name": "Race", "StartDate": now.isoformat(), "Meeting": {"RoundNumber": 12}}),
        ("DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"},
                        "2": {"Tla": "BBB", "TeamName": "Team"}}),
        ("LapCount", {"TotalLaps": 57}),
        ("TimingData", {"Lines": {"1": {"Position": "2", "NumberOfLaps": 1,
                                            "LastLapTime": {"Value": "1:30.000"}},
                                    "2": {"Position": "1", "NumberOfLaps": 1,
                                            "LastLapTime": {"Value": "1:30.000"}}}}),
        ("TimingData", {"Lines": {"1": {"Retired": True}}}),
        ("TimingData", {"Lines": {"1": {"Retired": False}}}),
        ("TimingData", {"Lines": {"1": {"Position": "2", "NumberOfLaps": 2,
                                            "LastLapTime": {"Value": "1:30.000"}}}}),
        ("TimingData", {"Lines": {"2": {"Position": "1", "NumberOfLaps": 2,
                                            "LastLapTime": {"Value": "1:30.000"}}}}),
    ]
    return [LiveTimingMessage(topic, payload, now + timedelta(seconds=index),
                              now + timedelta(seconds=index), sequence=index,
                              session_time_seconds=float(index))
            for index, (topic, payload) in enumerate(packets, 1)]


@pytest.mark.parametrize("recover", [True, False])
def test_real_worker_retirement_correction_matches_recovery_context_only(tmp_path, recover):
    messages = retirement_history()
    worker = TelemetryAnalysisWorker()
    try:
        controller = LiveTimingController()
        controller.set_telemetry_sink(worker.enqueue_message)
        worker.request_analysis_input(leader_lap=2, use_sector_boundary_traffic=False)
        if recover:
            recorder = LiveTimingRecorder(tmp_path / "live_timing_raw.jsonl")
            for message in messages:
                recorder.write(message)
            recorder.close()
            controller._recover_live_history(LiveConfig(2026, 12, "R"), tmp_path)
        else:
            for message in messages:
                controller._enqueue_message(message)
            controller.drain()
        deadline = time.monotonic() + 5
        results = []
        while not results and time.monotonic() < deadline:
            results = worker.poll_analysis_inputs()
            time.sleep(.005)
        assert results, worker.diagnostics()
        assert results[0].error is None
        snapshot = results[0].analysis_input
        assert snapshot.completed_laps == controller.store.snapshot().completed_laps
        actual = [lap.lap_number for lap in snapshot.completed_laps if lap.driver_number == "1"]
        assert actual == ([1, 2] if recover else [1])
        assert all(not message.historical_recovery for message in messages)
    finally:
        worker.shutdown()


def test_recovery_retirement_semantics_survive_forward_and_backward_causal_cuts():
    messages = retirement_history()
    history = [(message.sequence, replace(message, historical_recovery=True)) for message in messages]
    builder = _CausalSnapshotBuilder()
    early = CausalAnalysisCut(1, 4., 4, 0)
    retired = CausalAnalysisCut(1, 5., 5, 0)
    final = CausalAnalysisCut(2, 8., 8, 0)
    def target_laps(snapshot):
        return [lap.lap_number for lap in snapshot.completed_laps if lap.driver_number == "1"]
    assert target_laps(builder.build(history, early)) == [1]
    assert target_laps(builder.build(history, final)) == [1, 2]
    assert target_laps(builder.build(history, retired)) == [1]
    assert target_laps(builder.build(history, early)) == [1]
    assert target_laps(builder.build(history, final)) == [1, 2]
    assert not builder._store.allow_retirement_reactivation

    # After recovered history, live packets still use the original default.
    store = _analysis_store()
    for _, message in history:
        _apply_analysis_message(store, message)
        assert not store.allow_retirement_reactivation
    for index, patch in enumerate(({"Retired": True}, {"Retired": False},
                                    {"NumberOfLaps": 3, "LastLapTime": {"Value": "1:30.000"}}), 9):
        _apply_analysis_message(store, replace(messages[-1], sequence=index,
                               session_time_seconds=float(index), payload={"Lines": {"1": patch}}))
    assert target_laps(store.snapshot()) == [1, 2]
    assert not store.allow_retirement_reactivation
