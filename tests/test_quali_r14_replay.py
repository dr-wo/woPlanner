"""Focused R14 archive regression; the canonical archive remains outside Git."""

from pathlib import Path

import pytest

from wodata.live import QualiSessionStore
from wodata.live.replay import ReplayPlayer
from woplanner.quali.service import QualiLiveService


ARCHIVE = (
    Path(__file__).resolve().parents[2]
    / "woData/wodata/live_archive/schema_v1/year=2026/round=14/session=Q/replay/merged_replay_v1.jsonl"
)


@pytest.mark.skipif(not ARCHIVE.exists(), reason="R14 canonical archive unavailable")
def test_r14_deleted_lap_effective_finish_cutoff_and_bootstrap():
    store = QualiSessionStore()
    service = QualiLiveService()
    evidence = {}
    previous_lap_count = 0

    def observe(message):
        nonlocal previous_lap_count
        raw = store.apply(message)
        service._consume_message_snapshot(raw)
        sequence = message.sequence
        if raw.remaining_time_s == 0 and not evidence.get("prestart"):
            evidence["prestart"] = (sequence, raw.normal_finished,
                                    tuple(state.finished for state in service.snapshot.drivers))
        if raw.session_part == "Q1" and raw.normal_finished and raw.remaining_time_s == 0:
            evidence.setdefault("genuine_q1_end", (sequence, raw.normal_finish_source))
        for lap in raw.completed_lap_sequence[previous_lap_count:]:
            if lap.session_part == "Q1" and lap.driver_number in {"55", "23"}:
                driver = next((state for state in service.snapshot.drivers
                               if state.driver_number == lap.driver_number), None)
                evidence.setdefault("operational", []).append((
                    sequence, lap.driver_number, lap.lap_time_s,
                    raw.remaining_time_s, service.cool_lap_s,
                    driver.effective_finished if driver else None,
                    any(sample.driver == (driver.driver if driver else "")
                        and sample.lap_number == lap.lap_number for sample in service._track_samples),
                ))
        previous_lap_count = len(raw.completed_lap_sequence)
        if sequence == 3794:
            lin = next(state for state in service.snapshot.drivers if state.driver == "LIN")
            raw_lin = next(state for state in raw.drivers if state.driver_code == "LIN")
            evidence["lin"] = (
                raw_lin.best_lap_s, lin.best_lap_s, lin.rank,
                lin.official_position, tuple(state.driver for state in service.snapshot.drivers),
            )
        if sequence == 12574:
            cutoff = service.snapshot.cutoff
            evidence["cutoff"] = (
                cutoff.current_cutoff_s, cutoff.track_rate_s_per_field_push,
                cutoff.unfinished_driver_count, cutoff.projected_correction_s,
                cutoff.projected_cutoff_s,
                service.session_part,
                tuple((state.driver, state.active, state.rank, state.best_lap_s,
                       state.official_position) for state in service.snapshot.drivers),
            )

    ReplayPlayer(ARCHIVE, speed=0).play(observe, end_time_seconds=2700)
    assert evidence["prestart"][1:] == (False, ())
    assert evidence["genuine_q1_end"][0] > evidence["prestart"][0]
    assert evidence["lin"][0:2] == (None, None)
    assert evidence["lin"][3] == 20
    assert evidence["lin"][4].index("LIN") > evidence["lin"][4].index("TSU")
    sai = next(row for row in evidence["operational"] if row[1] == "55" and row[2] == pytest.approx(95.312))
    alb = next(row for row in evidence["operational"] if row[1] == "23" and row[2] == pytest.approx(95.307))
    assert sai[4] == alb[4] == pytest.approx(132.118)
    assert sai[3] == pytest.approx(77, abs=5)
    assert alb[3] == pytest.approx(54, abs=5)
    assert sai[5:] == (True, False)
    assert alb[5:] == (True, False)
    current, rate, unfinished, correction, projected, part, drivers = evidence["cutoff"]
    assert current == pytest.approx(93.588), (part, drivers)
    assert rate == pytest.approx(-0.038276, abs=0.000001)
    assert unfinished == 16
    assert correction == pytest.approx(-0.61242, abs=0.00001)
    assert projected == pytest.approx(92.97558, abs=0.00001)
