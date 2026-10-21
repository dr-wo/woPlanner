import os
import json
from types import MappingProxyType

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from wodata.event_operational import read_event_operational_inputs
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


def test_r15_native_curve_selection_keeps_green_overlay_through_replay_refresh(tmp_path):
    from pathlib import Path

    from PySide6 import QtCore, QtTest
    from wodata.live import load_recording

    # Original R15 messages, projected to the first-lap timing transitions.
    fixture = Path(__file__).parents[2] / "woData/tests/fixtures/r15_first_laps.jsonl"
    messages = [record.message for record in load_recording(fixture)]
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 15, "race": {"total_laps": 51},
        "pit_loss": {"green": {"total": 19.3}, "sc": {"total": None},
                     "vsc": {"total": None}},
    }))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    # This tests the timing/rendering path without dispatching analysis work.
    service.controller.set_telemetry_sink(lambda message: None)
    window = LivePlannerWindow(service=service, event_config_path=config)
    selected = []
    window.relative_plot.driver_selected.connect(selected.append)
    window.show()
    app.processEvents()
    plot = window.relative_plot.plot
    view = plot.getViewBox()

    def click_curve(driver):
        x, y = window.relative_plot.curves[driver].getData()
        assert len(x) >= 2
        point = QtCore.QPointF(float((x[0] + x[1]) / 2), float((y[0] + y[1]) / 2))
        QtTest.QTest.mouseClick(
            plot.viewport(), QtCore.Qt.MouseButton.LeftButton,
            pos=plot.mapFromScene(view.mapViewToScene(point)),
        )
        app.processEvents()
        assert window.relative_plot.selected_driver == driver

    def assert_green_overlay():
        items = window.pit_window.items
        assert len(items) == 4
        assert items[3].toPlainText() == "GREEN pit window"
        assert all(item in plot.getPlotItem().items and item.scene() is plot.scene()
                   and item.isVisible() for item in items)
        assert not items[2].path().isEmpty()
        assert items[2].path().intersects(view.viewRect())
        _, lower = items[0].getData()
        _, upper = items[1].getData()
        assert (upper - lower).tolist() == pytest.approx([38.6] * len(lower))

    try:
        for message in messages:
            if message.sequence <= 4213:
                service.controller._enqueue_message(message)
        window._poll()
        window.reference_driver.setCurrentText("RUS")
        app.processEvents()
        assert [window._pit_loss_value(field) for field in (
            window.normal_pit_loss, window.sc_pit_loss, window.vsc_pit_loss,
        )] == [19.3, None, None]
        click_curve("RUS")
        assert_green_overlay()
        old_items = list(window.pit_window.items)
        old_curves = list(window.relative_plot.curves.values())
        old_data = service._last_plot_data

        for message in messages:
            if 4213 < message.sequence <= 4269:
                service.controller._enqueue_message(message)
        window._poll()
        app.processEvents()
        assert window.relative_plot.selected_driver == "RUS"
        assert service._last_plot_data is not old_data
        assert all(item.scene() is None for item in old_items + old_curves)
        assert_green_overlay()
        click_curve("LEC")
        assert_green_overlay()

        # Empty space before the first real plotted sector clears selection.
        point = QtCore.QPointF(view.viewRect().left() + 0.05, view.viewRect().center().y())
        QtTest.QTest.mouseClick(
            plot.viewport(), QtCore.Qt.MouseButton.LeftButton,
            pos=plot.mapFromScene(view.mapViewToScene(point)),
        )
        app.processEvents()
        assert window.relative_plot.selected_driver is None
        assert window.pit_window.items == []
        click_curve("RUS")
        assert_green_overlay()
        assert selected == ["RUS", "LEC", "RUS"]
    finally:
        window.close()
        app.processEvents()


def test_live_window_constructs_and_disconnects_before_connect():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    assert window.reference_mode.currentData() == REFERENCE_MODE_AVERAGED_PACE
    assert window.strategy_prediction_button.text() == "Strategy"
    assert window.track_map_button.text() == "Track Map"
    assert window.track_map_collapse_button.text() == "Embedded Map"
    window._disconnect()
    assert "Disconnected" in window.connection_status.text()
    window.close()
    app.processEvents()


def test_single_provisional_observation_is_visible_and_selectable():
    from PySide6 import QtCore, QtTest

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = RelativeDeltaPlotWidget()
    widget.resize(1000, 500)
    widget.set_session_drivers(pd.DataFrame([{"driver": "AAA", "colour": "#123456"}]))
    widget.set_plot_data(pd.DataFrame([
        {"driver": "AAA", "lap": 2, "x_lap": 1.33, "delta_s": 1.0,
         "timing_state": "provisional"},
    ]))
    widget.plot.setXRange(1.0, 2.0)
    widget.plot.setYRange(-5.0, 5.0)
    widget.show()
    app.processEvents()
    try:
        assert widget.curves["AAA"].opts["symbol"] == "o"
        assert len(widget.curves["AAA"].scatter.points()) == 1
        view = widget.plot.getViewBox()
        QtTest.QTest.mouseClick(
            widget.plot.viewport(), QtCore.Qt.MouseButton.LeftButton,
            pos=widget.plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(1.33, 1.0))),
        )
        app.processEvents()
        assert widget.selected_driver == "AAA"
    finally:
        widget.close()
        app.processEvents()


def test_live_redraw_revises_relabels_and_removes_provisional_points_and_overlays(tmp_path):
    from dataclasses import replace
    from wodata.live.models import CompletedSector, ProvisionalSector

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    service.snapshot = replace(
        LiveSessionSnapshot.empty(),
        drivers=(LiveDriver("1", "AAA", "A", "A Driver", 1, None),),
        completed_sectors=tuple(
            CompletedSector("1", "AAA", "A", 1, f"S{s}", s, 30.0, s * 30.0, 1, None)
            for s in (1, 2, 3)
        ),
        provisional_sectors=(
            ProvisionalSector("1", "AAA", "A", 2, "S1", 4, 30.0, 120.0, 1, None,
                              source_time_s=120.0),
        ),
    )
    window = LivePlannerWindow(service=service, event_config_path=tmp_path / "absent.json")
    window.reference_driver.addItem("AAA")
    window.normal_pit_loss.setValue(10.0)
    window._select_driver("AAA")
    try:
        window._redraw()
        old_curve = window.relative_plot.curves["AAA"]
        old_overlay = list(window.pit_window.items)
        assert service._last_plot_data["segment_index"].tolist() == [1, 2, 3, 4]
        assert old_overlay

        service.snapshot = replace(service.snapshot, provisional_sectors=(
            ProvisionalSector("1", "AAA", "A", 3, "S1", 7, 31.0, 211.0, 1, None,
                              source_time_s=211.0),
        ))
        window._redraw()
        assert old_curve.scene() is None
        assert all(item.scene() is None for item in old_overlay)
        assert service._last_plot_data["segment_index"].tolist() == [1, 2, 3, 7]
        assert window.relative_plot.selected_driver == "AAA"

        service.snapshot = replace(service.snapshot, provisional_sectors=())
        window._redraw()
        assert service._last_plot_data["segment_index"].tolist() == [1, 2, 3]
        service.snapshot = replace(service.snapshot, completed_sectors=())
        window._redraw()
        assert service._last_plot_data.empty
        assert window.relative_plot.curves == {}
        assert window.pit_window.items == []
    finally:
        window.close()
        app.processEvents()


def test_replay_completion_without_new_timing_removes_live_only_display(tmp_path):
    from dataclasses import replace
    from wodata.live.models import LiveConnectionStatus, ProvisionalSector
    from woplanner.live.service import LivePlannerUpdate

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    service.snapshot = replace(
        LiveSessionSnapshot.empty(),
        drivers=(LiveDriver("1", "AAA", "A", "A Driver", 1, None),),
        provisional_sectors=(
            ProvisionalSector("1", "AAA", "A", 1, "S1", 1, 30.0, 30.0, 1, None,
                              source_time_s=30.0),
        ),
    )
    window = LivePlannerWindow(service=service, event_config_path=tmp_path / "absent.json")
    window.reference_driver.addItem("AAA")
    window._driver_signature = ("AAA",)
    window._active_source_mode = "replay"
    try:
        window._redraw()
        assert service._last_plot_data["timing_state"].tolist() == ["provisional"]
        service.poll = lambda: LivePlannerUpdate(
            service.snapshot, (LiveConnectionStatus("disconnected", "Replay completed"),),
            False, frozenset(),
        )
        window._poll()
        assert window._final_display
        assert service._last_plot_data.empty
        assert window.relative_plot.curves == {}
        window._prepare_start()
        assert not window._final_display
        window._redraw()
        assert service._last_plot_data["timing_state"].tolist() == ["provisional"]
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("terminal_status", ["Finished", "Finalised", "Ends"])
def test_real_live_completion_rebuilds_reference_and_overlay_and_survives_disconnect(
    tmp_path, terminal_status,
):
    from dataclasses import replace
    from wodata.live.models import CompletedSector, LiveConnectionStatus, ProvisionalSector
    from woplanner.live.service import LivePlannerUpdate

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    service.snapshot = replace(
        LiveSessionSnapshot.empty(), session_status="Started",
        drivers=(LiveDriver("1", "AAA", "A", "A Driver", 1, None),),
        completed_laps=(CompletedLap("1", "AAA", "A", 1, 90.0, 90.0, 1, None),),
        completed_sectors=tuple(
            CompletedSector("1", "AAA", "A", 1, f"S{s}", s, 30.0, s * 30.0, 1, None)
            for s in (1, 2, 3)
        ),
        provisional_sectors=(
            ProvisionalSector("1", "AAA", "A", 2, "S1", 4, 60.0, 150.0, 1, None,
                              source_time_s=150.0),
        ),
    )
    window = LivePlannerWindow(service=service, event_config_path=tmp_path / "absent.json")
    window._active_source_mode = "live"
    window.reference_driver.addItem("AAA")
    window._driver_signature = ("AAA",)
    window.normal_pit_loss.setValue(10.0)
    window._select_driver("AAA")
    try:
        window._redraw()
        assert service._last_plot_data["timing_state"].tolist() == ["committed"] * 3 + ["provisional"]
        assert service._last_plot_data["reference_cumulative_time_s"].tolist() == [45.0, 75.0, 105.0, 150.0]

        # Race interruption/restart and transport disconnection are not final.
        for temporary_status in ("Inactive", "Aborted", "Started"):
            service.snapshot = replace(service.snapshot, session_status=temporary_status)
            service.poll = lambda: LivePlannerUpdate(
                service.snapshot, (LiveConnectionStatus("disconnected", "Connection closed"),),
                True, frozenset({"session", "track_status"}),
            )
            window._poll()
            assert not window._final_display
            assert len(service._last_plot_data) == 4
        window._disconnect()
        assert not window._final_display

        old_curve = window.relative_plot.curves["AAA"]
        old_overlay = list(window.pit_window.items)
        service.snapshot = replace(service.snapshot, session_status=terminal_status)
        # Completion itself must trigger rebuilding, even without a timing diff.
        service.poll = lambda: LivePlannerUpdate(service.snapshot, (), False, frozenset())
        window._poll()
        assert window._final_display
        assert service._last_plot_data["timing_state"].tolist() == ["committed"] * 3
        assert service._last_plot_data["reference_cumulative_time_s"].tolist() == [30.0, 60.0, 90.0]
        assert service._last_plot_data["delta_s"].tolist() == [0.0] * 3
        assert old_curve.scene() is None
        assert old_overlay and all(item.scene() is None for item in old_overlay)
        assert window.relative_plot.selected_driver == "AAA"
        assert len(window.pit_window.items[0].getData()[0]) == 3

        canonical_plot = service._last_plot_data.copy(deep=True)
        window._disconnect()
        # A later status snapshot cannot unlatch final display for this source.
        service.snapshot = replace(service.snapshot, session_status="Started")
        service.poll = lambda: LivePlannerUpdate(
            service.snapshot, (LiveConnectionStatus("disconnected", "Connection closed"),),
            True, frozenset({"session"}),
        )
        window._poll()
        pd.testing.assert_frame_equal(service._last_plot_data, canonical_plot)
        assert window._final_display
        assert len(window.pit_window.items[0].getData()[0]) == 3

        window._prepare_start()
        assert not window._final_display
        window._redraw()
        assert service._last_plot_data["timing_state"].tolist() == ["committed"] * 3 + ["provisional"]
    finally:
        window.close()
        app.processEvents()


def test_live_window_updates_round_from_received_session_info():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()

    window._sync_session_context(
        {
            "Meeting": {
                "Name": "British Grand Prix", "Number": 12,
                "Country": {"Name": "Great Britain"},
                "Location": "Silverstone",
                "Circuit": {"Key": 2, "ShortName": "Silverstone"},
            },
            "Name": "Race",
            "StartDate": "2026-07-05T14:00:00Z",
        }
    )

    assert window.year_input.value() == 2026
    assert window.round_input.value() == 12
    assert "British Grand Prix" in window.windowTitle()
    assert window._event_identity == {
        "event_name": "British Grand Prix",
        "country": "Great Britain",
        "location": "Silverstone",
        "circuit": "Silverstone",
        "circuit_id": "2",
    }
    assert window._event_identity_event_key == (2026, 12)
    window.close()
    app.processEvents()


def test_unavailable_timing_keeps_displayed_lap_and_surfaces_status():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        (
            LiveDriver("1", "AAA", "Mercedes", "A Driver", 1, None),
            LiveDriver("2", "BBB", "Ferrari", "B Driver", 2, None),
        ),
        (
            CompletedLap("1", "AAA", "Mercedes", 8, 90.0, None, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 8, 91.0, None, 2, None),
        ),
        MappingProxyType({"1": 1, "2": 2}),
        None,
    )
    window = LivePlannerWindow(service=service)
    window.reference_driver.addItem("AAA")

    window._redraw()

    assert window.plot_progress_status.text().startswith("Timing unavailable:")
    assert "data 8" in window.plot_progress_status.text()
    assert "displayed 8" in window.plot_progress_status.text()
    assert len(service._last_plot_data) == 2
    assert not service._last_plot_data["timing_available"].any()

    service.snapshot = LiveSessionSnapshot(
        MappingProxyType({}),
        "Started",
        service.snapshot.drivers,
        (
            CompletedLap("1", "AAA", "Mercedes", 8, 90.0, 800.0, 1, None),
            CompletedLap("2", "BBB", "Ferrari", 8, 91.0, 802.0, 2, None),
        ),
        MappingProxyType({"1": 1, "2": 2}),
        None,
    )
    window._redraw()
    assert window.plot_progress_status.text().startswith("Plot:")
    assert service._last_plot_data["timing_available"].all()

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


def test_live_uses_persisted_values_and_renders_selected_driver_pit_window(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "race": {"total_laps": 57},
        "pit_loss": {
            "green": {"total": 24.2},
            "sc": {"total": 16.0},
            "vsc": {"total": 13.0},
        },
    }))
    window = LivePlannerWindow(event_config_path=config)
    assert isinstance(window.axis_controls, AxisRangeControls)
    assert window.normal_pit_loss.value() == pytest.approx(24.2)
    assert window.sc_pit_loss.value() == pytest.approx(16.0)
    assert window.vsc_pit_loss.value() == pytest.approx(13.0)
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

    assert len(window.pit_window.items) == 12
    assert [window.pit_window.items[index].toPlainText() for index in (3, 7, 11)] == [
        "GREEN pit window", "SC pit window", "VSC pit window",
    ]
    first_curve_x, _ = window.pit_window.items[0].getData()
    assert first_curve_x.tolist() == pytest.approx([2.0 - 1.0 / 3.0, 2.0])
    window._clear_selection()
    assert window.pit_window.items == []
    window.close()
    app.processEvents()


def test_vsc_edit_does_not_persist_until_apply_and_then_survives_reload(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    history_root = tmp_path / "data"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "race": {"total_laps": 57},
        "pit_loss": {
            "green": {"total": 24.2},
            "sc": {"total": 16.0},
            "vsc": {"total": 13.0},
            "sc_vsc": {"total": 12.5},
        },
    }))
    window = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
    )
    window._apply_operational_pit_losses()
    assert read_event_operational_inputs(
        season=2026, round_number=14, data_root=history_root,
    )["operational"]["pit_loss"]["vsc"]["total"] == 13.0

    resolved_snapshot = tmp_path / "runs" / "pre-race" / "config.resolved.json"
    resolved_snapshot.parent.mkdir(parents=True)
    resolved_snapshot.write_text(config.read_text())
    window.vsc_pit_loss.setValue(12.0)
    assert json.loads(config.read_text())["pit_loss"]["vsc"]["total"] == 13.0

    unsaved_reload = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
    )
    assert unsaved_reload.vsc_pit_loss.value() == pytest.approx(13.0)
    unsaved_reload.close()

    window._apply_operational_pit_losses()
    saved = json.loads(config.read_text())
    assert saved["pit_loss"]["vsc"]["total"] == 12.0
    assert saved["pit_loss"]["vsc"]["_provenance"]["updated_via"] == "woPlanner"
    assert saved["pit_loss"]["sc_vsc"]["total"] == 12.5
    assert read_event_operational_inputs(
        season=2026, round_number=14, data_root=history_root,
    )["operational"]["pit_loss"]["vsc"]["total"] == 12.0
    assert json.loads(resolved_snapshot.read_text())["pit_loss"]["vsc"]["total"] == 13.0
    reopened = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
    )
    assert reopened.vsc_pit_loss.value() == pytest.approx(12.0)
    reopened.close()

    next_event = json.loads(config.read_text())
    next_event["round_number"] = 15
    next_event["pit_loss"]["vsc"]["total"] = None
    config.write_text(json.dumps(next_event))
    assert read_event_operational_inputs(
        season=2026, round_number=14, data_root=history_root,
    )["operational"]["pit_loss"]["vsc"]["total"] == 12.0
    next_event_window = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
    )
    assert next_event_window.round_input.value() == 15
    assert next_event_window.vsc_pit_loss.value() == -1.0
    next_event_window.close()
    window.close()
    app.processEvents()


def test_same_window_event_transition_invalidates_identity_and_resolves_requested_event(
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    history_root = tmp_path / "data"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "race": {"total_laps": 57},
        "pit_loss": {
            "green": {"total": 24.2},
            "sc": {"total": None},
            "vsc": {"total": 13.0},
        },
    }))
    resolver_calls = []

    class R15Metadata:
        event_name = "British Grand Prix"
        official_event_name = None
        country = "Great Britain"
        location = "Silverstone"
        circuit = "Silverstone"
        circuit_id = "2"

    def resolver(event, *, include_race_distance):
        resolver_calls.append((event, include_race_distance))
        return R15Metadata()

    window = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
        event_resolver=resolver,
    )
    window._sync_session_context({
        "Meeting": {
            "Name": "Spanish Grand Prix", "Number": 14,
            "Country": {"Name": "Spain"}, "Location": "Madrid",
        },
        "Name": "Race", "StartDate": "2026-09-13T14:00:00Z",
    })
    assert window._event_identity_event_key == (2026, 14)
    window._apply_operational_pit_losses()
    window._apply_operational_pit_losses()
    assert resolver_calls == []

    next_event = json.loads(config.read_text())
    next_event["round_number"] = 15
    next_event["pit_loss"]["vsc"]["total"] = 12.0
    config.write_text(json.dumps(next_event))
    window._load_operational_inputs()

    assert window.round_input.value() == 15
    assert window._event_identity == {}
    assert window._event_identity_event_key is None
    window._apply_operational_pit_losses()

    assert resolver_calls == [("2026-15", False)]
    r15 = read_event_operational_inputs(
        season=2026, round_number=15, data_root=history_root,
    )
    assert r15["event_identity"] == {
        "event_name": "British Grand Prix",
        "country": "Great Britain",
        "location": "Silverstone",
        "circuit": "Silverstone",
        "circuit_id": "2",
    }
    assert "Spain" not in r15["event_identity"].values()
    r14 = read_event_operational_inputs(
        season=2026, round_number=14, data_root=history_root,
    )
    assert r14["event_identity"]["country"] == "Spain"
    assert r14["operational"]["pit_loss"]["vsc"]["total"] == 13.0
    window.close()
    app.processEvents()


def test_year_and_conflicting_meeting_metadata_invalidate_identity_ownership(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "pit_loss": {"green": {"total": 24.2}},
    }))
    window = LivePlannerWindow(event_config_path=config)
    r14 = {
        "Meeting": {
            "Name": "Spanish Grand Prix", "Number": 14,
            "Country": {"Name": "Spain"}, "Location": "Madrid",
        },
        "Name": "Race", "StartDate": "2026-09-13T14:00:00Z",
    }
    window._sync_session_context(r14)
    assert window._event_identity_event_key == (2026, 14)

    window.year_input.setValue(2027)
    assert window._event_identity == {}
    assert window._event_identity_event_key is None

    window.year_input.setValue(2026)
    refreshed_r14 = dict(r14)
    refreshed_r14["Type"] = "Race"
    window._sync_session_context(refreshed_r14)
    assert window._event_identity_event_key == (2026, 14)
    conflicting = {
        "Meeting": {
            "Name": "Different Grand Prix", "Number": 14,
            "Country": {"Name": "Different Country"},
            "Location": "Different Circuit",
        },
        "Name": "Race", "StartDate": "2026-09-14T14:00:00Z",
    }
    window._sync_session_context(conflicting)

    assert window._event_identity_event_key is None
    assert window._event_identity["country"] == "Different Country"
    window.close()
    app.processEvents()


def test_apply_reports_invalid_untouched_config_without_mutating_config_or_history(
    tmp_path, monkeypatch,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    history_root = tmp_path / "data"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "race": {"total_laps": -1},
        "pit_loss": {
            "green": {"total": 24.2},
            "sc": {"total": None},
            "vsc": {"total": 13.0},
        },
    }))
    original_config = config.read_bytes()
    warnings = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = LivePlannerWindow(
        event_config_path=config,
        operational_history_data_root=history_root,
    )
    window.year_input.setValue(2026)
    window.round_input.setValue(14)
    window.vsc_pit_loss.setValue(12.0)

    window._apply_operational_pit_losses()

    assert config.read_bytes() == original_config
    assert not history_root.exists()
    assert warnings
    assert warnings[-1][0] == "Current-event pit loss"
    assert "Prospective race.json is invalid; no changes were saved" in warnings[-1][1]
    assert "total_laps" in warnings[-1][1]
    window.close()
    app.processEvents()


def test_different_event_does_not_reuse_current_event_values(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 14,
        "pit_loss": {"green": {"total": 24.2}, "vsc": {"total": 13.0}},
    }))
    window = LivePlannerWindow(event_config_path=config)

    window.round_input.setValue(15)

    assert window.normal_pit_loss.specialValueText() == "Unresolved"
    assert window.normal_pit_loss.value() == -1.0
    assert window.vsc_pit_loss.value() == -1.0
    assert "config is 2026-14" in window.pit_loss_status.text()
    window.close()
    app.processEvents()


def test_track_map_window_reuses_current_presentation_and_embedded_map_can_collapse():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window._select_driver("AAA")
    window.sc_pit_loss.setValue(8.9)
    window._show_track_map_window()

    detached = window.track_map_window
    assert detached is not None
    assert detached.isVisible()
    assert detached.track_map.selected_driver == "AAA"
    assert detached.track_map._windows == window.track_map._windows

    # Closing hides the reusable, non-modal window; it does not clear the
    # selection or recreate a separate map state on the next opening.
    detached.close()
    app.processEvents()
    assert not detached.isVisible()
    detached.track_map.clear()
    window._show_track_map_window()
    assert window.track_map_window is detached
    assert detached.track_map.selected_driver == "AAA"
    assert detached.track_map._snapshot == window.service.latest_track_map_snapshot()

    window.track_map_collapse_button.click()
    assert window.track_map.isHidden()
    assert window.track_map_collapse_button.text() == "Embedded Map"
    assert not window.track_map_collapse_button.isChecked()
    window.track_map_collapse_button.click()
    assert not window.track_map.isHidden()
    assert window.track_map_collapse_button.isChecked()
    assert window.track_map._snapshot == window.service.latest_track_map_snapshot()
    window.close()
    app.processEvents()
    assert not detached.isVisible()

def test_narrow_window_keeps_two_control_rows_scrollable():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window.resize(720, 640)
    window.show()
    app.processEvents()

    bar = window.controls_scroll.horizontalScrollBar()
    assert bar.maximum() > 0
    window.controls_scroll.ensureWidgetVisible(window.disconnect_button)
    app.processEvents()
    assert bar.value() > 0
    assert window.strategy_prediction_button.text() == "Strategy"
    assert window.track_map_button.text() == "Track Map"
    assert window.track_map_collapse_button.text() == "Embedded Map"
    assert window.track_map_button.isEnabled()
    assert window.track_map_collapse_button.isEnabled()
    window.close()
    app.processEvents()

def test_validated_normal_window_fits_both_control_rows_without_scrolling():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    window.resize(1549, 900)
    window.show()
    app.processEvents()

    assert window.controls_scroll.horizontalScrollBar().maximum() == 0
    for control in (
        window.strategy_prediction_button,
        window.track_map_button,
        window.track_map_collapse_button,
    ):
        assert control.isVisible()
        assert control.isEnabled()
    window.tabs.setCurrentWidget(window.race_info_tab)
    app.processEvents()
    assert window.apply_pit_loss_button.isVisible()
    assert window.apply_pit_loss_button.isEnabled()
    window.close()
    app.processEvents()

@pytest.mark.parametrize("list_name", ["start_rank_list", "latest_rank_list"])
def test_rank_name_click_uses_shared_selection_and_survives_refresh(list_name):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = RelativeDeltaPlotWidget()
    ranks = pd.DataFrame([
        {"rank": 1, "driver": "AAA", "colour": "#123456"},
        {"rank": 2, "driver": "BBB", "colour": "#654321"},
    ])
    widget.set_rank_data(ranks, ranks)
    selected = []
    widget.driver_selected.connect(selected.append)

    target = getattr(widget, list_name).item(1)
    widget._rank_item_clicked(target)
    widget.set_rank_data(ranks.iloc[::-1], ranks.iloc[::-1])

    assert widget.selected_driver == "BBB"
    assert selected == ["BBB"]
    assert widget.start_rank_list.currentItem().data(
        QtCore.Qt.ItemDataRole.UserRole
    ) == "BBB"
    assert widget.latest_rank_list.currentItem().data(
        QtCore.Qt.ItemDataRole.UserRole
    ) == "BBB"
    widget.close()
    app.processEvents()


@pytest.mark.parametrize("source_mode", ["live", "replay"])
def test_live_strategy_entry_explicitly_enables_team_scope(monkeypatch, source_mode):
    from woplanner.analysis import gui as analysis_gui

    captured = {}

    class StrategyDialog(QtWidgets.QDialog):
        session_closing = QtCore.Signal(object, object)
        traffic_readiness_changed = QtCore.Signal(str)

        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(kwargs.get("parent"))
            self.mc_readiness_label = QtWidgets.QLabel("Sector timing boundaries")

        def set_live_feed_status(self, status):
            captured["feed_status"] = status

        def consume_live_snapshot(self, snapshot):
            captured["snapshot"] = snapshot

    monkeypatch.setattr(analysis_gui, "StrategyPredictionWindow", StrategyDialog)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    try:
        window._active_source_mode = source_mode
        window._show_strategy_prediction_window()
        assert captured["team_scope_enabled"] is True
        assert captured["mode"] is (
            analysis_gui.StrategyPredictionMode.HISTORICAL_WHAT_IF
            if source_mode == "replay" else analysis_gui.StrategyPredictionMode.CURRENT_EVENT
        )
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("canonical_x", [34.21, 34.73, 35.0, None])
def test_tyre_marker_preserves_curve_lap_coordinate_through_event_extraction(canonical_x):
    from woplanner.common.plot_data import build_tyre_event_data

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Stint change at an observation whose lap/sector/segment coordinates differ.
    # Unequal sector fractions must survive extraction, not be reconstructed.
    rows = pd.DataFrame([
        dict(driver="AAA", lap=34, sector="S3", segment_index=101,
             delta_s=7., compound="MEDIUM", stint=1, tyre_age=15),
        dict(driver="AAA", lap=35, sector="S1", segment_index=102,
             delta_s=9., compound="HARD", stint=2, tyre_age=1),
    ])
    if canonical_x is not None:
        rows["x_lap"] = [34., canonical_x]
    events = build_tyre_event_data(rows)
    widget = RelativeDeltaPlotWidget()
    try:
        for _ in range(2):  # Includes a curve clear/rebuild with unchanged events.
            widget.set_plot_data(rows)
            widget.set_tyre_event_data(events)
            x, y = widget.curves["AAA"].getData()
            marker = widget._tyre_items[0].points()[0].pos()
            assert marker.x() == pytest.approx(x[-1])
            assert marker.y() == pytest.approx(y[-1])
            assert events.iloc[0]["x_lap"] == pytest.approx(x[-1])
            label = widget._tyre_items[1].pos()
            assert label == marker
    finally:
        widget.close()
        app.processEvents()
