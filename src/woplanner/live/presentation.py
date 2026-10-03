"""Revisable display policy; these values are never canonical timing inputs."""
from __future__ import annotations

from itertools import combinations
import math

import pandas as pd


OFFICIAL_GAP_MAX_AGE_S = 5.0
OFFICIAL_GAP_PAIR_WINDOW_S = 2.0
ORDER_TOLERANCE_S = 0.1


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def guard_provisional_order(
    plot_data: pd.DataFrame, live_timing, *, reference_driver: str | None = None
) -> pd.DataFrame:
    """Prefer fresh official gaps over speculative current-region display points.

    Only each driver's newest point at a shared sector coordinate participates.
    Official gap samples must postdate those points, be at most five source-clock
    seconds old, and be within two seconds of each other. These are presentation
    freshness limits, not assumptions about canonical lap identity.
    """
    output = plot_data.copy()
    output["display_reason"] = ""
    required = {"driver", "segment_index", "source_time_s", "timing_state", "delta_s"}
    if output.empty or required.difference(output.columns):
        return output
    if not output["timing_state"].eq("provisional").any():
        return output
    latest = output.sort_values(
        ["driver", "segment_index", "source_time_s"], kind="mergesort"
    ).groupby("driver", sort=False).tail(1)
    # Before a full pace reference exists, compare only the latest provisional
    # region on the common publication clock. This estimate can move or vanish
    # on the next snapshot; it does not fill the missing historical reference.
    reference_rows = latest.loc[latest["driver"].eq(reference_driver)]
    if not reference_rows.empty:
        reference_row = reference_rows.iloc[-1]
        reference_cumulative = _finite(reference_row.get("cumulative_time_s"))
        historical_reference = output.loc[
            output["driver"].eq(reference_driver)
            & output["timing_state"].eq("committed")
        ].sort_values("segment_index")
        offsets = [
            value for raw in historical_reference["delta_s"]
            if (value := _finite(raw)) is not None
        ]
        offset = offsets[-1] if offsets else 0.0
        for index, row in latest.iterrows():
            if row["timing_state"] != "provisional" or _finite(row["delta_s"]) is not None:
                continue
            cumulative = _finite(row.get("cumulative_time_s"))
            if (
                reference_cumulative is None or cumulative is None
                or row["segment_index"] != reference_row["segment_index"]
            ):
                continue
            output.at[index, "delta_s"] = offset + cumulative - reference_cumulative
            output.at[index, "timing_available"] = True
            output.at[index, "display_reason"] = "provisional_reference"

    states = {state.driver_code: state for state in live_timing}
    clocks = [
        clock for state in states.values()
        if (clock := _finite(state.latest_source_time_s)) is not None
    ]
    if not clocks:
        return output
    now = max(clocks)

    def official(row):
        state = states.get(str(row["driver"]))
        if state is None:
            return None
        gap = _finite(state.gap_to_leader_s)
        stamp = _finite(state.gap_source_time_s)
        observed = _finite(row["source_time_s"])
        if gap is None or gap < 0 or stamp is None or observed is None:
            return None
        if stamp < observed or not 0 <= now - stamp <= OFFICIAL_GAP_MAX_AGE_S:
            return None
        return gap, stamp

    suppressed = set()
    for _, points in latest.groupby("segment_index", sort=False):
        for (left_index, left), (right_index, right) in combinations(points.iterrows(), 2):
            if left["timing_state"] != "provisional" and right["timing_state"] != "provisional":
                continue
            left_gap, right_gap = official(left), official(right)
            left_delta = _finite(output.at[left_index, "delta_s"])
            right_delta = _finite(output.at[right_index, "delta_s"])
            if (
                left_gap is None or right_gap is None
                or left_delta is None or right_delta is None
                or abs(left_gap[1] - right_gap[1]) > OFFICIAL_GAP_PAIR_WINDOW_S
            ):
                continue
            official_difference = left_gap[0] - right_gap[0]
            displayed_difference = left_delta - right_delta
            if (
                abs(official_difference) > ORDER_TOLERANCE_S
                and abs(displayed_difference) > ORDER_TOLERANCE_S
                and official_difference * displayed_difference < 0
            ):
                for index, row in ((left_index, left), (right_index, right)):
                    if row["timing_state"] == "provisional":
                        suppressed.add(index)
    for index in suppressed:
        output.at[index, "delta_s"] = float("nan")
        output.at[index, "timing_available"] = False
        output.at[index, "display_reason"] = "fresh_official_order_conflict"
    return output
