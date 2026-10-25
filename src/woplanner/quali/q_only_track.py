"""Qualifying-only pace admission, separate from operational lap states.

This path reuses the established first-fresh-installation/per-driver estimator.
It never writes FAST/SLOW, effective-finish inputs, the legacy correction, or an
FP reference. An accepted pace observation need not yet be tyre-comparable or
provide an identifiable driver slope.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
import math

from .track_evolution_policy import TrackEvolutionPolicy, THRESHOLDS
from .track_evolution import build_track_evolution_diagnostic


def q_lap_quality(raw, event, field_reference_s):
    """Return an auditable, non-circular timing-only admission decision."""
    if event is None or event.classification != "normal_timed":
        return "UNIDENTIFIED_VALID_COMPLETION"
    if not raw.driver_number or not raw.driver_code:
        return "DRIVER_IDENTITY_UNAVAILABLE"
    if not raw.timed_lap_eligible or raw.in_pit or raw.out_lap_confirmed:
        return "NOT_ELIGIBLE_TIMED_LAP"
    value = event.lap_time_s
    if value is None or not math.isfinite(value) or value <= 0:
        return "INVALID_LAP_TIME"
    sectors = event.sectors_s
    if len(sectors) != 3 or any(
        s is None or not math.isfinite(s) or s <= 0 for s in sectors
    ):
        return "INCOMPLETE_OWNED_SECTORS"
    if not math.isclose(sum(sectors), value, rel_tol=0., abs_tol=.01):
        return "SECTOR_TOTAL_MISMATCH"
    if (field_reference_s is not None and math.isfinite(field_reference_s)
            and field_reference_s > 0
            and value > field_reference_s * (1. + THRESHOLDS["gross_fraction"])):
        return "GROSS_SANITY_REJECTED"
    return None


class QOnlyTrackEvolution:
    """One session's causal Q-only ledger and shared regression policy."""

    def __init__(self):
        self._policy = TrackEvolutionPolicy(diagnostic_builder=build_track_evolution_diagnostic)
        self._seen_completion_keys = set()
        self._context_rejections = Counter()
        self._pending_sectors = OrderedDict()
        self._sector_enrichment_admissions = 0

    def reset(self):
        self._policy.reset()
        self._seen_completion_keys.clear()
        self._context_rejections.clear()
        self._pending_sectors.clear()
        self._sector_enrichment_admissions = 0

    def begin_publication(self, snapshot):
        self._policy.begin_publication(snapshot)

    def consume_new_completions(self, snapshot, active_numbers):
        """Use exact completion events, not the operational counter/FAST gate."""
        drivers = {driver.driver_number: driver for driver in snapshot.drivers}
        references = [driver.best_lap_s for driver in snapshot.drivers
                      if driver.driver_number in active_numbers
                      and driver.best_lap_s is not None
                      and math.isfinite(driver.best_lap_s) and driver.best_lap_s > 0]
        field_reference = min(references, default=None)
        events_by_key = {getattr(event, "event_key", None): event
                         for event in snapshot.completed_lap_sequence
                         if getattr(event, "event_key", None) is not None}
        for key, (original, reference) in list(self._pending_sectors.items()):
            if key in self._policy.revoked:
                del self._pending_sectors[key]
                self._context_rejections["EXPLICIT_COMPLETION_INVALIDATION"] += 1
                continue
            if key[0] != snapshot.session_part:
                del self._pending_sectors[key]
                self._context_rejections["SEGMENT_ENDED_WITH_INCOMPLETE_SECTORS"] += 1
                continue
            event = events_by_key.get(key)
            if event is None or q_lap_quality(original, event, reference) == "INCOMPLETE_OWNED_SECTORS":
                continue
            del self._pending_sectors[key]
            before = len(self._policy.samples)
            # Only the event's owned sector evidence may be enriched. Tyres,
            # physical eligibility and the reference remain as first received.
            self.consider(original, snapshot, reference, event.lap_number)
            self._sector_enrichment_admissions += int(len(self._policy.samples) > before)
        for event in snapshot.completed_lap_sequence:
            key = getattr(event, "event_key", None)
            identity = key or (event.session_part, event.driver_number, event.lap_number,
                               getattr(event, "session_time_s", None))
            if identity in self._seen_completion_keys:
                continue
            self._seen_completion_keys.add(identity)
            if key is None:
                self._context_rejections["COMPLETION_IDENTITY_UNAVAILABLE"] += 1
                continue
            driver = drivers.get(event.driver_number)
            if (event.session_part != snapshot.session_part or driver is None
                    or event.driver_number not in active_numbers
                    or driver.last_lap_s != event.lap_time_s):
                self._context_rejections["COMPLETION_CONTEXT_UNAVAILABLE"] += 1
                continue
            self.consider(driver, snapshot, field_reference, event.lap_number)

    def consider(self, raw, snapshot, field_reference_s, lap_number):
        events = [event for event in snapshot.completed_lap_sequence
                  if event.session_part == snapshot.session_part
                  and event.driver_number == raw.driver_number
                  and event.lap_number == lap_number
                  and event.lap_time_s == raw.last_lap_s]
        event = events[0] if len(events) == 1 else None
        reason = q_lap_quality(raw, event, field_reference_s)
        if reason == "INCOMPLETE_OWNED_SECTORS":
            self._pending_sectors[event.event_key] = (raw, field_reference_s)
            if len(self._pending_sectors) > 256:
                self._pending_sectors.popitem(last=False)
                self._context_rejections["PENDING_SECTOR_CAPACITY"] += 1
            return
        # A first credible completion can establish its own timing reference;
        # sector ownership/eligibility still apply, and no fitted slope is used.
        reference = field_reference_s
        if reference is None or not math.isfinite(reference) or reference <= 0:
            reference = event.lap_time_s if event is not None else None
        before = len(self._policy.decisions)
        # The shared policy's historical boolean gate is supplied with Q-only
        # quality here. Operational FAST is deliberately not an input.
        self._policy.consider(raw, snapshot, reason is None, reference, lap_number)
        if len(self._policy.decisions) > before:
            decision = self._policy.decisions[-1]
            decision["analysis_eligible"] = reason is None
            if reason is not None:
                decision["reason"] = reason

    def finish_publication(self, service):
        # Shares exact-key deletion/retraction handling and Q1 cohort context.
        self._policy.finish_publication(service)

    @property
    def diagnostic(self):
        self._policy.refresh()
        return self._policy._diagnostic

    def payload(self, *, detailed=False):
        result = self._policy.payload(detailed=detailed)
        diagnostic = self.diagnostic
        active = {key: sample for key, sample in self._policy.samples.items()
                  if key not in self._policy.revoked}
        rejected = Counter(d["reason"] for d in self._policy.decisions
                           if not d["pace_pass"])
        rejected.update(self._context_rejections)
        live = diagnostic.aggregate_slope_s_per_push
        result.update(
            source_scope="QUALIFYING_ONLY",
            estimator_policy_version=result["policy_version"],
            policy_version="q-only-owned-sectors-field7-v1",
            estimate_source="Q_ONLY_LIVE_PER_DRIVER" if live is not None else "Q_ONLY_OBSERVATIONS",
            estimate_s_per_global_push=live,
            automatic_source="Q_ONLY_LIVE_PER_DRIVER" if live is not None else "Q_ONLY_OBSERVATIONS",
            automatic_estimate_s_per_global_push=live,
            # A configured zero is not presented as a measured Q-only slope.
            candidate_count=len(self._seen_completion_keys),
            accepted_observation_count=len(active),
            comparable_observation_count=len(diagnostic.observations),
            fitted_observation_count=diagnostic.lap_count,
            rejection_counts=dict(rejected),
            tyre_comparability_exclusions=sum(
                d["reason"] == "TYRE_INSTALL_EXCLUDED" for d in self._policy.decisions),
            pending_sector_completions=len(self._pending_sectors),
            sector_enrichment_admissions=self._sector_enrichment_admissions,
            assumption=("Complete owned sectors, timing eligibility and field+7% gross sanity; "
                        "one fresh installation and at least two comparable observations per driver for fitting. "
                        "Driver/run effects can remain confounded with evolution."),
        )
        # Do not label Q-only eligibility as an operational FAST classification.
        result["attrition"].pop("non_operational_fast_completions", None)
        result["attrition"]["quality_eligible_candidates"] = result["attrition"].pop("operational_fast_candidates")
        if detailed:
            result["admission_decisions"] = [
                {key: value for key, value in row.items() if key != "operational_fast"}
                for row in result["admission_decisions"]
            ]
        for key in ("configured_fallback_s_per_global_push", "fallback_estimate_s_per_global_push",
                    "manual_override_s_per_global_push", "fallback_provenance"):
            result.pop(key, None)
        if not detailed:
            # Publication receipts/indices belong to the detailed audit ledger;
            # duplicate deliveries must not change an unchanged compact result.
            result.pop("as_of", None)
        return result
