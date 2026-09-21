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
    assert resolved.semantics["performance"]["reference_compound"] == "MEDIUM"
    snapshot = tyre_snapshot_from_overrides(resolved.strategy_overrides(), resolved)
    assert snapshot["semantics"] == resolved.semantics


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


def team_summary(*, ferrari_medium=.15, mclaren_medium=.22):
    rows = []
    for compound, degradation, delta in (
        ("HARD", .20, .7), ("MEDIUM", .30, 0.0), ("SOFT", .40, -.6)
    ):
        for parameter, value in (("degradation", degradation), ("compound_delta", delta)):
            rows.append({
                "parameter": parameter, "compound": compound, "median": value,
                "usable_lap_count": 20, "support_status": "measured", "session": "R",
            })
    for team, compound, value in (
        ("Ferrari", "HARD", .12), ("Ferrari", "MEDIUM", ferrari_medium),
        ("McLaren", "MEDIUM", mclaren_medium),
    ):
        rows.append({
            "parameter": "team_degradation", "team": team, "compound": compound,
            "median": value, "p10": value - .03, "p90": value + .04,
            "usable_lap_count": 10, "usable_run_count": 2, "ess": 777,
            "support_status": "measured", "session": "R",
        })
    return ModelSummary(
        {"analysis_id": "team-live", "as_of_leader_lap": 35,
         "sampler_quality": {"architecture": "live-retro-v1", "ess": 777,
                             "observed_compounds": ["HARD", "MEDIUM", "SOFT"]}},
        tuple(rows),
    )


def test_manual_scopes_preserve_v1_global_and_two_teams_across_restart(tmp_path):
    write_prior(tmp_path)
    service = TyreInputService(tmp_path)
    # A legacy schema-v1 file has only the Global values mapping.
    service.set_manual_value(2026, 11, "Deg_HARD", .91)
    assert "team_values" not in json.loads(service.manual_path(2026, 11).read_text())
    service.set_manual_value(2026, 11, "Deg_HARD", .11, team="Ferrari")
    service.set_manual_value(2026, 11, "Perf_SOFT_vs_MEDIUM", -.8, team="McLaren")
    service.set_manual_value(2026, 11, "Perf_HARD_vs_MEDIUM", .9)
    restarted = TyreInputService(tmp_path)
    assert restarted.resolve(2026, 11).by_parameter()["Deg_HARD"].selected_value == .91
    ferrari = restarted.resolve(2026, 11, team="Ferrari")
    assert ferrari.team == "Ferrari"
    assert ferrari.by_parameter()["Deg_HARD"].selected_value == .11
    assert ferrari.by_parameter()["Perf_HARD_vs_MEDIUM"].selected_value == pytest.approx(LEGACY["Perf_HARD_vs_MEDIUM"])
    mclaren = restarted.resolve(2026, 11, team="McLaren")
    assert mclaren.by_parameter()["Deg_HARD"].selected_value == LEGACY["Deg_HARD"]
    assert mclaren.by_parameter()["Perf_SOFT_vs_MEDIUM"].selected_value == -.8
    restarted.clear_all_manual_values(2026, 11, team="Ferrari")
    assert restarted.resolve(2026, 11, team="Ferrari").by_parameter()["Deg_HARD"].selected_value == LEGACY["Deg_HARD"]
    assert restarted.resolve(2026, 11, team="McLaren").by_parameter()["Perf_SOFT_vs_MEDIUM"].selected_value == -.8
    assert restarted.resolve(2026, 11).by_parameter()["Deg_HARD"].selected_value == .91
    restarted.clear_manual_value(2026, 11, "Perf_SOFT_vs_MEDIUM", team="McLaren")
    assert restarted.resolve(2026, 11, team="McLaren").by_parameter()["Perf_SOFT_vs_MEDIUM"].selected_value == pytest.approx(LEGACY["Perf_SOFT_vs_MEDIUM"])


def test_team_effective_rates_are_absolute_and_missing_use_nonmanual_initial_ratios(tmp_path):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.tyre_inputs.save_manual_inputs(2026, 11, {
        "Deg_HARD": .91, "Deg_MEDIUM": .99, "Deg_SOFT": .92,
        "Perf_HARD_vs_MEDIUM": 9, "Perf_SOFT_vs_MEDIUM": -9,
    })
    summary = team_summary()
    assert service.strategy_teams(summary) == ("Ferrari", "McLaren")
    effective = {}
    for team in (None, "Ferrari", "McLaren", "Ferrari", None):
        resolved = service.resolve_tyre_inputs(2026, 11, team=team)
        values, rows = service.effective_tyre_inputs(summary, resolved, manual_override=False)
        effective[team] = values
        assert values[("compound_delta", "HARD")] == .7
        assert values[("compound_delta", "SOFT")] == -.6
        assert all(row["performance_model_scope"] == "global" for row in rows)
        if team:
            assert all(row["compound_delta_source"] == "global_race_evidence" for row in rows)
    assert effective[None][("degradation", "MEDIUM")] == .30
    assert effective["Ferrari"][("degradation", "MEDIUM")] == .15
    assert effective["Ferrari"][("degradation", "HARD")] == .12
    assert effective["Ferrari"][("degradation", "SOFT")] == pytest.approx(.15 * LEGACY["Deg_SOFT"] / LEGACY["Deg_MEDIUM"])
    assert effective["McLaren"][("degradation", "HARD")] == pytest.approx(.22 * LEGACY["Deg_HARD"] / LEGACY["Deg_MEDIUM"])
    assert effective["McLaren"][("degradation", "SOFT")] == pytest.approx(.22 * LEGACY["Deg_SOFT"] / LEGACY["Deg_MEDIUM"])
    # No team observations uses the event defaults, never Global manuals.
    absent = service.resolve_tyre_inputs(2026, 11, team="Williams")
    values, rows = service.effective_tyre_inputs(summary, absent, manual_override=False)
    assert values[("degradation", "HARD")] == LEGACY["Deg_HARD"]
    assert {row["degradation_source"] for row in rows} == {"base"}


def test_team_manual_and_global_active_manual_survive_live_updates_and_switches(tmp_path):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.set_manual_tyre_input(2026, 11, "Deg_MEDIUM", .51, team="Ferrari")
    service.set_manual_tyre_input(2026, 11, "Perf_HARD_vs_MEDIUM", .81, team="Ferrari")
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", .62, team="McLaren")
    global_manual = {("degradation", "HARD"): .71, ("compound_delta", "SOFT"): -.91}
    for summary in (team_summary(), team_summary(ferrari_medium=.25, mclaren_medium=.32)):
        for team in (None, "Ferrari", "McLaren", None, "Ferrari"):
            resolved = service.resolve_tyre_inputs(2026, 11, team=team)
            values, rows = service.effective_tyre_inputs(
                summary, resolved, manual_override=False,
                manual_values=global_manual if team is None else None,
            )
            if team is None:
                assert values[("degradation", "HARD")] == .71
                assert values[("compound_delta", "SOFT")] == -.91
            elif team == "Ferrari":
                assert values[("degradation", "MEDIUM")] == .51
                assert values[("compound_delta", "HARD")] == .81
                assert values[("degradation", "HARD")] == .12
                direct_medium = next(
                    row["median"] for row in summary.parameters
                    if row.get("team") == "Ferrari" and row["compound"] == "MEDIUM"
                )
                assert values[("degradation", "SOFT")] == pytest.approx(
                    direct_medium * LEGACY["Deg_SOFT"] / LEGACY["Deg_MEDIUM"]
                )
            else:
                assert values[("degradation", "HARD")] == .62
                assert values[("compound_delta", "HARD")] == .7
            frozen = tyre_snapshot_from_overrides(values, resolved, assumptions=rows)
            assert "unattributed_override" not in frozen["selected_sources"].values()
            assert frozen["team"] == team
            if team == "Ferrari":
                assert frozen["values"]["Deg_MEDIUM"] == .51
                assert frozen["selected_sources"]["Deg_MEDIUM"] == "manual"
                assert frozen["selected_sources"]["Deg_HARD"] == "team_live_direct"
                assert frozen["selected_sources"]["Deg_SOFT"] == "team_live_derived"
                assert frozen["selected_sources"]["Perf_HARD_vs_MEDIUM"] == "manual"
                manual_row = next(row for row in rows if row["compound"] == "MEDIUM")
                assert manual_row["degradation_support_status"] == "manual"
                assert "p10" not in manual_row and "p90" not in manual_row and "ess" not in manual_row


def test_effective_snapshot_does_not_claim_stale_source_and_normalizes_reference(tmp_path):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    resolved = service.resolve_tyre_inputs(2026, 11)
    values, rows = service.effective_tyre_inputs(team_summary(), resolved, manual_override=False)
    snapshot = tyre_snapshot_from_overrides(values, resolved, assumptions=rows)
    assert snapshot["values"]["Deg_MEDIUM"] == .3
    assert snapshot["selected_sources"]["Deg_MEDIUM"] == "live_direct"
    legacy_call = tyre_snapshot_from_overrides(values, resolved)
    assert legacy_call["selected_sources"]["Deg_MEDIUM"] == "unattributed_override"
    with pytest.raises(ValueError, match="fixed reference and must be 0"):
        service.effective_tyre_inputs(
            team_summary(), resolved, manual_override=False,
            manual_values={("compound_delta", "MEDIUM"): .2},
        )
    with pytest.raises(ValueError, match="fixed reference and must be 0"):
        tyre_snapshot_from_overrides(
            values, resolved, assumptions=rows,
            manual_values={("compound_delta", "MEDIUM"): .2},
        )
    frozen = tyre_snapshot_from_overrides(
        values, resolved, assumptions=rows,
        manual_values={("compound_delta", "MEDIUM"): 0.0},
    )
    assert frozen["values"]["Perf_HARD_vs_MEDIUM"] == pytest.approx(.7)
    assert frozen["values"]["Perf_SOFT_vs_MEDIUM"] == pytest.approx(-.6)
    assert frozen["selected_sources"]["Perf_HARD_vs_MEDIUM"] == "race_evidence"
    assert frozen["selected_sources"]["Perf_SOFT_vs_MEDIUM"] == "race_evidence"


def test_team_resolution_rejects_global_manual_package(tmp_path):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    with pytest.raises(ValueError, match="selected team"):
        service.effective_tyre_inputs(
            team_summary(), service.resolve_tyre_inputs(2026, 11),
            manual_override=False, team="Ferrari",
        )


def test_team_live_snapshot_persists_exact_sources_and_values(tmp_path):
    write_prior(tmp_path)
    write_strategy_model(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", .19, team="Ferrari")
    resolved = service.resolve_tyre_inputs(2026, 11, team="Ferrari")
    values, rows = service.effective_tyre_inputs(team_summary(), resolved, manual_override=False)
    snapshot = tyre_snapshot_from_overrides(values, resolved, assumptions=rows)
    service.generate_continuations(
        year=2026, round_number=11,
        state=StrategySearchState(0, 12, "MEDIUM", 0, max_future_stops=1),
        pit_loss=20.0, k=2, overrides=values, tyre_input_snapshot=snapshot,
    )
    service.set_manual_tyre_input(2026, 11, "Deg_HARD", .99, team="Ferrari")
    paths = list((strategy_search_root(2026, 11, "global", tmp_path) / "tyre_input_snapshots").glob("*.json"))
    assert len(paths) == 1
    frozen = json.loads(paths[0].read_text())
    assert frozen["team"] == "Ferrari"
    assert frozen["values"]["Deg_HARD"] == .19
    assert frozen["values"]["Deg_MEDIUM"] == .15
    assert frozen["selected_sources"]["Deg_HARD"] == "manual"
    assert frozen["selected_sources"]["Deg_MEDIUM"] == "team_live_direct"
    assert frozen["selected_sources"]["Deg_SOFT"] == "team_live_derived"
    assert frozen["selected_sources"]["Perf_HARD_vs_MEDIUM"] == "global_race_evidence"
    assert service.load_named_snapshot(service.save_named_snapshot(2026, 11, "team-a", []))["tyre_input_snapshot"] == frozen


def r12_reference_summary():
    summary = team_summary()
    hard_reference_deltas = {"HARD": 0.0, "MEDIUM": -.297279, "SOFT": -.665295}
    return ModelSummary(summary.manifest, tuple(
        dict(row, median=hard_reference_deltas[row["compound"]], reference_compound="HARD")
        if row["parameter"] == "compound_delta" else row
        for row in summary.parameters
    ))


@pytest.mark.parametrize("team", [None, "Ferrari"])
def test_r12_live_performance_rebases_to_medium_without_changing_differences(tmp_path, team):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    resolved = service.resolve_tyre_inputs(2026, 11, team=team)
    summary = r12_reference_summary()
    previous = None
    for _ in range(3):
        values, rows = service.effective_tyre_inputs(summary, resolved, manual_override=False)
        assert values[("compound_delta", "MEDIUM")] == 0.0
        assert values[("compound_delta", "HARD")] == pytest.approx(.297279)
        assert values[("compound_delta", "SOFT")] == pytest.approx(-.368016)
        assert values[("compound_delta", "HARD")] - values[("compound_delta", "SOFT")] == pytest.approx(.665295)
        raw_deltas = {"HARD": 0.0, "MEDIUM": -.297279, "SOFT": -.665295}
        for row in rows:
            assert row["compound_delta_model_reference_offset"] == -.297279
            assert (
                row["compound_delta"] + row["compound_delta_model_reference_offset"]
            ) == raw_deltas[row["compound"]]
        frozen = tyre_snapshot_from_overrides(values, resolved, assumptions=rows)
        assert frozen["values"]["Perf_HARD_vs_MEDIUM"] == values[("compound_delta", "HARD")]
        assert frozen["values"]["Perf_SOFT_vs_MEDIUM"] == values[("compound_delta", "SOFT")]
        source = "global_race_evidence" if team else "race_evidence"
        assert frozen["selected_sources"]["Perf_HARD_vs_MEDIUM"] == source
        assert frozen["selected_sources"]["Perf_SOFT_vs_MEDIUM"] == source
        if previous is not None:
            assert values == previous
        previous = values


@pytest.mark.parametrize("team", [None, "Ferrari"])
@pytest.mark.parametrize("storage", ["persisted", "explicit"])
def test_r12_manual_performance_is_exact_medium_difference_on_every_resolution(tmp_path, team, storage):
    write_prior(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    if storage == "persisted":
        service.set_manual_tyre_input(2026, 11, "Perf_HARD_vs_MEDIUM", .81, team=team)
    # Preserve Global's existing Manual Override activation rule; Team manual
    # and explicit active-window overrides always win over automatic evidence.
    global_base_override = team is None and storage == "persisted"
    manual = {("compound_delta", "HARD"): .81, ("compound_delta", "MEDIUM"): 0.0}
    previous = None
    for _ in range(3):
        resolved = service.resolve_tyre_inputs(2026, 11, team=team)
        values, rows = service.effective_tyre_inputs(
            r12_reference_summary(), resolved, manual_override=global_base_override,
            manual_values=manual if storage == "explicit" else None,
        )
        assert values[("compound_delta", "MEDIUM")] == 0.0
        assert values[("compound_delta", "HARD")] == .81
        assert {row["compound_delta_model_reference_offset"] for row in rows} == {
            0.0 if global_base_override else -.297279
        }
        frozen = tyre_snapshot_from_overrides(
            values, resolved, assumptions=rows,
            manual_values=manual if storage == "explicit" else None,
        )
        assert frozen["values"]["Perf_HARD_vs_MEDIUM"] == .81
        assert frozen["selected_sources"]["Perf_HARD_vs_MEDIUM"] == "manual"
        if not global_base_override:
            assert values[("compound_delta", "SOFT")] == pytest.approx(-.368016)
            assert frozen["selected_sources"]["Perf_SOFT_vs_MEDIUM"] == (
                "global_race_evidence" if team else "race_evidence"
            )
        if previous is not None:
            assert values == previous
        previous = values
