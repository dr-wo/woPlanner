from __future__ import annotations

import json
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pyqtgraph as pg
import pytest
from PySide6 import QtCore, QtTest, QtWidgets

from wodata.live import LivePositionSample, LiveTimingMessage
from wodata.live.reducer import LiveSessionStore
from woplanner.common.track_position import ReferenceLapMapper
from woplanner.common.windows import resolve_effective_windows
from woplanner.live.telemetry import (
    TrackMapDriver,
    TrackMapReference,
    TrackMapReferencePoint,
    TrackMapSnapshot,
    TelemetryAnalysisWorker,
    _reference_from_store,
)
from woplanner.live.track_map_widget import TrackMapWidget
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerUpdate


def reference_points():
    return (
        TrackMapReferencePoint(0.0, 0.0, 0.0),
        TrackMapReferencePoint(10.0, 0.0, 10.0),
        TrackMapReferencePoint(10.0, 10.0, 20.0),
        TrackMapReferencePoint(0.0, 10.0, 30.0),
        TrackMapReferencePoint(0.0, 0.1, 40.0),
    )


@pytest.mark.parametrize("reset_path", ["reference_removed", "clear", "new_session"])
def test_track_map_geometry_readiness_in_embedded_and_popup_views(
    tmp_path, monkeypatch, reset_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 16, "race": {"total_laps": 57},
        "pit_loss": {"green": {"total": 10.0}, "sc": {"total": 5.0},
                     "vsc": {"total": 7.0}},
    }))
    window = LivePlannerWindow(event_config_path=config)
    driver = TrackMapDriver(
        "1", 8.25, 1.75, 0.0, True, 10.0, None, driver_code="AAA",
    )
    styles = pd.DataFrame([{
        "driver_number": "1", "driver": "AAA", "team": "Team A",
        "colour": "#ff0000", "line_style": "solid", "position": 1,
    }])
    snapshot = TrackMapSnapshot(1, None, 1, (driver,))
    monkeypatch.setattr(window.service, "latest_track_map_snapshot", lambda: snapshot)
    monkeypatch.setattr(window.service, "available_drivers", lambda: styles)
    monkeypatch.setattr(window.service, "poll", lambda: LivePlannerUpdate(
        window.service.snapshot, (), False, frozenset(),
    ))
    preparing = (
        "Track map preparing — waiting for a completed lap to establish the circuit."
    )
    window.show()
    window.timer.stop()
    app.processEvents()

    def assert_preparing():
        for widget in (window.track_map, window.track_map_window.track_map):
            assert widget.status.isVisible()
            assert widget.status.text() == preparing
            # No circuit, car scatter, position labels or marker-dependent overlays.
            assert widget.plot.getPlotItem().items == []
            assert widget.rendered_window_markers == ()
            assert widget.rendered_window_connectors == ()

    def assert_ready():
        for widget in (window.track_map, window.track_map_window.track_map):
            assert "preparing" not in widget.status.text()
            assert "AAA lap 2 (40.0 s)" in widget.status.text()
            outline = next(item for item in widget.plot.getPlotItem().items
                           if isinstance(item, pg.PlotDataItem) and item.zValue() == -20)
            x, y = outline.getData()
            assert list(x) == [point.x for point in reference.points]
            assert list(y) == [point.y for point in reference.points]
            car_layer = next(item for item in widget.plot.getPlotItem().items
                             if isinstance(item, pg.ScatterPlotItem)
                             and any(point.data() == "AAA" for point in item.points()))
            point, = car_layer.points()
            # XY remains the current raw position, even away from the reference.
            assert (point.pos().x(), point.pos().y()) == (driver.x, driver.y)
            assert point.size() == 19
            label = next(item for item in widget.plot.items()
                         if isinstance(item, pg.TextItem) and item.toPlainText() == "AAA")
            assert (label.pos().x(), label.pos().y()) == (driver.x, driver.y)
            assert [(mark.key, mark.direction, mark.seconds)
                    for mark in widget.rendered_window_markers] == [
                (key, direction, seconds)
                for key, seconds in (("pit", 10.0), ("sc", 5.0), ("vsc", 7.0))
                for direction in ("behind", "ahead")
            ]
            assert all((line.x1, line.y1) == (driver.x, driver.y)
                       for line in widget.rendered_window_connectors)

    try:
        assert window.track_map.status.text() == preparing
        window._poll()
        window._show_track_map_window()
        window._select_driver("AAA")
        assert_preparing()

        # Starting the session and receiving moving raw XY do not establish geometry.
        window.service.snapshot = replace(window.service.snapshot, session_status="Started")
        driver = replace(driver, x=7.125, y=2.875, sample_time_seconds=11.0)
        snapshot = replace(snapshot, drivers=(driver,), position_visual_version=2)
        window._poll()
        assert_preparing()

        # A reference is sufficient even while the session is inactive.
        window.service.snapshot = replace(window.service.snapshot, session_status="Inactive")
        reference = TrackMapReference("1", "AAA", 2, 40.0, reference_points())
        snapshot = replace(snapshot, reference=reference, position_visual_version=3)
        window._poll()
        assert_ready()

        # Ready maps continue following the latest raw position on refresh/reopen.
        driver = replace(driver, x=9.375, y=3.625, sample_time_seconds=12.0)
        snapshot = replace(snapshot, drivers=(driver,), position_visual_version=4)
        window.track_map_window.close()
        window._poll()
        window._show_track_map_window()
        assert_ready()

        snapshot = replace(snapshot, generation=2, reference=None, position_visual_version=0)
        if reset_path == "clear":
            window.track_map.clear()
            window.track_map_window.track_map.clear()
        elif reset_path == "new_session":
            window._prepare_start()
            window.timer.stop()
        else:
            window._poll()
        assert_preparing()

        # Popup reopen and embedded collapse/reopen synchronise the new empty reference.
        window.track_map_window.close()
        window._set_embedded_map_visible(False)
        window._show_track_map_window()
        window._set_embedded_map_visible(True)
        assert_preparing()
    finally:
        window.close()
        app.processEvents()


def test_effective_windows_omit_unresolved_vsc_without_legacy_input():
    windows = resolve_effective_windows(
        green_pit_loss_s=20.0,
        sc_pit_loss_s=9.5,
        vsc_pit_loss_s=None,
    )

    assert [(window.key, window.value_s) for window in windows] == [
        ("pit", 20.0), ("sc", 9.5),
    ]
    assert windows[1].source == "event-bound UI value"
    assert windows[1].label == "SC ±9.5 s"


def test_effective_windows_keep_independent_sc_and_vsc_distinct():
    windows = resolve_effective_windows(
        green_pit_loss_s=20.0,
        sc_pit_loss_s=9.5,
        vsc_pit_loss_s=11.0,
    )

    assert [(window.key, window.value_s) for window in windows] == [
        ("pit", 20.0), ("sc", 9.5), ("vsc", 11.0),
    ]


def test_equal_independent_sc_and_vsc_remain_separate():
    windows = resolve_effective_windows(
        green_pit_loss_s=20.0,
        sc_pit_loss_s=9.5,
        vsc_pit_loss_s=9.5,
    )

    assert [(window.key, window.value_s) for window in windows] == [
        ("pit", 20.0), ("sc", 9.5), ("vsc", 9.5),
    ]
    assert windows[1].source == windows[2].source == "event-bound UI value"


def test_reference_mapper_wraps_time_offsets_around_lap_boundary():
    mapper = ReferenceLapMapper(reference_points())

    behind = mapper.offset_from_xy(0.0, 0.1, -5.0)
    ahead = mapper.offset_from_xy(0.0, 0.1, 5.0)

    assert behind.elapsed_s == pytest.approx(35.0, abs=0.2)
    assert ahead.elapsed_s == pytest.approx(5.0, abs=0.2)


def test_reference_is_real_clean_lap_and_rejects_pit_lap():
    store = LiveSessionStore()
    store._drivers = {"1": {"Tla": "AAA", "TeamName": "Team A"}}
    store._position_samples = {
        "1": deque(
            LivePositionSample("1", float(index), float(index), float(index % 5), 0.0)
            for index in range(41)
        )
    }
    store._laps = {"1": {
        1: {"lap_end_time_s": 0.0, "pit_in": False, "pit_out": False},
        2: {"lap_end_time_s": 40.0, "pit_in": True, "pit_out": False},
    }}

    assert _reference_from_store(store, store._drivers) is None


def test_worker_filters_unknown_identity_and_marks_stale_zero_and_pit_state():
    store = LiveSessionStore()
    store._drivers = {
        "1": {"Tla": "AAA", "TeamName": "Team A"},
        "2": {"Tla": "BBB", "TeamName": "Team B"},
        "3": {"Tla": "CCC", "TeamName": "Team C"},
        "4": {"Tla": "DDD", "TeamName": "Team D"},
    }
    store._timing = {"1": {"InPit": True}}
    store._position_samples = {
        "1": deque([LivePositionSample("1", 10.0, 1.0, 1.0, 0.0, True)]),
        "2": deque([LivePositionSample("2", 1.0, 2.0, 2.0, 0.0, True)]),
        "3": deque([LivePositionSample("3", 10.0, 0.0, 0.0, 0.0, True)]),
        "4": deque([LivePositionSample("4", 100.0, 4.0, 4.0, 0.0, True)]),
        "241": deque([LivePositionSample("241", 10.0, 3.0, 3.0, 0.0, True)]),
    }
    worker = TelemetryAnalysisWorker()
    generation = worker.reset()
    message = LiveTimingMessage(
        "Position.z", {}, None, datetime.now(timezone.utc), sequence=1,
    )
    worker._publish_track_map(store, generation, message)

    drivers = {driver.driver_id: driver for driver in worker.track_map_snapshot().drivers}
    assert set(drivers) == {"1", "2", "3", "4"}
    assert drivers["1"].valid is True
    assert drivers["1"].in_pit is True
    assert drivers["2"].valid is False
    assert drivers["2"].fresh_age_s == pytest.approx(9.0)
    assert drivers["3"].valid is False
    assert drivers["4"].valid is False
    assert drivers["4"].fresh_age_s == pytest.approx(-90.0)
    worker.shutdown()


def test_track_map_uses_same_window_descriptors_and_hides_windows_in_pit():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reference = TrackMapReference("1", "AAA", 2, 40.0, reference_points())
    driver = TrackMapDriver(
        "1", 10.0, 0.0, 0.0, True, 10.0,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        True, "AAA", "Team A", False, 0.0,
    )
    snapshot = TrackMapSnapshot(1, None, 1, (driver,), reference)
    styles = pd.DataFrame([{
        "driver_number": "1", "driver": "AAA", "team": "Team A",
        "colour": "#ff0000", "line_style": "solid",
    }])
    windows = resolve_effective_windows(
        green_pit_loss_s=10.0, sc_pit_loss_s=5.0, vsc_pit_loss_s=None,
    )
    widget = TrackMapWidget()
    widget.show()
    app.processEvents()
    widget.set_windows(windows)
    widget.update_snapshot(snapshot, styles)
    widget.set_selected_driver("AAA")

    assert [
        (item.key, item.direction, item.seconds)
        for item in widget.rendered_window_markers
    ] == [
        ("pit", "behind", 10.0), ("pit", "ahead", 10.0),
        ("sc", "behind", 5.0), ("sc", "ahead", 5.0),
    ]
    # Each visible marker is tied to the selected car's actual position.  The
    # independently resolved SC descriptor remains one pair; VSC is absent.
    assert [
        (item.key, item.direction, item.x1, item.y1, item.colour, item.line_style)
        for item in widget.rendered_window_connectors
    ] == [
        ("pit", "behind", 10.0, 0.0, "#5f5f5f", "dash"),
        ("pit", "ahead", 10.0, 0.0, "#5f5f5f", "dash"),
        ("sc", "behind", 10.0, 0.0, "#f4b400", "dash"),
        ("sc", "ahead", 10.0, 0.0, "#f4b400", "dash"),
    ]

    pit_driver = SimpleNamespace(**{**vars(driver), "in_pit": True})
    widget.update_snapshot(
        TrackMapSnapshot(1, None, 2, (pit_driver,), reference), styles
    )
    assert widget.rendered_window_markers == ()
    assert widget.rendered_window_connectors == ()
    assert "pit-side" in widget.status.text()
    widget.close()
    app.processEvents()


def test_hidden_track_map_defers_all_full_renders_until_shown(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = TrackMapWidget()
    widget.show()
    app.processEvents()
    renders = []
    original_render = widget._render

    def record_render():
        renders.append(True)
        original_render()

    monkeypatch.setattr(widget, "_render", record_render)
    widget.hide()
    widget.set_selected_driver("AAA")
    widget.set_windows(resolve_effective_windows(
        green_pit_loss_s=20.0, sc_pit_loss_s=9.0, vsc_pit_loss_s=None,
    ))
    empty = TrackMapSnapshot(0, None, 0, ())
    widget.update_snapshot(empty, pd.DataFrame())
    app.processEvents()
    assert renders == []

    widget.show()
    widget.update_snapshot(empty, pd.DataFrame())
    app.processEvents()
    assert renders == [True]
    widget.close()


@pytest.mark.parametrize("presentation,size", [
    ("embedded", (1549, 900)),
    ("detached", (820, 620)),
    ("detached", (1280, 860)),
])
def test_overlapping_track_map_marker_click_uses_shared_selection_after_refresh(
    tmp_path, monkeypatch, presentation, size,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    config.write_text(json.dumps({
        "season": 2026, "round_number": 13, "race": {"total_laps": 72},
        "pit_loss": {"green": {"total": 10.0}, "sc": {"total": 5.0},
                     "vsc": {"total": 7.0}},
    }))
    window = LivePlannerWindow(event_config_path=config)
    reference = TrackMapReference("1", "AAA", 2, 40.0, tuple(
        TrackMapReferencePoint(x, y, seconds)
        for x, y, seconds in ((0, 0, 0), (10, 0, 10), (10, 10, 20),
                              (0, 10, 30), (0, 0.1, 40))
    ))
    snapshot = TrackMapSnapshot(1, None, 1, (
        TrackMapDriver("1", 10, 0, 0, True, 10, None, driver_code="AAA"),
        TrackMapDriver("2", 0, 10, 0, True, 10, None, driver_code="BBB"),
        TrackMapDriver("3", 0, 10, 0, True, 10, None, driver_code="CCC"),
    ), reference)
    styles = pd.DataFrame([
        {"driver": "AAA", "colour": "#ff0000", "rank": 1},
        {"driver": "BBB", "colour": "#0000ff", "rank": 2},
        {"driver": "CCC", "colour": "#00ff00", "rank": 3},
    ])
    data = pd.DataFrame([
        {"driver": code, "lap": lap, "x_lap": float(lap),
         "segment_index": lap * 3, "delta_s": delta, "plot_role": "actual"}
        for code, delta in (("AAA", 0.0), ("BBB", 3.0), ("CCC", 6.0)) for lap in (1, 2)
    ])
    monkeypatch.setattr(window.service, "latest_track_map_snapshot", lambda: snapshot)
    monkeypatch.setattr(window.service, "available_drivers", lambda: styles)
    window.service._last_plot_data = data
    window.relative_plot.set_session_drivers(styles)
    window.relative_plot.set_plot_data(data)
    window.relative_plot.set_rank_data(styles, styles)
    window.resize(1549, 900)
    window.show()
    app.processEvents()
    window.track_map.update_snapshot(snapshot, styles)
    window._show_track_map_window()
    detached = window.track_map_window
    if presentation == "detached":
        detached.resize(*size)
    # Native Qt applies window geometry and scene transforms asynchronously.
    QtTest.QTest.qWait(100)
    target_map = window.track_map if presentation == "embedded" else detached.track_map
    events = []
    target_map.driver_selected.connect(events.append)

    def click(plot, scene_point, button=QtCore.Qt.MouseButton.LeftButton):
        pos = plot.mapFromScene(scene_point)
        assert plot.viewport().rect().contains(pos)
        QtTest.QTest.mouseMove(plot.viewport(), pos)
        QtTest.QTest.mouseClick(plot.viewport(), button, pos=pos)
        QtTest.QTest.qWait(50)

    def click_car(x, y):
        click(target_map.plot, target_map.plot.getViewBox().mapViewToScene(
            QtCore.QPointF(x, y)
        ))

    def assert_selection(code):
        assert window.relative_plot.selected_driver == code
        for ranks in (window.relative_plot.start_rank_list,
                      window.relative_plot.latest_rank_list):
            assert ranks.currentItem().data(QtCore.Qt.ItemDataRole.UserRole) == code
        driver = next(item for item in snapshot.drivers if item.driver_code == code)
        mapper = ReferenceLapMapper(reference.points)
        for widget in (window.track_map, detached.track_map):
            assert widget.selected_driver == code
            label = next(item for item in widget.plot.items()
                         if isinstance(item, pg.TextItem) and item.toPlainText() == code)
            assert label.textItem.font().bold()
            car_layer = next(item for item in widget.plot.getPlotItem().items
                             if isinstance(item, pg.ScatterPlotItem)
                             and any(point.data() == code for point in item.points()))
            assert next(point for point in car_layer.points()
                        if point.data() == code).size() == 19
            assert [(mark.key, mark.direction, mark.seconds)
                    for mark in widget.rendered_window_markers] == [
                (key, direction, seconds)
                for key, seconds in (("pit", 10.0), ("sc", 5.0), ("vsc", 7.0))
                for direction in ("behind", "ahead")
            ]
            for mark in widget.rendered_window_markers:
                seconds = -mark.seconds if mark.direction == "behind" else mark.seconds
                expected = mapper.offset_from_xy(driver.x, driver.y, seconds)
                assert (mark.x, mark.y) == pytest.approx((expected.x, expected.y))
            assert all((line.x1, line.y1) == (driver.x, driver.y)
                       for line in widget.rendered_window_connectors)
        # The Planner's Pit/SC/VSC bands also use the same selected curve.
        selected_delta = {"AAA": 0.0, "BBB": 3.0, "CCC": 6.0}[code]
        assert len(window.pit_window.items) == 12
        for index, seconds in enumerate((10.0, 5.0, 7.0)):
            _, lower = window.pit_window.items[index * 4].getData()
            _, upper = window.pit_window.items[index * 4 + 1].getData()
            assert list(lower) == pytest.approx([selected_delta - seconds] * 2)
            assert list(upper) == pytest.approx([selected_delta + seconds] * 2)

    try:
        window._select_driver("AAA")
        QtTest.QTest.qWait(50)
        assert_selection("AAA")
        # Both dots occupy this point. ScatterPlotItem supplies a NumPy array
        # of hits in reverse draw order; the existing contract selects its first.
        click_car(0, 10)
        assert events == ["CCC"]
        assert_selection("CCC")

        # Rendered items are rebuilt by live refresh; their handlers must survive.
        for widget in (window.track_map, detached.track_map):
            widget.update_snapshot(snapshot, styles)
        QtTest.QTest.qWait(50)
        assert_selection("CCC")
        click_car(10, 0)
        assert events == ["CCC", "AAA"]
        assert_selection("AAA")
        click_car(0, 10)
        assert events == ["CCC", "AAA", "CCC"]
        assert_selection("CCC")

        # Existing Planner name selection must flow back to both map views.
        ranks = window.relative_plot.start_rank_list
        item = ranks.item(0)
        QtTest.QTest.mouseClick(ranks.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               pos=ranks.visualItemRect(item).center())
        app.processEvents()
        assert_selection("AAA")
    finally:
        window.close()
        app.processEvents()
