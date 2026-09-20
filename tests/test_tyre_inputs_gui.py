from __future__ import annotations

from datetime import datetime, timezone
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets
import pytest

from wodata.artifacts import weekend_model_root, write_model_snapshot
from wodata.contracts import ModelSnapshot, ParameterEstimate
import woplanner.analysis.gui as analysis_gui
from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import OfflineAnalysisService
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
