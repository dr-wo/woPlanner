from pathlib import Path
import time

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


FIXTURE = Path(__file__).parents[2] / "woData" / "tests" / "fixtures" / "live_phase_1.txt"


def loaded_service():
    service = LivePlannerService(LiveTimingController())
    service.start_replay(ReplayConfig(FIXTURE, speed=0))
    deadline = time.monotonic() + 2
    while service.controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    service.poll()
    return service


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
