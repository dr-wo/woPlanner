#!/usr/bin/env python3
"""Small diagnostic of real-data collapse in the revised 9D FP prototype."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import minimize

import prototype_fp_programme_blocks as prototype
import run_fp_programmeblock_q_integration as integration


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
SEEDS = integration.SEEDS
SIGMA_GRID = (0.25, 0.35713983997155274, 0.50, 0.70)
BOUNDS = np.array([[0,.1],[-.08,.08],[-.08,.08],[-.08,.08],
                   [0,.2],[0,.2],[0,.2],[0,1],[-1,0]],float)


def inputs():
    _, ledger, groups, full, raw = integration.revised_inputs()
    fp = prototype.teammate_estimates(raw)
    _, teammate = integration.q_teammate_inputs(raw, fp)
    data = integration.apply_teammate(raw, teammate)
    return ledger, groups, full, data, teammate


def transformed_blocks(data, sigma, teammate, x, y):
    teammate_sigma = {(row.team,row.session,row.driver): row.uncertainty_width_s/2.563
                      for row in teammate.itertuples()}
    blocks=[]
    for block, group in data.groupby("programme_block",sort=True):
        indices=group.index.to_numpy(); run_codes,runs=pd.factorize(group.run_id,sort=True)
        z=np.zeros((len(group),len(runs))); z[np.arange(len(group)),run_codes]=1
        variances=[]
        for run in runs:
            first=group[group.run_id.eq(run)].iloc[0]
            driver_sigma=teammate_sigma.get((first.team,first.session,first.driver),0)
            variances.append(sigma**2+driver_sigma**2)
        covariance=.35**2*np.eye(len(group))+z@np.diag(variances)@z.T
        inverse=np.linalg.inv(covariance); ones=np.ones(len(group))
        projection=np.eye(len(group))-np.outer(ones,(ones@inverse)/(ones@inverse@ones))
        transform=np.linalg.cholesky(inverse).T@projection
        blocks.append({"block":block,"indices":indices,"x":transform@x[indices],"y":transform@y[indices],
                       "transform":transform})
    return blocks


def stack(blocks):
    return np.vstack([b["x"] for b in blocks]),np.concatenate([b["y"] for b in blocks])


def cost(beta,xp,yp):
    residual=yp-xp@beta
    return float(residual@residual)


def constrained(beta):
    tolerance=1e-9
    return (beta[4]<=beta[5]+tolerance and beta[5]<=beta[6]+tolerance
            and beta[7]>=-tolerance and beta[8]<=tolerance)


def optimize_map(xp,yp,start):
    constraints=({"type":"ineq","fun":lambda b:b[5]-b[4]},
                 {"type":"ineq","fun":lambda b:b[6]-b[5]})
    result=minimize(lambda b:cost(b,xp,yp),start,bounds=BOUNDS,constraints=constraints,
                    method="SLSQP",options={"ftol":1e-10,"maxiter":2000})
    beta=result.x.copy()
    # Remove only floating-point constraint violations at an active ordering face.
    beta[5]=max(beta[5],beta[4]);beta[6]=max(beta[6],beta[5])
    return beta,cost(beta,xp,yp),result.success


def sampled_top(xp,yp,n=100000):
    all_rows=[]
    for seed in SEEDS:
        candidates=prototype.ordered_candidates(n,seed)
        costs=np.empty(n)
        for begin in range(0,n,2000):
            end=min(n,begin+2000); residual=yp[None,:]-candidates[begin:end]@xp.T
            costs[begin:end]=np.sum(residual**2,axis=1)
        lw=-.5*(costs-costs.min()); weights=np.exp(lw-lw.max());weights/=weights.sum()
        order=np.argsort(costs)[:20]; ess=1/np.sum(weights*weights)
        for rank,index in enumerate(order,1):
            row={"seed":seed,"rank":rank,"cost":costs[index],"rmse_whitened":np.sqrt(costs[index]/len(yp)),
                 "normalized_weight":weights[index],"ess":ess}
            row.update(dict(zip(prototype.PARAMETERS,candidates[index])))
            all_rows.append(row)
    return pd.DataFrame(all_rows)


def seed_maps(top):
    maps=top[top["rank"].eq(1)].set_index("seed")
    rows=[]; widths=BOUNDS[:,1]-BOUNDS[:,0]
    for left,right in combinations(maps.index,2):
        a=maps.loc[left,list(prototype.PARAMETERS)].to_numpy();b=maps.loc[right,list(prototype.PARAMETERS)].to_numpy()
        rows.append({"seed_a":left,"seed_b":right,"normalized_euclidean_distance":np.linalg.norm((a-b)/widths),
                     "maximum_coordinate_distance_fraction":np.max(np.abs(a-b)/widths),
                     **{f"delta_{name}":b[i]-a[i] for i,name in enumerate(prototype.PARAMETERS)}})
    return pd.DataFrame(rows)


def boundary_metrics(beta):
    rows=[]
    for i,name in enumerate(prototype.PARAMETERS):
        width=BOUNDS[i,1]-BOUNDS[i,0]
        rows.append({"parameter":name,"value":beta[i],"distance_to_lower_fraction":(beta[i]-BOUNDS[i,0])/width,
                     "distance_to_upper_fraction":(BOUNDS[i,1]-beta[i])/width})
    gaps={"deg_MEDIUM_minus_HARD":(beta[5]-beta[4])/.2,
          "deg_SOFT_minus_MEDIUM":(beta[6]-beta[5])/.2,
          "perf_HARD_minus_MEDIUM":beta[7],"perf_MEDIUM_minus_SOFT":-beta[8]}
    for name,value in gaps.items():rows.append({"parameter":name,"value":value,"distance_to_lower_fraction":value,"distance_to_upper_fraction":np.nan})
    return pd.DataFrame(rows)


def profiles_1d(beta,xp,yp):
    base=cost(beta,xp,yp);rows=[]
    for i,name in enumerate(prototype.PARAMETERS):
        low,high=BOUNDS[i]; span=(high-low)*.20
        local_low=max(low,beta[i]-span);local_high=min(high,beta[i]+span)
        if i==4:local_high=min(local_high,beta[5])
        if i==5:local_low=max(local_low,beta[4]);local_high=min(local_high,beta[6])
        if i==6:local_low=max(local_low,beta[5])
        for value in np.linspace(local_low,local_high,101):
            candidate=beta.copy();candidate[i]=value
            value_cost=cost(candidate,xp,yp)
            rows.append({"parameter":name,"value":value,"offset":value-beta[i],
                         "delta_cost":value_cost-base,"delta_rmse":np.sqrt(value_cost/len(yp))-np.sqrt(base/len(yp))})
    return pd.DataFrame(rows)


def strongest_pairs(xp,count=4):
    norms=np.linalg.norm(xp,axis=0); normalized=xp/np.where(norms>0,norms,1)
    correlation=normalized.T@normalized; candidates=[]
    for i,j in combinations(range(9),2):candidates.append((abs(correlation[i,j]),i,j,correlation[i,j]))
    return sorted(candidates,reverse=True)[:count]


def pair_profiles(beta,xp,yp,pairs):
    base=cost(beta,xp,yp);rows=[]
    for _,i,j,corr in pairs:
        span_i=(BOUNDS[i,1]-BOUNDS[i,0])*.12;span_j=(BOUNDS[j,1]-BOUNDS[j,0])*.12
        values_i=np.linspace(max(BOUNDS[i,0],beta[i]-span_i),min(BOUNDS[i,1],beta[i]+span_i),41)
        values_j=np.linspace(max(BOUNDS[j,0],beta[j]-span_j),min(BOUNDS[j,1],beta[j]+span_j),41)
        for left in values_i:
            for right in values_j:
                candidate=beta.copy();candidate[i]=left;candidate[j]=right
                if not constrained(candidate):continue
                rows.append({"parameter_a":prototype.PARAMETERS[i],"parameter_b":prototype.PARAMETERS[j],
                             "column_correlation":corr,"value_a":left,"value_b":right,
                             "delta_cost":cost(candidate,xp,yp)-base})
    return pd.DataFrame(rows)


def raw_residuals(data,beta,blocks):
    rows=[]
    for block in blocks:
        group=data.loc[block["indices"]].copy(); physical=prototype.build_physical_x(group,"track_time")@beta
        raw=group.corrected_lap_time.to_numpy()-physical
        baseline=raw.mean(); residual=raw-baseline
        for row,value in zip(group.itertuples(),residual):
            rows.append({"programme_block":row.programme_block,"session":row.session,"team":row.team,
                         "programme":row.programme,"compound":row.compound,"run_id":row.run_id,
                         "driver":row.driver,"session_time_s":row.session_time_s,"run_index":row.run_index,
                         "tyre_life":row.tyre_life,"residual_s":value})
    return pd.DataFrame(rows)


def block_costs(data,beta,blocks,candidate_label="global_map"):
    rows=[]
    for block in blocks:
        r=block["y"]-block["x"]@beta;group=data.loc[block["indices"]]
        rows.append({"candidate":candidate_label,"programme_block":block["block"],"session":group["session"].iloc[0],
                     "team":group["team"].iloc[0],"programme":group["programme"].iloc[0],
                     "compounds":"|".join(sorted(group["compound"].unique())),"observation_count":len(group),
                     "cost":r@r,"cost_per_observation":r@r/len(group),"residual_mean_whitened":r.mean(),
                     "residual_rms_whitened":np.sqrt(np.mean(r*r)),"largest_absolute_residual_whitened":np.max(np.abs(r))})
    return pd.DataFrame(rows)


def grouped_costs(residuals):
    rows=[]
    for dimension in ("session","programme","compound","team"):
        for value,group in residuals.groupby(dimension):
            r=group.residual_s.to_numpy();rows.append({"group_dimension":dimension,"group_value":value,
                "observation_count":len(group),"raw_sse":r@r,"raw_sse_per_observation":r@r/len(group),
                "residual_mean_s":r.mean(),"residual_rms_s":np.sqrt(np.mean(r*r)),"largest_absolute_residual_s":np.max(np.abs(r))})
    return pd.DataFrame(rows)


def influence(blocks,beta,top_blocks=6):
    full_x,full_y=stack(blocks);base=cost(beta,full_x,full_y);ranked=sorted(blocks,key=lambda b:cost(beta,b["x"],b["y"]),reverse=True)[:top_blocks]
    rows=[]
    for omitted in ranked:
        kept=[b for b in blocks if b["block"]!=omitted["block"]];xp,yp=stack(kept)
        fitted,value,success=optimize_map(xp,yp,beta)
        rows.append({"omitted_block":omitted["block"],"omitted_cost_at_global_map":cost(beta,omitted["x"],omitted["y"]),
                     "remaining_cost_at_global_map":cost(beta,xp,yp),"remaining_refit_cost":value,
                     "objective_improvement_after_refit":cost(beta,xp,yp)-value,"optimization_success":success,
                     "normalized_map_movement":np.linalg.norm((fitted-beta)/(BOUNDS[:,1]-BOUNDS[:,0])),
                     **{f"map_{name}":fitted[i] for i,name in enumerate(prototype.PARAMETERS)}})
    return pd.DataFrame(rows)


def sigma_sensitivity(data,teammate,start):
    x=prototype.build_physical_x(data,"track_time");y=data.corrected_lap_time.to_numpy();rows=[]
    for sigma in SIGMA_GRID:
        blocks=transformed_blocks(data,sigma,teammate,x,y);xp,yp=stack(blocks)
        fitted,best,success=optimize_map(xp,yp,start)
        candidates=prototype.ordered_candidates(100000,2026);costs=np.empty(len(candidates))
        for begin in range(0,len(candidates),2000):
            end=begin+2000;r=yp[None,:]-candidates[begin:end]@xp.T;costs[begin:end]=np.sum(r*r,axis=1)
        weights=np.exp(-.5*(costs-costs.min()));weights/=weights.sum()
        rows.append({"sigma_programme_s":sigma,"optimized_best_cost":best,"optimization_success":success,
                     "sampled_best_cost":costs.min(),"ess":1/np.sum(weights*weights),"max_weight":weights.max(),
                     "top5_weight":np.sort(weights)[-5:].sum(),"map_movement_from_reference":np.linalg.norm((fitted-start)/(BOUNDS[:,1]-BOUNDS[:,0])),
                     **{f"map_{name}":fitted[i] for i,name in enumerate(prototype.PARAMETERS)}})
    return pd.DataFrame(rows)


def synthetic_control(data,teammate,sigma):
    truth=np.array([.04,-.02,.01,-.01,.03,.07,.12,.5,-.6]);x=prototype.build_physical_x(data,"track_time")
    rng=np.random.default_rng(991);bc,_=pd.factorize(data.programme_block,sort=True);rc,_=pd.factorize(data.run_id,sort=True)
    y=80+bc*.2+x@truth+rng.normal(0,sigma,rc.max()+1)[rc]+rng.normal(0,.12,len(data))
    temporary=data.copy();temporary["corrected_lap_time"]=y
    blocks=transformed_blocks(temporary,sigma,teammate,x,y);xp,yp=stack(blocks);fitted,best,_=optimize_map(xp,yp,truth)
    h=2*xp.T@xp;values=np.linalg.eigvalsh(h)
    residual=yp-xp@fitted
    return pd.DataFrame([{"dataset":"synthetic_full_nuisance","best_cost":best,"residual_rms_whitened":np.sqrt(np.mean(residual**2)),
                          "hessian_condition_number":values[-1]/values[0],"smallest_hessian_eigenvalue":values[0],
                          "largest_hessian_eigenvalue":values[-1],"normalized_map_distance_from_truth":np.linalg.norm((fitted-truth)/(BOUNDS[:,1]-BOUNDS[:,0])),
                          **{f"map_{name}":fitted[i] for i,name in enumerate(prototype.PARAMETERS)}}])


def plots(profiles,pairs,blocks,residuals):
    fig,axes=plt.subplots(3,3,figsize=(15,12))
    for ax,(name,g) in zip(axes.flat,profiles.groupby("parameter",sort=False)):
        ax.plot(g.offset,g.delta_cost);ax.axhline(1,color="grey",ls="--");ax.set_title(name);ax.set_xlabel("offset");ax.set_ylabel("Δ cost");ax.grid(alpha=.2)
    fig.suptitle("Local one-dimensional profiles around optimized global MAP");fig.tight_layout();fig.savefig(OUTPUT/"fp_posterior_1d_profiles.png",dpi=180);plt.close(fig)
    unique=list(pairs.groupby(["parameter_a","parameter_b"],sort=False));fig,axes=plt.subplots(2,2,figsize=(13,10))
    for ax,((a,b),g) in zip(axes.flat,unique):
        pivot=g.pivot(index="value_b",columns="value_a",values="delta_cost")
        if min(pivot.shape)>=2:
            image=ax.contourf(pivot.columns,pivot.index,pivot.values,levels=30,cmap="viridis");fig.colorbar(image,ax=ax,label="Δ cost")
        else:
            ax.scatter(g.value_a,g.value_b,c=g.delta_cost,cmap="viridis",s=18)
            ax.text(.02,.98,"boundary-truncated to one grid row/column",transform=ax.transAxes,va="top",fontsize=8)
        ax.set(xlabel=a,ylabel=b,title=f"{a} vs {b}")
    fig.suptitle("Strongest projected-column pair surfaces");fig.tight_layout();fig.savefig(OUTPUT/"fp_posterior_pair_surfaces.png",dpi=180);plt.close(fig)
    top=blocks.sort_values("cost",ascending=False).head(20);fig,ax=plt.subplots(figsize=(13,8));ax.barh(top.programme_block[::-1],top.cost[::-1]);ax.set(xlabel="whitened cost",title="Largest ProgrammeBlock contributions at global MAP");fig.tight_layout();fig.savefig(OUTPUT/"fp_posterior_block_costs.png",dpi=180);plt.close(fig)
    fig,axes=plt.subplots(2,3,figsize=(17,10));axes=axes.flat
    for ax,column in zip(axes,("session_time_s","run_index","tyre_life")):
        ax.scatter(residuals[column],residuals.residual_s,s=8,alpha=.5);ax.axhline(0,color="black");ax.set(xlabel=column,ylabel="raw residual (s)")
    for ax,column in zip(axes[3:],("programme","compound","team")):
        groups=[g.residual_s.to_numpy() for _,g in residuals.groupby(column)];labels=[str(v) for v,_ in residuals.groupby(column)];ax.boxplot(groups,tick_labels=labels,showfliers=False);ax.tick_params(axis="x",rotation=75);ax.set(xlabel=column,ylabel="raw residual (s)")
    fig.suptitle("Residual structure at global MAP");fig.tight_layout();fig.savefig(OUTPUT/"fp_posterior_residual_structure.png",dpi=180);plt.close(fig)


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True);ledger,groups,full,data,teammate=inputs()
    sigma=SIGMA_GRID[1];x=prototype.build_physical_x(data,"track_time");y=data.corrected_lap_time.to_numpy()
    blocks=transformed_blocks(data,sigma,teammate,x,y);xp,yp=stack(blocks)
    top=sampled_top(xp,yp);sample_best=top.sort_values("cost").iloc[0][list(prototype.PARAMETERS)].to_numpy(float)
    beta,best,success=optimize_map(xp,yp,sample_best)
    maps=seed_maps(top);bounds=boundary_metrics(beta);profiles=profiles_1d(beta,xp,yp);pair=pair_profiles(beta,xp,yp,strongest_pairs(xp))
    residuals=raw_residuals(data,beta,blocks)
    block_frames=[block_costs(data,beta,blocks)]
    for row in top[top["rank"].eq(1)].itertuples():
        candidate=np.array([getattr(row,name) for name in prototype.PARAMETERS])
        block_frames.append(block_costs(data,candidate,blocks,f"seed_{row.seed}_sample_map"))
    block=pd.concat(block_frames,ignore_index=True);global_block=block[block.candidate.eq("global_map")]
    grouped=grouped_costs(residuals);influence_frame=influence(blocks,beta)
    sigma_frame=sigma_sensitivity(data,teammate,beta);synthetic=synthetic_control(data,teammate,sigma)
    h=2*xp.T@xp;eigenvalues=np.linalg.eigvalsh(h)
    real_geometry=pd.DataFrame([{"dataset":"real_R11","optimized_best_cost":best,"optimization_success":success,
                                 "residual_rms_whitened":np.sqrt(best/len(yp)),"hessian_condition_number":eigenvalues[-1]/eigenvalues[0],
                                 "smallest_hessian_eigenvalue":eigenvalues[0],"largest_hessian_eigenvalue":eigenvalues[-1],
                                 **{f"map_{name}":beta[i] for i,name in enumerate(prototype.PARAMETERS)}}])
    top.to_csv(OUTPUT/"fp_posterior_top_candidates.csv",index=False);maps.to_csv(OUTPUT/"fp_posterior_seed_map_comparison.csv",index=False)
    bounds.to_csv(OUTPUT/"fp_posterior_map_boundaries.csv",index=False);profiles.to_csv(OUTPUT/"fp_posterior_1d_profiles.csv",index=False)
    pair.to_csv(OUTPUT/"fp_posterior_pair_profiles.csv",index=False);block.to_csv(OUTPUT/"fp_posterior_block_costs.csv",index=False)
    grouped.to_csv(OUTPUT/"fp_posterior_group_costs.csv",index=False);influence_frame.to_csv(OUTPUT/"fp_posterior_block_influence.csv",index=False)
    residuals.to_csv(OUTPUT/"fp_posterior_residuals.csv",index=False);sigma_frame.to_csv(OUTPUT/"fp_posterior_sigma_programme_sensitivity.csv",index=False)
    pd.concat([real_geometry,synthetic],ignore_index=True).to_csv(OUTPUT/"fp_posterior_real_vs_synthetic_geometry.csv",index=False)
    plots(profiles,pair,global_block,residuals)
    print("sample collapse",top.groupby("seed").first()[["ess","normalized_weight","cost"]].to_string())
    print("optimized",best,dict(zip(prototype.PARAMETERS,beta)))
    print("top blocks\n",global_block.nlargest(8,"cost")[["programme_block","cost","cost_per_observation"]].to_string(index=False))
    print("sigma\n",sigma_frame[["sigma_programme_s","ess","max_weight","top5_weight","optimized_best_cost"]].to_string(index=False))


if __name__=="__main__":main()
