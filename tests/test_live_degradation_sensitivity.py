from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from wodata.artifacts import strategy_search_root
from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.live_sensitivity import (
    apply_live_degradation_sensitivity,
    live_calculation_tyre_inputs,
    replay_live_tyre_inputs,
)
from woplanner.analysis.service import OfflineAnalysisService

from test_tyre_inputs_gui import app, choose_team, strategy_cell, team_model, write_event


@pytest.mark.parametrize("factor", [1.0, .8, 0.0, 1.2])
@pytest.mark.parametrize("team", [None, "Ferrari"])
@pytest.mark.parametrize("manual_override", [False, True])
def test_final_transform_preserves_existing_resolution_and_replays(tmp_path, team, factor, manual_override):
    write_event(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs.save_manual_inputs(2026, 11, {"Deg_HARD": .77})
    if team:
        service.tyre_inputs.save_manual_inputs(2026, 11, {"Deg_MEDIUM": .45}, team=team)
    canonical_before = service.tyre_inputs.manual_path(2026, 11).read_bytes()
    summary = team_model()
    summary_before = json.dumps(summary.parameters)
    resolved = service.resolve_tyre_inputs(2026, 11, team=team)
    used = {("degradation", "SOFT"): .63, ("compound_delta", "HARD"): .81}
    base, assumptions = service.effective_tyre_inputs(
        summary, resolved, manual_override=manual_override, manual_values=used,
    )
    # The existing resolver still applies Global live overlays, Team manuals,
    # and final Used edits before the separate sensitivity ever runs.
    expected_hard = resolved.by_parameter()["Deg_HARD"].selected_value if manual_override else (.12 if team else .20)
    expected_medium = .45 if team else (
        resolved.by_parameter()["Deg_MEDIUM"].selected_value if manual_override else .30
    )
    assert base[("degradation", "HARD")] == expected_hard
    assert base[("degradation", "MEDIUM")] == expected_medium
    assert base[("degradation", "SOFT")] == .63
    assert base[("compound_delta", "HARD")] == .81
    final, snapshot = live_calculation_tyre_inputs(
        base, resolved, factor=factor, assumptions=assumptions, manual_values=used,
    )
    for coordinate, value in base.items():
        assert final[coordinate] == (value * factor if coordinate[0] == "degradation" else value)
    assert replay_live_tyre_inputs(json.loads(json.dumps(snapshot))) == final
    assert snapshot["live_degradation_sensitivity"]["base_values"]["Deg_SOFT"] == .63
    assert snapshot["selected_sources"]["Deg_SOFT"] == "manual"
    assert snapshot["team"] == team
    assert service.tyre_inputs.manual_path(2026, 11).read_bytes() == canonical_before
    assert json.dumps(summary.parameters) == summary_before
    assert base[("degradation", "SOFT")] == .63


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_sensitivity_rejected(value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        apply_live_degradation_sensitivity({("degradation", "HARD"): .1}, value)


@pytest.mark.parametrize("team", [None, "Ferrari"])
def test_live_ui_final_values_reach_real_search_reset_and_history(tmp_path, monkeypatch, team):
    write_event(tmp_path)
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    calls = []
    generate = service.generate_continuations

    def record_search(**kwargs):
        calls.append(kwargs)
        return generate(**kwargs)

    monkeypatch.setattr(service, "generate_continuations", record_search)
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True, live_operational_controls=True,
        state_directory=tmp_path / "live-state", initial_model_summary=team_model(),
    )
    try:
        choose_team(window, team)
        window.completed_lap_override_enabled.setChecked(True)
        window.completed_lap.setValue(35)
        window.total_laps.setValue(50)
        window.max_stops.setValue(1)
        window.result_count.setValue(3)
        strategy_cell(window, "SOFT", 7).setText(".63")
        strategy_cell(window, "HARD", 4).setText(".81")
        base = dict(window._manual_overrides)
        window.degradation_sensitivity.setValue(.8)
        assert "ACTIVE" in window.degradation_sensitivity_status.text()
        assert float(strategy_cell(window, "SOFT", 9).text()) == pytest.approx(.504)
        assert window._manual_overrides == base
        window._load_model(summary=team_model(ferrari_medium=.18))
        assert window._manual_overrides[("degradation", "SOFT")] == .63
        assert window.degradation_sensitivity.value() == .8
        window._start_search()
        results = window._future.result(timeout=10)
        window._poll_future()
        assert results
        call = calls[-1]
        assert call["overrides"][("degradation", "SOFT")] == pytest.approx(.504)
        assert call["overrides"][("compound_delta", "HARD")] == pytest.approx(.81)
        assert call["tyre_input_snapshot"]["values"]["Deg_SOFT"] == pytest.approx(.504)
        assert window._manual_overrides[("degradation", "SOFT")] == .63
        stored = json.loads(window._state_path.read_text())
        assert stored["controls"]["live_degradation_sensitivity"] == .8
        assert stored["last_live_tyre_input_snapshot"] == call["tyre_input_snapshot"]
        sidecars = list((strategy_search_root(2026, 11, "global", tmp_path) / "tyre_input_snapshots").glob("*.json"))
        assert sidecars
        snapshot = json.loads(sidecars[0].read_text())
        replayed = generate(**{**call, "overrides": replay_live_tyre_inputs(snapshot), "persist": False})
        assert [(r.plan, r.remaining_cost) for r in replayed] == [(r.plan, r.remaining_cost) for r in results]
        named = json.loads(service.save_named_snapshot(2026, 11, "Live sensitivity", window._last_strategy_rows).read_text())
        assert named["tyre_input_snapshot"] == snapshot
        window.reset_degradation_sensitivity.click()
        assert window.degradation_sensitivity.value() == 1.0
        assert "ACTIVE" not in window.degradation_sensitivity_status.text()
        assert float(strategy_cell(window, "SOFT", 9).text()) == .63
        window._start_search()
        baseline_results = window._future.result(timeout=10)
        window._poll_future()
        assert calls[-1]["overrides"] == window._manual_overrides
        identity = generate(**{**calls[-1], "overrides": dict(window._manual_overrides), "persist": False})
        assert [(r.plan, r.remaining_cost) for r in identity] == [(r.plan, r.remaining_cost) for r in baseline_results]
        assert baseline_results[0].remaining_cost != results[0].remaining_cost
    finally:
        window.close()
        qt.processEvents()


def test_live_state_restored_but_standard_consumer_does_not_apply_or_erase_it(tmp_path):
    write_event(tmp_path)
    qt = app()
    options = dict(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        state_directory=tmp_path / "state", service=OfflineAnalysisService(tmp_path),
        team_scope_enabled=True, initial_model_summary=team_model(),
    )
    live = StrategyPredictionWindow(**options, live_operational_controls=True)
    live.degradation_sensitivity.setValue(.8)
    live.close()
    qt.processEvents()
    standard = StrategyPredictionWindow(**options)
    try:
        assert standard._live_sensitivity_factor() == 1.0
        assert standard.strategy_input_table.columnCount() == 9
    finally:
        standard.close()
        qt.processEvents()
    reopened = StrategyPredictionWindow(**options, live_operational_controls=True)
    try:
        assert reopened.degradation_sensitivity.value() == .8
        assert reopened.strategy_input_table.columnCount() == 10
    finally:
        reopened.close()
        qt.processEvents()


def test_replay_refuses_inconsistent_provenance():
    snapshot = {
        "values": {"Deg_HARD": .1, "Deg_MEDIUM": .2, "Deg_SOFT": .3,
                   "Perf_HARD_vs_MEDIUM": .4, "Perf_SOFT_vs_MEDIUM": -.5},
    }
    assert replay_live_tyre_inputs(snapshot)[("degradation", "HARD")] == .1
    snapshot["live_degradation_sensitivity"] = {"factor": .8, "base_values": dict(snapshot["values"])}
    with pytest.raises(ValueError, match="disagree"):
        replay_live_tyre_inputs(snapshot)
