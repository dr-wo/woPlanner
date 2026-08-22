import os

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from woplanner.live.service import REFERENCE_MODE_AVERAGED_PACE
from woplanner.live.gui import LivePlannerWindow
from woplanner.common.plot_widget import (
    RelativeDeltaPlotWidget,
    format_live_hover_text,
    format_relative_delta_hover_text,
    nearest_relative_delta_row,
)
from woplanner.common.axis_controls import AxisRangeControls


def test_live_window_constructs_and_disconnects_before_connect():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    assert window.reference_mode.currentData() == REFERENCE_MODE_AVERAGED_PACE
    assert window.strategy_prediction_button.text() == "Strategy Prediction"
    window._disconnect()
    assert "Disconnected" in window.connection_status.text()
    window.close()
    app.processEvents()


def test_replay_browser_accepts_jsonl(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    selected_filter = []

    def choose_file(*args):
        selected_filter.append(args[3])
        return "/tmp/merged_replay_v1.jsonl", "JSONL replay (*.jsonl)"

    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", choose_file)
    window._browse_replay()

    assert window.replay_path.text() == "/tmp/merged_replay_v1.jsonl"
    assert "*.jsonl" in selected_filter[0]
    window.close()
    app.processEvents()


def test_replay_strategy_state_is_kept_beside_replay(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    replay = tmp_path / "replay" / "merged_replay_v1.jsonl"
    window.replay_path.setText(str(replay))
    window._active_source_mode = "replay"

    assert window._strategy_state_directory() == replay.parent

    window.close()
    app.processEvents()


def test_live_hover_text_includes_driver_sector_delta_and_tyre():
    text = format_live_hover_text(
        pd.Series(
            {
                "driver": "AAA",
                "lap": 12,
                "sector": "S2",
                "delta_s": 1.2345,
                "compound": "HARD",
                "tyre_age": 6,
            }
        )
    )

    assert text.splitlines() == [
        "AAA  Lap 12 S2",
        "Delta +1.234 s",
        "HARD  tyre age 5",
    ]


def test_live_rank_lists_render_compound_tyre_age_badges():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = RelativeDeltaPlotWidget()
    ranks = pd.DataFrame(
        [
            {
                "rank": 1,
                "driver": "AAA",
                "colour": "#123456",
                "start_tyre_compound": "MEDIUM",
                "start_tyre_age_label": "0",
                "latest_tyre_compound": "HARD",
                "latest_tyre_age_label": "12",
            }
        ]
    )

    widget.set_rank_data(ranks, ranks)

    start_item = widget.start_rank_list.item(0)
    latest_item = widget.latest_rank_list.item(0)
    assert not start_item.icon().isNull()
    assert start_item.toolTip() == "MEDIUM, tyre age 0"
    assert not latest_item.icon().isNull()
    assert latest_item.toolTip() == "HARD, tyre age 12"
    widget.close()
    app.processEvents()


def test_shared_hover_selects_nearest_simulation_row_and_formats_full_context():
    rows = pd.DataFrame(
        [
            {
                "driver": "AAA", "lap": 4, "sector": "S2", "segment_index": 11,
                "delta_s": 1.0, "compound": "MEDIUM", "tyre_age": 5,
                "plot_role": "actual",
            },
            {
                "driver": "AAA", "lap": 4, "sector": "S2", "segment_index": 11,
                "delta_s": 4.0, "compound": "HARD", "tyre_age": 2,
                "plot_role": "simulation",
            },
        ]
    )

    row = nearest_relative_delta_row(
        rows, 11.0, 3.9, x_span=20.0, y_span=10.0, driver="AAA"
    )

    assert row is not None
    assert row["plot_role"] == "simulation"
    assert format_relative_delta_hover_text(row).splitlines() == [
        "AAA  Simulation  Lap 4 S2",
        "Delta +4.000 s",
        "HARD  tyre age 1",
    ]


def test_live_curve_click_selects_driver_without_changing_reference():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window.reference_driver.addItems(["AAA", "BBB"])
    window.reference_driver.setCurrentText("AAA")

    window._select_driver("BBB")

    assert window.relative_plot.selected_driver == "BBB"
    assert window.reference_driver.currentText() == "AAA"
    window.close()
    app.processEvents()


def test_shared_curve_click_updates_selection_and_emits_driver():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = RelativeDeltaPlotWidget()
    selected = []
    widget.driver_selected.connect(selected.append)

    class Event:
        accepted = False

        def accept(self):
            self.accepted = True

    event = Event()
    widget._curve_clicked("AAA", event)

    assert widget.selected_driver == "AAA"
    assert selected == ["AAA"]
    assert event.accepted is True
    widget.close()
    app.processEvents()


def test_live_axis_controls_apply_fractional_sector_plot_range():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window.x_min_input.setValue(2.0)
    window.x_max_input.setValue(4.0)
    window.y_min_input.setValue(-10.0)
    window.y_max_input.setValue(20.0)

    window._apply_custom_ranges()

    x_range, y_range = window.relative_plot.plot.getPlotItem().vb.viewRange()
    assert x_range == pytest.approx([2.0 - 1.0 / 3.0, 4.0 + 1.0 / 3.0])
    assert y_range == pytest.approx([-10.0, 20.0])
    assert window.custom_ranges is True
    window.close()
    app.processEvents()


def test_live_uses_shared_axis_controls_and_renders_shared_pit_window():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    assert isinstance(window.axis_controls, AxisRangeControls)
    window.service._last_plot_data = pd.DataFrame(
        [
            {
                "driver": "AAA", "lap": 2, "sector": "S1",
                "segment_index": 4, "x_lap": 2.0 - 1.0 / 3.0,
                "delta_s": 1.0, "plot_role": "actual",
            },
            {
                "driver": "AAA", "lap": 2, "sector": "S2",
                "segment_index": 5, "x_lap": 2.0,
                "delta_s": 2.0, "plot_role": "actual",
            },
        ]
    )

    window._select_driver("AAA")

    assert len(window.pit_window.items) == 7
    first_curve_x, _ = window.pit_window.items[0].getData()
    assert first_curve_x.tolist() == pytest.approx([2.0 - 1.0 / 3.0, 2.0])
    window._clear_selection()
    assert window.pit_window.items == []
    window.close()
    app.processEvents()
