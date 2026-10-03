"""Physical-reference regressions; no MC resampling or model tuning."""
import json
from itertools import combinations

import pytest

from woplanner.analysis.service import ModelSummary, OfflineAnalysisService
from wostrategy.algorithm.exact_strategy_search import StrategyModel, stint_cost


COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
DEGRADATION = {"HARD": .08, "MEDIUM": .1, "SOFT": .18}


def summary(deltas, reference="HARD", *, observed=None):
    observed = set(deltas) if observed is None else set(observed)
    rows = []
    for compound, value in deltas.items():
        for parameter, median in (("compound_delta", value),
                                  ("degradation", DEGRADATION[compound])):
            rows.append(dict(parameter=parameter, compound=compound, median=median,
                             usable_lap_count=10, support_status="measured", session="R",
                             reference_compound=reference if parameter == "compound_delta" else None,
                             unit="s" if parameter == "compound_delta" else "s/lap"))
    return ModelSummary(
        {"analysis_id": "contract-test", "as_of_leader_lap": 25,
         "sampler_quality": {"architecture": "live-retro-v1", "observed_compounds": sorted(observed)}},
        tuple(rows),
    )


def resolve(base, live, reference="HARD", *, observed=None):
    rows = OfflineAnalysisService().strategy_assumptions(
        summary(live, reference, observed=observed),
        baseline_compound_delta=base, baseline_degradation=DEGRADATION,
    )
    return {r["compound"]: r["compound_delta"] for r in rows}


def assert_predictions(actual, expected, degradation=DEGRADATION):
    actual_model = StrategyModel(actual, degradation)
    expected_model = StrategyModel(expected, degradation)
    for a, b in combinations(actual, 2):
        assert actual[a] - actual[b] == pytest.approx(expected[a] - expected[b], abs=1e-12)
        for horizon in (1, 5, 20):
            assert stint_cost(horizon, a, 7, actual_model) - stint_cost(horizon, b, 7, actual_model) == pytest.approx(
                stint_cost(horizon, a, 7, expected_model) - stint_cost(horizon, b, 7, expected_model), abs=1e-11
            )


def test_rd_r13_l25_unobserved_soft_keeps_its_physical_difference():
    # Race MC fresh 20261002: merged_offsets.csv R13/L25 (pre-cutoff baseline).
    base = {"HARD": .3337454833569559, "MEDIUM": 0., "SOFT": -.31964500240628674}
    live = {"HARD": 0., "MEDIUM": -.6083795692887073}
    expected = {"HARD": .6083795692887073, "MEDIUM": 0., "SOFT": base["SOFT"]}
    actual = resolve(base, live)
    assert_predictions(actual, expected)


SUBSETS = [
    (("HARD",), {"HARD": .4, "MEDIUM": 0., "SOFT": -.5}),
    (("MEDIUM",), {"HARD": .4, "MEDIUM": 0., "SOFT": -.5}),
    (("SOFT",), {"HARD": .4, "MEDIUM": 0., "SOFT": -.5}),
    (("HARD", "MEDIUM"), {"HARD": .7, "MEDIUM": 0., "SOFT": -.5}),
    (("MEDIUM", "SOFT"), {"HARD": .4, "MEDIUM": 0., "SOFT": -.8}),
    (("HARD", "SOFT"), {"HARD": .4, "MEDIUM": 0., "SOFT": -1.1}),
    (COMPOUNDS, {"HARD": .7, "MEDIUM": 0., "SOFT": -.8}),
]


@pytest.mark.parametrize("observed,expected", SUBSETS)
@pytest.mark.parametrize("reference", (*COMPOUNDS, "SUM_ZERO"))
@pytest.mark.parametrize("base_shift", (0., 3.2))
def test_physical_contrasts_and_costs_do_not_depend_on_reference(observed, expected, reference, base_shift):
    base = {"HARD": .4 + base_shift, "MEDIUM": base_shift, "SOFT": -.5 + base_shift}
    physical = {"HARD": .7, "MEDIUM": 0., "SOFT": -.8}
    zero = physical.get(reference, sum(physical.values()) / 3)
    live = {c: physical[c] - zero for c in reversed(observed)}
    before = dict(base), dict(live)
    actual = resolve(base, live, reference)
    assert_predictions(actual, expected)
    assert (base, live) == before


def test_full_then_repeated_partial_updates_survive_serialization_and_reference_changes():
    full = resolve({}, {"HARD": 0., "MEDIUM": -.4, "SOFT": -.9})
    retained = json.loads(json.dumps(full))
    second = resolve(retained, {"HARD": .6, "MEDIUM": 0.}, "MEDIUM")
    third = resolve(json.loads(json.dumps(second)), {"MEDIUM": .7, "SOFT": 0.}, "SOFT")
    fourth = resolve(third, {"HARD": 24.}, "SUM_ZERO")
    assert_predictions(full, {"HARD": .4, "MEDIUM": 0., "SOFT": -.5})
    assert_predictions(second, {"HARD": .6, "MEDIUM": 0., "SOFT": -.5})
    assert_predictions(third, {"HARD": .6, "MEDIUM": 0., "SOFT": -.7})
    assert_predictions(fourth, third)


def test_unobserved_model_coordinates_do_not_anchor_or_replace_retained_values():
    base = {"HARD": .4, "MEDIUM": 0., "SOFT": -.5}
    actual = resolve(base, {"HARD": 900., "MEDIUM": -.6, "SOFT": -800.}, observed={"MEDIUM"})
    assert_predictions(actual, base)


def test_no_mismatch_preserves_existing_predictions_and_degradation():
    base = {"HARD": .4, "MEDIUM": 0., "SOFT": -.5}
    actual = resolve(base, {"HARD": .6, "MEDIUM": 0.}, "MEDIUM")
    assert_predictions(actual, {**base, "HARD": .6})
    rows = OfflineAnalysisService().strategy_assumptions(
        summary({"HARD": .6, "MEDIUM": 0.}, "MEDIUM"),
        baseline_compound_delta=base, baseline_degradation=DEGRADATION,
    )
    assert {r["compound"]: r["degradation"] for r in rows} == pytest.approx(DEGRADATION)
    assert_predictions(resolve(base, {}, observed=set()), base)


def test_missing_medium_uses_shared_hard_without_inventing_medium():
    from woplanner.analysis.compound_offsets import merge_compound_offsets
    actual = merge_compound_offsets({"HARD": .4, "SOFT": -.5}, {"HARD": -7., "SOFT": -8.2})
    assert_predictions(actual, {"HARD": 0., "SOFT": -1.2}, {"HARD": .08, "SOFT": .18})


@pytest.mark.parametrize("base,live,match", [
    ({"HARD": 0.}, {"SOFT": 0.}, "shared compound"),
    ({"MEDIUM": 0.}, {"MEDIUM": float("nan")}, "Nonfinite"),
    ({"MEDIUM": 0.}, {"WET": 0.}, "Unsupported"),
])
def test_invalid_merge_is_refused_without_mutation(base, live, match):
    from woplanner.analysis.compound_offsets import merge_compound_offsets
    retained = dict(base)
    with pytest.raises(ValueError, match=match):
        merge_compound_offsets(base, live)
    assert base == retained


def test_incompatible_live_metadata_is_refused_before_aggregation():
    from woplanner.analysis.compound_offsets import validate_offset_rows
    with pytest.raises(ValueError, match="seconds"):
        validate_offset_rows([dict(parameter="compound_delta", unit="s/lap")])
    with pytest.raises(ValueError, match="Unsupported"):
        validate_offset_rows([dict(parameter="compound_delta", reference_compound="UNKNOWN")])
    service = OfflineAnalysisService()
    mixed = ModelSummary({}, tuple(summary({"HARD": 0.}).parameters + summary({"MEDIUM": 0.}, "MEDIUM").parameters))
    with pytest.raises(ValueError, match="Mixed"):
        service.strategy_assumptions(mixed)


def test_effective_resolver_and_persisted_raw_model_round_trip(tmp_path):
    from datetime import datetime, timezone
    from wodata.artifacts import write_model_snapshot
    from wodata.contracts import ModelSnapshot, ParameterEstimate

    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs.save_manual_inputs(2026, 13, {
        "Deg_HARD": .08, "Deg_MEDIUM": .1, "Deg_SOFT": .18,
        "Perf_HARD_vs_MEDIUM": .4, "Perf_SOFT_vs_MEDIUM": -.5,
    })
    resolved = service.resolve_tyre_inputs(2026, 13)
    created = datetime(2026, 10, 3, tzinfo=timezone.utc)
    raw = summary({"HARD": 0., "MEDIUM": -.6})
    parameters = tuple(ParameterEstimate(
        "reference-test", "bank", "aggregate", 25, r["parameter"], "R", None,
        r["compound"], r["reference_compound"], r["median"], r["median"], r["median"],
        r["unit"], "measured", 10, 2, 50, .1, 0, 0, "input", "config", created,
    ) for r in raw.parameters)
    snapshot = ModelSnapshot("reference-test", "bank", "aggregate", 25, parameters,
                             "input", "config", created)
    write_model_snapshot(snapshot, year=2026, round_number=13, data_root=tmp_path)
    loaded = service.load_model(2026, 13)
    effective, _ = service.effective_tyre_inputs(loaded, resolved, manual_override=False)
    expected = {"HARD": .6, "MEDIUM": 0., "SOFT": -.5}
    assert_predictions({c: effective[("compound_delta", c)] for c in COMPOUNDS}, expected)
    manual, _ = service.effective_tyre_inputs(loaded, resolved, manual_override=True)
    assert manual == resolved.strategy_overrides()
    # Serialization retains original fitting coordinates, not a rewritten model.
    assert [float(r["median"]) for r in loaded.parameters] == [r.median for r in parameters]



def test_shared_pivot_preserves_untouched_connection_not_every_old_contrast():
    from woplanner.analysis.compound_offsets import merge_compound_offsets

    retained = {"MEDIUM": 0., "HARD": .4, "SOFT": -.5}
    incoming = {"MEDIUM": 0., "HARD": .7}
    merged = merge_compound_offsets(retained, incoming)
    assert merged["HARD"] - merged["MEDIUM"] == pytest.approx(.7)  # new contrast
    assert merged["SOFT"] - merged["MEDIUM"] == pytest.approx(-.5)  # pivot connection
    assert merged["SOFT"] - merged["HARD"] == pytest.approx(-1.2)  # old -0.9 changes
    # HARD anchoring would instead retain S-H=-0.9 and yield S-M=-0.8.
    assert merged["SOFT"] - merged["MEDIUM"] != pytest.approx(-.8)
    shifted = merge_compound_offsets({c: v + 5 for c, v in retained.items()},
                                     {c: v - 3 for c, v in incoming.items()})
    assert shifted == pytest.approx(merged)  # changing coordinates alone is harmless


def test_disconnected_live_vector_is_rejected_by_resolver():
    with pytest.raises(ValueError, match="shared compound"):
        resolve({"HARD": 0.}, {"SOFT": 0.})


@pytest.mark.parametrize("reference", (*COMPOUNDS, "SUM_ZERO"))
def test_complete_live_fit_preserves_original_physical_predictions(reference):
    physical = {"HARD": .7, "MEDIUM": 0., "SOFT": -.8}
    zero = physical.get(reference, sum(physical.values()) / 3)
    live = {c: physical[c] - zero for c in COMPOUNDS}
    raw = summary(live, reference)
    service = OfflineAnalysisService()
    original = service.build_strategy_model(raw)
    merged = resolve({"HARD": 2., "MEDIUM": 0., "SOFT": -3.}, live, reference)
    overrides = {("compound_delta", c): v for c, v in merged.items()}
    corrected = service.build_strategy_model(raw, overrides)
    assert_predictions(corrected.compound_delta, original.compound_delta)
    assert corrected.degradation == original.degradation
    # Reconstruct absolute lap predictions with the matching common baseline.
    original_baseline = 90.
    corrected_baseline = original_baseline + live["MEDIUM"]
    for compound in COMPOUNDS:
        for age in (0, 1, 10, 25):
            assert corrected_baseline + corrected.compound_delta[compound] + age * corrected.degradation[compound] == pytest.approx(
                original_baseline + original.compound_delta[compound] + age * original.degradation[compound]
            )
