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


def test_partial_strategy_model_overrides_align_before_retaining_other_compounds():
    service = OfflineAnalysisService()
    base = summary({"HARD": 0., "MEDIUM": -.4, "SOFT": -.9})
    model = service.build_strategy_model(base, {
        ("compound_delta", "MEDIUM"): 0., ("compound_delta", "SOFT"): -.7,
    })
    assert_predictions(model.compound_delta, {"HARD": .4, "MEDIUM": 0., "SOFT": -.7})
    assert model.degradation == DEGRADATION


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


def test_incompatible_source_metadata_is_refused_before_aggregation(tmp_path):
    from woplanner.analysis.compound_offsets import validate_offset_rows
    with pytest.raises(ValueError, match="seconds"):
        validate_offset_rows([dict(parameter="compound_delta", unit="s/lap")])
    with pytest.raises(ValueError, match="Unsupported"):
        validate_offset_rows([dict(parameter="compound_delta", reference_compound="UNKNOWN")])
    service = OfflineAnalysisService()
    mixed = ModelSummary({}, tuple(summary({"HARD": 0.}).parameters + summary({"MEDIUM": 0.}, "MEDIUM").parameters))
    with pytest.raises(ValueError, match="Mixed"):
        service.build_strategy_model(mixed)
    with pytest.raises(ValueError, match="Mixed"):
        service.strategy_assumptions(mixed)
    files = []
    for compound in ("HARD", "MEDIUM"):
        path = tmp_path / f"{compound}.csv"
        path.write_text(f"parameter,compound,median,reference_compound\ncompound_delta,{compound},0,{compound}\n")
        files.append(path)
    with pytest.raises(ValueError, match="Mixed"):
        service.load_tyre_information_files(files)


def test_effective_resolver_and_persisted_raw_model_round_trip(tmp_path):
    from datetime import datetime, timezone
    from wodata.artifacts import write_model_snapshot
    from wodata.contracts import ModelSnapshot, ParameterEstimate
    from woplanner.analysis.compound_offsets import COMPOUND_OFFSET_CONTRACT
    from woplanner.analysis.tyre_inputs import tyre_snapshot_from_overrides

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
    effective, assumptions = service.effective_tyre_inputs(loaded, resolved, manual_override=False)
    expected = {"HARD": .6, "MEDIUM": 0., "SOFT": -.5}
    assert_predictions({c: effective[("compound_delta", c)] for c in COMPOUNDS}, expected)
    assert {r["compound_offset_contract"] for r in assumptions} == {COMPOUND_OFFSET_CONTRACT}
    captured = tyre_snapshot_from_overrides(effective, resolved)
    restored = json.loads(json.dumps(captured))
    assert restored["compound_offset_contract"] == COMPOUND_OFFSET_CONTRACT
    assert restored["values"]["Perf_SOFT_vs_MEDIUM"] == pytest.approx(-.5)
    assert restored["values"]["Perf_HARD_vs_MEDIUM"] == pytest.approx(.6)
    manual, _ = service.effective_tyre_inputs(loaded, resolved, manual_override=True)
    assert manual == resolved.strategy_overrides()
    # Serialization retains original fitting coordinates, not a rewritten model.
    assert [float(r["median"]) for r in loaded.parameters] == [r.median for r in parameters]


def test_canonical_csv_ignores_unrelated_missing_or_invalid_references(tmp_path):
    path = tmp_path / "latest_parameters.csv"
    path.write_text(
        "parameter,compound,median,usable_lap_count,reference_compound,unit\n"
        "degradation,SOFT,0.2,10,,s/lap\n"
        "degradation,HARD,0.08,10,NaN,s/lap\n"
        "degradation,MEDIUM,0.1,10,UNRELATED,s/lap\n"
        "fuel_rate,,0.04,30,,s/lap\n"
        "compound_delta,HARD,0,10,HARD,s\n"
        "compound_delta,MEDIUM,-0.6,10,HARD,s\n"
    )
    rows, reference = OfflineAnalysisService().load_tyre_information_files([path])
    assert reference == "HARD"
    assert {r["compound"]: r["degradation"] for r in rows} == (DEGRADATION | {"SOFT": .2})
    assert {r["compound"]: r["compound_delta"] for r in rows if "compound_delta" in r} == {
        "HARD": 0., "MEDIUM": -.6,
    }


@pytest.mark.parametrize("reference", (None, "MEDIUM"))
def test_legacy_r15_l6_resolved_csv_is_rejected_even_with_reference(tmp_path, reference):
    import csv

    path = tmp_path / "legacy_history.csv"
    fields = ["parameter", "compound", "median", "compound_delta_source"]
    if reference is not None:
        fields.append("reference_compound")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for compound, value, source in (
            ("SOFT", -.32380563412992786, "resolved_pre_race_baseline"),
            ("MEDIUM", -.6001978479198112, "race_evidence"),
            ("HARD", .3258440819351384, "resolved_pre_race_baseline"),
        ):
            row = dict(parameter="compound_delta", compound=compound, median=value,
                       compound_delta_source=source)
            if reference is not None:
                row["reference_compound"] = reference
            writer.writerow(row)
    with pytest.raises(ValueError, match="Unverified.*compound offsets"):
        OfflineAnalysisService().load_tyre_information_files([path])


@pytest.mark.parametrize("reference", ("", "NaN", "MEDIUM"))
def test_current_contract_csv_is_importable_with_missing_reference(tmp_path, reference):
    from woplanner.analysis.compound_offsets import COMPOUND_OFFSET_CONTRACT

    path = tmp_path / "current_history.csv"
    path.write_text(
        "parameter,compound,median,reference_compound,compound_offset_contract,compound_delta_source\n"
        + "".join(f"compound_delta,{c},{v},{reference},{COMPOUND_OFFSET_CONTRACT},race_evidence\n"
                  for c, v in (("HARD", .6), ("MEDIUM", 0.), ("SOFT", -.5)))
    )
    rows, loaded_reference = OfflineAnalysisService().load_tyre_information_files([path])
    assert {r["compound"]: r["compound_delta"] for r in rows} == {
        "HARD": .6, "MEDIUM": 0., "SOFT": -.5,
    }
    assert loaded_reference == ("MEDIUM" if reference == "MEDIUM" else None)


@pytest.mark.parametrize("include_reference_column", (False, True))
def test_anonymous_external_csv_does_not_inherit_in_memory_trust(tmp_path, include_reference_column):
    path = tmp_path / "anonymous.csv"
    # Exercise both an absent column and a blank reference cell.
    path.write_text(
        "parameter,compound,median" + (",reference_compound" if include_reference_column else "")
        + "\ncompound_delta,SOFT,-0.5" + ("," if include_reference_column else "") + "\n"
    )
    with pytest.raises(ValueError, match="Unverified"):
        OfflineAnalysisService().load_tyre_information_files([path])
    assert_predictions(
        resolve({"HARD": .4, "MEDIUM": 0., "SOFT": -.5}, {"MEDIUM": 0., "SOFT": -.7}, None),
        {"HARD": .4, "MEDIUM": 0., "SOFT": -.7},
    )


@pytest.mark.parametrize("reference,contract,match", [
    ("UNKNOWN", "shared_compound_alignment_v1", "Unsupported"),
    ("MEDIUM", "old_contract", "Unverified"),
    ("MEDIUM", "", "Unverified"),
])
def test_declared_invalid_csv_metadata_is_not_bypassed(tmp_path, reference, contract, match):
    path = tmp_path / "invalid.csv"
    path.write_text(
        "parameter,compound,median,reference_compound,compound_offset_contract\n"
        f"compound_delta,MEDIUM,0,{reference},{contract}\n"
    )
    with pytest.raises(ValueError, match=match):
        OfflineAnalysisService().load_tyre_information_files([path])


@pytest.mark.parametrize("layout", ("combined", "review"))
@pytest.mark.parametrize("verified", (False, True))
def test_other_csv_layouts_enforce_the_same_provenance_boundary(tmp_path, layout, verified):
    from woplanner.analysis.compound_offsets import COMPOUND_OFFSET_CONTRACT

    if layout == "combined":
        path = tmp_path / "tyre_information.csv"
        header = "Compound,DegradationMedianSecondsPerLap,CompoundDeltaMedianSeconds"
        value = "SOFT,0.2,-0.5"
    else:
        path = tmp_path / "summary_compound_delta.csv"
        header = "Compound,Median"
        value = "SOFT,-0.5"
    if verified:
        header += ",compound_offset_contract"
        value += "," + COMPOUND_OFFSET_CONTRACT
    path.write_text(header + "\n" + value + "\n")
    service = OfflineAnalysisService()
    if not verified:
        with pytest.raises(ValueError, match="Unverified"):
            service.load_tyre_information_files([path])
    else:
        rows, reference = service.load_tyre_information_files([path])
        assert rows[0]["compound_delta"] == -.5
        assert reference is None


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
