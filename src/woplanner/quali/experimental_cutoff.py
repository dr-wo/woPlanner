"""Read-only presentation calculation for the frozen research ``half_live`` candidate.

Consumes one already published snapshot; never fits, selects production sources,
or writes a result back to the service, policy, or snapshot contracts.
"""

from dataclasses import dataclass
from math import isfinite

from .models import QualiSessionSnapshot


@dataclass(frozen=True)
class ExperimentalCutoff:
    projected_cutoff_s: float | None
    rate_s_per_field_push: float | None
    correction_s: float | None
    source: str
    status: str
    reason: str
    candidate: str = "half_live"


def _finite(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)


def _unavailable(reason: str) -> ExperimentalCutoff:
    return ExperimentalCutoff(None, None, None, "UNAVAILABLE", "UNAVAILABLE", reason)


def calculate_half_live(snapshot: QualiSessionSnapshot) -> ExperimentalCutoff:
    """Reproduce research/evaluate.py using its canonical global field-PUSH rate.

    A numeric live rate (including zero and LIVE_LIMITED) is halved. Otherwise
    use the configured fallback unchanged. Manual overrides are never inputs.
    Target and unfinished count come verbatim from this snapshot's Current cut.
    """
    cutoff = snapshot.cutoff
    if not _finite(cutoff.current_cutoff_s):
        return _unavailable("No observed cutoff target")
    result = snapshot.track_evolution_result
    if not result or "live_estimate_s_per_global_push" not in result:
        return _unavailable("No published track estimate")
    live = result["live_estimate_s_per_global_push"]
    if live is not None:
        if not _finite(live):
            return _unavailable("Invalid live estimate")
        status = result.get("status")
        if status not in {"LIVE_SUPPORTED", "LIVE_LIMITED"}:
            return _unavailable("Live estimate status unavailable")
        rate = 0.5 * live
        source = "LIVE_PER_DRIVER"
        reason = "Half of the canonical session-wide live fit; manual override excluded"
    else:
        rate = result.get("configured_fallback_s_per_global_push")
        if not _finite(rate):
            return _unavailable("No valid configured fallback")
        status = "FALLBACK"
        source = "CONFIGURED_FP_DEFAULT_PRIOR"
        reason = "No live fit; configured fallback unchanged (not a live R&D estimate)"
    unfinished = cutoff.unfinished_driver_count
    if not isinstance(unfinished, int) or isinstance(unfinished, bool) or unfinished < 0:
        return _unavailable("Invalid unfinished-driver count")
    correction = rate * unfinished
    projected = cutoff.current_cutoff_s + correction
    if not _finite(correction) or not _finite(projected):
        return _unavailable("Non-finite projection")
    return ExperimentalCutoff(
        min(projected, cutoff.current_cutoff_s), rate, correction, source, status, reason,
    )
