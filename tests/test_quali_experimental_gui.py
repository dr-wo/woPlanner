"""Two-line presentation, same-snapshot integration and unchanged Current label."""

import copy
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets

from woplanner.quali import gui
from woplanner.quali.gui import QualiWindow
from woplanner.quali.models import CutoffProjection, QualiSessionSnapshot


@pytest.fixture
def window():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = QualiWindow()
    yield view
    view.close()
    app.processEvents()


def snapshot(part="Q1", status="LIVE_SUPPORTED", *, manual=False):
    live = -0.04 if status in {"LIVE_SUPPORTED", "LIVE_LIMITED"} else None
    rate = -0.12 if manual else live if live is not None else -0.03
    return QualiSessionSnapshot(
        session_part=part, remaining_time_s=300, session_status="Started", drivers=(),
        cutoff=CutoffProjection("P1" if part == "Q3" else "CUTOFF", 1 if part == "Q3" else 10,
                                "AAA", 90.0, 10, rate, rate * 10, 90.0 + rate * 10),
        actual_cutoffs=(), track_source="MANUAL" if manual else status,
        track_evolution_result=None if status == "UNAVAILABLE" else {
            "status": status, "live_estimate_s_per_global_push": live,
            "configured_fallback_s_per_global_push": -0.03,
        },
    )


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
@pytest.mark.parametrize("status,expected_value,expected_status", [
    ("LIVE_SUPPORTED", "1:29.800", "Live supported"),
    ("LIVE_LIMITED", "1:29.800", "Live limited"),
    ("FALLBACK", "1:29.700", "Fallback · FP/default prior"),
    ("UNAVAILABLE", "—", "Unavailable"),
])
def test_prediction_labels_preserve_current_and_explain_experimental(
    window, part, status, expected_value, expected_status,
):
    state = snapshot(part, status)
    before = copy.deepcopy(state)
    window._render_cutoff_predictions(state)
    target = "Projected P1" if part == "Q3" else "Projected cutoff"
    # Literal accepted Current output: no new mathematics or source selection.
    current_value = "1:29.600" if status.startswith("LIVE") else "1:29.700"
    assert window.cutoff.text() == f"{target}: {current_value}"
    assert window.current_prediction_label.text() == "Current (legacy projection)"
    assert window.experimental_cutoff.text() == (
        f"R&D · Half-live [Experimental] · {target}: {expected_value} · {expected_status}"
    )
    assert "Candidate: half_live" in window.experimental_cutoff.toolTip()
    assert "Independent of manual overrides" in window.experimental_cutoff.toolTip()
    assert state == before


@pytest.mark.parametrize("status,expected_value", [
    ("LIVE_SUPPORTED", "1:29.800"), ("LIVE_LIMITED", "1:29.800"),
    ("FALLBACK", "1:29.700"),
])
def test_manual_current_and_automatic_experiment_remain_distinct(window, status, expected_value):
    window._render_cutoff_predictions(snapshot(status=status, manual=True))
    assert window.cutoff.text() == "Projected cutoff: 1:28.800"
    assert expected_value in window.experimental_cutoff.text()
    assert "manual override" in window.experimental_cutoff.toolTip().lower()


def test_missing_target_never_leaves_a_stale_experimental_value(window):
    state = snapshot()
    window._render_cutoff_predictions(state)
    missing = replace(state, cutoff=replace(state.cutoff, current_cutoff_s=None))
    window._render_cutoff_predictions(missing)
    assert window.cutoff.text() == "Projected cutoff: —"
    assert window.experimental_cutoff.text().endswith("Projected cutoff: — · Unavailable")
    assert "No observed cutoff target" in window.experimental_cutoff.toolTip()


def test_poll_renders_both_predictions_from_exactly_one_published_snapshot(window, monkeypatch):
    state = snapshot()
    calls = []
    calculate = gui.calculate_half_live

    def record(value):
        calls.append(value)
        return calculate(value)

    monkeypatch.setattr(gui, "calculate_half_live", record)
    monkeypatch.setattr(window.service, "poll", lambda: (state, (), None))
    monkeypatch.setattr(window, "_render_pit_status", lambda: None)
    window._poll()
    assert len(calls) == 1 and calls[0] is state
    assert window.cutoff.text() == "Projected cutoff: 1:29.600"
    assert "1:29.800" in window.experimental_cutoff.text()


def test_part_transitions_and_session_restart_clear_experimental_state(window, monkeypatch):
    for part in ("Q1", "Q2", "Q3", "Q1"):
        window._render_cutoff_predictions(snapshot(part))
        target = "Projected P1" if part == "Q3" else "Projected cutoff"
        assert target in window.cutoff.text()
        assert target in window.experimental_cutoff.text()
    window._prepare_session_start()
    assert window.experimental_cutoff.text().endswith("Unavailable")
    assert "new qualifying session" in window.experimental_cutoff.toolTip()
    window._render_cutoff_predictions(snapshot())
    monkeypatch.setattr(window, "_show_non_quali_warning", lambda: None)
    window._reject_non_quali_session()
    assert window.experimental_cutoff.text().endswith("Unavailable")
    assert "not Qualifying" in window.experimental_cutoff.toolTip()


@pytest.mark.parametrize("action", ["restart", "reject"])
def test_new_or_rejected_session_clears_previous_current_clock_and_finish(window, monkeypatch, action):
    state = replace(snapshot("Q3"), normal_finished=True, normal_finish_source="race_control")
    window._render_cutoff_predictions(state)
    window._render_normal_finish(state)
    window.clock.setText("Q3 0:00")
    window._presentation_countdown.update(state)
    window.timer.start()
    monkeypatch.setattr(window, "_show_non_quali_warning", lambda: None)
    if action == "restart":
        window._prepare_session_start()
    else:
        window._reject_non_quali_session()
    assert window.cutoff.text() == "Projected cutoff: —"
    assert window.clock.text() == "—:—"
    assert window.normal_finish.text() == ""
    assert window.normal_finish.toolTip() == ""
    assert window.experimental_cutoff.text().endswith("Unavailable")
    assert not window.timer.isActive()


@pytest.mark.parametrize("failure_stage", ["configure_event_geometry", "start_replay"])
def test_failed_replay_restart_cannot_repaint_previous_prediction(window, monkeypatch, tmp_path, failure_stage):
    path = tmp_path / "replay.jsonl"
    path.write_text("")
    window.replay_path.setText(str(path))
    window._render_cutoff_predictions(snapshot("Q3"))
    window.clock.setText("Q3 1:00")
    window.timer.start()
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(window.service, "configure_event_geometry", lambda *args: None)
    def fail(*args):
        raise RuntimeError("Replay startup failed")
    monkeypatch.setattr(window.service, failure_stage, fail)
    window._start_replay()
    QtWidgets.QApplication.processEvents()
    assert errors == ["Replay startup failed"]
    assert window.cutoff.text() == "Projected cutoff: —"
    assert window.clock.text() == "—:—"
    assert window.experimental_cutoff.text().endswith("Unavailable")
    assert not window.timer.isActive()


@pytest.mark.parametrize("width", [1500, 1100])
@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
def test_two_lines_fit_above_table_at_wide_and_minimum_window_sizes(window, width, part):
    window._render_cutoff_predictions(snapshot(part, "FALLBACK"))
    window.resize(width, 750)
    window.show()
    QtWidgets.QApplication.processEvents()
    current = window.cutoff.geometry()
    experimental = window.experimental_cutoff.geometry()
    table = window.table.geometry()
    assert experimental.top() > current.bottom()
    assert experimental.bottom() < table.top()
    assert window.centralWidget().rect().contains(experimental)
    assert experimental.height() >= window.experimental_cutoff.heightForWidth(experimental.width())
    assert current.width() >= window.cutoff.sizeHint().width()
    assert window.experimental_cutoff.textFormat() == QtCore.Qt.TextFormat.PlainText
    if width == 1500:
        metrics = window.experimental_cutoff.fontMetrics()
        assert experimental.width() >= metrics.horizontalAdvance(window.experimental_cutoff.text())
        assert window.experimental_cutoff.heightForWidth(experimental.width()) <= metrics.height() + 2
