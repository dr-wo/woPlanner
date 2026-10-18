"""Canonical qualifying admission, exact observation lifetime and engineering quality.

Promoted from the reviewed field+7 shadow. The shared per-driver estimator owns
all fitting mathematics; physical traffic is passive, exact-key context only.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
import math
import re

from wostrategy.analysis.pre_quali import dominant_track_compound

from .track_evolution import TrackEvolutionSample, build_track_evolution_diagnostic

POLICY_VERSION = "production-field7-exact-lifetime-v2"
QUALITY_VERSION = "engineering-reliability-only-20261011-v2"
THRESHOLDS = {
    "gross_fraction": .07, "min_supported_drivers": 3,
    "min_cohort_reference": 10, "min_cohort_share": .20,
    "disagreement_sigma_s_per_push": .02, "opposing_slope_s_per_push": .005,
    "min_opposing_support_share": .25, "max_driver_share": .50,
    "jump_s_per_push": .02, "jump_push_lifetime": 10,
}
DELETION = re.compile(r"CAR (\d+) \((\w+)\) TIME (\d+):(\d{2}\.\d{3}) DELETED\b")


def positive(value):
    return value is not None and math.isfinite(value) and value > 0


def tyre_eligible(sample):
    return (sample.fresh is True and sample.tyre_install_index is not None
            and sample.compound not in {None, "UNKNOWN", ""})


def quality(diagnostic, q1_bests, *, recent_jump=False, reference_population=()):
    """Support warnings only; none of these changes admission or source choice."""
    series = diagnostic.series
    total = diagnostic.lap_count
    order = sorted((time, driver) for driver, time in q1_bests.items() if positive(time))
    cohorts = {driver: "faster" if i < math.ceil(len(order) / 2) else "slower"
               for i, (_, driver) in enumerate(order)}
    counts = Counter()
    for item in series:
        counts[cohorts.get(item.driver, "unknown")] += len(item.samples)
    share = lambda count: count / total if total else None
    largest = max((len(item.samples) for item in series), default=0)
    pos = share(sum(len(s.samples) for s in series
                    if s.slope_s_per_push > THRESHOLDS["opposing_slope_s_per_push"]))
    neg = share(sum(len(s.samples) for s in series
                    if s.slope_s_per_push < -THRESHOLDS["opposing_slope_s_per_push"]))
    minority = share(min(counts["faster"], counts["slower"]))
    reasons = []
    information = []
    missing_ranks = sorted(set(reference_population) - set(cohorts))
    incomplete = (len(order) < THRESHOLDS["min_cohort_reference"]
                  or counts["unknown"] > 0 or bool(missing_ranks))
    if total:
        if diagnostic.driver_count < THRESHOLDS["min_supported_drivers"]:
            reasons.append("SPARSE_SUPPORT")
        if share(largest) > THRESHOLDS["max_driver_share"]:
            reasons.append("DOMINATED_BY_SINGLE_DRIVER")
        if (diagnostic.weighted_slope_std_s_per_push >= THRESHOLDS["disagreement_sigma_s_per_push"]
                and min(pos, neg) >= THRESHOLDS["min_opposing_support_share"]):
            reasons.append("HIGH_DRIVER_DISAGREEMENT")
        if recent_jump:
            reasons.append("RECENT_ESTIMATE_JUMP")
        if minority < THRESHOLDS["min_cohort_share"]:
            information.append("COHORT_IMBALANCE")
    else:
        reasons.append("NO_SUPPORTED_LIVE_FIT")
    if incomplete:
        information.append("COHORT_REFERENCE_INCOMPLETE")
    return {
        "status": ("LIVE_LIMITED" if reasons else "LIVE_SUPPORTED") if total else "FALLBACK",
        "reasons": reasons, "status_reasons": list(reasons), "information": information,
        "support": {"observations": len(diagnostic.observations), "fit_observations": total,
                    "drivers": diagnostic.driver_count},
        "metrics": {
            "weighted_driver_slope_sigma_s_per_push": diagnostic.weighted_slope_std_s_per_push,
            "largest_driver_support_share": share(largest),
            "positive_support_share": pos, "negative_support_share": neg,
            "cohort_reference_count": len(order), "cohort_reference_incomplete": incomplete,
            "known_q1_population_count": len(reference_population), "missing_q1_ranks": missing_ranks,
            "cohort_support": {key: counts[key] for key in ("faster", "slower", "unknown")},
            "minority_cohort_share": minority, "q1_ordering": [driver for _, driver in order],
        },
    }


class TrackEvolutionPolicy:
    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = {}  # exact completion key -> immutable candidate sample
        self.traffic_results = {}  # exact production completion key -> canonical metadata
        self.traffic_publications = {}  # latest production finalisation record, when present
        self.completions = {}  # all published completions, including non-FAST
        self.revoked = set()
        self.decisions = []
        self.lifetime_events = []
        self._notices_seen = set()
        self._invalidations_seen = set()
        self._decisions_seen = set()
        self.global_push_index = 0
        self.publication_index = 0
        self.publication_context = {}  # optional replay message/sequence identity
        self.as_of = {}
        self.q1_bests = {}
        self.q1_population = set()
        self.q1_frozen = False
        self._dirty = True
        self._diagnostic = None
        self._previous_live = None
        self._jump_index = None
        self._jump_delta = None
        self._last_delta = None
        self._sign_flip = False
        self._prior = 0.0
        self._override = None

    def begin_publication(self, raw):
        self.publication_index += 1
        self.as_of = {
            "publication_index": self.publication_index, "part": raw.session_part,
            "published_at": raw.last_message_at_utc.isoformat() if raw.last_message_at_utc else None,
            "message_source": getattr(raw, "message_source", None), "remaining_s": raw.remaining_time_s,
            "event": dict(raw.session_info), **self.publication_context,
        }
        # Keep identities even when timing subsequently retracts its register.
        for event in raw.completed_lap_sequence:
            key = getattr(event, "event_key", None)
            if key is not None:
                self.completions.setdefault(key, (event, dict(self.as_of)))
        for evidence in getattr(raw, "traffic_evidence_sequence", ()):
            key = evidence.completion_key
            if evidence.kind == "completion_invalidated" and key is not None:
                if key not in self._invalidations_seen:
                    self._invalidations_seen.add(key)
                    self.revoke(key, "EXPLICIT_COMPLETION_INVALIDATION")
        if raw.session_part not in {"Q1", "SQ1"}:
            self.q1_frozen = True

    def consider(self, raw, snapshot, operational_fast, field_reference, lap_number):
        events = [event for event in snapshot.completed_lap_sequence
                  if event.session_part == snapshot.session_part
                  and event.driver_number == raw.driver_number and event.lap_number == lap_number
                  and event.lap_time_s == raw.last_lap_s]
        event = events[0] if len(events) == 1 else None
        key = getattr(event, "event_key", None)
        identity = key or (snapshot.session_part, raw.driver_number, lap_number, None)
        if identity in self._decisions_seen:
            return
        self._decisions_seen.add(identity)
        reason = None
        if not operational_fast:
            reason = "NOT_OPERATIONAL_FAST"
        elif (key is None or event.classification != "normal_timed"
              or not raw.driver_number or not raw.driver_code):
            reason = "UNIDENTIFIED_VALID_COMPLETION"
        elif not positive(raw.last_lap_s):
            reason = "INVALID_LAP_TIME"
        elif not positive(field_reference):
            reason = "FIELD_REFERENCE_UNAVAILABLE"
        elif raw.last_lap_s > field_reference * (1 + THRESHOLDS["gross_fraction"]):
            reason = "GROSS_SANITY_REJECTED"
        pace_pass = reason is None
        tyre = raw.tyre_history[-1] if raw.tyre_history else None
        if pace_pass:
            # Preserve research semantics: count pace passes before tyre/selection,
            # including candidates later explicitly revoked. Never renumber.
            self.global_push_index += 1
            if (tyre is None or tyre.fresh is not True or tyre.stint_index is None
                    or tyre.compound in {None, "UNKNOWN", ""}):
                reason = "TYRE_INSTALL_EXCLUDED"
            sample = TrackEvolutionSample(
                snapshot.session_part, raw.driver_code, raw.team, lap_number,
                float(raw.last_lap_s), self.global_push_index,
                tyre.compound if tyre else None, tyre.start_laps if tyre else None,
                tyre.fresh if tyre else None, None,
                traffic_status="unknown", tyre_install_index=tyre.stint_index if tyre else None,
            )
            self.samples[key] = replace(sample, **self.traffic_results.get(key, {}))
            self._dirty = True
        self.decisions.append({
            "as_of": dict(self.as_of), "completion_key": key,
            "part": snapshot.session_part, "driver": raw.driver_code,
            "driver_number": raw.driver_number, "lap_number": lap_number,
            "lap_time_s": raw.last_lap_s, "field_reference_s": field_reference,
            "operational_fast": operational_fast, "pace_pass": pace_pass,
            "reason": reason, "global_push_index": self.global_push_index if pace_pass else None,
        })

    def revoke(self, key, reason, **detail):
        if key in self.revoked:
            return
        self.revoked.add(key)
        self._dirty = True
        self.lifetime_events.append({"as_of": dict(self.as_of), "completion_key": key,
                                     "reason": reason, "resolved": True, **detail})

    def deletion_notice(self, text):
        if "DELETED" not in text or text in self._notices_seen:
            return
        self._notices_seen.add(text)
        match = DELETION.search(text)
        if match is None:
            candidates = []
            reason = "NO_EXACT_DELETION_PATTERN"
        else:
            number, code, minutes, seconds = match.groups()
            time_ms = int(minutes) * 60000 + round(float(seconds) * 1000)
            candidates = [key for key, (event, _) in self.completions.items()
                          if event.driver_number == number and positive(event.lap_time_s)
                          and round(event.lap_time_s * 1000) == time_ms]
            reason = "NONUNIQUE_OR_UNMATCHED_DELETION"
        if len(candidates) == 1:
            self.revoke(candidates[0], "EXPLICIT_LAP_DELETION", text=text)
        else:
            self.lifetime_events.append({"as_of": dict(self.as_of), "text": text,
                                         "reason": reason, "matches": len(candidates), "resolved": False})
        # Unresolved notices are never retried against future completions.

    def update_traffic(self, key, sample, *, publication=None):
        """Passively copy a fully constructed production sample under its exact key.

        No evaluator calls, identity inference or clean-air recomputation here.
        The caller owns the sample/key association and publication lifecycle.
        """
        fields = {name: getattr(sample, name) for name in (
            "physical_mean_ahead_gap_s", "ahead_coverage", "traffic_status", "traffic_method",
            "traffic_target_samples", "traffic_ahead_samples", "traffic_evaluator_version",
            "traffic_circuit_length_m", "clean_air_at_completion", "clean_air_gap_threshold_s")}
        self.traffic_results[key] = fields
        if publication is not None:
            self.traffic_publications[key] = dict(publication)
        if key in self.samples:
            old = self.samples[key]
            self.samples[key] = replace(old, **fields)
            self._dirty |= old != self.samples[key]

    def finish_publication(self, service):
        # The established reducer already retains merged race-control messages.
        # Read-only adapter avoids changing reducer/publication schemas. Snapshot-
        # only integrations can call deletion_notice explicitly at publication.
        store = getattr(service.controller, "store", None)
        for notice in getattr(store, "_race_control_messages", {}).values():
            text = notice.get("Message")
            if isinstance(text, str):
                self.deletion_notice(text)
        if not self.q1_frozen:
            self.q1_population.update(d.driver_code for d in service.raw_snapshot.drivers
                                      if service._states.get(d.driver_number) is None
                                      or service._states[d.driver_number].active)
            self.q1_bests = {d.driver_code: d.best_lap_s for d in service.raw_snapshot.drivers
                             if positive(d.best_lap_s)
                             and (service._states.get(d.driver_number) is None
                                  or service._states[d.driver_number].active)}
        self._prior = service._fp_track_rate_s_per_field_push
        self._override = service.track_override_s_per_field_push
        self.refresh()

    def refresh(self):
        if not self._dirty:
            return
        self._diagnostic = build_track_evolution_diagnostic(
            tuple(s for key, s in self.samples.items() if key not in self.revoked and tyre_eligible(s)),
            clean_air_gap_s=3.0, require_traffic=False,
        )
        live = self._diagnostic.aggregate_slope_s_per_push
        self._last_delta = live - self._previous_live if live is not None and self._previous_live is not None else None
        self._sign_flip = (live * self._previous_live < 0
                           if live is not None and self._previous_live is not None else False)
        if self._last_delta is not None and abs(self._last_delta) >= THRESHOLDS["jump_s_per_push"]:
            self._jump_index = self.global_push_index
            self._jump_delta = self._last_delta
        self._previous_live = live
        self._dirty = False

    def payload(self, service=None, *, detailed=True):
        """Versioned production result; full audit ledger on explicit debug export."""
        if service is not None:
            self._prior = service._fp_track_rate_s_per_field_push
            self._override = service.track_override_s_per_field_push
        self.refresh()
        d = self._diagnostic
        recent = self._jump_index is not None and self.global_push_index - self._jump_index < THRESHOLDS["jump_push_lifetime"]
        result = quality(d, self.q1_bests, recent_jump=recent, reference_population=self.q1_population)
        live = d.aggregate_slope_s_per_push
        source = "LIVE_PER_DRIVER" if live is not None else "CONFIGURED_FP_DEFAULT_PRIOR"
        selected = live if live is not None else self._prior
        if self._override is not None:
            source, selected = "MANUAL_OVERRIDE", self._override
        active = [s for key, s in self.samples.items() if key not in self.revoked]
        tyre_ok = [s for s in active if tyre_eligible(s)]
        modal = {driver: dominant_track_compound(s.compound for s in d.observations if s.driver == driver)
                 for driver in {s.driver for s in d.observations}}
        nonmodal = sum(s.compound != modal[s.driver] for s in d.observations)
        states = Counter(s.traffic_status for s in active)
        pending = states["pending"]
        unavailable = sum(n for state, n in states.items() if state not in {"pending", "ok"})
        decisions = Counter(row["reason"] for row in self.decisions if row["operational_fast"])
        information = result["information"] + (["FIELD_REFERENCE_UNAVAILABLE"] if decisions["FIELD_REFERENCE_UNAVAILABLE"] else []) + (["AUX_TRAFFIC_PENDING"] if pending else []) + (["AUX_TRAFFIC_UNAVAILABLE"] if unavailable else [])
        result.update({
            "schema_version": 2, "policy_version": POLICY_VERSION,
            "quality_threshold_version": QUALITY_VERSION, "thresholds": dict(THRESHOLDS),
            "thresholds_calibrated": False, "shadow_only": False, "as_of": dict(self.as_of),
            "live_estimate_s_per_global_push": live,
            "estimate_s_per_global_push": selected, "estimate_source": source,
            "fallback_estimate_s_per_global_push": self._prior if live is None else None,
            "manual_override_s_per_global_push": self._override,
            "global_push_index": self.global_push_index,
            "diagnostic": asdict(d) if detailed else None,
            "driver_support": [{"driver": s.driver, "slope_s_per_push": s.slope_s_per_push,
                                "support": len(s.samples), "weight": len(s.samples) / d.lap_count}
                               for s in d.series],
            "information": information,
            "traffic": {"status_counts": dict(states), "unevaluated_candidates": sum(
                key not in self.traffic_results for key in self.samples if key not in self.revoked),
                "provenance": "production evidence when present; otherwise unavailable, not evaluated"},
            "attrition": {
                "operational_fast_candidates": sum(row["operational_fast"] for row in self.decisions),
                "pace_gross_sanity_rejected": decisions["GROSS_SANITY_REJECTED"],
                "field_reference_unavailable": decisions["FIELD_REFERENCE_UNAVAILABLE"],
                "identity_or_lap_time_excluded": decisions["UNIDENTIFIED_VALID_COMPLETION"] + decisions["INVALID_LAP_TIME"],
                "tyre_install_excluded_after_pace": decisions["TYRE_INSTALL_EXCLUDED"],
                "auxiliary_traffic_pending": pending, "auxiliary_traffic_unavailable": unavailable,
                "explicitly_revoked_pace_candidates": sum(key in self.revoked for key in self.samples),
                "installation_selection": len(tyre_ok) - len(d.observations),
                "modal_compound_selection": nonmodal,
                "unsupported_driver_observations": len(d.observations) - nonmodal - d.lap_count,
                "unsupported_or_nonmodal_observations": len(d.observations) - d.lap_count,
            },
            "lifetime_events": list(self.lifetime_events) if detailed else None,
            "sample_provenance": [{"completion_key": key, "publication": self.completions.get(key, (None, None))[1],
                                   "revoked": key in self.revoked, "sample": asdict(sample),
                                   "traffic_publication": self.traffic_publications.get(key)}
                                  for key, sample in self.samples.items()] if detailed else None,
        })
        result["metrics"].update({"last_live_update_s_per_push": self._last_delta,
                                  "sign_flip_at_last_update": self._sign_flip,
                                  "recent_jump_index": self._jump_index, "recent_jump_delta_s_per_push": self._jump_delta})
        provenance = getattr(service, "_track_fallback_provenance", {"kind": "UNKNOWN", "reference": None})
        result["fallback_provenance"] = dict(provenance)
        result["configured_fallback_s_per_global_push"] = self._prior
        result["automatic_estimate_s_per_global_push"] = live if live is not None else self._prior
        result["automatic_source"] = "LIVE_PER_DRIVER" if live is not None else "CONFIGURED_FP_DEFAULT_PRIOR"
        if detailed:
            result["admission_decisions"] = list(self.decisions)
        else:
            for key in ("diagnostic", "lifetime_events", "sample_provenance"):
                result.pop(key)
        return result
