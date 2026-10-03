import json
import math
import time
from pathlib import Path

import pytest

from types import MappingProxyType

from wodata.live import (
    CompletedLap,
    CompletedSector,
    ProvisionalSector,
    LiveSessionSnapshot,
    LiveTimingController,
    ReplayConfig,
)
from woplanner.live.service import (
    REFERENCE_MODE_ACTUAL_PACE,
    REFERENCE_MODE_AVERAGED_PACE,
    LivePlannerService,
    _sector_progress_fractions,
)
from woplanner.live.telemetry import ReplayPipelineConfig


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
    assert bbb["x_lap"].round(6).tolist() == [0.333333, 0.677778, 1.0]
    assert bbb["delta_s"].tolist() == [1.0, 1.5, 3.0]


def test_sector_geometry_uses_one_reference_lap_time_fraction_for_all_drivers():
    sectors = (
        CompletedSector("1", "AAA", "A", 1, "S1", 1, 20, 20, 1, None),
        CompletedSector("1", "AAA", "A", 1, "S2", 2, 30, 50, 1, None),
        CompletedSector("1", "AAA", "A", 1, "S3", 3, 50, 100, 1, None),
        CompletedSector("2", "BBB", "B", 1, "S1", 1, 40, 40, 2, None),
        CompletedSector("2", "BBB", "B", 1, "S2", 2, 20, 60, 2, None),
        CompletedSector("2", "BBB", "B", 1, "S3", 3, 40, 100, 2, None),
    )
    fractions, source = _sector_progress_fractions(sectors, "AAA")
    assert source == "reference_time_fraction"
    assert fractions == pytest.approx({"S1": .2, "S2": .5, "S3": 1.0})


def test_sector_geometry_equal_thirds_fallback_without_complete_reference_lap():
    fractions, source = _sector_progress_fractions((), "AAA")
    assert source == "legacy_equal_thirds"
    assert fractions == pytest.approx({"S1": 1 / 3, "S2": 2 / 3, "S3": 1.0})


def test_averaged_reference_continues_same_definition_after_reference_retires():
    service = LivePlannerService()
    sectors = []
    cumulative = {"AAA": 0.0, "BBB": 0.0}
    times = {"AAA": (20.0, 30.0, 50.0), "BBB": (21.0, 31.0, 51.0)}
    for driver, last_lap in (("AAA", 1), ("BBB", 3)):
        for lap in range(1, last_lap + 1):
            for index, value in enumerate(times[driver], 1):
                cumulative[driver] += value
                sectors.append(CompletedSector(
                    str(index), driver, driver, lap, f"S{index}", (lap - 1) * 3 + index,
                    value, cumulative[driver], 1 if driver == "AAA" else 2, None,
                ))
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}), None, (), (), MappingProxyType({}), None, tuple(sectors)
    )
    plot = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_AVERAGED_PACE)
    bbb = plot.loc[plot.driver.eq("BBB")]
    reference = bbb["cumulative_time_s"].to_numpy() - bbb["delta_s"].to_numpy()
    assert reference.tolist() == pytest.approx([20, 50, 100, 120, 150, 200, 220, 250, 300])
    service.shutdown()


@pytest.fixture
def r15_rus_lap2_sectors():
    # R15 recording, seq 4213: the first committed RUS lap is logical lap 2.
    return tuple(
        CompletedSector("63", "RUS", "Mercedes", 2, f"S{sector}", 3 + sector,
                        duration, cumulative, 1, None)
        for sector, duration, cumulative in (
            (1, 39.418, 151.826), (2, 44.492, 196.318), (3, 25.625, 221.943)
        )
    )


def test_r15_averaged_reference_keeps_origin_when_peer_early_rows_disappear(
    r15_rus_lap2_sectors,
):
    service = LivePlannerService()
    # Preserve the diagnosed canonical input shapes independently of the reducer
    # fix: STR's old pseudo-lap-1 rows disappear on its lap-2 commit at seq 4593.
    # These STR rows must never define the origin of the selected RUS reference.
    peer_cuts = (
        (1, ((40.512, 40.512), (45.788, 86.300), (26.149, 112.449))),
        (2, ((40.512, 168.802), (45.788, 214.590), (25.853, 240.443))),
    )
    retained = []
    try:
        for lap, values in peer_cuts:
            peer_sectors = tuple(
                CompletedSector("18", "STR", "Aston Martin", lap, f"S{sector}",
                                (lap - 1) * 3 + sector, duration, cumulative, 21, None)
                for sector, (duration, cumulative) in enumerate(values, 1)
            )
            service.snapshot = LiveSessionSnapshot(
                MappingProxyType({}), "Started", (), (), MappingProxyType({}), None,
                r15_rus_lap2_sectors + peer_sectors,
            )
            plot = service.build_plot_data("RUS", REFERENCE_MODE_AVERAGED_PACE)
            rus = plot.loc[plot.driver.eq("RUS")].reset_index(drop=True)
            assert rus["segment_index"].tolist() == [4, 5, 6]
            assert rus["segment_time_s"].tolist() == pytest.approx([39.418, 44.492, 25.625])
            assert rus["cumulative_time_s"].tolist() == pytest.approx([151.826, 196.318, 221.943])
            assert rus["reference_cumulative_time_s"].tolist() == pytest.approx(
                [148.953, 193.445, 219.070]
            )
            assert rus["delta_s"].tolist() == pytest.approx([2.873] * 3)
            retained.append(rus)
        assert retained[0]["reference_cumulative_time_s"].equals(
            retained[1]["reference_cumulative_time_s"]
        )
        assert retained[0]["delta_s"].equals(retained[1]["delta_s"])
    finally:
        service.shutdown()


def test_r15_new_reference_sector_sample_updates_historical_average(
    r15_rus_lap2_sectors,
):
    service = LivePlannerService()
    plots = []
    # R15 seq 4808: a valid lap-3 S1 sample changes the S1 mean to 39.135.
    new_sample = CompletedSector(
        "63", "RUS", "Mercedes", 3, "S1", 7, 38.852, 260.795, 1, None
    )
    try:
        for added_sectors in ((), (new_sample,)):
            service.snapshot = LiveSessionSnapshot(
                MappingProxyType({}), "Started", (), (), MappingProxyType({}), None,
                r15_rus_lap2_sectors + added_sectors,
            )
            plot = service.build_plot_data("RUS", REFERENCE_MODE_AVERAGED_PACE)
            plots.append(plot.loc[plot.lap.eq(2)].reset_index(drop=True))
        before, after = plots
        assert before["cumulative_time_s"].equals(after["cumulative_time_s"])
        assert before["reference_cumulative_time_s"].tolist() == pytest.approx(
            [148.953, 193.445, 219.070]
        )
        assert after["reference_cumulative_time_s"].tolist() == pytest.approx(
            [148.387, 192.879, 218.504]
        )
        assert after["delta_s"].tolist() == pytest.approx([3.439] * 3)
    finally:
        service.shutdown()


def test_averaged_lap_fallback_uses_logical_lap_with_missing_early_history():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (),
        (CompletedLap("63", "RUS", "Mercedes", 2, 109.535, 221.943, 1, None),),
        MappingProxyType({}), None,
    )
    try:
        plot = service.build_plot_data("RUS", REFERENCE_MODE_AVERAGED_PACE)
        assert plot["segment_index"].tolist() == [6]
        assert plot["reference_cumulative_time_s"].tolist() == pytest.approx([219.070])
        assert plot["delta_s"].tolist() == pytest.approx([2.873])
    finally:
        service.shutdown()


def test_unavailable_peer_lap_does_not_change_valid_reference_lap_fallbacks():
    service = LivePlannerService()
    reference_laps = (
        CompletedLap("1", "AAA", "A", 1, 90.0, 90.0, 1, None),
        CompletedLap("1", "AAA", "A", 2, 90.0, 180.0, 1, None),
    )
    # Source-less restart: the peer's lap duration and race-time base are unknown.
    unavailable_peer = CompletedLap("2", "BBB", "B", 3, float("nan"), None, 2, None)
    try:
        for mode in (REFERENCE_MODE_AVERAGED_PACE, REFERENCE_MODE_ACTUAL_PACE):
            results = []
            for laps in (reference_laps, reference_laps + (unavailable_peer,)):
                service.snapshot = LiveSessionSnapshot(
                    MappingProxyType({}), "Started", (), laps, MappingProxyType({}), None,
                )
                plot = service.build_plot_data("AAA", reference_mode=mode)
                results.append(plot)

            before, after = results
            aaa_before = before.loc[before.driver.eq("AAA")].reset_index(drop=True)
            aaa_after = after.loc[after.driver.eq("AAA")].reset_index(drop=True)
            assert aaa_before["reference_cumulative_time_s"].tolist() == pytest.approx(
                [90.0, 180.0]
            )
            assert aaa_after["reference_cumulative_time_s"].tolist() == pytest.approx(
                [90.0, 180.0]
            )
            assert aaa_before["delta_s"].tolist() == pytest.approx([0.0, 0.0])
            assert aaa_after["delta_s"].tolist() == pytest.approx([0.0, 0.0])
            assert aaa_after["timing_available"].all()

            peer = after.loc[after.driver.eq("BBB")].iloc[0]
            assert peer.segment_index == 9
            assert math.isnan(float(peer.lap_time_s))
            assert math.isnan(float(peer.cumulative_time_s))
            assert math.isnan(float(peer.delta_s))
            assert not peer.timing_available
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ((("S1", 1, 20.0),), {1: 20.0}),
        ((("S1", 1, 20.0), ("S2", 2, 30.0)), {1: 20.0, 2: 50.0}),
        # A genuine S3 sector cannot be treated as a full lap fallback.
        ((("S3", 3, 50.0),), {}),
        # cumsum must not silently skip a missing S2 mean to make S3 finite.
        ((("S1", 1, 20.0), ("S3", 3, 50.0)), {1: 20.0}),
    ],
)
def test_partial_sector_reference_requires_only_means_through_logical_coordinate(
    samples, expected,
):
    service = LivePlannerService()
    reference_sectors = tuple(
        CompletedSector("1", "AAA", "A", 1, sector, index, duration, index * 30, 1, None)
        for sector, index, duration in samples
    )
    # Peers can expose later coordinates but cannot supply missing reference means.
    peer_sectors = tuple(
        CompletedSector("2", "BBB", "B", (index - 1) // 3 + 1,
                        f"S{(index - 1) % 3 + 1}", index, 30.0, index * 30.0, 2, None)
        for index in range(1, 7)
    )
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), (), MappingProxyType({}), None,
        reference_sectors + peer_sectors,
    )
    try:
        plot = service.build_plot_data("AAA", REFERENCE_MODE_AVERAGED_PACE)
        for row in plot.itertuples():
            if row.segment_index in expected:
                assert row.reference_cumulative_time_s == pytest.approx(expected[row.segment_index])
                assert row.timing_available
            else:
                assert math.isnan(float(row.reference_cumulative_time_s))
                assert math.isnan(float(row.delta_s))
                assert not row.timing_available
    finally:
        service.shutdown()


def test_live_plot_consumes_canonical_cumulative_time_without_repairing_it():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        None,
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 1, 90.0, 90.0, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 1, 91.0, 91.0, 2, None),
            CompletedLap("1", "AAA", "Mercedes", 2, 95.0, 1985.0, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 2, 97.0, 1998.0, 2, None),
        ),
        MappingProxyType({}),
        None,
    )

    plot_data = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE)

    assert plot_data.loc[plot_data.driver.eq("AAA"), "cumulative_time_s"].tolist() == [90.0, 1985.0]
    assert plot_data.loc[plot_data.driver.eq("BBB"), "cumulative_time_s"].tolist() == [91.0, 1998.0]
    assert plot_data.loc[plot_data.driver.eq("BBB"), "delta_s"].tolist() == [1.0, 13.0]


def test_later_sector_data_only_replaces_its_exact_lap_fallback():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        None,
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 1, 90.0, 90.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 2, 91.0, 181.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 3, 92.0, 273.0, 1, None),
        ),
        MappingProxyType({}),
        None,
        (
            CompletedSector("1", "AAA", "Mercedes", 3, "S1", 7, 30.0, 211.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S2", 8, 31.0, 242.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S3", 9, 31.0, 273.0, 1, None),
        ),
    )

    rows, _fractions, _source = service._plot_rows("AAA")

    assert [(row["lap"], row["sector"], row["segment_index"]) for row in rows] == [
        (3, "S1", 7),
        (3, "S2", 8),
        (3, "S3", 9),
        (1, "S3", 3),
        (2, "S3", 6),
    ]


def test_actual_reference_keeps_exact_mixed_resolution_rows_coherent():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        None,
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 1, 90.0, 90.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 2, 91.0, 181.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 3, 93.0, 274.0, 1, None),
        ),
        MappingProxyType({}),
        None,
        (
            CompletedSector("1", "AAA", "Mercedes", 3, "S1", 7, 30.0, 211.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S2", 8, 32.0, 243.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S3", 9, 31.0, 274.0, 1, None),
        ),
    )

    plot = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE)

    assert plot["segment_index"].tolist() == [3, 6, 7, 8, 9]
    assert plot["delta_s"].tolist() == pytest.approx([0.0] * 5)


def test_averaged_reference_uses_homogeneous_lap_resolution_with_fallbacks():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        None,
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 1, 90.0, 90.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 2, 91.0, 181.0, 1, None),
            CompletedLap("1", "AAA", "Mercedes", 3, 93.0, 274.0, 1, None),
        ),
        MappingProxyType({}),
        None,
        (
            CompletedSector("1", "AAA", "Mercedes", 3, "S1", 7, 30.0, 211.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S2", 8, 32.0, 243.0, 1, None),
            CompletedSector("1", "AAA", "Mercedes", 3, "S3", 9, 31.0, 274.0, 1, None),
        ),
    )

    plot = service.build_plot_data("AAA", reference_mode=REFERENCE_MODE_AVERAGED_PACE)

    assert plot["segment_index"].tolist() == [3, 6, 9]
    assert plot["segment_time_s"].tolist() == pytest.approx([90.0, 91.0, 93.0])
    assert plot["delta_s"].tolist() == pytest.approx([-4 / 3, -5 / 3, 0.0])


def test_unavailable_canonical_rows_survive_without_false_delta_then_recover():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 8, 90.0, None, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 8, 91.0, None, 2, None),
        ),
        MappingProxyType({"1": 1, "2": 2}),
        None,
    )

    unavailable = service.build_plot_data(
        "AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE
    )

    assert unavailable[["driver", "lap"]].to_dict("records") == [
        {"driver": "AAA", "lap": 8},
        {"driver": "BBB", "lap": 8},
    ]
    assert unavailable["cumulative_time_s"].isna().all()
    assert unavailable["reference_cumulative_time_s"].isna().all()
    assert unavailable["delta_s"].isna().all()
    assert not unavailable["timing_available"].any()
    assert len(service._last_plot_data) == 2

    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        (
            CompletedLap("1", "AAA", "Mercedes", 8, 90.0, 800.0, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 8, 91.0, 802.0, 2, None),
        ),
        MappingProxyType({"1": 1, "2": 2}),
        None,
    )
    recovered = service.build_plot_data(
        "AAA", reference_mode=REFERENCE_MODE_ACTUAL_PACE
    )

    assert recovered["timing_available"].all()
    assert recovered["delta_s"].tolist() == pytest.approx([0.0, 2.0])


def test_pending_restart_sector_never_produces_a_finite_planner_delta():
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (),
        (),
        MappingProxyType({}),
        None,
        (
            CompletedSector(
                "1", "AAA", "Mercedes", 3, "S1", 7, 600.0, None, 1, None
            ),
            CompletedSector(
                "2", "BBB", "Ferrari", 3, "S1", 7, 30.0, 220.0, 2, None
            ),
        ),
    )

    plot = service.build_plot_data("BBB", reference_mode=REFERENCE_MODE_ACTUAL_PACE)
    pending = plot.loc[plot["driver"].eq("AAA")].iloc[0]

    assert pending["segment_time_s"] == 600.0
    assert pending["reference_cumulative_time_s"] == pytest.approx(220.0)
    assert bool(pending["timing_available"]) is False
    assert math.isnan(float(pending["delta_s"]))


def test_driver_styles_and_ranks_are_stable():
    service = loaded_service()
    first = service.available_drivers()
    second = service.available_drivers()
    assert first[["driver", "colour", "line_style"]].equals(second[["driver", "colour", "line_style"]])
    assert service.default_reference_driver() == "AAA"
    assert set(service.build_driver_ranks()["driver"]) == {"AAA", "BBB"}


def test_plot_requires_explicit_provisional_opt_in_and_clears_cached_overlay_data():
    from dataclasses import replace

    service = LivePlannerService()
    provisional = ProvisionalSector(
        "1", "AAA", "A", 1, "S1", 1, 30.0, 30.0, 1, None, source_time_s=30.0
    )
    service.snapshot = replace(LiveSessionSnapshot.empty(), provisional_sectors=(provisional,))
    try:
        assert service.build_plot_data("AAA").empty
        live = service.build_plot_data("AAA", include_provisional=True)
        assert len(live) == 1
        assert live.iloc[0].timing_state == "provisional"
        assert live.iloc[0].delta_s == pytest.approx(0.0)
        assert service.snapshot.completed_sectors == ()
        final = service.build_plot_data("AAA", include_provisional=False)
        assert final.empty and service._last_plot_data.empty
        assert service.build_pit_window_data("AAA", 20.0).empty
    finally:
        service.shutdown()


def test_provisional_reference_evolves_live_average_without_changing_canonical_plot(
    r15_rus_lap2_sectors,
):
    from dataclasses import replace

    service = LivePlannerService()
    service.snapshot = replace(
        LiveSessionSnapshot.empty(), completed_sectors=r15_rus_lap2_sectors,
        provisional_sectors=(ProvisionalSector(
            "63", "RUS", "Mercedes", 3, "S1", 7, 38.852, 260.795, 1, None,
            source_time_s=260.795,
        ),),
    )
    try:
        canonical = service.build_plot_data("RUS")
        live = service.build_plot_data("RUS", include_provisional=True)
        assert canonical.segment_index.tolist() == [4, 5, 6]
        assert live.segment_index.tolist() == [4, 5, 6, 7]
        assert canonical.reference_cumulative_time_s.tolist() == pytest.approx([148.953, 193.445, 219.070])
        assert live.loc[live.lap.eq(2), "reference_cumulative_time_s"].tolist() == pytest.approx([148.387, 192.879, 218.504])
        assert service.build_plot_data("RUS").equals(canonical)
    finally:
        service.shutdown()
