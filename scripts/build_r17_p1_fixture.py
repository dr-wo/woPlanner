"""Extract immutable R17 L12–L14 MC inputs from original recorded prefixes.

Run from woPlanner with ../.venv/bin/python scripts/build_r17_p1_fixture.py.
Only committed laps/sectors, driver identities and scheduled distance are saved.
No cache, fitted result, later message, or reconstructed future gap is consulted.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from wodata.live import LiveSessionStore, LiveTimingMessage
from wodata.live.replay.schema import parse_timestamp


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=root.parent / "woData")
    parser.add_argument("--output", type=Path,
                        default=root / "tests/fixtures/r17_p1_prefixes.json.gz")
    args = parser.parse_args()
    relative = "wodata/live_timing/schema_v1/year=2026/round=17/session=R/live_timing_raw.jsonl"
    store = LiveSessionStore()
    prefix_hash = hashlib.sha256()
    cuts = {}
    with (args.data_root / relative).open("rb") as source:
        for raw in source:
            prefix_hash.update(raw)
            value = json.loads(raw)
            store.reduce(LiveTimingMessage(
                topic=value["topic"], payload=value["payload"],
                source_timestamp_utc=parse_timestamp(value.get("source_timestamp_utc")),
                received_at_utc=parse_timestamp(value["received_at_utc"]),
                schema_version=value["message_schema_version"], source=value["source"],
                sequence=value["sequence"], session_time_seconds=value.get("session_time_seconds"),
            ))
            if value["topic"] != "TimingData" or not isinstance(value["payload"], dict):
                continue
            lines = value["payload"].get("Lines", {})
            if not any(isinstance(patch, dict) and "NumberOfLaps" in patch for patch in lines.values()):
                continue
            snapshot = store.snapshot()
            leader_lap = max((lap.lap_number for lap in snapshot.completed_laps if lap.position == 1), default=0)
            if leader_lap in (12, 13, 14) and str(leader_lap) not in cuts:
                cuts[str(leader_lap)] = {
                    "source_sequence": value["sequence"],
                    "source_timestamp_utc": value.get("source_timestamp_utc"),
                    "prefix_sha256": prefix_hash.hexdigest(),
                    "drivers": [asdict(driver) for driver in snapshot.drivers],
                    "completed_laps": [asdict(lap) for lap in snapshot.completed_laps],
                    "completed_sectors": [asdict(sector) for sector in snapshot.completed_sectors],
                    "scheduled_total_laps": snapshot.scheduled_total_laps,
                }
            if len(cuts) == 3:
                break
    assert set(cuts) == {"12", "13", "14"}, "The recording does not contain all requested cuts"
    result = {"source": relative, "description": "Original delivery-prefix inputs; sector traffic must be explicitly selected.", "cuts": cuts}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(result, default=lambda v: v.isoformat(), allow_nan=False, separators=(",", ":")).encode()
    args.output.write_bytes(gzip.compress(data, mtime=0))
    print(json.dumps({"output": str(args.output), "cuts": {
        key: {"sequence": item["source_sequence"], "completed_laps": len(item["completed_laps"])}
        for key, item in cuts.items()
    }}))


if __name__ == "__main__":
    main()
