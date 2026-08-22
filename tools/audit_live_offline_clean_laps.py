#!/usr/bin/env python3
"""Classify R11 live/offline eligible-lap differences at Race end."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import diagnose_r11_mc_instability as replay
from woplanner.analysis.service import _live_traffic_gaps

OUTPUT = Path("temp/r11_retro_convergence")


def main():
    snapshot, _, _ = replay.replay_snapshot(Path(
        "woData/wodata/live_archive/schema_v1/year=2026/round=11/session=R/replay/merged_replay_v1.jsonl"
    ))
    frames, _, _ = replay.build_race_laps(snapshot)
    live = frames[70].copy()
    offline = pd.read_csv(
        "woData/wostrategy/race_performance_review/schema_v1/year=2026/round=11/session=R/"
        "race_performance_2026_11_R_clean_laps.csv"
    )
    raw = pd.read_pickle(
        "woData/wostrategy/planner_race_laps/schema_v2/year=2026/round=11/session=R/laps.pkl"
    )
    keys = ["Driver", "LapNumber"]
    for frame in (live, offline, raw):
        frame["LapNumber"] = pd.to_numeric(frame.LapNumber, errors="coerce").astype("Int64")
        frame.drop_duplicates(keys, inplace=True)
    traffic = _live_traffic_gaps(snapshot, force_sector=True)
    traffic["LapNumber"] = pd.to_numeric(traffic.LapNumber, errors="coerce").astype("Int64")
    raw = raw.merge(traffic, on=keys, how="left", suffixes=("", "_live"))
    if pd.api.types.is_timedelta64_dtype(raw.LapTime):
        raw["raw_lap_time_s"] = raw.LapTime.dt.total_seconds()
    else:
        raw["raw_lap_time_s"] = pd.to_timedelta(raw.LapTime).dt.total_seconds()
    valid_fast = raw.PitOutTime.isna() & raw.PitInTime.isna()
    fastest_by_driver = raw.loc[valid_fast].groupby("Driver").raw_lap_time_s.min()
    raw["live_quick"] = raw.raw_lap_time_s <= 1.1 * raw.Driver.map(fastest_by_driver)
    key_status = raw.merge(live[keys].assign(live_included=True), on=keys, how="left")
    key_status = key_status.merge(offline[keys].assign(offline_included=True), on=keys, how="left")
    key_status["live_included"] = key_status.live_included.eq(True)
    key_status["offline_included"] = key_status.offline_included.eq(True)
    key_status["classification"] = np.select(
        [key_status.live_included & key_status.offline_included,
         ~key_status.live_included & key_status.offline_included,
         key_status.live_included & ~key_status.offline_included],
        ["included_by_both", "offline_only", "live_only"], default="excluded_by_both"
    )
    dry = key_status.Compound.astype(str).str.upper().isin(("SOFT", "MEDIUM", "HARD"))
    valid_metadata = key_status.Compound.notna() & key_status.TyreLife.notna() & key_status.Stint.notna()
    green = key_status.TrackStatus.astype(str).str.strip().eq("1")
    pit = key_status.PitOutTime.notna() | key_status.PitInTime.notna()
    live_ahead = pd.to_numeric(key_status.TrafficGapAheadSeconds, errors="coerce")
    live_behind = pd.to_numeric(key_status.TrafficGapBehindSeconds, errors="coerce")
    key_status["reason"] = "included"
    excluded = ~key_status.live_included
    key_status.loc[excluded & ~valid_metadata, "reason"] = "missing_tyre_or_stint_metadata"
    key_status.loc[excluded & valid_metadata & ~dry, "reason"] = "non_dry_compound"
    key_status.loc[excluded & valid_metadata & dry & pit, "reason"] = "pit_in_or_out"
    key_status.loc[excluded & valid_metadata & dry & ~pit & ~green, "reason"] = "non_green_track_status"
    key_status.loc[excluded & valid_metadata & dry & ~pit & green & ~key_status.live_quick,
                   "reason"] = "quick_lap_threshold"
    base = excluded & valid_metadata & dry & ~pit & green & key_status.live_quick
    key_status.loc[base & (live_ahead.isna() | live_behind.isna()), "reason"] = "missing_live_traffic_gap"
    key_status.loc[base & live_ahead.notna() & live_behind.notna()
                   & ((live_ahead < 2.5) | (live_behind < 1.0)), "reason"] = "live_traffic_gap"
    key_status.loc[base & live_ahead.ge(2.5) & live_behind.ge(1.0),
                   "reason"] = "consecutive_clean_run_or_other"
    # Live-only laps expose the inverse mismatch: offline telemetry/filter excluded
    # them, but the persisted artifact does not retain excluded-lap reason flags.
    key_status.loc[key_status.live_included & ~key_status.offline_included,
                   "reason"] = "offline_filter_exclusion_reason_not_persisted"
    columns = keys + ["classification", "reason", "Compound", "Stint", "TyreLife",
                      "TrackStatus", "live_quick", "TrafficGapAheadSeconds",
                      "TrafficGapBehindSeconds"]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    key_status[columns].sort_values(keys).to_csv(OUTPUT / "live_offline_clean_lap_diff.csv", index=False)
    summary = key_status.groupby(["classification", "reason"]).size().reset_index(name="lap_count")
    summary.to_csv(OUTPUT / "clean_lap_exclusion_reason_summary.csv", index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
