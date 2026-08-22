#!/usr/bin/env python3
"""R11 evidence for the live-retro semantics audit."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import diagnose_r11_mc_instability as replay
from wostrategy.algorithm.monte_carlo_race_performance import (
    MonteCarloRacePerformanceAlgorithm,
    MonteCarloRacePerformanceConfig,
)
from wostrategy.analysis.race_performance_review import summarize_monte_carlo_race_performance
from wostrategy.model.live_retro_performance import (
    prepare_live_retro_laps,
    run_live_retro_model,
)

OUTPUT = Path("temp/r11_retro_vs_live_semantics")
LAPS = (9, 22, 25, 37, 38, 55, 56, 70)


def config(settings: dict, sample_count: int = 5_000) -> MonteCarloRacePerformanceConfig:
    threshold = settings.get("degradation_order_track_temperature")
    return MonteCarloRacePerformanceConfig(
        sample_count=sample_count,
        fuel_rate_bounds=tuple(settings["fuel_rate_bounds"]),
        track_rate_bounds=tuple(settings["track_rate_bounds"]),
        limit_negative_track_correction=bool(settings["limit_negative_track_correction"]),
        compound_degradation_bounds={k: tuple(v) for k, v in settings["compound_degradation_bounds"].items()},
        default_compound_degradation_bounds=tuple(settings["default_compound_degradation_bounds"]),
        compound_delta_bounds={k: tuple(v) for k, v in settings["compound_delta_bounds"].items()},
        default_compound_delta_bounds=tuple(settings["default_compound_delta_bounds"]),
        compound_delta_reference=settings["compound_delta_reference"],
        team_variation_fraction=float(settings["team_variation_fraction"]),
        team_variation_absolute_min=float(settings["team_variation_absolute_min"]),
        clean_lap_noise_sigma=float(settings["clean_lap_noise_sigma"]),
        weight_strategy=settings["weight_strategy"],
        weight_effective_sample_count=settings.get("weight_effective_sample_count"),
        baseline_group=settings["baseline_group"],
        fuel_ref=float(settings["fuel_ref"]), race_lap_ref=settings["race_lap_ref"],
        tyre_age_ref=float(settings["tyre_age_ref"]), random_seed=1_898_758_267,
        sampling_strategy=settings["sampling_strategy"],
        track_temperature_celsius=float(threshold) + 1 if threshold is not None else None,
        degradation_order_track_temperature_celsius=threshold,
    )


def summary_rows(result, lap: int, runtime: float) -> list[dict]:
    summary = summarize_monte_carlo_race_performance(
        sample_parameters=result.sample_parameters,
        compound_degradation=result.compound_degradation,
        compound_delta=result.compound_delta,
        team_compound_degradation=result.team_compound_degradation,
        baseline_pace=result.baseline_pace,
    )
    weights = result.sample_parameters.Weight.to_numpy(float, copy=True)
    weights /= weights.sum()
    common = {"leader_lap": lap, "clean_laps": int(result.sample_parameters.CleanLapCount.iloc[0]),
              "dimension_count": 2 + result.compound_degradation.Compound.nunique()
              + max(result.compound_delta.Compound.nunique() - 1, 0)
              + len(result.team_compound_degradation[["Team", "Compound"]].drop_duplicates()),
              "ess": 1 / np.sum(weights**2), "max_weight": weights.max(),
              "runtime_seconds": runtime}
    rows = []
    for name, frame, group in (
        ("fuel_rate", summary["fuel_rate"], None),
        ("track_rate", summary["track_rate"], None),
        ("degradation", summary["compound_degradation"], "Compound"),
        ("compound_delta", summary["compound_delta"], "Compound"),
    ):
        for item in frame.to_dict("records"):
            rows.append({**common, "parameter": name,
                         "compound": item.get(group) if group else None,
                         "p10": item["P10"], "median": item["Median"], "p90": item["P90"],
                         "p10_p90_width": item["P90"] - item["P10"]})
    return rows


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    settings = json.loads(Path(
        "woData/wostrategy/race_performance_review/schema_v1/year=2026/round=11/session=R/"
        "race_performance_2026_11_R_metadata.json"
    ).read_text())
    cfg = config(settings)
    snapshot, _, _ = replay.replay_snapshot(Path(
        "woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl"
    ))
    frames, _, _ = replay.build_race_laps(snapshot)
    history = []
    equivalence = []
    race_end_summaries = []
    quality = []
    for lap in LAPS:
        started = time.perf_counter()
        live = run_live_retro_model(frames[lap], cfg, as_of_leader_lap=lap)
        elapsed = time.perf_counter() - started
        history.extend(summary_rows(live.retro_result, lap, elapsed))
        quality.append({"leader_lap": lap, "clean_laps": len(frames[lap]),
                        **live.quality, "runtime_seconds": elapsed})
        if lap == 70:
            prepared = prepare_live_retro_laps(frames[lap], race_lap_count=lap)
            direct = MonteCarloRacePerformanceAlgorithm(cfg).run(prepared)
            for table in ("sample_parameters", "compound_degradation", "compound_delta",
                          "team_compound_degradation", "baseline_pace"):
                left = getattr(live.retro_result, table)
                right = getattr(direct, table)
                numeric = left.select_dtypes(include="number").columns.intersection(
                    right.select_dtypes(include="number").columns
                )
                difference = 0.0 if not len(numeric) else float(np.nanmax(np.abs(
                    left[numeric].to_numpy(float) - right[numeric].to_numpy(float)
                )))
                equivalence.append({"leader_lap": lap, "table": table,
                                    "row_count_live": len(left), "row_count_retro": len(right),
                                    "maximum_numeric_difference": difference,
                                    "exact_frame_equal": left.equals(right)})
            for implementation, value in (("live_retro", live.retro_result),
                                          ("direct_retro", direct)):
                summaries = summarize_monte_carlo_race_performance(
                    sample_parameters=value.sample_parameters,
                    compound_degradation=value.compound_degradation,
                    compound_delta=value.compound_delta,
                    team_compound_degradation=value.team_compound_degradation,
                    baseline_pace=value.baseline_pace,
                )
                for name, frame in summaries.items():
                    for row in frame.to_dict("records"):
                        race_end_summaries.append({"implementation": implementation,
                                                  "summary": name, **row})
    pd.DataFrame(history).to_csv(OUTPUT / "live_retro_snapshot_history.csv", index=False)
    pd.DataFrame(quality).to_csv(OUTPUT / "live_retro_quality.csv", index=False)
    pd.DataFrame(equivalence).to_csv(OUTPUT / "race_end_exact_equivalence.csv", index=False)
    pd.DataFrame(race_end_summaries).to_csv(OUTPUT / "race_end_summary_equivalence.csv", index=False)
    saved_clean = pd.read_csv(
        "woData/wostrategy/race_performance_review/schema_v1/year=2026/round=11/session=R/"
        "race_performance_2026_11_R_clean_laps.csv"
    )
    pd.DataFrame([{
        "saved_retro_clean_laps": len(saved_clean), "live_end_clean_laps": len(frames[70]),
        "difference": len(frames[70]) - len(saved_clean),
        "reason": "different live/offline traffic and preparation inputs; algorithm equality tested on identical live frame",
    }]).to_csv(OUTPUT / "race_end_input_comparison.csv", index=False)
    print(json.dumps({"history_rows": len(history), "equivalence": equivalence,
                      "maximum_runtime_seconds": max(row["runtime_seconds"] for row in quality)}))


if __name__ == "__main__":
    main()
