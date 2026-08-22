#!/usr/bin/env python3
"""Diagnostics for the opt-in Race cross-stint compound-performance prototype."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

import diagnose_r11_mc_instability as replay
from woplanner.analysis_service import OfflineAnalysisService
from wostrategy.model.pre_race_performance import WeekendModelConfig, _prepare_analysis_laps
from wostrategy.model.race_specific_performance import (
    COMPOUNDS,
    compound_connectivity,
    run_race_specific_model,
)

OUTPUT = Path("temp/r11_compound_performance_review")
SNAPSHOTS = (9, 22, 25, 37, 55, 56, 70)
SEEDS = (11, 2026, 424242)
PERFORMANCE_MAP = {
    "HARD": (2 / 3, -1 / 3),
    "MEDIUM": (-1 / 3, -1 / 3),
    "SOFT": (-1 / 3, 2 / 3),
}


def design(prepared: pd.DataFrame, group: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    codes, labels = pd.factorize(prepared[group], sort=True)
    nuisance = np.eye(len(labels))[codes]
    compound = prepared.compound.astype(str).to_numpy()
    columns = [prepared.track_index.to_numpy(float)]
    names = ["net_lap_slope"]
    for item in COMPOUNDS:
        columns.append((compound == item) * prepared.tyre_age.to_numpy(float))
        names.append(f"degradation:{item}")
    columns.append(np.array([PERFORMANCE_MAP[item][0] for item in compound]))
    columns.append(np.array([PERFORMANCE_MAP[item][1] for item in compound]))
    names += ["performance:HARD_MINUS_MEDIUM", "performance:SOFT_MINUS_MEDIUM"]
    return np.column_stack((nuisance, *columns)), prepared.lap_time_s.to_numpy(float), names


def fit_architecture(prepared: pd.DataFrame, architecture: str) -> tuple[np.ndarray, np.ndarray, int]:
    group = "run_id" if architecture == "per_run" else "driver"
    matrix, y, _ = design(prepared, group)
    if architecture == "per_run":
        # Performance is deliberately absent in A; run intercepts span it exactly.
        matrix = matrix[:, :-2]
    beta = np.linalg.lstsq(matrix, y, rcond=None)[0]
    residual = y - matrix @ beta
    return beta, residual, int(np.linalg.matrix_rank(matrix))


def fit_regularized_stint(prepared: pd.DataFrame, sigma: float, tau: float = .20):
    base, y, _ = design(prepared, "driver")
    run_codes, runs = pd.factorize(prepared.run_id, sort=True)
    driver_codes, drivers = pd.factorize(prepared.driver, sort=True)
    stint = np.eye(len(runs))[run_codes]
    # Centre stint indicators within driver so they cannot replace the driver baseline.
    for code in range(len(drivers)):
        mask = driver_codes == code
        stint[mask] -= stint[mask].mean(axis=0, keepdims=True)
    matrix = np.column_stack((base, stint))
    penalty = np.zeros((len(runs), matrix.shape[1]))
    penalty[:, -len(runs):] = np.eye(len(runs)) * (sigma / tau)
    beta = np.linalg.lstsq(np.vstack((matrix, penalty)), np.r_[y, np.zeros(len(runs))], rcond=None)[0]
    residual = y - matrix @ beta
    return beta, residual, int(np.linalg.matrix_rank(matrix))


def centred_global_design(prepared: pd.DataFrame) -> tuple[np.ndarray, int]:
    codes, labels = pd.factorize(prepared.driver, sort=True)
    raw = np.eye(len(labels))[codes]
    global_driver = np.column_stack((np.ones(len(prepared)), raw[:, :-1] - raw[:, [-1]]))
    full, _, _ = design(prepared, "driver")
    return np.column_stack((global_driver, full[:, len(labels):])), len(labels)


def posterior_rows(result, lap: int, seed: int, architecture: str) -> list[dict]:
    quality = result.quality
    rows = []
    for item in result.aggregate_snapshot.parameters:
        if item.parameter not in {"net_lap_slope", "degradation", "compound_delta"}:
            continue
        rows.append({
            "leader_lap": lap, "seed": seed, "architecture": architecture,
            "parameter": item.parameter, "compound": item.compound,
            "p10": item.p10, "median": item.median, "p90": item.p90,
            "p10_p90_width": item.p90 - item.p10,
            "support_status": item.support_status,
            "ess": quality["ess"], "max_weight": quality["max_weight"],
            "total_model_seconds": quality.get("diagnostic_total_seconds"),
        })
    return rows


def profile_rows(prepared: pd.DataFrame, lap: int, sigma: float) -> list[dict]:
    matrix, y, names = design(prepared, "driver")
    driver_count = prepared.driver.nunique()
    rows = []
    for coordinate in ("performance:HARD_MINUS_MEDIUM", "performance:SOFT_MINUS_MEDIUM"):
        index = driver_count + names.index(coordinate)
        free = np.delete(matrix, index, axis=1)
        coordinate_identifiable = np.linalg.matrix_rank(free) < np.linalg.matrix_rank(matrix)
        if not coordinate_identifiable:
            rows.extend({"leader_lap": lap, "coordinate": coordinate, "value": value,
                         "delta_nll": 0.0, "structurally_identifiable": False}
                        for value in np.linspace(-2, 2, 101))
            continue
        optimum = np.linalg.lstsq(matrix, y, rcond=None)[0]
        centre = optimum[index]
        grid = np.linspace(centre - 1.25, centre + 1.25, 101)
        best = np.inf
        values = []
        for value in grid:
            residual_y = y - matrix[:, index] * value
            beta = np.linalg.lstsq(free, residual_y, rcond=None)[0]
            sse = float(np.sum((residual_y - free @ beta) ** 2))
            best = min(best, sse)
            values.append((value, sse))
        rows.extend({"leader_lap": lap, "coordinate": coordinate, "value": value,
                     "delta_nll": (sse - best) / (2 * sigma**2),
                     "structurally_identifiable": True} for value, sse in values)
    return rows


def gaussian_posterior(frame: pd.DataFrame, prior_mean: np.ndarray, prior_sd: np.ndarray,
                       sigma: float) -> tuple[np.ndarray, np.ndarray, int]:
    matrix, y, names = design(frame, "driver")
    driver_count = frame.driver.nunique()
    x = matrix[:, driver_count:]
    codes, _ = pd.factorize(frame.driver, sort=True)
    x = x - pd.DataFrame(x).groupby(codes).transform("mean").to_numpy()
    yy = y - pd.Series(y).groupby(codes).transform("mean").to_numpy()
    precision = x.T @ x / sigma**2 + np.diag(1 / prior_sd**2)
    covariance = np.linalg.inv(precision)
    mean = covariance @ (x.T @ yy / sigma**2 + prior_mean / prior_sd**2)
    return mean, covariance, int(np.linalg.matrix_rank(x))


def synthetic_frame(scenario: int, seed: int = 7) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    drivers = tuple("ABCDEF")
    sequences = {
        1: {driver: ("MEDIUM", "HARD") for driver in drivers},
        2: {"A": ("MEDIUM", "HARD"), "B": ("MEDIUM",), "C": ("MEDIUM",),
            "D": ("HARD",), "E": ("HARD",), "F": ("MEDIUM",)},
        3: {"A": ("MEDIUM", "SOFT"), "B": ("MEDIUM", "HARD"),
            "C": ("MEDIUM",), "D": ("HARD",), "E": ("MEDIUM",), "F": ("HARD",)},
        4: {driver: ("MEDIUM", "HARD") for driver in drivers},
        5: {"A": ("MEDIUM", "HARD"), "B": ("MEDIUM",), "C": ("HARD",),
            "D": ("MEDIUM", "HARD"), "E": ("MEDIUM",), "F": ("HARD",)},
        6: {driver: ("MEDIUM", "HARD") for driver in drivers},
    }[scenario]
    true = np.array([-.025, .045, .085, .14, .45, -.55])
    rows = []
    for d_index, driver in enumerate(drivers):
        lap = 0
        driver_offset = (-.75 + .3 * d_index) if scenario == 5 else .2 * d_index
        for stint, compound in enumerate(sequences[driver], 1):
            stint_offset = rng.normal(0, .12) if scenario == 6 else 0.0
            # Differing pit laps are important: if every driver changes compound
            # on exactly the same lap, a compound step is needlessly confounded
            # with the common secular trend in this intentionally small design.
            stint_length = 6 + ((d_index + stint) % 5)
            for age in range(1, stint_length + 1):
                lap += 1
                performance = PERFORMANCE_MAP[compound][0] * true[4] + PERFORMANCE_MAP[compound][1] * true[5]
                degradation = true[1 + COMPOUNDS.index(compound)]
                time_value = 90 + driver_offset + true[0] * lap + degradation * age + performance
                time_value += stint_offset + rng.normal(0, .08)
                rows.append({"driver": driver, "run_id": f"{driver}:{stint}", "compound": compound,
                             "tyre_age": age, "track_index": lap, "lap_time_s": time_value})
    return pd.DataFrame(rows), true


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    snapshot, _, _ = replay.replay_snapshot(Path(
        "woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl"
    ))
    frames, _, _ = replay.build_race_laps(snapshot)
    service = OfflineAnalysisService(Path("woData"))
    prior = service.frozen_pre_race_prior(2026, 11)
    stored = service.live_model_config(2026, 11, "R")
    sigma = float(stored["clean_lap_noise_sigma"])
    architecture_rows, connectivity_rows, performance_rows, residual_rows, profiles = [], [], [], [], []
    for lap in SNAPSHOTS:
        prepared = _prepare_analysis_laps(frames[lap])
        connectivity = compound_connectivity(prepared)
        connectivity_rows.append({"leader_lap": lap, "clean_laps": len(prepared),
            **{key: json.dumps(value) if isinstance(value, (tuple, dict)) else value
               for key, value in connectivity.items()}})
        for architecture in ("per_run", "per_driver", "global_driver"):
            beta, residual, rank = fit_architecture(prepared, architecture)
            nuisance_count = prepared.run_id.nunique() if architecture == "per_run" else prepared.driver.nunique()
            k = nuisance_count + (4 if architecture == "per_run" else 6)
            nll = float(np.sum(residual**2) / (2 * sigma**2))
            architecture_rows.append({"leader_lap": lap, "architecture": architecture,
                "clean_laps": len(prepared), "nuisance_count": nuisance_count,
                "design_columns": k, "design_rank": rank, "nll": nll,
                "aic": 2 * k + 2 * nll, "residual_rmse": float(np.sqrt(np.mean(residual**2))),
                "residual_p10": float(np.quantile(residual, .1)),
                "residual_p90": float(np.quantile(residual, .9))})
            if architecture == "per_driver":
                detail = prepared[["run_id", "driver", "compound"]].copy()
                detail["residual"] = residual
                for run_id, group in detail.groupby("run_id", sort=True):
                    residual_rows.append({"leader_lap": lap, "run_id": run_id,
                        "driver": group.driver.iloc[0], "compound": group.compound.iloc[0],
                        "lap_count": len(group), "mean_residual": group.residual.mean(),
                        "residual_sd": group.residual.std()})
        beta, residual, rank = fit_regularized_stint(prepared, sigma)
        nll = float(np.sum(residual**2) / (2 * sigma**2))
        architecture_rows.append({"leader_lap": lap,
            "architecture": "per_driver_regularized_stint_tau_0.20",
            "clean_laps": len(prepared), "nuisance_count": prepared.driver.nunique(),
            "design_columns": prepared.driver.nunique() + 6 + prepared.run_id.nunique(),
            "design_rank": rank, "nll": nll, "aic": np.nan,
            "residual_rmse": float(np.sqrt(np.mean(residual**2))),
            "residual_p10": float(np.quantile(residual, .1)),
            "residual_p90": float(np.quantile(residual, .9))})
        if lap in (37, 70):
            profiles.extend(profile_rows(prepared, lap, sigma))
        for seed in SEEDS:
            config = WeekendModelConfig(sample_count=80_000, random_seed=seed,
                fuel_rate_bounds=tuple(stored["fuel_rate_bounds"]),
                track_rate_bounds=tuple(stored["track_rate_bounds"]),
                default_degradation_bounds=tuple(stored["default_degradation_bounds"]),
                default_compound_delta_bounds=tuple(stored["default_compound_delta_bounds"]),
                clean_lap_noise_sigma=sigma, enforce_compound_order=True)
            for architecture in ("per_run", "per_driver"):
                started = time.perf_counter()
                result = run_race_specific_model(frames[lap], prior, config,
                    as_of_leader_lap=lap, baseline_architecture=architecture)
                elapsed = time.perf_counter() - started
                result.quality["diagnostic_total_seconds"] = elapsed
                performance_rows.extend(posterior_rows(result, lap, seed, architecture))

    sensitivity_rows = []
    prior_mean = np.array([0, .04, .08, .15, .45, -.55])
    prior_sd = np.array([.08, .05, .05, .06, .35, .35])
    for lap in (37, 70):
        prepared = _prepare_analysis_laps(frames[lap])
        for scale in (.5, 1.0, 2.0):
            mean, covariance, rank = gaussian_posterior(prepared, prior_mean, prior_sd * scale, sigma)
            sd = np.sqrt(np.diag(covariance))
            for index, name in enumerate(("net_lap_slope", "deg:H", "deg:M", "deg:S", "H-M", "S-M")):
                sensitivity_rows.append({"leader_lap": lap, "prior_sd_scale": scale,
                    "parameter": name, "posterior_mean": mean[index],
                    "p10_p90_width": 2 * abs(norm.ppf(.1)) * sd[index],
                    "global_design_rank": rank})

    synthetic_rows = []
    for scenario in range(1, 7):
        for seed in (7, 19, 41):
            frame, true = synthetic_frame(scenario, seed)
            mean, covariance, rank = gaussian_posterior(frame, prior_mean, prior_sd, .08)
            sd = np.sqrt(np.diag(covariance))
            connectivity = compound_connectivity(frame)
            for index, name in enumerate(("net_lap_slope", "deg:H", "deg:M", "deg:S", "H-M", "S-M")):
                synthetic_rows.append({"scenario": scenario, "seed": seed,
                    "parameter": name, "truth": true[index],
                    "posterior_mean": mean[index], "bias": mean[index] - true[index],
                    "p10": mean[index] + norm.ppf(.1) * sd[index],
                    "p90": mean[index] + norm.ppf(.9) * sd[index],
                    "posterior_sd": sd[index], "global_design_rank": rank,
                    "fully_connected": connectivity["fully_connected"],
                    "connector_drivers": connectivity["connector_drivers"]})

    pd.DataFrame(architecture_rows).to_csv(OUTPUT / "race_baseline_architecture_comparison.csv", index=False)
    pd.DataFrame(connectivity_rows).to_csv(OUTPUT / "compound_connectivity_by_snapshot.csv", index=False)
    pd.DataFrame(profiles).to_csv(OUTPUT / "compound_profile_likelihood.csv", index=False)
    pd.DataFrame(synthetic_rows).to_csv(OUTPUT / "compound_synthetic_recovery.csv", index=False)
    pd.DataFrame(residual_rows).to_csv(OUTPUT / "race_residual_by_stint.csv", index=False)
    pd.DataFrame(performance_rows).to_csv(OUTPUT / "compound_performance_history.csv", index=False)
    pd.DataFrame(sensitivity_rows).to_csv(OUTPUT / "race_prior_sensitivity.csv", index=False)
    print(json.dumps({"output": str(OUTPUT), "architecture_rows": len(architecture_rows),
                      "posterior_rows": len(performance_rows), "synthetic_rows": len(synthetic_rows)}))


if __name__ == "__main__":
    main()
