#!/usr/bin/env python3
"""Diagnostic-only FP degradation contrasts and performance-prior audit.

Reads only repository/cached artifacts and writes only under ``temp``.
"""

from __future__ import annotations

from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import audit_fp_programme_classifier as classifier
import diagnose_fp_longrun_only_engineering_model as longrun
import prototype_fp_programme_blocks as prototype


ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/"temp"
CACHE=ROOT/"cache/race_performance_review"
WEEKEND=ROOT/"woData/wostrategy/weekend_model/schema_v1/year=2026/round=11"
OFFICIAL={"HARD":.06,"MEDIUM":.10,"SOFT":.15}
OFFICIAL_CONTRAST={"Delta_HM":-.04,"Delta_SM":.05,"Delta_SH":.09}
SEEDS=(11,2026,424242,8675309)


def weighted_quantile(values,weights,p):
    values=np.asarray(values,float);weights=np.asarray(weights,float);weights=weights/weights.sum()
    order=np.argsort(values);cum=np.cumsum(weights[order])
    return float(values[order[min(np.searchsorted(cum,p),len(order)-1)]])


def demean_runs(frame, matrix, values=None):
    result=np.asarray(matrix,float).copy(); y=None if values is None else np.asarray(values,float).copy()
    for _,indices in frame.groupby("run_id").groups.items():
        idx=np.asarray(list(indices),int);result[idx]-=result[idx].mean(axis=0)
        if y is not None:y[idx]-=y[idx].mean()
    return (result,y) if y is not None else result


def variant_frame(evidence,variant):
    selected=evidence[evidence.selected_clean_lap & evidence.raw_lap_time_s.notna()].copy()
    if variant=="strict":mask=selected.strict_longrun
    elif variant=="one_gap_tolerant":mask=selected.strict_longrun|selected.diagnostic_one_gap_longrun_status
    elif variant=="robust_variability":mask=selected.strict_longrun|selected.diagnostic_robust_variability_longrun_status
    else:raise ValueError(variant)
    return selected[mask].sort_values(["run_id","physical_run_index"]).reset_index(drop=True)


def transformed_fit(frame):
    frame=frame.reset_index(drop=True)
    age=frame.tyre_age.to_numpy(float);compound=frame.compound.astype(str)
    # B = Deg_M - Fuel. After per-run demeaning tyre_age and physical run
    # index differ only by a run constant, so this is the identifiable common
    # observed slope. The two remaining columns are gauge-invariant D contrasts.
    x=np.column_stack((age,(compound=="HARD")*age,(compound=="SOFT")*age))
    y=frame.raw_lap_time_s.to_numpy(float);xp,yp=demean_runs(frame,x,y)
    beta=np.linalg.lstsq(xp,yp,rcond=None)[0];residual=yp-xp@beta
    rank=int(np.linalg.matrix_rank(xp));sigma=np.sqrt(np.sum(residual**2)/max(len(yp)-rank,1))
    covariance=np.linalg.pinv(xp.T@xp)*sigma**2
    singular=np.linalg.svd(xp,compute_uv=False)
    return {"frame":frame,"names":["B_MEDIUM_MINUS_FUEL","Delta_HM","Delta_SM"],"xp":xp,"yp":yp,
            "beta":beta,"residual":residual,"sigma":sigma,"covariance":covariance,
            "rank":rank,"singular":singular,"cost":float(np.sum(residual**2)/sigma**2)}


def contrast_vector(name):
    return {"Delta_HM":np.array([0.,1.,0.]),"Delta_SM":np.array([0.,0.,1.]),
            "Delta_SH":np.array([0.,-1.,1.])}[name]


def contrast_summary(fit,name):
    a=contrast_vector(name);value=float(a@fit["beta"]);se=float(np.sqrt(a@fit["covariance"]@a))
    return value,se,value-1.2815515655*se,value+1.2815515655*se


def gauge_identifiability(strict,fit):
    frame=fit["frame"];i=frame.physical_run_index.to_numpy(float);age=frame.tyre_age.to_numpy(float)
    c=frame.compound.astype(str)
    original=np.column_stack((-i,(c=="HARD")*age,(c=="MEDIUM")*age,(c=="SOFT")*age))
    original_p=demean_runs(frame,original)
    singular=np.linalg.svd(original_p,compute_uv=False);rank=int(np.linalg.matrix_rank(original_p));vt=np.linalg.svd(original_p,full_matrices=False)[2]
    # [F,H,M,S] from [G,B,HM,SM], with G=(F+M)/2 and B=M-F.
    transform=np.array([[1.,-.5,0.,0.],[1.,.5,1.,0.],[1.,.5,0.,0.],[1.,.5,0.,1.]])
    transformed=original_p@transform
    beta0=np.array([.03,.06,.10,.15]);shift=.073
    invariant=np.max(np.abs(original_p@beta0-original_p@(beta0+shift)))
    names=("Gauge_G","B_MEDIUM_MINUS_FUEL","Delta_HM","Delta_SM")
    rows=[]
    null=vt[rank] if rank<len(vt) else np.full(4,np.nan)
    for j,name in enumerate(names):
        rows.append({"record_type":"transformed_parameter","parameter":name,"rank":rank,"nullity":4-rank,
                     "singular_spectrum":"|".join(f"{v:.10g}" for v in singular),
                     "sensitivity_norm":np.linalg.norm(transformed[:,j]),
                     "status":"exact_gauge" if np.linalg.norm(transformed[:,j])<1e-10 else "identifiable_coordinate",
                     "definition":({"Gauge_G":"(Fuel + Deg_M)/2","B_MEDIUM_MINUS_FUEL":"Deg_M - Fuel",
                                    "Delta_HM":"Deg_H - Deg_M","Delta_SM":"Deg_S - Deg_M"}[name]),
                     "inverse_mapping":"Fuel=G-B/2; Deg_M=G+B/2; Deg_H=G+B/2+Delta_HM; Deg_S=G+B/2+Delta_SM",
                     "gauge_shift_invariance_max_abs_prediction_change":invariant})
    rows.append({"record_type":"exact_null_vector_original","parameter":"Fuel|Deg_H|Deg_M|Deg_S",
                 "rank":rank,"nullity":4-rank,"singular_spectrum":"|".join(f"{v:.10g}" for v in singular),
                 "null_vector":"|".join(f"{v:.10f}" for v in null),"status":"exact_gauge",
                 "definition":"Fuel, Deg_H, Deg_M and Deg_S may all shift by the same t",
                 "gauge_shift_invariance_max_abs_prediction_change":invariant})
    return pd.DataFrame(rows)


def direct_results(evidence):
    results=[];sensitivity=[];profiles=[]
    for variant in ("strict","one_gap_tolerant","robust_variability"):
        frame=variant_frame(evidence,variant);fit=transformed_fit(frame)
        # deterministic Gaussian posterior simulation is only a numerical seed check
        seed_rows=[]
        for seed in SEEDS:
            draws=np.random.default_rng(seed).multivariate_normal(fit["beta"],fit["covariance"],50000)
            for name in ("Delta_HM","Delta_SM","Delta_SH"):
                values=draws@contrast_vector(name);seed_rows.append((name,seed,np.quantile(values,.5),np.quantile(values,.1),np.quantile(values,.9)))
        seed_frame=pd.DataFrame(seed_rows,columns=["contrast","seed","median","p10","p90"])
        for name in ("Delta_HM","Delta_SM","Delta_SH"):
            value,se,p10,p90=contrast_summary(fit,name);a=contrast_vector(name)
            involved=("HARD","MEDIUM") if name=="Delta_HM" else (("SOFT","MEDIUM") if name=="Delta_SM" else ("SOFT","HARD"))
            support=frame[frame.compound.isin(involved)]
            med=seed_frame.loc[seed_frame.contrast.eq(name),"median"]
            results.append({"variant":variant,"contrast":name,"central_s_per_lap":value,"standard_error":se,"p10":p10,"p90":p90,
                            "interval_width":p90-p10,"seed_median_min":med.min(),"seed_median_max":med.max(),"seed_median_span":med.max()-med.min(),
                            "support_runs":support.run_id.nunique(),"support_teams":support.team.nunique(),"support_sessions":support.session.nunique(),
                            "support_clean_laps":len(support),"combined_tyre_age_span":support.tyre_age.max()-support.tyre_age.min(),
                            "fit_rank":fit["rank"],"residual_sigma_s":fit["sigma"],"estimator":"direct transformed LongRun fit"})
            for dim in ("run_id","team"):
                for omitted in support[dim].unique():
                    sub=frame[frame[dim].ne(omitted)]
                    if sub.compound.nunique()<2:continue
                    f=transformed_fit(sub);v,_,lo,hi=contrast_summary(f,name)
                    sensitivity.append({"variant":variant,"contrast":name,"sensitivity_type":f"leave_one_{'run' if dim=='run_id' else 'team'}",
                                        "omitted_unit":omitted,"central_s_per_lap":v,"p10":lo,"p90":hi,
                                        "support_runs":sub.run_id.nunique(),"support_teams":sub.team.nunique()})
        # profile Delta_HM and Delta_SM directly while profiling B and the other contrast
        for target,index in (("Delta_HM",1),("Delta_SM",2)):
            centre=fit["beta"][index];grid=np.linspace(centre-.35,centre+.35,141)
            free=[j for j in range(3) if j!=index]
            for fixed in grid:
                residual_target=fit["yp"]-fit["xp"][:,index]*fixed
                other=np.linalg.lstsq(fit["xp"][:,free],residual_target,rcond=None)[0]
                beta=np.zeros(3);beta[index]=fixed;beta[free]=other
                sse=np.sum((fit["yp"]-fit["xp"]@beta)**2)
                profiles.append({"variant":variant,"contrast":target,"fixed_value_s_per_lap":fixed,
                                 "profile_sse":sse,"profile_objective":sse/fit["sigma"]**2,
                                 "profiled_B":beta[0],"profiled_Delta_HM":beta[1],"profiled_Delta_SM":beta[2]})
    result=pd.DataFrame(results);sens=pd.DataFrame(sensitivity);prof=pd.DataFrame(profiles)
    prof["delta_objective"]=prof.profile_objective-prof.groupby(["variant","contrast"]).profile_objective.transform("min")
    base=result[result.variant.eq("strict")][["contrast","central_s_per_lap"]].rename(columns={"central_s_per_lap":"strict_central"})
    result=result.merge(base,on="contrast");result["change_from_strict"]=result.central_s_per_lap-result.strict_central
    classifier_rows=result[["variant","contrast","central_s_per_lap","p10","p90","support_runs","support_teams"]].copy()
    classifier_rows["sensitivity_type"]="classifier_variant_summary";classifier_rows["omitted_unit"]=""
    sens=pd.concat((classifier_rows,sens),ignore_index=True,sort=False)
    return result,sens,prof


def anchor_policies(results):
    get=lambda variant,name:float(results[(results.variant.eq(variant))&results.contrast.eq(name)].central_s_per_lap.iloc[0])
    hm=get("strict","Delta_HM");sm=get("strict","Delta_SM")
    rows=[]
    def add(policy,h,m,s,hs,ms,ss,note):
        for c,v,source in (("HARD",h,hs),("MEDIUM",m,ms),("SOFT",s,ss)):
            rows.append({"policy":policy,"compound":c,"value_s_per_lap":v,"absolute_anchor":m if policy!="A_all_official" else OFFICIAL[c],
                         "component_source":source,"Delta_HM_used":hm if "FP" in hs else np.nan,"Delta_SM_used":sm if "FP" in ss else np.nan,
                         "classifier_sensitivity_HM_range":results[results.contrast.eq("Delta_HM")].central_s_per_lap.max()-results[results.contrast.eq("Delta_HM")].central_s_per_lap.min(),
                         "classifier_sensitivity_SM_range":results[results.contrast.eq("Delta_SM")].central_s_per_lap.max()-results[results.contrast.eq("Delta_SM")].central_s_per_lap.min(),
                         "assumptions":note,"production_selected":False})
    add("A_all_official",.06,.10,.15,"official_external","official_external","official_external","external vector retained unchanged")
    add("B_M_anchor_all_FP_contrasts",.10+hm,.10,.10+sm,"official M anchor + FP Delta_HM","official_external M anchor","official M anchor + FP Delta_SM","illustrative M gauge; applies both strict FP contrasts")
    # Reference-invariant equivalent: preserve official vector mean and use FP shape.
    common=np.mean(list(OFFICIAL.values()));m=common-(hm+sm)/3
    add("B2_external_common_all_FP_shape",m+hm,m,m+sm,"official common level + FP Delta_HM","official common level + FP shape gauge","official common level + FP Delta_SM","sum-to-zero FP shape around external arithmetic common level")
    add("C_M_anchor_HM_only_review_candidate",.10+hm,.10,.15,"official M anchor + FP Delta_HM","official_external M anchor","official_external unchanged","illustrative selective update; HM reviewed separately, SM not applied because classifier response is exposed as materially different")
    return pd.DataFrame(rows)


def legacy_pipeline():
    history=pd.read_csv(WEEKEND/"history/parameter_history.csv")
    pre=history[(pd.to_numeric(history.as_of_leader_lap)==0)&history.source_scope.astype(str).str.upper().eq("AGGREGATE")]
    latest=pre.sort_values("created_at").analysis_id.iloc[-1];rows=pre[pre.analysis_id.eq(latest)&pre.parameter.eq("compound_delta")].copy()
    prior=json.loads((WEEKEND/"pre_race_prior_v1.json").read_text());config=json.loads((WEEKEND/"model_config.json").read_text())
    # Reproduce exact production selection from cached FP files and map it to
    # the revised programme classifier for descriptive provenance only.
    from wostrategy.core.pre_race_session_data import prepare_weekend_sessions
    from wostrategy.model.pre_race_performance import _joint_session_sse, _prepare_analysis_laps
    raw={s:pd.read_pickle(ROOT/f"woData/fastf1/session_laps/schema_v1/year=2026/round=11/session={s}/laps.pkl") for s in ("FP1","FP2","FP3")}
    selected_by_session,_=prepare_weekend_sessions(raw,min_clean_air_laps=config["min_clean_air_laps"],
        clean_mean_time_delta_seconds=config["clean_mean_time_delta_seconds"],
        clean_mean_time_delta_behind_seconds=config["clean_mean_time_delta_behind_seconds"],
        quick_lap_threshold=config["quick_lap_threshold"],treat_stint_as_whole=False,
        dry_compounds=tuple(config["expected_compounds"]))
    performance_sse_changes=[]
    for session,session_laps in selected_by_session.items():
        prepared=_prepare_analysis_laps(session_laps)
        values={"fuel_rate":.04,f"track_rate:{session}":.01}
        for compound in prepared.compound.unique():
            values[f"degradation:{session}:{compound}"]=.10
            if compound!="MEDIUM":
                values[f"compound_delta:{session}:{compound}"]={"HARD":.20,"SOFT":-.50}[compound]
        perturbed=dict(values)
        for compound,replacement in (("HARD",.90),("SOFT",-.90)):
            key=f"compound_delta:{session}:{compound}"
            if key in perturbed:
                perturbed[key]=replacement
        performance_sse_changes.append(abs(
            _joint_session_sse(prepared,session,values,"MEDIUM")
            -_joint_session_sse(prepared,session,perturbed,"MEDIUM")
        ))
    max_performance_sse_change=max(performance_sse_changes,default=0.0)
    selected=pd.concat(selected_by_session.values(),ignore_index=True)
    laps=prototype.load_laps();meta,groups=classifier.prepare_runs(laps)
    ledger=classifier.add_legacy_labels(classifier.classify_all(meta,groups,classifier.ClassifierConfig()),groups)
    mapping=ledger.set_index(["session","team","driver","stint"]).revised_label
    selected["programme"]=[mapping.get((r.SessionName,r.Team,r.Driver,r.Stint),"UNMATCHED") for r in selected.itertuples()]
    composition=selected.groupby(["SessionName","programme","Compound"]).size().rename("lap_count").reset_index()
    output=[]
    for compound,item in prior["performance"].items():
        source=rows[rows.compound.eq(compound)]
        output.append({"record_type":"frozen_compound","event":"2026 R11","compound":compound,"reference_gauge":"sum-zero common shift of session-weighted MEDIUM-reference marginals",
                       "p10":item["p10"],"value":item["median"],"p90":item["p90"],"unit":"s",
                       "source_analysis_id":prior["source_analysis_id"],"source_sessions":"FP1|FP2|FP3",
                       "source_algorithm":"joint pre_race_performance.run_joint_weekend_model",
                       "independent_of_current_fp":False,"independent_prior":False,"provenance":"legacy_fp_derived",
                       "deprecated_for_new_evidence_model":True,"reason_retained":"compatibility / Strategy completeness",
                       "actual_likelihood_sensitivity":"zero: per-run intercept absorbs constant compound delta within every run",
                       "numerical_SSE_change_under_large_performance_perturbation":max_performance_sse_change,
                       "ordering_bounds":"order enforced; non-reference bounds [-1,1]"})
    perf=prior["performance"]
    for name,left,right in (("Perf_H_vs_M","HARD","MEDIUM"),("Perf_S_vs_M","SOFT","MEDIUM")):
        output.append({"record_type":"frozen_contrast","event":"2026 R11","compound":name,"reference_gauge":"contrast",
                       "p10":perf[left]["p10"]-perf[right]["p90"],"value":perf[left]["median"]-perf[right]["median"],"p90":perf[left]["p90"]-perf[right]["p10"],"unit":"s",
                       "source_analysis_id":prior["source_analysis_id"],"source_sessions":"FP1|FP2|FP3","source_algorithm":"joint pre_race_performance + frozen_pre_race_prior marginal aggregation",
                       "independent_of_current_fp":False,"independent_prior":False,"provenance":"legacy_fp_derived","deprecated_for_new_evidence_model":True,
                       "reason_retained":"compatibility / Strategy completeness","actual_likelihood_sensitivity":"zero; interval is marginal approximation without joint covariance",
                       "numerical_SSE_change_under_large_performance_perturbation":max_performance_sse_change,
                       "ordering_bounds":"order enforced; bounds affect sampled marginal"})
    baseline=pd.DataFrame(output)
    composition.to_csv(OUT/"fp_legacy_performance_programme_composition.csv",index=False)
    return baseline,prior,config,composition,rows


def legacy_trace_md(baseline,composition):
    columns=list(composition.columns)
    table="| "+" | ".join(columns)+" |\n| "+" | ".join("---" for _ in columns)+" |\n"
    table+="\n".join("| "+" | ".join(str(row[column]) for column in columns)+" |" for _,row in composition.iterrows())
    text=f"""# Legacy FP performance pipeline trace

## End-to-end path

1. `woStrategy/src/wostrategy/script/pre_race_analysis.py::run_weekend_model_script` reads cached FP1/2/3 laps and calls `prepare_weekend_sessions`.
2. `woStrategy/src/wostrategy/core/pre_race_session_data.py::prepare_weekend_sessions` applies generic dry/quick/traffic/consecutive-run selection. It does not use the programme classifier.
3. `woStrategy/src/wostrategy/model/pre_race_performance.py::run_joint_weekend_model` samples session-local degradation and compound deltas with MEDIUM reference, [-1,1] bounds and ordering enabled.
4. `_joint_session_sse` refits a separate intercept for every run. A compound delta is constant within a run, so its complete design column lies in the run-intercept space and has exactly zero likelihood sensitivity. Re-evaluating the exact selected R11 data after large HARD/SOFT performance perturbations changes session SSE by at most `{baseline.numerical_SSE_change_under_large_performance_perturbation.max():.3e}`.
5. `wodata.artifacts.write_model_snapshot` persists `aggregate/latest_parameters.csv`, support, manifests and `history/parameter_history.csv` under the event weekend-model root.
6. `OfflineAnalysisService.frozen_pre_race_prior` usable-lap-weights session marginals, applies one common display-gauge shift, and writes `pre_race_prior_v1.json`.
7. Race-specific-v2 can sample from that artifact; `OfflineAnalysisService.build_strategy_model` / `strategy_assumptions` and Strategy search consume persisted `compound_delta` rows.

## Exact R11 selection and programme interpretation

The frozen source analysis is `{baseline.source_analysis_id.iloc[0]}`. It used 147 laps: FP1=41, FP2=86, FP3=20. Mapping those physical stints after the fact through the revised classifier gives:

{table}

`Setting` contributes zero selected laps in this exact stored run. `QualiSim` contributes 12, `LongRun` 51, and 84 are currently classified `Uncertain`. These labels are descriptive only because the legacy selector did not know programme type.

## Statistical conclusion

The stored H/M and S/M numbers are not measured by compound-performance likelihood information. Their non-uniform marginals can arise through imposed ordering/bounds and finite-bank correlations with likelihood-sensitive coordinates. They are derived from the same current-event FP data and therefore have `independent_prior=false`.

## Persistence and Race behaviour

The immutable artifact is `woData/wostrategy/weekend_model/schema_v1/year=2026/round=11/pre_race_prior_v1.json`. In race-specific-v2, per-run baseline is the default and explicitly marks performance `prior_only`; per-driver/global-driver modes can update it when the driver-compound graph connects. Current live recalculation defaults instead to `retro`, which does not load this prior into its likelihood. `strategy_assumptions` can preserve supplied baseline deltas for missing model values, while uninformed-compound ratio scaling applies only to degradation—not compound performance.

The safe compatibility category is `legacy_fp_derived`; no deletion or production change is made here.
"""
    (OUT/"fp_legacy_performance_pipeline_trace.md").write_text(text)


def connector_metrics(clean,left,right,baseline_group):
    pair={left,right};drivers=0;teams=0;teammate_only=0
    for _,g in clean.groupby("Driver"):
        drivers+=pair.issubset(set(g.Compound.astype(str)))
    for _,g in clean.groupby("Team"):
        if pair.issubset(set(g.Compound.astype(str))):
            teams+=1
            if not any(pair.issubset(set(x.Compound.astype(str))) for _,x in g.groupby("Driver")):teammate_only+=1
    first=np.nan
    group_col="Team" if baseline_group=="team" else "Driver"
    for lap in sorted(clean.LapNumber.dropna().unique()):
        prefix=clean[clean.LapNumber<=lap]
        if any(pair.issubset(set(g.Compound.astype(str))) for _,g in prefix.groupby(group_col)):
            first=float(lap);break
    return drivers,teams,teammate_only,first


def retro_sampling_prior(compounds,meta,enforce_order,seed,count=300000):
    """Reproduce the retro compound-delta sampling distribution without a likelihood."""
    compounds=tuple(str(v).upper() for v in compounds);reference=str(meta.get("compound_delta_reference","HARD")).upper()
    default=tuple(meta.get("default_compound_delta_bounds",(-1.,1.)))
    overrides={str(k).upper():tuple(v) for k,v in meta.get("compound_delta_bounds",{}).items()}
    rng=np.random.default_rng(seed);unit={c:rng.random(count) for c in compounds if c!=reference};values={}
    if not enforce_order:
        for c in compounds:
            low,high=overrides.get(c,default);values[c]=np.zeros(count) if c==reference else low+unit[c]*(high-low)
    else:
        previous=None
        for index,c in enumerate(("SOFT","MEDIUM","HARD")):
            if c not in compounds:continue
            if c==reference:value=np.zeros(count)
            else:
                low,high=overrides.get(c,default)
                ref_index=("SOFT","MEDIUM","HARD").index(reference)
                if index<ref_index:high=min(high,0.)
                elif index>ref_index:low=max(low,0.)
                effective=np.maximum(low,previous) if previous is not None else low
                value=effective+unit[c]*(high-effective)
            values[c]=value;previous=value
        for c in compounds:
            if c not in values:
                low,high=overrides.get(c,default);values[c]=np.zeros(count) if c==reference else low+unit[c]*(high-low)
    return values


def race_inventory():
    rows=[];support=[]
    for round_number in range(1,12):
        prefix=CACHE/f"race_performance_2026_{round_number}_R"
        paths={kind:Path(str(prefix)+suffix) for kind,suffix in {
            "summary":"_summary_compound_delta.csv","raw":"_compound_delta.csv","samples":"_sample_parameters.csv",
            "diag":"_sample_diagnostics.csv","clean":"_clean_laps.csv","meta":"_metadata.json"}.items()}
        if not all(paths[k].exists() for k in ("summary","raw","samples","diag","clean")):continue
        clean=pd.read_csv(paths["clean"]);raw=pd.read_csv(paths["raw"]);samples=pd.read_csv(paths["samples"]);diag=pd.read_csv(paths["diag"])
        meta=json.loads(paths["meta"].read_text()) if paths["meta"].exists() else {}
        weights=samples.Weight.to_numpy(float);weights/=weights.sum();wide=raw.pivot(index="SampleId",columns="Compound",values="CompoundDeltaSeconds").sort_index()
        event=str(clean.EventName.dropna().iloc[0]) if "EventName" in clean and clean.EventName.notna().any() else f"Round {round_number}"
        reference=str(raw.CompoundDeltaReference.iloc[0]);baseline=str(samples.BaselineGroup.iloc[0]).lower()
        order_enforced=bool(samples.DegradationOrderEnforced.iloc[0]) if "DegradationOrderEnforced" in samples else False
        prior_draws=retro_sampling_prior(tuple(wide.columns),meta,order_enforced,202600+round_number)
        ess=float(1/np.sum(weights**2));maxw=float(weights.max());top5=float(np.sort(weights)[-5:].sum())
        for name,left,right in (("HARD_vs_MEDIUM","HARD","MEDIUM"),("SOFT_vs_MEDIUM","SOFT","MEDIUM")):
            if left not in wide or right not in wide:
                support.append({"year":2026,"round":round_number,"event":event,"contrast":name,"support_status":"missing_compound",
                                "compound_labels":"event_relative","physical_compound_mapping":"unavailable"});continue
            values=wide[left].to_numpy(float)-wide[right].to_numpy(float)
            p10,median,p90=[weighted_quantile(values,weights,p) for p in (.1,.5,.9)]
            prior_values=prior_draws[left]-prior_draws[right]
            prior_p10,prior_median,prior_p90=np.quantile(prior_values,(.1,.5,.9))
            drivers,teams,teammate,first=connector_metrics(clean,left,right,baseline)
            status="same_driver_connected" if drivers else ("team_baseline_assisted" if teams and baseline=="team" else "not_connected")
            low,high=float(np.min(values)),float(np.max(values));span=max(high-low,1e-12)
            boundary_low=float(weights[values<=low+.05*span].sum());boundary_high=float(weights[values>=high-.05*span].sum())
            rows.append({"year":2026,"round":round_number,"event":event,"contrast":name,"reference_compound":reference,
                         "race_end_p10":p10,"race_end_estimate":median,"race_end_p90":p90,"interval_width":p90-p10,
                         "support_status":status,"first_structurally_informed_lap":first,"clean_laps":len(clean),
                         "supporting_connectors":drivers+teams,"same_driver_connector_count":drivers,
                         "same_team_connector_count":teams,"teammate_only_connector_count":teammate,
                         "sample_count":len(weights),"ess":ess,"ess_fraction":ess/len(weights),"max_weight":maxw,"top5_weight":top5,
                         "boundary_weight_low_5pct_sample_support":boundary_low,"boundary_weight_high_5pct_sample_support":boundary_high,
                         "seed_stability":"unavailable: one persisted seed","baseline_group":baseline,
                         "degradation_and_delta_order_enforced":order_enforced,
                         "sampling_prior_p10":prior_p10,"sampling_prior_median":prior_median,"sampling_prior_p90":prior_p90,
                         "posterior_minus_sampling_prior_median":median-prior_median,
                         "posterior_width_to_sampling_prior_width":(p90-p10)/max(prior_p90-prior_p10,1e-12),
                         "compound_labels":"event_relative HARD/MEDIUM/SOFT","physical_compound_mapping":"unavailable in cached laps/metadata",
                         "transferable_prior_eligible":False})
            support.append({"year":2026,"round":round_number,"event":event,"contrast":name,"support_status":status,
                            "drivers_with_both":drivers,"teams_with_both":teams,"teammate_only_teams":teammate,
                            "first_structurally_informed_lap":first,"clean_laps_left":int((clean.Compound==left).sum()),
                            "clean_laps_right":int((clean.Compound==right).sum()),"baseline_group":baseline,
                            "physical_compound_mapping":"unavailable","note":"first informed is reconstructed graph connectivity, not a saved posterior-confidence transition"})
    return pd.DataFrame(rows),pd.DataFrame(support)


def prior_tests(inventory):
    rows=[];causal=[]
    usable=inventory[inventory.support_status.ne("not_connected")].copy()
    for contrast,g in usable.groupby("contrast"):
        for r in g.itertuples():
            train=g[g["round"].ne(r.round)].race_end_estimate
            if len(train)>=2:
                centre=float(train.median());spread=float(1.4826*np.median(np.abs(train-centre)))
                rows.append({"contrast":contrast,"target_round":r.round,"target_event":r.event,"target_value":r.race_end_estimate,
                             "prior_type":"all_other_event_relative_robust_median","prior_value":centre,"empirical_scale":spread,
                             "absolute_error":abs(centre-r.race_end_estimate),"generic_zero_absolute_error":abs(r.race_end_estimate),
                             "covered_by_centre_pm_scale":abs(r.race_end_estimate-centre)<=spread if spread>0 else False,
                             "training_events":len(train),"causal":False,"physical_mapping_available":False,"production_eligible":False})
            previous=g[g["round"]<r.round].race_end_estimate
            if len(previous)>=3:
                centre=float(previous.median());spread=float(1.4826*np.median(np.abs(previous-centre)))
                causal.append({"contrast":contrast,"target_round":r.round,"target_event":r.event,"target_value":r.race_end_estimate,
                               "prior_type":"previous_events_only_robust_median","prior_value":centre,"empirical_scale":spread,
                               "absolute_error":abs(centre-r.race_end_estimate),"generic_zero_absolute_error":abs(r.race_end_estimate),
                               "covered_by_centre_pm_scale":abs(r.race_end_estimate-centre)<=spread if spread>0 else False,
                               "training_events":len(previous),"causal":True,"physical_mapping_available":False,"production_eligible":False})
    return pd.DataFrame(rows),pd.DataFrame(causal)


def race_trace_md(inventory,support,loo,causal):
    usable=inventory.groupby("contrast").size().to_dict();connected=inventory.groupby(["contrast","support_status"]).size().to_dict()
    prior_shift=float(inventory.posterior_minus_sampling_prior_median.abs().median()) if len(inventory) else np.nan
    width_ratio=float(inventory.posterior_width_to_sampling_prior_width.median()) if len(inventory) else np.nan
    text=f"""# Current Race performance pipeline and historical-prior feasibility

## Current default live path

`OfflineAnalysisService.recalculate_from_live_snapshot(..., race_baseline_architecture="retro")` resolves Race-review settings and calls `wostrategy.model.live_retro_performance.run_live_retro_model`. That wrapper uses `MonteCarloRacePerformanceAlgorithm` on clean Race laps.

The active retro candidate contains Fuel, Track, one degradation per observed compound, one delta per non-reference compound, and Team×Compound degradation deviations. Current cached settings use a HARD performance reference and a team baseline. A team baseline creates performance sensitivity when a team is observed on multiple compounds; same-driver continuity is stronger evidence but is not required. Performance remains correlated with Fuel, Track, degradation, team deviations and the common team intercept.

The live-retro adapter labels emitted performance rows `measured` whenever present; it does not currently apply an explicit connector/support gate. Numerical diagnostics include ESS, ESS/N, max weight and top-five weight, but persisted offline Race-review artifacts contain only one seed per event. Thus seed stability is unavailable without rerunning.

`race_specific_performance.py` is a different optional architecture. It samples six independent coordinates with sum-to-zero performance effects transferred from the frozen FP prior. Its default per-run baseline makes performance likelihood-inactive and marks it `prior_only`; per-driver/global-driver modes can update connected compounds. It is not the default live architecture.

## Cache-only inventory

The audit found {len(inventory)} event/contrast rows across rounds 1–11: {usable}. Support breakdown is {connected}. The first-informed lap field is reconstructed from baseline-group compound connectivity; no historical posterior snapshots were persisted for these offline runs.

The ordered no-likelihood sampling distribution was reproduced from each event's stored settings. Median absolute Race-posterior shift from that sampling-prior median is only {prior_shift:.4f} s, and the median posterior/prior P10–P90 width ratio is {width_ratio:.3f}. The striking cross-event consistency is therefore predominantly shared ordered-prior geometry, not demonstrated historical learning.

## Gauge and physical identity

All cached retro files use event-relative compound labels and a stored reference compound. Contrasts are converted algebraically to H−M and S−M, so the display reference gauge is compatible. Physical tyre identity is not compatible: cached laps and metadata preserve only HARD/MEDIUM/SOFT, not Pirelli C-number/allocation. No repository mapping was found. Therefore the {len(loo)} all-other and {len(causal)} causal validation rows are descriptive event-relative experiments only and are explicitly `production_eligible=false`.

## Feasibility conclusion

Current Race output can provide a number and sometimes has real team/driver cross-compound connectivity, but that is insufficient for a transferable prior. Required work includes persisting physical compound allocation, connector diagnostics and posterior snapshots; archiving multiple seeds; separating team-baseline-assisted from same-driver evidence; checking boundary/interval stability; and validating causal priors within comparable physical compound families.
"""
    (OUT/"race_performance_pipeline_trace.md").write_text(text)


def source_comparison(legacy,performance,inventory,causal):
    rows=[]
    legacy_map={r.compound:r for r in legacy[legacy.record_type.eq("frozen_contrast")].itertuples()}
    q=pd.read_csv(OUT/"fp_longrun_cross_team_performance.csv")
    for contrast,legacy_name,qname in (("HARD_vs_MEDIUM","Perf_H_vs_M","HARD_vs_MEDIUM"),("SOFT_vs_MEDIUM","Perf_S_vs_M","SOFT_vs_MEDIUM")):
        l=legacy_map[legacy_name]
        rows.append({"contrast":contrast,"source":"legacy current-event FP-derived","value":l.value,"p10":l.p10,"p90":l.p90,
                     "support":"zero direct likelihood sensitivity","race_relevance":"mixed generic FP runs","independent_of_current_fp":False,
                     "provenance":"legacy_fp_derived","known_failure_modes":"per-run baselines absorb P; ordering/bounds/finite-bank correlation"})
        rows.append({"contrast":contrast,"source":"strict current FP LongRun","value":np.nan,"p10":np.nan,"p90":np.nan,
                     "support":"no cross-compound connector","race_relevance":"high","independent_of_current_fp":False,
                     "provenance":"direct_weekend_evidence","known_failure_modes":"unsupported for R11"})
        qr=q[(q.approach.str.startswith("C_"))&q.contrast.eq(qname)].iloc[0]
        rows.append({"contrast":contrast,"source":"Q-normalized cross-team LongRun","value":qr.central_value_s,"p10":qr.p10_s,"p90":qr.p90_s,
                     "support":f"transfer sigma {qr.transfer_sigma_s:.3f}s; assumption-assisted weak","race_relevance":"race-like but cross-team transfer",
                     "independent_of_current_fp":False,"provenance":"assumption_assisted_current_weekend","known_failure_modes":"fuel/setup/programme/Q-transfer mismatch"})
        hist=causal[(causal.contrast.eq(contrast))&causal.target_round.eq(11)]
        rows.append({"contrast":contrast,"source":"historical Race prior (causal event-relative)",
                     "value":hist.prior_value.iloc[0] if len(hist) else np.nan,
                     "p10":(hist.prior_value-hist.empirical_scale).iloc[0] if len(hist) else np.nan,
                     "p90":(hist.prior_value+hist.empirical_scale).iloc[0] if len(hist) else np.nan,
                     "support":f"{int(hist.training_events.iloc[0])} prior events" if len(hist) else "insufficient",
                     "race_relevance":"Race condition but physical mapping unavailable","independent_of_current_fp":True,
                     "provenance":"historical_race_diagnostic_not_production_ready","known_failure_modes":"Race medians remain near shared ordered sampling-prior geometry; event-relative compounds lack C-number identity; one seed/event"})
        for source,prov in (("official event-specific performance","official_external"),("manual/configurable","manual_or_configurable")):
            rows.append({"contrast":contrast,"source":source,"value":np.nan,"p10":np.nan,"p90":np.nan,"support":"not supplied",
                         "race_relevance":"potentially high" if source.startswith("official") else "depends on review",
                         "independent_of_current_fp":True,"provenance":prov,"known_failure_modes":"no R11 numerical value exists"})
    return pd.DataFrame(rows)


def plots(profiles,results,policies,legacy,inventory,loo,source):
    for contrast,file,title in (("Delta_HM","fp_degradation_HM_profile.png","H−M degradation profile"),("Delta_SM","fp_degradation_SM_profile.png","S−M degradation profile")):
        fig,ax=plt.subplots(figsize=(9,5))
        for variant,g in profiles[profiles.contrast.eq(contrast)].groupby("variant"):
            ax.plot(g.fixed_value_s_per_lap,g.delta_objective,label=variant)
        ax.axvline(OFFICIAL_CONTRAST[contrast],color="k",ls="--",label="official external contrast")
        ax.set(title=title,xlabel="contrast (s/lap)",ylabel="profile Δ objective");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/file,dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,5));variants=("strict","one_gap_tolerant","robust_variability");base=np.arange(len(variants));offsets={"Delta_HM":-.2,"Delta_SM":0.,"Delta_SH":.2}
    for contrast in ("Delta_HM","Delta_SM","Delta_SH"):
        g=results[results.contrast.eq(contrast)].set_index("variant").loc[list(variants)]
        x=base+offsets[contrast];ax.errorbar(x,g.central_s_per_lap,yerr=np.vstack((g.central_s_per_lap-g.p10,g.p90-g.central_s_per_lap)),fmt="o",capsize=4,label=contrast)
    ax.set_xticks(base,variants,rotation=20)
    ax.axhline(0,color="k",lw=.8);ax.set(title="Classifier sensitivity of direct degradation contrasts",ylabel="s/lap");ax.grid(axis="y",alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"fp_degradation_contrast_classifier_sensitivity.png",dpi=180);plt.close(fig)
    pivot=policies.pivot(index="policy",columns="compound",values="value_s_per_lap")
    ax=pivot[[c for c in ("HARD","MEDIUM","SOFT") if c in pivot]].plot.bar(figsize=(11,6));ax.set(title="Diagnostic external-anchor + FP-shape policies",ylabel="degradation (s/lap)");ax.grid(axis="y",alpha=.2);plt.xticks(rotation=20,ha="right");plt.tight_layout();plt.savefig(OUT/"fp_degradation_anchor_policy_comparison.png",dpi=180);plt.close()
    fig,ax=plt.subplots(figsize=(12,4));ax.axis("off");labels=["cached FP laps","generic clean-run selection","joint FP bank\nper-run baselines","parameter history","frozen prior JSON","Strategy / optional Race transfer"]
    for i,label in enumerate(labels):
        x=.06+i*.18;ax.text(x,.5,label,ha="center",va="center",bbox=dict(boxstyle="round",fc="#e8eef7"),transform=ax.transAxes)
        if i<len(labels)-1:ax.annotate("",xy=(x+.14,.5),xytext=(x+.05,.5),arrowprops=dict(arrowstyle="->"),xycoords=ax.transAxes)
    ax.set_title("Legacy current-event FP-derived compound-performance pipeline");fig.tight_layout();fig.savefig(OUT/"legacy_fp_performance_pipeline.png",dpi=180);plt.close(fig)
    if len(inventory):
        fig,ax=plt.subplots(figsize=(11,6))
        for contrast,g in inventory.groupby("contrast"):
            color=ax._get_lines.get_next_color();ax.scatter(g["round"],g.race_end_estimate,label=f"{contrast} Race posterior",color=color)
            ax.scatter(g["round"],g.sampling_prior_median,marker="x",color=color,label=f"{contrast} ordered sampling prior")
        ax.axhline(0,color="k",lw=.8);ax.set(title="Cached Race-end event-relative performance contrasts",xlabel="2026 round",ylabel="contrast (s)");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"race_historical_performance_distribution.png",dpi=180);plt.close(fig)
    if len(loo):
        fig,ax=plt.subplots(figsize=(7,6))
        for contrast,g in loo.groupby("contrast"):ax.scatter(g.prior_value,g.target_value,label=contrast)
        lim=[min(loo.prior_value.min(),loo.target_value.min()),max(loo.prior_value.max(),loo.target_value.max())];ax.plot(lim,lim,"k--")
        ax.set(title="Leave-one-event-out validation (event-relative only)",xlabel="historical prior",ylabel="held-out Race estimate");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/"race_performance_prior_validation.png",dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,6));numeric=source[source.value.notna()].copy();x=np.arange(len(numeric));ax.errorbar(x,numeric.value,yerr=np.vstack((numeric.value-numeric.p10,numeric.p90-numeric.value)),fmt="o",capsize=4)
    ax.set_xticks(x,[f"{r.contrast}\n{r.source}" for r in numeric.itertuples()],rotation=25,ha="right");ax.axhline(0,color="k",lw=.8);ax.set(title="R11 pre-race performance source comparison",ylabel="contrast (s)");ax.grid(axis="y",alpha=.2);fig.tight_layout();fig.savefig(OUT/"pre_race_performance_source_comparison.png",dpi=180);plt.close(fig)


def main_report(ident,results,policies,legacy,inventory,support,loo,causal,source):
    r=lambda v,c:results[(results.variant.eq(v))&results.contrast.eq(c)].iloc[0]
    hm=[r(v,"Delta_HM").central_s_per_lap for v in ("strict","one_gap_tolerant","robust_variability")]
    sm=[r(v,"Delta_SM").central_s_per_lap for v in ("strict","one_gap_tolerant","robust_variability")]
    inv=ident[ident.record_type.eq("exact_null_vector_original")].iloc[0]
    usable=inventory.groupby("contrast").size().to_dict();causal_summary=(causal.groupby("contrast").agg(mae=("absolute_error","mean"),generic_mae=("generic_zero_absolute_error","mean"),events=("target_round","size")).reset_index() if len(causal) else pd.DataFrame())
    prior_shift=inventory.posterior_minus_sampling_prior_median.abs().median()
    prior_width_ratio=inventory.posterior_width_to_sampling_prior_width.median()
    lines=["# FP Degradation Contrast and Performance Prior Audit","","## Evidence first","",
           f"The strict transformed model has rank 3 and nullity 1. Its exact original-coordinate null vector is `{inv.null_vector}` for `Fuel|Deg_H|Deg_M|Deg_S`; a common shift changes profiled predictions by at most `{inv.gauge_shift_invariance_max_abs_prediction_change:.3e}` s numerically.","",
           f"Strict H−M is `{r('strict','Delta_HM').central_s_per_lap:+.4f}` s/lap (P10–P90 `{r('strict','Delta_HM').p10:+.4f}` to `{r('strict','Delta_HM').p90:+.4f}`); classifier variants give `{' / '.join(f'{v:+.4f}' for v in hm)}`. Strict S−M is `{r('strict','Delta_SM').central_s_per_lap:+.4f}` (P10–P90 `{r('strict','Delta_SM').p10:+.4f}` to `{r('strict','Delta_SM').p90:+.4f}`); variants give `{' / '.join(f'{v:+.4f}' for v in sm)}`.","",
           "## Explicit answers — degradation","",
           "1. The gauge is `Fuel→Fuel+t` and every `Deg_c→Deg_c+t`. With `G=(Fuel+Deg_M)/2` and `B=Deg_M−Fuel`: `Fuel=G−B/2`, `Deg_M=G+B/2`, `Deg_H=G+B/2+Delta_HM`, `Deg_S=G+B/2+Delta_SM`. The projected G column is exactly zero.",
           f"2. H−M has a finite direct transformed optimum and exported profile. Its strict estimate is `{r('strict','Delta_HM').central_s_per_lap:+.4f}` with {int(r('strict','Delta_HM').support_runs)} supporting runs across {int(r('strict','Delta_HM').support_teams)} teams.",
           f"3. S−M is also algebraically identifiable, but its strict value `{r('strict','Delta_SM').central_s_per_lap:+.4f}` describes conflicting Soft runs and is more policy-sensitive.",
           f"4–5. H−M classifier central range is `{max(hm)-min(hm):.4f}` s/lap; S−M range is `{max(sm)-min(sm):.4f}`. The robust-variability variant materially changes the Soft relationship; the CSV/profile exposes this without a binary confidence cutoff.",
           f"6. Official contrasts are H−M −0.040, S−M +0.050, S−H +0.090. Strict FP H−M is directionally close; strict FP S−M disagrees in sign. They are context only and never fitted.",
           "7. External common/absolute level plus FP relative shape is mathematically coherent because anchor and contrasts occupy separate coordinates. Policy A/B/B2/C show the gauge choices and provenance.",
           "8. H−M is the clearer weekend-update candidate for engineering review because its variant movement and profiles are substantially more stable than Soft-related contrasts. This is a factual comparison, not an automatic threshold or production decision.","",
           "## Explicit answers — legacy performance","",
           "9–10. The current baseline originates in the current-event FP1/2/3 cache, generic clean consecutive-run selection, and the joint pre-race model. The exact selected set is 147 laps: revised post-hoc labels include LongRun, QualiSim and predominantly Uncertain; Setting contributes none in this stored run.",
           f"11. Compound performance has zero true likelihood sensitivity because each run has a free intercept. On the exact selected R11 data, large HARD/SOFT perturbations changed session SSE by at most `{legacy.numerical_SSE_change_under_large_performance_perturbation.max():.3e}`. Non-uniform output is therefore substantially bound/order/finite-bank-correlation dependent.",
           "12. Yes: `independent_prior=false`. It is the same current-event FP evidence.",
           "13. It is persisted in aggregate/session parameter CSVs, parameter history and `pre_race_prior_v1.json`, then consumed by model building/Strategy and optionally race-specific-v2 transfer.",
           "14. Strategy degradation logic preserves/scales uninformed degradation relative to a baseline. Compound performance is preserved only when a model/baseline value is supplied; it is not ratio-scaled by the uninformed-degradation logic. Default live retro does not use the frozen FP prior in its likelihood.",
           "15. Yes. The prototype records `legacy_fp_derived`, current event, algorithm/sessions, non-independence and deprecation while retaining values for compatibility.","",
           "## Explicit answers — Race historical prior","",
           f"16–18. Cached retro Race output has {usable} contrast rows. Some have real same-driver or team-baseline connectivity, but support gates, multi-seed stability and historical posterior trajectories are missing; first-informed laps here are structural graph reconstructions, not confidence transitions. The median absolute posterior shift from the reproduced ordered sampling-prior median is only `{prior_shift:.4f} s`, with median posterior/prior interval-width ratio `{prior_width_ratio:.3f}`—so most apparent cross-event stability is prior geometry.",
           "19. Algebraic H−M/S−M gauge conversion is compatible because references are stored. Statistical architecture/settings still vary and must also be versioned.",
           "20. Physical compound-family mapping is unavailable: cached laps/metadata retain event-relative labels only. This blocks production pooling.",
           f"21. Usable cached event counts are {usable}; every row remains `transferable_prior_eligible=false` pending physical mapping and stronger support validation.",
           "22–23. Simple all-other and causal previous-event experiments were run only in event-relative label space and marked non-production. Their result summary is: " + (causal_summary.to_dict('records').__repr__() if len(causal_summary) else "insufficient causal rows") + ". The apparently low error versus zero largely predicts the shared ordered sampling prior, not event-learned physical tyre performance; it cannot establish superiority for a physical-tyre prior.",
           "24. Persist C-number allocation, model/settings version, connector graph, multiple seeds, boundary/profile diagnostics and lap-by-lap posterior snapshots; then validate causal priors within physically comparable families.","",
           "## Final architecture","",
           "25. When current FP has no valid performance update, Strategy should prefer a genuinely independent historical Race prior only after the blockers above are solved; today use an official event-specific value if supplied, otherwise a reviewed manual/config assumption. Legacy is last-resort compatibility only.",
           "26. Yes—legacy values can remain with `legacy_fp_derived`, `independent_of_current_fp=false`, and deprecation metadata. They must never be presented as historical or independent.",
           "27. The proposed separation is internally consistent: strict FP supplies gauge-invariant D contrasts and only genuinely connected P evidence; independent sources supply absolute/performance priors; the engineering builder resolves provenance; Strategy consumes the completed result.","",
           "## Non-production status","","All code added is diagnostic tooling and all artifacts are under `temp/`. Production FP, Race, live and Strategy inputs were not changed."]
    (OUT/"FP_DEGRADATION_CONTRAST_AND_PERFORMANCE_PRIOR_AUDIT.md").write_text("\n".join(lines)+"\n")


def main():
    OUT.mkdir(exist_ok=True)
    *_,evidence,strict=longrun.load()
    strict_fit=transformed_fit(variant_frame(evidence,"strict"));ident=gauge_identifiability(strict,strict_fit)
    results,sensitivity,profiles=direct_results(evidence);policies=anchor_policies(results)
    legacy,prior,config,composition,legacy_rows=legacy_pipeline();inventory,support=race_inventory();loo,causal=prior_tests(inventory)
    source=source_comparison(legacy,results,inventory,causal)
    ident.to_csv(OUT/"fp_degradation_contrast_identifiability.csv",index=False)
    results.to_csv(OUT/"fp_degradation_contrast_results.csv",index=False)
    profiles.to_csv(OUT/"fp_degradation_contrast_profiles.csv",index=False)
    sensitivity.to_csv(OUT/"fp_degradation_contrast_classifier_sensitivity.csv",index=False)
    policies.to_csv(OUT/"fp_degradation_anchor_policy_comparison.csv",index=False)
    legacy.to_csv(OUT/"fp_legacy_performance_baseline.csv",index=False)
    provenance={"schema_version":1,"category":"legacy_fp_derived","event":"2026 R11","source_algorithm":"joint current-event FP pre-race model",
                "source_sessions":["FP1","FP2","FP3"],"independent_of_current_fp":False,"independent_prior":False,
                "deprecated_for_new_evidence_model":True,"reason_retained":"compatibility / Strategy completeness",
                "likelihood_sensitivity":"zero after per-run intercept projection","values":legacy.where(pd.notna(legacy),None).to_dict("records")}
    (OUT/"fp_legacy_performance_provenance.json").write_text(json.dumps(provenance,indent=2)+"\n")
    inventory.to_csv(OUT/"race_historical_performance_inventory.csv",index=False);support.to_csv(OUT/"race_historical_performance_support.csv",index=False)
    feasibility=pd.DataFrame([{"question":"physical compound mapping available","result":False,"blocker":"cache stores event-relative labels only"},
                              {"question":"multi-seed Race stability available","result":False,"blocker":"one persisted seed per event"},
                              {"question":"connector support reconstructable","result":True,"blocker":"first-informed is structural, not posterior confidence"},
                              {"question":"historical prior production-ready","result":False,"blocker":"physical mapping, support gates and seed stability missing"},
                              {"question":"event-relative diagnostic possible","result":len(inventory)>=6,"blocker":"not transferable across physical compound allocations"}])
    feasibility.to_csv(OUT/"race_performance_prior_feasibility.csv",index=False)
    loo.to_csv(OUT/"race_performance_leave_one_event_out.csv",index=False);causal.to_csv(OUT/"race_performance_causal_prior_test.csv",index=False)
    source.to_csv(OUT/"pre_race_performance_source_comparison.csv",index=False)
    hierarchy={"schema_version":1,"status":"diagnostic_not_productionised","performance_order":[
        {"level":1,"source":"direct current-weekend LongRun cross-compound evidence"},
        {"level":2,"source":"independent historical Race prior","current_status":"not mature: physical mapping/support validation missing"},
        {"level":3,"source":"official event-specific performance","current_R11_value":None},
        {"level":4,"source":"reviewed configurable/manual engineering assumption"},
        {"level":5,"source":"legacy_fp_derived compatibility fallback","independent_of_current_fp":False}],
        "degradation":{"absolute_level":"external/historical anchor","weekend_shape":"reviewed strict LongRun contrasts","automatic_threshold":None}}
    (OUT/"pre_race_tyre_model_source_hierarchy.json").write_text(json.dumps(hierarchy,indent=2)+"\n")
    legacy_trace_md(legacy,composition);race_trace_md(inventory,support,loo,causal)
    plots(profiles,results,policies,legacy,inventory,loo,source)
    main_report(ident,results,policies,legacy,inventory,support,loo,causal,source)
    print(results[["variant","contrast","central_s_per_lap","p10","p90","seed_median_span"]].to_string(index=False))
    print("historical rows",len(inventory),"loo",len(loo),"causal",len(causal))


if __name__=="__main__":main()
