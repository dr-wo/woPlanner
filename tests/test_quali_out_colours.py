"""Q17-04: rendered colours change without rewriting qualifying state."""
import copy
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest
from PySide6 import QtGui, QtWidgets

from wodata.live import QualiPitState, QualiRawSnapshot, QualiTyreStint
from woplanner.quali.gui import QualiWindow
from woplanner.quali.models import FinishedReason, QualiDriverState
from woplanner.quali.planning import (
    DEFAULT_SESSION_DURATIONS_S, QualiArrangement, QualiRunPlan, QualiTimingValues,
    calculate_arrangement_timing,
)
from woplanner.quali.service import QualiLiveService
from woplanner.quali.widgets import QualiPlanDialog, ROLE_COLOURS, TimelineWidget


@pytest.fixture(params=["light", "dark"])
def app(request):
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    previous = application.palette()
    palette = QtGui.QPalette(previous)
    dark = request.param == "dark"
    for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base):
        palette.setColor(role, QtGui.QColor("#222222" if dark else "white"))
    for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text):
        palette.setColor(role, QtGui.QColor("white" if dark else "black"))
    application.setPalette(palette)
    yield application
    application.setPalette(previous)
    application.processEvents()


def save_evidence(widget, name):
    # Optional external output only; normal test runs do not leave screenshots.
    output = os.environ.get("Q17_04_EVIDENCE_DIR")
    if output:
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(path / f"{name}.png"))


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
def test_monitor_out_uses_slow_amber_without_changing_state(app, part):
    expected = {
        "OUT": "#b26a00", "FAST": "#2e7d32", "SLOW": "#b26a00",
        "IN": "#1976d2", "IN-PIT": "#ffffff", "FIRE-UP": "#f0ad00",
        "PIT-LANE": "#607d8b", "QUEUE": "#d00000", "NOT ACTIVE": "#e0e0e0",
    }
    states = tuple(
        QualiDriverState(
            str(i), f"D{i}", "Ferrari", rank=i, official_position=i,
            status=status, lap_state="OUT" if status == "OUT" else "UNKNOWN",
            pit_state=QualiPitState.ON_TRACK,
            sectors_s=(30., 31., 32.), last_lap_s=93.123, best_lap_s=92.987,
            tyre_history=(QualiTyreStint("SOFT", 0, True, 2),),
            finished=status == "IN", finished_reason=(
                FinishedReason.FINAL_LAP_COMPLETED if status == "IN" else None
            ),
        ) for i, status in enumerate(expected, 1)
    )
    service = QualiLiveService()
    snapshot = replace(service.consume(replace(QualiRawSnapshot.empty(), session_part=part)),
                       drivers=states)
    service.snapshot = snapshot
    before = copy.deepcopy(snapshot)
    window = QualiWindow()
    window.service = service
    try:
        window.resize(1500, 800)
        window._render_timing_table(snapshot)
        window.clock.setText(f"{part} —:—")
        window.show()
        app.processEvents()
        image = window.table.viewport().grab().toImage()
        for row, (status, colour) in enumerate(expected.items()):
            cell = window.table.item(row, 4)
            assert cell.text() == status
            assert cell.background().color().name() == colour
            assert cell.foreground().color().name() == (
                "#000000" if status in {"IN-PIT", "NOT ACTIVE", "FIRE-UP"} else "#ffffff"
            )
            rect = window.table.visualItemRect(cell)
            assert image.pixelColor(rect.left() + 4, rect.top() + 4).name() == colour
            assert window.table.item(row, 1).text() == states[row].driver
            assert window.table.item(row, 8).text() == "1:33.123"
            assert window.table.cellWidget(row, 3) is not None
        assert "🏁" in window.table.item(3, 0).text()
        assert service.snapshot == before
        assert service.snapshot.drivers[0].lap_state == "OUT"
        assert service.snapshot.drivers[0].status == "OUT"
        if app.palette().color(QtGui.QPalette.ColorRole.Window).name() == "#ffffff":
            save_evidence(window, f"monitor-{part}-offscreen")
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
def test_timeline_out_matches_prep_cool_and_preserves_geometry(app, part, monkeypatch):
    arrangement = QualiArrangement(runs=(QualiRunPlan(True, 2), QualiRunPlan()))
    timing = calculate_arrangement_timing(
        arrangement, QualiTimingValues(cool_lap_s=110.), DEFAULT_SESSION_DURATIONS_S[part]
    )
    before = copy.deepcopy(timing)
    widget = TimelineWidget()
    try:
        widget.resize(1800, 120)
        widget.set_timing(timing, DEFAULT_SESSION_DURATIONS_S[part], arrangement.margin_s)
        widget.show()
        app.processEvents()
        image = widget.grab().toImage()
        minimum = min(0., timing.latest_start_s)
        maximum = max(DEFAULT_SESSION_DURATIONS_S[part], max(s.end_s for s in timing.segments))
        scale = (widget.width() - 70.) / (maximum - minimum)
        expected = {"OUT": "#ffe69a", "PREP": "#ffe69a", "COOL": "#ffe69a",
                    "PUSH": "#a7e8a7", "IN": "#c7b3ff", "TURNAROUND": "#f4b183"}
        for segment in timing.segments:
            if segment.label == "PIT EXIT":
                continue
            x = int(55. + ((segment.start_s + segment.end_s) / 2. - minimum) * scale)
            # Above the text baseline, away from borders and timing markers.
            assert image.pixelColor(x, 36).name() == expected[segment.label]
        with monkeypatch.context() as patch:
            patch.setitem(ROLE_COLOURS, "OUT", "#b3d9ff")
            baseline = widget.grab().toImage()
        # Every pixel outside OUT rectangles retains the previous presentation.
        for x in range(image.width()):
            in_out = any(
                55. + (s.start_s - minimum) * scale - 2 <= x
                <= 55. + (s.end_s - minimum) * scale + 2
                for s in timing.segments if s.label == "OUT"
            )
            if not in_out:
                for y in range(image.height()):
                    assert image.pixelColor(x, y) == baseline.pixelColor(x, y)
        assert widget.timing == before
        assert timing == calculate_arrangement_timing(
            arrangement, QualiTimingValues(cool_lap_s=110.), DEFAULT_SESSION_DURATIONS_S[part]
        )
        if app.palette().color(QtGui.QPalette.ColorRole.Window).name() == "#ffffff":
            save_evidence(widget, f"timeline-{part}-offscreen")
    finally:
        widget.close()
        app.processEvents()


def test_debug_legend_and_cells_share_the_timeline_palette(app):
    analysis = SimpleNamespace(result=None, effective_fp_track_rate=lambda: (0., "Fallback", 0))
    dialog = QualiPlanDialog(analysis, QualiLiveService())
    try:
        labels = {label.text(): label for label in dialog.findChildren(QtWidgets.QLabel)
                  if label.text() in {"OUT", "COOL/PREP", "PUSH", "IN", "OTHER/INVALID"}}
        for role, colour in {"OUT": "#ffe69a", "COOL/PREP": "#ffe69a", "PUSH": "#a7e8a7",
                             "IN": "#c7b3ff", "OTHER/INVALID": "#dddddd"}.items():
            label = labels[role]
            label.ensurePolished()
            assert label.palette().color(QtGui.QPalette.ColorRole.Window).name() == colour
        laps = pd.DataFrame([
            {"session": "FP1", "driver": "D1", "lap_number": i,
             "raw_lap_time_s": 100., "lap_role": role, "run_id": 1,
             "is_valid_run": valid, "invalid_reason": "" if valid else "Invalid"}
            for i, (role, valid) in enumerate([
                ("OUT", True), ("PREP", True), ("COOL", True),
                ("PUSH", True), ("IN", True), ("OUT", False),
            ], 1)
        ])
        before = laps.copy(deep=True)
        dialog.result = SimpleNamespace(laps=laps)
        dialog._refresh_debug()
        for row, colour in enumerate([
            "#ffe69a", "#ffe69a", "#ffe69a", "#a7e8a7", "#c7b3ff", "#dddddd",
        ]):
            assert dialog.debug_table.item(row, 0).background().color().name() == colour
        pd.testing.assert_frame_equal(laps, before)
    finally:
        dialog.close()
        app.processEvents()
