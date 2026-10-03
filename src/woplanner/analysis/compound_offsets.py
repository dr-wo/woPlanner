"""Reference contract for additive compound pace offsets, in seconds.

Each input vector must have one common zero point; its fitting reference may
be absent. Only differences within a vector are meaningful. Never infer a zero
entry for an unobserved fitting reference.
"""

from math import isfinite
from typing import Iterable, Mapping


REFERENCE_ORDER = ("MEDIUM", "HARD", "SOFT")
COMPOUND_OFFSET_CONTRACT = "shared_compound_alignment_v1"


def has_current_offset_contract(row: Mapping[str, object]) -> bool:
    """Resolved exports must explicitly attest to the current merge contract."""
    return row.get("compound_offset_contract") == COMPOUND_OFFSET_CONTRACT


def validate_offset_rows(
    rows: Iterable[Mapping[str, object]], *, require_provenance: bool = False
) -> None:
    """Refuse incompatible declared coordinates before existing aggregation.

    Internal callers may supply a coherent vector without reference metadata.
    Imports require a current resolved contract or an explicit raw-fit reference.
    Resolved provenance cannot fall back to a raw-fit reference when its contract
    is missing/obsolete. No zero point or legacy intent is inferred from values.
    """
    references = set()
    for row in rows:
        if row.get("parameter") != "compound_delta":
            continue
        if row.get("unit") not in (None, "", "s"):
            raise ValueError("Compound offsets must be in seconds.")
        reference = str(row.get("reference_compound") or "").upper()
        if reference:
            if reference not in (*REFERENCE_ORDER, "SUM_ZERO"):
                raise ValueError(f"Unsupported compound offset reference: {reference}")
            references.add(reference)
        if require_provenance and not has_current_offset_contract(row) and (
            not reference or "compound_delta_source" in row or "compound_offset_contract" in row
        ):
            raise ValueError(
                "Unverified imported compound offsets; resolved history requires "
                "the current compound-offset contract. Recalculate legacy values "
                "from the original model and baseline."
            )
    if len(references) > 1:
        raise ValueError("Mixed compound offset references within one source; load coherent vectors separately.")


def merge_compound_offsets(
    retained: Mapping[str, float], updated: Mapping[str, float]
) -> dict[str, float]:
    """Align on shared MEDIUM/HARD/SOFT, replace, then rebase in that order.

    The pivot defines the retained connection to untouched compounds: their
    contrasts to that pivot survive, while incoming contrasts among updated
    compounds replace the old ones. Pivot preference is a physical update policy;
    only the final whole-vector normalization is coordinate-only.
    One observed compound supplies no new contrast. Full replacements
    preserve all updated contrasts. Disconnected nonempty vectors are refused.
    The output reference is the first available MEDIUM, HARD, SOFT (value zero).
    Inputs are not mutated. This changes no slopes or posterior aggregation.
    """
    vectors = []
    for values in (retained, updated):
        vector = {}
        for compound, value in values.items():
            key = str(compound).upper()
            if key not in REFERENCE_ORDER or key in vector:
                raise ValueError(f"Unsupported or duplicate compound offset: {compound}")
            number = float(value)
            if not isfinite(number):
                raise ValueError(f"Nonfinite compound offset: {compound}")
            vector[key] = number
        vectors.append(vector)
    base, live = vectors
    if base and live:
        pivot = next((c for c in REFERENCE_ORDER if c in base and c in live), None)
        if pivot is None:
            raise ValueError("Cannot merge compound offsets without a shared compound.")
        shift = base[pivot] - live[pivot]
        base.update({c: value + shift for c, value in live.items()})
    else:
        base.update(live)
    if not base:
        return {}
    reference = next(c for c in REFERENCE_ORDER if c in base)
    zero = base[reference]
    return {c: base[c] - zero for c in REFERENCE_ORDER if c in base}
