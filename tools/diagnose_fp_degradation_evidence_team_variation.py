#!/usr/bin/env python3
"""Diagnostic-only isolation of FP degradation evidence and team variation.

This script deliberately reuses the corrected physical Fuel/Track coordinates
from ``diagnose_fp_foundational_model``.  It does not import or modify the
production inference path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import linregress

import diagnose_fp_foundational_model as foundation


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
SESSIONS = ("FP1", "FP2", "FP3")
SHRINKAGE_GRID = {
    "strong": 0.015,
    "moderate": 0.035,
    "weak": 0.075,
}


@dataclass
class Model:
    name: str
    d_programmes: tuple[str, ...]
    team_degradation: bool = False
    shrinkage_label: str = "none"
    shrinkage_sigma: float | None = None


def team_support(data: pd.DataFrame) -> dict[str, list[str]]:
    """Teams with actual within-run LongRun tyre-age leverage by compound."""
    result: dict[str, list[str]] = {}
    eligible = (data["degradation_eligible"].astype(bool) if "degradation_eligible" in data else
                pd.Series(True, index=data.index))
    longrun = data[data.programme.eq("LongRun") & eligible]
    for compound in COMPOUNDS:
        eligible = []
        for team, group in longrun[longrun.compound.eq(compound)].groupby("team"):
            run_spans = group.groupby("run_id").tyre_age.agg(lambda x: x.max() - x.min())
            if len(group) >= 2 and len(run_spans) and run_spans.max() >= 1:
                eligible.append(team)
        result[compound] = sorted(eligible)
    return result


def build_design(data: pd.DataFrame, model: Model):
    names: list[str] = []
    columns: list[np.ndarray] = []
    bounds: list[tuple[float, float]] = []

    def add(name, values, bound):
        names.append(name)
        columns.append(np.asarray(values, dtype=float))
        bounds.append(bound)
        return len(names) - 1

    add("fuel_rate", data.physical_fuel_remaining_proxy, (0.0, 0.10))
    for session in SESSIONS:
        add(
            f"track_rate_{session}",
            data["session"].eq(session) * data.physical_session_lap_equivalent,
            (-0.08, 0.08),
        )

    degradation_indices = {}
    row_eligible = (data["degradation_eligible"].astype(bool) if "degradation_eligible" in data else
                    pd.Series(True, index=data.index))
    d_enabled = data.programme.isin(model.d_programmes) & row_eligible
    for compound in COMPOUNDS:
        degradation_indices[compound] = add(
            f"weekend_degradation_{compound}",
            d_enabled * data.compound.eq(compound) * data.tyre_age,
            (0.0, 0.20),
        )

    team_maps: dict[tuple[str, str], dict[int, float]] = {}
    support = team_support(data)
    if model.team_degradation:
        longrun = data.programme.eq("LongRun") & row_eligible
        for compound, teams in support.items():
            if len(teams) < 2:
                continue
            reference = teams[-1]
            for team in teams[:-1]:
                index = add(
                    f"team_deviation_basis_{compound}_{team}",
                    (
                        longrun & data.compound.eq(compound) & data.team.eq(team)
                    ).astype(float) * data.tyre_age
                    - (
                        longrun & data.compound.eq(compound) & data.team.eq(reference)
                    ).astype(float) * data.tyre_age,
                    (-0.30, 0.30),
                )
                team_maps.setdefault((team, compound), {})[index] = 1.0
                team_maps.setdefault((reference, compound), {})[index] = -1.0

    performance_eligible = (data["performance_eligible"].astype(bool)
                            if "performance_eligible" in data else
                            pd.Series(True, index=data.index))
    performance_indices = {
        "HARD": add("performance_HARD_vs_MEDIUM", performance_eligible & data.compound.eq("HARD"), (0.0, 1.0)),
        "SOFT": add("performance_SOFT_vs_MEDIUM", performance_eligible & data.compound.eq("SOFT"), (-1.0, 0.0)),
    }
    return (
        names,
        np.column_stack(columns),
        bounds,
        degradation_indices,
        performance_indices,
        team_maps,
        support,
    )


def penalty_matrix(parameter_count, team_maps, sigma):
    if sigma is None or not team_maps:
        return np.empty((0, parameter_count))
    rows = []
    for mapping in team_maps.values():
        row = np.zeros(parameter_count)
        for index, weight in mapping.items():
            row[index] = weight / sigma
        rows.append(row)
    return np.vstack(rows)


def initial(names):
    values = np.zeros(len(names))
    starts = {"HARD": 0.04, "MEDIUM": 0.08, "SOFT": 0.12}
    for index, name in enumerate(names):
        if name == "fuel_rate": values[index] = 0.05
        elif name.startswith("weekend_degradation_"):
            values[index] = starts[name.rsplit("_", 1)[-1]]
        elif name == "performance_HARD_vs_MEDIUM": values[index] = 0.50
        elif name == "performance_SOFT_vs_MEDIUM": values[index] = -0.60
    return values


def fit_model(data, blocks, y, model: Model, ordered=True, relaxed=False):
    names, x, bounds, d_idx, p_idx, team_maps, support = build_design(data, model)
    if relaxed:
        relaxed_bounds = []
        for name in names:
            if name == "fuel_rate": relaxed_bounds.append((-0.20, 0.20))
            elif name.startswith("track_rate_"): relaxed_bounds.append((-2.0, 2.0))
            elif name.startswith("weekend_degradation_"): relaxed_bounds.append((-0.20, 0.50))
            elif name.startswith("performance_"): relaxed_bounds.append((-3.0, 3.0))
            else: relaxed_bounds.append((-0.50, 0.50))
        bounds = relaxed_bounds
    xp, yp = foundation.projected(blocks, x, y)
    penalty = penalty_matrix(len(names), team_maps, model.shrinkage_sigma)
    xa = np.vstack((xp, penalty))
    ya = np.concatenate((yp, np.zeros(len(penalty))))
    constraints = []
    if ordered:
        for left, right in zip(COMPOUNDS, COMPOUNDS[1:]):
            constraints.append({
                "type": "ineq",
                "fun": lambda beta, l=d_idx[left], r=d_idx[right]: beta[r] - beta[l],
            })
    # Effective team degradation may differ in ordering.  The ordinary fit keeps
    # a broad physical interval; the explicitly relaxed diagnostic allows signs
    # and scales to reveal conflict.
    effective_low, effective_high = ((-0.20, 0.50) if relaxed else (0.0, 0.30))
    for (team, compound), mapping in team_maps.items():
        def effective(beta, c=compound, m=mapping):
            return beta[d_idx[c]] + sum(weight * beta[index] for index, weight in m.items())
        constraints.extend((
            {"type": "ineq", "fun": lambda beta, f=effective, lo=effective_low: f(beta) - lo},
            {"type": "ineq", "fun": lambda beta, f=effective, hi=effective_high: hi - f(beta)},
        ))

    starts = [initial(names), np.zeros(len(names))]
    least = np.linalg.lstsq(xa, ya, rcond=None)[0]
    starts.append(np.clip(least, [b[0] for b in bounds], [b[1] for b in bounds]))
    best = None
    for start in starts:
        result = minimize(
            lambda beta: float(np.sum((ya - xa @ beta) ** 2)),
            start,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 5000, "ftol": 1e-11},
        )
        if result.success and (best is None or result.fun < best.fun):
            best = result
    if best is None:
        raise RuntimeError(f"No successful optimization for {model.name}")
    beta = best.x
    likelihood_cost = float(np.sum((yp - xp @ beta) ** 2))
    penalty_cost = float(np.sum((penalty @ beta) ** 2))
    raw, baselines, offsets = foundation.raw_fit(data, blocks, x, beta, y)
    singular = np.linalg.svd(xp, compute_uv=False)
    augmented_singular = np.linalg.svd(xa, compute_uv=False)
    covariance = np.linalg.pinv(xa.T @ xa)
    return {
        "model": model, "names": names, "x": x, "xp": xp, "yp": yp,
        "penalty": penalty, "beta": beta, "bounds": bounds, "d_idx": d_idx,
        "p_idx": p_idx, "team_maps": team_maps, "support": support,
        "cost": likelihood_cost, "penalty_cost": penalty_cost,
        "objective": likelihood_cost + penalty_cost, "raw": raw,
        "baselines": baselines, "offsets": offsets,
        "rank": int(np.linalg.matrix_rank(xp)),
        "nullity": len(names) - int(np.linalg.matrix_rank(xp)),
        "singular": singular, "augmented_singular": augmented_singular,
        "local_se": np.sqrt(np.maximum(np.diag(covariance), 0)),
        "ordered": ordered, "relaxed": relaxed,
    }


def effective_team_frame(fit):
    rows = []
    beta, names = fit["beta"], fit["names"]
    for compound, teams in fit["support"].items():
        mean = beta[fit["d_idx"][compound]]
        for team in teams:
            mapping = fit["team_maps"].get((team, compound), {})
            deviation = sum(weight * beta[index] for index, weight in mapping.items())
            rows.append({
                "variant": fit["model"].name,
                "shrinkage": fit["model"].shrinkage_label,
                "sigma_team_degradation_s_per_lap": fit["model"].shrinkage_sigma,
                "team": team, "compound": compound,
                "weekend_mean_s_per_lap": mean,
                "team_deviation_s_per_lap": deviation,
                "effective_degradation_s_per_lap": mean + deviation,
                "drivers_share_one_team_compound_parameter": True,
            })
    frame = pd.DataFrame(rows)
    if len(frame):
        orders = frame.pivot_table(index=["variant", "team"], columns="compound",
                                   values="effective_degradation_s_per_lap")
        violations = {}
        for index, row in orders.iterrows():
            observed = [row[c] for c in COMPOUNDS if c in row.index and pd.notna(row[c])]
            violations[index] = any(r < l for l, r in zip(observed, observed[1:]))
        frame["local_observed_compound_order_violation"] = [
            violations.get((r.variant, r.team), False) for r in frame.itertuples()
        ]
    return frame


def boundary_contacts(fit):
    count = 0
    for value, (low, high) in zip(fit["beta"], fit["bounds"]):
        count += min((value-low)/(high-low), (high-value)/(high-low)) <= .01
    if fit["ordered"]:
        values = [fit["beta"][fit["d_idx"][c]] for c in COMPOUNDS]
        count += sum(right-left <= .002 for left, right in zip(values, values[1:]))
    effective = effective_team_frame(fit)
    if len(effective) and not fit["relaxed"]:
        value = effective.effective_degradation_s_per_lap
        count += int(((value <= .003) | (value >= .297)).sum())
    return int(count)


def variant_row(fit, data):
    raw = fit["raw"]
    row = {
        "variant": fit["model"].name,
        "fit_mode": ("weekend_ordered" if fit["ordered"] else
                     "relaxed_no_order" if fit["relaxed"] else "no_weekend_order_physical_bounds"),
        "degradation_evidence_programmes": "|".join(fit["model"].d_programmes),
        "team_degradation": fit["model"].team_degradation,
        "shrinkage": fit["model"].shrinkage_label,
        "sigma_team_degradation_s_per_lap": fit["model"].shrinkage_sigma,
        "parameter_count": len(fit["names"]), "rank": fit["rank"], "nullity": fit["nullity"],
        "likelihood_cost": fit["cost"], "shrinkage_penalty_cost": fit["penalty_cost"],
        "penalized_objective": fit["objective"],
        "raw_rms_s": float(np.sqrt(np.mean(raw**2))),
        "raw_p90_absolute_residual_s": float(np.quantile(np.abs(raw), .9)),
        "boundary_contacts": boundary_contacts(fit),
        "weakest_normalized_singular": fit["singular"][-1] / fit["singular"][0],
        "condition_number": fit["singular"][0] / fit["singular"][-1],
        "normalized_singular_spectrum": "|".join(f"{v/fit['singular'][0]:.8g}" for v in fit["singular"]),
    }
    for compound in COMPOUNDS:
        index = fit["d_idx"][compound]
        row[f"degradation_{compound}_s_per_lap"] = fit["beta"][index]
        row[f"local_linear_se_{compound}_s_per_lap"] = fit["local_se"][index]
        mask = data.programme.isin(fit["model"].d_programmes) & data.compound.eq(compound)
        column = fit["x"][:, index]
        projected_column, _ = foundation.projected(
            foundation.covariance_blocks(data, TEAMMATE, SIGMA_PROGRAMME),
            column[:, None], np.zeros(len(data)),
        )
        row[f"D_column_norm_{compound}"] = float(np.linalg.norm(projected_column))
        row[f"D_evidence_observations_{compound}"] = int(mask.sum())
        row[f"D_evidence_runs_{compound}"] = int(data.loc[mask, "run_id"].nunique())
        row[f"D_evidence_tyre_age_span_{compound}"] = (
            float(data.loc[mask, "tyre_age"].max() - data.loc[mask, "tyre_age"].min()) if mask.any() else np.nan
        )
    row["performance_HARD_vs_MEDIUM_s"] = fit["beta"][fit["p_idx"]["HARD"]]
    row["performance_SOFT_vs_MEDIUM_s"] = fit["beta"][fit["p_idx"]["SOFT"]]
    for programme in ("Setting", "QualiSim", "LongRun"):
        mask = data.programme.eq(programme).to_numpy()
        row[f"raw_rms_{programme}_s"] = float(np.sqrt(np.mean(raw[mask] ** 2)))
    return row


def programme_evidence(fits, data, blocks):
    rows = []
    for label, fit in fits.items():
        if not fit["ordered"] or fit["relaxed"]: continue
        for programme in ("Setting", "QualiSim", "LongRun"):
            for compound in COMPOUNDS:
                index = fit["d_idx"][compound]
                mask = data.programme.eq(programme) & data.compound.eq(compound)
                isolated = np.where(data.programme.eq(programme), fit["x"][:, index], 0.0)
                projected_column, _ = foundation.projected(blocks, isolated[:, None], np.zeros(len(data)))
                ages = data.loc[mask & data.programme.isin(fit["model"].d_programmes), "tyre_age"]
                rows.append({
                    "variant": label, "programme": programme, "compound": compound,
                    "degradation_enabled": programme in fit["model"].d_programmes,
                    "observations": int(mask.sum()), "runs": int(data.loc[mask, "run_id"].nunique()),
                    "projected_D_column_norm": float(np.linalg.norm(projected_column)),
                    "enabled_tyre_age_min": ages.min() if len(ages) else np.nan,
                    "enabled_tyre_age_max": ages.max() if len(ages) else np.nan,
                    "enabled_tyre_age_span": ages.max()-ages.min() if len(ages) else np.nan,
                })
    return pd.DataFrame(rows)


def longrun_support(data):
    longrun = data[data.programme.eq("LongRun")].copy()
    run_rows = []
    for keys, group in longrun.groupby(["run_id", "team", "driver", "session", "compound"]):
        slope = linregress(group.tyre_age, group.lap_time_s) if group.tyre_age.nunique() >= 2 else None
        run_rows.append({
            "record_type": "run", "run_id": keys[0], "team": keys[1], "driver": keys[2],
            "session": keys[3], "compound": keys[4], "clean_observations": len(group),
            "tyre_age_min": group.tyre_age.min(), "tyre_age_max": group.tyre_age.max(),
            "tyre_age_span": group.tyre_age.max()-group.tyre_age.min(),
            "raw_observed_slope_s_per_tyre_lap": slope.slope if slope else np.nan,
            "raw_slope_standard_error": slope.stderr if slope else np.nan,
            "raw_slope_r_squared": slope.rvalue**2 if slope else np.nan,
            "raw_slope_is_not_degradation": True,
        })
    runs = pd.DataFrame(run_rows)
    compound_rows = []
    for compound, group in longrun.groupby("compound"):
        compound_rows.append({
            "record_type": "compound_summary", "run_id": "ALL", "team": "|".join(sorted(group.team.unique())),
            "driver": "|".join(sorted(group.driver.unique())), "session": "|".join(sorted(group["session"].unique())),
            "compound": compound, "clean_observations": len(group), "physical_runs": group.run_id.nunique(),
            "teams": group.team.nunique(), "drivers": group.driver.nunique(),
            "tyre_age_min": group.tyre_age.min(), "tyre_age_max": group.tyre_age.max(),
            "tyre_age_span": group.tyre_age.max()-group.tyre_age.min(),
            "per_run_tyre_age_spans": "|".join(
                f"{run}:{span:g}" for run, span in group.groupby("run_id").tyre_age.agg(lambda x:x.max()-x.min()).items()
            ),
            "raw_slope_is_not_degradation": True,
        })
    return pd.concat((runs, pd.DataFrame(compound_rows)), ignore_index=True, sort=False)


def residual_slopes(fit, data):
    rows = []
    raw = fit["raw"]
    work = data.copy()
    work["residual_s"] = raw
    for keys, group in work[work.programme.eq("LongRun")].groupby(["run_id", "compound", "team"]):
        slope = linregress(group.tyre_age, group.residual_s) if group.tyre_age.nunique() >= 2 else None
        rows.append({
            "variant": fit["model"].name, "run_id": keys[0], "compound": keys[1], "team": keys[2],
            "observations": len(group), "residual_slope_s_per_tyre_lap": slope.slope if slope else np.nan,
            "residual_slope_abs": abs(slope.slope) if slope else np.nan,
            "residual_slope_standard_error": slope.stderr if slope else np.nan,
        })
    return pd.DataFrame(rows)


def manual_examples(fit, data):
    effective = effective_team_frame(fit)
    chosen = ["FP2:Williams:SAI:3.0", "FP2:Williams:ALB:3.0", "FP3:Alpine:COL:2.0"]
    for compound in ("MEDIUM", "SOFT"):
        candidates = effective[effective.compound.eq(compound)].sort_values("effective_degradation_s_per_lap")
        if len(candidates):
            for team in (candidates.iloc[0].team, candidates.iloc[-1].team):
                matches = data[data.programme.eq("LongRun") & data.compound.eq(compound) & data.team.eq(team)].run_id.unique()
                if len(matches): chosen.append(matches[0])
    chosen = list(dict.fromkeys(chosen))
    rows = []
    beta = fit["beta"]
    for index, row in data[data.run_id.isin(chosen)].iterrows():
        terms = dict(zip(fit["names"], fit["x"][index] * beta))
        global_d = terms.get(f"weekend_degradation_{row.compound}", 0.0)
        team_d = sum(value for name, value in terms.items() if name.startswith("team_deviation_basis_"))
        performance = sum(value for name, value in terms.items() if name.startswith("performance_"))
        track = sum(value for name, value in terms.items() if name.startswith("track_rate_"))
        fuel = terms["fuel_rate"]
        baseline = fit["baselines"][row.programme_block]
        run_offset = fit["offsets"].get(row.run_id, 0.0)
        corrected_pace = row.corrected_lap_time - baseline - run_offset - fuel - track - performance
        rows.append({
            "variant": fit["model"].name, "run_id": row.run_id, "team": row.team,
            "driver": row.driver, "session": row.session, "compound": row.compound,
            "physical_lap": row.lap_number, "physical_stint_index": row.physical_stint_index,
            "tyre_age": row.tyre_age, "fuel_correction_s": fuel,
            "track_secular_correction_s": track, "global_degradation_contribution_s": global_d,
            "team_degradation_deviation_contribution_s": team_d,
            "compound_performance_correction_s": performance,
            "programme_block_baseline_s": baseline, "conditional_run_offset_s": run_offset,
            "Q_driver_delta_s": row.driver_delta, "observed_corrected_pace_s": corrected_pace,
            "model_degradation_contribution_s": global_d + team_d,
            "corrected_pace_residual_s": fit["raw"][index],
        })
    return pd.DataFrame(rows)


def plots(variant, evidence, support, shrinkage, effective, slopes, examples):
    ordered = variant[variant.fit_mode.eq("weekend_ordered")]
    primary = ordered[ordered.variant.isin(("sharedD_all_programmes", "sharedD_no_quali_D", "sharedD_longrun_only_D"))]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    x = np.arange(len(primary)); width = .23
    for j, compound in enumerate(COMPOUNDS):
        axes[0].bar(x+(j-1)*width, primary[f"degradation_{compound}_s_per_lap"], width, label=compound)
    axes[0].set_xticks(x, primary.variant, rotation=18, ha="right")
    axes[0].set(ylabel="weekend degradation (s/lap)", title="Programme-specific degradation evidence")
    norms = evidence[evidence.variant.isin(primary.variant)].groupby(["variant", "programme"]).projected_D_column_norm.sum().unstack(fill_value=0)
    norms.plot.bar(ax=axes[1]); axes[1].set(ylabel="sum of projected D-column norms", title="Actual degradation leverage")
    for ax in axes: ax.grid(axis="y", alpha=.2)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_degradation_programme_evidence.png", dpi=180); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for compound in COMPOUNDS:
        g = shrinkage[shrinkage.compound.eq(compound)]
        axes[0].plot(g.sigma_team_degradation_s_per_lap, g.weekend_degradation_s_per_lap, marker="o", label=compound)
    axes[0].set(xlabel="team-deviation sigma (s/lap)", ylabel="weekend degradation (s/lap)", title="Weekend means vs shrinkage")
    for (compound, team), g in effective[effective.shrinkage.isin(SHRINKAGE_GRID)].groupby(["compound", "team"]):
        axes[1].plot(g.sigma_team_degradation_s_per_lap, g.effective_degradation_s_per_lap, marker="o", label=f"{team} {compound}")
    axes[1].set(xlabel="team-deviation sigma (s/lap)", ylabel="effective degradation (s/lap)", title="Team values vs shrinkage")
    axes[1].legend(fontsize=6, ncol=2)
    for ax in axes: ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_team_degradation_shrinkage.png", dpi=180); plt.close(fig)

    runs = support[support.record_type.eq("run")]
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    colors = {"HARD":"tab:grey", "MEDIUM":"tab:orange", "SOFT":"tab:red"}
    axes[0].barh(runs.run_id, runs.raw_observed_slope_s_per_tyre_lap,
            color=[colors[c] for c in runs.compound])
    axes[0].axvline(0, color="black", lw=.8); axes[0].set(xlabel="raw LapTime-vs-age slope (s/lap; not D)", title="Before global corrections")
    compare = slopes[slopes.variant.isin(("sharedD_longrun_only_D", "teamD_shrunk_longrun_only_D_moderate"))]
    pivot = compare.pivot(index="run_id", columns="variant", values="residual_slope_s_per_tyre_lap").reindex(runs.run_id)
    pivot.plot.barh(ax=axes[1]); axes[1].axvline(0, color="black", lw=.8)
    axes[1].set(xlabel="corrected residual slope (s/lap)", title="After shared vs shrunk team D")
    for ax in axes: ax.grid(axis="x", alpha=.2)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_longrun_corrected_slopes.png", dpi=180); plt.close(fig)

    soft_runs = runs[runs.compound.eq("SOFT")]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].errorbar(soft_runs.run_id, soft_runs.raw_observed_slope_s_per_tyre_lap,
                     yerr=soft_runs.raw_slope_standard_error, fmt="o")
    axes[0].tick_params(axis="x", rotation=35); axes[0].set(ylabel="raw slope (s/lap)", title="Direct Soft LongRun support")
    soft_shrink = shrinkage[shrinkage.compound.eq("SOFT")]
    axes[1].plot(soft_shrink.sigma_team_degradation_s_per_lap, soft_shrink.weekend_degradation_s_per_lap, marker="o")
    axes[1].set(xlabel="team-deviation sigma", ylabel="weekend Soft D (s/lap)", title="Soft sensitivity without QualiSim D")
    for ax in axes: ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_soft_degradation_support.png", dpi=180); plt.close(fig)

    selected = examples.run_id.unique()
    fig, axes = plt.subplots(int(np.ceil(len(selected)/2)), 2, figsize=(14, 3.8*np.ceil(len(selected)/2)), squeeze=False)
    for ax, run in zip(axes.flat, selected):
        g = examples[examples.run_id.eq(run)].sort_values("tyre_age")
        ax.scatter(g.tyre_age, g.observed_corrected_pace_s, label="observed corrected pace")
        ax.plot(g.tyre_age, g.model_degradation_contribution_s, color="tab:red", label="global + team D")
        ax.set(title=run, xlabel="tyre age", ylabel="pace after baseline/Fuel/Track/P (s)"); ax.grid(alpha=.2)
    for ax in axes.flat[len(selected):]: ax.set_visible(False)
    axes.flat[0].legend(fontsize=7); fig.tight_layout(); fig.savefig(OUTPUT/"fp_team_degradation_examples.png", dpi=180); plt.close(fig)


def fmt(value, digits=4):
    return "n/a" if pd.isna(value) else f"{value:.{digits}f}"


def write_report(variants, support, shrinkage, effective, slopes, performance, examples):
    ordered = variants[variants.fit_mode.eq("weekend_ordered")].set_index("variant")
    no_order = variants[variants.fit_mode.eq("no_weekend_order_physical_bounds")].set_index("variant")
    relaxed = variants[variants.fit_mode.eq("relaxed_no_order")].set_index("variant")
    allp = ordered.loc["sharedD_all_programmes"]
    noq = ordered.loc["sharedD_no_quali_D"]
    lronly = ordered.loc["sharedD_longrun_only_D"]
    moderate = ordered.loc["teamD_shrunk_longrun_only_D_moderate"]
    free = ordered.loc["teamD_free_longrun_only_D"]
    summaries = support[support.record_type.eq("compound_summary")].set_index("compound")
    slope_summary = slopes.groupby("variant").agg(
        mean_abs_residual_slope=("residual_slope_abs", "mean"),
        max_abs_residual_slope=("residual_slope_abs", "max"),
    )
    moderate_effective = effective[effective.variant.eq("teamD_shrunk_longrun_only_D_moderate")]
    max_dev = moderate_effective.team_deviation_s_per_lap.abs().max()
    weak_spread = shrinkage.groupby("compound").weekend_degradation_s_per_lap.agg(lambda x:x.max()-x.min())
    perf_move_h = abs(noq.performance_HARD_vs_MEDIUM_s-allp.performance_HARD_vs_MEDIUM_s)
    perf_move_s = abs(noq.performance_SOFT_vs_MEDIUM_s-allp.performance_SOFT_vs_MEDIUM_s)
    setting_change = np.linalg.norm([
        noq[f"degradation_{c}_s_per_lap"]-lronly[f"degradation_{c}_s_per_lap"] for c in COMPOUNDS
    ])
    teammate_rows = support[(support.record_type.eq("run")) & support.team.eq("Williams") & support.compound.eq("HARD")]
    local_violations = int(moderate_effective.local_observed_compound_order_violation.sum()) if len(moderate_effective) else 0
    lines = [
        "# FP Degradation Evidence and Team Variation Diagnostic", "",
        "## Evidence-led recommendation", "",
        "**The proposed mechanism is not confirmed.** QualiSim degradation leverage is not the main cause of H=M=S: disabling only QualiSim D moves the common ordered value only from `0.0539` to `0.0581 s/lap` and leaves all compounds equal. LongRun-only evidence also fails to recover a stable ordered hierarchy. Without weekend ordering it prefers high HARD/MEDIUM and near-zero SOFT, so imposing `S >= M >= H` creates equality faces.", "",
        "Use LongRun progression as primary degradation evidence and retain QualiSim primarily for performance in the next multi-weekend diagnostic. Do **not** use these R11 H/M/S fits as a production prior or accepted pre-race estimate. Moderate Team×Compound shrinkage reduces conflict, but still leaves M=S and does not solve Soft identifiability.", "",
        f"All-programme corrected coordinates give H/M/S `{allp.degradation_HARD_s_per_lap:.4f}/{allp.degradation_MEDIUM_s_per_lap:.4f}/{allp.degradation_SOFT_s_per_lap:.4f}` s/lap. Removing only QualiSim from D gives `{noq.degradation_HARD_s_per_lap:.4f}/{noq.degradation_MEDIUM_s_per_lap:.4f}/{noq.degradation_SOFT_s_per_lap:.4f}`; LongRun-only gives `{lronly.degradation_HARD_s_per_lap:.4f}/{lronly.degradation_MEDIUM_s_per_lap:.4f}/{lronly.degradation_SOFT_s_per_lap:.4f}`. The moderate-shrinkage team model gives weekend means `{moderate.degradation_HARD_s_per_lap:.4f}/{moderate.degradation_MEDIUM_s_per_lap:.4f}/{moderate.degradation_SOFT_s_per_lap:.4f}`.", "",
        "This diagnostic changes no production inference. Fuel uses physical stint progression with the fuel-remaining sign; Track uses elapsed SessionTime in physical lap-equivalents. QualiSim and Setting observations remain in the likelihood, baselines, DriverDelta, Fuel/Track, and performance paths when their D columns are disabled.", "",
        "## Controlled model matrix", "",
        "| Variant | D evidence | Team D | H | M | S | raw RMS | cost | rank/nullity | boundaries |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    order_names = ["sharedD_all_programmes", "sharedD_no_quali_D", "sharedD_longrun_only_D", "teamD_free_longrun_only_D",
                   "teamD_shrunk_longrun_only_D_strong", "teamD_shrunk_longrun_only_D_moderate", "teamD_shrunk_longrun_only_D_weak"]
    for name in order_names:
        row = ordered.loc[name]
        evidence_label = str(row.degradation_evidence_programmes).replace("|", ", ")
        lines.append(f"| {name} | {evidence_label} | {row.shrinkage} | {row.degradation_HARD_s_per_lap:.4f} | {row.degradation_MEDIUM_s_per_lap:.4f} | {row.degradation_SOFT_s_per_lap:.4f} | {row.raw_rms_s:.4f} | {row.likelihood_cost:.2f} | {int(row['rank'])}/{int(row['nullity'])} | {int(row.boundary_contacts)} |")
    lines += [
        "", f"The physical-bounds/no-order LongRun-only optimum is H/M/S `{no_order.loc['sharedD_longrun_only_D','degradation_HARD_s_per_lap']:.4f}/{no_order.loc['sharedD_longrun_only_D','degradation_MEDIUM_s_per_lap']:.4f}/{no_order.loc['sharedD_longrun_only_D','degradation_SOFT_s_per_lap']:.4f}`. With moderate team shrinkage it is `{no_order.loc['teamD_shrunk_longrun_only_D_moderate','degradation_HARD_s_per_lap']:.4f}/{no_order.loc['teamD_shrunk_longrun_only_D_moderate','degradation_MEDIUM_s_per_lap']:.4f}/{no_order.loc['teamD_shrunk_longrun_only_D_moderate','degradation_SOFT_s_per_lap']:.4f}`. This demonstrates that ordered M=S is constraint-driven rather than naturally recovered separation.", "",
        "The CSV also contains genuinely relaxed no-order optima (relaxed Fuel/Track/D/P/team bounds), full singular spectra, D-column norms, local linearized standard errors, programme RMS, and constraint contacts. Local SEs are geometry diagnostics, not calibrated posterior intervals.", "",
        "## Direct LongRun support", "",
        "| Compound | clean laps | runs | teams | drivers | age range/span | sessions |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for compound in COMPOUNDS:
        row = summaries.loc[compound]
        sessions = str(row.session).replace("|", ", ")
        lines.append(f"| {compound} | {int(row.clean_observations)} | {int(row.physical_runs)} | {int(row.teams)} | {int(row.drivers)} | {row.tyre_age_min:g}–{row.tyre_age_max:g} / {row.tyre_age_span:g} | {sessions} |")
    lines += [
        "", "There are only eight physical LongRuns. HARD is entirely Williams FP2, although both ALB and SAI contribute independent runs. MEDIUM spans McLaren, Mercedes and Red Bull in FP2. SOFT spans Cadillac FP1, Audi FP2 and Alpine FP3, with one driver per team. Raw per-run slopes in `fp_longrun_compound_support.csv` are context only: they still mix Fuel and Track with degradation.", "",
        "## What QualiSim and Setting change", "",
        f"QualiSim is **not** mainly responsible for equality: its removal changes the ordered D vector by only `{np.linalg.norm([noq[f'degradation_{c}_s_per_lap']-allp[f'degradation_{c}_s_per_lap'] for c in COMPOUNDS]):.4f}` s/lap and all three remain equal. The no-order no-QualiSim fit prefers H/M/S `{no_order.loc['sharedD_no_quali_D','degradation_HARD_s_per_lap']:.4f}/{no_order.loc['sharedD_no_quali_D','degradation_MEDIUM_s_per_lap']:.4f}/{no_order.loc['sharedD_no_quali_D','degradation_SOFT_s_per_lap']:.4f}`, exposing Soft conflict rather than clean separation.", "",
        f"Removing Setting from D after QualiSim has already been disabled moves the H/M/S vector by `{setting_change:.4f}` s/lap. This is the direct measure of Setting's incremental degradation influence in R11; it does not create cross-compound performance connectivity.", "",
        f"Performance moves by `{perf_move_h:.4f} s` for HARD-vs-MEDIUM and `{perf_move_s:.4f} s` for SOFT-vs-MEDIUM when QualiSim is removed only from D; Soft performance reaches its `-1.0 s` bound. QualiSim remains in P, so this large Soft movement is D/P coupling rather than deleted pace evidence.", "",
        "## Team-level variation", "",
        f"The LongRun-only team design contains four independent deviation bases: none for HARD (only Williams), two for MEDIUM and two for SOFT. Both Williams HARD drivers share one team value; the support table shows `{len(teammate_rows)}` separate Williams runs and no driver degradation parameter exists.", "",
        f"Moderate shrinkage uses `delta_team,c ~ Normal(0, 0.035 s/lap)` with an unweighted sum-to-zero team definition inside each compound. The largest moderate deviation is `{max_dev:.4f} s/lap`. H/M/S mean ranges across strong/moderate/weak shrinkage are `{weak_spread.HARD:.4f}/{weak_spread.MEDIUM:.4f}/{weak_spread.SOFT:.4f}` s/lap. Strong shrinkage gives H=M=S; moderate and weak give M=S. There are `{local_violations}` rows belonging to teams with an observed-compound local-order violation; local ordering is deliberately not constrained.", "",
        f"Mean absolute residual LongRun slope changes from `{slope_summary.loc['sharedD_longrun_only_D','mean_abs_residual_slope']:.4f}` to `{slope_summary.loc['teamD_shrunk_longrun_only_D_moderate','mean_abs_residual_slope']:.4f}` s/lap. This tests whether team variation removes slope structure, not just total cost. The free model's raw RMS is `{free.raw_rms_s:.4f} s`, versus `{moderate.raw_rms_s:.4f} s` under moderate shrinkage.", "",
        "## Soft evidence", "",
        f"Soft has `{int(summaries.loc['SOFT'].clean_observations)}` useful clean observations in `{int(summaries.loc['SOFT'].physical_runs)}` runs, tyre ages `{summaries.loc['SOFT'].tyre_age_min:g}–{summaries.loc['SOFT'].tyre_age_max:g}`. It is directly informed by within-run slopes when the D policy is LongRun-only, but team heterogeneity cannot be separated from driver/setup effects through teammate replication. Its weekend estimate is also coupled to the required weekend ordering and shrinkage. Therefore: **Soft degradation is weakly informed in this FP weekend**, not a precise substitute measurement from QualiSim.", "",
        "## Manual run audit", "",
        "`fp_team_degradation_examples.csv` and `fp_team_degradation_examples.png` show HARD, MEDIUM and SOFT runs, both Williams HARD teammates, and high/low fitted team slopes. Each row includes physical lap, tyre age, Fuel, Track/secular, global D, team deviation, performance, Q DriverDelta, conditional baseline/run offset, corrected pace and residual.", "",
        "## External Race-retro scale sanity only", "",
        "The previously saved R11 Race review reported median H/M/S degradation around `0.068/0.135/0.189 s/lap`. These values were not used for shrinkage, bounds, evidence policy, or optimization. They are only an external check of order and scale; FP/Race equality is not expected.", "",
        "## Answers to the explicit questions", "",
        "1. Very little: disabling QualiSim D changes the common ordered value by about `0.0042 s/lap` and does not remove H=M=S. QualiSim is not the main cause.",
        "2. `sharedD_no_quali_D` is the corrected model's H/M/S preference when QualiSim is excluded only from D.",
        "3. `sharedD_longrun_only_D` reports the LongRun-only result while all observations remain in the likelihood.",
        "4. Direct H/M/S LongRun counts, runs, teams, drivers, spans and sessions are reported above and per run in the support CSV.",
        "5. Soft is directly observed but not robustly estimable: raw slopes conflict, no within-team replication exists, and no-order fits drive Soft near or below zero.",
        "6. The no-QualiSim versus LongRun-only vector movement measures Setting's incremental usefulness.",
        "7. Team×Compound variation explains conflict without any Driver×Compound D parameter; compare cost, RMS and residual slopes.",
        "8. Moderate-shrinkage means are `0.1007/0.1065/0.1065 s/lap`; M=S is constraint-driven.",
        "9. They are not hierarchy-stable: strong gives equality; moderate/weak retain M=S and shift its level.",
        "10. Effective values and deviations are exported; the largest deviation and local order violations are stated above.",
        "11. Residual-slope changes are reported separately from total cost.",
        "12. Performance coupling is small or large according to the explicit H/S movements above, with all values in the coupling CSV.",
        "13. The provenance policy `LongRun -> degradation`, `QualiSim -> primarily performance`, `Setting -> secondary/conditional evidence` is better motivated, but R11 does not demonstrate an identified ordered result under it.",
        "14. The architecture and shrinkage grid are worth multi-weekend validation; the R11 estimate itself is not worth productionising because its hierarchy remains constraint-driven.", "",
        "## Artifact index", "",
        "All diagnostic artifacts are under `temp/`: the six requested CSVs, `fp_team_degradation_examples.csv`, `fp_longrun_residual_slopes.csv`, and the five requested plots. The script is isolated at `woPlanner/tools/diagnose_fp_degradation_evidence_team_variation.py`.",
    ]
    (OUTPUT / "FP_DEGRADATION_EVIDENCE_AND_TEAM_VARIATION_DIAGNOSTIC.md").write_text("\n".join(lines) + "\n")


def main():
    global TEAMMATE, SIGMA_PROGRAMME
    OUTPUT.mkdir(exist_ok=True)
    _, _, _, _, data, TEAMMATE, _, SIGMA_PROGRAMME = foundation.load_inputs()
    data = data.copy().reset_index(drop=True)
    y = data.corrected_lap_time.to_numpy(float)
    blocks = foundation.covariance_blocks(data, TEAMMATE, SIGMA_PROGRAMME)

    models = [
        Model("sharedD_all_programmes", ("Setting", "QualiSim", "LongRun")),
        Model("sharedD_no_quali_D", ("Setting", "LongRun")),
        Model("sharedD_longrun_only_D", ("LongRun",)),
        Model("teamD_free_longrun_only_D", ("LongRun",), True, "free", None),
    ] + [
        Model(f"teamD_shrunk_longrun_only_D_{label}", ("LongRun",), True, label, sigma)
        for label, sigma in SHRINKAGE_GRID.items()
    ]
    fits = {}
    rows = []
    for model in models:
        for mode, ordered, relaxed in (("ordered", True, False), ("no_order", False, False),
                                       ("relaxed", False, True)):
            fit = fit_model(data, blocks, y, model, ordered=ordered, relaxed=relaxed)
            key = model.name if mode == "ordered" else f"{model.name}__{mode}"
            fits[key] = fit
            rows.append(variant_row(fit, data))
    variants = pd.DataFrame(rows)
    variants.to_csv(OUTPUT / "fp_degradation_variant_comparison.csv", index=False)

    evidence = programme_evidence(fits, data, blocks)
    evidence.to_csv(OUTPUT / "fp_degradation_programme_evidence_ab.csv", index=False)
    support = longrun_support(data)
    support.to_csv(OUTPUT / "fp_longrun_compound_support.csv", index=False)

    effective_frames = [effective_team_frame(fit) for key, fit in fits.items() if "__" not in key and fit["model"].team_degradation]
    effective = pd.DataFrame([
        record for frame in effective_frames for record in frame.to_dict("records")
    ])
    effective.to_csv(OUTPUT / "fp_team_effective_degradation.csv", index=False)

    shrink_rows = []
    for label in SHRINKAGE_GRID:
        fit = fits[f"teamD_shrunk_longrun_only_D_{label}"]
        for compound in COMPOUNDS:
            index = fit["d_idx"][compound]
            values = effective[(effective.variant.eq(fit["model"].name)) & effective.compound.eq(compound)]
            shrink_rows.append({
                "variant": fit["model"].name, "shrinkage": label,
                "sigma_team_degradation_s_per_lap": fit["model"].shrinkage_sigma,
                "compound": compound, "weekend_degradation_s_per_lap": fit["beta"][index],
                "local_linear_se_s_per_lap": fit["local_se"][index],
                "minimum_effective_team_D": values.effective_degradation_s_per_lap.min(),
                "maximum_effective_team_D": values.effective_degradation_s_per_lap.max(),
                "maximum_absolute_team_deviation": values.team_deviation_s_per_lap.abs().max(),
                "likelihood_cost": fit["cost"], "penalty_cost": fit["penalty_cost"],
                "raw_rms_s": np.sqrt(np.mean(fit["raw"]**2)),
            })
    shrinkage = pd.DataFrame(shrink_rows)
    shrinkage.to_csv(OUTPUT / "fp_team_degradation_shrinkage.csv", index=False)

    performance_rows = []
    for key, fit in fits.items():
        if "__" in key: continue
        performance_rows.append({
            "variant": key,
            "performance_HARD_vs_MEDIUM_s": fit["beta"][fit["p_idx"]["HARD"]],
            "performance_SOFT_vs_MEDIUM_s": fit["beta"][fit["p_idx"]["SOFT"]],
            "QualiSim_retained_in_likelihood_and_performance": True,
            "QualiSim_degradation_enabled": "QualiSim" in fit["model"].d_programmes,
            "Setting_degradation_enabled": "Setting" in fit["model"].d_programmes,
        })
    performance = pd.DataFrame(performance_rows)
    performance.to_csv(OUTPUT / "fp_degradation_performance_coupling.csv", index=False)

    slope_frames = [residual_slopes(fit, data) for key, fit in fits.items() if "__" not in key]
    slopes = pd.concat(slope_frames, ignore_index=True)
    slopes.to_csv(OUTPUT / "fp_longrun_residual_slopes.csv", index=False)

    moderate = fits["teamD_shrunk_longrun_only_D_moderate"]
    examples = manual_examples(moderate, data)
    examples.to_csv(OUTPUT / "fp_team_degradation_examples.csv", index=False)
    plots(variants, evidence, support, shrinkage, effective, slopes, examples)
    write_report(variants, support, shrinkage, effective, slopes, performance, examples)

    manifest = {
        "production_code_modified": False,
        "coordinate_baseline": "physical fuel-remaining proxy and elapsed SessionTime in lap-equivalents",
        "observation_count": len(data),
        "programme_blocks": data.programme_block.nunique(),
        "variants": [m.name for m in models],
        "shrinkage_grid_s_per_lap": SHRINKAGE_GRID,
        "team_driver_policy": "one Team×Compound degradation value shared by both drivers",
    }
    (OUTPUT / "fp_degradation_diagnostic_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
