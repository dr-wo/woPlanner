"""Requested selection and immutable producing-model traffic provenance in Qt."""
import json
from types import SimpleNamespace

import pytest
from PySide6 import QtWidgets

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import (
    LiveRecalculationResult, ModelSummary, OfflineAnalysisService,
    live_traffic_provenance,
)
from wostrategy.analysis.traffic import SECTOR_BOUNDARY_FALLBACK


@pytest.fixture
def window(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = StrategyPredictionWindow(
        year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=tmp_path / "state",
        live_operational_controls=True,
    )
    yield dialog
    dialog.close()
    app.processEvents()


def model(*, requested="automatic", legacy=False):
    manifest = {"analysis_id": "model-at-L27", "as_of_leader_lap": 27, "status": "ready"}
    if not legacy:
        manifest["semantics"] = {"live_traffic_provenance": {
            "requested_traffic_mode": requested,
            "effective_traffic_method": SECTOR_BOUNDARY_FALLBACK,
            "traffic_method_version": "sector-boundaries-v1",
            "calculated_at": "2026-10-18T12:00:00+00:00",
        }}
    return ModelSummary(manifest, ())


def test_displayed_model_provenance_survives_changed_selection(window):
    window._load_model(summary=model())
    before = window.model_traffic_label.text()
    assert "requested: Automatic" in before
    assert "Sector timing boundaries" in before
    assert "2026-10-18T12:00:00+00:00" in before
    window.traffic_source.setCurrentIndex(window.traffic_source.findData("sector"))
    assert "Requested: Sector" in window.mc_readiness_label.text()
    assert window.model_traffic_label.text() == before
    assert ">2.5s ahead / >1.0s behind" in window.clean_definition_label.text()
    assert "all four boundaries" in window.clean_definition_label.text()


@pytest.mark.parametrize("encode", [json.dumps, repr])
def test_legacy_model_and_history_never_inherit_current_method(window, encode):
    window._load_model(summary=model(legacy=True))
    assert "unrecorded (legacy model)" in window.model_traffic_label.text()
    rows = [
        {"as_of_leader_lap": 14, "parameter": "degradation", "compound": "MEDIUM", "median": .1},
        {"as_of_leader_lap": 27, "parameter": "degradation", "compound": "MEDIUM", "median": .12,
         "semantics": encode(model().manifest["semantics"])},
    ]
    window._show_plot_rows(rows)
    assert "unrecorded" in window.history_table.item(0, 5).text()
    assert "requested: Automatic" in window.history_table.item(1, 5).text()
    assert rows[0].get("semantics") is None


def test_history_saves_producing_method_with_changed_selector(window):
    summary = model()
    window.traffic_source.setCurrentIndex(window.traffic_source.findData("sector"))
    window._load_model(summary=summary)
    # Exercise history recording with effective-input rows supplied at the service boundary.
    window.service.effective_tyre_inputs = lambda *args, **kwargs: ({}, [{
        "compound": "MEDIUM", "degradation": .1, "compound_delta": 0.,
        "degradation_source": "model", "compound_delta_source": "race_evidence",
    }])
    window._record_history_point(summary)
    produced = [row for row in window._working_plot_rows if row["as_of_leader_lap"] == 27]
    assert produced
    assert all(live_traffic_provenance(row)["requested_traffic_mode"] == "automatic" for row in produced)
    path = window.service.save_history_plot(2026, 17, "method-test", produced)
    restored = window.service.load_history_plot(str(path))
    assert all(live_traffic_provenance(row)["effective_traffic_method"] == SECTOR_BOUNDARY_FALLBACK
               for row in restored["parameter_history"])


def test_waiting_result_reports_real_eligibility_reason_and_frozen_method(window):
    result = LiveRecalculationResult(
        "waiting-for-clean-laps", 14, 0,
        traffic_unavailable_reason="Only three eligible observations; four required.",
        requested_traffic_mode="automatic", effective_traffic_method=SECTOR_BOUNDARY_FALLBACK,
        traffic_method_version="sector-boundaries-v1",
    )
    completed = SimpleNamespace(result=result, model_version=1, prepared_leader_lap=14,
                                prepared_cut=None, ipc_elapsed_seconds=0.)
    window._analysis_worker.poll = lambda: completed
    window.traffic_source.setCurrentIndex(window.traffic_source.findData("sector"))
    window._poll_analysis_worker()
    assert "Only three eligible observations" in window.status_label.text()
    assert "requested=automatic" in window.calculation_log.toPlainText()
    assert SECTOR_BOUNDARY_FALLBACK in window.calculation_log.toPlainText()
    assert "unavailable" not in window.mc_readiness_label.text()


def test_main_live_status_tracks_strategy_selection(tmp_path):
    from woplanner.live.gui import LivePlannerWindow
    from woplanner.live.service import LivePlannerService

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent = LivePlannerWindow(service=LivePlannerService(), event_config_path=tmp_path / "race.json",
                               operational_history_data_root=tmp_path)
    parent._strategy_state_directory = lambda: tmp_path / "strategy-state"
    try:
        parent._show_strategy_prediction_window()
        child = parent.strategy_prediction_window
        assert parent.mc_readiness_label.text() == child.mc_readiness_label.text()
        child.traffic_source.setCurrentIndex(child.traffic_source.findData("sector"))
        assert "Requested: Sector" in parent.mc_readiness_label.text()
        child.traffic_source.setCurrentIndex(child.traffic_source.findData("automatic"))
        assert "Automatic — using Sector timing boundaries" in parent.mc_readiness_label.text()
    finally:
        parent.close()
        app.processEvents()
