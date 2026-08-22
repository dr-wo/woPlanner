#!/usr/bin/env python3
"""Diagnostic-only FP evidence policy and lap-noise scale comparison."""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import audit_fp_longrun_evidence_selection as longrun
import diagnose_fp_posterior_geometry as geometry
import prototype_fp_programme_blocks as prototype
import run_fp_programmeblock_q_integration as integration


ROOT=Path(__file__).resolve().parents[2];OUTPUT=ROOT/"temp"
SEEDS=integration.SEEDS;SIGMAS=(.35,.40,.45,.50,.60)
PARAMETERS=prototype.PARAMETERS


def inputs():
    laps,ledger,groups,full,current,_,teammate,prepared,selected=longrun.build()
    current_keys=list(zip(current["session"],current["driver"],current["Stint"],current["lap_number"]))
    selected_keys=set(longrun.key_frame(selected))
    clean_base_keys=set(longrun.key_frame(prepared[prepared.IsCleanAirLongRunLap]))
    non_long=~current.programme.eq("LongRun")
    l0=current[non_long|pd.Series([key in selected_keys for key in current_keys],index=current.index)].copy()
    l1=current[non_long|pd.Series([key in clean_base_keys for key in current_keys],index=current.index)].copy()
    return laps,ledger,groups,full,current,l0.reset_index(drop=True),l1.reset_index(drop=True),teammate,prepared,selected


def transformed_blocks(data,sigma_programme,teammate,measurement_sigma):
    x=prototype.build_physical_x(data,"track_time");y=data.corrected_lap_time.to_numpy()
    teammate_sigma={(r.team,r.session,r.driver):r.uncertainty_width_s/2.563 for r in teammate.itertuples()}
    blocks=[]
    for name,g in data.groupby("programme_block",sort=True):
        idx=g.index.to_numpy();codes,runs=pd.factorize(g.run_id,sort=True)
        z=np.zeros((len(g),len(runs)));z[np.arange(len(g)),codes]=1
        variances=[]
        for run in runs:
            first=g[g.run_id.eq(run)].iloc[0]
            ds=teammate_sigma.get((first.team,first.session,first.driver),0)
            variances.append(sigma_programme**2+ds**2)
        lap_sigma=(measurement_sigma[g.programme.iloc[0]] if isinstance(measurement_sigma,dict)
                   else float(measurement_sigma))
        covariance=lap_sigma**2*np.eye(len(g))+z@np.diag(variances)@z.T
        inverse=np.linalg.inv(covariance);ones=np.ones(len(g))
        projection=np.eye(len(g))-np.outer(ones,(ones@inverse)/(ones@inverse@ones))
        transform=np.linalg.cholesky(inverse).T@projection
        blocks.append({"block":name,"indices":idx,"x":transform@x[idx],"y":transform@y[idx],
                       "transform":transform,"programme":g.programme.iloc[0]})
    return blocks


def fit(data,teammate,sigma_programme,measurement_sigma,start=None):
    blocks=transformed_blocks(data,sigma_programme,teammate,measurement_sigma)
    xp,yp=geometry.stack(blocks)
    if start is None:start=np.array([.06,0,0,-.05,0,.01,.02,.5,-.9])
    beta,best,success=geometry.optimize_map(xp,yp,start)
    singular=np.linalg.svd(xp,compute_uv=False);norms=np.linalg.norm(xp,axis=0)
    return {"data":data,"blocks":blocks,"xp":xp,"yp":yp,"beta":beta,"cost":best,"success":success,
            "rank":int(np.linalg.matrix_rank(xp)),"nullity":9-int(np.linalg.matrix_rank(xp)),
            "weakest":singular[-1]/singular[0],"condition":singular[0]/singular[-1],
            "spectrum":"|".join(f"{v/singular[0]:.8g}" for v in singular),"norms":norms}


def mc(fitted,n=100000):
    rows=[];xp=fitted["xp"];yp=fitted["yp"]
    for seed in SEEDS:
        candidates=prototype.ordered_candidates(n,seed);cost=np.empty(n)
        for begin in range(0,n,2000):
            end=min(begin+2000,n);r=yp[None,:]-candidates[begin:end]@xp.T
            cost[begin:end]=np.sum(r*r,axis=1)
        w=np.exp(-.5*(cost-cost.min()));w/=w.sum();ess=1/np.sum(w*w)
        for j,p in enumerate(PARAMETERS):
            order=np.argsort(candidates[:,j]);cum=np.cumsum(w[order])
            q=[candidates[order[min(np.searchsorted(cum,v),n-1)],j] for v in (.1,.5,.9)]
            rows.append({"seed":seed,"parameter":p,"p10":q[0],"median":q[1],"p90":q[2],
                         "width":q[2]-q[0],"ess":ess,"ess_fraction":ess/n,
                         "max_weight":w.max(),"top5_weight":np.sort(w)[-5:].sum(),
                         "best_sampled_cost":cost.min()})
    return pd.DataFrame(rows)


def raw_residuals(fitted):
    data=fitted["data"];beta=fitted["beta"];rows=[]
    for block in fitted["blocks"]:
        g=data.loc[block["indices"]];physical=prototype.build_physical_x(g,"track_time")@beta
        raw=g.corrected_lap_time.to_numpy()-physical;residual=raw-raw.mean()
        for row,value in zip(g.itertuples(),residual):
            rows.append({"programme_block":row.programme_block,"session":row.session,"team":row.team,
                         "programme":row.programme,"compound":row.compound,"run_id":row.run_id,
                         "driver":row.driver,"lap_number":row.lap_number,"tyre_life":row.tyre_life,
                         "run_index":row.run_index,"residual_s":value})
    return pd.DataFrame(rows)


def residual_scales(residuals):
    rows=[]
    for dimension in ("programme","session","compound","team"):
        for value,g in residuals.groupby(dimension):
            r=g.residual_s.to_numpy();absolute=np.abs(r);centre=np.median(r)
            trimmed=r[absolute<=np.quantile(absolute,.9)]
            rows.append({"group_dimension":dimension,"group_value":value,"observation_count":len(r),
                         "rms_s":np.sqrt(np.mean(r*r)),"std_s":np.std(r,ddof=1),
                         "mad_sigma_s":1.4826*np.median(np.abs(r-centre)),
                         "trimmed_90pct_rms_s":np.sqrt(np.mean(trimmed*trimmed)),
                         "absolute_p10_s":np.quantile(absolute,.1),"absolute_p50_s":np.quantile(absolute,.5),
                         "absolute_p90_s":np.quantile(absolute,.9),
                         "fraction_abs_over_0_35":np.mean(absolute>.35),
                         "fraction_abs_over_0_50":np.mean(absolute>.50),
                         "fraction_abs_over_1_00":np.mean(absolute>1.0)})
    return pd.DataFrame(rows)


def boundary_count(beta):
    frame=geometry.boundary_metrics(beta)
    base=frame[frame.parameter.isin(PARAMETERS)]
    bound=(base[["distance_to_lower_fraction","distance_to_upper_fraction"]].min(axis=1)<=.01).sum()
    faces=frame[frame.parameter.str.contains("minus")].distance_to_lower_fraction.le(.01).sum()
    return int(bound+faces)


def summary_row(name,fitted,convergence,measurement_sigma):
    data=fitted["data"];q=convergence.groupby("seed")[["ess","max_weight","top5_weight","best_sampled_cost"]].first()
    spans=convergence.pivot(index="parameter",columns="seed",values="median").apply(lambda x:x.max()-x.min(),axis=1)
    row={"variant":name,"measurement_sigma":str(measurement_sigma),"observation_count":len(data),
         "programme_block_count":data.programme_block.nunique(),"rank":fitted["rank"],"nullity":fitted["nullity"],
         "weakest_normalized_singular":fitted["weakest"],"condition_number":fitted["condition"],
         "normalized_singular_spectrum":fitted["spectrum"],"optimized_cost":fitted["cost"],
         "optimization_success":fitted["success"],"ess_min":q.ess.min(),"ess_max":q.ess.max(),
         "max_weight_min":q.max_weight.min(),"max_weight_max":q.max_weight.max(),
         "top5_weight_min":q.top5_weight.min(),"top5_weight_max":q.top5_weight.max(),
         "maximum_seed_median_span":spans.max(),"boundary_contact_count":boundary_count(fitted["beta"])}
    row.update({f"sensitivity_{p}":fitted["norms"][i] for i,p in enumerate(PARAMETERS)})
    row.update({f"map_{p}":fitted["beta"][i] for i,p in enumerate(PARAMETERS)})
    return row


def restored_audit(full,ledger,prepared,selected,current):
    label=ledger.set_index("run_id").revised_label;frame=full.copy();frame["programme"]=frame.run_id.map(label)
    selected_keys=set(longrun.key_frame(selected));current_keys=set(zip(current["session"],current["driver"],current["Stint"],current["lap_number"]))
    p=prepared.set_index(["Session","Driver","Stint","LapNumber"]);rows=[]
    for run_id,g in frame[frame.programme.eq("LongRun")].groupby("run_id"):
        keys=[(r.Session,r.Driver,r.Stint,r.LapNumber) for r in g.itertuples()]
        clean=[key for key in keys if key in p.index and bool(p.loc[key].IsCleanAirLongRunLap)]
        l0=[key for key in clean if key in selected_keys];l1=[key for key in clean if key in current_keys]
        restored=[key for key in l1 if key not in selected_keys]
        if not restored:continue
        tyre=pd.to_numeric(g[g.apply(lambda r:(r.Session,r.Driver,r.Stint,r.LapNumber) in l1,axis=1)].TyreLife,errors="coerce")
        rows.append({"team":g.Team.iloc[0],"driver":g.Driver.iloc[0],"session":g.Session.iloc[0],"run_id":run_id,
                     "stint":g.Stint.iloc[0],"compound":g.Compound.mode().iloc[0],"physical_laps":len(g),
                     "individually_clean_laps":len(clean),"l0_surviving_laps":len(l0),"l1_surviving_laps":len(l1),
                     "restored_lap_numbers":"|".join(str(int(k[3])) for k in restored),
                     "l0_discard_reason":"individually clean but outside a >=4 consecutive clean block",
                     "l1_tyre_age_min":tyre.min(),"l1_tyre_age_max":tyre.max(),"l1_tyre_age_span":tyre.max()-tyre.min(),
                     "meaningful_tyre_age_span":bool(len(tyre)>=2 and tyre.max()-tyre.min()>=3)})
    return pd.DataFrame(rows)


def evidence_support(data):
    return (data.groupby(["programme","compound"]).agg(observations=("lap_number","size"),runs=("run_id","nunique"))
            .reset_index())


def cost_concentration(fitted):
    costs=geometry.block_costs(fitted["data"],fitted["beta"],fitted["blocks"])
    total=costs.cost.sum();ordered=costs.sort_values("cost",ascending=False)
    return {"top3_cost_share":ordered.cost.head(3).sum()/total,"top5_cost_share":ordered.cost.head(5).sum()/total,
            **{f"{p.lower()}_cost":costs.loc[costs.programme.eq(p),"cost"].sum() for p in ("Setting","QualiSim","LongRun")}}


def relative_panel(run_ids,groups,clean_base_keys,l0_keys,l1_keys,longrun_ids,filename,title,columns=4):
    run_ids=list(run_ids);nrows=max(1,int(np.ceil(len(run_ids)/columns)))
    fig,axes=plt.subplots(nrows,columns,figsize=(5*columns,3.8*nrows),squeeze=False)
    for ax,run_id in zip(axes.flat,run_ids):
        g=groups[run_id].sort_values("LapNumber");timed=g[g.LapTimeSeconds.notna()].copy()
        timed["pace_pct"]=timed.RelativePace*100
        keys=[(r.Session,r.Driver,r.Stint,r.LapNumber) for r in timed.itertuples()]
        ax.plot(timed.LapNumber,timed.pace_pct,color="0.65",marker="o",label="physical timed")
        base=np.array([k in clean_base_keys for k in keys]);l0=np.array([k in l0_keys for k in keys]);l1=np.array([k in l1_keys for k in keys])
        ax.scatter(timed.loc[timed.Representative,"LapNumber"],timed.loc[timed.Representative,"pace_pct"],
                   facecolors="none",edgecolors="black",s=55,label="representative")
        ax.scatter(timed.loc[timed.Push,"LapNumber"],timed.loc[timed.Push,"pace_pct"],
                   marker="^",color="tab:orange",s=38,label="PUSH")
        if run_id in longrun_ids:
            ax.scatter(timed.loc[~base,"LapNumber"],timed.loc[~base,"pace_pct"],marker="x",color="red",label="disturbed")
            ax.scatter(timed.loc[l1,"LapNumber"],timed.loc[l1,"pace_pct"],facecolors="none",edgecolors="tab:green",s=75,label="L1 clean")
            ax.scatter(timed.loc[l0,"LapNumber"],timed.loc[l0,"pace_pct"],marker="s",facecolors="none",edgecolors="tab:blue",s=90,label="L0")
        for r in timed.itertuples():ax.annotate(f"{r.LapTimeSeconds:.1f}s\nT{r.TyreLife:g}",(r.LapNumber,r.pace_pct),fontsize=6)
        for y,label,style in ((100,"best","-"),(103,"PUSH","--"),(105,"guide only",":"),(115,"LongRun limit","-.")):
            ax.axhline(y,color="black" if y in (100,103) else "grey",ls=style,lw=.8);ax.text(.99,y,label,transform=ax.get_yaxis_transform(),ha="right",va="bottom",fontsize=6)
        ax.set(title=run_id.replace(":"," "),xlabel="Lap",ylabel="Driver/session best (%)")
        ax.set_ylim(98,min(165,max(117,timed.pace_pct.max()+2)));ax.grid(alpha=.2)
    for ax in axes.flat[len(run_ids):]:ax.set_visible(False)
    if run_ids:axes.flat[0].legend(fontsize=7)
    fig.suptitle(title);fig.tight_layout();fig.savefig(OUTPUT/filename,dpi=180);plt.close(fig)


def sigma_plot(frame):
    summary=frame.groupby("measurement_sigma_s").agg(ess_min=("ess","min"),ess_max=("ess","max"),
             max_weight_max=("max_weight","max"),optimized_cost=("optimized_cost","first")).reset_index()
    fig,axes=plt.subplots(1,3,figsize=(15,4.5));axes[0].fill_between(summary.measurement_sigma_s,summary.ess_min,summary.ess_max,alpha=.3);axes[0].plot(summary.measurement_sigma_s,summary.ess_min);axes[0].set_yscale("log");axes[0].set(ylabel="ESS range",xlabel="lap sigma (s)")
    axes[1].plot(summary.measurement_sigma_s,summary.max_weight_max,marker="o");axes[1].set(ylabel="worst max weight",xlabel="lap sigma (s)")
    axes[2].plot(summary.measurement_sigma_s,summary.optimized_cost,marker="o");axes[2].set(ylabel="optimized whitened cost",xlabel="lap sigma (s)")
    for ax in axes:ax.grid(alpha=.2)
    fig.suptitle("Common lap-noise sensitivity (preferred FP evidence policy)");fig.tight_layout();fig.savefig(OUTPUT/"fp_sigma_sensitivity.png",dpi=180);plt.close(fig)


def main():
    OUTPUT.mkdir(exist_ok=True);laps,ledger,groups,full,current,l0,l1,teammate,prepared,selected=inputs()
    sigma_programme=float(pd.read_csv(OUTPUT/"fp_programme_mismatch_estimate.csv").robust_sigma_programme_s.iloc[0])
    variants={"S0_setting_included_L0":l0,"S1_setting_excluded_L0":l0[~l0.programme.eq("Setting")].reset_index(drop=True),
              "L1_individual_clean_setting_included":l1}
    fits={};convergence={};summaries=[]
    start=None
    for name,data in variants.items():
        fits[name]=fit(data,teammate,sigma_programme,.35,start);start=fits[name]["beta"]
        convergence[name]=mc(fits[name]);row=summary_row(name,fits[name],convergence[name],.35);row.update(cost_concentration(fits[name]));summaries.append(row)
    # Setting is consistent and structurally useful if S1 does not improve raw residuals/cost concentration materially;
    # L1 is the independently requested FP policy. Use S0+L1 as the evidence-policy reference for sigma diagnostics.
    preferred_name="L1_individual_clean_setting_included";preferred=variants[preferred_name]
    residuals=raw_residuals(fits[preferred_name]);scale=residual_scales(residuals)
    sigma_rows=[];sigma_fits={}
    for sigma in SIGMAS:
        f=fit(preferred,teammate,sigma_programme,sigma,fits[preferred_name]["beta"]);c=mc(f);sigma_fits[sigma]=f
        for row in c.to_dict("records"):
            row.update({"measurement_sigma_s":sigma,"optimized_cost":f["cost"],"boundary_contact_count":boundary_count(f["beta"]),
                        **{f"map_{p}":f["beta"][i] for i,p in enumerate(PARAMETERS)}});sigma_rows.append(row)
    sigma_frame=pd.DataFrame(sigma_rows)
    # Transparent rounded robust programme scales; diagnostic only.
    robust=scale[scale.group_dimension.eq("programme")].set_index("group_value").mad_sigma_s
    programme_sigma={p:max(.35,round(float(robust[p])/0.05)*0.05) for p in ("Setting","QualiSim","LongRun")}
    pf=fit(preferred,teammate,sigma_programme,programme_sigma,fits[preferred_name]["beta"]);pc=mc(pf)
    pd.DataFrame([{**row,"programme_sigma":str(programme_sigma)} for row in pc.to_dict("records")]).to_csv(OUTPUT/"fp_programme_sigma_diagnostic.csv",index=False)
    c3_sigma=.50;combined=list(summaries)
    combined.append(summary_row("C3_L1_setting_included_sigma_0.50",sigma_fits[c3_sigma],sigma_frame[sigma_frame.measurement_sigma_s.eq(c3_sigma)],c3_sigma)|cost_concentration(sigma_fits[c3_sigma]))
    combined.append(summary_row("C4_L1_programme_specific_sigma",pf,pc,programme_sigma)|cost_concentration(pf))
    summary=pd.DataFrame(summaries);summary[summary.variant.str.startswith("S")].to_csv(OUTPUT/"fp_setting_inclusion_ab.csv",index=False)
    summary[summary.variant.isin(["S0_setting_included_L0","L1_individual_clean_setting_included"])].to_csv(OUTPUT/"fp_longrun_consecutive_policy_ab.csv",index=False)
    restored=restored_audit(full,ledger,prepared,selected,current);restored.to_csv(OUTPUT/"fp_longrun_restored_clean_laps.csv",index=False)
    scale.to_csv(OUTPUT/"fp_residual_noise_scale_summary.csv",index=False);sigma_frame.to_csv(OUTPUT/"fp_common_sigma_sensitivity.csv",index=False)
    pd.DataFrame(combined).to_csv(OUTPUT/"fp_evidence_noise_combined_comparison.csv",index=False)
    pd.concat([evidence_support(l0).assign(variant="L0"),
               evidence_support(l1).assign(variant="L1"),
               evidence_support(variants["S1_setting_excluded_L0"]).assign(variant="S1")],
              ignore_index=True).to_csv(OUTPUT/"fp_evidence_policy_compound_support.csv",index=False)
    # Review plots.
    priority=pd.read_csv(OUTPUT/"fp_programmeblock_manual_review_priority.csv");clean_keys=set(longrun.key_frame(prepared[prepared.IsCleanAirLongRunLap]));l0keys=set(longrun.key_frame(selected));l1keys=clean_keys
    longrun_ids=set(ledger.loc[ledger.revised_label.eq("LongRun"),"run_id"])
    relative_panel(priority[priority.review_tier.eq("Tier 1")].run_id,groups,clean_keys,l0keys,l1keys,longrun_ids,"fp_manual_review_relative_pace_tier1.png","Tier 1 manual review — normalized pace")
    relative_panel(priority[priority.review_tier.eq("Tier 2")].run_id,groups,clean_keys,l0keys,l1keys,longrun_ids,"fp_manual_review_relative_pace_tier2.png","Tier 2 manual review — normalized pace",5)
    restored_ids=restored.run_id.tolist();relative_panel(restored_ids,groups,clean_keys,l0keys,l1keys,longrun_ids,"fp_longrun_clean_policy_review.png","Restored individually clean FP LongRun evidence")
    early=ledger[ledger.session.eq("FP1")].sort_values(["driver","start_s"]).groupby("driver").head(3).run_id
    relative_panel(early,groups,clean_keys,l0keys,l1keys,longrun_ids,"fp_early_fp1_stint_review.png","Early FP1 — first three physical stints per driver",5)
    sigma_plot(sigma_frame)
    print(pd.DataFrame(combined)[["variant","observation_count","rank","optimized_cost","ess_min","ess_max","boundary_contact_count"]].to_string(index=False))
    print("programme sigma",programme_sigma);print("restored\n",restored.to_string(index=False))


if __name__=="__main__":main()
