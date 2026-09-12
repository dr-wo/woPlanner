from datetime import datetime, timezone
from dataclasses import replace

from wodata.live import LiveTimingMessage, QualiPitState, QualiSessionStore

from woplanner.quali.lap_state import LapPaceReference, classify_lap_state
from woplanner.quali.service import QualiLiveService


NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def event(topic, payload, seconds):
    return LiveTimingMessage(
        topic, payload, None, NOW, source="f1_archive_replay",
        session_time_seconds=float(seconds),
    )


def timing(patch, seconds):
    return event("TimingData", {"Lines": {"12": patch}}, seconds)


def car(speed, rpm, seconds):
    return event("CarData.z", {"Entries": [{"Cars": {"12": {
        "Channels": {"0": rpm, "2": speed, "3": 1},
    }}}]}, seconds)


def state(store):
    return store.snapshot().drivers[0].pit_state


def test_outbound_movement_queue_and_confirmed_exit():
    store = QualiSessionStore()
    store.apply(timing({"InPit": True}, 0))
    assert state(store) == QualiPitState.NOT_ACTIVE
    store.apply(car(0, 2000, 1))
    assert state(store) == QualiPitState.FIRE_UP
    store.apply(car(20, 3000, 2))
    assert state(store) == QualiPitState.PIT_LANE
    store.apply(timing({"InPit": False}, 3))
    assert state(store) == QualiPitState.PIT_LANE
    store.apply(car(0, 3000, 4))
    assert state(store) == QualiPitState.QUEUE
    store.apply(car(75, 4000, 5))
    assert state(store) == QualiPitState.PIT_LANE
    out = store.apply(car(120, 5000, 6)).drivers[0]
    assert out.pit_state == QualiPitState.ON_TRACK
    assert out.out_lap_confirmed
    assert classify_lap_state(
        out, previous_state="UNKNOWN", new_lap=False, reference=None,
    ) == "OUT"


def test_inbound_weighbridge_stop_and_hot_swap_remain_pit_lane():
    store = QualiSessionStore()
    store.apply(timing({"InPit": False}, 0))
    store.apply(car(140, 6000, 1))
    store.apply(timing({"InPit": True}, 2))
    assert state(store) == QualiPitState.IN_LAP
    store.apply(car(35, 3500, 3))
    assert state(store) == QualiPitState.PIT_LANE
    store.apply(car(0, 3000, 4))
    assert state(store) == QualiPitState.PIT_LANE
    store.apply(event("Heartbeat", {}, 20))
    assert state(store) == QualiPitState.PIT_LANE


def test_unknown_and_pit_side_staleness_do_not_invent_on_track_history():
    store = QualiSessionStore()
    store.apply(event("DriverList", {"12": {"Tla": "ANT"}}, 0))
    assert state(store) == QualiPitState.UNKNOWN
    store.apply(timing({"InPit": True}, 1))
    store.apply(car(0, 2000, 2))
    store.apply(event("Heartbeat", {}, 20))
    assert state(store) == QualiPitState.NOT_ACTIVE
    store.apply(car(0, 3000, 21))
    assert state(store) == QualiPitState.FIRE_UP

    on_track = QualiSessionStore()
    on_track.apply(timing({"InPit": False}, 0))
    on_track.apply(car(140, 6000, 1))
    on_track.apply(event("Heartbeat", {}, 20))
    assert state(on_track) == QualiPitState.ON_TRACK


def test_late_acquisition_can_classify_timed_lap_without_out_history():
    store = QualiSessionStore()
    store.apply(event("ExtrapolatedClock", {"Remaining": "18:00", "Extrapolating": True}, 0))
    raw = store.apply(timing({
        "NumberOfLaps": 4, "LastLapTime": {"Value": "1:22.0"},
        "Sectors": {"0": {"Value": "27.0"}}, "InPit": False,
    }, 100)).drivers[0]
    assert raw.timed_lap_eligible
    assert not raw.out_lap_confirmed
    assert classify_lap_state(
        raw, previous_state=None, new_lap=False,
        reference=LapPaceReference(start_to_s1_s=27.112),
    ) == "FAST"


def test_lap_publication_cannot_pair_with_later_counter_or_cross_segment():
    store = QualiSessionStore()
    store.apply(event("ExtrapolatedClock", {"Remaining": "18:00", "Extrapolating": True}, 0))
    store.apply(timing({"NumberOfLaps": 3, "InPit": False}, 10))
    store.apply(timing({"LastLapTime": {"Value": "1:30"}}, 100))
    raw = store.apply(timing({"NumberOfLaps": 4}, 150))
    assert raw.completed_lap_sequence == ()
    assert {e.reason for e in raw.lap_evidence_sequence if e.kind == "unassociated_lap"} >= {
        "counter_without_same_update_lap_time", "lap_time_without_same_update_counter",
    }

    store.apply(event("SessionInfo", {"Name": "Q3", "SessionPart": "Q3"}, 200))
    raw = store.apply(timing({"NumberOfLaps": 5, "LastLapTime": {"Value": "3:00"}}, 300))
    assert raw.completed_lap_sequence == ()
    assert raw.lap_evidence_sequence[-1].reason == "cross_segment_interval"


def test_service_rejects_counter_plus_last_without_confirmed_event():
    store = QualiSessionStore()
    service = QualiLiveService()
    service.consume(store.apply(timing({"NumberOfLaps": 1, "InPit": False}, 0)))
    raw = store.apply(timing({"NumberOfLaps": 2, "LastLapTime": {"Value": "1:20"}}, 80))
    service.consume(replace(raw, completed_lap_sequence=()))
    assert not service._part_push_laps["Q1"]


def test_sector_status_patch_after_rejected_boundary_does_not_enable_pace():
    store = QualiSessionStore()
    store.apply(event("ExtrapolatedClock", {"Remaining": "18:00", "Extrapolating": True}, 0))
    store.apply(timing({"InPit": True, "NumberOfLaps": 1}, 1))
    store.apply(timing({"InPit": False}, 10))
    store.apply(car(130, 5000, 11))
    rejected = store.apply(timing({
        "NumberOfLaps": 2, "LastLapTime": {"Value": "3:00"},
        "Sectors": {"1": {"Value": "30.0"}},
    }, 100))
    assert rejected.completed_lap_sequence == ()
    assert not rejected.drivers[0].timed_lap_eligible
    status_only = store.apply(timing({"Sectors": {"2": {"Status": 1}}}, 104))
    assert not status_only.drivers[0].timed_lap_eligible
    assert classify_lap_state(
        status_only.drivers[0], previous_state="SLOW", new_lap=False,
        reference=LapPaceReference(start_to_s1_s=27),
    ) == "UNKNOWN"


def test_late_clock_anchor_does_not_reject_first_observed_timed_lap():
    store = QualiSessionStore()
    store.apply(event("ExtrapolatedClock", {"Remaining": "10:00", "Extrapolating": True}, 500))
    raw = store.apply(timing({
        "InPit": False, "NumberOfLaps": 4,
        "LastLapTime": {"Value": "1:22"},
        "Sectors": {"0": {"Value": "27"}},
    }, 550))
    assert len(raw.completed_lap_sequence) == 1
    assert raw.drivers[0].timed_lap_eligible
