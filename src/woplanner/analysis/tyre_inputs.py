"""Resolve tyre inputs used by offline and live analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
import json
import math

from wodata.artifacts import weekend_model_root
from wodata.contracts import canonical_hash
from wostrategy import (
    calculate_fp_tyre_evidence,
    load_fp_tyre_evidence,
    local_fp_tyre_evidence_status,
)
from wostrategy.analysis.pre_race_tyre_prediction import (
    MissingPreRaceTyrePrediction,
    get_pre_race_tyre_prediction,
)
from .weekend_tyre_import import load_pre_race_tyre_import, list_pre_race_tyre_imports


MANUAL_TYRE_INPUT_SCHEMA_VERSION = 1
MANUAL_TYRE_INPUT_FILENAME = "manual_tyre_inputs_v1.json"
TYRE_PARAMETERS = (
    "Deg_HARD",
    "Deg_MEDIUM",
    "Deg_SOFT",
    "Perf_HARD_vs_MEDIUM",
    "Perf_SOFT_vs_MEDIUM",
)
PARAMETER_UNITS = {
    "Deg_HARD": "s/lap",
    "Deg_MEDIUM": "s/lap",
    "Deg_SOFT": "s/lap",
    "Perf_HARD_vs_MEDIUM": "s",
    "Perf_SOFT_vs_MEDIUM": "s",
}


@dataclass(frozen=True)
class ResolvedTyreInput:
    parameter: str
    selected_value: float | None
    selected_source: str
    manual_value: float | None
    legacy_value: float | None
    fp_direct_value: float | None
    fp_direct_support: str | None
    relevant_fp_contrast: str | None
    fp_contrast_value: float | None
    unit: str
    limitations: str
    independent_of_current_fp: bool
    default_source: str = "missing"


@dataclass(frozen=True)
class ResolvedTyreInputs:
    schema_version: int
    year: int
    round_number: int
    resolved_at: str
    legacy_source_analysis_id: str | None
    legacy_artifact: str | None
    fp_evidence_analysis_id: str | None
    parameters: tuple[ResolvedTyreInput, ...]
    semantics: Mapping[str, object]
    content_hash: str
    team: str | None = None

    def by_parameter(self) -> dict[str, ResolvedTyreInput]:
        return {row.parameter: row for row in self.parameters}

    def strategy_overrides(self) -> dict[tuple[str, str], float]:
        values = self.by_parameter()
        missing = [
            name for name in TYRE_PARAMETERS
            if values[name].selected_value is None
        ]
        if missing:
            raise ValueError("Missing required tyre inputs: " + ", ".join(missing))
        return {
            ("degradation", "HARD"): float(values["Deg_HARD"].selected_value),
            ("degradation", "MEDIUM"): float(values["Deg_MEDIUM"].selected_value),
            ("degradation", "SOFT"): float(values["Deg_SOFT"].selected_value),
            ("compound_delta", "MEDIUM"): 0.0,
            ("compound_delta", "HARD"): float(values["Perf_HARD_vs_MEDIUM"].selected_value),
            ("compound_delta", "SOFT"): float(values["Perf_SOFT_vs_MEDIUM"].selected_value),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "year": self.year,
            "round_number": self.round_number,
            "resolved_at": self.resolved_at,
            "legacy_source_analysis_id": self.legacy_source_analysis_id,
            "legacy_artifact": self.legacy_artifact,
            "fp_evidence_analysis_id": self.fp_evidence_analysis_id,
            "parameters": [asdict(row) for row in self.parameters],
            "semantics": dict(self.semantics),
            "content_hash": self.content_hash,
            "team": self.team,
        }


@dataclass(frozen=True)
class FPEvidenceCacheState:
    status: str
    evidence: dict[str, object] | None
    stale: bool = False
    stale_reason: str = ""


class TyreInputService:
    """Single Planner/live owner for manual persistence and fallback resolution."""

    def __init__(
        self,
        data_root: str | Path | None = None,
        *,
        fp_calculator: Callable[..., dict[str, object]] = calculate_fp_tyre_evidence,
    ) -> None:
        self.data_root = data_root
        self._fp_calculator = fp_calculator

    def manual_path(self, year: int, round_number: int) -> Path:
        return (
            weekend_model_root(year, round_number, self.data_root)
            / MANUAL_TYRE_INPUT_FILENAME
        )

    def _load_manual_payload(self, year: int, round_number: int) -> dict[str, object]:
        path = self.manual_path(year, round_number)
        if not path.is_file():
            return {}
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if int(payload.get("schema_version", 0)) != MANUAL_TYRE_INPUT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported manual tyre-input schema in {path}.")
        if int(payload.get("year")) != int(year) or int(payload.get("round_number")) != int(round_number):
            raise ValueError(f"Manual tyre inputs in {path} belong to another event.")
        return payload

    def load_manual_inputs(
        self, year: int, round_number: int, *, team: str | None = None
    ) -> dict[str, float | None]:
        payload = self._load_manual_payload(year, round_number)
        raw_values = dict(payload.get("values") or {}) if team is None else dict(
            dict(payload.get("team_values") or {}).get(team) or {}
        )
        values = {}
        for parameter in TYRE_PARAMETERS:
            value = raw_values.get(parameter)
            values[parameter] = None if value is None else _finite(value, parameter)
        return values

    def save_manual_inputs(
        self,
        year: int,
        round_number: int,
        values: Mapping[str, float | None],
        *,
        team: str | None = None,
        import_origin: Mapping[str, object] | None = None,
    ) -> Path:
        unknown = set(values).difference(TYRE_PARAMETERS)
        if unknown:
            raise ValueError(f"Unknown manual tyre parameters: {sorted(unknown)}")
        normalized = {
            parameter: (
                None if values.get(parameter) is None else _finite(values[parameter], parameter)
            )
            for parameter in TYRE_PARAMETERS
        }
        payload = self._load_manual_payload(year, round_number)
        payload.update({
            "schema_version": MANUAL_TYRE_INPUT_SCHEMA_VERSION,
            "year": int(year),
            "round_number": int(round_number),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if team is None:
            payload["values"] = normalized
            origin = _matching_import_origin(
                import_origin if import_origin is not None else payload.get("import_origin"),
                normalized,
            )
            if origin:
                payload["import_origin"] = origin
            else:
                payload.pop("import_origin", None)
        else:
            team_values = dict(payload.get("team_values") or {})
            team_values[team] = normalized
            payload["team_values"] = team_values
        path = self.manual_path(year, round_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
        return path

    def available_pre_race_runs(self, year: int, round_number: int):
        return list_pre_race_tyre_imports(year, round_number, self.data_root)

    def import_pre_race_run(self, year: int, round_number: int, run_id: str) -> Path:
        """Copy one immutable run into Global manuals; never establish a binding."""
        selected = load_pre_race_tyre_import(year, round_number, run_id, self.data_root)
        return self.save_manual_inputs(
            year, round_number, selected.values, import_origin=selected.origin
        )

    def set_manual_value(
        self, year: int, round_number: int, parameter: str, value: float,
        *, team: str | None = None,
    ) -> Path:
        if parameter not in TYRE_PARAMETERS:
            raise ValueError(f"Unknown manual tyre parameter: {parameter}")
        values = self.load_manual_inputs(year, round_number, team=team)
        values[parameter] = _finite(value, parameter)
        return self.save_manual_inputs(year, round_number, values, team=team)

    def clear_manual_value(
        self, year: int, round_number: int, parameter: str, *, team: str | None = None
    ) -> Path:
        if parameter not in TYRE_PARAMETERS:
            raise ValueError(f"Unknown manual tyre parameter: {parameter}")
        values = self.load_manual_inputs(year, round_number, team=team)
        values[parameter] = None
        return self.save_manual_inputs(year, round_number, values, team=team)

    def clear_all_manual_values(
        self, year: int, round_number: int, *, team: str | None = None
    ) -> Path:
        return self.save_manual_inputs(
            year, round_number, {parameter: None for parameter in TYRE_PARAMETERS}, team=team
        )

    def get_cached_fp_tyre_evidence(
        self, year: int, round_number: int
    ) -> FPEvidenceCacheState:
        """Local read/staleness check only; never invokes the calculator."""
        evidence = load_fp_tyre_evidence(year, round_number, self.data_root)
        if evidence is None:
            return FPEvidenceCacheState("not_calculated", None)
        status = local_fp_tyre_evidence_status(
            evidence,
            year=year,
            round_number=round_number,
            data_root=self.data_root,
        )
        return FPEvidenceCacheState(
            "cached_stale" if status["stale"] else "cached",
            evidence,
            bool(status["stale"]),
            str(status["reason"]),
        )

    def calculate_fp_tyre_evidence(
        self, year: int, round_number: int, *, force_refresh: bool = False
    ) -> dict[str, object]:
        """Explicit calculation entry point. Call only from a user action."""
        return self._fp_calculator(
            year=year,
            round_number=round_number,
            data_root=self.data_root,
            force_refresh=force_refresh,
        )

    def resolve(
        self, year: int, round_number: int, *, team: str | None = None
    ) -> ResolvedTyreInputs:
        manual = self.load_manual_inputs(year, round_number, team=team)
        defaults, default_sources, source_id, default_path, source_semantics = self._default_values(
            year, round_number
        )
        if team is None:
            origin = _matching_import_origin(
                self._load_manual_payload(year, round_number).get("import_origin"), manual
            )
            if origin:
                source_semantics["manual_import_origin"] = origin
        if team is not None:
            source_semantics = {
                **source_semantics,
                "team": team,
                "manual_scope": "team",
                "performance_model_scope": "global",
                "global_manual_inherited": False,
            }
        cache = self.get_cached_fp_tyre_evidence(year, round_number)
        evidence = cache.evidence or {}
        direct = {
            str(row.get("parameter")): row
            for row in evidence.get("performance_support") or ()
        }
        contrasts = {
            str(row.get("parameter")): row
            for row in evidence.get("degradation_contrasts") or ()
        }
        relevant = {
            "Deg_HARD": "Delta_HM",
            "Deg_MEDIUM": None,
            "Deg_SOFT": "Delta_SM",
            "Perf_HARD_vs_MEDIUM": None,
            "Perf_SOFT_vs_MEDIUM": None,
        }
        rows = []
        for parameter in TYRE_PARAMETERS:
            manual_value = manual[parameter]
            selected_source = (
                "manual" if manual_value is not None
                else default_sources[parameter] if defaults[parameter] is not None
                else "missing"
            )
            selected = manual_value if manual_value is not None else defaults[parameter]
            performance = direct.get(parameter, {})
            contrast_name = relevant[parameter]
            contrast = contrasts.get(contrast_name or "", {})
            if parameter.startswith("Deg_"):
                limitation = (
                    "Absolute default degradation shares the Fuel/common-D gauge. "
                    "FP contrast evidence is advisory only."
                )
            else:
                limitation = str(
                    performance.get("limitation")
                    or "Default performance has zero/negligible likelihood sensitivity."
                )
            rows.append(
                ResolvedTyreInput(
                    parameter=parameter,
                    selected_value=None if selected is None else float(selected),
                    selected_source=selected_source,
                    manual_value=manual_value,
                    legacy_value=(
                        None if defaults[parameter] is None else float(defaults[parameter])
                    ),
                    fp_direct_value=(
                        None if performance.get("fp_direct_value") is None
                        else float(performance["fp_direct_value"])
                    ),
                    fp_direct_support=(
                        str(performance.get("support_status")) if performance else None
                    ),
                    relevant_fp_contrast=contrast_name,
                    fp_contrast_value=(
                        None if contrast.get("strict_central") is None
                        else float(contrast["strict_central"])
                    ),
                    unit=PARAMETER_UNITS[parameter],
                    limitations=limitation,
                    independent_of_current_fp=(selected_source == "manual"),
                    default_source=default_sources[parameter],
                )
            )
        content = {
            "year": int(year), "round_number": int(round_number),
            "legacy_source_analysis_id": source_id,
            "fp_evidence_analysis_id": evidence.get("analysis_id"),
            "parameters": [asdict(row) for row in rows],
            "semantics": source_semantics,
        }
        return ResolvedTyreInputs(
            schema_version=1,
            year=int(year),
            round_number=int(round_number),
            resolved_at=datetime.now(timezone.utc).isoformat(),
            legacy_source_analysis_id=source_id,
            legacy_artifact=None if default_path is None else str(default_path),
            fp_evidence_analysis_id=(
                str(evidence.get("analysis_id")) if evidence.get("analysis_id") else None
            ),
            parameters=tuple(rows),
            semantics=source_semantics,
            content_hash=canonical_hash(content),
            team=team,
        )

    def physical_order_warnings(self, resolved: ResolvedTyreInputs) -> tuple[str, ...]:
        values = {row.parameter: row.selected_value for row in resolved.parameters}
        warnings = []
        if values["Deg_HARD"] is not None and values["Deg_MEDIUM"] is not None and values["Deg_HARD"] > values["Deg_MEDIUM"]:
            warnings.append("HARD degradation is greater than MEDIUM degradation.")
        if values["Deg_MEDIUM"] is not None and values["Deg_SOFT"] is not None and values["Deg_MEDIUM"] > values["Deg_SOFT"]:
            warnings.append("MEDIUM degradation is greater than SOFT degradation.")
        if values["Perf_HARD_vs_MEDIUM"] is not None and values["Perf_HARD_vs_MEDIUM"] < 0:
            warnings.append("HARD is entered quicker than MEDIUM.")
        if values["Perf_SOFT_vs_MEDIUM"] is not None and values["Perf_SOFT_vs_MEDIUM"] > 0:
            warnings.append("SOFT is entered slower than MEDIUM.")
        return tuple(warnings)

    def _default_values(self, year: int, round_number: int):
        values = {parameter: None for parameter in TYRE_PARAMETERS}
        sources = {parameter: "missing" for parameter in TYRE_PARAMETERS}
        source_id = None
        source_path = None
        semantics: dict[str, object] = {
            "contract": "planner_tyre_inputs_v1",
            "performance": {
                "unit": "s",
                "independent_variable": "dry_compound_choice",
                "positive_sign": "compound is slower than MEDIUM",
                "frame": "relative_to_compound",
                "reference_compound": "MEDIUM",
                "reference_value": 0.0,
            },
            "degradation": {
                "unit": "s/tyre-age-lap",
                "independent_variable": "tyre_age_laps",
                "positive_sign": "greater tyre age increases modelled lap time",
                "frame": "absolute_rate",
            },
            "uncertainty": "not_carried_by_resolved_strategy_scalar_inputs",
            "source_contract": "unavailable",
        }
        try:
            prediction = get_pre_race_tyre_prediction(
                season=year,
                round_number=round_number,
                data_root=self.data_root,
            )
        except MissingPreRaceTyrePrediction:
            prediction = None
        if prediction is not None:
            prediction_provenance = dict(prediction.provenance)
            for compound, item in prediction.compounds.items():
                compound = str(compound).upper()
                if compound in {"HARD", "MEDIUM", "SOFT"}:
                    values[f"Deg_{compound}"] = float(item.degradation_seconds_per_lap)
                    sources[f"Deg_{compound}"] = "pre_race_prediction"
                if compound == "HARD":
                    values["Perf_HARD_vs_MEDIUM"] = float(item.performance_delta_to_medium)
                    sources["Perf_HARD_vs_MEDIUM"] = "pre_race_prediction"
                elif compound == "SOFT":
                    values["Perf_SOFT_vs_MEDIUM"] = float(item.performance_delta_to_medium)
                    sources["Perf_SOFT_vs_MEDIUM"] = "pre_race_prediction"
            source_id = prediction.artifact_id
            source_path = prediction_provenance.get("path")
            semantics["source_contract"] = "canonical_pre_race_tyre_prediction"
            semantics["source_performance"] = dict(
                prediction_provenance.get("performance_semantics", {})
            )
            semantics["source_degradation"] = dict(
                prediction_provenance.get("degradation_semantics", {})
            )
        legacy, legacy_id, legacy_path = self._legacy_values(year, round_number)
        for parameter, value in legacy.items():
            if values[parameter] is None:
                values[parameter] = value
                sources[parameter] = "legacy_fp_derived"
        if source_id is None:
            source_id, source_path = legacy_id, legacy_path
            if legacy_id is not None:
                semantics["source_contract"] = "legacy_fp_derived"
                semantics["legacy_uncertainty"] = (
                    "marginal summaries; cross-compound joint covariance unavailable"
                )
        return values, sources, source_id, source_path, semantics

    def _legacy_values(self, year: int, round_number: int):
        path = weekend_model_root(year, round_number, self.data_root) / "pre_race_prior_v1.json"
        if not path.is_file():
            return {}, None, None
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        degradation = dict(payload.get("degradation") or {})
        performance = dict(payload.get("performance") or {})
        missing = [
            compound for compound in ("HARD", "MEDIUM", "SOFT")
            if compound not in degradation or compound not in performance
            or degradation[compound].get("median") is None
            or performance[compound].get("median") is None
        ]
        if missing:
            raise ValueError(f"Legacy pre-race artifact lacks required fields for {missing}.")
        values = {
            "Deg_HARD": float(degradation["HARD"]["median"]),
            "Deg_MEDIUM": float(degradation["MEDIUM"]["median"]),
            "Deg_SOFT": float(degradation["SOFT"]["median"]),
            "Perf_HARD_vs_MEDIUM": float(
                performance["HARD"]["median"] - performance["MEDIUM"]["median"]
            ),
            "Perf_SOFT_vs_MEDIUM": float(
                performance["SOFT"]["median"] - performance["MEDIUM"]["median"]
            ),
        }
        return values, str(payload.get("source_analysis_id") or "unknown"), path


def tyre_snapshot_from_overrides(
    overrides: Mapping[tuple[str, str], float],
    resolved: ResolvedTyreInputs,
    *,
    assumptions: Sequence[Mapping[str, object]] | None = None,
    manual_values: Mapping[tuple[str, str], float] | None = None,
) -> dict[str, object]:
    """Freeze actual values with provenance from the same effective resolution.

    Callers with live inputs must pass its assumption rows. A legacy caller
    changing a value without supplying provenance gets an explicit unattributed
    source, never the stale pre-race source of a different value.
    """
    medium = float(overrides[("compound_delta", "MEDIUM")])
    manual = manual_values or {}
    if float(manual.get(("compound_delta", "MEDIUM"), 0.0)) != 0.0:
        raise ValueError("MEDIUM performance is the fixed reference and must be 0.")
    actual = {
        "Deg_HARD": float(overrides[("degradation", "HARD")]),
        "Deg_MEDIUM": float(overrides[("degradation", "MEDIUM")]),
        "Deg_SOFT": float(overrides[("degradation", "SOFT")]),
        "Perf_HARD_vs_MEDIUM": float(overrides[("compound_delta", "HARD")]) - medium,
        "Perf_SOFT_vs_MEDIUM": float(overrides[("compound_delta", "SOFT")]) - medium,
    }
    rows = {str(row["compound"]).upper(): row for row in assumptions or ()}
    resolved_rows = resolved.by_parameter()
    source = {}
    for name, value in actual.items():
        degradation = name.startswith("Deg_")
        compound = name.split("_")[1]
        parameter = "degradation" if degradation else "compound_delta"
        coordinate = (parameter, compound)
        if coordinate in manual and float(manual[coordinate]) == value:
            source[name] = "manual"
            continue
        row = rows.get(compound)
        if row is not None:
            reference = rows.get("MEDIUM")
            expected = float(row[parameter])
            if not degradation:
                if reference is None:
                    source[name] = "unattributed_override"
                    continue
                expected -= float(reference[parameter])
            if value == expected:
                source[name] = str(
                    row.get("degradation_value_source", row.get("degradation_source"))
                    if degradation else row.get("compound_delta_source")
                )
                continue
        item = resolved_rows[name]
        source[name] = (
            item.selected_source
            if not rows and value == item.selected_value
            else "unattributed_override"
        )
    content = {
        "values": actual,
        "selected_sources": source,
        "semantics": dict(resolved.semantics),
        "team": resolved.team,
    }
    origin = _matching_import_origin(
        resolved.semantics.get("manual_import_origin"),
        {name: value for name, value in actual.items() if source[name] == "manual"},
    )
    content["semantics"].pop("manual_import_origin", None)
    if origin:
        content["semantics"]["manual_import_origin"] = origin
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "resolved_content_hash": resolved.content_hash,
        **content,
        "content_hash": canonical_hash(content),
    }


def _matching_import_origin(origin, values):
    """Keep attribution only for fields still using the copied numerical value."""
    if not origin:
        return None
    fields = {
        name: dict(field) for name, field in origin.get("fields", {}).items()
        if values.get(name) is not None and values[name] == field.get("value")
    }
    return {**origin, "fields": fields} if fields else None


def _finite(value, parameter: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{parameter} must be numeric.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{parameter} must be finite.")
    return number
