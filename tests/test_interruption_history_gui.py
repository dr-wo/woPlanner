import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pandas as pd
from PySide6 import QtWidgets
from wodata.interruption_history import write_history, history_path
from wostrategy.analysis.interruption_history import extract_race, history_rows, history_markdown
from woplanner.analysis.gui import StrategyPredictionWindow, StrategyPredictionMode
from woplanner.analysis.service import OfflineAnalysisService
from test_tyre_inputs_gui import write_event


def test_history_info_reads_canonical_values_and_reload_is_local(tmp_path, monkeypatch):
    qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    write_event(tmp_path)
    identity = {'country': 'UK', 'location': 'Silverstone', 'event_name': 'British GP'}
    race = extract_race({'season': 2025, 'round_number': 12, 'event_id': '2025-12', 'event_identity': identity}, {
        'track_status': pd.DataFrame([(0, '1'), (20, '4'), (30, '5'), (40, '1')], columns=['Time', 'Status']),
        'session_status': pd.DataFrame([(10, 'Started'), (100, 'Finished')], columns=['Time', 'Status']),
        'official_start': 10,
        'laps': pd.DataFrame({'LapNumber': [1], 'Time': [15], 'DriverNumber': ['1']}),
        'timing_stream': pd.DataFrame([(0, '1', 1, 'LAP 1'), (15, '1', 1, 'LAP 2')],
                                     columns=['Time', 'Driver', 'Position', 'GapToLeader'])})
    payload = {'schema_version': 1, 'semantic_role': 'historical_race_interruptions', 'status': 'available',
               'target': {'season': 2026, 'round_number': 11, 'event_identity': identity}, 'races': [race]}
    write_history(payload, tmp_path)
    import wodata.interruption_history as contract
    monkeypatch.setattr(contract, 'load_race_source', lambda *a, **k: (_ for _ in ()).throw(AssertionError('acquisition attempted')))
    window = StrategyPredictionWindow(year=2026, round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF, service=OfflineAnalysisService(tmp_path))
    try:
        assert 'History Info' in [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert window.historical_interruptions == payload
        expected = history_rows(payload)[0]
        texts = [window.history_info_table.item(0, i).text() for i in range(4)]
        assert texts == [expected[k] for k in ('edition', 'safety_car', 'red_flag', 'coverage')]
        assert all(text in history_markdown(payload) for text in texts)
        assert 'lap unavailable' in texts[1]
        window.completed_lap_override_enabled.setChecked(True)
        window.completed_lap.setValue(23)
        progress = window.completed_lap.value()
        history_path(2026, 11, tmp_path).unlink()
        window._reload_history_info()
        assert window.history_info_table.rowCount() == 0
        assert 'unavailable' in window.history_info_status.text().lower()
        assert window.completed_lap.value() == progress == 23
        assert window.completed_lap_override_enabled.isChecked()
        assert not window.completed_lap.isReadOnly()
    finally:
        window.close()
        qt.processEvents()
