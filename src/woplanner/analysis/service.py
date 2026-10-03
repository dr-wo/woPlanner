from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd
import numpy as np

from wodata import get_fastf1_session_laps_path
from wodata.artifacts import (
    read_latest_model_snapshot,
    read_weekend_model_config,
    write_weekend_model_config,
    read_strategy_records,
    strategy_search_root,
    weekend_model_root,
    write_joint_candidate_costs,
    write_model_snapshot,
    write_strategy_records,
)
from wodata.contracts import StrategySearchState, canonical_hash
from wostrategy.algorithm.exact_strategy_search import (
    StrategyModel,
    StrategyResult,
    StrategyRules,
    results_to_records,
    search_best_compound_sequences,
)
from wostrategy.core.pre_race_session_data import prepare_weekend_sessions
from wostrategy.core.race_performance_settings import resolve_race_performance_settings
from wostrategy.model.pre_race_performance import WeekendModelConfig, derive_race_seed
from wostrategy.model.race_specific_performance import (
    FrozenPreRacePrior,
    MarginalPrior,
    run_race_specific_model,
)
from wostrategy.model.live_retro_performance import run_live_retro_model
from wostrategy.algorithm.monte_carlo_race_performance import (
    MonteCarloRacePerformanceConfig,
)
from wostrategy.analysis.traffic import (
    COMBINATION_MODE_COMPATIBILITY_MIN,
    SECTOR_BOUNDARY_FALLBACK,
    TRAFFIC_EVALUATOR_VERSION,
    TrafficEvaluationConfig,
    combine_lap_traffic_components,
    evaluate_direct_fastf1_style,
    evaluate_physical_traffic,
)
from wostrategy.analysis.traffic import traffic_clean_mask
from wostrategy.analysis.long_run_performance import (
    prepare_and_select_clean_air_laps,
    select_consecutive_clean_air_runs,
)

from .compound_offsets import merge_compound_offsets, validate_offset_rows
from .tyre_inputs import ResolvedTyreInputs, TyreInputService


@dataclass(frozen=True)
class ModelSummary:
    manifest: dict[str, object]
    parameters: tuple[dict[str, str], ...]

    @property
    def status(self) -> str:
        return str(self.manifest.get("status") or "unavailable")


LIVE_TRAFFIC_QUARANTINE_REASON = (
    "Automatic Race traffic is unavailable: position-derived gap decisions are not validated; "
    "the zero-origin estimator is disabled. Existing model state is retained; no Race update "
    "was produced. Sector-boundary mode requires explicit selection."
)


@dataclass(frozen=True)
class LiveRecalculationResult:
    status: str
    leader_lap: int
    eligible_lap_count: int
    analysis_id: str | None = None
    previous_clean_lap_count: int = 0
    clean_lap_sources: tuple[tuple[str, int], ...] = ()
    elapsed_seconds: float = 0.0
    sample_count: int = 0
    runtime_breakdown: tuple[tuple[str, float], ...] = ()
    sampler_quality: tuple[tuple[str, object], ...] = ()
    exclusion_diagnostics: tuple[tuple[str, int], ...] = ()
    traffic_unavailable_reason: str | None = None
    model_state_retained: bool = False


class OfflineAnalysisService:
    """Planner-facing adapter for canonical woData model/strategy artifacts."""

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = data_root
        self.tyre_inputs = TyreInputService(data_root)
        self.pinned_strategy_ids: list[str] = []
        self._last_live_fingerprint: str | None = None
        self._last_clean_lap_count = 0
        self._last_clean_lap_sources: tuple[tuple[str, int], ...] = ()
        self._ephemeral_event: tuple[int, int] | None = None
        self._ephemeral_result = None
        self._ephemeral_summary: ModelSummary | None = None
        self._ephemeral_parameter_rows: list[dict[str, object]] = []
        self._ephemeral_run_rows: list[dict[str, object]] = []
        self._last_strategy_search = None
        self._last_live_calculation_inputs: dict[str, object] | None = None

    @property
    def last_live_calculation_inputs(self) -> dict[str, object] | None:
        """Resolved values consumed by the most recent live calculation.

        This deliberately excludes lookup paths and timestamps. It is captured
        at construction of the objects passed to the model, rather than rebuilt
        from files after the calculation.
        """
        if self._last_live_calculation_inputs is None:
            return None
        return dict(self._last_live_calculation_inputs)

    def get_cached_fp_tyre_evidence(self, year: int, round_number: int):
        """Read/cache-check FP evidence without ever invoking calculation."""
        return self.tyre_inputs.get_cached_fp_tyre_evidence(year, round_number)

    def calculate_fp_tyre_evidence(
        self, year: int, round_number: int, *, force_refresh: bool = False
    ) -> dict[str, object]:
        """Explicit user-action bridge to the public woStrategy Python API."""
        return self.tyre_inputs.calculate_fp_tyre_evidence(
            year, round_number, force_refresh=force_refresh
        )

    def resolve_tyre_inputs(self, year: int, round_number: int) -> ResolvedTyreInputs:
        return self.tyre_inputs.resolve(year, round_number)

    def set_manual_tyre_input(
        self, year: int, round_number: int, parameter: str, value: float
    ) -> Path:
        return self.tyre_inputs.set_manual_value(year, round_number, parameter, value)

    def clear_manual_tyre_input(
        self, year: int, round_number: int, parameter: str
    ) -> Path:
        return self.tyre_inputs.clear_manual_value(year, round_number, parameter)

    def load_model(
        self, year: int, round_number: int, source_scope: str = "aggregate"
    ) -> ModelSummary:
        if (
            source_scope.upper() == "AGGREGATE"
            and self._ephemeral_event == (int(year), int(round_number))
            and self._ephemeral_summary is not None
        ):
            return self._ephemeral_summary
        manifest, rows = read_latest_model_snapshot(
            year=year,
            round_number=round_number,
            source_scope=source_scope,
            data_root=self.data_root,
        )
        return ModelSummary(manifest, tuple(rows))

    def build_strategy_model(
        self,
        summary: ModelSummary,
        overrides: Mapping[tuple[str, str], float] | None = None,
    ) -> StrategyModel:
        degradation: dict[str, float] = {}
        deltas: dict[str, float] = {}
        weighted_values: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for row in summary.parameters:
            compound = str(row.get("compound") or "").upper()
            parameter = str(row.get("parameter") or "")
            if not compound or parameter not in {"degradation", "compound_delta"}:
                continue
            try:
                value = float(row["median"])
                weight = max(float(row.get("usable_lap_count") or 1.0), 1.0)
            except (KeyError, TypeError, ValueError):
                continue
            weighted_values.setdefault((parameter, compound), []).append((value, weight))
        for (parameter, compound), values in weighted_values.items():
            total_weight = sum(weight for _, weight in values)
            value = sum(value * weight for value, weight in values) / total_weight
            if parameter == "degradation":
                degradation[compound] = value
            else:
                deltas[compound] = value
        for (parameter, compound), value in (overrides or {}).items():
            compound = str(compound).upper()
            if parameter == "degradation":
                degradation[compound] = float(value)
            elif parameter == "compound_delta":
                deltas[compound] = float(value)
        if not degradation:
            raise ValueError("The selected model has no compound degradation parameters.")
        return StrategyModel(
            deltas,
            degradation,
            analysis_id=str(summary.manifest.get("analysis_id") or "unknown"),
            version=str(summary.manifest.get("config_hash") or "unknown"),
        )

    def strategy_assumptions(
        self,
        summary: ModelSummary,
        *,
        baseline_degradation: Mapping[str, float] | None = None,
        baseline_compound_delta: Mapping[str, float] | None = None,
        initial_degradation: Mapping[str, float] | None = None,
        baseline_degradation_sources: Mapping[str, str] | None = None,
        race_informed_compounds: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        """Resolve direct and fixed-initial-ratio-derived Race tyre coordinates."""
        model_values = _summary_tyre_coordinates(summary)
        base_degradation = {
            str(compound).upper(): float(value)
            for compound, value in (baseline_degradation or {}).items()
        }
        ratio_degradation = {
            str(compound).upper(): float(value)
            for compound, value in (initial_degradation or base_degradation).items()
        }
        degradation_sources = {
            str(compound).upper(): str(value)
            for compound, value in (baseline_degradation_sources or {}).items()
        }
        baseline_deltas = {
            str(compound).upper(): float(value)
            for compound, value in (baseline_compound_delta or {}).items()
        }
        informed_coordinates = _race_informed_coordinates(summary)
        informed_coordinates.update(
            ("degradation", str(value).upper())
            for value in (race_informed_compounds or ())
        )
        direct_degradation = {
            compound: model_values[("degradation", compound)]
            for parameter, compound in informed_coordinates
            if parameter == "degradation"
            and ("degradation", compound) in model_values
        }
        degradation_ratios = _initial_degradation_ratios(ratio_degradation)
        anchor = _preferred_degradation_anchor(
            summary, direct_degradation, degradation_ratios
        )
        medium_scale = (
            direct_degradation[anchor] / degradation_ratios[anchor]
            if anchor is not None else None
        )
        resolved_degradation = dict(base_degradation)
        degradation_states = {compound: "base" for compound in base_degradation}
        for compound, value in direct_degradation.items():
            resolved_degradation[compound] = value
            degradation_states[compound] = "live_direct"
        if medium_scale is not None:
            for compound, ratio in degradation_ratios.items():
                if (
                    compound in resolved_degradation
                    and compound not in direct_degradation
                ):
                    resolved_degradation[compound] = medium_scale * ratio
                    degradation_states[compound] = "live_derived"
        direct_deltas = {
            compound: model_values[(parameter, compound)]
            for parameter, compound in informed_coordinates
            if parameter == "compound_delta" and (parameter, compound) in model_values
        }
        baseline_deltas = merge_compound_offsets(baseline_deltas, direct_deltas)
        fastest = min(baseline_deltas.values(), default=0.0)
        rows = []
        for compound in ("SOFT", "MEDIUM", "HARD"):
            if compound not in resolved_degradation:
                continue
            contributing = [
                str(row.get("session") or "")
                for row in summary.parameters
                if str(row.get("parameter")) == "degradation"
                and str(row.get("compound") or "").upper() == compound
            ]
            if compound not in baseline_deltas:
                continue
            delta = float(baseline_deltas[compound])
            degradation_informed = compound in direct_degradation
            delta_informed = ("compound_delta", compound) in informed_coordinates
            degradation_state = degradation_states.get(compound, "base")
            degradation_anchor = (
                anchor
                if degradation_state == "live_derived"
                else compound
                if degradation_state == "live_direct"
                else None
            )
            aggregation = (
                "fixed initial degradation ratio"
                if degradation_state == "live_derived"
                else "usable-lap weighted mean"
                if degradation_state == "live_direct"
                else "manual > pre-race/default"
            )
            rows.append(
                {
                    "compound": compound,
                    "baseline_pace": delta - fastest,
                    "compound_delta": delta,
                    "compound_delta_available": True,
                    "compound_delta_source": (
                        "race_evidence"
                        if delta_informed
                        else "resolved_pre_race_baseline"
                    ),
                    "degradation": float(resolved_degradation[compound]),
                    "degradation_source": degradation_state,
                    "degradation_base_source": degradation_sources.get(
                        compound, "resolved_base"
                    ),
                    "degradation_anchor_compound": degradation_anchor,
                    "degradation_initial_ratio_to_medium": degradation_ratios.get(
                        compound
                    ),
                    "degradation_race_informed": degradation_informed,
                    "degradation_support_status": degradation_state,
                    "sessions": "+".join(dict.fromkeys(contributing)),
                    "aggregation": aggregation,
                }
            )
        return rows

    def effective_tyre_inputs(
        self,
        summary: ModelSummary,
        resolved: ResolvedTyreInputs,
        *,
        manual_override: bool,
        initial_degradation: Mapping[str, float] | None = None,
    ) -> tuple[dict[tuple[str, str], float], list[dict[str, object]]]:
        """Resolve the one authoritative tyre state handed to Strategy Prediction."""
        base = resolved.strategy_overrides()
        base_degradation = {
            compound: value
            for (parameter, compound), value in base.items()
            if parameter == "degradation"
        }
        base_deltas = {
            compound: value
            for (parameter, compound), value in base.items()
            if parameter == "compound_delta"
        }
        base_degradation_sources = {
            compound: resolved.by_parameter()[f"Deg_{compound}"].selected_source
            for compound in base_degradation
        }
        if (
            manual_override
            or int(float(summary.manifest.get("as_of_leader_lap") or 0)) == 0
        ):
            assumptions = _assumptions_from_base(
                base_degradation,
                base_deltas,
                degradation_sources=base_degradation_sources,
            )
        else:
            assumptions = self.strategy_assumptions(
                summary,
                baseline_degradation=base_degradation,
                baseline_compound_delta=base_deltas,
                initial_degradation=initial_degradation,
                baseline_degradation_sources=base_degradation_sources,
            )
        effective: dict[tuple[str, str], float] = {}
        for row in assumptions:
            compound = str(row["compound"]).upper()
            effective[("degradation", compound)] = float(row["degradation"])
            effective[("compound_delta", compound)] = float(row["compound_delta"])
        missing = set(base).difference(effective)
        if missing:
            names = [f"{parameter}:{compound}" for parameter, compound in sorted(missing)]
            raise ValueError("Missing required effective tyre inputs: " + ", ".join(names))
        return effective, assumptions

    def load_tyre_information_files(
        self, paths: list[str | Path] | tuple[str | Path, ...]
    ) -> tuple[list[dict[str, object]], str | None]:
        """Read canonical weekend parameters or race-review tyre CSV outputs."""
        values: dict[tuple[str, str], list[tuple[float, float, str]]] = {}
        references: list[str] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            frame = pd.read_csv(path)
            normalized = {str(column).lower(): str(column) for column in frame.columns}
            name = path.name.lower()
            if "team_compound_degradation" in name or (
                "Team" in frame.columns and "compound_degradation" in name
            ):
                raise ValueError(
                    f"{path.name} is team-specific; choose summary_compound_degradation.csv."
                )
            combined_columns = {
                "compound",
                "degradationmediansecondsperlap",
                "compounddeltamedianseconds",
            }
            if combined_columns.issubset(normalized):
                selected = frame
                scope_column = normalized.get("scope")
                if scope_column:
                    selected = selected.loc[
                        selected[scope_column].astype(str).str.lower() == "global"
                    ]
                for row in selected.to_dict("records"):
                    compound = str(row.get(normalized["compound"]) or "").upper()
                    if not compound:
                        continue
                    for parameter, value_key, weight_key in (
                        (
                            "degradation",
                            "degradationmediansecondsperlap",
                            "degradationsamplecount",
                        ),
                        (
                            "compound_delta",
                            "compounddeltamedianseconds",
                            "deltasamplecount",
                        ),
                    ):
                        try:
                            value = float(row[normalized[value_key]])
                            weight = max(
                                float(row.get(normalized.get(weight_key), 1.0)), 1.0
                            )
                        except (TypeError, ValueError):
                            continue
                        values.setdefault((parameter, compound), []).append(
                            (value, weight, path.name)
                        )
                    reference_column = normalized.get("referencecompound")
                    if reference_column and row.get(reference_column):
                        references.append(str(row[reference_column]).upper())
                continue
            if {"parameter", "compound", "median"}.issubset(normalized):
                for row in frame.to_dict("records"):
                    parameter = str(row.get(normalized["parameter"]) or "").lower()
                    compound = str(row.get(normalized["compound"]) or "").upper()
                    if parameter not in {"degradation", "compound_delta"} or not compound:
                        continue
                    try:
                        value = float(row[normalized["median"]])
                        weight = max(float(row.get(normalized.get("usable_lap_count"), 1.0)), 1.0)
                    except (TypeError, ValueError):
                        continue
                    values.setdefault((parameter, compound), []).append(
                        (value, weight, path.name)
                    )
                    reference_column = normalized.get("reference_compound")
                    if reference_column and row.get(reference_column):
                        references.append(str(row[reference_column]).upper())
                continue

            if "compound_degradation" in name:
                parameter = "degradation"
                value_column = normalized.get("median") or normalized.get(
                    "compounddegsecondsperlap"
                )
            elif "compound_delta" in name:
                parameter = "compound_delta"
                value_column = normalized.get("median") or normalized.get(
                    "compounddeltaseconds"
                )
                reference_column = normalized.get("compounddeltareference")
                if reference_column:
                    references.extend(
                        str(value).upper()
                        for value in frame[reference_column].dropna().unique()
                    )
            else:
                continue
            compound_column = normalized.get("compound")
            if not compound_column or not value_column:
                continue
            for compound, group in frame.groupby(compound_column):
                numeric = pd.to_numeric(group[value_column], errors="coerce").dropna()
                if numeric.empty:
                    continue
                values.setdefault((parameter, str(compound).upper()), []).append(
                    (float(numeric.median()), 1.0, path.name)
                )

        if not values:
            raise ValueError(
                "No tyre degradation or compound-delta values were found in the selected CSV file(s)."
            )
        compounds = sorted(
            {compound for _, compound in values},
            key=lambda value: ("SOFT", "MEDIUM", "HARD").index(value)
            if value in {"SOFT", "MEDIUM", "HARD"}
            else 99,
        )
        aggregated: dict[tuple[str, str], float] = {}
        sources: dict[str, set[str]] = {}
        for key, samples in values.items():
            total_weight = sum(weight for _, weight, _ in samples)
            aggregated[key] = sum(value * weight for value, weight, _ in samples) / total_weight
            sources.setdefault(key[1], set()).update(source for _, _, source in samples)
        deltas = {
            compound: aggregated[("compound_delta", compound)]
            for compound in compounds
            if ("compound_delta", compound) in aggregated
        }
        fastest = min(deltas.values(), default=0.0)
        rows = []
        for compound in compounds:
            row: dict[str, object] = {
                "compound": compound,
                "source": ", ".join(sorted(sources.get(compound, set()))),
            }
            if compound in deltas:
                row["compound_delta"] = deltas[compound]
                row["baseline_pace"] = deltas[compound] - fastest
            if ("degradation", compound) in aggregated:
                row["degradation"] = aggregated[("degradation", compound)]
            rows.append(row)
        reference = references[-1] if references else None
        return rows, reference

    def initial_fp_assumptions(
        self, year: int, round_number: int
    ) -> list[dict[str, object]]:
        """Return the newest aggregate lap-zero model preserved in history."""
        rows = [
            row for row in self.parameter_history(year, round_number, include_temporary=False)
            if str(row.get("source_scope", "")).upper() == "AGGREGATE"
            and int(float(row.get("as_of_leader_lap") or 0)) == 0
        ]
        if not rows:
            return []
        latest_analysis = max(
            rows,
            key=lambda row: str(row.get("created_at") or ""),
        ).get("analysis_id")
        selected = tuple(row for row in rows if row.get("analysis_id") == latest_analysis)
        return self.strategy_assumptions(ModelSummary({"analysis_id": latest_analysis}, selected))

    def frozen_pre_race_prior(self, year: int, round_number: int) -> FrozenPreRacePrior:
        """Load or create the immutable marginal-approximation Race prior artifact."""
        root = weekend_model_root(year, round_number, self.data_root)
        path = root / "pre_race_prior_v1.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FrozenPreRacePrior(
                schema_version=int(payload["schema_version"]), year=int(payload["year"]),
                round_number=int(payload["round_number"]),
                source_analysis_id=str(payload["source_analysis_id"]),
                source_sessions=tuple(payload["source_sessions"]),
                source_created_at=str(payload["source_created_at"]),
                frozen_at=str(payload["frozen_at"]), construction=str(payload["construction"]),
                fuel=MarginalPrior(**payload["fuel"]),
                degradation={key: MarginalPrior(**value) for key, value in payload["degradation"].items()},
                performance={key: MarginalPrior(**value) for key, value in payload["performance"].items()},
            )
        history = [
            row for row in self.parameter_history(year, round_number, include_temporary=False)
            if str(row.get("source_scope", "")).upper() == "AGGREGATE"
            and int(float(row.get("as_of_leader_lap") or 0)) == 0
        ]
        if not history:
            raise ValueError("No preserved lap-zero pre-race posterior is available.")
        latest = max(history, key=lambda row: str(row.get("created_at") or ""))
        selected = [row for row in history if row.get("analysis_id") == latest.get("analysis_id")]

        def marginal(parameter: str, compound: str | None = None) -> MarginalPrior:
            rows = [row for row in selected if str(row.get("parameter")) == parameter]
            if compound is not None:
                rows = [row for row in rows if str(row.get("compound") or "").upper() == compound]
            if not rows:
                raise ValueError(f"Pre-race prior lacks {parameter}:{compound or ''}")
            weights = np.asarray([max(float(row.get("usable_lap_count") or 1), 1) for row in rows])
            return MarginalPrior(
                **{key: float(np.average([float(row[key]) for row in rows], weights=weights))
                   for key in ("p10", "median", "p90")},
                unit=str(rows[0].get("unit") or ("s" if parameter == "compound_delta" else "s/lap")),
            )

        fuel = marginal("fuel_rate")
        degradation = {compound: marginal("degradation", compound) for compound in ("HARD", "MEDIUM", "SOFT")}
        raw_performance = {compound: marginal("compound_delta", compound) for compound in ("HARD", "MEDIUM", "SOFT")}
        # Convert the arbitrary display gauge using one common median shift. This
        # preserves each stored marginal width; it does not invent covariance.
        centre = np.mean([value.median for value in raw_performance.values()])
        raw_performance = {
            key: MarginalPrior(value.p10 - centre, value.median - centre,
                               value.p90 - centre, value.unit, value.approximation)
            for key, value in raw_performance.items()
        }
        prior = FrozenPreRacePrior(
            1, int(year), int(round_number), str(latest["analysis_id"]),
            tuple(dict.fromkeys(str(row.get("session") or "") for row in selected)),
            str(latest.get("created_at") or ""), datetime.now(timezone.utc).isoformat(),
            "marginal_split_normal_approximation_no_joint_covariance",
            fuel, degradation, raw_performance,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(prior), indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return prior

    def activate_temporary_replay_model(self, year: int, round_number: int) -> None:
        """Start a replay from its lap-zero FP model without future-data leakage."""
        rows = [
            row for row in self.parameter_history(year, round_number, include_temporary=False)
            if str(row.get("source_scope", "")).upper() == "AGGREGATE"
            and int(float(row.get("as_of_leader_lap") or 0)) == 0
        ]
        if not rows:
            return
        latest = max(rows, key=lambda row: str(row.get("created_at") or ""))
        analysis_id = latest.get("analysis_id")
        selected = tuple(row for row in rows if row.get("analysis_id") == analysis_id)
        self._ephemeral_event = (int(year), int(round_number))
        self._ephemeral_summary = ModelSummary(
            {
                "analysis_id": analysis_id,
                "source_scope": "aggregate",
                "as_of_leader_lap": 0,
                "created_at": latest.get("created_at"),
                "status": latest.get("status") or "updated",
                "stale_reason": None,
            },
            selected,
        )
        self._ephemeral_result = None
        self._ephemeral_parameter_rows.clear()
        self._ephemeral_run_rows.clear()
        self._last_live_fingerprint = None
        self._last_clean_lap_count = 0
        self._last_clean_lap_sources = ()

    def reset_live_tracking(self) -> None:
        self._last_live_fingerprint = None
        self._last_clean_lap_count = 0
        self._last_clean_lap_sources = ()

    def publish_temporary_worker_snapshot(
        self, year: int, round_number: int, snapshot
    ) -> None:
        """Publish all temporary fields together after a successful process result."""
        self._ephemeral_event = (int(year), int(round_number))
        self._ephemeral_summary = snapshot.summary
        self._ephemeral_result = snapshot.ephemeral_result
        self._ephemeral_parameter_rows = list(snapshot.parameter_rows)
        self._ephemeral_run_rows = list(snapshot.run_rows)

    def generate_continuations(
        self,
        *,
        year: int,
        round_number: int,
        state: StrategySearchState,
        pit_loss: float,
        k: int = 10,
        source_scope: str = "aggregate",
        overrides: Mapping[tuple[str, str], float] | None = None,
        persist: bool = True,
        model_summary: ModelSummary | None = None,
        tyre_input_snapshot: Mapping[str, object] | None = None,
        rules_compliant: bool = False,
    ) -> list[StrategyResult]:
        summary = model_summary or self.load_model(year, round_number, source_scope)
        model = self.build_strategy_model(summary, overrides)
        strategy_rules = (
            StrategyRules(minimum_distinct_dry_compounds=2)
            if rules_compliant else StrategyRules()
        )
        # ``selected_driver is None`` is the existing Pre-race / manual planning
        # scope.  Enumerate its possible starting compounds explicitly because
        # woStrategy's legacy ``allow_any_start`` path is coupled to lap zero;
        # the completed-lap anchor must still remain absolute for pit positions.
        new_strategy_start = state.selected_driver is None
        search_states = (
            [
                replace(
                    state,
                    current_compound=compound,
                    current_tyre_age=int(
                        (model.fitted_start_age or {}).get(compound.upper(), 0)
                    ),
                )
                for compound in model.degradation
            ]
            if new_strategy_start
            else [state]
        )
        candidates = [
            result
            for search_state in search_states
            for result in search_best_compound_sequences(
                search_state,
                model,
                pit_loss,
                max_stops=state.max_future_stops,
                k=k,
                strategy_rules=strategy_rules,
            )
        ]
        unique = {
            (result.plan.compounds, result.plan.pit_laps): result
            for result in candidates
        }
        ordered = sorted(
            unique.values(),
            key=lambda result: (
                result.remaining_cost,
                result.plan.compounds,
                result.plan.pit_laps,
            ),
        )[:k]
        best_cost = ordered[0].remaining_cost if ordered else 0.0
        results = [
            replace(
                result,
                rank=rank,
                delta_to_best=result.remaining_cost - best_cost,
            )
            for rank, result in enumerate(ordered, start=1)
        ]
        target = state.selected_driver or "global"
        records = results_to_records(results, state, model)
        if new_strategy_start:
            records = [
                replace(
                    record,
                    current_compound=result.plan.compounds[0],
                    current_tyre_age=int(
                        (model.fitted_start_age or {}).get(
                            result.plan.compounds[0].upper(), 0
                        )
                    ),
                    input_state_hash=canonical_hash(
                        replace(
                            state,
                            current_compound=result.plan.compounds[0],
                            current_tyre_age=int(
                                (model.fitted_start_age or {}).get(
                                    result.plan.compounds[0].upper(), 0
                                )
                            ),
                        )
                    ),
                )
                for record, result in zip(records, results)
            ]
        persistence_started = time.perf_counter()
        if persist:
            write_strategy_records(
                records,
                year=year,
                round_number=round_number,
                target=target,
                data_root=self.data_root,
            )
            if records and tyre_input_snapshot is not None:
                destination = (
                    strategy_search_root(year, round_number, target, self.data_root)
                    / "tyre_input_snapshots"
                    / f"{records[0].search_id}.json"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".json.tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(dict(tyre_input_snapshot), handle, indent=2, sort_keys=True)
                    handle.write("\n")
                temporary.replace(destination)
        self._last_strategy_search = (
            records, target, int(year), int(round_number),
            dict(tyre_input_snapshot) if tyre_input_snapshot is not None else None,
        )
        return results

    def model_config(self, year: int, round_number: int) -> dict[str, object]:
        return read_weekend_model_config(
            year=year, round_number=round_number, data_root=self.data_root
        )

    def resolve_live_model_config(
        self,
        year: int,
        round_number: int,
        *,
        replay_directory: str | Path | None = None,
    ) -> tuple[dict[str, object], str]:
        """Resolve and snapshot the exact configuration before replay can queue MC."""
        snapshot_path = (
            Path(replay_directory) / "replay_model_config.json"
            if replay_directory is not None else None
        )
        source = "event pre-race model_config"
        config: dict[str, object] = {}
        if snapshot_path is not None and snapshot_path.is_file():
            with snapshot_path.open(encoding="utf-8") as handle:
                config = dict(json.load(handle))
            source = f"replay snapshot {snapshot_path}"
        if not config:
            config = dict(self.model_config(year, round_number))
        if not config:
            resolved = resolve_race_performance_settings(
                year=year, round_number=round_number, session="R", data_root=self.data_root
            )
            settings = dict(resolved.settings) if resolved is not None else {}
            required_source = (
                "fuel_rate_bounds", "track_rate_bounds",
                "default_compound_degradation_bounds",
                "default_compound_delta_bounds", "sample_count",
            )
            missing = [key for key in required_source if settings.get(key) is None]
            if missing:
                raise ValueError(
                    "Replay analysis configuration is unavailable. Run pre_race_analysis "
                    f"for {year} round {round_number}; missing: {', '.join(missing)}."
                )
            config = {
                "sample_count": int(settings["sample_count"]),
                "random_seed": int(settings.get("random_seed") or derive_race_seed(year, round_number)),
                "fuel_rate_bounds": list(settings["fuel_rate_bounds"]),
                "track_rate_bounds": list(settings["track_rate_bounds"]),
                "default_degradation_bounds": list(settings["default_compound_degradation_bounds"]),
                "default_compound_delta_bounds": list(settings["default_compound_delta_bounds"]),
                "reference_compound": str(settings.get("compound_delta_reference") or "HARD"),
                "enforce_compound_order": True,
                "clean_lap_noise_sigma": float(settings.get("clean_lap_noise_sigma", 0.35)),
                "session_noise_sigma": {},
                "expected_sessions": ["FP1", "FP2", "FP3", "R"],
                "expected_compounds": list(settings.get("dry_compounds") or ("SOFT", "MEDIUM", "HARD")),
                "quick_lap_threshold": float(settings.get("quick_lap_threshold", 1.10)),
                "min_clean_air_laps": int(settings.get("min_clean_air_laps", 4)),
                "clean_mean_time_delta_seconds": float(settings.get("clean_mean_time_delta_seconds", 2.5)),
                "clean_mean_time_delta_behind_seconds": settings.get("clean_mean_time_delta_behind_seconds", 1.0),
                "clean_lap_mode": "telemetry-gap-pre-race; timing-proxy-live",
            }
            write_weekend_model_config(
                config, year=year, round_number=round_number, data_root=self.data_root
            )
            source = f"race/pre-race settings builder {resolved.path}"
        required = (
            "random_seed", "fuel_rate_bounds", "track_rate_bounds",
            "default_degradation_bounds", "default_compound_delta_bounds",
        )
        missing = [key for key in required if config.get(key) is None]
        if missing:
            raise ValueError("Replay model_config is incomplete: " + ", ".join(missing))
        if source.startswith("replay snapshot"):
            # The process worker resolves the event artifact; make the selected
            # replay snapshot authoritative for this reproducible run.
            write_weekend_model_config(
                config, year=year, round_number=round_number, data_root=self.data_root
            )
        if snapshot_path is not None:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = snapshot_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(snapshot_path)
        return config, source

    def live_model_config(
        self, year: int, round_number: int, session: str = "R"
    ) -> dict[str, object]:
        """Merge compatible race-review settings over the pre-race model config."""
        effective = dict(self.model_config(year, round_number))
        if not effective:
            return {}
        resolved = resolve_race_performance_settings(
            year=year,
            round_number=round_number,
            session=session,
            data_root=self.data_root,
        )
        if resolved is None:
            effective["settings_source"] = "pre_race_model_config"
            return effective

        aliases = {
            "default_compound_degradation_bounds": "default_degradation_bounds",
            "compound_delta_reference": "reference_compound",
            "dry_compounds": "expected_compounds",
        }
        compatible = (
            "sample_count",
            "fuel_rate_bounds",
            "track_rate_bounds",
            "default_compound_degradation_bounds",
            "default_compound_delta_bounds",
            "compound_delta_reference",
            "clean_lap_noise_sigma",
            "quick_lap_threshold",
            "min_clean_air_laps",
            "clean_mean_time_delta_seconds",
            "clean_mean_time_delta_behind_seconds",
            "dry_compounds",
        )
        for source_key in compatible:
            value = resolved.settings.get(source_key)
            if value is not None:
                effective[aliases.get(source_key, source_key)] = value
        random_seed = resolved.settings.get("random_seed")
        if random_seed is not None:
            effective["random_seed"] = random_seed
        effective["settings_source"] = resolved.source
        effective["settings_path"] = str(resolved.path)
        return effective

    def parameter_history(
        self, year: int, round_number: int, *, include_temporary: bool = True
    ) -> list[dict[str, object]]:
        path = weekend_model_root(year, round_number, self.data_root) / "history/parameter_history.csv"
        rows: list[dict[str, object]] = list(_read_csv_if_present(path))
        if include_temporary and self._ephemeral_event == (int(year), int(round_number)):
            rows.extend(self._ephemeral_parameter_rows)
        return rows

    def recalculate_from_live_snapshot(
        self,
        *,
        year: int,
        round_number: int,
        snapshot,
        sample_count: int,
        quick_lap_threshold: float,
        min_clean_laps: int,
        allowed_compounds: tuple[str, ...] = ("SOFT", "MEDIUM", "HARD"),
        exclude_stint_start_laps: int = 0,
        exclude_stint_end_laps: int = 0,
        use_sector_boundary_traffic: bool = False,
        min_gap_ahead_seconds: float = 2.5,
        min_gap_behind_seconds: float = 1.0,
        enforce_compound_order: bool | None = None,
        race_baseline_architecture: str = "retro",
        scheduled_total_laps_override: int | None = None,
        persist: bool = True,
    ) -> LiveRecalculationResult:
        started_at = time.perf_counter()
        leader_lap = _leader_lap(snapshot)
        if not use_sector_boundary_traffic:
            # Quarantine is an availability state, not a newly fitted empty input.
            # Do not mutate fingerprints, counts, model/run/parameter history.
            return LiveRecalculationResult(
                "traffic-unavailable", leader_lap, 0,
                previous_clean_lap_count=self._last_clean_lap_count,
                clean_lap_sources=self._last_clean_lap_sources,
                elapsed_seconds=time.perf_counter() - started_at,
                sample_count=int(sample_count),
                traffic_unavailable_reason=LIVE_TRAFFIC_QUARANTINE_REASON,
                model_state_retained=True,
            )
        scheduled_total_laps, scheduled_total_laps_source = _resolve_scheduled_total_laps(
            snapshot,
            manual_override=scheduled_total_laps_override,
            current_leader_lap=leader_lap,
        )
        previous_clean_lap_count = self._last_clean_lap_count
        clean_lap_started = time.perf_counter()
        race_laps = _live_analysis_laps(
            snapshot,
            quick_lap_threshold=quick_lap_threshold,
            min_clean_laps=min_clean_laps,
            allowed_compounds=allowed_compounds,
            exclude_stint_start_laps=exclude_stint_start_laps,
            exclude_stint_end_laps=exclude_stint_end_laps,
            use_sector_boundary_traffic=use_sector_boundary_traffic,
            min_gap_ahead_seconds=min_gap_ahead_seconds,
            min_gap_behind_seconds=min_gap_behind_seconds,
        )
        exclusion_diagnostics = tuple(
            dict(race_laps.attrs.get("exclusion_diagnostics", {})).items()
        )
        clean_lap_seconds = time.perf_counter() - clean_lap_started
        fingerprint = canonical_hash(
            race_laps[["Driver", "LapNumber", "LapTimeSeconds", "Compound", "TyreLife"]]
            .to_dict("records")
            if not race_laps.empty else []
        )
        if fingerprint == self._last_live_fingerprint:
            return LiveRecalculationResult(
                "no-new-clean-laps", leader_lap, len(race_laps),
                previous_clean_lap_count=previous_clean_lap_count,
                clean_lap_sources=self._last_clean_lap_sources,
                elapsed_seconds=time.perf_counter() - started_at,
                sample_count=int(sample_count),
                runtime_breakdown=(("clean_lap_preparation_seconds", clean_lap_seconds),),
                exclusion_diagnostics=exclusion_diagnostics,
            )
        if race_laps.empty:
            self._last_live_fingerprint = fingerprint
            self._last_clean_lap_count = 0
            self._last_clean_lap_sources = (("R", 0),)
            return LiveRecalculationResult(
                "waiting-for-clean-laps", leader_lap, 0,
                previous_clean_lap_count=previous_clean_lap_count,
                clean_lap_sources=self._last_clean_lap_sources,
                elapsed_seconds=time.perf_counter() - started_at,
                sample_count=int(sample_count),
                runtime_breakdown=(("clean_lap_preparation_seconds", clean_lap_seconds),),
                exclusion_diagnostics=exclusion_diagnostics,
            )

        stored = self.live_model_config(year, round_number, "R")
        if not stored:
            raise ValueError("Run pre_race_analysis first; model_config.json is missing.")
        config = WeekendModelConfig(
            sample_count=int(sample_count),
            random_seed=int(stored["random_seed"]),
            fuel_rate_bounds=tuple(stored["fuel_rate_bounds"]),
            track_rate_bounds=tuple(stored["track_rate_bounds"]),
            default_degradation_bounds=tuple(stored["default_degradation_bounds"]),
            default_compound_delta_bounds=tuple(stored["default_compound_delta_bounds"]),
            reference_compound=str(stored.get("reference_compound", "HARD")),
            clean_lap_noise_sigma=float(stored.get("clean_lap_noise_sigma", 0.35)),
            session_noise_sigma={
                str(key): float(value)
                for key, value in dict(stored.get("session_noise_sigma", {})).items()
            },
            enforce_compound_order=(
                bool(stored.get("enforce_compound_order", True))
                if enforce_compound_order is None
                else bool(enforce_compound_order)
            ),
        )
        analysis_options = {
            "sample_count": int(sample_count),
            "quick_lap_threshold": float(quick_lap_threshold),
            "min_clean_laps": int(min_clean_laps),
            "allowed_compounds": tuple(str(value).upper() for value in allowed_compounds),
            "exclude_stint_start_laps": int(exclude_stint_start_laps),
            "exclude_stint_end_laps": int(exclude_stint_end_laps),
            "use_sector_boundary_traffic": bool(use_sector_boundary_traffic),
            "min_gap_ahead_seconds": float(min_gap_ahead_seconds),
            "min_gap_behind_seconds": float(min_gap_behind_seconds),
            "enforce_compound_order": bool(config.enforce_compound_order),
            "race_baseline_architecture": str(race_baseline_architecture),
        }
        event_context = {
            "year": int(year),
            "round_number": int(round_number),
            "scheduled_total_laps": int(scheduled_total_laps),
        }
        input_provenance = {
            "scheduled_total_laps_source": str(scheduled_total_laps_source),
        }
        prior_started = time.perf_counter()
        if race_baseline_architecture == "retro":
            resolved = resolve_race_performance_settings(
                year=year, round_number=round_number, session="R", data_root=self.data_root
            )
            retro_settings = dict(resolved.settings) if resolved else {}
            order_threshold = retro_settings.get("degradation_order_track_temperature")
            live_track_temperature = retro_settings.get("track_temperature")
            if (
                live_track_temperature is None
                and order_threshold is not None
                and (enforce_compound_order is None or enforce_compound_order)
            ):
                # The retro review normally reads TrackTemp from its lap frame.
                # Live completed-lap contracts do not carry weather, so make the
                # requested ordering explicit instead of silently disabling it.
                live_track_temperature = float(order_threshold) + 1.0
            retro_config = MonteCarloRacePerformanceConfig(
                sample_count=int(sample_count),
                fuel_rate_bounds=tuple(retro_settings.get("fuel_rate_bounds", stored["fuel_rate_bounds"])),
                track_rate_bounds=tuple(retro_settings.get("track_rate_bounds", stored["track_rate_bounds"])),
                limit_negative_track_correction=bool(
                    retro_settings.get("limit_negative_track_correction", False)
                ),
                compound_degradation_bounds={
                    str(key).upper(): tuple(value) for key, value in
                    dict(retro_settings.get("compound_degradation_bounds", {})).items()
                },
                default_compound_degradation_bounds=tuple(retro_settings.get(
                    "default_compound_degradation_bounds", stored["default_degradation_bounds"]
                )),
                compound_delta_bounds={
                    str(key).upper(): tuple(value) for key, value in
                    dict(retro_settings.get("compound_delta_bounds", {})).items()
                },
                default_compound_delta_bounds=tuple(retro_settings.get(
                    "default_compound_delta_bounds", stored["default_compound_delta_bounds"]
                )),
                compound_delta_reference=str(retro_settings.get(
                    "compound_delta_reference", stored.get("reference_compound", "HARD")
                )),
                team_variation_fraction=float(retro_settings.get("team_variation_fraction", .5)),
                team_variation_absolute_min=float(retro_settings.get(
                    "team_variation_absolute_min", .005
                )),
                clean_lap_noise_sigma=float(retro_settings.get(
                    "clean_lap_noise_sigma", stored.get("clean_lap_noise_sigma", .15)
                )),
                weight_strategy=str(retro_settings.get("weight_strategy", "gaussian")),
                weight_effective_sample_count=retro_settings.get("weight_effective_sample_count"),
                baseline_group=str(retro_settings.get("baseline_group", "driver")),
                fuel_ref=float(retro_settings.get("fuel_ref", 0.0)),
                race_lap_ref=retro_settings.get("race_lap_ref"),
                tyre_age_ref=float(retro_settings.get("tyre_age_ref", 0.0)),
                random_seed=int(stored["random_seed"]),
                sampling_strategy=str(retro_settings.get("sampling_strategy", "random")),
                track_temperature_celsius=live_track_temperature,
                degradation_order_track_temperature_celsius=order_threshold,
                evaluator_backend=str(retro_settings.get("evaluator_backend", "accelerated")),
                candidate_chunk_size=int(retro_settings.get("candidate_chunk_size", 2_000)),
            )
            self._last_live_calculation_inputs = {
                "architecture": "retro",
                "event_context": event_context,
                "input_provenance": input_provenance,
                "analysis_options": analysis_options,
                "model_config": asdict(retro_config),
            }
            result = run_live_retro_model(
                race_laps, retro_config, as_of_leader_lap=leader_lap,
                race_lap_count=scheduled_total_laps,
            )
            result.quality["fuel_proxy_total_laps"] = scheduled_total_laps
            result.quality["fuel_proxy_total_laps_source"] = scheduled_total_laps_source
            prior_loaded = time.perf_counter()
        else:
            prior = self.frozen_pre_race_prior(year, round_number)
            self._last_live_calculation_inputs = {
                "architecture": str(race_baseline_architecture),
                "event_context": event_context,
                "input_provenance": input_provenance,
                "analysis_options": analysis_options,
                "model_config": asdict(config),
                "prior": asdict(prior),
            }
            prior_loaded = time.perf_counter()
            result = run_race_specific_model(
                race_laps, prior, config, as_of_leader_lap=leader_lap,
                baseline_architecture=race_baseline_architecture,
            )
        clean_lap_sources = (("R", int(len(race_laps))),)
        persistence_started = time.perf_counter()
        if persist:
            for model_snapshot in result.session_snapshots.values():
                write_model_snapshot(
                    model_snapshot, year=year, round_number=round_number, data_root=self.data_root
                )
            write_model_snapshot(
                result.aggregate_snapshot,
                year=year,
                round_number=round_number,
                data_root=self.data_root,
            )
            write_joint_candidate_costs(
                result.candidate_costs.to_dict("records"),
                year=year,
                round_number=round_number,
                data_root=self.data_root,
            )
        else:
            self._ephemeral_event = (int(year), int(round_number))
            self._ephemeral_result = result
            self._ephemeral_summary = _model_summary_from_snapshot(result.aggregate_snapshot)
            self._ephemeral_parameter_rows.extend(
                asdict(row) for row in result.aggregate_snapshot.parameters
            )
            self._ephemeral_run_rows.append(dict(self._ephemeral_summary.manifest))
        self._last_live_fingerprint = fingerprint
        self._last_clean_lap_count = len(race_laps)
        self._last_clean_lap_sources = clean_lap_sources
        persistence_finished = time.perf_counter()
        runtime = {
            "clean_lap_preparation_seconds": clean_lap_seconds,
            "pre_race_artifact_loading_seconds": prior_loaded - prior_started,
            **result.timings,
            "ui_history_persistence_seconds": persistence_finished - persistence_started,
        }
        runtime["total_recalculation_seconds"] = persistence_finished - started_at
        return LiveRecalculationResult(
            "updated", leader_lap, len(race_laps), result.aggregate_snapshot.analysis_id,
            previous_clean_lap_count=previous_clean_lap_count,
            clean_lap_sources=clean_lap_sources,
            elapsed_seconds=persistence_finished - started_at,
            sample_count=int(sample_count),
            runtime_breakdown=tuple(runtime.items()),
            sampler_quality=tuple(result.quality.items()),
            exclusion_diagnostics=exclusion_diagnostics,
        )


    def load_saved_strategies(
        self, year: int, round_number: int, target: str = "global"
    ) -> list[dict[str, str]]:
        try:
            return read_strategy_records(
                year=year,
                round_number=round_number,
                target=target,
                data_root=self.data_root,
            )
        except FileNotFoundError:
            return []

    def model_history(
        self, year: int, round_number: int, *, include_temporary: bool = True
    ) -> list[dict[str, object]]:
        path = weekend_model_root(year, round_number, self.data_root) / "history/analysis_runs.csv"
        rows: list[dict[str, object]] = list(_read_csv_if_present(path))
        if include_temporary and self._ephemeral_event == (int(year), int(round_number)):
            rows.extend(self._ephemeral_run_rows)
        return rows

    def save_named_snapshot(
        self,
        year: int,
        round_number: int,
        name: str,
        strategy_rows: list[dict[str, object]],
    ) -> Path:
        """Promote temporary replay work and archive a named durable bundle."""
        cleaned_name = str(name).strip()
        if not cleaned_name:
            raise ValueError("Snapshot name cannot be empty.")
        summary = self.load_model(year, round_number)
        if self._ephemeral_event == (int(year), int(round_number)) and self._ephemeral_result:
            result = self._ephemeral_result
            for snapshot in result.session_snapshots.values():
                write_model_snapshot(
                    snapshot, year=year, round_number=round_number, data_root=self.data_root
                )
            write_model_snapshot(
                result.aggregate_snapshot,
                year=year,
                round_number=round_number,
                data_root=self.data_root,
            )
            write_joint_candidate_costs(
                result.candidate_costs.to_dict("records"),
                year=year,
                round_number=round_number,
                data_root=self.data_root,
            )
        tyre_input_snapshot = None
        if self._last_strategy_search is not None:
            records, target, search_year, search_round, candidate_tyre_snapshot = (
                self._last_strategy_search
            )
            if (search_year, search_round) == (int(year), int(round_number)):
                tyre_input_snapshot = candidate_tyre_snapshot
                write_strategy_records(
                    records, year=year, round_number=round_number,
                    target=target, data_root=self.data_root,
                )
        if self._ephemeral_event == (int(year), int(round_number)):
            self._ephemeral_event = None
            self._ephemeral_result = None
            self._ephemeral_summary = None
            self._ephemeral_parameter_rows.clear()
            self._ephemeral_run_rows.clear()
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned_name).strip("._") or "snapshot"
        destination = weekend_model_root(
            year, round_number, self.data_root
        ) / "saved_snapshots" / slug / "snapshot.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "name": cleaned_name,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "year": int(year),
            "round_number": int(round_number),
            "model_manifest": summary.manifest,
            "parameters": list(summary.parameters),
            "strategy_results": strategy_rows,
            "tyre_input_snapshot": tyre_input_snapshot,
        }
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        temporary.replace(destination)
        return destination

    def list_named_snapshots(
        self, year: int, round_number: int
    ) -> list[dict[str, object]]:
        root = weekend_model_root(
            year, round_number, self.data_root
        ) / "saved_snapshots"
        snapshots = []
        for path in root.glob("*/snapshot.json") if root.is_dir() else ():
            try:
                with path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            snapshots.append(
                {
                    "name": str(payload.get("name") or path.parent.name),
                    "saved_at": str(payload.get("saved_at") or ""),
                    "path": path,
                }
            )
        return sorted(snapshots, key=lambda row: str(row["saved_at"]), reverse=True)

    def list_analysis_history_entries(
        self, year: int, round_number: int
    ) -> list[dict[str, object]]:
        """List each aggregate model update once for the history selector."""
        unique: dict[str, dict[str, object]] = {}
        for row in self.model_history(year, round_number):
            if str(row.get("source_scope", "")).upper() != "AGGREGATE":
                continue
            analysis_id = str(row.get("analysis_id") or "")
            if analysis_id:
                unique[analysis_id] = row
        return sorted(
            unique.values(),
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )

    def load_analysis_history_entry(
        self, year: int, round_number: int, analysis_id: str
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        runs = [
            row for row in self.model_history(year, round_number)
            if str(row.get("analysis_id") or "") == str(analysis_id)
            and str(row.get("source_scope", "")).upper() == "AGGREGATE"
        ]
        parameters = [
            row for row in self.parameter_history(year, round_number)
            if str(row.get("analysis_id") or "") == str(analysis_id)
            and str(row.get("source_scope", "")).upper() == "AGGREGATE"
        ]
        return runs, parameters

    @staticmethod
    def load_named_snapshot(path: str | Path) -> dict[str, object]:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)

    def save_history_plot(
        self,
        year: int,
        round_number: int,
        name: str,
        parameter_rows: list[dict[str, object]],
    ) -> Path:
        cleaned_name = str(name).strip()
        if not cleaned_name:
            raise ValueError("History plot name cannot be empty.")
        if not parameter_rows:
            raise ValueError("There is no value-lap history to save.")
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned_name).strip("._") or "plot"
        destination = weekend_model_root(
            year, round_number, self.data_root
        ) / "saved_history_plots" / slug / "plot.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "name": cleaned_name,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "year": int(year),
            "round_number": int(round_number),
            "parameter_history": parameter_rows,
        }
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        temporary.replace(destination)
        return destination

    def list_saved_history_plots(
        self, year: int, round_number: int
    ) -> list[dict[str, object]]:
        root = weekend_model_root(
            year, round_number, self.data_root
        ) / "saved_history_plots"
        plots = []
        for path in root.glob("*/plot.json") if root.is_dir() else ():
            try:
                with path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            plots.append(
                {
                    "name": str(payload.get("name") or path.parent.name),
                    "saved_at": str(payload.get("saved_at") or ""),
                    "path": path,
                }
            )
        return sorted(plots, key=lambda row: str(row["saved_at"]), reverse=True)

    @staticmethod
    def load_history_plot(path: str | Path) -> dict[str, object]:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)

    def strategy_history(
        self, year: int, round_number: int, target: str = "global"
    ) -> list[dict[str, str]]:
        path = strategy_search_root(
            year, round_number, target, self.data_root
        ) / "search_runs.csv"
        return _read_csv_if_present(path)

    def pin(self, strategy_id: str) -> tuple[str, ...]:
        if strategy_id in self.pinned_strategy_ids:
            return tuple(self.pinned_strategy_ids)
        if len(self.pinned_strategy_ids) >= 3:
            raise ValueError("A maximum of three strategy comparisons can be pinned.")
        self.pinned_strategy_ids.append(strategy_id)
        return tuple(self.pinned_strategy_ids)

    def unpin(self, strategy_id: str) -> tuple[str, ...]:
        if strategy_id in self.pinned_strategy_ids:
            self.pinned_strategy_ids.remove(strategy_id)
        return tuple(self.pinned_strategy_ids)


def snapshot_as_of_leader_lap(snapshot, leader_lap: int):
    """Return a committed-only causal snapshot for Planner's replay MC queue."""
    leader_lap = int(leader_lap)
    lap_candidates = [
        lap for lap in snapshot.completed_laps
        if int(lap.lap_number) == leader_lap and lap.position == 1
    ]
    if not lap_candidates:
        lap_candidates = [
            lap for lap in snapshot.completed_laps
            if int(lap.lap_number) == leader_lap
        ]
    clocks = [_completion_clock(lap) for lap in lap_candidates]
    clocks = [value for value in clocks if value is not None]
    cutoff = min(clocks) if clocks else None
    if cutoff is None:
        completed_laps = tuple(
            lap for lap in snapshot.completed_laps if int(lap.lap_number) <= leader_lap
        )
        completed_sectors = tuple(
            sector for sector in snapshot.completed_sectors
            if int(sector.lap_number) <= leader_lap
        )
        telemetry_samples = tuple(
            sample for sample in snapshot.telemetry_samples
            if int(sample.lap_number) <= leader_lap
        )
    else:
        completed_laps = tuple(
            lap for lap in snapshot.completed_laps
            if _completion_clock(lap) is not None and _completion_clock(lap) <= cutoff
        )
        completed_sectors = tuple(
            sector for sector in snapshot.completed_sectors
            if _completion_clock(sector) is not None
            and _completion_clock(sector) <= cutoff
        )
        telemetry_samples = tuple(
            sample for sample in snapshot.telemetry_samples
            if float(sample.session_time_seconds) <= cutoff
        )
    return replace(
        snapshot,
        completed_laps=completed_laps,
        completed_sectors=completed_sectors,
        telemetry_samples=telemetry_samples,
        # A materialized later snapshot has no position-arrival lineage. Only
        # the worker's ingress-bounded replay can preserve positions causally.
        position_samples=(),
        provisional_sectors=(),
        live_timing=(),
    )


def leader_lap_snapshots(snapshot, *, first_lap: int, last_lap: int):
    """Build all causal lap snapshots when replay delivery jumps several laps."""
    return [
        (leader_lap, snapshot_as_of_leader_lap(snapshot, leader_lap))
        for leader_lap in range(max(int(first_lap), 1), int(last_lap) + 1)
    ]


def _completion_clock(value) -> float | None:
    session_time = getattr(value, "session_time_seconds", None)
    if session_time is not None:
        return float(session_time)
    timestamp = getattr(value, "source_timestamp_utc", None)
    return timestamp.timestamp() if timestamp is not None else None


def decode_strategy_row(row: dict[str, str]) -> tuple[tuple[str, ...], tuple[int, ...]]:
    return (
        tuple(json.loads(row.get("compound_sequence_json") or "[]")),
        tuple(int(value) for value in json.loads(row.get("pit_laps_json") or "[]")),
    )


def _model_summary_from_snapshot(snapshot) -> ModelSummary:
    manifest = {
        "schema_version": snapshot.schema_version,
        "analysis_id": snapshot.analysis_id,
        "sample_bank_id": snapshot.sample_bank_id,
        "source_scope": snapshot.source_scope,
        "as_of_leader_lap": snapshot.as_of_leader_lap,
        "input_fingerprint": snapshot.input_fingerprint,
        "config_hash": snapshot.config_hash,
        "created_at": snapshot.created_at.isoformat(),
        "status": snapshot.status,
        "stale_reason": snapshot.stale_reason,
        "contributing_sessions": list(snapshot.contributing_sessions),
        "excluded_sessions": dict(snapshot.excluded_sessions),
        "sampler_quality": dict(getattr(snapshot, "sampler_quality", ())),
        "semantics": dict(getattr(snapshot, "semantics", ())),
    }
    return ModelSummary(manifest, tuple(asdict(row) for row in snapshot.parameters))


def _race_informed_coordinates(summary: ModelSummary) -> set[tuple[str, str]]:
    quality = summary.manifest.get("sampler_quality") or {}
    if isinstance(quality, (list, tuple)):
        quality = dict(quality)
    observed = set()
    architecture = ""
    if isinstance(quality, Mapping):
        architecture = str(quality.get("architecture") or "")
        observed = {
            str(value).upper() for value in quality.get("observed_compounds", ())
        }
    coordinates = set()
    for row in summary.parameters:
        parameter = str(row.get("parameter") or "")
        compound = str(row.get("compound") or "").upper()
        if parameter not in {"degradation", "compound_delta"} or not compound:
            continue
        status = str(row.get("support_status") or "").lower()
        try:
            usable = float(row.get("usable_lap_count") or 0)
            value = float(row["median"])
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(value) or usable <= 0:
            continue
        if architecture.startswith("live-retro"):
            informed = compound in observed
        else:
            informed = status in {"race_informed", "measured", "updated"}
        if informed:
            coordinates.add((parameter, compound))
    return coordinates


def _summary_tyre_coordinates(
    summary: ModelSummary,
) -> dict[tuple[str, str], float]:
    validate_offset_rows(summary.parameters)
    weighted: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in summary.parameters:
        parameter = str(row.get("parameter") or "")
        compound = str(row.get("compound") or "").upper()
        if parameter not in {"degradation", "compound_delta"} or not compound:
            continue
        try:
            value = float(row["median"])
            weight = max(float(row.get("usable_lap_count") or 1.0), 1.0)
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(value):
            weighted.setdefault((parameter, compound), []).append((value, weight))
    return {
        coordinate: sum(value * weight for value, weight in values)
        / sum(weight for _, weight in values)
        for coordinate, values in weighted.items()
    }


def _initial_degradation_ratios(
    degradation: Mapping[str, float],
) -> dict[str, float]:
    """Return immutable input ratios to MEDIUM when they are usable."""
    try:
        medium = float(degradation["MEDIUM"])
    except (KeyError, TypeError, ValueError):
        return {}
    if not np.isfinite(medium) or medium == 0.0:
        return {}
    ratios = {}
    for compound, raw_value in degradation.items():
        try:
            ratio = float(raw_value) / medium
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if np.isfinite(ratio):
            ratios[str(compound).upper()] = ratio
    return ratios


def _preferred_degradation_anchor(
    summary: ModelSummary,
    direct: Mapping[str, float],
    ratios: Mapping[str, float],
) -> str | None:
    """Prefer direct MEDIUM, otherwise the directly observed best-supported tyre."""
    if "MEDIUM" in direct and ratios.get("MEDIUM") not in (None, 0.0):
        return "MEDIUM"
    support = {compound: 0.0 for compound in direct}
    for row in summary.parameters:
        compound = str(row.get("compound") or "").upper()
        if str(row.get("parameter") or "") != "degradation" or compound not in direct:
            continue
        try:
            support[compound] += max(float(row.get("usable_lap_count") or 0.0), 0.0)
        except (TypeError, ValueError):
            continue
    candidates = [
        compound for compound in direct
        if ratios.get(compound) not in (None, 0.0)
    ]
    if not candidates:
        return None
    priority = {"HARD": 0, "SOFT": 1}
    return max(
        candidates,
        key=lambda compound: (support[compound], -priority.get(compound, 99)),
    )


def _assumptions_from_base(
    degradation: Mapping[str, float],
    deltas: Mapping[str, float],
    *,
    degradation_sources: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    fastest = min(deltas.values(), default=0.0)
    rows = []
    for compound in ("SOFT", "MEDIUM", "HARD"):
        if compound not in degradation or compound not in deltas:
            continue
        rows.append(
            {
                "compound": compound,
                "baseline_pace": float(deltas[compound]) - fastest,
                "compound_delta": float(deltas[compound]),
                "compound_delta_available": True,
                "compound_delta_source": "resolved_pre_race_baseline",
                "degradation": float(degradation[compound]),
                "degradation_source": "base",
                "degradation_base_source": str(
                    (degradation_sources or {}).get(compound, "resolved_base")
                ),
                "degradation_anchor_compound": None,
                "degradation_initial_ratio_to_medium": (
                    float(degradation[compound]) / float(degradation["MEDIUM"])
                    if float(degradation.get("MEDIUM", 0.0)) != 0.0 else None
                ),
                "degradation_race_informed": False,
                "degradation_support_status": "base",
                "sessions": "pre-race/manual",
                "aggregation": "manual > pre-race/default",
            }
        )
    return rows


def _read_csv_if_present(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _leader_lap(snapshot) -> int:
    leader = [lap.lap_number for lap in snapshot.completed_laps if lap.position == 1]
    all_laps = [lap.lap_number for lap in snapshot.completed_laps]
    return int(max(leader or all_laps or [0]))


def _resolve_scheduled_total_laps(
    snapshot, *, manual_override: int | None, current_leader_lap: int
) -> tuple[int, str]:
    """Resolve Lmax without inferring scheduled distance from completed laps."""
    live_value = getattr(snapshot, "scheduled_total_laps", None)
    if live_value is not None and int(live_value) > 0:
        return int(live_value), "live_lap_count"
    if manual_override is not None and int(manual_override) > 0:
        return int(manual_override), "manual_override"
    return int(current_leader_lap), "current_leader_lap_fallback"


def _live_analysis_laps(
    snapshot,
    *,
    quick_lap_threshold: float,
    min_clean_laps: int,
    allowed_compounds: tuple[str, ...] = ("SOFT", "MEDIUM", "HARD"),
    exclude_stint_start_laps: int = 0,
    exclude_stint_end_laps: int = 0,
    use_sector_boundary_traffic: bool = False,
    min_gap_ahead_seconds: float = 2.5,
    min_gap_behind_seconds: float = 1.0,
) -> pd.DataFrame:
    if not allowed_compounds:
        raise ValueError("At least one clean-lap compound must be selected.")
    if exclude_stint_start_laps < 0 or exclude_stint_end_laps < 0:
        raise ValueError("Stint edge exclusions must be non-negative.")
    if min_gap_ahead_seconds < 0 or min_gap_behind_seconds < 0:
        raise ValueError("Traffic gaps must be non-negative.")
    rows = []
    diagnostics = {
        "completed_laps": int(len(snapshot.completed_laps)),
        "missing_tyre_metadata": 0,
        "invalid_lap": 0,
        "disallowed_compound": 0,
        "pit_lap": 0,
        "non_green_track_status": 0,
        "non_quick_lap": 0,
        "traffic": 0,
        "stint_edge": 0,
        "insufficient_consecutive_laps": 0,
        "selected_clean_laps": 0,
    }
    for lap in snapshot.completed_laps:
        if lap.compound is None or lap.tyre_life is None or lap.stint is None:
            diagnostics["missing_tyre_metadata"] += 1
            continue
        if not bool(getattr(lap, "is_valid", True)):
            diagnostics["invalid_lap"] += 1
            continue
        rows.append(
            {
                "Driver": lap.driver_code,
                "Team": lap.team,
                "LapNumber": int(lap.lap_number),
                "LapTime": pd.to_timedelta(float(lap.lap_time_s), unit="s"),
                "Compound": str(lap.compound).upper(),
                "TyreLife": float(lap.tyre_life),
                "Stint": int(lap.stint),
                "PitOutTime": (
                    pd.to_timedelta(0, unit="s")
                    if bool(getattr(lap, "pit_out", False)) else pd.NaT
                ),
                "PitInTime": (
                    pd.to_timedelta(0, unit="s")
                    if bool(getattr(lap, "pit_in", False)) else pd.NaT
                ),
                "TrackStatus": getattr(lap, "track_status", None),
                "SessionName": "R",
                "track_index": int(lap.lap_number),
            }
        )
    laps = pd.DataFrame(rows)
    if laps.empty:
        laps.attrs["exclusion_diagnostics"] = diagnostics
        return laps
    laps = laps.sort_values(["Driver", "Stint", "LapNumber"], kind="mergesort")
    laps["StintLapNumber"] = laps.groupby(["Driver", "Stint"]).cumcount() + 1
    normalized_compounds = tuple(str(value).upper() for value in allowed_compounds)
    traffic = _live_traffic_gaps(snapshot, force_sector=use_sector_boundary_traffic)
    laps = laps.merge(traffic, on=["Driver", "LapNumber"], how="left")
    laps["MeanTimeDeltaToDriverAhead"] = laps["TrafficGapAheadSeconds"]
    laps["MeanTimeDeltaToDriverBehind"] = laps["TrafficGapBehindSeconds"]
    if laps["TrackStatus"].isna().all():
        laps = laps.drop(columns="TrackStatus")
    else:
        laps["TrackStatus"] = laps["TrackStatus"].fillna("UNKNOWN")
    prepared, _ = prepare_and_select_clean_air_laps(
        laps,
        min_clean_air_laps=1,
        clean_mean_time_delta_seconds=min_gap_ahead_seconds,
        clean_mean_time_delta_behind_seconds=min_gap_behind_seconds,
        quick_lap_threshold=quick_lap_threshold,
        dry_compounds=normalized_compounds,
    )
    diagnostics["disallowed_compound"] = int(
        (~prepared["Compound"].isin(normalized_compounds)).sum()
    )
    clean_before_edges = int(prepared["IsCleanAirLongRunLap"].sum())
    if exclude_stint_start_laps or exclude_stint_end_laps:
        _exclude_clean_run_edges(
            prepared,
            start_count=exclude_stint_start_laps,
            end_count=exclude_stint_end_laps,
        )
    diagnostics["stint_edge"] = clean_before_edges - int(
        prepared["IsCleanAirLongRunLap"].sum()
    )
    selected = select_consecutive_clean_air_runs(
        prepared, min_clean_air_laps=int(min_clean_laps)
    )
    # The live Retro MC contract historically called this identifier RunId;
    # preserve that input name while deriving the run with the shared selector.
    selected["RunId"] = selected["LongRunId"]
    diagnostics["pit_lap"] = int((prepared["IsOutLap"] | prepared["IsInLap"]).sum())
    diagnostics["non_green_track_status"] = int((~prepared["IsGreenTrackStatus"]).sum())
    diagnostics["non_quick_lap"] = int((~prepared["IsQuickLap"]).sum())
    traffic_ok = traffic_clean_mask(
        prepared,
        minimum_ahead_seconds=min_gap_ahead_seconds,
        minimum_behind_seconds=min_gap_behind_seconds,
    )
    diagnostics["traffic"] = int((~traffic_ok).sum())
    individual = int(prepared["IsCleanAirLongRunLap"].sum())
    diagnostics["insufficient_consecutive_laps"] = individual - int(len(selected))
    diagnostics["selected_clean_laps"] = int(len(selected))
    selected.attrs["exclusion_diagnostics"] = diagnostics
    return selected


def _exclude_clean_run_edges(
    laps: pd.DataFrame, *, start_count: int, end_count: int
) -> None:
    group_columns = ["Driver", "Stint"]
    ordered = laps.sort_values(group_columns + ["LapNumber"], kind="mergesort")
    for _, group in ordered.groupby(group_columns, dropna=False, sort=False):
        clean = group["IsCleanAirLongRunLap"].fillna(False).astype(bool)
        block_id = clean.ne(clean.shift(fill_value=False)).cumsum()
        for _, block in group.loc[clean].groupby(block_id.loc[clean], sort=False):
            excluded = list(block.index[: int(start_count)])
            if end_count:
                excluded.extend(block.index[-int(end_count):])
            laps.loc[excluded, "IsCleanAirLongRunLap"] = False


def live_position_traffic_shadow(snapshot, *, reference, coordinate_frame, cutoff, targets=None):
    """Inspect positions preserved by the real ingress/sample-time analysis cut.

    Diagnostic only: its outputs are not attached to LiveAnalysisInput. Lap
    membership follows target timing-publication boundaries, not physical finish
    crossings. No inferred first-lap start is supplied on a mid-session join.
    """
    from wostrategy.analysis.position_traffic import position_traffic_shadow

    codes = {driver.driver_number: driver.driver_code for driver in snapshot.drivers}
    positions = pd.DataFrame([
        {"Driver": codes.get(sample.driver_number, sample.driver_number),
         "SessionTime": sample.session_time_seconds, "X": sample.x, "Y": sample.y,
         "Z": sample.z, "OnTrack": sample.on_track}
        for sample in getattr(snapshot, "position_samples", ())
    ])
    windows = []
    by_driver = {}
    for lap in snapshot.completed_laps:
        clock = _completion_clock(lap)
        if clock is not None and clock <= cutoff:
            by_driver.setdefault(lap.driver_code, []).append((lap.lap_number, clock))
    for driver, boundaries in by_driver.items():
        ordered = sorted(boundaries)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] == previous[0] + 1 and current[1] > previous[1]:
                windows.append(dict(Driver=driver, LapNumber=current[0], Start=previous[1],
                                    End=current[1], Basis="target_timing_publication"))
    return position_traffic_shadow(
        positions, reference=reference, coordinate_frame=coordinate_frame, cutoff=cutoff,
        expected_drivers=set(codes.values()), targets=targets, lap_windows=pd.DataFrame(windows),
    )


def _live_traffic_gaps(snapshot, *, force_sector: bool = False) -> pd.DataFrame:
    prepared_rows = getattr(snapshot, "traffic_rows", ())
    if prepared_rows and force_sector:
        prepared = pd.DataFrame(list(prepared_rows))
        if "TrafficMethod" in prepared and prepared["TrafficMethod"].eq(SECTOR_BOUNDARY_FALLBACK).all():
            return prepared
    if force_sector:
        sector = _sector_boundary_traffic_gaps(snapshot)
        forced = _sector_as_selected_traffic(sector, reason="sector mode forced")
        return forced if not forced.empty else _unknown_live_traffic(
            snapshot, reason="sector mode forced but boundary data unavailable"
        )

    # Independent lap-labelled speed origins are not a shared track coordinate.
    # Position geometry/neighbours are validated separately in the shadow path;
    # its passage headways have not been validated for production decisions.
    # Never auto-promote missing geometry or diagnostic gaps to clean evidence.
    return _unknown_live_traffic(
        snapshot,
        reason=LIVE_TRAFFIC_QUARANTINE_REASON,
    )


def _snapshot_telemetry_frame(snapshot) -> pd.DataFrame:
    samples = getattr(snapshot, "telemetry_samples", ())
    if not samples:
        return pd.DataFrame(columns=["Driver", "SessionTime", "LapNumber", "Speed"])
    driver_codes = {driver.driver_number: driver.driver_code for driver in snapshot.drivers}
    return pd.DataFrame(
        [
            {
                "Driver": driver_codes.get(sample.driver_number, sample.driver_number),
                "SessionTime": float(sample.session_time_seconds),
                "LapNumber": int(sample.lap_number),
                "Speed": float(sample.speed_kph),
            }
            for sample in samples
        ]
    )


def _estimate_live_circuit_length(telemetry: pd.DataFrame) -> float | None:
    if telemetry.empty:
        return None
    ordered = telemetry.sort_values(["Driver", "LapNumber", "SessionTime"], kind="stable").copy()
    ordered["_Delta"] = ordered.groupby(["Driver", "LapNumber"])["SessionTime"].diff()
    ordered["_Distance"] = (ordered["Speed"] / 3.6) * ordered["_Delta"].clip(lower=0.0)
    spans = ordered.groupby(["Driver", "LapNumber"])["_Distance"].sum()
    spans = spans.loc[spans.ge(1000.0)]
    if spans.empty:
        return None
    median = float(spans.median())
    plausible = spans.loc[spans.between(median * 0.75, median * 1.25)]
    return float(plausible.median()) if not plausible.empty else median


def _sector_as_selected_traffic(sector: pd.DataFrame, *, reason: str) -> pd.DataFrame:
    if sector.empty:
        return pd.DataFrame(
            columns=[
                "Driver", "LapNumber", "TrafficGapAheadSeconds",
                "TrafficGapBehindSeconds", "TrafficMethod", "TrafficStatus",
                "TrafficStatusReason", "CoverageFraction",
            ]
        )
    output = sector.copy()
    output["TrafficGapAheadSeconds"] = output["MinGapAheadSeconds"]
    output["TrafficGapBehindSeconds"] = output["MinGapBehindSeconds"]
    output["TrafficMethod"] = SECTOR_BOUNDARY_FALLBACK
    output["TrafficStatus"] = "sector_fallback"
    output["TrafficStatusReason"] = reason
    output["CoverageFraction"] = 1.0
    return _with_live_traffic_contract(output)


def _unknown_live_traffic(snapshot, *, reason: str) -> pd.DataFrame:
    rows = {
        (str(lap.driver_code), int(lap.lap_number))
        for lap in snapshot.completed_laps
    }
    return _with_live_traffic_contract(pd.DataFrame(
        [
            {
                "Driver": driver,
                "LapNumber": lap_number,
                "TrafficGapAheadSeconds": pd.NA,
                "TrafficGapBehindSeconds": pd.NA,
                "TrafficMethod": pd.NA,
                "TrafficStatus": "unknown",
                "TrafficStatusReason": reason,
                "CoverageFraction": 0.0,
            }
            for driver, lap_number in sorted(rows)
        ],
        columns=["Driver", "LapNumber", "TrafficGapAheadSeconds", "TrafficGapBehindSeconds",
                 "TrafficMethod", "TrafficStatus", "TrafficStatusReason", "CoverageFraction"],
    ))


def _with_live_traffic_contract(traffic: pd.DataFrame) -> pd.DataFrame:
    output = traffic.copy()
    for prefix in ("Direct", "Physical", ""):
        for side in ("Ahead", "Behind"):
            column = f"{prefix}MeanTimeDeltaToDriver{side}"
            if column not in output:
                output[column] = float("nan")
    if "DirectTrafficStatus" not in output:
        output["DirectTrafficStatus"] = "unknown"
    if "PhysicalTrafficStatus" not in output:
        output["PhysicalTrafficStatus"] = "unknown"
    if "PhysicalCoverageFraction" not in output:
        output["PhysicalCoverageFraction"] = 0.0
    output["TrafficCombinationMode"] = COMBINATION_MODE_COMPATIBILITY_MIN
    output["TrafficEvaluatorVersion"] = TRAFFIC_EVALUATOR_VERSION
    return output


def _sector_boundary_traffic_gaps(snapshot) -> pd.DataFrame:
    """Return conservative nearest-car gaps at start/S1/S2/end boundaries.

    Sector messages expose their lap-completion timestamp rather than a GPS trace.
    Earlier boundary times are reconstructed by subtracting the remaining sector
    durations. A lap is returned only when both a preceding (ahead) and following
    (behind) crossing by another driver exist at all four boundaries.
    """
    grouped: dict[tuple[str, int], dict[str, object]] = {}
    for sector in snapshot.completed_sectors:
        if (
            sector.session_time_seconds is None
            and sector.source_timestamp_utc is None
        ):
            continue
        grouped.setdefault((str(sector.driver_code), int(sector.lap_number)), {})[
            str(sector.sector).upper()
        ] = sector

    crossings: list[dict[str, object]] = []
    for (driver, lap_number), sectors in grouped.items():
        if not all(name in sectors for name in ("S1", "S2", "S3")):
            continue
        s1, s2, s3 = (sectors[name] for name in ("S1", "S2", "S3"))
        finish = (
            float(s3.session_time_seconds)
            if s3.session_time_seconds is not None
            else s3.source_timestamp_utc.timestamp()
        )
        boundary_times = {
            "START": finish - s1.sector_time_s - s2.sector_time_s - s3.sector_time_s,
            "S1": finish - s2.sector_time_s - s3.sector_time_s,
            "S2": finish - s3.sector_time_s,
            "END": finish,
        }
        for boundary, crossing_time in boundary_times.items():
            crossings.append(
                {
                    "Driver": driver,
                    "LapNumber": lap_number,
                    "Boundary": boundary,
                    "CrossingTime": float(crossing_time),
                }
            )
    if not crossings:
        return pd.DataFrame(
            columns=["Driver", "LapNumber", "MinGapAheadSeconds", "MinGapBehindSeconds"]
        )
    by_boundary: dict[str, list[dict[str, object]]] = {}
    for crossing in crossings:
        by_boundary.setdefault(str(crossing["Boundary"]), []).append(crossing)
    gaps: dict[tuple[str, int], dict[str, list[float]]] = {}
    for boundary_crossings in by_boundary.values():
        ordered = sorted(boundary_crossings, key=lambda row: float(row["CrossingTime"]))
        for index, crossing in enumerate(ordered):
            driver = str(crossing["Driver"])
            key = (driver, int(crossing["LapNumber"]))
            previous_index = index - 1
            while previous_index >= 0 and str(ordered[previous_index]["Driver"]) == driver:
                previous_index -= 1
            next_index = index + 1
            while next_index < len(ordered) and str(ordered[next_index]["Driver"]) == driver:
                next_index += 1
            if previous_index < 0 or next_index >= len(ordered):
                continue
            crossing_time = float(crossing["CrossingTime"])
            entry = gaps.setdefault(key, {"ahead": [], "behind": []})
            entry["ahead"].append(
                crossing_time - float(ordered[previous_index]["CrossingTime"])
            )
            entry["behind"].append(
                float(ordered[next_index]["CrossingTime"]) - crossing_time
            )
    rows = [
        {
            "Driver": driver,
            "LapNumber": lap_number,
            "MinGapAheadSeconds": min(values["ahead"]),
            "MinGapBehindSeconds": min(values["behind"]),
        }
        for (driver, lap_number), values in gaps.items()
        if len(values["ahead"]) == 4 and len(values["behind"]) == 4
    ]
    return pd.DataFrame(
        rows,
        columns=["Driver", "LapNumber", "MinGapAheadSeconds", "MinGapBehindSeconds"],
    )
