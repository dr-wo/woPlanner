from dataclasses import replace

import numpy as np
import pandas as pd

from wodata.live import QualiPitState, QualiRawDriverState, QualiRawSnapshot
from wodata.live.quali_models import QualiPositionObservation
from wostrategy.analysis.traffic import (
    TrafficCoveragePolicy, TrafficEvaluationConfig, evaluate_physical_traffic,
)
from woplanner.quali.physical_traffic import (
    QUALIFYING_TRAFFIC_POLICY, RollingQualiPhysicalTraffic,
)
from woplanner.quali.track_evolution import TrackEvolutionSample, build_track_evolution_diagnostic


def _field(candidate_eligible=True):
    rows = []
    for time in np.arange(0, 20.5, .5):
        rows.extend((
            {"Driver": "AAA", "SessionTime": time, "LapNumber": 5,
             "Speed": 180., "Distance": 500., "CandidateEligible": True},
            {"Driver": "BBB", "SessionTime": time, "LapNumber": 4,
             "Speed": 0., "Distance": 600., "CandidateEligible": candidate_eligible},
        ))
    return pd.DataFrame(rows)


def test_shared_physical_semantics_and_qualifying_ahead_only_policy():
    field = _field()
    race = evaluate_physical_traffic(field, config=TrafficEvaluationConfig(1000.),
                                     target_lap=("AAA", 5)).laps.iloc[0]
    quali = evaluate_physical_traffic(
        field, config=TrafficEvaluationConfig(1000., coverage_policy=QUALIFYING_TRAFFIC_POLICY),
        target_lap=("AAA", 5),
    ).laps.iloc[0]
    assert quali["TrafficStatus"] == "ok"
    assert race["TrafficStatus"] != "ok"  # Existing both-directions Race gate.
    assert quali["AheadCoverageFraction"] > .5
    assert quali["BehindCoverageFraction"] == 0
    assert np.isfinite(quali["PhysicalMeanTimeDeltaToDriverAhead"])

    strict = evaluate_physical_traffic(
        field, config=TrafficEvaluationConfig(1000., coverage_policy=TrafficCoveragePolicy(
            require_ahead=True, require_behind=False, min_ahead_coverage=1.0,
        )), target_lap=("AAA", 5),
    ).laps.iloc[0]
    assert strict["TrafficStatus"] != "ok"


def test_pit_side_candidate_is_excluded_but_on_track_out_car_can_be_ahead():
    config = TrafficEvaluationConfig(1000., coverage_policy=QUALIFYING_TRAFFIC_POLICY)
    excluded = evaluate_physical_traffic(_field(False), config=config,
                                         target_lap=("AAA", 5)).laps.iloc[0]
    out_car = evaluate_physical_traffic(_field(True), config=config,
                                        target_lap=("AAA", 5)).laps.iloc[0]
    assert excluded["TrafficStatus"] != "ok"
    assert np.isnan(excluded["PhysicalMeanTimeDeltaToDriverAhead"])
    assert out_car["TrafficStatus"] == "ok"


def test_rolling_adapter_does_not_infer_phase_from_structural_evidence_alone():
    adapter = RollingQualiPhysicalTraffic()
    for time in np.arange(0, 20.5, .5):
        target = QualiRawDriverState(
            "1", "AAA", "Team", speed_kph=180.,
            latest_telemetry_time_s=float(time), latest_position_time_s=float(time),
            current_lap_identity=5, structural_state=QualiPitState.ON_TRACK,
        )
        other = QualiRawDriverState(
            "2", "BBB", "Team", speed_kph=0.,
            latest_telemetry_time_s=float(time), latest_position_time_s=float(time),
            current_lap_identity=4, structural_state=QualiPitState.ON_TRACK,
            out_lap_confirmed=True,
        )
        adapter.ingest(replace(QualiRawSnapshot.empty(), drivers=(target, other)))
    traffic = adapter.finalize("AAA", 5)
    assert not traffic.available
    assert traffic.method == "PHYSICAL_CIRCULAR"
    assert traffic.mean_ahead_gap_s is None
    assert not adapter._lap_spans
    assert adapter.row_count == 82

    pit_adapter = RollingQualiPhysicalTraffic()
    for time in np.arange(0, 20.5, .5):
        pit_adapter.ingest(replace(QualiRawSnapshot.empty(), drivers=(
            replace(target, latest_telemetry_time_s=float(time),
                    latest_position_time_s=float(time)),
            replace(other, structural_state=QualiPitState.IN_PIT,
                    latest_telemetry_time_s=float(time),
                    latest_position_time_s=float(time)),
        )))
    assert not pit_adapter.finalize("AAA", 5).available


def test_packed_position_samples_are_retained_and_pit_side_points_excluded():
    driver = QualiRawDriverState(
        "1", "AAA", "Team", speed_kph=180.,
        latest_telemetry_time_s=0., latest_position_time_s=.5,
        current_lap_identity=5, structural_state=QualiPitState.ON_TRACK,
    )
    snapshot = replace(
        QualiRawSnapshot.empty(), drivers=(driver,), position_observations=(
            QualiPositionObservation("1", 0., "OnTrack", False),
            QualiPositionObservation("1", .25, "OnTrack", True),
            QualiPositionObservation("1", .5, "OnTrack", False),
        ),
    )
    adapter = RollingQualiPhysicalTraffic()
    adapter.ingest(snapshot)
    assert adapter.row_count == 3
    assert [row["CandidateEligible"] for row in adapter._rows["1"]] == [True, False, True]


def test_track_evolution_uses_only_covered_physical_gap_and_configured_gap():
    samples = tuple(TrackEvolutionSample(
        "Q2", "AAA", "Team", lap, 90. - lap / 10, index,
        "SOFT", 0, True, gap, coverage, status, tyre_install_index=index,
    ) for lap, index, gap, coverage, status in (
        (3, 1, 4., .8, "ok"), (6, 2, 4., .8, "ok"),
        (9, 3, 99., .1, "partial_coverage"),
    ))
    assert build_track_evolution_diagnostic(samples, clean_air_gap_s=3.).lap_count == 2
    assert build_track_evolution_diagnostic(samples, clean_air_gap_s=5.).lap_count == 0
    # A ranking-relative Stats gap is absent from the sample contract and cannot
    # make a lap clean when physical evidence is unavailable.
    unavailable = replace(samples[0], physical_mean_ahead_gap_s=None)
    assert build_track_evolution_diagnostic((unavailable, samples[1]),
                                             clean_air_gap_s=3.).lap_count == 0
