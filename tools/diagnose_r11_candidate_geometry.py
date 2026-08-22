#!/usr/bin/env python3
"""Inspect dominant 2026 R11 fixed-bank candidates in normalized parameter space.

This is a diagnostic-only companion to ``diagnose_r11_mc_instability.py``. It
reuses that script's validated replay, bank, clean-lap, and likelihood helpers.
Nothing produced here is fed back into the production model.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial import cKDTree
from scipy.spatial.distance import pdist, squareform

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import diagnose_r11_mc_instability as base
from wostrategy.model.pre_race_performance import _apply_ordered_joint_samples


REPRESENTATIVE_LAPS = (9, 37, 38, 48, 50, 52, 55, 56, 65, 66, 70)
MAIN_CANDIDATES = (71_767, 71_539, 62_291)
TARGET_IMPORTANT_COUNT = 25
MAX_IMPORTANT_COUNT = 30
LOCAL_SCALES = (0.0001, 0.00025, 0.0005, 0.001, 0.002, 0.005, 0.01)
LOCAL_SAMPLE_COUNT = 2_000
LOCAL_SEED = 2_026_110_717
INTERPOLATION_LAPS = (48, 50, 52, 55, 56, 65, 66, 70)
INTERPOLATION_POINT_COUNT = 101


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("woData"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("temp/r11_mc_instability_diagnostic")
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=Path(
            "woData/wodata/live_archive/schema_v1/year=2026/round=11/"
            "session=R/replay/merged_replay_v1.jsonl"
        ),
    )
    return parser.parse_args()


def category(name: str) -> str:
    if name == "fuel_rate":
        return "fuel_correction"
    if name.startswith("track_rate:"):
        return "track_evolution"
    if name.startswith("degradation:"):
        return "tyre_degradation"
    if name.startswith("compound_delta:"):
        return "compound_delta"
    raise ValueError(f"Unknown sampled parameter: {name}")


def sampling_role(name: str) -> str:
    if name == "fuel_rate" or name.startswith("track_rate:"):
        return "direct_lhs_dimension"
    if name.startswith("degradation:"):
        compound = name.rsplit(":", 1)[-1]
        return (
            "direct_lhs_dimension"
            if compound == "HARD"
            else "lhs_dimension_coupled_by_degradation_order"
        )
    if name.startswith("compound_delta:"):
        compound = name.rsplit(":", 1)[-1]
        return (
            "lhs_dimension_clipped_by_reference_order"
            if compound == "SOFT"
            else "lhs_dimension_coupled_by_compound_order"
        )
    raise ValueError(name)


def parameter_rationale(name: str) -> str:
    family = category(name)
    if family == "fuel_correction":
        return "Configurable race-review default; numeric sampling proposal and hard support."
    if family == "track_evolution":
        return "Configurable race-review default per session; numeric sampling proposal and hard support."
    if family == "tyre_degradation":
        return "Configurable broad default with optional compound overrides; ordered physical support."
    return "Configurable broad default with optional compound overrides; HARD-relative ordered support."


def build_inputs(args: argparse.Namespace):
    service = base.OfflineAnalysisService(args.data_root)
    config, stored = base.model_config(service)
    snapshot, replay_count, invalid = base.replay_snapshot(args.replay)
    race_frames, _, snapshot_rows = base.build_race_laps(snapshot)
    fp = base.load_fp(args.data_root, stored)
    bank = base.generate_joint_sample_bank(
        base.SESSIONS,
        {session: base.COMPOUNDS for session in base.SESSIONS},
        config,
    )
    fp_likelihoods = {
        session: base.QuadraticSessionLikelihood(fp[session], session, bank.dimension_names)
        for session in base.SESSIONS[:-1]
    }
    fixed_cost = sum(
        likelihood.sse(bank.candidates) / float(config.clean_lap_noise_sigma**2)
        for likelihood in fp_likelihoods.values()
    )
    return config, stored, bank, race_frames, snapshot_rows, fp, fixed_cost, replay_count, invalid


def weights_for_lap(bank, config, fixed_cost, race_frame: pd.DataFrame):
    prepared = base.prepare("R", race_frame)
    likelihood = base.QuadraticSessionLikelihood(prepared, "R", bank.dimension_names)
    cost = fixed_cost + likelihood.sse(bank.candidates) / float(
        config.clean_lap_noise_sigma**2
    )
    return base.posterior_weights(cost), cost, likelihood


def representative_rankings(bank, config, fixed_cost, race_frames):
    rankings = {}
    weights_by_lap = {}
    costs_by_lap = {}
    for leader_lap in REPRESENTATIVE_LAPS:
        weights, costs, _ = weights_for_lap(
            bank, config, fixed_cost, race_frames[leader_lap]
        )
        weights_by_lap[leader_lap] = weights
        costs_by_lap[leader_lap] = costs
        rankings[leader_lap] = np.argsort(weights)[::-1]
    return rankings, weights_by_lap, costs_by_lap


def select_important_candidates(rankings, weights_by_lap, map_ids: set[int]):
    rank_limit = 10
    selected = set(map_ids)
    while len(selected) < TARGET_IMPORTANT_COUNT:
        for ranking in rankings.values():
            selected.update(int(value) for value in ranking[:rank_limit])
        if len(selected) >= TARGET_IMPORTANT_COUNT or rank_limit >= 100:
            break
        rank_limit += 5
    candidate_metrics = {}
    for candidate_id in selected:
        ranks = []
        weights = []
        for leader_lap, ranking in rankings.items():
            location = np.flatnonzero(ranking == candidate_id)
            ranks.append(int(location[0] + 1) if len(location) else len(ranking) + 1)
            weights.append(float(weights_by_lap[leader_lap][candidate_id]))
        candidate_metrics[candidate_id] = {
            "representative_best_rank": min(ranks),
            "representative_top10_count": sum(rank <= 10 for rank in ranks),
            "representative_max_weight": max(weights),
            "representative_weight_sum": sum(weights),
        }
    if len(selected) > MAX_IMPORTANT_COUNT:
        mandatory = set(map_ids)
        optional = sorted(
            selected - mandatory,
            key=lambda value: (
                candidate_metrics[value]["representative_top10_count"],
                candidate_metrics[value]["representative_max_weight"],
                candidate_metrics[value]["representative_weight_sum"],
            ),
            reverse=True,
        )
        selected = mandatory | set(optional[: MAX_IMPORTANT_COUNT - len(mandatory)])
    ordered = sorted(
        selected,
        key=lambda value: (
            value not in map_ids,
            -candidate_metrics[value]["representative_top10_count"],
            -candidate_metrics[value]["representative_max_weight"],
            value,
        ),
    )
    return ordered, candidate_metrics, rank_limit


def normalized_candidates(bank) -> np.ndarray:
    lower = np.asarray([bounds[0] for bounds in bank.bounds], dtype=float)
    upper = np.asarray([bounds[1] for bounds in bank.bounds], dtype=float)
    return (bank.candidates - lower) / (upper - lower)


def parameter_table(bank, important_ids, metrics, map_counts) -> pd.DataFrame:
    rows = []
    for candidate_id in important_ids:
        for index, (name, bounds) in enumerate(zip(bank.dimension_names, bank.bounds)):
            low, high = bounds
            value = float(bank.candidates[candidate_id, index])
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "is_replay_map_candidate": candidate_id in map_counts,
                    "map_snapshot_count": int(map_counts.get(candidate_id, 0)),
                    **metrics[candidate_id],
                    "parameter": name,
                    "category": category(name),
                    "sampling_role": sampling_role(name),
                    "derived_parameter": False,
                    "value": value,
                    "original_lower_bound": float(low),
                    "original_upper_bound": float(high),
                    "original_range_width": float(high - low),
                    "normalized_position": float((value - low) / (high - low)),
                    "distribution": "Latin-hypercube unit coordinate; transformed uniform support",
                    "range_rationale": parameter_rationale(name),
                }
            )
    return pd.DataFrame(rows)


def distance_outputs(bank, important_ids, normalized):
    categories = {
        "full_space": np.arange(len(bank.dimension_names)),
        "tyre_degradation": np.asarray(
            [i for i, name in enumerate(bank.dimension_names) if category(name) == "tyre_degradation"]
        ),
        "fuel_correction": np.asarray(
            [i for i, name in enumerate(bank.dimension_names) if category(name) == "fuel_correction"]
        ),
        "track_evolution": np.asarray(
            [i for i, name in enumerate(bank.dimension_names) if category(name) == "track_evolution"]
        ),
        "fuel_and_track": np.asarray(
            [
                i
                for i, name in enumerate(bank.dimension_names)
                if category(name) in {"fuel_correction", "track_evolution"}
            ]
        ),
        "compound_delta": np.asarray(
            [i for i, name in enumerate(bank.dimension_names) if category(name) == "compound_delta"]
        ),
    }
    rows = []
    difference_rows = []
    for left_id, right_id in combinations(important_ids, 2):
        delta = np.abs(normalized[left_id] - normalized[right_id])
        for group, indices in categories.items():
            selected = delta[indices]
            rows.append(
                {
                    "candidate_a": left_id,
                    "candidate_b": right_id,
                    "category": group,
                    "dimension_count": len(indices),
                    "normalized_rms_distance": float(np.sqrt(np.mean(selected**2))),
                    "maximum_normalized_coordinate_difference": float(selected.max()),
                    "mean_absolute_normalized_difference": float(selected.mean()),
                }
            )
        for index, name in enumerate(bank.dimension_names):
            low, high = bank.bounds[index]
            difference_rows.append(
                {
                    "candidate_a": left_id,
                    "candidate_b": right_id,
                    "parameter": name,
                    "category": category(name),
                    "candidate_a_value": float(bank.candidates[left_id, index]),
                    "candidate_b_value": float(bank.candidates[right_id, index]),
                    "absolute_difference": float(abs(bank.candidates[left_id, index] - bank.candidates[right_id, index])),
                    "original_range_width": float(high - low),
                    "normalized_difference": float(delta[index]),
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(difference_rows)


def occupancy_table(bank, important_ids) -> pd.DataFrame:
    values = bank.candidates[important_ids]
    rows = []
    for index, (name, bounds) in enumerate(zip(bank.dimension_names, bank.bounds)):
        low, high = bounds
        column = values[:, index]
        rows.append(
            {
                "parameter": name,
                "category": category(name),
                "sampling_role": sampling_role(name),
                "original_lower_bound": float(low),
                "original_upper_bound": float(high),
                "original_range_width": float(high - low),
                "important_candidate_minimum": float(column.min()),
                "important_candidate_maximum": float(column.max()),
                "important_candidate_mean": float(column.mean()),
                "important_candidate_std": float(column.std(ddof=1)),
                "important_candidate_range": float(column.max() - column.min()),
                "range_occupancy_fraction": float((column.max() - column.min()) / (high - low)),
                "all_important_candidates_below_10pct_occupancy": bool(
                    (column.max() - column.min()) / (high - low) < 0.10
                ),
                "range_rationale": parameter_rationale(name),
            }
        )
    return pd.DataFrame(rows).sort_values("range_occupancy_fraction")


def nearest_neighbour_outputs(bank, important_ids, normalized) -> pd.DataFrame:
    dimension_scale = np.sqrt(normalized.shape[1])
    tree = cKDTree(normalized)
    distances, indices = tree.query(normalized, k=2, workers=-1)
    all_nearest = distances[:, 1] / dimension_scale
    important = normalized[important_ids]
    important_distances = squareform(pdist(important)) / dimension_scale
    np.fill_diagonal(important_distances, np.inf)
    important_nearest = important_distances.min(axis=1)
    rows = []
    for population, values in (
        ("all_valid_bank_samples", all_nearest),
        ("important_high_posterior_samples", important_nearest),
    ):
        rows.append(
            {
                "diagnostic_type": "nearest_neighbour_distribution",
                "population": population,
                "count": len(values),
                "minimum": float(np.min(values)),
                "p10": float(np.quantile(values, 0.10)),
                "median": float(np.median(values)),
                "p90": float(np.quantile(values, 0.90)),
                "maximum": float(np.max(values)),
            }
        )
    for candidate_id in MAIN_CANDIDATES:
        rows.append(
            {
                "diagnostic_type": "candidate_nearest_neighbour",
                "candidate_id": candidate_id,
                "nearest_candidate_id": int(indices[candidate_id, 1]),
                "nearest_normalized_rms_distance": float(all_nearest[candidate_id]),
                "nearest_is_important": bool(int(indices[candidate_id, 1]) in important_ids),
            }
        )
    thresholds = (0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30)
    for threshold in thresholds:
        radius = threshold * dimension_scale
        combined = set()
        for candidate_id in important_ids:
            combined.update(tree.query_ball_point(normalized[candidate_id], radius))
        rows.append(
            {
                "diagnostic_type": "dominant_region_density",
                "population": "union_around_important_candidates",
                "distance_threshold": threshold,
                "bank_sample_count_including_centres": len(combined),
                "bank_sample_count_excluding_centres": len(combined - set(important_ids)),
            }
        )
        for candidate_id in MAIN_CANDIDATES:
            neighbours = set(tree.query_ball_point(normalized[candidate_id], radius))
            rows.append(
                {
                    "diagnostic_type": "dominant_region_density",
                    "population": f"around_candidate_{candidate_id}",
                    "candidate_id": candidate_id,
                    "distance_threshold": threshold,
                    "bank_sample_count_including_centres": len(neighbours),
                    "bank_sample_count_excluding_centres": len(neighbours - {candidate_id}),
                }
            )
    return pd.DataFrame(rows)


def latent_coordinates(candidate: np.ndarray, bank) -> np.ndarray:
    dimensions = bank.dimension_names
    bounds = bank.bounds
    index_by_name = {name: index for index, name in enumerate(dimensions)}
    unit = np.empty(len(dimensions), dtype=float)
    for index, name in enumerate(dimensions):
        if name.startswith("degradation:") or name.startswith("compound_delta:"):
            continue
        low, high = bounds[index]
        unit[index] = (candidate[index] - low) / (high - low)
    for session in base.SESSIONS:
        previous = None
        for compound in ("HARD", "MEDIUM", "SOFT"):
            index = index_by_name[f"degradation:{session}:{compound}"]
            low, high = bounds[index]
            effective_low = low if previous is None else max(low, previous)
            unit[index] = (candidate[index] - effective_low) / (high - effective_low)
            previous = candidate[index]
        soft_index = index_by_name[f"compound_delta:{session}:SOFT"]
        medium_index = index_by_name[f"compound_delta:{session}:MEDIUM"]
        low, high = bounds[soft_index]
        soft_high = min(high, 0.0)
        unit[soft_index] = (candidate[soft_index] - low) / (soft_high - low)
        low, high = bounds[medium_index]
        effective_low = max(low, candidate[soft_index])
        medium_high = min(high, 0.0)
        unit[medium_index] = (
            candidate[medium_index] - effective_low
        ) / (medium_high - effective_low)
    return np.clip(unit, 0.0, 1.0)


def candidates_from_latent(unit: np.ndarray, bank) -> np.ndarray:
    candidates = np.empty_like(unit)
    for index, (low, high) in enumerate(bank.bounds):
        candidates[:, index] = low + unit[:, index] * (high - low)
    _apply_ordered_joint_samples(
        unit,
        candidates,
        bank.dimension_names,
        bank.bounds,
        base.SESSIONS,
        "HARD",
    )
    return candidates


def local_probe(bank, config, fixed_cost, race_frames, fp) -> pd.DataFrame:
    rng = np.random.default_rng(LOCAL_SEED)
    rows = []
    centre_laps = {71_767: 37, 71_539: 55, 62_291: 70}
    width = np.asarray([value[1] - value[0] for value in bank.bounds])
    fp_likelihoods = {
        session: base.QuadraticSessionLikelihood(fp[session], session, bank.dimension_names)
        for session in base.SESSIONS[:-1]
    }
    for candidate_id, leader_lap in centre_laps.items():
        prepared = base.prepare("R", race_frames[leader_lap])
        race_likelihood = base.QuadraticSessionLikelihood(
            prepared, "R", bank.dimension_names
        )
        centre = bank.candidates[candidate_id]
        centre_cost = float(
            fixed_cost[candidate_id]
            + race_likelihood.sse(centre[None, :])[0] / config.clean_lap_noise_sigma**2
        )
        latent_centre = latent_coordinates(centre, bank)
        for scale in LOCAL_SCALES:
            latent = np.clip(
                latent_centre + rng.normal(0.0, scale, size=(LOCAL_SAMPLE_COUNT, len(centre))),
                0.0,
                1.0,
            )
            candidates = candidates_from_latent(latent, bank)
            # Re-evaluate the unchanged FP likelihood for diagnostic points.
            fp_cost = np.zeros(len(candidates))
            for fp_likelihood in fp_likelihoods.values():
                fp_cost += fp_likelihood.sse(candidates) / config.clean_lap_noise_sigma**2
            cost = fp_cost + race_likelihood.sse(candidates) / config.clean_lap_noise_sigma**2
            delta_nll = 0.5 * (cost - centre_cost)
            rms_distance = np.sqrt(
                np.mean(((candidates - centre) / width) ** 2, axis=1)
            )
            rows.append(
                {
                    "centre_candidate_id": candidate_id,
                    "leader_lap": leader_lap,
                    "latent_perturbation_sigma": scale,
                    "diagnostic_sample_count": LOCAL_SAMPLE_COUNT,
                    "actual_normalized_rms_distance_median": float(np.median(rms_distance)),
                    "actual_normalized_rms_distance_p90": float(np.quantile(rms_distance, 0.9)),
                    "delta_nll_minimum": float(delta_nll.min()),
                    "delta_nll_p10": float(np.quantile(delta_nll, 0.1)),
                    "delta_nll_median": float(np.median(delta_nll)),
                    "delta_nll_p90": float(np.quantile(delta_nll, 0.9)),
                    "fraction_with_delta_nll_le_1": float(np.mean(delta_nll <= 1.0)),
                    "fraction_with_delta_nll_le_2": float(np.mean(delta_nll <= 2.0)),
                    "fraction_with_delta_nll_le_5": float(np.mean(delta_nll <= 5.0)),
                    "fraction_improving_on_centre": float(np.mean(delta_nll < 0.0)),
                }
            )
    return pd.DataFrame(rows)


def interpolation_probe(bank, config, race_frames, fp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate straight paths between headline candidates in latent LHS space."""
    fp_likelihoods = {
        session: base.QuadraticSessionLikelihood(fp[session], session, bank.dimension_names)
        for session in base.SESSIONS[:-1]
    }
    latent = {
        candidate_id: latent_coordinates(bank.candidates[candidate_id], bank)
        for candidate_id in MAIN_CANDIDATES
    }
    detail_rows = []
    summary_rows = []
    t_values = np.linspace(0.0, 1.0, INTERPOLATION_POINT_COUNT)
    for left_id, right_id in combinations(MAIN_CANDIDATES, 2):
        path_latent = (
            (1.0 - t_values[:, None]) * latent[left_id][None, :]
            + t_values[:, None] * latent[right_id][None, :]
        )
        candidates = candidates_from_latent(path_latent, bank)
        fp_cost = np.zeros(len(candidates))
        for likelihood in fp_likelihoods.values():
            fp_cost += likelihood.sse(candidates) / config.clean_lap_noise_sigma**2
        for leader_lap in INTERPOLATION_LAPS:
            race = base.QuadraticSessionLikelihood(
                base.prepare("R", race_frames[leader_lap]), "R", bank.dimension_names
            )
            cost = fp_cost + race.sse(candidates) / config.clean_lap_noise_sigma**2
            nll = 0.5 * cost
            endpoint_max = max(nll[0], nll[-1])
            interior = nll[1:-1]
            for t_value, nll_value in zip(t_values, nll):
                detail_rows.append(
                    {
                        "candidate_a": left_id,
                        "candidate_b": right_id,
                        "leader_lap": leader_lap,
                        "path_fraction_from_a_to_b": float(t_value),
                        "negative_log_likelihood": float(nll_value),
                        "delta_nll_from_path_minimum": float(nll_value - nll.min()),
                    }
                )
            summary_rows.append(
                {
                    "candidate_a": left_id,
                    "candidate_b": right_id,
                    "leader_lap": leader_lap,
                    "candidate_a_nll": float(nll[0]),
                    "candidate_b_nll": float(nll[-1]),
                    "endpoint_nll_difference_b_minus_a": float(nll[-1] - nll[0]),
                    "path_minimum_nll": float(nll.min()),
                    "path_minimum_fraction": float(t_values[np.argmin(nll)]),
                    "path_minimum_improvement_over_better_endpoint": float(
                        min(nll[0], nll[-1]) - nll.min()
                    ),
                    "midpoint_delta_nll_from_better_endpoint": float(
                        nll[len(nll) // 2] - min(nll[0], nll[-1])
                    ),
                    "interior_barrier_above_worse_endpoint": float(
                        max(0.0, interior.max() - endpoint_max)
                    ),
                }
            )
    return pd.DataFrame(detail_rows), pd.DataFrame(summary_rows)


def geometry_plots(bank, important_ids, normalized, output_dir: Path):
    important = normalized[important_ids]
    distance_matrix = squareform(pdist(important)) / np.sqrt(important.shape[1])
    order = leaves_list(linkage(important, method="average", metric="euclidean"))
    ordered_matrix = distance_matrix[np.ix_(order, order)]
    ordered_ids = np.asarray(important_ids)[order]
    figure, axis = plt.subplots(figsize=(11, 9))
    image = axis.imshow(ordered_matrix, cmap="viridis", interpolation="nearest")
    axis.set_xticks(range(len(ordered_ids)), ordered_ids, rotation=90, fontsize=7)
    axis.set_yticks(range(len(ordered_ids)), ordered_ids, fontsize=7)
    axis.set_title("Dominant-candidate normalized RMS distance")
    figure.colorbar(image, ax=axis, label="Normalized RMS distance")
    figure.tight_layout()
    figure.savefig(output_dir / "dominant_candidate_distance_heatmap.png", dpi=180)
    plt.close(figure)

    centred = important - important.mean(axis=0)
    u, singular, vt = np.linalg.svd(centred, full_matrices=False)
    scores = u * singular
    variance = singular**2 / max(len(important) - 1, 1)
    explained = variance / variance.sum()
    figure, axis = plt.subplots(figsize=(10, 7))
    axis.scatter(scores[:, 0], scores[:, 1], color="#42a5f5", alpha=0.8)
    for row, candidate_id in enumerate(important_ids):
        if candidate_id in MAIN_CANDIDATES:
            axis.annotate(str(candidate_id), scores[row, :2], fontsize=10, fontweight="bold")
        else:
            axis.annotate(str(candidate_id), scores[row, :2], fontsize=6, alpha=0.75)
    axis.set_xlabel(f"PC1 ({explained[0] * 100:.1f}% variance)")
    axis.set_ylabel(f"PC2 ({explained[1] * 100:.1f}% variance)")
    axis.set_title("PCA of range-normalized important candidate vectors")
    axis.grid(alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_dir / "dominant_candidate_pca.png", dpi=180)
    plt.close(figure)
    loading_rows = []
    for component in range(min(5, len(explained))):
        for index, name in enumerate(bank.dimension_names):
            loading_rows.append(
                {
                    "component": component + 1,
                    "explained_variance_fraction": float(explained[component]),
                    "parameter": name,
                    "loading": float(vt[component, index]),
                    "absolute_loading": float(abs(vt[component, index])),
                }
            )
    return distance_matrix, pd.DataFrame(loading_rows)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (
        config,
        stored,
        bank,
        race_frames,
        snapshot_rows,
        fp,
        fixed_cost,
        replay_count,
        invalid,
    ) = build_inputs(args)
    history, _, persistence = base.full_posterior_history(
        bank=bank,
        config=config,
        fp_prepared=fp,
        race_frames=race_frames,
        snapshot_rows=snapshot_rows,
    )
    map_counts = {
        int(row.map_candidate_id): int(row.snapshot_count)
        for row in persistence.itertuples(index=False)
    }
    rankings, weights_by_lap, _ = representative_rankings(
        bank, config, fixed_cost, race_frames
    )
    important_ids, metrics, selection_rank_limit = select_important_candidates(
        rankings, weights_by_lap, set(map_counts)
    )
    if not set(MAIN_CANDIDATES).issubset(important_ids):
        raise RuntimeError("A required dominant candidate was not selected")
    normalized = normalized_candidates(bank)
    parameters = parameter_table(bank, important_ids, metrics, map_counts)
    distances, differences = distance_outputs(bank, important_ids, normalized)
    occupancy = occupancy_table(bank, important_ids)
    main_occupancy = occupancy_table(bank, list(MAIN_CANDIDATES))
    neighbours = nearest_neighbour_outputs(bank, important_ids, normalized)
    local = local_probe(bank, config, fixed_cost, race_frames, fp)
    interpolation_detail, interpolation_summary = interpolation_probe(
        bank, config, race_frames, fp
    )
    distance_matrix, loadings = geometry_plots(
        bank, important_ids, normalized, args.output_dir
    )

    selection_rows = []
    for candidate_id in important_ids:
        row = {
            "candidate_id": candidate_id,
            "is_replay_map_candidate": candidate_id in map_counts,
            "map_snapshot_count": map_counts.get(candidate_id, 0),
            **metrics[candidate_id],
        }
        for leader_lap in REPRESENTATIVE_LAPS:
            ranking = rankings[leader_lap]
            row[f"lap_{leader_lap}_rank"] = int(np.flatnonzero(ranking == candidate_id)[0] + 1)
            row[f"lap_{leader_lap}_weight"] = float(weights_by_lap[leader_lap][candidate_id])
        selection_rows.append(row)
    selection = pd.DataFrame(selection_rows)
    wide = pd.DataFrame(
        bank.candidates[important_ids], columns=bank.dimension_names, index=important_ids
    ).rename_axis("candidate_id").reset_index()
    matrix_frame = pd.DataFrame(
        distance_matrix, index=important_ids, columns=important_ids
    ).rename_axis("candidate_id")
    parameter_bounds = pd.DataFrame(
        [
            {
                "parameter": name,
                "category": category(name),
                "sampling_role": sampling_role(name),
                "lower_bound": low,
                "upper_bound": high,
                "range_width": high - low,
                "distribution": "Latin hypercube in latent unit coordinate",
                "range_source": stored.get("settings_source"),
                "range_source_path": stored.get("settings_path"),
                "rationale_found_in_code": parameter_rationale(name),
            }
            for name, (low, high) in zip(bank.dimension_names, bank.bounds)
        ]
    )
    top_differences = differences.loc[
        differences.apply(
            lambda row: {
                int(row["candidate_a"]), int(row["candidate_b"])
            }.issubset(MAIN_CANDIDATES),
            axis=1,
        )
    ].sort_values(["candidate_a", "candidate_b", "normalized_difference"], ascending=[True, True, False])

    parameters.to_csv(args.output_dir / "dominant_candidate_parameters.csv", index=False)
    wide.to_csv(args.output_dir / "dominant_candidate_vectors_wide.csv", index=False)
    selection.to_csv(args.output_dir / "dominant_candidate_selection.csv", index=False)
    distances.to_csv(args.output_dir / "dominant_candidate_distances.csv", index=False)
    matrix_frame.to_csv(args.output_dir / "dominant_candidate_distance_matrix.csv")
    differences.to_csv(args.output_dir / "dominant_candidate_parameter_differences.csv", index=False)
    top_differences.to_csv(args.output_dir / "main_candidate_parameter_differences.csv", index=False)
    occupancy.to_csv(args.output_dir / "parameter_range_occupancy.csv", index=False)
    main_occupancy.to_csv(args.output_dir / "main_candidate_range_occupancy.csv", index=False)
    parameter_bounds.to_csv(args.output_dir / "parameter_sampling_ranges.csv", index=False)
    neighbours.to_csv(args.output_dir / "nearest_neighbour_diagnostics.csv", index=False)
    local.to_csv(args.output_dir / "local_likelihood_probe.csv", index=False)
    interpolation_detail.to_csv(
        args.output_dir / "candidate_interpolation_probe.csv", index=False
    )
    interpolation_summary.to_csv(
        args.output_dir / "candidate_interpolation_summary.csv", index=False
    )
    loadings.to_csv(args.output_dir / "dominant_candidate_pca_loadings.csv", index=False)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "year": base.YEAR,
        "round": base.ROUND,
        "replay_event_count": replay_count,
        "invalid_replay_records": invalid,
        "bank_id": bank.sample_bank_id,
        "sample_count": len(bank.candidates),
        "independently_sampled_latent_dimension_count": len(bank.dimension_names),
        "physical_sampled_dimension_count": len(bank.dimension_names),
        "derived_reference_dimensions": [
            f"compound_delta:{session}:HARD" for session in base.SESSIONS
        ],
        "derived_nuisance_parameters": "Per-run intercepts are refitted analytically for every candidate.",
        "representative_laps": REPRESENTATIVE_LAPS,
        "main_candidates": MAIN_CANDIDATES,
        "important_candidate_count": len(important_ids),
        "important_candidate_ids": important_ids,
        "selection_rank_limit": selection_rank_limit,
        "base_config": asdict(config),
        "range_settings_source": stored.get("settings_source"),
        "range_settings_path": stored.get("settings_path"),
        "local_probe_seed": LOCAL_SEED,
        "local_probe_samples_per_scale": LOCAL_SAMPLE_COUNT,
        "local_probe_scales": LOCAL_SCALES,
        "interpolation_laps": INTERPOLATION_LAPS,
        "interpolation_point_count": INTERPOLATION_POINT_COUNT,
    }
    (args.output_dir / "candidate_geometry_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output_dir": str(args.output_dir), "manifest": manifest}, indent=2, default=str))


if __name__ == "__main__":
    main()
