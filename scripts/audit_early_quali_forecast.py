"""Research-only early qualifying audit; never writes production or frozen inputs.

Selection: chronological inner validation within R1--10 only. R11--15/R18 are
development evidence. R16/R17 field-model outcomes are unblinded only after a
saved choices/script-hash manifest; their raw/legacy TE sources were seen before.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np

from wostrategy.analysis.quali_forecast import (
    DriverPace, ForecastPrior, forecast_final_cutoff, forecast_partial_field_bounds,
)


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "outputs/r18_prediction_resilience_20261025"
OUTPUT = ROOT / "outputs/r18_early_forecast_audit_20261025"
METHODS = ("frozen", "prior_only_field_shape", "current_only", "q1_transfer", "reliability")
GRID = [(threshold, weight) for threshold in (.005, .01, .02, .03, .05)
        for weight in (0., .25, .5, .75, 1.)]


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def quantile(values, probability):
    return float(np.quantile(values, probability))


def summary(cuts, **extra):
    if len(cuts) < 2:
        return {"estimate_s": None, "lower_s": None, "upper_s": None, "scenario_count": len(cuts), **extra}
    return {"estimate_s": quantile(cuts, .5), "lower_s": quantile(cuts, .1),
            "upper_s": quantile(cuts, .9), "scenario_count": len(cuts), **extra}


def model_prior(records, *, revision):
    return ForecastPrior.from_records([{**row, "split": "train"} for row in records],
                                     revision=revision, metadata={"research_only": True})


def driver_inputs(record, method):
    return tuple(DriverPace(row["driver"],
                            None if method == "q1_transfer" and record["part"] == "Q2" else row["current_best_s"],
                            None if method == "current_only" else row["previous_part_best_s"])
                 for row in record["drivers"])


def evaluate_basis(record, prior, method):
    result = forecast_final_cutoff(driver_inputs(record, method), event_id=record["event_id"],
                                   part=record["part"], remaining_s=record["remaining_s"], prior=prior,
                                   target_rank=record["target_rank"], part_finalised=False)
    return result


def cap_transfer(record, result):
    current = {row["driver"]: row["current_best_s"] for row in record["drivers"]}
    scenarios = {}
    for scenario in result.scenarios:
        values = {code: min(value, current[code]) if value is not None and current[code] is not None else value
                  for code, value in scenario.driver_final_bests_s}
        scenarios[scenario.training_event] = values
    return scenarios


def basis(record, prior):
    frozen = evaluate_basis(record, prior, "frozen")
    current = evaluate_basis(record, prior, "current_only")
    transfer = evaluate_basis(record, prior, "q1_transfer")
    return {"frozen": frozen, "current_only": current, "q1_transfer": transfer,
            "frozen_vectors": {s.training_event: dict(s.driver_final_bests_s) for s in frozen.scenarios},
            "transfer_vectors": cap_transfer(record, transfer)}


def reliability(record, base, threshold, weight):
    if record["part"] != "Q2":
        return base["frozen"].estimate_s, [s.cutoff_s for s in base["frozen"].scenarios]
    rows = {row["driver"]: row for row in record["drivers"]}
    cuts = []
    for event, values in base["frozen_vectors"].items():
        transferred = base["transfer_vectors"].get(event)
        if transferred is None:
            continue
        finals = []
        for code, value in values.items():
            row = rows[code]
            current, previous = row["current_best_s"], row["previous_part_best_s"]
            if (current is not None and previous is not None and previous > 0
                    and current / previous - 1 > threshold
                    and transferred.get(code) is not None and value is not None):
                value = weight * value + (1 - weight) * transferred[code]
            if value is not None:
                finals.append(value)
        if len(finals) >= record["target_rank"]:
            cuts.append(sorted(finals)[record["target_rank"] - 1])
    # Never gain conditional accuracy by quietly dropping a difficult original
    # forecast. If the alternate basis lacks support, retain the original.
    if len(cuts) < 2:
        cuts = [s.cutoff_s for s in base["frozen"].scenarios]
    return quantile(cuts, .5) if len(cuts) >= 2 else None, cuts


def prior_only(record, training):
    """Historical field-shape prior conditional on a CAUSAL circuit pace scale.

    Q2 uses median available Q1 times; Q1 uses median observed Q bests. Without
    any circuit/pace scale an absolute-seconds prior is not defensible. This
    method ignores individual current-Q2 rank/improvement information.
    """
    part, lead = record["part"], record["remaining_s"]
    key = "previous_part_best_s" if part == "Q2" else "current_best_s"
    anchors = [row[key] for row in record["drivers"] if row[key] is not None]
    if len(anchors) < 3:
        return summary([], limitation="No causal circuit pace scale")
    scale = quantile(anchors, .5)
    event_ratios = {}
    for row in training:
        if row["part"] != part:
            continue
        values = [driver[key] for driver in row["drivers"] if driver[key] is not None]
        if len(values) >= 3:
            event_ratios.setdefault(row["event_id"], []).append((row["remaining_s"], row["final_cutoff_s"] / quantile(values, .5)))
    cuts = []
    for rows in event_ratios.values():
        rows.sort()
        if rows[0][0] <= lead <= rows[-1][0]:
            ratio = float(np.interp(lead, [row[0] for row in rows], [row[1] for row in rows]))
            value = ratio * scale
            if record["observed_cutoff_s"] is not None:
                value = min(value, record["observed_cutoff_s"])
            cuts.append(value)
    return summary(cuts, limitation="Historical field shape conditional on a causal median pace scale")


def predictions(record, prior, training, settings, base=None):
    base = base or basis(record, prior)
    rows = {}
    for method in ("frozen", "current_only"):
        result = base[method]
        rows[method] = summary([scenario.cutoff_s for scenario in result.scenarios])
    transfer_cuts = [sorted(value for value in scenario.values() if value is not None)[record["target_rank"] - 1]
                     for scenario in base["transfer_vectors"].values()
                     if sum(value is not None for value in scenario.values()) >= record["target_rank"]]
    rows["q1_transfer"] = summary(transfer_cuts)
    _, cuts = reliability(record, base, *settings)
    rows["reliability"] = summary(cuts)
    rows["prior_only_field_shape"] = prior_only(record, training)
    quality = input_quality(record)
    return [{"event_id": record["event_id"], "part": record["part"], "remaining_s": record["remaining_s"],
             "target_s": record["final_cutoff_s"], "observed_s": record["observed_cutoff_s"],
             "quality": quality, "method": method, **result} for method, result in rows.items()]


def input_quality(record):
    drivers = record["drivers"]
    current = [row for row in drivers if row["current_best_s"] is not None]
    suspicious = [row for row in current if row["previous_part_best_s"] is not None
                  and row["current_best_s"] / row["previous_part_best_s"] > 1.03]
    return {"roster": len(drivers), "current_pace": len(current), "untimed": len(drivers) - len(current),
            "previous_q1_pace": sum(row["previous_part_best_s"] is not None for row in drivers),
            "completed_credible_attempts": sum(row.get("attempt_count", 0) for row in drivers),
            "slow_anchor_count_3pct_diagnostic": len(suspicious),
            "category": "SLOW_CURRENT_ANCHOR" if suspicious else "INCOMPLETE_CURRENT_FIELD" if len(current) < len(drivers) else "COMPLETE_CURRENT_FIELD"}


def select_settings(validation_bases):
    """Event-weighted inner chronological validation, never development labels."""
    scores = []
    for threshold, weight in GRID:
        losses = {}
        for record, base in validation_bases:
            if record["part"] != "Q2":
                continue
            value, _ = reliability(record, base, threshold, weight)
            if value is not None:
                losses.setdefault(record["event_id"], []).append(abs(value - record["final_cutoff_s"]) / record["final_cutoff_s"])
        score = float(np.mean([np.mean(values) for values in losses.values()])) if losses else math.inf
        scores.append((score, -threshold, -weight, threshold, weight))
    if not validation_bases:
        return (.05, 1.), None
    best = min(scores)
    return (best[-2], best[-1]), best[0]


def metrics(rows):
    available = [row for row in rows if row["estimate_s"] is not None]
    errors = np.array([row["estimate_s"] - row["target_s"] for row in available])
    return {"rows": len(rows), "available": len(available), "availability": len(available) / len(rows) if rows else None,
            "mae_s": float(np.abs(errors).mean()) if len(errors) else None,
            "rmse_s": float(np.sqrt(np.square(errors).mean())) if len(errors) else None,
            "bias_s": float(errors.mean()) if len(errors) else None,
            "coverage": float(np.mean([row["lower_s"] <= row["target_s"] <= row["upper_s"] for row in available])) if available else None,
            "mean_width_s": float(np.mean([row["upper_s"] - row["lower_s"] for row in available])) if available else None}


def grouped_metrics(rows):
    groups = {"all": rows,
              **{f"part/{part}": [row for row in rows if row["part"] == part] for part in ("Q1", "Q2")},
              **{f"lead/{lead:g}": [row for row in rows if row["remaining_s"] == lead] for lead in sorted({row["remaining_s"] for row in rows})},
              **{f"quality/{quality}": [row for row in rows if row["quality"]["category"] == quality] for quality in {row["quality"]["category"] for row in rows}},
              **{f"event/{event}": [row for row in rows if row["event_id"] == event] for event in {row["event_id"] for row in rows}}}
    return {name: {method: metrics([row for row in values if row["method"] == method]) for method in METHODS}
            for name, values in groups.items()}


def cluster_comparison(rows, candidate="reliability"):
    events = {}
    for event in {row["event_id"] for row in rows}:
        selected = [row for row in rows if row["event_id"] == event]
        frozen = {(row["part"], row["remaining_s"]): row for row in selected if row["method"] == "frozen"}
        comparisons = []
        for row in selected:
            reference = frozen.get((row["part"], row["remaining_s"]))
            if row["method"] != candidate or reference is None or row["estimate_s"] is None or reference["estimate_s"] is None:
                continue
            comparisons.append((abs(reference["estimate_s"] - row["target_s"]), abs(row["estimate_s"] - row["target_s"])))
        if comparisons:
            events[event] = {"paired_checkpoints": len(comparisons), "frozen_mae_s": float(np.mean([x[0] for x in comparisons])),
                             "candidate_mae_s": float(np.mean([x[1] for x in comparisons]))}
    differences = np.array([value["candidate_mae_s"] - value["frozen_mae_s"] for value in events.values()])
    bootstrap = np.random.default_rng(1893).choice(differences, size=(10000, len(differences)), replace=True).mean(axis=1) if len(differences) else []
    return {"candidate": candidate, "events": events, "independent_event_count": len(events),
            "mean_event_mae_difference_s": float(differences.mean()) if len(differences) else None,
            "event_cluster_bootstrap_95pct_s": [quantile(bootstrap, .025), quantile(bootstrap, .975)] if len(bootstrap) else None,
            "limitation": "Events, not checkpoints, are resampling units; very small event counts do not establish generalization."}


def rolling_experiment(records, output):
    train = [row for row in records if row["round"] <= 10]
    validation_bases, outer_rows, settings_log = [], [], []
    for event in range(4, 11):
        earlier = [row for row in train if row["round"] < event]
        prior = model_prior(earlier, revision=f"research-prequential-before-R{event}")
        selected, score = select_settings(validation_bases)
        event_rows = [row for row in train if row["round"] == event]
        bases = [(record, basis(record, prior)) for record in event_rows]
        for record, base in bases:
            outer_rows.extend(predictions(record, prior, earlier, selected, base))
        settings_log.append({"validation_event": event, "prior_events": sorted({r["event_id"] for r in earlier}),
                             "inner_validation_events": sorted({r["event_id"] for r, _ in validation_bases}),
                             "threshold": selected[0], "current_weight": selected[1], "inner_score": score})
        validation_bases.extend(bases)
    selected, score = select_settings(validation_bases)
    choices = {"threshold": selected[0], "current_weight": selected[1], "selection_event_weighted_normalized_mae": score,
               "selection_data": "Only causal expanding-prior validation eventsR4--R10, each prior strictly earlier than its validation event",
               "new_outcome_holdouts": ["2026-R16", "2026-R17"],
               "holdout_disclosure": "Their raw recordings/legacy Track Evolution were previously inspected; new field-model outcome comparisons were not used for this selection.",
               "development_only_events": ["2026-R11", "2026-R12", "2026-R13", "2026-R14", "2026-R15", "2026-R18"],
               "script_sha256_before_new_outcome_unblinding": digest(Path(__file__)),
               "frozen_prior_sha256": digest(FROZEN / "forecast_prior_r1_r10.json"),
               "frozen_records_sha256": digest(FROZEN / "historical_records.json"),
               "grid": GRID, "settings_log": settings_log,
               "interval_calibration": "Event-block prequential maximum outside-interval scores, train events only; additive normalized residual quantile, no arbitrary width multiplier"}
    output.mkdir(parents=True, exist_ok=True)
    (output / "choices_before_unblinding.json").write_text(json.dumps(choices, indent=2) + "\n")
    (output / "prequential_predictions.json").write_text(json.dumps(outer_rows, indent=2) + "\n")
    (output / "prequential_metrics.json").write_text(json.dumps(grouped_metrics(outer_rows), indent=2) + "\n")
    (output / "prequential_event_comparison.json").write_text(json.dumps(cluster_comparison(outer_rows), indent=2) + "\n")
    print(json.dumps({"settings": selected, "prequential": grouped_metrics(outer_rows)["all"]}), flush=True)
    return choices, outer_rows


def calibration_scores(rows):
    scores = {}
    for row in rows:
        if row["estimate_s"] is None:
            continue
        bucket = "early" if row["remaining_s"] >= 600 else "middle" if row["remaining_s"] >= 300 else "late"
        key = (row["method"], row["part"], bucket)
        value = max(row["lower_s"] - row["target_s"], row["target_s"] - row["upper_s"], 0.) / row["target_s"]
        scores.setdefault(key, {})[row["event_id"]] = max(value, scores.get(key, {}).get(row["event_id"], 0.))
    return scores


def calibrated(row, scores):
    if row["estimate_s"] is None:
        return {**row, "calibration_events": 0}
    bucket = "early" if row["remaining_s"] >= 600 else "middle" if row["remaining_s"] >= 300 else "late"
    values = list(scores.get((row["method"], row["part"], bucket), {}).values())
    if not values:
        return {**row, "calibration_events": 0}
    probability = min(1., math.ceil((len(values) + 1) * .8) / len(values))
    score = float(np.quantile(values, probability, method="higher"))
    width = score * row["estimate_s"]
    return {**row, "lower_s": row["lower_s"] - width, "upper_s": row["upper_s"] + width,
            "calibration_events": len(values), "calibration_normalized_score": score}


def conditional_calibration_scores(rows):
    scores = {}
    for row in rows:
        if row["estimate_s"] is None:
            continue
        bucket = "early" if row["remaining_s"] >= 600 else "middle" if row["remaining_s"] >= 300 else "late"
        key = (row["method"], row["part"], bucket, row["quality"]["category"])
        value = max(row["lower_s"] - row["target_s"], row["target_s"] - row["upper_s"], 0.) / row["target_s"]
        scores.setdefault(key, {})[row["event_id"]] = max(value, scores.get(key, {}).get(row["event_id"], 0.))
    return scores


def conditionally_calibrated(row, conditional_scores, marginal_scores):
    bucket = "early" if row["remaining_s"] >= 600 else "middle" if row["remaining_s"] >= 300 else "late"
    values = conditional_scores.get((row["method"], row["part"], bucket, row["quality"]["category"]), {})
    if len(values) < 3:
        return {**calibrated(row, marginal_scores), "calibration_source": "MARGINAL_SMALL_CONDITIONAL_SAMPLE"}
    return {**calibrated(row, {(row["method"], row["part"], bucket): values}), "calibration_source": "INPUT_QUALITY_EVENT_BLOCK"}


def development(records, choices, prequential, output):
    prior = ForecastPrior.from_dict(json.loads((FROZEN / "forecast_prior_r1_r10.json").read_text()))
    training = [row for row in records if row["round"] <= 10]
    settings = (choices["threshold"], choices["current_weight"])
    rows = []
    for record in records:
        if 11 <= record["round"] <= 15:
            rows.extend(predictions(record, prior, training, settings))
    adjusted = [calibrated(row, calibration_scores(prequential)) for row in rows]
    (output / "development_predictions.json").write_text(json.dumps(rows, indent=2) + "\n")
    (output / "development_metrics.json").write_text(json.dumps(grouped_metrics(rows), indent=2) + "\n")
    (output / "development_calibrated_metrics.json").write_text(json.dumps(grouped_metrics(adjusted), indent=2) + "\n")
    (output / "development_calibrated_predictions.json").write_text(json.dumps(adjusted, indent=2) + "\n")
    conditional = [conditionally_calibrated(row, conditional_calibration_scores(prequential), calibration_scores(prequential)) for row in rows]
    (output / "development_conditional_calibrated_metrics.json").write_text(json.dumps(grouped_metrics(conditional), indent=2) + "\n")
    (output / "development_event_comparison.json").write_text(json.dumps(cluster_comparison(rows), indent=2) + "\n")
    return rows


def oracle_audit(records, output):
    """Future measurements are explicitly diagnostic; never selection/validation."""
    prior = ForecastPrior.from_dict(json.loads((FROZEN / "forecast_prior_r1_r10.json").read_text()))
    laps = json.loads((FROZEN / "historical_laps.json").read_text())
    cases = []
    for record in records:
        if record["round"] != 15 or record["part"] != "Q2" or record["remaining_s"] < 420:
            continue
        changed = []
        drivers = []
        for row in record["drivers"]:
            previous, current = row["previous_part_best_s"], row["current_best_s"]
            replacement = current
            if previous is not None and (current is None or current > 1.01 * previous):
                future = sorted((lap for lap in laps if lap["event_id"] == record["event_id"]
                                 and lap["part"] == "Q2" and lap["driver"] == row["driver"]
                                 and lap["completion_s"] > record["checkpoint_time_s"]
                                 and lap["lap_time_s"] is not None and lap["lap_time_s"] <= 1.07 * previous
                                 and not lap["pit_in"] and not lap["pit_out"]
                                 and all(value is not None and value > 0 for value in lap["sectors_s"])),
                                key=lambda lap: lap["completion_s"])
                if future:
                    replacement = min(current, future[0]["lap_time_s"]) if current is not None else future[0]["lap_time_s"]
                    changed.append({"driver": row["driver"], "old_s": current, "future_observation_s": replacement,
                                    "future_information_delay_s": future[0]["completion_s"] - record["checkpoint_time_s"]})
            drivers.append(DriverPace(row["driver"], replacement, previous))
        result = forecast_final_cutoff(tuple(drivers), event_id=record["event_id"], part="Q2",
                                       remaining_s=record["remaining_s"], prior=prior, target_rank=10)
        cases.append({"event_id": record["event_id"], "remaining_s": record["remaining_s"], "target_s": record["final_cutoff_s"],
                      "oracle_estimate_s": result.estimate_s, "oracle_lower_s": result.lower_s, "oracle_upper_s": result.upper_s,
                      "changed_inputs": changed,
                      "status": "ORACLE_NONCAUSAL_DIAGNOSTIC_ONLY",
                      "limitation": "Future first representative Q2 laps injected into the same estimator. Not a valid forecast, training example, lower bound on irreducible error, or untouched evaluation."})
    (output / "r15_oracle_information_gap.json").write_text(json.dumps(cases, indent=2) + "\n")


def collect_new_outcome_event(round_number, output):
    """Recorded packet validation: no retrospective earlier-capture reconstruction."""
    from wodata.live import QualiSessionStore
    from wodata.live.replay import ReplayPlayer
    data_root = ROOT.parent / "woData"
    recording = next((data_root / f"wodata/live_timing/schema_v1/year=2026/round={round_number}/session=Q").glob("live_timing_*.jsonl"))
    store = QualiSessionStore()
    active, previous_best, captured, targets = set(), {}, {}, {}
    previous_raw = None
    bootstrap_part = True
    previous_remaining = None
    first_remaining = {}
    latest_positive = {}

    def ingest(message):
        nonlocal previous_raw, active, previous_best, bootstrap_part, previous_remaining
        raw = store.apply(message)
        if previous_raw is not None and raw.session_part != previous_raw.session_part:
            old = previous_raw.session_part
            prior_active = [driver for driver in previous_raw.drivers if driver.driver_number in active]
            if previous_raw.session_part_established and prior_active:
                valid = sorted(driver.best_lap_s for driver in prior_active if driver.best_lap_s is not None)
                rank = {"Q1": 16, "Q2": 10}.get(old)
                if rank and len(valid) >= rank:
                    targets[old] = valid[rank - 1]
                ordered = sorted(prior_active, key=lambda d: (d.position is None, d.position or 999, d.driver_code))
                active = {driver.driver_number for driver in ordered[:max(0, len(ordered) - 6)]}
                previous_best = {driver.driver_number: driver.best_lap_s for driver in prior_active}
                bootstrap_part = False
            else:
                active = {driver.driver_number for driver in raw.drivers if driver.knocked_out is False}
                bootstrap_part = True
            previous_remaining = None
        if raw.session_part == "Q1":
            active.update(driver.driver_number for driver in raw.drivers)
        elif bootstrap_part:
            active.update(driver.driver_number for driver in raw.drivers if driver.knocked_out is False)
            active.difference_update(driver.driver_number for driver in raw.drivers if driver.knocked_out is True)
        part, remaining = raw.session_part, raw.remaining_time_s
        if part in {"Q1", "Q2"} and remaining is not None:
            first_remaining.setdefault(part, remaining)
            drivers = [{"driver": driver.driver_code, "current_best_s": driver.best_lap_s,
                        "previous_part_best_s": dict(driver.historical_results).get("Q1", previous_best.get(driver.driver_number)) if part == "Q2" else None,
                        "attempt_count": driver.completed_laps, "last_completion_time_s": None}
                       for driver in raw.drivers if driver.driver_number in active]
            values = sorted(driver["current_best_s"] for driver in drivers if driver["current_best_s"] is not None)
            rank = {"Q1": 16, "Q2": 10}[part]
            if len(values) >= rank:
                latest_positive[part] = values[rank - 1]
            for lead in (900., 780., 600., 420., 300., 180., 120., 60., 0.):
                if ((part, lead) not in captured and first_remaining[part] >= lead
                        and remaining <= lead and (previous_remaining is None or previous_remaining >= lead)):
                    captured[part, lead] = {"event_id": f"2026-R{round_number}", "round": round_number,
                                           "part": part, "remaining_s": remaining, "nominal_grid_lead_s": lead,
                                           "target_rank": rank, "drivers": drivers,
                                           "observed_cutoff_s": values[rank - 1] if len(values) >= rank else None,
                                           "sequence": message.sequence, "receipt_utc": message.received_at_utc.isoformat(),
                                           "provenance": "Native original recording, original source/receipt times preserved by ReplayPlayer; no future metadata reconstructed"}
            previous_remaining = remaining
        previous_raw = raw

    result = ReplayPlayer(recording, speed=0).play(ingest)
    records = []
    for record in captured.values():
        if record["part"] in targets:
            records.append({**record, "final_cutoff_s": targets[record["part"]]})
    manifest = {"round": round_number, "recording": str(recording), "sha256": digest(recording),
                "result": asdict(result), "first_captured_remaining_s": first_remaining,
                "final_targets_evaluation_only": targets, "records": len(records),
                "limits": "Only captured horizons evaluated; late Q2 join uses official supplied KnockedOut/history registers. Earlier timing is not rebuilt from later packets."}
    (output / f"r{round_number}_new_outcome_inputs.json").write_text(json.dumps(records, indent=2) + "\n")
    (output / f"r{round_number}_new_outcome_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return records


def validate_new_outcomes(records, choices, prequential, output):
    prior = ForecastPrior.from_dict(json.loads((FROZEN / "forecast_prior_r1_r10.json").read_text()))
    training = [row for row in records if row["round"] <= 10]
    settings = choices["threshold"], choices["current_weight"]
    rows = []
    for event in (16, 17):
        for record in collect_new_outcome_event(event, output):
            rows.extend(predictions(record, prior, training, settings))
    marginal = [calibrated(row, calibration_scores(prequential)) for row in rows]
    conditional = [conditionally_calibrated(row, conditional_calibration_scores(prequential), calibration_scores(prequential)) for row in rows]
    (output / "new_outcome_predictions.json").write_text(json.dumps(rows, indent=2) + "\n")
    (output / "new_outcome_metrics.json").write_text(json.dumps(grouped_metrics(rows), indent=2) + "\n")
    (output / "new_outcome_calibrated_metrics.json").write_text(json.dumps(grouped_metrics(marginal), indent=2) + "\n")
    (output / "new_outcome_conditional_calibrated_metrics.json").write_text(json.dumps(grouped_metrics(conditional), indent=2) + "\n")
    (output / "new_outcome_event_comparison.json").write_text(json.dumps(cluster_comparison(rows), indent=2) + "\n")
    print(json.dumps({"new_outcomes": grouped_metrics(rows)["all"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--unblind-new-outcomes", action="store_true")
    args = parser.parse_args()
    records = json.loads((FROZEN / "historical_records.json").read_text())
    choices, prequential = rolling_experiment(records, args.output)
    rows = development(records, choices, prequential, args.output)
    oracle_audit(records, args.output)
    print(json.dumps({"development": grouped_metrics(rows)["all"]}), flush=True)
    if args.unblind_new_outcomes:
        validate_new_outcomes(records, choices, prequential, args.output)


if __name__ == "__main__":
    main()
