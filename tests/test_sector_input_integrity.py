"""Recovered opponent timing uses the existing four-boundary Sector contract."""
from dataclasses import replace
import pickle

import pandas as pd
import pytest

from wodata.live import CompletedLap
from woplanner.analysis.service import (
    LIVE_SECTOR_METHOD_VERSION, _live_traffic_gaps, _sector_boundary_traffic_gaps,
)
from woplanner.analysis.worker import _serializable_live_snapshot
from woplanner.live.telemetry import CausalAnalysisCut, _snapshot_at_cut
from test_live_analysis_boundary import committed_snapshot


def with_opponent(*, recovered=True, finish_offset=-1.5):
    snapshot = committed_snapshot()
    opponents = tuple(CompletedLap(
        "4", "NEAR", "Team", lap, 90., 90. * lap + finish_offset, 2, None,
        "MEDIUM", float(lap), 1, True, 90. * lap + finish_offset,
        traffic_sector_times_s=(30., 30., 30.) if recovered else None,
        traffic_recovered_sector="S1" if recovered else None,
    ) for lap in range(1, 4))
    return replace(snapshot, completed_laps=snapshot.completed_laps + opponents,
                   scheduled_total_laps=62)


def target_gap(snapshot):
    return _live_traffic_gaps(snapshot).set_index(["Driver", "LapNumber"]).loc[("TARGET", 2)]


def test_incomplete_near_opponent_no_longer_silently_skipped_after_recovery():
    baseline = target_gap(with_opponent(recovered=False))
    actual = target_gap(with_opponent())
    assert baseline.TrafficGapAheadSeconds == 5.
    assert actual.TrafficGapAheadSeconds == 1.5
    assert actual.TrafficGapBehindSeconds == baseline.TrafficGapBehindSeconds == 3.
    assert actual.TrafficMethodVersion == LIVE_SECTOR_METHOD_VERSION


def test_unrelated_missing_opponent_does_not_globally_invalidate_sector():
    baseline = committed_snapshot()
    actual = with_opponent(recovered=False, finish_offset=-40.)
    pd.testing.assert_frame_equal(_sector_boundary_traffic_gaps(actual),
                                  _sector_boundary_traffic_gaps(baseline))


def test_complete_sector_calculation_and_canonical_priority_unchanged():
    original = committed_snapshot()
    # A conflicting extra recovery must never overwrite a complete measured triple.
    supplied = replace(original, completed_laps=tuple(replace(
        lap, traffic_sector_times_s=(20., 40., 30.), traffic_recovered_sector="S1",
    ) for lap in original.completed_laps))
    pd.testing.assert_frame_equal(_sector_boundary_traffic_gaps(original),
                                  _sector_boundary_traffic_gaps(supplied))


def test_recovered_and_measured_triples_have_identical_boundary_numbers():
    recovered = with_opponent()
    prototype = next(s for s in recovered.completed_sectors if s.driver_code == "TARGET")
    sectors = tuple(replace(
        prototype, driver_number="4", driver_code="NEAR", lap_number=lap,
        sector=sector, segment_index=(lap - 1) * 3 + index,
        cumulative_time_s=lap * 90. - 1.5 - (3 - index) * 30.,
        session_time_seconds=lap * 90. - 1.5,
    ) for lap in range(1, 4) for index, sector in enumerate(("S1", "S2", "S3"), 1))
    measured = replace(with_opponent(recovered=False),
                       completed_sectors=recovered.completed_sectors + sectors)
    pd.testing.assert_frame_equal(
        _sector_boundary_traffic_gaps(recovered).sort_values(["Driver", "LapNumber"]).reset_index(drop=True),
        _sector_boundary_traffic_gaps(measured).sort_values(["Driver", "LapNumber"]).reset_index(drop=True),
    )


@pytest.mark.parametrize("values, valid, marker", [
    ((30., 30., float("nan")), True, "S1"), ((-1., 61., 30.), True, "S1"),
    ((30., 30., 31.), True, "S1"), ((30., 30., 30.), False, "S1"),
    ((30., 30., 30.), True, None),
])
def test_invalid_recovered_evidence_is_not_used(values, valid, marker):
    snapshot = with_opponent()
    snapshot = replace(snapshot, completed_laps=tuple(replace(
        lap, traffic_sector_times_s=values, is_valid=valid, traffic_recovered_sector=marker,
    ) if lap.driver_code == "NEAR" else lap for lap in snapshot.completed_laps))
    assert target_gap(snapshot).TrafficGapAheadSeconds == 5.


def test_old_prepared_sector_cache_is_recomputed_and_new_evidence_survives_ipc():
    snapshot = with_opponent()
    old_rows = _live_traffic_gaps(with_opponent(recovered=False))
    old_rows["TrafficMethodVersion"] = "sector-boundaries-v1"
    payload = replace(_serializable_live_snapshot(snapshot), traffic_rows=tuple(old_rows.to_dict("records")))
    payload = pickle.loads(pickle.dumps(payload))
    assert payload.scheduled_total_laps == 62
    assert target_gap(payload).TrafficGapAheadSeconds == 1.5


def test_recovery_never_crosses_source_time_cut():
    snapshot = with_opponent()
    earlier = _snapshot_at_cut(snapshot, CausalAnalysisCut(2, 178., 42, 1))
    assert not any(lap.driver_code == "NEAR" and lap.lap_number == 2
                   for lap in earlier.completed_laps)
    at_publication = _snapshot_at_cut(snapshot, CausalAnalysisCut(2, 178.5, 43, 1))
    recovered = next(lap for lap in at_publication.completed_laps
                     if lap.driver_code == "NEAR" and lap.lap_number == 2)
    assert recovered.traffic_sector_times_s == (30., 30., 30.)


def test_original_r16_packets_reproduce_alo24_first_false_accept():
    import json
    from datetime import datetime
    from pathlib import Path
    from wodata.live import LiveSessionStore, LiveTimingMessage

    fixture = json.loads((Path(__file__).parent / "fixtures/r16_alo24_sector_packets.json").read_text())
    store = LiveSessionStore()
    def dt(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    for event in fixture["messages"]:
        store.reduce(LiveTimingMessage(
            event["topic"], event["payload"], dt(event.get("source_timestamp_utc")),
            dt(event["received_at_utc"]), sequence=event["sequence"],
            session_time_seconds=event.get("session_time_seconds"),
        ))
        if event["sequence"] == 43407:
            at_finish = _live_traffic_gaps(store.snapshot())
            assert not ((at_finish.Driver == "ALO") & (at_finish.LapNumber == 24)).any()
    actual = store.snapshot()
    old = replace(actual, completed_laps=tuple(replace(
        lap, traffic_sector_times_s=None, traffic_recovered_sector=None,
    ) for lap in actual.completed_laps))
    near = next(lap for lap in actual.completed_laps
                if lap.driver_code == "STR" and lap.lap_number == 24)
    assert near.traffic_sector_times_s == pytest.approx((26.201, 35.284, 42.035))
    assert not any(sector.driver_code == "STR" and sector.lap_number == 24
                   for sector in actual.completed_sectors)
    for snapshot, expected in [(old, 6.043), (actual, 1.778)]:
        row = _live_traffic_gaps(snapshot).set_index(["Driver", "LapNumber"]).loc[("ALO", 24)]
        assert row.TrafficGapAheadSeconds == pytest.approx(expected, abs=1e-6)
        assert row.TrafficGapBehindSeconds == pytest.approx(6.135, abs=1e-6)
    assert actual.scheduled_total_laps == 56
