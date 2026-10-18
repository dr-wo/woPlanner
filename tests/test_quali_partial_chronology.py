"""Known partial source history cannot be silently treated as complete."""
from dataclasses import replace
from datetime import timedelta
import pytest
from test_quali_epoch_witness_scope import harness, SOURCES, IDENTITY
from test_quali_review_corrections import NOW, message, clock
from test_quali_c3_ownership_reconciliation import flag
from woplanner.quali.models import FinishedReason


def history(source, green=None, extra=None):
    entries = {'0': {'Flag': 'CHEQUERED', 'Utc': stamp(350)},
               '1': {'Flag': 'GREEN'}, '2': {'Flag': 'CHEQUERED', 'Utc': stamp(600)}}
    if green is not None:
        entries['1']['Utc'] = stamp(green)
    entries.update(extra or {})
    return replace(message('RaceControlMessages', {'Messages': entries}, 610, source), source_timestamp_utc=None)


def stamp(t):
    return (NOW + timedelta(seconds=t)).isoformat(timespec='milliseconds' if t % 1 else 'seconds')


def enrich(source, index, t):
    return replace(message('RaceControlMessages', {'Messages': {str(index): {'Utc': stamp(t)}}}, 620.75, source), source_timestamp_utc=None)


def downstream(source, store, service, send):
    send(flag(10, 'GREEN', 700, 700.1, source))
    send(message('TimingData', {'SessionPart': 3, 'Lines': {'1': {
        'Position': 1, 'NumberOfLaps': 0, 'InPit': False, 'KnockedOut': False}}}, 700.2, source))
    send(clock(701, '08:00', True, source))
    send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 1,
        'LastLapTime': {'Value': '70'}, 'BestLapTime': {'Value': '70'}}}}, 780, source))
    ordinary = service.snapshot.drivers[0].finished_reason if service else None
    if service: assert ordinary != FinishedReason.FINAL_LAP_COMPLETED
    send(flag(11, 'CHEQUERED', 800, 800.1, source))
    send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 2,
        'LastLapTime': {'Value': '70'}, 'BestLapTime': {'Value': '70'}}}}, 850, source))
    final = service.snapshot.drivers[0].finished_reason if service else None
    if service: assert final == FinishedReason.FINAL_LAP_COMPLETED
    return (dict(store._race_control_owners), store.snapshot().normal_finished,
            ordinary, final, service.snapshot.drivers if service else store.snapshot().drivers)


SCHEDULES = [
    ('complete', 'clock', 'metadata'),                          # A
    ('partial', 'enrich', 'clock', 'metadata'),                 # B
    ('partial', 'clock', 'enrich', 'metadata'),                 # C/E
    ('partial', 'clock', 'metadata', 'enrich'),                 # D
    ('metadata', 'partial', 'clock', 'enrich'),                 # metadata before snapshot
    ('partial', 'partial', 'clock', 'enrich', 'complete', 'metadata', 'complete'), # F
]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
def test_equivalent_source_schedules_converge(source, path):
    results = []
    for order in SCHEDULES:
        store, service, send = harness(source, path)
        send(message('TimingData', {'SessionPart': 2}, 370, source))
        packets = {'complete': history(source, 400), 'partial': history(source),
                   'enrich': enrich(source, 1, 400), 'clock': clock(620, '09:20', True, source),
                   'metadata': message('TimingData', {'SessionPart': 3}, 390, source)}
        seen = set()
        for i, action in enumerate(order):
            send(packets[action], 610 + i * 2)
            seen.add(action)
            if 'metadata' not in seen:
                assert IDENTITY not in store._race_control_owners
        assert store._race_control_owners == {('0', 'CHEQUERED'): 'Q2', IDENTITY: 'Q3'}
        assert store.snapshot().normal_finished
        assert store.snapshot().session_part == "Q3"
        if service:
            assert service.snapshot.session_part == "Q3"
            assert service.snapshot.normal_finished
        initial = (dict(store._race_control_owners), store.snapshot().normal_finished)
        results.append((initial, downstream(source, store, service, send)))
    assert all(r == results[0] for r in results)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
@pytest.mark.parametrize('extra', [{}, {'3': {'Flag': 'GREEN'}}, {'x': {'Flag': 'GREEN'}}])
def test_unresolved_green_stays_represented_but_transition_can_identify(source, path, extra):
    store, service, send = harness(source, path)
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    packet = history(source, extra=extra)
    send(packet)
    for _ in range(3):
        # A dated redelivery must not lend its publication to a historical item.
        send(replace(packet, source_timestamp_utc=NOW + timedelta(seconds=620)))
        send(clock(620, '09:20', True, source))
        assert IDENTITY not in store._race_control_owners
        assert ('1', 'GREEN') in store._race_control_undated
        assert store._race_control_publications[('1', 'GREEN')] is None
    assert store._race_control_owners[('0', 'CHEQUERED')] == 'Q2'
    send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    assert store._race_control_owners[IDENTITY] == 'Q3'
    assert store.snapshot().normal_finished
    before = dict(store._race_control_owners)
    send(enrich(source, 1, 400))
    send(enrich(source, 1, 400))
    assert store._race_control_owners == before


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
def test_untimed_restart_resolves_to_same_part_without_duplicate_finish(source, path):
    store, service, send = harness(source, path)
    send(message('TimingData', {'SessionPart': 2}, 450, source))
    send(history(source))
    send(clock(620, '09:20', True, source))
    assert IDENTITY not in store._race_control_owners
    send(enrich(source, 1, 400))
    assert store._race_control_owners[IDENTITY] == 'Q2'
    assert store.snapshot().normal_finished
    send(message('TimingData', {'SessionPart': 3}, 650, source))
    assert not store.snapshot().normal_finished
    send(clock(651, '13:00', False, source))
    assert not store.snapshot().normal_finished


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
@pytest.mark.parametrize('enrichment_order', [(1, 3), (3, 1)])
def test_unknown_closure_limits_transition_and_cache_reconciles(source, path, enrichment_order):
    store, service, send = harness(source, path)
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    send(history(source, extra={'3': {'Flag': 'CHEQUERED'}}))
    send(clock(620, '09:20', True, source))
    send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    assert IDENTITY not in store._race_control_owners
    assert ('3', 'CHEQUERED') in store._race_control_undated
    # Numeric index is not a license to exclude an undated closure's effects.
    for index in enrichment_order:
        send(enrich(source, index, 300 if index == 3 else 400))
    assert store._race_control_owners[IDENTITY] == 'Q3'
    assert store.snapshot().normal_finished


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kind', ['GREEN', 'CHEQUERED'])
def test_source_span_certifies_despite_unordered_unrelated_evidence(source, kind):
    store, service, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 650, source))
    send(history(source, 400, extra={'old': {'Flag': kind}}))
    send(clock(660, '08:40', True, source))
    assert store._race_control_owners[IDENTITY] == 'Q2'
    assert store.snapshot().normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_unknown_boundary_invalidates_cached_facts_before_certification(source):
    store, _, send = harness(source)
    send(history(source, 400))
    assert IDENTITY not in store._race_control_owners
    send(replace(message('RaceControlMessages', {'Messages': {'x': {'Flag': 'CHEQUERED'}}}, 620, source), source_timestamp_utc=None))
    send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    assert IDENTITY not in store._race_control_owners
    send(enrich(source, 'x', 300))
    assert store._race_control_owners[IDENTITY] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_coarse_closure_does_not_certify_transition_inside_its_interval(source):
    store, _, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 349, source))
    send(history(source, 400))
    send(clock(620, '09:20', True, source))
    send(message('TimingData', {'SessionPart': 3}, 350.5, source), 621)
    assert IDENTITY not in store._race_control_owners
    send(enrich(source, 0, 350.2))
    assert store._race_control_owners[IDENTITY] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_repeated_snapshot_and_enrichment_do_not_grow_identity_history(source):
    store, _, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    send(history(source))
    send(clock(620, '09:20', True, source))
    send(enrich(source, 1, 400))
    send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    before = tuple(len(x) for x in [store._race_control_flags, store._race_control_messages,
        store._race_control_publications, store._race_control_order, store._race_control_provisional])
    for _ in range(100):
        send(history(source))
        send(history(source, 400))
        send(enrich(source, 1, 400))
    assert tuple(len(x) for x in [store._race_control_flags, store._race_control_messages,
        store._race_control_publications, store._race_control_order, store._race_control_provisional]) == before
    assert store._race_control_owners[IDENTITY] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('transition', [350.5, 351])
def test_closure_precision_enrichment_schedules_converge(source, transition):
    results = []
    for initially_precise in [False, True]:
        store, _, send = harness(source)
        send(message('TimingData', {'SessionPart': 3}, transition, source))
        packet = history(source, 400)
        if initially_precise:
            packet.payload['Messages']['0']['Utc'] = stamp(350.9)
        send(packet)
        send(clock(620, '09:20', True, source))
        if not initially_precise:
            if transition < 351: assert IDENTITY not in store._race_control_owners
            send(enrich(source, 0, 350.9))
        assert store._race_control_owners.get(IDENTITY) == ('Q3' if transition == 351 else None)
        results.append(dict(store._race_control_owners))
    assert results[0] == results[1]


@pytest.mark.parametrize('source', SOURCES)
def test_historical_three_parts_with_undated_greens(source):
    store, _, send = harness(source)
    # Independent explicit ranges identify earlier finishes even when all
    # three GREEN occurrences remain unordered. No index-derived dates.
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    send(message('TimingData', {'SessionPart': 3}, 390, source))
    send(history(source, extra={'q1-start': {'Flag': 'GREEN'}, 'q2-restart': {'Flag': 'GREEN'}}))
    send(clock(620, '09:20', True, source))
    assert store._race_control_owners == {('0', 'CHEQUERED'): 'Q2', IDENTITY: 'Q3'}
    for index,t in [('q1-start',100), ('q2-restart',250),('1',400)]:
        send(enrich(source,index,t))
    assert store._race_control_owners == {('0', 'CHEQUERED'): 'Q2', IDENTITY: 'Q3'}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
def test_overlapping_dated_boundaries_cannot_hide_next_part_before_enrichment(source, path):
    results=[]
    for precise in [False, True]:
        from wodata.live import QualiSessionStore, LiveTimingController
        from woplanner.quali.service import QualiLiveService
        from test_quali_review_corrections import ingress
        store=QualiSessionStore()
        service=QualiLiveService(LiveTimingController(store=store)) if path=='service' else None
        send=(lambda p: ingress(service,p)) if service else store.apply
        send(message('SessionInfo',{'Name':'Qualifying'},199,source))
        send(message('TimingData',{'SessionPart':1},200,source))
        send(clock(200.5,'08:00',True,source))
        send(message('TimingData',{'SessionPart':2},590,source))
        entries={'0':{'Flag':'CHEQUERED','Utc':stamp(350)},
                 '1':{'Flag':'GREEN','Utc':stamp(400)},
                 '2':{'Flag':'CHEQUERED','Utc':stamp(600.5)},
                 '3':{'Flag':'GREEN','Utc':stamp(600.6)},
                 '4':{'Flag':'CHEQUERED','Utc':stamp(600.8 if precise else 600)}}
        send(replace(message('RaceControlMessages',{'Messages':entries},610,source),source_timestamp_utc=None))
        send(clock(620,'09:20',True,source))
        assert ('4','CHEQUERED') not in store._race_control_owners
        if not precise:send(enrich(source,4,600.8))
        send(message('TimingData',{'SessionPart':3},600.7,source))
        assert store._race_control_owners[('4','CHEQUERED')]=='Q3'
        assert store.snapshot().normal_finished
        if service:assert service.snapshot.normal_finished
        results.append(dict(store._race_control_owners))
    assert results[0]==results[1]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
def test_unknown_closure_can_fall_inside_target_precision_interval(source, path):
    results=[]
    for precise in [False, True]:
        store,service,send=harness(source,path)
        send(message('TimingData',{'SessionPart':2},600.5,source))
        entries={'0':{'Flag':'CHEQUERED','Utc':stamp(350)},
                 '1':{'Flag':'GREEN','Utc':stamp(400)},
                 '2':{'Flag':'CHEQUERED'},
                 '3':{'Flag':'CHEQUERED','Utc':stamp(600.8 if precise else 600)}}
        if precise:entries['2']['Utc']=stamp(600.6)
        send(replace(message('RaceControlMessages',{'Messages':entries},610,source),source_timestamp_utc=None))
        send(clock(620,'09:20',True,source))
        assert ('3','CHEQUERED') not in store._race_control_owners
        if not precise:
            send(enrich(source,2,600.6))
            send(enrich(source,3,600.8))
        send(message('TimingData',{'SessionPart':3},600.7,source),621)
        assert store._race_control_owners[('3','CHEQUERED')]=='Q3'
        assert store.snapshot().normal_finished
        if service:assert service.snapshot.normal_finished
        results.append(dict(store._race_control_owners))
    assert results[0]==results[1]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kind', ['GREEN', 'CHEQUERED'])
def test_overlapping_predecessors_keep_their_full_precision_frontier(source, kind):
    results=[]
    for precise in [False, True]:
        store,_,send=harness(source)
        if kind=='CHEQUERED':send(message('TimingData',{'SessionPart':3},350.7,source))
        else:send(message('TimingData',{'SessionPart':2},350.7,source))
        entries={'0':{'Flag':kind,'Utc':stamp(350.9 if precise else 350)},
                 '1':{'Flag':kind,'Utc':stamp(350.5)},
                 '2':{'Flag':'CHEQUERED','Utc':stamp(600)}}
        send(replace(message('RaceControlMessages',{'Messages':entries},610,source),source_timestamp_utc=None))
        send(clock(620,'09:20',True,source))
        assert IDENTITY not in store._race_control_owners
        if not precise:send(enrich(source,0,350.9))
        assert IDENTITY not in store._race_control_owners
        results.append(dict(store._race_control_owners))
    assert results[0]==results[1]
