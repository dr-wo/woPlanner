"""Render a causal R13/R14 Q2 T−03:00 Track Evolution acceptance artifact."""

from __future__ import annotations

import json
import argparse
from statistics import median
from time import monotonic, sleep
from datetime import datetime, timezone
from pathlib import Path

from wodata.live import LiveTimingMessage
from wodata.live.replay import load_replay, parse_timestamp
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution import render_track_evolution_diagnostic


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT.parent / "woData"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=(13, 14), default=14)
    args = parser.parse_args()
    archive = DATA_ROOT / f"wodata/live_archive/schema_v1/year=2026/round={args.round}/session=Q/replay/merged_replay_v1.jsonl"
    stem = ROOT / f"doc/assets/qualifying_track_evolution_r{args.round}_q2_tminus3"
    png = stem.with_suffix(".png")
    ui_png = stem.with_name(stem.name + "_quali_plan").with_suffix(".png")
    evidence_path = stem.with_suffix(".json")
    events, _ = load_replay(archive)
    service = QualiLiveService()
    service._geometry_data_root = DATA_ROOT
    store = service.controller.store
    selected = None
    for event in events:
        message = LiveTimingMessage(
            event.topic, event.payload, parse_timestamp(event.original_timestamp),
            datetime.now(timezone.utc), source=event.source, sequence=event.sequence,
            session_time_seconds=event.message_session_time_seconds if event.preserve_message_session_time else event.session_time_seconds,
        )
        raw = store.apply(message)
        service._consume_message_snapshot(raw)
        if raw.session_part == "Q2" and raw.remaining_time_s is not None:
            if raw.remaining_time_s >= 180:
                samples = tuple(service._track_samples)
                snapshot = service.snapshot
                diagnostic = snapshot.track_evolution_diagnostic
                selected = (
                    event, raw, samples, diagnostic,
                    service._physical_traffic.row_count,
                    service._physical_traffic.max_rows,
                    service._physical_traffic.estimated_buffer_bytes
                    if raw.remaining_time_s <= 181 else None,
                    tuple(service._physical_traffic.evaluation_times_s),
                    service.clean_air_gap_s, service._physical_traffic.policy,
                    snapshot,
                )
            elif selected is not None:
                break
    if selected is None:
        raise RuntimeError("Q2 T−03:00 causal snapshot was not found")
    (event, raw, samples, diagnostic, buffer_rows, buffer_peak_rows, buffer_bytes,
     evaluation_times, clean_air_gap_s, coverage_policy, snapshot) = selected
    assert diagnostic.clean_air_gap_s == clean_air_gap_s
    assert diagnostic.min_ahead_coverage == coverage_policy.min_ahead_coverage
    fitted = tuple(sample for series in diagnostic.series for sample in series.samples)
    if args.round == 14:
        assert {sample.part for sample in fitted} == {"Q1", "Q2"}
    else:
        law = next(series for series in diagnostic.series if series.driver == "LAW")
        assert law.compound == "SOFT"
        assert {sample.tyre_install_index for sample in law.samples} == {2, 3}
        assert any(sample.driver == "LAW" and sample.compound == "MEDIUM"
                   and sample.part == "Q1" for sample in diagnostic.observations)
    from PySide6 import QtGui, QtWidgets
    from woplanner.quali.analysis import QualiAnalysisService
    from woplanner.quali.widgets import QualiPlanDialog
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The real Plan dialog consumes the same snapshot object as the monitor.
    service.snapshot = snapshot
    view = QualiPlanDialog(QualiAnalysisService(DATA_ROOT), service)
    view.tabs.setCurrentIndex(1)
    view.show()
    view.refresh_live_track(snapshot)
    plot = view.track_diagnostic
    assert plot.diagnostic is diagnostic
    assert view.track_diagnostic_summary.text().find(f"{diagnostic.driver_count} drivers / {diagnostic.lap_count} laps") >= 0
    rendered = render_track_evolution_diagnostic(diagnostic, png)
    deadline = monotonic() + 5.0
    while plot._plot.isNull() and monotonic() < deadline:
        app.processEvents()
        sleep(.01)
    assert not plot._plot.isNull(), "Qt diagnostic image was not rendered"
    assert plot._plot.toImage().convertToFormat(QtGui.QImage.Format.Format_RGB32) == \
        QtGui.QImage.fromData(rendered).convertToFormat(QtGui.QImage.Format.Format_RGB32)
    assert view.grab().save(str(ui_png))
    view.close()
    app.processEvents()
    pushes = list(samples)
    q2_pushes = [sample for sample in pushes if sample.part == "Q2"]
    fresh = [sample for sample in pushes if sample.fresh is True]
    covered = [sample for sample in fresh if sample.traffic_status == "ok"
               and sample.ahead_coverage >= diagnostic.min_ahead_coverage]
    clean = [sample for sample in covered if sample.physical_mean_ahead_gap_s is not None
             and sample.physical_mean_ahead_gap_s > diagnostic.clean_air_gap_s]
    groups = {}
    for sample in diagnostic.observations:
        groups.setdefault((sample.driver, sample.compound), []).append(sample)
    insufficient_groups = [
        {"driver": driver, "compound": compound,
         "laps": [{"part": sample.part, "lap": sample.lap_number,
                   "tyre_install_index": sample.tyre_install_index}
                  for sample in values], "support_count": len(values)}
        for (driver, compound), values in sorted(groups.items()) if len(values) < 2
    ]
    associated = {(lap.driver_number, lap.lap_number) for lap in raw.completed_lap_sequence
                  if lap.session_part == "Q2"}
    evidence = {
        "archive_timestamp": event.original_timestamp,
        "archive_session_time_s": event.session_time_seconds,
        "archive_sequence": event.sequence,
        "q2_remaining_s": raw.remaining_time_s,
        "selection": "latest processed Q2 state at or before T−03:00; next processed state is below 180 seconds",
        "clean_air_gap_s": diagnostic.clean_air_gap_s,
        "minimum_ahead_coverage": diagnostic.min_ahead_coverage,
        "require_ahead": coverage_policy.require_ahead,
        "require_behind": coverage_policy.require_behind,
        "tyre_install_identity": "TimingAppData.Stints index, retained across Q1/Q2/Q3",
        "funnel": {"associated_q2_timed_laps": len(associated),
                   "fast_push_q2": len(q2_pushes),
                   "fast_push_session_to_cutoff": len(pushes),
                   "fresh_new": len(fresh), "ahead_coverage": len(covered),
                   "clean": len(clean),
                   "first_eligible_install_observations": len(diagnostic.observations),
                   "supported_driver_fits": diagnostic.driver_count,
                   "laps_in_supported_fits": diagnostic.lap_count},
        "rolling_buffer_rows": buffer_rows,
        "rolling_buffer_peak_rows": buffer_peak_rows,
        "rolling_buffer_estimated_bytes": buffer_bytes,
        "last_completed_lap_evaluation_s": evaluation_times[-1] if evaluation_times else None,
        "median_completed_lap_evaluation_s": median(evaluation_times) if evaluation_times else None,
        "maximum_completed_lap_evaluation_s": max(evaluation_times, default=None),
        "driver_count": diagnostic.driver_count,
        "lap_count": diagnostic.lap_count,
        "aggregate_slope_s_per_push": diagnostic.aggregate_slope_s_per_push,
        "weighted_slope_std_s_per_push": diagnostic.weighted_slope_std_s_per_push,
        "quali_plan_ui_image": str(ui_png.relative_to(ROOT)),
        "previous_q2_only": {"drivers": 2, "laps": 4,
                             "aggregate_slope_s_per_push": -0.0401}
        if args.round == 14 else {"drivers": 1, "laps": 2,
                                  "aggregate_slope_s_per_push": 0.0156},
        "series": [
            {"driver": series.driver, "team": series.team, "compound": series.compound,
             "fit_support": len(series.samples), "slope_s_per_push": series.slope_s_per_push,
             "laps": [{"part": sample.part, "lap": sample.lap_number,
                       "push_index": sample.global_push_index,
                       "lap_time_s": sample.lap_time_s,
                       "stint_start_laps": sample.stint_start_laps,
                       "tyre_install_index": sample.tyre_install_index,
                       "compound": sample.compound,
                       "first_eligible_push_for_installation": sample.first_eligible_push_for_installation,
                       "physical_mean_ahead_gap_s": sample.physical_mean_ahead_gap_s,
                       "ahead_coverage": sample.ahead_coverage,
                       "traffic_status": sample.traffic_status,
                       "traffic_target_samples": sample.traffic_target_samples,
                       "traffic_ahead_samples": sample.traffic_ahead_samples,
                       "clean_air_at_completion": sample.clean_air_at_completion,
                       "traffic_evaluator_version": sample.traffic_evaluator_version,
                       "traffic_circuit_length_m": sample.traffic_circuit_length_m}
                      for sample in series.samples]}
            for series in diagnostic.series
        ],
        "omitted": diagnostic.omitted,
        "insufficient_fit_groups": insufficient_groups,
        "first_eligible_install_observations": [
            {"driver": s.driver, "part": s.part, "lap": s.lap_number,
             "push_index": s.global_push_index, "compound": s.compound,
             "tyre_install_index": s.tyre_install_index,
             "stint_start_laps": s.stint_start_laps,
             "first_eligible_push_for_installation": s.first_eligible_push_for_installation,
             "physical_mean_ahead_gap_s": s.physical_mean_ahead_gap_s,
             "ahead_coverage": s.ahead_coverage}
            for s in diagnostic.observations
        ],
        "all_fast_push_samples": [
            {"driver": s.driver, "part": s.part, "lap": s.lap_number,
             "push_index": s.global_push_index,
             "compound": s.compound, "stint_start_laps": s.stint_start_laps,
             "tyre_install_index": s.tyre_install_index,
             "first_eligible_push_for_installation": any(
                 observed.driver == s.driver and observed.global_push_index == s.global_push_index
                 for observed in diagnostic.observations
             ),
             "fresh": s.fresh, "physical_mean_ahead_gap_s": s.physical_mean_ahead_gap_s,
             "ahead_coverage": s.ahead_coverage, "traffic_status": s.traffic_status,
             "traffic_method": s.traffic_method,
             "clean_air_at_completion": s.clean_air_at_completion,
             "clean_air_gap_threshold_s": s.clean_air_gap_threshold_s,
             "traffic_circuit_length_m": s.traffic_circuit_length_m}
            for s in pushes
        ],
    }
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
