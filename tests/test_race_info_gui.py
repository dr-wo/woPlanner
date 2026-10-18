import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets
from wodata.live import LiveConfig, LiveTimingMessage, ReplayConfig
from wodata.live.recorder import LiveTimingRecorder
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerService


FIXTURE = Path(__file__).parent / "fixtures/r17_race_control_l52_l55.json"


def make_message(index, payload):
    return LiveTimingMessage("RaceControlMessages", {"Messages": {str(index): payload}},
                             None, datetime(2030, 1, 1, tzinfo=timezone.utc),
                             sequence=index + 1, session_time_seconds=float(index))


def test_race_info_live_updates_filters_and_pit_controls(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({"season": 2026, "round_number": 17, "race": {"total_laps": 62},
                                  "pit_loss": {"green": {"total": 29.0}, "sc": {"total": 15.0},
                                               "vsc": {"total": 20.0}}}))
    service = LivePlannerService()
    window = LivePlannerWindow(service=service, event_config_path=config)
    redraws = []
    monkeypatch.setattr(window, "_redraw", lambda: redraws.append(True))
    try:
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Live", "Race Info"]
        assert window.race_info_tab.layout().itemAt(0).widget().title() == "Race Control Events"
        assert window.race_info_tab.layout().itemAt(1).widget().title() == "Pit Delta"
        assert window.pit_delta_section.isAncestorOf(window.normal_pit_loss)
        assert [window._pit_loss_value(field) for field in (
            window.normal_pit_loss, window.sc_pit_loss, window.vsc_pit_loss)] == [29.0, 15.0, 20.0]
        for index, payload in enumerate(json.loads(FIXTURE.read_text())["messages"]):
            service.controller._enqueue_message(make_message(index, payload))
        # Fixture has original R17 L52–L55 notices. A synthetic penalty tests
        # support absent from this race's captured source messages.
        penalty = {"Utc": "2026-10-11T13:34:00Z", "Lap": 55,
                   "Message": "10 SECOND TIME PENALTY FOR CAR 12 - CAUSING A COLLISION",
                   "Category": "Other", "Status": "Issued"}
        service.controller._enqueue_message(make_message(100, penalty))
        window._poll()
        assert redraws == []
        widget = window.race_control_events
        assert widget.table.rowCount() == 9
        widget.filter.setCurrentText("Lap time deleted")
        assert widget.table.rowCount() == 3
        assert widget.table.item(0, 1).text() == "53"
        assert "LAP 51" in widget.table.item(0, 5).text()
        widget.filter.setCurrentText("Penalty")
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 3).text() == "12"
        assert widget.table.item(0, 4).text() == "Status: Issued"
        # Duplicate delivery stays a single row; a partial correction updates it.
        service.controller._enqueue_message(make_message(100, penalty))
        service.controller._enqueue_message(make_message(100, {"Status": "Served"}))
        window._poll()
        assert widget.table.rowCount() == 1
        assert widget.table.item(0, 4).text() == "Status: Served"
        window.normal_pit_loss.setValue(31.2)
        assert next(item.value_s for item in window.track_map._windows if item.key == "pit") == 31.2
        window._prepare_start()
        assert widget.table.rowCount() == 0
    finally:
        window.close()
        app.processEvents()


def test_r17_recorded_replay_matches_live_race_control_history(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    records = [make_message(i, payload) for i, payload in enumerate(json.loads(FIXTURE.read_text())["messages"])]
    path = tmp_path / "race_control.jsonl"
    recorder = LiveTimingRecorder(path)
    for message in records:
        recorder.write(message)
    recorder.close()
    service = LivePlannerService()
    window = LivePlannerWindow(service=service, event_config_path=tmp_path / "absent.json")
    try:
        service.start_replay(ReplayConfig(path, speed=0))
        deadline = time.monotonic() + 5
        while service.controller.is_running and time.monotonic() < deadline:
            window._poll()
            app.processEvents()
            time.sleep(.01)
        window._poll()
        assert not service.controller.is_running
        assert len(service.snapshot.race_control_events) == len(records) == 8
        assert [event.message for event in service.snapshot.race_control_events] == [
            record.payload["Messages"][str(i)]["Message"] for i, record in enumerate(records)]
        assert window.race_control_events.table.rowCount() == 8
    finally:
        window.close()
        app.processEvents()


def test_recovered_notices_render_without_a_fresh_feed_message(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    recorder = LiveTimingRecorder(tmp_path / "live_timing_raw.jsonl")
    recorder.write(LiveTimingMessage(
        "SessionInfo", {"Name": "Race", "StartDate": "2026-10-11",
                        "Meeting": {"Number": 17}},
        None, datetime(2026, 10, 11, tzinfo=timezone.utc),
    ))
    notice = json.loads(FIXTURE.read_text())["messages"][1]
    recorder.write(make_message(1, notice))
    recorder.close()
    service = LivePlannerService()
    window = LivePlannerWindow(service=service, event_config_path=tmp_path / "absent.json")
    try:
        window._prepare_start()
        service.controller._recover_live_history(LiveConfig(2026, 17, "R"), tmp_path)
        assert not service.controller.recovery_report.rejected_paths
        assert len(service.controller.store.snapshot().race_control_events) == 1
        assert service.controller.drain().changes == frozenset()
        window._poll()
        assert window.race_control_events.table.rowCount() == 1
        assert window.race_control_events.table.item(0, 5).text() == notice["Message"]
        # Polling unchanged recovered state does not rebuild the table.
        renders = []
        monkeypatch.setattr(window.race_control_events, "_render", lambda: renders.append(True))
        window._poll()
        assert renders == []
    finally:
        window.close()
        app.processEvents()


def test_returning_to_live_refreshes_map_after_hidden_poll(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow(event_config_path=tmp_path / "absent.json")
    rendered = []
    monkeypatch.setattr(window.track_map, "update_snapshot", lambda snapshot, drivers: rendered.append(snapshot))
    try:
        window.show()
        window.tabs.setCurrentWidget(window.race_info_tab)
        app.processEvents()
        assert not window.track_map.isVisible()
        window._poll()
        assert rendered == []
        latest = window.service.latest_track_map_snapshot()
        # No new feed/poll is necessary after a paused replay or disconnect.
        window.tabs.setCurrentIndex(0)
        app.processEvents()
        assert rendered == [latest]
        assert window.track_map.isVisible()
        window.track_map_collapse_button.setChecked(False)
        rendered.clear()
        window.tabs.setCurrentWidget(window.race_info_tab)
        window.tabs.setCurrentIndex(0)
        assert not window.track_map.isVisible()
        assert rendered == []
    finally:
        window.close()
        app.processEvents()
