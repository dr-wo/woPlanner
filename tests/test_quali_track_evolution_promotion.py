"""Production integration, provenance and finalized warning severity."""
from dataclasses import replace
import json

import pytest

from wodata.live import QualiTyreStint
from wodata.live.quali_models import QualiTrafficEvidence
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution_policy import POLICY_VERSION, QUALITY_VERSION, quality
from woplanner.quali.track_evolution_presentation import display_messages
from test_quali_service import driver, raw, with_test_pace_reference
from test_quali_shadow_track_evolution import candidate, fit, broad_reference


def test_default_production_broad_admission_without_traffic():
    service = with_test_pace_reference(QualiLiveService())
    for lap, time in ((0, 100.), (1, 104.), (2, 103.)):
        d = replace(driver(1, 100., laps=lap), last_lap_s=time,
                    tyre_history=(QualiTyreStint('SOFT', 0, True, lap),))
        service.consume(raw([d]))
    result = service.snapshot.track_evolution_result
    assert service.shadow_track_evolution is None
    assert not service._track_samples  # no auxiliary traffic result was fabricated
    assert result['support'] == {'drivers': 1, 'fit_observations': 2, 'observations': 2}
    assert result['estimate_source'] == 'LIVE_PER_DRIVER'
    assert result['live_estimate_s_per_global_push'] == pytest.approx(-1.)
    assert service.snapshot.cutoff.track_rate_s_per_field_push == result['estimate_s_per_global_push']
    assert all(s.clean_air_at_completion is None for s in service.track_evolution_policy.samples.values())
    assert result['schema_version'] == 2 and not result['shadow_only']
    assert result['policy_version'] == POLICY_VERSION
    assert result['quality_threshold_version'] == QUALITY_VERSION
    assert result['thresholds_calibrated'] is False
    before = service.snapshot
    service.set_clean_air_gap_s(30)
    assert service.snapshot.track_evolution_diagnostic is before.track_evolution_diagnostic
    assert service.snapshot.track_evolution_result == before.track_evolution_result


def test_publication_invalidation_replaces_installation_without_rewriting_history():
    service = QualiLiveService()
    policy = service.track_evolution_policy
    key = candidate(policy, time=100., install=1)
    candidate(policy, lap=2, time=99.9, install=1)
    candidate(policy, lap=3, time=99.8, install=2)
    before = service._build_snapshot()
    evidence = QualiTrafficEvidence('1', 'completion_invalidated', None, 1, key)
    after = service.consume(replace(raw([]), traffic_evidence_sequence=(evidence,)))
    assert [s.lap_number for s in before.track_evolution_diagnostic.observations] == [1,3]
    assert [s.lap_number for s in after.track_evolution_diagnostic.observations] == [2,3]
    assert [s.global_push_index for s in after.track_evolution_diagnostic.observations] == [2,3]
    assert after.track_evolution_result['global_push_index'] == 3
    assert after.cutoff.track_rate_s_per_field_push == after.track_evolution_diagnostic.aggregate_slope_s_per_push
    exported=service.track_evolution_payload()
    assert exported['lifetime_events'][-1]['as_of'] == exported['as_of']
    assert exported['sample_provenance'][0]['revoked']


def test_missing_field_omission_is_information_and_never_retried():
    service=QualiLiveService();p=service.track_evolution_policy
    for n in range(1,4):
        candidate(p, number=n, lap=1, time=100.)
        candidate(p, number=n, lap=2, time=99.999)
    omitted=candidate(p, number=4, lap=1, best=None)
    first=service.track_evolution_payload()
    assert first['status']=='LIVE_SUPPORTED'
    assert 'FIELD_REFERENCE_UNAVAILABLE' in first['information']
    assert first['attrition']['pace_gross_sanity_rejected']==0
    candidate(p,number=4,lap=1,best=100.)
    assert omitted not in p.samples
    assert len(p.samples)==6


@pytest.mark.parametrize('prior',[0.,-.03])
def test_fallback_manual_and_loaded_fp_provenance(prior):
    s=QualiLiveService(track_rate_s_per_field_push=prior)
    p=s.track_evolution_payload()
    assert p['status']=='FALLBACK' and p['live_estimate_s_per_global_push'] is None
    assert p['estimate_s_per_global_push']==prior
    assert p['fallback_provenance']=={'kind':'CONFIGURED_DEFAULT','reference':None}
    assert 'configured default' in display_messages(p)[0]['explanation']
    s.set_fp_track_rate(-.02,loaded_fp=True,reference='2026/R14/FP3')
    p=s.track_evolution_payload()
    assert p['fallback_provenance']=={'kind':'LOADED_FP_PRIOR','reference':'2026/R14/FP3'}
    assert 'loaded fp prior' in display_messages(p)[0]['explanation']
    s.set_track_override(.1)
    p=s.track_evolution_payload()
    assert p['estimate_source']=='MANUAL_OVERRIDE' and p['estimate_s_per_global_push']==.1
    assert p['automatic_estimate_s_per_global_push']==-.02
    assert display_messages(p)==[{'category':'Insufficient live support','explanation':'No supported live fit is available; using the manual override.','severity':'fallback'}]
    s.set_track_override(None)
    assert s.snapshot.cutoff.track_rate_s_per_field_push==-.02


def test_measured_zero_is_live_and_context_cannot_downgrade():
    q=quality(fit([0.]*3), {},reference_population=['BEA','STR'])
    assert q['status']=='LIVE_SUPPORTED' and q['status_reasons']==[]
    q['information']+=['AUX_TRAFFIC_UNAVAILABLE','AUX_TRAFFIC_PENDING','FIELD_REFERENCE_UNAVAILABLE']
    q['estimate_source']='LIVE_PER_DRIVER'
    messages=display_messages(q)
    assert [m['category'] for m in messages]==['Reference data incomplete','Limited field representation']
    assert all(m['severity']=='information' for m in messages)


def test_display_priority_two_limit_and_traffic_merge():
    q={'status':'LIVE_LIMITED','status_reasons':['RECENT_ESTIMATE_JUMP','DOMINATED_BY_SINGLE_DRIVER','HIGH_DRIVER_DISAGREEMENT','SPARSE_SUPPORT'], 'information':['COHORT_REFERENCE_INCOMPLETE']}
    before=json.dumps(q)
    assert [m['category'] for m in display_messages(q)]==['Limited data support','Driver trends disagree']
    assert json.dumps(q)==before
    q['status_reasons'].remove('HIGH_DRIVER_DISAGREEMENT')
    assert [m['category'] for m in display_messages(q)]==['Limited data support','Recent estimate change']
    q={'status':'LIVE_SUPPORTED','status_reasons':[], 'information':['AUX_TRAFFIC_PENDING','AUX_TRAFFIC_UNAVAILABLE']}
    assert display_messages(q)==[{'category':'Traffic context unavailable','explanation':'Traffic context is unavailable or still pending for some observations; it is not required for the live estimate.','severity':'information'}]


def test_qt_surface_separates_manual_status_and_mapped_information():
    from PySide6 import QtWidgets
    from woplanner.quali.gui import QualiWindow
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window=QualiWindow();s=window.service;s.session_is_qualifying=True
    window._show_plan();dialog=window.plan_dialog
    assert 'Configured default' in dialog.track_estimated.text()
    assert 'Insufficient live support' in dialog.track_messages.text()
    s.set_track_override(.12);dialog.refresh_live_track()
    assert dialog.track_source.text()=='Manual override'
    assert 'manual override' in dialog.track_messages.text()
    detail=json.loads(dialog.track_result_detail.toPlainText())
    assert detail['status_reasons']==['NO_SUPPORTED_LIVE_FIT']
    assert 'NO_SUPPORTED_LIVE_FIT' not in dialog.track_messages.text()
    dialog.close();window.close();app.processEvents()
