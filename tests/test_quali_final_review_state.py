"""C1-C3 regressions using source publication clocks and incremental ingress."""
from quali_publication_assertions import domain_snapshot
from dataclasses import replace
from datetime import timedelta
from copy import deepcopy
import pytest
from wodata.live import QualiSessionStore, LiveTimingMessage
from woplanner.quali.service import QualiLiveService
from test_quali_review_corrections import NOW, message, clock, ingress, recording_messages, finish_state


def session_clock(t, remaining, *, utc=None, running=True):
    payload={'Remaining':remaining,'Extrapolating':running}
    if utc is not None: payload['Utc']=(NOW+timedelta(seconds=utc)).isoformat()
    return LiveTimingMessage('ExtrapolatedClock',payload,None,NOW+timedelta(seconds=t),session_time_seconds=t,source='f1_archive_replay')


def part(store,n,t):
    return store.apply(message('TimingData',{'SessionPart':n},t))


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('late_mapping',[False,True])
def test_c1_cross_coordinate_stale_and_duplicates(reverse,late_mapping):
    store=QualiSessionStore();part(store,2,99)
    if not late_mapping: store.apply(session_clock(90,'08:10',utc=90))
    first=clock(100) if reverse else session_clock(100,'08:00')
    last=session_clock(580,'00:00',utc=580 if late_mapping else None,running=False) if reverse else clock(580,'00:00',False)
    store.apply(first)
    if late_mapping and not reverse: store.apply(session_clock(200,'06:20',utc=200))
    store.apply(last); before=finish_state(store.snapshot())
    stale=clock(110,'07:50') if reverse else session_clock(110,'07:50')
    assert finish_state(store.apply(stale))==before
    dup=clock(580,'13:00') if reverse else session_clock(580,'13:00')
    assert finish_state(store.apply(dup))==before
    assert part(store,2,590).session_part=='Q2'
    valid=clock(600,'13:00',False) if reverse else session_clock(600,'13:00',running=False)
    assert store.apply(valid).session_part=='Q3'


@pytest.mark.parametrize('conflict',[False,True])
def test_c1_incomparable_clock_cannot_infer_but_explicit_part_can(conflict):
    store=QualiSessionStore();part(store,2,90)
    if conflict: store.apply(session_clock(99,'08:01',utc=99))
    store.apply(session_clock(100,'08:00',utc=1100 if conflict else None))
    store.apply(clock(580,'00:00',False))
    incoming=session_clock(600,'13:00',running=False)
    assert store.apply(incoming).session_part=='Q2'
    assert part(store,3,601).session_part=='Q3'


def flag(t,publication,index):
    return message('RaceControlMessages',{'Messages':{str(index):{'Utc':(NOW+timedelta(seconds=t)).isoformat(timespec='seconds'),'Flag':'CHEQUERED','Scope':'Track'}}},publication)


def test_c3_owned_finish_cannot_cross_precision_overlap():
    store=QualiSessionStore();part(store,2,299)
    old=flag(300,300.1,1)
    assert store.apply(old).normal_finished
    assert not part(store,3,300.5).normal_finished
    assert not store.apply(clock(300.75,'13:00',False)).normal_finished
    assert not store.apply(old).normal_finished
    # Distinct source message in the same printed second belongs to Q3.
    current=flag(300,300.9,2)
    assert store.apply(current).normal_finished
    assert store.apply(clock(301,'12:59')).normal_finished
    assert store.apply(current).normal_finished


@pytest.mark.parametrize('driverlist_first',[False,True])
def test_c2_two_fragments_original_r17(driverlist_first):
    service=QualiLiveService();packets=recording_messages(); initial=packets[5]
    for p in packets[:5]:
        if p.topic!='DriverList': ingress(service,p)
    roster=next(p for p in packets if p.topic=='DriverList')
    if driverlist_first: ingress(service,roster)
    rows=list(deepcopy(initial.payload['Lines']).items())
    for group in (rows[:11],rows[11:]):
        ingress(service,replace(initial,payload={'SessionPart':2,'Lines':dict(group)}))
    if not driverlist_first: ingress(service,roster)
    assert sum(d.active for d in service.snapshot.drivers)==16
    assert service.snapshot.cutoff.cutoff_rank==10
    before=service.snapshot
    ingress(service,replace(initial,payload=deepcopy(initial.payload)))
    assert domain_snapshot(service.snapshot)==domain_snapshot(before)


@pytest.mark.parametrize('knocked_out',[False,True])
def test_c2_new_unknown_driver_resolves_after_bootstrap(knocked_out):
    service=QualiLiveService()
    ingress(service,message('SessionInfo', {'Name':'Qualifying'}, 98))
    ingress(service,message('TimingData',{'SessionPart':2,'Lines':{'1':{'KnockedOut':False}}},99))
    ingress(service,message('DriverList',{'2':{'Tla':'BBB'}},100))
    assert not next(d for d in service.snapshot.drivers if d.driver_number=='2').active
    packet=message('TimingData',{'Lines':{'2':{'KnockedOut':knocked_out}}},101)
    ingress(service,packet)
    assert next(d for d in service.snapshot.drivers if d.driver_number=='2').active is (not knocked_out)
    before=service.snapshot;ingress(service,packet);assert domain_snapshot(service.snapshot)==domain_snapshot(before)


def test_c2_later_elimination_and_local_transition_not_reactivated():
    service=QualiLiveService(eliminated_counts={'Q1':1,'Q2':1})
    ingress(service,message('SessionInfo', {'Name':'Qualifying'}, 98))
    ingress(service,message('TimingData',{'SessionPart':1,'Lines':{str(n):{'Position':n,'KnockedOut':False} for n in range(1,5)}},99))
    ingress(service,message('TimingData',{'SessionPart':2},100))
    assert sum(d.active for d in service.snapshot.drivers)==3
    assert service.snapshot.cutoff.cutoff_rank==2
    ingress(service,message('TimingData',{'Lines':{'4':{'KnockedOut':False}}},101))
    assert not next(d for d in service.snapshot.drivers if d.driver_number=='4').active
    ingress(service,message('TimingData',{'SessionPart':3},102))
    assert sum(d.active for d in service.snapshot.drivers)==2
    assert service.snapshot.cutoff.cutoff_rank==1
    ingress(service,message('TimingData',{'Lines':{'3':{'KnockedOut':False}}},103))
    assert not next(d for d in service.snapshot.drivers if d.driver_number=='3').active


def test_c2_resolved_late_join_driver_later_eliminated():
    service=QualiLiveService();ingress(service,message('SessionInfo', {'Name':'Qualifying'}, 98))
    ingress(service,message('TimingData',{'SessionPart':2,'Lines':{'1':{'KnockedOut':False}}},99))
    ingress(service,message('TimingData',{'Lines':{'1':{'KnockedOut':True}}},100))
    assert not service.snapshot.drivers[0].active


def test_c3_late_authoritative_part_binds_provisional_finish_before_clock():
    store=QualiSessionStore();part(store,2,200)
    history={'Messages':{'1':{'Utc':(NOW+timedelta(seconds=400)).isoformat(),'Flag':'GREEN'},
                         '2':{'Utc':(NOW+timedelta(seconds=600)).isoformat(),'Flag':'CHEQUERED'}}}
    assert store.apply(message('RaceControlMessages',history,610)).normal_finished
    assert part(store,3,390).normal_finished
    assert store.apply(clock(700,'04:39')).normal_finished
    assert part(store,3,390).normal_finished
    assert store.apply(message('RaceControlMessages',history,701)).normal_finished
