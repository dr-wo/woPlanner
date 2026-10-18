"""R17 late-join regression through ReplayPlayer and production ingress/service."""

from pathlib import Path

import pytest

from wodata.live.replay import ReplayPlayer
from woplanner.quali.models import FinishedReason
from woplanner.quali.service import QualiLiveService


RECORDING_DIR = (
    Path(__file__).resolve().parents[2]
    / "woData/wodata/live_timing/schema_v1/year=2026/round=17/session=Q"
)


def replay_r17():
    recording, = sorted(RECORDING_DIR.glob("*.jsonl"))
    service = QualiLiveService()
    evidence = {"segments": [], "finishes": {}, "final_laps": {},
                "premature_q2_finish": [], "premature_q2_flags": [],
                "q2_ordinary_completions": 0}
    previous_part = None
    previous_laps = 0

    def observe(raw):
        nonlocal previous_part, previous_laps
        service._consume_message_snapshot(raw)
        snapshot = service.snapshot
        part = raw.session_part
        # The default before the first TimingData snapshot is not established.
        if raw.session_part_established and part != previous_part:
            previous_part = part
            evidence["segments"].append({
                "part": part, "remaining": raw.remaining_time_s,
                "active": sum(d.active for d in snapshot.drivers),
                "target": snapshot.cutoff.target_kind,
                "rank": snapshot.cutoff.cutoff_rank,
            })
        if raw.normal_finished:
            evidence["finishes"].setdefault(part, raw.normal_finish_source)
        new_laps = raw.completed_lap_sequence[previous_laps:]
        previous_laps = len(raw.completed_lap_sequence)
        for lap in new_laps:
            state = next(d for d in snapshot.drivers if d.driver_number == lap.driver_number)
            if part == "Q2" and not raw.normal_finished:
                evidence["q2_ordinary_completions"] += 1
            if state.finished_reason == FinishedReason.FINAL_LAP_COMPLETED:
                evidence["final_laps"].setdefault(part, []).append(state.driver)
        # Q2 finishes at 14:10 UTC in the original feed, independently of our
        # reducer's segment and finish interpretations.
        if part == "Q2" and raw.last_message_at_utc.isoformat() < "2026-10-10T14:10:00":
            if raw.normal_finished:
                evidence["premature_q2_finish"].append(raw.last_message_at_utc.isoformat())
            flags = [d.driver for d in snapshot.drivers
                     if d.finished_reason == FinishedReason.FINAL_LAP_COMPLETED]
            if flags:
                evidence["premature_q2_flags"].append(flags)
        # Record the live cutoff with timing results, not just an empty Q3 reset.
        if snapshot.cutoff.current_cutoff_s is not None:
            evidence.setdefault("last_targets", {})[part] = {
                "kind": snapshot.cutoff.target_kind, "rank": snapshot.cutoff.cutoff_rank,
                "driver": snapshot.cutoff.driver, "time": snapshot.cutoff.current_cutoff_s,
            }

    service.controller.on_message_snapshot = observe

    def ingress(message):
        service.controller._enqueue_message(message)
        service.controller.drain(max_messages=1)
        if message.sequence in {6, 12, 2751, 4064, 4196, 7566}:
            raw = service.raw_snapshot
            evidence.setdefault("checkpoints", {})[str(message.sequence)] = {
                "part": raw.session_part, "finished": raw.normal_finished,
                "source": raw.normal_finish_source,
                "active": sum(d.active for d in service.snapshot.drivers),
            }

    result = ReplayPlayer(recording, speed=0).play(ingress)
    evidence["recording"] = str(recording)
    evidence["message_count"] = result.messages_emitted
    evidence["status"] = result.status
    evidence["actual_cutoffs"] = dict(service.snapshot.actual_cutoffs)
    return evidence


def assert_r17(evidence):
    assert evidence["status"] == "completed"
    assert evidence["message_count"] == 8134
    assert [row["part"] for row in evidence["segments"]] == ["Q2", "Q3"]
    assert evidence["segments"][0] == {
        "part": "Q2", "remaining": 512, "active": 16, "target": "CUTOFF", "rank": 10,
    }
    assert evidence["segments"][1]["active"] == 10
    assert not evidence["premature_q2_finish"]
    assert not evidence["premature_q2_flags"]
    assert evidence["q2_ordinary_completions"] > 0
    assert evidence["checkpoints"]["2751"]["source"] == "race_control"
    assert evidence["checkpoints"]["2751"]["finished"]
    assert evidence["checkpoints"]["4064"] == {
        "part": "Q3", "finished": False, "source": None, "active": 10,
    }
    assert not evidence["checkpoints"]["4196"]["finished"]
    assert evidence["checkpoints"]["7566"]["source"] == "race_control"
    assert evidence["checkpoints"]["7566"]["finished"]
    assert evidence["final_laps"]["Q2"] and evidence["final_laps"]["Q3"]
    assert evidence["last_targets"]["Q2"]["rank"] == 10
    assert evidence["last_targets"]["Q3"]["kind"] == "P1"
    assert evidence["last_targets"]["Q3"]["rank"] == 1
    assert evidence["actual_cutoffs"]["Q2"] == pytest.approx(92.602)
    evidence["scenario_results"] = dict.fromkeys((
        "initial_Q2", "no_premature_Q2_chequered", "genuine_Q2_finish",
        "actual_Q3", "genuine_Q3_finish", "segment_cutoff_and_P1",
    ), "PASS")


@pytest.mark.skipif(not RECORDING_DIR.exists(), reason="Original R17-Q recording unavailable")
def test_original_r17_join_finish_transition_and_cutoff_semantics():
    assert_r17(replay_r17())


if __name__ == "__main__":
    import json
    evidence = replay_r17()
    assert_r17(evidence)
    print(json.dumps(evidence, indent=2))
