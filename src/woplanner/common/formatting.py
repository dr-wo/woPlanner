"""Small display-formatting helpers shared by planner modes."""

import math


def tyre_age_label(value: object) -> str:
    try:
        age = float(value)
    except (TypeError, ValueError):
        return "?"
    if not math.isfinite(age):
        return "?"
    return str(max(int(round(age)) - 1, 0))
