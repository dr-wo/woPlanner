from dataclasses import replace
from types import MappingProxyType

import pytest

from wodata.live import (
    DrainResult,
    QualiCheckpoint,
    QualiPitState,
    QualiRawDriverState,
    QualiRawSnapshot,
)
from woplanner.quali.models import FinishedReason
from woplanner.quali.service import QualiLiveService


def raw(drivers, *, part="Q1", remaining=600, session_info=None):
    return QualiRawSnapshot(
        MappingProxyType(session_info or {}),
        "Started",
        part,
        remaining,
        True,
        tuple(drivers),
        None,
    )


def driver(number, best=None, *, laps=1, in_pit=False, sectors=(30, 30, 30), progress=None):
    return QualiRawDriverState(
        str(number), f"D{number}", "Team", completed_laps=laps,
        last_lap_s=best, best_lap_s=best, sectors_s=sectors,
        segments=(QualiCheckpoint("sector", 1, sectors[0]),), in_pit=in_pit,
        pit_state=QualiPitState.QUEUE if in_pit else QualiPitState.ON_TRACK,
        pit_lane_progress=progress,
    )


def test_ranking_no_time_gap_interval_and_projection_reference_replacement():
    service = QualiLiveService(eliminated_counts={"Q1": 1})
    snapshot = service.consume(raw([driver(1, 90), driver(2, 91), driver(3, None, laps=0)]))
    states = snapshot.drivers
    assert [s.driver for s in states] == ["D1", "D2", "D3"]
    assert states[1].gap_s == states[1].interval_s == 1
    assert states[2].best_lap_s is None
    assert snapshot.cutoff.cutoff_rank == 2
    assert snapshot.cutoff.current_cutoff_s == 91
    # New fastest replaces the reference and recomputes active checkpoint projections.
    snapshot = service.consume(raw([driver(1, 90, sectors=(31, 30, 29)), driver(2, 89, sectors=(29, 30, 30))]))
    assert snapshot.drivers[0].driver == "D2"


def test_finished_reasons_and_projected_cutoff():
    service = QualiLiveService(
        eliminated_counts={"Q1": 1}, out_lap_s=100, cool_lap_s=110,
        track_rate_s_per_field_push=0.02,
    )
    snapshot = service.consume(raw([driver(1, 90, in_pit=True), driver(2, 91)], remaining=50))
    first = next(s for s in snapshot.drivers if s.driver == "D1")
    assert first.finished_reason == FinishedReason.IN_PIT_NO_OUTLAP_TIME
    assert snapshot.cutoff.unfinished_driver_count == 1
    assert snapshot.cutoff.projected_cutoff_s == 89.98
    # A completed push with insufficient cool time has its explicit reason.
    service = QualiLiveService(cool_lap_s=110)
    service.consume(raw([driver(1, 90, laps=1)], remaining=200))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=100))
    assert snapshot.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    service = QualiLiveService()
    service.consume(raw([driver(1, 90, laps=1)], remaining=20))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=0))
    assert snapshot.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    # A real chequer applies when any current lap is completed after zero,
    # including a slow lap rather than only a push lap.
    service = QualiLiveService()
    service.consume(raw([driver(1, 90, laps=1)], remaining=0))
    snapshot = service.consume(
        raw([driver(1, 120, laps=2, in_pit=True)], remaining=0)
    )
    assert snapshot.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED


def test_q1_q2_transition_freezes_eliminated_and_clears_advancers_then_q3_finalises():
    service = QualiLiveService(eliminated_counts={"Q1": 1, "Q2": 1, "Q3": 0})
    service.consume(raw([driver(1, 90), driver(2, 91), driver(3, 92)]))
    service.transition_to("Q2")
    states = {s.driver: s for s in service._states.values()}
    assert not states["D3"].active and states["D3"].finished_reason == FinishedReason.ELIMINATED
    assert states["D1"].active and states["D1"].best_lap_s is None
    service.consume(raw([driver(1, 89), driver(2, 90)], part="Q2"))
    service.transition_to("Q3")
    service.consume(raw([driver(1, 88)], part="Q3"))
    service.finalise()
    assert next(s for s in service.snapshot.drivers if s.driver == "D1").finished_reason == FinishedReason.SESSION_FINALISED


def test_next_part_clears_completed_final_push_marker_for_advancing_driver():
    service = QualiLiveService(eliminated_counts={"Q1": 0}, cool_lap_s=110)
    service.consume(raw([driver(1, 90, laps=1)], remaining=200))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=100))
    assert snapshot.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH

    service.transition_to("Q2")

    state = service.snapshot.drivers[0]
    assert state.active is True
    assert state.finished is False
    assert state.finished_reason is None


def test_sq1_sq2_transition_uses_sprint_parts_without_phantom_q1_elimination():
    service = QualiLiveService(eliminated_counts={"SQ1": 1, "SQ2": 1, "SQ3": 0})
    snapshot = service.consume(
        raw([driver(1, 90), driver(2, 91), driver(3, 92)], part="SQ1")
    )
    assert snapshot.session_part == "SQ1"
    assert all(state.active for state in snapshot.drivers)
    service.consume(
        raw(
            [driver(1, 89, laps=2), driver(2, 90, laps=2), driver(3, 91, laps=2)],
            part="SQ1",
        )
    )

    service.transition_to("SQ2")
    states = {state.driver: state for state in service._states.values()}
    assert states["D3"].finished_reason == FinishedReason.ELIMINATED
    assert states["D1"].active is True
    assert service.track_source == "SQ1 estimate"


def test_completed_part_updates_existing_track_evolution_fit():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    service.consume(raw([driver(1, 91, laps=1), driver(2, 92, laps=1)]))
    service.consume(raw([driver(1, 90, laps=2), driver(2, 91, laps=2)]))
    service.transition_to("Q2")
    assert service._track_estimates["Q1"][2] == "estimated"
    assert service.track_source == "Q1 estimate"


def test_track_evolution_updates_live_before_part_transition():
    service = QualiLiveService(
        eliminated_counts={"Q1": 0}, track_rate_s_per_field_push=0.25
    )
    service.consume(raw([driver(1, 90, laps=1)]))
    service.consume(raw([driver(1, 89, laps=2)]))
    assert service.track_source == "FP prior"

    snapshot = service.consume(raw([driver(1, 88, laps=3)]))

    assert service.session_part == "Q1"
    assert service.track_source == "Q1 live estimate"
    rate, samples, quality = service._track_estimates["Q1"]
    assert rate == pytest.approx(1.0)
    assert (samples, quality) == (2, "estimated")
    assert snapshot.cutoff.track_rate_s_per_field_push == pytest.approx(1.0)


def test_manual_track_override_does_not_replace_latest_automatic_fit():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    service.consume(raw([driver(1, 90, laps=1)]))
    service.consume(raw([driver(1, 89, laps=2)]))
    service.consume(raw([driver(1, 88, laps=3)]))
    service.set_track_override(0.5)

    service.consume(raw([driver(1, 87, laps=4)]))

    assert service.track_rate_s_per_field_push == 0.5
    assert service.track_source == "Manual override"
    service.set_track_override(None)
    assert service.track_rate_s_per_field_push == pytest.approx(1.0)
    assert service.track_source == "Q1 live estimate"


def test_pit_groups_are_exclusive_and_queue_uses_physical_progress():
    service = QualiLiveService()
    inputs = [driver(1, None, laps=0, in_pit=True, progress=.2), driver(2, None, laps=0, in_pit=True, progress=.8)]
    snapshot = raw(inputs)
    service.consume(snapshot)
    assert service.pit_status_groups()["Queue"] == ("D2", "D1")
    assert not service.pit_status_groups()["On-track"]


def test_poll_rejects_non_qualifying_session_before_loading_drivers():
    race_snapshot = raw(
        [driver(1, 90)],
        session_info={"Meeting": {"Name": "British Grand Prix"}, "Name": "Race"},
    )

    class Controller:
        def drain(self):
            return DrainResult(race_snapshot, (), 1, True, frozenset({"timing"}))

    service = QualiLiveService(controller=Controller())
    snapshot, _, changed = service.poll()

    assert service.session_is_qualifying is False
    assert service.received_session_name == "Race"
    assert snapshot.drivers == ()
    assert changed is False


def test_poll_loads_drivers_after_qualifying_session_is_confirmed():
    quali_snapshot = raw(
        [driver(1, 90)],
        session_info={
            "Meeting": {"Name": "British Grand Prix", "Number": 12},
            "Name": "Qualifying",
        },
    )

    class Controller:
        def drain(self):
            return DrainResult(quali_snapshot, (), 1, True, frozenset({"timing"}))

    service = QualiLiveService(controller=Controller())
    snapshot, _, changed = service.poll()

    assert service.session_is_qualifying is True
    assert [state.driver for state in snapshot.drivers] == ["D1"]
    assert changed is True
