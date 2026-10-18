"""Independent review reproductions through reducer/controller/service."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest
from wodata.live import LiveTimingMessage, QualiSessionStore
from wodata.live.reducer import _deep_merge
from wodata.live.replay import ReplayPlayer
from woplanner.quali.models import FinishedReason
from woplanner.quali.service import QualiLiveService

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RECORDING = next((Path(__file__).resolve().parents[2] / 'woData/wodata/live_timing/schema_v1/year=2026/round=17/session=Q').glob('*.jsonl'))


def message(topic, payload, seconds, source='f1_archive_replay'):
    utc = NOW + timedelta(seconds=seconds)
    return LiveTimingMessage(topic, payload, utc, utc, source=source)


def ingress(service, packet):
    service.controller._enqueue_message(packet)
    service.controller.drain(max_messages=1)
    return service.raw_snapshot


@lru_cache(None)
def recording_messages():
    result = []
    ReplayPlayer(RECORDING, speed=0).play(result.append)
    return result


def clock(seconds, remaining='08:00', running=True, source='f1_archive_replay'):
    return message('ExtrapolatedClock', {'Utc': (NOW + timedelta(seconds=seconds)).isoformat(),
                   'Remaining': remaining, 'Extrapolating': running}, seconds, source)


def chequered(seconds, *, whole_second=False):
    utc = (NOW + timedelta(seconds=seconds)).isoformat(timespec='seconds' if whole_second else 'microseconds')
    return message('RaceControlMessages', {'Messages': {'1': {'Utc': utc, 'Flag': 'CHEQUERED', 'Scope': 'Track'}}}, seconds)


def finish_state(raw):
    return raw.session_part, raw.remaining_time_s, raw.clock_running, raw.normal_finished, raw.normal_finish_source


def test_stale_synthetic_clock_and_duplicate_cannot_advance_or_rewrite_part():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 99))
    old = clock(100)
    store.apply(old)
    store.apply(clock(580, '00:00', False))
    before = finish_state(store.snapshot())
    assert finish_state(store.apply(replace(old, received_at_utc=NOW + timedelta(seconds=581)))) == before
    assert finish_state(store.apply(clock(580, '13:00', True))) == before
    assert store.apply(message('TimingData', {'SessionPart': 2}, 582)).session_part == 'Q2'
    assert store.apply(clock(600, '13:00', False)).session_part == 'Q3'


def test_original_r17_clock_five_redelivery_after_genuine_q2_finish():
    service = QualiLiveService()
    packets = recording_messages()
    for packet in packets[:2751]:
        ingress(service, packet)
    before = finish_state(service.raw_snapshot)
    assert before[0] == 'Q2' and before[3]
    ingress(service, replace(packets[4], received_at_utc=packets[2750].received_at_utc))
    assert finish_state(service.raw_snapshot) == before
    assert ingress(service, message('TimingData', {'SessionPart': 2}, 900)).session_part == 'Q2'


def reconnect_snapshot_through(end):
    registers = {}
    for packet in recording_messages()[:end]:
        if packet.topic in {'SessionInfo', 'SessionStatus', 'ExtrapolatedClock', 'DriverList', 'TimingData', 'RaceControlMessages'}:
            _deep_merge(registers.setdefault(packet.topic, {}), deepcopy(packet.payload))
    receipt = recording_messages()[end - 1].received_at_utc
    return [LiveTimingMessage(topic, registers[topic], None, receipt, source='f1_archive_replay')
            for topic in ('SessionInfo', 'SessionStatus', 'ExtrapolatedClock', 'DriverList', 'TimingData', 'RaceControlMessages')]


def test_original_r17_pre_q3_start_reconnect_and_repeat_snapshot():
    service = QualiLiveService()
    packets = reconnect_snapshot_through(4068)
    for _ in range(2):
        for packet in packets:
            ingress(service, packet)
        raw = service.raw_snapshot
        assert raw.session_part == 'Q3'
        assert raw.remaining_time_s == 780 and not raw.clock_running
        assert sum(d.active for d in service.snapshot.drivers) == 10
        assert not raw.normal_finished
    for packet in recording_messages()[4068:7566]:
        ingress(service, packet)
    assert service.raw_snapshot.normal_finished
    assert service.raw_snapshot.normal_finish_source == 'race_control'


def test_original_r17_finished_q2_reconnect_preserves_current_chequered():
    service = QualiLiveService()
    packets = reconnect_snapshot_through(2751)
    for _ in range(2):
        for packet in packets:
            ingress(service, packet)
        assert service.raw_snapshot.session_part == 'Q2'
        assert service.raw_snapshot.normal_finished
        assert service.raw_snapshot.normal_finish_source == 'race_control'


@pytest.mark.parametrize('source', ['signalr_live', 'f1_archive_replay'])
def test_shortened_finish_latches_through_positive_clock_and_marks_completed_lap(source):
    service = QualiLiveService()
    ingress(service, message('SessionInfo', {'Name': 'Qualifying'}, 98, source))
    ingress(service, message('TimingData', {'SessionPart': 2, 'Lines': {'1': {
        'Position': 1, 'NumberOfLaps': 0, 'InPit': False, 'KnockedOut': False}}}, 99, source))
    ingress(service, clock(100, source=source))
    assert ingress(service, chequered(300)).normal_finished
    assert ingress(service, clock(301, '04:39', source=source)).normal_finished
    ingress(service, message('TimingData', {'Lines': {'1': {'NumberOfLaps': 1,
        'LastLapTime': {'Value': '90'}, 'BestLapTime': {'Value': '90'}}}}, 302, source))
    assert service.snapshot.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    assert not ingress(service, message('TimingData', {'SessionPart': 3}, 400, source)).normal_finished


def test_second_precision_finish_overlaps_subsecond_boundary_without_admitting_previous_second():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 299.5))
    store.apply(clock(300.25))
    assert not store.apply(chequered(299, whole_second=True)).normal_finished
    assert not store.apply(chequered(300.2)).normal_finished
    assert store.apply(chequered(300, whole_second=True)).normal_finished
    assert store.apply(clock(301, '07:59')).normal_finished


def test_newer_green_restarts_same_part_and_old_finish_redelivery_stays_old():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 99))
    store.apply(clock(100))
    packet = chequered(300)
    assert store.apply(packet).normal_finished
    green = message('RaceControlMessages', {'Messages': {'2': {'Utc': (NOW + timedelta(seconds=400)).isoformat(), 'Flag': 'GREEN'}}}, 400)
    assert not store.apply(green).normal_finished
    assert not store.apply(packet).normal_finished
    assert store.apply(chequered(500)).normal_finished


def test_explicit_part_owned_finish_survives_first_positive_clock():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 99))
    assert store.apply(chequered(300)).normal_finished
    assert store.apply(clock(301)).normal_finished
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 400)).normal_finished


def test_split_original_r17_participation_bootstrap_and_repeated_registers():
    service = QualiLiveService()
    packets = recording_messages()
    for packet in packets[:5]:
        ingress(service, packet)
    initial = packets[5]
    timing = deepcopy(initial.payload)
    participation = {'SessionPart': timing['SessionPart'], 'Lines': {}}
    for number, row in timing['Lines'].items():
        participation['Lines'][number] = {'KnockedOut': row.pop('KnockedOut')}
    ingress(service, replace(initial, payload=timing))
    assert not service._initial_timing_received
    assert all(d.knocked_out is None for d in service.raw_snapshot.drivers)
    assert not any(d.active for d in service.snapshot.drivers)
    items = list(participation['Lines'].items())
    for group in (items[:11], items[11:]):
        ingress(service, replace(initial, payload={'SessionPart': 2, 'Lines': dict(group)}))
    assert service._initial_timing_received
    assert sum(d.active for d in service.snapshot.drivers) == 16
    assert service.snapshot.cutoff.cutoff_rank == 10
    before = service.snapshot
    ingress(service, replace(initial, payload=participation))
    current = service.snapshot
    assert current.track_evolution_result['as_of']['publication_index'] == before.track_evolution_result['as_of']['publication_index'] + 1
    # Repeated registers retain domain state; only publication provenance advances.
    assert replace(current, track_evolution_result={
        **current.track_evolution_result, 'as_of': before.track_evolution_result['as_of']
    }) == before


def test_participation_initialisation_does_not_require_positions():
    service = QualiLiveService(eliminated_counts={'Q2': 0})
    ingress(service, message('SessionInfo', {'Name': 'Qualifying', 'SessionPart': 2}, 100))
    ingress(service, message('DriverList', {'1': {'Tla': 'AAA'}, '2': {'Tla': 'BBB'}}, 101))
    assert not any(d.active for d in service.snapshot.drivers)
    assert not service._initial_timing_received
    ingress(service, message('TimingData', {'Lines': {
        '1': {'KnockedOut': False}, '2': {'KnockedOut': True}}}, 102))
    assert service._initial_timing_received
    assert {d.driver for d in service.snapshot.drivers if d.active} == {'AAA'}
    assert service.snapshot.cutoff.cutoff_rank == 1
