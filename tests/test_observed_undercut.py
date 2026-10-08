from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from wodata.contracts import ModelSnapshot, ParameterEstimate
from wodata.live import CompletedLap, LiveSessionSnapshot
from woplanner.analysis.observed_undercut import (
    build_observed_undercut_estimates, interpret_mc_track,
)
from woplanner.analysis.service import (
    ModelSummary, _live_analysis_laps, _model_summary_from_snapshot, snapshot_as_of_leader_lap,
)
from woplanner.analysis.worker import _serializable_live_snapshot
from wostrategy.model.pace_contract import race_pace_semantics


def snapshot():
    laps = []
    for lap in range(1, 13):
        stint = (lap - 1) // 4 + 1
        laps.append(CompletedLap(
            "1", "AAA", "Team", lap, 96.0 - lap, lap * 90.0, 1, None,
            "MEDIUM" if stint == 1 else "HARD", float((lap - 1) % 4 + 1),
            stint, True, lap * 90.0, "1", lap in (5, 9), lap in (4, 8), True,
            10 if lap in (4, 5) else 20 if lap in (8, 9) else None,
        ))
    return replace(LiveSessionSnapshot.empty(), completed_laps=tuple(laps))


def change(snap, lap_number, **fields):
    return replace(snap, completed_laps=tuple(
        replace(lap, **fields) if lap.lap_number == lap_number else lap
        for lap in snap.completed_laps
    ))


@pytest.fixture
def traffic(monkeypatch):
    dirty = set()

    def gaps(snap, **kwargs):
        return pd.DataFrame([dict(
            Driver=lap.driver_code, LapNumber=lap.lap_number,
            TrafficGapAheadSeconds=0.1 if lap.lap_number in dirty else 10.,
            TrafficGapBehindSeconds=10., TrafficStatus="sector_fallback",
        ) for lap in snap.completed_laps])

    monkeypatch.setattr("woplanner.analysis.service._live_traffic_gaps", gaps)
    return dirty


def estimate(snap, **kwargs):
    return build_observed_undercut_estimates(
        snap, quick_lap_threshold=1.2, use_sector_boundary_traffic=True, **kwargs,
    )


def summary(rate=.02, reference=6.):
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    row = ParameterEstimate(
        "live-fit", "live-bank", "aggregate", 12, "track_rate", "R", None, None,
        None, -.03, rate, .08, "s/lap", "measured", 10, 2, 50., .2, 0., 0.,
        "input-hash", "config-hash", now,
    )
    model = ModelSnapshot(
        "live-fit", "live-bank", "aggregate", 12, (row,), "input-hash", "config-hash", now,
        contributing_sessions=("R",), semantics=race_pace_semantics(
            reference_compound="HARD", sample_bank_id="live-bank",
            architecture="live-retro-v1", fuel_reference=0., race_lap_reference=reference,
            fuel_track_identifiability="exact_linear_alias",
        ),
    )
    return _model_summary_from_snapshot(model)


def test_adjacent_stints_in_out_excluded_and_observation_sign(traffic):
    first, second = estimate(snapshot())
    assert (first.pre_reference_lap, first.post_reference_lap) == (3, 6)
    assert (second.pre_reference_lap, second.post_reference_lap) == (7, 10)
    assert first.reference_separation == 3
    assert first.observed_pace_delta_s == 3.
    assert first.duration_source == "unmodelled CompletedLap.lap_time_s"
    assert first.pre_lap_duration_s == 93.
    assert first.post_lap_duration_s == 90.


def test_invalid_first_post_lap_skipped(traffic):
    first = estimate(change(snapshot(), 6, is_valid=False))[0]
    assert first.post_reference_lap == 7


def test_nearest_clean_full_laps_using_existing_quick_and_traffic_flags(traffic):
    traffic.add(3)
    first = estimate(change(snapshot(), 6, lap_time_s=200.))[0]
    assert (first.pre_reference_lap, first.post_reference_lap) == (2, 7)


@pytest.mark.parametrize("missing", ["pre", "post", "both"])
def test_missing_references_stay_pending(traffic, missing):
    traffic.update(([1, 2, 3] if missing in ("pre", "both") else []) +
                   ([6, 7] if missing in ("post", "both") else []))
    first = estimate(snapshot())[0]
    assert first.status == "pending"
    assert first.observed_pace_delta_s is None
    assert first.reference_separation is None
    assert first.pending_reason


def test_references_do_not_cross_another_stop(traffic):
    traffic.update([6, 7])
    first, second = estimate(snapshot())
    assert first.post_reference_lap is None  # Cannot use lap 10 in the next stint.
    assert second.pre_reference_lap is None  # Cannot go back to lap 3.


def test_unchanged_stint_pit_visit_also_bounds_references(traffic):
    snap = snapshot()
    for number in range(9, 13):
        snap = change(snap, number, stint=2)
    traffic.update([6, 7])
    rows = estimate(snap)
    assert len(rows) == 1
    assert rows[0].post_reference_lap is None  # Cannot cross unchanged-tyre visit 8/9.


def test_no_tyre_change_visit_with_no_inferred_pit_flags_still_bounds_refs(traffic):
    snap = snapshot()
    for number in range(9, 13):
        snap = change(snap, number, stint=2)
    snap = change(snap, 8, pit_in=False)
    snap = change(snap, 9, pit_out=False)
    traffic.update([6, 7])
    assert estimate(snap)[0].post_reference_lap is None


def test_red_flag_or_unproved_stint_change_rejected(traffic):
    snap = snapshot()
    for number in (4, 5, 8, 9):
        snap = change(snap, number, racing_pit_stop_id=None)
    assert estimate(snap) == ()


@pytest.mark.parametrize("field", ["pit_in", "pit_out"])
def test_requires_both_pit_flags(traffic, field):
    rows = estimate(change(snapshot(), 4 if field == "pit_in" else 5, **{field: False}))
    assert [row.pit_in_lap for row in rows] == [8]


def test_same_compound_racing_replacement_is_eligible(traffic):
    snap = snapshot()
    snap = replace(snap, completed_laps=tuple(replace(lap, compound="MEDIUM")
                                              for lap in snap.completed_laps))
    assert len(estimate(snap)) == 2


def test_individual_classification_does_not_require_mc_runs_or_edges(traffic):
    snap = snapshot()
    mc = _live_analysis_laps(
        snap, quick_lap_threshold=1.2, min_clean_laps=4,
        exclude_stint_start_laps=1, exclude_stint_end_laps=1,
        use_sector_boundary_traffic=True,
    )
    assert mc.empty
    assert estimate(snap)[0].status == "available"


@pytest.mark.parametrize("rate", [.02, -.02, 0.])
def test_retro_track_sign_reference_and_canonical_snapshot_status(traffic, rate):
    # Real ModelSnapshot defaults to uppercase UPDATED, not a hand-written fixture.
    model = summary(rate=rate)
    first = estimate(snapshot(), model_summary=model)[0]
    track = first.track
    assert track.status == "available"
    assert track.pre_track_term_s == pytest.approx(rate * (3 - 6))
    assert track.post_track_term_s == pytest.approx(rate * (6 - 6))
    assert track.track_term_change_s == pytest.approx(rate * 3)
    assert first.observed_pace_delta_s == 3.  # Interpretation never corrects raw delta.
    assert track.analysis_id == "live-fit"
    assert track.sample_bank_id == "live-bank"
    assert track.config_hash == "config-hash"
    assert track.identifiability == "exact_linear_alias"
    assert first.to_record()["track"]["race_lap_reference"] == 6.


def test_track_change_uses_one_current_reference():
    a = interpret_mc_track(summary(reference=0), 3, 6)
    b = interpret_mc_track(summary(reference=10), 3, 6)
    assert a.track_term_change_s == b.track_term_change_s == pytest.approx(.06)
    assert a.pre_track_term_s != b.pre_track_term_s


def test_unsupported_architecture_is_unavailable(traffic):
    model = summary()
    manifest = dict(model.manifest)
    manifest["semantics"] = dict(manifest["semantics"], architecture="race-specific-v2")
    first = estimate(snapshot(), model_summary=ModelSummary(manifest, model.parameters))[0]
    assert first.observed_pace_delta_s == 3.
    assert first.track.status == "unavailable"
    assert first.track.reason == "Unsupported MC architecture"


def test_mismatched_rate_provenance_is_rejected():
    model = summary()
    wrong = tuple(dict(row, sample_bank_id="different-fit") for row in model.parameters)
    assert interpret_mc_track(ModelSummary(model.manifest, wrong), 3, 6).status == "unavailable"


def test_stale_snapshot_is_rejected():
    model = summary()
    manifest = dict(model.manifest, status="STALE")
    assert interpret_mc_track(ModelSummary(manifest, model.parameters), 3, 6).status == "unavailable"


def test_worker_serialization_and_historical_cut_keep_stop_evidence(traffic):
    import pickle
    snap = snapshot()
    carried = pickle.loads(pickle.dumps(_serializable_live_snapshot(snap)))
    assert estimate(carried) == estimate(snap)
    cut = snapshot_as_of_leader_lap(snap, 7)
    assert len(estimate(cut)) == 1
    assert estimate(cut)[0].racing_pit_stop_id == 10


def test_unknown_traffic_leaves_refs_pending(monkeypatch):
    from woplanner.analysis.service import _unknown_live_traffic
    monkeypatch.setattr("woplanner.analysis.service._live_traffic_gaps",
                        lambda snap, **kw: _unknown_live_traffic(snap, reason="existing boundary"))
    assert all(row.status == "pending" for row in estimate(snapshot()))


@pytest.mark.parametrize("name", ["Qualifying", "Q", "Practice 1", "FP3", "Sprint Shootout"])
def test_non_racing_session_is_not_an_observed_undercut(traffic, name):
    assert estimate(replace(snapshot(), session_info={"Name": name})) == ()


@pytest.mark.parametrize("name", ["Race", "R", "Sprint", "S"])
def test_racing_session_identity_is_supported(traffic, name):
    assert len(estimate(replace(snapshot(), session_info={"Name": name}))) == 2
