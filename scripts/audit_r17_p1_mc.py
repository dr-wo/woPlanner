#!/usr/bin/env python3
"""Read-only R17 P1 input audit; no MC filter or model persistence.

Print prefix-cut diagnostics as JSONL, preserving recording delivery order.
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from wodata.live import LiveSessionStore, LiveTimingMessage
from wodata.live.replay.schema import parse_timestamp
from woplanner.analysis.service import _live_analysis_laps, _live_individual_clean_laps

def finite_json(value):
    """Keep unavailable numerical evidence JSON-compatible, without imputation."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_json(item) for item in value]
    return value


def emit(value):
    print(json.dumps(finite_json(value), allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path,
        default=Path(__file__).resolve().parents[2] / "woData",
        help="woData repository/data root (default: workspace sibling)",
    )
    args = parser.parse_args()
    root = args.data_root / "wodata/live_timing/schema_v1/year=2026/round=17/session=R"
    if not any(root.glob("*.jsonl")):
        parser.error(f"No R17 Race recordings found under {root}")
    for path in sorted(root.glob('*.jsonl')):
        store = LiveSessionStore()
        seen = set()
        counts = Counter()
        for line in path.open():
            value = json.loads(line)
            counts[value['topic']] += 1
            message = LiveTimingMessage(
                topic=value['topic'], payload=value['payload'],
                source_timestamp_utc=parse_timestamp(value.get('source_timestamp_utc')),
                received_at_utc=parse_timestamp(value['received_at_utc']),
                schema_version=value['message_schema_version'], source=value['source'],
                sequence=value['sequence'], session_time_seconds=value.get('session_time_seconds'),
            )
            store.reduce(message)
            payload = value['payload']
            if value['topic'] != 'TimingData' or not isinstance(payload, dict):
                continue
            if not any(
                'NumberOfLaps' in patch
                for patch in payload.get('Lines', {}).values()
                if isinstance(patch, dict)
            ):
                continue
            snapshot = store.snapshot()
            leaders = [lap for lap in snapshot.completed_laps if lap.position == 1]
            latest = max((lap.lap_number for lap in leaders), default=0)
            if latest in (12, 13, 14) and latest not in seen:
                seen.add(latest)
                leader = next(lap for lap in leaders if lap.lap_number == latest)
                frame = _live_analysis_laps(
                    snapshot, quick_lap_threshold=1.1, min_clean_laps=4,
                    use_sector_boundary_traffic=True,
                )
                individual = _live_individual_clean_laps(
                    snapshot, quick_lap_threshold=1.1, use_sector_boundary_traffic=True,
                )
                leader_rows = individual.loc[individual.Driver.eq(leader.driver_code)]
                diagnostic_columns = [
                    'LapNumber', 'TrafficGapAheadSeconds', 'TrafficGapBehindSeconds',
                    'IsCleanAirLongRunLap', 'IsQuickLap', 'IsGreenTrackStatus',
                ]
                leader_diagnostic = leader_rows[
                    [column for column in diagnostic_columns if column in leader_rows]
                ].to_dict('records')
                leader_laps = (
                    frame.loc[frame.Driver.eq(leader.driver_code), 'LapNumber'].tolist()
                    if 'Driver' in frame else []
                )
                emit({
                    'recording': path.name, 'cut': latest, 'sequence': value['sequence'],
                    'source_timestamp': value.get('source_timestamp_utc'),
                    'leader': leader.driver_code, 'lap_time': leader.lap_time_s,
                    'completed_laps': len(snapshot.completed_laps),
                    'position_samples_retained': len(snapshot.position_samples),
                    'telemetry_samples_retained': len(snapshot.telemetry_samples),
                    'explicit_sector_clean_laps': len(frame),
                    'explicit_sector_clean_leader_laps': leader_laps,
                    'exclusion_diagnostics': frame.attrs.get('exclusion_diagnostics'),
                })
                emit({'cut': latest, 'leader_individual_laps': leader_diagnostic})
            if latest > 14:
                break
        emit({'recording':path.name, 'topics_read':dict(counts), 'cuts_found':sorted(seen)})


if __name__ == "__main__":
    main()
