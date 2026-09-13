import os
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from wodata.live import QualiPitState, QualiRawSnapshot, QualiTyreStint
from woplanner.quali.gui import PIT_STATUS_BACKGROUNDS, PresentationCountdown, QualiWindow, format_countdown
from woplanner.quali.models import FinishedReason, QualiDriverState
from woplanner.quali.service import QualiLiveService


def test_quali_window_updates_round_from_received_session_info():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()

    window._sync_session_context(
        {
            "Meeting": {"Name": "British Grand Prix", "Number": 12},
            "Name": "Qualifying",
            "StartDate": "2026-07-04T15:00:00Z",
        }
    )

    assert window.year.value() == 2026
    assert window.round.value() == 12
    assert "British Grand Prix" in window.windowTitle()
    window.close()
    app.processEvents()


def test_presentation_countdown_advances_only_its_local_display_state():
    clock = PresentationCountdown()
    service = QualiLiveService()
    raw = replace(
        QualiRawSnapshot.empty(),
        session_part="Q2", remaining_time_s=125.0, clock_running=True,
        clock_anchor_session_time_s=1000.0, normal_finished=False, session_status="Started",
    )
    live = service.consume(raw)
    assert live.clock_running is True
    assert live.clock_anchor_session_time_s == 1000.0
    assert clock.update(live, now_s=10.0) == 125.0
    assert clock.update(live, now_s=12.9) == 122.1
    assert live.remaining_time_s == 125.0
    assert format_countdown(clock.update(live, now_s=13.0)) == "2:02"

    paused = service.consume(replace(raw, clock_running=False))
    assert clock.update(paused, now_s=14.0) == 125.0
    assert clock.update(paused, now_s=19.0) == 125.0

    finished = service.consume(replace(raw, normal_finished=True))
    assert clock.update(finished, now_s=20.0) == 125.0
    assert clock.update(finished, now_s=25.0) == 125.0


def test_quali_window_labels_sprint_qualifying_parts():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()

    window._sync_session_context(
        {
            "Meeting": {"Name": "British Grand Prix", "Number": 12},
            "Name": "Sprint Qualifying",
            "StartDate": "2026-07-03T15:00:00Z",
        }
    )

    assert [label.text() for label in window.elimination_labels.values()] == [
        "SQ1 eliminated",
        "SQ2 eliminated",
        "SQ3 eliminated",
    ]
    window.close()
    app.processEvents()


def test_quali_window_warns_and_clears_tables_for_race_session(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    warnings = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args: warnings.append((args[1], args[2])),
    )
    window.service.session_is_qualifying = False
    window.service.received_session_name = "Race"
    window.table.setRowCount(1)
    window._prepare_session_start()

    window._reject_non_quali_session()

    assert window.table.rowCount() == 0
    assert window.connection.text() == "Not a qualifying session"
    assert window.analyze.isEnabled() is False
    assert window.plan.isEnabled() is False
    assert warnings == [
        (
            "Not a qualifying session",
            "Race is not Qualifying. No qualifying results were loaded.",
        )
    ]
    window.close()
    app.processEvents()


def test_status_column_colours_operational_states():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    states = tuple(
        QualiDriverState(
            str(index),
            label,
            "Team",
            status=label,
            pit_state=pit_state,
        )
        for index, (label, pit_state) in enumerate(
            (
                ("UNKNOWN", QualiPitState.UNKNOWN),
                ("FIRE-UP", QualiPitState.FIRE_UP),
                ("QUEUE", QualiPitState.QUEUE),
                ("FAST", QualiPitState.ON_TRACK),
                ("IN", QualiPitState.IN_LAP),
                ("IN-PIT", QualiPitState.IN_PIT),
            ),
            start=1,
        )
    )
    window._render_timing_table(
        SimpleNamespace(drivers=states, cutoff=SimpleNamespace(cutoff_rank=0))
    )

    assert window.table.item(0, 4).background().style() == QtCore.Qt.BrushStyle.NoBrush
    assert window.table.item(1, 4).background().color().name() == (
        PIT_STATUS_BACKGROUNDS["FIRE-UP"]
    )
    assert window.table.item(2, 4).background().color().name() == (
        PIT_STATUS_BACKGROUNDS["QUEUE"]
    )
    assert window.table.item(3, 4).background().color().name() == (
        PIT_STATUS_BACKGROUNDS["FAST"]
    )
    assert window.table.item(4, 4).background().color().name() == (
        PIT_STATUS_BACKGROUNDS["IN"]
    )
    assert window.table.item(5, 4).background().color().name() == (
        PIT_STATUS_BACKGROUNDS["IN-PIT"]
    )
    assert window.table.item(5, 4).foreground().color().name() == "#000000"
    window.close()
    app.processEvents()


def test_eliminated_table_row_keeps_known_identity_and_unknown_falls_back_safely():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    states = (
        QualiDriverState("15", "AAA", "Ferrari", active=False,
                         finished_reason=FinishedReason.ELIMINATED, frozen_part="Q1"),
        QualiDriverState("17", "17", "", active=False,
                         finished_reason=FinishedReason.ELIMINATED, frozen_part="Q2"),
    )
    window._render_timing_table(
        SimpleNamespace(drivers=states, cutoff=SimpleNamespace(cutoff_rank=0))
    )
    assert (window.table.item(0, 1).text(), window.table.item(0, 2).text()) == (
        "AAA", "Ferrari",
    )
    assert window.table.item(0, 1).background().color().isValid()
    assert (window.table.item(1, 1).text(), window.table.item(1, 2).text()) == (
        "17", "",
    )
    window.close()
    app.processEvents()


def test_effective_finish_has_distinct_colour_and_explanation():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    states = (
        QualiDriverState(
            "1",
            "Final push",
            "Team",
            rank=1,
            finished=True,
            finished_reason=FinishedReason.NO_TIME_FOR_NEXT_PUSH,
            effective_finished=True,
            effective_finish_remaining_s=80,
            effective_finish_recovery_s=105,
            effective_finish_timing_source="test policy",
        ),
        QualiDriverState(
            "2",
            "In pit",
            "Team",
            rank=2,
            finished=True,
            finished_reason=FinishedReason.IN_PIT_NO_OUTLAP_TIME,
        ),
    )
    window._render_timing_table(
        SimpleNamespace(drivers=states, cutoff=SimpleNamespace(cutoff_rank=0))
    )

    assert window.table.item(0, 0).background().color().name() == "#f4b183"
    assert "Not the session chequered flag" in window.table.item(0, 0).toolTip()
    assert window.table.item(1, 0).background().style() == (
        QtCore.Qt.BrushStyle.NoBrush
    )
    window.close()
    app.processEvents()


def test_tyre_history_uses_compound_circles_with_age_at_stint_start():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    widget = window._tyre_history_widget(
        (
            QualiTyreStint("SOFT", 0, True),
            QualiTyreStint("MEDIUM", 4, False),
        )
    )

    first = widget.layout().itemAt(0).widget()
    second = widget.layout().itemAt(1).widget()
    assert (first.text(), second.text()) == ("0", "4")
    assert first.toolTip() == "SOFT tyre age at stint start: 0 laps"
    assert second.toolTip() == "MEDIUM tyre age at stint start: 4 laps"
    widget.close()
    window.close()
    app.processEvents()


def test_tyre_age_column_is_hidden_until_real_age_data_is_available():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    without_age = QualiDriverState("1", "AAA", "Team")
    window._render_timing_table(
        SimpleNamespace(drivers=(without_age,), cutoff=SimpleNamespace(cutoff_rank=0))
    )
    assert window.table.isColumnHidden(3) is True

    with_age = QualiDriverState(
        "1", "AAA", "Team", tyre_history=(QualiTyreStint("SOFT", 4, True),)
    )
    window._render_timing_table(
        SimpleNamespace(drivers=(with_age,), cutoff=SimpleNamespace(cutoff_rank=0))
    )
    assert window.table.isColumnHidden(3) is False
    window.close()
    app.processEvents()


def test_normal_finish_is_a_session_label_not_a_car_marker():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    window._render_normal_finish(SimpleNamespace(normal_finished=True, normal_finish_source="race_control"))
    assert window.normal_finish.text() == "SESSION CHEQUERED"
    window._render_normal_finish(SimpleNamespace(normal_finished=False, normal_finish_source=None))
    assert window.normal_finish.text() == ""
    window.close()
