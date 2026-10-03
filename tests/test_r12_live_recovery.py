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
from woplanner.live.service import (
    LivePlannerService,
    REFERENCE_MODE_ACTUAL_PACE,
    REFERENCE_MODE_AVERAGED_PACE,
)


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
    assert len(snapshot.completed_laps) == 1344
    # Initial lap-2 reconnect registers have no preceding source boundary.
    # HAD lap 5 also contains S1 published before its retained lap-4 counter.
    # Those 22 complete tuples remain uncertified; retained values are checked
    # against the full baseline by the recording acceptance audit.
    assert len(snapshot.completed_sectors) == 3909 - 66
    uncertified_laps = {
        (number, 2) for number in (
            "1", "10", "11", "12", "14", "16", "18", "22", "23", "27",
            "30", "31", "41", "43", "44", "5", "55", "63", "77", "81", "87",
        )
    } | {("41", 5)}
    assert not uncertified_laps.intersection(
        (sector.driver_number, sector.lap_number) for sector in snapshot.completed_sectors
    )
    nor_restart = [
        lap
        for lap in snapshot.completed_laps
        if lap.driver_code == "NOR" and lap.lap_number in {2, 3, 4}
    ]
    assert [lap.lap_number for lap in nor_restart] == [2, 3, 4]
    assert [lap.lap_time_s for lap in nor_restart] == pytest.approx(
        [123.197, 134.117, 198.679]
    )
    assert [lap.cumulative_time_s for lap in nor_restart] == pytest.approx(
        [123.197, 257.314, 455.993]
    )
    assert all(
        later.cumulative_time_s > earlier.cumulative_time_s
        for earlier, later in zip(nor_restart, nor_restart[1:])
    )
    assert not [
        sector
        for sector in snapshot.completed_sectors
        if sector.driver_code == "NOR" and sector.lap_number == 3
    ]
    nor_l16 = next(
        lap
        for lap in snapshot.completed_laps
        if lap.driver_code == "NOR" and lap.lap_number == 16
    )
    assert (nor_l16.driver_number, nor_l16.driver_code, nor_l16.lap_number) == (
        "1", "NOR", 16
    )
    nor_l16_sectors = [
        sector
        for sector in snapshot.completed_sectors
        if sector.driver_code == "NOR" and sector.lap_number == 16
    ]
    assert [sector.sector_time_s for sector in nor_l16_sectors] == pytest.approx(
        [25.908, 28.026, 23.258]
    )
    sector_total = sum(sector.sector_time_s for sector in nor_l16_sectors)
    assert sector_total == pytest.approx(nor_l16.lap_time_s)
    assert sector_total == pytest.approx(77.192)

    restart_service = LivePlannerService()
    restart_service.snapshot = snapshot
    actual = restart_service.build_plot_data(
        "NOR", reference_mode=REFERENCE_MODE_ACTUAL_PACE
    )
    actual_restart = actual.loc[
        actual["driver"].eq("NOR") & actual["lap"].isin([2, 3, 4])
    ]
    assert actual_restart["delta_s"].tolist() == pytest.approx([0.0] * 3)
    averaged = restart_service.build_plot_data(
        "NOR", reference_mode=REFERENCE_MODE_AVERAGED_PACE
    )
    nor_averaged = averaged.loc[averaged["driver"].eq("NOR")]
    assert nor_averaged["delta_s"].abs().max() < 500.0
    restart_service.shutdown()

    clean = _live_analysis_laps(
        snapshot, quick_lap_threshold=1.10, min_clean_laps=4
    )
    diagnostics = clean.attrs["exclusion_diagnostics"]
    assert len(clean) == 204
    assert diagnostics["selected_clean_laps"] == 204
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
    assert len(overlap) == 181

    # STR is marked retired at lap 45. Because the replay contains canonical
    # lap fallbacks, averaged mode deliberately uses one homogeneous lap
    # resolution and must continue that same average beyond the retirement.
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
    assert set(plot["sector"]) == {"S3"}
    assert increments.loc[138] == pytest.approx(increments.loc[135])
    assert increments.loc[216] == pytest.approx(increments.loc[135])
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
        10: (121, 2),
        20: (321, 9),
        40: (710, 25),
        60: (1087, 43),
        72: (1289, 44),
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
    assert first_unchanged_marker_redraw == 8
    assert int(service._last_plot_data["lap"].max()) == 72
    final_events = service.build_tyre_event_data()
    assert len(final_events) == 44
    assert not final_events.duplicated(["driver", "lap", "scenario_id"]).any()

    window.close()
    app.processEvents()
