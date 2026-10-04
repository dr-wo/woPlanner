from __future__ import annotations

import pandas as pd

from .formatting import tyre_age_label


def build_pit_window_data(
    selected_driver_delta: pd.DataFrame,
    *,
    normal_pit_loss_s: float | None,
    sc_pit_loss_s: float | None = None,
    vsc_pit_loss_s: float | None = None,
    sc_vsc_pit_loss_s: float | None = None,
) -> pd.DataFrame:
    """Build mode-independent pit-loss boundaries around a driver's delta."""
    result_columns = [
        "lap", "sector", "segment_index", "x_lap",
        "normal_lower_boundary_s", "vsc_lower_boundary_s",
        "vsc_upper_boundary_s", "sc_lower_boundary_s",
        "sc_upper_boundary_s", "legacy_sc_vsc_lower_boundary_s",
        "legacy_sc_vsc_upper_boundary_s", "normal_upper_boundary_s",
    ]
    if selected_driver_delta.empty:
        return pd.DataFrame(columns=result_columns)

    source_columns = [
        column for column in ("lap", "sector", "segment_index", "x_lap")
        if column in selected_driver_delta.columns
    ]
    output = selected_driver_delta.loc[:, [*source_columns, "delta_s"]].copy()
    for state, value in (
        ("normal", normal_pit_loss_s),
        ("sc", sc_pit_loss_s),
        ("vsc", vsc_pit_loss_s),
        ("legacy_sc_vsc", sc_vsc_pit_loss_s),
    ):
        if value is None:
            continue
        output[f"{state}_lower_boundary_s"] = output["delta_s"] - float(value)
        output[f"{state}_upper_boundary_s"] = output["delta_s"] + float(value)
    return output.loc[:, [column for column in result_columns if column in output.columns]]


def build_tyre_event_data(plot_data: pd.DataFrame) -> pd.DataFrame:
    """Extract actual and simulated tyre changes from either planner mode."""
    required = {"driver", "lap", "delta_s", "compound", "stint", "tyre_age"}
    if required.difference(plot_data.columns):
        return empty_tyre_event_data()

    events: list[pd.DataFrame] = []
    is_simulated = (
        plot_data["is_simulated"].fillna(False).astype(bool)
        if "is_simulated" in plot_data.columns
        else pd.Series(False, index=plot_data.index)
    )
    plot_role = (
        plot_data["plot_role"].astype(str)
        if "plot_role" in plot_data.columns
        else pd.Series("actual", index=plot_data.index)
    )
    actual_data = plot_data.loc[~is_simulated & plot_role.ne("simulation")].copy()
    for _, driver_laps in actual_data.sort_values(
        ["driver", "lap"], kind="mergesort"
    ).groupby("driver", sort=False):
        driver_laps = driver_laps.dropna(subset=["lap", "delta_s"]).copy()
        if driver_laps.empty:
            continue
        previous_stint = driver_laps["stint"].shift(1)
        previous_compound = driver_laps["compound"].shift(1)
        event_mask = (
            driver_laps["stint"].ne(previous_stint)
            | driver_laps["compound"].ne(previous_compound)
        ) & previous_stint.notna()
        if "pit_out" in driver_laps.columns:
            pit_out = driver_laps["pit_out"].fillna(False).astype(bool) & previous_stint.notna()
            if "sector" in driver_laps.columns:
                pit_out &= driver_laps["sector"].astype(str).eq("S1")
            event_mask |= pit_out
        events.append(driver_laps.loc[event_mask])

    simulated_data = plot_data.loc[is_simulated].copy()
    if not simulated_data.empty and "scenario_id" in simulated_data.columns:
        simulated_data = simulated_data.dropna(subset=["lap", "delta_s"]).sort_values(
            ["scenario_id", "driver", "lap"], kind="mergesort"
        )
        if "pit_out" in simulated_data.columns:
            simulated_mask = simulated_data["pit_out"].fillna(False).astype(bool)
            if "sector" in simulated_data.columns:
                simulated_mask &= simulated_data["sector"].astype(str).eq("S1")
            simulated_events = simulated_data.loc[simulated_mask].copy()
        else:
            simulated_events = simulated_data.groupby(
                "scenario_id", as_index=False, sort=False
            ).first()
        if not simulated_events.empty:
            events.append(simulated_events)

    if not events:
        return empty_tyre_event_data()
    output = pd.concat(events, ignore_index=True).dropna(subset=["lap", "delta_s"])
    if output.empty:
        return empty_tyre_event_data()
    output["tyre_age_label"] = output["tyre_age"].map(tyre_age_label)
    defaults = {
        "scenario_id": pd.NA, "is_simulated": False, "is_faded": False,
        "stop_after_lap": pd.NA, "sector": pd.NA, "segment_index": pd.NA,
        # Preserve the curve's canonical coordinate, including unequal sector
        # fractions. Lap-only curves use the same lap fallback as their markers.
        "x_lap": output["lap"],
    }
    for column, default in defaults.items():
        if column not in output.columns:
            output[column] = default
    return output.loc[:, empty_tyre_event_data().columns]


def empty_tyre_event_data() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "driver", "lap", "sector", "segment_index", "x_lap", "delta_s", "compound",
        "tyre_age_label", "scenario_id", "is_simulated", "is_faded",
        "stop_after_lap",
    ])
