from __future__ import annotations

import pandas as pd


def collapse_cumulative_time_gaps(
    rows: pd.DataFrame,
    *,
    driver_column: str,
    order_column: str,
    cumulative_column: str,
    duration_column: str,
    eligible_gap: pd.Series | None = None,
    minimum_gap_s: float = 5.0,
    order_step: int = 1,
) -> pd.Series:
    """Remove inactive gaps from an otherwise absolute cumulative timeline.

    A suspension appears as time between two consecutive timing segments that
    is not part of the latter segment's measured duration.  Removing that
    excess preserves normal race gaps and the relative timing at the first
    observed segment while keeping subsequent simulated and observed times on
    the same racing-time axis.
    """
    corrected = pd.Series(float("nan"), index=rows.index, dtype="float64")
    if rows.empty:
        return corrected

    allowed = (
        pd.Series(True, index=rows.index, dtype=bool)
        if eligible_gap is None
        else eligible_gap.reindex(rows.index).fillna(False).astype(bool)
    )
    for _, group in rows.groupby(driver_column, sort=False, dropna=False):
        ordered = group.sort_values(order_column, kind="mergesort")
        cumulative = pd.to_numeric(ordered[cumulative_column], errors="coerce")
        duration = pd.to_numeric(ordered[duration_column], errors="coerce")
        order = pd.to_numeric(ordered[order_column], errors="coerce")
        excess = cumulative.diff() - duration
        is_gap = (
            order.diff().eq(int(order_step))
            & cumulative.notna()
            & cumulative.shift().notna()
            & duration.notna()
            & excess.gt(float(minimum_gap_s))
            & allowed.loc[ordered.index]
        )
        removed = excess.where(is_gap, 0.0).cumsum()
        corrected.loc[ordered.index] = cumulative - removed
    return corrected
