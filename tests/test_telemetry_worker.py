from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from wodata.live import LiveTimingController, LiveTimingMessage
from wodata.live.fastf1_source import LIVE_PHASE_1_TOPICS
from wodata.live.reducer import LiveSessionStore
from woplanner.analysis.service import _live_analysis_laps
from woplanner.live.telemetry import (
    CausalAnalysisCut,
    ReplayPipelineConfig,
    TelemetryAnalysisWorker,
    _CausalSnapshotBuilder,
    _ReplayTelemetryWatermarks,
    _partition_ready_builds,
)


NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def test_ready_cut_partition_can_bound_large_snapshot_preparation():
    cut = CausalAnalysisCut(1, 10.0, 10, 1)
    commands = [("build", 1, request_id, 1, True) for request_id in range(3)]

    ready, waiting = _partition_ready_builds(
        commands, {1: cut}, processed_ingress=10, max_ready=1
    )

    assert ready == [(commands[0], cut)]
    assert waiting == commands[1:]


def message(
    topic,
    payload,
    sequence,
    *,
    source="signalr_live",
    session_time=None,
):
    return LiveTimingMessage(
        topic,
        payload,
        NOW,
        NOW,
        source=source,
        sequence=sequence,
        session_time_seconds=float(sequence if session_time is None else session_time),
    )


def car_message(sequence, speed=200, *, session_time=None):
    return message(
        "CarData.z",
        {
            "Entries": [
                {
                    "Utc": "2026-08-09T00:00:00Z",
                    "Cars": {"1": {"Channels": {"2": speed}}},
                }
            ]
        },
        sequence,
        session_time=session_time,
    )


def position_message(
    sequence,
    x=None,
    *,
    source="signalr_live",
    session_time=None,
):
    value = float(sequence if x is None else x)
    return message(
        "Position.z",
        {
            "Position": [
                {
                    "Timestamp": f"2026-08-09T00:00:{sequence % 60:02d}Z",
                    "Entries": {
                        "1": {"X": value, "Y": value + 1, "Z": 0, "Status": "OnTrack"}
                    },
                }
            ]
        },
        sequence,
        source=source,
        session_time=session_time,
    )


def completed_lap_message(
    sequence,
    lap,
    *,
    driver="1",
    position=1,
    session_time=None,
):
    return message(
        "TimingData",
        {
            "Lines": {
                driver: {
                    "Position": str(position),
                    "NumberOfLaps": int(lap),
                    "LastLapTime": {"Value": "1:30.000"},
                }
            }
        },
        sequence,
        session_time=session_time,
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("background telemetry worker did not catch up")


def test_replay_pipeline_watermark_defaults_and_validation():
    config = ReplayPipelineConfig()

    assert config.replay_telemetry_high_watermark == 256
    assert config.replay_telemetry_low_watermark == 128
    assert config.replay_mc_max_queued_requests == 1
    with pytest.raises(ValueError, match="exceed"):
        ReplayPipelineConfig(
            replay_telemetry_high_watermark=8,
            replay_telemetry_low_watermark=8,
        )


def test_replay_telemetry_watermarks_use_hysteresis():
    gate = _ReplayTelemetryWatermarks(
        ReplayPipelineConfig(
            replay_telemetry_high_watermark=4,
            replay_telemetry_low_watermark=2,
        )
    )

    assert gate.has_capacity(3)
    assert not gate.has_capacity(4)
    assert not gate.has_capacity(3)
    assert not gate.has_capacity(2)
    assert gate.has_capacity(1)
    assert gate.has_capacity(3)


def test_live_signalr_topics_include_analysis_telemetry():
    assert {"CarData.z", "Position.z"}.issubset(LIVE_PHASE_1_TOPICS)


def test_raw_telemetry_is_processed_but_does_not_dirty_planner_snapshot():
    worker = TelemetryAnalysisWorker()
    worker.reset()
    controller = LiveTimingController(telemetry_sink=worker.enqueue_message)
    controller._enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 1))
    controller._enqueue_message(car_message(2))
    controller._enqueue_message(position_message(3))

    drained = controller.drain()
    wait_until(lambda: worker.diagnostics().telemetry_messages_processed == 2)

    assert drained.changes == frozenset({"drivers"})
    assert drained.snapshot.telemetry_samples == ()
    assert drained.snapshot.position_samples == ()
    assert worker.track_map_snapshot().position_visual_version == 1
    worker.shutdown()


def test_timing_messages_still_dirty_lightweight_planner_state():
    worker = TelemetryAnalysisWorker()
    worker.reset()
    controller = LiveTimingController(telemetry_sink=worker.enqueue_message)
    controller._enqueue_message(
        message(
            "TimingData",
            {"Lines": {"1": {"Position": "1", "NumberOfLaps": 1}}},
            1,
        )
    )

    drained = controller.drain()

    assert drained.changed
    assert drained.changes == frozenset({"timing"})
    worker.shutdown()


def test_position_visual_output_coalesces_to_latest_without_losing_analysis_packets():
    worker = TelemetryAnalysisWorker()
    generation = worker.reset()
    for sequence in range(1, 51):
        worker.enqueue_message(position_message(sequence, x=sequence))
    wait_until(lambda: worker.diagnostics().position_messages_processed == 50)

    snapshot = worker.track_map_snapshot()
    assert snapshot.generation == generation
    assert snapshot.position_visual_version == 50
    assert len(snapshot.drivers) == 1
    assert snapshot.drivers[0].x == 50.0
    worker.shutdown()


def test_live_and_replay_messages_use_identical_telemetry_pipeline():
    outputs = []
    for source in ("signalr_live", "f1_archive_replay"):
        worker = TelemetryAnalysisWorker()
        worker.reset()
        worker.enqueue_message(position_message(7, source=source))
        wait_until(lambda: worker.diagnostics().position_messages_processed == 1)
        outputs.append(worker.track_map_snapshot().drivers)
        worker.shutdown()

    assert outputs[0] == outputs[1]


def test_reset_clears_position_visual_generation_and_old_state():
    worker = TelemetryAnalysisWorker()
    first_generation = worker.reset()
    worker.enqueue_message(position_message(1))
    wait_until(lambda: worker.track_map_snapshot().position_visual_version == 1)

    second_generation = worker.reset()
    snapshot = worker.track_map_snapshot()

    assert second_generation == first_generation + 1
    assert snapshot.generation == second_generation
    assert snapshot.position_visual_version == 0
    assert snapshot.drivers == ()
    worker.shutdown()


def test_mc_input_is_prepared_without_raw_telemetry_or_position_history():
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 1))
    worker.enqueue_message(car_message(2))
    worker.enqueue_message(position_message(3))
    worker.enqueue_message(completed_lap_message(4, 1))
    request_id = worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )

    results = []
    wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))
    result = results[0]

    assert result.request_id == request_id
    assert result.error is None
    assert result.analysis_input is not None
    assert result.analysis_input.telemetry_samples == ()
    assert not hasattr(result.analysis_input, "position_samples")
    worker.shutdown()


def test_mc_build_overtakes_replay_telemetry_backlog(monkeypatch):
    original_publish = TelemetryAnalysisWorker._publish_track_map

    def slow_publish(self, store, generation, timing_message):
        time.sleep(0.002)
        original_publish(self, store, generation, timing_message)

    monkeypatch.setattr(TelemetryAnalysisWorker, "_publish_track_map", slow_publish)
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 1))
    worker.enqueue_message(completed_lap_message(2, 1))
    for sequence in range(3, 203):
        worker.enqueue_message(position_message(sequence))

    worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )
    results = []
    wait_until(
        lambda: bool(results.extend(worker.poll_analysis_inputs()) or results),
        timeout=3.0,
    )

    assert results[0].error is None
    assert results[0].cut is not None
    assert results[0].cut.ingress_sequence == 2
    assert worker.diagnostics().message_queue_depth > 0
    worker.shutdown()


def test_causal_cut_processes_through_watermark_but_not_later_queue(monkeypatch):
    original_apply = LiveSessionStore._apply_message

    def slow_car_data(self, timing_message):
        if timing_message.topic == "CarData.z":
            time.sleep(0.003)
        original_apply(self, timing_message)

    monkeypatch.setattr(LiveSessionStore, "_apply_message", slow_car_data)
    worker = TelemetryAnalysisWorker()
    generation = worker.reset()
    worker.enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 1))
    for sequence in range(2, 12):
        worker.enqueue_message(car_message(sequence, session_time=sequence))
    worker.enqueue_message(completed_lap_message(12, 1, session_time=12))
    for sequence in range(13, 63):
        worker.enqueue_message(car_message(sequence, session_time=sequence))
    worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )

    results = []
    wait_until(
        lambda: bool(results.extend(worker.poll_analysis_inputs()) or results),
        timeout=3.0,
    )
    result = results[0]
    diagnostics = worker.diagnostics()

    assert result.cut is not None
    assert result.cut.session_generation == generation
    assert result.cut.ingress_sequence == 12
    assert diagnostics.processed_ingress_sequence >= result.cut.ingress_sequence
    assert diagnostics.message_queue_depth > 0
    worker.shutdown()


def test_increasing_replay_cuts_extend_one_reducer_instead_of_replaying_prefixes(
    monkeypatch,
):
    original_apply = LiveSessionStore._apply_message
    applied_sequences = []

    def record_apply(self, timing_message):
        applied_sequences.append(timing_message.sequence)
        original_apply(self, timing_message)

    monkeypatch.setattr(LiveSessionStore, "_apply_message", record_apply)
    history = [
        (1, message("DriverList", {"1": {"Tla": "AAA"}}, 1)),
        (2, completed_lap_message(2, 1, session_time=10)),
        (3, car_message(3, session_time=15)),
        (4, completed_lap_message(4, 2, session_time=20)),
    ]
    builder = _CausalSnapshotBuilder()

    first = builder.build(history, CausalAnalysisCut(1, 10.0, 2, 1, NOW))
    second = builder.build(history, CausalAnalysisCut(2, 20.0, 4, 1, NOW))

    assert [lap.lap_number for lap in first.completed_laps] == [1]
    assert [lap.lap_number for lap in second.completed_laps] == [1, 2]
    assert applied_sequences == [1, 2, 3, 4]


def test_causal_cut_excludes_late_old_packet_and_pre_cut_future_data(monkeypatch):
    def summarize_speeds(snapshot, *, force_sector=False):
        return pd.DataFrame(
            [
                {
                    "Driver": "AAA",
                    "LapNumber": 1,
                    "ObservedSpeeds": tuple(
                        sample.speed_kph for sample in snapshot.telemetry_samples
                    ),
                }
            ]
        )

    monkeypatch.setattr(
        "woplanner.live.telemetry._live_traffic_gaps", summarize_speeds
    )
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 1))
    worker.enqueue_message(car_message(2, speed=101, session_time=5))
    worker.enqueue_message(car_message(3, speed=202, session_time=15))
    worker.enqueue_message(completed_lap_message(4, 1, session_time=10))
    # This packet arrived after the cut and must stay out even though its event
    # time is before the leader boundary.
    worker.enqueue_message(car_message(5, speed=303, session_time=6))
    worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )

    results = []
    wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))

    assert results[0].cut is not None
    assert results[0].cut.ingress_sequence == 4
    assert results[0].analysis_input.traffic_rows[0]["ObservedSpeeds"] == (101.0,)
    worker.shutdown()


def test_lapped_driver_completed_laps_are_included_without_reaching_leader_lap():
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(
        message("DriverList", {"1": {"Tla": "AAA"}, "2": {"Tla": "BBB"}}, 1)
    )
    worker.enqueue_message(
        completed_lap_message(2, 28, driver="2", position=2, session_time=280)
    )
    worker.enqueue_message(
        completed_lap_message(3, 30, driver="1", position=1, session_time=300)
    )
    worker.request_analysis_input(
        leader_lap=30,
        use_sector_boundary_traffic=False,
    )

    results = []
    wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))
    laps = {
        (lap.driver_code, lap.lap_number)
        for lap in results[0].analysis_input.completed_laps
    }

    assert ("AAA", 30) in laps
    assert ("BBB", 28) in laps
    assert ("BBB", 30) not in laps
    worker.shutdown()


def test_cut_is_captured_at_lap_crossing_not_later_lap_time_completion():
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(
        message("DriverList", {"1": {"Tla": "AAA"}, "2": {"Tla": "BBB"}}, 1)
    )
    worker.enqueue_message(
        completed_lap_message(2, 28, driver="2", position=2, session_time=280)
    )
    worker.enqueue_message(
        message(
            "TimingData",
            {"Lines": {"1": {"Position": "1", "NumberOfLaps": 30}}},
            3,
            session_time=300,
        )
    )
    # LastLapTime finalises the reducer's leader-lap record, but arrived after
    # the causal cut and therefore must not be pulled backward into it.
    worker.enqueue_message(
        message(
            "TimingData",
            {"Lines": {"1": {"LastLapTime": {"Value": "1:30.000"}}}},
            4,
            session_time=301,
        )
    )
    worker.request_analysis_input(
        leader_lap=30,
        use_sector_boundary_traffic=False,
    )

    results = []
    wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))
    result = results[0]

    assert result.cut.ingress_sequence == 3
    assert result.cut.leader_lap_end_session_time == 300.0
    assert [
        (lap.driver_code, lap.lap_number)
        for lap in result.analysis_input.completed_laps
    ] == [("BBB", 28)]
    worker.shutdown()


def test_unknown_lap_can_become_clean_only_in_a_later_causal_cut(monkeypatch):
    def traffic_from_available_telemetry(snapshot, *, force_sector=False):
        has_traffic = bool(snapshot.telemetry_samples)
        return pd.DataFrame(
            [
                {
                    "Driver": lap.driver_code,
                    "LapNumber": lap.lap_number,
                    "TrafficGapAheadSeconds": 3.0 if has_traffic else pd.NA,
                    "TrafficGapBehindSeconds": 2.0 if has_traffic else pd.NA,
                    "TrafficStatus": "sector_fallback" if has_traffic else "unknown",
                    "TrafficMethod": "SECTOR_BOUNDARY_FALLBACK",
                }
                for lap in snapshot.completed_laps
            ]
        )

    monkeypatch.setattr(
        "woplanner.live.telemetry._live_traffic_gaps",
        traffic_from_available_telemetry,
    )
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(
        message("DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"}}, 1)
    )
    worker.enqueue_message(
        message(
            "TimingAppData",
            {
                "Lines": {
                    "1": {
                        "Stints": {
                            "0": {"Compound": "MEDIUM", "New": "true", "StartLaps": 0}
                        }
                    }
                }
            },
            2,
        )
    )
    worker.enqueue_message(completed_lap_message(3, 1, session_time=10))
    worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=True,
    )
    first = []
    wait_until(lambda: bool(first.extend(worker.poll_analysis_inputs()) or first))

    worker.enqueue_message(car_message(4, speed=200, session_time=9))
    worker.enqueue_message(completed_lap_message(5, 2, session_time=20))
    worker.request_analysis_input(
        leader_lap=2,
        use_sector_boundary_traffic=True,
    )
    second = []
    wait_until(lambda: bool(second.extend(worker.poll_analysis_inputs()) or second))

    common = {
        "use_sector_boundary_traffic": True,
        "quick_lap_threshold": 1.1,
        "min_clean_laps": 1,
        "min_gap_ahead_seconds": 2.5,
        "min_gap_behind_seconds": 1.0,
    }
    first_clean = _live_analysis_laps(first[0].analysis_input, **common)
    second_clean = _live_analysis_laps(second[0].analysis_input, **common)

    assert first_clean.empty
    assert 1 in second_clean["LapNumber"].tolist()
    worker.shutdown()


def test_malformed_packet_does_not_kill_telemetry_worker(monkeypatch):
    original_apply = LiveSessionStore._apply_message

    def fail_one_packet(self, timing_message):
        if timing_message.topic == "BrokenTelemetry":
            raise RuntimeError("bad packet")
        original_apply(self, timing_message)

    monkeypatch.setattr(LiveSessionStore, "_apply_message", fail_one_packet)
    worker = TelemetryAnalysisWorker()
    worker.reset()
    worker.enqueue_message(message("BrokenTelemetry", {}, 1))
    worker.enqueue_message(message("DriverList", {"1": {"Tla": "AAA"}}, 2))
    worker.enqueue_message(completed_lap_message(3, 1))
    worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )
    results = []
    wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))

    diagnostics = worker.diagnostics()
    assert results[0].error is None
    assert diagnostics.thread_alive
    assert diagnostics.last_error == "RuntimeError: bad packet"
    worker.shutdown()


def test_position_history_survives_real_worker_cut_without_late_packet_leakage(monkeypatch):
    observed=[]
    def capture(snapshot, *, force_sector=False):
        observed.extend(snapshot.position_samples)
        return pd.DataFrame()
    monkeypatch.setattr('woplanner.live.telemetry._live_traffic_gaps', capture)
    worker=TelemetryAnalysisWorker()
    try:
        worker.reset()
        worker.enqueue_message(message('DriverList', {'1': {'Tla': 'AAA'}}, 1))
        # Embedded sample UTC is NOW + sequence, so adjusted clock = session_time + sequence.
        worker.enqueue_message(position_message(2, x=101, session_time=3))  # t=5
        worker.enqueue_message(position_message(3, x=202, session_time=12)) # t=15
        worker.enqueue_message(completed_lap_message(4, 1, session_time=10))
        worker.enqueue_message(position_message(5, x=303, session_time=1))  # late t=6
        worker.request_analysis_input(leader_lap=1, use_sector_boundary_traffic=False)
        results=[]
        wait_until(lambda: bool(results.extend(worker.poll_analysis_inputs()) or results))
        assert results[0].error is None
        assert [(s.session_time_seconds,s.x) for s in observed] == [(5.,101.)]
    finally:
        worker.shutdown()
