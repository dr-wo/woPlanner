"""Read-only Phase 0 audit; source/publication clocks never imply live arrival.

The speed-history comparison is a causal evidence counterfactual, not a
production fix. Temporal completeness does not certify route, traffic or fits.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right, insort
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np

from wodata.live import LiveTimingMessage, QualiSessionStore
from wodata.live.models import PublicationTime
from wodata.live.reducer import _decode_compressed_payload
from wodata.live.replay import load_replay, parse_timestamp


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "woData"
WINDOWS = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0)


def distribution(values):
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    values = np.asarray(values, dtype=float)
    return {"count": len(values), **{
        key: float(np.quantile(values, fraction))
        for key, fraction in (("p50", .5), ("p95", .95), ("p99", .99))
    }, "max": float(np.max(values))}


def sampling(records):
    differences = []
    duplicates = out_of_order = 0
    for observations in records.values():
        times = [row[0] for row in observations]
        duplicates += len(times) - len(set(times))
        out_of_order += sum(b < a for a, b in zip(times, times[1:]))
        ordered = sorted(set(times))
        differences.extend(b - a for a, b in zip(ordered, ordered[1:]))
    return {"observations": sum(map(len, records.values())),
            "drivers": len(records), "duplicates": duplicates,
            "out_of_order_in_delivery": out_of_order,
            "interval_seconds": distribution(differences),
            "intervals_over_1_second": sum(value > 1.000001 for value in differences)}


def temporal_complete(rows, start, end):
    times = sorted({row[0] for row in rows})
    return (len(times) >= 2 and abs(times[0] - start) <= .5
            and abs(times[-1] - end) <= .5
            and all(b - a <= 1.0 for a, b in zip(times, times[1:])))


def paths():
    result = []
    for round_number in (12, 13, 14, 16, 17):
        if round_number in (12, 13):
            path = DATA / f"wodata/live_archive/schema_v1/year=2026/round={round_number}/session=Q/replay/merged_replay_v1.jsonl"
        else:
            directory = DATA / f"wodata/live_timing/schema_v1/year=2026/round={round_number}/session=Q"
            path, = sorted(directory.glob("*.jsonl"))
        result.append((round_number, path))
    return result


def audit(round_number, path):
    events, invalid = load_replay(path)
    assert invalid == 0
    real_arrival = all(event.original_received_at is not None
                       and event.source == "signalr_live" for event in events)
    store = QualiSessionStore()
    speed_times, speed_values = defaultdict(list), defaultdict(dict)
    speeds, positions = defaultdict(list), defaultdict(list)
    boundaries, completions = {}, {}
    publication_owners = defaultdict(set)
    invalidations = set()
    sync = Counter()
    batch_sizes = defaultdict(list)
    parts, statuses = [], []
    evidence_index = 0
    first_received = last_received = None
    for delivery, event in enumerate(events):
        receipt = parse_timestamp(event.original_received_at)
        message = LiveTimingMessage(
            event.topic, event.payload, parse_timestamp(event.original_timestamp),
            receipt or datetime(2000, 1, 1, tzinfo=timezone.utc),
            source=event.source, sequence=event.sequence,
            session_time_seconds=event.message_session_time_seconds,
        )
        raw = store.apply(message)
        arrival = receipt.timestamp() if real_arrival else None
        if receipt:
            first_received = first_received or receipt.isoformat()
            last_received = receipt.isoformat()
        if raw.session_part_established and (not parts or parts[-1]["part"] != raw.session_part):
            parts.append({"part": raw.session_part, "sequence": event.sequence})
        if event.topic == "SessionStatus":
            statuses.append({"payload": event.payload, "source": event.original_timestamp,
                             "arrival": event.original_received_at})
        if event.topic in {"CarData", "CarData.z"}:
            payload = _decode_compressed_payload(event.payload)
            count = 0
            for entry in payload.get("Entries", ()):
                stamp = store._sample_time(entry.get("Utc"), message)
                if stamp is None:
                    continue
                for number, car in entry.get("Cars", {}).items():
                    value = car.get("Channels", {}).get("2")
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(value) or value < 0:
                        continue
                    number = str(number)
                    coordinate = ("session" if (message.session_time_seconds is not None
                                  and message.source_timestamp_utc is not None)
                                  or (store._utc_offset_s is not None and message.source_timestamp_utc is None)
                                  else "utc")
                    speeds[number].append((stamp, arrival, delivery, coordinate))
                    if stamp not in speed_values[number]:
                        insort(speed_times[number], stamp)
                    speed_values[number][stamp] = value
                    count += 1
            batch_sizes["speed"].append(count)
        if event.topic in {"Position", "Position.z"}:
            batch_sizes["position"].append(len(raw.position_observations))
        drivers = {driver.driver_number: driver for driver in raw.drivers}
        for observation in raw.position_observations:
            number, stamp = observation.driver_number, observation.session_time_s
            driver = drivers.get(number)
            latest = driver.latest_telemetry_time_s if driver else None
            value = driver.speed_kph if driver else None
            legacy = (latest is not None and value is not None and value >= 0
                      and 0 <= stamp - latest <= 5.)
            index = bisect_right(speed_times[number], stamp) - 1
            causal = index >= 0 and 0 <= stamp - speed_times[number][index] <= 5.
            sync["positions"] += 1
            sync["legacy_temporal_matches"] += bool(legacy)
            sync["causal_temporal_matches"] += bool(causal)
            if latest is not None and latest > stamp:
                sync["future_speed_rejections"] += 1
                sync["future_rejections_with_received_earlier_speed"] += bool(causal)
            elif not legacy:
                sync["missing_or_stale_speed_rejections"] += 1
            coordinate = ("session" if (message.session_time_seconds is not None
                          and message.source_timestamp_utc is not None)
                          or (store._utc_offset_s is not None and message.source_timestamp_utc is None)
                          else "utc")
            positions[number].append((stamp, arrival, delivery, causal, legacy, coordinate))
        for evidence in raw.traffic_evidence_sequence[evidence_index:]:
            publication = evidence.publication_time
            if evidence.kind == "lap_counter" and publication is not None:
                key = (number := evidence.driver_number, evidence.lap_number)
                previous = boundaries.get(key)
                boundary = (publication.coordinate, publication.seconds, arrival, delivery)
                publication_owners[(number, publication)].add(evidence.lap_number)
                if previous is None:
                    boundaries[key] = boundary
                elif previous[:2] != boundary[:2]:
                    boundaries[key] = ("ambiguous", None, None, None)
            elif evidence.kind == "completion" and publication is not None:
                completed = next((lap for lap in reversed(raw.completed_lap_sequence)
                                  if lap.event_key == evidence.completion_key), None)
                if completed is not None and completed.classification == "normal_timed":
                    completions.setdefault(completed.event_key, (completed, publication, arrival, delivery))
            elif evidence.kind == "completion_invalidated":
                invalidations.add(evidence.completion_key)
        evidence_index = len(raw.traffic_evidence_sequence)

    late_position, useful_late_position, late_speed = [], [], []
    completeness = {str(window): Counter() for window in WINDOWS}
    gap_summary = Counter()
    laps = []
    for key, (lap, publication, completion_arrival, completion_delivery) in completions.items():
        if key in invalidations:
            gap_summary["invalidated_completions"] += 1
            continue
        start_boundary = boundaries.get((lap.driver_number, lap.lap_number - 1))
        end_boundary = boundaries.get((lap.driver_number, lap.lap_number))
        if (start_boundary is None or start_boundary[0] != publication.coordinate
                or end_boundary is None or end_boundary[:2] != (publication.coordinate, publication.seconds)
                or len(publication_owners[(lap.driver_number, publication)]) != 1
                or abs(publication.seconds - start_boundary[1] - lap.lap_time_s) > 1.0):
            gap_summary["unanchored_or_inconsistent_completed_laps"] += 1
            continue
        start, end = start_boundary[1], publication.seconds
        if len(publication_owners[(lap.driver_number, PublicationTime(start_boundary[0], start))]) != 1:
            gap_summary["unanchored_or_inconsistent_completed_laps"] += 1
            continue
        if (positions[lap.driver_number]
                and not any(row[5] == publication.coordinate for row in positions[lap.driver_number])):
            gap_summary["incomparable_sample_boundary_clock_laps"] += 1
            continue
        pos = [row for row in positions[lap.driver_number]
               if row[5] == publication.coordinate and start < row[0] <= end]
        spd = [row for row in speeds[lap.driver_number]
               if row[3] == publication.coordinate and start < row[0] <= end]
        gap_summary["anchored_laps"] += 1
        times = sorted({row[0] for row in pos})
        interior = [b - a for a, b in zip(times, times[1:]) if b - a > 1.0]
        gap_summary["laps_with_recorded_interior_position_gaps"] += bool(interior)
        gap_summary["recorded_interior_position_gaps"] += len(interior)
        gap_summary["eventual_position_temporally_complete_laps"] += temporal_complete(pos, start, end)
        gap_summary["eventual_causal_speed_temporally_complete_laps"] += temporal_complete(
            [row for row in pos if row[3]], start, end)
        lap_delays = []
        if real_arrival and completion_arrival is not None:
            seen_positions = set()
            for row in pos:
                # First receipt of a timestamp is the useful evidence arrival;
                # a later duplicate does not demonstrate finalisation benefit.
                if row[0] not in seen_positions and row[3] and row[2] > completion_delivery and row[1] > completion_arrival:
                    useful_late_position.append(row[1] - completion_arrival)
                seen_positions.add(row[0])
            for records, destination in ((pos, late_position), (spd, late_speed)):
                for row in records:
                    if row[2] > completion_delivery and row[1] > completion_arrival:
                        delay = row[1] - completion_arrival
                        destination.append(delay)
                        if records is pos:
                            lap_delays.append(delay)
            for window in WINDOWS:
                available = [row for row in pos if row[1] <= completion_arrival + window
                             and (window > 0 or row[2] <= completion_delivery)]
                metric = completeness[str(window)]
                metric["raw_position_temporally_complete_laps"] += temporal_complete(available, start, end)
                metric["causal_speed_temporally_complete_laps"] += temporal_complete(
                    [row for row in available if row[3]], start, end)
                metric["positions_within_window"] += len(available)
                metric["valid_causal_positions_within_window"] += sum(row[3] for row in available)
        laps.append({"part": lap.session_part, "driver": lap.driver_number,
                     "lap": lap.lap_number, "start": start, "end": end,
                     "position_count": len(pos), "recorded_gap_count": len(interior),
                     "maximum_source_gap_s": max(interior, default=None),
                     "late_position_count": len(lap_delays),
                     "maximum_late_arrival_s": max(lap_delays, default=None)})
    return {
        "round": round_number, "path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
        "messages": len(events), "real_arrival_timestamps": real_arrival,
        "receipt_range": [first_received, last_received], "parts": parts,
        "session_status_messages": statuses, "sampling": {"position": sampling(positions), "speed": sampling(speeds)},
        "batch_observation_counts": {kind: distribution(values) for kind, values in batch_sizes.items()},
        "synchronisation": dict(sync), "source_gap_summary": dict(gap_summary),
        "position_after_completion_arrival_s": distribution(late_position) if real_arrival else None,
        "unique_causal_position_after_completion_arrival_s": distribution(useful_late_position) if real_arrival else None,
        "speed_after_completion_arrival_s": distribution(late_speed) if real_arrival else None,
        "late_position_two_second_coverage": (sum(delay <= 2 for delay in late_position) / len(late_position)
                                              if late_position else None),
        "unique_causal_late_position_two_second_coverage": (sum(delay <= 2 for delay in useful_late_position) / len(useful_late_position)
                                                           if useful_late_position else None),
        "window_temporal_completeness": {key: dict(value) for key, value in completeness.items()} if real_arrival else None,
        "laps": laps,
        "limitations": [
            "Completeness here is time coverage only: no route, candidate state, circuit sanity, 50% ahead coverage or fitting eligibility is inferred.",
            "Boundary ownership uses independently received counter publications that agree with completed lap duration within the unchanged 1s anchor tolerance.",
            "Causal match uses all speed samples already received at each position packet, never subsequently received or future-timestamp speeds.",
            "True received_at_utc is used only for signalr_live captures; archive delivery/source coordinates do not establish live latency.",
            "Recorded gaps establish missing observations in these files; upstream acquisition gaps cannot be distinguished from incomplete recording.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"gate": "INSUFFICIENT EVIDENCE", "sessions": [], "scope": "Phase 0 only; production unchanged"}
    for round_number, path in paths():
        result["sessions"].append(audit(round_number, path))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        summary = result["sessions"][-1]
        print(json.dumps({key: summary[key] for key in ("round", "messages", "real_arrival_timestamps", "synchronisation", "source_gap_summary", "position_after_completion_arrival_s", "late_position_two_second_coverage", "window_temporal_completeness")}), flush=True)
    sources = [Path(__file__), DATA / "src/wodata/live/quali_reducer.py", DATA / "src/wodata/live/replay/player.py",
               ROOT / "src/woplanner/quali/physical_traffic.py", ROOT / "src/woplanner/quali/service.py",
               ROOT.parent / "woStrategy/src/wostrategy/analysis/traffic.py"]
    result["source_sha256"] = {str(path): sha256(path.read_bytes()).hexdigest() for path in sources}
    result["gate_reason"] = "Only three independently recorded Qualifying sessions have true message-arrival clocks; minimum is R17 plus three other comparable sessions. Archive source/sample time and ordered replay are excluded as latency evidence."
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
