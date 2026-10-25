"""Readiness and admission explanations preserve the accepted domain policy."""
import os
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6 import QtWidgets

from test_quali_service import driver, raw
from test_quali_canonical_track_rate import sample, seed
from wodata.live import QualiTyreStint
from woplanner.quali import gui
from woplanner.quali.experimental_cutoff import calculate_half_live
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution_presentation import readiness_messages


@pytest.fixture
def window():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = gui.QualiWindow()
    yield view
    view.close()
    app.processEvents()


@pytest.mark.parametrize("reference", [None, LapPaceReference(s1_to_s2_s=30.),
    LapPaceReference(start_to_s1_s=float("nan")),
    LapPaceReference(start_to_5pct_s=0., start_to_s1_s=-1.)])
def test_unusable_pace_reference_explains_missing_fast_input(reference):
    service = QualiLiveService()
    service._common_lap_state_reference = reference
    result = service.track_evolution_payload()
    assert not result["readiness"]["pace_reference_available"]
    assert not result["readiness"]["cool_duration_available"]
    text = "\n".join(readiness_messages(result))
    assert "FP pace reference unavailable" in text
    assert "COOL/PREP duration unavailable" in text
    assert "No admitted observations" in text
    assert result["estimate_s_per_global_push"] == 0.
    assert result["status"] == "FALLBACK"


def test_partial_driver_references_and_finite_positive_cool_duration():
    service = QualiLiveService()
    service.consume(raw([driver(1, 90), driver(2, 91)]))
    service.lap_state_references["1"] = LapPaceReference(start_to_5pct_s=5.)
    result = service.track_evolution_payload()
    assert result["readiness"]["pace_reference_available"]
    assert result["readiness"]["drivers_without_pace_reference"] == ("D2",)
    assert "unavailable for: D2" in "\n".join(readiness_messages(result))
    service.set_manual_operational_timing("cool_lap_s", 120.)
    assert service.track_evolution_payload()["readiness"]["cool_duration_available"]
    service.set_manual_operational_timing("cool_lap_s", float("inf"))
    assert not service.track_evolution_payload()["readiness"]["cool_duration_available"]


def test_ingested_lap_without_reference_is_reported_without_admitting_it():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    service.consume(raw([driver(1, None, laps=0)]))
    timed = replace(driver(1, 90., laps=1), tyre_history=(QualiTyreStint("SOFT", 0, True, 1),))
    snapshot = service.consume(raw([timed]))
    result = snapshot.track_evolution_result
    assert snapshot.drivers[0].lap_state == "UNKNOWN"
    assert snapshot.drivers[0].last_lap_s == 90.
    assert result["attrition"]["non_operational_fast_completions"] == 1
    assert result["support"] == {"observations": 0, "fit_observations": 0, "drivers": 0}
    assert "1 completed laps ingested but not operational FAST" in "\n".join(readiness_messages(result))
    assert result["estimate_source"] == "CONFIGURED_FP_DEFAULT_PRIOR"


def test_admitted_observation_is_distinct_from_supported_driver_slope():
    service = QualiLiveService()
    seed(service, sample(1, 90., code="RUS"), sample(2, 90., code="ANT"))
    first = service.track_evolution_payload()
    assert first["support"] == {"observations": 2, "fit_observations": 0, "drivers": 0}
    assert all(row["reason"] == "INSUFFICIENT_COMPARABLE_OBSERVATIONS"
               for row in first["observation_support"])
    assert "second comparable tyre installation" in "\n".join(readiness_messages(first))
    # Returned diagnostic containers are detached from the immutable-identity cache.
    first["observation_support"][0]["observations"] = 99
    assert service.track_evolution_payload()["observation_support"][0]["observations"] == 1
    seed(service, sample(3, 89.8, code="RUS"))
    second = service.track_evolution_payload()
    rus = next(row for row in second["observation_support"] if row["driver"] == "RUS")
    assert rus == {"driver": "RUS", "observations": 2, "comparable_observations": 2,
                   "fit_observations": 2, "reason": None}
    assert second["support"]["drivers"] == 1


def test_zero_fallback_and_missing_target_are_distinct_without_formula_changes(window):
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    assert calculate_half_live(service.snapshot).reason == "No observed cutoff target"
    snapshot = service.consume(raw([driver(1, 90.)]))
    prediction = calculate_half_live(snapshot)
    assert prediction.status == "FALLBACK"
    assert prediction.rate_s_per_field_push == prediction.correction_s == 0.
    assert prediction.projected_cutoff_s == 90.
    assert "fallback is zero" in prediction.reason
    window._render_cutoff_predictions(snapshot)
    assert "Zero fallback; target unchanged" in window.experimental_cutoff.text()
    assert "FP pace reference unavailable" in window.readiness.text()
    assert "COOL/PREP duration unavailable" in window.readiness.text()
    window._render_cutoff_predictions(replace(snapshot, cutoff=replace(snapshot.cutoff, current_cutoff_s=None)))
    assert "No observed cutoff target" in window.experimental_cutoff.toolTip()


def test_analysis_failure_uses_phase_and_path_diagnostics(window, monkeypatch):
    error = FileNotFoundError("missing session")
    def fail(*args):
        raise error
    window.analysis_service = SimpleNamespace(analyze=fail,
        failure_message=lambda exc: f"FP2 session_load: {exc}; diagnostics: /tmp/fp-diagnostic.json")
    messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: messages.append(args[2]))
    window._show_fp_analysis_failure(error)
    assert messages == ["FP2 session_load: missing session; diagnostics: /tmp/fp-diagnostic.json"]


def test_partial_fp_success_keeps_success_and_reports_unavailable_session(window, monkeypatch):
    result = SimpleNamespace(laps=pd.DataFrame(), durations=pd.DataFrame())
    window.analysis_service = SimpleNamespace(
        analyze=lambda *args: result, effective_fp_track_rate=lambda: (0., "No usable FP estimate", 0),
        last_diagnostics={"sessions": {"FP1": {"status": "success"},
            "FP2": {"status": "failed", "error": {"type": "FileNotFoundError"}}},
            "warnings": [{"phase": "reference_telemetry"}]},
        diagnostic_path="/tmp/fp-diagnostic.json",
    )
    messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *args: messages.append(args[2]))
    window._apply_fp_analysis(result, window.year.value(), window.round.value())
    assert "Analysed 0 FP laps" in messages[0]
    assert "Sessions unavailable: FP2" in messages[0]
    assert "1 reference-detail warnings" in messages[0]
    assert "Diagnostics: /tmp/fp-diagnostic.json" in messages[0]
