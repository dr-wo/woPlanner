#!/usr/bin/env python3
"""Diagnostic-only FP programme-block model prototype for 2026 R11."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
DATA = ROOT / "woData/fastf1/session_laps/schema_v1/year=2026/round=11"
SESSIONS = ("FP1", "FP2", "FP3")
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
PARAMETERS = ("fuel", "track_FP1", "track_FP2", "track_FP3",
              "deg_HARD", "deg_MEDIUM", "deg_SOFT", "perf_HARD", "perf_SOFT")


def load_laps():
    frames = []
    for session in SESSIONS:
        frame = pd.read_pickle(DATA / f"session={session}/laps.pkl").copy()
        frame["Session"] = session
        frames.append(frame)
    laps = pd.concat(frames, ignore_index=True)
    laps["LapTimeSeconds"] = laps.LapTime.dt.total_seconds()
    laps["Compound"] = laps.Compound.astype(str).str.upper()
    laps["SessionSeconds"] = pd.to_timedelta(laps.Time, errors="coerce").dt.total_seconds()
    laps["DryTimed"] = laps.Compound.isin(COMPOUNDS) & laps.LapTimeSeconds.notna()
    laps["Representative"] = (laps.DryTimed & laps.PitOutTime.isna() & laps.PitInTime.isna()
                               & laps.TrackStatus.astype(str).eq("1"))
    fastest = laps.loc[laps.Representative].groupby(["Session", "Driver"]).LapTimeSeconds.transform("min")
    # Align transformed values through a separate map because the filtered index is sparse.
    fast_map = laps.loc[laps.Representative].groupby(["Session", "Driver"]).LapTimeSeconds.min()
    laps["DriverSessionFastest"] = [fast_map.get((s, d), np.nan)
                                    for s, d in zip(laps.Session, laps.Driver)]
    laps["RelativePace"] = laps.LapTimeSeconds / laps.DriverSessionFastest
    laps["Push"] = laps.Representative & laps.RelativePace.le(1.03)
    laps["SlowCoolPrep"] = laps.DryTimed & ~laps.Push
    return laps


def pattern(group):
    symbols = []
    for row in group.sort_values("LapNumber").itertuples():
        if bool(row.PitOutTime == row.PitOutTime): symbols.append("O")
        elif bool(row.PitInTime == row.PitInTime): symbols.append("I")
        elif row.Push: symbols.append("F")
        else: symbols.append("S")
    return "/".join(symbols)


def classify_stint(group):
    ordered = group.sort_values("LapNumber")
    representative = ordered[ordered.Representative]
    n, pushes = len(ordered), int(ordered.Push.sum())
    rep = len(representative)
    transitions = sum(a != b for a, b in zip(ordered.Push.iloc[:-1], ordered.Push.iloc[1:]))
    pace_cv = (representative.LapTimeSeconds.std(ddof=1) / representative.LapTimeSeconds.mean()
               if rep >= 2 else np.nan)
    if rep >= 6 and pushes / max(rep, 1) <= .35 and (np.isnan(pace_cv) or pace_cv < .025):
        return "LongRun", .90, "at least 6 representative laps; race-like stable pace; little push/cool alternation"
    if ordered.Session.iloc[0] == "FP1" and n >= 4 and pushes >= 2 and transitions >= 2:
        return "Setting", .70, "early-FP1 repeated push/slow transitions"
    if pushes >= 1 and pushes <= max(4, int(np.ceil(n/2))):
        return "QualiSim", .75, "one or more near-session-best pushes separated by preparation/cool laps"
    if rep >= 5 and (np.isnan(pace_cv) or pace_cv < .04):
        return "LongRun", .60, "multi-lap representative sequence, weaker race-like signature"
    return "Uncertain", .30, "insufficient or irregular representative-lap pattern"


def inventory(laps):
    dry = laps[laps.DryTimed].copy()
    rows, lap_rows = [], []
    keys = ["Session", "Team", "Driver", "Stint"]
    for key, group in dry.groupby(keys, dropna=False, sort=True):
        programme, confidence, reason = classify_stint(group)
        group = group.sort_values("LapNumber")
        run_id = ":".join(map(str, key))
        rows.append({
            "session": key[0], "team": key[1], "driver": key[2], "stint": key[3],
            "run_id": run_id, "compound": "|".join(sorted(group.Compound.unique())),
            "lap_sequence": "|".join(map(lambda v: str(int(v)), group.LapNumber)),
            "lap_time_sequence_s": "|".join(f"{v:.3f}" for v in group.LapTimeSeconds),
            "tyre_life_sequence": "|".join(str(v) for v in group.TyreLife),
            "session_time_sequence_s": "|".join(f"{v:.1f}" for v in group.SessionSeconds),
            "pit_boundary_pattern": pattern(group), "run_length": len(group),
            "representative_laps": int(group.Representative.sum()), "push_laps": int(group.Push.sum()),
            "minimum_relative_pace": group.RelativePace.min(),
            "median_relative_pace": group.RelativePace.median(),
            "programme": programme, "classification_confidence": confidence,
            "classification_reason": reason,
        })
        for row in group.itertuples():
            lap_rows.append({
                "session": key[0], "team": key[1], "driver": key[2], "stint": key[3],
                "run_id": run_id, "programme": programme, "compound": row.Compound,
                "lap_number": row.LapNumber, "lap_time_s": row.LapTimeSeconds,
                "tyre_life": row.TyreLife, "session_time_s": row.SessionSeconds,
                "relative_pace": row.RelativePace, "representative": row.Representative,
                "push": row.Push, "pit_out": bool(row.PitOutTime == row.PitOutTime),
                "pit_in": bool(row.PitInTime == row.PitInTime),
            })
    return pd.DataFrame(rows), pd.DataFrame(lap_rows)


def model_observations(lap_inventory, classifications):
    data = lap_inventory.merge(classifications[["run_id", "programme"]], on="run_id",
                               suffixes=("", "_classified"))
    data["programme"] = data["programme_classified"]
    # Long runs use all representative race-like laps; other programmes use pushes only.
    use = ((data["programme"].eq("LongRun") & data.representative)
           | (data["programme"].isin(["QualiSim", "Setting"]) & data.push))
    data = data[use & data["programme"].ne("Uncertain")].copy()
    data["run_index"] = data.groupby("run_id").cumcount() + 1
    data["tyre_age"] = pd.to_numeric(data.tyre_life, errors="coerce")
    data["track_dense"] = data.groupby("session").session_time_s.rank(method="dense") - 1
    data["track_time"] = data.session_time_s - data.groupby("session").session_time_s.transform("min")
    data["track_time"] /= data.groupby("session").track_time.transform("max").replace(0, 1)
    data["track_progress"] = data.groupby("session").session_time_s.rank(pct=True)
    data["programme_block"] = (data["team"].astype(str) + ":" + data["session"].astype(str)
                               + ":" + data["programme"].astype(str))
    return data.reset_index(drop=True)


def teammate_estimates(data, *, selection_variant="push_1.03"):
    q = data[data["programme"].isin(["QualiSim", "Setting"])].copy()
    rows = []
    rng = np.random.default_rng(202611)
    for (team, session), group in q.groupby(["team", "session"]):
        drivers = sorted(group["driver"].unique())
        if len(drivers) != 2 or len(group) < 4: continue
        reference, other = drivers
        # Adjust push laps for compound using robust within-session pooled medians; deliberately broad.
        compound_median = group.groupby("compound").lap_time_s.median()
        centre = compound_median.mean()
        adjusted = group.lap_time_s - group.compound.map(compound_median) + centre
        values = {driver: adjusted[group["driver"].eq(driver)].to_numpy() for driver in drivers}
        if min(map(len, values.values())) < 1: continue
        draws = []
        for _ in range(2000):
            a = rng.choice(values[other], len(values[other]), replace=True)
            b = rng.choice(values[reference], len(values[reference]), replace=True)
            draws.append(np.median(a)-np.median(b))
        rows.append({"team": team, "session": session, "reference_driver": reference,
                     "driver": other, "source": "FP programme-classified push laps; no cached Q",
                     "selection_variant": selection_variant,
                     "reference_push_laps": len(values[reference]), "driver_push_laps": len(values[other]),
                     "median_delta_s": np.median(draws), "p10_delta_s": np.quantile(draws,.1),
                     "p90_delta_s": np.quantile(draws,.9), "uncertainty_width_s": np.quantile(draws,.9)-np.quantile(draws,.1)})
    return pd.DataFrame(rows)


def apply_driver_correction(data, teammate):
    result = data.copy(); result["driver_delta"] = 0.0
    for row in teammate.itertuples():
        mask = (result["team"].eq(row.team) & result["session"].eq(row.session)
                & result["driver"].eq(row.driver))
        result.loc[mask, "driver_delta"] = row.median_delta_s
    result["corrected_lap_time"] = result.lap_time_s - result.driver_delta
    return result


def build_physical_x(data, track_column):
    x = np.zeros((len(data), len(PARAMETERS)))
    x[:,0] = data.run_index
    for index, session in enumerate(SESSIONS, 1): x[:,index] = data["session"].eq(session) * data[track_column]
    for index, compound in enumerate(COMPOUNDS, 4): x[:,index] = data.compound.eq(compound) * data.tyre_age
    x[:,7] = data.compound.eq("HARD"); x[:,8] = data.compound.eq("SOFT")
    return x


def residualize(matrix, labels):
    codes, groups = pd.factorize(labels, sort=True)
    out = matrix.copy().astype(float)
    for code in range(len(groups)):
        mask = codes == code; out[mask] -= out[mask].mean(axis=0)
    return out, codes, groups


def diagnostics(data, architecture, track_column="track_time"):
    x = build_physical_x(data, track_column)
    if architecture == "production_19D":
        # Structural result from the completed exact production audit.
        return {"architecture":architecture,"physical_parameter_count":19,"nuisance_baselines":24,
                "observation_count":147,"rank":10,"nullity":9,"performance_sensitivity_norm":0,
                "fuel_identifiable":False,"absolute_degradation_identifiable":False,
                "degradation_contrasts_identifiable":True,"performance_contrasts_identifiable":False,
                "normalized_singular_values":"see fp_identifiability_summary.csv","condition_number":np.inf}
    labels = data["run_id"] if architecture == "prototype_1_free_run" else data["programme_block"]
    projected, codes, groups = residualize(x, labels)
    singular = np.linalg.svd(projected, compute_uv=False); tol=max(projected.shape)*np.finfo(float).eps*singular[0]
    rank=int(np.sum(singular>tol)); norms=np.linalg.norm(projected,axis=0)
    return {"architecture":architecture,"physical_parameter_count":9,"nuisance_baselines":len(groups),
            "observation_count":len(data),"rank":rank,"nullity":9-rank,
            "performance_sensitivity_norm":float(np.linalg.norm(norms[7:])),
            "fuel_identifiable":bool(rank==9),"absolute_degradation_identifiable":bool(rank==9),
            "degradation_contrasts_identifiable":bool(np.all(norms[4:7]>tol)),
            "performance_contrasts_identifiable":bool(np.all(norms[7:]>tol)),
            "normalized_singular_values":"|".join(f"{v/singular[0]:.6g}" for v in singular),
            "condition_number":float(singular[0]/singular[-1]) if rank==9 else np.inf,
            "minimum_normalized_singular":float(singular[-1]/singular[0])}


def identifiability_details(data, teammate, sigma_programme):
    """Export spectra, parameter correlations and exact/weak directions."""
    x = build_physical_x(data, "track_time")
    y = data.corrected_lap_time.to_numpy()
    spectra, correlations, directions = [], [], []
    for architecture in ("prototype_1_free_run", "prototype_2_exact_programme_baseline",
                         "prototype_3_programme_random_run"):
        if architecture == "prototype_1_free_run":
            projected, _, _ = residualize(x, data.run_id)
        elif architecture == "prototype_2_exact_programme_baseline":
            projected, _, _ = residualize(x, data["programme_block"])
        else:
            projected, _ = gls_project(data, x, y, sigma_programme, teammate)
        _, singular, vt = np.linalg.svd(projected, full_matrices=False)
        tolerance = max(projected.shape) * np.finfo(float).eps * singular[0]
        rank = int(np.sum(singular > tolerance))
        for index, value in enumerate(singular, 1):
            spectra.append({"architecture": architecture, "singular_index": index,
                            "singular_value": value,
                            "normalized_singular_value": value / singular[0],
                            "exact_null": value <= tolerance})
        norms = np.linalg.norm(projected, axis=0)
        normalized = projected / np.where(norms > tolerance, norms, 1)
        gram = normalized.T @ normalized
        for left in range(len(PARAMETERS)):
            for right in range(left + 1, len(PARAMETERS)):
                correlations.append({"architecture": architecture,
                                     "parameter_a": PARAMETERS[left],
                                     "parameter_b": PARAMETERS[right],
                                     "projected_column_correlation": gram[left, right]})
        selected = sorted(set(range(rank, len(PARAMETERS))) |
                          set(range(max(0, rank - 3), rank)))
        for vector_index in selected:
            vector = vt[vector_index]
            scale = np.max(np.abs(vector)) or 1
            for parameter, coefficient in zip(PARAMETERS, vector / scale):
                directions.append({"architecture": architecture,
                                   "direction_index": vector_index + 1,
                                   "direction_type": "exact_null" if vector_index >= rank else "weak_non_null",
                                   "normalized_singular_value": singular[vector_index] / singular[0],
                                   "parameter": parameter,
                                   "coefficient_max_abs_scaled": coefficient})
    return pd.DataFrame(spectra), pd.DataFrame(correlations), pd.DataFrame(directions)


def fit_linear(data, track_column, baseline="programme_block"):
    x=build_physical_x(data,track_column); labels=data[baseline]
    xp,codes,groups=residualize(x,labels); yp,_c,_g=residualize(data.corrected_lap_time.to_numpy()[:,None],labels); yp=yp[:,0]
    beta=np.linalg.lstsq(xp,yp,rcond=None)[0]; residual=yp-xp@beta
    return beta,residual,xp,yp,codes,groups


def gls_project(data, x, y, sigma_programme, teammate):
    """Profile programme means under run-correlated mismatch and teammate uncertainty."""
    blocks = []
    teammate_sigma = {(row.team,row.session,row.driver): row.uncertainty_width_s/2.563
                      for row in teammate.itertuples()}
    for block, group in data.groupby("programme_block", sort=True):
        indices = group.index.to_numpy()
        run_codes, runs = pd.factorize(group.run_id, sort=True)
        z = np.zeros((len(group), len(runs))); z[np.arange(len(group)),run_codes]=1
        run_variances=[]
        for run in runs:
            first=group[group.run_id.eq(run)].iloc[0]
            driver_sigma=teammate_sigma.get((first.team,first.session,first.driver),0.0)
            run_variances.append(sigma_programme**2+driver_sigma**2)
        covariance=.35**2*np.eye(len(group))+z@np.diag(run_variances)@z.T
        inverse=np.linalg.inv(covariance); ones=np.ones(len(group))
        projection=np.eye(len(group))-np.outer(ones,(ones@inverse)/(ones@inverse@ones))
        # Whiten after profiling the exact block mean.
        chol=np.linalg.cholesky(inverse)
        blocks.append((indices, chol.T@projection))
    xp=[];yp=[]
    for indices, transform in blocks:
        xp.append(transform@x[indices]);yp.append(transform@y[indices])
    return np.vstack(xp),np.concatenate(yp)


def mismatch(data, teammate):
    beta,residual,xp,yp,codes,groups=fit_linear(data,"track_time")
    # Estimate each run offset after physical correction, relative to its programme mean.
    corrected=data.corrected_lap_time.to_numpy()-build_physical_x(data,"track_time")@beta
    frame=data[["run_id","programme_block","team","session","programme","compound","driver"]].copy()
    frame["baseline_residual"]=corrected
    run=frame.groupby(["run_id","programme_block","team","session","programme","compound","driver"],as_index=False).baseline_residual.mean()
    run["programme_mean"]=run.groupby("programme_block").baseline_residual.transform("mean")
    run["run_offset_s"]=run.baseline_residual-run.programme_mean
    # Explicit same-compound pairs within a programme block.
    pairs=[]; compound_pairs=[]
    for block,g in run.groupby("programme_block"):
        for compound,cg in g.groupby("compound"):
            records=list(cg.itertuples())
            for i in range(len(records)):
                for j in range(i+1,len(records)):
                    pairs.append({"programme_block":block,"compound":compound,
                                  "run_a":records[i].run_id,"run_b":records[j].run_id,
                                  "driver_a":records[i].driver,"driver_b":records[j].driver,
                                  "corrected_baseline_difference_s":records[i].baseline_residual-records[j].baseline_residual})
        records=list(g.itertuples())
        for i in range(len(records)):
            for j in range(i+1,len(records)):
                if records[i].compound==records[j].compound: continue
                same_driver=records[i].driver==records[j].driver
                compound_pairs.append({
                    "programme_block":block,"team":records[i].team,"session":records[i].session,
                    "programme":records[i].programme,"run_a":records[i].run_id,"run_b":records[j].run_id,
                    "driver_a":records[i].driver,"driver_b":records[j].driver,
                    "compound_a":records[i].compound,"compound_b":records[j].compound,
                    "evidence_strength":"same_driver_same_session_programme" if same_driver else "teammate_corrected_same_team_session_programme",
                    "corrected_reference_pace_difference_s":records[i].baseline_residual-records[j].baseline_residual,
                })
    pair=pd.DataFrame(pairs)
    values=pair.corrected_baseline_difference_s.to_numpy() if len(pair) else run.run_offset_s.to_numpy()
    median=np.median(values); mad=np.median(np.abs(values-median)); sigma=1.4826*mad/np.sqrt(2) if len(pair) else 1.4826*mad
    summary=pd.DataFrame([{"same_compound_pair_count":len(pair),"run_offset_count":len(run),
                           "median_pair_difference_s":median,"robust_sigma_programme_s":sigma,
                           "p10_pair_difference_s":np.quantile(values,.1),"p90_pair_difference_s":np.quantile(values,.9),
                           "interpretation":"fuel/setup/programme/tyre-state/model mismatch, not pure fuel"}])
    return pair,pd.DataFrame(compound_pairs),summary,max(float(sigma),.15)


def ordered_candidates(n,seed):
    rng=np.random.default_rng(seed); u=(np.arange(n)[:,None]+rng.random((n,9)))/n
    for j in range(9): rng.shuffle(u[:,j])
    c=np.empty_like(u); c[:,0]=.1*u[:,0]
    c[:,1:4]=-.08+.16*u[:,1:4]
    hard=.2*u[:,4]; medium=hard+(.2-hard)*u[:,5]; soft=medium+(.2-medium)*u[:,6]
    c[:,4]=hard;c[:,5]=medium;c[:,6]=soft;c[:,7]=u[:,7];c[:,8]=-u[:,8]
    return c


def mc(data,sigma_programme,teammate,counts=(20000,50000,100000),seeds=(1,2026,424242,1898758267)):
    x=build_physical_x(data,"track_time"); y=data.corrected_lap_time.to_numpy()
    xp,yp=gls_project(data,x,y,sigma_programme,teammate)
    rows=[]
    for n in counts:
        for seed in seeds:
            c=ordered_candidates(n,seed); residual=yp[None,:]-c@xp.T
            cost=np.sum(residual**2,axis=1); lw=-.5*(cost-cost.min());w=np.exp(lw-lw.max());w/=w.sum()
            ess=1/np.sum(w*w)
            for j,name in enumerate(PARAMETERS):
                order=np.argsort(c[:,j]);cum=np.cumsum(w[order]); q=[c[order[min(np.searchsorted(cum,p),n-1)],j] for p in (.1,.5,.9)]
                rows.append({"sample_count":n,"seed":seed,"parameter":name,"p10":q[0],"median":q[1],"p90":q[2],"width":q[2]-q[0],
                             "ess":ess,"ess_fraction":ess/n,"max_weight":w.max(),"top5_weight":np.sort(w)[-5:].sum(),
                             "sigma_programme_s":sigma_programme,"measurement_sigma_s":.35})
    return pd.DataFrame(rows)


def hierarchy_weighting(data, sigma_programme, teammate, n=100000,
                        seeds=(1, 2026, 424242, 1898758267)):
    """Contrast hierarchy induced by the ordered proposal with likelihood weighting."""
    x = build_physical_x(data, "track_time"); y = data.corrected_lap_time.to_numpy()
    xp, yp = gls_project(data, x, y, sigma_programme, teammate)
    rows = []
    for seed in seeds:
        candidates = ordered_candidates(n, seed)
        gaps = {"deg_MEDIUM_minus_HARD": candidates[:, 5] - candidates[:, 4],
                "deg_SOFT_minus_MEDIUM": candidates[:, 6] - candidates[:, 5],
                "perf_HARD_minus_MEDIUM": candidates[:, 7],
                "perf_MEDIUM_minus_SOFT": -candidates[:, 8]}
        residual = yp[None, :] - candidates @ xp.T
        cost = np.sum(residual**2, axis=1)
        log_weight = -.5 * (cost - cost.min())
        weight = np.exp(log_weight - log_weight.max()); weight /= weight.sum()
        for name, values in gaps.items():
            for stage, stage_weight in (("ordered_proposal_pre_likelihood", np.full(n, 1/n)),
                                        ("likelihood_weighted", weight)):
                order = np.argsort(values); cumulative = np.cumsum(stage_weight[order])
                quantiles = [values[order[min(np.searchsorted(cumulative, p), n-1)]]
                             for p in (.1, .5, .9)]
                rows.append({"sample_count": n, "seed": seed, "gap": name, "stage": stage,
                             "p10": quantiles[0], "median": quantiles[1], "p90": quantiles[2],
                             "width": quantiles[2] - quantiles[0]})
    return pd.DataFrame(rows)


def synthetic(data,sigma_programme,teammate):
    truths=np.array([.04,-.02,.01,-.01,.03,.07,.12,.5,-.6]); x=build_physical_x(data,"track_time")
    rng=np.random.default_rng(991); block_codes,_=pd.factorize(data["programme_block"],sort=True);run_codes,_=pd.factorize(data["run_id"],sort=True)
    base=80+block_codes*.2; run_offset=rng.normal(0,sigma_programme,run_codes.max()+1); noise=rng.normal(0,.12,len(data))
    scenarios={"track_only":np.array([0,*truths[1:4],0,0,0,0,0]),
               "track_degradation":np.array([0,*truths[1:7],0,0]),
               "track_degradation_fuel":np.array([*truths[:7],0,0]),
               "add_performance":truths,
               "full_nuisance":truths}
    rows=[]
    for scenario,truth in scenarios.items():
        y=base+x@truth
        if scenario=="full_nuisance": y=y+run_offset[run_codes]+noise
        temp=data.copy();temp["corrected_lap_time"]=y
        result=mc(temp,sigma_programme,teammate,counts=(100000,),seeds=(2026,))
        result["scenario"]=scenario;result["truth"]=[truth[PARAMETERS.index(p)] for p in result.parameter]
        result["median_error"]=result["median"]-result.truth;result["truth_inside_p10_p90"]=(result.p10<=result.truth)&(result.truth<=result.p90)
        rows.append(result)
    return pd.concat(rows,ignore_index=True)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    laps=load_laps(); runs,lap_inventory=inventory(laps)
    classifications=runs[["run_id","session","team","driver","stint","programme","classification_confidence","classification_reason"]].copy()
    data=model_observations(lap_inventory,classifications)
    teammate=teammate_estimates(data); data=apply_driver_correction(data,teammate)
    classification_sensitivity=[]; teammate_variants=[teammate]
    base_programme=runs.set_index("run_id").programme
    for threshold in (1.02,1.05):
        variant_laps=laps.copy();variant_laps["Push"]=variant_laps.Representative & variant_laps.RelativePace.le(threshold)
        variant_laps["SlowCoolPrep"]=variant_laps.DryTimed & ~variant_laps.Push
        variant_runs,variant_lap_inventory=inventory(variant_laps)
        joined=base_programme.to_frame("base").join(variant_runs.set_index("run_id").programme.rename("variant"))
        classification_sensitivity.append({"push_relative_pace_threshold":threshold,
                                           "stint_count":len(joined),"classification_agreement_fraction":(joined.base==joined.variant).mean(),
                                           **{f"count_{name}":int(variant_runs.programme.eq(name).sum()) for name in ("Setting","QualiSim","LongRun","Uncertain")}})
        variant_class=variant_runs[["run_id","programme"]];variant_data=model_observations(variant_lap_inventory,variant_class)
        teammate_variants.append(teammate_estimates(variant_data,selection_variant=f"push_{threshold:.2f}"))
    teammate_all=pd.concat(teammate_variants,ignore_index=True)
    pairs,compound_pairs,mismatch_summary,sigma_programme=mismatch(data,teammate)
    architecture=[diagnostics(data,"production_19D")]
    for name in ("prototype_1_free_run","prototype_2_exact_programme_baseline","prototype_3_programme_random_run"):
        architecture.append(diagnostics(data,name))
    spectra, correlations, directions = identifiability_details(data, teammate, sigma_programme)
    weighted_x, _ = gls_project(data, build_physical_x(data, "track_time"),
                                data.corrected_lap_time.to_numpy(), sigma_programme, teammate)
    weighted_singular = np.linalg.svd(weighted_x, compute_uv=False)
    architecture[-1].update({
        "performance_sensitivity_norm": float(np.linalg.norm(weighted_x[:, 7:])),
        "normalized_singular_values": "|".join(
            f"{value/weighted_singular[0]:.6g}" for value in weighted_singular),
        "condition_number": float(weighted_singular[0] / weighted_singular[-1]),
        "minimum_normalized_singular": float(weighted_singular[-1] / weighted_singular[0]),
    })
    track_rows=[]
    for coordinate in ("track_dense","track_time","track_progress"):
        beta,residual,*_=fit_linear(data,coordinate)
        diag=diagnostics(data,"prototype_2_exact_programme_baseline",coordinate)
        track_rows.append({"track_coordinate":coordinate,"rmse_s":np.sqrt(np.mean(residual**2)),"rank":diag["rank"],
                           "minimum_normalized_singular":diag.get("minimum_normalized_singular"),
                           "gap_sensitivity":"dense rank ignores inactive time" if coordinate=="track_dense" else
                           "actual/normalized time includes red flags and inactive gaps" if coordinate=="track_time" else
                           "percentile rank ignores the duration of inactive gaps"})
    convergence=mc(data,sigma_programme,teammate)
    hierarchy = hierarchy_weighting(data, sigma_programme, teammate)
    synthetic_result=synthetic(data,sigma_programme,teammate)
    architecture_frame=pd.DataFrame(architecture)
    architecture_frame["fit_rmse_s"]=np.nan
    for i,row in architecture_frame.iterrows():
        if row.architecture!="production_19D":
            baseline="run_id" if row.architecture=="prototype_1_free_run" else "programme_block"
            architecture_frame.loc[i,"fit_rmse_s"]=np.sqrt(np.mean(fit_linear(data,"track_time",baseline)[1]**2))
    runs.to_csv(OUTPUT/"fp_programme_run_inventory.csv",index=False)
    classifications.to_csv(OUTPUT/"fp_programme_classification.csv",index=False)
    teammate_all.to_csv(OUTPUT/"fp_teammate_pace_estimates.csv",index=False)
    pd.DataFrame(classification_sensitivity).to_csv(OUTPUT/"fp_programme_classification_sensitivity.csv",index=False)
    pairs.to_csv(OUTPUT/"fp_same_compound_pair_residuals.csv",index=False)
    compound_pairs.to_csv(OUTPUT/"fp_compound_performance_pairs.csv",index=False)
    mismatch_summary.to_csv(OUTPUT/"fp_programme_mismatch_estimate.csv",index=False)
    architecture_frame.to_csv(OUTPUT/"fp_model_architecture_comparison.csv",index=False)
    architecture_frame.to_csv(OUTPUT/"fp_programme_identifiability.csv",index=False)
    spectra.to_csv(OUTPUT/"fp_programme_identifiability_spectrum.csv",index=False)
    correlations.to_csv(OUTPUT/"fp_programme_identifiability_correlations.csv",index=False)
    directions.to_csv(OUTPUT/"fp_programme_identifiability_directions.csv",index=False)
    pd.DataFrame(track_rows).to_csv(OUTPUT/"fp_track_coordinate_comparison.csv",index=False)
    convergence.to_csv(OUTPUT/"fp_programme_mc_convergence.csv",index=False)
    hierarchy.to_csv(OUTPUT/"fp_programme_hierarchy_weighting.csv",index=False)
    synthetic_result.to_csv(OUTPUT/"fp_programme_synthetic_recovery.csv",index=False)
    (OUTPUT/"fp_programme_prototype_manifest.json").write_text(json.dumps({
        "inventory_stints":len(runs),"prototype_observations":len(data),"programme_blocks":int(data["programme_block"].nunique()),
        "runs_used":int(data["run_id"].nunique()),"classification_counts":runs.programme.value_counts().to_dict(),
        "sigma_programme_s":sigma_programme,"production_modified":False},indent=2)+"\n")
    print((OUTPUT/"fp_programme_prototype_manifest.json").read_text())

if __name__=="__main__": main()
