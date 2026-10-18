"""Source-dated part evidence identifies epochs; generic clocks do not label them."""
from dataclasses import replace
from datetime import timedelta

import pytest

from wodata.live import LiveTimingController, QualiSessionStore
from woplanner.quali.service import QualiLiveService
from test_quali_review_corrections import NOW, message, clock, ingress
from test_quali_c3_ownership_reconciliation import setup, flag, business_run


SOURCES = ['signalr_live', 'f1_archive_replay']


def history(source, *, include_first=True):
    events = [(0, 350, 'CHEQUERED'), (1, 400, 'GREEN'), (2, 600, 'CHEQUERED')]
    if not include_first:
        events = events[1:]
    return message('RaceControlMessages', {'Messages': {str(i): {
        'Utc': (NOW+timedelta(seconds=t)).isoformat(timespec='seconds'),
        'Flag': f, 'Scope': 'Track'} for i,t,f in events}}, 610, source)


def harness(source, path='reducer'):
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store)) if path=='service' else None
    send = (lambda p: ingress(service,p)) if service else store.apply
    send(message('SessionInfo', {'Name':'Qualifying'}, 199, source))
    send(message('TimingData', {'SessionPart':2}, 200, source))
    send(clock(200.5, '08:00', True, source))
    return store, service, send


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer','service'])
def test_exact_delayed_q3_epoch_cannot_be_certified_to_displayed_q2(source,path):
    store,service,send = harness(source,path)
    snapshot = history(source)
    send(snapshot)
    send(clock(620,'09:20',True,source))
    assert ('2','CHEQUERED') not in store._race_control_owners
    metadata = replace(message('TimingData',{'SessionPart':3},390,source),
                       received_at_utc=NOW+timedelta(seconds=621))
    assert send(metadata).normal_finished
    assert store._race_control_owners[('2','CHEQUERED')]=='Q3'
    for received in (622,623):
        assert send(replace(snapshot,received_at_utc=NOW+timedelta(seconds=received))).normal_finished
        assert store._race_control_owners[('2','CHEQUERED')]=='Q3'
    if service:
        assert service.snapshot.session_part=='Q3' and service.snapshot.normal_finished


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('order', [
    ('history','activity','metadata'), ('metadata','history','activity'),
    ('activity','metadata','history'), ('history','metadata','activity')])
def test_unique_source_owners_agree_across_delivery_schedules(source,order):
    store,_,send = harness(source,'service')
    original = {'history':history(source),
                'activity':clock(620,'09:20',True,source),
                'metadata':message('TimingData',{'SessionPart':3},390,source)}
    for i,name in enumerate(order):
        send(replace(original[name],received_at_utc=NOW+timedelta(seconds=621+i)))
    assert store._race_control_owners[('0','CHEQUERED')]=='Q2'
    assert store._race_control_owners[('2','CHEQUERED')]=='Q3'
    assert store.snapshot().session_part=='Q3' and store.snapshot().normal_finished
    owners=dict(store._race_control_owners)
    send(replace(original['history'],received_at_utc=NOW+timedelta(seconds=630)))
    assert store._race_control_owners==owners


@pytest.mark.parametrize('source', SOURCES)
def test_reaffirmed_q2_epoch_preserves_previous_duplicate_invariance(source):
    a=business_run(source,False);b=business_run(source,True)
    assert a==b
    assert a[0][('2','CHEQUERED')]=='Q2'
    assert a[1].normal_finished and not a[2].normal_finished and not a[3].normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_future_green_without_complete_close_history_still_needs_part_identity(source):
    store,_,send=harness(source)
    send(history(source,include_first=False))
    send(clock(620,'09:20',True,source))
    assert ('2','CHEQUERED') not in store._race_control_owners
    assert send(message('TimingData',{'SessionPart':3},390,source)).normal_finished
    assert store._race_control_owners[('2','CHEQUERED')]=='Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_explicit_same_part_restart_identifies_later_epoch(source):
    store,_,send=harness(source)
    send(history(source))
    send(clock(620,'09:20',True,source))
    assert ('2','CHEQUERED') not in store._race_control_owners
    # A source-dated Q2 reaffirmation after the first finish identifies restart,
    # rather than guessing whether GREEN 400 means Q2 or Q3.
    assert send(message('TimingData',{'SessionPart':2},450,source)).normal_finished
    assert store._race_control_owners[('2','CHEQUERED')]=='Q2'
    before=store.snapshot()
    send(message('TimingData',{'SessionPart':2},450,source))
    assert store.snapshot()==before


@pytest.mark.parametrize('source', SOURCES)
def test_stale_and_unrelated_activity_cannot_create_identification(source):
    store,_,send=harness(source)
    send(history(source))
    send(clock(200.5,'09:20',True,source))
    send(message('DriverList',{'1':{'Tla':'AAA'}},620,source))
    send(message('SessionStatus',{'Status':'Started'},620,source))
    assert ('2','CHEQUERED') not in store._race_control_owners
    send(clock(620,'09:20',True,source))
    assert ('2','CHEQUERED') not in store._race_control_owners


@pytest.mark.parametrize('source', SOURCES)
def test_multiple_closed_epochs_do_not_follow_generic_clock(source):
    store,_,send=harness(source)
    send(history(source))
    send(flag(3,'GREEN',700,700.1,source))
    send(flag(4,'CHEQUERED',800,800.1,source))
    send(clock(820,'09:20',True,source))
    assert ('2','CHEQUERED') not in store._race_control_owners
    assert ('4','CHEQUERED') not in store._race_control_owners
    send(message('TimingData',{'SessionPart':3},390,source))
    assert store._race_control_owners[('2','CHEQUERED')]=='Q3'
    assert ('4','CHEQUERED') not in store._race_control_owners
    send(message('TimingData',{'SessionPart':3},750,source))
    assert store._race_control_owners[('4','CHEQUERED')]=='Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_timestamp_enrichment_and_same_second_distinct_events_keep_owners(source):
    store=QualiSessionStore();packet=setup(store,source)
    store.apply(packet);store.apply(clock(300.2,'07:58',True,source))
    store.apply(message('TimingData',{'SessionPart':3},300.5,source))
    assert not store.apply(clock(300.75,'13:00',False,source)).normal_finished
    enriched=message('RaceControlMessages',{'Messages':{'2':{
        'Utc':(NOW+timedelta(seconds=300)).isoformat(timespec='milliseconds')}}},300.85,source)
    assert not store.apply(enriched).normal_finished
    assert store._race_control_owners[('2','CHEQUERED')]=='Q2'
    assert store.apply(flag(3,'CHEQUERED',300,300.95,source)).normal_finished
    assert store._race_control_owners[('3','CHEQUERED')]=='Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_valid_clock_reset_is_part_specific_transition_evidence(source):
    store=QualiSessionStore();packet=setup(store,source)
    store.apply(packet);store.apply(clock(300.2,'07:58',True,source))
    store.apply(clock(301,'00:00',False,source))
    assert store.apply(clock(302,'13:00',False,source)).session_part=='Q3'
    assert not store.snapshot().normal_finished
    assert store.apply(flag(3,'CHEQUERED',400,400.1,source)).normal_finished
    assert store._race_control_owners[('3','CHEQUERED')]=='Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_coarse_history_across_known_boundary_waits_for_precise_source_enrichment(source):
    store=QualiSessionStore();packet=setup(store,source)
    store.apply(packet);store.apply(clock(300.2,'07:58',True,source))
    store.apply(message('TimingData',{'SessionPart':3},300.5,source))
    store.apply(clock(300.75,'13:00',False,source))
    # This snapshot has no original envelope publication for a new identity.
    # Its whole-second item time overlaps two source part intervals.
    ambiguous=replace(flag(3,'CHEQUERED',300,300.95,source),source_timestamp_utc=None)
    assert not store.apply(ambiguous).normal_finished
    assert ('3','CHEQUERED') not in store._race_control_owners
    assert store._race_control_owners[('2','CHEQUERED')]=='Q2'
    precise=message('RaceControlMessages',{'Messages':{'3':{
        'Utc':(NOW+timedelta(seconds=300.9)).isoformat(timespec='milliseconds')}}},301,source)
    assert store.apply(precise).normal_finished
    assert store._race_control_owners[('3','CHEQUERED')]=='Q3'
