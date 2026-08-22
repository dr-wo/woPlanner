#!/usr/bin/env python3
"""Build the deterministic R11 FP engineering degradation evidence package.

Diagnostic/prototype only.  No production model or Strategy Prediction input is
changed by this script.
"""

from __future__ import annotations

from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import linregress

import diagnose_fp_foundational_model as foundation
import diagnose_fp_degradation_evidence_team_variation as degradation


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
SOFT_FALLBACK_RATIO_EXAMPLE = 1.20
SOFT_FALLBACK_RATIO_SENSITIVITY = (1.10, 1.20, 1.35)
MODERATE_TEAM_SIGMA = 0.035


def fit(data, teammate, sigma_programme, *, team=False, sigma=None,
        ordered=False, relaxed=False, name="engineering_fit"):
    blocks = foundation.covariance_blocks(data, teammate, sigma_programme)
    model = degradation.Model(name, ("LongRun",), team, "moderate" if sigma else "none", sigma)
    return degradation.fit_model(
        data, blocks, data.corrected_lap_time.to_numpy(float), model,
        ordered=ordered, relaxed=relaxed,
    )


def parameter(fitted, compound):
    return float(fitted["beta"][fitted["d_idx"][compound]])


def performance(fitted):
    return {
        "HARD_vs_MEDIUM_s": float(fitted["beta"][fitted["p_idx"]["HARD"]]),
        "SOFT_vs_MEDIUM_s": float(fitted["beta"][fitted["p_idx"]["SOFT"]]),
    }


def json_clean(value):
    if isinstance(value, dict): return {str(k): json_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_clean(v) for v in value]
    if isinstance(value, (np.integer,)): return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)): return bool(value)
    return value


def raw_run_support(data):
    rows = []
    for keys, group in data[data.programme.eq("LongRun")].groupby(
        ["run_id", "team", "driver", "session", "compound"]
    ):
        raw = linregress(group.tyre_age, group.lap_time_s)
        corrected = linregress(group.tyre_age, group.corrected_lap_time)
        rows.append({
            "run_id": keys[0], "team": keys[1], "driver": keys[2],
            "session": keys[3], "compound": keys[4], "clean_laps": len(group),
            "tyre_age_min": float(group.tyre_age.min()),
            "tyre_age_max": float(group.tyre_age.max()),
            "tyre_age_span": float(group.tyre_age.max()-group.tyre_age.min()),
            "raw_lap_time_slope_s_per_lap": float(raw.slope),
            "raw_slope_standard_error": float(raw.stderr),
            "q_corrected_lap_time_slope_s_per_lap": float(corrected.slope),
            "q_correction_changes_within_run_slope": bool(abs(raw.slope-corrected.slope) > 1e-12),
        })
    return pd.DataFrame(rows)


def q_driver_audit(data, teammate, sigma_programme, with_q, without_q):
    hard = data[data.programme.eq("LongRun") & data.team.eq("Williams") & data.compound.eq("HARD")]
    q = teammate[(teammate.team.eq("Williams")) & teammate["session"].eq("FP2")]
    qrow = q.iloc[0] if len(q) else None
    raw_means = hard.groupby(["driver", "run_id"]).lap_time_s.mean()
    corrected_means = hard.groupby(["driver", "run_id"]).corrected_lap_time.mean()
    raw_driver = hard.groupby("driver").lap_time_s.mean()
    corrected_driver = hard.groupby("driver").corrected_lap_time.mean()
    raw_difference = float(raw_driver.get("SAI", np.nan)-raw_driver.get("ALB", np.nan))
    corrected_difference = float(corrected_driver.get("SAI", np.nan)-corrected_driver.get("ALB", np.nan))
    rows = []
    for (driver, run_id), raw_baseline in raw_means.items():
        group = hard[(hard.driver.eq(driver)) & hard.run_id.eq(run_id)]
        rows.append({
            "record_type": "driver_run_baseline", "team": "Williams", "compound": "HARD",
            "driver": driver, "run_id": run_id, "clean_laps": len(group),
            "raw_longrun_mean_lap_time_s": raw_baseline,
            "analytical_q_driver_delta_s": float(group.driver_delta.iloc[0]),
            "q_corrected_mean_lap_time_s": corrected_means.loc[(driver, run_id)],
            "raw_SAI_minus_ALB_baseline_s": raw_difference,
            "q_corrected_SAI_minus_ALB_baseline_s": corrected_difference,
            "q_delta_source": qrow.source if qrow is not None else "unavailable",
            "q_delta_uncertainty_sigma_s": qrow.uncertainty_sigma_s if qrow is not None else np.nan,
            "q_driver_delta_already_applied": bool(group.driver_delta.abs().max() > 0 or driver == "ALB"),
            "shared_HARD_D_without_Q_s_per_lap": parameter(without_q, "HARD"),
            "shared_HARD_D_with_Q_s_per_lap": parameter(with_q, "HARD"),
            "HARD_D_change_due_to_Q_s_per_lap": parameter(with_q, "HARD")-parameter(without_q, "HARD"),
            "interpretation": "Q delta aligns a rough constant driver baseline; it does not alter within-run slope",
        })
    return pd.DataFrame(rows)


def effective_values(fitted):
    return degradation.effective_team_frame(fitted)


def leave_one_out(data, teammate, sigma_programme):
    rows = []
    policies = {
        "HARD": [("ALB_only", "driver", "SAI"), ("SAI_only", "driver", "ALB"), ("both_drivers", None, None)],
        "MEDIUM": [("leave_McLaren_out", "team", "McLaren"),
                   ("leave_Mercedes_out", "team", "Mercedes"),
                   ("leave_Red_Bull_out", "team", "Red Bull Racing"),
                   ("all_teams", None, None)],
        "SOFT": [("leave_Cadillac_out", "team", "Cadillac"),
                 ("leave_Audi_out", "team", "Audi"),
                 ("leave_Alpine_out", "team", "Alpine"),
                 ("all_runs", None, None)],
    }
    for compound, variants in policies.items():
        for label, dimension, excluded in variants:
            subset = data.copy()
            if dimension is not None:
                mask = (subset.programme.eq("LongRun") & subset.compound.eq(compound)
                        & subset[dimension].eq(excluded))
                # Conventional leave-one-out: remove the held-out LongRun from
                # every fitted path.  This is intentionally different from the
                # programme evidence A/B, which retained QualiSim/Setting for P.
                subset = subset.loc[~mask].reset_index(drop=True)
            use_team = compound in {"MEDIUM", "SOFT"}
            fitted = fit(subset, teammate, sigma_programme, team=use_team,
                         sigma=MODERATE_TEAM_SIGMA if use_team else None,
                         ordered=False, name=f"loo_{compound}_{label}")
            evidence = subset[
                subset.programme.eq("LongRun") & subset.compound.eq(compound)
            ]
            rows.append({
                "compound": compound, "robustness_variant": label,
                "excluded_dimension": dimension, "excluded_value": excluded,
                "estimate_s_per_lap": parameter(fitted, compound),
                "clean_laps_remaining": len(evidence), "runs_remaining": evidence.run_id.nunique(),
                "teams_remaining": evidence.team.nunique(), "drivers_remaining": evidence.driver.nunique(),
                "nuisance_used": use_team, "sigma_nuisance_s_per_lap": MODERATE_TEAM_SIGMA if use_team else np.nan,
                "fit_boundary_zero": parameter(fitted, compound) <= 1e-5,
            })
    frame = pd.DataFrame(rows)
    frame["all_data_estimate_s_per_lap"] = frame.groupby("compound").estimate_s_per_lap.transform(
        lambda values: values.iloc[-1]
    )
    frame["change_from_all_data_s_per_lap"] = frame.estimate_s_per_lap-frame.all_data_estimate_s_per_lap
    return frame


def soft_nuisance_grid(data, teammate, sigma_programme):
    rows = []
    settings = [("free", None), ("strong", .015), ("moderate", .035), ("weak", .075)]
    for label, sigma in settings:
        for relaxed in (False, True):
            fitted = fit(data, teammate, sigma_programme, team=True, sigma=sigma,
                         ordered=False, relaxed=relaxed, name=f"soft_{label}_{'relaxed' if relaxed else 'physical'}")
            values = effective_values(fitted)
            soft = values[values.compound.eq("SOFT")]
            for row in soft.itertuples():
                rows.append({
                    "nuisance_strength": label, "sigma_nuisance_s_per_lap": sigma,
                    "bounds_mode": "relaxed" if relaxed else "physical",
                    "weekend_soft_D_s_per_lap": parameter(fitted, "SOFT"),
                    "team_or_run": row.team,
                    "nuisance_s_per_lap": row.team_deviation_s_per_lap,
                    "effective_soft_D_s_per_lap": row.effective_degradation_s_per_lap,
                    "nuisance_sign": "positive" if row.team_deviation_s_per_lap > 1e-6 else "negative" if row.team_deviation_s_per_lap < -1e-6 else "zero",
                    "likelihood_cost": fitted["cost"], "penalty_cost": fitted["penalty_cost"],
                    "raw_rms_s": float(np.sqrt(np.mean(fitted["raw"]**2))),
                })
    return pd.DataFrame(rows)


def compound_support(data, runs, leave_out):
    rows = []
    longrun = data[data.programme.eq("LongRun")]
    for compound in COMPOUNDS:
        group = longrun[longrun.compound.eq(compound)]
        run_info = runs[runs.compound.eq(compound)]
        loo = leave_out[leave_out.compound.eq(compound)]
        driver_counts = group.groupby("team").driver.nunique()
        rows.append({
            "compound": compound, "clean_longrun_laps": len(group),
            "runs": group.run_id.nunique(), "teams": group.team.nunique(),
            "team_names": "|".join(sorted(group.team.unique())),
            "drivers": group.driver.nunique(), "driver_names": "|".join(sorted(group.driver.unique())),
            "sessions": "|".join(sorted(group["session"].unique())),
            "tyre_age_min": group.tyre_age.min(), "tyre_age_max": group.tyre_age.max(),
            "tyre_age_span": group.tyre_age.max()-group.tyre_age.min(),
            "teammate_replication": bool((driver_counts >= 2).any()),
            "cross_team_replication": group.team.nunique() >= 2,
            "raw_run_slope_min": run_info.raw_lap_time_slope_s_per_lap.min(),
            "raw_run_slope_max": run_info.raw_lap_time_slope_s_per_lap.max(),
            "raw_run_slope_range": run_info.raw_lap_time_slope_s_per_lap.max()-run_info.raw_lap_time_slope_s_per_lap.min(),
            "leave_one_out_estimate_min": loo.estimate_s_per_lap.min(),
            "leave_one_out_estimate_max": loo.estimate_s_per_lap.max(),
            "leave_one_out_estimate_range": loo.estimate_s_per_lap.max()-loo.estimate_s_per_lap.min(),
        })
    return pd.DataFrame(rows)


def corrected_pace_rows(data, fitted):
    rows = []
    beta = fitted["beta"]
    effective = effective_values(fitted)
    effective_lookup = {
        (r.team, r.compound): r.effective_degradation_s_per_lap for r in effective.itertuples()
    }
    for index, row in data[data.programme.eq("LongRun")].iterrows():
        terms = dict(zip(fitted["names"], fitted["x"][index]*beta))
        fuel = terms["fuel_rate"]
        track = sum(v for k, v in terms.items() if k.startswith("track_rate_"))
        perf = sum(v for k, v in terms.items() if k.startswith("performance_"))
        baseline = fitted["baselines"][row.programme_block]
        offset = fitted["offsets"].get(row.run_id, 0.0)
        corrected_pace = row.corrected_lap_time-baseline-offset-fuel-track-perf
        global_d = parameter(fitted, row.compound)
        effective_d = effective_lookup.get((row.team, row.compound), global_d)
        rows.append({
            "run_id": row.run_id, "team": row.team, "driver": row.driver,
            "session": row["session"], "compound": row.compound,
            "physical_lap": row.lap_number, "physical_stint_index": row.physical_stint_index,
            "tyre_age": row.tyre_age, "corrected_pace_s": corrected_pace,
            "fitted_weekend_D_s_per_lap": global_d,
            "fitted_effective_D_s_per_lap": effective_d,
            "fitted_degradation_contribution_s": effective_d*row.tyre_age,
            "fuel_correction_s": fuel, "track_correction_s": track,
            "performance_correction_s": perf, "Q_driver_delta_s": row.driver_delta,
        })
    return pd.DataFrame(rows)


def plot_compounds(pace):
    colours = plt.get_cmap("tab10")
    for compound in COMPOUNDS:
        group = pace[pace.compound.eq(compound)]
        fig, ax = plt.subplots(figsize=(11, 6))
        for i, (run_id, run) in enumerate(group.groupby("run_id")):
            run = run.sort_values("tyre_age")
            label = f"{run.team.iloc[0]} / {run.driver.iloc[0]} / {run['session'].iloc[0]}"
            ax.scatter(run.tyre_age, run.corrected_pace_s, color=colours(i), label=label)
            ax.plot(run.tyre_age, run.fitted_degradation_contribution_s,
                    color=colours(i), linestyle="--")
        ax.set(xlabel="tyre age", ylabel="pace after block/run, Q, Fuel, Track and P corrections (s)",
               title=f"{compound} LongRun evidence and fitted effective degradation")
        ax.grid(alpha=.2); ax.legend(fontsize=8); fig.tight_layout()
        fig.savefig(OUTPUT/f"fp_{compound.lower()}_longrun_evidence.png", dpi=180); plt.close(fig)


def remaining_plots(data, support, leave_out, results):
    longrun = data[data.programme.eq("LongRun")]
    teams = sorted(longrun.team.unique())
    fig, ax = plt.subplots(figsize=(9, 7))
    team_y = {team: i for i, team in enumerate(teams)}
    compound_y = {compound: i*len(teams)/2 for i, compound in enumerate(COMPOUNDS)}
    for team, y in team_y.items(): ax.scatter(0, y, s=90, color="tab:blue"); ax.text(-.04, y, team, ha="right", va="center")
    for compound, y in compound_y.items(): ax.scatter(1, y, s=120, marker="s", color="tab:orange"); ax.text(1.04, y, compound, ha="left", va="center")
    for (team, compound), group in longrun.groupby(["team", "compound"]):
        ax.plot([0,1], [team_y[team], compound_y[compound]], alpha=.65, linewidth=1+len(group)/5)
        ax.text(.5, (team_y[team]+compound_y[compound])/2, str(len(group)), fontsize=7)
    ax.set(xlim=(-.55,1.45), xticks=[], yticks=[], title="R11 LongRun Team ↔ Compound coverage (edge label = clean laps)")
    for spine in ax.spines.values(): spine.set_visible(False)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_team_compound_coverage.png", dpi=180); plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(17, 5))
    for ax, compound in zip(axes, COMPOUNDS):
        group = leave_out[leave_out.compound.eq(compound)]
        ax.bar(group.robustness_variant, group.estimate_s_per_lap,
               color=["tab:blue" if "all" not in x and "both" not in x else "tab:green" for x in group.robustness_variant])
        ax.tick_params(axis="x", rotation=35); ax.set(title=compound, ylabel="D estimate (s/lap)"); ax.grid(axis="y", alpha=.2)
    fig.suptitle("Leave-one-run/team-out robustness"); fig.tight_layout(); fig.savefig(OUTPUT/"fp_degradation_robustness.png", dpi=180); plt.close(fig)

    kinds = ["direct_no_order_estimate_s_per_lap", "ordered_fit_estimate_s_per_lap",
             "nuisance_assisted_estimate_s_per_lap"]
    x = np.arange(3); width=.19
    fig, ax = plt.subplots(figsize=(11, 6))
    for i, kind in enumerate(kinds):
        ax.bar(x+(i-1.5)*width, results[kind], width,
               label=kind.replace("_estimate_s_per_lap", ""))
    engineering_bars = ax.bar(x+1.5*width, results.engineering_estimate_s_per_lap, width,
                              label="engineering")
    for bar, provenance in zip(engineering_bars, results.provenance):
        if provenance == "fallback_assumed":
            bar.set_hatch("//"); bar.set_edgecolor("black"); bar.set_facecolor("tab:red")
        elif provenance == "single_team_assumption":
            bar.set_hatch(".."); bar.set_edgecolor("black")
    ax.set_xticks(x, results.compound); ax.set(ylabel="degradation (s/lap)", title="Statistical and engineering degradation outputs")
    ax.axhline(0, color="black", lw=.8); ax.grid(axis="y", alpha=.2); ax.legend(fontsize=8)
    ax.text(.99, .02, "engineering hatch: dotted = single-team assumption; diagonal = fallback",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8)
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_statistical_vs_engineering_degradation.png", dpi=180); plt.close(fig)


def schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://dr-wo.local/schemas/fp-tyre-evidence-report.schema.json",
        "title": "FP tyre evidence report package", "type": "object",
        "required": ["schema_version", "event", "model", "coordinate_definitions",
                     "programme_evidence_policy", "driver_delta", "compounds", "runs",
                     "strategy_prediction_candidate", "external_context", "plot_manifest"],
        "properties": {
            "schema_version": {"type": "string"},
            "event": {"type": "object", "required": ["year", "round"],
                      "properties": {"year":{"type":"integer"}, "round":{"type":"integer"},
                                     "event_name":{"type":["string","null"]}}},
            "model": {"type":"object"}, "coordinate_definitions":{"type":"object"},
            "programme_evidence_policy":{"type":"object"}, "driver_delta":{"type":"object"},
            "compounds": {"type":"array", "items": {"type":"object", "required":[
                "compound", "engineering_estimate_s_per_lap", "direct_estimate_s_per_lap",
                "provenance", "direct_evidence", "model_usage", "assumptions", "limitations", "robustness"]}},
            "runs":{"type":"array", "items":{"type":"object"}},
            "strategy_prediction_candidate":{"type":"array", "items":{"type":"object"}},
            "performance_estimates":{"type":"object"},
            "fallback_interface":{"type":"object"},
            "external_context":{"type":"array", "items":{"type":"object", "required":[
                "source","source_url","published_at","category","statement","quantitative_value","unit","relevance"]}},
            "plot_manifest":{"type":"array", "items":{"type":"object", "required":["name","description"]}},
        },
        "additionalProperties": True,
    }


def markdown_table(frame):
    display = frame.copy()
    for column in display.select_dtypes(include=["float"]).columns:
        display[column] = display[column].map(lambda value: f"{value:.4f}" if pd.notna(value) else "")
    header = "| " + " | ".join(display.columns) + " |"
    separator = "|" + "|".join("---" for _ in display.columns) + "|"
    rows = ["| " + " | ".join(str(value).replace("|", ", ") for value in row) + " |"
            for row in display.itertuples(index=False, name=None)]
    return "\n".join([header, separator, *rows])


def reports(package, results, q_audit, support, soft_grid, leave_out):
    lookup = results.set_index("compound")
    lines = [
        "# FP Engineering Degradation Estimator Diagnostic", "",
        "## Recommendation", "",
        "The prototype produces physically separated H/M/S engineering values without presenting constraint-created equality as measurement. It keeps the direct and ordered statistical outputs beside each engineering value and labels every assumption. It is more interpretable for Strategy Prediction than the equality-collapse output, but remains diagnostic-only.", "",
        "| Compound | engineering D | direct no-order D | ordered statistical D | provenance | key limitation |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in results.itertuples():
        lines.append(f"| {row.compound} | {row.engineering_estimate_s_per_lap:.4f} | {row.direct_no_order_estimate_s_per_lap:.4f} | {row.ordered_fit_estimate_s_per_lap:.4f} | {row.provenance} | {row.key_limitation} |")
    q_change = q_audit.HARD_D_change_due_to_Q_s_per_lap.iloc[0]
    q_raw = q_audit.raw_SAI_minus_ALB_baseline_s.iloc[0]
    q_corrected = q_audit.q_corrected_SAI_minus_ALB_baseline_s.iloc[0]
    medium_loo = leave_out[leave_out.compound.eq("MEDIUM")]
    hard_loo = leave_out[leave_out.compound.eq("HARD")].set_index("robustness_variant")
    run_frame = pd.DataFrame(package["runs"])
    medium_runs = run_frame[run_frame.compound.eq("MEDIUM")]
    medium_direct_residual_slope = medium_runs.direct_fit_residual_slope_s_per_lap.abs().mean()
    medium_nuisance_residual_slope = medium_runs.nuisance_fit_residual_slope_s_per_lap.abs().mean()
    medium_effective = pd.DataFrame(package["nuisance_values"]["team_effective_moderate"])
    medium_effective = medium_effective[medium_effective.compound.eq("MEDIUM")]
    soft_physical = soft_grid[(soft_grid.nuisance_strength.eq("moderate")) & soft_grid.bounds_mode.eq("physical")]
    soft_nuisance = soft_physical.weekend_soft_D_s_per_lap.iloc[0]
    fallback_examples = soft_grid[soft_grid.diagnostic_type.eq("fallback_sensitivity")]
    lines += [
        "", "## DriverDelta audit", "",
        f"The analytical Q correction is already applied before modelling as `corrected_lap_time = lap_time - driver_delta`. Williams SAI receives `{q_audit.analytical_q_driver_delta_s.max():.4f} s`; ALB is the zero reference. The raw SAI−ALB LongRun mean difference is `{q_raw:.4f} s`, becoming `{q_corrected:.4f} s` after Q adjustment. HARD D changes by `{q_change:.6f} s/lap` between the without-Q and with-Q shared fits. A constant correction leaves every within-run slope unchanged; it only provides an imperfect baseline alignment.", "",
        "## HARD policy", "",
        f"Strategy candidate: `{lookup.loc['HARD','engineering_estimate_s_per_lap']:.4f} s/lap`. It uses both Williams drivers and assumes Williams represents the field. R11 cannot identify a Williams-vs-field tyre-usage bias. ALB-only, SAI-only and combined estimates are `{hard_loo.loc['ALB_only','estimate_s_per_lap']:.4f}/{hard_loo.loc['SAI_only','estimate_s_per_lap']:.4f}/{hard_loo.loc['both_drivers','estimate_s_per_lap']:.4f} s/lap`.", "",
        "## MEDIUM policy", "",
        f"Strategy candidate: `{lookup.loc['MEDIUM','engineering_estimate_s_per_lap']:.4f} s/lap`, from a no-order Team×Compound model with Gaussian team-deviation sigma `{MODERATE_TEAM_SIGMA:.3f} s/lap`. Effective McLaren/Mercedes/Red Bull values are `" + "/".join(f"{row.team} {row.effective_degradation_s_per_lap:.4f}" for row in medium_effective.itertuples()) + f"`. Mean absolute MEDIUM residual slope changes from `{medium_direct_residual_slope:.4f}` to `{medium_nuisance_residual_slope:.4f} s/lap`. Leave-one-team-out estimates span `{medium_loo.estimate_s_per_lap.min():.4f}–{medium_loo.estimate_s_per_lap.max():.4f}` and leaving Mercedes out reaches the `0.20 s/lap` physical bound. Shrinkage was not selected against Race retro.", "",
        "## SOFT policy", "",
        f"LongRun direct physical-bounds D is `{lookup.loc['SOFT','direct_no_order_estimate_s_per_lap']:.4f}`, and the relaxed direct result is `{lookup.loc['SOFT','direct_relaxed_estimate_s_per_lap']:.4f} s/lap`. Moderate nuisance assistance gives `{soft_nuisance:.4f}` and requires opposite-sign run/team corrections. These are not credible physical Strategy inputs. No legacy numerical degradation ratio was found. The configurable ratio sensitivity `{', '.join(f'{r.fallback_soft_to_medium_ratio:.2f}→{r.fallback_candidate_s_per_lap:.4f}' for r in fallback_examples.itertuples())}` is exported. The selected diagnostic example `Soft = {SOFT_FALLBACK_RATIO_EXAMPLE:.2f} × Medium` gives `{lookup.loc['SOFT','engineering_estimate_s_per_lap']:.4f} s/lap`. This is assumption-assisted, not measured, and must be reviewed before adoption.", "",
        "## Existing fallback audit", "",
        "`OfflineAnalysisService.strategy_assumptions` already supports preserving a supplied pre-race H/M/S baseline and scaling uninformed compounds by the mean Race-informed ratio. This is a transport/update mechanism, not a source of baseline compound ratios. The repository's pre-race defaults are bounds only. No defensible legacy Soft/Medium degradation ratio was found, so this prototype exposes a configurable ratio and a sensitivity grid rather than silently creating production logic.", "",
        "No suitable locally cached official F1/Pirelli R11 tyre statement was found. `external_context` is therefore empty and the package includes a typed template for later population.", "",
        "## Answers", "",
        f"1. HARD supplied: `{lookup.loc['HARD','engineering_estimate_s_per_lap']:.4f} s/lap`.",
        f"2. Q-baseline handling changes fitted HARD by `{q_change:.6f} s/lap`; ALB-only/SAI-only/both estimates are `{hard_loo.loc['ALB_only','estimate_s_per_lap']:.4f}/{hard_loo.loc['SAI_only','estimate_s_per_lap']:.4f}/{hard_loo.loc['both_drivers','estimate_s_per_lap']:.4f}`.",
        "3. HARD has direct within-run evidence but its field-wide interpretation is a single-team assumption.",
        f"4. MEDIUM supplied: `{lookup.loc['MEDIUM','engineering_estimate_s_per_lap']:.4f} s/lap`.",
        f"5. MEDIUM leave-one-team-out range: `{medium_loo.estimate_s_per_lap.min():.4f}–{medium_loo.estimate_s_per_lap.max():.4f}`.",
        f"6. Direct Soft says `{lookup.loc['SOFT','direct_no_order_estimate_s_per_lap']:.4f}` under physical bounds and `{lookup.loc['SOFT','direct_relaxed_estimate_s_per_lap']:.4f}` relaxed.",
        f"7. Moderate nuisance-assisted Soft: `{soft_nuisance:.4f} s/lap`.",
        f"8. Soft fallback comes from an exposed diagnostic configuration example (`{SOFT_FALLBACK_RATIO_EXAMPLE:.2f} × Medium`), because no repository numerical ratio exists.",
        "9. Yes, the engineering candidates are separated; ordering did not generate them.",
        "10. Within-run slopes are direct data; Medium's cross-team mean is nuisance-assisted.",
        "11. HARD field representativeness and Soft/Medium fallback ratio materially depend on assumptions.",
        "12. Yes for engineering interpretability and provenance, not because of Race-retro agreement. Every non-data step is machine-readable and direct results remain visible.", "",
        "## Deliverables", "",
        "All requested tables, JSON, deterministic reports and plots are stored under `temp/`. Production Race inference and Strategy Prediction inputs were not changed.",
    ]
    (OUTPUT/"FP_ENGINEERING_DEGRADATION_ESTIMATOR_DIAGNOSTIC.md").write_text("\n".join(lines)+"\n")

    evidence_lines = [
        "# FP Tyre Evidence Report", "", "Deterministic evidence package; this document contains no LLM-generated estimates.", "",
        "## Field definitions", "",
        "- `direct_estimate`: physical-bounds LongRun-only, no-order statistical fit.",
        "- `direct_relaxed_estimate`: no-order diagnostic with relaxed bounds; exposes nonphysical preferences.",
        "- `ordered_fit_estimate`: statistical fit with H≤M≤S; retained only to detect equality collapse.",
        "- `nuisance_assisted_estimate`: no-order Team×Compound fit with explicit Gaussian shrinkage.",
        "- `engineering_estimate`: candidate after the declared compound policy and assumptions.",
        "- `provenance`: factual origin (`single_team_assumption`, `nuisance_assisted`, or `fallback_assumed`).", "",
        "## Corrections and evidence policy", "",
        "Fuel uses actual physical stint progression with the fuel-remaining sign. Track uses elapsed SessionTime in lap-equivalents. Q DriverDelta is a constant approximate driver-baseline correction. Only LongRun rows have degradation sensitivity; QualiSim and Setting remain in Fuel/Track, performance and baseline paths.", "",
        "## Strategy candidate", "", markdown_table(results[["compound","engineering_estimate_s_per_lap","direct_no_order_estimate_s_per_lap","provenance","key_limitation"]]), "",
        "## Evidence coverage", "", markdown_table(support), "",
        "## What cannot be determined", "",
        "R11 cannot determine Williams HARD representativeness, a field-wide HARD team correction, driver-specific degradation, or a direct robust Soft degradation. It also provides no locally established numerical Soft fallback ratio. External context is empty until separately supplied and is never converted silently into a prior.",
    ]
    (OUTPUT/"FP_TYRE_EVIDENCE_REPORT.md").write_text("\n".join(evidence_lines)+"\n")

    guide = """# FP Tyre Report Agent Guide

Read `fp_tyre_evidence_report.json` as the canonical source. Explain the supplied numbers, compare factual evidence coverage, surface assumptions and limitations, identify run/team disagreements, and summarize implications for pre-race strategy.

Do not refit data, invent values, change estimator outputs, silently replace assumptions, infer confidence labels not present in the package, or describe an assumption/fallback as directly measured. Keep statistical, nuisance-assisted and engineering values distinct. Treat `external_context` as explanatory context only unless the package explicitly says otherwise.
"""
    (OUTPUT/"FP_TYRE_REPORT_AGENT_GUIDE.md").write_text(guide)
    prompt = """# FP Tyre Report Agent Prompt

You are writing a pre-race tyre engineering report from a deterministic evidence package.

Inputs:
1. `fp_tyre_evidence_report.json` — canonical numbers and provenance.
2. `FP_TYRE_EVIDENCE_REPORT.md` — field definitions and deterministic summary.
3. Plot files listed in `plot_manifest`.

Produce a clear report that:
- states the Strategy Prediction candidate H/M/S degradation values;
- distinguishes direct, ordered-statistical, nuisance-assisted and fallback outputs;
- compares clean laps, runs, teams, tyre-age span and leave-one-out stability;
- explains Q DriverDelta as an approximate constant baseline correction;
- identifies contradictory Soft evidence and all material assumptions;
- describes external context only if entries are supplied.

Never recalculate or replace values, invent missing tyre quantities, turn commentary into an undeclared numerical prior, or claim fallback values are measured. If evidence is insufficient, say so explicitly.
"""
    (OUTPUT/"FP_TYRE_REPORT_AGENT_PROMPT.md").write_text(prompt)


def main():
    OUTPUT.mkdir(exist_ok=True)
    _, _, _, _, data, teammate, _, sigma_programme = foundation.load_inputs()
    data = data.copy().reset_index(drop=True)

    direct = fit(data, teammate, sigma_programme, ordered=False, name="direct_longrun_no_order")
    direct_relaxed = fit(data, teammate, sigma_programme, ordered=False, relaxed=True, name="direct_longrun_relaxed")
    ordered = fit(data, teammate, sigma_programme, ordered=True, name="ordered_longrun")
    nuisance = fit(data, teammate, sigma_programme, team=True, sigma=MODERATE_TEAM_SIGMA,
                   ordered=False, name="nuisance_assisted_no_order")
    no_q_data = data.copy(); no_q_data["driver_delta"] = 0.0; no_q_data["corrected_lap_time"] = no_q_data.lap_time_s
    # Retain the same declared Q uncertainty covariance and remove only the
    # central DriverDelta, isolating the central baseline-correction effect.
    without_q = fit(no_q_data, teammate, sigma_programme, ordered=False, name="direct_without_Q")

    runs = raw_run_support(data)
    direct_slopes = degradation.residual_slopes(direct, data)[
        ["run_id", "residual_slope_s_per_tyre_lap"]
    ].rename(columns={"residual_slope_s_per_tyre_lap":"direct_fit_residual_slope_s_per_lap"})
    nuisance_slopes = degradation.residual_slopes(nuisance, data)[
        ["run_id", "residual_slope_s_per_tyre_lap"]
    ].rename(columns={"residual_slope_s_per_tyre_lap":"nuisance_fit_residual_slope_s_per_lap"})
    runs = runs.merge(direct_slopes, on="run_id", how="left").merge(nuisance_slopes, on="run_id", how="left")
    q_audit = q_driver_audit(data, teammate, sigma_programme, direct, without_q)
    leave_out = leave_one_out(data, teammate, sigma_programme)
    soft_grid = soft_nuisance_grid(data, teammate, sigma_programme)
    support = compound_support(data, runs, leave_out)

    medium_engineering = parameter(nuisance, "MEDIUM")
    soft_grid["diagnostic_type"] = "nuisance_model"
    soft_grid["fallback_soft_to_medium_ratio"] = np.nan
    soft_grid["fallback_candidate_s_per_lap"] = np.nan
    soft_grid["fallback_source"] = ""
    fallback_rows = pd.DataFrame([{
        "nuisance_strength":"fallback_example", "sigma_nuisance_s_per_lap":np.nan,
        "bounds_mode":"assumption", "weekend_soft_D_s_per_lap":np.nan,
        "team_or_run":"ALL", "nuisance_s_per_lap":np.nan,
        "effective_soft_D_s_per_lap":np.nan, "nuisance_sign":"not_applicable",
        "likelihood_cost":np.nan, "penalty_cost":np.nan, "raw_rms_s":np.nan,
        "diagnostic_type":"fallback_sensitivity",
        "fallback_soft_to_medium_ratio":ratio,
        "fallback_candidate_s_per_lap":ratio*medium_engineering,
        "fallback_source":"configurable diagnostic example; no repository numerical ratio found",
    } for ratio in SOFT_FALLBACK_RATIO_SENSITIVITY])
    soft_grid = pd.concat((soft_grid, fallback_rows), ignore_index=True)
    engineering = {
        "HARD": parameter(direct, "HARD"),
        "MEDIUM": medium_engineering,
        "SOFT": SOFT_FALLBACK_RATIO_EXAMPLE*medium_engineering,
    }
    provenance = {"HARD":"single_team_assumption", "MEDIUM":"nuisance_assisted", "SOFT":"fallback_assumed"}
    limitations = {
        "HARD":"one team only; Williams-to-field tyre-usage bias is not identifiable",
        "MEDIUM":"three teams; weekend mean and team deviations depend on shrinkage",
        "SOFT":"conflicting runs, no teammate replication, direct fit near/below zero; configurable fallback required",
    }
    result_rows = []
    for compound in COMPOUNDS:
        result_rows.append({
            "compound": compound,
            "direct_estimate_s_per_lap": parameter(direct, compound),
            "direct_no_order_estimate_s_per_lap": parameter(direct, compound),
            "direct_relaxed_estimate_s_per_lap": parameter(direct_relaxed, compound),
            "ordered_fit_estimate_s_per_lap": parameter(ordered, compound),
            "nuisance_assisted_estimate_s_per_lap": parameter(nuisance, compound),
            "engineering_estimate_s_per_lap": engineering[compound],
            "provenance": provenance[compound],
            "nuisance_used_for_engineering": compound == "MEDIUM",
            "fallback_used_for_engineering": compound == "SOFT",
            "fallback_configuration": f"soft_to_medium_ratio={SOFT_FALLBACK_RATIO_EXAMPLE}" if compound == "SOFT" else "",
            "ordered_equality_face": abs(parameter(ordered,"HARD")-parameter(ordered,"MEDIUM")) < 1e-5 and abs(parameter(ordered,"MEDIUM")-parameter(ordered,"SOFT")) < 1e-5,
            "direct_fit_at_physical_boundary": parameter(direct, compound) <= 1e-5 or parameter(direct, compound) >= .19999,
            "key_limitation": limitations[compound],
        })
    results = pd.DataFrame(result_rows)
    result_export = []
    for row in results.to_dict("records"):
        result_export.extend([
            {"compound":row["compound"], "output_type":"direct_no_order", "estimate_s_per_lap":row["direct_no_order_estimate_s_per_lap"], "provenance":"direct_data"},
            {"compound":row["compound"], "output_type":"direct_relaxed", "estimate_s_per_lap":row["direct_relaxed_estimate_s_per_lap"], "provenance":"direct_data_diagnostic_relaxed"},
            {"compound":row["compound"], "output_type":"ordered_statistical", "estimate_s_per_lap":row["ordered_fit_estimate_s_per_lap"], "provenance":"constraint_affected_statistical"},
            {"compound":row["compound"], "output_type":"nuisance_assisted", "estimate_s_per_lap":row["nuisance_assisted_estimate_s_per_lap"], "provenance":"nuisance_assisted"},
            {"compound":row["compound"], "output_type":"engineering", "estimate_s_per_lap":row["engineering_estimate_s_per_lap"], "provenance":row["provenance"]},
        ])
    pd.DataFrame(result_export).to_csv(OUTPUT/"fp_engineering_degradation_results.csv", index=False)

    provenance_rows = support.merge(results, on="compound")
    provenance_rows.to_csv(OUTPUT/"fp_compound_evidence_provenance.csv", index=False)
    q_audit.to_csv(OUTPUT/"fp_driver_delta_longrun_audit.csv", index=False)
    leave_out.to_csv(OUTPUT/"fp_leave_one_out_degradation.csv", index=False)
    soft_grid.to_csv(OUTPUT/"fp_soft_nuisance_diagnostic.csv", index=False)
    team_effective = effective_values(nuisance)
    team_effective.to_csv(OUTPUT/"fp_engineering_team_effective_degradation.csv", index=False)
    strategy = provenance_rows[["compound","engineering_estimate_s_per_lap","direct_no_order_estimate_s_per_lap","provenance","runs","teams","nuisance_used_for_engineering","fallback_used_for_engineering","key_limitation"]].copy()
    strategy.columns = ["compound","engineering_degradation_s_per_lap","direct_degradation_s_per_lap","source_provenance","number_of_longruns","number_of_teams","nuisance_used","fallback_used","key_limitation"]
    strategy.to_csv(OUTPUT/"fp_strategy_prediction_candidate.csv", index=False)

    pace = corrected_pace_rows(data, nuisance)
    pace.to_csv(OUTPUT/"fp_engineering_longrun_corrected_pace.csv", index=False)
    plot_compounds(pace); remaining_plots(data, support, leave_out, results)

    first = data.iloc[0]
    compound_packages = []
    for row in provenance_rows.to_dict("records"):
        compound = row["compound"]
        robustness = leave_out[leave_out.compound.eq(compound)].to_dict("records")
        assumptions = []
        if compound == "HARD": assumptions = ["Williams HARD degradation is representative of the weekend field", "Q pace difference is an approximate constant LongRun baseline correction"]
        elif compound == "MEDIUM": assumptions = [f"Team deviations are exchangeable Gaussian nuisances with sigma {MODERATE_TEAM_SIGMA:.3f} s/lap"]
        else: assumptions = [f"Diagnostic configurable Soft/Medium degradation ratio is {SOFT_FALLBACK_RATIO_EXAMPLE:.2f}", "This ratio is not a repository legacy value or direct Soft measurement"]
        compound_packages.append({
            "compound": compound,
            "engineering_estimate_s_per_lap": row["engineering_estimate_s_per_lap"],
            "direct_estimate_s_per_lap": row["direct_no_order_estimate_s_per_lap"],
            "direct_relaxed_estimate_s_per_lap": row["direct_relaxed_estimate_s_per_lap"],
            "ordered_statistical_estimate_s_per_lap": row["ordered_fit_estimate_s_per_lap"],
            "nuisance_assisted_estimate_s_per_lap": row["nuisance_assisted_estimate_s_per_lap"],
            "provenance": row["provenance"],
            "direct_evidence": {
                "clean_laps": row["clean_longrun_laps"], "runs": row["runs"],
                "teams": row["team_names"].split("|"), "drivers": row["driver_names"].split("|"),
                "sessions": row["sessions"].split("|"), "tyre_age_min": row["tyre_age_min"],
                "tyre_age_max": row["tyre_age_max"], "tyre_age_span": row["tyre_age_span"],
                "teammate_replication": row["teammate_replication"],
                "cross_team_replication": row["cross_team_replication"],
                "raw_run_slope_range_s_per_lap": row["raw_run_slope_range"],
            },
            "model_usage": {"quali_driver_delta_used": True,
                            "team_nuisance_used": bool(row["nuisance_used_for_engineering"]),
                            "fallback_used": bool(row["fallback_used_for_engineering"])},
            "assumptions": assumptions, "limitations": [row["key_limitation"]],
            "robustness": {"leave_one_out": robustness},
        })
    plots = [
        ("fp_hard_longrun_evidence.png","HARD corrected LongRun pace and fitted degradation"),
        ("fp_medium_longrun_evidence.png","MEDIUM corrected LongRun pace and team-effective degradation"),
        ("fp_soft_longrun_evidence.png","SOFT corrected LongRun pace and nuisance-assisted degradation"),
        ("fp_team_compound_coverage.png","LongRun Team-to-Compound support graph"),
        ("fp_degradation_robustness.png","Leave-one-run/team-out estimates"),
        ("fp_statistical_vs_engineering_degradation.png","Direct, ordered, nuisance and engineering comparison"),
    ]
    package = {
        "schema_version":"1.0.0",
        "event":{"year":int(first.Year), "round":int(first.Round), "event_name":str(first.EventName)},
        "model":{"diagnostic_name":"FP engineering degradation estimator", "version":"r11-prototype-1",
                 "production":False, "lap_noise_sigma_s":foundation.LAP_SIGMA,
                 "programme_mismatch_sigma_s":sigma_programme},
        "coordinate_definitions":{
            "fuel":"negative physical stint index; omitted laps count; positive rate represents fuel remaining",
            "track":"elapsed SessionTime from session start divided by representative session lap time",
            "tyre_age":"public timing TyreLife on individually clean laps"},
        "programme_evidence_policy":{"LongRun":"direct degradation evidence", "QualiSim":"no degradation sensitivity; retained for performance/baselines", "Setting":"no degradation sensitivity; retained for performance/baselines"},
        "driver_delta":{"source":"analytical Q track-corrected quickest-driver result", "role":"approximate constant driver baseline alignment", "claim_not_made":"qualifying and LongRun driver deltas are physically identical", "audit":q_audit.to_dict("records")},
        "compounds":compound_packages, "runs":runs.to_dict("records"),
        "statistical_estimates":results.to_dict("records"),
        "nuisance_values":{"team_effective_moderate":team_effective.to_dict("records"),
                           "soft_nuisance_and_fallback_sensitivity":soft_grid.to_dict("records")},
        "robustness_tests":leave_out.to_dict("records"),
        "strategy_prediction_candidate":strategy.to_dict("records"),
        "performance_estimates":performance(direct),
        "fallback_interface":{"type":"configurable_soft_to_medium_ratio", "selected_diagnostic_example":SOFT_FALLBACK_RATIO_EXAMPLE,
                              "selected_diagnostic_value_s_per_lap":engineering["SOFT"],
                              "sensitivity_examples":list(SOFT_FALLBACK_RATIO_SENSITIVITY),
                              "sensitivity_values_s_per_lap":{str(ratio):ratio*medium_engineering for ratio in SOFT_FALLBACK_RATIO_SENSITIVITY},
                              "repository_existing_mechanism":"Race uninformed compounds can preserve/scale supplied pre-race baselines",
                              "repository_numeric_ratio_found":False,
                              "warning":"diagnostic example only; not a production or measured ratio"},
        "external_context":[],
        "external_context_template":{"source":"F1/Pirelli", "source_url":"", "published_at":"", "category":"", "statement":"", "quantitative_value":None, "unit":None, "relevance":""},
        "external_context_allowed_categories":["compound allocation", "expected compound delta", "observed compound delta", "degradation comment", "thermal degradation", "wear degradation", "track roughness", "track evolution", "tyre stress", "limited-data warning", "strategy expectation"],
        "plot_manifest":[{"name":name,"description":description} for name,description in plots],
        "limitations":["single weekend", "public timing only", "no driver-specific degradation", "Soft fallback is assumed", "no historical learning"],
    }
    package = json_clean(package)
    report_schema = schema()
    (OUTPUT/"fp_tyre_evidence_report.json").write_text(json.dumps(package, indent=2)+"\n")
    (OUTPUT/"fp_tyre_report_schema.json").write_text(json.dumps(report_schema, indent=2)+"\n")
    reports(package, results, q_audit, support, soft_grid, leave_out)


if __name__ == "__main__":
    main()
