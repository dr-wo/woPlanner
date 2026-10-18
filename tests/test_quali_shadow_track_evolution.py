"""Shadow causality, quality and production isolation (no synthetic clean-air)."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from wodata.live import QualiTyreStint
from wodata.live.quali_models import QualiCompletedLap, QualiTrafficEvidence
from woplanner.quali.physical_traffic import PhysicalLapTraffic
from woplanner.quali.service import QualiLiveService
from woplanner.quali.shadow_track_evolution import ShadowTrackEvolution, quality
from woplanner.quali.track_evolution import TrackEvolutionSample, build_track_evolution_diagnostic
from test_quali_service import driver, raw, with_test_pace_reference


def candidate(shadow, *, lap=1, time=100., best=100., number=1, install=None,
              fresh=True, compound='SOFT', operational=True, part='Q1'):
    d = replace(driver(number, time, laps=lap), tyre_history=(
        QualiTyreStint(compound, 0, fresh, lap if install is None else install),))
    e = QualiCompletedLap(part, str(number), lap, time, float(lap * 120 + number))
    snapshot = replace(raw([d], part=part), completed_lap_sequence=(e,))
    shadow.begin_publication(snapshot)
    shadow.consider(d, snapshot, operational, best, lap)
    shadow.refresh()
    return e.event_key


def fit(slopes, supports=None, traffic='unknown'):
    samples = []
    for i, slope in enumerate(slopes):
        n = supports[i] if supports else 2
        for j in range(n):
            x = i * 10 + j + 1
            samples.append(TrackEvolutionSample('Q1', f'D{i}', 'Team', x, 100+slope*x,
                           x, 'SOFT', 0, True, None, traffic_status=traffic, tyre_install_index=x))
    return build_track_evolution_diagnostic(tuple(samples), clean_air_gap_s=3, require_traffic=False)


def broad_reference():
    return {f'D{i}': 100+i for i in range(10)}


@pytest.mark.parametrize('time,best,reason', [
    (107., 100., None), (107.001, 100., 'GROSS_SANITY_REJECTED'),
    (100., None, 'FIELD_REFERENCE_UNAVAILABLE'), (100., 0., 'FIELD_REFERENCE_UNAVAILABLE'),
    (100., float('nan'), 'FIELD_REFERENCE_UNAVAILABLE'),
    (100., float('inf'), 'FIELD_REFERENCE_UNAVAILABLE'),
    (0., 100., 'INVALID_LAP_TIME'), (-1., 100., 'INVALID_LAP_TIME'),
    (float('inf'), 100., 'INVALID_LAP_TIME'),
])
def test_gross_guard_and_missing_reference(time, best, reason):
    s = ShadowTrackEvolution()
    candidate(s, time=time, best=best)
    assert s.decisions[-1]['reason'] == reason
    assert s.global_push_index == int(reason is None)
    assert len(s.payload()['diagnostic']['observations']) == int(reason is None)


@pytest.mark.parametrize('fresh,compound', [(False, 'SOFT'), (None, 'SOFT'), (True, None), (True, 'UNKNOWN'), (True, '')])
def test_tyre_requirements_keep_push_index(fresh, compound):
    s = ShadowTrackEvolution()
    candidate(s, fresh=fresh, compound=compound)
    assert s.global_push_index == 1
    assert s.payload()['support']['observations'] == 0
    assert s.payload()['attrition']['tyre_install_excluded_after_pace'] == 1


def test_no_own_best_or_traffic_gate_and_causal_field_guard():
    s = ShadowTrackEvolution()
    first = candidate(s, time=104., best=100.)
    second = candidate(s, lap=2, time=105., best=100.)
    assert s.payload()['status'] == 'LIVE_LIMITED'
    assert s.payload()['support']['observations'] == 2
    assert set(s.payload()['information']) == {'COHORT_IMBALANCE', 'COHORT_REFERENCE_INCOMPLETE', 'AUX_TRAFFIC_UNAVAILABLE'}
    assert all(v.physical_mean_ahead_gap_s is None for v in s.samples.values())
    candidate(s, lap=3, time=106., best=90.)
    assert s.decisions[-1]['reason'] == 'GROSS_SANITY_REJECTED'
    assert first in s.samples and second in s.samples  # no retroactive field gate


def test_exact_deletion_lifetime_and_later_installation_replacement():
    s = ShadowTrackEvolution()
    first = candidate(s, time=100., install=1)
    later = candidate(s, lap=2, time=99.9, install=1)
    candidate(s, lap=3, time=99.8, install=2)
    before = s.payload()
    assert [x['lap_number'] for x in before['diagnostic']['observations']] == [1, 3]
    s.deletion_notice('CAR 1 (D1) TIME 1:40.000 DELETED - TRACK LIMITS')
    after = s.payload()
    assert first in s.revoked and later not in s.revoked
    assert [x['lap_number'] for x in after['diagnostic']['observations']] == [2, 3]
    assert s.global_push_index == 3
    assert [x['global_push_index'] for x in after['diagnostic']['observations']] == [2, 3]
    assert after['support']['drivers'] == 1
    assert before['diagnostic']['observations'][0]['lap_number'] == 1


def test_exact_invalidation_and_best_register_change_not_deletion():
    s = ShadowTrackEvolution()
    key = candidate(s)
    candidate(s, lap=2, time=99.9)
    best_change = replace(raw([driver(1, 110, laps=2)]), completed_lap_sequence=())
    s.begin_publication(best_change)
    assert s.payload()['support']['observations'] == 2
    evidence = QualiTrafficEvidence('1', 'completion_invalidated', None, 1, key)
    s.begin_publication(replace(best_change, traffic_evidence_sequence=(evidence,)))
    assert s.payload()['support']['observations'] == 1
    assert s.payload()['status'] == 'FALLBACK'
    s.begin_publication(replace(best_change, traffic_evidence_sequence=(evidence,)))
    assert len(s.lifetime_events) == 1
    assert s.global_push_index == 2


def test_ambiguous_and_future_deletions_do_nothing():
    s = ShadowTrackEvolution()
    candidate(s)
    candidate(s, lap=2)
    s.deletion_notice('CAR 1 (D1) TIME 1:40.000 DELETED')
    assert not s.revoked
    assert s.lifetime_events[-1]['matches'] == 2
    s.deletion_notice('CAR 1 (D1) TIME 1:39.000 DELETED')
    candidate(s, lap=3, time=99.)
    s.deletion_notice('CAR 1 (D1) TIME 1:39.000 DELETED')
    assert not s.revoked
    s.deletion_notice('CAR 1 LAP 3 DELETED')
    assert s.lifetime_events[-1]['reason'] == 'NO_EXACT_DELETION_PATTERN'


def test_duplicate_nonadmitted_completion_also_makes_notice_ambiguous():
    s = ShadowTrackEvolution()
    candidate(s)
    candidate(s, lap=2, operational=False)
    s.deletion_notice('CAR 1 (D1) TIME 1:40.000 DELETED')
    assert not s.revoked


def test_quality_states_and_information_only_traffic():
    reference = broad_reference()
    supported = fit([-.01]*10)
    q = quality(supported, reference)
    assert q['status'] == 'LIVE_SUPPORTED' and q['reasons'] == []
    assert quality(fit([-.01]), reference)['status'] == 'LIVE_LIMITED'
    assert quality(fit([]), reference)['status'] == 'FALLBACK'
    # The exact same unknown/pending/available metadata cannot change quality.
    for state in ('unknown', 'pending', 'ok'):
        assert quality(fit([-.01]*10, traffic=state), reference) == q


@pytest.mark.parametrize('slopes,supports,code', [
    ([-.01], None, 'SPARSE_SUPPORT'),
    ([-.01]*3, [5,2,2], 'DOMINATED_BY_SINGLE_DRIVER'),
    ([-.04, .04], None, 'HIGH_DRIVER_DISAGREEMENT'),
    ([-.01]*3, None, 'COHORT_IMBALANCE'),
])
def test_individual_quality_reasons(slopes, supports, code):
    assert code in quality(fit(slopes, supports), broad_reference())['information' if code.startswith('COHORT_') else 'reasons']


def test_incomplete_cohort_and_frozen_q1_reference():
    s = ShadowTrackEvolution()
    candidate(s)
    service = SimpleNamespace(controller=None, raw_snapshot=raw([driver(1, 100)]),
                              _states={}, _track_samples=[], _fp_track_rate_s_per_field_push=0.,
                              track_override_s_per_field_push=None)
    s.finish_publication(service)
    assert s.q1_bests == {'D1': 100}
    s.begin_publication(raw([driver(2, 90)], part='Q2'))
    service.raw_snapshot = raw([driver(2, 90)], part='Q2')
    s.finish_publication(service)
    assert s.q1_bests == {'D1': 100}
    q = quality(fit([-.01]*10), {'D0': 100})
    assert 'COHORT_REFERENCE_INCOMPLETE' in q['information']
    assert q['metrics']['cohort_support']['unknown'] == 18
    assert q['metrics']['q1_ordering'] == ['D0']


def test_disagreement_needs_material_support_on_both_sides():
    assert 'HIGH_DRIVER_DISAGREEMENT' not in quality(fit([0., .2]), broad_reference())['reasons']
    assert 'HIGH_DRIVER_DISAGREEMENT' not in quality(fit([-.04, .04], [2,8]), broad_reference())['reasons']


def test_jump_persists_across_revisions_expires_on_tenth_push_and_invalidation_updates():
    s = ShadowTrackEvolution()
    first = candidate(s, time=100.)
    candidate(s, lap=2, time=99.9)
    candidate(s, lap=3, time=100.2)
    p = s.payload()
    assert 'RECENT_ESTIMATE_JUMP' in p['reasons']
    jump_index = s.global_push_index
    # Auxiliary revision is an update, but cannot restart the lifetime.
    s.update_traffic(first, replace(s.samples[first], traffic_status='pending'))
    assert 'RECENT_ESTIMATE_JUMP' in s.payload()['reasons']
    assert s._jump_index == jump_index
    for lap in range(4, 13):
        candidate(s, lap=lap, time=100., install=3)
        assert 'RECENT_ESTIMATE_JUMP' in s.payload()['reasons']
    candidate(s, lap=13, time=100., install=3)
    assert 'RECENT_ESTIMATE_JUMP' not in s.payload()['reasons']
    s.revoke(first, 'EXPLICIT_COMPLETION_INVALIDATION')
    assert 'RECENT_ESTIMATE_JUMP' in s.payload()['reasons']
    assert s._jump_index == 13


def test_fallback_and_manual_source_labelling():
    service = QualiLiveService(shadow_track_evolution=True, track_rate_s_per_field_push=-.03)
    s = service.shadow_track_evolution
    p = s.payload(service)
    assert p['status'] == 'FALLBACK'
    assert p['live_estimate_s_per_global_push'] is None
    assert p['estimate_source'] == 'CONFIGURED_FP_DEFAULT_PRIOR'
    assert p['estimate_s_per_global_push'] == -.03
    service.set_track_override(.12)
    p = s.payload(service)
    assert p['status'] == 'FALLBACK' and p['estimate_source'] == 'MANUAL_OVERRIDE'
    candidate(s)
    candidate(s, lap=2, time=99.9)
    p = s.payload(service)
    assert p['status'] == 'LIVE_LIMITED' and p['estimate_source'] == 'MANUAL_OVERRIDE'
    assert p['estimate_s_per_global_push'] == .12
    assert p['live_estimate_s_per_global_push'] == pytest.approx(-.1)
    service.set_track_override(None)
    assert s.payload(service)['estimate_source'] == 'LIVE_PER_DRIVER'
    service.reset()
    assert s.global_push_index == 0 and not s.completions and not s.revoked


def test_service_shadow_isolation_and_shared_canonical_fit(monkeypatch):
    services = [with_test_pace_reference(QualiLiveService(shadow_track_evolution=enabled))
                for enabled in (False, True)]
    for lap, time in [(0, 90), (1, 90), (2, 89.9), (3, 94.)]:
        d = replace(driver(1, time, laps=lap), tyre_history=(QualiTyreStint('SOFT', 0, True, lap),))
        snapshot = raw([d])
        for service in services:
            service.consume(snapshot)
        assert services[0].snapshot == services[1].snapshot
        assert services[0]._track_samples == services[1]._track_samples
    shadow = services[1].shadow_track_evolution
    assert shadow.payload()['support']['drivers'] == 1
    assert services[1].snapshot.track_evolution_diagnostic.driver_count == 1
    assert shadow is services[1].track_evolution_policy
    assert services[0].shadow_track_evolution is None
    calls = []
    import woplanner.quali.track_evolution as canonical
    original = canonical.fit_driver_track_progression
    def traced(x, y):
        calls.append(True)
        return original(x, y)
    monkeypatch.setattr(canonical, 'fit_driver_track_progression', traced)
    shadow._dirty = True
    shadow.refresh()
    assert calls


def test_missing_exact_completion_never_enters_shadow():
    s = ShadowTrackEvolution()
    d = driver(1, 100., laps=1)
    snapshot = replace(raw([d]), completed_lap_sequence=())
    s.begin_publication(snapshot)
    s.consider(d, snapshot, True, 100., 1)
    assert s.decisions[-1]['reason'] == 'UNIDENTIFIED_VALID_COMPLETION'
    assert s.global_push_index == 0


def test_auxiliary_numeric_evidence_preserved_without_admission_veto():
    s = ShadowTrackEvolution()
    key = candidate(s)
    candidate(s, lap=2, time=99.9)
    before = s.payload()
    s.update_traffic(key, replace(s.samples[key], traffic_status='ok', physical_mean_ahead_gap_s=1., ahead_coverage=.1))
    after = s.payload()
    assert after['live_estimate_s_per_global_push'] == before['live_estimate_s_per_global_push']
    assert after['status'] == before['status']
    assert after['diagnostic']['observations'][0]['physical_mean_ahead_gap_s'] == 1.
    assert after['diagnostic']['observations'][0]['clean_air_at_completion'] is None


def test_inactive_driver_never_enters_shadow():
    service = with_test_pace_reference(QualiLiveService(shadow_track_evolution=True))
    service.consume(raw([driver(1, 90., laps=0)]))
    service._states['1'] = replace(service._states['1'], active=False)
    service.consume(raw([driver(1, 90., laps=1)]))
    assert not service.shadow_track_evolution.decisions
    assert not service._track_samples


def test_modal_attrition_includes_unsupported_drivers():
    s = ShadowTrackEvolution()
    candidate(s, compound='SOFT')
    candidate(s, lap=2, compound='MEDIUM')
    p = s.payload()
    assert p['attrition']['modal_compound_selection'] == 1
    assert p['attrition']['unsupported_driver_observations'] == 1
    assert p['status'] == 'FALLBACK'


def test_debug_export_tracks_source_edits_without_a_new_publication():
    off = QualiLiveService()
    assert off.shadow_track_evolution_payload() is None
    service = QualiLiveService(shadow_track_evolution=True, track_rate_s_per_field_push=-.04)
    assert service.shadow_track_evolution_payload()['estimate_s_per_global_push'] == -.04
    service.set_fp_track_rate(-.06)
    assert service.shadow_track_evolution_payload()['estimate_s_per_global_push'] == -.06
    service.set_track_override(.1)
    assert service.shadow_track_evolution_payload()['estimate_source'] == 'MANUAL_OVERRIDE'
    assert not hasattr(service.snapshot, 'shadow_track_evolution')


def test_missing_driver_identity_rejected():
    s = ShadowTrackEvolution()
    d = replace(driver(1, 100., laps=1), driver_code='')
    snapshot = raw([d])
    s.begin_publication(snapshot)
    s.consider(d, snapshot, True, 100., 1)
    assert s.decisions[-1]['reason'] == 'UNIDENTIFIED_VALID_COMPLETION'
    assert s.global_push_index == 0


def test_cohort_missing_known_rank_is_incomplete_even_with_ten_references():
    p = quality(fit([-.01]*10), broad_reference(), reference_population=[f'D{i}' for i in range(11)])
    assert p['status'] == 'LIVE_SUPPORTED'
    assert p['reasons'] == []
    assert p['information'] == ['COHORT_REFERENCE_INCOMPLETE']
    assert p['metrics']['missing_q1_ranks'] == ['D10']
    assert p['metrics']['cohort_reference_count'] == 10
    assert 'D10' not in p['metrics']['q1_ordering']
