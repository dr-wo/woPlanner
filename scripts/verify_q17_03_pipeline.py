"""Cross-round production replay regression, retaining Phase 0 unchanged.

Run from woPlanner with PYTHONPATH=../woData/src:../woStrategy/src:src
../.venv/bin/python scripts/verify_q17_03_pipeline.py --mode all

Direct mode retains original live sources and receipts. Player mode exercises
ReplayPlayer(speed=0), including its explicit preserved-receipt trust flag.
Archive scheduler coordinates are never reported as measured live latency.
The same runner supports an independently frozen legacy runtime via PYTHONPATH
and --label before. No per-message baseline equality is asserted unless an
ordinary-reference report is supplied.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import json
import math
from pathlib import Path
import time
import traceback
from types import SimpleNamespace

from wodata.live import LiveTimingMessage
from wodata.live.replay.player import ReplayPlayer, load_replay
from wodata.live.replay.schema import parse_timestamp
from woplanner.quali.service import QualiLiveService
from woplanner.quali.physical_traffic import PhysicalLapTraffic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "woData"
PHASE0 = ROOT / "temp/q17_03_phase0_baseline"
DEFAULT_OUT = ROOT / "temp/q17_03_implementation"
INPUTS = {
    "R12_archive": DATA / "wodata/live_archive/schema_v1/year=2026/round=12/session=Q/replay/merged_replay_v1.jsonl",
    "R13_archive": DATA / "wodata/live_archive/schema_v1/year=2026/round=13/session=Q/replay/merged_replay_v1.jsonl",
    "R14_live": DATA / "wodata/live_timing/schema_v1/year=2026/round=14/session=Q/live_timing_133f3aa055664ba3b123b4c6fb3e402f.jsonl",
    "R16_live_copy": ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl",
    "R17_live": DATA / "wodata/live_timing/schema_v1/year=2026/round=17/session=Q/live_timing_24c397c4a58944febd83dac4664d8ac9.jsonl",
}
MODULES = (
    "woplanner.quali.service", "woplanner.quali.physical_traffic",
    "woplanner.quali.track_evolution", "woplanner.quali.lap_state",
    "woplanner.quali.planning", "woplanner.quali.models", "woplanner.common.session_info",
    "wodata.live.quali_reducer", "wodata.live.quali_models",
    "wodata.live.quali_clock", "wodata.live.reducer",
    "wodata.live.models", "wodata.live.controller", "wodata.live.fastf1_source",
    "wodata.live.replay.player", "wodata.live.replay.schema",
    "wostrategy.analysis.traffic", "wostrategy.analysis.pre_quali",
)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def source_hashes():
    runner = Path(__file__).resolve()
    sources = {"validation_runner": {"path": str(runner), "sha256": sha256(runner.read_bytes()).hexdigest()}}
    for name in (*MODULES, "woplanner.quali.traffic_finalisation"):
        try:
            path = Path(importlib.import_module(name).__file__).resolve()
        except ModuleNotFoundError:
            continue  # Independently frozen pre-implementation runtime.
        sources[name] = {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
    return sources


PROCESS_SOURCE_HASHES = source_hashes()


def distribution(values):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not values:
        return {"count": 0, "p50_s": None, "p95_s": None, "p99_s": None, "max_s": None}
    def percentile(p):
        index = (len(values) - 1) * p
        lower = int(index)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (index - lower)
    return {"count": len(values), "p50_s": percentile(.5), "p95_s": percentile(.95),
            "p99_s": percentile(.99), "max_s": values[-1]}


class ObservedDeque(deque):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer
    def append(self, row):
        result = super().append(row)
        self.observer(row)
        return result
    def insert(self, index, row):
        result = super().insert(index, row)
        self.observer(row)
        return result


class ObservedRows(dict):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer
    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = ObservedDeque(self.observer)
        return self[key]


PACE_FIELDS = ("part", "driver", "lap_number", "lap_time_s", "global_push_index",
               "compound", "stint_start_laps", "fresh", "tyre_install_index")


def pace_identity(sample):
    value = asdict(sample) if not isinstance(sample, dict) else sample
    return tuple(value.get(key) for key in PACE_FIELDS)


def ordinary_state(snapshot):
    driver_fields = (
        "driver_number", "driver", "rank", "official_position", "status", "lap_state",
        "sectors_s", "last_lap_s", "best_lap_s", "gap_s", "interval_s", "finished",
        "finished_reason", "pit_state", "pit_lane_progress", "active", "frozen_part",
        "historical_results", "effective_finished", "effective_finish_remaining_s",
        "effective_finish_recovery_s", "effective_finish_timing_source",
    )
    return {
        "part": snapshot.session_part, "remaining_time_s": snapshot.remaining_time_s,
        "session_status": snapshot.session_status, "normal_finished": snapshot.normal_finished,
        "normal_finish_source": snapshot.normal_finish_source,
        "clock_running": snapshot.clock_running,
        "drivers": [{key: getattr(driver, key) for key in driver_fields} for driver in snapshot.drivers],
        "current_cutoff": {key: getattr(snapshot.cutoff, key) for key in
            ("target_kind", "cutoff_rank", "driver", "current_cutoff_s", "unfinished_driver_count")},
        "actual_cutoffs": snapshot.actual_cutoffs,
    }


def verify(name, *, mode, output_dir, label, ordinary_reference_dir=None):
    path = INPUTS[name]
    phase0 = json.loads((PHASE0 / f"{name}.json").read_text())
    input_sha = sha256(path.read_bytes()).hexdigest()
    if input_sha != phase0["input_sha256"]:
        raise ValueError(f"Input changed since Phase 0: {name}")
    # Imports are cached for the process lifetime: later on-disk edits must
    # invalidate every subsequent result too, not masquerade as a new runtime.
    source_start = PROCESS_SOURCE_HASHES
    service = QualiLiveService()
    service._geometry_data_root = DATA
    # A service-only shadow consumes the same immutable reducer snapshots.
    # It needs no second reducer/controller replay, and only traffic evidence
    # ingestion/evaluation is disabled. Operational FP data remains identical.
    control = QualiLiveService(controller=SimpleNamespace(store=None))
    control._geometry_data_root = DATA
    control_adapter = control._physical_traffic
    control_adapter.ingest = lambda raw: None
    control_adapter.finalize = lambda *args, **kwargs: PhysicalLapTraffic()
    control_adapter.completion_bounds = lambda *args, **kwargs: None
    control_adapter.completed_lap_for_sample = lambda *args, **kwargs: None
    adapter = service._physical_traffic
    has_history = hasattr(service.controller.store.snapshot(), "speed_history")
    accepted, observations, matches = set(), set(), set()
    accepted_parts, matched_parts, rejected = Counter(), Counter(), Counter()
    topics, original_sources, clocks, trust_flags = Counter(), Counter(), Counter(), Counter()
    finalize_calls, admissions, transitions, checkpoints = [], [], [], []
    publication_records = []
    ordinary_trace, candidate_publications, warning_changes = [], [], []
    unique_fit_states, eligible_states = {}, {}
    current_sequence = 0
    trigger_clock_s = None
    trigger_clock_kind = None
    inserted_rows = 0
    previous_part = None
    previous_warning = ""
    candidate_count = 0
    maximum_pending = 0
    maximum_warning_count = 0
    ordinary_control_checks = 0
    ordinary_control_mismatch_count = 0
    ordinary_control_mismatches = []
    available_messages = 0
    causal_match_violations = []
    active_fit_parts = Counter()

    def on_row(row):
        nonlocal inserted_rows
        inserted_rows += 1
        key = (row["Driver"], row["SessionTime"])
        if key not in accepted:
            accepted.add(key)
            accepted_parts[service.raw_snapshot.session_part] += 1

    adapter._rows = ObservedRows(on_row)
    original_ingest, original_finalize = adapter.ingest, adapter.finalize

    def ingest(raw):
        drivers = {driver.driver_number: driver for driver in raw.drivers}
        history = getattr(raw, "speed_history", None)
        freshness = getattr(raw, "speed_freshness_s", 5.)
        for obs in raw.position_observations:
            number, stamp = obs.driver_number, obs.session_time_s
            identity = (number, stamp)
            observations.add(identity)
            driver = drivers.get(number)
            if driver is None or stamp is None:
                rejected["missing_driver_or_position_time"] += 1
                continue
            receipt = getattr(obs, "received_at_utc", None) or raw.last_message_at_utc
            receipt_mono = getattr(obs, "received_at_monotonic_s", None)
            coordinate = getattr(obs, "acquisition_coordinate", None)
            offset = getattr(raw, "speed_coordinate_offset_s", None)
            selected, speed_time, speed = None, None, None
            if history is not None:
                # Instrumentation oracle only: production physical evaluation
                # remains original_ingest below. Histories are ordered within
                # their own coordinates and retain original UTC/session values.
                for speed_obs in history.get(number, ()):
                    candidate_time = speed_obs.session_time_s
                    candidate_coordinate = getattr(speed_obs, "acquisition_coordinate", None)
                    if (coordinate is not None and candidate_coordinate is not None
                            and coordinate != candidate_coordinate):
                        if offset is None or not math.isfinite(offset):
                            continue
                        candidate_time += offset if coordinate == "utc" else -offset
                    if not 0 <= stamp - candidate_time <= freshness:
                        continue
                    candidate_mono = getattr(speed_obs, "received_at_monotonic_s", None)
                    if candidate_mono is not None and receipt_mono is not None:
                        if candidate_mono > receipt_mono:
                            continue
                    elif (speed_obs.received_at_utc is not None and receipt is not None
                          and speed_obs.received_at_utc > receipt):
                        continue
                    if speed_time is None or candidate_time > speed_time:
                        selected, speed_time, speed = speed_obs, candidate_time, speed_obs.speed_kph
            else:
                speed_time, speed = driver.latest_telemetry_time_s, driver.speed_kph
            if speed_time is None or speed is None:
                rejected["missing_fresh_causal_speed"] += 1
            elif speed < 0 or not math.isfinite(speed):
                rejected["invalid_speed"] += 1
            elif not 0 <= stamp - speed_time <= freshness:
                rejected["future_or_stale_speed"] += 1
            else:
                if identity not in matches:
                    matches.add(identity)
                    matched_parts[raw.session_part] += 1
                if selected:
                    candidate_mono = getattr(selected, "received_at_monotonic_s", None)
                    future_receipt = (
                        candidate_mono > receipt_mono
                        if candidate_mono is not None and receipt_mono is not None else
                        selected.received_at_utc is not None and receipt is not None
                        and selected.received_at_utc > receipt
                    )
                    if speed_time > stamp or future_receipt:
                        causal_match_violations.append({"sequence": current_sequence,
                            "driver": number, "position_time": stamp,
                            "position_coordinate": coordinate, "matched_speed_time": speed_time})
        return original_ingest(raw)

    def finalize(driver, lap_number, **kwargs):
        before = set(adapter._admitted_completion_keys)
        traffic = original_finalize(driver, lap_number, **kwargs)
        for key in sorted(adapter._admitted_completion_keys - before, key=str):
            admissions.append({"sequence": current_sequence, "part": key[0], "completion_key": key,
                               "span_m": adapter._span_history.get(key), "deadline": kwargs})
        finalize_calls.append({"sequence": current_sequence, "part": service.session_part,
                               "driver": driver, "lap_number": lap_number,
                               "deadline": kwargs, **asdict(traffic)})
        return traffic

    adapter.ingest, adapter.finalize = ingest, finalize

    original_publish = getattr(service, "_publish_traffic", None)
    if original_publish is not None:
        def publish(pending, cutoff, reason):
            actual_clock = service._traffic_now
            trigger_matches = trigger_clock_kind == pending.clock_kind
            publication_records.append({
                "sequence": current_sequence, "completion_key": pending.key,
                "clock_kind": pending.clock_kind, "reason": reason,
                "completed_clock_s": pending.completed_at,
                "evidence_cutoff_clock_s": cutoff,
                "evidence_cutoff_delay_s": cutoff - pending.completed_at,
                "publication_service_clock_s": actual_clock,
                "publication_service_clock_delay_s": actual_clock - pending.completed_at if actual_clock is not None else None,
                "trigger_message_clock_s": trigger_clock_s if trigger_matches else None,
                "trigger_message_clock_delay_s": trigger_clock_s - pending.completed_at if trigger_matches and trigger_clock_s is not None else None,
            })
            return original_publish(pending, cutoff, reason)
        service._publish_traffic = publish

    def compare_control(kind):
        nonlocal ordinary_control_checks, ordinary_control_mismatch_count
        actual = ordinary_state(service.snapshot)
        shadow = ordinary_state(control.snapshot)
        ordinary_control_checks += 1
        if digest(actual) != digest(shadow):
            ordinary_control_mismatch_count += 1
            if len(ordinary_control_mismatches) < 20:
                ordinary_control_mismatches.append({"sequence": current_sequence, "kind": kind,
                    "traffic_enabled": actual, "traffic_disabled": shadow})

    def checkpoint(kind):
        diagnostic = service.snapshot.track_evolution_diagnostic
        return {"kind": kind, "sequence": current_sequence, "part": service.session_part,
            "pace_candidates": len(service._track_samples),
            "traffic_supported_candidates": sum(s.traffic_status == "ok" and s.physical_mean_ahead_gap_s is not None for s in service._track_samples),
            "eligible_observations": len(diagnostic.observations), "fits": diagnostic.driver_count,
            "aggregate_slope_s_per_push": diagnostic.aggregate_slope_s_per_push,
            "series": [asdict(series) for series in diagnostic.series],
            "pending": len(getattr(service, "_pending_traffic", {})),
            "ordinary": ordinary_state(service.snapshot)}

    def observe(message):
        nonlocal current_sequence, previous_part, candidate_count, maximum_pending
        nonlocal previous_warning, maximum_warning_count, available_messages
        nonlocal trigger_clock_s, trigger_clock_kind
        current_sequence = message.sequence
        trust_flags[str(getattr(message, "received_at_is_trusted", None))] += 1
        if mode == "player" and "live" in name and getattr(message, "received_at_is_trusted", None) is not True:
            raise AssertionError("Production ReplayPlayer lost preserved live receipt trust")
        raw = service.controller.store.apply(message)
        trigger_clock_s = getattr(raw, "traffic_clock_time_s", None)
        trigger_clock_kind = getattr(raw, "traffic_clock_kind", None)
        if raw.session_part != previous_part:
            if previous_part is not None:
                checkpoints.append(checkpoint("before_part_change"))
            transitions.append({"sequence": current_sequence, "part": raw.session_part,
                "established": raw.session_part_established, "remaining_time_s": raw.remaining_time_s})
            previous_part = raw.session_part
        service._consume_message_snapshot(raw)
        control._consume_message_snapshot(raw)
        compare_control("message")
        topics[message.topic] += 1
        original_sources[message.source] += 1
        clocks[str(getattr(raw, "traffic_clock_kind", None))] += 1
        maximum_pending = max(maximum_pending, len(getattr(service, "_pending_traffic", {})))
        for sample in service._track_samples[candidate_count:]:
            candidate_publications.append({"sequence": current_sequence, "identity": pace_identity(sample)})
        candidate_count = len(service._track_samples)
        ordinary = ordinary_state(service.snapshot)
        ordinary_trace.append({"sequence": current_sequence, "sha256": digest(ordinary)})
        diagnostic = service.snapshot.track_evolution_diagnostic
        if diagnostic.aggregate_slope_s_per_push is not None:
            available_messages += 1
            active_fit_parts[service.session_part] += 1
        for series in diagnostic.series:
            record = asdict(series)
            unique_fit_states.setdefault(digest(record), {"first_sequence": current_sequence, **record})
        for sample in diagnostic.observations:
            record = asdict(sample)
            eligible_states.setdefault(digest(record), {"first_sequence": current_sequence, **record})
        warning = getattr(service.snapshot, "telemetry_delay_warning", "")
        count = getattr(service.snapshot, "telemetry_delayed_lap_count", 0)
        maximum_warning_count = max(maximum_warning_count, count)
        if warning != previous_warning:
            warning_changes.append({"sequence": current_sequence, "warning": warning, "count": count})
            previous_warning = warning
        if sum(topics.values()) % 4000 == 0:
            print(json.dumps({"progress": name, "mode": mode, "messages": sum(topics.values()),
                "part": service.session_part, "retained_rows": len(accepted),
                "complete_spans": len(adapter._admitted_completion_keys),
                "candidates": len(service._track_samples), "fits": diagnostic.driver_count}), flush=True)

    status, error = "completed", None
    started = time.perf_counter()
    try:
        if mode == "player":
            replay_result = ReplayPlayer(path, speed=0).play(observe)
            if replay_result.status != "completed":
                raise AssertionError(f"Production replay stopped: {replay_result.status}")
        else:
            events, invalid = load_replay(path)
            if invalid:
                raise AssertionError(f"Invalid recording lines: {invalid}")
            supports_trust = "received_at_is_trusted" in {field.name for field in fields(LiveTimingMessage)}
            for event in events:
                receipt = parse_timestamp(event.original_received_at)
                envelope = dict(topic=event.topic, payload=event.payload,
                    source_timestamp_utc=parse_timestamp(event.original_timestamp),
                    received_at_utc=receipt or datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=event.session_time_seconds),
                    schema_version=event.schema_version, source=event.source, sequence=event.sequence,
                    session_time_seconds=event.message_session_time_seconds, playback_time_seconds=event.session_time_seconds)
                if supports_trust:
                    envelope["received_at_is_trusted"] = receipt is not None and event.source == "signalr_live"
                observe(LiveTimingMessage(**envelope))
        if hasattr(service, "advance_traffic_clock"):
            service.advance_traffic_clock(force=True, reason="replayEOF")
            control.advance_traffic_clock(force=True, reason="replayEOF")
        compare_control("replayEOF")
        if getattr(service, "_pending_traffic", {}):
            raise AssertionError("Pending traffic survived replay EOF")
    except Exception:
        status, error = "failed", traceback.format_exc()
    elapsed = time.perf_counter() - started
    if ordinary_control_mismatch_count:
        status = "failed"
        error = (error or "") + f"\nTraffic-disabled ordinary-state control differed at {ordinary_control_mismatch_count} checks"
    checkpoints.append(checkpoint("final"))
    samples = tuple(service._track_samples)
    diagnostic = service.snapshot.track_evolution_diagnostic
    records = list(getattr(service, "traffic_finalisation_records", ()))
    normal_records = [record for record in records if record["reason"] != "exceptional grace"]
    grace_records = [record for record in records if record["reason"] == "exceptional grace"]
    normal_publications = [record for record in publication_records if record["reason"] != "exceptional grace"]
    grace_publications = [record for record in publication_records if record["reason"] == "exceptional grace"]
    monitor = getattr(service, "telemetry_delays", None)
    per_part = []
    for part in sorted(set(accepted_parts) | {sample.part for sample in samples}):
        candidate_samples = [sample for sample in samples if sample.part == part]
        per_part.append({"part": part, "retained_unique_rows": accepted_parts[part],
            "valid_causal_speed_matches": matched_parts[part], "candidate_pace_laps": len(candidate_samples),
            "complete_span_admissions": sum(record["part"] == part for record in admissions),
            "traffic_supported_candidates": sum(sample.traffic_status == "ok" and sample.physical_mean_ahead_gap_s is not None for sample in candidate_samples),
            "eligible_observations": sum(sample.part == part for sample in diagnostic.observations)})
    after_identities = [pace_identity(sample) for sample in samples]
    before_identities = [pace_identity(sample) for sample in phase0["pace_candidates"]]
    before_counts, after_counts = Counter(before_identities), Counter(after_identities)
    identity_differences = [{"index": index,
        "before": before_identities[index] if index < len(before_identities) else None,
        "after": after_identities[index] if index < len(after_identities) else None}
        for index in range(max(len(before_identities), len(after_identities)))
        if (before_identities[index] if index < len(before_identities) else None)
        != (after_identities[index] if index < len(after_identities) else None)]
    transition_differences = [{"index": index,
        "before": phase0["transitions"][index] if index < len(phase0["transitions"]) else None,
        "after": transitions[index] if index < len(transitions) else None}
        for index in range(max(len(phase0["transitions"]), len(transitions)))
        if (phase0["transitions"][index] if index < len(phase0["transitions"]) else None)
        != (transitions[index] if index < len(transitions) else None)]
    comparison = {
        "input_identical": input_sha == phase0["input_sha256"],
        "message_count_identical": sum(topics.values()) == phase0["message_count"],
        "pace_candidate_identity_sequence_identical": after_identities == before_identities,
        "pace_candidate_identity_fields": PACE_FIELDS,
        "pace_identity_added": list((after_counts - before_counts).elements()),
        "pace_identity_removed": list((before_counts - after_counts).elements()),
        "pace_identity_difference_count": len(identity_differences),
        "pace_identity_first_20_differences": identity_differences[:20],
        "before_candidate_pace_laps": phase0["candidate_pace_laps"],
        "before_retained_unique_rows": phase0["accepted_unique_row_additions"],
        "before_valid_temporal_matches": phase0["valid_temporal_speed_matches_unique_position_samples"],
        "before_complete_span_admissions": phase0["complete_lap_spans_admitted_count"],
        "before_traffic_supported_candidates": phase0["traffic_supported_candidate_laps"],
        "before_eligible_observations": phase0["eligible_clean_air_observations"],
        "before_supported_driver_fits": phase0["supported_driver_fits"],
        "transitions_exactly_identical": transitions == phase0["transitions"],
        "transition_differences": transition_differences,
        "transition_part_sequence_identical": [(v["sequence"], v["part"]) for v in transitions] == [(v["sequence"], v["part"]) for v in phase0["transitions"]],
        "ordinary_per_message_comparison": "NOT AVAILABLE: Phase 0 did not record per-message ordinary states",
    }
    if ordinary_reference_dir is not None:
        reference_path = ordinary_reference_dir / f"{name}_{mode}.json"
        reference = json.loads(reference_path.read_text())
        if reference["input_sha256"] != input_sha:
            raise AssertionError("Ordinary-state reference input differs")
        comparison["ordinary_per_message_comparison"] = "PASS" if reference["ordinary_state_trace"] == ordinary_trace else "FAIL"
        comparison["ordinary_reference"] = str(reference_path)
        comparison["ordinary_mismatch_sequences"] = [after["sequence"] for before, after in zip(reference["ordinary_state_trace"], ordinary_trace) if before != after]
        comparison["ordinary_trace_length_identical"] = len(reference["ordinary_state_trace"]) == len(ordinary_trace)
    result = {
        "name": name, "mode": mode, "label": label, "status": status, "error": error,
        "input": str(path), "input_sha256": input_sha, "phase0_report": str(PHASE0 / f"{name}.json"),
        "source_sha256_start": source_start, "source_sha256_end": source_hashes(),
        "source_unchanged_during_replay": source_start == source_hashes(),
        "accelerated_replay_elapsed_s": elapsed,
        "clock_note": "Elapsed wall time is execution cost only; it is never measured telemetry arrival latency.",
        "receipt_semantics": "preserved original live receipts" if "live" in name else "archive scheduling coordinates only; no recorded adapter arrival evidence",
        "message_count": sum(topics.values()), "topic_counts": dict(topics), "envelope_sources": dict(original_sources),
        "traffic_clock_kinds": dict(clocks), "receipt_trust_flags": dict(trust_flags),
        "has_causal_history_interface": has_history,
        "unique_position_observations": len(observations), "adapter_insert_or_append_count": inserted_rows,
        "retained_unique_rows": len(accepted), "valid_causal_speed_matches": len(matches),
        "causal_match_violations": causal_match_violations, "position_rejections": dict(rejected),
        "row_count_final": adapter.row_count, "row_count_max": adapter.max_rows,
        "complete_span_admissions": len(adapter._admitted_completion_keys),
        "active_complete_span_history": list(adapter._span_history.items()), "span_admissions": admissions,
        "candidate_pace_laps": len(samples), "candidate_publications": candidate_publications,
        "pace_candidates": [asdict(sample) for sample in samples],
        "traffic_supported_candidates": sum(sample.traffic_status == "ok" and sample.physical_mean_ahead_gap_s is not None for sample in samples),
        "eligible_observations": len(diagnostic.observations), "eligible_observations_ever": list(eligible_states.values()),
        "supported_driver_fits": diagnostic.driver_count,
        "supported_driver_fit_states_ever": list(unique_fit_states.values()),
        "track_correction_available_final": diagnostic.aggregate_slope_s_per_push is not None,
        "track_correction_available_message_count": available_messages, "available_message_states_by_part": dict(active_fit_parts),
        "aggregate_slope_s_per_push": diagnostic.aggregate_slope_s_per_push,
        "final_series": [asdict(series) for series in diagnostic.series],
        "pending_final": len(getattr(service, "_pending_traffic", {})), "pending_max": maximum_pending,
        "finalize_calls": finalize_calls, "finalisation_records": records,
        "normal_finalisation_delay": distribution(record["delay_s"] for record in normal_records),
        "finalisation_delay_semantics": "normal_finalisation_delay/exceptional_grace_delay describe evidence cutoffs, not publication scheduling delay",
        "publication_records": publication_records,
        "normal_evidence_cutoff_delay": distribution(record["evidence_cutoff_delay_s"] for record in normal_publications),
        "normal_publication_service_clock_delay": distribution(record["publication_service_clock_delay_s"] for record in normal_publications),
        "normal_trigger_message_clock_delay": distribution(record["trigger_message_clock_delay_s"] for record in normal_publications),
        "grace_publication_service_clock_delay": distribution(record["publication_service_clock_delay_s"] for record in grace_publications),
        "publication_clock_note": "Replay-derived first-advance publication clocks; receive clocks preserve recorded receipts, archive clocks describe playback only. Direct and ReplayPlayer callbacks have no active live poll timer, so sparse messages may trigger publication after the 2s evidence cutoff. These are not measured historical feature publication times or telemetry sample-arrival delays.",
        "exceptional_grace_updates": len(grace_records),
        "exceptional_grace_delay": distribution(record["delay_s"] for record in grace_records),
        "finalisation_reason_counts": dict(Counter(record["reason"] for record in records)),
        "deadline_clock_kinds": dict(Counter(record["clock_kind"] for record in records)),
        "delay_warning": {"changes": warning_changes, "maximum_distinct_lap_count": maximum_warning_count,
            "final_distinct_lap_count": getattr(service.snapshot, "telemetry_delayed_lap_count", 0),
            "observed_late_samples": getattr(monitor, "observed_late_samples", 0),
            "maximum_observed_sample_delay_s": getattr(monitor, "maximum_delay_s", None)},
        "per_part": per_part, "transitions": transitions, "checkpoints": checkpoints,
        "ordinary_state_trace": ordinary_trace, "ordinary_final": ordinary_state(service.snapshot),
        "ordinary_traffic_disabled_control": {"checks": ordinary_control_checks,
            "mismatch_count": ordinary_control_mismatch_count,
            "first_20_mismatches": ordinary_control_mismatches,
            "status": "PASS" if ordinary_control_mismatch_count == 0 else "FAIL",
            "scope": "Current-runtime service consumes identical raw snapshots with traffic ingestion/evaluation disabled. Compares ordinary lap states, timing, classification/rank, finish/planning state and actual/current cutoffs. Excludes intended Track Correction forecasts. This is not an original Phase 0 per-message baseline comparison."},
        "baseline_comparison": comparison,
        "metric_note": "Retained rows, complete-span admissions, traffic support, eligible observations and supported fits are separate metrics; none implies the next.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{name}_{mode}.json"
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite replay evidence: {target}")
    target.write_text(json.dumps(result, indent=2, default=str) + "\n")
    summary_keys = ("name", "mode", "status", "message_count", "retained_unique_rows", "valid_causal_speed_matches",
                    "complete_span_admissions", "candidate_pace_laps", "traffic_supported_candidates",
                    "eligible_observations", "supported_driver_fits", "track_correction_available_final",
                    "pending_final", "exceptional_grace_updates", "source_unchanged_during_replay")
    summary = {key: result[key] for key in summary_keys}
    summary["pace_candidates_identical"] = comparison["pace_candidate_identity_sequence_identical"]
    summary["transitions_identical"] = comparison["transitions_exactly_identical"]
    summary["ordinary_comparison"] = comparison["ordinary_per_message_comparison"]
    summary["ordinary_control_status"] = result["ordinary_traffic_disabled_control"]["status"]
    summary["ordinary_control_mismatches"] = ordinary_control_mismatch_count
    summary["report"] = str(target)
    print(json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=INPUTS)
    parser.add_argument("--mode", choices=("direct", "player", "all"), default="all")
    parser.add_argument("--label", default="after")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--ordinary-reference-dir", type=Path)
    parser.add_argument("--summarize-existing", action="store_true",
                        help="Consolidate completed independent runs without replaying or overwriting their evidence")
    args = parser.parse_args()
    output_dir = args.output_root / args.label
    if output_dir.resolve() == PHASE0.resolve() or PHASE0.resolve() in output_dir.resolve().parents:
        raise ValueError("Phase 0 evidence must remain untouched")
    names = [args.session] if args.session else list(INPUTS)
    summaries = []
    for name in names:
        modes = [args.mode] if args.mode != "all" else (["direct", "player"] if "live" in name else ["direct"])
        for mode in modes:
            if args.summarize_existing:
                report_path = output_dir / f"{name}_{mode}.json"
                report = json.loads(report_path.read_text())
                keys = ("name", "mode", "status", "message_count", "retained_unique_rows", "valid_causal_speed_matches",
                        "complete_span_admissions", "candidate_pace_laps", "traffic_supported_candidates",
                        "eligible_observations", "supported_driver_fits", "track_correction_available_final",
                        "pending_final", "exceptional_grace_updates", "source_unchanged_during_replay")
                result = {key: report[key] for key in keys}
                comparison = report["baseline_comparison"]
                result.update(pace_candidates_identical=comparison["pace_candidate_identity_sequence_identical"],
                              transitions_identical=comparison["transitions_exactly_identical"],
                              ordinary_comparison=comparison["ordinary_per_message_comparison"], report=str(report_path),
                              ordinary_control_status=report["ordinary_traffic_disabled_control"]["status"],
                              ordinary_control_mismatches=report["ordinary_traffic_disabled_control"]["mismatch_count"],
                              source_matches_current_runtime=report["source_sha256_end"] == PROCESS_SOURCE_HASHES,
                              warning_max_distinct_laps=report["delay_warning"]["maximum_distinct_lap_count"],
                              warning_late_sample_count=report["delay_warning"]["observed_late_samples"],
                              normal_finalisation_delay=report["normal_finalisation_delay"],
                              normal_evidence_cutoff_delay=report["normal_evidence_cutoff_delay"],
                              normal_publication_service_clock_delay=report["normal_publication_service_clock_delay"],
                              normal_trigger_message_clock_delay=report["normal_trigger_message_clock_delay"],
                              exceptional_grace_delay=report["exceptional_grace_delay"])
                if not result["source_matches_current_runtime"]:
                    raise AssertionError(f"Consolidation source differs from replay source: {name}/{mode}")
                print(json.dumps(result), flush=True)
            else:
                result = verify(name, mode=mode, output_dir=output_dir, label=args.label,
                                ordinary_reference_dir=args.ordinary_reference_dir)
            summaries.append(result)
            if result["status"] != "completed" or not result["source_unchanged_during_replay"]:
                raise RuntimeError(f"Replay evidence invalid; stop before subsequent cached-runtime replays: {name}/{mode}")
    summary_path = output_dir / (f"summary_{args.session or 'all'}_{args.mode}.json")
    if summary_path.exists():
        raise FileExistsError(summary_path)
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    if any(item["status"] != "completed" or not item["source_unchanged_during_replay"] or
           not item["pace_candidates_identical"] or item["ordinary_comparison"] == "FAIL" for item in summaries):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
