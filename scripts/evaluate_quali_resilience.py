"""Offline, chronological calibration and evaluation for qualifying resilience.

Read-only inputs: genuine local FastF1 lap/status/race-control caches. Cached
historical runs are completion-time reconstructions, not receipt-exact replays.
Terminal Deleted/IsAccurate/FreshTyre fields never gate earlier pace features.
Final best laps are labels only, explicitly kept outside the live model inputs.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import pickle
import re

import numpy as np
import pandas as pd


LEADS = (900.0, 780.0, 600.0, 420.0, 300.0, 180.0, 120.0, 60.0, 0.0)
DURATIONS = {"Q1": 1080.0, "Q2": 900.0}
RANKS = {"Q1": 16, "Q2": 10}
DELETE = re.compile(r"CAR (\d+) \((\w+)\) TIME (\d+):(\d{2}\.\d{3}) (DELETED|REINSTATED)")


def _seconds(value):
    return None if pd.isna(value) else float(pd.Timedelta(value).total_seconds())


def _read(path):
    with path.open("rb") as handle:
        return pickle.load(handle)["data"]


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _clock_checkpoint(statuses, lower, upper, duration, remaining):
    """Locate a green-clock checkpoint using only Started/Aborted/Finished spans.

    Red-flag pauses consume no session clock. Selecting an evaluation horizon
    from the realized clock is not a feature or an opportunity assumption.
    """
    elapsed = 0.0
    running_from = None
    target_elapsed = duration - remaining
    for time, status in statuses:
        if not lower <= time < upper:
            continue
        if status == "Started":
            if running_from is None:
                running_from = time
                if target_elapsed <= elapsed:
                    return time
        elif status in {"Aborted", "Finished", "Finalised", "Ends", "Inactive"}:
            if running_from is None:
                continue
            span = time - running_from
            if elapsed + span >= target_elapsed - 0.1:
                return min(time, running_from + target_elapsed - elapsed)
            elapsed += span
            running_from = None
    raise ValueError(f"Clock does not reach remaining={remaining} inside part")


def _credible(lap):
    """Completion-time measurements only; no terminal accuracy/freshness label."""
    sectors = lap["sectors_s"]
    return (lap["lap_time_s"] is not None and lap["lap_time_s"] > 0
            and not lap["pit_in"] and not lap["pit_out"]
            and all(value is not None and value > 0 for value in sectors)
            and abs(sum(sectors) - lap["lap_time_s"]) <= .01)


def _deletions_at(laps, notices, checkpoint):
    deleted = set()
    for notice in notices:
        if notice["session_time_s"] > checkpoint:
            continue
        for lap in laps:
            if (lap["driver_number"] == notice["number"]
                    and lap["lap_time_s"] is not None
                    and abs(lap["lap_time_s"] - notice["lap_time_s"]) < .0005
                    and lap["completion_s"] <= notice["session_time_s"] + 1):
                if notice["action"] == "DELETED":
                    deleted.add(lap["id"])
                else:
                    deleted.discard(lap["id"])
    return deleted


def _best(laps, driver, *, time=math.inf, deleted=(), final=False):
    values = [lap["lap_time_s"] for lap in laps
              if lap["driver"] == driver and lap["completion_s"] <= time
              and lap["id"] not in deleted and _credible(lap)
              and (not final or not lap["terminal_deleted"])]
    return min(values, default=None)


def build_records(data_root, output):
    lap_root = data_root / "fastf1/session_laps/schema_v1/year=2026"
    cache_root = data_root / "fastf1/raw_cache/schema_v1/2026"
    event_cache = {}
    for info_path in cache_root.glob("*/2026-*_Qualifying/session_info.ff1pkl"):
        info = _read(info_path)
        event_name = info.get("Meeting", {}).get("Name")
        if event_name:
            # Meeting.Number can retain a pre-calendar-change number (Miami6
            # versus actual event partition4), even colliding with Monaco6.
            # Join on the independently recorded event name within this year.
            assert event_name not in event_cache
            event_cache[event_name] = (info_path.parent, info)
    inventory, records, all_laps = [], [], []
    for lap_path in sorted(lap_root.glob("round=*/session=Q/laps.pkl"),
                           key=lambda path: int(path.parts[-3].split("=")[1])):
        round_number = int(lap_path.parts[-3].split("=")[1])
        frame = pd.read_pickle(lap_path)
        assert set(frame["Round"].dropna().astype(int)) == {round_number}
        event_name = str(frame["EventName"].iloc[0])
        if event_name not in event_cache:
            inventory.append({"round": round_number, "excluded": "No matching raw event cache"})
            continue
        raw_root, info = event_cache[event_name]
        status_raw = _read(raw_root / "session_status_data.ff1pkl")
        statuses = [(_seconds(t), str(status))
                    for t, status in zip(status_raw["Time"], status_raw["Status"])]
        splits = [_seconds(t) for t in _read(raw_root / "_extended_timing_data.ff1pkl")[2]]
        control = _read(raw_root / "race_control_messages.ff1pkl")
        # Source UTC/session bridge: scheduled first green and first Started are
        # independently dated public messages. RC timestamps have1-second
        # precision; retain the bridge spread as a reconstruction limitation.
        green_utc = [pd.Timestamp(t).timestamp() for t, message in zip(control["Time"], control["Message"])
                     if "GREEN LIGHT - PIT EXIT OPEN" in str(message)]
        started = [time for time, status in statuses if status == "Started"]
        if not green_utc or len(green_utc) != len(started):
            inventory.append({"round": round_number, "excluded": "Unmatched green/Started UTC bridge"})
            continue
        offsets = [utc - time for utc, time in zip(green_utc, started)]
        # Use the FIRST already-observed green/Started pair for every causal
        # conversion. Later pairs audit precision only; they cannot revise an
        # earlier deletion timestamp or forecast input retrospectively.
        offset = float(offsets[0])
        spread = max(offsets) - min(offsets)
        if spread > 2:
            inventory.append({"round": round_number, "excluded": "Inconsistent UTC bridge", "spread_s": spread})
            continue
        notices = []
        for utc, message in zip(control["Time"], control["Message"]):
            match = DELETE.search(str(message))
            if match:
                number, driver, minutes, seconds, action = match.groups()
                notices.append({"number": number, "driver": driver,
                                "lap_time_s": int(minutes) * 60 + float(seconds), "action": action,
                                "session_time_s": pd.Timestamp(utc).timestamp() - offset,
                                "message": message})
        drivers_raw = _read(raw_root / "driver_info.ff1pkl")
        roster = sorted({str(row.get("Tla") or row.get("BroadcastName") or number)
                         for number, row in drivers_raw.items()})
        laps = []
        for index, row in frame.reset_index(drop=True).iterrows():
            lap = {"event_id": f"2026-R{round_number}", "id": int(index), "driver": str(row["Driver"]),
                   "driver_number": str(row["DriverNumber"]), "part": str(row["QualifyingPart"]),
                   "completion_s": _seconds(row["Time"]), "start_s": _seconds(row["LapStartTime"]),
                   "lap_time_s": _seconds(row["LapTime"]),
                   "sectors_s": [_seconds(row[f"Sector{n}Time"]) for n in (1, 2, 3)],
                   "pit_in": pd.notna(row["PitInTime"]), "pit_out": pd.notna(row["PitOutTime"]),
                   "compound": str(row["Compound"]), "installation": float(row["Stint"]) if pd.notna(row["Stint"]) else None,
                   "terminal_deleted": bool(row["Deleted"]) if pd.notna(row["Deleted"]) else False}
            laps.append(lap)
        all_laps.extend(laps)
        event_record = {"event_id": f"2026-R{round_number}", "round": round_number,
                        "raw_declared_meeting_number": info["Meeting"].get("Number"),
                        "event_name": info["Meeting"]["Name"], "lap_count": len(laps), "roster": roster,
                        "clock_utc_bridge_spread_s": spread, "parts": {},
                        "sources": {str(path): _hash(path) for path in [lap_path, raw_root / "session_status_data.ff1pkl",
                                   raw_root / "race_control_messages.ff1pkl", raw_root / "_extended_timing_data.ff1pkl"]}}
        q1 = [lap for lap in laps if lap["part"] == "Q1"]
        q2_start = next(time for time, status in statuses if status == "Started" and splits[1] <= time < splits[2])
        q1_at_q2_start = {driver: _best(q1, driver, time=q2_start,
                                      deleted=_deletions_at(q1, notices, q2_start)) for driver in roster}
        q2_roster = sorted(roster, key=lambda driver: (q1_at_q2_start[driver] is None,
                                                     q1_at_q2_start[driver] or math.inf, driver))[:16]
        for part in ("Q1", "Q2"):
            part_laps = [lap for lap in laps if lap["part"] == part]
            part_roster = roster if part == "Q1" else q2_roster
            lower = splits[int(part[-1]) - 1]
            upper = splits[int(part[-1])] if len(splits) > int(part[-1]) else math.inf
            final_best = {driver: _best(part_laps, driver, final=True) for driver in part_roster}
            final_values = sorted(value for value in final_best.values() if value is not None)
            if len(final_values) < RANKS[part]:
                event_record["parts"][part] = {"excluded": "Insufficient final classification times"}
                continue
            unmatched_deleted = [lap["id"] for lap in part_laps if _credible(lap) and lap["terminal_deleted"]
                                 and not any(notice["number"] == lap["driver_number"]
                                             and abs(notice["lap_time_s"] - lap["lap_time_s"]) < .0005
                                             for notice in notices)]
            if unmatched_deleted:
                event_record["parts"][part] = {"excluded": "Credible deleted lap lacks a timed notice", "lap_ids": unmatched_deleted}
                continue
            rank = RANKS[part]
            final_cutoff = final_values[rank - 1]
            event_record["parts"][part] = {"roster_size": len(part_roster), "rank": rank,
                                          "final_cutoff_s_evaluation_only": final_cutoff,
                                          "timed_deletion_notices": len(notices), "lower_s": lower, "upper_s": upper}
            for remaining in LEADS:
                try:
                    clock_time = _clock_checkpoint(statuses, lower, upper, DURATIONS[part], remaining)
                except ValueError as error:
                    event_record["parts"][part].setdefault("clock_exclusions", []).append(str(error))
                    continue
                deleted = _deletions_at(part_laps, notices, clock_time)
                previous_deleted = _deletions_at(q1, notices, clock_time) if part == "Q2" else ()
                drivers = []
                for driver in part_roster:
                    available = [lap for lap in part_laps if lap["driver"] == driver
                                 and lap["completion_s"] <= clock_time and _credible(lap) and lap["id"] not in deleted]
                    # Q1 results are allowed in Q2 only: that entire part has
                    # concluded before these Q2 checkpoints. No Q2 final label
                    # or future Q2 completion enters a DriverPace feature.
                    drivers.append({"driver": driver, "current_best_s": min((lap["lap_time_s"] for lap in available), default=None),
                                    "previous_part_best_s": (_best(q1, driver, time=clock_time, deleted=previous_deleted)
                                                             if part == "Q2" else None),
                                    "attempt_count": len(available),
                                    "last_completion_time_s": max((lap["completion_s"] for lap in available), default=None),
                                    "final_best_s": final_best[driver],
                                    "future_credible_attempt": any(lap["driver"] == driver and _credible(lap)
                                                                  and lap["completion_s"] > clock_time
                                                                  and not lap["terminal_deleted"] for lap in part_laps)})
                current = sorted(d["current_best_s"] for d in drivers if d["current_best_s"] is not None)
                records.append({"event_id": event_record["event_id"], "round": round_number, "part": part,
                                "remaining_s": remaining, "drivable_remaining_s": remaining,
                                "checkpoint_time_s": clock_time, "drivers": drivers,
                                "target_rank": rank, "observed_cutoff_s": current[rank - 1] if len(current) >= rank else None,
                                "final_cutoff_s": final_cutoff, "split": "train" if round_number <= 10 else "holdout",
                                "provenance": "completion-time reconstruction; timed RC deletions; no terminal accuracy/freshness gates"})
        inventory.append(event_record)
    manifest = {"schema_version": 1, "training_rounds": list(range(1, 11)), "holdout_rounds": list(range(11, 16)),
                "lead_times_s": LEADS, "records": len(records), "events": inventory,
                "limitations": ["Cached lap Time is a completion timestamp, not a measured receipt/publication time.",
                                "Terminal Deleted flags are evaluation labels; early features use only already-published timed RC notices.",
                                "LapStartDate is unavailable in these caches; first/continued GREEN and Started messages provide a UTC/session bridge.",
                                "Final Q1 classification is available to Q2; no final Q2 labels enter forecast inputs.",
                                "FreshTyre, IsAccurate and retroactively selected session references are not early forecast features."]}
    output.mkdir(parents=True, exist_ok=True)
    (output / "historical_inventory.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "historical_records.json").write_text(json.dumps(records, indent=2) + "\n")
    (output / "historical_laps.json").write_text(json.dumps(all_laps, indent=2) + "\n")
    print(json.dumps({"events": len(inventory), "records": len(records), "train": sum(r["split"] == "train" for r in records),
                      "holdout": sum(r["split"] == "holdout" for r in records), "output": str(output)}), flush=True)
    return records


def _metrics(rows, value, lower=None, upper=None):
    available = [row for row in rows if row.get(value) is not None]
    errors = [row[value] - row["final_cutoff_s"] for row in available]
    return {"checkpoints": len(rows), "available": len(available),
            "availability": len(available) / len(rows) if rows else None,
            "mae_s": float(np.mean(np.abs(errors))) if errors else None,
            "rmse_s": float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
            "bias_s": float(np.mean(errors)) if errors else None,
            "interval_coverage": (float(np.mean([row[lower] <= row["final_cutoff_s"] <= row[upper]
                                                for row in available])) if lower and available else None),
            "mean_interval_width_s": (float(np.mean([row[upper] - row[lower] for row in available]))
                                      if lower and available else None)}


def evaluate_records(records, output):
    from dataclasses import asdict
    from wostrategy.analysis.quali_forecast import DriverPace, ForecastPrior, forecast_final_cutoff

    training = [record for record in records if record["split"] == "train"]
    holdout = [record for record in records if record["split"] == "holdout"]
    assert {record["event_id"] for record in training}.isdisjoint(record["event_id"] for record in holdout)
    prior = ForecastPrior.from_records(
        training, revision="empirical-r1-r10-v1-frozen-20261025", interval_coverage=.8,
        metadata={"training_rounds": list(range(1, 11)), "holdout_rounds": list(range(11, 16)),
                  "data_protocol": "completion-time reconstruction; causal timed RC deletions; green clock; final targets labels only",
                  "tuning": "No holdout tuning; empirical whole-field scenario V1",
                  "training_record_count": len(training),
                  "training_records_sha256": sha256(json.dumps(training, sort_keys=True).encode()).hexdigest(),
                  "training_source_hashes": {event["event_id"]: event["sources"]
                                             for event in json.loads((output / "historical_inventory.json").read_text())["events"]
                                             if event.get("round", 99) <= 10 and "sources" in event},
                  "release_status": "EXPERIMENTAL; small chronological holdout, not validated prospective calibration",
                  "validation_status": "experimental; empirical coverage unvalidated prospectively"},
    )
    prior_dict = prior.to_dict()
    (output / "forecast_prior_r1_r10.json").write_text(json.dumps(prior_dict, indent=2) + "\n")
    predictions = []
    for record in holdout:
        # This explicit allowlist is the train/label firewall: final_best_s and
        # future_credible_attempt are training/evaluation labels, never features.
        drivers = tuple(DriverPace(driver=row["driver"], current_best_s=row["current_best_s"],
                                   previous_part_best_s=row["previous_part_best_s"])
                        for row in record["drivers"])
        forecast = forecast_final_cutoff(drivers, event_id=record["event_id"], part=record["part"],
                                         remaining_s=record["remaining_s"], prior=prior,
                                         target_rank=record["target_rank"], part_finalised=False)
        predictions.append({"event_id": record["event_id"], "part": record["part"], "remaining_s": record["remaining_s"],
                            "final_cutoff_s": record["final_cutoff_s"], "observed_cutoff_s": record["observed_cutoff_s"],
                            "existing_no_fp_projection_s": record["observed_cutoff_s"],
                            "forecast_s": forecast.estimate_s, "forecast_lower_s": forecast.lower_s,
                            "forecast_upper_s": forecast.upper_s, "forecast": asdict(forecast)})
    metrics = {}
    groups = {"all": predictions,
              **{part: [row for row in predictions if row["part"] == part] for part in DURATIONS},
              **{f"lead_{lead:g}": [row for row in predictions if row["remaining_s"] == lead] for lead in LEADS},
              **{f"event_{event}": [row for row in predictions if row["event_id"] == event]
                 for event in {row["event_id"] for row in predictions}},
              **{f"source_{source}": [row for row in predictions if row["forecast"]["source"] == source]
                 for source in {row["forecast"]["source"] for row in predictions}}}
    for name, rows in groups.items():
        paired = [row for row in rows if row["observed_cutoff_s"] is not None and row["forecast_s"] is not None]
        metrics[name] = {"observed": _metrics(rows, "observed_cutoff_s"),
                         "existing_no_fp": _metrics(rows, "existing_no_fp_projection_s"),
                         "new": _metrics(rows, "forecast_s", "forecast_lower_s", "forecast_upper_s"),
                         "paired_observed": _metrics(paired, "observed_cutoff_s"),
                         "paired_new": _metrics(paired, "forecast_s", "forecast_lower_s", "forecast_upper_s")}
        deltas = [abs(row["forecast_s"] - row["final_cutoff_s"])
                  - abs(row["observed_cutoff_s"] - row["final_cutoff_s"]) for row in paired]
        metrics[name]["paired_outcomes"] = {"improved": sum(value < -1e-10 for value in deltas),
                                            "worsened": sum(value > 1e-10 for value in deltas),
                                            "matched": sum(abs(value) <= 1e-10 for value in deltas)}
    (output / "holdout_predictions.json").write_text(json.dumps(predictions, indent=2) + "\n")
    (output / "holdout_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    first_predictions = {}
    for event in sorted({row["event_id"] for row in predictions}):
        for part in DURATIONS:
            rows = sorted((row for row in predictions if row["event_id"] == event and row["part"] == part),
                          key=lambda row: row["remaining_s"], reverse=True)
            first_predictions[f"{event}/{part}"] = {
                "first_numeric_forecast_lead_s": next((row["remaining_s"] for row in rows if row["forecast_s"] is not None), None),
                "first_observed_cutoff_lead_s": next((row["remaining_s"] for row in rows if row["observed_cutoff_s"] is not None), None),
                "precision": "Earliest evaluated grid horizon; not claimed first actual packet/publication timestamp"}
    (output / "holdout_first_prediction.json").write_text(json.dumps(first_predictions, indent=2) + "\n")
    print(json.dumps({"prior_templates": len(prior_dict.get("templates", ())), "holdout": len(predictions),
                      "metrics": metrics["all"]}), flush=True)
    return prior


def collect_r18(data_root, output, round_number=18):
    """Full original recording, real reducer, lightweight causal checkpoints.

    No physical traffic service is stubbed. This path deliberately evaluates
    raw reducer publications only; the normal service/UI path is validated
    separately by the coordinator. Native ReplayPlayer retains recorded receipt
    provenance and the production archive source/clock contract.
    """
    from dataclasses import asdict
    from types import SimpleNamespace
    from wodata.live import QualiSessionStore
    from wodata.live.replay import ReplayPlayer
    from woplanner.quali.q_only_track import QOnlyTrackEvolution

    recording = next((data_root / f"wodata/live_timing/schema_v1/year=2026/round={round_number}/session=Q").glob("live_timing_*.jsonl"))
    store = QualiSessionStore()
    q_only = QOnlyTrackEvolution()
    checkpoints, candidates = [], []
    previous_part = "Q1"
    previous_drivers = ()
    active = set()
    previous_part_bests = {}
    seen_keys = set()
    prior_bin = None
    prior_observed = None
    final_targets = {}
    first_q_only_fit = None
    required = {520, 1446, 2285, 4350, 4568, 4644, 6069, 6555, 6623, 6825, 6975, 7018, 7063, 7358}
    if round_number == 14:
        legacy_report = json.loads((Path(__file__).resolve().parents[1]
                                    / "outputs/quali_half_live_release_20261019/merged-r14/report.json").read_text())
        required = {row["message"] for row in legacy_report["update_events"]}
        required.update(row["messages"] for row in legacy_report["captures"])

    def ingest(message):
        nonlocal previous_part, previous_drivers, active, previous_part_bests, prior_bin, prior_observed, first_q_only_fit
        raw = store.apply(message)
        q_only.begin_publication(raw)
        part = raw.session_part
        if part != previous_part and previous_drivers:
            prior_active = [driver for driver in previous_drivers if driver.driver_number in active]
            official = sorted(prior_active, key=lambda driver: (driver.position is None, driver.position or 999, driver.driver_code))
            rank = RANKS.get(previous_part)
            valid = sorted(driver.best_lap_s for driver in prior_active if driver.best_lap_s is not None)
            if rank and len(valid) >= rank:
                final_targets[previous_part] = {"value_s": valid[rank - 1], "source": "last current valid reducer bests before part transition"}
            active = {driver.driver_number for driver in official[:max(0, len(official) - 6)]}
            previous_part_bests = {driver.driver_number: driver.best_lap_s for driver in previous_drivers}
            prior_bin = None
        if part in {"Q1", "SQ1"}:
            active.update(driver.driver_number for driver in raw.drivers)
        previous_part = part
        for event in raw.completed_lap_sequence:
            if event.event_key in seen_keys:
                continue
            seen_keys.add(event.event_key)
            driver = next(driver for driver in raw.drivers if driver.driver_number == event.driver_number)
            field = min((driver.best_lap_s for driver in raw.drivers
                         if driver.best_lap_s is not None and driver.driver_number in active), default=None)
            sectors = event.sectors_s
            reason = None
            if event.classification != "normal_timed":
                reason = "UNIDENTIFIED_VALID_COMPLETION"
            elif not driver.timed_lap_eligible or driver.in_pit or driver.out_lap_confirmed:
                reason = "PHYSICAL_LAP_INELIGIBLE"
            elif not all(value is not None and value > 0 for value in sectors):
                reason = "OWNED_SECTORS_INCOMPLETE"
            elif abs(sum(sectors) - event.lap_time_s) > .01:
                reason = "SECTOR_TOTAL_MISMATCH"
            elif field is None:
                reason = "FIELD_REFERENCE_UNAVAILABLE"
            elif event.lap_time_s > field * 1.07:
                reason = "GROSS_SANITY_REJECTED"
            tyre = driver.tyre_history[-1] if driver.tyre_history else None
            candidates.append({"sequence": message.sequence, "receipt_utc": message.received_at_utc.isoformat(),
                               "source_utc": message.source_timestamp_utc.isoformat() if message.source_timestamp_utc else None,
                               "event": asdict(event), "event_key": event.event_key,
                               "driver": driver.driver_code, "remaining_s": raw.remaining_time_s,
                               "field_reference_s": field, "q_only_accept": reason is None, "rejection_reason": reason,
                               "timed_lap_eligible": driver.timed_lap_eligible, "in_pit": driver.in_pit,
                               "tyre": asdict(tyre) if tyre else None})
        q_only.consume_new_completions(raw, active)
        q_only.finish_publication(SimpleNamespace(
            controller=SimpleNamespace(store=store), raw_snapshot=raw,
            _states={driver.driver_number: SimpleNamespace(active=driver.driver_number in active) for driver in raw.drivers},
            _fp_track_rate_s_per_field_push=0.0, track_override_s_per_field_push=None,
        ))
        new_first_fit = first_q_only_fit is None and q_only.diagnostic.aggregate_slope_s_per_push is not None
        if new_first_fit:
            first_q_only_fit = {"sequence": message.sequence, "part": part,
                                "receipt_utc": message.received_at_utc.isoformat(),
                                "remaining_s": raw.remaining_time_s,
                                "slope_s_per_push": q_only.diagnostic.aggregate_slope_s_per_push}
        if part not in DURATIONS or raw.remaining_time_s is None:
            previous_drivers = raw.drivers
            return
        drivers = []
        for driver in raw.drivers:
            if driver.driver_number not in active:
                continue
            history = dict(driver.historical_results)
            previous = history.get("Q1", previous_part_bests.get(driver.driver_number)) if part == "Q2" else None
            drivers.append({"driver": driver.driver_code, "driver_number": driver.driver_number,
                            "current_best_s": driver.best_lap_s, "previous_part_best_s": previous,
                            "in_pit": driver.in_pit, "completed_laps": driver.completed_laps,
                            "knocked_out": driver.knocked_out})
        values = sorted(driver["current_best_s"] for driver in drivers if driver["current_best_s"] is not None)
        rank = RANKS[part]
        observed = values[rank - 1] if len(values) >= rank else None
        bucket = (part, int(raw.remaining_time_s // 10))
        if bucket != prior_bin or message.sequence in required or observed != prior_observed or new_first_fit:
            checkpoints.append({"sequence": message.sequence, "part": part, "remaining_s": raw.remaining_time_s,
                                "receipt_utc": message.received_at_utc.isoformat(), "drivers": drivers,
                                "observed_cutoff_s": observed, "normal_finished": raw.normal_finished,
                                "target_rank": rank, "event_id": f"2026-R{round_number}",
                                "q_only": q_only.payload(detailed=False)})
        prior_bin, prior_observed = bucket, observed
        previous_drivers = raw.drivers

    result = ReplayPlayer(recording, speed=0).play(ingest)
    manifest = {"recording": str(recording), "sha256": _hash(recording), "result": asdict(result),
                "checkpoint_count": len(checkpoints), "candidate_count": len(candidates),
                "q_only_accepted_at_arrival": sum(row["q_only_accept"] for row in candidates),
                "first_q_only_live_fit": first_q_only_fit,
                "q_only_final": q_only.payload(detailed=False),
                "q_only_revoked_keys": list(q_only._policy.revoked),
                "q_only_live_samples": [asdict(sample) for key, sample in q_only._policy.samples.items()
                                        if key not in q_only._policy.revoked],
                "final_targets_evaluation_only": final_targets,
                "limitation": "Reducer-only chronological collection. Q-only criteria independently mirror the coordinator's reviewed admission contract; normal full service/UI validation is separate."}
    (output / f"r{round_number}_reducer_checkpoints.json").write_text(json.dumps(checkpoints, indent=2) + "\n")
    (output / f"r{round_number}_candidate_arrivals.json").write_text(json.dumps(candidates, indent=2) + "\n")
    (output / f"r{round_number}_reducer_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest), flush=True)


def analyse_collected(output, round_number=18):
    from dataclasses import asdict, replace
    from wostrategy.analysis.quali_forecast import DriverPace, ForecastPrior, forecast_final_cutoff
    from woplanner.quali.track_evolution import TrackEvolutionSample, build_track_evolution_diagnostic

    prior = ForecastPrior.from_dict(json.loads((output / "forecast_prior_r1_r10.json").read_text()))
    checkpoints = json.loads((output / f"r{round_number}_reducer_checkpoints.json").read_text())
    candidates = json.loads((output / f"r{round_number}_candidate_arrivals.json").read_text())
    manifest = json.loads((output / f"r{round_number}_reducer_manifest.json").read_text())
    predictions = []
    legacy_events = {}
    if round_number == 14:
        legacy_path = Path(__file__).resolve().parents[1] / "outputs/quali_half_live_release_20261019/merged-r14/report.json"
        legacy = json.loads(legacy_path.read_text())
        assert legacy["recording_sha256"] == manifest["sha256"]
        legacy_events = {row["message"]: row for row in legacy["update_events"] if row["part"] in DURATIONS}
    for record in checkpoints:
        drivers = tuple(DriverPace(driver=row["driver"], current_best_s=row["current_best_s"],
                                   previous_part_best_s=row["previous_part_best_s"])
                        for row in record["drivers"])
        forecast = forecast_final_cutoff(drivers, event_id=record["event_id"], part=record["part"],
                                         remaining_s=record["remaining_s"], prior=prior,
                                         target_rank=record["target_rank"], part_finalised=False)
        target = manifest["final_targets_evaluation_only"][record["part"]]["value_s"]
        legacy = legacy_events.get(record["sequence"])
        # Genuine-FP legacy values are from the prior saved native application
        # replay, not re-estimated here or copied from a cached future result.
        if legacy:
            assert legacy["part"] == record["part"]
            assert abs(legacy["observed"] - record["observed_cutoff_s"]) < 1e-9
        predictions.append({**{key: record[key] for key in ("sequence", "part", "remaining_s", "receipt_utc")},
                            "final_cutoff_s": target, "observed_cutoff_s": record["observed_cutoff_s"],
                            "existing_no_fp_projection_s": record["observed_cutoff_s"],
                            "existing_genuine_fp_projection_s": legacy["current"] if legacy else None,
                            "existing_genuine_fp_rate_s_per_push": legacy["rate"] if legacy else None,
                            "forecast_s": forecast.estimate_s, "forecast_lower_s": forecast.lower_s,
                            "forecast_upper_s": forecast.upper_s, "forecast": asdict(forecast),
                            "q_only": record.get("q_only")})
    metrics = {}
    for part in DURATIONS:
        rows = [row for row in predictions if row["part"] == part]
        duration = max(row["remaining_s"] for row in rows)
        weighted = {"observed": 0.0, "new": 0.0}
        for first, second in zip(rows, rows[1:]):
            weight = max(0.0, first["remaining_s"] - second["remaining_s"])
            weighted["observed"] += weight * (first["observed_cutoff_s"] is not None)
            weighted["new"] += weight * (first["forecast_s"] is not None)
        available = [row for row in rows if row["forecast_s"] is not None]
        metrics[part] = {"observed": _metrics(rows, "observed_cutoff_s"),
                         "new": _metrics(rows, "forecast_s", "forecast_lower_s", "forecast_upper_s"),
                         "time_covered_s": duration,
                         "time_fraction_observed": weighted["observed"] / duration,
                         "time_fraction_new": weighted["new"] / duration,
                         "first_prediction": {key: available[0][key] for key in ("sequence", "receipt_utc", "remaining_s")} if available else None,
                         "pre_observed_target_predictions": sum(row["forecast_s"] is not None and row["observed_cutoff_s"] is None for row in rows)}
    legacy_paired = [row for row in predictions if row["existing_genuine_fp_projection_s"] is not None and row["forecast_s"] is not None]
    if legacy_paired:
        metrics["genuine_fp_legacy_paired"] = {
            "observed": _metrics(legacy_paired, "observed_cutoff_s"),
            "existing_genuine_fp": _metrics(legacy_paired, "existing_genuine_fp_projection_s"),
            "new": _metrics(legacy_paired, "forecast_s", "forecast_lower_s", "forecast_upper_s"),
            "source": "Saved native application R14 replay with genuine FP artifacts; SHA verified; no additional independent evaluation event"}
    accepted = [row for row in candidates if row["q_only_accept"]]
    if "q_only_live_samples" in manifest:
        # Preserve exact production indices and already-published revocations;
        # never renumber the coordinate after deleting a lap.
        samples = [TrackEvolutionSample(**sample) for sample in manifest["q_only_live_samples"]]
    else:
        samples = []
        for index, row in enumerate(accepted, 1):
            event, tyre = row["event"], row["tyre"] or {}
            samples.append(TrackEvolutionSample(event["session_part"], row["driver"], "recorded field", event["lap_number"],
                                               event["lap_time_s"], index, tyre.get("compound"), tyre.get("start_laps"),
                                               tyre.get("fresh"), None, tyre_install_index=tyre.get("stint_index")))
    def fit_summary(values):
        d = build_track_evolution_diagnostic(tuple(values), clean_air_gap_s=3.0, require_traffic=False)
        return {"accepted_pace_observations": len(values), "comparable_installation_observations": len(d.observations),
                "fitted_observations": d.lap_count, "fitted_drivers": d.driver_count,
                "live_slope_s_per_push": d.aggregate_slope_s_per_push}
    first = {}
    for sample in samples:
        first.setdefault(sample.driver, sample)
    ablations = {
        "actual_no_fp_legacy": {"candidate_laps": len(candidates), "accepted_pace_observations": 0,
                                "fitted_observations": 0, "reason": "Established original replay: no operational pace reference; NOT_OPERATIONAL_FAST"},
        "genuine_r18_fp": {"status": "UNAVAILABLE", "reason": "The intended FastF1/full FP preparation control is unavailable: F1 timing returned401 and the mirror404. OpenF1 sessions11389/11390/11391 supply genuine partial FP1–3 lap/sector data, but validated pit/in-lap/run-clock mapping into the full preparation is absent; no equivalent full-input ablation or substitute FP artifact was fabricated."},
        "q_only_no_fp": fit_summary(samples),
        "q_only_lifetime": {"accepted_at_arrival": len(accepted),
                             "sector_enrichment_admissions": manifest.get("q_only_final", {}).get("sector_enrichment_admissions", 0),
                             "pending_sector_completions": manifest.get("q_only_final", {}).get("pending_sector_completions"),
                             "admitted_over_time": manifest.get("q_only_final", {}).get("attrition", {}).get("quality_eligible_candidates", len(accepted)),
                             "invalidated_completion_keys": len(manifest.get("q_only_revoked_keys", ())),
                             "withdrawn_accepted_observations": manifest.get("q_only_final", {}).get("attrition", {}).get("explicitly_revoked_pace_candidates", 0),
                             "withdrawn_at_arrival_observations": sum(tuple(row["event_key"]) in {
                                 tuple(key) for key in manifest.get("q_only_revoked_keys", ())} for row in accepted),
                             "production_active_ledger": manifest.get("q_only_final", {}).get("accepted_observation_count")},
        "q_pace_no_cool_duration": {**fit_summary(samples), "forecast_difference_s": 0.0,
                                    "reason": "Neither Q-only admission nor field prior model consumes COOL; operational Effective Finish remains separate"},
        "one_observation_per_driver": fit_summary(first.values()),
        "unknown_tyre_freshness": fit_summary([replace(sample, fresh=None) for sample in samples]),
        "auxiliary_traffic_absent": {**fit_summary(samples), "reason": "Physical traffic is auxiliary; actual source snapshots omitted from fit inputs"},
        "no_live_track_fit_driver_pace_present": {"forecast_difference_s": 0.0, "reason": "Field model API has no fitted Track Evolution input; residual vectors already include typical shared evolution"},
        "before_observed_cutoff": {part: metrics[part]["pre_observed_target_predictions"] for part in DURATIONS},
    }
    (output / f"r{round_number}_predictions.json").write_text(json.dumps(predictions, indent=2) + "\n")
    (output / f"r{round_number}_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (output / f"r{round_number}_ablations.json").write_text(json.dumps(ablations, indent=2) + "\n")
    print(json.dumps({"round": round_number, "metrics": metrics, "ablations": ablations}), flush=True)


def check_genuine_fp_recovery(data_root, output):
    """Normal service/controller prefix, genuinely matching R14 FP artifacts."""
    from dataclasses import asdict
    import tempfile
    from types import SimpleNamespace
    from wodata.artifacts import read_pre_quali_analysis, pre_quali_root
    from wodata.live import LiveTimingController, QualiSessionStore
    from wodata.live.replay import ReplayPlayer
    from woplanner.quali.runtime_paths import RuntimePaths
    from woplanner.quali.service import QualiLiveService

    recording = next((data_root / "wodata/live_timing/schema_v1/year=2026/round=14/session=Q").glob("live_timing_*.jsonl"))
    laps, durations, _ = read_pre_quali_analysis(year=2026, round_number=14, data_root=data_root)
    artifact_root = pre_quali_root(2026, 14, data_root)
    evidence = {"event_id": "2026-R14", "recording_sha256": _hash(recording),
                "artifact_root": str(artifact_root), "artifact_hashes": {
                    path.name: _hash(path) for path in artifact_root.iterdir() if path.is_file()},
                "mode": "Original native ReplayPlayer/controller/service; genuine matching R14 FP loaded after sequence3000; prefix bounded at4000"}
    with tempfile.TemporaryDirectory(prefix="r18-fp-recovery-") as temporary:
        root = Path(temporary).resolve()
        (root / "resource").mkdir()
        (root / "output").mkdir()
        controller = LiveTimingController(store=QualiSessionStore())
        service = QualiLiveService(controller, runtime_paths=RuntimePaths(root / "resource", root / "output", isolated=True))

        def ingest(message):
            controller._enqueue_message(message)
            controller.drain(max_messages=1)
            if message.sequence == 3000:
                policy = service.q_only_track_evolution._policy
                keys = set(policy.samples)
                before = service.q_only_track_evolution.payload()
                forecast = service.snapshot.final_cutoff_forecast
                evidence["before"] = {"q_only": before, "cool_s": service.cool_lap_s,
                                      "pace_reference": str(service._common_lap_state_reference),
                                      "forecast": forecast}
                assert keys and service._common_lap_state_reference is None
                service.load_operational_analysis(SimpleNamespace(laps=laps, durations=durations))
                service.snapshot = service._build_snapshot()
                evidence["after"] = {"q_only": service.q_only_track_evolution.payload(),
                                     "cool_s": service.cool_lap_s,
                                     "pace_reference": str(service._common_lap_state_reference),
                                     "forecast": service.snapshot.final_cutoff_forecast}
                assert service._common_lap_state_reference is not None
                assert service.cool_lap_s is not None
                assert set(policy.samples) == keys
                assert evidence["after"]["q_only"] == before
                assert evidence["after"]["forecast"] == forecast
                # Re-presenting this exact reducer state must not admit it again.
                service.consume(service.raw_snapshot)
                assert set(policy.samples) == keys
                assert len(policy.samples) == len(keys)
                evidence["checks"] = {"genuine_fp_loaded": True, "observations_preserved": True,
                                      "no_double_count": True, "field_forecast_preserved": True}
            if message.sequence == 4000:
                evidence["after_further_messages"] = {"q_only": service.q_only_track_evolution.payload(),
                                                      "legacy_track": service.track_evolution_payload(),
                                                      "part": service.session_part, "remaining_s": service.snapshot.remaining_time_s}
                raise StopIteration("intentional bounded genuine-FP recovery check")
        try:
            ReplayPlayer(recording, speed=0).play(ingest)
        except Exception as error:
            if not isinstance(error.__cause__, StopIteration):
                raise
        evidence["status"] = "passed"
    (output / "r14_genuine_fp_recovery.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"genuine_fp_recovery": evidence["status"], "checks": evidence.get("checks")}), flush=True)


def evaluate_partial_bounds(output):
    """Separate mathematical scenario envelopes from frozen point-model scores."""
    from dataclasses import asdict
    from wostrategy.analysis.quali_forecast import DriverPace, ForecastPrior, forecast_partial_field_bounds

    prior = ForecastPrior.from_dict(json.loads((output / "forecast_prior_r1_r10.json").read_text()))
    holdout = [record for record in json.loads((output / "historical_records.json").read_text()) if record["split"] == "holdout"]
    point = {(row["event_id"], row["part"], row["remaining_s"]): row
             for row in json.loads((output / "holdout_predictions.json").read_text())}
    outputs = {}
    datasets = {"historical_holdout": holdout}
    for rd in (18, 14):
        path = output / f"r{rd}_reducer_checkpoints.json"
        if path.exists():
            manifest = json.loads((output / f"r{rd}_reducer_manifest.json").read_text())
            datasets[f"r{rd}_packets"] = [{**row, "final_cutoff_s": manifest["final_targets_evaluation_only"][row["part"]]["value_s"]}
                                            for row in json.loads(path.read_text())]
    for name, records in datasets.items():
        rows = []
        for record in records:
            if name == "historical_holdout":
                if point[record["event_id"], record["part"], record["remaining_s"]]["forecast_s"] is not None:
                    continue
            else:
                prediction = next(row for row in json.loads((output / f"{name.split('_')[0]}_predictions.json").read_text())
                                  if row["sequence"] == record["sequence"])
                if prediction["forecast_s"] is not None:
                    continue
            drivers = tuple(DriverPace(row["driver"], row["current_best_s"], row["previous_part_best_s"])
                            for row in record["drivers"])
            result = forecast_partial_field_bounds(drivers, event_id=record["event_id"], part=record["part"],
                                                   remaining_s=record["remaining_s"], prior=prior,
                                                   target_rank=record["target_rank"])
            rows.append({"event_id": record["event_id"], "part": record["part"], "remaining_s": record["remaining_s"],
                         "sequence": record.get("sequence"), "final_cutoff_s": record["final_cutoff_s"],
                         "result": asdict(result)})
        available = [row for row in rows if row["result"]["lower_s"] is not None and row["result"]["upper_s"] is not None]
        outputs[name] = {"point_unavailable_checkpoints": len(rows), "finite_scenario_ranges": len(available),
                         "empirical_target_containment": (sum(row["result"]["lower_s"] <= row["final_cutoff_s"] <= row["result"]["upper_s"]
                                                               for row in available) / len(available)) if available else None,
                         "mean_width_s": float(np.mean([row["result"]["upper_s"] - row["result"]["lower_s"] for row in available])) if available else None,
                         "interpretation": "Conditional envelope over historical scenarios and all unknown-driver rank placements; no point estimate, probabilities, or nominal coverage claim.",
                         "rows": rows}
    (output / "partial_scenario_evaluation.json").write_text(json.dumps(outputs, indent=2) + "\n")
    print(json.dumps({"partial_scenarios": {key: {k: v for k, v in value.items() if k != "rows"}
                                            for key, value in outputs.items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(__file__).resolve().parents[2] / "woData")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "outputs/r18_prediction_resilience_20261025")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate the frozen training-only empirical prior")
    parser.add_argument("--r18", action="store_true", help="Collect genuine R18 reducer-only checkpoints and arrival eligibility")
    parser.add_argument("--round14-legacy", action="store_true", help="Collect actual R14 packet states for genuine-FP legacy projection comparison")
    parser.add_argument("--analyse-collected", action="store_true", help="Evaluate the frozen model on already collected R18 and optional R14 packets")
    parser.add_argument("--genuine-fp-recovery", action="store_true", help="Exercise genuine R14 FP recovery in a normal controller/service prefix")
    parser.add_argument("--partial-bounds", action="store_true", help="Evaluate non-probabilistic partial-field scenario ranges separately")
    args = parser.parse_args()
    records = build_records(args.data_root, args.output)
    if args.evaluate:
        evaluate_records(records, args.output)
    if args.r18:
        collect_r18(args.data_root, args.output)
    if args.round14_legacy:
        collect_r18(args.data_root, args.output, round_number=14)
    if args.analyse_collected:
        analyse_collected(args.output)
        if (args.output / "r14_reducer_checkpoints.json").exists():
            analyse_collected(args.output, round_number=14)
    if args.genuine_fp_recovery:
        check_genuine_fp_recovery(args.data_root, args.output)
    if args.partial_bounds:
        evaluate_partial_bounds(args.output)


if __name__ == "__main__":
    main()
