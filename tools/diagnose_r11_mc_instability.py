#!/usr/bin/env python3
"""Diagnose fixed-bank posterior instability for the saved 2026 R11 replay.

This script is intentionally separate from production code. It reuses the replay
reducer, leader-lap snapshot logic, clean-lap filtering, FP preparation, constrained
sample-bank generator, and production parameter preparation. The only accelerated
piece is evaluation of the existing least-squares likelihood: after removing the
per-run intercept analytically, the same SSE is evaluated as a quadratic form.
The accelerated result is checked against the production SSE function before any
diagnostics are written.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from wodata.live import LiveSessionStore, LiveTimingMessage
from wodata.live.replay import load_replay
from woplanner.analysis.gui import StrategyPredictionWindow
from woplanner.analysis.service import OfflineAnalysisService, _live_analysis_laps
from wostrategy.core.pre_race_session_data import prepare_weekend_sessions
from wostrategy.model.pre_race_performance import (
    WeekendModelConfig,
    _joint_session_sse,
    _prepare_analysis_laps,
    generate_joint_sample_bank,
)


YEAR = 2026
ROUND = 11
LEADER_LAPS = (37, 38, 55, 56)
FULL_LEADER_LAPS = tuple(range(1, 71))
PRODUCTION_SAMPLE_COUNT = 80_000
PRODUCTION_SEED = 1_898_758_267
SAMPLE_COUNTS = (80_000, 160_000, 320_000)
SEEDS = (PRODUCTION_SEED, 1, 2026, 11_037, 424_242)
SESSIONS = ("FP1", "FP2", "FP3", "R")
COMPOUNDS = ("SOFT", "MEDIUM", "HARD")
EXPECTED_BANK_ID = "bank-5d15e8edcaf0fb977c42"
EXPECTED_PRODUCTION_DEGRADATION = {
    37: {"SOFT": 0.29142557231456984, "MEDIUM": 0.14078554728742518, "HARD": 0.08516402977596403},
    38: {"SOFT": 0.3599201474781104, "MEDIUM": 0.10318931479141713, "HARD": 0.05447754444199232},
    55: {"SOFT": 0.3466957122060802, "MEDIUM": 0.0856137755344304, "HARD": 0.04624090126921309},
    56: {"SOFT": 0.3010342680012868, "MEDIUM": 0.1430824890115309, "HARD": 0.07971600798606836},
}


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


def replay_snapshot(path: Path):
    events, invalid = load_replay(path)
    messages = [
        LiveTimingMessage(
            topic=event.topic,
            payload=event.payload,
            source_timestamp_utc=None,
            received_at_utc=datetime.now(timezone.utc),
            schema_version=event.schema_version,
            source="archive_replay",
            sequence=event.sequence,
            session_time_seconds=event.session_time_seconds,
        )
        for event in events
        if event.topic not in {"CarData.z", "Position.z"}
    ]
    store = LiveSessionStore()
    store.apply_many(messages)
    return store.snapshot(), len(events), invalid


def model_config(service: OfflineAnalysisService) -> tuple[WeekendModelConfig, dict]:
    stored = service.live_model_config(YEAR, ROUND, "R")
    config = WeekendModelConfig(
        sample_count=PRODUCTION_SAMPLE_COUNT,
        random_seed=PRODUCTION_SEED,
        fuel_rate_bounds=tuple(stored["fuel_rate_bounds"]),
        track_rate_bounds=tuple(stored["track_rate_bounds"]),
        default_degradation_bounds=tuple(stored["default_degradation_bounds"]),
        default_compound_delta_bounds=tuple(stored["default_compound_delta_bounds"]),
        reference_compound=str(stored.get("reference_compound", "HARD")),
        clean_lap_noise_sigma=float(stored.get("clean_lap_noise_sigma", 0.35)),
        session_noise_sigma={
            str(key): float(value)
            for key, value in dict(stored.get("session_noise_sigma", {})).items()
        },
        enforce_compound_order=True,
    )
    return config, stored


def build_race_laps(
    snapshot,
) -> tuple[dict[int, pd.DataFrame], list[dict], list[dict]]:
    point_snapshots = dict(
        StrategyPredictionWindow._leader_lap_snapshots(
            snapshot, first_lap=min(FULL_LEADER_LAPS), last_lap=max(FULL_LEADER_LAPS)
        )
    )
    frames: dict[int, pd.DataFrame] = {}
    input_changes: list[dict] = []
    snapshot_rows: list[dict] = []
    previous_keys: set[tuple[str, int]] = set()
    previous_fingerprint = None
    for leader_lap in FULL_LEADER_LAPS:
        frames[leader_lap] = _live_analysis_laps(
            point_snapshots[leader_lap],
            quick_lap_threshold=1.1,
            min_clean_laps=4,
            allowed_compounds=COMPOUNDS,
            exclude_stint_start_laps=0,
            exclude_stint_end_laps=0,
            use_sector_boundary_traffic=True,
            min_gap_ahead_seconds=2.5,
            min_gap_behind_seconds=1.0,
        )
        frame = frames[leader_lap]
        keys = {
            (str(row.Driver), int(row.LapNumber))
            for row in frame.itertuples(index=False)
        }
        fingerprint = tuple(
            sorted(
                (
                    str(row.Driver),
                    int(row.LapNumber),
                    float(row.LapTimeSeconds),
                    str(row.Compound),
                    float(row.TyreLife),
                )
                for row in frame.itertuples(index=False)
            )
        )
        lap_candidates = [
            lap
            for lap in snapshot.completed_laps
            if int(lap.lap_number) == leader_lap and lap.position == 1
        ] or [
            lap for lap in snapshot.completed_laps if int(lap.lap_number) == leader_lap
        ]
        clocks = [
            StrategyPredictionWindow._completion_clock(lap) for lap in lap_candidates
        ]
        clocks = [value for value in clocks if value is not None]
        cutoff = min(clocks) if clocks else None
        if not fingerprint:
            status = "not-applicable-no-clean-race-laps"
        elif fingerprint == previous_fingerprint:
            status = "no-new-clean-laps"
        else:
            status = "production-valid-updated"
        snapshot_rows.append(
            {
                "leader_lap": leader_lap,
                "cutoff_time_s": cutoff,
                "snapshot_status": status,
                "race_clean_lap_count": len(frame),
                "new_observation_count": len(keys - previous_keys),
                "removed_observation_count": len(previous_keys - keys),
                "race_soft_count": int(frame["Compound"].eq("SOFT").sum()) if not frame.empty else 0,
                "race_medium_count": int(frame["Compound"].eq("MEDIUM").sum()) if not frame.empty else 0,
                "race_hard_count": int(frame["Compound"].eq("HARD").sum()) if not frame.empty else 0,
                "race_driver_count": int(frame["Driver"].nunique()) if not frame.empty else 0,
                "race_team_count": int(frame["Team"].nunique()) if not frame.empty else 0,
            }
        )
        previous_keys = keys
        previous_fingerprint = fingerprint
    for before, after in ((37, 38), (55, 56)):
        left = frames[before].set_index(["Driver", "LapNumber"])
        right = frames[after].set_index(["Driver", "LapNumber"])
        for driver, lap_number in right.index.difference(left.index):
            row = right.loc[(driver, lap_number)]
            input_changes.append(
                {
                    "before_leader_lap": before,
                    "after_leader_lap": after,
                    "change": "added",
                    "driver": driver,
                    "lap_number": int(lap_number),
                    "lap_time_s": float(row["LapTimeSeconds"]),
                    "compound": str(row["Compound"]),
                    "tyre_age": float(row["TyreLife"]),
                    "stint": int(row["Stint"]),
                    "run_id": str(row["RunId"]),
                    "gap_ahead_s": float(row["TrafficGapAheadSeconds"]),
                    "gap_behind_s": float(row["TrafficGapBehindSeconds"]),
                }
            )
        for driver, lap_number in left.index.difference(right.index):
            row = left.loc[(driver, lap_number)]
            input_changes.append(
                {
                    "before_leader_lap": before,
                    "after_leader_lap": after,
                    "change": "removed",
                    "driver": driver,
                    "lap_number": int(lap_number),
                    "lap_time_s": float(row["LapTimeSeconds"]),
                    "compound": str(row["Compound"]),
                    "tyre_age": float(row["TyreLife"]),
                    "stint": int(row["Stint"]),
                    "run_id": str(row["RunId"]),
                    "gap_ahead_s": float(row["TrafficGapAheadSeconds"]),
                    "gap_behind_s": float(row["TrafficGapBehindSeconds"]),
                }
            )
    return frames, input_changes, snapshot_rows


def load_fp(data_root: Path, stored: dict) -> dict[str, pd.DataFrame]:
    raw = {
        session: pd.read_pickle(
            data_root
            / "fastf1/session_laps/schema_v1"
            / f"year={YEAR}"
            / f"round={ROUND}"
            / f"session={session}"
            / "laps.pkl"
        )
        for session in SESSIONS[:-1]
    }
    prepared, _ = prepare_weekend_sessions(
        raw,
        min_clean_air_laps=int(stored.get("min_clean_air_laps", 4)),
        clean_mean_time_delta_seconds=float(stored.get("clean_mean_time_delta_seconds", 2.5)),
        clean_mean_time_delta_behind_seconds=float(
            stored.get("clean_mean_time_delta_behind_seconds", 1.0)
        ),
        quick_lap_threshold=float(stored.get("quick_lap_threshold", 1.1)),
        dry_compounds=tuple(stored.get("expected_compounds", COMPOUNDS)),
    )
    return {name: prepare(name, frame) for name, frame in prepared.items()}


def prepare(session: str, laps: pd.DataFrame) -> pd.DataFrame:
    output = _prepare_analysis_laps(laps)
    output["session"] = session
    output["run_id"] = session + ":" + output["run_id"].astype(str)
    return output


class QuadraticSessionLikelihood:
    def __init__(self, prepared: pd.DataFrame, session: str, dimension_names: tuple[str, ...]):
        self.prepared = prepared
        self.session = session
        indices = [dimension_names.index("fuel_rate"), dimension_names.index(f"track_rate:{session}")]
        columns = [prepared["run_lap_index"].to_numpy(float), prepared["track_index"].to_numpy(float)]
        for compound in sorted(COMPOUNDS):
            indices.append(dimension_names.index(f"degradation:{session}:{compound}"))
            columns.append(
                (prepared["compound"].eq(compound) * prepared["tyre_age"]).to_numpy(float)
            )
        for compound in sorted(COMPOUNDS):
            if compound == "HARD":
                continue
            indices.append(dimension_names.index(f"compound_delta:{session}:{compound}"))
            columns.append(prepared["compound"].eq(compound).to_numpy(float))
        x = np.column_stack(columns)
        y = prepared["lap_time_s"].to_numpy(float)
        codes, _ = pd.factorize(prepared["run_id"], sort=True)
        counts = np.bincount(codes)
        y -= np.bincount(codes, weights=y)[codes] / counts[codes]
        for column in range(x.shape[1]):
            x[:, column] -= np.bincount(codes, weights=x[:, column])[codes] / counts[codes]
        self.indices = np.asarray(indices)
        self.xtx = x.T @ x
        self.xty = x.T @ y
        self.yty = float(y @ y)

    def sse(self, candidates: np.ndarray) -> np.ndarray:
        beta = candidates[:, self.indices]
        return self.yty - 2.0 * (beta @ self.xty) + np.einsum(
            "bi,ij,bj->b", beta, self.xtx, beta, optimize=True
        )

    def validate(self, candidates: np.ndarray, dimension_names: tuple[str, ...]) -> float:
        maximum_error = 0.0
        for sample_id in (0, len(candidates) // 3, len(candidates) - 1):
            values = dict(zip(dimension_names, candidates[sample_id]))
            production = _joint_session_sse(self.prepared, self.session, values, "HARD")
            accelerated = float(self.sse(candidates[sample_id : sample_id + 1])[0])
            maximum_error = max(maximum_error, abs(production - accelerated))
        return maximum_error


def weighted_quantile_with_id(
    values: np.ndarray, weights: np.ndarray, quantile: float
) -> tuple[float, int]:
    order = np.argsort(values, kind="mergesort")
    position = min(
        int(np.searchsorted(np.cumsum(weights[order]), quantile, side="left")),
        len(order) - 1,
    )
    sample_id = int(order[position])
    return float(values[sample_id]), sample_id


def posterior_weights(cost: np.ndarray) -> np.ndarray:
    log_weights = -0.5 * (cost - float(cost.min()))
    weights = np.exp(log_weights - float(log_weights.max()))
    return weights / float(weights.sum())


def aggregate_candidate_degradation(
    candidates: np.ndarray,
    dimension_names: tuple[str, ...],
    support: dict[str, int],
    compound: str,
) -> np.ndarray:
    total = float(sum(support.values()))
    return sum(
        candidates[:, dimension_names.index(f"degradation:{session}:{compound}")]
        * support[session]
        for session in SESSIONS
    ) / total


def summarize_run(
    *,
    bank,
    weights: np.ndarray,
    support: dict[str, int],
    sample_count: int,
    seed: int,
    leader_lap: int,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    order = np.argsort(weights)[::-1]
    map_id = int(order[0])
    ess = float(1.0 / np.sum(weights**2))
    summary = {
        "sample_count": sample_count,
        "valid_candidate_count": sample_count,
        "seed": seed,
        "leader_lap": leader_lap,
        "ess": ess,
        "ess_fraction": ess / sample_count,
        "max_weight": float(weights[map_id]),
        "top5_weight": float(weights[order[:5]].sum()),
        "top10_weight": float(weights[order[:10]].sum()),
        "map_candidate_id": map_id,
        "race_clean_lap_count": support["R"],
    }
    parameter_rows = []
    for index, name in enumerate(bank.dimension_names):
        values = bank.candidates[:, index]
        p10, p10_id = weighted_quantile_with_id(values, weights, 0.10)
        p50, p50_id = weighted_quantile_with_id(values, weights, 0.50)
        p90, p90_id = weighted_quantile_with_id(values, weights, 0.90)
        parameter_rows.append(
            {
                **{key: summary[key] for key in ("sample_count", "seed", "leader_lap")},
                "parameter": name,
                "weighted_mean": float(weights @ values),
                "p10": p10,
                "p50": p50,
                "p90": p90,
                "p10_source_candidate_id": p10_id,
                "p50_source_candidate_id": p50_id,
                "p90_source_candidate_id": p90_id,
                "map_value": float(values[map_id]),
            }
        )
    degradation_rows = []
    for compound in COMPOUNDS:
        aggregate = aggregate_candidate_degradation(
            bank.candidates, bank.dimension_names, support, compound
        )
        p10, p10_id = weighted_quantile_with_id(aggregate, weights, 0.10)
        p50, p50_id = weighted_quantile_with_id(aggregate, weights, 0.50)
        p90, p90_id = weighted_quantile_with_id(aggregate, weights, 0.90)
        session_medians = []
        session_median_ids = []
        for session in SESSIONS:
            values = bank.candidates[
                :, bank.dimension_names.index(f"degradation:{session}:{compound}")
            ]
            median, median_id = weighted_quantile_with_id(values, weights, 0.50)
            session_medians.append(median * support[session])
            session_median_ids.append(median_id)
        degradation_rows.append(
            {
                **{key: summary[key] for key in ("sample_count", "seed", "leader_lap")},
                "compound": compound,
                "weighted_mean": float(weights @ aggregate),
                "posterior_p10": p10,
                "posterior_p50": p50,
                "posterior_p90": p90,
                "posterior_p10_source_candidate_id": p10_id,
                "posterior_p50_source_candidate_id": p50_id,
                "posterior_p90_source_candidate_id": p90_id,
                "production_reported_median": float(sum(session_medians) / sum(support.values())),
                "production_session_median_candidate_ids": "+".join(
                    str(value) for value in session_median_ids
                ),
                "map_value": float(aggregate[map_id]),
            }
        )
    top_rows = []
    for rank, sample_id in enumerate(order[:20], 1):
        row = {
            **{key: summary[key] for key in ("sample_count", "seed", "leader_lap")},
            "rank": rank,
            "candidate_id": int(sample_id),
            "weight": float(weights[sample_id]),
        }
        for index, name in enumerate(bank.dimension_names):
            row[name] = float(bank.candidates[sample_id, index])
        for compound in COMPOUNDS:
            aggregate = aggregate_candidate_degradation(
                bank.candidates[sample_id : sample_id + 1],
                bank.dimension_names,
                support,
                compound,
            )
            row[f"aggregate_degradation:{compound}"] = float(aggregate[0])
        top_rows.append(row)
    return summary, parameter_rows, degradation_rows, top_rows


def correlation_rows(bank, weights, sample_count: int, seed: int, leader_lap: int) -> list[dict]:
    names = bank.dimension_names
    mean = weights @ bank.candidates
    centered = bank.candidates - mean
    covariance = (centered * weights[:, None]).T @ centered
    scales = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    denominator = np.outer(scales, scales)
    correlation = np.divide(
        covariance,
        denominator,
        out=np.full_like(covariance, np.nan),
        where=denominator > 0,
    )
    rows = []
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            rows.append(
                {
                    "sample_count": sample_count,
                    "seed": seed,
                    "leader_lap": leader_lap,
                    "parameter_a": names[left],
                    "parameter_b": names[right],
                    "weighted_correlation": float(correlation[left, right]),
                }
            )
    return rows


def candidates_for_mass(weights: np.ndarray, target: float) -> int:
    ordered = np.sort(weights)[::-1]
    return int(np.searchsorted(np.cumsum(ordered), target, side="left") + 1)


def production_degradation_summary(
    bank,
    weights: np.ndarray,
    support: dict[str, int],
    compound: str,
) -> dict[str, float]:
    aggregate = aggregate_candidate_degradation(
        bank.candidates, bank.dimension_names, support, compound
    )
    posterior_median, _ = weighted_quantile_with_id(aggregate, weights, 0.50)
    session_medians = []
    for session in SESSIONS:
        values = bank.candidates[
            :, bank.dimension_names.index(f"degradation:{session}:{compound}")
        ]
        median, _ = weighted_quantile_with_id(values, weights, 0.50)
        session_medians.append(median * support[session])
    return {
        "mean": float(weights @ aggregate),
        "posterior_median": posterior_median,
        "production": float(sum(session_medians) / sum(support.values())),
    }


def full_posterior_history(
    *,
    bank,
    config: WeekendModelConfig,
    fp_prepared: dict[str, pd.DataFrame],
    race_frames: dict[int, pd.DataFrame],
    snapshot_rows: list[dict],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    likelihoods = {
        session: QuadraticSessionLikelihood(
            fp_prepared[session], session, bank.dimension_names
        )
        for session in SESSIONS[:-1]
    }
    fixed_cost = sum(
        likelihoods[session].sse(bank.candidates)
        / float(config.clean_lap_noise_sigma**2)
        for session in SESSIONS[:-1]
    )
    rows = []
    switch_rows = []
    previous_map_id = None
    previous_map_weight = None
    previous_weights = None
    previous_valid_lap = None
    snapshot_by_lap = {int(row["leader_lap"]): row for row in snapshot_rows}
    for leader_lap in FULL_LEADER_LAPS:
        snapshot_row = snapshot_by_lap[leader_lap]
        if snapshot_row["snapshot_status"] != "production-valid-updated":
            continue
        race = prepare("R", race_frames[leader_lap])
        likelihood = QuadraticSessionLikelihood(race, "R", bank.dimension_names)
        total_cost = fixed_cost + likelihood.sse(bank.candidates) / float(
            config.clean_lap_noise_sigma**2
        )
        weights = posterior_weights(total_cost)
        order = np.argsort(weights)[::-1]
        map_id = int(order[0])
        support = {
            **{session: len(frame) for session, frame in fp_prepared.items()},
            "R": len(race),
        }
        degradation = {
            compound: production_degradation_summary(bank, weights, support, compound)
            for compound in COMPOUNDS
        }
        changed = previous_map_id is not None and map_id != previous_map_id
        row = {
            **snapshot_row,
            "sample_count": len(bank.candidates),
            "valid_candidate_count": len(bank.candidates),
            "ess": float(1.0 / np.sum(weights**2)),
            "ess_fraction": float(1.0 / np.sum(weights**2) / len(bank.candidates)),
            "max_weight": float(weights[map_id]),
            "top5_weight": float(weights[order[:5]].sum()),
            "top10_weight": float(weights[order[:10]].sum()),
            "candidate_count_50pct": candidates_for_mass(weights, 0.50),
            "candidate_count_90pct": candidates_for_mass(weights, 0.90),
            "candidate_count_99pct": candidates_for_mass(weights, 0.99),
            "map_candidate_id": map_id,
            "map_candidate_weight": float(weights[map_id]),
            "map_changed": bool(changed),
        }
        for compound in COMPOUNDS:
            lower = compound.lower()
            row[f"{lower}_degradation_production"] = degradation[compound]["production"]
            row[f"{lower}_degradation_mean"] = degradation[compound]["mean"]
            row[f"{lower}_degradation_posterior_median"] = degradation[compound][
                "posterior_median"
            ]
        rows.append(row)
        if changed:
            switch_rows.append(
                {
                    "leader_lap": leader_lap,
                    "previous_valid_leader_lap": previous_valid_lap,
                    "previous_map_candidate_id": previous_map_id,
                    "new_map_candidate_id": map_id,
                    "previous_map_weight_before": previous_map_weight,
                    "previous_map_weight_after": float(weights[previous_map_id]),
                    "new_map_weight_before": float(previous_weights[map_id]),
                    "new_map_weight_after": float(weights[map_id]),
                    "ess_before": float(1.0 / np.sum(previous_weights**2)),
                    "ess_after": row["ess"],
                    "new_observation_count": snapshot_row["new_observation_count"],
                }
            )
        previous_map_id = map_id
        previous_map_weight = float(weights[map_id])
        previous_weights = weights
        previous_valid_lap = leader_lap
    history = pd.DataFrame(rows)
    switches = pd.DataFrame(switch_rows)
    persistence = (
        history.groupby("map_candidate_id")
        .agg(
            snapshot_count=("leader_lap", "size"),
            first_leader_lap=("leader_lap", "min"),
            last_leader_lap=("leader_lap", "max"),
            mean_map_weight=("map_candidate_weight", "mean"),
            min_map_weight=("map_candidate_weight", "min"),
            max_map_weight=("map_candidate_weight", "max"),
        )
        .sort_values("snapshot_count", ascending=False)
        .reset_index()
    )
    return history, switches, persistence


def first_threshold(history: pd.DataFrame, column: str, threshold: float, op: str):
    mask = history[column].lt(threshold) if op == "lt" else history[column].gt(threshold)
    matching = history.loc[mask]
    return None if matching.empty else int(matching.iloc[0]["leader_lap"])


def collapse_summary(history: pd.DataFrame) -> dict[str, object]:
    first = history.iloc[0]
    minimum_ess = history.loc[history["ess"].idxmin()]
    maximum_weight = history.loc[history["max_weight"].idxmax()]
    summary: dict[str, object] = {
        "first_valid_leader_lap": int(first["leader_lap"]),
        "first_valid_race_clean_lap_count": int(first["race_clean_lap_count"]),
        "ess_at_first_valid": float(first["ess"]),
        "max_weight_at_first_valid": float(first["max_weight"]),
        "minimum_ess": float(minimum_ess["ess"]),
        "minimum_ess_leader_lap": int(minimum_ess["leader_lap"]),
        "maximum_candidate_weight": float(maximum_weight["max_weight"]),
        "maximum_candidate_weight_leader_lap": int(maximum_weight["leader_lap"]),
    }
    for threshold in (100, 50, 20, 10, 5):
        summary[f"first_ess_below_{threshold}"] = first_threshold(
            history, "ess", threshold, "lt"
        )
    for percentage in (10, 25, 50, 75, 90):
        summary[f"first_max_weight_above_{percentage}pct"] = first_threshold(
            history, "max_weight", percentage / 100.0, "gt"
        )
    return summary


def plot_posterior_history(history: pd.DataFrame, output_dir: Path) -> None:
    laps = history["leader_lap"]
    figure, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    axes[0].plot(laps, history["ess"], marker="o", markersize=3)
    axes[0].set_ylabel("ESS")
    axes[0].grid(alpha=0.25)
    axes[1].semilogy(laps, history["ess"], marker="o", markersize=3)
    axes[1].set_ylabel("ESS (log scale)")
    axes[1].grid(alpha=0.25)
    axes[2].semilogy(laps, history["ess_fraction"], marker="o", markersize=3)
    axes[2].set_ylabel("ESS / N (log scale)")
    axes[2].set_xlabel("Leader lap")
    axes[2].grid(alpha=0.25)
    figure.suptitle("2026 R11 fixed-bank posterior effective sample size")
    figure.tight_layout()
    figure.savefig(output_dir / "ess_history.png", dpi=180)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(11, 5))
    axis.plot(laps, history["max_weight"], marker="o", markersize=3)
    for value in (0.1, 0.25, 0.5, 0.75, 0.9):
        axis.axhline(value, color="grey", linewidth=0.7, alpha=0.35)
    axis.set(xlabel="Leader lap", ylabel="Maximum candidate weight", ylim=(0, 1.02))
    axis.grid(alpha=0.25)
    axis.set_title("Maximum posterior candidate weight")
    figure.tight_layout()
    figure.savefig(output_dir / "max_weight_history.png", dpi=180)
    plt.close(figure)

    figure, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    axes[0].plot(laps, history["top5_weight"], label="Top 5")
    axes[0].plot(laps, history["top10_weight"], label="Top 10")
    axes[0].set(ylabel="Cumulative posterior weight", ylim=(0, 1.02))
    axes[0].legend()
    axes[0].grid(alpha=0.25)
    for column, label in (
        ("candidate_count_50pct", "50% mass"),
        ("candidate_count_90pct", "90% mass"),
        ("candidate_count_99pct", "99% mass"),
    ):
        axes[1].semilogy(laps, history[column], marker="o", markersize=2, label=label)
    axes[1].set(xlabel="Leader lap", ylabel="Candidates required (log scale)")
    axes[1].legend()
    axes[1].grid(alpha=0.25)
    figure.suptitle("Posterior concentration history")
    figure.tight_layout()
    figure.savefig(output_dir / "posterior_concentration_history.png", dpi=180)
    plt.close(figure)

    figure, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    colours = {"soft": "#e53935", "medium": "#d99b00", "hard": "#555555"}
    for compound in ("soft", "medium", "hard"):
        axes[0].plot(
            laps,
            history[f"{compound}_degradation_production"],
            marker="o",
            markersize=2.5,
            label=compound.upper(),
            color=colours[compound],
        )
    axes[0].set_ylabel("Production degradation (s/lap)")
    axes[0].legend()
    axes[0].grid(alpha=0.25)
    axes[1].semilogy(laps, history["ess"], color="#42a5f5", marker="o", markersize=2.5)
    axes[1].set_ylabel("ESS (log scale)")
    axes[1].grid(alpha=0.25)
    axes[2].plot(laps, history["max_weight"], color="#ab47bc", marker="o", markersize=2.5)
    axes[2].set(xlabel="Leader lap", ylabel="Maximum weight", ylim=(0, 1.02))
    axes[2].grid(alpha=0.25)
    figure.suptitle("Tyre degradation history versus posterior quality")
    figure.tight_layout()
    figure.savefig(output_dir / "degradation_vs_posterior_quality.png", dpi=180)
    plt.close(figure)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    service = OfflineAnalysisService(args.data_root)
    base_config, stored = model_config(service)
    snapshot, replay_event_count, invalid_replay_records = replay_snapshot(args.replay)
    race_frames, input_changes, snapshot_rows = build_race_laps(snapshot)
    fp_prepared = load_fp(args.data_root, stored)
    race_prepared = {lap: prepare("R", race_frames[lap]) for lap in LEADER_LAPS}

    run_rows: list[dict] = []
    parameter_rows: list[dict] = []
    degradation_rows: list[dict] = []
    top_rows: list[dict] = []
    correlation_output: list[dict] = []
    validations: dict[str, object] = {}
    combinations = [(count, PRODUCTION_SEED) for count in SAMPLE_COUNTS]
    combinations += [
        (count, seed)
        for count in (80_000, 320_000)
        for seed in SEEDS
        if seed != PRODUCTION_SEED
    ]
    for sample_count, seed in combinations:
        config = WeekendModelConfig(
            **{
                **asdict(base_config),
                "sample_count": sample_count,
                "random_seed": seed,
            }
        )
        bank = generate_joint_sample_bank(
            SESSIONS,
            {session: COMPOUNDS for session in SESSIONS},
            config,
        )
        likelihoods = {
            session: QuadraticSessionLikelihood(
                fp_prepared[session], session, bank.dimension_names
            )
            for session in SESSIONS[:-1]
        }
        fixed_cost = sum(
            likelihoods[session].sse(bank.candidates)
            / float(config.clean_lap_noise_sigma**2)
            for session in SESSIONS[:-1]
        )
        if sample_count == PRODUCTION_SAMPLE_COUNT and seed == PRODUCTION_SEED:
            validations["bank_id"] = bank.sample_bank_id
            validations["expected_bank_id"] = EXPECTED_BANK_ID
            validations["accelerated_sse_max_abs_error"] = max(
                likelihood.validate(bank.candidates, bank.dimension_names)
                for likelihood in likelihoods.values()
            )
        for leader_lap in LEADER_LAPS:
            race_likelihood = QuadraticSessionLikelihood(
                race_prepared[leader_lap], "R", bank.dimension_names
            )
            if sample_count == PRODUCTION_SAMPLE_COUNT and seed == PRODUCTION_SEED:
                validations["accelerated_sse_max_abs_error"] = max(
                    float(validations["accelerated_sse_max_abs_error"]),
                    race_likelihood.validate(bank.candidates, bank.dimension_names),
                )
            total_cost = fixed_cost + race_likelihood.sse(bank.candidates) / float(
                config.clean_lap_noise_sigma**2
            )
            weights = posterior_weights(total_cost)
            support = {
                **{session: len(frame) for session, frame in fp_prepared.items()},
                "R": len(race_prepared[leader_lap]),
            }
            summary, parameters, degradation, top = summarize_run(
                bank=bank,
                weights=weights,
                support=support,
                sample_count=sample_count,
                seed=seed,
                leader_lap=leader_lap,
            )
            run_rows.append(summary)
            parameter_rows.extend(parameters)
            degradation_rows.extend(degradation)
            top_rows.extend(top)
            if sample_count == PRODUCTION_SAMPLE_COUNT and seed == PRODUCTION_SEED:
                correlation_output.extend(
                    correlation_rows(bank, weights, sample_count, seed, leader_lap)
                )
    production_degradation = pd.DataFrame(degradation_rows)
    production_degradation = production_degradation.loc[
        production_degradation["sample_count"].eq(PRODUCTION_SAMPLE_COUNT)
        & production_degradation["seed"].eq(PRODUCTION_SEED)
    ]
    maximum_history_error = 0.0
    for row in production_degradation.to_dict("records"):
        expected = EXPECTED_PRODUCTION_DEGRADATION[int(row["leader_lap"])][row["compound"]]
        maximum_history_error = max(
            maximum_history_error, abs(float(row["production_reported_median"]) - expected)
        )
    validations["production_history_max_abs_error"] = maximum_history_error

    production_bank = generate_joint_sample_bank(
        SESSIONS,
        {session: COMPOUNDS for session in SESSIONS},
        base_config,
    )
    posterior_history, map_switches, map_persistence = full_posterior_history(
        bank=production_bank,
        config=base_config,
        fp_prepared=fp_prepared,
        race_frames=race_frames,
        snapshot_rows=snapshot_rows,
    )
    saved_plot_path = (
        args.data_root
        / "wostrategy/weekend_model/schema_v1"
        / f"year={YEAR}"
        / f"round={ROUND}"
        / "saved_history_plots/2026-08-07_21-27-52/plot.json"
    )
    saved_plot = json.loads(saved_plot_path.read_text(encoding="utf-8"))
    saved_degradation = pd.DataFrame(saved_plot["parameter_history"])
    saved_degradation = saved_degradation.loc[
        saved_degradation["parameter"].eq("degradation")
        & saved_degradation["as_of_leader_lap"].gt(0)
    ]
    expected_valid_laps = sorted(saved_degradation["as_of_leader_lap"].unique().tolist())
    actual_valid_laps = posterior_history["leader_lap"].astype(int).tolist()
    validations["full_history_laps_match"] = actual_valid_laps == expected_valid_laps
    full_history_error = 0.0
    history_by_lap = posterior_history.set_index("leader_lap")
    for row in saved_degradation.itertuples(index=False):
        actual = history_by_lap.loc[
            int(row.as_of_leader_lap),
            f"{str(row.compound).lower()}_degradation_production",
        ]
        full_history_error = max(full_history_error, abs(float(actual) - float(row.median)))
    validations["full_history_max_abs_error"] = full_history_error
    validations["passed"] = bool(
        validations["bank_id"] == EXPECTED_BANK_ID
        and float(validations["accelerated_sse_max_abs_error"]) < 1e-8
        and maximum_history_error < 1e-12
        and validations["full_history_laps_match"]
        and full_history_error < 1e-12
    )
    if not validations["passed"]:
        raise RuntimeError(f"Diagnostic validation failed: {validations}")

    runs_frame = pd.DataFrame(run_rows)
    parameters_frame = pd.DataFrame(parameter_rows)
    degradation_frame = pd.DataFrame(degradation_rows)
    top_frame = pd.DataFrame(top_rows)
    runs_frame.to_csv(args.output_dir / "posterior_runs.csv", index=False)
    parameters_frame.to_csv(
        args.output_dir / "parameter_posterior_statistics.csv", index=False
    )
    degradation_frame.to_csv(
        args.output_dir / "degradation_posterior_statistics.csv", index=False
    )
    top_frame.to_csv(args.output_dir / "top20_candidates.csv", index=False)
    pd.DataFrame(correlation_output).to_csv(
        args.output_dir / "production_weighted_correlations.csv", index=False
    )
    pd.DataFrame(input_changes).to_csv(args.output_dir / "clean_lap_input_changes.csv", index=False)
    degradation_wide = degradation_frame.pivot(
        index=["sample_count", "seed", "leader_lap"],
        columns="compound",
        values=["weighted_mean", "production_reported_median"],
    )
    degradation_wide.columns = [f"{metric}:{compound}" for metric, compound in degradation_wide.columns]
    individual = runs_frame.merge(degradation_wide.reset_index(), on=["sample_count", "seed", "leader_lap"])
    individual.to_csv(args.output_dir / "seed_individual_summary.csv", index=False)
    individual.loc[individual["seed"].eq(PRODUCTION_SEED)].to_csv(
        args.output_dir / "sample_count_sensitivity.csv", index=False
    )
    seed_degradation = degradation_frame.loc[
        degradation_frame["sample_count"].isin((80_000, 320_000))
    ].groupby(["sample_count", "leader_lap", "compound"])[
        ["production_reported_median", "weighted_mean"]
    ].agg(["mean", "std", "min", "max"])
    seed_degradation.columns = [f"{metric}_{stat}" for metric, stat in seed_degradation.columns]
    seed_runs = runs_frame.loc[runs_frame["sample_count"].isin((80_000, 320_000))].groupby(
        ["sample_count", "leader_lap"]
    )[["ess", "max_weight"]].agg(["mean", "min", "max"])
    seed_runs.columns = [f"{metric}_{stat}" for metric, stat in seed_runs.columns]
    seed_degradation.reset_index().merge(
        seed_runs.reset_index(), on=["sample_count", "leader_lap"]
    ).to_csv(args.output_dir / "seed_aggregate_summary.csv", index=False)
    degradation_frame.loc[
        degradation_frame["sample_count"].eq(PRODUCTION_SAMPLE_COUNT)
        & degradation_frame["seed"].eq(PRODUCTION_SEED)
    ].to_csv(args.output_dir / "production_mean_median_map.csv", index=False)
    posterior_history.to_csv(args.output_dir / "posterior_history.csv", index=False)
    pd.DataFrame(snapshot_rows).to_csv(args.output_dir / "replay_snapshot_status.csv", index=False)
    map_switches.to_csv(args.output_dir / "map_switches.csv", index=False)
    map_persistence.to_csv(args.output_dir / "map_persistence.csv", index=False)
    summary = collapse_summary(posterior_history)
    evidence_onset = {
        compound: int(
            posterior_history.loc[
                posterior_history[f"race_{compound.lower()}_count"].gt(0), "leader_lap"
            ].iloc[0]
        )
        if posterior_history[f"race_{compound.lower()}_count"].gt(0).any()
        else None
        for compound in COMPOUNDS
    }
    relationship = {
        "clean_laps_vs_ess_pearson": float(
            posterior_history["race_clean_lap_count"].corr(posterior_history["ess"])
        ),
        "clean_laps_vs_ess_spearman": float(
            posterior_history["race_clean_lap_count"].corr(
                posterior_history["ess"], method="spearman"
            )
        ),
        "clean_laps_vs_max_weight_pearson": float(
            posterior_history["race_clean_lap_count"].corr(
                posterior_history["max_weight"]
            )
        ),
        "clean_laps_vs_max_weight_spearman": float(
            posterior_history["race_clean_lap_count"].corr(
                posterior_history["max_weight"], method="spearman"
            )
        ),
    }
    pd.DataFrame(
        [{"metric": key, "value": value} for key, value in summary.items()]
    ).to_csv(args.output_dir / "posterior_collapse_summary.csv", index=False)
    (args.output_dir / "posterior_collapse_summary.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "race_compound_evidence_onset": evidence_onset,
                "observation_relationship": relationship,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plot_posterior_history(posterior_history, args.output_dir)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "year": YEAR,
        "round": ROUND,
        "leader_laps": LEADER_LAPS,
        "full_leader_laps": FULL_LEADER_LAPS,
        "sample_counts": SAMPLE_COUNTS,
        "seeds": SEEDS,
        "production_sample_count": PRODUCTION_SAMPLE_COUNT,
        "production_seed": PRODUCTION_SEED,
        "replay": str(args.replay),
        "replay_event_count": replay_event_count,
        "invalid_replay_records": invalid_replay_records,
        "sampled_dimension_count": len(bank.dimension_names),
        "dimension_names": bank.dimension_names,
        "base_config": asdict(base_config),
        "resolved_settings_source": stored.get("settings_source"),
        "resolved_settings_path": stored.get("settings_path"),
        "validations": validations,
        "posterior_collapse_summary": summary,
        "race_compound_evidence_onset": evidence_onset,
        "observation_relationship": relationship,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output_dir": str(args.output_dir), "validations": validations}, indent=2))


if __name__ == "__main__":
    main()
