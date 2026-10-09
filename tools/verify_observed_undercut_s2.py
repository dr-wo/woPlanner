"""Replay local R16 and compare every original audit field with accepted B2.

Run from woPlanner with its src and sibling woData/woStrategy src on PYTHONPATH.
This reads the recording and baseline Git source without changing either.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

from wodata.live import LiveSessionStore
from wodata.live.replay import ReplayPlayer
from woplanner.analysis.observed_undercut import build_observed_undercut_estimates


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "479e0f09c6e3f6ed2da8f5bd3b3959b60ee8228d"
SOURCE = "src/woplanner/analysis/observed_undercut.py"
RECORDING = ROOT.parent / (
    "woData/wodata/live_timing/schema_v1/year=2026/round=16/session=R/live_timing_raw.jsonl"
)


def main():
    baseline_source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:{SOURCE}"], cwd=ROOT, text=True,
    )
    baseline = ModuleType("woplanner.analysis._observed_undercut_baseline")
    sys.modules[baseline.__name__] = baseline
    exec(compile(baseline_source, f"{BASELINE}:{SOURCE}", "exec"), baseline.__dict__)

    store = LiveSessionStore()
    replay = ReplayPlayer(RECORDING, speed=0).play(store.reduce)
    assert replay.status == "completed"
    snapshot = store.snapshot()
    options = dict(quick_lap_threshold=1.1, use_sector_boundary_traffic=True)
    before = [row.to_record() for row in baseline.build_observed_undercut_estimates(
        snapshot, **options,
    )]
    after = [row.to_record() for row in build_observed_undercut_estimates(snapshot, **options)]
    original_fields = [{key: value for key, value in row.items() if key != "s2"}
                       for row in after]
    assert original_fields == before, "Original observed-undercut audit changed"
    examples = [row for row in after if row["s2"]["status"] == "available"]
    assert examples, "No real S2 reference pair found"
    for row in examples:
        s2 = row["s2"]
        scale = .5 * (row["pre_lap_duration_s"] / s2["pre_sector_duration_s"]
                      + row["post_lap_duration_s"] / s2["post_sector_duration_s"])
        assert scale == s2["scale_factor"]
        assert ((s2["pre_sector_duration_s"] - s2["post_sector_duration_s"]) * scale
                == s2["lap_equivalent_delta_s"])
    evidence = dict(
        event="2026 R16 Race", recording=str(RECORDING),
        recording_sha256=hashlib.sha256(RECORDING.read_bytes()).hexdigest(),
        baseline_commit=BASELINE,
        baseline_source_sha256=hashlib.sha256(baseline_source.encode()).hexdigest(),
        current_source_sha256=hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest(),
        replay_status=replay.status, replay_messages=replay.messages_emitted,
        completed_laps=len(snapshot.completed_laps),
        completed_sectors=len(snapshot.completed_sectors), clean_lap_options=options,
        eligible_stops=len(after),
        available_full_lap_pairs=sum(row["status"] == "available" for row in after),
        available_s2_pairs=len(examples),
        all_original_audit_fields_identical=True,
        examples=examples,
    )
    output = ROOT / "doc/assets/observed_undercut_s2_r16.json"
    output.write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in evidence.items() if key != "examples"},
                     indent=2))
    print(f"Audit examples: {output}")


if __name__ == "__main__":
    main()
