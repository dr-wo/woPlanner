"""Visible fallback changes selection semantics, never the Sector numerical contract."""
from dataclasses import replace
import json

import pandas as pd
import pytest

from wodata.artifacts import write_model_snapshot, write_weekend_model_config
from wodata.live import CompletedLap
from woplanner.analysis.service import (
    OfflineAnalysisService, _live_analysis_laps, _live_traffic_gaps,
    live_traffic_provenance, live_traffic_method_label, LIVE_SECTOR_METHOD_VERSION,
)
from test_analysis_service import model_snapshot, snapshot_with_clear_sector_traffic


@pytest.fixture
def scenario(tmp_path):
    write_weekend_model_config({
        "sample_count": 100, "random_seed": 42,
        "fuel_rate_bounds": [0., .1], "track_rate_bounds": [-.05, .05],
        "default_degradation_bounds": [0., .2],
        "default_compound_delta_bounds": [-1., 1.],
        "reference_compound": "HARD", "expected_sessions": ["R"],
    }, year=2026, round_number=5, data_root=tmp_path)
    laps = tuple(CompletedLap(
        "1", "AAA", "Team", lap, 90. + lap * .1, 90. * lap,
        1, None, "MEDIUM", float(lap), 1, True,
    ) for lap in range(1, 5))
    return snapshot_with_clear_sector_traffic(laps), dict(
        year=2026, round_number=5, sample_count=100, quick_lap_threshold=1.1,
        min_clean_laps=3,
    )


def test_automatic_executes_identical_sector_mc_and_persists_provenance(tmp_path, scenario):
    snapshot, options = scenario
    automatic = OfflineAnalysisService(tmp_path)
    explicit = OfflineAnalysisService(tmp_path)
    first = automatic.recalculate_from_live_snapshot(snapshot=snapshot, **options)
    other = explicit.recalculate_from_live_snapshot(
        snapshot=snapshot, **options, use_sector_boundary_traffic=True, persist=False,
    )
    assert first.status == other.status == "updated"
    assert first.eligible_lap_count == other.eligible_lap_count == 4
    loaded = OfflineAnalysisService(tmp_path).load_model(2026, 5)
    numerical = ["parameter", "compound", "team", "p10", "median", "p90"]
    left = pd.DataFrame(loaded.parameters)[numerical].fillna("").astype(str)
    right = pd.DataFrame(explicit.load_model(2026, 5).parameters)[numerical].fillna("").astype(str)
    pd.testing.assert_frame_equal(left, right)
    provenance = live_traffic_provenance(loaded.manifest)
    assert provenance["requested_traffic_mode"] == "automatic"
    assert provenance["effective_traffic_method"] == "SECTOR_BOUNDARY_FALLBACK"
    assert provenance["traffic_method_version"] == LIVE_SECTOR_METHOD_VERSION
    assert provenance["calculated_at"] and provenance["input_snapshot_id"]
    assert live_traffic_provenance(automatic.model_history(2026, 5)[-1]) == provenance
    assert live_traffic_provenance(explicit.load_model(2026, 5).manifest)["requested_traffic_mode"] == "sector"
    assert "requested: Automatic" in live_traffic_method_label(loaded.manifest)


def test_changed_request_recalculates_without_relabelling_old_result(tmp_path, scenario):
    snapshot, options = scenario
    service = OfflineAnalysisService(tmp_path)
    first = service.recalculate_from_live_snapshot(snapshot=snapshot, **options, persist=False)
    original = service.load_model(2026, 5)
    assert service.recalculate_from_live_snapshot(snapshot=snapshot, **options, persist=False).status == "no-new-clean-laps"
    second = service.recalculate_from_live_snapshot(
        snapshot=snapshot, **options, use_sector_boundary_traffic=True, persist=False,
    )
    assert second.status == "updated" and first.analysis_id != second.analysis_id
    assert live_traffic_provenance(original.manifest)["requested_traffic_mode"] == "automatic"
    assert live_traffic_provenance(service.load_model(2026, 5).manifest)["requested_traffic_mode"] == "sector"
    path = service.save_named_snapshot(2026, 5, "sector-replay", [])
    saved = json.loads(path.read_text())
    assert live_traffic_provenance(saved["model_manifest"])["requested_traffic_mode"] == "sector"
    assert live_traffic_provenance(OfflineAnalysisService(tmp_path).load_model(2026, 5).manifest)["requested_traffic_mode"] == "sector"


def test_legacy_model_never_receives_current_selection_provenance(tmp_path):
    write_model_snapshot(model_snapshot(), year=2026, round_number=5, data_root=tmp_path)
    service = OfflineAnalysisService(tmp_path)
    legacy = service.load_model(2026, 5)
    assert live_traffic_provenance(legacy.manifest) == {}
    assert "unrecorded" in live_traffic_method_label(legacy.manifest)
    assert live_traffic_provenance(service.model_history(2026, 5)[-1]) == {}


@pytest.mark.parametrize("ahead,behind,eligible", [(4.999,4.999,True),(5.,1.,False),(2.5,5.,False),(5.001,1.,False)])
def test_strict_thresholds_preserved_in_automatic(tmp_path, scenario, ahead, behind, eligible):
    snapshot, _ = scenario
    rows = _live_analysis_laps(snapshot, quick_lap_threshold=1.1, min_clean_laps=3,
                               min_gap_ahead_seconds=ahead, min_gap_behind_seconds=behind)
    assert bool(len(rows)) is eligible


def test_missing_boundary_and_unvalidated_position_rows_never_become_clean(tmp_path, scenario):
    snapshot, options = scenario
    broken = replace(snapshot, completed_sectors=tuple(
        sector for sector in snapshot.completed_sectors if sector.sector != "S2"
    ))
    service = OfflineAnalysisService(tmp_path)
    result = service.recalculate_from_live_snapshot(snapshot=broken, **options)
    assert result.status == "waiting-for-clean-laps"
    assert result.analysis_id is None
    assert "Insufficient eligible observations" in result.traffic_unavailable_reason
    assert _live_traffic_gaps(broken).TrafficStatus.eq("unknown").all()


def test_actual_standard_submit_keeps_race_distance(tmp_path, scenario, monkeypatch):
    from woplanner.analysis.worker import LiveAnalysisProcessService
    snapshot, options = scenario
    snapshot = replace(snapshot, scheduled_total_laps=62)
    worker = LiveAnalysisProcessService(tmp_path)
    captured = []
    monkeypatch.setattr(worker, '_start', captured.append)
    try:
        worker.submit(year=2026, round_number=5, snapshot=snapshot,
                      options={'use_sector_boundary_traffic': False})
        assert captured[0].snapshot.scheduled_total_laps == 62
    finally:
        worker.shutdown()
