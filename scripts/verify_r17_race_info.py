"""Replay the checked-in R17 notice excerpt and capture the Race Info UI.

This is an archived-notice replay, not an original live-feed capture. The old
R17 race recording did not subscribe to RaceControlMessages. No network access
or persisted event configuration is needed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

from PySide6 import QtGui, QtWidgets
from wodata.live import LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.replay.player import ReplayPlayer
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerService


def main():
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / "tests/fixtures/r17_race_control_l52_l55.json").read_text())
    output = root / "doc/assets/26r17_race_info.png"
    evidence_dir = root / "doc/assets/26r17_final_acceptance"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    for role, colour in (
        (QtGui.QPalette.ColorRole.Window, "#ffffff"),
        (QtGui.QPalette.ColorRole.Base, "#ffffff"),
        (QtGui.QPalette.ColorRole.AlternateBase, "#f3f5f7"),
        (QtGui.QPalette.ColorRole.WindowText, "#202833"),
        (QtGui.QPalette.ColorRole.Text, "#202833"),
        (QtGui.QPalette.ColorRole.Button, "#f3f5f7"),
        (QtGui.QPalette.ColorRole.ButtonText, "#202833"),
    ):
        palette.setColor(role, QtGui.QColor(colour))
    app.setPalette(palette)
    with TemporaryDirectory(prefix="r17-race-info-") as tmp:
        recording = Path(tmp) / "notices.jsonl"
        recorder = LiveTimingRecorder(recording)
        for index, fields in enumerate(fixture["messages"]):
            source_time = datetime.fromisoformat(fields["Utc"].replace("Z", "+00:00"))
            recorder.write(LiveTimingMessage(
                "RaceControlMessages", {"Messages": {str(index): fields}},
                source_time, source_time, source="synthetic_test", sequence=index + 1,
                session_time_seconds=float(index),
            ))
        recorder.close()
        config = Path(tmp) / "race.json"
        config.write_text(json.dumps({
            "season": 2026, "round_number": 17, "race": {"total_laps": 62},
            "pit_loss": {kind: {"total": None} for kind in ("green", "sc", "vsc")},
        }))
        service = LivePlannerService()
        window = LivePlannerWindow(service=service, event_config_path=config)
        try:
            window.resize(1520, 820)
            window.year_input.setValue(2026)
            window.round_input.setValue(17)
            window.tabs.setCurrentWidget(window.race_info_tab)
            window.show()
            result = ReplayPlayer(recording, speed=0).play(service.controller._enqueue_message)
            window._poll()
            app.processEvents()
            events = service.snapshot.race_control_events
            assert result.status == "completed" and len(events) == 8
            assert [event.message for event in events] == [v["Message"] for v in fixture["messages"]]
            assert sum(event.kind == "Lap time deleted" for event in events) == 3
            assert events[1].fields["Lap"] == 53 and "LAP 51" in events[1].message
            # Re-delivery is idempotent through the same replay/controller/UI path.
            ReplayPlayer(recording, speed=0).play(service.controller._enqueue_message)
            window._poll()
            assert service.snapshot.race_control_events == events
            window.connection_status.setText("Archived R17 notices · L52–L55")
            window.session_status.setText("Session: Race")
            window.last_message_status.setText("Last notice: 13:33:42 UTC")
            window.plot_progress_status.clear()
            app.processEvents()
            output.parent.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(output))
            assert window.grab().save(str(evidence_dir / "race_info_archived.png"))
            widget = window.race_control_events
            widget.filter.setCurrentText("Lap time deleted")
            assert widget.table.rowCount() == 3
            app.processEvents()
            assert window.grab().save(str(evidence_dir / "race_info_deleted.png"))
            penalty = {"Utc": "2026-10-11T13:34:00Z", "Lap": 55,
                       "Message": "10 SECOND TIME PENALTY FOR CAR 12 - CAUSING A COLLISION"}
            service.controller._enqueue_message(LiveTimingMessage(
                "RaceControlMessages", {"Messages": {"100": penalty}},
                datetime.fromisoformat("2026-10-11T13:34:00+00:00"),
                datetime.fromisoformat("2026-10-11T13:34:00+00:00"),
                source="synthetic_test", session_time_seconds=100.,
            ))
            window._poll()
            widget.filter.setCurrentText("Penalty")
            assert widget.table.rowCount() == 1
            assert widget.table.item(0, 3).text() == "12"
            window.connection_status.setText("Synthetic penalty acceptance case")
            app.processEvents()
            assert window.grab().save(str(evidence_dir / "race_info_penalty.png"))
            window.tabs.setCurrentIndex(0)
            app.processEvents()
            assert window.track_map.isVisible()
            assert window.relative_plot.isVisible()
            window.tabs.setCurrentWidget(window.race_info_tab)
            window._prepare_start()
            assert widget.table.rowCount() == 0
            evidence = {"archived_events": len(events), "lap_deletions": 3,
                        "duplicate_replay": "unchanged", "filters": "passed",
                        "synthetic_penalty": "passed", "live_tab_return": "passed",
                        "reset_clears_events": True,
                        "screenshots": [str(evidence_dir / name) for name in
                                        ("race_info_archived.png", "race_info_deleted.png", "race_info_penalty.png")]}
            (evidence_dir / "race_info_gui.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps({"events": len(events), "lap_deletions": 3,
                              "duplicate_replay": "unchanged", "screenshot": str(output)}))
        finally:
            window.close()
            app.processEvents()


if __name__ == "__main__":
    main()
