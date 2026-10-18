"""Validate separate P1 fits at immutable R17 prefix cuts without writing models."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from wodata.live import CompletedLap, CompletedSector, LiveDriver
from wodata.live.replay.schema import parse_timestamp
from woplanner.analysis.service import OfflineAnalysisService
from woplanner.analysis.worker import LiveAnalysisInput


def load_prefixes(path):
    evidence = json.loads(gzip.decompress(Path(path).read_bytes()))
    snapshots = {}
    for key, cut in evidence["cuts"].items():
        def records(name, cls):
            return tuple(cls(**{**row, "source_timestamp_utc": parse_timestamp(row["source_timestamp_utc"])})
                         for row in cut[name])
        snapshots[int(key)] = LiveAnalysisInput(
            records("drivers", LiveDriver), records("completed_laps", CompletedLap),
            records("completed_sectors", CompletedSector), (),
            scheduled_total_laps=cut["scheduled_total_laps"],
        )
    return evidence, snapshots


def model_digest(model):
    if model is None:
        return None
    digest = hashlib.sha256(model.sample_bank.candidates.tobytes())
    digest.update(model.candidate_costs.to_csv(index=False).encode())
    return digest.hexdigest()


def json_diagnostics(value):
    if isinstance(value, dict):
        return {key: json_diagnostics(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_diagnostics(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        # Infinite condition numbers are meaningful identifiability diagnostics.
        return str(value)
    return value


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=root.parent / "woData")
    parser.add_argument("--fixture", type=Path, default=root / "tests/fixtures/r17_p1_prefixes.json.gz")
    parser.add_argument("--output", type=Path, default=root / "doc/assets/26r17_p1_reference_validation.json")
    parser.add_argument("--sample-count", type=int, default=2000)
    parser.add_argument("--baseline-service", type=Path, help="Optional service.py copied before this change")
    args = parser.parse_args()
    baseline_class = None
    if args.baseline_service:
        name = "woplanner.analysis._p1_prechange_service"
        spec = importlib.util.spec_from_file_location(name, args.baseline_service)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        baseline_class = module.OfflineAnalysisService
    evidence, snapshots = load_prefixes(args.fixture)
    results = []
    options = dict(year=2026, round_number=17, sample_count=args.sample_count,
                   quick_lap_threshold=1.1, min_clean_laps=4, use_sector_boundary_traffic=True)
    for lap, snapshot in snapshots.items():
        service = OfflineAnalysisService(args.data_root)
        standard = service.recalculate_from_live_snapshot(snapshot=snapshot, persist=False, **options)
        before = dict(service.__dict__)
        standard_hash = model_digest(service._ephemeral_result)
        if baseline_class:
            baseline = baseline_class(args.data_root)
            old = baseline.recalculate_from_live_snapshot(snapshot=snapshot, persist=False, **options)
            assert old.status == standard.status
            assert old.eligible_lap_count == standard.eligible_lap_count
            if service._ephemeral_result is not None:
                np.testing.assert_array_equal(service._ephemeral_result.sample_bank.candidates,
                                              baseline._ephemeral_result.sample_bank.candidates)
                pd.testing.assert_frame_equal(service._ephemeral_result.candidate_costs,
                                              baseline._ephemeral_result.candidate_costs, check_exact=True)
            assert standard_hash == model_digest(baseline._ephemeral_result)
        alternative = service.calculate_p1_reference(
            snapshot=snapshot, driver_code="VER", first_lap=3, last_lap=lap, **options)
        assert set(before) == set(service.__dict__)
        assert all(service.__dict__[key] is value for key, value in before.items()), "P1 fit mutated standard service state"
        assert standard_hash == model_digest(service._ephemeral_result)
        narrow = service.calculate_p1_reference(
            snapshot=snapshot, driver_code="VER", first_lap=12, last_lap=lap, **options)
        assert narrow.status == "waiting-for-clean-laps"
        assert narrow.eligible_lap_count == 0 and not narrow.applicable_parameters
        future = service.calculate_p1_reference(
            snapshot=snapshot, driver_code="VER", first_lap=3, last_lap=lap + 1, **options)
        assert future.status == "invalid-selection"
        results.append({
            "leader_lap": lap, "source_sequence": evidence["cuts"][str(lap)]["source_sequence"],
            "standard_status": standard.status, "standard_clean_laps": standard.eligible_lap_count,
            "standard_numeric_sha256": standard_hash, "prechange_numeric_equal": baseline_class is not None,
            "p1_status": alternative.status, "p1_selected_laps": alternative.selected_lap_numbers,
            "p1_quality": dict(alternative.sampler_quality),
            "p1_exclusions": dict(alternative.exclusion_diagnostics),
            "applicable_parameters": alternative.applicable_parameters,
            "short_interval_status": narrow.status, "short_interval_exclusions": dict(narrow.exclusion_diagnostics),
            "future_interval_status": future.status, "standard_state_unchanged": True,
        })
    output = {"fixture": str(args.fixture.relative_to(root)) if args.fixture.is_relative_to(root) else str(args.fixture),
              "sample_count": args.sample_count, "results": results,
              "note": "Only supplied original prefixes and existing configuration were used. No model writes or future observations."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(json_diagnostics(output), indent=2, default=str, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output), "cuts": [
        {key: row[key] for key in ("leader_lap", "p1_status", "p1_selected_laps", "standard_state_unchanged", "prechange_numeric_equal")}
        for row in results]}))


if __name__ == "__main__":
    main()
