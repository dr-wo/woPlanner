#!/usr/bin/env python3
"""Diagnostic-only revised FP ProgrammeBlock + analytical-Q teammate prototype."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

import audit_fp_programme_classifier as classifier
import prototype_fp_programme_blocks as prototype


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
Q_USAGE = OUTPUT / "f1_reports/quali_performance_2026_1-11_Mercedes_linear_usage.csv"
SEEDS = (1, 2026, 424242, 1898758267)
TEAM_COLORS = {team: plt.get_cmap("tab20")(index)
               for index, team in enumerate(("Alpine", "Aston Martin", "Audi", "Cadillac",
                                               "Ferrari", "Haas F1 Team", "McLaren", "Mercedes",
                                               "Racing Bulls", "Red Bull Racing", "Williams"))}
LABEL_MARKERS = {"Setting": "s", "QualiSim": "o", "LongRun": "^", "Uncertain": "x"}


def revised_inputs():
    laps = prototype.load_laps()
    meta, groups = classifier.prepare_runs(laps)
    ledger = classifier.add_legacy_labels(
        classifier.classify_all(meta, groups, classifier.ClassifierConfig()), groups)
    ledger = classifier.performance_selection(classifier.mark_model_critical(ledger))
    rows = []
    for run in ledger.itertuples():
        group = groups[run.run_id].sort_values("LapNumber").copy()
        group["run_id"] = run.run_id; group["programme"] = run.revised_label
        group["push_cfg"] = group.Representative & group.RelativePace.le(1.03)
        rows.append(group)
    full = pd.concat(rows, ignore_index=True)
    use = ((full["programme"].eq("LongRun") & full.Representative)
           | (full["programme"].isin(["Setting", "QualiSim"]) & full.push_cfg))
    data = full[use].copy()
    data = data.rename(columns={"Session": "session", "Team": "team", "Driver": "driver",
                                "Compound": "compound", "LapNumber": "lap_number",
                                "LapTimeSeconds": "lap_time_s", "TyreLife": "tyre_life",
                                "SessionSeconds": "session_time_s"})
    data["run_index"] = data.groupby("run_id").cumcount() + 1
    data["tyre_age"] = pd.to_numeric(data.tyre_life, errors="coerce")
    data["track_time"] = data.session_time_s - data.groupby("session").session_time_s.transform("min")
    data["track_time"] /= data.groupby("session").track_time.transform("max").replace(0, 1)
    data["programme_block"] = (data["team"].astype(str) + ":" + data["session"].astype(str)
                               + ":" + data["programme"].astype(str))
    return laps, ledger, groups, full, data.reset_index(drop=True)


def q_teammate_inputs(data, fp_teammate):
    usage = pd.read_csv(Q_USAGE)
    usage = usage[(usage.Race.eq(11)) & usage.ResultType.eq("average")].copy()
    rows = []
    for row in usage.itertuples():
        drivers = str(row.Driver).split("/")
        if len(drivers) == 1:
            # The analytical tracker suppresses the slower teammate from the averaged team
            # output above 0.6%; infer the other R11 Q driver from the FP roster.
            roster = sorted(set(data.loc[data["team"].eq(row.Team), "driver"]))
            other = [driver for driver in roster if driver != drivers[0]]
            if row.Team == "Red Bull Racing" and "HAD" in other:
                drivers.append("HAD")
            elif len(other) == 1:
                drivers.append(other[0])
        if len(drivers) != 2:
            continue
        fastest, slower = drivers
        delta = float(row.TeammateDeltaSeconds)
        reference, driver = sorted(drivers)
        signed = delta if driver == slower else -delta
        # Attempt-level corrected laps were not persisted. Use a conservative analytic
        # uncertainty floor plus half the observed teammate separation.
        sigma = max(0.150, delta / 2)
        rows.append({"team": row.Team, "reference_driver": reference, "driver": driver,
                     "median_delta_s": signed, "uncertainty_sigma_s": sigma,
                     "p10_delta_s": signed - 1.2816 * sigma,
                     "p90_delta_s": signed + 1.2816 * sigma,
                     "uncertainty_width_s": 2 * 1.2816 * sigma,
                     "source": "analytical Q track-corrected quickest-driver result",
                     "q_fastest_driver": fastest, "q_slower_driver": slower,
                     "q_qualifying_parts": row.QualifyingPart,
                     "q_source_laps": row.SourceLaps,
                     "uncertainty_method": "max(0.150 s, abs(delta)/2); conservative because eligible Q attempts were not persisted"})
    q = pd.DataFrame(rows)

    selected = []
    for (team, session), group in data.groupby(["team", "session"]):
        drivers = sorted(group.driver.unique())
        qrow = q[q["team"].eq(team)]
        if len(drivers) == 2 and len(qrow) and set(drivers) == {qrow.iloc[0].reference_driver, qrow.iloc[0].driver}:
            source = qrow.iloc[0].to_dict(); source["session"] = session
            source["calibration_choice"] = "Q"
            selected.append(source)
        else:
            old = fp_teammate[(fp_teammate["team"].eq(team)) & (fp_teammate["session"].eq(session))]
            if len(old):
                source = old.iloc[0].to_dict()
                source["uncertainty_sigma_s"] = source["uncertainty_width_s"] / 2.563
                source["calibration_choice"] = "FP_fallback_driver_pair_mismatch"
                selected.append(source)
    return q, pd.DataFrame(selected)


def comparison_table(fp, q_selected):
    old = fp.rename(columns={"median_delta_s": "fp_median_delta_s",
                             "uncertainty_width_s": "fp_p10_p90_width_s"})
    new = q_selected.rename(columns={"median_delta_s": "selected_median_delta_s",
                                     "uncertainty_width_s": "selected_p10_p90_width_s",
                                     "source": "selected_source"})
    columns = ["team", "session", "reference_driver", "driver", "selected_median_delta_s",
               "selected_p10_p90_width_s", "uncertainty_sigma_s", "selected_source",
               "calibration_choice"]
    merged = old.merge(new[columns], on=["team", "session", "reference_driver", "driver"], how="outer")
    merged["absolute_central_change_s"] = (merged.selected_median_delta_s - merged.fp_median_delta_s).abs()
    merged["width_ratio_selected_to_fp"] = merged.selected_p10_p90_width_s / merged.fp_p10_p90_width_s
    return merged


def apply_teammate(data, teammate):
    result = data.copy(); result["driver_delta"] = 0.0
    for row in teammate.itertuples():
        mask = (result["team"].eq(row.team) & result["session"].eq(row.session)
                & result["driver"].eq(row.driver))
        result.loc[mask, "driver_delta"] = row.median_delta_s
    result["corrected_lap_time"] = result.lap_time_s - result.driver_delta
    return result


def model_diagnostics(data, teammate, sigma_programme, scenario):
    x = prototype.build_physical_x(data, "track_time")
    y = data.corrected_lap_time.to_numpy()
    xp, _ = prototype.gls_project(data, x, y, sigma_programme, teammate)
    singular = np.linalg.svd(xp, compute_uv=False)
    rank = int(np.linalg.matrix_rank(xp))
    return {"scenario": scenario, "observation_count": len(data), "run_count": data.run_id.nunique(),
            "programme_block_count": data.programme_block.nunique(), "rank": rank,
            "nullity": 9-rank, "condition_number": singular[0]/singular[-1],
            "minimum_normalized_singular": singular[-1]/singular[0],
            "normalized_singular_spectrum": "|".join(f"{value/singular[0]:.7g}" for value in singular)}


def bounded_mc(data, sigma_programme, teammate, counts=(20000, 50000, 100000), seeds=SEEDS):
    x = prototype.build_physical_x(data, "track_time"); y = data.corrected_lap_time.to_numpy()
    xp, yp = prototype.gls_project(data, x, y, sigma_programme, teammate)
    rows = []
    for n in counts:
        for seed in seeds:
            candidates = prototype.ordered_candidates(n, seed)
            costs = np.empty(n)
            for start in range(0, n, 2000):
                stop = min(start + 2000, n)
                residual = yp[None, :] - candidates[start:stop] @ xp.T
                costs[start:stop] = np.sum(residual**2, axis=1)
            log_weight = -.5 * (costs - costs.min())
            weight = np.exp(log_weight - log_weight.max()); weight /= weight.sum()
            ess = 1 / np.sum(weight * weight)
            for index, parameter in enumerate(prototype.PARAMETERS):
                order = np.argsort(candidates[:, index]); cumulative = np.cumsum(weight[order])
                quantiles = [candidates[order[min(np.searchsorted(cumulative, p), n-1)], index]
                             for p in (.1, .5, .9)]
                rows.append({"sample_count": n, "seed": seed, "parameter": parameter,
                             "p10": quantiles[0], "median": quantiles[1], "p90": quantiles[2],
                             "width": quantiles[2]-quantiles[0], "ess": ess, "ess_fraction": ess/n,
                             "max_weight": weight.max(), "top5_weight": np.sort(weight)[-5:].sum(),
                             "sigma_programme_s": sigma_programme, "measurement_sigma_s": .35})
    return pd.DataFrame(rows)


def run_mc_comparison(data_fp, data_q, fp_teammate, q_teammate, sigma_programme):
    outputs = []
    for scenario, data, teammate in (("revised_classifier_FP_teammate", data_fp, fp_teammate),
                                      ("revised_classifier_Q_preferred", data_q, q_teammate)):
        result = bounded_mc(data, sigma_programme, teammate)
        result["scenario"] = scenario; outputs.append(result)
    return pd.concat(outputs, ignore_index=True)


def bounded_synthetic(data, sigma_programme, teammate):
    truths=np.array([.04,-.02,.01,-.01,.03,.07,.12,.5,-.6])
    x=prototype.build_physical_x(data,"track_time")
    rng=np.random.default_rng(991)
    block_codes,_=pd.factorize(data["programme_block"],sort=True)
    run_codes,_=pd.factorize(data["run_id"],sort=True)
    base=80+block_codes*.2; run_offset=rng.normal(0,sigma_programme,run_codes.max()+1)
    noise=rng.normal(0,.12,len(data))
    scenarios={"track_only":np.array([0,*truths[1:4],0,0,0,0,0]),
               "track_degradation":np.array([0,*truths[1:7],0,0]),
               "track_degradation_fuel":np.array([*truths[:7],0,0]),
               "add_performance":truths,"full_nuisance":truths}
    rows=[]
    for scenario, truth in scenarios.items():
        temporary=data.copy(); values=base+x@truth
        if scenario=="full_nuisance": values=values+run_offset[run_codes]+noise
        temporary["corrected_lap_time"]=values
        result=bounded_mc(temporary,sigma_programme,teammate,counts=(100000,),seeds=(2026,))
        result["scenario"]=scenario
        result["truth"]=[truth[prototype.PARAMETERS.index(parameter)] for parameter in result.parameter]
        result["median_error"]=result["median"]-result.truth
        result["truth_inside_p10_p90"]=(result.p10<=result.truth)&(result.truth<=result.p90)
        rows.append(result)
    return pd.concat(rows,ignore_index=True)


def summarize_prototype(diagnostics, convergence):
    rows = []
    for scenario, group in convergence.groupby("scenario"):
        diag = diagnostics[diagnostics.scenario.eq(scenario)].iloc[0].to_dict()
        for sample_count, count_group in group.groupby("sample_count"):
            quality = count_group.groupby("seed")[["ess", "max_weight", "top5_weight"]].first()
            medians = count_group.pivot(index="parameter", columns="seed", values="median")
            widths = count_group.groupby("parameter").width.mean()
            rows.append({**diag, "sample_count": sample_count,
                         "ess_min": quality.ess.min(), "ess_max": quality.ess.max(),
                         "max_weight_min": quality.max_weight.min(), "max_weight_max": quality.max_weight.max(),
                         "top5_weight_min": quality.top5_weight.min(), "top5_weight_max": quality.top5_weight.max(),
                         "maximum_parameter_median_seed_span": (medians.max(axis=1)-medians.min(axis=1)).max(),
                         "mean_p10_p90_width": widths.mean()})
    return pd.DataFrame(rows)


def previous_prototype_summary():
    convergence = pd.read_csv(OUTPUT / "fp_programme_mc_convergence.csv")
    architecture = pd.read_csv(OUTPUT / "fp_model_architecture_comparison.csv")
    architecture = architecture[architecture.architecture.eq("prototype_3_programme_random_run")].iloc[0]
    rows=[]
    for sample_count, group in convergence.groupby("sample_count"):
        quality=group.groupby("seed")[["ess","max_weight","top5_weight"]].first()
        medians=group.pivot(index="parameter",columns="seed",values="median")
        rows.append({"scenario":"previous_classifier_FP_teammate","observation_count":361,
                     "run_count":191,"programme_block_count":55,"rank":int(architecture["rank"]),
                     "nullity":int(architecture["nullity"]),"condition_number":architecture.condition_number,
                     "minimum_normalized_singular":architecture.minimum_normalized_singular,
                     "normalized_singular_spectrum":architecture.normalized_singular_values,
                     "sample_count":sample_count,"ess_min":quality.ess.min(),"ess_max":quality.ess.max(),
                     "max_weight_min":quality.max_weight.min(),"max_weight_max":quality.max_weight.max(),
                     "top5_weight_min":quality.top5_weight.min(),"top5_weight_max":quality.top5_weight.max(),
                     "maximum_parameter_median_seed_span":(medians.max(axis=1)-medians.min(axis=1)).max(),
                     "mean_p10_p90_width":group.groupby("parameter").width.mean().mean()})
    return pd.DataFrame(rows)


def longrun_failure_audit(ledger, groups):
    config = classifier.ClassifierConfig(); rows = []
    targets = ledger[(ledger.current_label.eq("Uncertain"))
                     & ledger.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability")]
    for row in targets.itertuples():
        group = groups[row.run_id].sort_values("LapNumber")
        representative = group[group.Representative]
        rep_numbers = representative.LapNumber.astype(int).tolist()
        gaps = np.diff(rep_numbers) if len(rep_numbers) > 1 else np.array([])
        max_gap = int(gaps.max()) if len(gaps) else 0
        criteria = {
            "same_compound": bool(row.same_compound),
            "min_consecutive": row.longest_consecutive_representative >= config.long_run_min_consecutive_representative_laps,
            "pace_limit": pd.notna(row.maximum_representative_relative_pace) and row.maximum_representative_relative_pace <= config.long_run_relative_pace_limit,
            "variability": pd.notna(row.representative_pace_cv) and row.representative_pace_cv <= config.long_run_variability_limit,
            "representative_fraction": row.representative_fraction >= config.long_run_min_representative_fraction,
            "no_attack_cycle": row.fast_slow_fast_cycles == 0,
        }
        failed = [name for name, passed in criteria.items() if not passed]
        plausible = (row.same_compound and row.representative_laps >= 4
                     and (pd.isna(row.representative_pace_cv) or row.representative_pace_cv <= .04)
                     and row.fast_slow_fast_cycles == 0)
        rows.append({"session": row.session, "team": row.team, "driver": row.driver,
                     "run_id": row.run_id, "stint": row.stint, "compound": row.compound,
                     "lap_numbers": row.lap_number_sequence, "lap_times_s": row.lap_time_sequence_s,
                     "representative_mask": "|".join("1" if value else "0" for value in group.Representative),
                     "pit_out_mask": "|".join("1" if pd.notna(value) else "0" for value in group.PitOutTime),
                     "pit_in_mask": "|".join("1" if pd.notna(value) else "0" for value in group.PitInTime),
                     "tyre_life_sequence": row.tyre_life_sequence,
                     "same_compound_pass": criteria["same_compound"],
                     "consecutive_representative_laps": row.longest_consecutive_representative,
                     "maximum_representative_gap_lapnumber": max_gap,
                     "representative_fraction": row.representative_fraction,
                     "representative_pace_cv": row.representative_pace_cv,
                     "fast_slow_fast_cycles": row.fast_slow_fast_cycles,
                     "failed_criteria": "|".join(failed), "failed_criterion_count": len(failed),
                     "visually_plausible_longrun_heuristic": plausible,
                     "threshold_min_consecutive": config.long_run_min_consecutive_representative_laps,
                     "threshold_max_gap_lapnumber": config.long_run_max_internal_gap_laps,
                     "threshold_min_representative_fraction": config.long_run_min_representative_fraction,
                     "threshold_max_pace_cv": config.long_run_variability_limit,
                     "threshold_max_relative_pace": config.long_run_relative_pace_limit})
    return pd.DataFrame(rows)


def short_run_audit(ledger):
    short = ledger[(ledger.total_physical_laps.le(2)) | (ledger.total_timed_laps.le(2))].copy()
    short["micro_test_influences_model"] = short.revised_label.ne("Uncertain")
    short["micro_test_reason"] = np.where(short.micro_test_influences_model,
                                          "passes usable programme despite <=2 physical/timed laps",
                                          "excluded as Uncertain")
    return short


def manual_priority(ledger, sensitivity):
    result = ledger.copy()
    changed = result.current_label.ne(result.revised_label)
    near = result.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability")
    rare = result.rare_hard_observation | result.rare_compound_long_run
    result["review_tier"] = np.select(
        [(changed & result.model_critical) | (near & result.current_label.eq("LongRun")),
         rare | near | result.current_long_run_evidence,
         result.model_critical], ["Tier 1", "Tier 2", "Tier 3"], default="Not prioritised")
    result["review_reason"] = result.apply(lambda row: "|".join(filter(None, [
        "label_changed" if row.current_label != row.revised_label else "",
        "near_longrun_failure" if row.uncertain_breakdown == "NearLongRunFailedContinuityOrVariability" else "",
        row.model_critical_reason])), axis=1)
    return result[result.review_tier.ne("Not prioritised")].copy()


def combined_time(full):
    result = full.copy(); offsets = {}; current = 0
    for session in prototype.SESSIONS:
        offsets[session] = current
        maximum = result.loc[result.Session.eq(session), "SessionSeconds"].max()
        current += maximum + 600
    result["weekend_time_min"] = [(offsets[session] + seconds) / 60
                                  for session, seconds in zip(result.Session, result.SessionSeconds)]
    return result, offsets


def plot_timeline(full, ledger):
    data, offsets = combined_time(full)
    label_map = ledger.set_index("run_id").revised_label
    fig, ax = plt.subplots(figsize=(24, 12))
    for run_id, group in data.groupby("run_id"):
        label = label_map[run_id]; team = group.Team.iloc[0]
        timed = group[group.LapTimeSeconds.notna()]
        ax.plot(timed.weekend_time_min, timed.LapTimeSeconds, color=TEAM_COLORS[team], alpha=.55, lw=1)
        ax.scatter(timed.weekend_time_min, timed.LapTimeSeconds, color=TEAM_COLORS[team],
                   marker=LABEL_MARKERS[label], s=18, alpha=.8)
        if len(timed):
            mid = timed.iloc[len(timed)//2]
            short_id = f"{mid.Driver}-{int(group.Stint.iloc[0])}"
            ax.annotate(short_id, (mid.weekend_time_min, mid.LapTimeSeconds), fontsize=5, alpha=.8)
    boundaries = [(offsets[s]/60, s) for s in prototype.SESSIONS]
    for x, session in boundaries: ax.axvline(x, color="black", ls="--", lw=1); ax.text(x+1, 72, session, weight="bold")
    ax.set_ylim(70, min(160, data.LapTimeSeconds.quantile(.995)))
    ax.set(title="R11 FP weekend: every pit-to-pit stint (labels: Driver–Stint)", xlabel="Concatenated FP1 → FP2 → FP3 time (min)", ylabel="Lap time (s)")
    handles = [Line2D([0],[0], color=color, lw=3, label=team) for team,color in TEAM_COLORS.items()]
    handles += [Line2D([0],[0], color="black", marker=marker, ls="", label=label) for label,marker in LABEL_MARKERS.items()]
    ax.legend(handles=handles, ncol=4, fontsize=8, loc="upper right")
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_weekend_timeline_overview.png", dpi=180); plt.close(fig)

    fig, axes = plt.subplots(3, 1, figsize=(24, 18), sharey=True)
    for ax, session in zip(axes, prototype.SESSIONS):
        session_data = full[full.Session.eq(session)]
        for run_id, group in session_data.groupby("run_id"):
            label=label_map[run_id]; team=group.Team.iloc[0]; timed=group[group.LapTimeSeconds.notna()]
            ax.plot(timed.SessionSeconds/60, timed.LapTimeSeconds, color=TEAM_COLORS[team], alpha=.55)
            ax.scatter(timed.SessionSeconds/60, timed.LapTimeSeconds, color=TEAM_COLORS[team], marker=LABEL_MARKERS[label], s=22)
            if len(timed):
                mid=timed.iloc[len(timed)//2]; ax.annotate(f"{mid.Driver}-{int(group.Stint.iloc[0])}", (mid.SessionSeconds/60,mid.LapTimeSeconds),fontsize=6)
        ax.set_title(session); ax.set_ylabel("Lap time (s)"); ax.grid(alpha=.2)
    axes[-1].set_xlabel("Session time (min)"); axes[0].set_ylim(70,min(160,full.LapTimeSeconds.quantile(.995)))
    fig.suptitle("R11 physical stint timelines by session — team colour, programme marker, Driver–Stint label")
    fig.tight_layout(); fig.savefig(OUTPUT/"fp_weekend_timeline_by_session.png",dpi=180); plt.close(fig)


def panel_plot(run_ids, groups, filename, title, columns=5):
    run_ids = list(run_ids); rows = int(np.ceil(len(run_ids)/columns))
    fig, axes = plt.subplots(rows, columns, figsize=(4.5*columns, 3.5*rows), squeeze=False)
    for ax, run_id in zip(axes.flat, run_ids):
        group=groups[run_id].sort_values("LapNumber"); timed=group[group.LapTimeSeconds.notna()]
        ax.plot(timed.LapNumber,timed.LapTimeSeconds,color=TEAM_COLORS.get(group.Team.iloc[0],"grey"),marker="o",alpha=.7)
        rep=timed[timed.Representative]; ax.scatter(rep.LapNumber,rep.LapTimeSeconds,facecolors="none",edgecolors="black",s=70,label="representative")
        for row in timed.itertuples():
            role="P" if row.Representative and row.RelativePace<=1.03 else ("R" if row.Representative else "O")
            ax.annotate(role,(row.LapNumber,row.LapTimeSeconds),fontsize=7,xytext=(0,5),textcoords="offset points")
        ax.set_title(run_id.replace(":"," "),fontsize=8); ax.grid(alpha=.2); ax.set_xlabel("LapNumber"); ax.set_ylabel("s")
    for ax in axes.flat[len(run_ids):]: ax.set_visible(False)
    fig.suptitle(title+"\nP=push, R=representative non-push, O=other; circles outline representative")
    fig.tight_layout(); fig.savefig(OUTPUT/filename,dpi=180); plt.close(fig)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    laps, ledger, groups, full, raw_data = revised_inputs()
    fp_teammate = prototype.teammate_estimates(raw_data)
    q_all, q_selected = q_teammate_inputs(raw_data, fp_teammate)
    comparison = comparison_table(fp_teammate, q_selected)
    data_fp = apply_teammate(raw_data, fp_teammate)
    data_q = apply_teammate(raw_data, q_selected)
    # Preserve the previously estimated mismatch scale as explicitly required.
    sigma_programme = float(pd.read_csv(OUTPUT/"fp_programme_mismatch_estimate.csv").robust_sigma_programme_s.iloc[0])
    diagnostics = pd.DataFrame([model_diagnostics(data_fp, fp_teammate, sigma_programme, "revised_classifier_FP_teammate"),
                                model_diagnostics(data_q, q_selected, sigma_programme, "revised_classifier_Q_preferred")])
    convergence = run_mc_comparison(data_fp, data_q, fp_teammate, q_selected, sigma_programme)
    summary = pd.concat([previous_prototype_summary(), summarize_prototype(diagnostics, convergence)],
                        ignore_index=True)
    failures = longrun_failure_audit(ledger, groups)
    short = short_run_audit(ledger)
    sensitivity = pd.read_csv(OUTPUT/"fp_programme_classifier_sensitivity.csv")
    priority = manual_priority(ledger, sensitivity)
    synthetic = bounded_synthetic(data_q, sigma_programme, q_selected)

    comparison.to_csv(OUTPUT/"fp_programmeblock_q_teammate_comparison.csv",index=False)
    q_all.to_csv(OUTPUT/"fp_programmeblock_q_teammate_uncertainty.csv",index=False)
    summary.to_csv(OUTPUT/"fp_programmeblock_r11_revised_prototype_comparison.csv",index=False)
    convergence.to_csv(OUTPUT/"fp_programmeblock_r11_revised_mc_convergence.csv",index=False)
    diagnostics.to_csv(OUTPUT/"fp_programmeblock_r11_revised_identifiability.csv",index=False)
    synthetic.to_csv(OUTPUT/"fp_programmeblock_r11_revised_synthetic_recovery.csv",index=False)
    failures.to_csv(OUTPUT/"fp_programmeblock_longrun_failure_audit.csv",index=False)
    short.to_csv(OUTPUT/"fp_programmeblock_short_run_audit.csv",index=False)
    priority.to_csv(OUTPUT/"fp_programmeblock_manual_review_priority.csv",index=False)
    plot_timeline(full, ledger)
    panel_plot(failures.run_id,groups,"fp_longrun_failure_panels.png","Current-Uncertain near-LongRun failures",columns=5)
    panel_plot(priority[priority.review_tier.eq("Tier 1")].run_id,groups,"fp_manual_review_tier1.png","Manual review Tier 1",columns=4)
    panel_plot(priority[priority.review_tier.eq("Tier 2")].run_id,groups,"fp_manual_review_tier2.png","Manual review Tier 2",columns=5)
    print("observations",len(raw_data),"runs",raw_data.run_id.nunique(),"blocks",raw_data.programme_block.nunique())
    print("Q selected",q_selected.calibration_choice.value_counts().to_dict())
    print("near LongRun",len(failures),"short usable",int(short.micro_test_influences_model.sum()))
    print(summary.to_string(index=False))


if __name__ == "__main__": main()
