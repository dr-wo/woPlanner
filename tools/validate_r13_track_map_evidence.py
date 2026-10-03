#!/usr/bin/env python3
"""Collect read-only R13 evidence for the live track-map integration.

This is deliberately an evidence collector, not a test and not a replay
writer.  It consumes an existing recording and immutable event configuration,
uses the normal replay/reducer/widget integration, and writes one JSON report
to a caller-selected path *outside* this repository.

The few reducer attributes used to recover several historical clean laps are
called out as ``private_diagnostic_access`` in the report.  They are not used
by the application rendering path.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from collections import Counter
import json
import math
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Make a headless CI invocation usable, while honouring a caller's display
# choice.  This must happen before importing PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6 import QtWidgets

from wodata.live import ReplayConfig
from wodata.live.reducer import LiveSessionStore
from wodata.live.replay import load_replay
from woplanner.common.track_position import ReferenceLapMapper
from woplanner.common.windows import resolve_effective_windows
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerService
from woplanner.live.telemetry import (
    TelemetryAnalysisWorker,
    TrackMapDriver,
    TrackMapReferencePoint,
    TrackMapSnapshot,
    _optional_bool,  # private diagnostic access, reported below
    _reference_from_store,  # private diagnostic access, reported below
)
from woplanner.live.track_map_widget import TrackMapWidget


CHECKPOINTS = (
    ("before", 3478.0),
    # The TimingData deltas which flip LAW's flag are at 3487.278 and
    # 3518.180 in the R13 recording.  These targets intentionally fall just
    # after those packets, rather than sampling a preceding partial packet.
    ("entry", 3487.3),
    ("pit", 3502.0),
    ("exit", 3518.2),
    ("return", 3532.0),
)
OFFSETS_S = (-23.0, -15.0, -9.0, 9.0, 15.0, 23.0)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--event-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=3540.0)
    return parser.parse_args()


def _outside_repository(path: Path, repository: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repository)
    except ValueError:
        return resolved
    raise ValueError("--output must be outside the repository (the tool is evidence-only)")


def _driver_number(store: LiveSessionStore, code: str) -> str:
    for number, identity in store._drivers.items():  # private diagnostic access
        if str(identity.get("Tla") or "").upper() == code:
            return str(number)
    raise RuntimeError(f"No {code} identity was seen in DriverList")


def _snapshot_for(store: LiveSessionStore, driver_number: str, reference) -> TrackMapSnapshot:
    """Diagnostic snapshot made from the same reducer state used by the worker."""
    sample = store._position_samples.get(driver_number, ())[-1]  # private diagnostic access
    identity = store._drivers[driver_number]  # private diagnostic access
    timing = store._timing.get(driver_number, {})  # private diagnostic access
    driver = TrackMapDriver(
        driver_id=driver_number, x=float(sample.x), y=float(sample.y), z=float(sample.z),
        on_track=sample.on_track, sample_time_seconds=float(sample.session_time_seconds),
        sample_timestamp=None, valid=bool(sample.x or sample.y),
        driver_code=str(identity.get("Tla") or ""), team=str(identity.get("TeamName") or ""),
        in_pit=_optional_bool(timing.get("InPit")), fresh_age_s=0.0,
    )
    return TrackMapSnapshot(0, None, 0, (driver,), reference)


def _checkpoint_evidence(events, windows, app) -> tuple[dict[str, Any], Any, LiveSessionStore]:
    store = LiveSessionStore(position_sample_limit=None)
    widget = TrackMapWidget()
    widget.show()
    app.processEvents()
    widget.set_windows(windows)
    results: dict[str, Any] = {}
    next_checkpoint = 0
    last_reference = None
    for event in events:
        # ReplayPlayer is intentionally used for production replays; here the
        # loaded ReplayEvents are translated through its normal message contract
        # by a no-delay player below only where timing is measured.  The reducer
        # operation itself is the production reduction API.
        from datetime import datetime, timezone
        from wodata.live.models import LiveTimingMessage
        from wodata.live.replay.player import parse_timestamp
        message = LiveTimingMessage(
            event.topic, event.payload, parse_timestamp(event.original_timestamp),
            datetime.now(timezone.utc), event.schema_version, "f1_archive_replay",
            event.sequence, event.message_session_time_seconds if event.preserve_message_session_time else event.session_time_seconds,
        )
        store.reduce(message)
        if event.topic == "TimingData":
            last_reference = _reference_from_store(store, store._drivers)
        while next_checkpoint < len(CHECKPOINTS) and event.session_time_seconds >= CHECKPOINTS[next_checkpoint][1]:
            name, target = CHECKPOINTS[next_checkpoint]
            number = _driver_number(store, "LAW")
            reference = last_reference or _reference_from_store(store, store._drivers)
            snapshot = _snapshot_for(store, number, reference)
            widget.update_snapshot(snapshot, pd.DataFrame())
            widget.set_selected_driver("LAW")
            app.processEvents()
            selected = snapshot.drivers[0]
            results[name] = {
                "target_recording_seconds": target,
                "observed_recording_seconds": event.session_time_seconds,
                "driver_id": number,
                "in_pit": selected.in_pit,
                "on_track": selected.on_track,
                "actual_position_marker": {"x": selected.x, "y": selected.y, "z": selected.z,
                                           "sample_time_seconds": selected.sample_time_seconds},
                "reference_source": None if reference is None else reference.source,
                "rendered_window_marker_count": len(widget.rendered_window_markers),
                "rendered_window_markers": [asdict(marker) for marker in widget.rendered_window_markers],
                "status": widget.status.text(),
            }
            next_checkpoint += 1
        if next_checkpoint == len(CHECKPOINTS):
            break
    widget.close()
    if next_checkpoint != len(CHECKPOINTS):
        raise RuntimeError("recording ended before all R13 pit-cycle checkpoints")
    return results, last_reference, store


def _normal_lap_mappers(store: LiveSessionStore) -> list[dict[str, Any]]:
    """Recover 3-5 historical normal laps; reducer internals are diagnostic only."""
    candidates = []
    for number, laps in store._laps.items():  # private diagnostic access
        samples = tuple(store._position_samples.get(number, ()))  # private diagnostic access
        identity = store._drivers.get(number, {})  # private diagnostic access
        for lap_number, current in laps.items():
            previous = laps.get(lap_number - 1)
            if not isinstance(current, dict) or not isinstance(previous, dict):
                continue
            if any(bool(row.get(key)) for row in (previous, current) for key in ("pit_in", "pit_out")):
                continue
            start, end = previous.get("lap_end_time_s"), current.get("lap_end_time_s")
            if start is None or end is None or float(end) <= float(start):
                continue
            rows = [row for row in samples if float(start) <= row.session_time_seconds <= float(end)]
            if len(rows) < 30:
                continue
            extent = max(max(r.x for r in rows) - min(r.x for r in rows), max(r.y for r in rows) - min(r.y for r in rows))
            closure = math.hypot(rows[0].x - rows[-1].x, rows[0].y - rows[-1].y)
            if extent <= 0 or closure > extent * .2:
                continue
            points = [TrackMapReferencePoint(float(r.x), float(r.y), float(r.session_time_seconds - start)) for r in rows]
            if points[0].elapsed_s > 0:
                points.insert(0, TrackMapReferencePoint(points[0].x, points[0].y, 0.0))
            duration = float(end) - float(start)
            if points[-1].elapsed_s < duration:
                points.append(TrackMapReferencePoint(points[0].x, points[0].y, duration))
            try:
                mapper = ReferenceLapMapper(points)
            except ValueError:
                continue
            candidates.append({"driver": str(identity.get("Tla") or number), "lap": int(lap_number),
                               "extent": float(extent), "mapper": mapper, "points": len(points)})
    # Recent, diverse laps make a useful real comparison while remaining reproducible.
    return sorted(candidates, key=lambda item: (item["driver"], item["lap"]), reverse=True)[:5]


def _mapping_evidence(store: LiveSessionStore) -> dict[str, Any]:
    laps = _normal_lap_mappers(store)
    if len(laps) < 3:
        return {"status": "insufficient_valid_normal_laps", "count": len(laps), "private_diagnostic_access": True}
    anchor_fraction = 0.50
    report_laps = []
    all_variation = []
    for offset in OFFSETS_S:
        coordinates = []
        for item in laps:
            mapper = item["mapper"]
            point = mapper.at_elapsed(mapper.lap_time_s * anchor_fraction + offset)
            coordinates.append((item, point))
        maximum = max(math.hypot(left[1].x - right[1].x, left[1].y - right[1].y)
                      for left in coordinates for right in coordinates)
        normalizer = max(item["extent"] for item, _ in coordinates)
        all_variation.append({"offset_s": offset, "max_pairwise_distance": maximum,
                              "normalizing_track_extent": normalizer,
                              "max_pairwise_variation_normalized": maximum / normalizer})
    for item in laps:
        mapper = item["mapper"]
        report_laps.append({key: item[key] for key in ("driver", "lap", "extent", "points") } | {
            "lap_time_s": mapper.lap_time_s,
            "common_anchor_fraction": anchor_fraction,
            "coordinates": [{"offset_s": offset, "x": point.x, "y": point.y,
                             "progress": point.progress}
                            for offset in OFFSETS_S
                            for point in (mapper.at_elapsed(mapper.lap_time_s * anchor_fraction + offset),)],
        })
    maximum_normalized = max(row["max_pairwise_variation_normalized"] for row in all_variation)
    return {"status": "ok", "private_diagnostic_access": True, "selection": "five latest valid non-pit laps sorted by driver/lap",
            "common_anchor_fraction": anchor_fraction, "laps": report_laps, "offset_variation": all_variation,
            "max_pairwise_variation_normalized": maximum_normalized,
            "interpretation_hint": "local-like below 0.15 track extents; major-segment-like at or above 0.15",
            "observed_classification": "local-like" if maximum_normalized < .15 else "major-segment-like"}


def _wait_worker(worker: TelemetryAnalysisWorker, deadline_s: float = 40.0) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        d = worker.diagnostics()
        if d.latest_ingress_sequence and d.processed_ingress_sequence == d.latest_ingress_sequence:
            return asdict(d)
        time.sleep(.01)
    raise RuntimeError("telemetry worker did not drain before the evidence deadline")


def _performance_evidence(
    recording: Path, max_seconds: float, windows, app, event_config: Path,
) -> dict[str, Any]:
    """Exercise the production window continuously during a Max replay."""
    events, _invalid = load_replay(recording)
    expected_messages = sum(
        event.session_time_seconds <= max_seconds for event in events
    )
    expected_planner_topic_counts = Counter(
        event.topic
        for event in events
        if event.session_time_seconds <= max_seconds
        and event.topic not in {"CarData.z", "Position.z"}
    )
    service = LivePlannerService()
    config_copy = tempfile.TemporaryDirectory(prefix="track-map-evidence-")
    isolated_config = Path(config_copy.name) / "event.json"
    source_config_bytes = event_config.read_bytes()
    shutil.copyfile(event_config, isolated_config)
    window = LivePlannerWindow(service=service, event_config_path=isolated_config)
    window.apply_pit_loss_button.setEnabled(False)
    window.track_map.set_windows(windows)
    window.resize(1200, 760)
    window.show()
    app.processEvents()

    snapshot_dispatches = {"embedded": 0, "detached": 0}
    full_renders = {"embedded": 0, "detached": 0}
    timing_plot_redraws = 0
    embedded_update = window.track_map.update_snapshot
    embedded_render = window.track_map._render
    planner_redraw = window._redraw

    def update_embedded(*args, **kwargs):
        snapshot_dispatches["embedded"] += 1
        return embedded_update(*args, **kwargs)

    def render_embedded(*args, **kwargs):
        full_renders["embedded"] += 1
        return embedded_render(*args, **kwargs)

    window.track_map.update_snapshot = update_embedded
    window.track_map._render = render_embedded

    def redraw_planner(*args, **kwargs):
        nonlocal timing_plot_redraws
        timing_plot_redraws += 1
        return planner_redraw(*args, **kwargs)

    window._redraw = redraw_planner
    hidden_redraw_start = None
    hidden_redraw_end = None
    detached_spy_installed = False
    selected = None
    selection_latencies_ms: list[float] = []
    poll_latencies_ms: list[float] = []
    event_probe_latencies_ms: list[float] = []
    transitions: list[dict[str, Any]] = []

    started = time.perf_counter()
    service.start_replay(
        ReplayConfig(recording, speed=0.0, end_time_seconds=max_seconds)
    )
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        poll_started = time.perf_counter()
        window._poll()
        poll_latencies_ms.append((time.perf_counter() - poll_started) * 1000)
        probe_started = time.perf_counter()
        app.processEvents()
        event_probe_latencies_ms.append((time.perf_counter() - probe_started) * 1000)

        diagnostics = service.telemetry_worker.diagnostics()
        progress = (
            diagnostics.latest_ingress_sequence / expected_messages
            if expected_messages else 1.0
        )
        available = service.available_drivers()
        codes = list(available.get("driver", pd.Series(dtype=str)).astype(str))
        if selected is None and codes:
            selected = "LAW" if "LAW" in codes else codes[0]
            selection_started = time.perf_counter()
            window._select_driver(selected)
            app.processEvents()
            selection_latencies_ms.append((time.perf_counter() - selection_started) * 1000)
            transitions.append({"scenario": "embedded_visible", "progress": progress})
        if progress >= 0.25 and hidden_redraw_start is None:
            window._set_embedded_map_visible(False)
            hidden_redraw_start = full_renders["embedded"]
            transitions.append({"scenario": "embedded_hidden", "progress": progress})
        if progress >= 0.45 and window.track_map_window is None:
            window._show_track_map_window()
            detached = window.track_map_window
            detached.resize(820, 620)
            detached_update = detached.track_map.update_snapshot
            detached_render = detached.track_map._render

            def update_detached(*args, **kwargs):
                snapshot_dispatches["detached"] += 1
                return detached_update(*args, **kwargs)

            def render_detached(*args, **kwargs):
                full_renders["detached"] += 1
                return detached_render(*args, **kwargs)

            detached.track_map.update_snapshot = update_detached
            detached.track_map._render = render_detached
            detached_spy_installed = True
            transitions.append({"scenario": "detached_normal", "progress": progress})
        if progress >= 0.65 and len(selection_latencies_ms) == 1 and len(codes) > 1:
            selected = codes[-1] if codes[-1] != selected else codes[0]
            selection_started = time.perf_counter()
            window._select_driver(selected)
            app.processEvents()
            selection_latencies_ms.append((time.perf_counter() - selection_started) * 1000)
        if (
            progress >= 0.75
            and window.track_map_window is not None
            and window.track_map_window.size().width() < 1200
        ):
            window.track_map_window.resize(1280, 860)
            transitions.append({"scenario": "detached_enlarged", "progress": progress})

        if not service.controller.is_running:
            window._poll()
            diagnostics = service.telemetry_worker.diagnostics()
            if (
                diagnostics.latest_ingress_sequence == expected_messages
                and diagnostics.processed_ingress_sequence
                == diagnostics.latest_ingress_sequence
                and service.controller.message_counts
                == expected_planner_topic_counts
            ):
                break
        time.sleep(0.002)
    else:
        raise RuntimeError("production Max replay did not finish before the evidence deadline")

    diagnostics = _wait_worker(service.telemetry_worker)
    hidden_redraw_end = full_renders["embedded"]
    detached = window.track_map_window
    synchronised = bool(
        selected
        and window.relative_plot.selected_driver == selected
        and window.track_map.selected_driver == selected
        and detached is not None
        and detached.track_map.selected_driver == selected
    )
    scenario_names = {item["scenario"] for item in transitions}
    required_scenarios = {
        "embedded_visible", "embedded_hidden", "detached_normal", "detached_enlarged",
    }
    checks = {
        "all_messages_ingressed": diagnostics["latest_ingress_sequence"]
        == expected_messages,
        "worker_drained": diagnostics["processed_ingress_sequence"]
        == diagnostics["latest_ingress_sequence"],
        "planner_queue_drained": service.controller.message_counts
        == expected_planner_topic_counts,
        "all_scenarios_exercised": required_scenarios <= scenario_names,
        "visible_and_detached_rendered": full_renders["embedded"] > 0
        and full_renders["detached"] > 0,
        "two_selection_probes": len(selection_latencies_ms) >= 2,
        "hidden_embedded_did_not_redraw": hidden_redraw_end
        - hidden_redraw_start == 0,
        "presentations_synchronised": synchronised,
    }
    if not all(checks.values()):
        raise RuntimeError(f"production Max replay evidence incomplete: {checks}")
    result = {
        "replay_speed": "max (ReplayConfig speed=0)",
        "replay": {
            "status": "completed",
            "messages_emitted": diagnostics["latest_ingress_sequence"],
            "expected_messages": expected_messages,
            "planner_topic_messages": int(sum(service.controller.message_counts.values())),
            "planner_topic_counts": dict(service.controller.message_counts),
            "expected_planner_topic_counts": dict(expected_planner_topic_counts),
            "wall_seconds": time.perf_counter() - started,
            "diagnostics_after_sync": diagnostics,
        },
        "production_window": {
            "poll_count": len(poll_latencies_ms),
            "timing_plot_redraw_count": timing_plot_redraws,
            "embedded_snapshot_dispatch_count": snapshot_dispatches["embedded"],
            "detached_snapshot_dispatch_count": snapshot_dispatches["detached"],
            "embedded_full_render_count": full_renders["embedded"],
            "detached_full_render_count": full_renders["detached"],
            "hidden_embedded_full_render_count": hidden_redraw_end - hidden_redraw_start,
            "detached_spy_installed": detached_spy_installed,
            "max_poll_ms": max(poll_latencies_ms, default=0.0),
            "max_event_probe_ms": max(event_probe_latencies_ms, default=0.0),
            "max_selection_ms": max(selection_latencies_ms, default=0.0),
            "poll_samples_ms": poll_latencies_ms,
            "event_probe_samples_ms": event_probe_latencies_ms,
            "selection_samples_ms": selection_latencies_ms,
            "transitions": transitions,
        },
        "verification": checks,
        "sync": {
            "worker_drained": diagnostics["processed_ingress_sequence"]
            == diagnostics["latest_ingress_sequence"],
            "selected_driver": selected,
            "all_presentations_synchronised": synchronised,
        },
    }
    window.close()
    assert event_config.read_bytes() == source_config_bytes
    config_copy.cleanup()
    service.shutdown()
    app.processEvents()
    return result


def main() -> int:
    args = _args()
    repository = Path(__file__).resolve().parents[1]
    output = _outside_repository(args.output, repository)
    if not args.recording.is_file() or not args.event_config.is_file():
        raise FileNotFoundError("--recording and --event-config must name existing files")
    event_config = json.loads(args.event_config.read_text(encoding="utf-8"))
    event_identity = {
        "event_id": event_config.get("event_id"),
        "season": event_config.get("season"),
        "round_number": event_config.get("round_number"),
        "session": event_config.get("session"),
        "event_name": event_config.get("event_name"),
    }
    if event_identity["season"] != 2026 or event_identity["round_number"] != 13:
        raise ValueError(f"event config is not the required R13 input: {event_identity}")
    # Recording parsing is the production replay loader; the config is read-only
    # and used only to prove that the paired preparation input is event-bound.
    events, invalid = load_replay(args.recording)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
    windows = resolve_effective_windows(green_pit_loss_s=20.0, sc_pit_loss_s=9.0, vsc_pit_loss_s=7.0,
                                        source="evidence-only fixed display values")
    checkpoints, _, store = _checkpoint_evidence(events, windows, app)
    evidence = {"purpose": "R13 Track Map-only evidence on current main", "recording": str(args.recording.resolve()),
                "event_config": str(args.event_config.resolve()), "event_config_isolated_copy": True,
                "event_identity": event_identity,
                "output_policy": "outside repository only", "invalid_record_count": invalid,
                "private_diagnostic_access": ["LiveSessionStore._drivers/_timing/_laps/_position_samples", "_reference_from_store", "_optional_bool"],
                "D_pit_cycle": {"driver": "LAW", "expected_transition_seconds": {"in_pit_true_near": 3487, "in_pit_false_near": 3518},
                                "checkpoints": checkpoints,
                                "verification": {
                                    "entry_and_pit_flags_true": all(checkpoints[name]["in_pit"] is True for name in ("entry", "pit")),
                                    "exit_and_return_flags_false": all(checkpoints[name]["in_pit"] is False for name in ("exit", "return")),
                                    "pit_windows_suppressed": all(checkpoints[name]["rendered_window_marker_count"] == 0 for name in ("entry", "pit")),
                                    "windows_resume_after_exit": all(checkpoints[name]["rendered_window_marker_count"] > 0 for name in ("exit", "return")),
                                    "markers_are_actual_position_coordinates": "TrackMapDriver coordinates are copied directly from the reducer's latest Position.z sample",
                                },
                                "conclusion": "The checkpoint records include the real Position.z coordinates and the widget's actual rendered marker count."},
                "E_reference_lap_mapping": _mapping_evidence(store),
                "F_practical_max_replay": _performance_evidence(
                    args.recording, args.max_seconds, windows, app, args.event_config
                )}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
