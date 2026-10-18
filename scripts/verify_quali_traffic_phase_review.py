"""Read-only R13/R14/R16 availability audit and real-reducer blocker controls.

Use --baseline-adapter with the pre-repair physical_traffic.py to compare both
adapters against the same immutable raw snapshots and unchanged production gate.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd

from wodata.live import LiveTimingMessage, QualiSessionStore
from wodata.live.replay import load_replay, parse_timestamp
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
from woplanner.quali.service import QualiLiveService


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "woData"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def messages(path, round_number):
    if round_number == 16:
        for line in path.open():
            record = json.loads(line)
            yield LiveTimingMessage(
                record["topic"], record["payload"], parse_timestamp(record.get("source_timestamp_utc")),
                parse_timestamp(record["received_at_utc"]), source=record["source"],
                sequence=record["sequence"], session_time_seconds=record.get("session_time_seconds"),
            )
    else:
        events, invalid = load_replay(path)
        assert invalid == 0
        for event in events:
            yield LiveTimingMessage(
                event.topic, event.payload, parse_timestamp(event.original_timestamp),
                datetime(2000, 1, 1, tzinfo=timezone.utc), source=event.source, sequence=event.sequence,
                session_time_seconds=(event.message_session_time_seconds
                                      if event.preserve_message_session_time else event.session_time_seconds),
            )


def metrics(service):
    samples = service._track_samples
    diagnostic = service.track_evolution_diagnostic()
    return {
        "pace_samples": len(samples),
        "available_traffic": sum(s.traffic_status == "ok" and s.physical_mean_ahead_gap_s is not None
                                 for s in samples),
        "diagnostic_laps": diagnostic.lap_count,
        "aggregate": diagnostic.aggregate_slope_s_per_push,
        "automatic_source": service.automatic_track_source,
        "circuit_history_members": len(service._physical_traffic._lap_spans),
        "by_part": {part: {
            "pace_samples": sum(s.part == part for s in samples),
            "available_traffic": sum(s.part == part and s.traffic_status == "ok"
                                     and s.physical_mean_ahead_gap_s is not None for s in samples),
        } for part in ("Q1", "Q2", "Q3")},
    }


def audit(round_number, baseline_class=None):
    if round_number == 16:
        path = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"
    else:
        path = DATA / f"wodata/live_archive/schema_v1/year=2026/round={round_number}/session=Q/replay/merged_replay_v1.jsonl"
    services = {"after": QualiLiveService()}
    if baseline_class:
        services["before"] = QualiLiveService()
        services["before"]._physical_traffic = baseline_class()
    store = QualiSessionStore()
    for service in services.values():
        service._geometry_data_root = DATA
        service.controller.store = store
    message_count = 0
    availability_changes = []
    max_history = {key: 0 for key in services}
    for message in messages(path, round_number):
        raw = store.apply(message)
        for key, service in services.items():
            service._consume_message_snapshot(raw)
            max_history[key] = max(max_history[key], len(service._physical_traffic._lap_spans))
        message_count += 1
        if message_count % 5000 == 0:
            print(f"R{round_number}: {message_count} messages", flush=True)
    if "before" in services:
        before, after = services["before"]._track_samples, services["after"]._track_samples
        assert [(s.part, s.driver, s.lap_number, s.global_push_index, s.lap_time_s) for s in before] == [
            (s.part, s.driver, s.lap_number, s.global_push_index, s.lap_time_s) for s in after]
        for old, new in zip(before, after):
            if (old.traffic_status, old.physical_mean_ahead_gap_s) != (new.traffic_status, new.physical_mean_ahead_gap_s):
                availability_changes.append({"driver": new.driver, "part": new.part,
                                             "lap": new.lap_number, "before": asdict(old), "after": asdict(new)})
    return {
        "round": round_number, "input": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
        "message_count": message_count, "production_field_fraction": services["after"].push_threshold_fraction,
        "clean_air_gap_s": services["after"].clean_air_gap_s,
        "metrics": {key: metrics(service) for key, service in services.items()},
        "maximum_history_members": max_history, "traffic_changes": availability_changes,
    }


def reproductions(adapter_class):
    fixture = load_module("traffic_review_fixture", ROOT / "tests/test_quali_traffic_review_blockers.py")
    _, delayed = fixture.real_field(delay=100, adapter_type=adapter_class)
    delayed_result = delayed.finalize("A", 2)
    store, revoked = fixture.real_field(adapter_type=adapter_class)
    revoked.finalize("A", 2)
    fixture.timing(store, revoked, "A", {"NumberOfLaps": 2, "LastLapTime": {"Value": ""}}, 110.1)
    _, sanity = fixture.real_field(b_speed=900., adapter_type=adapter_class)
    sanity.finalize("A", 2)
    sanity_result = sanity.finalize("B", 2)
    store, pit = fixture.real_field(unfinished_b=True, adapter_type=adapter_class)
    fixture.timing(store, pit, "B", {"InPit": True}, 107.)
    # Old adapter has no independently retained state stream.
    if not hasattr(pit, "_candidate_states"):
        pit._candidate_states = {}
    pit_samples = fixture.samples(pit)
    return {
        "delayed_publication": asdict(delayed_result),
        "retraction": {"end_anchor": revoked._rows["A"][-1]["LapEndTime"],
                       "history": list(revoked._lap_spans)},
        "distance_sanity": {"history": list(sanity._lap_spans), "result": asdict(sanity_result)},
        "pit_transition": {str(t): (None if pd.isna(pit_samples.loc[t, "PhysicalDriverAhead"])
                                    else pit_samples.loc[t, "PhysicalDriverAhead"]) for t in (106.5, 107.)},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-adapter", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, nargs="*", default=[13, 14, 16])
    args = parser.parse_args()
    baseline = (load_module("traffic_review_baseline", args.baseline_adapter).RollingQualiPhysicalTraffic
                if args.baseline_adapter else None)
    result = {"reproductions": {"after": reproductions(RollingQualiPhysicalTraffic)}}
    source_paths = [Path(__file__),
                    ROOT / "src/woplanner/quali/physical_traffic.py",
                    ROOT / "tests/test_quali_traffic_review_blockers.py",
                    DATA / "src/wodata/live/quali_models.py",
                    DATA / "src/wodata/live/quali_reducer.py",
                    ROOT.parent / "woStrategy/src/wostrategy/analysis/traffic.py"]
    result["source_sha256"] = {str(path): sha256(path.read_bytes()).hexdigest() for path in source_paths}
    if baseline:
        result["baseline_adapter_sha256"] = sha256(args.baseline_adapter.read_bytes()).hexdigest()
        result["reproductions"]["before"] = reproductions(baseline)
    result["events"] = []
    for round_number in args.rounds:
        result["events"].append(audit(round_number, baseline))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
