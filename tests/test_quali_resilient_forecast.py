"""End-to-end Q-only analysis and distinct experimental field forecasts."""
from dataclasses import replace
from types import SimpleNamespace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6 import QtWidgets
from wodata.live import QualiTyreStint

from test_quali_r18_integration import _run, _driver, LAPS
from test_quali_service import raw, driver
from woplanner.quali.experimental_cutoff import calculate_half_live
from woplanner.quali.gui import QualiWindow
from woplanner.quali.runtime_paths import RuntimePaths
from woplanner.quali.service import QualiLiveService


@pytest.fixture(scope="module")
def r18(tmp_path_factory):
    root = tmp_path_factory.mktemp("resilient-r18")
    resources, output = root / "resources", root / "output"
    resources.mkdir(); output.mkdir()
    return _run(RuntimePaths(resources, output, isolated=True))


def test_real_r18_laps_admit_without_operational_fast_or_fp(r18):
    for sequence in LAPS:
        checkpoint = r18.checkpoints[sequence]
        snapshot = checkpoint["snapshot"]
        expected = "RUS" if sequence in (4568, 6555) else "ANT"
        assert _driver(snapshot, expected).lap_state == "UNKNOWN"
        q = snapshot.q_only_track_evolution_result
        assert q["accepted_observation_count"] > 0
        assert q["source_scope"] == "QUALIFYING_ONLY"
        assert checkpoint["result"]["live_estimate_s_per_global_push"] is None
    first = r18.checkpoints[4644]["snapshot"].q_only_track_evolution_diagnostic
    assert not {"RUS", "ANT"}.intersection(series.driver for series in first.series)
    final = r18.checkpoints[6623]["snapshot"]
    assert {"RUS", "ANT"}.issubset(series.driver for series in final.q_only_track_evolution_diagnostic.series)
    assert final.q_only_track_evolution_result["live_estimate_s_per_global_push"] < 0
    # These are separate analysis products; the legacy/Half-live formulas stay intact.
    assert final.cutoff.track_rate_s_per_field_push == 0
    assert calculate_half_live(final).projected_cutoff_s == final.cutoff.current_cutoff_s


def test_final_cutoff_forecast_precedes_observed_q2_boundary(r18):
    checkpoint = r18.checkpoints[2285]["snapshot"]
    assert checkpoint.cutoff.current_cutoff_s is None
    forecast = checkpoint.final_cutoff_forecast
    assert forecast["status"] == "EXPERIMENTAL"
    assert forecast["estimate_s"] is not None
    assert forecast["source"] == "HISTORICAL_JOINT_FIELD_PRIOR"
    assert forecast["previous_part_pace_count"] == 16
    assert forecast["target_rank"] == 10
    assert set(forecast["training_events"]).isdisjoint({"2026-R18"})
    assert forecast["lower_s"] <= forecast["estimate_s"] <= forecast["upper_s"]


def test_partial_q1_field_has_conditional_scenario_envelope_not_invented_gas_pace(r18):
    forecast = r18.checkpoints[520]["snapshot"].final_cutoff_forecast
    assert forecast["status"] == "SCENARIO_RANGE"
    assert forecast["estimate_s"] is None and forecast["interval_coverage"] is None
    assert forecast["lower_s"] < forecast["upper_s"]
    assert forecast["source"] == "PARTIAL_FIELD_SCENARIO_ENVELOPE"
    assert "unknown" in " ".join(forecast["limitations"]).lower()


def test_fp_recovery_does_not_reset_or_duplicate_q_only_observations(r18):
    service = r18.service
    before = service.q_only_track_evolution.payload(detailed=True)
    # Explicit controlled references isolate software recovery. They are not
    # labelled genuine R18 FP measurements (current upstream access failed).
    analysis = SimpleNamespace(laps=pd.DataFrame([{
        "driver": "controlled", "is_driver_fp_reference": True, "raw_lap_time_s": 96.,
        "fp_start_to_s1_s": 40., "fp_s1_to_s2_s": 40.,
    }]), durations=pd.DataFrame([{
        "category": "cool_lap_s", "selected_default_s": 125., "selected_count": 1,
    }]))
    service.load_operational_analysis(analysis)
    service.consume(service.raw_snapshot)
    after = service.q_only_track_evolution.payload(detailed=True)
    for key in ("candidate_count", "accepted_observation_count", "fitted_observation_count", "global_push_index"):
        assert before[key] == after[key]
    assert before["sample_provenance"] == after["sample_provenance"]
    assert service.readiness_diagnostics()["pace_reference_available"]


def test_missing_calibration_never_renames_current_cutoff_as_a_forecast(r18):
    service = QualiLiveService()
    service.consume(_field())
    assert service.snapshot.cutoff.current_cutoff_s is not None
    service.set_final_cutoff_prior(None)
    result = service.snapshot.final_cutoff_forecast
    assert result["status"] == "UNAVAILABLE" and result["estimate_s"] is None
    assert "not substituted" in " ".join(result["limitations"])


def test_q_only_requires_owned_consistent_sectors_and_keeps_unknown_tyres_observable():
    service = QualiLiveService()
    service.consume(raw([driver(1, None, laps=0)]))
    first = service.consume(raw([driver(1, 90., laps=1)]))
    q = first.q_only_track_evolution_result
    assert first.drivers[0].lap_state == "UNKNOWN"
    assert q["candidate_count"] == q["accepted_observation_count"] == 1
    assert q["fitted_observation_count"] == 0
    assert q["tyre_comparability_exclusions"] == 1
    bad = service.consume(raw([driver(1, 89., laps=2, sectors=(30., 30., 30.))]))
    assert bad.q_only_track_evolution_result["accepted_observation_count"] == 1
    assert bad.q_only_track_evolution_result["rejection_counts"]["SECTOR_TOTAL_MISMATCH"] == 1


@pytest.mark.parametrize("original_fresh,late_fresh,comparable", [(None, True, 0), (True, False, 1)])
def test_owned_sector_enrichment_recovers_exact_candidate_without_future_tyre_backfill(
    original_fresh, late_fresh, comparable,
):
    service = QualiLiveService()
    service.consume(raw([driver(1, None, laps=0)]))
    incomplete = replace(driver(1, 90., laps=1, sectors=(30., 30., None)),
                         tyre_history=(QualiTyreStint("SOFT", 0, original_fresh, 1),))
    before = service.consume(raw([incomplete])).q_only_track_evolution_result
    assert before["pending_sector_completions"] == before["candidate_count"] == 1
    assert before["accepted_observation_count"] == 0
    enriched = replace(driver(1, 90., laps=1),
                       tyre_history=(QualiTyreStint("SOFT", 0, late_fresh, 99),))
    snapshot = raw([enriched])
    after = service.consume(snapshot).q_only_track_evolution_result
    assert after["pending_sector_completions"] == 0
    assert after["sector_enrichment_admissions"] == 1
    assert after["candidate_count"] == after["accepted_observation_count"] == 1
    assert after["comparable_observation_count"] == comparable
    detailed = service.q_only_track_evolution.payload(detailed=True)
    sample = detailed["sample_provenance"][0]["sample"]
    assert sample["fresh"] is original_fresh and sample["tyre_install_index"] == 1
    again = service.consume(snapshot).q_only_track_evolution_result
    assert again["global_push_index"] == after["global_push_index"] == 1
    assert again["sector_enrichment_admissions"] == 1


def _field(*, status="Started", remaining=250., finished=False):
    return replace(raw([driver(i, 90. + i / 10) for i in range(1, 23)], remaining=remaining,
                       session_info={"Year": 2026, "Meeting": {"Number": 18}, "Name": "Qualifying"}),
                   session_status=status, normal_finished=finished)


def test_inactive_is_not_final_and_forecast_caches_only_analysis_inputs():
    service = QualiLiveService()
    first = service.consume(_field(status="Inactive"))
    assert first.final_cutoff_forecast["status"] != "OBSERVED_FINAL"
    calls = service.final_cutoff_calculation_count
    second = service.consume(_field(status="Inactive", remaining=249.))
    assert service.final_cutoff_calculation_count == calls
    assert second.remaining_time_s == 249.
    service.consume(_field(status="Finalised", finished=True, remaining=0.))
    assert service.snapshot.final_cutoff_forecast["status"] == "OBSERVED_FINAL"
    assert service.snapshot.final_cutoff_forecast["estimate_s"] == service.snapshot.cutoff.current_cutoff_s


@pytest.mark.parametrize("remaining", [float("nan"), float("inf")])
def test_invalid_clock_is_unavailable_to_new_forecast_without_rewriting_domain_clock(remaining):
    service = QualiLiveService()
    snapshot = service.consume(_field(remaining=remaining))
    assert snapshot.final_cutoff_forecast["status"] == "UNAVAILABLE"
    assert snapshot.final_cutoff_forecast["estimate_s"] is None


def test_normal_gui_exposes_both_new_products_with_legacy_and_half_live_distinct(r18):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    snapshot = r18.checkpoints[4644]["snapshot"]
    window._render_cutoff_predictions(snapshot)
    assert "Final P10 forecast [Experimental]" in window.final_cutoff_forecast.text()
    assert "historical scenario range" in window.final_cutoff_forecast.text()
    assert "Q-only Track Evolution" in window.q_only_track.text()
    assert "accepted" in window.q_only_track.text()
    assert "legacy" in window.current_prediction_label.text()
    assert "Half-live" in window.experimental_cutoff.text()
    from dataclasses import replace
    final = replace(snapshot, final_cutoff_forecast={
        **snapshot.final_cutoff_forecast, "status": "OBSERVED_FINAL",
        "source": "OBSERVED_FINAL_CLASSIFICATION",
    })
    window._render_cutoff_predictions(final)
    assert "observed final classification" in window.final_cutoff_forecast.text()
    assert "historical scenario" not in window.final_cutoff_forecast.text()
    window._prepare_session_start()
    assert "unavailable" in window.final_cutoff_forecast.text()
    window.close(); app.processEvents()
