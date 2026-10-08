"""Explicit reads of finalized pre-race run inputs; no latest/default fallback."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from wodata.weekend_runs import WeekendRunStore, weekend_runs_root


@dataclass(frozen=True)
class PreRaceTyreImport:
    run_id: str
    values: dict[str, float]
    origin: dict[str, object]


def load_pre_race_tyre_import(year, round_number, run_id, data_root=None):
    """Validate the exact run again at adoption time, before any Planner write."""
    event = f"{int(year)}-{int(round_number):02d}"
    workflow = "race_preparation"
    try:
        run = WeekendRunStore.load(
            event=event, workflow=workflow, run_id=run_id, data_root=data_root
        )
        if (run.event, run.workflow, run.run_id, run.path.name) != (
            event, workflow, run_id, run_id
        ):
            raise ValueError("Pre-race run identity does not match the selected event/run.")
        index = run.artifact_metadata()["artifacts"]

        def checked_json(filename):
            raw = (run.path / filename).read_bytes()
            if hashlib.sha256(raw).hexdigest() != index[filename]["sha256"]:
                raise ValueError(f"Immutable run artifact hash mismatch: {filename}")
            return json.loads(raw)

        manifest = checked_json("manifest.json")
        if any(manifest.get(key) != value for key, value in (
            ("event", event), ("workflow", workflow), ("run_id", run_id),
            ("status", "SUCCESS"),
        )):
            raise ValueError("Import requires a successful run for the selected event.")
        for component in ("effective_tyre_prediction", "strategy_rules_compliant"):
            if manifest["outputs"].get(component) != "SUCCESS":
                raise ValueError(f"Pre-race run has no successful {component}.")
        strategy = checked_json("strategy_rules_compliant.json")
        if not isinstance(strategy, list) or not strategy:
            raise ValueError("Pre-race run has no completed strategy result.")
        # Only identity is read from this frozen config, never tyre values.
        config = checked_json("config.resolved.json")
        if (config["season"], config["round_number"], config["session"]) != (
            int(year), int(round_number), "R"
        ) or config.get("event_id", event) != event:
            raise ValueError("Pre-race run belongs to another event/session.")
        effective = checked_json("tyre_prediction.effective.json")
        if effective["reference_compound"] != "MEDIUM":
            raise ValueError("Pre-race tyre inputs must use MEDIUM as reference.")
        values, fields = {}, {}
        for compound in ("HARD", "MEDIUM", "SOFT"):
            for metric, parameter in (
                ("degradation_seconds_per_lap", f"Deg_{compound}"),
                ("performance_delta_to_medium", f"Perf_{compound}_vs_MEDIUM"),
            ):
                field = effective["compounds"][compound][metric]
                raw = field["effective_value"]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ValueError(f"Invalid effective tyre value: {parameter}")
                value = float(raw)
                if not math.isfinite(value) or not field.get("source"):
                    raise ValueError(f"Incomplete effective tyre input: {parameter}")
                if metric == "degradation_seconds_per_lap" and value < 0:
                    raise ValueError(f"Negative effective degradation: {parameter}")
                if parameter == "Perf_MEDIUM_vs_MEDIUM":
                    if value != 0.0:
                        raise ValueError("MEDIUM performance reference must be zero.")
                    continue
                values[parameter] = value
                fields[parameter] = {"value": value, "source": field["source"]}
        origin = {
            "source": "woweekend_pre_race_run", "event": event, "session": "R",
            "run_id": run_id,
            "effective_input_sha256": index["tyre_prediction.effective.json"]["sha256"],
            "fields": fields,
        }
        return PreRaceTyreImport(run_id, values, origin)
    except (KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError(f"Incomplete or invalid pre-race run {run_id}: {exc}") from exc


def list_pre_race_tyre_imports(year, round_number, data_root=None):
    """Skip incomplete directories individually; no run is implicitly selected."""
    root = (weekend_runs_root(data_root) / f"event={int(year)}-{int(round_number):02d}"
            / "workflow=race_preparation" / "runs")
    candidates = []
    for path in sorted(root.iterdir()) if root.is_dir() else ():
        if not path.is_dir():
            continue
        try:
            candidates.append(load_pre_race_tyre_import(year, round_number, path.name, data_root))
        except ValueError:
            continue
    return candidates
