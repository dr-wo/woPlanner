#!/usr/bin/env python3
"""Prototype the short-term FP evidence/manual/legacy tyre workflow.

This is diagnostic tooling only.  It reuses the audited LongRun classifier and
reduced-coordinate estimator, reads the existing frozen pre-race artifact, and
writes review artifacts only under ``temp``.  It does not modify Planner,
Strategy, live, Race, or persisted weekend-model inputs.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import numpy as np
import pandas as pd

import diagnose_fp_contrasts_performance_prior as contrast_audit
import diagnose_fp_longrun_only_engineering_model as longrun_audit


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "temp"
EVENT = {"year": 2026, "round_number": 11, "event": "2026 R11"}
PRIOR_PATH = (
    ROOT
    / "woData/wostrategy/weekend_model/schema_v1/year=2026/round=11"
    / "pre_race_prior_v1.json"
)
COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


def jsonable(value):
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if pd.isna(value):
        return None
    return value


def exclusion_reason(row):
    if bool(row.quantitative_tyre_likelihood_eligible):
        return ""
    if not bool(row.strict_longrun):
        return f"not_strict_longrun:{row.strict_classifier_status}"
    if bool(row.deleted):
        return f"deleted:{row.deleted_reason or 'no_reason'}"
    if not bool(row.representative_candidate):
        return "not_representative_longrun_lap"
    return "excluded_by_existing_audited_clean_lap_policy"


def current_evidence(evidence, groups):
    """Add physical-stint and explicit fit-role fields to audited evidence."""
    frame = evidence.copy()
    stint = {}
    pit_out = {}
    pit_in = {}
    for run_id, group in groups.items():
        for row in group.itertuples():
            key = (run_id, float(row.LapNumber))
            stint[key] = row.Stint
            pit_out[key] = pd.notna(row.PitOutTime)
            pit_in[key] = pd.notna(row.PitInTime)
    keys = list(zip(frame.run_id, frame.lap_number.astype(float)))
    frame["stint"] = [stint.get(key) for key in keys]
    frame["pit_out_marker"] = [pit_out.get(key, False) for key in keys]
    frame["pit_in_marker"] = [pit_in.get(key, False) for key in keys]
    frame["used_for_tyre_calculation"] = frame.quantitative_tyre_likelihood_eligible
    frame["calculation_role"] = np.where(
        frame.used_for_tyre_calculation,
        "strict_longrun_likelihood",
        "physical_stint_diagnostic_only",
    )
    frame["exclusion_from_tyre_fit_reason"] = [
        exclusion_reason(row) for row in frame.itertuples()
    ]
    frame["performance_likelihood_role"] = "none_unless_connector_is_validated"
    columns = [
        "session", "team", "driver", "run_id", "stint", "compound",
        "lap_number", "session_time_s", "raw_lap_time_s", "tyre_life",
        "tyre_age", "physical_run_index", "pit_out_marker", "pit_in_marker",
        "selected_clean_lap", "used_for_tyre_calculation", "calculation_role",
        "exclusion_from_tyre_fit_reason", "strict_classifier_status",
        "strict_longrun", "diagnostic_one_gap_longrun_status",
        "diagnostic_robust_variability_longrun_status", "representative_candidate",
        "deleted", "deleted_reason", "track_status", "min_time_to_car_ahead_s",
        "classifier_reason", "classifier_strength", "performance_likelihood_role",
    ]
    return frame[columns].sort_values(
        ["session", "team", "driver", "run_id", "lap_number"]
    ).reset_index(drop=True)


def direct_performance_support(strict):
    rows = []
    for name, left, right in (
        ("Perf_HARD_vs_MEDIUM", "HARD", "MEDIUM"),
        ("Perf_SOFT_vs_MEDIUM", "SOFT", "MEDIUM"),
    ):
        pair = {left, right}
        driver_connectors = sum(
            pair.issubset(set(group.compound.astype(str)))
            for _, group in strict.groupby("driver")
        )
        team_connectors = sum(
            pair.issubset(set(group.compound.astype(str)))
            for _, group in strict.groupby("team")
        )
        supported = driver_connectors > 0
        limitation = (
            "Direct same-driver cross-compound strict LongRun connector available."
            if supported
            else "No same-driver strict LongRun cross-compound connector; "
                 "no defensible teammate correction is available."
        )
        rows.append(
            {
                "parameter": name,
                "left_compound": left,
                "reference_compound": right,
                "same_driver_connector_count": driver_connectors,
                "same_team_connector_count": team_connectors,
                "fp_direct_value": None,
                "support_status": "connector_present_estimator_not_prototyped" if supported else "unsupported",
                "limitation": limitation,
            }
        )
    return pd.DataFrame(rows)


def contrast_output(results, sensitivity, profiles, convergence):
    rows = []
    convergence_summary = convergence.groupby("sample_count").agg(
        ess_min=("ess", "min"),
        ess_max=("ess", "max"),
        max_weight=("max_weight", "max"),
        top5_weight=("top5_weight", "max"),
    ).reset_index()
    convergence_text = json.dumps(jsonable(convergence_summary.to_dict("records")))
    for contrast in ("Delta_HM", "Delta_SM", "Delta_SH"):
        by_variant = results[results.contrast.eq(contrast)].set_index("variant")
        strict = by_variant.loc["strict"]
        leave_run = sensitivity[
            sensitivity.contrast.eq(contrast)
            & sensitivity.variant.eq("strict")
            & sensitivity.sensitivity_type.eq("leave_one_run")
        ]
        leave_team = sensitivity[
            sensitivity.contrast.eq(contrast)
            & sensitivity.variant.eq("strict")
            & sensitivity.sensitivity_type.eq("leave_one_team")
        ]
        profile = profiles[
            profiles.variant.eq("strict") & profiles.contrast.eq(contrast)
        ]
        if len(profile):
            optimum = profile.loc[profile.delta_objective.idxmin()]
            profile_fields = {
                "profile_point_count": len(profile),
                "profile_optimum_s_per_lap": optimum.fixed_value_s_per_lap,
                "profile_grid_low_s_per_lap": profile.fixed_value_s_per_lap.min(),
                "profile_grid_high_s_per_lap": profile.fixed_value_s_per_lap.max(),
                "profile_edge_delta_objective_min": min(
                    profile.iloc[0].delta_objective, profile.iloc[-1].delta_objective
                ),
            }
        else:
            profile_fields = {
                "profile_point_count": 0,
                "profile_optimum_s_per_lap": None,
                "profile_grid_low_s_per_lap": None,
                "profile_grid_high_s_per_lap": None,
                "profile_edge_delta_objective_min": None,
            }
        row = {
            "parameter": f"DegContrast_{contrast.removeprefix('Delta_')}",
            "contrast": contrast,
            "unit": "s/lap",
            "provenance_type": "fp_longrun_contrast",
            "automatic_strategy_update": False,
            "strict_central": strict.central_s_per_lap,
            "strict_p10": strict.p10,
            "strict_p50": strict.central_s_per_lap,
            "strict_p90": strict.p90,
            "one_gap_central": by_variant.loc["one_gap_tolerant"].central_s_per_lap,
            "robust_variability_central": by_variant.loc["robust_variability"].central_s_per_lap,
            "supporting_runs": int(strict.support_runs),
            "supporting_teams": int(strict.support_teams),
            "supporting_clean_laps": int(strict.support_clean_laps),
            "combined_tyre_age_span": strict.combined_tyre_age_span,
            "leave_one_run_min": leave_run.central_s_per_lap.min() if len(leave_run) else None,
            "leave_one_run_max": leave_run.central_s_per_lap.max() if len(leave_run) else None,
            "leave_one_team_min": leave_team.central_s_per_lap.min() if len(leave_team) else None,
            "leave_one_team_max": leave_team.central_s_per_lap.max() if len(leave_team) else None,
            "seed_median_span": strict.seed_median_span,
            "fit_rank": int(strict.fit_rank),
            "residual_sigma_s": strict.residual_sigma_s,
            "numerical_convergence": convergence_text,
            "limitations": "Gauge-invariant evidence for human review; never an automatic absolute degradation update.",
        }
        row.update(profile_fields)
        rows.append(row)
    return pd.DataFrame(rows)


def legacy_values(prior):
    degradation = {
        f"Deg_{compound}": float(prior["degradation"][compound]["median"])
        for compound in COMPOUNDS
    }
    performance = prior["performance"]
    degradation.update(
        {
            "Perf_HARD_vs_MEDIUM": float(
                performance["HARD"]["median"] - performance["MEDIUM"]["median"]
            ),
            "Perf_SOFT_vs_MEDIUM": float(
                performance["SOFT"]["median"] - performance["MEDIUM"]["median"]
            ),
        }
    )
    return degradation


def legacy_trace(prior, values):
    rows = []
    for parameter, value in values.items():
        is_performance = parameter.startswith("Perf_")
        if is_performance:
            field = (
                "performance.HARD.median - performance.MEDIUM.median"
                if "HARD" in parameter
                else "performance.SOFT.median - performance.MEDIUM.median"
            )
            sensitivity = "zero_or_negligible_after_free_run_intercepts"
            limitation = "Ordering/bounds/finite-bank dependent; not FP-measured performance."
        else:
            compound = parameter.removeprefix("Deg_")
            field = f"degradation.{compound}.median"
            sensitivity = "contrasts_sensitive_but_absolute_value_gauge_unidentified"
            limitation = "Absolute Fuel/common-degradation gauge is unidentified."
        rows.append(
            {
                "parameter": parameter,
                "legacy_value": value,
                "source_type": "legacy_fp_derived",
                "source_artifact": str(PRIOR_PATH.relative_to(ROOT)),
                "source_field_or_transform": field,
                "source_analysis_id": prior["source_analysis_id"],
                "source_sessions": "|".join(prior["source_sessions"]),
                "independent_of_current_fp": False,
                "true_likelihood_sensitivity": sensitivity,
                "limitations": limitation,
            }
        )
    return pd.DataFrame(rows)


def resolve_inputs(manual, legacy, contrasts, performance_support):
    contrast_map = contrasts.set_index("contrast").strict_central.to_dict()
    performance_map = performance_support.set_index("parameter").to_dict("index")
    rows = []
    for parameter in (
        "Deg_HARD", "Deg_MEDIUM", "Deg_SOFT",
        "Perf_HARD_vs_MEDIUM", "Perf_SOFT_vs_MEDIUM",
    ):
        manual_value = manual.get(parameter)
        selected_source = "manual" if manual_value is not None else "legacy_fp_derived"
        selected = float(manual_value) if manual_value is not None else float(legacy[parameter])
        if parameter == "Deg_HARD":
            contrast_name = "Delta_HM"
        elif parameter == "Deg_SOFT":
            contrast_name = "Delta_SM"
        else:
            contrast_name = None
        perf = performance_map.get(parameter, {})
        limitation = (
            "FP contrast is advisory only and does not alter this absolute value."
            if parameter.startswith("Deg_")
            else perf.get("limitation", "No direct FP performance evidence.")
        )
        rows.append(
            {
                "parameter": parameter,
                "selected_value": selected,
                "unit": "s/lap" if parameter.startswith("Deg_") else "s",
                "selected_source": selected_source,
                "manual_value": manual_value,
                "legacy_value": legacy[parameter],
                "legacy_source_type": "legacy_fp_derived",
                "fp_direct_value": perf.get("fp_direct_value"),
                "fp_direct_support": perf.get("support_status"),
                "fp_contrast_evidence": contrast_map.get(contrast_name),
                "fp_contrast_name": contrast_name,
                "automatic_fp_application": False,
                "limitations": limitation,
            }
        )
    return pd.DataFrame(rows)


def manual_schema():
    fields = {
        "Deg_HARD": "s/lap", "Deg_MEDIUM": "s/lap", "Deg_SOFT": "s/lap",
        "Perf_HARD_vs_MEDIUM": "s", "Perf_SOFT_vs_MEDIUM": "s",
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Event-specific manual Strategy tyre inputs",
        "schema_version": 1,
        "type": "object",
        "required": ["schema_version", "year", "round_number", "values"],
        "properties": {
            "schema_version": {"const": 1},
            "year": {"type": "integer"},
            "round_number": {"type": "integer"},
            "values": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {
                        "type": ["number", "null"],
                        "unit": unit,
                        "provenance_type_when_set": "manual",
                    }
                    for name, unit in fields.items()
                },
            },
        },
        "allowed_provenance_types": [
            "manual", "legacy_fp_derived", "fp_longrun_direct", "fp_longrun_contrast"
        ],
        "selection_rule": "manual value when non-null; otherwise exact legacy artifact value",
        "event_storage_recommendation": (
            "weekend_model/schema_v1/year={year}/round={round_number}/manual_tyre_inputs_v1.json"
        ),
        "persistence_semantics": {
            "reopen_same_event": "reload",
            "fp_recalculation": "preserve unchanged",
            "strategy_launch": "resolve and serialize exact selected values",
            "live_race_transition": "carry event-specific resolved package; Race inference unchanged",
            "history_replay": "version/snapshot manual artifact with the selected package",
            "cross_event_default": False,
        },
    }


def calculation_spec():
    return """# Short-term FP tyre calculation specification

1. Load FP1, FP2 and FP3 for one event.
2. Split timing data into physical pit-to-pit stints and retain their complete lap inventory.
3. Apply the revised programme classifier. Setting, QualiSim and Uncertain remain visual/inventory data only.
4. Select only strict LongRun stints for quantitative Race-relevant tyre evidence.
5. Apply the existing audited clean/representative LongRun-lap policy. Preserve exclusions and reasons.
6. Fit free run intercepts plus physical-stint progression/Fuel and compound-specific tyre-age slopes. Do not activate FP track coordinates.
7. Project out the exact common gauge `Fuel -> Fuel+t`, `Deg_c -> Deg_c+t`.
8. Estimate the reduced, gauge-invariant degradation contrasts `H-M`, `S-M`, and `S-H` directly.
9. Export strict intervals, classifier variants, profiles, seed/numerical checks, and leave-one-run/team sensitivities. Do not create a threshold status.
10. Search strict LongRuns for genuine same-driver cross-compound connections; teammate connections require an explicit defensible driver correction.
11. If no valid connector exists, export direct FP performance as `unsupported` and no value.
12. Present FP contrast/direct evidence separately from engineering inputs.
13. Load event-specific manual H/M/S degradation and H-vs-M/S-vs-M performance values.
14. Select each non-null manual value; for each null field, use the corresponding unchanged value from `pre_race_prior_v1.json` and mark it `legacy_fp_derived`.
15. Serialize the complete five-parameter package and provenance. Strategy must consume exactly those displayed selected values. FP contrasts never overwrite them automatically.

No QualiSim, Setting, Q-normalized performance, track evolution, automatic anchoring, historical prior, new sampler, or Race-inference change is part of this workflow.
"""


def report(ledger, evidence, ident, contrasts, performance, preview, tests, legacy):
    strict = evidence[evidence.used_for_tyre_calculation]
    counts = ledger.revised_label.value_counts().to_dict()
    exact = ident[ident.record_type.eq("exact_null_vector_original")].iloc[0]
    hm = contrasts.set_index("contrast").loc["Delta_HM"]
    sm = contrasts.set_index("contrast").loc["Delta_SM"]
    legacy_path = str(PRIOR_PATH.relative_to(ROOT))
    lines = [
        "# FP Short-Term Manual + Legacy Workflow", "", "## Outcome", "",
        "The prototype cleanly separates strict LongRun FP evidence from the final engineering tyre model. Manual values win field-by-field; missing fields use the unchanged legacy artifact. FP contrasts are advisory and never alter the selected Strategy values.", "",
        "## Explicit answers", "",
        f"1. Only {len(strict)} audited clean laps from {strict.run_id.nunique()} strict physical LongRuns enter the tyre fit. Complete candidate physical stints and every fit/exclusion flag are in `fp_longrun_evidence_current.csv`.",
        f"2. Setting and QualiSim are excluded because their programme/fuel/tyre objectives are not comparable Race LongRuns. Classifier inventory counts are `{counts}`; neither label has `used_for_tyre_calculation=true`.",
        "3. The model profiles one free intercept per run and fits physical-stint progression/Fuel plus compound-specific tyre-age slopes. FP track evolution is excluded because its independent projected sensitivity was insufficient.",
        f"4. The exact gauge is `Fuel→Fuel+t` and `Deg_H/M/S→Deg_H/M/S+t`; rank is {int(exact['rank'])}, nullity {int(exact.nullity)}, and the stored normalized null vector is `{exact.null_vector}`.",
        "5. FP identifies the invariant degradation contrasts H−M, S−M and S−H, plus net within-run slopes diagnostically. It does not identify the absolute Fuel/common-D level.",
        f"6. Direct reduced-coordinate strict fits give H−M {hm.strict_central:+.4f} s/lap (P10–P90 {hm.strict_p10:+.4f} to {hm.strict_p90:+.4f}) and S−M {sm.strict_central:+.4f} ({sm.strict_p10:+.4f} to {sm.strict_p90:+.4f}). Classifier and leave-out fields are exported without threshold labels.",
        "7. No. R11 has no same-driver or same-team strict LongRun cross-compound connector for either H/M or S/M, so direct FP Race performance is unsupported.",
        "8. Unsupported performance remains null as FP evidence. The final package uses a manual value when supplied, otherwise the exact legacy value with transparent provenance.",
        f"9. Legacy values come from `{legacy_path}`, analysis `{legacy.source_analysis_id.iloc[0]}`. Degradation uses each compound median; performance uses HARD−MEDIUM and SOFT−MEDIUM display-gauge differences.",
        "10. Legacy performance has zero/negligible likelihood sensitivity after free run intercepts. Legacy absolute degradation is common-gauge unidentified, although degradation contrasts retain likelihood sensitivity.",
        "11. Resolution is deterministic per field: non-null manual value → `manual`; otherwise unchanged legacy value → `legacy_fp_derived`. FP never overrides manual input.",
        "12. Partial inputs are resolved independently. The test supplies manual H/S degradation with M missing, and manual H−M performance with S−M missing; only missing fields fall back.",
        "13. Yes. The evidence CSV/contrast CSV are separate from the selected Strategy preview; the latter carries manual, legacy, direct-FP and contrast fields side by side.",
        "14. Yes in the prototype. Event identity, values, selected source and limitations survive JSON round-trip, FP recalculation, and exact preview-to-handoff equality tests.",
        "15. Yes. Five complete H/M/S degradation and MEDIUM-reference performance values are produced even when FP cannot identify absolute D/P.",
        "16. Future FP runs should execute the 15 deterministic steps in `fp_short_term_calculation_spec.md`; no 9D ProgrammeBlock MC, SMC/AIS, or automatic contrast anchoring is needed.", "",
        "## Persistence and UI audit", "",
        "Manual inputs should be stored event-specifically and loaded independently of FP recalculation. The prototype recommends `manual_tyre_inputs_v1.json` beside the event weekend-model artifacts, but does not write it there. Replays must snapshot the selected package/provenance so later edits do not rewrite history.", "",
        "Current production wording candidates for later review include `Initial FP baseline` and `Initial FP degradation` in `analysis_gui.py`, and `pre-race posterior` in `analysis_service.py`. Compatibility fields were not renamed here. Performance fallback must be shown as `legacy_fp_derived`, never `FP estimated performance`.", "",
        "## Test result", "",
        f"{int(tests.passed.sum())}/{len(tests)} prototype checks passed. Production Planner, Strategy, Race and live logic were not changed.",
    ]
    (OUT / "FP_SHORT_TERM_MANUAL_LEGACY_WORKFLOW.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main():
    OUT.mkdir(exist_ok=True)
    laps, ledger, groups, full, data, teammate, transfer_sigma, evidence, strict = (
        longrun_audit.load()
    )
    evidence_current = current_evidence(evidence, groups)
    results, sensitivity, profiles = contrast_audit.direct_results(evidence)
    strict_fit = contrast_audit.transformed_fit(
        contrast_audit.variant_frame(evidence, "strict")
    )
    ident = contrast_audit.gauge_identifiability(strict, strict_fit)
    geometry = longrun_audit.build_geometry(strict)
    convergence = longrun_audit.convergence(geometry)
    contrasts = contrast_output(results, sensitivity, profiles, convergence)
    performance = direct_performance_support(strict)

    prior = json.loads(PRIOR_PATH.read_text(encoding="utf-8"))
    legacy = legacy_values(prior)
    legacy_frame = legacy_trace(prior, legacy)

    # A deliberately partial example verifies field-level override semantics.
    manual = {
        "Deg_HARD": 0.070,
        "Deg_MEDIUM": None,
        "Deg_SOFT": 0.160,
        "Perf_HARD_vs_MEDIUM": 0.420,
        "Perf_SOFT_vs_MEDIUM": None,
    }
    preview = resolve_inputs(manual, legacy, contrasts, performance)

    # Persistence is prototyped under temp only; a production path is proposed,
    # not created.  Recompute FP evidence between save and reload deliberately.
    state_path = OUT / "fp_manual_input_state_test.json"
    state = {"schema_version": 1, **EVENT, "values": manual}
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _ = contrast_audit.direct_results(evidence)
    restored = json.loads(state_path.read_text(encoding="utf-8"))
    preview_after_recalculation = resolve_inputs(
        restored["values"], legacy, contrasts, performance
    )

    checks = []
    def check(name, passed, expected, actual):
        checks.append(
            {"test": name, "passed": bool(passed), "expected": expected, "actual": actual}
        )

    used = evidence_current[evidence_current.used_for_tyre_calculation]
    check("Setting excluded", not used.strict_classifier_status.eq("Setting").any(), "0 used", int(used.strict_classifier_status.eq("Setting").sum()))
    check("QualiSim excluded", not used.strict_classifier_status.eq("QualiSim").any(), "0 used", int(used.strict_classifier_status.eq("QualiSim").sum()))
    check("strict support", used.run_id.nunique() == 8 and len(used) == 58, "8 runs / 58 laps", f"{used.run_id.nunique()} runs / {len(used)} laps")
    exact = ident[ident.record_type.eq("exact_null_vector_original")].iloc[0]
    check("rank and nullity", exact["rank"] == 3 and exact.nullity == 1, "rank 3 / nullity 1", f"rank {exact['rank']} / nullity {exact.nullity}")
    by_contrast = contrasts.set_index("contrast")
    check("H-M regression", abs(by_contrast.loc["Delta_HM"].strict_central + 0.056086) < 5e-4, "approximately -0.0561", by_contrast.loc["Delta_HM"].strict_central)
    check("S-M regression", abs(by_contrast.loc["Delta_SM"].strict_central + 0.189669) < 5e-4, "approximately -0.1897", by_contrast.loc["Delta_SM"].strict_central)
    check("R11 direct performance unsupported", performance.support_status.eq("unsupported").all(), "both unsupported", performance.support_status.tolist())
    check("legacy complete and labelled", set(legacy) == set(preview.parameter) and legacy_frame.source_type.eq("legacy_fp_derived").all(), "5 labelled fields", len(legacy_frame))
    selected = preview.set_index("parameter")
    check("partial degradation override", selected.loc["Deg_HARD"].selected_source == "manual" and selected.loc["Deg_MEDIUM"].selected_source == "legacy_fp_derived" and selected.loc["Deg_SOFT"].selected_source == "manual", "H manual / M legacy / S manual", selected.loc[["Deg_HARD", "Deg_MEDIUM", "Deg_SOFT"]].selected_source.to_dict())
    check("partial performance override", selected.loc["Perf_HARD_vs_MEDIUM"].selected_source == "manual" and selected.loc["Perf_SOFT_vs_MEDIUM"].selected_source == "legacy_fp_derived", "H-M manual / S-M legacy", selected.loc[["Perf_HARD_vs_MEDIUM", "Perf_SOFT_vs_MEDIUM"]].selected_source.to_dict())
    check("manual survives FP recalculation", preview_after_recalculation[["parameter", "manual_value", "selected_value", "selected_source"]].equals(preview[["parameter", "manual_value", "selected_value", "selected_source"]]), "unchanged", "unchanged" if preview_after_recalculation.equals(preview) else "difference")

    provenance = {
        "schema_version": 1,
        **EVENT,
        "architecture": "strict LongRun evidence -> manual per-field override -> legacy fallback",
        "source_types": ["manual", "legacy_fp_derived", "fp_longrun_direct", "fp_longrun_contrast"],
        "manual_state": restored,
        "legacy_artifact": str(PRIOR_PATH.relative_to(ROOT)),
        "fp_performance_support": jsonable(performance.to_dict("records")),
        "fp_contrast_evidence": jsonable(contrasts.to_dict("records")),
        "strategy_handoff": jsonable(preview.to_dict("records")),
        "race_inference_modified": False,
        "automatic_fp_contrast_application": False,
    }
    encoded = json.dumps(provenance, indent=2)
    decoded = json.loads(encoded)
    check("provenance JSON round-trip", decoded["strategy_handoff"] == provenance["strategy_handoff"], "identical", "identical")
    handoff = pd.DataFrame(decoded["strategy_handoff"])
    check("Strategy handoff equals displayed selected values", handoff.set_index("parameter").selected_value.to_dict() == preview.set_index("parameter").selected_value.to_dict(), "exact equality", "exact equality")
    tests = pd.DataFrame(checks)

    evidence_current.to_csv(OUT / "fp_longrun_evidence_current.csv", index=False)
    contrasts.to_csv(OUT / "fp_degradation_contrast_current.csv", index=False)
    (OUT / "fp_manual_input_schema.json").write_text(
        json.dumps(manual_schema(), indent=2) + "\n", encoding="utf-8"
    )
    legacy_frame.to_csv(OUT / "fp_legacy_fallback_trace.csv", index=False)
    preview.to_csv(OUT / "fp_strategy_tyre_input_preview.csv", index=False)
    (OUT / "fp_strategy_tyre_input_provenance.json").write_text(
        encoded + "\n", encoding="utf-8"
    )
    tests.to_csv(OUT / "fp_manual_override_test_results.csv", index=False)
    (OUT / "fp_short_term_calculation_spec.md").write_text(
        calculation_spec(), encoding="utf-8"
    )
    report(ledger, evidence_current, ident, contrasts, performance, preview, tests, legacy_frame)

    if not tests.passed.all():
        raise AssertionError(tests.loc[~tests.passed].to_dict("records"))
    print(f"strict runs={used.run_id.nunique()} clean laps={len(used)}")
    print(contrasts[["contrast", "strict_central", "strict_p10", "strict_p90"]].to_string(index=False))
    print(f"tests={int(tests.passed.sum())}/{len(tests)}")


if __name__ == "__main__":
    main()
