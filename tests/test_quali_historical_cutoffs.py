"""Historical finalization uses reducer results even after feed best resets."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

import pytest

from wodata.live import LiveTimingMessage
from woplanner.quali.service import QualiLiveService


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class QualifyingFlow:
    def __init__(self):
        self.service = QualiLiveService(eliminated_counts={"Q1": 1, "Q2": 1, "Q3": 0})
        self.send("SessionInfo", {"Name": "Qualifying"}, 0)
        self.clock("18:00", 0, running=True)

    @property
    def raw(self):
        return self.service.controller.store.snapshot()

    def send(self, topic, payload, seconds):
        self.service.controller._enqueue_message(LiveTimingMessage(
            topic, payload, None, NOW, source="f1_archive_replay",
            session_time_seconds=seconds,
        ))
        return self.service.poll()[0]

    def clock(self, remaining, seconds, running=False):
        return self.send("ExtrapolatedClock", {
            "Remaining": remaining, "Extrapolating": running,
        }, seconds)

    def results(self, times, seconds=100):
        positions = {number: rank for rank, (number, _) in enumerate(
            sorted(times.items(), key=lambda item: (item[1] is None, item[1] or float("inf"))), 1)}
        return self.send("TimingData", {"Lines": {
            str(number): {"Position": positions[number], "NumberOfLaps": 1,
                          "BestLapTime": {"Value": "" if time is None else str(time)},
                          "LastLapTime": {"Value": "" if time is None else str(time)}}
            for number, time in times.items()
        }}, seconds)

    def reset_bests(self, numbers, next_part, seconds):
        return self.send("TimingData", {"SessionPart": next_part, "Lines": {
            str(n): {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}}
            for n in numbers
        }}, seconds)

    def complete_q1(self, times):
        self.results(times)
        self.clock("0:00", 1080)
        self.reset_bests(times, 2, 1199)
        assert all(d.best_lap_s is None for d in self.raw.drivers)
        assert all(d.best_lap_s is None for d in self.service.snapshot.drivers)
        return self.clock("15:00", 1200)


def test_completed_q1_cutoff_survives_reset_in_real_controller_transition():
    flow = QualifyingFlow()
    snapshot = flow.complete_q1({1: 90, 2: 91, 3: 92})
    assert dict(snapshot.actual_cutoffs) == {"Q1": 91}
    assert dict(next(d for d in flow.raw.drivers if d.driver_number == "2").historical_results)["Q1"] == 91
    assert {d.driver_number for d in snapshot.drivers if d.active} == {"1", "2"}


def test_completed_q2_cutoff_survives_reset_and_q3_keeps_p1_semantics():
    flow = QualifyingFlow()
    flow.complete_q1({1: 90, 2: 91, 3: 92})
    flow.clock("14:59", 1201, running=True)
    flow.results({1: 88, 2: 89}, 1300)
    flow.clock("0:00", 2100)
    flow.reset_bests((1, 2), 3, 2159)
    assert all(d.best_lap_s is None for d in flow.service.snapshot.drivers)
    snapshot = flow.clock("13:00", 2160)
    assert dict(snapshot.actual_cutoffs) == {"Q1": 91, "Q2": 88}
    assert snapshot.cutoff.target_kind == "P1"
    flow.service.finalise()
    assert dict(flow.service.snapshot.actual_cutoffs) == {"Q1": 91, "Q2": 88, "Q3": None}


@pytest.mark.parametrize("corrected_best, expected, advancing", [
    (93, 92, {"1", "3"}),  # Deleted best reverts to a slower valid lap.
    (None, 92, {"1", "3"}),  # Entire result retracted; never resurrect 91.
    (89, 90, {"1", "2"}),  # Correction changes the last advancing driver.
])
def test_corrected_final_result_changes_historical_boundary(corrected_best, expected, advancing):
    flow = QualifyingFlow()
    flow.results({1: 90, 2: 91, 3: 92})
    positions = {1: 2, 2: 1, 3: 3} if corrected_best == 89 else {1: 1, 3: 2, 2: 3}
    flow.send("TimingData", {"Lines": {
        str(n): {"Position": p, **({"BestLapTime": {"Value": "" if corrected_best is None else str(corrected_best)}}
                                  if n == 2 else {})}
        for n, p in positions.items()
    }}, 101)
    flow.clock("0:00", 1080)
    flow.reset_bests((1, 2, 3), 2, 1199)
    snapshot = flow.clock("15:00", 1200)
    result = next(d for d in flow.raw.drivers if d.driver_number == "2")
    assert dict(result.historical_results)["Q1"] == corrected_best
    assert dict(snapshot.actual_cutoffs)["Q1"] == expected
    assert {d.driver_number for d in snapshot.drivers if d.active} == advancing


@pytest.mark.parametrize("times", [{1: None, 2: None, 3: None}, {1: 90, 2: None, 3: None}])
def test_insufficient_valid_completed_results_remain_unavailable(times):
    snapshot = QualifyingFlow().complete_q1(times)
    assert dict(snapshot.actual_cutoffs) == {"Q1": None}


def test_history_is_authoritative_with_no_live_minima_or_current_row_order():
    flow = QualifyingFlow()
    flow.results({1: 90, 2: 91, 3: 92})
    flow.clock("0:00", 1080)
    flow.reset_bests((1, 2, 3), 2, 1199)
    # Populate completed history by the reducer, then deliberately present
    # different next-part row positions. No live best survives on either side.
    next_raw = flow.service.controller.store.apply(LiveTimingMessage(
        "ExtrapolatedClock", {"Remaining": "15:00"}, None, NOW,
        source="f1_archive_replay", session_time_seconds=1200,
    ))
    next_raw = replace(next_raw, drivers=tuple(
        replace(d, position=4 - int(d.driver_number)) for d in next_raw.drivers))
    snapshot = flow.service.consume(next_raw)
    assert dict(snapshot.actual_cutoffs)["Q1"] == 91


def test_missing_completed_history_does_not_fall_back_to_live_or_projected_cutoff():
    flow = QualifyingFlow()
    live = flow.results({1: 90, 2: 91, 3: 92})
    assert live.cutoff.current_cutoff_s == 91
    flow.service.transition_to("Q2")
    assert dict(flow.service.snapshot.actual_cutoffs)["Q1"] is None


def test_deletion_leaving_too_few_valid_results_remains_unavailable():
    flow = QualifyingFlow()
    flow.results({1: 90, 2: 91, 3: None})
    flow.send("TimingData", {"Lines": {"2": {"BestLapTime": {"Value": ""}}}}, 101)
    flow.clock("0:00", 1080)
    flow.reset_bests((1, 2, 3), 2, 1199)
    snapshot = flow.clock("15:00", 1200)
    assert dict(snapshot.actual_cutoffs)["Q1"] is None


def test_live_outputs_ignore_completed_part_history():
    flow = QualifyingFlow()
    live = flow.results({1: 90, 2: 91, 3: 92})
    raw = replace(flow.raw, drivers=tuple(
        replace(d, historical_results=(("Q1", 999.0),)) for d in flow.raw.drivers))
    updated = flow.service.consume(raw)
    assert asdict(updated.cutoff) == asdict(live.cutoff)
    assert [(d.rank, d.projected_lap_s, d.official_position) for d in updated.drivers] == [
        (d.rank, d.projected_lap_s, d.official_position) for d in live.drivers]


@pytest.mark.parametrize("part, deleted_number, expected", [("Q1", 2, 92), ("Q2", 1, 91)])
@pytest.mark.parametrize("declared_part", ["name", "number"])
@pytest.mark.parametrize("deleted_value", ["", None, "0"])
def test_explicit_post_chequered_deletion_updates_cutoff(
        part, deleted_number, expected, declared_part, deleted_value):
    flow = QualifyingFlow()
    if part == "Q2":
        flow.complete_q1({1: 90, 2: 91, 3: 92})
        flow.clock("14:59", 1201, running=True)
    flow.results({1: 90, 2: 91, **({3: 92} if part == "Q1" else {})},
                 100 if part == "Q1" else 1300)
    flow.clock("0:00", 1080 if part == "Q1" else 2100)
    assert flow.raw.normal_finished
    scope = part if declared_part == "name" else int(part[-1])
    deleted = flow.send("TimingData", {"SessionPart": scope, "Lines": {
        str(deleted_number): {"BestLapTime": {"Value": deleted_value},
                              "LastLapTime": {"Value": ""}},
    }}, 1100 if part == "Q1" else 2110)
    assert next(d for d in deleted.drivers if d.driver_number == str(deleted_number)).best_lap_s is None
    numbers = (1, 2, 3) if part == "Q1" else (1, 2)
    flow.reset_bests(numbers, int(part[-1]) + 1, 1199 if part == "Q1" else 2159)
    snapshot = flow.clock("15:00" if part == "Q1" else "13:00", 1200 if part == "Q1" else 2160)
    result = next(d for d in flow.raw.drivers if d.driver_number == str(deleted_number))
    assert dict(result.historical_results)[part] is None
    assert dict(snapshot.actual_cutoffs)[part] == expected


def test_explicit_post_chequered_deletion_can_make_cutoff_unavailable():
    flow = QualifyingFlow()
    flow.results({1: 90, 2: 91, 3: None})
    flow.clock("0:00", 1080)
    flow.send("TimingData", {"SessionPart": "Q1", "Lines": {
        "2": {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}},
    }}, 1100)
    flow.reset_bests((1, 2, 3), 2, 1199)
    assert dict(flow.clock("15:00", 1200).actual_cutoffs)["Q1"] is None


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


@pytest.mark.skipif(not RECORDING.exists(), reason="R16 live recording unavailable")
def test_r16_completed_cutoffs_match_final_rankings_after_reset():
    spec = importlib.util.spec_from_file_location(
        "r16_historical_cutoffs", ROOT / "scripts/verify_quali_r16_historical_cutoffs.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    result = replay.verify(RECORDING)
    assert result["message_count"] == 16496
    for transition, (part, rank, driver, time) in zip(result["transitions"], [
        ("Q1", 16, "LIN", 97.883), ("Q2", 10, "BOR", 96.814),
    ], strict=True):
        assert transition["completed_part"] == part
        assert transition["boundary_rank"] == rank
        assert transition["historical_threshold"][0] == driver
        assert transition["historical_threshold"][1] == pytest.approx(time)
        assert transition["stored"] == pytest.approx(time)
        assert transition["last_valid_live"]["cutoff"]["current_cutoff_s"] == pytest.approx(time)
        # TimingData.SessionPart now transitions before applying the same
        # packet's clears; the callback still sees the completed-part result.
        assert next(value for code, value in transition["planner_bests_before_callback"]
                    if code == driver) == pytest.approx(time)


@pytest.mark.skipif(not RECORDING.exists(), reason="R16 live recording unavailable")
def test_r16_post_chequered_lin_deletion_moves_historical_cutoff_to_hul():
    spec = importlib.util.spec_from_file_location(
        "r16_post_chequered", ROOT / "scripts/verify_quali_r16_historical_cutoffs.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    result = replay.verify(RECORDING, inject_q1_deletion=True, stop_sequence=7531)
    injection = result["injection"]
    assert injection["chequered"]
    assert injection["retained_before"] == pytest.approx(97.883)
    assert injection["live_best_after"] is None
    assert injection["retained_after"] is None
    transition, = result["transitions"]
    assert dict(transition["history_ranking"])["LIN"] is None
    assert transition["boundary_rank"] == 16
    assert transition["historical_threshold"][0] == "HUL"
    assert transition["historical_threshold"][1] == pytest.approx(97.970)
    assert transition["stored"] == pytest.approx(97.970)
