"""Complete payload equivalence against the pre-cache modal summary expression."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wodata.live.quali_models import QualiTrafficEvidence
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution_policy import TrackEvolutionPolicy
from wostrategy.analysis.pre_quali import dominant_track_compound
from test_quali_service import driver, raw
from test_quali_shadow_track_evolution import candidate


class UncachedPolicy(TrackEvolutionPolicy):
    def _diagnostic_nonmodal_count(self, diagnostic):
        # Exact uncached expression from accepted ce67da8 payload(). Keep the
        # oracle independent of the cache helper and its stored value/key.
        modal = {driver: dominant_track_compound(
            s.compound for s in diagnostic.observations if s.driver == driver)
            for driver in {s.driver for s in diagnostic.observations}}
        return sum(s.compound != modal[s.driver] for s in diagnostic.observations)


@pytest.fixture
def pair():
    return TrackEvolutionPolicy(), UncachedPolicy()


def equivalent(pair, service=None):
    result = None
    for detailed in (False, True):
        actual = pair[0].payload(service, detailed=detailed)
        expected = pair[1].payload(service, detailed=detailed)
        assert actual == expected  # All fields, including provenance and ledgers.
        result = actual
    return result


def add(pair, **kwargs):
    keys = [candidate(p, **kwargs) for p in pair]
    assert keys[0] == keys[1]
    equivalent(pair)
    return keys[0]


def supported(pair):
    for n in range(1, 4):
        add(pair, number=n, time=100.)
        add(pair, number=n, lap=2, time=99.99)
    result = equivalent(pair)
    assert result['status'] == 'LIVE_SUPPORTED'
    assert result['support'] == {'drivers': 3, 'observations': 6, 'fit_observations': 6}
    return result


def test_repeated_complete_payload_reuses_summary_and_keeps_snapshots(pair, monkeypatch):
    supported(pair)
    p = pair[0]
    before = equivalent(pair)
    saved = deepcopy(before)
    diagnostic = p._diagnostic
    def unexpected(*args):
        pytest.fail('Unchanged diagnostic recalculated modal compounds')
    monkeypatch.setattr('woplanner.quali.track_evolution_policy.dominant_track_compound', unexpected)
    for _ in range(4):
        assert equivalent(pair) == saved
        assert p._summary_diagnostic is diagnostic
    # Returned mutable containers must never be stored as the cached summary.
    before['driver_support'][0]['support'] = -1
    before['diagnostic']['observations'][0]['compound'] = 'BROKEN'
    assert equivalent(pair) == saved


def test_new_supported_fit_candidate_ties_compound_change_and_installations(pair):
    first = add(pair, lap=1, compound='SOFT')
    assert equivalent(pair)['status'] == 'FALLBACK'
    add(pair, lap=2, compound='SOFT', time=99.9)
    assert equivalent(pair)['support']['fit_observations'] == 2
    add(pair, lap=3, compound='MEDIUM', time=99.8)
    assert equivalent(pair)['attrition']['modal_compound_selection'] == 1
    add(pair, lap=4, compound='MEDIUM', time=99.7)
    tied = equivalent(pair)
    assert tied['diagnostic']['series'][0]['compound'] == 'MEDIUM'
    assert tied['attrition']['modal_compound_selection'] == 2
    # No new installation: candidate changes while fitting observations do not.
    add(pair, lap=5, install=4, compound='SOFT', time=99.6)
    assert equivalent(pair)['attrition']['installation_selection'] == 1
    old = deepcopy(tied)
    for p in pair:
        p.revoke(first, 'EXPLICIT_COMPLETION_INVALIDATION')
    after = equivalent(pair)
    assert after['attrition']['modal_compound_selection'] == 1
    assert tied == old


@pytest.mark.parametrize('metadata', [
    {'compound': None}, {'compound': 'UNKNOWN'}, {'compound': ''},
    {'fresh': False}, {'fresh': None}, {'best': None}, {'best': 0.},
    {'best': float('inf')},
])
def test_missing_invalid_metadata_and_reference_with_supported_fit(pair, metadata):
    supported(pair)
    add(pair, number=4, **metadata)
    result = equivalent(pair)
    assert result['support']['fit_observations'] == 6
    assert result['attrition']['tyre_install_excluded_after_pace'] == int('best' not in metadata)
    assert result['attrition']['field_reference_unavailable'] == int('best' in metadata)


def test_missing_installation_and_deletion_reselects_next_candidate(pair):
    first = add(pair, install=1, time=100.)
    add(pair, lap=2, install=1, time=99.9)
    add(pair, lap=3, install=2, time=99.8)
    for p in pair:
        # Imported candidates may lack an installation; refresh still excludes it.
        key = candidate(p, number=2)
        p.samples[key] = replace(p.samples[key], tyre_install_index=None)
        p._dirty = True
    before = equivalent(pair)
    saved = deepcopy(before)
    assert [s['lap_number'] for s in before['diagnostic']['observations']] == [1, 3]
    for p in pair:
        p.deletion_notice('CAR 1 (D1) TIME 1:40.000 DELETED - TRACK LIMITS')
    after = equivalent(pair)
    assert [s['lap_number'] for s in after['diagnostic']['observations']] == [2, 3]
    assert all(first in p.revoked for p in pair)
    assert before == saved
    evidence = QualiTrafficEvidence('1', 'completion_invalidated', None, 3,
                                   next(k for k in pair[0].samples if k[2] == 3))
    for p in pair:
        p.begin_publication(replace(raw([]), traffic_evidence_sequence=(evidence,)))
    assert equivalent(pair)['support']['fit_observations'] == 0


def test_publication_order_clock_source_quality_override_and_finalisation(pair):
    supported(pair)
    p = pair[0]
    identity = p._diagnostic
    before = equivalent(pair)
    saved = deepcopy(before)
    service = SimpleNamespace(controller=None, raw_snapshot=raw([driver(1, 100)]),
                              _states={}, _fp_track_rate_s_per_field_push=-.03,
                              track_override_s_per_field_push=None,
                              _track_fallback_provenance={'kind': 'LOADED_FP_PRIOR', 'reference': 'R17/FP3'})
    origin = datetime(2026, 10, 10, tzinfo=timezone.utc)
    for i in range(2):
        publication = replace(raw([driver(1, 100)], remaining=500-i),
                              last_message_at_utc=origin+timedelta(seconds=i),
                              message_source='f1_archive_replay')
        for policy in pair:
            policy.publication_context = {'sequence': 100+i}
            policy.begin_publication(publication)
        begun = equivalent(pair, service)
        assert begun['as_of']['published_at'] == publication.last_message_at_utc.isoformat()
        assert begun['as_of']['publication_index'] == 7+i
        assert begun['as_of']['sequence'] == 100+i
        assert begun['as_of']['message_source'] == 'f1_archive_replay'
        service.raw_snapshot = publication
        for policy in pair:
            policy.finish_publication(service)
        finished = equivalent(pair, service)
        assert finished['metrics']['q1_ordering'] == ['D1']
        assert p._diagnostic is identity
    service.track_override_s_per_field_push = .2
    assert equivalent(pair, service)['estimate_source'] == 'MANUAL_OVERRIDE'
    assert equivalent(pair, service)['estimate_s_per_global_push'] == .2
    service.track_override_s_per_field_push = None
    assert equivalent(pair, service)['estimate_source'] == 'LIVE_PER_DRIVER'
    key = next(iter(p.samples))
    for policy in pair:
        policy.update_traffic(key, replace(policy.samples[key], traffic_status='pending'),
                              publication={'status': 'pending', 'deadline': 2})
    assert equivalent(pair, service)['traffic']['status_counts']['pending'] == 1
    updated_identity = p._diagnostic
    for policy in pair:
        # Same sample, new finalisation metadata: must not require a new diagnostic.
        policy.update_traffic(key, policy.samples[key],
                              publication={'status': 'final', 'deadline': 4, 'reason': 'EOF'})
    final = equivalent(pair, service)
    assert p._diagnostic is updated_identity
    assert final['sample_provenance'][0]['traffic_publication'] == {
        'status': 'final', 'deadline': 4, 'reason': 'EOF'}
    assert before == saved


@pytest.mark.parametrize('alias', [False, True])
def test_service_segment_reset_fallback_and_alias_equivalence(alias):
    services = [QualiLiveService(shadow_track_evolution=alias, track_rate_s_per_field_push=-.03)
                for _ in range(2)]
    services[1].track_evolution_policy = UncachedPolicy()
    if alias:
        services[1].shadow_track_evolution = services[1].track_evolution_policy
    policies = tuple(s.track_evolution_policy for s in services)
    snapshots = []
    def check():
        assert services[0].track_evolution_payload() == services[1].track_evolution_payload()
        assert asdict(services[0].snapshot) == asdict(services[1].snapshot)
        for s in services:
            if alias:
                assert s.shadow_track_evolution is s.track_evolution_policy
                assert s.shadow_track_evolution_payload() == s.track_evolution_payload()
        for snapshot, saved in snapshots:
            assert asdict(snapshot) == saved
        snapshots.append((services[0].snapshot, deepcopy(asdict(services[0].snapshot))))
    check()
    add(policies)
    for s in services:
        s.consume(raw([]))
    check()
    assert services[0].track_evolution_payload()['estimate_source'] == 'CONFIGURED_FP_DEFAULT_PRIOR'
    for part in ('Q2', 'Q3'):
        for s in services:
            s.transition_to(part)
        add(policies, lap=2 if part == 'Q2' else 3, part=part, time=99.9)
        for s in services:
            s.consume(raw([], part=part))
        check()
        assert services[0].track_evolution_payload()['as_of']['part'] == part
        assert services[0].track_evolution_payload()['estimate_source'] == 'LIVE_PER_DRIVER'
    for s in services:
        s.set_track_override(.2)
        s.finalise()
    check()
    for s in services:
        s.reset()
        s.set_fp_track_rate(-.04, loaded_fp=True, reference='R17/FP3')
    check()
    p = services[0].track_evolution_payload()
    assert p['support'] == {'drivers': 0, 'fit_observations': 0, 'observations': 0}
    assert p['as_of'] == {}
    assert p['estimate_s_per_global_push'] == -.04
    assert p['manual_override_s_per_global_push'] is None
    assert p['fallback_provenance']['reference'] == 'R17/FP3'
    for s in services:
        s.set_track_override(.1)
    check()
    assert services[0].track_evolution_payload()['automatic_estimate_s_per_global_push'] == -.04
    for s in services:
        s.set_track_override(None)
    check()
    assert services[0].track_evolution_payload()['estimate_s_per_global_push'] == -.04
