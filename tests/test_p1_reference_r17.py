"""Real R17 delivery prefixes: rear-gap waiver, refusal cases, standard isolation."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from wodata.artifacts import write_weekend_model_config
from wodata.live import CompletedLap, CompletedSector, LiveDriver
from wodata.live.replay.schema import parse_timestamp
from woplanner.analysis.service import OfflineAnalysisService, _live_analysis_laps
from woplanner.analysis.worker import LiveAnalysisInput, P1ReferenceProcessService


@pytest.fixture
def prefixes():
    evidence = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/r17_p1_prefixes.json.gz").read_bytes()))
    result = {}
    for key, cut in evidence["cuts"].items():
        def records(name, cls):
            return tuple(cls(**{**row, "source_timestamp_utc": parse_timestamp(row["source_timestamp_utc"])})
                         for row in cut[name])
        result[int(key)] = LiveAnalysisInput(
            records("drivers", LiveDriver), records("completed_laps", CompletedLap),
            records("completed_sectors", CompletedSector), (), scheduled_total_laps=cut["scheduled_total_laps"],
        )
    return result


@pytest.fixture
def service(tmp_path):
    # Original pre-race configured bounds; no retrospective fit or race cache.
    write_weekend_model_config(dict(
        random_seed=3900707354, fuel_rate_bounds=(0., .1), track_rate_bounds=(-.05, .05),
        default_degradation_bounds=(0., .5), default_compound_delta_bounds=(-1.2, .5),
        reference_compound="HARD", clean_lap_noise_sigma=.5, enforce_compound_order=True,
    ), year=2026, round_number=17, data_root=tmp_path)
    return OfflineAnalysisService(tmp_path)


def options(snapshot, last_lap):
    return dict(year=2026, round_number=17, snapshot=snapshot, driver_code="VER",
                first_lap=3, last_lap=last_lap, sample_count=256, quick_lap_threshold=1.1,
                min_clean_laps=4, use_sector_boundary_traffic=True)


@pytest.mark.parametrize("cut,expected", [(12, (3, 4, 5, 6, 7)),
    (13, (3, 4, 5, 6, 7, 10, 11, 12, 13)), (14, (3, 4, 5, 6, 7, 10, 11, 12, 13, 14))])
def test_opening_p1_fit_uses_only_valid_observed_prefix_laps(service, prefixes, cut, expected):
    snapshot = prefixes[cut]
    before = dict(vars(service))
    original_files = {path: path.read_bytes() for path in Path(service.data_root).rglob("*") if path.is_file()}
    standard = _live_analysis_laps(snapshot, quick_lap_threshold=1.1, min_clean_laps=4,
                                   use_sector_boundary_traffic=True)
    assert not standard.Driver.eq("VER").any()
    result = service.calculate_p1_reference(**options(snapshot, cut))
    assert result.status == "updated"
    assert result.selected_lap_numbers == expected
    assert result.eligible_lap_count == len(expected)
    assert result.summary.manifest["reference_kind"] == "manual_p1"
    assert dict(result.sampler_quality)["sample_count"] == 256
    assert dict(result.sampler_quality)["observed_compounds"] == ("SOFT",)
    assert result.applicable_parameters
    assert all(row["team"] == "Red Bull Racing" and row["compound"] == "SOFT"
               and row["parameter"] == "degradation" for row in result.applicable_parameters)
    assert all(vars(service)[key] is value for key, value in before.items())
    assert {path: path.read_bytes() for path in Path(service.data_root).rglob("*") if path.is_file()} == original_files
    future = service.calculate_p1_reference(**{**options(snapshot, cut), "last_lap": cut + 1})
    assert future.status == "invalid-selection"


def test_r17_l12_to_l14_refuses_short_run_without_relaxing_requirements(service, prefixes):
    result = service.calculate_p1_reference(**{**options(prefixes[14], 14), "first_lap": 12})
    assert result.status == "waiting-for-clean-laps"
    assert result.summary is None and not result.applicable_parameters
    assert dict(result.exclusion_diagnostics)["insufficient_consecutive_laps"] == 3
    assert "4 consecutive" in result.message


def test_r17_real_spawned_worker_returns_separate_fit(service, prefixes):
    import time

    worker = P1ReferenceProcessService(service.data_root)
    params = options(prefixes[14], 14)
    snapshot = params.pop("snapshot")
    year, round_number = params.pop("year"), params.pop("round_number")
    try:
        worker.submit(year=year, round_number=round_number, snapshot=snapshot, options=params)
        result = None
        deadline = time.monotonic() + 30
        while result is None and time.monotonic() < deadline:
            result = worker.poll()
            if result is None:
                time.sleep(.01)
        assert result is not None, "P1 worker did not complete"
        assert result.status == "updated"
        assert result.selected_lap_numbers == (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)
        assert service._ephemeral_result is None
    finally:
        worker.shutdown()
