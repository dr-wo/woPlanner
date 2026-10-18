"""C3 certification must follow accepted activity, not duplicate flag ingress."""
from quali_publication_assertions import domain_snapshot
from dataclasses import replace
from datetime import timedelta

import pytest

from wodata.live import LiveTimingController, QualiSessionStore
from woplanner.quali.models import FinishedReason
from woplanner.quali.service import QualiLiveService
from test_quali_review_corrections import NOW, clock, ingress, message


SOURCES = ['signalr_live', 'f1_archive_replay']


def flag(index, kind, event_time, publication, source):
    return message('RaceControlMessages', {'Messages': {str(index): {
        'Utc': (NOW + timedelta(seconds=event_time)).isoformat(timespec='seconds'),
        'Flag': kind, 'Scope': 'Track'}}}, publication, source)


def setup(store, source, send=None):
    send = send or store.apply
    send(message('TimingData', {'SessionPart': 2}, 298, source))
    send(clock(298.5, '08:00', True, source))
    send(flag(1, 'GREEN', 299, 299.1, source))
    send(message('TimingData', {'SessionPart': 2}, 299.5, source))
    return flag(2, 'CHEQUERED', 300, 300.1, source)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
def test_exact_reviewer_activity_certifies_before_q3(source, path):
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store)) if path == 'service' else None
    send = (lambda p: ingress(service, p)) if service else store.apply
    if service:
        send(message('SessionInfo', {'Name': 'Qualifying'}, 297, source))
    chequered = setup(store, source, send)
    assert send(chequered).normal_finished
    assert ('2', 'CHEQUERED') not in store._race_control_owners
    assert send(clock(300.2, '07:58', True, source)).normal_finished
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    assert not send(message('TimingData', {'SessionPart': 3}, 300.5, source)).normal_finished
    assert not send(clock(300.75, '13:00', False, source)).normal_finished
    if service:
        assert service.snapshot.session_part == 'Q3'
        assert not service.snapshot.normal_finished


def business_run(source, duplicate):
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store), eliminated_counts={'Q2': 0})
    send = lambda p: ingress(service, p)
    send(message('SessionInfo', {'Name': 'Qualifying'}, 297, source))
    send(message('TimingData', {'SessionPart': 2, 'Lines': {'1': {
        'Position': 1, 'NumberOfLaps': 0, 'InPit': False, 'KnockedOut': False}}}, 298, source))
    chequered = setup(store, source, send)
    send(chequered)
    send(clock(300.2, '07:58', True, source))
    before = service.snapshot
    if duplicate:
        send(replace(chequered, received_at_utc=NOW+timedelta(seconds=300.3)))
        assert domain_snapshot(service.snapshot) == domain_snapshot(before)
    send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 1,
        'LastLapTime': {'Value': '90'}, 'BestLapTime': {'Value': '90'}}}}, 300.4, source))
    q2 = service.snapshot
    send(message('TimingData', {'SessionPart': 3}, 300.5, source))
    paused = service.snapshot
    send(clock(300.75, '13:00', False, source))
    send(clock(301, '13:00', True, source))
    send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 2,
        'LastLapTime': {'Value': '89'}, 'BestLapTime': {'Value': '89'}}}}, 391, source))
    return (dict(store._race_control_owners), domain_snapshot(q2), domain_snapshot(paused), domain_snapshot(service.snapshot))


@pytest.mark.parametrize('source', SOURCES)
def test_duplicate_delivery_preserves_ownership_and_final_lap_business_state(source):
    a = business_run(source, False)
    b = business_run(source, True)
    assert a == b
    assert a[0][('2', 'CHEQUERED')] == 'Q2'
    assert a[1].drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    assert not a[2].normal_finished and not a[3].normal_finished
    assert a[3].drivers[0].finished_reason != FinishedReason.FINAL_LAP_COMPLETED


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('activity_before_flag', [False, True])
def test_activity_before_or_after_flag_has_same_owner(source, activity_before_flag):
    store = QualiSessionStore()
    packet = setup(store, source)
    activity = clock(300.2, '07:58', True, source)
    if activity_before_flag:
        store.apply(activity)
    store.apply(packet)
    if not activity_before_flag:
        store.apply(activity)
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 300.5, source)).normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_stale_duplicate_and_insufficient_activity_do_not_certify(source):
    store = QualiSessionStore()
    packet = setup(store, source)
    store.apply(packet)
    original_activity = store._part_activity_at
    for p in (clock(298.5, '07:58', True, source), clock(298.4, '07:58', True, source)):
        store.apply(p)
        assert store._part_activity_at == original_activity
        assert ('2', 'CHEQUERED') not in store._race_control_owners
    # Still before GREEN, so this is accepted but insufficient evidence.
    store.apply(clock(298.75, '07:59', True, source))
    assert ('2', 'CHEQUERED') not in store._race_control_owners
    store.apply(clock(300.2, '07:58', True, source))
    owner = dict(store._race_control_owners)
    store.apply(clock(300.2, '13:00', False, source))
    assert store._race_control_owners == owner
    assert store.snapshot().session_part == 'Q2'


@pytest.mark.parametrize('source', SOURCES)
def test_started_status_reconciles_sufficient_activity_without_new_flag(source):
    store = QualiSessionStore()
    store.apply(setup(store, source))
    assert ('2', 'CHEQUERED') not in store._race_control_owners
    store.apply(message('SessionStatus', {'Status': 'Started'}, 300.2, source))
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 300.5, source)).normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_certified_owner_survives_snapshots_and_utc_enrichment(source):
    store = QualiSessionStore()
    setup(store, source)
    bare = message('RaceControlMessages', {'Messages': {'2': {
        'Flag': 'CHEQUERED', 'Scope': 'Track'}}}, 300.1, source)
    store.apply(bare)
    store.apply(clock(300.2, '07:58', True, source))
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    store.apply(message('TimingData', {'SessionPart': 3}, 300.5, source))
    store.apply(clock(300.75, '13:00', False, source))
    enriched = flag(2, 'CHEQUERED', 300, 300.9, source)
    for _ in range(3):
        assert not store.apply(replace(enriched, source_timestamp_utc=None)).normal_finished
        assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    assert store.apply(flag(3, 'CHEQUERED', 300, 300.95, source)).normal_finished
    assert store._race_control_owners[('3', 'CHEQUERED')] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('running', [False, True])
def test_shortened_owned_finish_survives_later_paused_and_positive_clocks(source, running):
    store = QualiSessionStore()
    store.apply(setup(store, source))
    store.apply(clock(300.2, '07:58', True, source))
    assert store.apply(clock(301, '07:57', running, source)).normal_finished
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q2'
    store.apply(flag(3, 'GREEN', 302, 302.1, source))
    assert not store.snapshot().normal_finished
    store.apply(clock(302.2, '07:56', True, source))
    assert not store.snapshot().normal_finished
    assert not store.apply(flag(2, 'CHEQUERED', 300, 302.3, source)).normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_unanchored_historical_epoch_remains_provisional_until_metadata(source):
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 200, source))
    store.apply(clock(200.5, '08:00', True, source))
    store.apply(flag(1, 'GREEN', 400, 610, source))
    store.apply(flag(2, 'CHEQUERED', 600, 610.1, source))
    store.apply(clock(210, '07:50', True, source))
    assert ('2', 'CHEQUERED') not in store._race_control_owners
    assert store.apply(message('TimingData', {'SessionPart': 3}, 390, source)).normal_finished
    assert store._race_control_owners[('2', 'CHEQUERED')] == 'Q3'
    assert store.apply(clock(700, '04:39', True, source)).normal_finished
