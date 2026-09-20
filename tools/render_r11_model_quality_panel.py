#!/usr/bin/env python3
"""Render the Strategy history tab with Race-redesign quality data for UI QA."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6 import QtCore, QtWidgets

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow


def main():
    root = Path("temp/r11_mc_instability_diagnostic")
    summary = pd.read_csv(root / "race_redesign_replay_summary.csv")
    parameters = pd.read_csv(root / "race_redesign_parameter_status.csv")
    seed = 1_898_758_267
    summary = summary[summary.seed == seed].set_index("leader_lap")
    rows = []
    for row in parameters[parameters.seed == seed].to_dict("records"):
        lap = int(row["leader_lap"]); compound = str(row.get("compound") or "").upper()
        if row["parameter"] not in {"degradation", "compound_delta"} or not compound:
            continue
        quality = summary.loc[lap]
        evidence = {
            name: {
                "status": quality[f"{name.lower()}_status"],
                "clean_laps": int(quality[f"{name.lower()}_clean_laps"]),
                "clean_runs": int(quality[f"{name.lower()}_clean_runs"]),
                "tyre_age_span": quality[f"{name.lower()}_tyre_age_span"],
            } for name in ("HARD", "MEDIUM", "SOFT")
        }
        rows.append({
            "as_of_leader_lap": lap, "source_scope": "aggregate",
            "parameter": row["parameter"], "compound": compound,
            "median": row["median"], "p10": row["p10"], "p90": row["p90"],
            "p10_p90_width": row["p10_p90_width"], "sampler_ess": quality["ess"],
            "sample_count": int(quality["sample_count"]),
            "ess_fraction": quality["ess_fraction"], "max_weight": quality["max_weight"],
            "top5_weight": quality["top5_weight"], "clean_race_laps": int(quality["clean_race_laps"]),
            "clean_race_runs": int(quality["clean_race_runs"]),
            "compound_evidence": evidence, "numerical_quality_status": "adequate",
        })
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(
        year=2026,
        round_number=11,
        mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        persistence_mode="temporary",
    )
    window.tabs.setCurrentWidget(window.history_tab)
    window._show_plot_rows(rows)
    window.resize(1500, 1000)
    window.show()
    app.processEvents()
    destination = root / "model_quality_panel.png"
    window.history_tab.grab().save(str(destination))
    window.close(); app.processEvents()
    print(destination)


if __name__ == "__main__":
    main()
