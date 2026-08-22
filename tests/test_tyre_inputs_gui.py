from __future__ import annotations

from datetime import datetime, timezone
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets
import pytest

from wodata.artifacts import weekend_model_root, write_model_snapshot
from wodata.contracts import ModelSnapshot, ParameterEstimate
from woplanner.analysis.gui import StrategyPredictionWindow
from woplanner.analysis.service import OfflineAnalysisService
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


def test_strategy_window_and_tyre_tab_never_auto_calculate(tmp_path):
    write_event(tmp_path)
    calls = []
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs._fp_calculator = lambda **kwargs: calls.append(kwargs) or {
        "analysis_id": "explicit"
    }
    qt = app()
    window = StrategyPredictionWindow(
        year=2026, round_number=11, service=service
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
    window = StrategyPredictionWindow(year=2026, round_number=11, service=service)

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
        year=2026, round_number=11, service=first_service
    )
    live = StrategyPredictionWindow(
        year=2026, round_number=11, service=OfflineAnalysisService(tmp_path)
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
