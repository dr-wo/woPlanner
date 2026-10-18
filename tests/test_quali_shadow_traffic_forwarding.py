"""Exact-key passive traffic forwarding, including timer-only publications."""
from dataclasses import replace

import pytest

from wodata.live import QualiTyreStint
from wodata.live.quali_models import QualiCompletedLap, QualiTrafficEvidence
from woplanner.quali.physical_traffic import PhysicalLapTraffic, QUALIFYING_TRAFFIC_POLICY
from woplanner.quali.service import QualiLiveService
from woplanner.quali.traffic_finalisation import PendingTraffic
from test_quali_service import driver, raw, with_test_pace_reference


FIELDS = ('physical_mean_ahead_gap_s', 'ahead_coverage', 'traffic_status', 'traffic_method',
          'traffic_target_samples', 'traffic_ahead_samples', 'traffic_evaluator_version',
          'traffic_circuit_length_m', 'clean_air_at_completion', 'clean_air_gap_threshold_s')


class Traffic:
    policy = QUALIFYING_TRAFFIC_POLICY
    revision = 0
    changed_sample_times = changed_state_times = ()

    def __init__(self):
        self.result = PhysicalLapTraffic(9., .91, 'ok', 20, 18, 5000., 'physical_circular', 'test-v1')
        self.calls = 0

    def ingest(self, raw):
        pass

    def completion_bounds(self, key):
        return (0., 100.)

    def finalize(self, *args, **kwargs):
        self.calls += 1
        return self.result


def snapshot(time=90., stamp=90., *, laps=1, clock=None, evidence=()):
    d = replace(driver(1, 90., laps=laps), last_lap_s=time,
                tyre_history=(QualiTyreStint('SOFT', 0, True, 0),))
    e = QualiCompletedLap('Q1', '1', 1, time, stamp)
    return replace(raw([d]), completed_lap_sequence=(e,) if laps else (),
                   traffic_clock_time_s=clock, traffic_clock_kind='receive',
                   traffic_evidence_sequence=evidence), e.event_key


def setup(clock=100.):
    s = with_test_pace_reference(QualiLiveService(shadow_track_evolution=True))
    s._physical_traffic = Traffic()
    initial, _ = snapshot(laps=0, clock=clock)
    s.consume(initial)
    r, key = snapshot(clock=clock)
    s.consume(r)
    assert len(s._track_samples) == 1
    return s, key


def assert_copy(s, key, index=0):
    a, b = s.shadow_track_evolution.samples[key], s._track_samples[index]
    assert {f: getattr(a, f) for f in FIELDS} == {f: getattr(b, f) for f in FIELDS}


@pytest.mark.parametrize('clock', [None, 100.])
def test_initial_canonical_result_is_forwarded(clock):
    s, key = setup(clock)
    assert_copy(s, key)
    assert s.shadow_track_evolution_payload()['traffic']['unevaluated_candidates'] == 0
    assert s._physical_traffic.calls == int(clock is None)


def test_timer_finalisation_without_next_timing_publication():
    s, key = setup()
    s.advance_traffic_clock(102.)
    assert s._track_samples[0].clean_air_at_completion is True
    assert_copy(s, key)
    assert s._physical_traffic.calls == 1


def test_repeated_grace_revisions_both_clean_air_directions_without_publication():
    s, key = setup()
    s.advance_traffic_clock(102.)
    # Reproduce the old later-publication repair before stale clean -> dirty.
    s.shadow_track_evolution.finish_publication(s)
    for revision, gap in enumerate((1., 9., 2., 8.), 1):
        traffic = s._physical_traffic
        traffic.result = replace(traffic.result, mean_ahead_gap_s=gap, ahead_coverage=.8 + revision / 100,
                                 target_samples=20 + revision, evaluator_version=f'test-v{revision+1}')
        traffic.revision = revision
        traffic.changed_sample_times = (('1', 50.),)
        s.advance_traffic_clock(102. + revision * .3)
        assert s._track_samples[0].clean_air_at_completion is (gap > s.clean_air_gap_s)
        assert_copy(s, key)
    assert traffic.calls == 5


def collision():
    s, original = setup(None)
    old = s.shadow_track_evolution.samples[original]
    invalidation = QualiTrafficEvidence('1', 'completion_invalidated', None, 1, original)
    rollback, _ = snapshot(laps=0, evidence=(invalidation,))
    s.consume(rollback)
    replacement, second = snapshot(94., 194.)
    s.consume(replacement)
    assert original != second
    assert len(s._track_samples) == 1  # 94 is outside production, inside shadow field+7%.
    assert original in s.shadow_track_evolution.revoked
    assert s.shadow_track_evolution.samples[original] == old
    return s, original, second


def test_rollback_collision_cannot_borrow_original_traffic():
    s, first, second = collision()
    candidate = s.shadow_track_evolution.samples[second]
    assert candidate.physical_mean_ahead_gap_s is None
    assert candidate.clean_air_at_completion is None
    assert candidate.traffic_status == 'unknown'
    assert s.shadow_track_evolution_payload()['traffic']['unevaluated_candidates'] == 1


def test_later_exact_key_result_and_revisions_only_update_that_candidate():
    s, first, second = collision()
    old = s.shadow_track_evolution.samples[first]
    # The later canonical producer supplies the exact identity and its sample slot.
    s._track_samples.append(replace(s.shadow_track_evolution.samples[second], clean_air_gap_threshold_s=3.))
    pending = PendingTraffic(second, 'D1', 1, 1, 200., 'receive')
    for gap in (1., 9., 9., None):
        s._physical_traffic.result = replace(s._physical_traffic.result, mean_ahead_gap_s=gap,
                                              status='unknown' if gap is None else 'ok')
        s._publish_traffic(pending, 202., 'test revision')
        assert_copy(s, second, 1)
        assert s.shadow_track_evolution.samples[first] == old
        assert s.shadow_track_evolution_payload()['traffic']['unevaluated_candidates'] == 0


def test_forwarder_copies_canonical_clean_air_and_context_without_recomputation():
    s, key = setup()
    # Deliberately inconsistent synthetic context proves the copier does no evaluation.
    canonical = replace(s._track_samples[0], physical_mean_ahead_gap_s=99.,
                        clean_air_at_completion=False, clean_air_gap_threshold_s=123.)
    s._track_samples[0] = canonical
    before_calls = s._physical_traffic.calls
    s.shadow_track_evolution.update_traffic(key, canonical)
    assert_copy(s, key)
    assert s._physical_traffic.calls == before_calls


def test_identical_result_still_copies_latest_production_lifecycle_record():
    s, key = setup()
    s.advance_traffic_clock(102.)
    pending = s._pending_traffic[key]
    revision = s._track_samples_revision
    assert not s._publish_traffic(pending, 103., 'exceptional grace')
    assert s._track_samples_revision == revision
    row = next(row for row in s.shadow_track_evolution_payload()['sample_provenance']
               if row['completion_key'] == key)
    assert row['traffic_publication'] == s.traffic_finalisation_records[-1]
    assert row['traffic_publication'] is not s.traffic_finalisation_records[-1]
    s.reset()
    assert not s.shadow_track_evolution.traffic_results
    assert not s.shadow_track_evolution.traffic_publications
