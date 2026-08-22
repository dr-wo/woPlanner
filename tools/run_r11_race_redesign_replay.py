#!/usr/bin/env python3
"""Run the isolated Race-specific prototype over representative 2026 R11 laps."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

import diagnose_r11_mc_instability as base
from woplanner.analysis.service import OfflineAnalysisService
from wostrategy.model.pre_race_performance import WeekendModelConfig
from wostrategy.model.race_specific_performance import run_race_specific_model


LAPS = (9, 22, 25, 37, 38, 55, 56, 70)
SEEDS = (1_898_758_267, 1, 2_026, 11_037, 424_242)
OUTPUT = Path("temp/r11_mc_instability_diagnostic")


def main():
    service = OfflineAnalysisService(Path("woData"))
    artifact_started = time.perf_counter()
    prior = service.frozen_pre_race_prior(2026, 11)
    artifact_seconds = time.perf_counter() - artifact_started
    stored = service.live_model_config(2026, 11, "R")
    replay = Path("woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl")
    snapshot, replay_count, invalid = base.replay_snapshot(replay)
    frames, _, _ = base.build_race_laps(snapshot)
    production = pd.read_csv(OUTPUT / "posterior_history.csv").set_index("leader_lap")
    summary_rows, parameter_rows, quality_rows, runtime_rows = [], [], [], []
    for seed in SEEDS:
        config = WeekendModelConfig(
            sample_count=80_000, random_seed=seed,
            fuel_rate_bounds=tuple(stored["fuel_rate_bounds"]),
            track_rate_bounds=tuple(stored["track_rate_bounds"]),
            default_degradation_bounds=tuple(stored["default_degradation_bounds"]),
            default_compound_delta_bounds=tuple(stored["default_compound_delta_bounds"]),
            clean_lap_noise_sigma=float(stored["clean_lap_noise_sigma"]),
            enforce_compound_order=True,
        )
        for lap in LAPS:
            total_started = time.perf_counter()
            result = run_race_specific_model(frames[lap], prior, config, as_of_leader_lap=lap)
            total = time.perf_counter() - total_started
            quality = dict(result.quality)
            evidence = quality.pop("compound_evidence")
            quality.pop("numerical_quality_thresholds", None)
            row = {"leader_lap": lap, "seed": seed, **quality,
                   "production_ess": production.loc[lap, "ess"],
                   "production_max_weight": production.loc[lap, "max_weight"]}
            for compound, item in evidence.items():
                for key, value in item.items():
                    row[f"{compound.lower()}_{key}"] = value
            quality_rows.append(row)
            parameters = {(item.parameter, item.compound): item for item in result.aggregate_snapshot.parameters}
            summary = dict(row)
            for parameter, compound, label in (
                ("net_lap_slope", None, "net_lap_slope"),
                ("fuel_rate", None, "fuel_prior"), ("track_rate", None, "derived_track"),
                ("degradation", "HARD", "hard_degradation"),
                ("degradation", "MEDIUM", "medium_degradation"),
                ("degradation", "SOFT", "soft_degradation"),
            ):
                item = parameters[(parameter, compound)]
                for quantile in ("p10", "median", "p90"):
                    summary[f"{label}_{quantile}"] = getattr(item, quantile)
                summary[f"{label}_width"] = item.p90 - item.p10
                parameter_rows.append({
                    "leader_lap": lap, "seed": seed, "parameter": parameter,
                    "compound": compound, "p10": item.p10, "median": item.median,
                    "p90": item.p90, "p10_p90_width": item.p90 - item.p10,
                    "evidence_status": item.support_status,
                    "clean_laps": item.usable_lap_count, "clean_runs": item.usable_run_count,
                })
            for compound in ("HARD", "MEDIUM", "SOFT"):
                item = parameters[("compound_delta", compound)]
                parameter_rows.append({
                    "leader_lap": lap, "seed": seed, "parameter": "compound_delta",
                    "compound": compound, "p10": item.p10, "median": item.median,
                    "p90": item.p90, "p10_p90_width": item.p90 - item.p10,
                    "evidence_status": item.support_status,
                    "clean_laps": item.usable_lap_count, "clean_runs": item.usable_run_count,
                })
            summary_rows.append(summary)
            runtime_rows.extend(
                {"leader_lap": lap, "seed": seed, "stage": stage, "seconds": seconds}
                for stage, seconds in {"pre_race_artifact_loading": artifact_seconds,
                                       **result.timings, "total_model": total}.items()
            )
    pd.DataFrame(summary_rows).to_csv(OUTPUT / "race_redesign_replay_summary.csv", index=False)
    pd.DataFrame(parameter_rows).to_csv(OUTPUT / "race_redesign_parameter_status.csv", index=False)
    pd.DataFrame(quality_rows).to_csv(OUTPUT / "race_redesign_quality_history.csv", index=False)
    pd.DataFrame(runtime_rows).to_csv(OUTPUT / "race_redesign_runtime.csv", index=False)
    manifest = {"replay_event_count": replay_count, "invalid_replay_records": invalid,
                "representative_laps": LAPS, "seeds": SEEDS,
                "sample_count": 80_000, "prior": asdict(prior)}
    (OUTPUT / "race_redesign_manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(json.dumps({"rows": len(summary_rows), "minimum_ess": min(row["ess"] for row in summary_rows),
                      "maximum_runtime": max(row["seconds"] for row in runtime_rows if row["stage"] == "total_model")}, indent=2))


if __name__ == "__main__":
    main()
