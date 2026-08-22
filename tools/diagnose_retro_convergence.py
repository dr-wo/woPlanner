#!/usr/bin/env python3
"""Vectorized, production-cross-checked convergence diagnostics for retro Race MC."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import diagnose_r11_mc_instability as replay
from wostrategy.algorithm.monte_carlo_race_performance import (
    MonteCarloRacePerformanceAlgorithm,
)
from wostrategy.algorithm.sampling import get_unit_cube_sampler
from wostrategy.model.live_retro_performance import prepare_live_retro_laps

from audit_retro_vs_live_semantics import config

OUTPUT = Path("temp/r11_retro_convergence")
LAPS = (9, 22, 25, 37, 38, 55, 56, 70)
COUNTS = (5_000, 10_000, 20_000, 40_000, 80_000)
SEEDS = (1_898_758_267, 1, 2_026, 11_037, 424_242)
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


def candidates(prepared: pd.DataFrame, cfg, count: int, seed: int):
    compounds = tuple(sorted(prepared.Compound.unique()))
    pairs = tuple(sorted(map(tuple, prepared[["Team", "Compound"]].drop_duplicates().to_numpy())))
    nonreference = tuple(c for c in compounds if c != cfg.compound_delta_reference)
    dimensions = 2 + len(compounds) + len(nonreference) + len(pairs)
    unit = get_unit_cube_sampler(cfg.sampling_strategy).sample(
        sample_count=count, dimension_count=dimensions, seed=seed
    )
    fuel = cfg.fuel_rate_bounds[0] + unit[:, 0] * np.ptp(cfg.fuel_rate_bounds)
    track_bounds = (max(0.0, cfg.track_rate_bounds[0]), cfg.track_rate_bounds[1])
    track = track_bounds[0] + unit[:, 1] * np.ptp(track_bounds)
    offset = 2
    degradation = {}
    previous = None
    # Exact equal-bound hot-track transformation used by R11 retro config.
    degradation_units = {c: unit[:, offset + i] for i, c in enumerate(compounds)}
    offset += len(compounds)
    for compound in ("HARD", "MEDIUM", "SOFT"):
        if compound not in degradation_units:
            continue
        low, high = cfg.compound_degradation_bounds.get(
            compound, cfg.default_compound_degradation_bounds
        )
        if previous is not None:
            low = np.maximum(low, previous)
        value = low + degradation_units[compound] * (high - low)
        degradation[compound] = value
        previous = value
    delta_units = {c: unit[:, offset + i] for i, c in enumerate(nonreference)}
    offset += len(nonreference)
    delta = {cfg.compound_delta_reference: np.zeros(count)}
    previous_delta = None
    for compound in ("SOFT", "MEDIUM", "HARD"):
        if compound not in compounds or compound == cfg.compound_delta_reference:
            if compound == cfg.compound_delta_reference:
                previous_delta = np.zeros(count)
            continue
        low, high = cfg.compound_delta_bounds.get(compound, cfg.default_compound_delta_bounds)
        if compound in ("SOFT", "MEDIUM"):
            high = min(high, 0.0)
        if previous_delta is not None:
            low = np.maximum(low, previous_delta)
        value = low + delta_units[compound] * (high - low)
        delta[compound] = value
        previous_delta = value
    variations = {}
    for pair in pairs:
        base = degradation[pair[1]]
        limit = np.maximum(cfg.team_variation_fraction * np.abs(base),
                           cfg.team_variation_absolute_min)
        variations[pair] = (2 * unit[:, offset] - 1) * limit
        offset += 1
    return fuel, track, degradation, delta, variations, pairs, dimensions


def evaluate(prepared, cfg, generated, chunk=5_000):
    fuel, track, degradation, delta, variations, pairs, dimensions = generated
    n = len(fuel)
    y = prepared.LapTimeSeconds.to_numpy(float)
    fuel_x = prepared.FuelProxyLapsRemaining.to_numpy(float) - cfg.fuel_ref
    race_ref = prepared.LapNumber.mean() if cfg.race_lap_ref is None else cfg.race_lap_ref
    track_x = prepared.LapNumber.to_numpy(float) - race_ref
    age = prepared.TyreAgeLaps.to_numpy(float) - cfg.tyre_age_ref
    compounds = prepared.Compound.astype(str).to_numpy()
    team_pairs = list(zip(prepared.Team.astype(str), compounds))
    groups, _ = pd.factorize(prepared.Team if cfg.baseline_group == "team"
                             else prepared.Team.astype(str) + "||" + prepared.Driver.astype(str),
                             sort=True)
    rmse = np.empty(n)
    baseline_representative = np.empty(n)
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        effect = fuel[start:stop, None] * fuel_x + track[start:stop, None] * track_x
        for i, (pair, compound, tyre_age) in enumerate(zip(team_pairs, compounds, age)):
            rate = degradation[compound][start:stop] + variations[pair][start:stop]
            effect[:, i] += rate * tyre_age + delta[compound][start:stop]
        residual = y - effect
        first_baseline = None
        for code in range(groups.max() + 1):
            mask = groups == code
            mean = residual[:, mask].mean(axis=1, keepdims=True)
            if first_baseline is None:
                first_baseline = mean[:, 0]
            residual[:, mask] -= mean
        rmse[start:stop] = np.sqrt(np.mean(residual**2, axis=1))
        baseline_representative[start:stop] = first_baseline
    raw = np.exp(-(rmse**2) / (2 * cfg.clean_lap_noise_sigma**2))
    weights = raw / raw.sum()
    return rmse, weights, baseline_representative, dimensions


def quantiles(values, weights):
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return tuple(float(values[order[np.searchsorted(cumulative, q)]]) for q in (.1, .5, .9))


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    settings = json.loads(Path(
        "woData/wostrategy/race_performance_review/schema_v1/year=2026/round=11/session=R/"
        "race_performance_2026_11_R_metadata.json"
    ).read_text())
    snapshot, _, _ = replay.replay_snapshot(Path(
        "woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl"
    ))
    frames, _, _ = replay.build_race_laps(snapshot)
    rows = []
    for lap in LAPS:
        for seed in SEEDS:
            for count in COUNTS:
                cfg = config(settings, count)
                cfg = cfg.__class__(**{**cfg.__dict__, "random_seed": seed})
                prepared = prepare_live_retro_laps(frames[lap], race_lap_count=70)
                generated_at = time.perf_counter()
                generated = candidates(prepared, cfg, count, seed)
                generation_seconds = time.perf_counter() - generated_at
                evaluated_at = time.perf_counter()
                rmse, weights, baseline, dimension_count = evaluate(prepared, cfg, generated)
                evaluation_seconds = time.perf_counter() - evaluated_at
                fuel, track, degradation, delta, variations, pairs, _ = generated
                parameter_values = {"fuel": fuel, "track": track, "baseline:first": baseline}
                parameter_values.update({f"deg:{c}": v for c, v in degradation.items()})
                parameter_values.update({f"delta:{c}": v for c, v in delta.items()})
                for pair in pairs[:1]:
                    parameter_values[f"team_deg:{pair[0]}:{pair[1]}"] = (
                        degradation[pair[1]] + variations[pair]
                    )
                aggregate_at = time.perf_counter()
                common = {"leader_lap": lap, "seed": seed, "sample_count": count,
                          "dimension_count": dimension_count, "clean_laps": len(prepared),
                          "ess": 1 / np.sum(weights**2), "ess_fraction": 1 / np.sum(weights**2) / count,
                          "max_weight": weights.max(), "best_rmse": rmse.min(),
                          "weighted_rmse": np.sum(weights * rmse),
                          "candidate_generation_seconds": generation_seconds,
                          "candidate_evaluation_seconds": evaluation_seconds}
                for name, values in parameter_values.items():
                    p10, median, p90 = quantiles(values, weights)
                    rows.append({**common, "parameter": name, "p10": p10,
                                 "median": median, "p90": p90, "width": p90 - p10})
                aggregation_seconds = time.perf_counter() - aggregate_at
                for row in rows[-len(parameter_values):]:
                    row["posterior_aggregation_seconds"] = aggregation_seconds
                    row["model_runtime_seconds"] = generation_seconds + evaluation_seconds + aggregation_seconds
    output = pd.DataFrame(rows)
    output.to_csv(OUTPUT / "retro_mc_convergence.csv", index=False)
    sensitivity = output.groupby(["leader_lap", "sample_count", "parameter"]).agg(
        median_seed_mean=("median", "mean"), median_seed_min=("median", "min"),
        median_seed_max=("median", "max"), p10_seed_min=("p10", "min"),
        p10_seed_max=("p10", "max"), p90_seed_min=("p90", "min"),
        p90_seed_max=("p90", "max"), posterior_width_mean=("width", "mean"),
        ess_mean=("ess", "mean"), ess_fraction_mean=("ess_fraction", "mean"),
    ).reset_index()
    sensitivity["median_seed_span"] = sensitivity.median_seed_max - sensitivity.median_seed_min
    sensitivity["median_span_over_width"] = sensitivity.median_seed_span / sensitivity.posterior_width_mean.replace(0, np.nan)
    sensitivity.to_csv(OUTPUT / "retro_mc_seed_sensitivity.csv", index=False)
    runtime = output.drop_duplicates(["leader_lap", "seed", "sample_count"])
    runtime.to_csv(OUTPUT / "retro_mc_runtime_scaling.csv", index=False)
    print(json.dumps({"runs": len(runtime), "rows": len(output),
                      "max_runtime": runtime.model_runtime_seconds.max()}))


if __name__ == "__main__":
    main()
