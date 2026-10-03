"""Live display revisions must not change the inputs to analysis or persistence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from wodata.live import (
    CompletedLap,
    CompletedSector,
    LiveSessionSnapshot,
    LiveSessionStore,
    LiveTimingMessage,
    LiveTimingState,
    ProvisionalSector,
)
from woplanner.analysis.service import (
    _live_analysis_laps,
    _live_traffic_gaps,
    snapshot_as_of_leader_lap,
)
from woplanner.analysis.worker import _serializable_live_snapshot
from woplanner.live.telemetry import CausalAnalysisCut, _snapshot_at_cut


def committed_snapshot():
    laps, sectors = [], []
    for number, code, offset, position in (
        ("1", "AHEAD", -5.0, 1),
        ("2", "TARGET", 0.0, 2),
        ("3", "BEHIND", 3.0, 3),
    ):
        for lap in range(1, 4):
            finish = lap * 90.0 + offset
            laps.append(CompletedLap(
                number, code, "Team", lap, 90.0, finish, position, None,
                "MEDIUM", float(lap), 1, True, finish,
            ))
            for index, sector in enumerate(("S1", "S2", "S3"), 1):
                sectors.append(CompletedSector(
                    number, code, "Team", lap, sector, (lap - 1) * 3 + index,
                    30.0, finish - (3 - index) * 30.0, position, None,
                    session_time_seconds=finish,
                ))
    return replace(
        LiveSessionSnapshot.empty(), completed_laps=tuple(laps),
        completed_sectors=tuple(sectors),
    )


def with_conflicting_display(snapshot):
    # Deliberately contradictory duplicates and future points: these would
    # materially change clean-air filtering if merged into canonical sectors.
    provisional = tuple(
        ProvisionalSector(
            "2", "TARGET", "Team", lap, sector, (lap - 1) * 3 + index,
            1.0, 85.1 + lap, 2, None,
            session_time_seconds=85.1 + lap, source_time_s=300.0,
        )
        for lap in range(1, 5)
        for index, sector in enumerate(("S1", "S2", "S3"), 1)
    )
    return replace(
        snapshot, provisional_sectors=provisional,
        live_timing=(LiveTimingState(
            "2", "TARGET", completed_laps=4, position=1,
            gap_to_leader_s=0.0, gap_source_time_s=300.0,
            latest_source_time_s=300.0,
        ),),
    )


def test_display_revisions_do_not_change_clean_laps_or_traffic_for_fitting():
    canonical = committed_snapshot()
    display = with_conflicting_display(canonical)
    pd.testing.assert_frame_equal(
        _live_traffic_gaps(canonical), _live_traffic_gaps(display),
    )
    options = dict(quick_lap_threshold=1.1, min_clean_laps=1)
    expected = _live_analysis_laps(canonical, **options)
    assert not expected.empty
    assert "TARGET" in expected["Driver"].values
    pd.testing.assert_frame_equal(
        expected, _live_analysis_laps(display, **options),
    )


@pytest.mark.parametrize("cut_kind", ["leader_lap", "worker"])
def test_causal_analysis_cuts_discard_live_display_registers(cut_kind):
    snapshot = with_conflicting_display(committed_snapshot())
    if cut_kind == "leader_lap":
        cut = snapshot_as_of_leader_lap(snapshot, 2)
    else:
        cut = _snapshot_at_cut(snapshot, CausalAnalysisCut(2, 175.0, 100, 1))
    assert cut.completed_laps
    assert cut.completed_sectors
    assert cut.provisional_sectors == ()
    assert cut.live_timing == ()
    assert all(lap.session_time_seconds <= 175.0 for lap in cut.completed_laps)


def test_mc_process_payload_has_only_committed_timing_history():
    canonical = committed_snapshot()
    actual = _serializable_live_snapshot(with_conflicting_display(canonical))
    assert actual == _serializable_live_snapshot(canonical)
    assert actual.completed_laps == canonical.completed_laps
    assert actual.completed_sectors == canonical.completed_sectors
    assert not hasattr(actual, "provisional_sectors")
    assert not hasattr(actual, "live_timing")


def reducer_traffic_snapshot(target_evidence):
    """Replay the review's 1s-to-5s pollution through the production reducer."""
    store = LiveSessionStore()
    origin = datetime(2026, 7, 25, tzinfo=timezone.utc)

    def send(second, payload, topic="TimingData"):
        store.reduce(LiveTimingMessage(
            topic, payload, origin + timedelta(seconds=second), origin,
            session_time_seconds=second,
        ))

    send(0, {"Status": "Started"}, "SessionStatus")
    send(0, {"Status": "1"}, "TrackStatus")
    drivers = (("1", "AHEAD", -5), ("2", "TARGET", 0), ("3", "BEHIND", 3))
    send(0, {number: {"Tla": code, "TeamName": "Team"}
             for number, code, _ in drivers}, "DriverList")
    send(0, {"Lines": {number: {"Stints": {"0": {
        "Compound": "MEDIUM", "New": "true", "StartLaps": 0,
    }}} for number, _, _ in drivers}}, "TimingAppData")
    for number, _, offset in drivers:
        send(90 + offset, {"Lines": {number: {
            "NumberOfLaps": 1, "LastLapTime": {"Value": 90},
            "Sectors": [{"Value": value} for value in (20, 30, 40)],
        }}})
    for number, _, offset in drivers:
        if number == "2" and target_evidence == "missing":
            continue
        values = (16, 34, 40) if number == "2" and target_evidence == "traffic" else (20, 30, 40)
        second = offset if number == "2" and target_evidence == "delayed" else 90 + offset
        for index, value in enumerate(values):
            second += value
            send(second, {"Lines": {number: {"Sectors": {str(index): {"Value": value}}}}})
    live = store.snapshot()
    for number, _, offset in drivers:
        send(180 + offset, {"Lines": {number: {
            "NumberOfLaps": 2, "LastLapTime": {"Value": 90},
        }}})
    return live, store.snapshot()


def target_lap2_selected(snapshot):
    laps = _live_analysis_laps(
        snapshot, quick_lap_threshold=1.1, min_clean_laps=1,
        use_sector_boundary_traffic=True,
        min_gap_ahead_seconds=4, min_gap_behind_seconds=2,
    )
    return not laps.loc[laps["Driver"].eq("TARGET") & laps["LapNumber"].eq(2)].empty


def test_reducer_delayed_sectors_cannot_change_traffic_or_mc_clean_selection():
    live, delayed = reducer_traffic_snapshot("delayed")
    _, missing = reducer_traffic_snapshot("missing")
    _, traffic = reducer_traffic_snapshot("traffic")
    truthful_gap = _live_traffic_gaps(traffic, force_sector=True).set_index(["Driver", "LapNumber"])
    assert truthful_gap.loc[("TARGET", 2), "TrafficGapAheadSeconds"] == pytest.approx(1)
    assert not target_lap2_selected(traffic)
    # Previously the delayed 20/30/40 tuple became TARGET lap 2, changed this
    # minimum to 5s, and passed the 4s clean-air threshold. It remains display
    # evidence only; the absence of supported lap-2 sectors must be preserved.
    assert [point.cumulative_time_s for point in live.provisional_sectors
            if point.driver_code == "TARGET"] == [110, 140, 180]
    assert not any(point.driver_code == "TARGET" and point.lap_number == 2
                   for point in delayed.completed_sectors)
    assert delayed.completed_sectors == missing.completed_sectors
    pd.testing.assert_frame_equal(
        _live_traffic_gaps(delayed, force_sector=True),
        _live_traffic_gaps(missing, force_sector=True),
    )
    assert not target_lap2_selected(delayed)
    payload = _serializable_live_snapshot(delayed)
    assert payload == _serializable_live_snapshot(missing)
    assert any(lap.driver_code == "TARGET" and lap.lap_number == 2
               for lap in payload.completed_laps)  # Duration itself is supported.
    assert not target_lap2_selected(payload)


def test_reducer_supported_sector_interval_reaches_clean_laps_and_mc_payload():
    _, supported = reducer_traffic_snapshot("clean")
    gaps = _live_traffic_gaps(supported, force_sector=True).set_index(["Driver", "LapNumber"])
    assert gaps.loc[("TARGET", 2), "TrafficGapAheadSeconds"] == pytest.approx(5)
    assert target_lap2_selected(supported)
    assert target_lap2_selected(_serializable_live_snapshot(supported))
