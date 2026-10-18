"""Prove the live R16 source using current production admission and timestamps.

Run with --baseline before the fix; without it, assert the canonical contract.
The recording remains outside Git. No frozen/candidate-gate modules are imported.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

from wodata.live import LiveTimingMessage
from woplanner.quali.service import QualiLiveService
import woplanner.quali.track_evolution as diagnostic_module
import wostrategy.analysis.live_qualifying as legacy_module


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _slope(samples):
    """Independent OLS reconstruction, without either production fit helper."""
    x = [sample.global_push_index for sample in samples]
    y = [sample.lap_time_s for sample in samples]
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    return sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)) / sum(
        (a - mean_x) ** 2 for a in x
    )


def verify(recording=RECORDING, *, baseline=False, stop_sequence=None):
    service = QualiLiveService()
    service._geometry_data_root = ROOT.parent / "woData"
    store = service.controller.store
    fit_calls, aggregate_calls = [], []
    transitions, checkpoints = [], []
    previous_part = None
    checked_supported_states = 0
    fit = diagnostic_module.fit_driver_track_progression
    aggregate = diagnostic_module.aggregate_driver_track_progression
    legacy_estimator = legacy_module.calculate_live_qualifying_track_evolution

    def traced_fit(indices, lap_times):
        indices, lap_times = tuple(indices), tuple(lap_times)
        rate = fit(indices, lap_times)
        fit_calls.append({"indices": indices, "lap_times_s": lap_times, "slope": rate})
        return rate

    def traced_aggregate(values):
        values = tuple(values)
        rate = aggregate(values)
        aggregate_calls.append({"driver_slopes_and_support": values, "rate": rate})
        return rate

    def evidence(sequence):
        diagnostic = service.snapshot.track_evolution_diagnostic
        series = [
            {"driver": item.driver, "compound": item.compound,
             "support": len(item.samples), "slope": item.slope_s_per_push,
             "reconstructed_slope": _slope(item.samples),
             "samples": [asdict(sample) for sample in item.samples]}
            for item in diagnostic.series
        ]
        for item in series:
            assert abs(item["slope"] - item["reconstructed_slope"]) < 1e-10
        support = sum(item["support"] for item in series)
        reconstructed = (sum(item["support"] * item["reconstructed_slope"]
                             for item in series) / support) if support else None
        if reconstructed is not None:
            assert abs(reconstructed - diagnostic.aggregate_slope_s_per_push) < 1e-10
        # The former live scalar pooled every production pace-admitted PUSH in
        # the current part, regardless of diagnostic traffic/tyre eligibility.
        pooled_samples = [sample for sample in service._track_samples
                          if sample.part == service.session_part]
        pooled_values = [sample.lap_time_s for sample in pooled_samples]
        legacy = legacy_estimator(pooled_values)
        actual_fits = fit_calls[-len(series):] if series else []
        if series:
            # Match observed fit arguments by identity of indices/time arrays,
            # not by slope coincidence; fitting order precedes driver sorting.
            for item in diagnostic.series:
                assert any(call["indices"] == tuple(s.global_push_index for s in item.samples)
                           and call["lap_times_s"] == tuple(s.lap_time_s for s in item.samples)
                           for call in actual_fits)
            assert aggregate_calls[-1]["driver_slopes_and_support"] == tuple(
                (item.slope_s_per_push, len(item.samples)) for item in diagnostic.series
            )
            assert aggregate_calls[-1]["rate"] == diagnostic.aggregate_slope_s_per_push
        return {
            "sequence": sequence, "part": service.session_part,
            "series": series, "aggregate": diagnostic.aggregate_slope_s_per_push,
            "reconstructed_aggregate": reconstructed,
            "legacy_pooled_scalar": legacy.rate_s_per_field_push,
            "legacy_pooled_support": legacy.evidence_count,
            "automatic_rate": service.automatic_track_rate_s_per_field_push,
            "effective_rate": service.track_rate_s_per_field_push,
            "effective_source": service.track_source,
            "snapshot": asdict(service.snapshot.cutoff),
            "admitted_sample_count": len(service.track_evolution_policy.samples),
            "auxiliary_traffic_sample_count": len(service._track_samples),
            "traffic_available_sample_count": sum(
                sample.traffic_status == "ok" and sample.physical_mean_ahead_gap_s is not None
                for sample in service._track_samples
            ),
            "traffic_circuit_history_size": len(service._physical_traffic._lap_spans),
            "admitted_samples_sha256": sha256(json.dumps(
                [asdict(s) for s in service.track_evolution_policy.samples.values()], sort_keys=True,
            ).encode()).hexdigest(),
            "actual_aggregate_call": aggregate_calls[-1] if aggregate_calls else None,
            "actual_driver_fit_calls": actual_fits,
        }

    # Only calls originating inside live consumption are counted. Independent
    # legacy calculations call the original function outside the wrapper.
    with patch.object(diagnostic_module, "fit_driver_track_progression", traced_fit), \
            patch.object(diagnostic_module, "aggregate_driver_track_progression", traced_aggregate), \
            patch.object(legacy_module, "calculate_live_qualifying_track_evolution",
                         wraps=legacy_module.calculate_live_qualifying_track_evolution) as legacy_calls:
        with Path(recording).open() as stream:
            for line in stream:
                record = json.loads(line)
                message = LiveTimingMessage(
                    topic=record["topic"], payload=record["payload"],
                    source_timestamp_utc=_stamp(record.get("source_timestamp_utc")),
                    received_at_utc=_stamp(record["received_at_utc"]),
                    schema_version=record.get("message_schema_version", 1),
                    source=record["source"], sequence=record["sequence"],
                    session_time_seconds=record.get("session_time_seconds"),
                )
                service._consume_message_snapshot(store.apply(message))
                snapshot = service.snapshot
                diagnostic = snapshot.track_evolution_diagnostic
                if not baseline and diagnostic.aggregate_slope_s_per_push is not None:
                    assert service.automatic_track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
                    assert snapshot.cutoff.track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
                    assert snapshot.automatic_track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
                    assert snapshot.automatic_track_source == "Live per-driver diagnostic (session-wide)"
                    checked_supported_states += 1
                if service.session_part != previous_part:
                    transitions.append({"sequence": message.sequence,
                                        "part": service.session_part,
                                        "aggregate": diagnostic.aggregate_slope_s_per_push,
                                        "automatic_rate": service.automatic_track_rate_s_per_field_push,
                                        "effective_rate": service.track_rate_s_per_field_push,
                                        "source": service.track_source,
                                        "support": diagnostic.lap_count})
                    previous_part = service.session_part
                if message.sequence == 6451:
                    # Evidence must use the exact causal anchor, before Q2/Q3.
                    checkpoints.append(evidence(message.sequence))
                if stop_sequence is not None and message.sequence >= stop_sequence:
                    break
        service_legacy_call_count = legacy_calls.call_count
    if not checkpoints or checkpoints[-1]["sequence"] != message.sequence:
        checkpoints.append(evidence(message.sequence))
    if not baseline:
        assert service_legacy_call_count == 0, "Pooled estimator reentered live consumption"
        assert checkpoints[0]["aggregate"] != checkpoints[0]["legacy_pooled_scalar"]
        for checkpoint in checkpoints:
            if checkpoint["aggregate"] is None:
                # Corrected phase validity may remove every former traffic
                # observation. Verify the existing unavailable fallback.
                assert checkpoint["automatic_rate"] == service._fp_track_rate_s_per_field_push
                assert checkpoint["effective_source"] == "Configured fallback"
            else:
                assert checkpoint["automatic_rate"] == checkpoint["aggregate"]
    runtime_sources = {
        "service": str(Path(__import__(QualiLiveService.__module__, fromlist=["x"]).__file__).resolve()),
        "diagnostic": str(Path(diagnostic_module.__file__).resolve()),
        "legacy": str(Path(legacy_module.__file__).resolve()),
    }
    return {
        "baseline": baseline, "recording": str(recording),
        "recording_sha256": sha256(Path(recording).read_bytes()).hexdigest(),
        "runtime_sources": runtime_sources,
        "source_sha256": {name: sha256(Path(path).read_bytes()).hexdigest()
                          for name, path in runtime_sources.items()},
        "consumer_path": (["QualiLiveService._consume_driver / transition_to",
                           "_update_track_from_part",
                           "calculate_live_qualifying_track_evolution",
                           "estimate_push_sequence_track_progression",
                           "LinearTrackEvolutionModel.fit",
                           "TrackEvolutionFit.evolution_rate_seconds_per_lap (-raw slope)",
                           "QualiLiveService._build_snapshot", "CutoffProjection"]
                          if baseline else
                          ["QualiLiveService._build_snapshot",
                           "track_evolution_diagnostic", "build_track_evolution_diagnostic",
                           "fit_driver_track_progression (per driver)",
                           "aggregate_driver_track_progression (slope, support count)",
                           "_update_track_from_diagnostic", "CutoffProjection"]),
        "auxiliary_traffic_threshold_fraction": service.push_threshold_fraction,
        "production_gross_sanity_fraction": service.track_evolution_payload()["thresholds"]["gross_fraction"],
        "live_consumer_legacy_calls": service_legacy_call_count,
        "supported_states_checked": checked_supported_states,
        "transitions": transitions, "checkpoints": checkpoints,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", type=Path, default=RECORDING)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--stop-sequence", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.recording, baseline=args.baseline, stop_sequence=args.stop_sequence)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output),
                      "live_consumer_legacy_calls": result["live_consumer_legacy_calls"],
                      "supported_states_checked": result["supported_states_checked"],
                      "transitions": result["transitions"],
                      "anchor": {key: value for key, value in result["checkpoints"][0].items()
                                 if key not in {"series", "actual_driver_fit_calls"}}}, indent=2))


if __name__ == "__main__":
    main()
