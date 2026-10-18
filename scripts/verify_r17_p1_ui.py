"""Exercise actual R17 P1 calculation, Apply/Restore and a white UI capture.

Uses only the checked-in observed prefix and existing model configuration. Saved
window settings are isolated in a temporary directory; fitted models are not saved.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from verify_r17_p1_reference import load_prefixes
from woplanner.analysis.gui import StrategyPredictionWindow, StrategyPredictionMode
from woplanner.analysis.service import OfflineAnalysisService


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=root.parent / "woData")
    args = parser.parse_args()
    evidence_dir = root / "doc/assets/26r17_final_acceptance"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = {}
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    for role, colour in (
        (QtGui.QPalette.ColorRole.Window, "white"),
        (QtGui.QPalette.ColorRole.Base, "white"),
        (QtGui.QPalette.ColorRole.WindowText, "#172033"),
        (QtGui.QPalette.ColorRole.Text, "#172033"),
        (QtGui.QPalette.ColorRole.Button, "#f4f6f8"),
        (QtGui.QPalette.ColorRole.ButtonText, "#172033"),
    ):
        palette.setColor(role, QtGui.QColor(colour))
    app.setPalette(palette)
    _, snapshots = load_prefixes(root / "tests/fixtures/r17_p1_prefixes.json.gz")
    service = OfflineAnalysisService(args.data_root)
    service.recalculate_from_live_snapshot(
        year=2026, round_number=17, snapshot=snapshots[14], sample_count=2000,
        quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True,
        persist=False,
    )
    standard = service.load_model(2026, 17)
    with tempfile.TemporaryDirectory(prefix="r17-p1-ui-") as state:
        window = StrategyPredictionWindow(
            year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
            service=service, state_directory=state, initial_model_summary=standard,
            team_scope_enabled=True, live_operational_controls=True,
        )
        try:
            assert not window.p1_reference.acknowledge.isChecked()
            window.auto_recalculate.setChecked(False)
            window.sample_count.setValue(2000)
            window.consume_live_snapshot(snapshots[14])
            panel = window.p1_reference
            window.tabs.setCurrentWidget(panel)
            window.resize(1520, 900)
            window.show()
            app.processEvents()
            panel.first_lap.setValue(3)
            panel.sector_traffic.setChecked(True)
            assert not panel.acknowledge.isChecked()
            assert not panel.calculate.isEnabled() and not panel.apply.isEnabled()
            panel.calculate.click()
            assert not panel.worker.running
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
            assert window.grab().save(str(evidence_dir / "p1_initial_unchecked.png"))
            evidence["fresh_checkbox_unchecked"] = True
            evidence["unconfirmed_calculation_and_apply_blocked"] = True
            assert window.tabs.tabText(window.tabs.indexOf(panel)) == "P1 Pace Reference"
            assert panel.title() == "Leader Pace Estimation"
            assert panel.calculate.text() == "Calculate Reference"
            assert any(label.text() == "Lap range" for label in panel.findChildren(QtWidgets.QLabel))
            QtTest.QTest.mouseClick(panel.acknowledge, QtCore.Qt.MouseButton.LeftButton,
                                   pos=QtCore.QPoint(8, panel.acknowledge.height() // 2))
            assert panel.acknowledge.isChecked() and panel.calculate.isEnabled()
            panel.calculate.click()
            deadline = time.monotonic() + 30
            while panel.worker.running and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            app.processEvents()
            assert panel.result is not None, panel.status.text()
            assert panel.result.status == "updated", panel.status.text()
            assert panel.result.selected_lap_numbers == (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)
            assert window.traffic_source.currentData() == "automatic"
            team = panel.result.team
            assert window.team_buttons[team].isEnabled()
            window.team_buttons[team].click()
            app.processEvents()
            assert panel.apply.isEnabled()
            assert window._current_model_summary is standard
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
            output = root / "doc/assets/26r17_p1_reference.png"
            assert window.grab().save(str(output))
            assert window.grab().save(str(evidence_dir / "p1_calculated.png"))
            before = dict(window._manual_overrides)
            before_scoped = copy.deepcopy(window._scope_manual_values.get(team, {}))
            panel.apply.click()
            applied = {("degradation", row["compound"]): float(row["median"])
                       for row in panel.result.applicable_parameters}
            assert all(window._manual_overrides[key] == value for key, value in applied.items())
            assert all(window._manual_overrides[key] == value for key, value in before.items() if key not in applied)
            assert window._current_model_summary is standard
            assert service.load_model(2026, 17) is standard
            assert window.traffic_source.currentData() == "automatic"
            panel.restore.click()
            assert window._manual_overrides == before
            evidence["accepted_laps"] = list(panel.result.selected_lap_numbers)
            evidence["actual_spawned_worker"] = True
            evidence["apply_restore_and_standard_isolation"] = True
            panel.apply.click()
            saved = json.loads(window._state_path.read_text())
            saved_team = {(row["parameter"], row["compound"]): row["value"]
                          for row in saved["scope_manual_values"].get(team, [])}
            assert saved_team == before_scoped
            evidence["saved_settings_exclude_p1_overrides"] = True
            window.consume_live_snapshot(snapshots[12])
            assert window._scope_manual_values.get(team, {}) == before_scoped
            assert not any(value.startswith("P1 ") for value in window._external_tyre_sources.values())
            assert panel.result is None and not panel.acknowledge.isChecked()
            assert not panel.apply.isEnabled()
            evidence["rewind_restores_applied_inputs"] = True
            window._restart_analysis_worker()
            assert not panel.acknowledge.isChecked()
            assert not panel.calculate.isEnabled()
            evidence["session_reset_requires_reconfirmation"] = True
        finally:
            window.close()
            app.processEvents()
        reopened = StrategyPredictionWindow(
            year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
            service=service, state_directory=state, initial_model_summary=standard,
            team_scope_enabled=True, live_operational_controls=True,
        )
        try:
            reopened.auto_recalculate.setChecked(False)
            reopened.consume_live_snapshot(snapshots[14])
            assert not reopened.p1_reference.acknowledge.isChecked()
            assert reopened.p1_reference.result is None
            assert not reopened.p1_reference.apply.isEnabled()
            assert not reopened.p1_reference.calculate.isEnabled()
            assert reopened._scope_manual_values.get(team, {}) == before_scoped
            evidence["reopening_does_not_reapply_saved_reference"] = True
        finally:
            reopened.close()
            app.processEvents()
    evidence["screenshots"] = [str(evidence_dir / name) for name in
                                ("p1_initial_unchecked.png", "p1_calculated.png")]
    (evidence_dir / "p1_gui.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
