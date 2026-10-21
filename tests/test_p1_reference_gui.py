from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.p1_reference import P1ReferenceResult
from woplanner.analysis.service import ModelSummary, OfflineAnalysisService
from woplanner.analysis import worker as worker_module
from woplanner.analysis import p1_reference_widget as widget_module

from test_tyre_inputs_gui import app, choose_team, team_model, write_event


class FakeWorker:
    def __init__(self, _root):
        self.running = False
        self.calls = []
        self.completed = None

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        self.running = True

    def poll(self):
        result = self.completed
        if result is not None:
            self.completed = None
            self.running = False
        return result

    def shutdown(self):
        self.running = False


def snapshot(laps=14, key="race-1"):
    return SimpleNamespace(
        session_info={"Key": key},
        drivers=(SimpleNamespace(driver_code="LEC", position=1, retired=False),),
        completed_laps=tuple(SimpleNamespace(driver_code="LEC", lap_number=lap, position=1)
                             for lap in range(1, laps + 1)),
        completed_sectors=(), telemetry_samples=(), scheduled_total_laps=53,
    )


def result():
    row = dict(parameter="team_degradation", compound="MEDIUM", team="Ferrari",
               median=.222, p10=.19, p90=.25)
    return P1ReferenceResult(
        status="updated", message="Separate reference. Single-stint parameters are confounded.",
        driver_code="LEC", first_lap=3, last_lap=14, leader_lap=14, team="Ferrari",
        eligible_lap_count=4, selected_lap_numbers=(10, 11, 12, 13),
        summary=ModelSummary({"as_of_leader_lap": 14}, (row,)),
        sampler_quality=(("ess", 20), ("sample_count", 100)),
        exclusion_diagnostics=(("traffic_ahead", 1),),
        applicable_parameters=({**row, "parameter": "degradation", "source_parameter": "team_degradation"},),
    )


@pytest.fixture
def window(tmp_path, monkeypatch):
    qt = app()
    write_event(tmp_path)
    monkeypatch.setattr(widget_module, "P1ReferenceProcessService", FakeWorker)
    win = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=tmp_path / "state",
        initial_model_summary=team_model(), team_scope_enabled=True, live_operational_controls=True,
    )
    win.auto_recalculate.setChecked(False)
    win._live_snapshot = snapshot()
    win.p1_reference.consume_snapshot(win._live_snapshot)
    yield win
    win.close()
    qt.processEvents()


def calculate(window):
    panel = window.p1_reference
    panel.sector_traffic.setChecked(True)
    panel.first_lap.setValue(3)
    panel.acknowledge.setChecked(True)
    panel.calculate.click()
    panel.worker.completed = result()
    panel._poll()
    return panel


def test_fresh_gui_requires_explicit_tactical_confirmation(window):
    from PySide6 import QtCore, QtTest

    panel = window.p1_reference
    window.tabs.setCurrentWidget(panel)
    window.show()
    app().processEvents()
    assert window.tabs.tabText(window.tabs.indexOf(panel)) == "P1 Pace Reference"
    assert panel.title() == "Leader Pace Estimation"
    assert panel.calculate.text() == "Calculate Reference"
    assert panel.acknowledge.text() == "Confirm selected laps are suitable for pace estimation"
    assert not panel.acknowledge.isChecked()
    panel.sector_traffic.setChecked(True)
    assert not panel.calculate.isEnabled()
    panel.calculate.click()
    assert panel.worker.calls == []
    assert not panel.apply.isEnabled()
    QtTest.QTest.mouseClick(panel.acknowledge, QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(8, panel.acknowledge.height() // 2))
    assert panel.acknowledge.isChecked()
    assert panel.calculate.isEnabled()
    panel.calculate.click()
    assert len(panel.worker.calls) == 1
    panel.worker.completed = result()
    panel._poll()
    choose_team(window, "Ferrari")
    assert panel.apply.isEnabled()
    QtTest.QTest.mouseClick(panel.acknowledge, QtCore.Qt.MouseButton.LeftButton,
                           pos=QtCore.QPoint(8, panel.acknowledge.height() // 2))
    assert panel.result is None
    assert not panel.calculate.isEnabled()
    assert not panel.apply.isEnabled()


def test_manual_reference_does_not_change_standard_until_explicit_apply_and_restores(window):
    baseline = window._current_model_summary
    before = dict(window._manual_overrides)
    panel = window.p1_reference
    assert panel.driver.currentData() == "LEC"
    assert panel.last_lap.maximum() == 14
    assert not panel.calculate.isEnabled()
    panel = calculate(window)
    assert window._manual_overrides == before
    assert window._current_model_summary is baseline
    assert window._analysis_worker.current_result is None
    assert window.traffic_source.currentData() == "automatic"
    assert panel.worker.calls[0]["options"]["last_lap"] == 14
    assert not panel.apply.isEnabled()  # Global is not P1's team.
    assert "No standard MC comparison is available for this race lap" in panel.status.text()
    assert panel.comparison.item(0, 2).text() == "—"
    choose_team(window, "Ferrari")
    assert panel.apply.isEnabled()
    previous = dict(window._manual_overrides)
    panel.apply.click()
    assert window.traffic_source.currentData() == "automatic"
    assert window._manual_overrides[("degradation", "MEDIUM")] == .222
    assert all(window._manual_overrides[k] == v for k, v in previous.items()
               if k != ("degradation", "MEDIUM"))
    assert window._current_model_summary is baseline
    panel.restore.click()
    assert window._manual_overrides == previous


def test_supported_standard_comparison_and_unavailable_quality(window):
    standard_row = {"parameter": "team_degradation", "compound": "MEDIUM", "team": "Ferrari", "median": .15}
    window._current_model_summary = ModelSummary({"as_of_leader_lap": 12}, (standard_row,))
    panel = calculate(window)
    assert panel.comparison.item(0, 2).text() == "0.15"
    panel.result = replace(result(), status="waiting-for-clean-laps", summary=None,
                           applicable_parameters=(), eligible_lap_count=0,
                           selected_lap_numbers=(), message="Need four consecutive clean laps.")
    panel._render_result()
    panel._refresh_actions()
    assert "Need four consecutive clean laps" in panel.status.text()
    assert not panel.apply.isEnabled()


def test_control_change_and_session_reset_discard_inflight_results(window):
    panel = window.p1_reference
    panel.sector_traffic.setChecked(True)
    panel.acknowledge.setChecked(True)
    panel.calculate.click()
    window.min_clean_laps.setValue(5)
    panel.worker.completed = result()
    panel._poll()
    assert panel.result is None
    assert not panel.apply.isEnabled()
    panel.calculate.click()
    panel.consume_snapshot(snapshot(laps=2, key="race-2"))
    panel.worker.completed = result()
    panel._poll()
    assert panel.result is None
    assert not panel.acknowledge.isChecked()
    assert not panel.calculate.isEnabled()


def test_live_updates_do_not_replace_submitted_cut_and_restore_keeps_later_edits(window):
    panel = calculate(window)
    panel.consume_snapshot(snapshot(laps=15))
    assert panel.result.leader_lap == 14
    assert panel.worker.calls[0]["snapshot"].completed_laps[-1].lap_number == 14
    choose_team(window, "Ferrari")
    window._active_manual_values()[("degradation", "MEDIUM")] = .18
    panel.apply.click()
    panel.apply.click()
    panel.restore.click()
    assert window._manual_overrides[("degradation", "MEDIUM")] == .18
    panel.apply.click()
    window._active_manual_values()[("degradation", "MEDIUM")] = .27
    panel.restore.click()
    assert window._manual_overrides[("degradation", "MEDIUM")] == .27


def test_worker_routes_separate_nonpublishing_fit(monkeypatch):
    calls = []
    service = SimpleNamespace(calculate_p1_reference=lambda **kwargs: calls.append(kwargs) or "reference")
    monkeypatch.setattr(worker_module, "_PROCESS_SERVICE", service)
    request = worker_module.AnalysisRequest(1, 2026, 17, snapshot(), (("driver_code", "LEC"),))
    assert worker_module._run_p1_reference(request) == "reference"
    assert calls[0]["snapshot"] is request.snapshot
    assert calls[0]["driver_code"] == "LEC"


def test_p1_distance_metadata_does_not_change_standard_worker_inputs(tmp_path, monkeypatch):
    observed = snapshot()
    requests = []
    standard_worker = worker_module.LiveAnalysisProcessService(tmp_path)
    monkeypatch.setattr(standard_worker, "_start", requests.append)
    try:
        standard_worker.submit(year=2026, round_number=17, snapshot=observed, options={})
    finally:
        standard_worker.shutdown(wait=False)
    standard = requests[0].snapshot
    reference = worker_module._serializable_live_snapshot(
        observed, include_telemetry=False, include_race_distance=True,
    )
    # Phase 1 established that standard MC also needs the full race distance.
    assert standard.scheduled_total_laps == observed.scheduled_total_laps
    assert reference.scheduled_total_laps == observed.scheduled_total_laps == 53
    assert standard.completed_laps == reference.completed_laps == observed.completed_laps
    assert standard.completed_sectors == reference.completed_sectors == observed.completed_sectors


def test_alternative_enables_only_supported_team_without_changing_standard(window):
    standard = team_model(include_ferrari=False)
    window._load_model(summary=standard)
    assert not window.team_buttons["Ferrari"].isEnabled()
    panel = calculate(window)
    assert window.team_buttons["Ferrari"].isEnabled()
    choose_team(window, "Ferrari")
    before = dict(window._manual_overrides)
    panel.apply.click()
    assert window._manual_overrides[("degradation", "MEDIUM")] == .222
    assert window._current_model_summary is standard
    panel.first_lap.setValue(4)
    assert panel.result is None
    assert window.team_buttons["Ferrari"].isEnabled()  # The explicit override still owns this scope.
    panel.restore.click()
    assert not window.team_buttons["Ferrari"].isEnabled()
    assert window._selected_team is None
    assert ("degradation", "MEDIUM") not in window._scope_manual_values["Ferrari"]
    assert window._current_model_summary is standard


@pytest.mark.parametrize("next_snapshot", [snapshot(laps=5), snapshot(laps=14, key="different-race")])
def test_reset_restores_applied_future_reference_and_provenance(window, next_snapshot):
    panel = calculate(window)
    choose_team(window, "Ferrari")
    before = dict(window._manual_overrides)
    panel.apply.click()
    assert window._external_tyre_sources["MEDIUM"].startswith("P1")
    panel.consume_snapshot(next_snapshot)
    assert window._manual_overrides == before
    assert "MEDIUM" not in window._external_tyre_sources
    assert panel._restore is None
    assert panel.result is None


def test_p1_reference_does_not_persist_as_unscoped_manual_after_reopen(window):
    panel = calculate(window)
    choose_team(window, "Ferrari")
    window._active_manual_values()[("degradation", "MEDIUM")] = .18
    panel.apply.click()
    state = json.loads(window._state_path.read_text())
    saved = {(r["parameter"], r["compound"]): r["value"] for r in state["scope_manual_values"]["Ferrari"]}
    assert saved[("degradation", "MEDIUM")] == .18
    assert "MEDIUM" not in state["external_tyre_sources"]
    assert window._manual_overrides[("degradation", "MEDIUM")] == .222
    window._active_manual_values()[("degradation", "MEDIUM")] = .27
    window._save_state()
    state = json.loads(window._state_path.read_text())
    saved = {(r["parameter"], r["compound"]): r["value"] for r in state["scope_manual_values"]["Ferrari"]}
    assert saved[("degradation", "MEDIUM")] == .27


def test_explicit_session_restart_cancels_p1_even_at_same_observed_cut(window):
    panel = calculate(window)
    choose_team(window, "Ferrari")
    before = dict(window._manual_overrides)
    panel.apply.click()
    window._restart_analysis_worker()
    assert panel.result is None
    assert window._manual_overrides == before
    assert not panel.calculate.isEnabled()
    panel.consume_snapshot(snapshot())
    assert not panel.calculate.isEnabled()  # New session needs explicit operator choices.


def test_nonmatching_parameter_coordinates_are_not_compared(window):
    rows = (
        dict(parameter="baseline_pace", compound="MEDIUM", median=97., race_lap_ref=10),
        dict(parameter="compound_delta", compound="HARD", median=.4, reference_compound="MEDIUM"),
        dict(parameter="fuel", compound="", median=.04),
    )
    window._current_model_summary = ModelSummary({"as_of_leader_lap": 12}, rows)
    panel = calculate(window)
    p1_rows = tuple({**row, **({"race_lap_ref": 12} if row["parameter"] == "baseline_pace"
                               else {"reference_compound": "SOFT"} if row["parameter"] == "compound_delta" else {})}
                    for row in rows)
    panel.result = replace(result(), summary=ModelSummary({"as_of_leader_lap": 14}, p1_rows))
    panel._render_result()
    assert all(panel.comparison.item(row, 2).text() == "—" for row in range(3))
