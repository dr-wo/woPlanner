#!/usr/bin/env python3
"""Diagnostic-only reconciliation of R11 FP tyre evidence and model geometry."""

from __future__ import annotations

from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
from scipy.optimize import lsq_linear
from scipy.stats import linregress, theilslopes

import audit_fp_programme_classifier as classifier
import diagnose_fp_foundational_model as foundation
import diagnose_fp_degradation_evidence_team_variation as tyre
import prototype_fp_programme_blocks as prototype


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
OFFICIAL = {"HARD": .06, "MEDIUM": .10, "SOFT": .15}
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
SESSIONS = ("FP1", "FP2", "FP3")


def model_design(data):
    model = tyre.Model("current_reconciled_model", ("LongRun",))
    names, x, bounds, d_idx, p_idx, team_maps, support = tyre.build_design(data, model)
    return model, names, x, bounds, d_idx, p_idx


def solve(data, teammate, sigma_programme, fixed=None, bound_overrides=None):
    fixed = fixed or {}; bound_overrides = bound_overrides or {}
    model, names, x, bounds, d_idx, p_idx = model_design(data)
    blocks = foundation.covariance_blocks(data, teammate, sigma_programme)
    y = data.corrected_lap_time.to_numpy(float)
    xp, yp = foundation.projected(blocks, x, y)
    fixed_indices = {names.index(name): float(value) for name, value in fixed.items()}
    target = yp.copy()
    for index, value in fixed_indices.items(): target -= xp[:, index]*value
    free = [i for i in range(len(names)) if i not in fixed_indices]
    use_bounds = [bound_overrides.get(names[i], bounds[i]) for i in free]
    result = lsq_linear(
        xp[:, free], target,
        bounds=(np.array([b[0] for b in use_bounds]), np.array([b[1] for b in use_bounds])),
        tol=1e-12, max_iter=2000,
    )
    beta = np.zeros(len(names))
    beta[free] = result.x
    for index, value in fixed_indices.items(): beta[index] = value
    cost = float(np.sum((yp-xp@beta)**2))
    raw, baselines, offsets = foundation.raw_fit(data, blocks, x, beta, y)
    return {"names":names,"x":x,"bounds":bounds,"beta":beta,"cost":cost,"raw":raw,
            "blocks":blocks,"baselines":baselines,"offsets":offsets,"d_idx":d_idx,"p_idx":p_idx,
            "rank":int(np.linalg.matrix_rank(xp)),"xp":xp,"yp":yp}


def value(fit, name): return float(fit["beta"][fit["names"].index(name)])


def fit_summary(fit):
    return {name:value(fit,name) for name in fit["names"]}


def nuisance_summary(fit):
    offsets = np.array(list(fit["offsets"].values()), float)
    return {"run_offset_rms_s":float(np.sqrt(np.mean(offsets**2))) if len(offsets) else 0,
            "run_offset_max_abs_s":float(np.max(np.abs(offsets))) if len(offsets) else 0}


def profile(data, teammate, sigma_programme, parameter, grid):
    rows=[]
    for fixed_value in grid:
        fit=solve(data,teammate,sigma_programme,{parameter:fixed_value})
        row={"profile_parameter":parameter,"fixed_value":fixed_value,"profile_cost":fit["cost"],
             **fit_summary(fit),**nuisance_summary(fit)}
        rows.append(row)
    frame=pd.DataFrame(rows);frame["delta_cost_from_profile_minimum"]=frame.profile_cost-frame.profile_cost.min()
    frame["profile_minimum_fixed_value"]=frame.loc[frame.profile_cost.idxmin(),"fixed_value"]
    return frame


def performance_observations(data, fit):
    rows=[]
    for block in fit["blocks"]:
        group=data.loc[block["indices"]]
        for index,row in group.iterrows():
            parameter=("performance_HARD_vs_MEDIUM" if row.compound=="HARD" else
                       "performance_SOFT_vs_MEDIUM" if row.compound=="SOFT" else "MEDIUM_reference")
            column=(fit["x"][:,fit["names"].index(parameter)] if parameter in fit["names"] else np.zeros(len(data)))
            projected_norm=float(np.linalg.norm(block["transform"]@column[block["indices"]]))
            rows.append({"team":row.team,"session":row["session"],"programme":row.programme,
                "driver":row.driver,"run_id":row.run_id,"compound":row.compound,
                "lap_number":row.lap_number,"baseline_group":row.programme_block,
                "performance_parameter":parameter,"raw_performance_sensitivity":0 if parameter=="MEDIUM_reference" else 1,
                "projected_block_performance_column_norm":projected_norm,
                "performance_sensitive_after_baseline_projection":projected_norm>1e-10,
                "driver_delta_s":row.driver_delta,
                "programme_mean_profiled":True,"run_offset_treatment":"Gaussian correlated mismatch; conditional run offset",
            })
    return pd.DataFrame(rows)


def connectors(data, fit):
    physical=np.zeros(len(data))
    for j,name in enumerate(fit["names"]):
        if name.startswith("performance_"): continue
        physical += fit["x"][:,j]*fit["beta"][j]
    work=data.copy();work["reference_pace_no_P"]=work.corrected_lap_time-physical
    rows=[]
    for block,group in work.groupby("programme_block"):
        block_definition=next(item for item in fit["blocks"] if item["name"]==block)
        run_summary=group.groupby("run_id").agg(team=("team","first"),session=("session","first"),
            programme=("programme","first"),driver=("driver","first"),compound=("compound","first"),
            corrected_reference_pace=("reference_pace_no_P","mean"),raw_pace=("lap_time_s","mean"),
            driver_delta=("driver_delta","mean"),supporting_laps=("lap_number","size")).reset_index()
        for i,a in run_summary.iterrows():
            for _,b in run_summary.iloc[i+1:].iterrows():
                pair={a.compound,b.compound}
                if "MEDIUM" not in pair or len(pair)!=2: continue
                other=(pair-{"MEDIUM"}).pop();parameter=f"performance_{other}_vs_MEDIUM"
                pindex=fit["names"].index(parameter)
                projected_norm=float(np.linalg.norm(block_definition["transform"]@fit["x"][block_definition["indices"],pindex]))
                info=min(a.supporting_laps,b.supporting_laps)*(2 if a.driver==b.driver else 1)
                rows.append({"connector_parameter":parameter,"team":a.team,"session":a["session"],
                    "programme":a.programme,"baseline_group":block,"run_a":a.run_id,"run_b":b.run_id,
                    "driver_a":a.driver,"driver_b":b.driver,"same_driver":a.driver==b.driver,
                    "teammate_connector":a.driver!=b.driver,"compound_a":a.compound,"compound_b":b.compound,
                    "corrected_reference_pace_difference_b_minus_a_s":b.corrected_reference_pace-a.corrected_reference_pace,
                    "raw_pace_difference_b_minus_a_s":b.raw_pace-a.raw_pace,
                    "driver_delta_correction_b_minus_a_s":b.driver_delta-a.driver_delta,
                    "run_programme_mismatch_treatment":"profiled ProgrammeBlock mean + Gaussian run mismatch",
                    "supporting_laps_a":a.supporting_laps,"supporting_laps_b":b.supporting_laps,
                    "LongRun_involved":a.programme=="LongRun","Setting_involved":a.programme=="Setting",
                    "QualiSim_involved":a.programme=="QualiSim","projected_performance_column_norm":projected_norm,
                    "independent_information_score":info,
                    "ranking_metric_note":"pair heuristic=min supporting laps times 2 for same driver; block projected norm is the matrix sensitivity measure"})
    frame=pd.DataFrame(rows)
    if len(frame):frame["information_rank_within_parameter"]=frame.groupby("connector_parameter").independent_information_score.rank(method="dense",ascending=False).astype(int)
    return frame


def performance_influence(data, teammate, sigma_programme, connectors_frame):
    rows=[]
    connector_blocks=sorted(connectors_frame.baseline_group.unique())
    variants=[]
    for block in connector_blocks: variants.append((f"leave_block:{block}",~data.programme_block.eq(block)))
    # Performance-evidence policies retain observations for all non-P paths.
    same_blocks=set(connectors_frame.loc[connectors_frame.same_driver,"baseline_group"])
    teammate_blocks=set(connectors_frame.loc[connectors_frame.teammate_connector & ~connectors_frame.baseline_group.isin(same_blocks),"baseline_group"])
    variants += [
        ("all_current",pd.Series(True,index=data.index)),
        ("same_driver_connector_only",data.programme_block.isin(same_blocks)),
        ("teammate_connector_only",data.programme_block.isin(teammate_blocks)),
        ("QualiSim_only_performance",data.programme.eq("QualiSim")),
        ("Setting_only_performance",data.programme.eq("Setting")),
        ("LongRun_only_performance",data.programme.eq("LongRun")),
        ("LongRun_excluded_performance",~data.programme.eq("LongRun")),
    ]
    for name,eligible in variants:
        if name.startswith("leave_block:"):
            subset=data.loc[eligible].reset_index(drop=True)
        else:
            subset=data.copy();subset["performance_eligible"]=eligible.to_numpy()
        base=solve(subset,teammate,sigma_programme)
        soft_col=base["x"][:,base["names"].index("performance_SOFT_vs_MEDIUM")]
        hard_col=base["x"][:,base["names"].index("performance_HARD_vs_MEDIUM")]
        soft_norm=np.linalg.norm(foundation.projected(base["blocks"],soft_col[:,None],np.zeros(len(subset)))[0])
        hard_norm=np.linalg.norm(foundation.projected(base["blocks"],hard_col[:,None],np.zeros(len(subset)))[0])
        if soft_norm>1e-10:
            scan=profile(subset,teammate,sigma_programme,"performance_SOFT_vs_MEDIUM",np.arange(-2.0,.001,.25))
            pmin=float(scan.loc[scan.profile_cost.idxmin(),"fixed_value"]);support=scan[scan.delta_cost_from_profile_minimum<=1]
            lo,hi=support.fixed_value.min(),support.fixed_value.max()
        else:pmin=lo=hi=np.nan
        rows.append({"variant":name,"observation_count":len(subset),"soft_column_norm":soft_norm,"hard_column_norm":hard_norm,
            "soft_identifiable":soft_norm>1e-10,"hard_identifiable":hard_norm>1e-10,
            "bounded_Perf_H_vs_M":value(base,"performance_HARD_vs_MEDIUM"),
            "bounded_Perf_S_vs_M":value(base,"performance_SOFT_vs_MEDIUM"),"fit_cost":base["cost"],
            "soft_profile_minimum":pmin,"soft_delta_cost_le_1_low":lo,"soft_delta_cost_le_1_high":hi})
    frame=pd.DataFrame(rows)
    allrow=frame[frame.variant.eq("all_current")].iloc[0]
    frame["change_H_from_all"]=frame.bounded_Perf_H_vs_M-allrow.bounded_Perf_H_vs_M
    frame["change_S_from_all"]=frame.bounded_Perf_S_vs_M-allrow.bounded_Perf_S_vs_M
    return frame


def degradation_profiles(data, teammate, sigma_programme, current_direct, nuisance_values):
    names={c:f"weekend_degradation_{c}" for c in COMPOUNDS}
    rows=[]
    references={"official_F1":OFFICIAL,
                "current_direct":{c:value(current_direct,names[c]) for c in COMPOUNDS},
                "current_nuisance":nuisance_values}
    for ref_name,ref in references.items():
        for k in np.arange(-.10,.151,.01):
            fixed={names[c]:ref[c]+k for c in COMPOUNDS}
            fitted=solve(data,teammate,sigma_programme,fixed)
            rows.append({"reference":ref_name,"common_shift_k_s_per_lap":k,"profile_cost":fitted["cost"],
                **{f"fixed_D_{c}":fixed[names[c]] for c in COMPOUNDS},**fit_summary(fitted),**nuisance_summary(fitted)})
    frame=pd.DataFrame(rows);frame["delta_cost_within_reference"]=frame.profile_cost-frame.groupby("reference").profile_cost.transform("min")
    return frame


def external_refits(data, teammate, sigma_programme, best):
    scenarios=[("unconstrained_current",{}),("all_official",{f"weekend_degradation_{c}":OFFICIAL[c] for c in COMPOUNDS})]
    scenarios += [(f"only_{c}_official",{f"weekend_degradation_{c}":OFFICIAL[c]}) for c in COMPOUNDS]
    rows=[]
    for name,fixed in scenarios:
        fitted=best if not fixed else solve(data,teammate,sigma_programme,fixed)
        base={"scenario":name,"record_type":"overall","group_dimension":"all","group_value":"all",
              "cost":fitted["cost"],"raw_rms_s":np.sqrt(np.mean(fitted["raw"]**2)),**fit_summary(fitted),**nuisance_summary(fitted)}
        rows.append(base)
        residual=pd.DataFrame({"session":data["session"],"programme":data.programme,"compound":data.compound,"squared":fitted["raw"]**2})
        for dimension in ("session","programme","compound"):
            for group_value,g in residual.groupby(dimension):
                rows.append({**base,"record_type":"raw_group","group_dimension":dimension,"group_value":group_value,
                             "group_observations":len(g),"group_raw_sse":g.squared.sum(),"group_raw_rms_s":np.sqrt(g.squared.mean())})
    return pd.DataFrame(rows)


def fuel_profile(data, teammate, sigma_programme):
    rows=[]
    for fuel in np.linspace(0,.10,21):
        fitted=solve(data,teammate,sigma_programme,{"fuel_rate":fuel})
        rows.append({"fixed_fuel_rate_s_per_lap":fuel,"profile_cost":fitted["cost"],
            **{f"D_{c}_s_per_lap":value(fitted,f'weekend_degradation_{c}') for c in COMPOUNDS},
            **{f"track_{s}_s_per_lap_equivalent":value(fitted,f'track_rate_{s}') for s in SESSIONS},
            "Perf_H_vs_M":value(fitted,"performance_HARD_vs_MEDIUM"),"Perf_S_vs_M":value(fitted,"performance_SOFT_vs_MEDIUM")})
    frame=pd.DataFrame(rows);frame["delta_cost"]=frame.profile_cost-frame.profile_cost.min();return frame


def longest_block(numbers, allow_one_gap=False):
    if not numbers:return []
    best=[]
    for start in range(len(numbers)):
        used=False;current=[numbers[start]]
        for value in numbers[start+1:]:
            gap=value-current[-1]
            if gap==1:current.append(value)
            elif allow_one_gap and gap==2 and not used:used=True;current.append(value)
            else:break
        if len(current)>len(best):best=current
    return best


def continuity_diagnostic(ledger,groups):
    rows=[]
    for row in ledger.itertuples():
        g=groups[row.run_id].sort_values("LapNumber");rep=g[g.Representative & g.DryTimed]
        nums=rep.LapNumber.astype(int).tolist();block=longest_block(nums);relaxed=longest_block(nums,True)
        block_laps=rep[rep.LapNumber.isin(block)].LapTimeSeconds
        raw_cv=rep.LapTimeSeconds.std(ddof=1)/rep.LapTimeSeconds.mean() if len(rep)>=2 else np.nan
        block_cv=block_laps.std(ddof=1)/block_laps.mean() if len(block_laps)>=2 else np.nan
        median=rep.LapTimeSeconds.median();robust=1.4826*np.median(np.abs(rep.LapTimeSeconds-median))/median if len(rep) else np.nan
        gaps=[]
        for a,b in zip(nums,nums[1:]):
            if b-a>1:
                rejected=g[(g.LapNumber>a)&(g.LapNumber<b)]
                gaps.extend(f"{int(r.LapNumber)}:{'non-representative' if r.DryTimed else 'not-dry/timed'}" for r in rejected.itertuples())
        rows.append({"run_id":row.run_id,"team":row.team,"driver":row.driver,"session":row.session,
            "compound":row.compound,"current_label":row.current_label,"revised_label":row.revised_label,
            "uncertain_breakdown":row.uncertain_breakdown,"representative_laps":len(rep),
            "longest_continuous_actual":len(block),"longest_continuous_required":6,
            "longest_allowing_one_excluded_lap":len(relaxed),"passes_six_with_one_excluded":len(block)<6 and len(relaxed)>=6,
            "representative_fraction_actual":row.representative_fraction,"representative_fraction_required":.60,
            "raw_all_representative_cv":raw_cv,"continuous_block_cv":block_cv,"robust_mad_cv":robust,"cv_limit":.025,
            "maximum_representative_relative_pace":row.maximum_representative_relative_pace,"relative_pace_limit":1.15,
            "continuity_break_laps":"|".join(gaps)})
    return pd.DataFrame(rows)


def priority_table(ledger,continuity,connectors_frame):
    result=ledger.merge(continuity.drop(columns=[c for c in ["team","driver","session","compound","current_label","revised_label","uncertain_breakdown"] if c in continuity]),on="run_id")
    connector_runs=set(connectors_frame.run_a)|set(connectors_frame.run_b)
    result["performance_connector"]=result.run_id.isin(connector_runs)
    result["soft_longrun"]=result.revised_label.eq("LongRun")&result.compound.eq("SOFT")
    result["continuity_rule_sensitive"]=(result.passes_six_with_one_excluded
        & result.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability"))
    result["one_lap_variability_sensitive"]=(result.raw_all_representative_cv>.025)&(result.robust_mad_cv<=.025)
    result["review_tier"]=np.select([
        result.soft_longrun|result.continuity_rule_sensitive,
        result.performance_connector|result.one_lap_variability_sensitive|result.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability")],
        ["Tier 1","Tier 2"],default="Tier 3")
    result["review_reason"]=result.apply(lambda r:"|".join(k for k,v in {
        "soft_longrun":r.soft_longrun,"continuity_rule_sensitive":r.continuity_rule_sensitive,
        "performance_connector":r.performance_connector,"variability_outlier_sensitive":r.one_lap_variability_sensitive}.items() if v),axis=1)
    return result


def soft_lap_audit(full,data,prepared,fit):
    selected={(r.run_id,float(r.lap_number)):i for i,r in data[data.programme.eq("LongRun")&data.compound.eq("SOFT")].iterrows()}
    prep=prepared.set_index(["Session","Driver","Stint","LapNumber"])
    rows=[]
    session_min=full.groupby("Session").SessionSeconds.min();rep_lap=full[full.Representative&full.LapTimeSeconds.notna()].groupby("Session").LapTimeSeconds.median()
    for run_id,g in full[full.programme.eq("LongRun")&full.Compound.eq("SOFT")].groupby("run_id"):
        g=g.sort_values(["SessionSeconds","LapNumber"]);first=g.iloc[0]
        block=f"{first.Team}:{first.Session}:LongRun";base=fit["baselines"].get(block,np.nan);offset=fit["offsets"].get(run_id,0)
        driver_delta=float(data[data.run_id.eq(run_id)].driver_delta.iloc[0])
        for ordinal,r in enumerate(g.itertuples(),1):
            key=(r.Session,r.Driver,r.Stint,r.LapNumber);p=prep.loc[key] if key in prep.index else None
            accepted=(run_id,float(r.LapNumber)) in selected
            reasons=[]
            if p is None:reasons.append("not_in_cleaning_frame")
            else:
                for col,label in (("DryTimed","not_dry_timed"),("IsGreenTrackStatus","track_status"),("IsQuickLap","pace"),("IsCleanAirLongRunLap","traffic_or_pit_or_validity")):
                    if col in p and not bool(p[col]):reasons.append(label)
                if bool(p.get("IsOutLap",False)):reasons.append("out_lap")
                if bool(p.get("IsInLap",False)):reasons.append("in_lap")
            fuel=value(fit,"fuel_rate")*(-ordinal)
            track=value(fit,f"track_rate_{r.Session}")*((r.SessionSeconds-session_min[r.Session])/rep_lap[r.Session])
            perf=value(fit,"performance_SOFT_vs_MEDIUM")
            corrected=(r.LapTimeSeconds-driver_delta-base-offset-fuel-track-perf if pd.notna(r.LapTimeSeconds) else np.nan)
            rows.append({"run_id":run_id,"team":r.Team,"driver":r.Driver,"session":r.Session,"compound":r.Compound,
                "physical_lap":r.LapNumber,"physical_stint_index":ordinal,"tyre_age":r.TyreLife,"raw_lap_time_s":r.LapTimeSeconds,
                "relative_pace_percent":100*r.RelativePace if pd.notna(r.RelativePace) else np.nan,"accepted_clean_degradation_observation":accepted,
                "exclusion_reason":"accepted" if accepted else "|".join(dict.fromkeys(reasons)) or "not_selected_by_L1_policy",
                "track_status":r.TrackStatus,"deleted":r.Deleted,"deleted_reason":r.DeletedReason,
                "min_time_to_driver_ahead_s":r.MinTimeDeltaToDriverAhead,"fuel_correction_s":fuel,"track_correction_s":track,
                "performance_correction_s":perf,"programme_baseline_s":base,"run_offset_s":offset,"Q_driver_delta_s":driver_delta,
                "corrected_pace_s":corrected})
    frame=pd.DataFrame(rows)
    for run_id,g in frame[frame.accepted_clean_degradation_observation].groupby("run_id"):
        slope=linregress(g.tyre_age,g.corrected_pace_s).slope
        frame.loc[frame.run_id.eq(run_id),"accepted_corrected_slope_s_per_lap"]=slope
    return frame


def soft_robustness(audit):
    rows=[]
    for run_id,g in audit[audit.accepted_clean_degradation_observation].groupby("run_id"):
        g=g.sort_values("tyre_age");x=g.tyre_age.to_numpy(float);y=g.corrected_pace_s.to_numpy(float)
        def add(method,xv,yv):
            if len(xv)<2:return
            rows.append({"run_id":run_id,"team":g.team.iloc[0],"method":method,"observations":len(xv),
                         "slope_s_per_lap":linregress(xv,yv).slope})
        add("full_accepted",x,y);split=len(g)//2;add("first_half",x[:max(split,2)],y[:max(split,2)]);add("second_half",x[split:],y[split:])
        if len(g)>=3:
            slopes=[linregress(np.delete(x,i),np.delete(y,i)).slope for i in range(len(g))]
            rows += [{"run_id":run_id,"team":g.team.iloc[0],"method":"one_point_leaveout_min","observations":len(g)-1,"slope_s_per_lap":min(slopes)},
                     {"run_id":run_id,"team":g.team.iloc[0],"method":"one_point_leaveout_max","observations":len(g)-1,"slope_s_per_lap":max(slopes)}]
            robust=theilslopes(y,x)
            rows.append({"run_id":run_id,"team":g.team.iloc[0],"method":"Theil_Sen_robust","observations":len(g),"slope_s_per_lap":robust.slope})
            best=None
            for start in range(len(g)-2):
                for end in range(start+3,len(g)+1):
                    yy=y[start:end];score=np.std(yy)/max(abs(np.mean(yy)),1e-9)
                    if best is None or score<best[0]:best=(score,start,end)
            _,start,end=best;add("longest_stable_subsection",x[start:end],y[start:end])
        physical=audit[audit.run_id.eq(run_id)].sort_values("physical_lap")
        rows.append({"run_id":run_id,"team":g.team.iloc[0],"method":"relaxed_continuity_count","observations":len(physical),
                     "slope_s_per_lap":np.nan,"diagnostic_value":len(longest_block(physical.loc[physical.raw_lap_time_s.notna(),"physical_lap"].astype(int).tolist(),True))})
    return pd.DataFrame(rows)


def review_plot(run_ids,groups,ledger,filename,title,columns=3):
    run_ids=list(run_ids);nrows=max(1,int(np.ceil(len(run_ids)/columns)))
    fig,axes=plt.subplots(nrows,columns,figsize=(5.5*columns,4.2*nrows),squeeze=False)
    ledger_idx=ledger.set_index("run_id")
    for ax,run_id in zip(axes.flat,run_ids):
        g=groups[run_id].sort_values("LapNumber");row=ledger_idx.loc[run_id];timed=g[g.LapTimeSeconds.notna()]
        roles=np.where(timed.Push,"PUSH",np.where(timed.Representative,"REP","OTHER"))
        for role,marker,color in (("PUSH","^","tab:orange"),("REP","o","tab:blue"),("OTHER","x","tab:red")):
            m=roles==role;ax.scatter(timed.loc[m,"LapNumber"],100*timed.loc[m,"RelativePace"],marker=marker,color=color,label=role)
        out=timed.PitOutTime.notna();inside=timed.PitInTime.notna()
        ax.scatter(timed.loc[out,"LapNumber"],100*timed.loc[out,"RelativePace"],marker="s",facecolors="none",edgecolors="purple",s=75,label="pit out")
        ax.scatter(timed.loc[inside,"LapNumber"],100*timed.loc[inside,"RelativePace"],marker="D",facecolors="none",edgecolors="brown",s=65,label="pit in")
        for r in timed.itertuples():ax.annotate(f"{r.LapTimeSeconds:.1f}s T{r.TyreLife:g}",(r.LapNumber,100*r.RelativePace),fontsize=6)
        rep=timed[timed.Representative];block=longest_block(rep.LapNumber.astype(int).tolist())
        if block:ax.axvspan(min(block)-.4,max(block)+.4,color="tab:green",alpha=.10,label="longest continuous rep block")
        for a,b in zip(rep.LapNumber,rep.LapNumber.iloc[1:]):
            if b-a>1:ax.axvline((a+b)/2,color="red",ls=":",lw=1.5)
        for y,label,ls in ((100,"best","-"),(103,"PUSH","--"),(105,"visual only",":"),(115,"LongRun limit","-.")):
            ax.axhline(y,color="black" if y<105 else "grey",ls=ls,lw=.8);ax.text(.99,y,label,transform=ax.get_yaxis_transform(),ha="right",va="bottom",fontsize=6)
        flag=lambda passed:"OK" if passed else "FAIL"
        metrics=(f"{flag(row.longest_consecutive_representative>=6)} block {row.longest_consecutive_representative}/6  "
                 f"{flag(row.representative_fraction>=.60)} rep {row.representative_fraction:.2f}/.60\n"
                 f"{flag(pd.isna(row.representative_pace_cv) or row.representative_pace_cv<=.025)} CV {row.representative_pace_cv:.3f}/.025  "
                 f"{flag(pd.isna(row.maximum_representative_relative_pace) or row.maximum_representative_relative_pace<=1.15)} max {row.maximum_representative_relative_pace:.3f}/1.15")
        ax.text(.01,.99,metrics,transform=ax.transAxes,va="top",fontsize=7,bbox=dict(facecolor="white",alpha=.75))
        ax.set(title=f"{row.team} {row.driver} {row.session} run {row.stint}\n{row.current_label}→{row.revised_label} {row.compound}",xlabel="raw LapNumber",ylabel="relative pace (%)");ax.grid(alpha=.2)
    for ax in axes.flat[len(run_ids):]:ax.set_visible(False)
    if run_ids:axes.flat[0].legend(fontsize=6,loc="lower left")
    fig.suptitle(title);fig.tight_layout();fig.savefig(OUTPUT/filename,dpi=180);plt.close(fig)


def overview_plots(laps,ledger,groups):
    frame=laps[laps.LapTimeSeconds.notna()].copy()
    if "run_id" not in frame:
        frame["run_id"]=(frame.Session.astype(str)+":"+frame.Team.astype(str)+":"+
                         frame.Driver.astype(str)+":"+frame.Stint.astype(str))
    if "programme" not in frame:
        frame["programme"]=frame.run_id.map(ledger.set_index("run_id").revised_label)
    offset={"FP1":0,"FP2":max(laps.loc[laps.Session.eq("FP1"),"SessionSeconds"].max(),1)+300,
        "FP3":max(laps.loc[laps.Session.eq("FP1"),"SessionSeconds"].max(),1)+max(laps.loc[laps.Session.eq("FP2"),"SessionSeconds"].max(),1)+600}
    frame["weekend_x"]=[offset[s]+t for s,t in zip(frame.Session,frame.SessionSeconds)]
    for normalized,name in ((False,"fp_weekend_timeline_raw.png"),(True,"fp_weekend_timeline_relative_pace.png")):
        fig,ax=plt.subplots(figsize=(18,8));teams=sorted(frame.Team.unique());cmap=plt.get_cmap("tab20")
        programme_markers={"Setting":"s","QualiSim":"^","LongRun":"o","Uncertain":"x"}
        for i,(driver,dg) in enumerate(frame.groupby("Driver")):
            for programme,g in dg.groupby("programme"):
                y=100*g.RelativePace if normalized else g.LapTimeSeconds
                ax.scatter(g.weekend_x,y,s=12,color=cmap(teams.index(g.Team.iloc[0])%20),
                           marker=programme_markers.get(programme,"."),alpha=.7,
                           facecolors="none" if i%2 else None,
                           label=f"{g.Team.iloc[0]} {driver} {programme}")
        for run_id,g in frame.groupby("run_id"):
            first=g.sort_values("weekend_x").iloc[0]
            y=100*first.RelativePace if normalized else first.LapTimeSeconds
            ax.annotate(f"S{first.Stint:g}",(first.weekend_x,y),fontsize=5,alpha=.65)
        handles=[Line2D([0],[0],marker=marker,color="grey",linestyle="None",label=programme)
                 for programme,marker in programme_markers.items()]
        handles += [Line2D([0],[0],marker="o",color="black",linestyle="None",label="driver style A filled"),
                    Line2D([0],[0],marker="o",color="black",markerfacecolor="none",linestyle="None",label="driver style B open")]
        ax.legend(handles=handles,fontsize=7,ncol=3,loc="upper center")
        for session in ("FP2","FP3"):ax.axvline(offset[session],color="black")
        if normalized:
            for y,ls in ((100,"-"),(103,"--"),(105,":"),(115,"-.")):ax.axhline(y,color="grey",ls=ls)
            ax.set_ylabel("LapTime / DriverSessionBest (%)");ax.set_ylim(98,125)
        else:ax.set_ylabel("raw lap time (s)");ax.set_ylim(frame.LapTimeSeconds.quantile(.01),frame.LapTimeSeconds.quantile(.95))
        ax.set(xlabel="concatenated FP1 → FP2 → FP3 SessionTime",title="R11 FP weekend timeline");ax.grid(alpha=.15);fig.tight_layout();fig.savefig(OUTPUT/name,dpi=180);plt.close(fig)

    # First three physical FP1 stints for every driver, including zero-evidence runs.
    drivers=sorted(laps.loc[laps.Session.eq("FP1"),"Driver"].unique());fig,axes=plt.subplots(5,4,figsize=(18,20),squeeze=False)
    led=ledger.set_index("run_id")
    for ax,driver in zip(axes.flat,drivers):
        candidates=ledger[(ledger.session.eq("FP1"))&ledger.driver.eq(driver)].sort_values("run_order").head(3)
        for run_id in candidates.run_id:
            g=groups[run_id].sort_values("LapNumber");ax.plot(g.LapNumber,100*g.RelativePace,marker="o",label=f"stint {led.loc[run_id].stint} {led.loc[run_id].revised_label}")
        for y,ls in ((100,"-"),(103,"--"),(105,":"),(115,"-.")):ax.axhline(y,color="grey",ls=ls,lw=.7)
        ax.set(title=f"{candidates.team.iloc[0] if len(candidates) else ''} {driver}",xlabel="LapNumber",ylabel="relative pace %");ax.grid(alpha=.2);ax.legend(fontsize=6)
    for ax in axes.flat[len(drivers):]:ax.set_visible(False)
    fig.suptitle("FP1 early micro-tests: first three physical stints per driver");fig.tight_layout();fig.savefig(OUTPUT/"fp_fp1_early_microtests.png",dpi=180);plt.close(fig)


def other_plots(soft_profile,hard_profile,connectors_frame,external,fuel,soft_audit):
    for frame,name,title in ((soft_profile,"fp_performance_soft_profile.png","Soft vs Medium profile"),(hard_profile,"fp_performance_hard_profile.png","Hard vs Medium profile")):
        fig,ax=plt.subplots(figsize=(8,5));ax.plot(frame.fixed_value,frame.delta_cost_from_profile_minimum,marker="o");ax.axhline(1,color="grey",ls="--");ax.set(xlabel="fixed performance (s)",ylabel="Δ cost",title=title);ax.grid(alpha=.2);fig.tight_layout();fig.savefig(OUTPUT/name,dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(11,7));blocks=sorted(connectors_frame.baseline_group.unique());params=sorted(connectors_frame.connector_parameter.unique())
    for i,b in enumerate(blocks):ax.scatter(0,i,color="tab:blue");ax.text(-.03,i,b,ha="right",va="center",fontsize=7)
    for j,p in enumerate(params):y=j*max(len(blocks)-1,1);ax.scatter(1,y,marker="s",color="tab:orange",s=120);ax.text(1.03,y,p,va="center")
    for r in connectors_frame.itertuples():ax.plot([0,1],[blocks.index(r.baseline_group),params.index(r.connector_parameter)*max(len(blocks)-1,1)],alpha=.25+min(r.independent_information_score/20,.6),lw=1+r.independent_information_score/10)
    ax.set(xlim=(-.7,1.6),xticks=[],yticks=[],title="Actual cross-compound performance connectors");[s.set_visible(False) for s in ax.spines.values()];fig.tight_layout();fig.savefig(OUTPUT/"fp_performance_connector_graph.png",dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,6));wide=external.pivot(index="compound",columns="estimate_type",values="estimate_s_per_lap").reindex(COMPOUNDS);wide.plot.bar(ax=ax);ax.set(ylabel="degradation (s/lap)",title="FP degradation outputs vs supplied official F1 benchmark");ax.grid(axis="y",alpha=.2);fig.tight_layout();fig.savefig(OUTPUT/"fp_degradation_vs_f1_benchmark.png",dpi=180);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(14,5));axes[0].plot(fuel.fixed_fuel_rate_s_per_lap,fuel.delta_cost);axes[0].set(xlabel="fixed Fuel rate",ylabel="Δ cost")
    for c in COMPOUNDS:axes[1].plot(fuel.fixed_fuel_rate_s_per_lap,fuel[f"D_{c}_s_per_lap"],label=c)
    axes[1].set(xlabel="fixed Fuel rate",ylabel="optimized D (s/lap)");axes[1].legend();[a.grid(alpha=.2) for a in axes];fig.tight_layout();fig.savefig(OUTPUT/"fp_fuel_degradation_profile.png",dpi=180);plt.close(fig)
    runs=soft_audit.run_id.unique();fig,axes=plt.subplots(len(runs),2,figsize=(15,4.5*len(runs)),squeeze=False)
    for row,run_id in enumerate(runs):
        g=soft_audit[soft_audit.run_id.eq(run_id)].sort_values("physical_lap");accepted=g.accepted_clean_degradation_observation
        axes[row,0].plot(g.physical_lap,100*g.relative_pace_percent/100,color="grey");axes[row,0].scatter(g.loc[accepted,"physical_lap"],g.loc[accepted,"relative_pace_percent"],color="tab:green",label="accepted");axes[row,0].scatter(g.loc[~accepted,"physical_lap"],g.loc[~accepted,"relative_pace_percent"],marker="x",color="red",label="excluded")
        for y,ls in ((100,"-"),(103,"--"),(105,":"),(115,"-.")):axes[row,0].axhline(y,color="grey",ls=ls,lw=.7)
        axes[row,0].set(title=run_id+" raw/normalized",ylabel="relative pace %")
        axes[row,1].scatter(g.loc[accepted,"tyre_age"],g.loc[accepted,"corrected_pace_s"],color="tab:blue");axes[row,1].set(title="accepted corrected pace",xlabel="tyre age",ylabel="corrected pace s")
        for ax in axes[row]:ax.grid(alpha=.2)
    axes[0,0].legend();fig.tight_layout();fig.savefig(OUTPUT/"fp_soft_longrun_detailed_review.png",dpi=180);plt.close(fig)


def report(soft_profile,hard_profile,influence,connectors_frame,external_refit,fuel,continuity,priority,soft_robust,external):
    smin=soft_profile.loc[soft_profile.profile_cost.idxmin()];hmin=hard_profile.loc[hard_profile.profile_cost.idxmin()]
    ssupport=soft_profile[soft_profile.delta_cost_from_profile_minimum<=1]
    hsupport=hard_profile[hard_profile.delta_cost_from_profile_minimum<=1]
    current=influence[influence.variant.eq("all_current")].iloc[0]
    strongest=connectors_frame[connectors_frame.connector_parameter.eq("performance_SOFT_vs_MEDIUM")].sort_values("independent_information_score",ascending=False).head(5)
    block_strength=(connectors_frame[connectors_frame.connector_parameter.eq("performance_SOFT_vs_MEDIUM")]
                    .groupby("baseline_group").agg(connector_pairs=("run_a","size"),
                    information_score=("independent_information_score","sum"),
                    projected_P_column_norm=("projected_performance_column_norm","first"))
                    .sort_values("projected_P_column_norm",ascending=False).head(5))
    material=influence[influence.change_S_from_all.abs()>.10].sort_values("change_S_from_all",key=abs,ascending=False)
    official=external_refit[(external_refit.scenario.eq("all_official"))&external_refit.record_type.eq("overall")].iloc[0]
    uncon=external_refit[(external_refit.scenario.eq("unconstrained_current"))&external_refit.record_type.eq("overall")].iloc[0]
    individual={c:external_refit[(external_refit.scenario.eq(f"only_{c}_official"))&external_refit.record_type.eq("overall")].iloc[0] for c in COMPOUNDS}
    near=continuity[continuity.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability")]
    soft_summary=soft_robust.groupby("run_id").slope_s_per_lap.agg(["min","max"])
    fuel_support=fuel[fuel.delta_cost<=1]
    shifts=pd.read_csv(OUTPUT/"fp_degradation_common_shift_profile.csv")
    shift_summary={ref:g.loc[g.profile_cost.idxmin()] for ref,g in shifts.groupby("reference")}
    lines=["# FP Tyre Reconciliation Audit","","## Evidence-first conclusions","",
        f"Soft-vs-Medium reaches exactly `-1.0 s` because the bounded least-squares solution is truncated by the configured `[-1, 0] s` interval. This is **Case A**: a finite diagnostic minimum at `{smin.fixed_value:.3f} s`, with Δcost≤1 support `{ssupport.fixed_value.min():.2f}–{ssupport.fixed_value.max():.2f}`. The wall is technically about `0.05 s` too narrow for this model, but the broad support and connector sensitivity mean widening it is not a substantive repair.","",
        f"HARD-vs-MEDIUM has a finite profile minimum near `{hmin.fixed_value:.3f} s`, with Δcost≤1 support `{hsupport.fixed_value.min():.2f}–{hsupport.fixed_value.max():.2f}`. The current bounded fit is H `{current.bounded_Perf_H_vs_M:.4f}`, S `{current.bounded_Perf_S_vs_M:.4f}`.","",
        "No cross-programme baseline connection was found: `baseline_group = Team:Session:Programme`. QualiSim and LongRun rows can coexist in the global likelihood and share Fuel/Track/D/P parameters, but their ProgrammeMeans are distinct. LongRun and Setting performance columns exist in the raw matrix; because every R11 block of those programmes is single-compound, their constant P columns project to zero and provide no independent absolute compound-performance connector.","",
        "## Strongest Soft connectors","","Aggregated block support:",
        *[f"- {block}: projected P-column norm {r.projected_P_column_norm:.3f}; {int(r.connector_pairs)} run pairs; heuristic pair score {r.information_score:g}" for block,r in block_strength.iterrows()],"",
        "Highest-scoring individual run pairs:",
        *[f"- {r.baseline_group}: {r.run_a} ↔ {r.run_b}; {r.driver_a}/{r.driver_b}; score {r.independent_information_score}" for r in strongest.itertuples()],"",
        "All surviving H/M and S/M connectors are QualiSim comparisons inside the same Team×Session×Programme baseline. Their run mismatch is not fixed: it is represented by the declared Gaussian run-mismatch covariance and conditional offsets.","",
        "## Connector influence","",
        "Variants moving Soft by more than 0.10 s from the bounded all-current fit:",
        *[f"- `{r.variant}`: S `{r.bounded_Perf_S_vs_M:.3f}`, profile min `{r.soft_profile_minimum if pd.notna(r.soft_profile_minimum) else 'unidentified'}`, cost `{r.fit_cost:.2f}`" for r in material.itertuples()],"",
        "## Official degradation benchmark","",
        f"The user-supplied official F1 vector `0.06/0.10/0.15 s/lap` is registered as benchmark-only. Fixing all three gives cost `{official.cost:.2f}` versus `{uncon.cost:.2f}` for the current direct no-order optimum (Δ `{official.cost-uncon.cost:.2f}`). It was not used as a prior or replacement input.","",
        f"Individual official fixes cost Δ H/M/S `{individual['HARD'].cost-uncon.cost:.2f}/{individual['MEDIUM'].cost-uncon.cost:.2f}/{individual['SOFT'].cost-uncon.cost:.2f}`. The official-reference common-shift profile chooses `k={shift_summary['official_F1'].common_shift_k_s_per_lap:.3f}`, not `+0.05`; the apparent common offset does not survive profiling.","",
        f"Fixing the full official vector moves Fuel `{uncon.fuel_rate:.4f}→{official.fuel_rate:.4f}`. Fixing Soft alone moves it to `{individual['SOFT'].fuel_rate:.4f}`, showing stronger Soft/Fuel compensation than the all-vector change.","",
        f"Fuel's Δcost≤1 profile spans `{fuel_support.fixed_fuel_rate_s_per_lap.min():.3f}–{fuel_support.fixed_fuel_rate_s_per_lap.max():.3f}`. Across that range D_H/M/S move by `{fuel_support.D_HARD_s_per_lap.max()-fuel_support.D_HARD_s_per_lap.min():.3f}/{fuel_support.D_MEDIUM_s_per_lap.max()-fuel_support.D_MEDIUM_s_per_lap.min():.3f}/{fuel_support.D_SOFT_s_per_lap.max()-fuel_support.D_SOFT_s_per_lap.min():.3f} s/lap`; full algebraic rank therefore does not imply a reliable physical Fuel/D split.","",
        "## Soft LongRuns","",
        "The three accepted runs disagree in full-run direction, and the detailed CSV retains every physical lap, acceptance flag and available exclusion reason. Robust/half-run/one-point-leaveout ranges are:",
        *[f"- {run}: {row['min']:.3f} to {row['max']:.3f} s/lap" for run,row in soft_summary.iterrows()],"",
        "No lap was manually deleted. Large variation between halves and leave-one-out estimates indicates both genuine run-shape differences and sensitivity to individual disturbances, rather than one universal Soft slope.","",
        "All accepted Soft laps are green-status and non-deleted under the current clean flag. Nevertheless, several accepted laps have very small recorded minimum time-to-car-ahead; that proximity metric alone is not the production exclusion rule, but it makes those points priority manual checks. Alpine's first and last accepted points are especially slow, consistent with warm-up/end-of-run disturbance or traffic/setup effects that the public flags do not uniquely identify.","",
        "## Continuity and variability","",
        f"There are `{len(near)}` disputed near-LongRun failures; `{int(near.passes_six_with_one_excluded.sum())}` would reach six representative laps if one excluded physical lap were bridged diagnostically. `{int(((near.raw_all_representative_cv>.025)&(near.robust_mad_cv<=.025)).sum())}` fail raw CV while their MAD-based robust CV is inside the limit, showing how often one/few laps dominate variability. These are review findings, not classifier changes.","",
        "## Relation to MC collapse","",
        f"Neither performance direction is exceptionally narrow on its one-dimensional profile: Soft Δcost≤1 width is `{ssupport.fixed_value.max()-ssupport.fixed_value.min():.2f} s`, HARD width `{hsupport.fixed_value.max()-hsupport.fixed_value.min():.2f} s`. Severe 9D importance-weight collapse is therefore better explained by joint bounded/correlated geometry—Soft wall truncation, Fuel/D and Track/P coupling, and incompatible connector blocks—than by one ultra-narrow performance coordinate. No sampler change was attempted.","",
        "## Explicit answers","",
        f"1. Soft hits exactly -1 because the optimizer wants a more-negative or boundary-adjacent value and the configured lower bound truncates it; the contributing information is cross-run QualiSim connectivity plus nuisance assumptions.",
        f"2. Case A: a finite minimum at `{smin.fixed_value:.3f}`, rising on both sides; Δcost≤1 spans `{ssupport.fixed_value.min():.2f}–{ssupport.fixed_value.max():.2f}`.",
        "3. The bound is technically slightly narrow, but it is also protective. Do not widen it from this result alone because block removals and evidence-policy changes move the estimate by much more than 0.05 s.",
        "4. The exact strongest blocks/runs are listed above and exhaustively in `fp_performance_connectors.csv`.",
        "5. No. QualiSim and LongRun have distinct baseline groups everywhere; matrix evidence is exported per observation.",
        "6. LongRun rows have raw P sensitivity in code, but R11 LongRun blocks are single-compound so their P column is removed by baseline projection. The architecture should still be reviewed because future cross-compound LongRun blocks would contribute.",
        f"7. Material removals/policies are listed in the connector influence section and CSV.",
        f"8. HARD has a finite profile minimum near `{hmin.fixed_value:.3f}` and is not at its current bound, though connector leave-outs still measure its fragility.",
        f"9. No. The official-vector profile chooses `k={shift_summary['official_F1'].common_shift_k_s_per_lap:.3f}`, so the apparent +0.05 common offset is not likelihood-supported.",
        f"10. Official all-fixed Δcost is `{official.cost-uncon.cost:.2f}`—outside the current model's low-cost region. H and M alone are less inconsistent; Soft supplies most conflict.",
        f"11. Full official fixing moves Fuel `{uncon.fuel_rate:.4f}→{official.fuel_rate:.4f}`; Soft-only fixing moves it to `{individual['SOFT'].fuel_rate:.4f}`. Full Track/P changes are exported.",
        "12. Yes if the Δcost≤1 Fuel range induces material D movement, as quantified above; formal rank is not sufficient.",
        "13. Cadillac trends upward while Audi/Alpine raw and corrected sections trend differently; half-run and robust results show non-linear/disturbed shapes and between-session/team conflict.",
        f"14. Yes for a subset: one-gap bridging rescues `{int(near.passes_six_with_one_excluded.sum())}` near failures and robust CV rescues `{int(((near.raw_all_representative_cv>.025)&(near.robust_mad_cv<=.025)).sum())}` variability failures diagnostically.",
        "15. Inspect Tier 1 first: all Soft LongRuns plus one-gap-sensitive near-LongRuns; Tier 2 contains performance connectors and robust-CV-sensitive cases.",
        "16. HARD degradation is data-informed plus single-team assumption; MEDIUM is nuisance-assisted; Soft engineering degradation is fallback-assumed; Soft performance is boundary-limited; official H/M/S are external-only.","",
        "## Non-production status","","All outputs are diagnostic and under `temp/`. Production FP, Race, live, Strategy Prediction and Qualifying algorithms were not modified."]
    (OUTPUT/"FP_TYRE_RECONCILIATION_AUDIT.md").write_text("\n".join(lines)+"\n")


def main():
    OUTPUT.mkdir(exist_ok=True)
    laps,ledger,groups,full,data,teammate,prepared,sigma=foundation.load_inputs();data=data.reset_index(drop=True)
    current=solve(data,teammate,sigma)
    observations=performance_observations(data,current);observations.to_csv(OUTPUT/"fp_performance_observation_sensitivity.csv",index=False)
    conn=connectors(data,current);conn.to_csv(OUTPUT/"fp_performance_connectors.csv",index=False)
    influence=performance_influence(data,teammate,sigma,conn);influence.to_csv(OUTPUT/"fp_performance_connector_influence.csv",index=False);influence.to_csv(OUTPUT/"fp_performance_block_leaveout.csv",index=False)
    soft_grid=np.unique(np.r_[np.arange(-2.0,.001,.25),np.arange(-1.6,-.59,.05)])
    hard_grid=np.arange(-.25,1.251,.10)
    soft_prof=profile(data,teammate,sigma,"performance_SOFT_vs_MEDIUM",soft_grid);soft_prof.to_csv(OUTPUT/"fp_performance_soft_profile.csv",index=False)
    hard_prof=profile(data,teammate,sigma,"performance_HARD_vs_MEDIUM",hard_grid);hard_prof.to_csv(OUTPUT/"fp_performance_hard_profile.csv",index=False)
    status=[]
    for compound,parameter,frame,bounded,lower,upper in (
        ("HARD","performance_HARD_vs_MEDIUM",hard_prof,value(current,"performance_HARD_vs_MEDIUM"),0,1),
        ("SOFT","performance_SOFT_vs_MEDIUM",soft_prof,value(current,"performance_SOFT_vs_MEDIUM"),-1,0)):
        support=frame[frame.delta_cost_from_profile_minimum<=1];minimum=frame.loc[frame.profile_cost.idxmin()]
        status.append({"compound":compound,"parameter":parameter,"statistical_fit_s":bounded,
            "configured_lower_bound_s":lower,"configured_upper_bound_s":upper,
            "boundary_hit":abs(bounded-lower)<1e-5 or abs(bounded-upper)<1e-5,
            "profile_minimum_s":minimum.fixed_value,"profile_delta_cost_le_1_low_s":support.fixed_value.min(),
            "profile_delta_cost_le_1_high_s":support.fixed_value.max(),"engineering_candidate_s":np.nan,
            "engineering_provenance":"not selected in this audit; statistical failure remains visible"})
    pd.DataFrame(status).to_csv(OUTPUT/"fp_performance_engineering_status.csv",index=False)
    eng=pd.read_csv(OUTPUT/"fp_engineering_degradation_results.csv");get=lambda c,t:float(eng[(eng.compound.eq(c))&eng.output_type.eq(t)].estimate_s_per_lap.iloc[0])
    nuisance_values={c:get(c,"nuisance_assisted") for c in COMPOUNDS}
    shifts=degradation_profiles(data,teammate,sigma,current,nuisance_values)
    refits=external_refits(data,teammate,sigma,current);refits.to_csv(OUTPUT/"fp_degradation_external_refit.csv",index=False)
    external_rows=[]
    for c in COMPOUNDS:
        vals={"direct":get(c,"direct_no_order"),"nuisance_assisted":get(c,"nuisance_assisted"),"engineering":get(c,"engineering"),"official_external_benchmark":OFFICIAL[c]}
        for kind,v in vals.items():external_rows.append({"compound":c,"estimate_type":kind,"estimate_s_per_lap":v,"official_benchmark_s_per_lap":OFFICIAL[c],"difference_from_benchmark_s_per_lap":v-OFFICIAL[c],"provenance":"external_only" if kind.startswith("official") else kind})
    external=pd.DataFrame(external_rows);external.to_csv(OUTPUT/"fp_degradation_external_benchmark.csv",index=False)
    # Include common-shift profiles as typed rows in the external-refit deliverable.
    shifts.assign(record_type="common_shift_profile").to_csv(OUTPUT/"fp_degradation_common_shift_profile.csv",index=False)
    fuel=fuel_profile(data,teammate,sigma);fuel.to_csv(OUTPUT/"fp_fuel_degradation_profile.csv",index=False)
    continuity=continuity_diagnostic(ledger,groups);continuity.to_csv(OUTPUT/"fp_longrun_continuity_variability_diagnostic.csv",index=False)
    priority=priority_table(ledger,continuity,conn);priority.to_csv(OUTPUT/"fp_manual_review_priority_updated.csv",index=False)
    soft_audit=soft_lap_audit(full,data,prepared,current);soft_audit.to_csv(OUTPUT/"fp_soft_longrun_lap_audit.csv",index=False)
    soft_robust=soft_robustness(soft_audit);soft_robust.to_csv(OUTPUT/"fp_soft_longrun_robustness.csv",index=False)
    review_plot(priority[priority.review_tier.eq("Tier 1")].run_id.head(12),groups,ledger,"fp_manual_review_tier1_relative.png","Tier 1 normalized engineering review")
    review_plot(priority[priority.review_tier.eq("Tier 2")].run_id.head(15),groups,ledger,"fp_manual_review_tier2_relative.png","Tier 2 normalized engineering review")
    failures=priority[priority.uncertain_breakdown.eq("NearLongRunFailedContinuityOrVariability")].sort_values(["continuity_rule_sensitive","one_lap_variability_sensitive"],ascending=False)
    review_plot(failures.run_id.head(15),groups,ledger,"fp_longrun_failure_reference_panels.png","Near-LongRun continuity and variability failures")
    overview_plots(laps,ledger,groups);other_plots(soft_prof,hard_prof,conn,external,fuel,soft_audit)
    # Register user-supplied benchmark without changing any estimator value.
    evidence_path=OUTPUT/"fp_tyre_evidence_report.json"
    if evidence_path.exists():
        package=json.loads(evidence_path.read_text());package["external_context"]=[{
            "source":"official F1 pre-race graphic supplied by user","source_url":"","published_at":"",
            "category":"degradation estimate","statement":f"Official pre-race {compound} degradation engineering benchmark for 2026 R11",
            "quantitative_value":benchmark,"unit":"s/lap","compound":compound,
            "relevance":"benchmark only; not used as likelihood prior, constraint, or automatic fallback","usage":"benchmark only"}
            for compound,benchmark in OFFICIAL.items()]
        evidence_path.write_text(json.dumps(package,indent=2)+"\n")
    report(soft_prof,hard_prof,influence,conn,refits,fuel,continuity,priority,soft_robust,external)


if __name__=="__main__":main()
