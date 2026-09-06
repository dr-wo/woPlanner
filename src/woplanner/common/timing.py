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
    priority_column: str | None = None,
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
    candidates = []
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
        candidate = pd.DataFrame(
            {
                "order": order,
                "gap": excess,
                "cumulative": cumulative,
                "priority": (
                    pd.to_numeric(ordered[priority_column], errors="coerce")
                    if priority_column is not None and priority_column in ordered.columns
                    else float("nan")
                ),
            },
            index=ordered.index,
        ).loc[is_gap]
        if not candidate.empty:
            candidates.append(candidate)

    correction_by_order: dict[float, float] = {}
    if candidates:
        gap_candidates = pd.concat(candidates, ignore_index=True)
        gap_candidates["has_priority"] = gap_candidates["priority"].notna()
        gap_candidates = gap_candidates.sort_values(
            ["order", "has_priority", "priority", "cumulative"],
            ascending=[True, False, True, True],
            kind="mergesort",
        ).drop_duplicates("order", keep="first")
        gap_steps = dict(zip(gap_candidates["order"], gap_candidates["gap"], strict=False))
        running_correction = 0.0
        for order_value in sorted(
            pd.to_numeric(rows[order_column], errors="coerce").dropna().unique()
        ):
            running_correction += float(gap_steps.get(order_value, 0.0))
            correction_by_order[float(order_value)] = running_correction

    cumulative = pd.to_numeric(rows[cumulative_column], errors="coerce")
    order = pd.to_numeric(rows[order_column], errors="coerce")
    correction = order.map(correction_by_order).fillna(0.0)
    corrected.loc[rows.index] = cumulative - correction
    return corrected
