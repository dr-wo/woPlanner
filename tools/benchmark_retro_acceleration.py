#!/usr/bin/env python3
"""Reproducible R11 equivalence, runtime, chunk, and identifiability evidence."""

from __future__ import annotations

import gc
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import diagnose_r11_mc_instability as replay
from audit_retro_vs_live_semantics import config
from wostrategy.algorithm.monte_carlo_race_performance import (
    EVALUATOR_ACCELERATED,
    EVALUATOR_LEGACY,
    MonteCarloRacePerformanceAlgorithm,
)
from wostrategy.model.correction_identifiability import diagnose_retro_fuel_track
from wostrategy.model.live_retro_performance import prepare_live_retro_laps, run_live_retro_model

ROOT = Path(__file__).resolve().parents[2]
LAPS = (9, 22, 37, 55, 70)


def max_differences(left: pd.DataFrame, right: pd.DataFrame):
    numeric = left.select_dtypes(include="number").columns.intersection(
        right.select_dtypes(include="number").columns
    )
    absolute = np.abs(left[numeric].to_numpy(float) - right[numeric].to_numpy(float))
    scale = np.maximum(np.abs(left[numeric].to_numpy(float)),
                       np.abs(right[numeric].to_numpy(float)))
    relative = np.divide(absolute, scale, out=np.zeros_like(absolute), where=scale > 0)
    return float(np.nanmax(absolute, initial=0)), float(np.nanmax(relative, initial=0))


def weighted_quantiles(values, weights):
    order = np.argsort(values, kind="mergesort")
    cumulative = np.cumsum(weights[order])
    return [float(values[order[min(np.searchsorted(cumulative, q), len(order) - 1)]])
            for q in (.1, .5, .9)]


def output_bytes(result):
    return sum(getattr(result, name).memory_usage(index=True, deep=True).sum()
               for name in result.__dataclass_fields__)


def approximate_working_array_bytes(prepared, sample_count, chunk_size, cfg):
    compounds = prepared.Compound.nunique()
    pairs = len(prepared[["Team", "Compound"]].drop_duplicates())
    groups = len(prepared[["Team", "Driver"] if cfg.baseline_group == "driver" else ["Team"]]
                 .drop_duplicates())
    nonreference = compounds - int(cfg.compound_delta_reference.upper() in set(prepared.Compound))
    dimensions = 2 + compounds + nonreference + pairs
    # Unit bank + transformed compound/delta/variation/team-rate arrays + baselines/RMSE.
    persistent_doubles = sample_count * (
        dimensions + compounds + nonreference + 2 * pairs + groups + 3
    )
    # Effect and residual are simultaneously live for one candidate chunk.
    chunk_doubles = min(sample_count, chunk_size) * len(prepared) * 2
    return 8 * (persistent_doubles + chunk_doubles)


def main():
    settings = json.loads((ROOT / "woData/wostrategy/race_performance_review/schema_v1/"
        "year=2026/round=11/session=R/race_performance_2026_11_R_metadata.json").read_text())
    snapshot, _, _ = replay.replay_snapshot(ROOT / "woData/wodata/live_archive/schema_v1/"
        "year=2026/round=11/session=R/replay/merged_replay_v1.jsonl")
    frames, _, _ = replay.build_race_laps(snapshot)

    equivalence = []
    identifiability = []
    for lap in LAPS:
        prepared = prepare_live_retro_laps(frames[lap], race_lap_count=70)
        base = config(settings, 5_000)
        legacy = MonteCarloRacePerformanceAlgorithm(
            replace(base, evaluator_backend=EVALUATOR_LEGACY)
        ).run(prepared)
        accelerated = MonteCarloRacePerformanceAlgorithm(
            replace(base, evaluator_backend=EVALUATOR_ACCELERATED, candidate_chunk_size=4_000)
        ).run(prepared)
        for table in legacy.__dataclass_fields__:
            left, right = getattr(legacy, table), getattr(accelerated, table)
            maximum_absolute, maximum_relative = max_differences(left, right)
            equivalence.append({
                "leader_lap": lap, "comparison": "table", "quantity": table,
                "rows": len(left), "max_absolute_difference": maximum_absolute,
                "max_relative_difference": maximum_relative,
                "exact_non_numeric_and_order": left.drop(columns=left.select_dtypes("number").columns).equals(
                    right.drop(columns=right.select_dtypes("number").columns)),
            })
        for implementation, result in (("legacy", legacy), ("accelerated", accelerated)):
            raw = result.sample_parameters.Weight.to_numpy(float)
            weights = raw / raw.sum()
            quantities = {
                "rmse": result.sample_parameters.RMSESeconds.to_numpy(float),
                "raw_weight": raw,
                "normalized_weight": weights,
                "fuel": result.sample_parameters.FuelRateSecondsPerLap.to_numpy(float),
                "track": result.sample_parameters.TrackRateSecondsPerLap.to_numpy(float),
            }
            for compound, group in result.compound_degradation.groupby("Compound", sort=True):
                quantities[f"degradation:{compound}"] = group.CompoundDegSecondsPerLap.to_numpy(float)
            for compound, group in result.compound_delta.groupby("Compound", sort=True):
                quantities[f"delta:{compound}"] = group.CompoundDeltaSeconds.to_numpy(float)
            for (team, compound), group in result.team_compound_degradation.groupby(
                ["Team", "Compound"], sort=True
            ):
                quantities[f"team_degradation:{team}:{compound}"] = (
                    group.TeamCompoundDegSecondsPerLap.to_numpy(float)
                )
            for key, group in result.baseline_pace.groupby("BaselineGroupKey", sort=True):
                quantities[f"baseline:{key}"] = group.CorrectedBaselinePaceSeconds.to_numpy(float)
            if implementation == "legacy":
                legacy_quantities = quantities
                legacy_aggregates = {
                    "ess": np.array([1 / np.sum(weights**2)]),
                    **{f"{name}:quantiles": np.array(weighted_quantiles(value, weights))
                       for name, value in quantities.items() if name != "normalized_weight"},
                }
            else:
                accelerated_quantities = quantities
                accelerated_aggregates = {
                    "ess": np.array([1 / np.sum(weights**2)]),
                    **{f"{name}:quantiles": np.array(weighted_quantiles(value, weights))
                       for name, value in quantities.items() if name != "normalized_weight"},
                }
        for name in legacy_quantities:
            absolute = np.abs(legacy_quantities[name] - accelerated_quantities[name])
            scale = np.maximum(np.abs(legacy_quantities[name]),
                               np.abs(accelerated_quantities[name]))
            relative = np.divide(absolute, scale, out=np.zeros_like(absolute), where=scale > 0)
            equivalence.append({
                "leader_lap": lap, "comparison": "candidate", "quantity": name,
                "rows": len(absolute), "max_absolute_difference": float(absolute.max(initial=0)),
                "max_relative_difference": float(relative.max(initial=0)),
                "exact_non_numeric_and_order": True,
            })
        for name in legacy_aggregates:
            absolute = np.abs(legacy_aggregates[name] - accelerated_aggregates[name])
            scale = np.maximum(np.abs(legacy_aggregates[name]), np.abs(accelerated_aggregates[name]))
            relative = np.divide(absolute, scale, out=np.zeros_like(absolute), where=scale > 0)
            equivalence.append({
                "leader_lap": lap, "comparison": "posterior", "quantity": name,
                "rows": len(absolute), "max_absolute_difference": float(absolute.max(initial=0)),
                "max_relative_difference": float(relative.max(initial=0)),
                "exact_non_numeric_and_order": True,
            })
        diagnostic = diagnose_retro_fuel_track(prepared, baseline_group=base.baseline_group)
        identifiability.append({
            "leader_lap": lap, "clean_laps": len(prepared),
            "baseline_groups": diagnostic.baseline_group_count,
            "parameters": "|".join(diagnostic.parameter_names),
            "components": "|".join(diagnostic.component_names), "rank": diagnostic.rank,
            "singular_value_1": diagnostic.singular_values[0],
            "singular_value_2": diagnostic.singular_values[1],
            "normalized_singular_value_2": diagnostic.normalized_singular_values[1],
            "condition_number": diagnostic.condition_number,
            "fuel_track_sensitivity_correlation": diagnostic.sensitivity_correlation[0][1],
            "identifiable_combination": "track_rate - fuel_rate",
            "scope": diagnostic.scope, "projection": diagnostic.projection,
        })
        del legacy, accelerated
        gc.collect()

    prepared = prepare_live_retro_laps(frames[70], race_lap_count=70)
    chunk_rows = []
    for chunk in (1_000, 2_000, 4_000, 8_000, 16_000):
        algorithm = MonteCarloRacePerformanceAlgorithm(replace(
            config(settings, 80_000), evaluator_backend=EVALUATOR_ACCELERATED,
            candidate_chunk_size=chunk,
        ))
        started = time.perf_counter(); result = algorithm.run(prepared); elapsed = time.perf_counter() - started
        chunk_rows.append({"sample_count": 80_000, "chunk_size": chunk,
                           "repetition": 1, "total_runtime_seconds": elapsed,
                           "output_memory_mib": output_bytes(result) / 2**20,
                           "approximate_working_array_mib": approximate_working_array_bytes(
                               prepared, 80_000, chunk, algorithm.config
                           ) / 2**20,
                           **algorithm.last_timings})
        del result, algorithm
        gc.collect()

    runtime_detail = []
    for count in (40_000, 80_000):
        for repetition in range(1, 6):
            cfg = replace(config(settings, count), evaluator_backend=EVALUATOR_ACCELERATED,
                          candidate_chunk_size=2_000)
            started = time.perf_counter()
            live = run_live_retro_model(frames[70], cfg, as_of_leader_lap=70,
                                        race_lap_count=70)
            elapsed = time.perf_counter() - started
            runtime_detail.append({
                "sample_count": count, "repetition": repetition,
                "full_live_service_runtime_seconds": elapsed,
                "output_memory_mib": output_bytes(live.retro_result) / 2**20,
                "approximate_working_array_mib": approximate_working_array_bytes(
                    prepared, count, 2_000, cfg
                ) / 2**20,
                **live.timings,
            })
            del live
            gc.collect()
    details = pd.DataFrame(runtime_detail)
    runtime = []
    metric_columns = [column for column in details if column.endswith("_seconds")]
    for count, group in details.groupby("sample_count"):
        row = {"sample_count": count, "repetitions": len(group),
               "output_memory_mib": group.output_memory_mib.max(),
               "approximate_working_array_mib": group.approximate_working_array_mib.max()}
        for metric in metric_columns:
            values = group[metric].to_numpy(float)
            row[f"{metric}_median"] = np.median(values)
            row[f"{metric}_p90"] = np.quantile(values, .9)
            row[f"{metric}_maximum"] = values.max()
        runtime.append(row)

    pd.DataFrame(equivalence).to_csv(ROOT / "retro_acceleration_equivalence.csv", index=False)
    pd.DataFrame(runtime).to_csv(ROOT / "retro_acceleration_runtime.csv", index=False)
    pd.DataFrame(chunk_rows).to_csv(ROOT / "retro_acceleration_chunk_benchmark.csv", index=False)
    pd.DataFrame(identifiability).to_csv(ROOT / "identifiability_diagnostics.csv", index=False)
    details.to_csv(ROOT / "temp/retro_acceleration_runtime_detail.csv", index=False)
    print(json.dumps({
        "maximum_equivalence_absolute": max(row["max_absolute_difference"] for row in equivalence),
        "runtime": runtime, "chunks": chunk_rows,
    }, default=float))


if __name__ == "__main__":
    main()
