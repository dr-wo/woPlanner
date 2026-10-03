"""Render and verify the live and detached maps against the local R13 recording."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from dataclasses import replace
import json
import time
from pathlib import Path

from PySide6 import QtCore, QtTest, QtWidgets

from wodata.live import ReplayConfig
from woplanner.live.gui import LivePlannerWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--event-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end-seconds", type=float, default=600.0)
    args = parser.parse_args()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config_copy = tempfile.TemporaryDirectory(prefix="track-map-evidence-")
    isolated_config = Path(config_copy.name) / "event.json"
    source_config_bytes = args.event_config.read_bytes()
    shutil.copyfile(args.event_config, isolated_config)
    window = LivePlannerWindow(event_config_path=isolated_config)
    window.apply_pit_loss_button.setEnabled(False)
    window.resize(1800, 1180)
    window.show()
    # This is an explicit validation draft.  It proves the UI rule without
    # promoting the artifact's evidence-only legacy combined field.
    window.sc_pit_loss.setValue(8.9)
    window._prepare_start()
    window.service.start_replay(ReplayConfig(
        args.recording, 0.0, end_time_seconds=args.end_seconds,
    ))
    window._active_source_mode = "replay"

    deadline = time.monotonic() + 45.0
    stable = 0
    while time.monotonic() < deadline:
        app.processEvents()
        window._poll()
        diagnostics = window.service.telemetry_worker.diagnostics()
        drained = (
            not window.service.controller.is_running
            and window.service.controller._messages.empty()
            and diagnostics.latest_ingress_sequence > 0
            and diagnostics.position_messages_processed > 0
            and diagnostics.processed_ingress_sequence
            == diagnostics.latest_ingress_sequence
        )
        stable = stable + 1 if drained else 0
        if stable >= 3:
            break
        time.sleep(0.01)
    else:
        raise RuntimeError("R13 replay did not settle before the evidence deadline")
    # Freeze the settled replay snapshot while QTest drives the three UI
    # selection entries; otherwise the polling timer may rebuild list items
    # between scrollToItem and mouseClick.
    window.timer.stop()
    window._poll()
    app.processEvents()

    drivers = window.service.available_drivers()
    available = list(drivers["driver"].astype(str))
    selected = "RUS" if "RUS" in set(available) else available[0]
    alternatives = [driver for driver in available if driver != selected]
    if len(alternatives) < 3:
        raise RuntimeError("Need four distinct drivers for interaction evidence")
    window._select_driver(alternatives[2])
    window._redraw()
    window.x_min_input.setValue(0.0)
    window.x_max_input.setValue(2.5)
    window.y_min_input.setValue(-30.0)
    window.y_max_input.setValue(30.0)
    window._apply_custom_ranges()
    app.processEvents()

    def selection_is_synchronised(expected: str) -> bool:
        ranks = (
            window.relative_plot.start_rank_list,
            window.relative_plot.latest_rank_list,
        )
        return bool(
            window.relative_plot.selected_driver == expected
            and window.track_map.selected_driver == expected
            and (
                window.track_map_window is None
                or window.track_map_window.track_map.selected_driver == expected
            )
            and all(
                widget.currentItem() is not None
                and widget.currentItem().data(QtCore.Qt.ItemDataRole.UserRole) == expected
                for widget in ranks
            )
        )

    entry_results = {}
    for name, widget, target in (
        ("start_name", window.relative_plot.start_rank_list, alternatives[0]),
        ("right_name", window.relative_plot.latest_rank_list, alternatives[1]),
    ):
        item = next(
            widget.item(index)
            for index in range(widget.count())
            if widget.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == target
        )
        widget.scrollToItem(item)
        app.processEvents()
        QtTest.QTest.mouseClick(
            widget.viewport(), QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            widget.visualItemRect(item).center(),
        )
        app.processEvents()
        entry_results[name] = {
            "target": target,
            "synchronised": selection_is_synchronised(target),
        }

    if selected not in window.relative_plot.curves:
        raise RuntimeError(
            f"Selected curve {selected} is unavailable; completed_laps="
            f"{len(window.service.snapshot.completed_laps)}, "
            f"plot_drivers={sorted(window.relative_plot.curves)}"
        )
    curve = window.relative_plot.curves[selected]
    curve_x, curve_y = curve.getData()
    curve_index = len(curve_x) // 2
    scene_point = window.relative_plot.plot.getPlotItem().vb.mapViewToScene(
        QtCore.QPointF(float(curve_x[curve_index]), float(curve_y[curve_index]))
    )
    QtTest.QTest.mouseClick(
        window.relative_plot.plot.viewport(), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        window.relative_plot.plot.mapFromScene(scene_point),
    )
    app.processEvents()
    entry_results["curve"] = {
        "target": selected,
        "synchronised": selection_is_synchronised(selected),
    }
    if not all(result["synchronised"] for result in entry_results.values()):
        raise RuntimeError(f"Selection interaction failed: {entry_results}")
    window._redraw()
    entry_results["refresh_preserved"] = selection_is_synchronised(selected)

    QtTest.QTest.mouseClick(
        window.track_map_button, QtCore.Qt.MouseButton.LeftButton
    )
    app.processEvents()
    detached = window.track_map_window
    if detached is None or not detached.isVisible():
        raise RuntimeError("Track map button did not open the detached window")
    detached_identity = id(detached)
    detached_sync_on_open = {
        "selected_driver": detached.track_map.selected_driver,
        "windows": [
            (item.key, item.value_s, item.label)
            for item in detached.track_map._windows
        ],
    }

    sc_only = [(item.key, item.value_s, item.label) for item in window.track_map._windows]
    assert [item.key for item in window.track_map._windows] == ["pit", "sc"]
    window.sc_pit_loss.setValue(9.4)
    app.processEvents()
    changed_seconds = {
        "planner_labels": [
            item.toPlainText() for item in window.pit_window.items[3::4]
        ],
        "map_labels": sorted({
            item.label for item in window.track_map.rendered_window_markers
        }),
        "detached_map_labels": sorted({
            item.label for item in detached.track_map.rendered_window_markers
        }),
    }
    window.sc_pit_loss.setValue(8.9)
    window.vsc_pit_loss.setValue(7.2)
    split = [(item.key, item.value_s, item.label) for item in window.track_map._windows]
    assert [item.key for item in window.track_map._windows] == ["pit", "sc", "vsc"]
    window.vsc_pit_loss.setValue(8.9)
    equal_windows = [(item.key, item.value_s) for item in window.track_map._windows]
    assert equal_windows == [("pit", 23.0), ("sc", 8.9), ("vsc", 8.9)]
    window.vsc_pit_loss.setValue(-1.0)
    app.processEvents()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    popup_path = args.output.with_name(f"{args.output.stem}_popup{args.output.suffix}")
    popup_large_path = args.output.with_name(
        f"{args.output.stem}_popup_large{args.output.suffix}"
    )
    detached.resize(820, 620)
    app.processEvents()
    popup_size = [detached.width(), detached.height()]
    if not detached.grab().save(str(popup_path)):
        raise RuntimeError(f"Could not save {popup_path}")
    detached.resize(1280, 860)
    app.processEvents()
    popup_large_size = [detached.width(), detached.height()]
    if not detached.grab().save(str(popup_large_path)):
        raise RuntimeError(f"Could not save {popup_large_path}")
    detached.close()
    app.processEvents()
    hidden_after_close = not detached.isVisible()
    QtTest.QTest.mouseClick(
        window.track_map_button, QtCore.Qt.MouseButton.LeftButton
    )
    app.processEvents()
    detached_reused = id(window.track_map_window) == detached_identity
    reopen_preserved = selection_is_synchronised(selected)

    track_snapshot = window.service.latest_track_map_snapshot()
    reference = track_snapshot.reference
    mapper = window.track_map._mapper
    wrap = None if mapper is None else {
        "minus_1_second_elapsed_s": mapper.at_elapsed(-1.0).elapsed_s,
        "plus_lap_and_1_second_elapsed_s": mapper.at_elapsed(
            mapper.lap_time_s + 1.0
        ).elapsed_s,
        "lap_time_s": mapper.lap_time_s,
    }
    stale_snapshot = replace(
        track_snapshot,
        drivers=tuple(
            replace(driver, valid=False, fresh_age_s=6.0)
            if (driver.driver_code or driver.driver_id) == selected else driver
            for driver in track_snapshot.drivers
        ),
    )
    window.track_map.update_snapshot(stale_snapshot, drivers)
    stale_suppression = {
        "window_marker_count": len(window.track_map.rendered_window_markers),
        "status": window.track_map.status.text(),
    }
    window.track_map.update_snapshot(track_snapshot, drivers)
    window.round_input.setValue(14)
    event_mismatch = {
        "pit_value": window.normal_pit_loss.value(),
        "sc_value": window.sc_pit_loss.value(),
        "vsc_value": window.vsc_pit_loss.value(),
        "status": window.pit_loss_status.text(),
    }
    window.round_input.setValue(13)
    window.sc_pit_loss.setValue(8.9)
    app.processEvents()
    window.track_map_collapse_button.setChecked(False)
    window.resize(1500, 900)
    app.processEvents()
    evidence = {
        "recording": str(args.recording),
        "event_config": str(args.event_config),
        "event_config_isolated_copy": True,
        "event_key": [window.year_input.value(), window.round_input.value()],
        "selected_driver": selected,
        "selection_entries": entry_results,
        "sc_only_windows": sc_only,
        "changed_seconds": changed_seconds,
        "split_windows": split,
        "equal_independent_windows": equal_windows,
        "planner_labels": [
            item.toPlainText() for item in window.pit_window.items[3::4]
        ],
        "map_window_markers": [vars(item) for item in window.track_map.rendered_window_markers],
        "detached_map_window_markers": [
            vars(item) for item in detached.track_map.rendered_window_markers
        ],
        "detached_map_connectors": [
            vars(item) for item in detached.track_map.rendered_window_connectors
        ],
        "detached_window": {
            "non_modal": not detached.isModal(),
            "sync_on_open": detached_sync_on_open,
            "normal_size": popup_size,
            "large_size": popup_large_size,
            "hidden_after_close": hidden_after_close,
            "same_instance_after_reopen": detached_reused,
            "selection_preserved_after_reopen": reopen_preserved,
            "popup_image": str(popup_path),
            "large_popup_image": str(popup_large_path),
        },
        "main_window": {
            "size": [window.width(), window.height()],
            "embedded_map_collapsed": window.track_map.isHidden(),
            "strategy_button_text": window.strategy_prediction_button.text(),
            "track_map_button_text": window.track_map_button.text(),
        },
        "reference": None if reference is None else {
            "driver": reference.driver_code,
            "lap": reference.lap_number,
            "lap_time_s": reference.lap_time_s,
            "point_count": len(reference.points),
            "source": reference.source,
        },
        "track_driver_count": len(window.service.latest_track_map_snapshot().drivers),
        "ghost_ids_present": sorted({"241", "242", "243"}.intersection(
            driver.driver_id
            for driver in window.service.latest_track_map_snapshot().drivers
        )),
        "event_status": window.pit_loss_status.text(),
        "wrap": wrap,
        "stale_suppression": stale_suppression,
        "event_mismatch": event_mismatch,
    }
    if not window.grab().save(str(args.output)):
        raise RuntimeError(f"Could not save {args.output}")
    args.output.with_suffix(".json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    detached.hide()
    window.close()
    assert args.event_config.read_bytes() == source_config_bytes
    config_copy.cleanup()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
