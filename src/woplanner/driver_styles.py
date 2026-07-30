from __future__ import annotations

import pandas as pd

from wodata.colours import F1_TEAM_COLORS as TEAM_COLOURS

FALLBACK_COLOURS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def assign_driver_styles(drivers: pd.DataFrame) -> pd.DataFrame:
    output = drivers.copy()
    team_colour_lookup: dict[str, str] = {}
    fallback_index = 0
    for team in output["team"].fillna("").astype(str).unique():
        if team in TEAM_COLOURS:
            team_colour_lookup[team] = TEAM_COLOURS[team]
        else:
            team_colour_lookup[team] = FALLBACK_COLOURS[fallback_index % len(FALLBACK_COLOURS)]
            fallback_index += 1
    output["colour"] = output["team"].fillna("").astype(str).map(team_colour_lookup)
    output["line_style"] = "solid"
    for _, team_rows in output.groupby("team", sort=False, dropna=False):
        for offset, row_index in enumerate(team_rows.index):
            output.loc[row_index, "line_style"] = "solid" if offset == 0 else "dash"
    return output
