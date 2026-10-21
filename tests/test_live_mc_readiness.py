"""The effective Automatic fallback is visible before any race data arrives."""
import json

import pytest
from PySide6 import QtWidgets

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import OfflineAnalysisService
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerService


@pytest.fixture
def qt():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_main_live_window_warns_before_connection_and_outside_tabs(qt, tmp_path):
    window = LivePlannerWindow(
        service=LivePlannerService(), event_config_path=tmp_path / "race.json",
        operational_history_data_root=tmp_path,
    )
    try:
        window.show()
        qt.processEvents()
        assert window.connection_status.text() == "Idle"
        assert window.mc_readiness_label.isVisible()
        assert "Automatic — using Sector timing boundaries" in window.mc_readiness_label.text()
        assert "Position-derived traffic is not certified" in window.mc_readiness_label.text()
        window.tabs.setCurrentWidget(window.race_info_tab)
        window.connection_status.setText("Receiving")
        assert window.mc_readiness_label.isVisible()
        assert "Automatic — using Sector timing boundaries" in window.mc_readiness_label.text()
    finally:
        window.close()
        qt.processEvents()


@pytest.mark.parametrize("source", ["automatic", "sector"])
def test_strategy_readiness_restores_mode_and_survives_status_changes(qt, tmp_path, source):
    state = tmp_path / "state"
    state.mkdir()
    (state / "strategy_prediction_state.json").write_text(json.dumps({
        "year":2026, "round_number":17, "schema_version":1,
        "controls":{"traffic_source":source},
    }))
    window = StrategyPredictionWindow(
        year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=state,
        live_operational_controls=True,
    )
    try:
        window.show()
        qt.processEvents()
        assert window.traffic_source.currentData() == source
        expected = ("Automatic — using Sector timing boundaries" if source == "automatic"
                    else "Requested: Sector")
        assert expected in window.mc_readiness_label.text()
        assert window.mc_readiness_label.isVisible()
        for index in range(window.tabs.count()):
            window.tabs.setCurrentIndex(index)
            window.status_label.setText("Model loaded")
            assert window.mc_readiness_label.isVisible()
            assert expected in window.mc_readiness_label.text()
        window.traffic_source.setCurrentIndex(window.traffic_source.findData("automatic"))
        assert "Position-derived traffic is not certified" in window.mc_readiness_label.text()
        window.traffic_source.setCurrentIndex(window.traffic_source.findData("sector"))
        assert "when enough clean laps" in window.mc_readiness_label.text()
        assert "Effective: Sector timing boundaries" in window.mc_readiness_label.text()
        assert window._analysis_worker.current_result is None
    finally:
        window.close()
        qt.processEvents()


def test_standard_strategy_window_does_not_show_live_readiness(qt, tmp_path):
    window = StrategyPredictionWindow(
        year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=tmp_path / "state",
        live_operational_controls=False,
    )
    try:
        window.show()
        qt.processEvents()
        assert window.mc_readiness_label.isHidden()
    finally:
        window.close()
        qt.processEvents()
