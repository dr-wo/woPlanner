"""Elimination markers are rendered rank boundaries, independent of forecasts."""

from dataclasses import replace
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets
import pytest

from woplanner.quali.gui import QualiWindow
from woplanner.quali.models import QualiDriverState
from woplanner.quali.widgets import CutoffTable, format_time


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    window = QualiWindow()
    window.timer.stop()
    yield window
    window.close()
    app.processEvents()


def snapshot(window, part, count, *, threshold_row=None, numeric=None):
    states = tuple(QualiDriverState(
        str(index), f"D{index:02}", "Team", rank=index, official_position=index,
        best_lap_s=90 + index / 100 if numeric is not None else None,
    ) for index in range(1, count + 1))
    cutoff = replace(
        window.service.snapshot.cutoff,
        target_kind="P1" if part.endswith("3") else "CUTOFF",
        # Deliberately unrelated calculated rank: partial fields must still
        # use the part's fixed presentation boundary.
        cutoff_rank=1,
        driver=states[threshold_row].driver if threshold_row is not None else None,
        current_cutoff_s=numeric,
        projected_cutoff_s=numeric - .25 if numeric is not None else None,
    )
    return replace(window.service.snapshot, session_part=part, drivers=states, cutoff=cutoff)


@pytest.mark.parametrize("part,count,row", [("Q1", 22, 15), ("Q2", 16, 9)])
@pytest.mark.parametrize("threshold_row", [0, 3, 5, -1, None])
@pytest.mark.parametrize("numeric", [None, 94.123])
def test_fixed_boundary_ignores_threshold_driver_and_numeric_support(
        window, part, count, row, threshold_row, numeric):
    state = snapshot(window, part, count, threshold_row=threshold_row, numeric=numeric)
    window.service.snapshot = state
    window._poll()
    assert window.table.cutoff_row() == row
    assert window.table.rowCount() == count
    expected = "—" if numeric is None else format_time(numeric - .25)
    assert window.cutoff.text() == f"Projected cutoff: {expected}"
    assert window.service.snapshot is state


@pytest.mark.parametrize("part,count,row", [
    ("Q1", 0, None), ("Q1", 15, None), ("Q1", 16, 15),
    ("Q2", 9, None), ("Q2", 10, 9), ("Q2", 13, 9),
    ("Q3", 22, None), ("SQ1", 22, 15), ("SQ2", 16, 9), ("SQ3", 10, None),
])
def test_missing_boundary_row_is_hidden_without_inventing_participants(window, part, count, row):
    window._render_timing_table(snapshot(window, part, count))
    assert window.table.rowCount() == count
    assert window.table.cutoff_row() == row


def test_official_reorder_and_incomplete_positions_keep_fixed_boundary_and_selection(window):
    state = snapshot(window, "Q2", 16, threshold_row=0)
    window._render_timing_table(state)
    window.table.setCurrentCell(0, 1)
    selected = window.table.currentItem().text()
    reordered = tuple(replace(d, official_position=None if index % 2 else 40 - index)
                      for index, d in enumerate(reversed(state.drivers)))
    window._render_timing_table(replace(state, drivers=reordered))
    assert window.table.cutoff_row() == 9
    assert window.table.currentItem().text() == selected
    assert [window.table.item(row, 1).text() for row in range(16)] == [d.driver for d in reordered]
    assert window.table.item(9, 0).text() == "—"


def test_q3_keeps_projected_p1_label_and_driver_rows_without_elimination_line(window):
    state = snapshot(window, "Q3", 10, threshold_row=0, numeric=94.123)
    window.service.snapshot = state
    window._poll()
    assert window.table.cutoff_row() is None
    assert window.table.rowCount() == 10
    assert window.cutoff.text() == f"Projected P1: {format_time(93.873)}"
    assert window.table.item(0, 1).text() == state.drivers[0].driver


@pytest.mark.parametrize("rank,count", [(16, 22), (10, 16), (0, 10)])
def test_offscreen_paint_has_exactly_one_line_at_boundary_or_no_q3_line(app, rank, count):
    table = CutoffTable()
    table.setColumnCount(1)
    table.setRowCount(count)
    table.cutoff_rank = rank
    for row in range(count):
        table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(row + 1)))
        table.setRowHeight(row, 24)
    table.resize(500, 750)
    table.show()
    app.processEvents()
    pixmap = table.viewport().grab()
    image = pixmap.toImage()
    red_rows = [y for y in range(image.height())
                if any(image.pixelColor(x, y).name() == "#d00000" for x in (2, 8, 20, 32))]
    if rank:
        expected = (table.visualItemRect(table.item(rank - 1, 0)).bottom() + 1) * pixmap.devicePixelRatio()
        assert red_rows
        assert all(abs(y - expected) <= 2 * pixmap.devicePixelRatio() for y in red_rows)
        assert max(red_rows) - min(red_rows) <= 3 * pixmap.devicePixelRatio()
    else:
        assert red_rows == []
    table.close()
    app.processEvents()


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


@pytest.mark.skipif(not RECORDING.exists(), reason="R16 live recording unavailable")
def test_r16_all_rendered_boundaries_are_fixed_and_q3_has_none():
    spec = importlib.util.spec_from_file_location("red_line_replay", ROOT / "scripts/verify_quali_r16_red_line.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    result = replay.verify(RECORDING)
    assert result["message_count"] == 16496
    assert {part for _, part, _, _ in result["markers"]} == {"Q1", "Q2", "Q3"}
    for _, part, row, count in result["markers"]:
        rank = {"Q1": 16, "Q2": 10}.get(part, 0)
        assert row == (rank - 1 if rank and count >= rank else None)
    q2 = {c["sequence"]: c for c in result["checkpoints"] if c["part"] == "Q2"}
    for sequence in (8988, 9128, 9183, 9466, 11700):
        assert q2[sequence]["rendered_boundary_after_rank"] == 10
    # At 12465 TimingData explicitly switches to Q3; the old clock-reset
    # inference left this packet incorrectly scoped to Q2.
    q3_start = next(c for c in result["checkpoints"] if c["sequence"] == 12465)
    assert q3_start["part"] == "Q3"
    assert q3_start["rendered_boundary_after_rank"] is None
    assert q2[8988]["current_cutoff_s"] is None
    assert q2[9466]["current_cutoff_s"] is not None
    assert q3_start["current_cutoff_s"] is None
    assert dict(result["actual_cutoffs"]) == {"Q1": pytest.approx(97.883), "Q2": pytest.approx(96.814)}
