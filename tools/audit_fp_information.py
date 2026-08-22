#!/usr/bin/env python3
"""Diagnostic-only audit of what the production FP likelihood identifies."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from wostrategy.core.pre_race_session_data import prepare_weekend_sessions
from wostrategy.model.pre_race_performance import (
    WeekendModelConfig,
    _joint_session_sse,
    _prepare_analysis_laps,
    generate_joint_sample_bank,
)
from wostrategy.model.correction_identifiability import baseline_project

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "woData"
MODEL_ROOT = DATA / "wostrategy/weekend_model/schema_v1/year=2026/round=11"
SESSIONS = ("FP1", "FP2", "FP3")
EXPECTED_SESSIONS = (*SESSIONS, "R")
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


def weighted_quantiles(values, weights):
    order = np.argsort(values, kind="mergesort")
    cumulative = np.cumsum(weights[order])
    return tuple(float(values[order[min(np.searchsorted(cumulative, q), len(order)-1)]])
                 for q in (.1, .5, .9))


def posterior_weights(cost):
    log_weight = -.5 * (cost - float(np.min(cost)))
    weights = np.exp(log_weight - float(np.max(log_weight)))
    return weights / weights.sum()


def project_by_run(matrix, codes):
    return baseline_project(np.asarray(matrix, float), codes)


class Likelihood:
    def __init__(self, prepared, session, dimension_names):
        self.prepared = prepared
        self.session = session
        self.dimension_names = dimension_names
        names = ["fuel_rate", f"track_rate:{session}"]
        columns = [prepared.run_lap_index.to_numpy(float), prepared.track_index.to_numpy(float)]
        for compound in COMPOUNDS:
            names.append(f"degradation:{session}:{compound}")
            columns.append((prepared.compound.eq(compound) * prepared.tyre_age).to_numpy(float))
        for compound in COMPOUNDS:
            if compound == "MEDIUM":
                continue
            names.append(f"compound_delta:{session}:{compound}")
            columns.append(prepared.compound.eq(compound).to_numpy(float))
        self.names = tuple(names)
        self.indices = np.array([dimension_names.index(name) for name in names])
        self.codes, self.runs = pd.factorize(prepared.run_id, sort=True)
        x = project_by_run(np.column_stack(columns), self.codes)
        y = project_by_run(prepared.lap_time_s.to_numpy(float)[:, None], self.codes)[:, 0]
        self.x = x
        self.y = y
        self.xtx, self.xty, self.yty = x.T @ x, x.T @ y, float(y @ y)

    def sse(self, candidates):
        beta = candidates[:, self.indices]
        value = self.yty - 2 * beta @ self.xty + np.einsum(
            "bi,ij,bj->b", beta, self.xtx, beta, optimize=True
        )
        return np.maximum(value, 0.0)

    def run_sse(self, candidates):
        output = {}
        beta = candidates[:, self.indices]
        for code, run in enumerate(self.runs):
            mask = self.codes == code
            x, y = self.x[mask], self.y[mask]
            residual = y[None, :] - beta @ x.T
            output[str(run)] = np.sum(residual**2, axis=1)
        return output


def load_inputs():
    stored = json.loads((MODEL_ROOT / "model_config.json").read_text())
    raw = {session: pd.read_pickle(
        DATA / f"fastf1/session_laps/schema_v1/year=2026/round=11/session={session}/laps.pkl"
    ) for session in SESSIONS}
    selected, excluded = prepare_weekend_sessions(
        raw, min_clean_air_laps=int(stored["min_clean_air_laps"]),
        clean_mean_time_delta_seconds=float(stored["clean_mean_time_delta_seconds"]),
        clean_mean_time_delta_behind_seconds=float(stored["clean_mean_time_delta_behind_seconds"]),
        quick_lap_threshold=float(stored["quick_lap_threshold"]),
        dry_compounds=tuple(stored["expected_compounds"]),
    )
    if excluded:
        raise RuntimeError(f"Unexpected R11 FP exclusions: {excluded}")
    prepared = {}
    for session, laps in selected.items():
        frame = _prepare_analysis_laps(laps)
        frame["session"] = session
        frame["run_id"] = session + ":" + frame.run_id.astype(str)
        prepared[session] = frame
    config = WeekendModelConfig(
        sample_count=int(stored["sample_count"]), random_seed=int(stored["random_seed"]),
        fuel_rate_bounds=tuple(stored["fuel_rate_bounds"]),
        track_rate_bounds=tuple(stored["track_rate_bounds"]),
        default_degradation_bounds=tuple(stored["default_degradation_bounds"]),
        default_compound_delta_bounds=tuple(stored["default_compound_delta_bounds"]),
        reference_compound=str(stored["reference_compound"]),
        clean_lap_noise_sigma=float(stored["clean_lap_noise_sigma"]),
        session_noise_sigma=stored.get("session_noise_sigma", {}),
        enforce_compound_order=bool(stored["enforce_compound_order"]),
    )
    return stored, selected, prepared, config


def run_support(selected, prepared):
    rows = []
    for session, frame in prepared.items():
        source = selected[session].copy()
        source["_driver"] = source.get("Driver", "unknown").astype(str)
        source["_team"] = source.get("Team", "unknown").astype(str)
        identity = source.groupby("RunId").agg(driver=("_driver", "first"), team=("_team", "first"))
        for run, group in frame.groupby("run_id", sort=True):
            original = run.removeprefix(session + ":")
            info = identity.loc[original]
            rows.append({
                "session": session, "run_id": run, "driver": info.driver, "team": info.team,
                "compound": group.compound.iloc[0], "selected_clean_laps": len(group),
                "qualifying_long_runs": 1, "run_length": len(group),
                "starting_tyre_age": float(group.tyre_age.iloc[0]),
                "minimum_tyre_age": float(group.tyre_age.min()),
                "maximum_tyre_age": float(group.tyre_age.max()),
                "tyre_age_span": float(group.tyre_age.max() - group.tyre_age.min()),
                "lap_number_start": float(group.lap_number.min()),
                "lap_number_end": float(group.lap_number.max()),
                "track_index_span": float(group.track_index.max() - group.track_index.min()),
                "compound_count_within_run": int(group.compound.nunique()),
                "tyre_age_minus_run_index_span": float(np.ptp(
                    group.tyre_age.to_numpy() - group.run_lap_index.to_numpy()
                )),
            })
    return pd.DataFrame(rows)


def identifiability(prepared, dimension_names):
    likelihoods = {s: Likelihood(p, s, dimension_names) for s, p in prepared.items()}
    active_names = ["fuel_rate"]
    for session in SESSIONS:
        active_names += [name for name in dimension_names if name.startswith((
            f"track_rate:{session}", f"degradation:{session}:", f"compound_delta:{session}:"
        ))]
    blocks = []
    for session, likelihood in likelihoods.items():
        block = np.zeros((len(likelihood.x), len(active_names)))
        for local, name in enumerate(likelihood.names):
            block[:, active_names.index(name)] = likelihood.x[:, local]
        blocks.append(block)
    design = np.vstack(blocks)
    u, singular, vt = np.linalg.svd(design, full_matrices=False)
    tolerance = max(design.shape) * np.finfo(float).eps * singular[0]
    rank = int(np.sum(singular > tolerance))
    norms = np.linalg.norm(design, axis=0)
    normalized = np.divide(design, norms, out=np.zeros_like(design), where=norms > 0)
    correlations = normalized.T @ normalized
    summary_rows = [{
        "scope": "joint_FP1_FP2_FP3",
        "observation_count": len(design), "parameter_count": len(active_names), "rank": rank,
        "nullity": len(active_names)-rank, "largest_singular_value": singular[0],
        "smallest_singular_value": singular[-1],
        "smallest_normalized_singular_value": singular[-1]/singular[0],
        "condition_number": np.inf if rank < len(active_names) else singular[0]/singular[-1],
        "singular_values": "|".join(f"{value:.17g}" for value in singular),
        "normalized_singular_values": "|".join(f"{value/singular[0]:.17g}" for value in singular),
    }]
    for session, likelihood in likelihoods.items():
        local_singular = np.linalg.svd(likelihood.x, compute_uv=False)
        local_tolerance = max(likelihood.x.shape) * np.finfo(float).eps * local_singular[0]
        local_rank = int(np.sum(local_singular > local_tolerance))
        summary_rows.append({
            "scope": session, "observation_count": len(likelihood.x),
            "parameter_count": len(likelihood.names), "rank": local_rank,
            "nullity": len(likelihood.names)-local_rank,
            "largest_singular_value": local_singular[0],
            "smallest_singular_value": local_singular[-1],
            "smallest_normalized_singular_value": local_singular[-1]/local_singular[0],
            "condition_number": (np.inf if local_rank < len(likelihood.names)
                                 else local_singular[0]/local_singular[-1]),
            "singular_values": "|".join(f"{value:.17g}" for value in local_singular),
            "normalized_singular_values": "|".join(
                f"{value/local_singular[0]:.17g}" for value in local_singular
            ),
        })
    pair_rows = []
    for left in range(len(active_names)):
        for right in range(left + 1, len(active_names)):
            pair_rows.append({"parameter_a": active_names[left], "parameter_b": active_names[right],
                              "projected_sensitivity_correlation": correlations[left, right],
                              "norm_a": norms[left], "norm_b": norms[right]})
    null_rows = []
    for null_index, vector in enumerate(vt[rank:]):
        scale = np.max(np.abs(vector))
        for name, coefficient in zip(active_names, vector / scale):
            if abs(coefficient) > 1e-8:
                null_rows.append({"null_vector": null_index + 1, "parameter": name,
                                  "coefficient_scaled_max_abs_1": coefficient,
                                  "design_residual_norm": np.linalg.norm(design @ vector)})
    column_rows = [{"parameter": name, "projected_sensitivity_norm": norm,
                    "structurally_zero_after_run_profiling": bool(norm < tolerance)}
                   for name, norm in zip(active_names, norms)]
    exact_null_rows = []
    observed_degradation = []
    for session, frame in prepared.items():
        for compound in COMPOUNDS:
            name = f"degradation:{session}:{compound}"
            if frame.compound.eq(compound).any():
                observed_degradation.append(name)
            else:
                exact_null_rows.append({"null_direction": f"unobserved_{name}",
                                        "parameter": name, "coefficient": 1.0,
                                        "reason": "no selected observations for this session-compound"})
        for compound in ("HARD", "SOFT"):
            name = f"compound_delta:{session}:{compound}"
            exact_null_rows.append({"null_direction": f"profiled_constant_{name}",
                                    "parameter": name, "coefficient": 1.0,
                                    "reason": "constant within every single-compound run; absorbed by run intercept"})
    exact_null_rows.append({"null_direction": "fuel_vs_all_observed_degradation",
                            "parameter": "fuel_rate", "coefficient": 1.0,
                            "reason": "tyre_age minus run_lap_index is constant within each run"})
    for name in observed_degradation:
        exact_null_rows.append({"null_direction": "fuel_vs_all_observed_degradation",
                                "parameter": name, "coefficient": -1.0,
                                "reason": "tyre_age minus run_lap_index is constant within each run"})
    return (likelihoods, pd.DataFrame(summary_rows), pd.DataFrame(column_rows),
            pd.DataFrame(pair_rows), pd.DataFrame(null_rows), pd.DataFrame(exact_null_rows))


def parameter_summaries(bank, weights, stage, scenario="full"):
    rows = []
    for index, (name, bounds) in enumerate(zip(bank.dimension_names, bank.bounds)):
        if ":R" in name:
            continue
        p10, median, p90 = weighted_quantiles(bank.candidates[:, index], weights)
        rows.append({"scenario": scenario, "stage": stage, "parameter": name,
                     "lower_bound": bounds[0], "upper_bound": bounds[1],
                     "p10": p10, "median": median, "p90": p90, "width": p90-p10})
    for session in SESSIONS:
        rows.append({"scenario": scenario, "stage": stage,
                     "parameter": f"compound_delta:{session}:MEDIUM", "lower_bound": 0,
                     "upper_bound": 0, "p10": 0, "median": 0, "p90": 0, "width": 0})
    return rows


def costs_and_weights(bank, likelihoods, config, include_sessions=SESSIONS, omit_run=None):
    total = np.zeros(len(bank.candidates))
    run_costs = {}
    for session in include_sessions:
        sigma = config.session_noise_sigma.get(session, config.clean_lap_noise_sigma)
        costs = likelihoods[session].sse(bank.candidates) / sigma**2
        per_run = {run: value / sigma**2
                   for run, value in likelihoods[session].run_sse(bank.candidates).items()}
        if omit_run is not None and omit_run in per_run:
            costs = costs - per_run[omit_run]
        total += costs
        run_costs.update(per_run)
    return total, posterior_weights(total), run_costs


def leave_out(bank, likelihoods, config):
    full_cost, full_weights, run_costs = costs_and_weights(bank, likelihoods, config)
    scenarios = [("full", full_cost, full_weights)]
    for session in SESSIONS:
        cost, weights, _ = costs_and_weights(
            bank, likelihoods, config, tuple(value for value in SESSIONS if value != session)
        )
        scenarios.append((f"remove_session:{session}", cost, weights))
    for run in sorted(run_costs):
        cost, weights, _ = costs_and_weights(bank, likelihoods, config, omit_run=run)
        scenarios.append((f"remove_run:{run}", cost, weights))
    full_medians = {row["parameter"]: row["median"] for row in parameter_summaries(
        bank, full_weights, "weighted", "full"
    )}
    rows = []
    for scenario, cost, weights in scenarios:
        ess = 1 / np.sum(weights**2)
        base = parameter_summaries(bank, weights, "weighted", scenario)
        for row in base:
            row.update(ess=ess, ess_fraction=ess/len(weights), max_weight=weights.max(),
                       best_total_cost=cost.min(),
                       median_change_from_full=row["median"]-full_medians[row["parameter"]])
            rows.append(row)
    return pd.DataFrame(rows), full_weights


def seed_count(prepared, base_config):
    rows = []
    for count in (20_000, 50_000, 100_000):
        for seed in (base_config.random_seed, 1, 2_026, 424_242):
            config = replace(base_config, sample_count=count, random_seed=seed)
            bank = generate_joint_sample_bank(EXPECTED_SESSIONS,
                {s: COMPOUNDS for s in EXPECTED_SESSIONS}, config)
            likelihoods = {s: Likelihood(p, s, bank.dimension_names) for s, p in prepared.items()}
            cost, weights, _ = costs_and_weights(bank, likelihoods, config)
            ess = 1 / np.sum(weights**2)
            for row in parameter_summaries(bank, weights, "weighted"):
                row.update(sample_count=count, seed=seed, ess=ess, ess_fraction=ess/count,
                           max_weight=weights.max(), top5_weight=np.sort(weights)[-5:].sum(),
                           best_total_cost=cost.min())
                rows.append(row)
    return pd.DataFrame(rows)


def synthetic(prepared, bank, config):
    truths = {"fuel_rate": .04}
    tracks = {"FP1": -.02, "FP2": .01, "FP3": -.01}
    degradation = {"HARD": .03, "MEDIUM": .07, "SOFT": .12}
    deltas = {"HARD": .50, "MEDIUM": 0.0, "SOFT": -.60}
    rows, weights_by_scenario = [], {}
    for scenario in ("fuel_track_only", "add_degradation", "add_compound_performance", "full"):
        synthetic_prepared = {}
        for session, source in prepared.items():
            frame = source.copy()
            codes, _ = pd.factorize(frame.run_id, sort=True)
            y = 80 + codes * .3 + truths["fuel_rate"] * frame.run_lap_index.to_numpy(float)
            y += tracks[session] * frame.track_index.to_numpy(float)
            if scenario in {"add_degradation", "full"}:
                y += np.array([degradation[value] for value in frame.compound]) * frame.tyre_age
            if scenario in {"add_compound_performance", "full"}:
                y += np.array([deltas[value] for value in frame.compound])
            frame["lap_time_s"] = y
            synthetic_prepared[session] = frame
        likelihoods = {s: Likelihood(p, s, bank.dimension_names)
                       for s, p in synthetic_prepared.items()}
        cost, weights, _ = costs_and_weights(bank, likelihoods, config)
        weights_by_scenario[scenario] = weights
        truth = {"fuel_rate": truths["fuel_rate"]}
        for session in SESSIONS:
            truth[f"track_rate:{session}"] = tracks[session]
            for compound in COMPOUNDS:
                truth[f"degradation:{session}:{compound}"] = (
                    degradation[compound] if scenario in {"add_degradation", "full"} else 0.0
                )
            for compound in ("HARD", "SOFT"):
                truth[f"compound_delta:{session}:{compound}"] = (
                    deltas[compound] if scenario in {"add_compound_performance", "full"} else 0.0
                )
        ess = 1 / np.sum(weights**2)
        for row in parameter_summaries(bank, weights, "weighted", scenario):
            row.update(truth=truth.get(row["parameter"], 0.0),
                       median_error=row["median"]-truth.get(row["parameter"], 0.0),
                       truth_inside_p10_p90=(row["p10"] <= truth.get(row["parameter"], 0.0)
                                             <= row["p90"]),
                       ess=ess, max_weight=weights.max(), best_total_cost=cost.min())
            rows.append(row)
    comparison = pd.DataFrame([
        {"scenario_a": "fuel_track_only", "scenario_b": "add_compound_performance",
         "maximum_weight_difference": np.max(np.abs(
             weights_by_scenario["fuel_track_only"]-weights_by_scenario["add_compound_performance"]
         ))},
        {"scenario_a": "add_degradation", "scenario_b": "full",
         "maximum_weight_difference": np.max(np.abs(
             weights_by_scenario["add_degradation"]-weights_by_scenario["full"]
         ))},
    ])
    return pd.DataFrame(rows), comparison


def main():
    stored, selected, prepared, config = load_inputs()
    bank = generate_joint_sample_bank(EXPECTED_SESSIONS,
        {session: COMPOUNDS for session in EXPECTED_SESSIONS}, config)
    likelihoods, rank, columns, correlations, nulls, exact_nulls = identifiability(
        prepared, bank.dimension_names
    )
    equivalence_rows = []
    for session, likelihood in likelihoods.items():
        accelerated = likelihood.sse(bank.candidates)
        for sample_id in (0, len(bank.candidates)//3, len(bank.candidates)-1):
            values = dict(zip(bank.dimension_names, bank.candidates[sample_id]))
            production = _joint_session_sse(prepared[session], session, values, "MEDIUM")
            equivalence_rows.append({
                "session": session, "sample_id": sample_id,
                "production_sse": production, "diagnostic_sse": accelerated[sample_id],
                "absolute_difference": abs(production-accelerated[sample_id]),
            })
    leaveouts, full_weights = leave_out(bank, likelihoods, config)
    unconstrained = generate_joint_sample_bank(EXPECTED_SESSIONS,
        {session: COMPOUNDS for session in EXPECTED_SESSIONS},
        replace(config, enforce_compound_order=False))
    uniform = np.full(len(bank.candidates), 1/len(bank.candidates))
    stages = (
        parameter_summaries(unconstrained, uniform, "original_unordered_base")
        + parameter_summaries(bank, uniform, "ordered_base")
        + parameter_summaries(bank, full_weights, "final_weighted")
    )
    support = run_support(selected, prepared)
    support_summary = support.groupby(["session", "compound"], as_index=False).agg(
        selected_clean_laps=("selected_clean_laps", "sum"),
        qualifying_long_runs=("run_id", "nunique"),
        minimum_run_length=("run_length", "min"), maximum_run_length=("run_length", "max"),
        minimum_starting_tyre_age=("starting_tyre_age", "min"),
        maximum_starting_tyre_age=("starting_tyre_age", "max"),
        maximum_tyre_age_span=("tyre_age_span", "max"),
    )
    stability = seed_count(prepared, config)
    synthetic_rows, synthetic_comparison = synthetic(prepared, bank, config)
    support.to_csv(ROOT / "fp_r11_run_support.csv", index=False)
    support_summary.to_csv(ROOT / "fp_r11_parameter_support.csv", index=False)
    rank.to_csv(ROOT / "fp_identifiability_summary.csv", index=False)
    columns.to_csv(ROOT / "fp_identifiability_columns.csv", index=False)
    correlations.to_csv(ROOT / "fp_identifiability_correlations.csv", index=False)
    nulls.to_csv(ROOT / "fp_identifiability_null_space.csv", index=False)
    exact_nulls.to_csv(ROOT / "fp_identifiability_exact_null_directions.csv", index=False)
    pd.DataFrame(equivalence_rows).to_csv(
        ROOT / "fp_diagnostic_likelihood_equivalence.csv", index=False
    )
    pd.DataFrame(stages).to_csv(ROOT / "fp_parameter_stage_comparison.csv", index=False)
    leaveouts.to_csv(ROOT / "fp_leave_out_sensitivity.csv", index=False)
    stability.to_csv(ROOT / "fp_seed_sample_stability.csv", index=False)
    synthetic_rows.to_csv(ROOT / "fp_synthetic_recovery.csv", index=False)
    synthetic_comparison.to_csv(ROOT / "fp_synthetic_invariance.csv", index=False)
    classification = pd.DataFrame([
        {"parameter": "fuel_rate", "practice_data_identifiable": "no (absolute value)",
         "main_information_source": "bounds/order plus likelihood on fuel+degradation slope combinations",
         "degeneracy": "exact shared fuel versus all observed session-compound degradation slopes",
         "strong_bounds_order_dependence": True, "evidence_volume": "147 laps / 24 runs"},
        *[{"parameter": f"track_rate:{session}", "practice_data_identifiable": "yes, conditional on active model",
           "main_information_source": "within-session timing-rank variation not collinear with run lap/tyre age",
           "degeneracy": "not in exact joint null space",
           "strong_bounds_order_dependence": False,
           "evidence_volume": f"{len(prepared[session])} laps / {prepared[session].run_id.nunique()} runs"}
          for session in SESSIONS],
        *[{"parameter": f"degradation:{compound}",
           "practice_data_identifiable": "contrasts only; not absolute frozen value",
           "main_information_source": "within-run slopes and cross-compound slope contrasts; then ordering and marginal averaging",
           "degeneracy": "exact global shift against fuel; session-compounds with no laps are wholly unobserved",
           "strong_bounds_order_dependence": True,
           "evidence_volume": f"{int(support.loc[support.compound.eq(compound), 'selected_clean_laps'].sum())} laps / {int(support.loc[support.compound.eq(compound), 'run_id'].nunique())} runs"}
          for compound in COMPOUNDS],
        *[{"parameter": f"compound_performance:{compound}-MEDIUM",
           "practice_data_identifiable": "no",
           "main_information_source": "ordered sampling bounds and finite-bank Monte Carlo geometry",
           "degeneracy": "exact absorption by each single-compound run intercept",
           "strong_bounds_order_dependence": True,
           "evidence_volume": "zero cross-compound likelihood contrasts"}
          for compound in ("HARD", "SOFT")],
    ])
    classification.to_csv(ROOT / "fp_frozen_output_classification.csv", index=False)
    manifest = {
        "production_config": stored, "sample_bank_id": bank.sample_bank_id,
        "sampled_dimension_count": len(bank.dimension_names),
        "fp_active_sampled_dimension_count": 19,
        "run_profiled_intercept_count": int(sum(p.run_id.nunique() for p in prepared.values())),
        "selected_laps": {s: len(p) for s,p in prepared.items()},
        "selected_runs": {s: int(p.run_id.nunique()) for s,p in prepared.items()},
        "ess": float(1/np.sum(full_weights**2)), "max_weight": float(full_weights.max()),
    }
    (ROOT / "fp_information_audit_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
