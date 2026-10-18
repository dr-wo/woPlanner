"""Use distinct event/count identities and retain the existing projection math."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from wodata.live import LiveTimingMessage, QualiPitState, QualiRawDriverState, QualiRawSnapshot, QualiSessionStore
from wodata.live.quali_models import QualiCompletedLap
from woplanner.quali.service import QualiLiveService, _associated_best_sector_checkpoints


def evidence():
    event = QualiCompletedLap("Q1", "31", 3, 98.879, 100,
                              sectors_s=(25.488, 32.895, 40.496), official_lap_number=2)
    owner = QualiRawDriverState("31", "OCO", "Haas", completed_laps=1,
                                last_lap_s=98.879, best_lap_s=98.879,
                                sectors_s=event.sectors_s, best_lap_event_key=event.event_key,
                                knocked_out=False)
    current = QualiRawDriverState("3", "VER", "Red Bull", sectors_s=(24.932, None, None),
                                  in_pit=False, pit_state=QualiPitState.ON_TRACK,
                                  timed_lap_eligible=True, knocked_out=False)
    snapshot = replace(QualiRawSnapshot.empty(), remaining_time_s=600,
                       drivers=(owner, current), completed_lap_sequence=(event,))
    return event, owner, current, snapshot


def test_divergent_event_and_count_associate_and_restore_the_existing_formula():
    event, owner, current, snapshot = evidence()
    assert event.lap_number != owner.completed_laps
    checkpoints = _associated_best_sector_checkpoints(owner, (event,), "Q1")
    assert checkpoints[("sector", 3)] == pytest.approx(owner.best_lap_s)
    service = QualiLiveService()
    result = service.consume(snapshot)
    assert service._reference_total_s == owner.best_lap_s
    projected = next(driver for driver in result.drivers if driver.driver_number == "3")
    assert projected.projected_lap_s == pytest.approx(24.932 * 98.879 / 25.488)


def test_equal_lap_times_and_confusable_counts_do_not_join_distinct_events():
    event, owner, _, _ = evidence()
    wrong = replace(event, lap_number=1, session_time_s=200)
    assert wrong.lap_number == owner.completed_laps
    assert wrong.lap_time_s == event.lap_time_s
    assert _associated_best_sector_checkpoints(owner, (wrong,), "Q1") == {}
    wrong_time = replace(event, session_time_s=101)
    assert _associated_best_sector_checkpoints(owner, (wrong_time,), "Q1") == {}


@pytest.mark.parametrize("change", [
    {"best_lap_s": None}, {"best_lap_event_key": None},
    {"best_lap_event_key": ("Q1", "31", 6, 200)},
    {"best_lap_event_key": ("Q2", "31", 3, 100)},
    {"best_lap_s": 97.0},
])
def test_cached_reference_is_revalidated_even_with_unchanged_best_time(change):
    _, owner, _, snapshot = evidence()
    service = QualiLiveService()
    service.consume(snapshot)
    result = service.consume(replace(snapshot, drivers=(replace(owner, **change), snapshot.drivers[1])))
    assert service._reference_total_s is None
    assert all(driver.projected_lap_s is None for driver in result.drivers)


@pytest.mark.parametrize("event_change", [
    {"sectors_s": (25.488, 32.895, None)},
    {"sectors_s": (25.488, 32.895, 41.496)},
    {"sectors_s": (25.488, 32.895, float("nan"))},
    {"classification": "invalid"}, {"driver_number": "3"},
])
def test_changed_or_invalid_event_evidence_retracts_reference(event_change):
    event, _, _, snapshot = evidence()
    service = QualiLiveService()
    service.consume(snapshot)
    service.consume(replace(snapshot, completed_lap_sequence=(replace(event, **event_change),)))
    assert service._reference_total_s is None


def test_late_sector_event_enrichment_restores_reference_without_new_best_value():
    event, _, _, snapshot = evidence()
    service = QualiLiveService()
    service.consume(replace(snapshot, completed_lap_sequence=(replace(event, sectors_s=(25.488, 32.895, None)),)))
    assert service._reference_total_s is None
    result = service.consume(snapshot)
    assert service._reference_total_s == 98.879
    assert next(d for d in result.drivers if d.driver == "VER").projected_lap_s is not None


@pytest.mark.parametrize("pit_state", [QualiPitState.PIT_LANE, QualiPitState.ON_TRACK])
def test_pit_and_out_ineligible_current_driver_remain_unprojected(pit_state):
    _, _, current, snapshot = evidence()
    current = replace(current, timed_lap_eligible=False, pit_state=pit_state,
                      in_pit=pit_state == QualiPitState.PIT_LANE)
    result = QualiLiveService().consume(replace(snapshot, drivers=(snapshot.drivers[0], current)))
    assert next(d for d in result.drivers if d.driver == "VER").projected_lap_s is None


def test_insufficient_current_evidence_remains_unprojected():
    _, _, current, snapshot = evidence()
    current = replace(current, sectors_s=(None, None, None))
    result = QualiLiveService().consume(replace(snapshot, drivers=(snapshot.drivers[0], current)))
    assert next(d for d in result.drivers if d.driver == "VER").projected_lap_s is None


@pytest.mark.parametrize("part", ["Q2", "Q3"])
def test_stale_part_key_cannot_match_reused_numbers(part):
    event, owner, _, snapshot = evidence()
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
    service.consume(snapshot)
    reused = replace(event, session_part=part)
    transitioned = replace(snapshot, session_part=part, completed_lap_sequence=(event, reused))
    service.consume(transitioned)
    assert service._reference_total_s is None
    owner = replace(owner, best_lap_event_key=reused.event_key)
    service.consume(replace(transitioned, drivers=(owner, snapshot.drivers[1])))
    assert service._reference_total_s == 98.879
    service.reset()
    assert not service._driver_best_references and service._reference_total_s is None


def test_reducer_to_service_late_sectors_deletion_and_new_completed_best():
    store, service = QualiSessionStore(), QualiLiveService()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def consume(patch, seconds):
        snapshot = store.apply(LiveTimingMessage(
            "TimingData", {"Lines": {"31": patch}}, now, now,
            session_time_seconds=float(seconds)))
        service.consume(snapshot)
        return snapshot

    incomplete = consume({"InPit": False, "NumberOfLaps": 3,
                          "LastLapTime": {"Value": "1:38.879"},
                          "BestLapTime": {"Value": "1:38.879", "Lap": 2},
                          "Sectors": [{"Value": "25.488"}, {"Value": "32.895"}]}, 100)
    assert incomplete.drivers[0].completed_laps != incomplete.completed_lap_sequence[0].lap_number
    assert service._reference_total_s is None
    ready = consume({"Sectors": {"2": {"Value": "40.496"}}}, 100.1)
    assert service._reference_total_s == pytest.approx(98.879)
    assert ready.drivers[0].best_lap_event_key == incomplete.drivers[0].best_lap_event_key
    consume({"BestLapTime": {"Value": ""}}, 101)
    assert service._reference_total_s is None
    consume({"Sectors": {"2": {"PersonalFastest": False}}}, 102)
    assert service._reference_total_s is None
    consume({"NumberOfLaps": 6, "LastLapTime": {"Value": "1:30"},
             "BestLapTime": {"Value": "1:30", "Lap": 5},
             "Sectors": [{"Value": "30"}] * 3}, 200)
    assert service._reference_total_s == 90


@pytest.mark.parametrize("case", ["duplicate", "retraction", "part_transition"])
def test_reducer_sector_redelivery_cannot_create_a_planner_reference_for_event_b(case):
    store = QualiSessionStore()
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sectors = {str(i): {"Value": str(value)} for i, value in enumerate((20, 30, 40))}

    def consume(patch, seconds, topic="TimingData"):
        payload = {"Lines": {"31": patch}} if topic == "TimingData" else patch
        snapshot = store.apply(LiveTimingMessage(topic, payload, now, now,
                                                session_time_seconds=float(seconds)))
        service.consume(snapshot)
        return snapshot

    first = consume({"NumberOfLaps": 3, "InPit": False,
                     "LastLapTime": {"Value": "1:30"},
                     "BestLapTime": {"Value": "1:30", "Lap": 2}, "Sectors": sectors}, 100)
    key = first.completed_lap_sequence[0].event_key
    assert service._reference_total_s == 90
    if case == "retraction":
        consume({"NumberOfLaps": 3, "LastLapTime": {"Value": ""}}, 101)
        assert service._reference_total_s is None
    elif case == "part_transition":
        consume({"SessionPart": "Q2"}, 110, "SessionInfo")
        assert service._reference_total_s is None
    consume({"Position": 1}, 150)
    consume({"Sectors": sectors}, 151)
    later = consume({"NumberOfLaps": 4, "LastLapTime": {"Value": "1:30"},
                     "BestLapTime": {"Value": "1:30", "Lap": 3}}, 220)
    assert later.completed_lap_sequence[0].event_key == key
    assert later.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)
    assert later.drivers[0].best_lap_event_key == later.completed_lap_sequence[1].event_key
    current = QualiRawDriverState("1", "VER", "Red Bull", sectors_s=(25, None, None),
                                  in_pit=False, pit_state=QualiPitState.ON_TRACK,
                                  timed_lap_eligible=True)
    result = service.consume(replace(later, drivers=(*later.drivers, current)))
    assert service._reference_total_s is None
    assert next(driver for driver in result.drivers if driver.driver_number == "1").projected_lap_s is None
