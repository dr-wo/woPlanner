import os
from types import MappingProxyType

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from wodata.live import CompletedLap, LiveDriver, LiveSessionSnapshot

from woplanner.live.service import LivePlannerService, REFERENCE_MODE_AVERAGED_PACE
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


def test_live_window_updates_round_from_received_session_info():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()

    window._sync_session_context(
        {
            "Meeting": {"Name": "British Grand Prix", "Number": 12},
            "Name": "Race",
            "StartDate": "2026-07-05T14:00:00Z",
        }
    )

    assert window.year_input.value() == 2026
    assert window.round_input.value() == 12
    assert "British Grand Prix" in window.windowTitle()
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


def test_direct_plot_zoom_survives_redraw_and_auto_range_restores_data(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window.reference_driver.addItem("AAA")
    plot_data = pd.DataFrame([
        {"driver": "AAA", "lap": 1, "x_lap": 1.0, "delta_s": -2.0},
        {"driver": "AAA", "lap": 20, "x_lap": 20.0, "delta_s": 8.0},
    ])
    drivers = pd.DataFrame([{"driver": "AAA", "colour": "#123456"}])
    ranks = pd.DataFrame(columns=["driver", "start_rank", "latest_rank"])
    monkeypatch.setattr(window.service, "available_drivers", lambda: drivers)
    monkeypatch.setattr(window.service, "build_driver_ranks", lambda: ranks)
    monkeypatch.setattr(window.service, "build_plot_data", lambda *_args, **_kwargs: plot_data)
    tyre_events = pd.DataFrame([
        {
            "driver": "AAA", "lap": 2, "sector": "S1", "segment_index": 4,
            "delta_s": -1.0, "compound": "HARD", "tyre_age_label": "1",
            "scenario_id": pd.NA, "is_simulated": False, "is_faded": False,
            "stop_after_lap": pd.NA,
        }
    ])
    # Tyre-event plot data intentionally has no raw stint column.
    monkeypatch.setattr(window.service, "build_tyre_event_data", lambda: tyre_events)

    window.relative_plot.plot.setXRange(5.0, 9.0, padding=0)
    window.relative_plot.plot.setYRange(-1.0, 3.0, padding=0)
    app.processEvents()
    assert window.custom_ranges is True
    custom = window.relative_plot.plot.viewRange()

    window._redraw()
    assert window.relative_plot.plot.viewRange()[0] == pytest.approx(custom[0])
    assert window.relative_plot.plot.viewRange()[1] == pytest.approx(custom[1])
    assert len(window.relative_plot._tyre_items) == 2

    # An unchanged marker signature must still be reconciled after the curve
    # layer clears and rebuilds the managed plot lifecycle.
    window._redraw()
    assert len(window.relative_plot._tyre_items) == 2
    assert all(
        item in window.relative_plot.plot.getPlotItem().items
        for item in window.relative_plot._tyre_items
    )

    window.service._last_plot_data = plot_data
    window._reset_auto_ranges()
    auto = window.relative_plot.plot.viewRange()
    assert window.custom_ranges is False
    assert auto[0] != pytest.approx(custom[0])
    assert auto[1] != pytest.approx(custom[1])
    assert len(window.relative_plot._tyre_items) == 2
    assert all(
        item in window.relative_plot.plot.getPlotItem().items
        for item in window.relative_plot._tyre_items
    )
    window.close()
    app.processEvents()


def test_live_and_replay_reconcile_corrected_tyre_history_without_duplicates():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    window = LivePlannerWindow(service=service)
    window.reference_driver.addItem("AAA")

    def snapshot(second_compound):
        return LiveSessionSnapshot(
            MappingProxyType({}),
            "Started",
            (LiveDriver("1", "AAA", "Team", "A Driver", 1, None),),
            (
                CompletedLap(
                    "1", "AAA", "Team", 1, 90.0, 90.0, 1, None,
                    "MEDIUM", 1.0, 1, False,
                ),
                CompletedLap(
                    "1", "AAA", "Team", 2, 91.0, 181.0, 1, None,
                    second_compound, 1.0, 2, False,
                ),
            ),
            MappingProxyType({"1": 1}),
            None,
            scheduled_total_laps=72,
        )

    signatures = []
    for mode, compound in (("live", "HARD"), ("replay", "HARD"), ("replay", "SOFT")):
        window._active_source_mode = mode
        service.snapshot = snapshot(compound)
        window._redraw()
        events = service.build_tyre_event_data()
        assert len(events) == 1
        assert len(window.relative_plot._tyre_items) == 2
        marker_layer = window.relative_plot._tyre_items[0]
        assert len(marker_layer.points()) == 1
        assert all(
            item in window.relative_plot.plot.getPlotItem().items
            for item in window.relative_plot._tyre_items
        )
        signatures.append(window._tyre_event_signature)

    assert signatures[0] == signatures[1]
    assert signatures[2] != signatures[1]
    assert service.build_tyre_event_data().iloc[0]["compound"] == "SOFT"

    # A fresh window/service reconstructs the same corrected marker solely from
    # authoritative recovered history; no MC result or marker cache is involved.
    recovered_service = LivePlannerService()
    recovered_service.snapshot = snapshot("SOFT")
    recovered = LivePlannerWindow(service=recovered_service)
    recovered.reference_driver.addItem("AAA")
    recovered._active_source_mode = "live"
    recovered._redraw()
    assert recovered._tyre_event_signature == signatures[2]
    assert len(recovered.relative_plot._tyre_items[0].points()) == 1

    recovered.close()
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
