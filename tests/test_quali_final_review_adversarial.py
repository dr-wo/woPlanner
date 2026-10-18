"""Independent source-supported adjacent cases for final Q17 state review."""
from datetime import timedelta
from dataclasses import replace
import pytest

from wodata.live import LiveTimingMessage, QualiSessionStore
from woplanner.quali.service import QualiLiveService
from test_quali_review_corrections import NOW, clock, ingress, message


@pytest.mark.parametrize('utc_first', [False, True])
def test_fractional_mapped_duplicate_cannot_reset_segment(utc_first):
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 89))
    store.apply(LiveTimingMessage(
        'ExtrapolatedClock', {'Utc': (NOW + timedelta(seconds=90)).isoformat(),
                             'Remaining': '00:10', 'Extrapolating': True},
        None, NOW + timedelta(seconds=90), session_time_seconds=90,
        source='f1_archive_replay'))
    utc_clock = message('ExtrapolatedClock', {'Remaining': '00:00', 'Extrapolating': False}, 100.1)
    session_clock = LiveTimingMessage(
        'ExtrapolatedClock', {'Remaining': '00:00', 'Extrapolating': False},
        None, NOW + timedelta(seconds=100.1), session_time_seconds=100.1,
        source='f1_archive_replay')
    first, duplicate = (utc_clock, session_clock) if utc_first else (session_clock, utc_clock)
    store.apply(first)
    duplicate = replace(duplicate, payload={'Remaining': '13:00', 'Extrapolating': False})
    result = store.apply(duplicate)
    assert result.session_part == 'Q2'
    assert result.remaining_time_s == 0


def test_live_subscription_countdown_latency_is_not_subtracted_twice():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 99, 'signalr_live'))
    snapshot_clock = LiveTimingMessage(
        'ExtrapolatedClock',
        {'Utc': (NOW + timedelta(seconds=100)).isoformat(),
         'Remaining': '08:00', 'Extrapolating': True},
        None, NOW + timedelta(seconds=105), source='signalr_live',
    )
    assert store.apply(snapshot_clock).remaining_time_s == 475
    # Subsequent timestamped live publication is one second after receipt.
    result = store.apply(message('DriverList', {'1': {'Tla': 'AAA'}}, 106, 'signalr_live'))
    assert result.remaining_time_s == 474


def test_pending_roster_driver_can_resolve_after_observed_part_transition():
    service = QualiLiveService(eliminated_counts={'Q2': 1})
    ingress(service, message('SessionInfo', {'Name': 'Qualifying'}, 98))
    ingress(service, message('TimingData', {'SessionPart': 2, 'Lines': {
        '1': {'Position': 1, 'KnockedOut': False},
        '2': {'Position': 2, 'KnockedOut': False},
        '3': {'Position': 3, 'KnockedOut': False},
    }}, 99))
    ingress(service, message('DriverList', {'4': {'Tla': 'DDD'}}, 100))
    assert not next(d for d in service.snapshot.drivers if d.driver_number == '4').active
    ingress(service, message('TimingData', {'SessionPart': 3}, 101))
    # This driver was unknown, never a locally selected survivor/elimination.
    # New-part affirmative register must be allowed to resolve participation.
    ingress(service, message('TimingData', {'Lines': {'4': {'KnockedOut': False}}}, 102))
    assert next(d for d in service.snapshot.drivers if d.driver_number == '4').active
    assert not next(d for d in service.snapshot.drivers if d.driver_number == '3').active


def test_distinct_same_second_green_and_finish_follow_source_order():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 99))
    stamp = (NOW + timedelta(seconds=100)).isoformat(timespec='seconds')
    for index, flag, publication in ((1, 'GREEN', 100.1), (2, 'CHEQUERED', 100.9)):
        result = store.apply(message('RaceControlMessages', {'Messages': {
            str(index): {'Utc': stamp, 'Flag': flag, 'Scope': 'Track'}
        }}, publication))
    assert result.normal_finished
    assert result.normal_finish_source == 'race_control'


def test_duplicate_historical_snapshot_keeps_owned_finish_out_of_next_part():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 299))
    stamp = (NOW + timedelta(seconds=300)).isoformat(timespec='seconds')
    payload = {'Messages': {'1': {'Utc': stamp, 'Flag': 'CHEQUERED', 'Scope': 'Track'}}}
    assert store.apply(message('RaceControlMessages', payload, 300.1)).normal_finished
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 300.5)).normal_finished
    # Subscription snapshots omit envelope publication timestamps.
    snapshot = LiveTimingMessage('RaceControlMessages', payload, None,
                                NOW + timedelta(seconds=300.9), source='signalr_live')
    for _ in range(3):
        assert not store.apply(snapshot).normal_finished


def test_owned_finish_later_utc_metadata_keeps_source_message_identity():
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 299))
    # Legacy timestamped delta dates a flag even before its Utc field arrives.
    first = {'Messages': {'1': {'Flag': 'CHEQUERED', 'Scope': 'Track'}}}
    assert store.apply(message('RaceControlMessages', first, 300.1)).normal_finished
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 300.5)).normal_finished
    # Sparse field completion of this same source index is evidence enrichment,
    # not a new chequered event for Q3.
    stamp = (NOW + timedelta(seconds=300)).isoformat(timespec='seconds')
    enriched = {'Messages': {'1': {'Utc': stamp}}}
    assert not store.apply(message('RaceControlMessages', enriched, 300.9)).normal_finished


@pytest.mark.parametrize('zero_before_flag', [False, True])
def test_finish_after_observed_green_and_running_clock_keeps_owner(zero_before_flag):
    store = QualiSessionStore()
    store.apply(message('TimingData', {'SessionPart': 2}, 298))
    store.apply(clock(298.5, '00:02', False))
    green = {'Messages': {'1': {
        'Utc': (NOW + timedelta(seconds=299)).isoformat(timespec='seconds'),
        'Flag': 'GREEN', 'Scope': 'Track'}}}
    store.apply(message('RaceControlMessages', green, 299.1))
    store.apply(clock(299.5, '00:01', True))
    if zero_before_flag:
        store.apply(clock(300.05, '00:00', False))
    finish = {'Messages': {'2': {
        'Utc': (NOW + timedelta(seconds=300)).isoformat(timespec='seconds'),
        'Flag': 'CHEQUERED', 'Scope': 'Track'}}}
    assert store.apply(message('RaceControlMessages', finish, 300.1)).normal_finished
    assert not store.apply(message('TimingData', {'SessionPart': 3}, 300.5)).normal_finished
    assert not store.apply(clock(300.75, '13:00', False)).normal_finished
