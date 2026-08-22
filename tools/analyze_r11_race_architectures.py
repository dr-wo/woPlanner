#!/usr/bin/env python3
"""Diagnostic-only comparison of alternative 2026 R11 Race MC architectures."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, qmc

import diagnose_r11_mc_instability as base


COUNT = 80_000
SEEDS = (1_898_758_267, 1, 2_026, 11_037, 424_242)
NAMES = (
    "fuel_rate", "track_rate:R", "degradation:R:HARD",
    "degradation:R:MEDIUM", "degradation:R:SOFT",
    "compound_delta:R:MEDIUM", "compound_delta:R:SOFT",
)
BOUNDS = np.asarray(((0, .1), (-.05, .05), (0, .5), (0, .5), (0, .5),
                     (-1.2, .5), (-1.2, .5)), dtype=float)


def weighted_quantile(values, weights, probability):
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    return float(values[np.searchsorted(np.cumsum(weights), probability, side="left")])


def weights(sse, sigma=.5):
    cost = sse / sigma**2
    value = np.exp(-.5 * (cost - cost.min()))
    return value / value.sum()


def broad_samples(seed):
    unit = qmc.LatinHypercube(7, seed=seed).random(COUNT)
    out = np.empty_like(unit)
    out[:, 0] = .1 * unit[:, 0]
    out[:, 1] = -.05 + .1 * unit[:, 1]
    out[:, 2] = .5 * unit[:, 2]
    out[:, 3] = out[:, 2] + unit[:, 3] * (.5 - out[:, 2])
    out[:, 4] = out[:, 3] + unit[:, 4] * (.5 - out[:, 3])
    out[:, 6] = -1.2 + 1.2 * unit[:, 6]
    out[:, 5] = out[:, 6] + unit[:, 5] * (0 - out[:, 6])
    return out


def latest_prerace_rows():
    path = Path("woData/wostrategy/weekend_model/schema_v1/year=2026/round=11/history/parameter_history.csv")
    frame = pd.read_csv(path)
    frame = frame[(frame.as_of_leader_lap == 0) & (frame.source_scope.str.upper() == "AGGREGATE")]
    latest = frame.sort_values("created_at").iloc[-1].analysis_id
    return frame[frame.analysis_id == latest].copy(), latest


def aggregate_prior(frame, parameter, compound=None):
    selected = frame[frame.parameter == parameter]
    if compound is not None:
        selected = selected[selected.compound == compound]
    lap_weights = selected.usable_lap_count.to_numpy(float)
    values = {
        key: float(np.average(selected[key], weights=lap_weights))
        for key in ("p10", "median", "p90")
    }
    values["sigma"] = (values["p90"] - values["p10"]) / (2 * norm.ppf(.9))
    return values


def prior_definition(frame):
    return {
        "fuel": aggregate_prior(frame, "fuel_rate"),
        **{f"deg_{c}": aggregate_prior(frame, "degradation", c) for c in ("HARD", "MEDIUM", "SOFT")},
        **{f"delta_{c}": aggregate_prior(frame, "compound_delta", c) for c in ("HARD", "SOFT")},
    }


def informative_samples(seed, prior):
    rng = np.random.default_rng(seed)
    accepted = []
    while sum(len(value) for value in accepted) < COUNT:
        size = COUNT
        fuel = rng.normal(prior["fuel"]["median"], prior["fuel"]["sigma"], size)
        deg_h = rng.normal(prior["deg_HARD"]["median"], prior["deg_HARD"]["sigma"], size)
        deg_m = rng.normal(prior["deg_MEDIUM"]["median"], prior["deg_MEDIUM"]["sigma"], size)
        deg_s = rng.normal(prior["deg_SOFT"]["median"], prior["deg_SOFT"]["sigma"], size)
        delta_h_vs_m = rng.normal(prior["delta_HARD"]["median"], prior["delta_HARD"]["sigma"], size)
        delta_s_vs_m = rng.normal(prior["delta_SOFT"]["median"], prior["delta_SOFT"]["sigma"], size)
        track = rng.uniform(-.05, .05, size)
        delta_m_vs_h = -delta_h_vs_m
        delta_s_vs_h = delta_s_vs_m - delta_h_vs_m
        values = np.column_stack((fuel, track, deg_h, deg_m, deg_s,
                                  delta_m_vs_h, delta_s_vs_h))
        keep = (
            (fuel >= 0) & (fuel <= .1)
            & (deg_h >= 0) & (deg_h <= deg_m) & (deg_m <= deg_s) & (deg_s <= .5)
            & (delta_s_vs_h >= -1.2) & (delta_s_vs_h <= delta_m_vs_h)
            & (delta_m_vs_h >= -1.2) & (delta_m_vs_h <= 0)
        )
        accepted.append(values[keep])
    return np.vstack(accepted)[:COUNT]


def fixed_samples(seed, prior):
    track = qmc.LatinHypercube(1, seed=seed).random(COUNT)[:, 0] * .1 - .05
    constants = np.asarray((
        prior["fuel"]["median"], 0,
        prior["deg_HARD"]["median"], prior["deg_MEDIUM"]["median"], prior["deg_SOFT"]["median"],
        -prior["delta_HARD"]["median"],
        prior["delta_SOFT"]["median"] - prior["delta_HARD"]["median"],
    ))
    out = np.tile(constants, (COUNT, 1)); out[:, 1] = track
    return out


def design(prepared):
    compounds = prepared.compound.to_numpy(str)
    columns = [
        prepared.run_lap_index.to_numpy(float), prepared.track_index.to_numpy(float),
        prepared.tyre_age.to_numpy(float) * (compounds == "HARD"),
        prepared.tyre_age.to_numpy(float) * (compounds == "MEDIUM"),
        prepared.tyre_age.to_numpy(float) * (compounds == "SOFT"),
        (compounds == "MEDIUM").astype(float), (compounds == "SOFT").astype(float),
    ]
    matrix = np.column_stack(columns)
    y = prepared.lap_time_s.to_numpy(float)
    codes, _ = pd.factorize(prepared.run_id, sort=True)
    counts = np.bincount(codes)
    y -= (np.bincount(codes, weights=y) / counts)[codes]
    for index in range(matrix.shape[1]):
        matrix[:, index] -= (np.bincount(codes, weights=matrix[:, index]) / counts)[codes]
    return matrix, y


def summarize(leader_lap, architecture, seed, samples, matrix, y):
    residual = y[:, None] - matrix @ samples.T
    w = weights(np.sum(residual**2, axis=0))
    rows = []
    for index, name in enumerate(NAMES):
        rows.append({
            "leader_lap": leader_lap, "architecture": architecture, "seed": seed,
            "parameter": name,
            "p10": weighted_quantile(samples[:, index], w, .1),
            "median": weighted_quantile(samples[:, index], w, .5),
            "p90": weighted_quantile(samples[:, index], w, .9),
            "p10_p90_width": weighted_quantile(samples[:, index], w, .9) - weighted_quantile(samples[:, index], w, .1),
            "ess": float(1 / np.sum(w**2)), "max_weight": float(w.max()),
        })
    active = ("fuel_rate", "track_rate:R", "degradation:R:MEDIUM")
    indices = [NAMES.index(name) for name in active]
    mean = np.average(samples[:, indices], axis=0, weights=w)
    centred = samples[:, indices] - mean
    covariance = (centred * w[:, None]).T @ centred
    scale = np.sqrt(np.diag(covariance))
    with np.errstate(divide="ignore", invalid="ignore"):
        correlation = covariance / np.outer(scale, scale)
    correlation[np.outer(scale, scale) < 1e-24] = np.nan
    corr_rows = [
        {"leader_lap": leader_lap, "architecture": architecture, "seed": seed,
         "parameter_a": active[i],
         "parameter_b": active[j], "weighted_correlation": correlation[i, j]}
        for i in range(3) for j in range(i + 1, 3)
    ]
    return rows, corr_rows


def main():
    output = Path("temp/r11_mc_instability_diagnostic/race_architecture_review")
    output.mkdir(parents=True, exist_ok=True)
    stored_frame, analysis_id = latest_prerace_rows()
    prior = prior_definition(stored_frame)
    replay = Path("woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl")
    service = base.OfflineAnalysisService(Path("woData"))
    snapshot, _, _ = base.replay_snapshot(replay)
    frames, _, _ = base.build_race_laps(snapshot)
    summaries, correlations = [], []
    lap_evidence = {}
    for leader_lap in (9, 37):
        prepared = base.prepare("R", frames[leader_lap])
        matrix, y = design(prepared)
        lap_evidence[str(leader_lap)] = {
            "clean_laps": len(prepared), "runs": int(prepared.run_id.nunique()),
            "compound_laps": prepared.compound.value_counts().to_dict(),
            "within_run_design_rank": int(np.linalg.matrix_rank(matrix)),
        }
        for seed in SEEDS:
            for name, samples in (
                ("A_race_only_broad", broad_samples(seed)),
                ("B_fp_quantities_fixed", fixed_samples(seed, prior)),
                ("C_approx_informative_prior", informative_samples(seed, prior)),
            ):
                rows, corr = summarize(leader_lap, name, seed, samples, matrix, y)
                summaries.extend(rows); correlations.extend(corr)
    pd.DataFrame(summaries).to_csv(output / "lap9_architecture_posteriors.csv", index=False)
    pd.DataFrame(correlations).to_csv(output / "lap9_architecture_correlations.csv", index=False)
    evidence = {
        "pre_race_analysis_id": analysis_id, "sample_count_per_seed": COUNT,
        "seeds": SEEDS, "lap_evidence": lap_evidence,
        "prior_approximation": prior,
    }
    (output / "manifest.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
