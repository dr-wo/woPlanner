"""Replay current production R16 state; compare all non-cutoff-history snapshots.

Run once with --output before editing, then with --compare and --output after.
Original publication/receive timestamps and production operational inputs are used.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from wodata.live import LiveTimingMessage
from woplanner.quali.service import QualiLiveService


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _raw_snapshot_hash(raw, ledger_cache):
    state = {field.name: getattr(raw, field.name) for field in fields(raw)}
    # This verifier compares ordinary timing/projection state. The new bounded
    # traffic histories and delivery metadata have their own Q17-03 verifier;
    # including them here changes the old hash contract and serializes thousands
    # of unrelated speed rows for every timing message.
    for name in ("speed_history", "speed_observations", "speed_freshness_s",
                 "message_source", "traffic_clock_time_s", "traffic_clock_kind",
                 "receive_time_trusted"):
        state.pop(name, None)
    state["session_info"] = dict(raw.session_info)
    # Immutable cumulative evidence is unchanged on most packets. Hash each
    # append once; rebuild on replacement so corrected provenance is compared.
    for name in ("completed_lap_sequence", "lap_evidence_sequence"):
        items = state[name]
        previous, digest = ledger_cache.get(name, ((), sha256()))
        if items != previous:
            if len(items) >= len(previous) and items[:len(previous)] == previous:
                digest = digest.copy()
                additions = items[len(previous):]
            else:
                digest = sha256()
                additions = items
            for item in additions:
                digest.update(json.dumps(asdict(item), sort_keys=True).encode() + b"\n")
            ledger_cache[name] = (items, digest)
        state[name] = {"count": len(items), "sha256": digest.hexdigest()}
    encoded = json.dumps(state, sort_keys=True, default=lambda value:
                         dict(value) if isinstance(value, Mapping) else
                         asdict(value) if is_dataclass(value) else value.isoformat())
    return sha256(encoded.encode()).hexdigest()


def verify(recording=RECORDING, *, inject_q1_deletion=False, stop_sequence=None):
    service = QualiLiveService()
    service._geometry_data_root = ROOT.parent / "woData"
    store = service.controller.store
    hashes, raw_hashes, transitions = [], [], []
    raw_ledger_cache = {}
    injection = None
    recent = deque(maxlen=12)
    last_valid = {}
    for line in Path(recording).read_text().splitlines():
        record = json.loads(line)
        message = LiveTimingMessage(
            topic=record["topic"], payload=record["payload"],
            source_timestamp_utc=_stamp(record.get("source_timestamp_utc")),
            received_at_utc=_stamp(record["received_at_utc"]),
            schema_version=record.get("message_schema_version", 1),
            source=record["source"], sequence=record["sequence"],
            session_time_seconds=record.get("session_time_seconds"),
        )
        previous_part = service.session_part
        active_numbers = {d.driver_number for d in service.snapshot.drivers if d.active}
        before = [(d.driver, d.best_lap_s) for d in service.snapshot.drivers if d.active]
        raw = store.apply(message)
        if message.topic in {"TimingData", "TimingStats", "ExtrapolatedClock", "SessionInfo"}:
            if message.topic in {"TimingData", "TimingStats"}:
                lines = message.payload.get("Lines", message.payload)
                patch = {n: p for n, p in lines.items() if isinstance(p, dict)
                         and any(k in p for k in ("BestLapTime", "BestLap", "LastLapTime"))}
            else:
                patch = message.payload
            if patch:
                recent.append({"sequence": message.sequence, "topic": message.topic,
                               "source_time": record.get("source_timestamp_utc"), "patch": patch})
        service._consume_message_snapshot(raw)
        if inject_q1_deletion and message.sequence == 7524:
            assert raw.session_part == "Q1" and raw.normal_finished
            driver = next(d for d in raw.drivers if d.driver_code == "LIN")
            packet = replace(
                message, topic="TimingData", source="synthetic_test", sequence=0,
                payload={"SessionPart": raw.session_part, "Lines": {
                    driver.driver_number: {"BestLapTime": {"Value": ""},
                                           "LastLapTime": {"Value": ""}},
                }},
                source_timestamp_utc=(message.source_timestamp_utc + timedelta(microseconds=1)
                                      if message.source_timestamp_utc else None),
                received_at_utc=message.received_at_utc + timedelta(microseconds=1),
                session_time_seconds=(message.session_time_seconds + .000001
                                      if message.session_time_seconds is not None else None),
            )
            retained_before = store._timing[driver.driver_number].get("_segment_best_s")
            raw = store.apply(packet)
            service._consume_message_snapshot(raw)
            deleted = next(d for d in raw.drivers if d.driver_number == driver.driver_number)
            injection = {"after_sequence": message.sequence, "payload": packet.payload,
                         "chequered": raw.normal_finished,
                         "retained_before": retained_before,
                         "retained_after": store._timing[driver.driver_number].get("_segment_best_s"),
                         "live_best_after": deleted.best_lap_s,
                         "live_cutoff_after": asdict(service.snapshot.cutoff)}
        if raw.session_part != previous_part:
            results = sorted(
                [(d.driver_code, dict(d.historical_results).get(previous_part))
                 for d in raw.drivers if d.driver_number in active_numbers],
                key=lambda row: (row[1] is None, row[1] or float("inf"), row[0]),
            )
            rank = len(results) - service.eliminated_counts[previous_part]
            transitions.append({
                "sequence": message.sequence, "topic": message.topic,
                "completed_part": previous_part, "new_part": raw.session_part,
                "boundary_rank": rank, "history_ranking": results,
                "historical_threshold": results[rank - 1] if rank > 0 else None,
                "last_valid_live": last_valid.get(previous_part),
                "planner_bests_before_callback": before,
                "raw_current_bests": [(d.driver_code, d.best_lap_s) for d in raw.drivers],
                "stored": dict(service.snapshot.actual_cutoffs).get(previous_part),
                "recent_messages": list(recent),
            })
        elif service.snapshot.cutoff.current_cutoff_s is not None:
            last_valid[raw.session_part] = {
                "sequence": message.sequence,
                "ranking": [(d.driver, d.best_lap_s) for d in sorted(
                    (d for d in service.snapshot.drivers if d.active), key=lambda d: d.rank)],
                "cutoff": asdict(service.snapshot.cutoff),
            }
        state = asdict(service.snapshot)
        state.pop("actual_cutoffs")
        hashes.append(sha256(json.dumps(state, sort_keys=True).encode()).hexdigest())
        raw_hashes.append(_raw_snapshot_hash(raw, raw_ledger_cache))
        if stop_sequence is not None and message.sequence >= stop_sequence:
            break
    return {"message_count": len(hashes), "non_history_snapshot_hashes": hashes,
            "raw_snapshot_hashes": raw_hashes, "injection": injection,
            "transitions": transitions, "actual_cutoffs": dict(service.snapshot.actual_cutoffs)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, default=RECORDING)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--inject-q1-deletion", action="store_true")
    parser.add_argument("--stop-sequence", type=int)
    args = parser.parse_args()
    result = verify(args.recording, inject_q1_deletion=args.inject_q1_deletion,
                    stop_sequence=args.stop_sequence)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        assert result["non_history_snapshot_hashes"] == baseline["non_history_snapshot_hashes"]
        assert result["raw_snapshot_hashes"] == baseline["raw_snapshot_hashes"]
        print(f"All {result['message_count']} non-history snapshots unchanged")
    print(json.dumps({"actual_cutoffs": result["actual_cutoffs"], "transitions": [
        {k: t[k] for k in ("sequence", "completed_part", "boundary_rank", "historical_threshold", "stored")}
        for t in result["transitions"]]}, indent=2))
