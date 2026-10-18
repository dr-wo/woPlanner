"""A completed-part reaffirmation cannot identify a subsequent GREEN epoch."""
from dataclasses import replace
from datetime import timedelta

import pytest

from wodata.live import LiveTimingController, QualiSessionStore
from woplanner.quali.models import FinishedReason
from woplanner.quali.service import QualiLiveService
from test_quali_review_corrections import NOW, clock, ingress, message
from test_quali_c3_ownership_reconciliation import flag, business_run
from test_quali_segment_epoch_ownership import history


SOURCES = ['signalr_live', 'f1_archive_replay']
IDENTITY = ('2', 'CHEQUERED')


def harness(source, path='service'):
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store)) if path == 'service' else None
    sequence = 0

    def send(packet, receipt=None):
        nonlocal sequence
        sequence += 1
        packet = replace(
            packet,
            received_at_utc=NOW + timedelta(seconds=receipt) if receipt is not None else packet.received_at_utc,
            received_at_monotonic_s=1000 + sequence if source == 'signalr_live' else None,
        )
        return ingress(service, packet) if service else store.apply(packet)

    send(message('SessionInfo', {'Name': 'Qualifying'}, 199, source))
    send(message('TimingData', {'SessionPart': 2}, 200, source))
    send(clock(200.5, '08:00', True, source))
    return store, service, send


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
@pytest.mark.parametrize('duplicate', [False, True])
@pytest.mark.parametrize('reaffirmation', [None, 300, 370])
def test_old_part_witness_waits_for_delayed_next_epoch(source, path, duplicate, reaffirmation):
    store, service, send = harness(source, path)
    if reaffirmation is not None:
        send(message('TimingData', {'SessionPart': 2}, reaffirmation, source))
    original = history(source)
    send(original)
    send(clock(200.5, '13:00', False, source), 611)  # duplicate source clock
    send(clock(200.4, '13:00', False, source), 612)  # stale source clock
    send(clock(620, '09:20', True, source))
    assert store.snapshot().session_part == 'Q2'
    assert store.snapshot().normal_finished
    assert IDENTITY not in store._race_control_owners
    assert store._race_control_owners[('0', 'CHEQUERED')] == 'Q2'
    if duplicate:
        send(original, 620.5)
        assert IDENTITY not in store._race_control_owners
    result = send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    assert (result.session_part, result.normal_finished) == ('Q3', True)
    assert store._race_control_owners[IDENTITY] == 'Q3'
    if duplicate:
        send(original, 622)
        send(original, 623)
    assert store.snapshot().normal_finished
    assert store._race_control_owners[IDENTITY] == 'Q3'
    assert store.snapshot().receive_time_trusted == (source == 'signalr_live')
    if service:
        assert service.snapshot.session_part == 'Q3' and service.snapshot.normal_finished


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('path', ['reducer', 'service'])
@pytest.mark.parametrize('duplicate', [False, True])
def test_after_green_q2_reaffirmation_still_certifies_without_redelivery(source, path, duplicate):
    store, service, send = harness(source, path)
    send(message('TimingData', {'SessionPart': 2}, 298, source))
    send(clock(298.5, '08:00', True, source))
    send(flag(1, 'GREEN', 299, 299.1, source))
    send(message('TimingData', {'SessionPart': 2}, 299.5, source))
    finish = flag(2, 'CHEQUERED', 300, 300.1, source)
    send(finish)
    send(clock(300.2, '07:58', True, source))
    assert store._race_control_owners[IDENTITY] == 'Q2'
    assert store.snapshot().normal_finished
    if duplicate:
        send(finish, 300.3)
    assert not send(message('TimingData', {'SessionPart': 3}, 300.5, source)).normal_finished
    assert not send(clock(300.75, '13:00', False, source)).normal_finished
    assert store._race_control_owners[IDENTITY] == 'Q2'
    if service:
        assert not service.snapshot.normal_finished


SCHEDULES = [
    ('metadata', 'history', 'activity'),
    ('history', 'metadata', 'activity'),
    ('history', 'activity', 'metadata'),
    ('activity', 'history', 'metadata'),
    ('history', 'history', 'activity', 'metadata'),
    ('history', 'activity', 'metadata', 'history'),
]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('metadata_time', [390, 450])
def test_delivery_schedules_preserve_owners_and_downstream_laps(source, metadata_time):
    results = []
    for order in SCHEDULES:
        store, service, send = harness(source)
        send(message('TimingData', {'SessionPart': 2}, 370, source))
        packets = {'history': history(source), 'activity': clock(620, '09:20', True, source),
                   'metadata': message('TimingData', {'SessionPart': 3}, metadata_time, source)}
        for i, name in enumerate(order):
            send(packets[name], 621 + i)
        assert store._race_control_owners == {('0', 'CHEQUERED'): 'Q2', IDENTITY: 'Q3'}
        assert service.snapshot.normal_finished
        # A supported same-part restart provides common downstream lap history
        # after all schedules have established Q3; no late-join lap is invented.
        send(flag(3, 'GREEN', 700, 700.1, source))
        send(message('TimingData', {'SessionPart': 3, 'Lines': {'1': {
            'Position': 1, 'NumberOfLaps': 0, 'InPit': False, 'KnockedOut': False}}}, 700.2, source))
        send(clock(701, '08:00', True, source))
        send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 1,
            'LastLapTime': {'Value': '70'}, 'BestLapTime': {'Value': '70'}}}}, 780, source))
        ordinary = service.snapshot.drivers[0].finished_reason
        assert ordinary != FinishedReason.FINAL_LAP_COMPLETED
        send(flag(4, 'CHEQUERED', 800, 800.1, source))
        send(message('TimingData', {'Lines': {'1': {'NumberOfLaps': 2,
            'LastLapTime': {'Value': '70'}, 'BestLapTime': {'Value': '70'}}}}, 850, source))
        final = service.snapshot.drivers[0].finished_reason
        assert final == FinishedReason.FINAL_LAP_COMPLETED
        assert store._race_control_owners[('4', 'CHEQUERED')] == 'Q3'
        results.append((dict(store._race_control_owners), service.snapshot.normal_finished, ordinary, final))
    assert all(result == results[0] for result in results)
    # Preserve the original genuine Q2 final lap and ordinary Q3 completion too.
    no_duplicate, duplicate = business_run(source, False), business_run(source, True)
    assert no_duplicate == duplicate
    assert no_duplicate[1].drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    assert no_duplicate[3].drivers[0].finished_reason != FinishedReason.FINAL_LAP_COMPLETED


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('reaffirmation,expected', [(None, None), (370, None), (450, 'Q2')])
def test_same_part_restart_requires_identifying_evidence(source, reaffirmation, expected):
    store, _, send = harness(source)
    if reaffirmation is not None:
        send(message('TimingData', {'SessionPart': 2}, reaffirmation, source))
    send(history(source))
    send(clock(620, '09:20', True, source))
    assert store._race_control_owners.get(IDENTITY) == expected


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('preparing', [False, True])
def test_bootstrap_pre_green_preparation_is_distinct_from_a_label(source, preparing):
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 380, source))
    store.apply(clock(390, '08:00', not preparing, source))
    store.apply(history(source))
    store.apply(clock(620, '04:10', True, source))
    assert store._race_control_owners.get(IDENTITY) == ('Q2' if preparing else None)


@pytest.mark.parametrize('source', SOURCES)
def test_each_closed_epoch_needs_its_own_witness(source):
    store, _, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    send(history(source))
    send(clock(620, '09:20', True, source))
    send(message('TimingData', {'SessionPart': 3}, 390, source), 621)
    send(message('TimingData', {'SessionPart': 3}, 670, source))
    send(flag(3, 'GREEN', 700, 700.1, source))
    send(flag(4, 'CHEQUERED', 800, 800.1, source))
    send(clock(820, '06:00', True, source))
    assert store._race_control_owners[IDENTITY] == 'Q3'
    assert ('4', 'CHEQUERED') not in store._race_control_owners
    send(message('TimingData', {'SessionPart': 3}, 750, source), 821)
    assert store._race_control_owners[('4', 'CHEQUERED')] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_later_part_bound_does_not_identify_intervening_green(source):
    store, _, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 370, source))
    send(history(source))
    # The later Q3 observation cannot prove whether GREEN 400 restarted Q2
    # or was a part transition whose earlier identifying metadata is missing.
    send(message('TimingData', {'SessionPart': 3}, 900, source))
    assert IDENTITY not in store._race_control_owners


@pytest.mark.parametrize('source', SOURCES)
def test_q1_q2_q3_history_uses_each_parts_transition_witness(source):
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 1}, 100, source))
    store.apply(clock(100.5, '18:00', False, source))
    store.apply(message('TimingData', {'SessionPart': 2}, 190, source))
    store.apply(message('TimingData', {'SessionPart': 3}, 390, source))
    events = [(0, 110, 'GREEN'), (1, 150, 'CHEQUERED'), (2, 200, 'GREEN'),
              (3, 350, 'CHEQUERED'), (4, 400, 'GREEN'), (5, 600, 'CHEQUERED')]
    packet = message('RaceControlMessages', {'Messages': {str(i): {
        'Utc': (NOW + timedelta(seconds=t)).isoformat(timespec='seconds'),
        'Flag': f, 'Scope': 'Track'} for i, t, f in events}}, 610, source)
    store.apply(packet)
    assert store._race_control_owners == {
        ('1', 'CHEQUERED'): 'Q1', ('3', 'CHEQUERED'): 'Q2', ('5', 'CHEQUERED'): 'Q3'}
    assert store.snapshot().normal_finished


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('metadata_first', [False, True])
def test_reaffirmation_inside_green_precision_interval_remains_ambiguous(source, metadata_first):
    store, service, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 400.5, source))
    metadata = message('TimingData', {'SessionPart': 3}, 400.7, source)
    # Actual GREEN may be 400.9, printed as 400. Neither label's relationship
    # to GREEN can be inferred from that printed lower bound alone.
    if metadata_first:
        send(metadata, 609)
    send(history(source))
    send(clock(620, '09:20', True, source))
    if not metadata_first:
        assert IDENTITY not in store._race_control_owners
        # More precise own-Utc evidence confirms that Q2's label was in the gap.
        send(message('RaceControlMessages', {'Messages': {'1': {
            'Utc': (NOW + timedelta(seconds=400.9)).isoformat(timespec='milliseconds')}}}, 620.5, source))
        assert IDENTITY not in store._race_control_owners
        send(metadata, 621)
    assert store._race_control_owners[IDENTITY] == 'Q3'
    assert service.snapshot.normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_original_green_publication_can_establish_subsecond_source_order(source):
    store, _, send = harness(source)
    send(flag(0, 'CHEQUERED', 350, 350.1, source))
    send(flag(1, 'GREEN', 400, 400.2, source))
    send(message('TimingData', {'SessionPart': 2}, 400.5, source))
    send(clock(500, '03:00', True, source))
    send(flag(2, 'CHEQUERED', 600, 600.1, source))
    assert store._race_control_owners[IDENTITY] == 'Q2'
    assert store.snapshot().normal_finished


@pytest.mark.parametrize('source', SOURCES)
def test_green_enrichment_cannot_retain_a_superseded_restart(source):
    store, service, send = harness(source)
    send(message('TimingData', {'SessionPart': 3}, 390, source))
    send(flag(2, 'CHEQUERED', 405, 405.1, source))
    assert store._race_control_owners[IDENTITY] == 'Q3'
    send(message('RaceControlMessages', {'Messages': {'1': {
        'Flag': 'GREEN', 'Scope': 'Track'}}}, 410, source))
    send(message('RaceControlMessages', {'Messages': {'1': {
        'Utc': (NOW + timedelta(seconds=400)).isoformat(timespec='seconds')}}}, 415, source))
    send(clock(420, '08:00', True, source))
    assert service.snapshot.normal_finished
    assert store._race_control_owners[IDENTITY] == 'Q3'


@pytest.mark.parametrize('source', SOURCES)
def test_green_precision_enrichment_uses_the_current_source_interval(source):
    store, _, send = harness(source)
    send(message('TimingData', {'SessionPart': 2}, 400.5, source))
    send(history(source))
    send(clock(620, '09:20', True, source))
    assert IDENTITY not in store._race_control_owners
    # The numeric instant is unchanged, but milliseconds now establish that
    # the reaffirmation was after GREEN rather than inside its uncertainty.
    send(message('RaceControlMessages', {'Messages': {'1': {
        'Utc': (NOW + timedelta(seconds=400)).isoformat(timespec='milliseconds')}}}, 621, source))
    assert store._race_control_owners[IDENTITY] == 'Q2'
