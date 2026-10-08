from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6 import QtWidgets
import pytest

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import OfflineAnalysisService
from test_weekend_tyre_import import install_evidence, RUN_ID
from test_tyre_inputs_gui import write_event


def make_window(root, **kwargs):
    return StrategyPredictionWindow(
        year=2026, round_number=16, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(root), **kwargs,
    )


def test_ui_explicit_import_replaces_local_values_refreshes_and_survives_reopen(tmp_path, monkeypatch):
    install_evidence(tmp_path)
    write_event(tmp_path, round_number=16)
    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = make_window(tmp_path)
    try:
        window._active_manual_values()[('degradation', 'SOFT')] = .8
        offered = []
        def select(*args):
            offered.extend(args[3])
            return args[3][0], True
        monkeypatch.setattr(QtWidgets.QInputDialog, 'getItem', select)
        window.import_pre_race_button.click()
        assert RUN_ID in offered[0]
        assert not window._active_manual_values()
        assert window._manual_overrides[('degradation', 'SOFT')] == .062
        assert RUN_ID in window.tyre_import_origin_label.text()
        assert RUN_ID in window.strategy_import_origin_label.text()
        assert 'woWeekend' in window.tyre_input_table.item(2, 2).text()
        assert window._resolved_tyre_inputs.strategy_overrides() == pytest.approx(window._manual_overrides)
    finally:
        window.close()
        qt.processEvents()
    restarted = make_window(tmp_path)
    try:
        assert restarted._manual_overrides[('degradation', 'SOFT')] == .062
        assert RUN_ID in restarted.strategy_import_origin_label.text()
        restarted._clear_all_manual_tyre_inputs()
        assert restarted._manual_overrides[('degradation', 'SOFT')] == pytest.approx(.20460105651325672)
        assert not restarted.tyre_import_origin_label.text()
    finally:
        restarted.close()
        qt.processEvents()


@pytest.mark.parametrize('action', ['cancel', 'switch_event', 'invalidate'])
def test_ui_cancel_stale_event_or_invalidated_run_never_imports(tmp_path, monkeypatch, action):
    run = install_evidence(tmp_path)
    write_event(tmp_path, round_number=16)
    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = make_window(tmp_path)
    try:
        def select(*args):
            if action == 'switch_event':
                window.round_number = 15
            elif action == 'invalidate':
                (run / 'tyre_prediction.effective.json').write_text('{}')
            return args[3][0], action != 'cancel'
        monkeypatch.setattr(QtWidgets.QInputDialog, 'getItem', select)
        window.import_pre_race_button.click()
        assert not window.service.tyre_inputs.manual_path(2026, 16).exists()
    finally:
        window.close()
        qt.processEvents()


def test_import_is_unavailable_in_live_and_team_scopes(tmp_path):
    install_evidence(tmp_path)
    write_event(tmp_path, round_number=16)
    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    live = make_window(tmp_path, team_scope_enabled=True)
    if hasattr(live, 'live_operational_controls'):
        live.live_operational_controls = True
        live._refresh_tyre_inputs()
    team = make_window(tmp_path)
    try:
        assert not live.import_pre_race_button.isEnabled()
        team._selected_team = 'Ferrari'
        team._refresh_tyre_inputs()
        assert not team.import_pre_race_button.isEnabled()
        team._import_pre_race_tyre_inputs()
        assert not team.service.tyre_inputs.manual_path(2026, 16).exists()
    finally:
        live.close()
        team.close()
        qt.processEvents()
