#!/usr/bin/env python3
"""Diagnostic-only audit of foundational assumptions in the R11 FP model."""

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

import diagnose_fp_evidence_noise_policy as evidence
import prototype_fp_programme_blocks as prototype


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"
SESSIONS = prototype.SESSIONS
COMPOUNDS = prototype.COMPOUNDS
LAP_SIGMA = 0.50


@dataclass
class Specification:
    name: str
    parameter_names: list[str]
    x: np.ndarray
    bounds: list[tuple[float | None, float | None]]
    relaxed_bounds: list[tuple[float | None, float | None]]
    order_groups: list[list[int]]
    performance_pairs: list[tuple[int | None, int | None]]
    effective_team_degradation: dict[tuple[str, str], dict[int, float]]
    description: str


def load_inputs():
    laps, ledger, groups, full, _current, _l0, data, teammate, prepared, _selected = evidence.inputs()
    data = data.copy().reset_index(drop=True)
    full = full.copy()
    session_bounds = full.groupby("Session").SessionSeconds.agg(["min", "max"])
    physical_lookup = {}
    for run_id, group in full.groupby("run_id"):
        ordered = group.sort_values(["SessionSeconds", "LapNumber"])
        for ordinal, row in enumerate(ordered.itertuples(), 1):
            physical_lookup[(run_id, float(row.LapNumber))] = ordinal
    data["physical_stint_index"] = [
        physical_lookup[(run, float(lap))] for run, lap in zip(data["run_id"], data["lap_number"])
    ]
    # A positive fuel-rate coefficient multiplies fuel remaining.  The unknown
    # starting amount is absorbed by the block/run baseline, so -elapsed laps is
    # the correct identifiable physical coordinate.
    data["physical_fuel_remaining_proxy"] = -data.physical_stint_index.astype(float)
    data["physical_session_progress"] = [
        (time - session_bounds.loc[session, "min"])
        / max(session_bounds.loc[session, "max"] - session_bounds.loc[session, "min"], 1.0)
        for session, time in zip(data["session"], data["session_time_s"])
    ]
    representative_lap = full.loc[
        full.Representative & full.LapTimeSeconds.notna()
    ].groupby("Session").LapTimeSeconds.median()
    data["physical_session_lap_equivalent"] = [
        (time-session_bounds.loc[session,"min"])
        / max(float(representative_lap.loc[session]), 1.0)
        for session,time in zip(data["session"],data["session_time_s"])
    ]
    all_times = full.groupby("Session").SessionSeconds.apply(
        lambda values: sorted(pd.to_numeric(values, errors="coerce").dropna().unique())
    )
    data["physical_chronological_progress"] = [
        np.searchsorted(all_times[session], time)
        / max(len(all_times[session]) - 1, 1)
        for session, time in zip(data["session"], data["session_time_s"])
    ]
    # Keep the pre-existing selected-observation definitions explicitly.
    data["current_fuel_coordinate"] = data.run_index.astype(float)
    data["current_track_coordinate"] = data.track_time.astype(float)
    sigma_programme = float(
        pd.read_csv(OUTPUT / "fp_programme_mismatch_estimate.csv")
        .robust_sigma_programme_s.iloc[0]
    )
    return laps, ledger, groups, full, data, teammate, prepared, sigma_programme


def covariance_blocks(data, teammate, sigma_programme):
    teammate_sigma = {
        (row.team, row.session, row.driver): row.uncertainty_width_s / 2.563
        for row in teammate.itertuples()
    }
    blocks = []
    for name, group in data.groupby("programme_block", sort=True):
        indices = group.index.to_numpy()
        run_codes, runs = pd.factorize(group.run_id, sort=True)
        z = np.zeros((len(group), len(runs)))
        z[np.arange(len(group)), run_codes] = 1.0
        run_variances = []
        for run in runs:
            first = group[group.run_id.eq(run)].iloc[0]
            q_sigma = teammate_sigma.get((first.team, first.session, first.driver), 0.0)
            run_variances.append(sigma_programme**2 + q_sigma**2)
        g = np.diag(run_variances)
        covariance = LAP_SIGMA**2 * np.eye(len(group)) + z @ g @ z.T
        inverse = np.linalg.inv(covariance)
        ones = np.ones(len(group))
        projection = np.eye(len(group)) - np.outer(
            ones, (ones @ inverse) / (ones @ inverse @ ones)
        )
        transform = np.linalg.cholesky(inverse).T @ projection
        blocks.append({
            "name": name, "indices": indices, "group": group, "z": z,
            "g": g, "inverse": inverse, "transform": transform,
            "runs": runs, "run_codes": run_codes,
        })
    return blocks


def projected(blocks, x, y):
    return (
        np.vstack([block["transform"] @ x[block["indices"]] for block in blocks]),
        np.concatenate([block["transform"] @ y[block["indices"]] for block in blocks]),
    )


def add_column(names, columns, bounds, relaxed, name, values, bound, relaxed_bound):
    names.append(name); columns.append(np.asarray(values, float))
    bounds.append(bound); relaxed.append(relaxed_bound)
    return len(names) - 1


def make_spec(data, name):
    names, columns, bounds, relaxed = [], [], [], []
    orders, performance = [], []
    team_effects = {}

    corrected = name != "B0_current_9D"
    net = name in {"B5_net_secular", "B6_net_session_D", "B7_net_session_DP"}
    session_d = name in {"B2_session_D", "B4_session_DP", "B6_net_session_D", "B7_net_session_DP"}
    session_p = name in {"B4_session_DP", "B7_net_session_DP"}
    team_d = name == "B3_team_compound_D"

    if net:
        for session in SESSIONS:
            add_column(names, columns, bounds, relaxed, f"net_{session}",
                       data["session"].eq(session) * data.physical_session_progress,
                       (-2.0, 2.0), (-5.0, 5.0))
    else:
        fuel_values = (data.physical_fuel_remaining_proxy if corrected
                       else data.current_fuel_coordinate)
        add_column(names, columns, bounds, relaxed, "fuel", fuel_values,
                   (0.0, 0.10), (-0.20, 0.20))
        track_values = (data.physical_session_lap_equivalent if corrected
                        else data.current_track_coordinate)
        for session in SESSIONS:
            add_column(names, columns, bounds, relaxed, f"track_{session}",
                       data["session"].eq(session) * track_values,
                       (-0.08, 0.08), (-2.0, 2.0))

    if session_d:
        for session in SESSIONS:
            group = []
            for compound in COMPOUNDS:
                mask = data["session"].eq(session) & data["compound"].eq(compound)
                if not mask.any():
                    continue
                group.append(add_column(
                    names, columns, bounds, relaxed, f"deg_{session}_{compound}",
                    mask * data.tyre_age, (0.0, 0.20), (-0.20, 0.50)
                ))
            orders.append(group)
    else:
        group = []
        for compound in COMPOUNDS:
            group.append(add_column(
                names, columns, bounds, relaxed, f"deg_{compound}",
                data["compound"].eq(compound) * data.tyre_age,
                (0.0, 0.20), (-0.20, 0.50)
            ))
        orders.append(group)

    if team_d:
        # Sum-to-zero contrasts identify the global value as the unweighted mean
        # across teams with actual projected age support; no shrinkage is tuned.
        for compound in COMPOUNDS:
            eligible = []
            for team, group in data[data["compound"].eq(compound)].groupby("team"):
                if len(group) >= 2 and group.tyre_age.nunique() >= 2:
                    eligible.append(team)
            eligible = sorted(eligible)
            if len(eligible) < 2:
                continue
            reference = eligible[-1]
            for team in eligible[:-1]:
                index = add_column(
                    names, columns, bounds, relaxed, f"teamdeg_{team}_{compound}",
                    (data["team"].eq(team) & data["compound"].eq(compound)).astype(float) * data.tyre_age
                    - (data["team"].eq(reference) & data["compound"].eq(compound)).astype(float) * data.tyre_age,
                    (-0.20, 0.20), (-0.50, 0.50)
                )
                team_effects.setdefault((team, compound), {})[index] = 1.0
                team_effects.setdefault((reference, compound), {})[index] = -1.0

    if session_p:
        for session in SESSIONS:
            hard = soft = None
            if (data["session"].eq(session) & data["compound"].eq("HARD")).any():
                hard = add_column(names, columns, bounds, relaxed, f"perf_{session}_HARD",
                                  data["session"].eq(session) & data["compound"].eq("HARD"),
                                  (0.0, 1.0), (-3.0, 3.0))
            if (data["session"].eq(session) & data["compound"].eq("SOFT")).any():
                soft = add_column(names, columns, bounds, relaxed, f"perf_{session}_SOFT",
                                  data["session"].eq(session) & data["compound"].eq("SOFT"),
                                  (-1.0, 0.0), (-3.0, 3.0))
            performance.append((hard, soft))
    else:
        hard = add_column(names, columns, bounds, relaxed, "perf_HARD",
                          data["compound"].eq("HARD"), (0.0, 1.0), (-3.0, 3.0))
        soft = add_column(names, columns, bounds, relaxed, "perf_SOFT",
                          data["compound"].eq("SOFT"), (-1.0, 0.0), (-3.0, 3.0))
        performance.append((hard, soft))

    descriptions = {
        "B0_current_9D": "selected-observation Fuel index; selected-endpoint SessionTime; shared D/P",
        "B1_physical_coordinates": "fuel-remaining physical stint laps; full-session time in lap-equivalents; shared D/P",
        "B2_session_D": "B1 plus supported session-local degradation",
        "B3_team_compound_D": "B1 plus sum-zero supported team-compound degradation nuisance",
        "B4_session_DP": "B1 plus supported session-local degradation and performance",
        "B5_net_secular": "full-session-time NetSecular per session; shared D/P",
        "B6_net_session_D": "NetSecular plus supported session-local degradation",
        "B7_net_session_DP": "NetSecular plus supported session-local D/P",
    }
    return Specification(name, names, np.column_stack(columns), bounds, relaxed,
                         orders, performance, team_effects, descriptions[name])


def constraint_functions(spec, bounds_mode):
    constraints = []
    # Ordering remains active in constrained and relaxed modes.
    for group in spec.order_groups:
        for left, right in zip(group, group[1:]):
            constraints.append({"type": "ineq", "fun": lambda b, l=left, r=right: b[r] - b[l]})
    if spec.name == "B3_team_compound_D":
        global_index = {c: spec.parameter_names.index(f"deg_{c}") for c in COMPOUNDS}
        teams = sorted({team for team, _ in spec.effective_team_degradation})
        low, high = ((0.0, 0.20) if bounds_mode == "constrained" else (-0.20, 0.50))
        for team in teams:
            effective = {}
            for compound in COMPOUNDS:
                if (team, compound) not in spec.effective_team_degradation:
                    continue
                coeff = spec.effective_team_degradation[(team, compound)]
                def value(b, c=compound, mapping=coeff):
                    return b[global_index[c]] + sum(weight * b[i] for i, weight in mapping.items())
                effective[compound] = value
                constraints.append({"type": "ineq", "fun": lambda b, f=value, lo=low: f(b) - lo})
                constraints.append({"type": "ineq", "fun": lambda b, f=value, hi=high: hi - f(b)})
            for left, right in zip(COMPOUNDS, COMPOUNDS[1:]):
                if left in effective and right in effective:
                    constraints.append({"type": "ineq", "fun": lambda b, l=effective[left], r=effective[right]: r(b)-l(b)})
    return constraints


def initial(spec):
    values = np.zeros(len(spec.parameter_names))
    for i, name in enumerate(spec.parameter_names):
        if name == "fuel": values[i] = 0.05
        elif name.endswith("HARD") and name.startswith("deg"): values[i] = 0.04
        elif name.endswith("MEDIUM") and name.startswith("deg"): values[i] = 0.08
        elif name.endswith("SOFT") and name.startswith("deg"): values[i] = 0.12
        elif name.endswith("HARD") and name.startswith("perf"): values[i] = 0.5
        elif name.endswith("SOFT") and name.startswith("perf"): values[i] = -0.5
    return values


def fit(spec, xp, yp, mode):
    if mode == "unconstrained":
        beta = np.linalg.lstsq(xp, yp, rcond=None)[0]
        return beta, float(np.sum((yp-xp@beta)**2)), True
    use_bounds = spec.bounds if mode == "constrained" else spec.relaxed_bounds
    constraints = constraint_functions(spec, mode) if mode in {"constrained", "relaxed"} else []
    if mode == "no_order":
        use_bounds = spec.relaxed_bounds
    starts = [initial(spec), np.zeros(len(spec.parameter_names))]
    least = np.linalg.lstsq(xp, yp, rcond=None)[0]
    starts.append(np.array([
        min(max(value, low if low is not None else -np.inf), high if high is not None else np.inf)
        for value, (low, high) in zip(least, use_bounds)
    ]))
    best = None
    for start in starts:
        result = minimize(
            lambda b: float(np.sum((yp-xp@b)**2)), start, method="SLSQP",
            bounds=use_bounds, constraints=constraints,
            options={"ftol": 1e-11, "maxiter": 4000},
        )
        if result.success and (best is None or result.fun < best.fun):
            best = result
    if best is None:
        result = minimize(lambda b: float(np.sum((yp-xp@b)**2)), starts[0], method="SLSQP",
                          bounds=use_bounds, constraints=constraints,
                          options={"ftol": 1e-9, "maxiter": 8000})
        best = result
    return best.x, float(np.sum((yp-xp@best.x)**2)), bool(best.success)


def raw_fit(data, blocks, x, beta, y):
    residual = np.empty(len(data)); baseline = {}; run_offsets = {}
    for block in blocks:
        idx = block["indices"]
        remainder = y[idx] - x[idx] @ beta
        inv = block["inverse"]; one = np.ones(len(idx))
        mean = float(one @ inv @ remainder / (one @ inv @ one))
        centred = remainder - mean
        # Conditional mean of random run mismatch under the stated covariance.
        offsets = block["g"] @ block["z"].T @ inv @ centred
        residual[idx] = centred - block["z"] @ offsets
        baseline[block["name"]] = mean
        run_offsets.update(dict(zip(block["runs"], offsets)))
    return residual, baseline, run_offsets


def boundary_count(spec, beta):
    count = 0
    for value, (low, high) in zip(beta, spec.bounds):
        if low is not None and high is not None and high > low:
            fraction = min((value-low)/(high-low), (high-value)/(high-low))
            count += fraction <= 0.01
    for group in spec.order_groups:
        count += sum(beta[r]-beta[l] <= 0.002 for l, r in zip(group, group[1:]))
    return int(count)


def coordinate_audit(full, data, prepared):
    selected = set(zip(data["run_id"], data["lap_number"].astype(float)))
    clean = set(zip(prepared.Session, prepared.Driver, prepared.Stint, prepared.LapNumber))
    rows = []
    for run_id, group in full.groupby("run_id"):
        programme = group.programme.iloc[0]
        if programme not in {"Setting", "QualiSim", "LongRun"}:
            continue
        ordered = group.sort_values(["SessionSeconds", "LapNumber"])
        selected_model_rows = data[data["run_id"].eq(run_id)]
        first_selected_physical = None
        first_selected_current = None
        if len(selected_model_rows):
            first_model = selected_model_rows.sort_values("lap_number").iloc[0]
            first_selected_physical = next(
                ordinal for ordinal, physical in enumerate(ordered.itertuples(), 1)
                if float(physical.LapNumber) == float(first_model.lap_number)
            )
            first_selected_current = float(first_model.current_fuel_coordinate)
        selected_counter = 0
        selected_rows = data[data["run_id"].eq(run_id)].set_index("lap_number")
        for physical_index, row in enumerate(ordered.itertuples(), 1):
            is_selected = (run_id, float(row.LapNumber)) in selected
            if is_selected: selected_counter += 1
            current = selected_rows.loc[row.LapNumber] if is_selected else None
            rows.append({
                "run_id": run_id, "programme": programme, "session": row.Session,
                "team": row.Team, "driver": row.Driver, "stint": row.Stint,
                "compound": row.Compound, "physical_lap_number": row.LapNumber,
                "physical_stint_index": physical_index,
                "selected_observation_index": selected_counter if is_selected else np.nan,
                "current_fuel_coordinate": current.current_fuel_coordinate if is_selected else np.nan,
                "correct_physical_fuel_remaining_proxy": -float(physical_index),
                "fuel_elapsed_coordinate_for_audit": float(physical_index),
                "session_time_s": row.SessionSeconds, "tyre_life": row.TyreLife,
                "selected": is_selected,
                "individually_clean_longrun": (
                    (row.Session, row.Driver, row.Stint, row.LapNumber) in clean
                    if programme == "LongRun" else np.nan
                ),
                "current_track_coordinate": current.current_track_coordinate if is_selected else np.nan,
                "physical_session_time_normalized": current.physical_session_progress if is_selected else np.nan,
                "physical_session_lap_equivalent": current.physical_session_lap_equivalent if is_selected else np.nan,
                "physical_chronological_progress": current.physical_chronological_progress if is_selected else np.nan,
                "fuel_coordinate_constant_offset_laps": (
                    physical_index-float(current.current_fuel_coordinate)
                    if is_selected else np.nan
                ),
                "fuel_gap_compression_laps": (
                    (physical_index-first_selected_physical)
                    - (float(current.current_fuel_coordinate)-first_selected_current)
                    if is_selected else np.nan
                ),
            })
    frame = pd.DataFrame(rows)
    # Export all physical laps for representative examples and selected rows for
    # the remainder, keeping the artifact manageable and fully auditable.
    candidates = []
    for programme in ("Setting", "QualiSim", "LongRun"):
        inventory = frame[frame.programme.eq(programme)].groupby("run_id").agg(
            physical=("physical_lap_number", "size"), selected=("selected", "sum"),
            max_compression=("fuel_gap_compression_laps", "max")
        )
        candidates.extend(inventory.sort_values(["max_compression", "physical"], ascending=False).head(3).index)
    return frame[frame.run_id.isin(candidates) | frame.selected].copy(), frame


def geometry_table(data, blocks):
    rows = []
    for session in SESSIONS:
        indices = data.index[data["session"].eq(session)].to_numpy()
        sub_blocks = []
        index_set = set(indices)
        for block in blocks:
            if set(block["indices"]).issubset(index_set): sub_blocks.append(block)
        representations = {
            "current_selected_index_and_normalized_selected_time": (
                data.current_fuel_coordinate.to_numpy(),
                data.current_track_coordinate.to_numpy(),
            ),
            "physical_fuel_remaining_and_session_lap_equivalent": (
                data.physical_fuel_remaining_proxy.to_numpy(),
                data.physical_session_lap_equivalent.to_numpy(),
            ),
        }
        for representation,(fuel,track) in representations.items():
            x = np.column_stack((fuel, track))
            xp, _ = projected(sub_blocks, x, data.corrected_lap_time.to_numpy())
            norms = np.linalg.norm(xp, axis=0)
            corr = float(xp[:,0] @ xp[:,1] / max(norms[0]*norms[1], 1e-15))
            singular = np.linalg.svd(xp, compute_uv=False)
            rows.append({
                "representation":representation,"session": session, "observations": len(indices),
                "projected_rank": int(np.linalg.matrix_rank(xp)),
                "projected_fuel_track_correlation": corr,
                "fuel_column_norm": norms[0], "track_column_norm": norms[1],
                "singular_1": singular[0], "singular_2": singular[-1],
                "normalized_weak_singular": singular[-1]/singular[0],
                "dominant_to_residual_ratio": singular[0]/singular[-1],
            })
    return pd.DataFrame(rows)


def effective_team_values(spec, beta):
    rows=[]
    global_index={c:spec.parameter_names.index(f"deg_{c}") for c in COMPOUNDS}
    for (team,compound),mapping in spec.effective_team_degradation.items():
        deviation=sum(weight*beta[index] for index,weight in mapping.items())
        rows.append({"team":team,"compound":compound,"weekend_mean":beta[global_index[compound]],
                     "team_deviation":deviation,"effective_degradation":beta[global_index[compound]]+deviation})
    return pd.DataFrame(rows)


def session_values(spec, beta, data):
    rows=[]
    for parameter,value in zip(spec.parameter_names,beta):
        if not (parameter.startswith("deg_FP") or parameter.startswith("perf_FP")): continue
        kind,session,compound=parameter.split("_",2)
        support=data[data["session"].eq(session)&data["compound"].eq(compound)]
        rows.append({"variant":spec.name,"parameter":kind,"session":session,"compound":compound,
                     "value":value,"observations":len(support),"runs":support.run_id.nunique(),
                     "teams":support.team.nunique(),"tyre_age_min":support.tyre_age.min(),
                     "tyre_age_max":support.tyre_age.max(),"tyre_age_span":support.tyre_age.max()-support.tyre_age.min()})
    frame=pd.DataFrame(rows)
    if len(frame):
        frame["weekend_supported_session_mean"] = frame.groupby(
            ["variant","parameter","compound"]
        ).value.transform("mean")
        frame["session_deviation_from_weekend_mean"] = (
            frame.value-frame.weekend_supported_session_mean
        )
        return frame.to_dict("records")
    return rows


def trusted_blocks(data):
    chosen = ["Williams:FP2:LongRun", "Alpine:FP3:LongRun",
              "Ferrari:FP2:QualiSim", "Williams:FP2:QualiSim"]
    same_driver = None
    for block, group in data.groupby("programme_block"):
        for _driver, driver in group.groupby("driver"):
            if driver.compound.nunique() >= 2:
                same_driver = block; break
        if same_driver: break
    if same_driver and same_driver not in chosen: chosen.append(same_driver)
    return [value for value in chosen if value in set(data["programme_block"])]


def forward_examples(data, blocks, spec, beta, y):
    selected_blocks = trusted_blocks(data)
    reference = np.zeros(len(spec.parameter_names))
    for i,name in enumerate(spec.parameter_names):
        if name == "fuel": reference[i]=.05
        elif name == "deg_HARD": reference[i]=.04
        elif name == "deg_MEDIUM": reference[i]=.08
        elif name == "deg_SOFT": reference[i]=.12
        elif name == "perf_HARD": reference[i]=.5
        elif name == "perf_SOFT": reference[i]=-.6
    rows=[]
    for label,candidate in (("current_constrained_MAP",beta),("physical_reference_not_fitted",reference)):
        residual,means,offsets=raw_fit(data,blocks,spec.x,candidate,y)
        for block in blocks:
            if block["name"] not in selected_blocks: continue
            group=data.loc[block["indices"]]
            for index,row in group.iterrows():
                terms={name:spec.x[index,j]*candidate[j] for j,name in enumerate(spec.parameter_names)}
                fuel=sum(v for k,v in terms.items() if k=="fuel" or k.startswith("net_"))
                track=sum(v for k,v in terms.items() if k.startswith("track_"))
                degradation=sum(v for k,v in terms.items() if k.startswith("deg_"))
                performance=sum(v for k,v in terms.items() if k.startswith("perf_"))
                prediction=(means[block["name"]]+offsets.get(row.run_id,0)+row.driver_delta+
                            fuel+track+degradation+performance)
                rows.append({
                    "candidate":label,"programme_block":block["name"],"run_id":row.run_id,
                    "session":row["session"],"team":row["team"],"driver":row["driver"],
                    "programme":row.programme,"compound":row.compound,"lap_number":row.lap_number,
                    "tyre_age":row.tyre_age,"observed_lap_time":row.lap_time_s,
                    "programme_mean":means[block["name"]],"run_offset":offsets.get(row.run_id,0),
                    "driver_delta":row.driver_delta,"fuel_or_net_correction":fuel,
                    "track_correction":track,"tyre_degradation_correction":degradation,
                    "compound_performance_correction":performance,"predicted_lap_time":prediction,
                    "residual":row.lap_time_s-prediction,
                })
    return pd.DataFrame(rows)


def plots(audit, session_frame, team_frame, geometry, model_frame, forward):
    examples=audit[audit.run_id.isin(audit.groupby("programme").run_id.first())]
    fig,axes=plt.subplots(1,3,figsize=(17,5))
    for ax,(programme,g) in zip(axes,examples.groupby("programme")):
        for run,rg in g.groupby("run_id"):
            ax.plot(rg.physical_stint_index,rg.correct_physical_fuel_remaining_proxy,color="grey")
            s=rg[rg.selected];ax.scatter(s.physical_stint_index,s.current_fuel_coordinate,label="current selected index")
            ax.scatter(s.physical_stint_index,s.correct_physical_fuel_remaining_proxy,marker="x",label="physical remaining proxy")
        ax.set(title=programme,xlabel="physical stint index",ylabel="Fuel coordinate");ax.grid(alpha=.2)
    axes[0].legend(fontsize=8);fig.tight_layout();fig.savefig(OUTPUT/"fp_physical_vs_selected_fuel_progression.png",dpi=180);plt.close(fig)

    fig,axes=plt.subplots(1,2,figsize=(14,5))
    for ax,kind,variant in zip(axes,("deg","perf"),("B2_session_D","B4_session_DP")):
        q=session_frame[(session_frame.parameter.eq(kind)) & (session_frame.variant.eq(variant))]
        for compound,g in q.groupby("compound"):
            ax.plot(g.session,g.value,marker="o",label=compound)
        ax.set(title=f"Session-local {kind} preference",ylabel="s/lap" if kind=="deg" else "s");ax.grid(alpha=.2);ax.legend()
    fig.tight_layout();fig.savefig(OUTPUT/"fp_session_dp_preference.png",dpi=180);plt.close(fig)

    if len(team_frame):
        pivot=team_frame.pivot(index="team",columns="compound",values="effective_degradation")
        ax=pivot.plot.bar(figsize=(14,6));ax.set(ylabel="effective degradation (s/lap)",title="B3 team-compound nuisance values");ax.grid(axis="y",alpha=.2);plt.tight_layout();plt.savefig(OUTPUT/"fp_team_degradation_variation.png",dpi=180);plt.close()

    pivot=geometry.pivot(index="session",columns="representation",values="projected_fuel_track_correlation")
    ax=pivot.plot.bar(figsize=(10,5));ax.axhline(0,color="black");ax.set(ylabel="projected correlation",title="Fuel vs Track geometry");ax.grid(axis="y",alpha=.2);plt.tight_layout();plt.savefig(OUTPUT/"fp_fuel_track_projected_geometry.png",dpi=180);plt.close()

    fig,axes=plt.subplots(1,2,figsize=(15,5));axes[0].bar(model_frame.variant,model_frame.raw_rms_s);axes[1].bar(model_frame.variant,model_frame.boundary_contacts)
    for ax in axes:ax.tick_params(axis="x",rotation=60);ax.grid(axis="y",alpha=.2)
    axes[0].set(ylabel="raw residual RMS (s)");axes[1].set(ylabel="boundary contacts");fig.tight_layout();fig.savefig(OUTPUT/"fp_model_variant_residual_boundary.png",dpi=180);plt.close(fig)

    summary=forward.groupby(["candidate","programme_block"]).residual.apply(lambda x:np.sqrt(np.mean(x*x))).unstack(0)
    ax=summary.plot.bar(figsize=(14,6));ax.set(ylabel="raw RMS (s)",title="Hand-audited forward examples");ax.grid(axis="y",alpha=.2);plt.tight_layout();plt.savefig(OUTPUT/"fp_forward_sanity_examples.png",dpi=180);plt.close()


def main():
    OUTPUT.mkdir(exist_ok=True)
    laps,ledger,groups,full,data,teammate,prepared,sigma_programme=load_inputs()
    y=data.corrected_lap_time.to_numpy(float)
    blocks=covariance_blocks(data,teammate,sigma_programme)
    audit,complete_audit=coordinate_audit(full,data,prepared)
    audit.to_csv(OUTPUT/"fp_progress_coordinate_audit.csv",index=False)

    affected=complete_audit[complete_audit.selected]
    coordinate_summary={
        "selected_observations":len(affected),
        "fuel_compressed_observations":int((affected.fuel_gap_compression_laps>0).sum()),
        "maximum_fuel_compression_laps":float(affected.fuel_gap_compression_laps.max()),
        "mean_fuel_compression_laps":float(affected.fuel_gap_compression_laps.mean()),
    }
    (OUTPUT/"fp_progress_coordinate_summary.json").write_text(json.dumps(coordinate_summary,indent=2))

    geometry=geometry_table(data,blocks);geometry.to_csv(OUTPUT/"fp_fuel_track_geometry.csv",index=False)
    specs=[make_spec(data,name) for name in (
        "B0_current_9D","B1_physical_coordinates","B2_session_D","B3_team_compound_D",
        "B4_session_DP","B5_net_secular","B6_net_session_D","B7_net_session_DP")]
    model_rows=[];comparison_rows=[];session_rows=[];team_rows=[];residual_group_rows=[];fits={}
    for spec in specs:
        xp,yp=projected(blocks,spec.x,y)
        singular=np.linalg.svd(xp,compute_uv=False);rank=int(np.linalg.matrix_rank(xp))
        for mode in ("constrained","relaxed","no_order","unconstrained"):
            beta,cost,success=fit(spec,xp,yp,mode)
            raw,means,offsets=raw_fit(data,blocks,spec.x,beta,y)
            row={"variant":spec.name,"description":spec.description,"fit_mode":mode,
                 "parameter_count":len(spec.parameter_names),"rank":rank,"nullity":len(spec.parameter_names)-rank,
                 "minimum_normalized_singular":singular[-1]/singular[0] if len(singular) else np.nan,
                 "condition_number":singular[0]/singular[-1] if singular[-1]>1e-12 else np.inf,
                 "whitened_cost":cost,"raw_rms_s":np.sqrt(np.mean(raw*raw)),
                 "raw_p90_absolute_residual_s":np.quantile(np.abs(raw),.9),"optimization_success":success,
                 "boundary_contacts":boundary_count(spec,beta) if mode=="constrained" else np.nan,
                 "top3_raw_sse_share":pd.Series(raw*raw,index=data["programme_block"]).groupby(level=0).sum().nlargest(3).sum()/np.sum(raw*raw),
                 "parameter_values":json.dumps(dict(zip(spec.parameter_names,beta))) }
            comparison_rows.append(row)
            for parameter,value in zip(spec.parameter_names,beta):
                model_rows.append({**row,"parameter":parameter,"value":value})
            if mode=="constrained":
                fits[spec.name]=(spec,beta,xp,yp,raw,means,offsets)
                session_rows.extend(session_values(spec,beta,data))
                residual_frame=data[["team","session","programme","compound","programme_block"]].copy()
                residual_frame["residual_s"]=raw
                for dimension in ("team","session","programme","compound"):
                    for group_value,group in residual_frame.groupby(dimension):
                        residual_group_rows.append({"variant":spec.name,"group_dimension":dimension,
                            "group_value":group_value,"observations":len(group),
                            "raw_rms_s":np.sqrt(np.mean(group.residual_s**2)),
                            "raw_sse":np.sum(group.residual_s**2),
                            "p90_absolute_residual_s":np.quantile(np.abs(group.residual_s),.9)})
                if spec.name=="B3_team_compound_D":
                    t=effective_team_values(spec,beta);t["variant"]=spec.name;team_rows.extend(t.to_dict("records"))
    model=pd.DataFrame(comparison_rows)
    constrained_cost = model[model.fit_mode.eq("constrained")].set_index("variant").whitened_cost
    model["cost_improvement_over_constrained"] = model.apply(
        lambda row: constrained_cost[row.variant]-row.whitened_cost, axis=1
    )
    model["cost_improvement_over_B0_constrained"] = (
        constrained_cost["B0_current_9D"]-model.whitened_cost
    )
    model.to_csv(OUTPUT/"fp_model_variant_comparison.csv",index=False)
    pd.DataFrame(model_rows).to_csv(OUTPUT/"fp_constrained_vs_unconstrained.csv",index=False)
    session_frame=pd.DataFrame(session_rows);session_frame.to_csv(OUTPUT/"fp_session_dp_variation.csv",index=False)
    team_frame=pd.DataFrame(team_rows);team_frame.to_csv(OUTPUT/"fp_team_degradation_variation.csv",index=False)
    pd.DataFrame(residual_group_rows).to_csv(OUTPUT/"fp_model_variant_residual_groups.csv",index=False)
    model[model.variant.isin(["B1_physical_coordinates","B5_net_secular","B6_net_session_D","B7_net_session_DP"])].to_csv(OUTPUT/"fp_net_secular_comparison.csv",index=False)

    b0spec,b0beta,*_=fits["B0_current_9D"]
    forward=forward_examples(data,blocks,b0spec,b0beta,y)
    forward.to_csv(OUTPUT/"fp_forward_sanity_examples.csv",index=False)
    plots(audit,session_frame,team_frame,geometry,model[model.fit_mode.eq("constrained")],forward)
    print("coordinate",coordinate_summary)
    print(model[model.fit_mode.eq("constrained")][["variant","parameter_count","rank","nullity","whitened_cost","raw_rms_s","boundary_contacts"]].to_string(index=False))
    print("geometry\n",geometry.to_string(index=False))


if __name__ == "__main__":
    main()
