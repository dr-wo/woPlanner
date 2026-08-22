#!/usr/bin/env python3
"""Diagnostic A/B: current versus production-clean LongRun evidence in FP Prototype 3."""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import diagnose_fp_posterior_geometry as geometry
import prototype_fp_programme_blocks as prototype
import run_fp_programmeblock_q_integration as integration
from wostrategy.analysis.long_run_performance import _prepare_laps, select_consecutive_clean_air_runs


ROOT=Path(__file__).resolve().parents[2];OUTPUT=ROOT/"temp"
MIN_CLEAN=4;AHEAD=3.0;BEHIND=1.0;QUICK=1.10
DOMINANT=("Williams:FP2:LongRun","Mercedes:FP2:LongRun","Red Bull Racing:FP2:LongRun","Alpine:FP3:LongRun")


def production_clean(laps):
    prepared=[];selected=[]
    for session in prototype.SESSIONS:
        raw=laps[laps.Session.eq(session)].copy()
        p=_prepare_laps(raw,clean_mean_time_delta_seconds=AHEAD,
                        clean_mean_time_delta_behind_seconds=BEHIND,
                        quick_lap_threshold=QUICK,dry_compounds=prototype.COMPOUNDS)
        p["Session"]=session;prepared.append(p)
        s=select_consecutive_clean_air_runs(p,min_clean_air_laps=MIN_CLEAN);s["Session"]=session;selected.append(s)
    return pd.concat(prepared),pd.concat(selected)


def key_frame(frame):
    return pd.MultiIndex.from_frame(frame[["Session","Driver","Stint","LapNumber"]])


def build():
    laps,ledger,groups,full,data=integration.revised_inputs()
    fp=prototype.teammate_estimates(data);_,teammate=integration.q_teammate_inputs(data,fp)
    current=integration.apply_teammate(data,teammate)
    prepared,selected=production_clean(laps);selected_keys=set(key_frame(selected))
    current_keys=pd.MultiIndex.from_arrays([current["session"],current["driver"],current["Stint"],current["lap_number"]])
    keep=~current["programme"].eq("LongRun")|pd.Series([key in selected_keys for key in current_keys],index=current.index)
    clean=current[keep].copy().reset_index(drop=True)
    return laps,ledger,groups,full,current,clean,teammate,prepared,selected


def model(data,teammate,sigma,start=None):
    x=prototype.build_physical_x(data,"track_time");y=data.corrected_lap_time.to_numpy()
    blocks=geometry.transformed_blocks(data,sigma,teammate,x,y);xp,yp=geometry.stack(blocks)
    if start is None:start=np.array([.07,0,0,-.05,.01,.02,.03,.5,-.9])
    beta,best,success=geometry.optimize_map(xp,yp,start)
    singular=np.linalg.svd(xp,compute_uv=False)
    diag={"observation_count":len(data),"longrun_observation_count":int(data.programme.eq("LongRun").sum()),
          "run_count":data.run_id.nunique(),"longrun_run_count":data.loc[data.programme.eq("LongRun"),"run_id"].nunique(),
          "physical_dimension":9,"rank":np.linalg.matrix_rank(xp),"nullity":9-np.linalg.matrix_rank(xp),
          "weakest_normalized_singular":singular[-1]/singular[0],"condition_number":singular[0]/singular[-1],
          "optimized_cost":best,"optimization_success":success}
    return beta,blocks,xp,yp,diag


def exclusion_reason(row):
    reasons=[]
    if not row.DryTimed:reasons.append("not_dry_timed")
    if pd.notna(row.PitOutTime):reasons.append("pit_out")
    if pd.notna(row.PitInTime):reasons.append("pit_in")
    if str(row.TrackStatus)!="1":reasons.append("not_green")
    if not bool(row.IsQuickLap):reasons.append("slower_than_110pct")
    ahead=pd.to_numeric(pd.Series([row.MeanTimeDeltaToDriverAhead]),errors="coerce").iloc[0]
    behind=pd.to_numeric(pd.Series([row.MeanTimeDeltaToDriverBehind]),errors="coerce").iloc[0]
    if pd.isna(ahead) or ahead<=AHEAD:reasons.append("gap_ahead_not_over_3s")
    if pd.isna(behind) or behind<=BEHIND:reasons.append("gap_behind_not_over_1s")
    return "|".join(reasons) or "clean_lap_but_not_in_4_consecutive_block"


def lap_audit(full,ledger,prepared,selected,current,beta,blocks):
    labels=ledger.set_index("run_id").revised_label;full=full.copy()
    full["programme"] = full.run_id.map(labels)
    current_keys=set(zip(current["session"],current["driver"],current["Stint"],current["lap_number"]))
    selected_keys=set(key_frame(selected));prep=prepared.set_index(["Session","Driver","Stint","LapNumber"])
    # raw MAP residual with an exact mean per current programme block
    residual_map={}
    for block in blocks:
        group=current.loc[block["indices"]];effect=prototype.build_physical_x(group,"track_time")@beta
        raw=group.corrected_lap_time.to_numpy()-effect;res=raw-raw.mean()
        for row,value in zip(group.itertuples(),res):residual_map[(row.session,row.driver,row.Stint,row.lap_number)]=value
    rows=[]
    for row in full[full.programme.eq("LongRun")].itertuples():
        key=(row.Session,row.Driver,row.Stint,row.LapNumber);p=prep.loc[key] if key in prep.index else None
        clean_base=bool(p.IsCleanAirLongRunLap) if p is not None else False
        production_selected=key in selected_keys
        reasons="" if production_selected else (exclusion_reason(p) if p is not None and not clean_base else "clean_lap_but_not_in_4_consecutive_block")
        rows.append({"programme_block":f"{row.Team}:{row.Session}:LongRun","team":row.Team,"driver":row.Driver,
          "session":row.Session,"run_id":row.run_id,"stint":row.Stint,"lap_number":row.LapNumber,
          "lap_time_s":row.LapTimeSeconds,"compound":row.Compound,"tyre_life":row.TyreLife,
          "run_index_current":next((r.run_index for r in current.itertuples() if (r.session,r.driver,r.Stint,r.lap_number)==key),np.nan),
          "track_status":row.TrackStatus,"pit_out":pd.notna(row.PitOutTime),"pit_in":pd.notna(row.PitInTime),
          "prototype_representative":row.Representative,"prototype_likelihood":key in current_keys,
          "production_clean_air_base":clean_base,"production_consecutive_selected":production_selected,
          "mean_gap_ahead_s":getattr(row,"MeanTimeDeltaToDriverAhead",np.nan),"mean_gap_behind_s":getattr(row,"MeanTimeDeltaToDriverBehind",np.nan),
          "min_gap_ahead_s":getattr(row,"MinTimeDeltaToDriverAhead",np.nan),"min_gap_behind_s":getattr(row,"MinTimeDeltaToDriverBehind",np.nan),
          "traffic_source":"cached unified gap summaries; TrafficStatus/source column unavailable",
          "production_exclusion_reason":reasons,"residual_at_current_global_map_s":residual_map.get(key,np.nan)})
    return pd.DataFrame(rows)


def evidence_summary(audit,current,clean,beta_a,blocks_a,teammate,sigma):
    current_cost=geometry.block_costs(current,beta_a,blocks_a).set_index("programme_block")
    # Evaluate clean evidence under current physical beta, with clean data's own exact block transforms.
    x=prototype.build_physical_x(clean,"track_time");y=clean.corrected_lap_time.to_numpy();blocks_clean=geometry.transformed_blocks(clean,sigma,teammate,x,y)
    clean_cost=geometry.block_costs(clean,beta_a,blocks_clean).set_index("programme_block")
    rows=[]
    for run_id,g in audit.groupby("run_id"):
        block=g.programme_block.iloc[0];cur=g.prototype_likelihood.sum();sel=g.production_consecutive_selected.sum()
        rows.append({"programme_block":block,"session":g.session.iloc[0],"team":g.team.iloc[0],"driver":g.driver.iloc[0],
          "run_id":run_id,"stint":g.stint.iloc[0],"compound":g.compound.dropna().mode().iloc[0],"physical_laps":len(g),
          "current_likelihood_laps":int(cur),"production_clean_laps":int(sel),"removed_laps":int(cur-sel),
          "current_block_cost_at_current_map":current_cost.loc[block,"cost"] if block in current_cost.index else np.nan,
          "clean_block_cost_at_current_map":clean_cost.loc[block,"cost"] if block in clean_cost.index else np.nan,
          "current_block_rms_whitened":current_cost.loc[block,"residual_rms_whitened"] if block in current_cost.index else np.nan,
          "clean_block_rms_whitened":clean_cost.loc[block,"residual_rms_whitened"] if block in clean_cost.index else np.nan,
          "sufficient_clean_evidence":sel>=MIN_CLEAN,"clean_base_laps_before_consecutive_rule":int(g.production_clean_air_base.sum()),
          "destroyed_only_by_consecutive_rule":bool(g.production_clean_air_base.sum()>=MIN_CLEAN and sel==0)})
    return pd.DataFrame(rows)


def convergence(variants,sigma):
    rows=[]
    for name,data,teammate,beta,_,xp,yp,_ in variants:
        # Reproduce the existing fixed candidate banks exactly, while retaining the
        # minimum sampled cost (the shared helper only returns weighted summaries).
        for n in (20000,50000,100000):
            for seed in integration.SEEDS:
                candidates=prototype.ordered_candidates(n,seed);cost=np.empty(n)
                for start in range(0,n,2000):
                    stop=min(start+2000,n)
                    residual=yp[None,:]-candidates[start:stop]@xp.T
                    cost[start:stop]=np.sum(residual**2,axis=1)
                log_weight=-.5*(cost-cost.min());weight=np.exp(log_weight-log_weight.max());weight/=weight.sum()
                ess=1/np.sum(weight*weight)
                for index,parameter in enumerate(prototype.PARAMETERS):
                    order=np.argsort(candidates[:,index]);cumulative=np.cumsum(weight[order])
                    quantiles=[candidates[order[min(np.searchsorted(cumulative,p),n-1)],index] for p in (.1,.5,.9)]
                    rows.append({"variant":name,"sample_count":n,"seed":seed,"parameter":parameter,
                      "p10":quantiles[0],"median":quantiles[1],"p90":quantiles[2],"width":quantiles[2]-quantiles[0],
                      "ess":ess,"ess_fraction":ess/n,"max_weight":weight.max(),"top5_weight":np.sort(weight)[-5:].sum(),
                      "best_sampled_cost":cost.min(),"optimized_cost":float(np.sum((yp-xp@beta)**2)),
                      "sigma_programme_s":sigma,"measurement_sigma_s":.35})
    return pd.DataFrame(rows)


def boundary_frame(variants):
    rows=[]
    for name,_,_,beta,*_ in variants:
        f=geometry.boundary_metrics(beta);f["variant"]=name;rows.append(f)
    return pd.concat(rows,ignore_index=True)


def residual_and_cost(variants):
    costs=[];summaries=[];residuals=[]
    for name,data,_,beta,blocks,*_ in variants:
        c=geometry.block_costs(data,beta,blocks);c["variant"]=name;costs.append(c)
        r=geometry.raw_residuals(data,beta,blocks);r["variant"]=name;residuals.append(r)
        g=geometry.grouped_costs(r)
        g["variant"]=name;summaries.append(g)
    return pd.concat(costs),pd.concat(summaries),pd.concat(residuals)


def influence_comparison(variants):
    rows=[]
    for name,_,_,beta,blocks,*_ in variants:
        frame=geometry.influence(blocks,beta,top_blocks=8);frame["variant"]=name;rows.append(frame)
    return pd.concat(rows,ignore_index=True)


def plot_block(block,audit,beta,current,filename):
    rows=audit[audit.programme_block.eq(block)];fig,ax=plt.subplots(figsize=(12,7))
    for run_id,g in rows.groupby("run_id"):
        ax.plot(g.lap_number,g.lap_time_s,color="grey",alpha=.5,label="physical stint" if run_id==rows.run_id.iloc[0] else None)
        a=g[g.prototype_likelihood];ax.scatter(a.lap_number,a.lap_time_s,c="tab:orange",s=55,label="current likelihood" if run_id==rows.run_id.iloc[0] else None)
        c=g[g.production_consecutive_selected];ax.scatter(c.lap_number,c.lap_time_s,facecolors="none",edgecolors="tab:green",s=110,linewidth=2,label="production clean" if run_id==rows.run_id.iloc[0] else None)
        e=g[g.prototype_likelihood&~g.production_consecutive_selected];ax.scatter(e.lap_number,e.lap_time_s,marker="x",c="red",s=80,label="traffic/disturbance exclusion" if run_id==rows.run_id.iloc[0] else None)
        for row in g.nlargest(2,"residual_at_current_global_map_s").itertuples():ax.annotate(f"res {row.residual_at_current_global_map_s:+.2f}",(row.lap_number,row.lap_time_s),fontsize=8)
        for row in g[g.pit_out|g.pit_in].itertuples():
            label="pit out" if row.pit_out else "pit in"
            ax.axvline(row.lap_number,color="tab:blue",linestyle=":",alpha=.45)
            ax.text(row.lap_number,.99,label,rotation=90,va="top",ha="right",fontsize=8,
                    color="tab:blue",transform=ax.get_xaxis_transform())
    # Pit laps can be tens of seconds slower and otherwise compress the evidence
    # into an unreadable strip. Keep their boundaries visible while scaling to the
    # actual likelihood observations.
    visible=rows.loc[rows.prototype_likelihood,"lap_time_s"]
    if len(visible):ax.set_ylim(visible.min()-1,visible.max()+1)
    ax.set(title=f"{block}: current vs production-clean LongRun evidence",xlabel="LapNumber",ylabel="Lap time (s)");ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUTPUT/filename,dpi=180);plt.close(fig)


def overview_plots(costs,residuals):
    top=costs.sort_values(["variant","cost"],ascending=[True,False]).groupby("variant").head(10)
    fig,axes=plt.subplots(1,2,figsize=(18,8))
    for ax,(name,g) in zip(axes,top.groupby("variant")):
        ax.barh(g.programme_block[::-1],g.cost[::-1]);ax.set_title(name);ax.set_xlabel("whitened cost")
    fig.tight_layout();fig.savefig(OUTPUT/"fp_longrun_ab_block_costs.png",dpi=180);plt.close(fig)
    fig,axes=plt.subplots(2,3,figsize=(17,10))
    for ax,column in zip(axes.flat,("session_time_s","run_index","tyre_life")):
        for name,g in residuals.groupby("variant"):ax.scatter(g[column],g.residual_s,s=8,alpha=.35,label=name)
        ax.axhline(0,color="black");ax.set(xlabel=column,ylabel="residual s")
    for ax,column in zip(axes.flat[3:],("programme","compound","team")):
        summary=residuals.groupby(["variant",column]).residual_s.apply(lambda x:np.sqrt(np.mean(x*x))).unstack(0)
        summary.plot.bar(ax=ax);ax.set_ylabel("raw residual RMS s");ax.tick_params(axis="x",rotation=70)
    axes[0,0].legend();fig.tight_layout();fig.savefig(OUTPUT/"fp_longrun_ab_residual_structure.png",dpi=180);plt.close(fig)


def main():
    OUTPUT.mkdir(exist_ok=True);laps,ledger,groups,full,a,b,teammate,prepared,selected=build()
    sigma=float(pd.read_csv(OUTPUT/"fp_programme_mismatch_estimate.csv").robust_sigma_programme_s.iloc[0])
    beta_a,blocks_a,xpa,ypa,diag_a=model(a,teammate,sigma)
    beta_b,blocks_b,xpb,ypb,diag_b=model(b,teammate,sigma,beta_a)
    variants=[("current",a,teammate,beta_a,blocks_a,xpa,ypa,diag_a),("production_clean_longrun",b,teammate,beta_b,blocks_b,xpb,ypb,diag_b)]
    audit=lap_audit(full,ledger,prepared,selected,a,beta_a,blocks_a)
    evidence=evidence_summary(audit,a,b,beta_a,blocks_a,teammate,sigma)
    dominant=audit[audit.programme_block.isin(DOMINANT)].copy()
    ident=pd.DataFrame([{**v[-1],"variant":v[0]} for v in variants])
    conv=convergence(variants,sigma);bounds=boundary_frame(variants);costs,summaries,residuals=residual_and_cost(variants)
    influence=influence_comparison(variants)
    evidence.to_csv(OUTPUT/"fp_longrun_current_vs_clean_evidence.csv",index=False);dominant.to_csv(OUTPUT/"fp_longrun_dominant_block_lap_audit.csv",index=False)
    ident.to_csv(OUTPUT/"fp_longrun_ab_identifiability.csv",index=False);conv.to_csv(OUTPUT/"fp_longrun_ab_convergence.csv",index=False)
    costs.to_csv(OUTPUT/"fp_longrun_ab_block_costs.csv",index=False);bounds.to_csv(OUTPUT/"fp_longrun_ab_boundaries.csv",index=False)
    summaries.to_csv(OUTPUT/"fp_longrun_ab_residual_summary.csv",index=False);residuals.to_csv(OUTPUT/"fp_longrun_ab_residuals.csv",index=False)
    influence.to_csv(OUTPUT/"fp_longrun_ab_block_influence.csv",index=False)
    names={DOMINANT[0]:"fp_longrun_williams_fp2_review.png",DOMINANT[1]:"fp_longrun_mercedes_fp2_review.png",DOMINANT[2]:"fp_longrun_redbull_fp2_review.png",DOMINANT[3]:"fp_longrun_alpine_fp3_review.png"}
    for block,filename in names.items():plot_block(block,audit,beta_a,a,filename)
    overview_plots(costs,residuals)
    print(ident.to_string(index=False));print("evidence\n",evidence.to_string(index=False))


if __name__=="__main__":main()
