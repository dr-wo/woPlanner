"""Delivery warning stays independent of diagnostic fit and render success."""
from dataclasses import replace
from types import SimpleNamespace

from PySide6 import QtWidgets

from woplanner.quali.service import QualiLiveService
from woplanner.quali.widgets import QualiPlanDialog


def test_track_tab_warning_is_amber_nonmodal_and_clears_without_fit(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = QualiLiveService()
    analysis = SimpleNamespace(result=None, effective_fp_track_rate=lambda: (0., "No usable FP estimate", 0))
    # Rendering itself has dedicated worker/decode/recovery tests. Keep this
    # test focused on the synchronous status line and unsupported diagnostic.
    monkeypatch.setattr("woplanner.quali.widgets.TrackEvolutionDiagnosticView._start_render", lambda self: None)
    dialog = QualiPlanDialog(analysis, service)
    assert dialog.track_telemetry_warning.isHidden()
    assert service.snapshot.track_evolution_diagnostic.driver_count == 0
    warning = "⚠ Track telemetry delayed: 3 laps exceeded 2s in the last 10 min. Track Correction may update late."
    snapshot = replace(service.snapshot, telemetry_delay_warning=warning, telemetry_delayed_lap_count=3)
    dialog.refresh_live_track(snapshot)
    label = dialog.track_telemetry_warning
    assert not label.isHidden()
    assert label.text() == warning
    assert "#b26a00" in label.styleSheet()
    assert not label.wordWrap()
    for _ in range(10):
        dialog.refresh_live_track(snapshot)
    assert label.text() == warning
    assert not any(isinstance(widget, QtWidgets.QMessageBox) for widget in app.topLevelWidgets())
    dialog.refresh_live_track(service.snapshot)
    assert label.isHidden()
    assert label.text() == ""
    assert dialog.track_diagnostic._support_status.text().startswith("Track Correction unsupported")
    dialog.close()
    app.processEvents()
