#!/usr/bin/env python3
"""Diagnostic-only audit of the R11 FP programme classifier."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

import prototype_fp_programme_blocks as legacy


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "temp"


@dataclass(frozen=True)
class ClassifierConfig:
    push_lap_threshold: float = 1.03
    setting_first_substantial_stint_rule: str = "first_min_timed"
    setting_substantial_min_timed_laps: int = 4
    setting_max_session_progress: float = 0.35
    setting_min_push_laps: int = 2
    setting_min_fast_slow_fast_cycles: int = 1
    quali_sim_min_push_laps: int = 1
    long_run_min_consecutive_representative_laps: int = 6
    long_run_relative_pace_limit: float = 1.15
    long_run_variability_limit: float = 0.025
    long_run_min_representative_fraction: float = 0.60
    long_run_max_internal_gap_laps: int = 1


def fast_slow_fast_cycles(push: list[bool]) -> int:
    compressed = []
    for value in push:
        symbol = "F" if value else "S"
        if not compressed or compressed[-1] != symbol:
            compressed.append(symbol)
    return sum(compressed[index:index + 3] == ["F", "S", "F"]
               for index in range(len(compressed) - 2))


def max_consecutive_laps(lap_numbers: list[int], max_gap: int) -> int:
    if not lap_numbers:
        return 0
    best = current = 1
    for previous, current_lap in zip(lap_numbers, lap_numbers[1:]):
        current = current + 1 if current_lap - previous <= max_gap else 1
        best = max(best, current)
    return best


def prepare_runs(laps: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    groups, rows = {}, []
    for key, group in laps.groupby(["Session", "Team", "Driver", "Stint"], dropna=False, sort=True):
        group = group.sort_values(["SessionSeconds", "LapNumber"]).copy()
        run_id = ":".join(map(str, key)); groups[run_id] = group
        dry_timed = group[group.DryTimed]
        rows.append({"run_id": run_id, "session": key[0], "team": key[1], "driver": key[2],
                     "stint": key[3], "start_s": group.SessionSeconds.min(),
                     "end_s": group.SessionSeconds.max(), "total_physical_laps": len(group),
                     "total_timed_laps": len(dry_timed)})
    meta = pd.DataFrame(rows).sort_values(["session", "driver", "start_s", "stint"])
    meta["run_order"] = meta.groupby(["session", "driver"]).cumcount() + 1
    meta["prior_max_timed_laps"] = (meta.groupby(["session", "driver"]).total_timed_laps
                                      .transform(lambda values: values.shift().cummax()).fillna(0))
    meta["session_progress_start"] = meta.start_s / meta.groupby("session").end_s.transform("max")
    meta["first_raw_stint"] = meta.run_order.eq(1)
    meta["first_substantial"] = False
    for (_, _), driver_runs in meta.groupby(["session", "driver"], sort=False):
        eligible = driver_runs[driver_runs.total_timed_laps.ge(4)]
        if len(eligible): meta.loc[eligible.index[0], "first_substantial"] = True
    return meta, groups


def is_setting_candidate(row, config: ClassifierConfig) -> bool:
    if config.setting_first_substantial_stint_rule == "first_min_timed":
        return (row.total_timed_laps >= config.setting_substantial_min_timed_laps
                and row.prior_max_timed_laps < config.setting_substantial_min_timed_laps)
    if config.setting_first_substantial_stint_rule == "raw_first":
        return bool(row.first_raw_stint)
    if config.setting_first_substantial_stint_rule == "early_substantial":
        return row.total_timed_laps >= config.setting_substantial_min_timed_laps
    raise ValueError(config.setting_first_substantial_stint_rule)


def classify(group: pd.DataFrame, row, config: ClassifierConfig) -> dict:
    ordered = group.sort_values("LapNumber").copy()
    timed = ordered[ordered.DryTimed].copy()
    ordered["push_cfg"] = ordered.Representative & ordered.RelativePace.le(config.push_lap_threshold)
    roles = np.where(ordered.push_cfg, "PUSH",
                     np.where(ordered.Representative, "PREP_COOL", "OTHER"))
    representative = ordered[ordered.Representative]
    pushes = int(ordered.push_cfg.sum())
    push_pattern = ordered.loc[ordered.Representative, "push_cfg"].astype(bool).tolist()
    cycles = fast_slow_fast_cycles(push_pattern)
    same_compound = timed.Compound.nunique() == 1
    rep_fraction = len(representative) / max(len(timed), 1)
    pace_cv = (representative.LapTimeSeconds.std(ddof=1) / representative.LapTimeSeconds.mean()
               if len(representative) >= 2 else np.nan)
    max_relative = representative.RelativePace.max() if len(representative) else np.nan
    max_consecutive = max_consecutive_laps(
        representative.LapNumber.astype(int).tolist(), config.long_run_max_internal_gap_laps)

    setting_context = (row.session == "FP1" and is_setting_candidate(row, config)
                       and row.session_progress_start <= config.setting_max_session_progress)
    if (setting_context and len(timed) >= config.setting_substantial_min_timed_laps
            and pushes >= config.setting_min_push_laps
            and cycles >= config.setting_min_fast_slow_fast_cycles):
        label = "Setting"; confidence = "strong"
        reason = (f"FP1 + first substantial rule={config.setting_first_substantial_stint_rule} + "
                  f"{pushes} push-like laps + {cycles} fast-slow-fast cycle(s)")
    elif (same_compound and max_consecutive >= config.long_run_min_consecutive_representative_laps
          and max_relative <= config.long_run_relative_pace_limit
          and (np.isnan(pace_cv) or pace_cv <= config.long_run_variability_limit)
          and rep_fraction >= config.long_run_min_representative_fraction and cycles == 0):
        label = "LongRun"; confidence = "strong"
        reason = (f"one compound + {max_consecutive} consecutive representative laps + "
                  f"pace CV={pace_cv:.3f} + no fast-slow-fast attack cycle")
    elif pushes >= config.quali_sim_min_push_laps:
        label = "QualiSim"; confidence = "strong" if pushes >= 2 else "moderate"
        separators = max(0, int(ordered.Representative.sum()) - pushes)
        reason = (f"{pushes} near-best push(es), {separators} representative prep/cool lap(s); "
                  "not classified as early-FP1 Setting")
    else:
        label = "Uncertain"; confidence = "insufficient"
        reason = "no qualifying push and insufficient sustained race-like evidence"

    if label == "Uncertain":
        if len(ordered) <= 2 or len(representative) <= 1:
            uncertain_type = "TooShortOrInsufficient"
        elif len(representative) and representative.RelativePace.median() > config.long_run_relative_pace_limit:
            uncertain_type = "SlowOrSpecialTest"
        elif setting_context and cycles < config.setting_min_fast_slow_fast_cycles:
            uncertain_type = "PossibleSettingInsufficientPattern"
        elif same_compound and len(representative) >= 4:
            uncertain_type = "NearLongRunFailedContinuityOrVariability"
        else:
            uncertain_type = "GenuinelyAmbiguous"
    else:
        uncertain_type = "NotUncertain"

    return {"revised_label": label, "classification_reason": reason,
            "classification_evidence_strength": confidence,
            "uncertain_breakdown": uncertain_type, "push_laps": pushes,
            "fast_slow_fast_cycles": cycles, "same_compound": same_compound,
            "representative_laps": len(representative), "representative_fraction": rep_fraction,
            "longest_consecutive_representative": max_consecutive,
            "representative_pace_cv": pace_cv, "maximum_representative_relative_pace": max_relative,
            "lap_roles": "|".join(roles),
            "relative_pace_sequence": "|".join(f"{value:.4f}" if pd.notna(value) else "NA"
                                                 for value in ordered.RelativePace),
            "lap_time_sequence_s": "|".join(f"{value:.3f}" if pd.notna(value) else "NA"
                                              for value in ordered.LapTimeSeconds),
            "lap_number_sequence": "|".join(str(int(value)) for value in ordered.LapNumber),
            "compound": timed.Compound.mode().iloc[0] if len(timed) else "UNKNOWN",
            "compound_sequence": "|".join(ordered.Compound.fillna("UNKNOWN").astype(str)),
            "tyre_life_sequence": "|".join(f"{value:g}" if pd.notna(value) else "NA"
                                            for value in ordered.TyreLife),
            "pit_boundary_sequence": legacy.pattern(ordered)}


def classify_all(meta, groups, config):
    records = []
    for row in meta.itertuples():
        result = classify(groups[row.run_id], row, config)
        records.append({**row._asdict(), **result})
    return pd.DataFrame(records)


def add_legacy_labels(frame, groups):
    old = []
    for row in frame.itertuples():
        label, confidence, reason = legacy.classify_stint(groups[row.run_id][groups[row.run_id].DryTimed])
        old.append((label, confidence, reason))
    frame = frame.copy()
    frame[["current_label", "current_confidence", "current_reason"]] = pd.DataFrame(old, index=frame.index)
    return frame


def mark_model_critical(frame):
    result = frame.copy()
    result["programme_block"] = result.team + ":" + result.session + ":" + result.revised_label
    usable = result.revised_label.isin(["Setting", "QualiSim", "LongRun"])
    compounds_per_block = result[usable].groupby("programme_block").compound.transform("nunique")
    result["connects_compounds_in_block"] = False
    result.loc[result[usable].index, "connects_compounds_in_block"] = compounds_per_block.gt(1)
    driver_compounds = result[usable].groupby(["team", "session", "revised_label", "driver"]).compound.transform("nunique")
    result["same_driver_cross_compound"] = False
    result.loc[result[usable].index, "same_driver_cross_compound"] = driver_compounds.gt(1)
    result["teammate_cross_compound"] = False
    for _, group in result[usable].groupby(["team", "session", "revised_label"]):
        if group.driver.nunique() > 1 and group.compound.nunique() > 1:
            result.loc[group.index, "teammate_cross_compound"] = True
    long_counts = result[result.revised_label.eq("LongRun")].groupby("compound").run_id.transform("count")
    result["rare_compound_long_run"] = False
    indices = result.index[result.revised_label.eq("LongRun")]
    result.loc[indices, "rare_compound_long_run"] = long_counts.le(3).to_numpy()
    result["rare_hard_observation"] = usable & result.compound.eq("HARD")
    result["current_long_run_evidence"] = result.current_label.eq("LongRun")
    flags = ["connects_compounds_in_block", "same_driver_cross_compound",
             "teammate_cross_compound", "rare_compound_long_run", "rare_hard_observation",
             "current_long_run_evidence"]
    result["model_critical"] = result[flags].any(axis=1)
    result["model_critical_reason"] = result.apply(
        lambda row: "|".join(flag for flag in flags if row[flag]), axis=1)
    return result


def performance_selection(frame):
    frame = frame.copy()
    frame["contributes_to_later_dp"] = frame.revised_label.ne("Uncertain")
    frame["later_dp_lap_selection"] = np.select(
        [frame.revised_label.isin(["Setting", "QualiSim"]), frame.revised_label.eq("LongRun")],
        ["PUSH laps only", "representative sustained laps"], default="none")
    frame["exclusion_reason"] = np.where(frame.revised_label.eq("Uncertain"),
                                         frame.uncertain_breakdown, "")
    frame["human_summary"] = (frame.session + " " + frame.team + " " + frame.driver
                              + " run " + frame.run_order.astype(str) + ": "
                              + frame.revised_label + " — " + frame.classification_reason)
    return frame


def sensitivity(meta, groups, baseline):
    configs, summaries, transitions = [], [], []
    default = ClassifierConfig()
    # One-factor-at-a-time variants make individual threshold effects auditable
    # without conflating them in a large Cartesian grid.
    configs_to_test = [default]
    configs_to_test += [replace(default, push_lap_threshold=value) for value in (1.02, 1.05)]
    configs_to_test += [replace(default, setting_first_substantial_stint_rule="raw_first")]
    configs_to_test += [replace(default, long_run_min_consecutive_representative_laps=value)
                        for value in (5, 7)]
    configs_to_test += [replace(default, long_run_relative_pace_limit=value)
                        for value in (1.10, 1.20)]
    configs_to_test += [replace(default, long_run_variability_limit=value)
                        for value in (0.02, 0.04)]
    configs_to_test += [replace(default, setting_min_fast_slow_fast_cycles=2)]
    for index, config in enumerate(configs_to_test, 1):
        candidate = classify_all(meta, groups, config).set_index("run_id")
        joined = baseline.set_index("run_id")[["revised_label", "model_critical"]].join(
            candidate.revised_label.rename("candidate"))
        config_id = f"cfg_{index:03d}"
        summaries.append({"config_id": config_id, **asdict(config),
                          "overall_stability": (joined.revised_label == joined.candidate).mean(),
                          **{f"baseline_{label}_retention":
                             (joined.loc[joined.revised_label.eq(label), "candidate"] == label).mean()
                             for label in ("Setting", "QualiSim", "LongRun", "Uncertain")},
                          **{f"count_{label}": int(candidate.revised_label.eq(label).sum())
                             for label in ("Setting", "QualiSim", "LongRun", "Uncertain")},
                          "setting_to_qualisim": int(((joined.revised_label == "Setting") & (joined.candidate == "QualiSim")).sum()),
                          "qualisim_to_uncertain": int(((joined.revised_label == "QualiSim") & (joined.candidate == "Uncertain")).sum()),
                          "longrun_to_uncertain": int(((joined.revised_label == "LongRun") & (joined.candidate == "Uncertain")).sum()),
                          "model_critical_changed": int((joined.model_critical & (joined.revised_label != joined.candidate)).sum())})
        matrix = joined.groupby(["revised_label", "candidate"]).size().rename("run_count").reset_index()
        matrix["config_id"] = config_id
        critical = joined[joined.model_critical].groupby(["revised_label", "candidate"]).size().rename("model_critical_run_count")
        matrix = matrix.merge(critical.reset_index(), how="left", on=["revised_label", "candidate"])
        transitions.append(matrix.fillna({"model_critical_run_count": 0}))
        configs.append(config)
    return pd.DataFrame(summaries), pd.concat(transitions, ignore_index=True)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    laps = legacy.load_laps(); meta, groups = prepare_runs(laps)
    base = add_legacy_labels(classify_all(meta, groups, ClassifierConfig()), groups)
    base = performance_selection(mark_model_critical(base))
    sensitivity_frame, transition = sensitivity(meta, groups, base)

    config = ClassifierConfig()
    for field, value in asdict(config).items(): base[f"threshold_{field}"] = value
    current_vs = base[["run_id", "session", "team", "driver", "stint", "run_order",
                       "current_label", "revised_label", "current_reason",
                       "classification_reason", "uncertain_breakdown", "model_critical"]].copy()
    current_vs["changed"] = current_vs.current_label.ne(current_vs.revised_label)
    uncertain = (base[base.current_label.eq("Uncertain")]
                 .groupby(["uncertain_breakdown", "revised_label"]).size()
                 .rename("run_count").reset_index())
    critical = base[base.model_critical].copy()

    base.to_csv(OUTPUT / "fp_programme_classifier_ledger.csv", index=False)
    current_vs.to_csv(OUTPUT / "fp_programme_classifier_current_vs_revised.csv", index=False)
    sensitivity_frame.to_csv(OUTPUT / "fp_programme_classifier_sensitivity.csv", index=False)
    transition.to_csv(OUTPUT / "fp_programme_classifier_transition_matrix.csv", index=False)
    uncertain.to_csv(OUTPUT / "fp_programme_classifier_uncertain_breakdown.csv", index=False)
    critical.to_csv(OUTPUT / "fp_programme_classifier_model_critical_runs.csv", index=False)
    print(pd.crosstab(base.current_label, base.revised_label))
    print("model-critical", int(base.model_critical.sum()), "of", len(base))


if __name__ == "__main__":
    main()
