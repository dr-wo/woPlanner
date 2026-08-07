from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd

from wodata import get_fastf1_session_laps_path
from wodata.artifacts import (
    read_latest_model_snapshot,
    read_weekend_model_config,
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
    results_to_records,
    search_best_compound_sequences,
)
from wostrategy.core.pre_race_session_data import prepare_weekend_sessions
from wostrategy.core.race_performance_settings import resolve_race_performance_settings
from wostrategy.model.pre_race_performance import WeekendModelConfig, run_joint_weekend_model
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


@dataclass(frozen=True)
class ModelSummary:
    manifest: dict[str, object]
    parameters: tuple[dict[str, str], ...]

    @property
    def status(self) -> str:
        return str(self.manifest.get("status") or "unavailable")


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


class OfflineAnalysisService:
    """Planner-facing adapter for canonical woData model/strategy artifacts."""

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = data_root
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
        for compound in degradation:
            deltas.setdefault(compound, 0.0)
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

    def strategy_assumptions(self, summary: ModelSummary) -> list[dict[str, object]]:
        """Return transparent support-weighted FP/race inputs used by strategy search."""
        model = self.build_strategy_model(summary)
        fastest = min(model.compound_delta.values(), default=0.0)
        rows = []
        for compound in ("SOFT", "MEDIUM", "HARD"):
            if compound not in model.degradation:
                continue
            contributing = [
                str(row.get("session") or "")
                for row in summary.parameters
                if str(row.get("parameter")) == "degradation"
                and str(row.get("compound") or "").upper() == compound
            ]
            delta = float(model.compound_delta.get(compound, 0.0))
            rows.append(
                {
                    "compound": compound,
                    "baseline_pace": delta - fastest,
                    "compound_delta": delta,
                    "degradation": float(model.degradation[compound]),
                    "sessions": "+".join(dict.fromkeys(contributing)),
                    "aggregation": "usable-lap weighted mean",
                }
            )
        return rows

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
    ) -> list[StrategyResult]:
        summary = self.load_model(year, round_number, source_scope)
        model = self.build_strategy_model(summary, overrides)
        results = search_best_compound_sequences(
            state,
            model,
            pit_loss,
            max_stops=state.max_future_stops,
            k=k,
            allow_any_start=(
                state.current_completed_lap == 0 and state.selected_driver is None
            ),
        )
        target = state.selected_driver or "global"
        records = results_to_records(results, state, model)
        self._last_strategy_search = (records, target, int(year), int(round_number))
        if persist:
            write_strategy_records(
                records,
                year=year,
                round_number=round_number,
                target=target,
                data_root=self.data_root,
            )
        return results

    def model_config(self, year: int, round_number: int) -> dict[str, object]:
        return read_weekend_model_config(
            year=year, round_number=round_number, data_root=self.data_root
        )

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
        persist: bool = True,
    ) -> LiveRecalculationResult:
        started_at = time.perf_counter()
        leader_lap = _leader_lap(snapshot)
        previous_clean_lap_count = self._last_clean_lap_count
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
        sessions: dict[str, pd.DataFrame] = {"R": race_laps}
        raw_fp = {}
        for session in ("FP1", "FP2", "FP3"):
            path = get_fastf1_session_laps_path(
                year=year, round_number=round_number, session=session, data_root=self.data_root
            )
            if path.exists():
                raw_fp[session] = pd.read_pickle(path)
        if raw_fp:
            prepared, _ = prepare_weekend_sessions(
                raw_fp,
                min_clean_air_laps=int(stored.get("min_clean_air_laps", 4)),
                clean_mean_time_delta_seconds=float(
                    stored.get("clean_mean_time_delta_seconds", 2.5)
                ),
                clean_mean_time_delta_behind_seconds=stored.get(
                    "clean_mean_time_delta_behind_seconds", 1.0
                ),
                quick_lap_threshold=float(stored.get("quick_lap_threshold", 1.10)),
                dry_compounds=tuple(stored.get("expected_compounds", ("SOFT", "MEDIUM", "HARD"))),
            )
            sessions.update(prepared)
        clean_lap_sources = tuple(
            (session, int(len(laps))) for session, laps in sorted(sessions.items())
        )
        result = run_joint_weekend_model(
            sessions,
            config,
            expected_sessions=tuple(stored.get("expected_sessions", ("FP1", "FP2", "FP3", "R"))),
            expected_compounds=tuple(stored.get("expected_compounds", ("SOFT", "MEDIUM", "HARD"))),
            as_of_leader_lap=leader_lap,
        )
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
        return LiveRecalculationResult(
            "updated", leader_lap, len(race_laps), result.aggregate_snapshot.analysis_id,
            previous_clean_lap_count=previous_clean_lap_count,
            clean_lap_sources=clean_lap_sources,
            elapsed_seconds=time.perf_counter() - started_at,
            sample_count=int(sample_count),
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
        if self._last_strategy_search is not None:
            records, target, search_year, search_round = self._last_strategy_search
            if (search_year, search_round) == (int(year), int(round_number)):
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
    }
    return ModelSummary(manifest, tuple(asdict(row) for row in snapshot.parameters))


def _read_csv_if_present(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _leader_lap(snapshot) -> int:
    leader = [lap.lap_number for lap in snapshot.completed_laps if lap.position == 1]
    all_laps = [lap.lap_number for lap in snapshot.completed_laps]
    return int(max(leader or all_laps or [0]))


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
    for lap in snapshot.completed_laps:
        if lap.compound is None or lap.tyre_life is None or lap.stint is None:
            continue
        rows.append(
            {
                "Driver": lap.driver_code,
                "Team": lap.team,
                "LapNumber": int(lap.lap_number),
                "LapTimeSeconds": float(lap.lap_time_s),
                "Compound": str(lap.compound).upper(),
                "TyreLife": max(float(lap.tyre_life) - 1.0, 0.0),
                "Stint": int(lap.stint),
                "SessionName": "R",
                "track_index": int(lap.lap_number),
            }
        )
    laps = pd.DataFrame(rows)
    if laps.empty:
        return laps
    normalized_compounds = tuple(str(value).upper() for value in allowed_compounds)
    laps = laps.loc[laps["Compound"].isin(normalized_compounds)].copy()
    fastest = laps.groupby("Driver")["LapTimeSeconds"].transform("min")
    laps = laps.loc[laps["LapTimeSeconds"] <= float(quick_lap_threshold) * fastest].copy()
    traffic = _live_traffic_gaps(snapshot, force_sector=use_sector_boundary_traffic)
    laps = laps.merge(traffic, on=["Driver", "LapNumber"], how="left")
    laps = laps.loc[
        traffic_clean_mask(
            laps,
            minimum_ahead_seconds=min_gap_ahead_seconds,
            minimum_behind_seconds=min_gap_behind_seconds,
            ahead_column="TrafficGapAheadSeconds",
            behind_column="TrafficGapBehindSeconds",
            allow_missing_side_with_other_evidence=False,
        )
    ].copy()
    laps = laps.sort_values(["Driver", "Stint", "LapNumber"], kind="mergesort")
    gap = laps.groupby(["Driver", "Stint"])["LapNumber"].diff().fillna(1).ne(1)
    laps["_run_group"] = gap.groupby([laps["Driver"], laps["Stint"]]).cumsum()
    laps["RunId"] = (
        "R:"
        + laps["Driver"].astype(str)
        + ":"
        + laps["Stint"].astype(str)
        + ":"
        + laps["_run_group"].astype(str)
    )
    group_columns = ["Driver", "Stint", "_run_group"]
    position_from_start = laps.groupby(group_columns).cumcount()
    position_from_end = laps.groupby(group_columns).cumcount(ascending=False)
    laps = laps.loc[
        position_from_start.ge(int(exclude_stint_start_laps))
        & position_from_end.ge(int(exclude_stint_end_laps))
    ].copy()
    run_sizes = laps.groupby("RunId")["RunId"].transform("size")
    return laps.loc[run_sizes >= int(min_clean_laps)].drop(columns="_run_group")


def _live_traffic_gaps(snapshot, *, force_sector: bool = False) -> pd.DataFrame:
    keys = ["Driver", "LapNumber"]
    sector = _sector_boundary_traffic_gaps(snapshot)
    if force_sector:
        forced = _sector_as_selected_traffic(sector, reason="sector mode forced")
        return forced if not forced.empty else _unknown_live_traffic(
            snapshot, reason="sector mode forced but boundary data unavailable"
        )

    telemetry = _snapshot_telemetry_frame(snapshot)
    circuit_length = _estimate_live_circuit_length(telemetry)
    if telemetry.empty or circuit_length is None:
        reason = "telemetry unavailable" if telemetry.empty else "circuit length unavailable"
        fallback = _sector_as_selected_traffic(sector, reason=reason)
        return fallback if not fallback.empty else _unknown_live_traffic(
            snapshot, reason=f"{reason}; sector fallback unavailable"
        )

    physical = evaluate_physical_traffic(
        telemetry,
        config=TrafficEvaluationConfig(circuit_length_m=circuit_length),
        group_columns=("Driver", "LapNumber"),
    ).laps
    direct = evaluate_direct_fastf1_style(
        telemetry,
        group_columns=("Driver", "LapNumber"),
    ).laps
    selected = combine_lap_traffic_components(
        direct,
        physical,
        group_columns=tuple(keys),
        combination_mode=COMBINATION_MODE_COMPATIBILITY_MIN,
    )
    selected["TrafficGapAheadSeconds"] = selected["MeanTimeDeltaToDriverAhead"]
    selected["TrafficGapBehindSeconds"] = selected["MeanTimeDeltaToDriverBehind"]

    fallback = _sector_as_selected_traffic(
        sector,
        reason="telemetry unavailable or below coverage threshold",
    )
    if fallback.empty:
        return selected
    fallback = fallback.set_index(keys)
    selected = selected.set_index(keys)
    bad = ~selected["TrafficStatus"].eq("ok")
    replacement_keys = selected.index[bad].intersection(fallback.index)
    if len(replacement_keys):
        replacement_columns = [
            "TrafficGapAheadSeconds",
            "TrafficGapBehindSeconds",
            "TrafficMethod",
            "TrafficStatus",
            "TrafficStatusReason",
            "CoverageFraction",
        ]
        selected.loc[replacement_keys, replacement_columns] = fallback.loc[
            replacement_keys, replacement_columns
        ]
    missing_keys = fallback.index.difference(selected.index)
    if len(missing_keys):
        selected = pd.concat([selected, fallback.loc[missing_keys]], axis=0, sort=False)
    return selected.reset_index()


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
        ]
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
