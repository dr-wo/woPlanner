import os

import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from woplanner.live_service import REFERENCE_MODE_AVERAGED_PACE
from woplanner.live_gui import LivePlannerWindow
from woplanner.plot_widget import RelativeDeltaPlotWidget, format_live_hover_text


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
