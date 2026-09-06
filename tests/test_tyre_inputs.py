from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from wodata.artifacts import strategy_search_root, weekend_model_root, write_model_snapshot
from wodata.contracts import ModelSnapshot, ParameterEstimate, StrategySearchState
from woplanner.analysis.service import OfflineAnalysisService
from woplanner.analysis.service import ModelSummary
from woplanner.analysis.tyre_inputs import (
    TYRE_PARAMETERS,
    TyreInputService,
    tyre_snapshot_from_overrides,
)
from wostrategy.analysis.fp_tyre_evidence import write_fp_tyre_evidence


LEGACY = {
    "Deg_HARD": 0.049608730122481265,
    "Deg_MEDIUM": 0.09494384183899782,
    "Deg_SOFT": 0.16897262521052198,
    "Perf_HARD_vs_MEDIUM": 0.4645393488485038,
    "Perf_SOFT_vs_MEDIUM": -0.5470998943515507,
}


def write_prior(root: Path, year=2026, round_number=11):
    path = weekend_model_root(year, round_number, root) / "pre_race_prior_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "year": year,
                "round_number": round_number,
                "source_analysis_id": f"legacy-{year}-{round_number}",
                "source_sessions": ["FP1", "FP2", "FP3"],
                "source_created_at": "2026-08-01T00:00:00+00:00",
                "frozen_at": "2026-08-01T00:00:00+00:00",
                "construction": "test",
                "fuel": {"p10": 0.01, "median": 0.02, "p90": 0.03, "unit": "s/lap"},
                "degradation": {
                    "HARD": {"p10": 0.04, "median": LEGACY["Deg_HARD"], "p90": 0.06, "unit": "s/lap"},
                    "MEDIUM": {"p10": 0.08, "median": LEGACY["Deg_MEDIUM"], "p90": 0.11, "unit": "s/lap"},
                    "SOFT": {"p10": 0.14, "median": LEGACY["Deg_SOFT"], "p90": 0.19, "unit": "s/lap"},
                },
                "performance": {
                    "HARD": {"p10": 0.3, "median": 0.49205953068285274, "p90": 0.7, "unit": "s"},
                    "MEDIUM": {"p10": 0.027520181834348918, "median": 0.027520181834348918, "p90": 0.027520181834348918, "unit": "s"},
                    "SOFT": {"p10": -0.7, "median": -0.5195797125172017, "p90": -0.3, "unit": "s"},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_strategy_model(root: Path, year=2026, round_number=11):
    created = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = []
    for compound, degradation, performance in (
        ("HARD", LEGACY["Deg_HARD"], LEGACY["Perf_HARD_vs_MEDIUM"]),
        ("MEDIUM", LEGACY["Deg_MEDIUM"], 0.0),
        ("SOFT", LEGACY["Deg_SOFT"], LEGACY["Perf_SOFT_vs_MEDIUM"]),
    ):
        for parameter, value, unit in (
            ("degradation", degradation, "s/lap"),
            ("compound_delta", performance, "s"),
        ):
            rows.append(
                ParameterEstimate(
                    "model", "bank", "aggregate", 0, parameter, "FP",
                    None, compound, "MEDIUM", value - .01, value,
                    value + .01, unit, "measured", 10, 2, 100, .1,
                    0, 0, "input", "config", created,
                )
            )
    write_model_snapshot(
        ModelSnapshot(
            "model", "bank", "aggregate", 0, tuple(rows),
            "input", "config", created,
        ),
        year=year, round_number=round_number, data_root=root,
    )


def test_no_manual_is_exact_legacy_and_full_manual_is_exact_manual(tmp_path):
    write_prior(tmp_path)
    service = TyreInputService(tmp_path)

    baseline = service.resolve(2026, 11).by_parameter()
    assert {name: row.selected_value for name, row in baseline.items()} == pytest.approx(LEGACY)
    assert {row.selected_source for row in baseline.values()} == {"legacy_fp_derived"}

    manual = {name: index / 10 for index, name in enumerate(TYRE_PARAMETERS, 1)}
    service.save_manual_inputs(2026, 11, manual)
    resolved = service.resolve(2026, 11).by_parameter()
    assert {name: row.selected_value for name, row in resolved.items()} == manual
    assert {row.selected_source for row in resolved.values()} == {"manual"}


def test_partial_override_clear_restart_and_cross_event_isolation(tmp_path):
    write_prior(tmp_path, 2026, 11)
    write_prior(tmp_path, 2026, 12)
    first = TyreInputService(tmp_path)
    first.save_manual_inputs(
        2026,
        11,
        {
            "Deg_HARD": 0.07,
            "Deg_MEDIUM": None,
            "Deg_SOFT": 0.16,
            "Perf_HARD_vs_MEDIUM": 0.42,
            "Perf_SOFT_vs_MEDIUM": None,
        },
    )

    restarted = TyreInputService(tmp_path)
    rows = restarted.resolve(2026, 11).by_parameter()
    assert [rows[name].selected_source for name in TYRE_PARAMETERS] == [
        "manual", "legacy_fp_derived", "manual", "manual", "legacy_fp_derived"
    ]
    assert all(
        row.selected_source == "legacy_fp_derived"
        for row in restarted.resolve(2026, 12).parameters
    )
    restarted.clear_manual_value(2026, 11, "Deg_HARD")
    assert restarted.resolve(2026, 11).by_parameter()["Deg_HARD"].selected_value == pytest.approx(
        LEGACY["Deg_HARD"]
    )


def test_cache_reads_refreshes_and_manual_edits_never_calculate(tmp_path):
    write_prior(tmp_path)
    calls = []

    def calculator(**kwargs):
        calls.append(kwargs)
        return {"analysis_id": "explicit"}

    service = TyreInputService(tmp_path, fp_calculator=calculator)
    assert service.get_cached_fp_tyre_evidence(2026, 11).status == "not_calculated"
    service.resolve(2026, 11)
    service.set_manual_value(2026, 11, "Deg_HARD", 0.08)
    service.clear_manual_value(2026, 11, "Deg_HARD")
    assert calls == []

    assert service.calculate_fp_tyre_evidence(2026, 11) == {"analysis_id": "explicit"}
    assert len(calls) == 1
    assert service.calculate_fp_tyre_evidence(2026, 11, force_refresh=True) == {
        "analysis_id": "explicit"
    }
    assert len(calls) == 2


def test_cached_and_locally_stale_evidence_are_read_without_calculation(tmp_path):
    write_prior(tmp_path)
    calls = []
    service = TyreInputService(
        tmp_path, fp_calculator=lambda **kwargs: calls.append(kwargs) or {}
    )
    payload = {
        "schema_version": 1,
        "analysis_id": "cached",
        "sessions_used": ["FP1"],
        "source_fingerprints": {},
        "degradation_contrasts": [],
        "performance_support": [],
    }
    write_fp_tyre_evidence(payload, year=2026, round_number=11, data_root=tmp_path)
    cached = service.get_cached_fp_tyre_evidence(2026, 11)
    assert cached.status == "cached"
    assert calls == []

    from wodata import get_fastf1_session_laps_path

    fp2 = get_fastf1_session_laps_path(
        year=2026, round_number=11, session="FP2", data_root=tmp_path
    )
    fp2.parent.mkdir(parents=True, exist_ok=True)
    fp2.write_bytes(b"new local session")
    stale = service.get_cached_fp_tyre_evidence(2026, 11)
    assert stale.status == "cached_stale"
    assert "FP2" in stale.stale_reason
    assert stale.evidence["analysis_id"] == "cached"
    assert calls == []


def test_manual_survives_explicit_calculation_and_snapshot_is_immutable(tmp_path):
    write_prior(tmp_path)
    service = TyreInputService(
        tmp_path, fp_calculator=lambda **_kwargs: {"analysis_id": "new-evidence"}
    )
    service.set_manual_value(2026, 11, "Deg_HARD", 0.071)
    before = service.resolve(2026, 11)
    snapshot = tyre_snapshot_from_overrides(before.strategy_overrides(), before)

    service.calculate_fp_tyre_evidence(2026, 11)
    assert service.load_manual_inputs(2026, 11)["Deg_HARD"] == 0.071
    service.set_manual_value(2026, 11, "Deg_HARD", 0.099)

    assert snapshot["values"]["Deg_HARD"] == 0.071
    assert service.resolve(2026, 11).by_parameter()["Deg_HARD"].selected_value == 0.099


def test_strategy_search_persists_immutable_exact_tyre_snapshot(tmp_path):
    write_prior(tmp_path)
    write_strategy_model(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", 0.071)
    resolved_a = service.resolve_tyre_inputs(2026, 11)
    overrides = resolved_a.strategy_overrides()
    snapshot_a = tyre_snapshot_from_overrides(overrides, resolved_a)

    service.generate_continuations(
        year=2026,
        round_number=11,
        state=StrategySearchState(0, 12, "MEDIUM", 0, max_future_stops=1),
        pit_loss=20.0,
        k=2,
        overrides=overrides,
        tyre_input_snapshot=snapshot_a,
    )
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", 0.099)

    snapshots = list(
        (strategy_search_root(2026, 11, "global", tmp_path) / "tyre_input_snapshots")
        .glob("*.json")
    )
    assert len(snapshots) == 1
    persisted = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert persisted["values"]["Deg_HARD"] == 0.071
    assert service.resolve_tyre_inputs(2026, 11).by_parameter()["Deg_HARD"].selected_value == 0.099

    named_path = service.save_named_snapshot(2026, 11, "model-a", [])
    replay = service.load_named_snapshot(named_path)
    assert replay["tyre_input_snapshot"]["values"]["Deg_HARD"] == 0.071


def test_failed_explicit_recalculation_preserves_cached_evidence(tmp_path):
    write_prior(tmp_path)
    evidence_path = weekend_model_root(2026, 11, tmp_path) / "fp_tyre_evidence_v1.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "schema_version": 1,
        "year": 2026,
        "round_number": 11,
        "analysis_id": "successful-cache",
        "source_fingerprints": [],
    }
    evidence_path.write_text(json.dumps(original), encoding="utf-8")

    def fail_calculation(**_kwargs):
        raise RuntimeError("source unavailable")

    service = TyreInputService(tmp_path, fp_calculator=fail_calculation)
    with pytest.raises(RuntimeError, match="source unavailable"):
        service.calculate_fp_tyre_evidence(2026, 11, force_refresh=True)

    assert service.get_cached_fp_tyre_evidence(2026, 11).evidence == original


def test_provider_only_prediction_maps_to_five_planner_coordinates(tmp_path, monkeypatch):
    compounds = {
        "HARD": SimpleNamespace(
            degradation_seconds_per_lap=.08, performance_delta_to_medium=.42
        ),
        "MEDIUM": SimpleNamespace(
            degradation_seconds_per_lap=.13, performance_delta_to_medium=0.0
        ),
        "SOFT": SimpleNamespace(
            degradation_seconds_per_lap=.21, performance_delta_to_medium=-.36
        ),
    }
    prediction = SimpleNamespace(
        compounds=compounds,
        artifact_id="provider-artifact",
        provenance=(("path", "prediction.json"),),
    )
    monkeypatch.setattr(
        "woplanner.analysis.tyre_inputs.get_pre_race_tyre_prediction",
        lambda **_kwargs: prediction,
    )

    resolved = TyreInputService(tmp_path).resolve(2026, 12)

    assert {row.selected_source for row in resolved.parameters} == {
        "pre_race_prediction"
    }
    assert resolved.strategy_overrides() == pytest.approx({
        ("degradation", "HARD"): .08,
        ("degradation", "MEDIUM"): .13,
        ("degradation", "SOFT"): .21,
        ("compound_delta", "MEDIUM"): 0.0,
        ("compound_delta", "HARD"): .42,
        ("compound_delta", "SOFT"): -.36,
    })


def test_no_pre_race_artifact_supports_manual_only_and_reports_partial_missing(tmp_path):
    service = TyreInputService(tmp_path)
    empty = service.resolve(2099, 29)
    assert [row.selected_value for row in empty.parameters] == [None] * 5

    values = {name: (index + 1) / 100 for index, name in enumerate(TYRE_PARAMETERS)}
    service.save_manual_inputs(2099, 29, values)
    assert service.resolve(2099, 29).strategy_overrides() == pytest.approx({
        ("degradation", "HARD"): .01,
        ("degradation", "MEDIUM"): .02,
        ("degradation", "SOFT"): .03,
        ("compound_delta", "MEDIUM"): 0.0,
        ("compound_delta", "HARD"): .04,
        ("compound_delta", "SOFT"): .05,
    })

    service.clear_manual_value(2099, 29, "Perf_SOFT_vs_MEDIUM")
    with pytest.raises(ValueError, match="Perf_SOFT_vs_MEDIUM"):
        service.resolve(2099, 29).strategy_overrides()


def test_effective_resolver_derives_from_observed_medium_and_override_uses_base(tmp_path):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    resolved = service.resolve_tyre_inputs(2026, 11)
    rows = []
    for compound, degradation, delta in (
        ("HARD", .9, .9), ("MEDIUM", .17, 0.0), ("SOFT", .8, -.8)
    ):
        rows.extend((
            {"parameter": "degradation", "compound": compound, "median": degradation,
             "usable_lap_count": 12, "support_status": "measured", "session": "R"},
            {"parameter": "compound_delta", "compound": compound, "median": delta,
             "usable_lap_count": 12, "support_status": "measured", "session": "R"},
        ))
    summary = ModelSummary(
        {"analysis_id": "live", "as_of_leader_lap": 20,
         "sampler_quality": {"architecture": "live-retro-v1",
                             "observed_compounds": ["MEDIUM"]}},
        tuple(rows),
    )
    base = resolved.strategy_overrides()

    normal, _ = service.effective_tyre_inputs(summary, resolved, manual_override=False)
    overridden, _ = service.effective_tyre_inputs(summary, resolved, manual_override=True)

    assert normal[("degradation", "MEDIUM")] == pytest.approx(.17)
    assert normal[("degradation", "HARD")] == pytest.approx(
        .17 * base[("degradation", "HARD")] / base[("degradation", "MEDIUM")]
    )
    assert normal[("degradation", "SOFT")] == pytest.approx(
        .17 * base[("degradation", "SOFT")] / base[("degradation", "MEDIUM")]
    )
    assert normal[("compound_delta", "HARD")] == base[("compound_delta", "HARD")]
    assert normal[("compound_delta", "SOFT")] == base[("compound_delta", "SOFT")]
    assert overridden == base


def test_manual_only_base_supports_live_derivation_without_pre_race_artifact(tmp_path):
    service = OfflineAnalysisService(tmp_path)
    manual = {
        "Deg_HARD": .08,
        "Deg_MEDIUM": .10,
        "Deg_SOFT": .18,
        "Perf_HARD_vs_MEDIUM": .4,
        "Perf_SOFT_vs_MEDIUM": -.5,
    }
    service.tyre_inputs.save_manual_inputs(2026, 11, manual)
    resolved = service.resolve_tyre_inputs(2026, 11)
    assert resolved.legacy_artifact is None
    rows = []
    for compound, degradation, delta in (
        ("HARD", .20, .4), ("MEDIUM", .90, 0.0), ("SOFT", .90, -.5)
    ):
        rows.extend((
            {"parameter": "degradation", "compound": compound,
             "median": degradation, "usable_lap_count": 12,
             "support_status": "measured", "session": "R"},
            {"parameter": "compound_delta", "compound": compound,
             "median": delta, "usable_lap_count": 12,
             "support_status": "measured", "session": "R"},
        ))
    summary = ModelSummary(
        {"analysis_id": "live", "as_of_leader_lap": 31,
         "sampler_quality": {"architecture": "live-retro-v1",
                             "observed_compounds": ["HARD"]}},
        tuple(rows),
    )
    effective, assumptions = service.effective_tyre_inputs(
        summary, resolved, manual_override=False
    )
    sources = {row["compound"]: row["degradation_source"] for row in assumptions}
    assert effective[("degradation", "HARD")] == pytest.approx(.20)
    assert effective[("degradation", "MEDIUM")] == pytest.approx(.25)
    assert effective[("degradation", "SOFT")] == pytest.approx(.45)
    assert sources == {
        "SOFT": "live_derived", "MEDIUM": "live_derived", "HARD": "live_direct"
    }
    overridden, override_rows = service.effective_tyre_inputs(
        summary, resolved, manual_override=True
    )
    assert overridden == resolved.strategy_overrides()
    assert {row["degradation_source"] for row in override_rows} == {"base"}


def test_strategy_model_does_not_invent_missing_compound_delta():
    summary = ModelSummary(
        {"analysis_id": "partial"},
        (
            {"parameter": "degradation", "compound": "SOFT", "median": .2},
            {"parameter": "degradation", "compound": "MEDIUM", "median": .1},
            {"parameter": "compound_delta", "compound": "MEDIUM", "median": 0.0},
        ),
    )
    with pytest.raises(ValueError, match="both a delta and degradation"):
        OfflineAnalysisService().build_strategy_model(summary)
