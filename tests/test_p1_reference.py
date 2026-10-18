"""Manual leader references must waive only rear traffic and remain isolated."""
from dataclasses import replace
import copy

import pandas as pd
import pytest

from wodata.live import CompletedLap, CompletedSector, LiveDriver, LiveSessionSnapshot
from woplanner.analysis.p1_reference import current_p1_driver, select_p1_reference_laps
from woplanner.analysis.service import OfflineAnalysisService, _live_analysis_laps
from wostrategy.algorithm.monte_carlo_race_performance import MonteCarloRacePerformanceConfig


def p1_snapshot():
    laps, sectors, drivers = [], [], []
    for number, code, offset, rank in (("1", "LEA", 0., 1), ("2", "P2", .7, 2), ("3", "P3", 5., 3)):
        drivers.append(LiveDriver(number, code, "Team" + number, code, rank, None))
        for lap in range(1, 9):
            finish = 90. * lap + offset
            laps.append(CompletedLap(number, code, "Team" + number, lap,
                90. + lap * .03, finish, rank, None, "MEDIUM", float(lap), 1,
                True, finish, track_status="1"))
            for index, sector in enumerate(("S1", "S2", "S3"), 1):
                sectors.append(CompletedSector(number, code, "Team" + number, lap,
                    sector, (lap - 1) * 3 + index, 30., finish - (3 - index) * 30.,
                    rank, None, session_time_seconds=finish))
    return replace(LiveSessionSnapshot.empty(), drivers=tuple(drivers), completed_laps=tuple(laps),
                   completed_sectors=tuple(sectors), scheduled_total_laps=56)


def selection(snapshot=None, **kwargs):
    options = dict(driver_code="LEA", first_lap=2, last_lap=8,
                   quick_lap_threshold=1.1, min_clean_laps=4)
    options.update(kwargs)
    return select_p1_reference_laps(snapshot or p1_snapshot(), **options)


def configure(service, monkeypatch):
    config = MonteCarloRacePerformanceConfig(sample_count=256, random_seed=1729,
        fuel_rate_bounds=(.04, .08), track_rate_bounds=(-.01, .01),
        default_compound_degradation_bounds=(.01, .1),
        default_compound_delta_bounds=(-.2, .2), compound_delta_reference="HARD",
        clean_lap_noise_sigma=.35)
    monkeypatch.setattr(service, "_live_retro_config", lambda *a, **k: config)
    return config


def calculate(service, snapshot=None, **kwargs):
    options = dict(year=2026, round_number=17, snapshot=snapshot or p1_snapshot(),
        driver_code="LEA", first_lap=2, last_lap=8, sample_count=256,
        quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True)
    options.update(kwargs)
    return service.calculate_p1_reference(**options)


def test_close_rear_gap_only_is_bypassed_and_original_selection_unchanged():
    snapshot = p1_snapshot()
    options = dict(quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True)
    before = _live_analysis_laps(snapshot, **options)
    assert not before.Driver.eq("LEA").any()
    alternative = selection(snapshot)
    assert alternative.LapNumber.tolist() == list(range(2, 9))
    assert alternative.MeanTimeDeltaToDriverBehind.between(.69, .71).all()
    assert alternative.StintLapNumber.tolist() == list(range(2, 9))
    pd.testing.assert_frame_equal(before, _live_analysis_laps(snapshot, **options))


def test_three_laps_do_not_satisfy_four_lap_run():
    result = selection(first_lap=6, last_lap=8)
    assert result.empty
    assert result.attrs["exclusion_diagnostics"]["insufficient_consecutive_laps"] == 3


@pytest.mark.parametrize("change", [dict(is_valid=False), dict(compound="INTERMEDIATE"),
    dict(track_status="4"), dict(pit_in=True), dict(pit_out=True), dict(lap_time_s=110.),
    dict(tyre_life=None), dict(position=2)])
def test_other_clean_lap_requirements_are_preserved(change):
    snapshot = p1_snapshot()
    snapshot = replace(snapshot, completed_laps=tuple(
        replace(lap, **change) if lap.driver_code == "LEA" and lap.lap_number == 5 else lap
        for lap in snapshot.completed_laps))
    # Both remaining blocks are shorter than the four-lap minimum.
    assert selection(snapshot).empty


def test_missing_ahead_evidence_fails_closed_but_missing_rear_does_not():
    snapshot = p1_snapshot()
    # Leader is the last observed crossing on lap 8; its ahead crossings exist.
    truncated = replace(snapshot, completed_sectors=tuple(s for s in snapshot.completed_sectors
        if s.driver_code == "LEA" or s.lap_number < 8))
    result = selection(truncated)
    assert 8 in result.LapNumber.to_list()
    assert pd.isna(result.loc[result.LapNumber.eq(8), "MeanTimeDeltaToDriverBehind"]).all()
    # With no preceding other-driver crossing there is no physical-ahead evidence.
    isolated = replace(snapshot, completed_sectors=tuple(s for s in snapshot.completed_sectors if s.driver_code == "LEA"))
    assert selection(isolated).empty


def test_close_ahead_is_not_bypassed():
    snapshot = p1_snapshot()
    sectors = tuple(replace(s, session_time_seconds=s.session_time_seconds - .8)
                    if s.driver_code == "P2" else s for s in snapshot.completed_sectors)
    assert selection(replace(snapshot, completed_sectors=sectors)).empty


def test_fastest_lap_context_and_stint_age_are_not_reset_to_interval():
    snapshot = p1_snapshot()
    laps = tuple(replace(lap, lap_time_s=70.) if lap.driver_code == "LEA" and lap.lap_number == 1 else lap
                 for lap in snapshot.completed_laps)
    selected = selection(replace(snapshot, completed_laps=laps))
    assert selected.empty
    assert selected.attrs["exclusion_diagnostics"]["non_quick_lap"] == 7
    assert selection().StintLapNumber.tolist() == list(range(2, 9))


def test_edges_are_applied_before_consecutive_run_requirement():
    selected = selection(exclude_stint_start_laps=1, exclude_stint_end_laps=1)
    assert selected.LapNumber.tolist() == list(range(3, 8))
    assert selection(exclude_stint_start_laps=2, exclude_stint_end_laps=2).empty


def test_selection_rejects_wrong_driver_future_range_and_quarantined_traffic(tmp_path):
    service = OfflineAnalysisService(tmp_path)
    assert calculate(service, driver_code="P2").status == "invalid-selection"
    assert calculate(service, last_lap=9).status == "invalid-selection"
    assert calculate(service, use_sector_boundary_traffic=False).status == "traffic-unavailable"
    result = calculate(service, first_lap=6)
    assert result.status == "waiting-for-clean-laps"
    assert "4 consecutive" in result.message
    assert not list(tmp_path.iterdir())


def test_fit_is_same_method_reproducible_and_never_mutates_standard_state(tmp_path, monkeypatch):
    from wostrategy.model.live_retro_performance import run_live_retro_model

    service = OfflineAnalysisService(tmp_path)
    config = configure(service, monkeypatch)
    before = copy.deepcopy({k: v for k, v in vars(service).items() if k != "tyre_inputs"})
    result = calculate(service)
    expected = run_live_retro_model(selection(), config, as_of_leader_lap=8, race_lap_count=56)
    assert result.status == "updated"
    assert result.summary.manifest["reference_kind"] == "manual_p1"
    expected_rows = [(r.parameter, r.compound, r.team, r.p10, r.median, r.p90) for r in expected.aggregate_snapshot.parameters]
    assert [(r["parameter"], r["compound"], r["team"], r["p10"], r["median"], r["p90"]) for r in result.summary.parameters] == expected_rows
    repeated = calculate(service)
    assert [(r["compound"], r["median"], r["p10"], r["p90"]) for r in result.applicable_parameters] == [
        (r["compound"], r["median"], r["p10"], r["p90"]) for r in repeated.applicable_parameters]
    assert result.applicable_parameters
    assert all(r["parameter"] == "degradation" and r["compound"] == "MEDIUM" and r["team"] == "Team1"
               for r in result.applicable_parameters)
    assert dict(result.sampler_quality)["observed_compounds"] == ("MEDIUM",)
    assert vars(service) == {**before, "tyre_inputs": service.tyre_inputs}
    assert not list(tmp_path.iterdir())


def test_current_leader_uses_published_positions_then_committed_history():
    snapshot = p1_snapshot()
    assert current_p1_driver(snapshot) == "LEA"
    assert current_p1_driver(replace(snapshot, drivers=())) == "LEA"
    missing_leader = tuple(replace(d, position=2) for d in snapshot.drivers)
    assert current_p1_driver(replace(snapshot, drivers=missing_leader)) is None


def test_degenerate_samples_are_diagnosed_and_cannot_be_applied(tmp_path, monkeypatch):
    import woplanner.analysis.p1_reference as module
    service = OfflineAnalysisService(tmp_path)
    configure(service, monkeypatch)
    real_fit = module.run_live_retro_model

    def degenerate_fit(*args, **kwargs):
        fitted = real_fit(*args, **kwargs)
        fitted.quality["numerical_quality_status"] = "degenerate"
        return fitted

    monkeypatch.setattr(module, "run_live_retro_model", degenerate_fit)
    result = calculate(service)
    assert result.summary is not None
    assert result.applicable_parameters == ()
    assert "degenerate" in result.message


def test_p1_calculation_retains_existing_standard_fit_cache_and_fingerprint(tmp_path, monkeypatch):
    service = OfflineAnalysisService(tmp_path)
    config = configure(service, monkeypatch)
    monkeypatch.setattr(service, "live_model_config", lambda *a: dict(
        random_seed=config.random_seed, fuel_rate_bounds=config.fuel_rate_bounds,
        track_rate_bounds=config.track_rate_bounds,
        default_degradation_bounds=config.default_compound_degradation_bounds,
        default_compound_delta_bounds=config.default_compound_delta_bounds,
    ))
    options = dict(year=2026, round_number=17, snapshot=p1_snapshot(), sample_count=256,
        quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True, persist=False)
    standard = service.recalculate_from_live_snapshot(**options)
    assert standard.status == "updated"
    fingerprint = service._last_live_fingerprint
    cached_result = service._ephemeral_result
    cached_parameters = copy.deepcopy(service._ephemeral_summary.parameters)
    cached_inputs = copy.deepcopy(service.last_live_calculation_inputs)
    alternative = calculate(service)
    assert alternative.status == "updated"
    assert service._ephemeral_result is cached_result
    assert service._ephemeral_summary.parameters == cached_parameters
    assert service._last_live_fingerprint == fingerprint
    assert service.last_live_calculation_inputs == cached_inputs
    assert service.recalculate_from_live_snapshot(**options).status == "no-new-clean-laps"
    assert not list(tmp_path.iterdir())


def test_current_leader_with_incomplete_display_metadata_fails_closed():
    from types import SimpleNamespace
    snapshot = SimpleNamespace(drivers=(), completed_laps=(SimpleNamespace(lap_number=35, position=1),))
    assert current_p1_driver(snapshot) is None
