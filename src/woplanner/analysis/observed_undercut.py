"""Observed full-lap pace around racing stops, separate from MC interpretation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Mapping


@dataclass(frozen=True)
class MCTrackInterpretation:
    status: str = "unavailable"
    reason: str | None = "No current live Retro model"
    analysis_id: str | None = None
    sample_bank_id: str | None = None
    architecture: str | None = None
    as_of_leader_lap: int | None = None
    created_at: str | None = None
    input_fingerprint: str | None = None
    config_hash: str | None = None
    track_rate_s_per_lap: float | None = None
    race_lap_reference: float | None = None
    pre_track_term_s: float | None = None
    post_track_term_s: float | None = None
    track_term_change_s: float | None = None
    formula: str = "track_rate * (LapNumber - RaceLapRef)"
    sign: str = "Positive change means the later reference is modelled slower"
    identifiability: str | None = None


@dataclass(frozen=True)
class S2LapEquivalentDiagnostic:
    status: str = "unavailable"
    reason: str | None = "Missing full-lap reference pair"
    pre_sector_duration_s: float | None = None
    post_sector_duration_s: float | None = None
    raw_delta_s: float | None = None
    scale_factor: float | None = None
    lap_equivalent_delta_s: float | None = None
    formula: str = "(pre_S2 - post_S2) * 0.5 * (pre_lap / pre_S2 + post_lap / post_S2)"
    duration_source: str = "unmodelled CompletedSector.sector_time_s (S2)"
    reference_selection: str = "Same selected pre/post clean full-lap references"
    sign: str = "Positive means the post-stop S2 was faster; auxiliary extrapolation"


@dataclass(frozen=True)
class ObservedUndercutEstimate:
    driver: str
    driver_number: str
    racing_pit_stop_id: int
    pit_in_lap: int
    pit_out_lap: int
    old_stint: int
    replacement_stint: int
    old_compound: str | None
    replacement_compound: str | None
    status: str
    pending_reason: str | None
    pre_reference_lap: int | None
    post_reference_lap: int | None
    pre_lap_duration_s: float | None
    post_lap_duration_s: float | None
    reference_separation: int | None
    observed_pace_delta_s: float | None
    track: MCTrackInterpretation
    clean_lap_settings: tuple[tuple[str, object], ...]
    duration_source: str = "unmodelled CompletedLap.lap_time_s"
    observation_sign: str = "Positive means the post-stop clean lap was faster"
    s2: S2LapEquivalentDiagnostic = field(default_factory=S2LapEquivalentDiagnostic)

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def _finite(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mapping(value) -> Mapping:
    return dict(value) if isinstance(value, (Mapping, tuple, list)) else {}


def _s2_lap_equivalent(pre, post, sectors_by_lap) -> S2LapEquivalentDiagnostic:
    """Extrapolate S2 using the mean of both reference laps' lap/S2 ratios.

    Never select another lap or stop when a chosen reference lacks usable S2.
    Equal weighting makes the scale invariant to exchanging pre and post.
    """
    durations = []
    missing = []
    for side, lap in (("pre-stop", pre), ("post-stop", post)):
        candidates = sectors_by_lap.get((lap.driver_number, lap.lap_number), ()) if lap else ()
        sector = candidates[0] if len(candidates) == 1 else None
        value = _finite(sector.sector_time_s) if sector else None
        if (lap is None or sector is None or value is None
                or isinstance(sector.sector_time_s, bool) or not 0 < value < lap.lap_time_s
                or sector.driver_code != lap.driver_code
                or (sector.stint is not None and sector.stint != lap.stint)):
            value = None
            missing.append(side)
        durations.append(value)
    evidence = dict(pre_sector_duration_s=durations[0], post_sector_duration_s=durations[1])
    if missing:
        return S2LapEquivalentDiagnostic(
            reason="Missing or unusable " + " and ".join(missing) + " reference S2",
            **evidence,
        )
    pre_s2, post_s2 = durations
    raw = pre_s2 - post_s2
    scale = 0.5 * (pre.lap_time_s / pre_s2 + post.lap_time_s / post_s2)
    equivalent = raw * scale
    if _finite(scale) is None or _finite(equivalent) is None:
        return S2LapEquivalentDiagnostic(reason="Non-finite S2 scaling result", **evidence)
    return S2LapEquivalentDiagnostic(
        status="available", reason=None, raw_delta_s=raw, scale_factor=scale,
        lap_equivalent_delta_s=equivalent, **evidence,
    )


def interpret_mc_track(model_summary, pre_lap: int | None, post_lap: int | None):
    """Evaluate the current Retro median's two terms using one model reference.

    This exposes a fitted coordinate, never a correction to the observation or
    an independently measured track effect. No marginal intervals are combined.
    """
    if model_summary is None:
        return MCTrackInterpretation()
    manifest = model_summary.manifest
    semantics = _mapping(manifest.get("semantics"))
    architecture = str(semantics.get("architecture") or
                       _mapping(manifest.get("sampler_quality")).get("architecture") or "")
    provenance = dict(
        analysis_id=manifest.get("analysis_id"),
        sample_bank_id=manifest.get("sample_bank_id"),
        architecture=architecture or None,
        as_of_leader_lap=manifest.get("as_of_leader_lap"),
        created_at=manifest.get("created_at"),
        input_fingerprint=manifest.get("input_fingerprint"),
        config_hash=manifest.get("config_hash"),
    )
    if architecture != "live-retro-v1":
        return MCTrackInterpretation(reason="Unsupported MC architecture", **provenance)
    if str(manifest.get("status") or "").upper() != "UPDATED":
        return MCTrackInterpretation(reason="Current MC model is unavailable or stale", **provenance)
    if (_finite(manifest.get("as_of_leader_lap")) or 0) <= 0:
        return MCTrackInterpretation(reason="No fitted live Race snapshot", **provenance)
    quantity = _mapping(_mapping(semantics.get("quantities")).get("track_rate"))
    reference = _finite(quantity.get("reference_value"))
    if (quantity.get("formula_term") != MCTrackInterpretation.formula
            or quantity.get("independent_variable") != "LapNumber" or reference is None):
        return MCTrackInterpretation(reason="MC track reference/semantics unavailable", **provenance)
    rows = [row for row in model_summary.parameters
            if row.get("parameter") == "track_rate" and row.get("session") == "R"
            and not row.get("team") and not row.get("compound")
            and row.get("analysis_id") == manifest.get("analysis_id")
            and row.get("sample_bank_id") == manifest.get("sample_bank_id")]
    rate = _finite(rows[0].get("median")) if len(rows) == 1 else None
    if rate is None:
        return MCTrackInterpretation(reason="Current MC track rate unavailable", **provenance)
    provenance.update(track_rate_s_per_lap=rate, race_lap_reference=reference,
                      identifiability=str(_mapping(
                          semantics.get("fuel_track_identifiability")).get("status") or ""))
    if pre_lap is None or post_lap is None:
        return MCTrackInterpretation(reason="Reference pair pending", **provenance)
    pre = rate * (pre_lap - reference)
    post = rate * (post_lap - reference)
    return MCTrackInterpretation(
        status="available", reason=None, pre_track_term_s=pre,
        post_track_term_s=post, track_term_change_s=rate * (post_lap - pre_lap),
        **provenance,
    )


def build_observed_undercut_estimates(
    snapshot, *, model_summary=None, quick_lap_threshold: float = 1.07,
    allowed_compounds: tuple[str, ...] = ("SOFT", "MEDIUM", "HARD"),
    use_sector_boundary_traffic: bool = False,
    min_gap_ahead_seconds: float = 2.5, min_gap_behind_seconds: float = 1.0,
) -> tuple[ObservedUndercutEstimate, ...]:
    """Select nearest individual clean full laps in the adjacent stop stints.

    Existing inferred pit flags are insufficient: the additive Data visit ID
    must certify explicit racing pit entry and exit. Old snapshots without that
    evidence are safely unavailable. MC minimum-run and edge rules do not apply.
    """
    from .service import _live_individual_clean_laps
    from wodata.live.session_identity import parse_session_identity

    session_name = parse_session_identity(
        getattr(snapshot, "session_info", {})
    ).session_name.upper()
    if session_name and session_name not in {"R", "RACE", "S", "SPRINT"}:
        return ()

    settings = dict(
        quick_lap_threshold=float(quick_lap_threshold),
        allowed_compounds=tuple(allowed_compounds),
        use_sector_boundary_traffic=bool(use_sector_boundary_traffic),
        min_gap_ahead_seconds=float(min_gap_ahead_seconds),
        min_gap_behind_seconds=float(min_gap_behind_seconds),
    )
    prepared = _live_individual_clean_laps(snapshot, **settings)
    clean_keys = set()
    if not prepared.empty:
        clean_keys = {(str(row.Driver), int(row.LapNumber)) for row in
                      prepared.loc[prepared["IsCleanAirLongRunLap"]].itertuples()}
    by_driver = {}
    for lap in snapshot.completed_laps:
        by_driver.setdefault(lap.driver_number, []).append(lap)
    sectors_by_lap = {}
    for sector in getattr(snapshot, "completed_sectors", ()):
        if str(sector.sector).upper() == "S2":
            sectors_by_lap.setdefault((sector.driver_number, sector.lap_number), []).append(sector)
    result = []
    for number, laps in sorted(by_driver.items()):
        laps = sorted(laps, key=lambda lap: lap.lap_number)
        visits = []
        for left, right in zip(laps, laps[1:]):
            visit_id = getattr(left, "racing_pit_stop_id", None)
            if (isinstance(visit_id, int) and not isinstance(visit_id, bool) and visit_id > 0
                    and visit_id == getattr(right, "racing_pit_stop_id", None)
                    and right.lap_number == left.lap_number + 1):
                visits.append((left.lap_number, right.lap_number))
        for before, after in zip(laps, laps[1:]):
            stop_id = getattr(before, "racing_pit_stop_id", None)
            if (not isinstance(stop_id, int) or isinstance(stop_id, bool) or stop_id <= 0
                    or stop_id != getattr(after, "racing_pit_stop_id", None)
                    or not before.pit_in or not after.pit_out
                    or after.lap_number != before.lap_number + 1
                    or before.stint is None or after.stint is None
                    or before.stint == after.stint):
                continue
            suitable = [lap for lap in laps if (lap.driver_code, lap.lap_number) in clean_keys
                        and not lap.pit_in and not lap.pit_out
                        and bool(lap.is_valid) and _finite(lap.lap_time_s) is not None
                        and float(lap.lap_time_s) > 0]
            # A later visit may leave the physical stint unchanged. Bound by
            # pit laps as well as stint identity so references never cross it.
            pre_floor = max((lap.lap_number for lap in laps if lap.pit_out
                             and lap.lap_number < before.lap_number), default=0)
            post_ceiling = min((lap.lap_number for lap in laps if lap.pit_in
                                and lap.lap_number > after.lap_number), default=math.inf)
            pre_floor = max(pre_floor, max((out_lap for _, out_lap in visits
                                           if out_lap < before.lap_number), default=0))
            post_ceiling = min(post_ceiling, min((in_lap for in_lap, _ in visits
                                                 if in_lap > after.lap_number), default=math.inf))
            pre = [lap for lap in suitable if lap.stint == before.stint
                   and pre_floor < lap.lap_number < before.lap_number]
            post = [lap for lap in suitable if lap.stint == after.stint
                    and after.lap_number < lap.lap_number < post_ceiling]
            pre = pre[-1] if pre else None
            post = post[0] if post else None
            pre_lap = pre.lap_number if pre else None
            post_lap = post.lap_number if post else None
            available = pre is not None and post is not None
            missing = [name for name, lap in (("pre-stop", pre), ("post-stop", post))
                       if lap is None]
            result.append(ObservedUndercutEstimate(
                before.driver_code, number, int(stop_id), before.lap_number,
                after.lap_number, before.stint, after.stint, before.compound, after.compound,
                "available" if available else "pending",
                None if available else "Missing clean " + " and ".join(missing) + " reference",
                pre_lap, post_lap, float(pre.lap_time_s) if pre else None,
                float(post.lap_time_s) if post else None,
                post_lap - pre_lap if available else None,
                float(pre.lap_time_s - post.lap_time_s) if available else None,
                interpret_mc_track(model_summary, pre_lap, post_lap), tuple(settings.items()),
                s2=_s2_lap_equivalent(pre, post, sectors_by_lap),
            ))
    return tuple(result)
