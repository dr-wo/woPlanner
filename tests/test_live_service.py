import json
import time
from pathlib import Path

import pytest

from types import MappingProxyType

from wodata.live import (
    CompletedLap,
    CompletedSector,
    LiveSessionSnapshot,
    LiveTimingController,
    ReplayConfig,
)
from woplanner.live_service import (
    REFERENCE_MODE_ACTUAL_PACE,
    REFERENCE_MODE_AVERAGED_PACE,
    LivePlannerService,
)
from woplanner.telemetry_worker import ReplayPipelineConfig


FIXTURE = Path(__file__).parents[2] / "woData" / "tests" / "fixtures" / "live_phase_1.txt"


def loaded_service():
    service = LivePlannerService(LiveTimingController())
    service.start_replay(ReplayConfig(FIXTURE, speed=0))
    deadline = time.monotonic() + 2
    while service.controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    service.poll()
    return service


def test_live_planner_accepts_project_level_replay_pipeline_config():
    config = ReplayPipelineConfig(
        replay_telemetry_high_watermark=32,
        replay_telemetry_low_watermark=12,
        replay_mc_max_queued_requests=2,
    )

    service = LivePlannerService(replay_pipeline_config=config)

    assert service.telemetry_worker.replay_pipeline_config is config
    service.shutdown()


def test_jsonl_archive_replay_reaches_planner_plot_pipeline(tmp_path):
    path = tmp_path / "merged_replay_v1.jsonl"
    events = [
        {
            "schema_version": 1,
            "topic": "DriverList",
            "session_time_seconds": 0.0,
            "sequence": 1,
            "payload": {
                "1": {
                    "Tla": "AAA",
                    "FullName": "Archive Driver",
                    "TeamName": "Mercedes",
                }
            },
            "source": "f1_archive_replay",
        },
        {
            "schema_version": 1,
            "topic": "TimingAppData",
            "session_time_seconds": 0.5,
            "sequence": 2,
            "payload": {
                "Lines": {
                    "1": {
                        "Stints": {
                            "0": {"Compound": "MEDIUM", "New": "true", "StartLaps": 0}
                        }
                    }
                }
            },
            "source": "f1_archive_replay",
        },
        {
            "schema_version": 1,
            "topic": "TimingData",
            "session_time_seconds": 90.0,
            "sequence": 3,
            "payload": {
                "Lines": {
                    "1": {
                        "Position": "1",
                        "NumberOfLaps": 1,
                        "LastLapTime": {"Value": "01:30.000"},
                    }
                }
            },
            "source": "f1_archive_replay",
        },
        {
            "schema_version": 1,
            "topic": "TimingAppData",
            "session_time_seconds": 91.0,
            "sequence": 4,
            "payload": {
                "Lines": {
                    "1": {
                        "Stints": {
                            "1": {"Compound": "HARD", "New": "true", "StartLaps": 0}
                        }
                    }
                }
            },
            "source": "f1_archive_replay",
        },
        {
            "schema_version": 1,
            "topic": "TimingData",
            "session_time_seconds": 181.0,
            "sequence": 5,
            "payload": {
                "Lines": {
                    "1": {
                        "Position": "1",
                        "NumberOfLaps": 2,
                        "LastLapTime": {"Value": "01:31.000"},
                    }
                }
            },
            "source": "f1_archive_replay",
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    service = LivePlannerService()
    service.start_replay(ReplayConfig(path, speed=0))
    deadline = time.monotonic() + 2
    while service.controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    update = service.poll()

    assert update.snapshot.drivers[0].driver_code == "AAA"
    assert len(update.snapshot.completed_laps) == 2
    plot_data = service.build_plot_data("AAA")
    assert plot_data[["lap", "compound", "tyre_age"]].to_dict("records") == [
        {"lap": 1, "compound": "MEDIUM", "tyre_age": 1.0},
        {"lap": 2, "compound": "HARD", "tyre_age": 1.0},
    ]
    tyre_events = service.build_tyre_event_data()
    assert tyre_events[["lap", "compound", "tyre_age_label"]].to_dict("records") == [
        {"lap": 2, "compound": "HARD", "tyre_age_label": "0"}
    ]
    ranks = service.build_driver_ranks().iloc[0]
    assert ranks["start_tyre_compound"] == "MEDIUM"
    assert ranks["latest_tyre_compound"] == "HARD"

    # Request both cuts after the zero-delay replay has already delivered its
    # future events. Lap 1 must still be reconstructed at ingress 3, before the
    # lap-2 HARD stint update and completion arrived.
    first_request = service.telemetry_worker.request_analysis_input(
        leader_lap=1,
        use_sector_boundary_traffic=False,
    )
    second_request = service.telemetry_worker.request_analysis_input(
        leader_lap=2,
        use_sector_boundary_traffic=False,
    )
    prepared = []
    deadline = time.monotonic() + 2
    while len(prepared) < 2 and time.monotonic() < deadline:
        prepared.extend(service.telemetry_worker.poll_analysis_inputs())
        time.sleep(0.005)
    by_request = {result.request_id: result for result in prepared}

    assert by_request[first_request].cut.ingress_sequence == 3
    assert by_request[second_request].cut.ingress_sequence == 5
    assert [
        (lap.lap_number, lap.compound)
        for lap in by_request[first_request].analysis_input.completed_laps
    ] == [(1, "MEDIUM")]
    assert [
        (lap.lap_number, lap.compound)
        for lap in by_request[second_request].analysis_input.completed_laps
    ] == [(1, "MEDIUM"), (2, "HARD")]
    service.shutdown()


def test_replay_source_waits_for_analysis_capacity_without_dropping_events(tmp_path):
    path = tmp_path / "paced_replay.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "topic": "Heartbeat",
                    "session_time_seconds": float(sequence),
                    "sequence": sequence,
                    "payload": {},
                    "source": "f1_archive_replay",
                }
            )
            + "\n"
            for sequence in range(1, 4)
        ),
        encoding="utf-8",
    )
    service = LivePlannerService()
    has_capacity = [False]
    service.telemetry_worker.set_bounded_replay_mc_pipeline_capacity_probe(
        lambda: has_capacity[0]
    )

    service.start_replay(ReplayConfig(path, speed=0))
    time.sleep(0.03)

    assert service.controller.is_running
    assert service.telemetry_worker.diagnostics().latest_ingress_sequence == 1

    has_capacity[0] = True
    deadline = time.monotonic() + 2
    while service.controller.is_running and time.monotonic() < deadline:
        time.sleep(0.005)

    assert not service.controller.is_running
    service.poll()
    assert service.telemetry_worker.diagnostics().latest_ingress_sequence == 3
    service.shutdown()


def test_stopping_replay_interrupts_analysis_backpressure(tmp_path):
    path = tmp_path / "blocked_replay.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "Heartbeat",
                "session_time_seconds": 1.0,
                "sequence": 1,
                "payload": {},
                "source": "f1_archive_replay",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    service = LivePlannerService()
    service.telemetry_worker.set_bounded_replay_mc_pipeline_capacity_probe(lambda: False)
    service.start_replay(ReplayConfig(path, speed=0))
    time.sleep(0.02)

    service.stop()

    assert not service.controller.is_running
    service.shutdown()


def test_reference_rebases_actual_completed_laps_only():
    service = loaded_service()
    aaa = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE)
    bbb = service.build_plot_data("BBB", reference_mode=REFERENCE_MODE_ACTUAL_PACE)
    assert aaa.loc[aaa.driver.eq("AAA"), "delta_s"].tolist() == [0.0, 0.0, 0.0]
    assert aaa.loc[aaa.driver.eq("BBB"), "delta_s"].tolist() == [1.0, 3.0, 4.0]
    assert bbb.loc[bbb.driver.eq("AAA"), "delta_s"].tolist() == [-1.0, -3.0, -4.0]
    assert set(aaa["sector"]) == {"S3"}
    assert len(aaa) == 6
    assert not aaa["is_simulated"].any()


def test_averaged_reference_uses_selected_driver_average_lap_time():
    service = loaded_service()
    plot_data = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_AVERAGED_PACE)
    aaa = plot_data.loc[plot_data.driver.eq("AAA"), "delta_s"].round(6).tolist()
    bbb = plot_data.loc[plot_data.driver.eq("BBB"), "delta_s"].round(6).tolist()
    assert aaa == [-0.833333, -1.166667, 0.0]
    assert bbb == [0.166667, 1.833333, 4.0]


def test_sector_rows_are_used_when_available():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        None,
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 1, 90.0, 90.0, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 1, 93.0, 93.0, 2, None),
        ),
        MappingProxyType({}),
        None,
        (
            CompletedSector("1", "AAA", "Mercedes", 1, "S1", 1, 30.0, 30.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 1, "S2", 2, 31.0, 61.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 1, "S3", 3, 29.0, 90.0, 1, None),
            CompletedSector("2", "BBB", "Ferrari", 1, "S1", 1, 31.0, 31.0, 2, None),
            CompletedSector("2", "BBB", "Ferrari", 1, "S2", 2, 31.5, 62.5, 2, None),
            CompletedSector("2", "BBB", "Ferrari", 1, "S3", 3, 30.5, 93.0, 2, None),
        ),
    )
    plot_data = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE)
    bbb = plot_data.loc[plot_data.driver.eq("BBB")]
    assert bbb["sector"].tolist() == ["S1", "S2", "S3"]
    assert bbb["x_lap"].round(6).tolist() == [0.666667, 1.0, 1.333333]
    assert bbb["delta_s"].tolist() == [1.0, 1.5, 3.0]


def test_driver_styles_and_ranks_are_stable():
    service = loaded_service()
    first = service.available_drivers()
    second = service.available_drivers()
    assert first[["driver", "colour", "line_style"]].equals(second[["driver", "colour", "line_style"]])
    assert service.default_reference_driver() == "AAA"
    assert set(service.build_driver_ranks()["driver"]) == {"AAA", "BBB"}
