import json
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import ModelSummary, OfflineAnalysisService
from test_observed_undercut import snapshot, summary
from test_tyre_inputs_gui import app, write_event


def make_window(tmp_path, monkeypatch, *, live=True):
    write_event(tmp_path)
    monkeypatch.setattr(
        "woplanner.analysis.service._live_traffic_gaps",
        lambda snap, **kwargs: pd.DataFrame([
            dict(Driver=lap.driver_code, LapNumber=lap.lap_number,
                 TrafficGapAheadSeconds=10., TrafficGapBehindSeconds=10.,
                 TrafficStatus="sector_fallback")
            for lap in snap.completed_laps
        ]),
    )
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), live_operational_controls=live,
        team_scope_enabled=live,
        state_directory=tmp_path / "live-state", initial_model_summary=summary(),
    )
    window.auto_recalculate.setChecked(False)
    window.quick_threshold.setValue(1.2)
    window.traffic_source.setCurrentIndex(window.traffic_source.findData("sector"))
    return window


def test_live_observation_display_audit_and_sensitivity_isolation(tmp_path, monkeypatch):
    qt = app()
    window = make_window(tmp_path, monkeypatch)
    try:
        window.consume_live_snapshot(snapshot())
        table = window.observed_undercut.table
        assert table.rowCount() == 2
        assert [table.item(0, col).text() for col in (3, 4, 5, 6, 7)] == [
            "3", "6", "3", "+3.000", "+0.060",
        ]
        audit = json.loads(table.item(0, 7).toolTip())
        assert audit["track"]["analysis_id"] == "live-fit"
        assert audit["track"]["config_hash"] == "config-hash"
        assert audit["pre_lap_duration_s"] == 93.
        assert audit["post_lap_duration_s"] == 90.
        assert audit["track"]["race_lap_reference"] == 6.
        before = window.observed_undercut.records
        window.degradation_sensitivity.setValue(.8)
        window.min_clean_laps.setValue(30)
        window.exclude_stint_start_laps.setValue(10)
        window.exclude_stint_end_laps.setValue(10)
        assert window.observed_undercut.records == before
        window._save_state()
        saved = json.loads(window._state_path.read_text())["live_observed_undercut"]
        assert saved["event"] == {"year": 2026, "round_number": 11, "session": "R"}
        assert saved["estimates"][0]["observed_pace_delta_s"] == 3.
        assert saved["estimates"][0]["track"]["track_term_change_s"] == pytest.approx(.06)
        # A new current fit changes only the separate interpretation.
        window._load_model(summary=summary(rate=-.02, reference=10.))
        assert table.item(0, 6).text() == "+3.000"
        assert table.item(0, 7).text() == "-0.060"
    finally:
        window.close()
        qt.processEvents()


def test_live_observation_pending_unsupported_and_replay_clear(tmp_path, monkeypatch):
    qt = app()
    window = make_window(tmp_path, monkeypatch)
    try:
        model = summary()
        manifest = dict(model.manifest)
        manifest["semantics"] = dict(manifest["semantics"], architecture="race-specific-v2")
        window._load_model(summary=ModelSummary(manifest, model.parameters))
        snap = snapshot()
        snap = replace(snap, completed_laps=tuple(
            replace(lap, is_valid=False) if lap.lap_number in (6, 7) else lap
            for lap in snap.completed_laps
        ))
        window.consume_live_snapshot(snap)
        table = window.observed_undercut.table
        assert table.item(0, 4).text() == "—"
        assert table.item(0, 6).text() == "—"
        assert "post-stop" in table.item(0, 8).text()
        assert table.item(1, 7).text() == "Unavailable"
        assert "Unsupported MC architecture" in table.item(1, 7).toolTip()
        window.persistence_mode = "temporary"
        window.reset_replay_history()
        assert table.rowCount() == 0
    finally:
        window.close()
        qt.processEvents()


def test_standard_window_has_no_observation_panel(tmp_path, monkeypatch):
    qt = app()
    window = make_window(tmp_path, monkeypatch, live=False)
    try:
        assert window.observed_undercut.isHidden()
        window.consume_live_snapshot(snapshot())
        assert window.observed_undercut.records == ()
    finally:
        window.close()
        qt.processEvents()


def test_session_metadata_only_change_invalidates_observations(tmp_path, monkeypatch):
    qt = app()
    window = make_window(tmp_path, monkeypatch)
    try:
        snap = snapshot()
        window.consume_live_snapshot(snap)
        assert window.observed_undercut.table.rowCount() == 2
        window.consume_live_snapshot(replace(snap, session_info={"Name": "Qualifying"}))
        assert window.observed_undercut.table.rowCount() == 0
        assert window.observed_undercut.records == ()
        window.consume_live_snapshot(replace(snap, session_info={"Name": "Race"}))
        assert window.observed_undercut.table.rowCount() == 2
        saved = json.loads(window._state_path.read_text())["live_observed_undercut"]
        assert saved["event"]["session"] == "R"
        window.consume_live_snapshot(replace(snap, session_info={"Name": "Sprint"}))
        saved = json.loads(window._state_path.read_text())["live_observed_undercut"]
        assert saved["event"]["session"] == "S"
    finally:
        window.close()
        qt.processEvents()
