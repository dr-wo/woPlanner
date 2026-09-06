from pathlib import Path
import json
import os

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from wodata.live.recovery import (
    discover_live_recordings,
    load_recording,
    merge_recordings,
    validate_recording_identity,
)
from wodata.live.reducer import LiveSessionStore
from woplanner.analysis.service import (
    ModelSummary,
    OfflineAnalysisService,
    _live_analysis_laps,
    leader_lap_snapshots,
)
from woplanner.live.gui import LivePlannerWindow
from woplanner.live.service import LivePlannerService, REFERENCE_MODE_AVERAGED_PACE


def test_saved_2026_r12_overlap_recovers_full_race_and_retro_clean_laps():
    workspace = Path(__file__).resolve().parents[2]
    recording_root = (
        workspace / "woData/wodata/live_timing/schema_v1/"
        "year=2026/round=12/session=R"
    )
    recordings = [
        load_recording(path) for path in discover_live_recordings(recording_root)
    ]
    assert len(recordings) == 4
    assert all(
        validate_recording_identity(
            recording, year=2026, round_number=12, session="R"
        ) is None
        for recording in recordings
    )

    store = LiveSessionStore(allow_retirement_reactivation=True)
    store.apply_many([item.message for item in merge_recordings(recordings)])
    snapshot = store.snapshot()

    assert max(lap.lap_number for lap in snapshot.completed_laps) == 72
    assert len(snapshot.completed_laps) == 1343
    assert len(snapshot.completed_sectors) == 4002

    clean = _live_analysis_laps(
        snapshot, quick_lap_threshold=1.10, min_clean_laps=4
    )
    diagnostics = clean.attrs["exclusion_diagnostics"]
    assert len(clean) == 188
    assert diagnostics["selected_clean_laps"] == 188
    assert diagnostics["traffic"] > 0
    assert diagnostics["pit_lap"] > 0
    assert diagnostics["non_green_track_status"] > 0

    retro = pd.read_csv(
        workspace / "woData/wostrategy/race_performance_review/schema_v1/"
        "year=2026/round=12/session=R/"
        "race_performance_2026_12_R_clean_laps.csv"
    )
    overlap = set(zip(clean["Driver"], clean["LapNumber"])).intersection(
        zip(retro["Driver"], retro["LapNumber"])
    )
    assert len(retro) == 273
    assert len(overlap) == 174

    # STR is marked retired at lap 45. The averaged reference after that cut
    # must continue with the same selected-driver sector averages, not switch
    # to another population or stop at the retirement boundary.
    service = LivePlannerService()
    service.snapshot = snapshot
    plot = service.build_plot_data("STR", reference_mode=REFERENCE_MODE_AVERAGED_PACE)
    reference = (
        plot[["segment_index", "reference_cumulative_time_s"]]
        .drop_duplicates("segment_index").set_index("segment_index")
        ["reference_cumulative_time_s"].sort_index()
    )
    increments = reference.diff()
    assert int(plot["lap"].max()) == 72
    assert increments.loc[136] == pytest.approx(increments.loc[133])
    assert increments.loc[137] == pytest.approx(increments.loc[134])
    assert increments.loc[138] == pytest.approx(increments.loc[135])
    service.shutdown()


def test_saved_2026_r12_degradation_transitions_use_fixed_initial_ratios():
    workspace = Path(__file__).resolve().parents[2]
    plot_path = (
        workspace / "woData/wostrategy/weekend_model/schema_v1/year=2026/round=12/"
        "saved_history_plots/Live_2026_R12_2026-08-23_15-05-14/plot.json"
    )
    payload = json.loads(plot_path.read_text(encoding="utf-8"))
    degradation_rows = [
        row for row in payload["parameter_history"]
        if row["parameter"] == "degradation"
    ]
    base = {
        row["compound"]: float(row["median"])
        for row in degradation_rows if int(row["as_of_leader_lap"]) == 0
    }
    assert base == pytest.approx({"HARD": .045, "MEDIUM": .07, "SOFT": .83})
    service = OfflineAnalysisService()

    def resolved_at(lap):
        selected = [
            row for row in degradation_rows
            if int(row["as_of_leader_lap"]) == lap
        ]
        summary = ModelSummary(
            {"analysis_id": f"r12-{lap}"},
            tuple({
                "parameter": "degradation",
                "compound": row["compound"],
                "median": float(row["median"]),
                "usable_lap_count": int(row.get("clean_race_laps") or 1),
                "session": "R",
                "support_status": (
                    "race_informed"
                    if row.get("degradation_race_informed") else "prior_only"
                ),
            } for row in selected),
        )
        assumptions = service.strategy_assumptions(
            summary,
            baseline_degradation=base,
            baseline_compound_delta={"HARD": .4, "MEDIUM": 0.0, "SOFT": -.5},
            initial_degradation=base,
        )
        return {row["compound"]: row for row in assumptions}

    lap31 = resolved_at(31)
    assert lap31["HARD"]["degradation"] == pytest.approx(.2173734861968975)
    assert lap31["MEDIUM"]["degradation"] == pytest.approx(.3381365340840628)
    assert lap31["SOFT"]["degradation"] == pytest.approx(4.009333189853887)
    assert lap31["HARD"]["degradation_source"] == "live_direct"
    assert lap31["MEDIUM"]["degradation_source"] == "live_derived"
    assert lap31["SOFT"]["degradation_source"] == "live_derived"

    lap64 = resolved_at(64)
    assert lap64["HARD"]["degradation"] == pytest.approx(.13617520873523467)
    assert lap64["MEDIUM"]["degradation"] == pytest.approx(.2594445409530268)
    assert lap64["SOFT"]["degradation"] == pytest.approx(
        .2594445409530268 * .83 / .07
    )
    assert lap64["MEDIUM"]["degradation_source"] == "live_direct"
    assert lap64["SOFT"]["degradation_source"] == "live_derived"
    assert lap64["SOFT"]["degradation_anchor_compound"] == "MEDIUM"

    lap65 = resolved_at(65)
    assert lap65["SOFT"]["degradation"] == pytest.approx(.38581543189349365)
    assert {
        lap65[compound]["degradation_source"]
        for compound in ("HARD", "MEDIUM", "SOFT")
    } == {"live_direct"}


def test_saved_2026_r12_tyre_markers_survive_every_replay_redraw(monkeypatch):
    workspace = Path(__file__).resolve().parents[2]
    recording_root = (
        workspace / "woData/wodata/live_timing/schema_v1/"
        "year=2026/round=12/session=R"
    )
    recordings = [
        load_recording(path) for path in discover_live_recordings(recording_root)
    ]
    store = LiveSessionStore(allow_retirement_reactivation=True)
    store.apply_many([item.message for item in merge_recordings(recordings)])
    cuts = dict(
        leader_lap_snapshots(store.snapshot(), first_lap=1, last_lap=72)
    )

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    service = LivePlannerService()
    service.snapshot = cuts[1]
    window = LivePlannerWindow(service=service)
    window._active_source_mode = "replay"
    window.reference_driver.addItem(service.default_reference_driver() or "VER")
    marker_points_passed = []
    set_tyre_event_data = window.relative_plot.set_tyre_event_data

    def record_tyre_event_data(events):
        marker_points_passed.append(len(events))
        set_tyre_event_data(events)

    monkeypatch.setattr(
        window.relative_plot, "set_tyre_event_data", record_tyre_event_data
    )

    expected = {
        # leader lap: (completed laps with tyre metadata, marker events)
        10: (121, 1),
        20: (320, 8),
        40: (709, 22),
        60: (1086, 39),
        72: (1288, 40),
    }
    first_marker_lap = None
    first_unchanged_marker_redraw = None
    previous_signature = None

    for leader_lap in range(1, 73):
        service.snapshot = cuts[leader_lap]
        window._redraw()
        events = service.build_tyre_event_data()
        signature = window._tyre_event_signature
        assert marker_points_passed[-1] == len(events)

        if not events.empty and first_marker_lap is None:
            first_marker_lap = leader_lap
        if (
            not events.empty
            and signature == previous_signature
            and first_unchanged_marker_redraw is None
        ):
            first_unchanged_marker_redraw = leader_lap

        if events.empty:
            assert window.relative_plot._tyre_items == []
        else:
            marker_layer = window.relative_plot._tyre_items[0]
            assert len(marker_layer.points()) == len(events)
            assert len(window.relative_plot._tyre_items) == len(events) + 1
            assert all(
                item in window.relative_plot.plot.getPlotItem().items
                for item in window.relative_plot._tyre_items
            )

        if leader_lap in expected:
            tyre_laps = sum(
                lap.compound is not None
                and (lap.tyre_life is not None or lap.stint is not None)
                for lap in service.snapshot.completed_laps
            )
            expected_tyre_laps, expected_events = expected[leader_lap]
            assert tyre_laps == expected_tyre_laps
            assert len(events) == expected_events
            assert int(service._last_plot_data["lap"].max()) == leader_lap

            # A second identical redraw models range/selection/MC refreshes:
            # marker points and their managed plot items must be reconstructed.
            window._redraw()
            repeated_events = service.build_tyre_event_data()
            assert marker_points_passed[-1] == expected_events
            assert len(repeated_events) == expected_events
            assert len(window.relative_plot._tyre_items[0].points()) == expected_events
            assert len(window.relative_plot._tyre_items) == expected_events + 1

        previous_signature = signature

    assert first_marker_lap == 3
    # Under the old signature shortcut, this was the first leader-lap redraw
    # that cleared the overlay and then skipped recreating unchanged markers.
    assert first_unchanged_marker_redraw == 11
    assert int(service._last_plot_data["lap"].max()) == 72
    final_events = service.build_tyre_event_data()
    assert len(final_events) == 40
    assert not final_events.duplicated(["driver", "lap", "scenario_id"]).any()

    window.close()
    app.processEvents()
