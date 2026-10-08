"""Operational Live sensitivity applied after normal tyre-input resolution.

The fitted model, canonical Tyre Inputs and editable Used values stay in their
original coordinates. Stored calculation snapshots contain both vectors so a
Live history calculation can reproduce the final transform without refitting.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from wodata.contracts import canonical_hash

from .tyre_inputs import ResolvedTyreInputs, tyre_snapshot_from_overrides


def validate_degradation_sensitivity(value: float) -> float:
    factor = float(value)
    if not math.isfinite(factor) or factor < 0:
        raise ValueError("Live degradation sensitivity must be finite and nonnegative.")
    return factor


def apply_live_degradation_sensitivity(
    effective: Mapping[tuple[str, str], float], factor: float,
) -> dict[tuple[str, str], float]:
    """Multiply the final effective degradation only, never the source vector."""
    factor = validate_degradation_sensitivity(factor)
    if factor == 1.0:
        return dict(effective)
    return {
        coordinate: float(value) * factor if coordinate[0] == "degradation" else value
        for coordinate, value in effective.items()
    }


def live_calculation_tyre_inputs(
    effective: Mapping[tuple[str, str], float],
    resolved: ResolvedTyreInputs,
    *,
    factor: float,
    assumptions: Sequence[Mapping[str, object]],
    manual_values: Mapping[tuple[str, str], float],
) -> tuple[dict[tuple[str, str], float], dict[str, object]]:
    """Freeze source provenance and the exact final Live search input vector."""
    final = apply_live_degradation_sensitivity(effective, factor)
    snapshot = tyre_snapshot_from_overrides(
        effective, resolved, assumptions=assumptions, manual_values=manual_values,
    )
    base_values = dict(snapshot["values"])
    snapshot["values"] = {
        name: float(final[("degradation", name.removeprefix("Deg_"))])
        if name.startswith("Deg_") else value
        for name, value in base_values.items()
    }
    snapshot["live_degradation_sensitivity"] = {
        "schema_version": 1,
        "factor": validate_degradation_sensitivity(factor),
        "application": "final_effective_degradation_only",
        "base_values": base_values,
        "source_provenance": "selected_sources describe the pre-sensitivity effective inputs",
    }
    snapshot["content_hash"] = canonical_hash({
        key: value for key, value in snapshot.items()
        if key not in {"content_hash", "captured_at", "resolved_content_hash"}
    })
    return final, snapshot


def replay_live_tyre_inputs(snapshot: Mapping[str, object]) -> dict[tuple[str, str], float]:
    """Recreate search inputs from the saved base vector and Live factor.

    Legacy snapshots have no operational transform and retain their values.
    A transformed snapshot must agree with its stored final vector.
    """
    sensitivity = snapshot.get("live_degradation_sensitivity")
    values = dict(snapshot["values"])
    base = dict(sensitivity["base_values"]) if sensitivity is not None else values
    effective = {
        ("degradation", compound): float(base[f"Deg_{compound}"])
        for compound in ("HARD", "MEDIUM", "SOFT")
    }
    effective.update({
        ("compound_delta", "MEDIUM"): 0.0,
        ("compound_delta", "HARD"): float(base["Perf_HARD_vs_MEDIUM"]),
        ("compound_delta", "SOFT"): float(base["Perf_SOFT_vs_MEDIUM"]),
    })
    final = apply_live_degradation_sensitivity(
        effective, sensitivity["factor"] if sensitivity is not None else 1.0,
    )
    for name, value in values.items():
        coordinate = ("degradation", name.removeprefix("Deg_")) if name.startswith("Deg_") else (
            "compound_delta", name.split("_")[1]
        )
        if float(final[coordinate]) != float(value):
            raise ValueError("Live tyre snapshot final values disagree with its sensitivity provenance.")
    return final
