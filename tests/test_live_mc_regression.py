"""Race MC must retain distance and distinguish policy blocking from no data."""

from dataclasses import replace
import pickle

import pytest

from wodata.live import LiveSessionSnapshot
from woplanner.analysis.service import OfflineAnalysisService, _resolve_scheduled_total_laps
from woplanner.analysis.worker import _serializable_live_snapshot
from woplanner.live.telemetry import CausalAnalysisCut, TelemetryAnalysisWorker


@pytest.mark.parametrize("distance", [62, 56, None])
def test_race_distance_survives_both_mc_boundaries(distance):
    snapshot = replace(LiveSessionSnapshot.empty(), scheduled_total_laps=distance)
    worker = TelemetryAnalysisWorker()
    cut = CausalAnalysisCut(14, 1400., 100, worker.generation)
    try:
        worker._prepare_analysis_input(
            snapshot, ("build", worker.generation, 1, 14, True), cut,
        )
        prepared, = worker.poll_analysis_inputs()
    finally:
        worker.shutdown()
    assert prepared.error is None
    for payload in (snapshot, prepared.analysis_input):
        payload = pickle.loads(pickle.dumps(_serializable_live_snapshot(payload)))
        assert payload.scheduled_total_laps == distance
        assert _resolve_scheduled_total_laps(
            payload, manual_override=70, current_leader_lap=14,
        ) == ((distance, "live_lap_count") if distance else (70, "manual_override"))


def test_insufficient_run_exposes_received_laps_without_fitting(tmp_path):
    from test_live_analysis_boundary import committed_snapshot

    snapshot = committed_snapshot()
    service = OfflineAnalysisService(tmp_path)
    result = service.recalculate_from_live_snapshot(
        year=2026, round_number=17, snapshot=snapshot, sample_count=100,
        quick_lap_threshold=1.1, min_clean_laps=4,
    )
    assert result.status == "waiting-for-clean-laps"
    counts = dict(result.exclusion_diagnostics)
    assert counts["completed_laps"] == len(snapshot.completed_laps)
    assert "traffic_policy_blocked_laps" not in counts
    assert result.effective_traffic_method == "SECTOR_BOUNDARY_FALLBACK"
    assert counts["selected_clean_laps"] == 0
    assert service._last_live_fingerprint is not None
    assert not list(tmp_path.iterdir())


def test_observed_stop_reports_missing_sector_reference():
    from test_observed_undercut import snapshot
    from woplanner.analysis.observed_undercut import build_observed_undercut_estimates

    estimates = build_observed_undercut_estimates(snapshot())
    assert estimates
    for estimate in estimates:
        assert estimate.status == "pending"
        assert "Missing clean" in estimate.pending_reason
        assert dict(estimate.clean_lap_settings)["effective_traffic_method"] == "SECTOR_BOUNDARY_FALLBACK"
        assert estimate.observed_pace_delta_s is None


def test_disallowed_compounds_counted_before_shared_preparation_drops_them():
    from test_live_analysis_boundary import committed_snapshot
    from woplanner.analysis.service import _live_analysis_laps

    original = committed_snapshot()
    wet_laps = tuple(replace(lap, compound="INTERMEDIATE") for lap in original.completed_laps[:3])
    snapshot = replace(original, completed_laps=wet_laps + original.completed_laps[3:])
    selected = _live_analysis_laps(snapshot, quick_lap_threshold=1.1,
                                  min_clean_laps=1, use_sector_boundary_traffic=True)
    assert selected.attrs["exclusion_diagnostics"]["disallowed_compound"] == 3
    assert not selected.Compound.eq("INTERMEDIATE").any()


def test_genuinely_missing_metadata_does_not_create_a_race_fit(tmp_path):
    from test_live_analysis_boundary import committed_snapshot

    original = committed_snapshot()
    snapshot = replace(original, completed_laps=tuple(
        replace(lap, compound=None, tyre_life=None, stint=None)
        for lap in original.completed_laps
    ))
    service = OfflineAnalysisService(tmp_path)
    result = service.recalculate_from_live_snapshot(
        year=2026, round_number=17, snapshot=snapshot, sample_count=100,
        quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True,
    )
    assert result.status == "waiting-for-clean-laps"
    assert result.analysis_id is None
    assert dict(result.exclusion_diagnostics)["missing_tyre_metadata"] == len(snapshot.completed_laps)
    assert service._ephemeral_result is None
    assert not list(tmp_path.iterdir())
