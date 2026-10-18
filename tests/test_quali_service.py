from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pandas as pd
import pytest

from wodata.live import (
    DrainResult,
    LiveTimingMessage,
    QualiCheckpoint,
    QualiPitState,
    QualiRawDriverState,
    QualiRawSnapshot,
    QualiTyreStint,
)
from wodata.live.quali_models import QualiCompletedLap
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.models import FinishedReason
from woplanner.quali.physical_traffic import PhysicalLapTraffic
from woplanner.quali.service import QualiLiveService


def raw(drivers, *, part="Q1", remaining=600, session_info=None):
    # Supply explicit provenance; legacy defaults deliberately cannot create
    # a projection reference. Identity divergence is covered separately.
    drivers = [replace(driver, best_lap_event_key=(part, driver.driver_number, driver.completed_laps, None))
               if driver.best_lap_s is not None and driver.completed_laps else driver
               for driver in drivers]
    return QualiRawSnapshot(
        MappingProxyType(session_info or {}),
        "Started",
        part,
        remaining,
        True,
        tuple(drivers),
        None,
        completed_lap_sequence=tuple(
            QualiCompletedLap(part, driver.driver_number, lap, driver.last_lap_s or 0.0, None,
                              sectors_s=driver.sectors_s)
            for driver in drivers for lap in range(1, driver.completed_laps + 1)
        ),
    )


def driver(number, best=None, *, laps=1, in_pit=False, sectors=(30, 30, 30), progress=None, position=None):
    return QualiRawDriverState(
        str(number), f"D{number}", "Team", position=position, completed_laps=laps,
        last_lap_s=best, best_lap_s=best, sectors_s=sectors,
        segments=(QualiCheckpoint("sector", 1, sectors[0]),), in_pit=in_pit,
        pit_state=QualiPitState.QUEUE if in_pit else QualiPitState.ON_TRACK,
        pit_lane_progress=progress,
        timed_lap_eligible=not in_pit,
        structural_state=QualiPitState.ON_TRACK if not in_pit else QualiPitState.UNKNOWN,
        knocked_out=False,
    )


def with_test_pace_reference(service):
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=30.0)
    return service


@pytest.mark.parametrize("source", ["signalr_live", "f1_archive_replay"])
def test_q2_historical_flags_ordinary_and_final_laps_through_production_ingress(source):
    service = with_test_pace_reference(QualiLiveService(cool_lap_s=120, eliminated_counts={"Q2": 0}))
    origin = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def send(topic, payload, seconds, snapshot=False):
        utc = origin + timedelta(seconds=seconds)
        service.controller._enqueue_message(LiveTimingMessage(
            topic, payload, None if snapshot else utc, utc, source=source,
        ))
        return service.poll()[0]

    def rc_flag(kind, seconds):
        return {"Utc": (origin + timedelta(seconds=seconds)).isoformat(),
                "Flag": kind, "Scope": "Track"}

    def lap(number, seconds):
        return send("TimingData", {"Lines": {"1": {
            "NumberOfLaps": number, "LastLapTime": {"Value": "90"},
            "BestLapTime": {"Value": "90"}, "InPit": False,
            "Sectors": [{"Value": "30"}, {"Value": "30"}, {"Value": "30"}],
        }}}, seconds)

    send("SessionInfo", {"Name": "Qualifying", "SessionPart": "Q2"}, 200, True)
    send("DriverList", {"1": {"Tla": "AAA"}}, 200, True)
    send("TimingData", {"Lines": {"1": {"Position": 1, "NumberOfLaps": 0, "InPit": False, "KnockedOut": False}}}, 200, True)
    history = {"Messages": [rc_flag("GREEN", 0), rc_flag("CHEQUERED", 100), rc_flag("GREEN", 150)]}
    # Q17-02 is independent: Q2 was already established through SessionInfo.
    assert not send("RaceControlMessages", history, 200, True).normal_finished
    send("ExtrapolatedClock", {"Remaining": "10:00", "Extrapolating": True}, 200)
    ordinary = lap(1, 300)
    assert not ordinary.normal_finished
    assert not ordinary.drivers[0].finished
    send("RaceControlMessages", history, 301, True)
    assert not lap(2, 400).drivers[0].finished
    # Predictive finish retains its own reason before the actual flag.
    send("ExtrapolatedClock", {"Remaining": "01:40", "Extrapolating": True}, 400)
    predictive = lap(3, 490).drivers[0]
    assert predictive.finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    assert not service.snapshot.normal_finished
    genuine = {"Messages": {"3": rc_flag("CHEQUERED", 500)}}
    assert send("RaceControlMessages", genuine, 500).normal_finished
    final = lap(4, 590).drivers[0]
    assert final.finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    send("RaceControlMessages", history, 591, True)
    assert service.snapshot.normal_finished  # Reconnect does not undo real finish.
    next_part = send("TimingData", {"SessionPart": 3}, 600)
    assert next_part.session_part == "Q3" and not next_part.normal_finished
    assert not next_part.drivers[0].finished
    assert not send("RaceControlMessages", genuine, 601, True).normal_finished


def test_q2_initial_snapshot_uses_feed_survivors_and_reconnection_keeps_them():
    service = QualiLiveService(eliminated_counts={"Q2": 1})
    store = service.controller.store
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def send(topic, payload):
        service.controller._enqueue_message(LiveTimingMessage(topic, payload, None, now))
        return service.poll()[0]

    send("SessionInfo", {"Name": "Qualifying"})
    send("DriverList", {str(n): {"Tla": f"D{n}"} for n in (1, 2, 3)})
    packet = {"SessionPart": 2, "Lines": {
        "1": {"Position": 1, "KnockedOut": False, "BestLapTime": {"Value": "88"},
              "BestLapTimes": [{"Value": "90"}, {"Value": "88"}, {}]},
        "2": {"Position": 2, "KnockedOut": False, "BestLapTime": {"Value": "89"},
              "BestLapTimes": [{"Value": "91"}, {"Value": "89"}, {}]},
        "3": {"Position": 3, "KnockedOut": True, "BestLapTime": {"Value": "92"},
              "BestLapTimes": [{"Value": "92"}, {}, {}]},
    }}
    initial = send("TimingData", packet)
    assert initial.session_part == "Q2"
    assert {d.driver for d in initial.drivers if d.active} == {"D1", "D2"}
    assert initial.cutoff.current_cutoff_s == 88
    assert initial.cutoff.cutoff_rank == 1
    assert dict(store.snapshot().drivers[0].historical_results) == {"Q1": 90}
    assert next(d for d in initial.drivers if d.driver == "D3").frozen_part == "Q1"
    assert send("TimingData", packet).drivers == initial.drivers
    service.reset()
    assert not service._initial_timing_received


def test_q3_initial_snapshot_keeps_p1_and_both_previous_part_owners():
    service = QualiLiveService()
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def send(topic, payload):
        service.controller._enqueue_message(LiveTimingMessage(topic, payload, None, now))
        return service.poll()[0]

    send("SessionInfo", {"Name": "Qualifying"})
    send("DriverList", {str(n): {"Tla": f"D{n}"} for n in (1, 2, 3, 4)})
    snapshot = send("TimingData", {"SessionPart": 3, "Lines": {
        "1": {"Position": 1, "KnockedOut": False, "BestLapTime": {"Value": "87"},
              "BestLapTimes": [{"Value": "90"}, {"Value": "88"}, {"Value": "87"}]},
        "2": {"Position": 2, "KnockedOut": False, "BestLapTime": {"Value": "89"}},
        "3": {"Position": 3, "KnockedOut": True,
              "BestLapTimes": [{"Value": "92"}, {"Value": "90"}, {}]},
        "4": {"Position": 4, "KnockedOut": True,
              "BestLapTimes": [{"Value": "93"}, {}, {}]},
    }})
    assert snapshot.session_part == "Q3"
    assert snapshot.cutoff.target_kind == "P1" and snapshot.cutoff.cutoff_rank == 1
    assert snapshot.cutoff.current_cutoff_s == 87
    assert {d.driver for d in snapshot.drivers if d.active} == {"D1", "D2"}
    assert next(d for d in snapshot.drivers if d.driver == "D3").frozen_part == "Q2"
    assert next(d for d in snapshot.drivers if d.driver == "D4").frozen_part == "Q1"


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


def test_reverted_best_never_pairs_with_deleted_lap_sectors():
    unconfirmed = QualiLiveService()
    unconfirmed.consume(replace(
        raw([driver(1, 90, sectors=(30, 30, 30))]),
        completed_lap_sequence=(),
    ))
    assert unconfirmed._reference_total_s is None

    service = QualiLiveService(eliminated_counts={"Q1": 0})
    service.consume(raw([driver(1, 91, sectors=(30, 30, 31))]))
    assert service._reference_total_s == 91
    service.consume(raw([driver(1, 90, laps=2, sectors=(30, 30, 30))]))
    assert service._reference_total_s == 90

    reverted = replace(
        driver(1, 90, laps=2, sectors=(30, 30, 30)), best_lap_s=91,
    )
    snapshot = service.consume(raw([reverted]))
    assert snapshot.drivers[0].best_lap_s == 91
    assert service._reference_total_s is None
    assert service._reference_checkpoints == {}
    assert snapshot.drivers[0].projected_lap_s is None

    # The deleted lap's sectors can remain in the next timing update.
    service.consume(raw([reverted]))
    assert service._reference_total_s is None
    service.consume(raw([driver(1, 89, laps=3, sectors=(29, 30, 30))]))
    assert service._reference_total_s == 89
    assert service._reference_checkpoints[("sector", 3)] == 89
    snapshot = service.consume(raw([
        driver(1, 89, laps=3, sectors=(29, 30, 30)),
        driver(2, None, laps=0, sectors=(28, None, None)),
    ]))
    assert next(state for state in snapshot.drivers if state.driver == "D2").projected_lap_s == pytest.approx(28 * 89 / 29)

    service.transition_to("Q2")
    assert service._reference_total_s is None
    service.consume(raw([driver(1, 88, sectors=(29, 29, 30))], part="Q2"))
    assert service._reference_total_s == 88


def test_q3_projects_p1_while_q1_and_q2_keep_elimination_cutoffs():
    service = QualiLiveService(eliminated_counts={"Q1": 1, "Q2": 1, "Q3": 6})
    q1 = service.consume(raw([driver(1, 90), driver(2, 91), driver(3, 92)]))
    assert (q1.cutoff.target_kind, q1.cutoff.cutoff_rank, q1.cutoff.current_cutoff_s) == (
        "CUTOFF", 2, 91,
    )

    service.transition_to("Q2")
    q2 = service.consume(raw([driver(1, 89), driver(2, 90)], part="Q2"))
    assert (q2.cutoff.target_kind, q2.cutoff.cutoff_rank, q2.cutoff.current_cutoff_s) == (
        "CUTOFF", 1, 89,
    )

    service.transition_to("Q3")
    q3 = service.consume(raw([driver(1, 88), driver(2, 89)], part="Q3"))
    assert (q3.cutoff.target_kind, q3.cutoff.cutoff_rank, q3.cutoff.driver) == (
        "P1", 1, "D1",
    )
    assert q3.cutoff.current_cutoff_s == 88


def test_finished_reasons_and_projected_cutoff():
    service = QualiLiveService(
        eliminated_counts={"Q1": 1}, out_lap_s=100, cool_lap_s=110,
        track_rate_s_per_field_push=0.02,
    )
    snapshot = service.consume(raw([driver(1, 90, in_pit=True), driver(2, 91)], remaining=50))
    first = next(s for s in snapshot.drivers if s.driver == "D1")
    assert first.finished_reason == FinishedReason.IN_PIT_NO_OUTLAP_TIME
    assert snapshot.cutoff.unfinished_driver_count == 1
    # Positive evolution means no projected improvement; the observed cutoff
    # remains the safe published threshold.
    assert snapshot.cutoff.projected_cutoff_s == 90
    # A completed push with insufficient cool time has its explicit reason.
    service = with_test_pace_reference(QualiLiveService(cool_lap_s=110))
    service.consume(raw([driver(1, 90, laps=1)], remaining=200))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=100))
    assert snapshot.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    service = with_test_pace_reference(QualiLiveService())
    service.consume(raw([driver(1, 90, laps=1)], remaining=20))
    snapshot = service.consume(replace(raw([driver(1, 89, laps=2)], remaining=0), normal_finished=True))
    assert snapshot.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    # A real chequer applies when any current lap is completed after zero,
    # including a slow lap rather than only a push lap.
    service = QualiLiveService()
    service.consume(replace(raw([driver(1, 90, laps=1)], remaining=0), normal_finished=True))
    snapshot = service.consume(
        replace(raw([driver(1, 120, laps=2, in_pit=True)], remaining=0), normal_finished=True)
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


def test_eliminated_driver_identity_and_tyre_history_survive_both_part_transitions():
    service = QualiLiveService(eliminated_counts={"Q1": 1, "Q2": 1})
    tyres = (QualiTyreStint("SOFT", 0, True, 0),)
    q1 = [driver(1, 90), driver(2, 91), replace(driver(3, 92), tyre_history=tyres)]
    service.consume(raw(q1))
    service.transition_to("Q2")
    q1_out = next(s for s in service.snapshot.drivers if s.driver_number == "3")
    assert (q1_out.driver, q1_out.team, q1_out.tyre_history, q1_out.frozen_part) == (
        "D3", "Team", tyres, "Q1",
    )
    service.consume(raw([driver(1, 89), driver(2, 90)], part="Q2"))
    service.transition_to("Q3")
    q2_out = next(s for s in service.snapshot.drivers if s.driver_number == "2")
    q1_out = next(s for s in service.snapshot.drivers if s.driver_number == "3")
    assert (q2_out.driver, q2_out.team, q2_out.frozen_part) == ("D2", "Team", "Q2")
    assert (q1_out.driver, q1_out.team, q1_out.tyre_history) == ("D3", "Team", tyres)


def test_next_part_clears_completed_final_push_marker_for_advancing_driver():
    service = with_test_pace_reference(QualiLiveService(eliminated_counts={"Q1": 0}, cool_lap_s=110))
    service.consume(raw([driver(1, 90, laps=1)], remaining=200))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=100))
    assert snapshot.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH

    service.transition_to("Q2")

    state = service.snapshot.drivers[0]
    assert state.active is True
    assert state.finished is False
    assert state.finished_reason is None


def test_sq1_sq2_transition_uses_sprint_parts_without_phantom_q1_elimination():
    service = with_test_pace_reference(QualiLiveService(eliminated_counts={"SQ1": 1, "SQ2": 1, "SQ3": 0}))
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
    # Raw field laps alone cannot establish per-driver diagnostic support.
    assert service.track_source == "Configured fallback"


def test_completed_part_without_diagnostic_support_preserves_prior():
    service = with_test_pace_reference(QualiLiveService(eliminated_counts={"Q1": 0}))
    service.consume(raw([driver(1, 91, laps=1), driver(2, 92, laps=1)]))
    service.consume(raw([driver(1, 90, laps=2), driver(2, 91, laps=2)]))
    service.transition_to("Q2")
    assert len(service._track_samples) == 2
    assert service.automatic_track_estimate is None
    assert service.track_source == "Configured fallback"


def test_global_push_index_continues_across_qualifying_parts():
    service = with_test_pace_reference(QualiLiveService(
        eliminated_counts={"Q1": 0, "Q2": 0},
    ))
    service.consume(raw([driver(1, 90, laps=1)]))
    service.consume(raw([driver(1, 89, laps=2)]))
    service.transition_to("Q2")
    service.consume(raw([driver(1, 88, laps=3)], part="Q2"))
    service.consume(raw([driver(1, 87, laps=4)], part="Q2"))
    service.transition_to("Q3")
    service.consume(raw([driver(1, 86, laps=5)], part="Q3"))
    service.consume(raw([driver(1, 85, laps=6)], part="Q3"))
    assert [(sample.part, sample.global_push_index) for sample in service._track_samples] == [
        ("Q1", 1), ("Q2", 2), ("Q3", 3),
    ]


def test_track_evolution_updates_live_before_part_transition(monkeypatch):
    service = with_test_pace_reference(QualiLiveService(
        eliminated_counts={"Q1": 0}, track_rate_s_per_field_push=0.25
    ))
    monkeypatch.setattr(service._physical_traffic, "finalize", lambda *_: PhysicalLapTraffic(4, .8, "ok"))

    def fresh_driver(laps, best):
        return replace(driver(1, best, laps=laps),
                       tyre_history=(QualiTyreStint("SOFT", 0, True, laps),))

    service.consume(raw([fresh_driver(1, 90)]))
    service.consume(raw([fresh_driver(2, 89)]))
    assert service.track_source == "Configured fallback"

    snapshot = service.consume(raw([fresh_driver(3, 88)]))

    assert service.session_part == "Q1"
    assert service.track_source == "Live per-driver diagnostic (session-wide)"
    rate, samples, quality = service.automatic_track_estimate
    assert rate == pytest.approx(-1.0)
    assert (samples, quality) == (2, "estimated")
    assert snapshot.cutoff.track_rate_s_per_field_push == pytest.approx(-1.0)


def test_manual_track_override_does_not_replace_latest_automatic_fit(monkeypatch):
    service = with_test_pace_reference(QualiLiveService(eliminated_counts={"Q1": 0}))
    monkeypatch.setattr(service._physical_traffic, "finalize", lambda *_: PhysicalLapTraffic(4, .8, "ok"))

    def fresh_driver(laps, best):
        return replace(driver(1, best, laps=laps),
                       tyre_history=(QualiTyreStint("SOFT", 0, True, laps),))

    service.consume(raw([fresh_driver(1, 90)]))
    service.consume(raw([fresh_driver(2, 89)]))
    service.consume(raw([fresh_driver(3, 88)]))
    automatic_before = service.automatic_track_rate_s_per_field_push
    assert automatic_before == pytest.approx(-1.0)
    live_source = "Live per-driver diagnostic (session-wide)"
    assert service.snapshot.automatic_track_source == live_source
    manual_override = 0.5
    assert manual_override != pytest.approx(automatic_before)
    service.set_track_override(manual_override)

    # A third fresh-installation observation changes the fitted times from
    # [89, 88] to [89, 88, 86] on global PUSH indices [1, 2, 3].
    snapshot = service.consume(raw([fresh_driver(4, 86)]))
    diagnostic = snapshot.track_evolution_diagnostic
    assert diagnostic.lap_count == 3
    automatic_during_override = service.automatic_track_rate_s_per_field_push
    assert automatic_during_override == pytest.approx(-1.5)
    assert automatic_during_override != pytest.approx(automatic_before)
    assert automatic_during_override == diagnostic.aggregate_slope_s_per_push
    assert snapshot.automatic_track_rate_s_per_field_push == automatic_during_override
    assert service.automatic_track_source == snapshot.automatic_track_source == live_source
    assert service.track_override_s_per_field_push == manual_override
    assert service.track_rate_s_per_field_push == manual_override
    assert snapshot.cutoff.track_rate_s_per_field_push == manual_override
    assert service.track_source == snapshot.track_source == "Manual override"

    service.set_track_override(None)
    # Clearing publishes the latest automatic value immediately, without
    # consuming another observation or rebuilding the diagnostic.
    assert service.track_rate_s_per_field_push == automatic_during_override
    assert service.snapshot.cutoff.track_rate_s_per_field_push == automatic_during_override
    assert service.track_rate_s_per_field_push != pytest.approx(automatic_before)
    assert service.track_source == service.snapshot.track_source == live_source
    assert service.snapshot.automatic_track_source == live_source
    assert service.snapshot.track_evolution_diagnostic is diagnostic


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


def test_effective_finish_requires_a_new_push_and_is_independent_of_session_finish():
    from woplanner.quali.planning import QualiTimingValues
    service = with_test_pace_reference(QualiLiveService(eliminated_counts={"Q1": 0}))
    service.set_operational_timings(QualiTimingValues(cool_lap_s=110), source="test operational policy")
    service.consume(raw([driver(1, 90, laps=1), driver(2, 91, laps=1)], remaining=200))
    unchanged = service.consume(raw([driver(1, 90, laps=1), driver(2, 91, laps=1)], remaining=100))
    assert not any(d.effective_finished for d in unchanged.drivers)
    slow = service.consume(raw([
        driver(1, 120, laps=2, sectors=(34, 30, 30)), driver(2, 91, laps=1),
    ], remaining=100))
    assert not any(d.effective_finished for d in slow.drivers)
    assert not any(sample.driver == "D1" for sample in service._track_samples)
    push = service.consume(raw([driver(1, 89, laps=3), driver(2, 91, laps=1)], remaining=99))
    assert not push.normal_finished
    first = next(d for d in push.drivers if d.driver == "D1")
    assert first.effective_finished
    assert first.effective_finish_recovery_s == 110
    assert first.effective_finish_timing_source == "test operational policy"
    assert not next(d for d in push.drivers if d.driver == "D2").effective_finished
    normal = service.consume(replace(raw([driver(1, 89, laps=3), driver(2, 91, laps=1)], remaining=80), normal_finished=True, normal_finish_source="race_control"))
    assert normal.normal_finished
    assert not next(d for d in normal.drivers if d.driver == "D2").effective_finished


@pytest.mark.parametrize("cool", [50.0, 200.0, None])
@pytest.mark.parametrize("remaining,expected", [(101, False), (100, False), (99, True)])
def test_pit_no_time_marker_uses_existing_out_threshold_without_a_new_push(cool, remaining, expected):
    service = QualiLiveService(out_lap_s=100, cool_lap_s=cool, eliminated_counts={"Q1": 0})
    service.set_manual_operational_timing("out_lap_s", 100)
    pit = replace(
        driver(1, None, laps=0, in_pit=True),
        pit_state=QualiPitState.IN_PIT, structural_state=QualiPitState.IN_PIT,
    )
    service.consume(raw([pit], remaining=300))
    snapshot = service.consume(raw([pit], remaining=remaining))
    state = snapshot.drivers[0]
    assert state.effective_finished is expected
    assert state.finished is expected
    assert state.finished_reason == (FinishedReason.IN_PIT_NO_OUTLAP_TIME if expected else None)
    assert snapshot.cutoff.unfinished_driver_count == (0 if expected else 1)
    assert not snapshot.normal_finished
    assert not service._track_samples
    if expected:
        assert state.effective_finish_remaining_s == remaining
        assert state.effective_finish_recovery_s == 100
        assert state.effective_finish_timing_source == "manual override"
    else:
        assert state.effective_finish_remaining_s is None
        assert state.effective_finish_recovery_s is None
        assert state.effective_finish_timing_source is None


@pytest.mark.parametrize("push_s", [90.0, 300.0])
def test_pit_has_a_new_push_opportunity_even_when_its_completion_is_after_zero(push_s):
    service = with_test_pace_reference(QualiLiveService(
        out_lap_s=100, cool_lap_s=200, eliminated_counts={"Q1": 0},
    ))
    service.set_manual_operational_timing("push_lap_s", push_s)
    pit = replace(
        driver(1, None, laps=0, in_pit=True),
        pit_state=QualiPitState.IN_PIT, structural_state=QualiPitState.IN_PIT,
    )
    # OUT fits before zero, although OUT + PUSH does not. In the existing
    # qualifying model the resulting PUSH can complete after session zero.
    snapshot = service.consume(raw([pit], remaining=150))
    assert service.out_lap_s < 150 < service.out_lap_s + push_s
    state = snapshot.drivers[0]
    assert not state.effective_finished
    assert not state.finished
    assert state.finished_reason is None
    assert snapshot.cutoff.unfinished_driver_count == 1

    final = service.consume(replace(
        raw([driver(1, push_s, laps=1)], remaining=0),
        normal_finished=True, normal_finish_source="clock_zero",
    ))
    assert final.drivers[0].last_lap_s == push_s
    assert final.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED
    assert final.cutoff.unfinished_driver_count == 0


@pytest.mark.parametrize("remaining,expected", [(109, True), (110, False), (150, False)])
def test_on_track_orange_uses_recovery_to_push_start_not_recovery_plus_push(remaining, expected):
    service = with_test_pace_reference(QualiLiveService(cool_lap_s=110))
    service.consume(raw([driver(1, 90, laps=1)], remaining=300))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=remaining))
    assert remaining < service.cool_lap_s + service.operational_timings.push_lap_s
    state = snapshot.drivers[0]
    assert state.effective_finished is expected
    assert state.finished is expected
    assert state.finished_reason == (FinishedReason.NO_TIME_FOR_NEXT_PUSH if expected else None)


@pytest.mark.parametrize("remaining", [None, 0])
def test_pit_no_time_marker_keeps_missing_and_presession_clock_guards(remaining):
    service = QualiLiveService()
    state = service.consume(raw([driver(1, None, laps=0, in_pit=True)], remaining=remaining)).drivers[0]
    assert not state.effective_finished
    assert not state.finished
    assert state.finished_reason is None


@pytest.mark.parametrize("pit_state", [
    QualiPitState.NOT_ACTIVE, QualiPitState.FIRE_UP, QualiPitState.IN_PIT,
    QualiPitState.PIT_LANE, QualiPitState.QUEUE,
])
def test_pit_no_time_marker_reuses_in_pit_detection_and_out_provenance(pit_state):
    service = QualiLiveService(out_lap_s=100)
    service._fp_timing_values["out_lap_s"] = 113.186
    service._fp_timing_sources["out_lap_s"] = "FP-derived (n=4)"
    service._apply_operational_sources()
    pit = replace(driver(1, 90, in_pit=True), pit_state=pit_state)
    state = service.consume(raw([pit], remaining=110)).drivers[0]
    assert state.effective_finished
    assert state.effective_finish_recovery_s == 113.186
    assert state.effective_finish_timing_source == "FP-derived (n=4)"
    assert state.pit_state == pit_state
    assert state.finished_reason == FinishedReason.IN_PIT_NO_OUTLAP_TIME


def test_pit_no_time_marker_latches_and_resets_for_next_segment():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    pit = driver(1, 90, in_pit=True)
    first = service.consume(raw([pit], remaining=50)).drivers[0]
    later = service.consume(raw([replace(pit, pit_state=QualiPitState.FIRE_UP)], remaining=40)).drivers[0]
    assert first.effective_finished and later.effective_finished
    assert later.effective_finish_remaining_s == first.effective_finish_remaining_s == 50
    assert later.effective_finish_recovery_s == first.effective_finish_recovery_s == 100
    service.transition_to("Q2")
    state = service.snapshot.drivers[0]
    assert state.active and not state.finished and not state.effective_finished
    assert state.effective_finish_remaining_s is None
    assert state.effective_finish_recovery_s is None
    assert state.effective_finish_timing_source is None


def test_current_official_best_retraction_is_not_a_historical_minimum():
    service = QualiLiveService(eliminated_counts={"Q1": 1})
    service.consume(raw([driver(1, 95.149, position=1), driver(2, 95.4, position=2)]))

    retracted = service.consume(raw([
        driver(1, None, position=20), driver(2, 95.4, position=1),
    ]))
    lin = next(state for state in retracted.drivers if state.driver == "D1")
    assert lin.best_lap_s is None
    assert lin.rank == 2
    assert [state.driver for state in retracted.drivers] == ["D2", "D1"]
    assert retracted.cutoff.current_cutoff_s == 95.4
    # The official 95.4 s best has no matching sector evidence in this fixture.
    assert service._reference_total_s is None

    replacement = service.consume(raw([
        driver(1, 94.9, laps=2, position=1), driver(2, 95.4, position=2),
    ]))
    assert next(state for state in replacement.drivers if state.driver == "D1").best_lap_s == 94.9

    service.transition_to("Q2")
    assert next(state for state in service.snapshot.drivers if state.driver == "D1").best_lap_s is None


def test_official_position_selects_advancers_when_end_of_part_bests_are_cleared():
    service = QualiLiveService(eliminated_counts={"Q1": 1, "Q2": 0})
    service.consume(raw([
        driver(1, 90, position=1), driver(2, 91, position=2),
        driver(3, 92, position=3),
    ]))
    # The feed clears a valid result just before the segment reset.  Its
    # official position still determines who advances.
    service.consume(raw([
        driver(1, None, position=1), driver(2, 91, position=2),
        driver(3, 92, position=3),
    ]))
    service.transition_to("Q2")
    assert {state.driver for state in service.snapshot.drivers if state.active} == {"D1", "D2"}
    assert all(state.best_lap_s is None for state in service.snapshot.drivers if state.active)


def test_operational_push_is_independent_of_evolution_sample_filter():
    service = with_test_pace_reference(QualiLiveService(cool_lap_s=132.118))
    service.consume(raw([
        driver(1, 93.267, laps=1), driver(2, 95.312, laps=1),
        driver(3, 95.307, laps=1),
    ], remaining=200))
    snapshot = service.consume(raw([
        driver(1, 93.267, laps=1), driver(2, 95.312, laps=2),
        driver(3, 95.307, laps=2),
    ], remaining=77))
    sai = next(state for state in snapshot.drivers if state.driver == "D2")
    alb = next(state for state in snapshot.drivers if state.driver == "D3")
    assert sai.effective_finished and alb.effective_finished
    assert sai.finished_reason == alb.finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    assert not [sample for sample in service._track_samples if sample.driver in {"D2", "D3"}]

    sample_count = len(service._track_samples)
    fast = service.consume(raw([
        driver(1, 93.0, laps=2), driver(2, 95.312, laps=2),
        driver(3, 95.307, laps=2),
    ], remaining=50))
    assert next(state for state in fast.drivers if state.driver == "D1").effective_finished
    assert len(service._track_samples) == sample_count + 1
    assert any(sample.driver == "D1" and sample.lap_number == 2
               for sample in service._track_samples)


def test_presession_zero_does_not_set_driver_finish_and_real_end_upgrades_reason():
    service = with_test_pace_reference(QualiLiveService(cool_lap_s=132.118))
    bootstrap = service.consume(raw([driver(1, None, laps=0, in_pit=True)], remaining=0))
    assert not bootstrap.drivers[0].finished

    service.consume(raw([driver(1, 93.0, laps=1)], remaining=200))
    effective = service.consume(raw([driver(1, 92.0, laps=2)], remaining=77))
    assert effective.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    chequered = service.consume(replace(
        raw([driver(1, 91.0, laps=3)], remaining=0), normal_finished=True,
    ))
    assert chequered.drivers[0].finished_reason == FinishedReason.FINAL_LAP_COMPLETED


@pytest.mark.parametrize(("rate", "expected"), [(-0.038276, 90.387584), (0.0, 91.0), (0.02, 91.0)])
def test_projected_cutoff_uses_rate_sign_and_never_slows_current_cutoff(rate, expected):
    service = QualiLiveService(eliminated_counts={"Q2": 6}, track_rate_s_per_field_push=rate)
    snapshot = service.consume(raw(
        [driver(number, 90 + number / 10) for number in range(1, 17)],
        part="Q2",
    ))
    # P10 is 91.0 in this fixture and all drivers are unfinished.
    assert snapshot.cutoff.current_cutoff_s == 91.0
    assert snapshot.cutoff.projected_cutoff_s == pytest.approx(expected)
    assert snapshot.cutoff.projected_cutoff_s <= snapshot.cutoff.current_cutoff_s


def test_cutoff_with_insufficient_live_support_uses_zero_prior_and_current_result():
    service = QualiLiveService(eliminated_counts={"Q1": 1})
    snapshot = service.consume(raw([driver(1, 90), driver(2, 91)]))
    assert service.track_source == "Configured fallback"
    assert snapshot.cutoff.track_rate_s_per_field_push == 0
    assert snapshot.cutoff.projected_cutoff_s == snapshot.cutoff.current_cutoff_s == 90


def test_effective_finish_timing_hierarchy_loads_fp_and_uses_fp_fallback(monkeypatch):
    service = QualiLiveService()
    durations = pd.DataFrame([
        {"category": "cool_lap_s", "selected_default_s": 111.0, "selected_count": 4},
        {"category": "out_lap_s", "selected_default_s": 99.0, "selected_count": 3},
    ])
    monkeypatch.setattr(
        "wodata.artifacts.read_pre_quali_analysis",
        lambda **_: ({}, durations, {}),
    )
    service._consume_message_snapshot(raw(
        [],
        session_info={
            "StartDate": "2026-09-05T16:00:00Z",
            "Name": "Qualifying",
            "Meeting": {"Number": 13},
        },
    ))
    assert service.operational_timing_sources["cool_lap_s"] == "FP-derived (n=4)"
    assert service._effective_cool_timing(90) == (111.0, "FP-derived (n=4)")

    service.set_manual_operational_timing("cool_lap_s", 112)
    assert service._effective_cool_timing(90) == (112.0, "manual override")

    service.clear_manual_operational_timings()
    service.load_operational_analysis(type("Analysis", (), {
        "durations": pd.DataFrame(),
        "laps": pd.DataFrame([{
            "is_driver_fp_reference": True, "raw_lap_time_s": 92.797,
            "driver": "ANT",
        }]),
    })())
    value, source = service._effective_cool_timing(90)
    assert value == pytest.approx(92.797 * 1.30)
    assert source == "FP best × 1.30 fallback"
    assert service._effective_cool_timing(80)[0] == 92.797 * 1.30
    service.load_operational_analysis(type("Analysis", (), {
        "durations": pd.DataFrame([{
            "category": "push_lap_s", "selected_default_s": 94.865,
            "selected_count": 4,
        }]),
        "laps": pd.DataFrame(),
    })())
    value, source = service._effective_cool_timing(80)
    assert value == pytest.approx(94.865 * 1.30)
    assert source == "FP representative × 1.30 fallback"
    service.load_operational_analysis(type("Analysis", (), {
        "durations": pd.DataFrame(), "laps": pd.DataFrame(),
    })())
    assert service._effective_cool_timing(90) == (None, "unavailable — manual override required")
    service = with_test_pace_reference(QualiLiveService())
    service.consume(raw([driver(1, 90, laps=1)], remaining=200))
    snapshot = service.consume(raw([driver(1, 89, laps=2)], remaining=30))
    assert snapshot.drivers[0].effective_finished is False
    assert snapshot.drivers[0].finished_reason is None


def test_fp_fastest_lap_references_load_with_operational_artifact():
    service = QualiLiveService()
    service.load_operational_analysis(type("Analysis", (), {
        "durations": pd.DataFrame(),
        "laps": pd.DataFrame([{
            "driver": "D1", "is_driver_fp_reference": True,
            "fp_start_to_5pct_s": 5.0, "fp_start_to_s1_s": 30.0,
            "fp_s1_to_s2_s": 31.0,
        }]),
    })())
    reference = service._common_lap_state_reference
    assert (reference.start_to_5pct_s, reference.start_to_s1_s, reference.s1_to_s2_s) == (5, 30, 31)


def test_clean_air_threshold_is_explicit_and_does_not_change_push_classification():
    service = QualiLiveService(clean_air_gap_s=3)
    service.set_clean_air_gap_s(4.5)
    assert service.clean_air_gap_s == 4.5
    with pytest.raises(ValueError):
        service.set_clean_air_gap_s(0)
