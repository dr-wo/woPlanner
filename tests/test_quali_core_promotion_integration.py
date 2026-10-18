"""Combined accepted publication and causal evidence contracts."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
import pytest
from wodata.live import QualiTyreStint
from woplanner.quali.service import QualiLiveService
from test_quali_service import driver, raw, with_test_pace_reference
from test_quali_shadow_track_evolution import candidate
from test_quali_third_historical_evidence import calibrated_field, target, business, withdraw_old
from test_quali_calibration_lifecycle import position


def domain(payload):
    result=deepcopy(payload)
    result['as_of']['publication_index'] = 0
    result['as_of']['published_at'] = None
    return result


def coherent(service):
    snap=service.snapshot; p=service.track_evolution_payload(detailed=False)
    raw=service.raw_snapshot
    assert p['as_of']['part']==raw.session_part
    assert p['as_of']['message_source']==getattr(raw,'message_source',None)
    assert p['as_of']['published_at']==(raw.last_message_at_utc.isoformat() if raw.last_message_at_utc else None)
    assert snap.track_evolution_result == p
    assert snap.track_evolution_diagnostic == service.track_evolution_policy._diagnostic
    assert snap.cutoff.track_rate_s_per_field_push == p['estimate_s_per_global_push']
    assert snap.automatic_track_rate_s_per_field_push == p['automatic_estimate_s_per_global_push']
    assert snap.track_source == {'LIVE_PER_DRIVER':'Live per-driver diagnostic (session-wide)', 'CONFIGURED_FP_DEFAULT_PRIOR':'Configured fallback', 'MANUAL_OVERRIDE':'Manual override'}[p['estimate_source']]


@pytest.mark.parametrize('alias',[False,True])
def test_supported_fit_atomic_and_duplicate_input_has_no_domain_side_effect(alias):
    s=with_test_pace_reference(QualiLiveService(shadow_track_evolution=alias));p=s.track_evolution_policy
    assert s.shadow_track_evolution is (p if alias else None)
    old=[]
    for lap,time in ((0,100.),(1,104.),(2,103.)):
        d=replace(driver(1,100.,laps=lap),last_lap_s=time,tyre_history=(QualiTyreStint('SOFT',0,True,lap),))
        update=raw([d]);before=p.publication_index
        with patch.object(p,'begin_publication',wraps=p.begin_publication) as begin, patch.object(p,'finish_publication',wraps=p.finish_publication) as finish:
            s.consume(update)
            assert begin.call_count==finish.call_count==1
        assert p.publication_index==before+1
        coherent(s);old.append(deepcopy(s.snapshot))
    assert old[1].track_evolution_result['status']=='FALLBACK'
    assert old[2].track_evolution_result['live_estimate_s_per_global_push']==pytest.approx(-1.)
    original=s.snapshot;payload=domain(s.track_evolution_payload());records=deepcopy(s.traffic_finalisation_records)
    s.consume(update)
    assert domain(s.track_evolution_payload())==payload
    assert s.snapshot.drivers==original.drivers
    assert s.snapshot.cutoff==original.cutoff
    assert s.traffic_finalisation_records==records
    assert old[1].track_evolution_result['status']=='FALLBACK'
    coherent(s)


@pytest.mark.parametrize('live',[False,True])
@pytest.mark.parametrize('receipt',[112.,113.,114.,114.1])
def test_fit_publication_coincides_with_calibration_revocation_and_deadlines(live,receipt):
    s=calibrated_field(live);p=s.track_evolution_policy
    # Use the accepted policy fixture to stage independent valid fit evidence.
    # Neither its drivers nor its completions overlap the physical target A.
    candidate(p,number=1,lap=1,time=100.);s.snapshot=s._build_snapshot()
    before=deepcopy(s.snapshot);assert before.track_evolution_result['status']=='FALLBACK'
    candidate(p,number=1,lap=2,time=99.9)
    initial=p.publication_index
    with patch.object(p,'begin_publication',wraps=p.begin_publication) as begin, patch.object(p,'finish_publication',wraps=p.finish_publication) as finish:
        withdraw_old(s,receipt)
        assert begin.call_count==finish.call_count==1
    assert p.publication_index==initial+1
    coherent(s)
    assert s.snapshot.track_evolution_result['live_estimate_s_per_global_push']==pytest.approx(-.1)
    assert before.track_evolution_result['status']=='FALLBACK'
    s.advance_traffic_clock(115.)
    assert target(s).traffic_circuit_length_m==pytest.approx(1000. if receipt<=114. else 1100.)
    assert target(s).clean_air_at_completion is (receipt>114.)
    locked=business(s);records=deepcopy(s.traffic_finalisation_records)
    withdraw_old(s,116.)
    assert business(s)==locked and s.traffic_finalisation_records==records
    assert p._diagnostic.aggregate_slope_s_per_push==pytest.approx(-.1)
    coherent(s)


@pytest.mark.parametrize('live',[False,True])
def test_canonical_fit_survives_physical_invalidation_recovery_and_hard_lock(live):
    s=calibrated_field(live);p=s.track_evolution_policy
    candidate(p,lap=1,time=100.);candidate(p,lap=2,time=99.9)
    s.advance_traffic_clock(112.);fit=deepcopy(s.track_evolution_payload()['diagnostic'])
    position(s,'A',105.25,113.,'OffTrack')
    assert target(s).traffic_status!='ok';coherent(s)
    position(s,'A',105.25,113.5)
    assert target(s).clean_air_at_completion is True
    s.advance_traffic_clock(114.);locked=target(s)
    position(s,'A',105.25,114.1,'OffTrack')
    assert target(s)==locked
    assert s.track_evolution_payload()['diagnostic']==fit
    coherent(s)


def test_segment_transition_intermediate_build_does_not_publish_new_diagnostic():
    s=calibrated_field();p=s.track_evolution_policy
    candidate(p,lap=1,time=100.);candidate(p,lap=2,time=99.9)
    s.snapshot=s._build_snapshot();old=s.snapshot.track_evolution_diagnostic
    observed=[];build=s._build_snapshot
    def observe():
        result=build()
        if s._track_publication_in_progress:
            observed.append(result)
            assert result.track_evolution_diagnostic is old
            assert result.track_evolution_result is None
        return result
    update=replace(s.raw_snapshot,session_part='Q2',session_part_established=True)
    with patch.object(s,'_build_snapshot',side_effect=observe),patch.object(p,'begin_publication',wraps=p.begin_publication) as begin:
        s.consume(update)
        assert begin.call_count==1
    assert observed
    assert s.snapshot.session_part=='Q2'
    assert not s._pending_traffic
    coherent(s)
