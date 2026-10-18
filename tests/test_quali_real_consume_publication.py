"""A new canonical fit and causal traffic revocation share one real consume.

All candidates and invalidations originate in the production reducer. No policy
fixture may pre-admit or pre-refresh the candidate under test.
"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from wodata.live import LiveTimingMessage
import woplanner.quali.track_evolution_policy as policy_module

from test_quali_core_promotion_integration import coherent, domain
from test_quali_third_historical_evidence import (
    EPOCH, business, calibrated_field, send, target, timing, withdraw_old,
)


def fresh_tyres(service, stint):
    send(service, "TimingAppData", {"Lines": {"1": {"Stints": {
        str(stint): {"Compound": "SOFT", "New": "true", "StartLaps": 0},
    }}}}, 109., 110.)


@pytest.mark.parametrize("live", [False, True], ids=["archive", "live-receipt"])
@pytest.mark.parametrize("receipt", [112., 113., 114., 114.1],
                         ids=["normal-2s", "grace-3s", "hard-lock-4s", "after-lock-4.1s"])
def test_real_consume_admits_fits_and_revokes_atomically(live, receipt):
    service = calibrated_field(live)
    policy = service.track_evolution_policy
    send(service, "DriverList", {"1": {"Tla": "D1"}}, 109., 110.)
    timing(service, "1", {"NumberOfLaps": 0, "InPit": False,
                          "BestLapTime": {"Value": "10.000"}}, 109., 110.)
    fresh_tyres(service, 0)
    timing(service, "1", {"NumberOfLaps": 1, "LastLapTime": {"Value": "10.400"},
                          "Sectors": {"0": {"Value": "3.000"}}}, 109., 110.)
    fresh_tyres(service, 1)
    first_key = next(key for key, sample in policy.samples.items() if sample.driver == "D1")
    old_calibration = next(key for key in service._physical_traffic._span_history if key[1] == "C")
    target_key = next(key for key in service._pending_traffic if key[1] == "A")
    previous = service.snapshot
    frozen = deepcopy(previous)
    old_diagnostic = previous.track_evolution_diagnostic
    initial_index = policy.publication_index
    push_index = policy.global_push_index
    assert previous.track_evolution_result["status"] == "FALLBACK"
    assert old_diagnostic.aggregate_slope_s_per_push is None
    assert [s.lap_number for s in policy.samples.values() if s.driver == "D1"] == [1]

    # The same TimingData packet creates lap 2 and invalidates C's earlier lap.
    packet = LiveTimingMessage(
        "TimingData", {"Lines": {
            "1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "10.300"},
                  "Sectors": {"0": {"Value": "3.000"}}},
            "C": {"NumberOfLaps": 1},
        }}, EPOCH + timedelta(seconds=110.), EPOCH + timedelta(seconds=receipt),
        source="signalr_live" if live else "f1_archive_replay",
        session_time_seconds=110., playback_time_seconds=receipt,
        received_at_is_trusted=live, received_at_monotonic_s=receipt if live else None,
    )
    update = service.controller.store.apply(packet)
    new_event = next(e for e in update.completed_lap_sequence
                     if e.driver_number == "1" and e.lap_number == 2)
    new_key = new_event.event_key
    assert new_key not in policy.samples
    assert policy._diagnostic is old_diagnostic
    assert policy.publication_index == initial_index
    assert any(e.kind == "completion_invalidated" and e.completion_key == old_calibration
               for e in update.traffic_evidence_sequence)

    trace = []
    begin, finish = policy.begin_publication, policy.finish_publication
    consider, ingest, build = policy.consider, service._physical_traffic.ingest, service._build_snapshot
    fitter = policy_module.build_track_evolution_diagnostic

    def begin_guarded(raw):
        assert service._track_publication_in_progress
        assert policy._diagnostic is old_diagnostic and new_key not in policy.samples
        if live:
            assert raw.receive_time_trusted
            assert raw.traffic_clock_kind == "live_monotonic"
            assert raw.traffic_clock_time_s == receipt
        else:
            assert raw.message_source == "f1_archive_replay"
        trace.append("begin")
        return begin(raw)

    def ingest_guarded(raw):
        assert policy._diagnostic is old_diagnostic
        trace.append("ingest")
        return ingest(raw)

    def admit(*args):
        result = consider(*args)
        if args[0].driver_number == "1":
            assert new_key in policy.samples
            assert policy._diagnostic is old_diagnostic
            trace.append("admit")
        return result

    def fit(*args, **kwargs):
        assert service._track_publication_in_progress
        assert trace[-1] == "finish"
        assert new_key in policy.samples
        trace.append("fit")
        return fitter(*args, **kwargs)

    def finish_guarded(s):
        assert service._track_publication_in_progress
        trace.append("finish")
        return finish(s)

    def observe_build():
        result = build()
        if service._track_publication_in_progress:
            assert result.track_evolution_diagnostic is old_diagnostic
            assert result.track_evolution_result is None
        else:
            assert trace[-1] == "fit"
            trace.append("published")
        return result

    with patch.object(policy, "begin_publication", side_effect=begin_guarded) as started, \
         patch.object(policy, "finish_publication", side_effect=finish_guarded) as finished, \
         patch.object(policy, "consider", side_effect=admit), \
         patch.object(service._physical_traffic, "ingest", side_effect=ingest_guarded), \
         patch.object(service, "_build_snapshot", side_effect=observe_build), \
         patch.object(policy_module, "build_track_evolution_diagnostic", side_effect=fit) as fitted:
        service.consume(update)
        assert started.call_count == finished.call_count == fitted.call_count == 1
    assert trace == ["begin", "ingest", "admit", "finish", "fit", "published"]
    assert policy.publication_index == initial_index + 1
    assert policy.global_push_index == push_index + 1
    assert first_key in policy.samples and new_key in policy.samples
    assert old_calibration in policy.revoked
    assert len([event for event in policy.lifetime_events
                if event.get("completion_key") == old_calibration]) == 1
    assert old_calibration not in service._physical_traffic._span_history
    assert policy._diagnostic.aggregate_slope_s_per_push == pytest.approx(-.1)
    payload = service.track_evolution_payload(detailed=False)
    assert payload["status"] == "LIVE_LIMITED"
    assert payload["estimate_s_per_global_push"] == pytest.approx(-.1)
    assert payload["automatic_estimate_s_per_global_push"] == pytest.approx(-.1)
    assert service.snapshot.cutoff.track_rate_s_per_field_push == pytest.approx(-.1)
    coherent(service)

    publications = [record for record in service.traffic_finalisation_records
                    if record["key"] == target_key]
    assert publications[0]["delay_s"] == 2.
    assert publications[-1]["evidence_deadline_s"] == (receipt if receipt <= 114. else 112.)
    assert publications[-1]["reason"] == ("exceptional grace" if 112. < receipt <= 114. else "deadline")
    assert (target_key in service._pending_traffic) is (receipt < 114.)
    # Timer finalisation follows its own lifecycle, without consume envelopes.
    with patch.object(policy, "begin_publication", wraps=begin) as started, \
         patch.object(policy, "finish_publication", wraps=finish) as finished:
        service.advance_traffic_clock(115.)
        assert started.call_count == finished.call_count == 0
    assert target(service).traffic_circuit_length_m == pytest.approx(1000. if receipt <= 114. else 1100.)
    assert target(service).clean_air_at_completion is (receipt > 114.)
    assert not service._pending_traffic
    locked, records = business(service), deepcopy(service.traffic_finalisation_records)
    decisions, events = deepcopy(policy.decisions), deepcopy(policy.lifetime_events)
    payload = domain(service.track_evolution_payload())
    # Duplicate consume still gets its accepted envelope, but does no new fit.
    with patch.object(policy_module, "build_track_evolution_diagnostic", wraps=fitter) as fitted, \
         patch.object(policy, "begin_publication", wraps=begin) as started, \
         patch.object(policy, "finish_publication", wraps=finish) as finished:
        service.consume(update)
        assert fitted.call_count == 0
        assert started.call_count == finished.call_count == 1
    assert policy.publication_index == initial_index + 2
    assert policy.global_push_index == push_index + 1
    assert policy.decisions == decisions and policy.lifetime_events == events
    assert domain(service.track_evolution_payload()) == payload
    withdraw_old(service, 116.)
    assert business(service) == locked
    assert service.traffic_finalisation_records == records
    assert previous == frozen
    coherent(service)
