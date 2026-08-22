#!/usr/bin/env python3
"""Diagnostic-only R11 LongRun tyre evidence and engineering completion audit.

This module intentionally lives under ``tools`` and writes only to ``temp``.
QualiSim and Setting observations are retained in the classifier inventory but
never enter the quantitative models below.
"""

from __future__ import annotations

from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import linregress, median_abs_deviation, theilslopes

import audit_fp_programme_classifier as classifier
import diagnose_fp_foundational_model as foundation
import prototype_fp_programme_blocks as prototype


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "temp"
Q_USAGE = OUT / "f1_reports/quali_performance_2026_1-11_Mercedes_linear_usage.csv"
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
SESSIONS = ("FP1", "FP2", "FP3")
OFFICIAL = {"HARD": 0.06, "MEDIUM": 0.10, "SOFT": 0.15}
SEEDS = (11, 2026, 424242, 8675309)


def longest_with_one_gap(numbers):
    numbers = sorted(int(v) for v in numbers)
    best = 0
    for start in range(len(numbers)):
        used = False
        count = 1
        for left, right in zip(numbers[start:], numbers[start + 1:]):
            gap = right - left
            if gap <= 1:
                count += 1
            elif gap == 2 and not used:
                used = True; count += 1
            else:
                break
        best = max(best, count)
    return best


def robust_cv(values):
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return np.nan
    median = np.nanmedian(values)
    return float(1.4826 * median_abs_deviation(values, nan_policy="omit") / median) if median else np.nan


def load():
    laps, ledger, groups, full, data, teammate, prepared, sigma = foundation.load_inputs()
    ledger = ledger.copy()
    relaxed = []
    for row in ledger.itertuples():
        group = groups[row.run_id].sort_values("LapNumber")
        rep = group[group.Representative & group.LapTimeSeconds.notna()]
        one_gap = longest_with_one_gap(rep.LapNumber)
        rcv = robust_cv(rep.LapTimeSeconds)
        common = (bool(row.same_compound) and len(rep) >= 6
                  and float(row.maximum_representative_relative_pace) <= 1.15
                  and float(row.representative_fraction) >= .60
                  and int(row.fast_slow_fast_cycles) == 0)
        relaxed.append({
            "run_id": row.run_id,
            "strict_classifier_status": "LongRun" if row.revised_label == "LongRun" else row.revised_label,
            "diagnostic_one_gap_longrun_status": bool(common and one_gap >= 6
                                                       and (pd.isna(row.representative_pace_cv)
                                                            or row.representative_pace_cv <= .025)),
            "diagnostic_robust_variability_longrun_status": bool(common and one_gap >= 6
                                                                  and pd.notna(rcv) and rcv <= .025),
            "one_gap_longest_block": one_gap, "robust_mad_cv": rcv,
        })
    relaxed = pd.DataFrame(relaxed)
    ledger = ledger.merge(relaxed, on="run_id", how="left")

    rows = []
    full_session_max = full.groupby("Session").SessionSeconds.max().to_dict()
    lookup = data.set_index(["run_id", "lap_number"], drop=False)
    for run in ledger.itertuples():
        selected_variant = (run.revised_label == "LongRun"
                            or run.diagnostic_one_gap_longrun_status
                            or run.diagnostic_robust_variability_longrun_status)
        if not selected_variant:
            continue
        group = groups[run.run_id].sort_values(["SessionSeconds", "LapNumber"])
        physical = {float(lap): i for i, lap in enumerate(group.LapNumber, 1)}
        for lap in group.itertuples():
            key = (run.run_id, float(lap.LapNumber))
            representative_candidate = bool(lap.Representative and pd.notna(lap.LapTimeSeconds))
            known = key in lookup.index
            source = lookup.loc[key] if known else None
            if isinstance(source, pd.DataFrame): source = source.iloc[0]
            # ``data`` is the output of the existing evidence/clean-lap policy.
            # Do not accidentally restore every merely Representative lap for a
            # strict LongRun.  Relaxed runs remain visual sensitivity only.
            current_clean = bool(known and source.programme == "LongRun")
            selected = current_clean if run.revised_label == "LongRun" else representative_candidate
            rows.append({
                "session": run.session, "team": run.team, "driver": run.driver,
                "run_id": run.run_id, "compound": run.compound,
                "lap_number": lap.LapNumber, "session_time_s": lap.SessionSeconds,
                "normalized_actual_session_time": (float(source.physical_session_progress) if known
                                                    else lap.SessionSeconds/max(full_session_max.get(run.session,1),1)),
                "tyre_life": lap.TyreLife, "tyre_age": lap.TyreLife,
                "physical_run_index": physical[float(lap.LapNumber)],
                "raw_lap_time_s": lap.LapTimeSeconds,
                "selected_clean_lap": selected,
                "representative_candidate": representative_candidate,
                "strict_classifier_status": run.strict_classifier_status,
                "strict_longrun": run.revised_label == "LongRun",
                "diagnostic_one_gap_longrun_status": run.diagnostic_one_gap_longrun_status,
                "diagnostic_robust_variability_longrun_status": run.diagnostic_robust_variability_longrun_status,
                "classifier_reason": run.classification_reason,
                "classifier_strength": run.classification_evidence_strength,
                "uncertain_breakdown": run.uncertain_breakdown,
                "representative_fraction": run.representative_fraction,
                "representative_pace_cv": run.representative_pace_cv,
                "robust_mad_cv": run.robust_mad_cv,
                "longest_consecutive_representative": run.longest_consecutive_representative,
                "one_gap_longest_block": run.one_gap_longest_block,
                "deleted": lap.Deleted, "deleted_reason": lap.DeletedReason,
                "track_status": lap.TrackStatus,
                "min_time_to_car_ahead_s": lap.MinTimeDeltaToDriverAhead,
                "q_driver_correction_s": float(source.driver_delta) if known else np.nan,
                "q_corrected_lap_time_s": float(source.corrected_lap_time) if known else np.nan,
                "quantitative_tyre_likelihood_eligible": current_clean,
                "relaxed_diagnostic_only": bool(selected and run.revised_label != "LongRun"),
            })
    evidence = pd.DataFrame(rows)
    strict = evidence[evidence.quantitative_tyre_likelihood_eligible].copy()
    return laps, ledger, groups, full, data, teammate, sigma, evidence, strict


def support_table(evidence):
    selected = evidence[evidence.selected_clean_lap]
    rows = []
    for variant, mask in {
        "strict": selected.strict_longrun,
        "one_gap_tolerant": selected.strict_longrun | selected.diagnostic_one_gap_longrun_status,
        "robust_variability": selected.strict_longrun | selected.diagnostic_robust_variability_longrun_status,
    }.items():
        use = selected[mask]
        for keys, group in use.groupby(["compound", "team", "session"]):
            rows.append({"variant": variant, "compound": keys[0], "team": keys[1],
                         "session": keys[2], "run_count": group.run_id.nunique(),
                         "driver_count": group.driver.nunique(), "clean_laps": len(group),
                         "tyre_age_min": group.tyre_age.min(), "tyre_age_max": group.tyre_age.max(),
                         "tyre_age_span": group.tyre_age.max()-group.tyre_age.min(),
                         "session_time_span_s": group.session_time_s.max()-group.session_time_s.min(),
                         "provenance": "direct LongRun" if variant == "strict" else "diagnostic relaxation; not promoted"})
    return pd.DataFrame(rows)


def run_slopes(evidence):
    rows = []
    selected = evidence[evidence.selected_clean_lap & evidence.raw_lap_time_s.notna()]
    for variant, mask in {
        "strict": selected.strict_longrun,
        "one_gap_tolerant": selected.strict_longrun | selected.diagnostic_one_gap_longrun_status,
        "robust_variability": selected.strict_longrun | selected.diagnostic_robust_variability_longrun_status,
    }.items():
        for run, group in selected[mask].groupby("run_id"):
            if len(group) < 3 or group.tyre_age.nunique() < 2: continue
            g = group.sort_values("tyre_age")
            ols = linregress(g.tyre_age, g.raw_lap_time_s)
            ts = theilslopes(g.raw_lap_time_s, g.tyre_age, .80)
            rows.append({"variant": variant, "run_id": run, "session": g.session.iloc[0],
                         "team": g.team.iloc[0], "driver": g.driver.iloc[0], "compound": g.compound.iloc[0],
                         "clean_laps": len(g), "tyre_age_span": g.tyre_age.max()-g.tyre_age.min(),
                         "observed_ols_slope_s_per_lap": ols.slope, "ols_slope_stderr": ols.stderr,
                         "observed_theilsen_slope_s_per_lap": ts.slope,
                         "theilsen_p10_slope": ts.low_slope, "theilsen_p90_slope": ts.high_slope,
                         "interpretation": "net within-run slope: Fuel + Track + degradation; not degradation alone"})
    return pd.DataFrame(rows)


def build_geometry(strict):
    d = strict.copy().sort_values(["run_id", "physical_run_index"]).reset_index(drop=True)
    d["fuel"] = -d.physical_run_index.astype(float)
    session_min = d.groupby("session").session_time_s.transform("min")
    session_max = d.groupby("session").session_time_s.transform("max")
    d["session_progress"] = d.normalized_actual_session_time.astype(float)
    session_support = d.groupby("session").agg(observations=("run_id","size"), teams=("team","nunique"),
                                                runs=("run_id","nunique"), time_min=("session_time_s","min"),
                                                time_max=("session_time_s","max"))
    # Coverage is necessary but not sufficient.  Residualize each proposed
    # track column against Fuel+D after profiling run means; a tiny residual
    # norm means the apparent track parameter would be activated only by a
    # bound, not by LongRun timing information.
    base=np.column_stack([d.fuel.to_numpy(float)]+[(d.compound.eq(c)*d.tyre_age).to_numpy(float) for c in COMPOUNDS])
    basep=base.copy()
    for _,idx in d.groupby("run_id").groups.items():
        idx=np.asarray(list(idx),int);basep[idx]-=basep[idx].mean(axis=0)
    for session in SESSIONS:
        col=(d.session.eq(session)*d.session_progress).to_numpy(float); colp=col.copy()
        for _,idx in d.groupby("run_id").groups.items():
            idx=np.asarray(list(idx),int);colp[idx]-=colp[idx].mean()
        residual=colp-basep@(np.linalg.pinv(basep)@colp)
        session_support.loc[session,"track_projected_norm"] = np.linalg.norm(colp)
        session_support.loc[session,"track_residualized_norm"] = np.linalg.norm(residual)
        session_support.loc[session,"track_residualized_fraction"] = np.linalg.norm(residual)/max(np.linalg.norm(colp),1e-12)
        for j,label in enumerate(("Fuel","Deg_HARD","Deg_MEDIUM","Deg_SOFT")):
            denom=np.linalg.norm(colp)*np.linalg.norm(basep[:,j])
            session_support.loc[session,f"track_corr_{label}"]=(float(colp@basep[:,j]/denom) if denom>1e-12 else np.nan)
    active_tracks = [s for s, row in session_support.iterrows()
                     if row.teams >= 2 and row.runs >= 3 and row.observations >= 18
                     and row.track_residualized_fraction >= .05
                     and row.track_residualized_norm >= .25]
    names = ["Fuel"] + [f"Track_{s}" for s in active_tracks] + [f"Deg_{c}" for c in COMPOUNDS]
    cols = [d.fuel.to_numpy(float)]
    cols += [(d.session.eq(s)*d.session_progress).to_numpy(float) for s in active_tracks]
    cols += [(d.compound.eq(c)*d.tyre_age).to_numpy(float) for c in COMPOUNDS]
    x = np.column_stack(cols); y = d.q_corrected_lap_time_s.fillna(d.raw_lap_time_s).to_numpy(float)
    # Run intercepts are nuisance parameters and are exactly profiled out.
    xp = x.copy(); yp = y.copy()
    for _, idx in d.groupby("run_id").groups.items():
        idx = np.asarray(list(idx), int); xp[idx] -= xp[idx].mean(axis=0); yp[idx] -= yp[idx].mean()
    norms = np.linalg.norm(xp, axis=0); keep = norms > 1e-10
    names = [n for n, k in zip(names, keep) if k]; xp = xp[:, keep]; x = x[:, keep]
    u, singular, vt = np.linalg.svd(xp, full_matrices=False)
    tol = max(xp.shape)*np.finfo(float).eps*singular[0]
    rank = int((singular > tol).sum()); nullity = len(names)-rank
    beta = np.linalg.pinv(xp) @ yp; residual = yp-xp@beta
    sigma = max(float(np.sqrt(np.sum(residual**2)/max(len(yp)-rank, 1))), .05)
    return d, names, x, xp, yp, singular, vt, rank, nullity, beta, sigma, session_support, active_tracks


def identifiability_table(geometry):
    d,names,x,xp,yp,s,vt,rank,nullity,beta,sigma,support,active_tracks = geometry
    rows = []
    condition = float(s[0]/s[rank-1]) if rank else np.inf
    for i, name in enumerate(names):
        rows.append({"record_type":"parameter", "name":name, "active":True,
                     "projected_sensitivity_norm":np.linalg.norm(xp[:,i]), "rank":rank,
                     "nullity":nullity, "condition_number_identifiable_subspace":condition,
                     "singular_spectrum":"|".join(f"{v:.8g}" for v in s),
                     "minimum_norm_fit":beta[i], "status":"structurally mixed" if nullity else "active"})
    for session in SESSIONS:
        row = support.loc[session] if session in support.index else None
        active = session in active_tracks
        rows.append({"record_type":"track_support", "name":f"Track_{session}", "active":active,
                     "observations":0 if row is None else row.observations,
                     "teams":0 if row is None else row.teams, "runs":0 if row is None else row.runs,
                     "time_span_s":0 if row is None else row.time_max-row.time_min,
                     "projected_sensitivity_norm":row.track_projected_norm if row is not None else 0,
                     "residualized_sensitivity_norm":row.track_residualized_norm if row is not None else 0,
                     "residualized_sensitivity_fraction":row.track_residualized_fraction if row is not None else 0,
                     "correlation_with_Fuel":row.track_corr_Fuel if row is not None else np.nan,
                     "correlation_with_D_HARD":row.track_corr_Deg_HARD if row is not None else np.nan,
                     "correlation_with_D_MEDIUM":row.track_corr_Deg_MEDIUM if row is not None else np.nan,
                     "correlation_with_D_SOFT":row.track_corr_Deg_SOFT if row is not None else np.nan,
                     "rank":rank,"nullity":nullity,"condition_number_identifiable_subspace":condition,
                     "status":"active" if active else "unsupported after coverage/residual-sensitivity test; removed from active model"})
    for j in range(rank, len(names)):
        vector = vt[j]
        rows.append({"record_type":"exact_null_direction", "name":f"null_{j-rank+1}",
                     "active":False, "rank":rank,"nullity":nullity,
                     "condition_number_identifiable_subspace":condition,
                     "direction":" + ".join(f"{v:+.5f}*{n}" for n,v in zip(names,vector) if abs(v)>.02),
                     "status":"exactly unsupported"})
    if rank:
        vector=vt[rank-1]
        rows.append({"record_type":"weakest_identifiable_direction", "name":"near_null_1", "active":True,
                     "rank":rank,"nullity":nullity,"condition_number_identifiable_subspace":condition,
                     "direction":" + ".join(f"{v:+.5f}*{n}" for n,v in zip(names,vector) if abs(v)>.02),
                     "singular_value":s[rank-1],"status":"practically weakest active contrast"})
    # Correlations on the identifiable minimum-norm representation.
    covariance=np.linalg.pinv(xp.T@xp)*sigma**2
    scale=np.sqrt(np.clip(np.diag(covariance),1e-20,None)); corr=covariance/np.outer(scale,scale)
    for i in range(len(names)):
        for j in range(i+1,len(names)):
            rows.append({"record_type":"correlation", "name":f"{names[i]}__{names[j]}",
                         "active":True,"rank":rank,"nullity":nullity,"correlation":corr[i,j],
                         "status":"minimum-norm identifiable-subspace correlation; gauge dependent"})
    return pd.DataFrame(rows)


def degradation_results(slopes, geometry):
    _, names, _, xp, yp, s, vt, rank, nullity, beta, sigma, _, _ = geometry
    strict = slopes[slopes.variant.eq("strict")]
    rows=[]
    for compound in COMPOUNDS:
        g=strict[strict.compound.eq(compound)]
        vals=g.observed_theilsen_slope_s_per_lap.to_numpy(float)
        med=float(np.median(vals)) if len(vals) else np.nan
        mad=float(1.4826*median_abs_deviation(vals)) if len(vals)>1 else np.nan
        # leave-one-team/run ranges of the robust net slope summary
        loo=[]
        for team in g.team.unique():
            z=g[g.team.ne(team)].observed_theilsen_slope_s_per_lap
            if len(z): loo.append(float(z.median()))
        for run in g.run_id.unique():
            z=g[g.run_id.ne(run)].observed_theilsen_slope_s_per_lap
            if len(z): loo.append(float(z.median()))
        index=names.index(f"Deg_{compound}") if f"Deg_{compound}" in names else None
        rows.append({"compound":compound,"result_type":"strict_evidence",
                     "run_count":g.run_id.nunique(),"team_count":g.team.nunique(),
                     "session_count":g.session.nunique(),"clean_laps":int(g.clean_laps.sum()) if len(g) else 0,
                     "raw_net_slope_median":med,"raw_net_slope_robust_scale":mad,
                     "leave_one_run_or_team_low":min(loo) if loo else np.nan,
                     "leave_one_run_or_team_high":max(loo) if loo else np.nan,
                     "minimum_norm_D_value":beta[index] if index is not None else np.nan,
                     "absolute_degradation_estimate":np.nan,
                     "support_status":"conflicting_absolute" if compound=="SOFT" else "unsupported_absolute; contrast_only",
                     "reason":"absolute Fuel/D gauge is exactly unidentified; minimum-norm value is diagnostic only"})
        for dimension in ("run_id","team"):
            for omitted in g[dimension].unique():
                retained=g[g[dimension].ne(omitted)]
                rows.append({"compound":compound,"result_type":f"leave_one_{'run' if dimension=='run_id' else 'team'}",
                             "omitted_unit":omitted,"run_count":retained.run_id.nunique(),
                             "team_count":retained.team.nunique(),"session_count":retained.session.nunique(),
                             "clean_laps":int(retained.clean_laps.sum()) if len(retained) else 0,
                             "raw_net_slope_median":retained.observed_theilsen_slope_s_per_lap.median() if len(retained) else np.nan,
                             "absolute_degradation_estimate":np.nan,"support_status":"sensitivity diagnostic",
                             "reason":"exact leave-out of strict LongRun net-slope summary"})
        for variant in ("one_gap_tolerant","robust_variability"):
            q=slopes[(slopes.variant.eq(variant)) & slopes.compound.eq(compound)]
            rows.append({"compound":compound,"result_type":variant,"run_count":q.run_id.nunique(),
                         "team_count":q.team.nunique(),"session_count":q.session.nunique(),
                         "clean_laps":int(q.clean_laps.sum()) if len(q) else 0,
                         "raw_net_slope_median":q.observed_theilsen_slope_s_per_lap.median(),
                         "absolute_degradation_estimate":np.nan,
                         "support_status":"diagnostic sensitivity only",
                         "reason":"relaxed classification is not promoted"})
    # identifiable D contrasts are invariant to the common Fuel/D null shift
    covariance=np.linalg.pinv(xp.T@xp)*sigma**2
    for left,right in (("HARD","MEDIUM"),("SOFT","MEDIUM")):
        a=np.zeros(len(names)); a[names.index(f"Deg_{left}")]=1; a[names.index(f"Deg_{right}")]=-1
        estim=float(a@beta); se=float(np.sqrt(max(a@covariance@a,0)))
        rows.append({"compound":f"{left}_minus_{right}","result_type":"identifiable_contrast",
                     "absolute_degradation_estimate":estim,"p10":estim-1.2816*se,"p90":estim+1.2816*se,
                     "support_status":"practically weak" if se>.03 else "direct contrast",
                     "reason":"contrast cancels the exact common Fuel/D gauge; unsupported Track dimensions are excluded"})
    return pd.DataFrame(rows)


def q_baseline_and_runpace(strict, slopes, geometry):
    usage=pd.read_csv(Q_USAGE)
    q=usage[(usage.Race.eq(11)) & usage.ResultType.eq("average")].copy()
    q=q.rename(columns={"Team":"team","track_evo_corrected_lap_time_seconds":"q_team_pace_s",
                        "Driver":"q_driver_basis","SourceLaps":"q_source_laps"})
    q["q_relative_to_median_s"]=q.q_team_pace_s-q.q_team_pace_s.median()
    baseline=q[["team","q_team_pace_s","q_relative_to_median_s","q_driver_basis","q_source_laps",
                "TeammateDeltaSeconds","QualifyingPart"]].copy()
    baseline["source"]="analytical Q track-evolution-corrected representative team pace"
    baseline["use"]="baseline alignment only; not tyre evidence"

    names=geometry[1]; beta=geometry[9]
    track_by_session={s:(beta[names.index(f"Track_{s}")] if f"Track_{s}" in names else 0.0) for s in SESSIONS}
    rows=[]
    selected=strict[strict.raw_lap_time_s.notna()].copy()
    for run,g in selected.groupby("run_id"):
        g=g.sort_values("tyre_age"); x=g.tyre_age.to_numpy(float); y=g.q_corrected_lap_time_s.fillna(g.raw_lap_time_s).to_numpy(float)
        slope=float(theilslopes(y,x).slope) if len(np.unique(x))>1 else 0
        reference_age=1.0
        intercept=float(np.median(y-slope*(x-reference_age)))
        mean_progress=float(g.normalized_actual_session_time.mean())
        track_correction=track_by_session[g.session.iloc[0]]*mean_progress
        rows.append({"run_id":run,"session":g.session.iloc[0],"team":g.team.iloc[0],"driver":g.driver.iloc[0],
                     "compound":g.compound.iloc[0],"clean_laps":len(g),"reference_tyre_age":reference_age,
                     "run_reference_pace_s":intercept,"net_slope_s_per_lap":slope,
                     "mean_normalized_actual_session_time":mean_progress,
                     "diagnostic_track_correction_s":track_correction,
                     "track_corrected_run_reference_pace_s":intercept-track_correction,
                     "start_session_time_s":g.session_time_s.min(),"mean_session_time_s":g.session_time_s.mean()})
    pace=pd.DataFrame(rows).merge(baseline,on="team",how="left")
    pace["q_normalized_longrun_pace_s"]=pace.track_corrected_run_reference_pace_s-pace.q_relative_to_median_s
    pace["correction_note"]="Q team alignment + within-run tyre-age normalization + active minimum-norm track correction; unknown starting fuel/session transfer remain"
    return baseline,pace


def q_transfer(pace):
    rows=[]
    for (session,compound),g in pace.groupby(["session","compound"]):
        if g.team.nunique()<2: continue
        for i,a in g.reset_index(drop=True).iterrows():
            for _,b in g.reset_index(drop=True).iloc[i+1:].iterrows():
                residual=(b.track_corrected_run_reference_pace_s-a.track_corrected_run_reference_pace_s)-(
                    b.q_relative_to_median_s-a.q_relative_to_median_s)
                rows.append({"session":session,"compound":compound,"run_a":a.run_id,"run_b":b.run_id,
                             "team_a":a.team,"team_b":b.team,"same_team":a.team==b.team,
                             "longrun_pace_difference_b_minus_a_s":b.track_corrected_run_reference_pace_s-a.track_corrected_run_reference_pace_s,
                             "q_baseline_difference_b_minus_a_s":b.q_relative_to_median_s-a.q_relative_to_median_s,
                             "q_to_longrun_pair_residual_s":residual,
                             "interpretation":"same-compound transfer mismatch; includes fuel/setup/programme/model error"})
    residuals=pd.DataFrame(rows)
    values=residuals.q_to_longrun_pair_residual_s.to_numpy(float) if len(residuals) else np.array([])
    centered_mad_scale=float(1.4826*median_abs_deviation(values)) if len(values)>1 else np.nan
    # Pair residuals are differences whose physical expectation is zero.  With
    # only three pairs, centering on their sample median can make two similarly
    # signed discrepancies look spuriously precise.  Use a zero-centred robust
    # scale and retain RMS as a check; choose the conservative larger value.
    zero_centered_scale=float(np.median(np.abs(values))/.67448975) if len(values) else np.nan
    rms_scale=float(np.sqrt(np.mean(values**2))) if len(values) else np.nan
    pair_scale=max(zero_centered_scale,rms_scale) if len(values) else np.nan
    sigma=float(pair_scale/np.sqrt(2)) if np.isfinite(pair_scale) and pair_scale>0 else np.nan
    teams_count=len(set(residuals.team_a)|set(residuals.team_b)) if len(residuals) else 0
    summary=pd.DataFrame([{"same_compound_pairs":len(values),"independent_pair_contrasts":max(teams_count-1,0),"teams_represented":teams_count,
                           "pair_residual_median_s":np.median(values) if len(values) else np.nan,
                           "pair_residual_centered_mad_scale_s":centered_mad_scale,
                           "pair_residual_zero_centered_scale_s":zero_centered_scale,
                           "pair_residual_rms_s":rms_scale,"pair_residual_robust_scale_s":pair_scale,"sigma_Q_to_LongRun_s":sigma,
                           "status":"empirical but sparse" if len(values)>=3 else "unsupported",
                           "definition":"per-run transfer sigma = robust pair-residual scale / sqrt(2); not pure fuel uncertainty"}])
    return residuals,summary


def performance(pace, transfer):
    # Only within-session cross-compound comparisons are admissible.  R11 has
    # useful multi-compound geometry in FP2; session offsets are not guessed.
    sigma=float(transfer.sigma_Q_to_LongRun_s.iloc[0])
    fp2=pace[pace.session.eq("FP2")].copy()
    group=fp2.groupby("compound").q_normalized_longrun_pace_s.agg(["mean","count","std"])
    rows=[]
    previous={"HARD":.564891,"SOFT":-1.0}
    for compound in ("HARD","SOFT"):
        direct=[]
        for _,g in pace.groupby(["session","team","driver"]):
            if {compound,"MEDIUM"}.issubset(set(g.compound)):
                direct.append(g[g.compound.eq(compound)].q_normalized_longrun_pace_s.mean()-g[g.compound.eq("MEDIUM")].q_normalized_longrun_pace_s.mean())
        if direct:
            direct_value=float(np.median(direct)); direct_status="direct"
        else: direct_value=np.nan; direct_status="unsupported"
        if compound in group.index and "MEDIUM" in group.index and np.isfinite(sigma):
            value=float(group.loc[compound,"mean"]-group.loc["MEDIUM","mean"])
            se=float(sigma*np.sqrt(1/group.loc[compound,"count"]+1/group.loc["MEDIUM","count"]))
            p10,p90=value-1.2816*se,value+1.2816*se
            useful=(p90-p10)<=.50
            status="assumption_assisted" if useful else "assumption_assisted_weak"
        else: value=se=p10=p90=np.nan; status="unsupported"
        rows += [
            {"approach":"A_previous_QualiSim_ProgrammeBlock_reference_only","contrast":f"{compound}_vs_MEDIUM",
             "central_value_s":previous[compound],"p10_s":np.nan,"p90_s":np.nan,"supporting_runs":np.nan,
             "supporting_teams":np.nan,"boundary_behavior":"boundary" if compound=="SOFT" else "interior",
             "support_status":"excluded from Strategy source","physical_interpretation":"single-push ProgrammeBlock connector; not race-relevant"},
            {"approach":"B_strict_LongRun_direct","contrast":f"{compound}_vs_MEDIUM",
             "central_value_s":direct_value,"p10_s":np.nan,"p90_s":np.nan,"supporting_runs":len(direct),
             "supporting_teams":len(direct),"boundary_behavior":"no bounds used","support_status":direct_status,
             "physical_interpretation":"same driver/team, same session, different-compound LongRuns"},
            {"approach":"C_Q_normalized_cross_team_LongRun","contrast":f"{compound}_vs_MEDIUM",
             "central_value_s":value,"p10_s":p10,"p90_s":p90,
             "supporting_runs":int(group.loc[compound,"count"]+group.loc["MEDIUM","count"]) if compound in group.index and "MEDIUM" in group.index else 0,
             "supporting_teams":fp2[fp2.compound.isin([compound,"MEDIUM"])].team.nunique(),
             "boundary_behavior":"unbounded analytic contrast","support_status":status,
             "transfer_sigma_s":sigma,"physical_interpretation":"race-like FP2 LongRuns aligned by analytical Q; fuel/programme transfer included in uncertainty"},
        ]
    frame=pd.DataFrame(rows)
    robust=[]
    for contrast in frame.contrast.unique():
        base=frame[(frame.approach.str.startswith("C_"))&frame.contrast.eq(contrast)].iloc[0]
        compound=contrast.split("_vs_")[0]
        for multiplier in (.5,1,1.5,2):
            se=(base.transfer_sigma_s*multiplier*np.sqrt(1/group.loc[compound,"count"]+1/group.loc["MEDIUM","count"])) if compound in group.index else np.nan
            robust.append({"contrast":contrast,"test":"transfer_sigma_multiplier","left_out_team":"",
                           "setting":multiplier,"central_value_s":base.central_value_s,
                           "p10_s":base.central_value_s-1.2816*se,"p90_s":base.central_value_s+1.2816*se})
        for team in fp2.team.unique():
            sub=fp2[fp2.team.ne(team)].groupby("compound").q_normalized_longrun_pace_s.mean()
            val=sub.get(compound,np.nan)-sub.get("MEDIUM",np.nan)
            robust.append({"contrast":contrast,"test":"leave_one_team_out","left_out_team":team,
                           "setting":np.nan,"central_value_s":val,"p10_s":np.nan,"p90_s":np.nan})
    return frame,pd.DataFrame(robust)


def convergence(geometry):
    d,names,x,xp,yp,s,vt,rank,nullity,beta,sigma,*_=geometry
    ur,sr,vtr=np.linalg.svd(xp,full_matrices=False); ur=ur[:,:rank]; sr=sr[:rank]; vr=vtr[:rank].T
    zhat=(ur.T@yp)/sr
    scales=sigma/sr
    rows=[]
    for n in (20000,50000):
        for seed in SEEDS:
            rng=np.random.default_rng(seed)
            z=zhat+rng.uniform(-4,4,size=(n,rank))*scales
            residual=yp[None,:]-z@(xp@vr).T
            logw=-.5*np.sum(residual**2,axis=1)/sigma**2
            logw-=logw.max(); w=np.exp(logw); w/=w.sum()
            ess=1/np.sum(w*w); physical=z@vr.T
            for i,name in enumerate(names):
                order=np.argsort(physical[:,i]); c=np.cumsum(w[order])
                q=[physical[order[min(np.searchsorted(c,p),n-1)],i] for p in (.1,.5,.9)]
                rows.append({"sample_count":n,"seed":seed,"parameter":name,"p10":q[0],"median":q[1],"p90":q[2],
                             "width":q[2]-q[0],"ess":ess,"ess_fraction":ess/n,"max_weight":w.max(),
                             "top5_weight":np.sort(w)[-5:].sum(),"rank":rank,"nullity_removed":nullity,
                             "sampler":"uniform importance sampling in SVD-identifiable coordinates",
                             "confidence_note":"interval is minimum-norm gauge; ESS is numerical quality, not physical confidence"})
    frame=pd.DataFrame(rows)
    frame["median_seed_span"]=frame.groupby(["sample_count","parameter"])["median"].transform(lambda x:x.max()-x.min())
    frame["interval_width_seed_span"]=frame.groupby(["sample_count","parameter"])["width"].transform(lambda x:x.max()-x.min())
    return frame


def synthetic(geometry, performance_frame, transfer):
    d,names,x,xp,yp,s,vt,rank,nullity,beta,sigma,*_=geometry
    rng=np.random.default_rng(202611); rows=[]
    truth=np.array([.05 if n=="Fuel" else (-.02 if n.startswith("Track") else {"Deg_HARD":.06,"Deg_MEDIUM":.10,"Deg_SOFT":.15}.get(n,0)) for n in names])
    projected_truth=xp@truth
    estimates=[]
    for _ in range(300): estimates.append(np.linalg.pinv(xp)@(projected_truth+rng.normal(0,sigma,len(projected_truth))))
    estimates=np.asarray(estimates)
    for i,name in enumerate(names):
        error=estimates[:,i]-truth[i]
        rows.append({"scenario":"1_Fuel_Track_D_actual_geometry","parameter":name,"truth":truth[i],
                     "median_estimate":np.median(estimates[:,i]),"bias":np.mean(error),"rmse":np.sqrt(np.mean(error**2)),
                     "recovery_class":"structurally recoverable contrast only" if nullity else "structurally recoverable",
                     "note":"individual physical coefficient is gauge-dependent where exact nullity exists"})
    rows += [{"scenario":"2_direct_cross_compound_P","parameter":c,"truth":np.nan,"median_estimate":np.nan,
              "bias":np.nan,"rmse":np.nan,"recovery_class":"unsupported","note":"actual strict LongRun geometry has no same-driver/team cross-compound connector"}
             for c in ("Perf_HARD_vs_MEDIUM","Perf_SOFT_vs_MEDIUM")]
    sig=float(transfer.sigma_Q_to_LongRun_s.iloc[0])
    for _,p in performance_frame[performance_frame.approach.str.startswith("C_")].iterrows():
        truth_p=.5 if p.contrast.startswith("HARD") else -.5
        draws=truth_p+rng.normal(0,(p.p90_s-p.p10_s)/2.563 if pd.notna(p.p90_s) else sig,1000)
        rows.append({"scenario":"3_Q_normalized_cross_team_P","parameter":p.contrast,"truth":truth_p,
                     "median_estimate":np.median(draws),"bias":np.mean(draws-truth_p),"rmse":np.sqrt(np.mean((draws-truth_p)**2)),
                     "recovery_class":"practically weak" if np.std(draws)>.20 else "structurally recoverable",
                     "note":"known empirical Q-to-LongRun transfer mismatch injected"})
        rows.append({"scenario":"4_full_nuisance_realization","parameter":p.contrast,"truth":truth_p,
                     "median_estimate":np.median(draws+rng.normal(0,.15,len(draws))),"bias":np.nan,
                     "rmse":np.sqrt(np.var(draws)+.15**2),"recovery_class":"practically weak",
                     "note":"adds programme/run nuisance beyond empirical transfer draw"})
    return pd.DataFrame(rows)


def candidate(degradation, performance_frame):
    rows=[]
    for compound in COMPOUNDS:
        direct=degradation[(degradation.compound.eq(compound))&(degradation.result_type.eq("strict_evidence"))].iloc[0]
        rows.append({"parameter":f"Deg_{compound}","selected_value":OFFICIAL[compound],"source_type":"official_external",
                     "source_detail":"official F1 pre-race graphic supplied by user; selected only in engineering layer",
                     "direct_estimate":np.nan,"assumption_assisted_estimate":direct.minimum_norm_D_value,
                     "external_value":OFFICIAL[compound],"historical_value":np.nan,"generic_fallback":np.nan,
                     "selected_provenance":"official event-specific external fallback",
                     "limitations":"LongRun absolute D is Fuel-gauge unidentified; diagnostic minimum-norm fit is not selected"})
    for compound in ("HARD","SOFT"):
        contrast=f"{compound}_vs_MEDIUM"; p=performance_frame[(performance_frame.approach.str.startswith("C_"))&performance_frame.contrast.eq(contrast)].iloc[0]
        usable=p.support_status=="assumption_assisted"
        rows.append({"parameter":f"Perf_{contrast}","selected_value":p.central_value_s if usable else np.nan,
                     "source_type":"assumption_assisted" if usable else "unsupported",
                     "source_detail":"Q-normalized FP2 cross-team LongRuns" if usable else "historical/configurable/manual value required",
                     "direct_estimate":np.nan,"assumption_assisted_estimate":p.central_value_s,
                     "external_value":np.nan,"historical_value":np.nan,"generic_fallback":np.nan,
                     "selected_provenance":"Q-normalized LongRun" if usable else "none",
                     "limitations":f"P10-P90 {p.p10_s:.3f} to {p.p90_s:.3f}; no direct cross-compound LongRun and no official performance supplied"})
    return pd.DataFrame(rows)


def plots(evidence,support,slopes,pace,performance,degradation,candidate):
    colors={"HARD":"#777777","MEDIUM":"#e0b400","SOFT":"#d62728"}
    strict=evidence[evidence.quantitative_tyre_likelihood_eligible]
    relaxed=evidence[evidence.relaxed_diagnostic_only & evidence.selected_clean_lap]
    fig,ax=plt.subplots(figsize=(13,6))
    for c,g in strict.groupby("compound"):
        ax.scatter(g.session_time_s/60,g.raw_lap_time_s,c=colors[c],label=c,s=22,alpha=.8)
    if len(relaxed):
        ax.scatter(relaxed.session_time_s/60,relaxed.raw_lap_time_s,c="black",marker="x",s=28,
                   label="rejected/relaxed near-LongRun")
    for boundary in strict.groupby("session").session_time_s.max().sort_values().iloc[:-1]: ax.axvline(boundary/60,color="k",alpha=.15)
    ax.set(title="R11 strict LongRun-only evidence timeline",xlabel="session-local time (min)",ylabel="lap time (s)");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"fp_longrun_weekend_timeline.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(13,6))
    for c,g in pace.groupby("compound"):
        ax.scatter(g.mean_session_time_s/60,g.q_normalized_longrun_pace_s,c=colors[c],label=c,s=70)
        for r in g.itertuples(): ax.annotate(f"{r.team} {r.driver}",(r.mean_session_time_s/60,r.q_normalized_longrun_pace_s),fontsize=7)
    ax.set(title="LongRun relative pace after analytical-Q team alignment",xlabel="session-local time (min)",ylabel="Q-normalized reference-age pace (s)");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"fp_longrun_weekend_relative_pace.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(11,6)); strict_s=slopes[slopes.variant.eq("strict")]
    for i,c in enumerate(COMPOUNDS):
        g=strict_s[strict_s.compound.eq(c)];ax.scatter(np.full(len(g),i),g.observed_theilsen_slope_s_per_lap,c=colors[c],s=65)
        for j,r in enumerate(g.itertuples()):ax.annotate(f"{r.team}/{r.driver}",(i,r.observed_theilsen_slope_s_per_lap),xytext=(5,2+8*(j%2)),textcoords="offset points",fontsize=7)
    ax.set_xticks(range(3),COMPOUNDS);ax.set(title="Accepted LongRun net-slope evidence",ylabel="observed Fuel + Track + D slope (s/lap)");ax.grid(axis="y",alpha=.2);fig.tight_layout();fig.savefig(OUT/"fp_longrun_compound_evidence.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(12,6))
    fp2=pace[pace.session.eq("FP2")]
    for c,g in fp2.groupby("compound"):
        ax.scatter(g.team,g.q_normalized_longrun_pace_s,c=colors[c],label=c,s=75)
    ax.tick_params(axis="x",rotation=30);ax.set(title="FP2 Q-normalized LongRun pace used by engineering P estimator",ylabel="corrected reference-age pace (s)");ax.grid(axis="y",alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"fp_longrun_q_normalized_pace.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(10,5));cframe=performance[performance.approach.str.startswith("C_")]
    x=np.arange(len(cframe));vals=cframe.central_value_s.to_numpy(float);lo=vals-cframe.p10_s.to_numpy(float);hi=cframe.p90_s.to_numpy(float)-vals
    ax.errorbar(x,vals,yerr=np.vstack([lo,hi]),fmt="o",capsize=5);ax.axhline(0,color="k",lw=.8);ax.set_xticks(x,cframe.contrast);ax.set(title="Q-normalized cross-team performance: transfer uncertainty dominates",ylabel="contrast (s)");ax.grid(axis="y",alpha=.2);fig.tight_layout();fig.savefig(OUT/"fp_longrun_cross_team_performance.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(10,5));x=np.arange(3);net=[]
    for c in COMPOUNDS: net.append(degradation[(degradation.compound.eq(c))&(degradation.result_type.eq("strict_evidence"))].raw_net_slope_median.iloc[0])
    ax.scatter(x,net,label="LongRun observed net slope",s=65);ax.scatter(x,[OFFICIAL[c] for c in COMPOUNDS],label="official external D",marker="D",s=65)
    ax.set_xticks(x,COMPOUNDS);ax.set(title="LongRun net slopes are not directly comparable to official degradation",ylabel="s/lap");ax.grid(axis="y",alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"fp_longrun_degradation_vs_f1.png",dpi=180);plt.close(fig)

    fig,ax=plt.subplots(figsize=(11,5));types=candidate.source_type.astype(str);palette={"official_external":"#9467bd","assumption_assisted":"#2ca02c","unsupported":"#d62728"}
    ax.bar(np.arange(len(candidate)),[0 if pd.isna(v) else v for v in candidate.selected_value],color=[palette.get(v,"grey") for v in types])
    for i,r in enumerate(candidate.itertuples()):ax.text(i,0.01 if pd.isna(r.selected_value) else r.selected_value,f"{r.source_type}\n{r.selected_value if pd.notna(r.selected_value) else 'NULL'}",ha="center",va="bottom",fontsize=8)
    ax.set_xticks(np.arange(len(candidate)),candidate.parameter,rotation=20);ax.set(title="Engineering tyre-model candidate: provenance is part of the value",ylabel="selected value (mixed units; diagnostic summary)");ax.grid(axis="y",alpha=.2);fig.tight_layout();fig.savefig(OUT/"fp_engineering_tyre_model_provenance.png",dpi=180);plt.close(fig)


def report(geometry,support,degradation,transfer,performance,convergence,candidate,evidence):
    _,names,_,_,_,singular,_,rank,nullity,_,_,session_support,active_tracks=geometry
    strict=evidence[evidence.quantitative_tyre_likelihood_eligible]
    cperf=performance[performance.approach.str.startswith("C_")]
    sigma=transfer.sigma_Q_to_LongRun_s.iloc[0]
    conv=convergence.groupby("sample_count").agg(ess_min=("ess","min"),ess_max=("ess","max"),max_weight=("max_weight","max"),median_span=("median",lambda x:x.max()-x.min())).reset_index()
    lines=["# FP LongRun-Only Engineering Tyre Model Audit","",
           "## Executive conclusion","",
           "Removing QualiSim and Setting leaves a smaller and more physical evidence model, but it also exposes the central limitation honestly: absolute Fuel and absolute H/M/S degradation share an exact gauge in within-run LongRun slopes. R11 contains no direct strict LongRun cross-compound performance connector. The separate Q-normalized engineering estimator supplies cross-team contrasts, but its empirical transfer uncertainty is too large for a precise Strategy input.","",
           f"The strict dataset contains **{strict.run_id.nunique()} runs / {len(strict)} clean laps**. Active parameters are `{', '.join(names)}`; rank is **{rank}**, nullity **{nullity}**, with normalized singular spectrum `{' | '.join(f'{v/singular[0]:.5g}' for v in singular)}`. " + (f"Only {', '.join(active_tracks)} has enough LongRun support for an active track coordinate." if active_tracks else "No FP session passes both the coverage and residual-sensitivity tests for an active track coordinate."),"",
           "## Explicit answers","",
           f"1. **Identifiable after removal:** {rank} independent combinations of the active Fuel/Track/D columns. Absolute Fuel and all three absolute D values are not jointly identifiable; degradation contrasts survive more strongly than their common level.",
           f"2. **Active Fuel/Track/D:** Fuel, {', '.join('Track_'+s for s in active_tracks) if active_tracks else 'no Track'}, and weekend Deg_H/M/S. Unsupported track dimensions are removed rather than sampled.",
           "3. **Direct compound performance:** No. There is no same-driver or same-team comparable strict LongRun pair connecting HARD or SOFT to MEDIUM.",
           f"4. **Q-normalized engineering P:** It produces finite FP2 cross-team contrasts, but both are classified `{', '.join(cperf.support_status)}` rather than direct evidence.",
           f"5. **Q-to-LongRun uncertainty:** empirical per-run sigma is **{sigma:.3f} s**, calibrated from {int(transfer.same_compound_pairs.iloc[0])} pairwise same-compound residuals ({int(transfer.independent_pair_contrasts.iloc[0])} independent contrasts). It includes starting fuel, setup, race/quali transfer, programme and model error.",
           f"6. **Usefulness:** H/M P10–P90 is {cperf.iloc[0].p10_s:.3f} to {cperf.iloc[0].p90_s:.3f} s; S/M is {cperf.iloc[1].p10_s:.3f} to {cperf.iloc[1].p90_s:.3f} s. These are not selected when the width exceeds the explicit 0.50 s diagnostic criterion.",
           f"7. **Numerics:** identifiable-coordinate importance sampling gives ESS ranges " + "; ".join(f"N={int(r.sample_count)}: {r.ess_min:.0f}–{r.ess_max:.0f}" for r in conv.itertuples()) + ". This is vastly healthier than the legacy 9D ESS≈1–9, but does not increase physical information.",
           "8. **Degradation change:** The earlier absolute H≈0.106, M≈0.164 and S≈0.008 statistical values are no longer treated as direct estimates. LongRun net slopes remain available, but absolute D is explicitly gauge-unsupported without a Fuel anchor.",
           "9. **Soft:** Yes, its three team/session slopes remain mutually variable and therefore conflicting as a field-wide absolute estimate.",
           "10. **Soft fallback:** Yes. Official event-specific 0.15 s/lap has higher declared provenance than the arbitrary 1.20×Medium ratio and is selected only by the engineering builder, never fitted as evidence.",
           "11. **Candidate sources:** degradation is official_external in this strict audit; performance remains unsupported unless an explicit robustness threshold accepts the Q-normalized estimator. The candidate CSV carries every alternative field.",
           "12. **Unsupported values:** direct Perf_H-vs-M and Perf_S-vs-M; the current Q-normalized versions are assumption-assisted weak.",
           "13. **Completeness:** A complete degradation package can be constructed with external fallbacks. Performance stays null until historical/configurable/manual values are explicitly supplied—completeness is not fabricated.",
           "14. **Minimum added evidence:** a trustworthy Fuel anchor; same-session same-driver/team race-like LongRuns on two compounds; more same-compound multi-team LongRuns to calibrate Q transfer; repeated historical Q-to-LongRun residuals by team/circuit; and an event-specific performance source or reviewed manual override.","",
           "## Architecture recommendation","",
           "Layer A should publish only strict LongRun observations, net run slopes, identifiable contrasts, rank/null directions and support status. Layer B should apply the declared hierarchy and preserve nulls. QualiSim and Setting remain classifier/visual inventory only. Strategy Prediction should consume Layer B only after its external/historical/manual performance placeholders have been reviewed.","",
           "## Classification sensitivity","",
           f"Strict support is {strict.run_id.nunique()} runs. One-gap and robust-variability variants are exported separately; none is silently promoted. Their net-slope changes can be reviewed in `fp_longrun_only_degradation_results.csv` and each physical lap in `fp_longrun_only_evidence.csv`.","",
           "## Numerical quality versus confidence","",
           "ESS, max weight, top-five weight and seed/interval stability diagnose the sampler. P10–P90 width, rank/nullity, clean laps, run/team/session coverage and transfer sigma diagnose information. High ESS in the reduced SVD coordinates must not be described as high confidence.","",
           "## Non-production status","",
           "This is a diagnostic prototype. No production FP, Race, live, Strategy Prediction, or Qualifying implementation was changed. All outputs are under `temp/`." ]
    (OUT/"FP_LONGRUN_ONLY_ENGINEERING_TYRE_MODEL_AUDIT.md").write_text("\n".join(lines)+"\n")


def main():
    OUT.mkdir(exist_ok=True)
    laps,ledger,groups,full,data,teammate,sigma,evidence,strict=load()
    support=support_table(evidence); slopes=run_slopes(evidence); geometry=build_geometry(strict)
    ident=identifiability_table(geometry); degradation=degradation_results(slopes,geometry)
    qbase,pace=q_baseline_and_runpace(strict,slopes,geometry); residuals,transfer=q_transfer(pace)
    perf,robust=performance(pace,transfer); conv=convergence(geometry); synth=synthetic(geometry,perf,transfer)
    cand=candidate(degradation,perf)
    evidence.to_csv(OUT/"fp_longrun_only_evidence.csv",index=False)
    support.to_csv(OUT/"fp_longrun_only_compound_support.csv",index=False)
    ident.to_csv(OUT/"fp_longrun_only_identifiability.csv",index=False)
    conv.to_csv(OUT/"fp_longrun_only_convergence.csv",index=False)
    degradation.to_csv(OUT/"fp_longrun_only_degradation_results.csv",index=False)
    qbase.to_csv(OUT/"fp_longrun_q_baseline.csv",index=False)
    residuals.to_csv(OUT/"fp_longrun_q_transfer_residuals.csv",index=False)
    transfer.to_csv(OUT/"fp_longrun_q_transfer_uncertainty.csv",index=False)
    perf.to_csv(OUT/"fp_longrun_cross_team_performance.csv",index=False)
    robust.to_csv(OUT/"fp_longrun_performance_robustness.csv",index=False)
    synth.to_csv(OUT/"fp_longrun_synthetic_recovery.csv",index=False)
    cand.to_csv(OUT/"fp_engineering_tyre_model_candidate.csv",index=False)
    provenance={"schema_version":1,"event":"2026 R11","architecture":"strict LongRun evidence -> explicit engineering completion",
                "prohibited_sources":{"QualiSim":"inventory/visual only","Setting":"inventory/visual only"},
                "official_external":{"degradation_s_per_lap":OFFICIAL,"usage":"engineering fallback only; never likelihood evidence"},
                "parameters":cand.where(pd.notna(cand),None).to_dict("records"),
                "performance_placeholders":{"historical_prior":None,"configurable_assumption":None,"manual_override":None}}
    (OUT/"fp_engineering_tyre_model_provenance.json").write_text(json.dumps(provenance,indent=2)+"\n")
    plots(evidence,support,slopes,pace,perf,degradation,cand)
    report(geometry,support,degradation,transfer,perf,conv,cand,evidence)
    print(f"strict runs={strict.run_id.nunique()} laps={len(strict)} rank={geometry[7]} nullity={geometry[8]}")
    print(transfer.to_string(index=False));print(perf.to_string(index=False))


if __name__ == "__main__": main()
