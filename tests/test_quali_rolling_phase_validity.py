"""Causal boundary ingestion and retained-origin correctness."""
from dataclasses import replace

import numpy as np
import pytest

from wodata.live import QualiPitState, QualiRawDriverState, QualiRawSnapshot
from wodata.live.models import PublicationTime
from wodata.live.quali_models import QualiCompletedLap, QualiLapEvidence, QualiTrafficEvidence
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic


def ingest_field(*, unfinished=None, missing_start=None, state=None, outlap=None,
                 transition=None):
    adapter = RollingQualiPhysicalTraffic()
    starts = {"A": 0., "B": -1., "C": -2.}
    evidence, completed, traffic_evidence = [], [], []
    for time in np.arange(-2., 10.5, .5):
        drivers = []
        for code, start in starts.items():
            if time == start and code != missing_start:
                evidence.append(QualiLapEvidence("Q1", code, "lap_counter", 0, float(time)))
                traffic_evidence.append(QualiTrafficEvidence(
                    code, "lap_counter", PublicationTime("session", float(time)), 0))
            if not start <= time <= start + 10:
                continue
            if code == unfinished and time > 6:
                continue
            drivers.append(QualiRawDriverState(
                code, code, "Team", speed_kph=360., latest_position_time_s=float(time),
                latest_telemetry_time_s=float(time), current_lap_identity=1,
                structural_state=(transition if code == "B" and time == 2 and transition is not None
                                  else state if code == "B" and state is not None
                                  else QualiPitState.ON_TRACK),
                out_lap_confirmed=code == outlap,
            ))
            if time == start + 10 and code != unfinished:
                completed.append(QualiCompletedLap("Q1", code, 1, 10., float(time)))
                traffic_evidence.extend((
                    QualiTrafficEvidence(code, "lap_counter", PublicationTime("session", float(time)), 1),
                    QualiTrafficEvidence(code, "completion", PublicationTime("session", float(time)),
                                         1, completed[-1].event_key),
                ))
        adapter.ingest(replace(QualiRawSnapshot.empty(), drivers=tuple(drivers),
                               lap_evidence_sequence=tuple(evidence),
                               completed_lap_sequence=tuple(completed),
                               traffic_evidence_sequence=tuple(traffic_evidence)))
    return adapter


def test_completed_anchored_control_keeps_length_and_numeric_gap():
    adapter = ingest_field()
    traffic = adapter.finalize("A", 1)
    assert traffic.available
    assert traffic.mean_ahead_gap_s == pytest.approx(1.)
    assert traffic.circuit_length_m == pytest.approx(1000.)
    assert traffic.evaluator_version == "quali-physical-phase-v1"
    assert list(adapter._lap_spans) == [1000.]


@pytest.mark.parametrize("kwargs", [{"unfinished": "B"}, {"missing_start": "B"},
                                   {"outlap": "B"}, {"transition": QualiPitState.IN_LAP},
                                   {"transition": QualiPitState.PIT_LANE}])
def test_invalid_competitor_vetoes_traffic_but_not_valid_target_length(kwargs):
    adapter = ingest_field(**kwargs)
    traffic = adapter.finalize("A", 1)
    assert not traffic.available
    assert traffic.mean_ahead_gap_s is None
    assert list(adapter._lap_spans) == [1000.]
    adapter.finalize("B", 1)
    assert list(adapter._lap_spans) == [1000.]


def test_evicted_prefix_loses_anchor_and_cannot_enter_length_history(monkeypatch):
    monkeypatch.setattr("woplanner.quali.physical_traffic.ROLLING_TRAFFIC_SECONDS", 10.5)
    adapter = ingest_field()
    assert adapter._rows["B"][0]["SessionTime"] == -.5
    assert adapter._rows["B"][0]["LapStartTime"] is None
    assert not adapter.finalize("A", 1).available
    assert list(adapter._lap_spans) == [1000.]
    assert not adapter.finalize("B", 1).available
    assert list(adapter._lap_spans) == [1000.]


@pytest.mark.parametrize("state", [QualiPitState.IN_PIT, QualiPitState.PIT_LANE,
                                  QualiPitState.IN_LAP])
def test_explicit_pit_candidate_is_irrelevant(state):
    traffic = ingest_field(state=state).finalize("A", 1)
    assert traffic.available
    assert traffic.mean_ahead_gap_s == pytest.approx(2.)


def test_invalid_target_cannot_seed_length_history():
    adapter = ingest_field(missing_start="A")
    assert not adapter.finalize("A", 1).available
    assert not adapter._lap_spans


def test_wholly_evicted_on_track_car_retains_uncertainty(monkeypatch):
    monkeypatch.setattr("woplanner.quali.physical_traffic.ROLLING_TRAFFIC_SECONDS", .5)
    adapter = ingest_field(unfinished="B")
    assert len(adapter._rows["B"]) == 1
    assert adapter._rows["B"][0]["SessionTime"] == 6.
    assert adapter._rows["B"][0]["CandidateEligible"] is True
    # Supply the other two complete retained histories independently; stale B
    # must veto their otherwise valid numeric result rather than disappear.
    monkeypatch.setattr("woplanner.quali.physical_traffic.ROLLING_TRAFFIC_SECONDS", 240.)
    complete = ingest_field()
    assert complete.finalize("A", 1).available
    complete._rows["B"] = adapter._rows["B"]
    assert not complete.finalize("A", 1).available
