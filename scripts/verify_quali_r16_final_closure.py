"""Compare supported and canonical R16 replay with the task-start player state.

The separate projection-identity verifier covers direct envelopes and Qt. This
verifier covers every state through ReplayPlayer and directory recovery, including
an overlapping duplicate recording, without using candidate selection as oracle.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from wodata.live import LiveTimingController, QualiSessionStore
from wodata.live.recovery import recover_replay_directory
from wodata.live.replay import ReplayPlayer
from woplanner.quali.service import QualiLiveService
from verify_quali_r16_projection_identity import _domain, _load_baseline, RECORDING, ROOT


def verify(baseline: Path, recording: Path = RECORDING):
    old_reducer = _load_baseline("wodata.live._final_closure_reducer",
        baseline / "woData/src/wodata/live/quali_reducer.py").QualiSessionStore
    old_service = _load_baseline("woplanner.quali._final_closure_service",
        baseline / "woPlanner/src/woplanner/quali/service.py").QualiLiveService
    old_player = _load_baseline("wodata.live.replay._final_closure_player",
        baseline / "woData/src/wodata/live/replay/player.py").ReplayPlayer
    old_recovery = _load_baseline("wodata.live._final_closure_recovery",
        baseline / "woData/src/wodata/live/recovery.py")
    streams = {name: [] for name in ("task_start_player", "task_start_canonical", "player", "canonical")}
    old_player(recording, speed=0).play(streams["task_start_player"].append)
    ReplayPlayer(recording, speed=0).play(streams["player"].append)
    with tempfile.TemporaryDirectory(prefix="quali-final-r16-") as temporary:
        directory = Path(temporary)
        sources = [directory / f"live_timing_raw_{i}.jsonl" for i in (1, 2)]
        for path in sources:
            shutil.copyfile(recording, path)
        previous_merge = old_recovery.merge_recordings([old_recovery.load_recording(p) for p in sources])
        previous_count = len(previous_merge)
        previous_topics = Counter(item.message.topic for item in previous_merge)
        # Compare the full task-start recovery result, including its original
        # dedup and canonical JSON serialization, with the repaired path.
        old_canonical = directory / "task_start_writer.jsonl"
        old_recovery._write_canonical(old_canonical, previous_merge)
        old_player(old_canonical, speed=0).play(streams["task_start_canonical"].append)
        recovered = recover_replay_directory(directory, year=2026, round_number=16, session="Q")
        ReplayPlayer(recovered.path, speed=0).play(streams["canonical"].append)
        input_count, merged_count = recovered.report.input_messages, recovered.report.merged_messages
    assert [len(values) for values in streams.values()] == [16496] * 4
    contexts = {}
    for name in streams:
        store_class = old_reducer if name.startswith("task_start") else QualiSessionStore
        service_class = old_service if name.startswith("task_start") else QualiLiveService
        store = store_class()
        service = service_class(LiveTimingController(store=store))
        service._geometry_data_root = ROOT.parent / "woData"
        contexts[name] = (store, service)
    counts, anchors, evidence_index = {}, {}, 0
    canonical_order_differences = Counter()
    raw_fields = [f.name for f in fields(contexts["player"][0].snapshot()) if f.name != "lap_evidence_sequence"]
    for index in range(16496):
        baseline_message = streams["task_start_player"][index]
        old_store, old_service_instance = contexts["task_start_player"]
        old_raw = old_store.apply(baseline_message)
        old_service_instance._consume_message_snapshot(old_raw)
        old_canonical_store, old_canonical_service = contexts["task_start_canonical"]
        old_canonical_raw = old_canonical_store.apply(streams["task_start_canonical"][index])
        old_canonical_service._consume_message_snapshot(old_canonical_raw)
        for name in ("player", "canonical"):
            message = streams[name][index]
            # Canonical scheduling/sequence are permitted to differ. Immutable
            # provenance, arrival, payload and order must agree at every state.
            assert _domain(message, {"playback_time_seconds", "sequence"}) == _domain(
                baseline_message, {"playback_time_seconds", "sequence"}), (name, index, "envelope")
            store, service = contexts[name]
            raw = store.apply(message)
            service._consume_message_snapshot(raw)
            expected_raw = old_canonical_raw if name == "canonical" else old_raw
            for field in raw_fields:
                assert getattr(raw, field) == getattr(expected_raw, field), (name, index, field)
                if getattr(raw, field) != getattr(old_raw, field):
                    assert name == "canonical" and field == "completed_lap_sequence", (name, index, field)
                    assert sorted(raw.completed_lap_sequence, key=lambda e: e.event_key) == sorted(
                        old_raw.completed_lap_sequence, key=lambda e: e.event_key)
                    canonical_order_differences[field] += 1
            assert len(raw.lap_evidence_sequence) == len(old_raw.lap_evidence_sequence)
            actual_evidence = raw.lap_evidence_sequence[evidence_index:]
            assert actual_evidence == expected_raw.lap_evidence_sequence[evidence_index:]
            direct_evidence = old_raw.lap_evidence_sequence[evidence_index:]
            if actual_evidence != direct_evidence:
                assert name == "canonical"
                assert Counter(actual_evidence) == Counter(direct_evidence)
                canonical_order_differences["within_message_evidence_order"] += 1
            assert service.snapshot == old_service_instance.snapshot, (name, index, "service")
            if name == "canonical":
                assert service.snapshot == old_canonical_service.snapshot
            assert service._driver_best_references == old_service_instance._driver_best_references
            assert service._reference_checkpoints == old_service_instance._reference_checkpoints
            assert service._reference_total_s == old_service_instance._reference_total_s
            count = counts.setdefault(name, {}).setdefault(raw.session_part, Counter())
            count["states"] += 1
            count["states_with_reference"] += int(service._reference_total_s is not None)
            count["projections"] += sum(d.projected_lap_s is not None for d in service.snapshot.drivers)
            if index + 1 in (1685, 2465):
                oco = next(d for d in raw.drivers if d.driver_code == "OCO")
                ver = next(d for d in service.snapshot.drivers if d.driver == "VER")
                anchors.setdefault(name, {})[str(index + 1)] = {
                    "oco_key": oco.best_lap_event_key, "reference_s": service._reference_total_s,
                    "ver_projection_s": ver.projected_lap_s,
                }
        evidence_index = len(old_raw.lap_evidence_sequence)
    for name in counts:
        assert anchors[name]["1685"]["oco_key"] == ("Q1", "31", 3, 1791014637.756)
        assert abs(anchors[name]["1685"]["reference_s"] - 98.879) < 1e-10
        assert abs(anchors[name]["2465"]["ver_projection_s"] - 96.72203499686125) < 1e-10
        # Original TimingData explicitly advances at 7525 / 12465, before
        # clock resets at 7531 / 12467. Q17 retires references at those declared
        # boundaries (6 / 2 states earlier); projection outputs are unchanged.
        for part, expected in {"Q1": (5840, 47324), "Q2": (3524, 20123), "Q3": (2882, 14907)}.items():
            assert (counts[name][part]["states_with_reference"], counts[name][part]["projections"]) == expected
    return {
        "recording": str(recording), "recording_sha256": sha256(recording.read_bytes()).hexdigest(),
        "baseline": str(baseline), "states_compared_per_path": 16496,
        "changed_raw_states_vs_corresponding_task_start_path": 0,
        "changed_service_states": 0, "changed_reference_states": 0,
        "preexisting_canonical_order_differences_vs_direct": canonical_order_differences,
        "canonical_input_messages": input_count, "canonical_output_messages": merged_count,
        "task_start_canonical_output_messages": previous_count,
        "recovered_topic_occurrences": dict(Counter(m.topic for m in streams["canonical"]) - previous_topics),
        "counts": counts, "anchors": anchors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--recording", type=Path, default=RECORDING)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.baseline, args.recording)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
