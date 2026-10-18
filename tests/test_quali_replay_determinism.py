"""R13 integration: compare each canonical transition, not final snapshots.

The archive stays outside git. Set QUALI_R13_ARCHIVE to run elsewhere.
ReplayPlayer's waiting is replaced by a virtual wall clock, so speed 1/25/0
exercise scheduling without spending 77 minutes per replay.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from wodata.live import LiveTimingController, QualiSessionStore
from wodata.live.replay import ReplayPlayer
from wodata.live.session_identity import parse_session_identity
from woplanner.quali.service import QualiLiveService

ARCHIVE = Path(os.environ.get("QUALI_R13_ARCHIVE", str(
    Path(__file__).resolve().parents[2] / "woData/wodata/live_archive/schema_v1/year=2026/round=13/session=Q/replay/merged_replay_v1.jsonl"
)))


def replay_transitions(path, batch_size, speed, reads):
    controller = LiveTimingController(store=QualiSessionStore())
    service = QualiLiveService(controller)
    sequences = {key: [] for key in (
        "identity", "segment", "countdown_finish", "laps", "best", "queue",
        "effective", "planner_target",
    )}
    previous = {}
    evidence = {"segments": [], "effective": [], "alb": [], "zero": [], "normal_flags": [], "ant_first_q3_result": None}
    message_index = 0
    last_lap_count = 0
    virtual_wall = [0.0]

    def changed(key, value):
        if previous.get(key) != value:
            sequences[key].append((message_index, value))
            previous[key] = value
            return True
        return False

    def observe(raw):
        nonlocal message_index, last_lap_count
        message_index += 1
        service._consume_message_snapshot(raw)
        t = controller.store._latest_session_time_s
        identity = parse_session_identity(raw.session_info)
        changed("identity", (identity.year, identity.round_number, identity.session_name, identity.event_name))
        if changed("segment", raw.session_part):
            ant = next((d for d in raw.drivers if d.driver_code == "ANT"), None)
            evidence["segments"].append({"part": raw.session_part, "time": t, "duration": raw.segment_duration_s,
                "ANT": {"best": ant.best_lap_s, "last": ant.last_lap_s, "sectors": ant.sectors_s,
                        "history": ant.historical_results} if ant else None})
        if changed("countdown_finish", (raw.session_part, raw.remaining_time_s, raw.clock_running, raw.normal_finished, raw.normal_finish_source)):
            if raw.normal_finish_source == "race_control":
                item = {"part": raw.session_part, "time": t}
                if not evidence["normal_flags"] or evidence["normal_flags"][-1]["part"] != raw.session_part:
                    evidence["normal_flags"].append(item)
        if raw.session_part == "Q3":
            ant = next(d for d in raw.drivers if d.driver_code == "ANT")
            if t < 4049.759:
                assert ant.best_lap_s is None, "An untimed/past-segment lap became ANT's current Q3 result"
            elif evidence["ant_first_q3_result"] is None and ant.best_lap_s is not None:
                evidence["ant_first_q3_result"] = {"time": t, "best": ant.best_lap_s}
        if raw.normal_finished and raw.remaining_time_s == 0 and (not evidence["zero"] or evidence["zero"][-1]["part"] != raw.session_part):
            evidence["zero"].append({"part": raw.session_part, "time": t, "remaining": 0, "source": raw.normal_finish_source})
        for lap in raw.completed_lap_sequence[last_lap_count:]:
            sequences["laps"].append((message_index, tuple(asdict(lap).values())))
        last_lap_count = len(raw.completed_lap_sequence)
        changed("best", tuple((d.driver_number, d.best_lap_s, d.last_lap_s, d.sectors_s, d.historical_results) for d in sorted(raw.drivers, key=lambda d: d.driver_number)))
        changed("queue", tuple((d.driver_number, d.pit_state.value, d.pit_lane_progress) for d in sorted(raw.drivers, key=lambda d: d.driver_number)))
        effective = tuple((d.driver_number, d.effective_finished, d.effective_finish_remaining_s, d.effective_finish_recovery_s, d.effective_finish_timing_source) for d in sorted(service.snapshot.drivers, key=lambda d: d.driver_number))
        if changed("effective", effective):
            for d in service.snapshot.drivers:
                if d.effective_finished:
                    item = {"part": raw.session_part, "driver": d.driver, "remaining": d.effective_finish_remaining_s, "recovery": d.effective_finish_recovery_s, "source": d.effective_finish_timing_source}
                    if item not in evidence["effective"]:
                        evidence["effective"].append(item)
        cutoff = service.snapshot.cutoff
        changed("planner_target", (
            cutoff.target_kind, cutoff.cutoff_rank, cutoff.driver,
            cutoff.current_cutoff_s, cutoff.projected_cutoff_s,
            tuple((d.driver_number, d.rank, d.best_lap_s) for d in service.snapshot.drivers),
        ))
        alb = next((d for d in raw.drivers if d.driver_code == "ALB"), None)
        if alb and raw.session_part == "Q1":
            if not evidence["alb"] or evidence["alb"][-1]["state"] != alb.pit_state.value:
                evidence["alb"].append({"time": t, "state": alb.pit_state.value, "speed": alb.speed_kph, "rpm": alb.rpm})
        for _ in range(reads):
            assert controller.store.snapshot() is raw
            service.pit_status_groups()

    controller.on_message_snapshot = observe
    pending = 0

    def ingress(message):
        nonlocal pending
        controller._enqueue_message(message)
        pending += 1
        if pending == batch_size:
            controller.drain(max_messages=batch_size)
            pending = 0
            # Empty GUI polls must not repeat consumer domain transitions.
            for _ in range(reads):
                service.poll()

    def virtual_wait(delay, _stop_event):
        virtual_wall[0] += delay
        return True

    with patch.object(ReplayPlayer, "_wait", side_effect=virtual_wait), patch("time.monotonic", side_effect=lambda: virtual_wall[0]):
        result = ReplayPlayer(path, speed=speed).play(ingress)
        controller.drain(max_messages=batch_size)
    assert result.status == "completed"
    evidence["counts"] = {key: len(value) for key, value in sequences.items()}
    evidence["hashes"] = {key: sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for key, value in sequences.items()}
    evidence["message_count"] = message_index
    evidence["automatic_track"] = (
        service.snapshot.automatic_track_rate_s_per_field_push,
        service.snapshot.automatic_track_source,
        service.automatic_track_estimate,
    )
    return sequences, evidence


def run_comparison(path):
    reference = None
    rows = []
    evidence = None
    for batch, speed, reads in ((1, 0, 0), (7, 25, 3), (500, 1, 7)):
        sequences, current = replay_transitions(path, batch, speed, reads)
        if reference is None:
            reference, evidence = sequences, current
        else:
            for key in reference:
                assert sequences[key] == reference[key], f"{key} differs with batch={batch}, speed={speed}"
            assert current["automatic_track"] == evidence["automatic_track"]
        rows.append({"batch": batch, "speed": speed, "extra_reads": reads, "all_sequences_match": True})
        print(f"R13 batch={batch} speed={speed} reads={reads}: all sequences match", flush=True)
    evidence["configurations"] = rows
    assert [s["part"] for s in evidence["segments"]] == ["Q1", "Q2", "Q3"]
    q3 = evidence["segments"][-1]
    assert q3["duration"] == 780 and q3["ANT"]["best"] is None
    assert dict(q3["ANT"]["history"])["Q2"] == pytest.approx(81.882)
    assert [s["part"] for s in evidence["zero"]] == ["Q1", "Q2", "Q3"]
    assert evidence["effective"]
    assert evidence["ant_first_q3_result"]["time"] == 4049.759
    assert evidence["ant_first_q3_result"]["best"] == pytest.approx(82.093)
    assert [s["part"] for s in evidence["normal_flags"]] == ["Q1", "Q2", "Q3"]
    return evidence


@pytest.mark.skipif(not ARCHIVE.exists(), reason="R13 archive not installed; set QUALI_R13_ARCHIVE")
def test_r13_canonical_sequences_are_independent_of_batch_speed_and_reads():
    run_comparison(ARCHIVE)


if __name__ == "__main__":
    result = run_comparison(ARCHIVE)
    output = Path(__file__).resolve().parents[1] / "doc/assets/qualifying_correctness_r13.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(output)
