from __future__ import annotations

from datetime import datetime, timezone
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets
import pytest

from wodata.artifacts import weekend_model_root, write_model_snapshot
from wodata.contracts import ModelSnapshot, ParameterEstimate
from wodata.colours import TEAM_COLOURS, get_team_colour
import woplanner.analysis.gui as analysis_gui
from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import ModelSummary, OfflineAnalysisService
from woplanner.operational_inputs import OperationalRaceInputs
from woplanner.live.gui import LivePlannerWindow
from woplanner.analysis.tyre_inputs import TyreInputService
from wostrategy.analysis.fp_tyre_evidence import write_fp_tyre_evidence


def write_event(root, year=2026, round_number=11):
    created = datetime(2026, 8, 1, tzinfo=timezone.utc)
    parameters = []
    values = {
        "HARD": (0.049608730122481265, 0.49205953068285274),
        "MEDIUM": (0.09494384183899782, 0.027520181834348918),
        "SOFT": (0.16897262521052198, -0.5195797125172017),
    }
    for compound, (degradation, performance) in values.items():
        for parameter, median, unit in (
            ("degradation", degradation, "s/lap"),
            ("compound_delta", performance, "s"),
        ):
            parameters.append(
                ParameterEstimate(
                    "pre", "bank", "aggregate", 0, parameter, "FP1+FP2+FP3",
                    None, compound, "MEDIUM", median - 0.01, median,
                    median + 0.01, unit, "measured", 20, 4, 100, 0.2,
                    0, 0, "input", "config", created,
                )
            )
    write_model_snapshot(
        ModelSnapshot(
            "pre", "bank", "aggregate", 0, tuple(parameters),
            "input", "config", created,
        ),
        year=year,
        round_number=round_number,
        data_root=root,
    )
    prior = {
        "schema_version": 1, "year": year, "round_number": round_number,
        "source_analysis_id": "pre", "source_sessions": ["FP1", "FP2", "FP3"],
        "source_created_at": created.isoformat(), "frozen_at": created.isoformat(),
        "construction": "test", "fuel": {"p10": .01, "median": .02, "p90": .03, "unit": "s/lap"},
        "degradation": {}, "performance": {},
    }
    for compound, (degradation, performance) in values.items():
        prior["degradation"][compound] = {
            "p10": degradation - .01, "median": degradation,
            "p90": degradation + .01, "unit": "s/lap",
        }
        prior["performance"][compound] = {
            "p10": performance - .01, "median": performance,
            "p90": performance + .01, "unit": "s",
        }
    path = weekend_model_root(year, round_number, root) / "pre_race_prior_v1.json"
    path.write_text(json.dumps(prior), encoding="utf-8")


def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def team_model(*, ferrari_medium=.15, include_ferrari=True):
    rows = []
    for compound, degradation, performance in (
        ("HARD", .20, .70), ("MEDIUM", .30, 0.), ("SOFT", .40, -.60)
    ):
        for parameter, value in (("degradation", degradation), ("compound_delta", performance)):
            rows.append({
                "parameter": parameter, "compound": compound, "median": value,
                "p10": value - .01, "p90": value + .01,
                "usable_lap_count": 20, "support_status": "measured", "session": "R",
            })
    for team, compound, value in (
        ("Ferrari", "HARD", .12), ("Ferrari", "MEDIUM", ferrari_medium),
        ("McLaren", "MEDIUM", .22),
    ):
        if team == "Ferrari" and not include_ferrari:
            continue
        rows.append({
            "parameter": "team_degradation", "team": team, "compound": compound,
            "median": value, "p10": value - .03, "p90": value + .04,
            "usable_lap_count": 10, "support_status": "measured", "session": "R",
        })
    return ModelSummary(
        {"analysis_id": f"live-{ferrari_medium}", "as_of_leader_lap": 35}, tuple(rows)
    )


def choose_team(window, team):
    assert team in window.team_buttons
    assert window.team_buttons[team].isEnabled()
    window.team_buttons[team].click()
    assert window.team_buttons[team].isChecked()
    assert window.team_button_group.checkedButton() is window.team_buttons[team]


def strategy_cell(window, compound, column):
    row = next(
        index for index in range(window.strategy_input_table.rowCount())
        if window.strategy_input_table.item(index, 0).text() == compound
    )
    return window.strategy_input_table.item(row, column)


def write_standings_run(root, run_id, generated_at, rows, *, status="SUCCESS"):
    run = root / "woweekend/runs/schema_v1/event=2026-10/workflow=post_race/runs" / run_id
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "event": "2026-10", "generated_at": generated_at, "status": "PARTIAL",
        "outputs": {"standings": status},
    }))
    (run / "standings.json").write_text(json.dumps({"constructors": {"actual": rows}}))
    return run


def test_team_roster_uses_latest_successful_prior_round_wcc_and_disables_missing_models(tmp_path, monkeypatch):
    write_event(tmp_path)
    old_rows = [{"Team": "Ferrari", "RoundNumber": 10, "CumulativePoints": 999.}]
    write_standings_run(tmp_path, "old-success", "2026-08-20T00:00:00Z", old_rows)
    rows = [
        {"Team": "Ferrari", "RoundNumber": 9, "CumulativePoints": 1000.},
        {"Team": "Ferrari", "RoundNumber": 10, "CumulativePoints": 100.},
        {"Team": "Mercedes", "RoundNumber": 10, "CumulativePoints": 200.},
        {"Team": "McLaren", "RoundNumber": 10, "CumulativePoints": 150.},
    ]
    for team in TEAM_COLOURS:
        if team not in {"Ferrari", "Mercedes", "McLaren"}:
            rows.append({"Team": team, "RoundNumber": 10, "CumulativePoints": 0.})
    selected = write_standings_run(tmp_path, "selected-success", "2026-08-21T00:00:00Z", rows)
    write_standings_run(tmp_path, "newer-failure", "2026-08-22T00:00:00Z", old_rows, status="FAILED")
    write_standings_run(tmp_path, "wrong-round", "2026-08-23T00:00:00Z", [
        {"Team": "Ferrari", "RoundNumber": 11, "CumulativePoints": 2000.},
    ])
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    try:
        window._load_model(summary=team_model())
        expected = [None, "Mercedes", "McLaren", "Ferrari"] + [
            team for team in TEAM_COLOURS if team not in {"Mercedes", "McLaren", "Ferrari"}
        ]
        assert list(window.team_buttons) == expected
        assert str(selected / "standings.json") in window.team_standings_source
        assert window.team_scope_grid.rowCount() == 2
        for index, team in enumerate(expected):
            row, column = divmod(index, 6)
            button = window.team_buttons[team]
            assert window.team_scope_grid.itemAtPosition(row, column).widget() is button
            assert window.team_standings_source in button.toolTip()
            assert button.isEnabled() == (team in {None, "Ferrari", "McLaren"})
        calls = []
        resolve = service.resolve_tyre_inputs

        def track_resolve(*args, **kwargs):
            calls.append(kwargs.get("team"))
            return resolve(*args, **kwargs)

        monkeypatch.setattr(service, "resolve_tyre_inputs", track_resolve)
        window.team_buttons["Mercedes"].click()
        window._team_scope_changed("Mercedes")
        assert not calls and window._selected_team is None
        choose_team(window, "Ferrari")
        assert calls[-1] == "Ferrari"
        assert window._manual_overrides[("degradation", "HARD")] == .12
    finally:
        window.close()
        qt.processEvents()


def test_available_team_colours_use_wodata_palette_and_unavailable_are_grey(tmp_path, monkeypatch):
    write_event(tmp_path)
    shared_colours = dict(TEAM_COLOURS)
    summary = team_model()
    summary = ModelSummary(summary.manifest, summary.parameters + tuple({
        "parameter": "team_degradation", "team": team, "compound": "MEDIUM",
        "median": .2, "usable_lap_count": 10, "support_status": "measured", "session": "R",
    } for team in ("Audi", "Racing Bulls", "Williams")))
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), team_scope_enabled=True,
    )
    try:
        window._load_model(summary=summary)
        assert not hasattr(analysis_gui, "STRATEGY_TEAM_DISPLAY_COLOURS")
        for team in ("Audi", "Racing Bulls", "Williams", "Ferrari", "McLaren"):
            colour = get_team_colour(team)
            assert colour == TEAM_COLOURS[team]
            button = window.team_buttons[team]
            assert button.isEnabled()
            assert f"color: {colour}" in button.styleSheet()
            assert f"border: 2px solid {colour}" in button.styleSheet()
        assert get_team_colour("Audi") != get_team_colour("Ferrari")
        assert get_team_colour("Racing Bulls") != get_team_colour("Williams")
        assert TEAM_COLOURS == shared_colours
        monkeypatch.setitem(TEAM_COLOURS, "Audi", "#123ABC")
        window._load_model(summary=summary)
        assert "color: #123ABC" in window.team_buttons["Audi"].styleSheet()
        choose_team(window, "Audi")
        window._load_model(summary=team_model())
        assert window._selected_team is None and window.team_buttons[None].isChecked()
        for team in ("Audi", "Racing Bulls", "Williams"):
            button = window.team_buttons[team]
            assert not button.isEnabled()
            assert f"color: {analysis_gui.STRATEGY_UNAVAILABLE_TEAM_COLOUR}" in button.styleSheet()
        assert "standings unavailable" in window.team_standings_source
        assert tuple(team for team in window.team_buttons if team is not None) == tuple(TEAM_COLOURS)
    finally:
        window.close()
        qt.processEvents()


def test_saved_roster_team_without_current_model_resets_to_global(tmp_path):
    write_event(tmp_path)
    state_dir = tmp_path / "saved"
    state_dir.mkdir()
    (state_dir / "strategy_prediction_state.json").write_text(json.dumps({
        "year": 2026, "round_number": 11, "controls": {"team": "Audi"},
    }))
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=state_dir, team_scope_enabled=True,
    )
    try:
        assert not window.team_buttons["Audi"].isEnabled()
        assert window._selected_team is None
        assert window.team_buttons[None].isChecked() and window.team_buttons[None].isEnabled()
        assert "Saved team Audi" in window.calculation_log.toPlainText()
    finally:
        window.close()
        qt.processEvents()


def test_team_selector_effective_sources_and_global_manual_isolation(tmp_path):
    write_event(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.set_manual_tyre_input(2026, 11, "Deg_SOFT", .99)
    service.set_manual_tyre_input(2026, 11, "Perf_HARD_vs_MEDIUM", 9.)
    service.set_manual_tyre_input(2026, 11, "Deg_MEDIUM", .45, team="Ferrari")
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    try:
        assert window.team_button_group.checkedButton().text() == "Global"
        window._load_model(summary=team_model())
        assert [button.text() for button in window.team_buttons.values()] == ["Global", *TEAM_COLOURS]
        assert window.team_scope_grid.rowCount() == 2
        assert window.team_scope_grid.itemAtPosition(0, 0).widget().text() == "Global"
        assert window.team_scope_grid.itemAtPosition(0, 1).widget().text() == "McLaren"
        assert window.team_scope_grid.itemAtPosition(1, 0).widget().text() == "Audi"
        assert window.team_buttons[None].isChecked()
        assert "TEAM_COLOURS roster order" in window.team_standings_source
        for team in ("Ferrari", "McLaren"):
            button = window.team_buttons[team]
            assert button.isCheckable() and not button.isChecked()
            assert button.isEnabled()
            assert f"color: {get_team_colour(team)}" in button.styleSheet()
            assert "border: 2px solid" in button.styleSheet()
        for team in set(TEAM_COLOURS).difference({"Ferrari", "McLaren"}):
            button = window.team_buttons[team]
            assert not button.isEnabled()
            assert f"color: {analysis_gui.STRATEGY_UNAVAILABLE_TEAM_COLOUR}" in button.styleSheet()
            assert "Unavailable" in button.toolTip()
            button.click()
            assert window._selected_team is None
        window.driver.addItem("LEC", "LEC")
        window.driver.setCurrentIndex(1)
        choose_team(window, "Ferrari")
        assert not window.team_buttons[None].isChecked()
        assert not window.team_buttons["McLaren"].isChecked()
        assert window.driver.currentData() == "LEC"
        assert window._manual_overrides[("degradation", "HARD")] == .12
        assert window._manual_overrides[("degradation", "MEDIUM")] == .45
        assert window._manual_overrides[("compound_delta", "HARD")] == .70
        defaults = service.resolve_tyre_inputs(2026, 11, team="Ferrari").by_parameter()
        expected_soft = .15 * defaults["Deg_SOFT"].legacy_value / defaults["Deg_MEDIUM"].legacy_value
        assert window._initial_degradation["MEDIUM"] == defaults["Deg_MEDIUM"].legacy_value
        assert window._manual_overrides[("degradation", "SOFT")] == pytest.approx(expected_soft)
        assert "team_live_direct" in strategy_cell(window, "HARD", 8).text()
        assert "team_live_derived" in strategy_cell(window, "SOFT", 8).text()
        assert "global_race_evidence" in strategy_cell(window, "HARD", 8).text()
        assert "Global model" in strategy_cell(window, "HARD", 8).text()
        window.manual_override.setChecked(True)
        assert window._manual_overrides[("degradation", "SOFT")] == defaults["Deg_SOFT"].legacy_value
        assert window._manual_overrides[("compound_delta", "HARD")] == defaults["Perf_HARD_vs_MEDIUM"].legacy_value
        choose_team(window, None)
        assert window._manual_overrides[("degradation", "SOFT")] == .99
        assert window._manual_overrides[("compound_delta", "HARD")] == 9.
    finally:
        window.close()
        qt.processEvents()


def test_scope_manual_edits_refresh_persistence_and_clear_are_isolated(tmp_path):
    write_event(tmp_path)
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    try:
        window._load_model(summary=team_model())
        for team, degradation, performance, persisted in (
            (None, .71, -.91, .41), ("Ferrari", .72, -.92, .42), ("McLaren", .73, -.93, .43)
        ):
            choose_team(window, team)
            window.tyre_input_table.item(1, 3).setText(str(persisted))
            strategy_cell(window, "HARD", 7).setText(str(degradation))
            strategy_cell(window, "SOFT", 4).setText(str(performance))
            assert service.tyre_inputs.load_manual_inputs(2026, 11, team=team)["Deg_MEDIUM"] == persisted
        for team, degradation, performance, persisted in (
            (None, .71, -.91, .41), ("Ferrari", .72, -.92, .42), ("McLaren", .73, -.93, .43)
        ):
            choose_team(window, team)
            window._load_model(summary=team_model(ferrari_medium=.25))
            window._refresh_tyre_inputs(apply_to_strategy=True)
            assert window._manual_overrides[("degradation", "HARD")] == degradation
            assert window._manual_overrides[("compound_delta", "SOFT")] == pytest.approx(performance)
            if team is not None:
                assert window._manual_overrides[("degradation", "MEDIUM")] == persisted
            assert "HARD" in window._manual_degradation_compounds
            assert "D: manual" in strategy_cell(window, "HARD", 8).text()
            assert "P: manual" in strategy_cell(window, "SOFT", 8).text()
        choose_team(window, "Ferrari")
        window._clear_manual_tyre_input("Deg_MEDIUM")
        assert window._manual_overrides[("degradation", "MEDIUM")] == .25
        window._clear_all_manual_tyre_inputs()
        assert window._manual_overrides[("degradation", "HARD")] == .12
        assert not window._active_manual_values()
        choose_team(window, "McLaren")
        assert window._manual_overrides[("degradation", "HARD")] == .73
        assert window._manual_overrides[("degradation", "MEDIUM")] == .43
        choose_team(window, None)
        assert window._manual_overrides[("degradation", "HARD")] == .71
        assert service.tyre_inputs.load_manual_inputs(2026, 11)["Deg_MEDIUM"] == .41
    finally:
        window.close()
        qt.processEvents()


def test_team_selection_and_local_manual_restore_then_missing_team_falls_back(tmp_path, monkeypatch):
    write_event(tmp_path)
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    monkeypatch.setattr(service, "load_model", lambda *args: team_model())
    options = dict(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    window = StrategyPredictionWindow(**options)
    strategy_cell(window, "HARD", 7).setText(".71")
    choose_team(window, "Ferrari")
    strategy_cell(window, "HARD", 7).setText(".72")
    choose_team(window, "McLaren")
    strategy_cell(window, "HARD", 7).setText(".73")
    choose_team(window, "Ferrari")
    window.close()
    qt.processEvents()
    restored = StrategyPredictionWindow(**options)
    try:
        assert restored.team_buttons["Ferrari"].isChecked()
        assert restored._selected_team == "Ferrari"
        assert restored._manual_overrides[("degradation", "HARD")] == .72
        choose_team(restored, "McLaren")
        assert restored._manual_overrides[("degradation", "HARD")] == .73
        choose_team(restored, None)
        assert restored._manual_overrides[("degradation", "HARD")] == .71
        choose_team(restored, "Ferrari")
        restored._load_model(summary=team_model(include_ferrari=False))
        assert restored.team_buttons[None].isChecked()
        assert restored._selected_team is None
        assert restored._manual_overrides[("degradation", "HARD")] == .71
        assert "Ferrari is absent" in restored.calculation_log.toPlainText()
        assert not restored.team_buttons["Ferrari"].isEnabled()
    finally:
        restored.close()
        qt.processEvents()


def test_team_search_snapshot_freezes_effective_values_and_history_stays_global(tmp_path, monkeypatch):
    write_event(tmp_path)
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    captured = {}
    snapshots = []
    actual_snapshot = analysis_gui.tyre_snapshot_from_overrides

    def freeze(overrides, resolved, **kwargs):
        snapshots.append((resolved.team, kwargs))
        return actual_snapshot(overrides, resolved, **kwargs)

    monkeypatch.setattr(analysis_gui, "tyre_snapshot_from_overrides", freeze)
    monkeypatch.setattr(service, "generate_continuations", lambda **kwargs: captured.update(kwargs) or [])
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    try:
        window._load_model(summary=team_model())
        choose_team(window, "Ferrari")
        strategy_cell(window, "HARD", 7).setText(".72")
        strategy_cell(window, "SOFT", 4).setText("-.92")
        window._load_model(summary=team_model(ferrari_medium=.25), record_history=True)
        history_hard = next(
            row for row in window._working_plot_rows
            if row["as_of_leader_lap"] == 35 and row["compound"] == "HARD"
            and row["parameter"] == "degradation"
        )
        assert history_hard["median"] == .20
        window._start_search()
        window._future.result(timeout=5)
        assert snapshots[0][0] == "Ferrari"
        assert snapshots[0][1]["manual_values"][("degradation", "HARD")] == .72
        assert all(row["team"] == "Ferrari" for row in snapshots[0][1]["assumptions"])
        overrides = captured["overrides"]
        frozen = captured["tyre_input_snapshot"]
        assert frozen["team"] == "Ferrari"
        for compound in ("HARD", "MEDIUM", "SOFT"):
            assert frozen["values"][f"Deg_{compound}"] == overrides[("degradation", compound)]
            assert float(strategy_cell(window, compound, 7).text()) == pytest.approx(
                overrides[("degradation", compound)], abs=.00005
            )
        for compound in ("HARD", "SOFT"):
            assert frozen["values"][f"Perf_{compound}_vs_MEDIUM"] == pytest.approx(
                overrides[("compound_delta", compound)] - overrides[("compound_delta", "MEDIUM")]
            )
        assert frozen["selected_sources"]["Deg_HARD"] == "manual"
        assert frozen["selected_sources"]["Deg_MEDIUM"] == "team_live_direct"
        assert frozen["selected_sources"]["Deg_SOFT"] == "team_live_derived"
        assert frozen["selected_sources"]["Perf_SOFT_vs_MEDIUM"] == "manual"
        assert "unattributed_override" not in frozen["selected_sources"].values()
        manual = next(row for row in snapshots[0][1]["assumptions"] if row["compound"] == "HARD")
        assert not {"p10", "p90", "ess"}.intersection(manual)
        assert overrides[("degradation", "HARD")] == .72
        assert overrides[("compound_delta", "SOFT")] == pytest.approx(-.92)
    finally:
        window.close()
        qt.processEvents()


def test_global_manual_history_has_no_model_uncertainty_while_team_active(tmp_path):
    write_event(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", .72)
    service.set_manual_tyre_input(2026, 11, "Perf_HARD_vs_MEDIUM", .81)
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    summary = team_model()
    summary = ModelSummary({
        **summary.manifest,
        "sampler_quality": {
            "ess": 80., "sample_count": 100, "ess_fraction": .8, "max_weight": .02,
            "top5_weight": .1, "clean_race_laps": 20, "clean_race_runs": 4,
            "numerical_quality_status": "stable",
        },
    }, summary.parameters)
    try:
        window._load_model(summary=summary)
        window.manual_override.setChecked(True)
        choose_team(window, "Ferrari")
        window._load_model(summary=summary, record_history=True)
        rows = [row for row in window._working_plot_rows if row["as_of_leader_lap"] == 35]
        for parameter, value in (("degradation", .72), ("compound_delta", .81)):
            row = next(row for row in rows if row["compound"] == "HARD" and row["parameter"] == parameter)
            assert row["median"] == pytest.approx(value)
            assert row.get("p10") is None and row.get("p90") is None
            assert row.get("p10_p90_width") is None
            assert row["sampler_ess"] == 80.
            assert row["source_scope"] == "aggregate"
        assert "team" not in analysis_gui.format_quality_hover_text(rows, leader_lap=35).lower()
    finally:
        window.close()
        qt.processEvents()


@pytest.mark.parametrize("scoped", [
    None,
    {
        "Global": [
            {"parameter": "degradation", "compound": "HARD"},
            {"parameter": "degradation", "compound": "SOFT", "value": {}},
            {"parameter": ["degradation"], "compound": "HARD", "value": .9},
            {"parameter": "degradation", "compound": "SOFT", "value": "nan"},
            None,
            {"parameter": "degradation", "compound": "MEDIUM", "value": .51},
        ],
        "Ferrari": None,
    },
])
def test_invalid_saved_scope_manual_values_do_not_break_window(tmp_path, scoped):
    write_event(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "strategy_prediction_state.json").write_text(json.dumps({
        "year": 2026, "round_number": 11,
        "scope_manual_values": scoped, "controls": {},
    }))
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=state_dir,
        team_scope_enabled=True,
    )
    try:
        assert window._selected_team is None
        assert "Ignored invalid saved scope manual" in window.calculation_log.toPlainText()
        assert ("degradation", "HARD") not in window._active_manual_values()
        if scoped is not None:
            assert window._manual_overrides[("degradation", "MEDIUM")] == .51
    finally:
        window.close()
        qt.processEvents()


def test_standard_default_keeps_global_scope_and_hides_team_controls(tmp_path):
    write_event(tmp_path)
    state_dir = tmp_path / "shared_state"
    state_dir.mkdir()
    (state_dir / "strategy_prediction_state.json").write_text(json.dumps({
        "year": 2026, "round_number": 11, "controls": {"team": "Ferrari"},
        "scope_manual_values": {
            "Global": [{"parameter": "degradation", "compound": "HARD", "value": .72}],
        },
    }))
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=state_dir,
    )
    try:
        window._load_model(summary=team_model())
        window.show()
        qt.processEvents()
        assert not window.team_scope_enabled
        assert not window.team_buttons and window.team_scope_grid.count() == 0
        assert window.team_scope_widget.isHidden()
        assert window.strategy_input_table.editTriggers() == QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        assert not strategy_cell(window, "HARD", 7).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
        assert not window._active_manual_values()
        window._team_scope_changed("Ferrari")
        assert window._selected_team is None
        assert window._manual_overrides[("degradation", "HARD")] == .20
        window._save_state()
        saved = json.loads((state_dir / "strategy_prediction_state.json").read_text())
        assert saved["controls"]["team"] == "Ferrari"
        assert saved["scope_manual_values"]["Global"][0]["value"] == .72
    finally:
        window.close()
        qt.processEvents()


@pytest.mark.parametrize("team", [None, "Ferrari"])
def test_nonzero_medium_performance_manual_is_stable_at_search_boundary(tmp_path, monkeypatch, team):
    write_event(tmp_path)
    qt = app()
    service = OfflineAnalysisService(tmp_path)
    calls = []
    monkeypatch.setattr(service, "generate_continuations", lambda **kwargs: calls.append(kwargs) or [])
    summary = team_model()
    deltas = {"HARD": 0., "MEDIUM": -.297279, "SOFT": -.665295}
    summary = ModelSummary({
        **summary.manifest,
        "sampler_quality": {"ess": 80., "sample_count": 100, "ess_fraction": .8},
    }, tuple(
        {**row, "median": deltas[row["compound"]],
         "p10": deltas[row["compound"]] - .01, "p90": deltas[row["compound"]] + .01}
        if row["parameter"] == "compound_delta" else row for row in summary.parameters
    ))
    window = StrategyPredictionWindow(
        year=2026, round_number=11, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=service, team_scope_enabled=True,
    )
    try:
        def assert_raw_global_history():
            history = {
                (row["parameter"], row["compound"]): row
                for row in window._working_plot_rows if row["as_of_leader_lap"] == 35
            }
            for compound, value in deltas.items():
                row = history[("compound_delta", compound)]
                assert row["median"] == pytest.approx(value)
                model_row = next(
                    item for item in summary.parameters
                    if item["parameter"] == "compound_delta" and item["compound"] == compound
                )
                assert row["p10"] == model_row["p10"]
                assert row["p90"] == model_row["p90"]
            assert history[("degradation", "HARD")]["median"] == .20

        window._load_model(summary=summary, record_history=True)
        assert_raw_global_history()
        assert {
            compound: window._manual_overrides[("compound_delta", compound)]
            for compound in ("HARD", "MEDIUM", "SOFT")
        } == pytest.approx({"HARD": .297279, "MEDIUM": 0., "SOFT": -.368016})
        strategy_cell(window, "HARD", 4).setText(".91")
        strategy_cell(window, "HARD", 7).setText(".71")
        choose_team(window, team)
        strategy_cell(window, "HARD", 4).setText(".81")
        strategy_cell(window, "HARD", 7).setText(".72")
        window._load_model(summary=summary, record_history=True)
        assert_raw_global_history()
        for _ in range(3):
            window._sync_strategy_overrides()
            assert window._manual_overrides[("compound_delta", "HARD")] == pytest.approx(.81)
            assert window._manual_overrides[("compound_delta", "MEDIUM")] == 0.
            assert float(strategy_cell(window, "HARD", 4).text()) == .81
        window._start_search()
        window._future.result(timeout=5)
        assert calls[-1]["overrides"][("compound_delta", "HARD")] == pytest.approx(.81)
        snapshot = calls[-1]["tyre_input_snapshot"]
        assert snapshot["team"] == team
        assert snapshot["values"]["Perf_HARD_vs_MEDIUM"] == pytest.approx(.81)
        assert snapshot["selected_sources"]["Perf_HARD_vs_MEDIUM"] == "manual"
    finally:
        window.close()
        qt.processEvents()


def test_current_event_strategy_inputs_stay_canonical_when_live_snapshot_arrives(
    tmp_path, monkeypatch,
):
    write_event(tmp_path, year=2026, round_number=14)
    monkeypatch.setattr(
        analysis_gui,
        "load_operational_race_inputs",
        lambda: OperationalRaceInputs(
            tmp_path / "race.json", 2026, 14, 57, 24.2, 16.0, 13.0, None
        ),
    )
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=14,
        mode=StrategyPredictionMode.CURRENT_EVENT,
        service=OfflineAnalysisService(tmp_path),
    )

    assert window.total_laps.value() == 57
    assert window.pit_loss.value() == pytest.approx(24.2)
    assert window.total_laps.isReadOnly()
    assert window.pit_loss.isReadOnly()

    window._live_snapshot = type(
        "Snapshot", (), {"completed_laps": (), "scheduled_total_laps": 70}
    )()
    window._apply_latest_race_state()
    assert window.total_laps.value() == 57

    window.close()
    qt.processEvents()


def test_current_event_config_failure_stays_unresolved_and_ignores_stale_state(
    tmp_path, monkeypatch,
):
    write_event(tmp_path, year=2026, round_number=14)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "strategy_prediction_state.json").write_text(json.dumps({
        "year": 2026, "round_number": 14,
        "controls": {"total_laps": 66, "pit_loss": 18.0},
    }))
    monkeypatch.setattr(
        analysis_gui, "load_operational_race_inputs",
        lambda: (_ for _ in ()).throw(OSError("missing race config")),
    )
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=14,
        mode=StrategyPredictionMode.CURRENT_EVENT,
        service=OfflineAnalysisService(tmp_path), state_directory=state_dir,
    )

    assert window.total_laps.value() == 0
    assert window.pit_loss.value() == -1.0
    assert not window.generate_button.isEnabled()
    assert "could not be read" in window.operational_status_label.text()
    window._start_search()
    assert window._future is None
    assert "calculation blocked" in window.status_label.text()
    window._live_snapshot = type(
        "Snapshot", (), {"completed_laps": (), "scheduled_total_laps": 70}
    )()
    window._apply_latest_race_state()
    assert window.total_laps.value() == 0

    window.close()
    qt.processEvents()


def test_strategy_mode_controls_defaults_instead_of_config_mismatch(tmp_path, monkeypatch):
    write_event(tmp_path, year=2026, round_number=14)
    monkeypatch.setattr(
        analysis_gui,
        "load_operational_race_inputs",
        lambda: OperationalRaceInputs(
            tmp_path / "race.json", 2026, 13, 57, 24.2, 15.0, 12.0, None
        ),
    )
    qt = app()
    current = StrategyPredictionWindow(
        year=2026, round_number=14,
        mode=StrategyPredictionMode.CURRENT_EVENT,
        service=OfflineAnalysisService(tmp_path),
    )
    historical = StrategyPredictionWindow(
        year=2026, round_number=14,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path),
    )

    assert current.total_laps.value() == 0
    assert current.pit_loss.value() == -1.0
    assert not current.generate_button.isEnabled()
    assert "config is 2026-13" in current.operational_status_label.text()
    assert historical.total_laps.value() == 57
    assert historical.pit_loss.value() == pytest.approx(20.0)
    assert not historical.total_laps.isReadOnly()
    assert not historical.pit_loss.isReadOnly()

    current.close()
    historical.close()
    qt.processEvents()


def test_strategy_window_and_tyre_tab_never_auto_calculate(tmp_path):
    write_event(tmp_path)
    calls = []
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs._fp_calculator = lambda **kwargs: calls.append(kwargs) or {
        "analysis_id": "explicit"
    }
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF, service=service
    )

    assert window.tabs.indexOf(window.tyre_inputs_tab) >= 0
    assert calls == []
    window.tabs.setCurrentWidget(window.tyre_inputs_tab)
    qt.processEvents()
    assert calls == []
    assert window.fp_evidence_status_label.text() == "FP evidence not calculated"

    manual = window.tyre_input_table.item(0, 3)
    manual.setText("0.071")
    qt.processEvents()
    assert calls == []
    assert service.tyre_inputs.load_manual_inputs(2026, 11)["Deg_HARD"] == 0.071
    window._clear_manual_tyre_input("Deg_HARD")
    assert calls == []

    window.close()
    qt.processEvents()


def test_explicit_buttons_make_exactly_one_request_and_cached_display_is_read_only(tmp_path):
    write_event(tmp_path)
    calls = []
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs._fp_calculator = lambda **kwargs: calls.append(kwargs) or {
        "analysis_id": "explicit"
    }
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF, service=service,
    )

    window._start_fp_evidence_calculation(force_refresh=False)
    window._fp_future.result(timeout=5)
    window._poll_fp_evidence()
    assert len(calls) == 1

    evidence = {
        "schema_version": 1,
        "analysis_id": "cached",
        "calculated_at": "2026-08-16T10:00:00+00:00",
        "sessions_used": ["FP1", "FP2"],
        "source_fingerprints": {},
        "degradation_contrasts": [],
        "performance_support": [],
        "longrun_laps": [],
    }
    write_fp_tyre_evidence(evidence, year=2026, round_number=11, data_root=tmp_path)
    window._refresh_fp_evidence()
    assert "Calculated" in window.fp_evidence_status_label.text()
    assert len(calls) == 1

    from wodata import get_fastf1_session_laps_path

    fp3 = get_fastf1_session_laps_path(
        year=2026, round_number=11, session="FP3", data_root=tmp_path
    )
    fp3.parent.mkdir(parents=True, exist_ok=True)
    fp3.write_bytes(b"new local FP3")
    window._refresh_fp_evidence()
    assert "FP data have changed" in window.fp_evidence_status_label.text()
    assert window._cached_fp_evidence["analysis_id"] == "cached"
    assert len(calls) == 1

    window._start_fp_evidence_calculation(force_refresh=True)
    window._fp_future.result(timeout=5)
    window._poll_fp_evidence()
    assert len(calls) == 2
    assert calls[-1]["force_refresh"] is True
    window.close()
    qt.processEvents()


def test_selected_table_equals_pre_race_strategy_handoff_and_state_is_shared(tmp_path):
    write_event(tmp_path)
    first_service = OfflineAnalysisService(tmp_path)
    first_service.set_manual_tyre_input(2026, 11, "Deg_HARD", 0.071)
    qt = app()
    planner = StrategyPredictionWindow(
        year=2026, round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF, service=first_service
    )
    live = StrategyPredictionWindow(
        year=2026, round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path),
    )

    resolved = first_service.resolve_tyre_inputs(2026, 11)
    assert planner._manual_overrides == pytest.approx(resolved.strategy_overrides())
    assert live._manual_overrides == pytest.approx(resolved.strategy_overrides())
    assert planner._resolved_tyre_inputs.content_hash == live._resolved_tyre_inputs.content_hash

    planner.close()
    live.close()
    qt.processEvents()


def test_live_application_startup_does_not_calculate_fp(monkeypatch):
    calls = []
    monkeypatch.setattr(
        TyreInputService,
        "calculate_fp_tyre_evidence",
        lambda self, *args, **kwargs: calls.append((args, kwargs)),
    )
    qt = app()
    window = LivePlannerWindow()

    assert calls == []
    window.close()
    qt.processEvents()
