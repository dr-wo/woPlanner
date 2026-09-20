"""Current-event race inputs shared with woWeekend.

The editable woWeekend race configuration is the owner. Planner mirrors each
successful save into woData's durable event-scoped operational history, but
never reads that mirror back as editable state. Immutable ``config.resolved``
run artifacts and empirical review outputs are likewise never editable sources
of operational truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from wodata.event_operational import (
    event_operational_inputs_path,
    resolve_event_operational_identity,
    write_event_operational_inputs,
)


DEFAULT_EVENT_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "woWeekend" / "configs" / "race.json"
)


@dataclass(frozen=True)
class OperationalRaceInputs:
    config_path: Path
    season: int | None
    round_number: int | None
    total_laps: int | None
    green_pit_loss: float | None
    sc_pit_loss: float | None
    vsc_pit_loss: float | None
    legacy_sc_vsc_pit_loss: float | None

    @property
    def event_id(self) -> str | None:
        if self.season is None or self.round_number is None:
            return None
        return f"{self.season:04d}-{self.round_number:02d}"


def load_operational_race_inputs(
    path: str | Path = DEFAULT_EVENT_CONFIG_PATH,
) -> OperationalRaceInputs:
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return _operational_race_inputs_from_payload(payload, source)


def _operational_race_inputs_from_payload(
    payload: object, source: Path,
) -> OperationalRaceInputs:
    if not isinstance(payload, Mapping):
        raise ValueError("Current-event race config must be a JSON object")
    pit_loss = payload.get("pit_loss")
    if not isinstance(pit_loss, Mapping):
        pit_loss = {}
    race = payload.get("race")
    if not isinstance(race, Mapping):
        race = {}
    return OperationalRaceInputs(
        config_path=source,
        season=_optional_positive_int(payload.get("season"), "season"),
        round_number=_optional_positive_int(payload.get("round_number"), "round_number"),
        total_laps=_optional_positive_int(
            race.get("total_laps", payload.get("total_laps")), "race.total_laps"
        ),
        green_pit_loss=_effective_total(pit_loss.get("green"), "pit_loss.green"),
        sc_pit_loss=_effective_total(pit_loss.get("sc"), "pit_loss.sc"),
        vsc_pit_loss=_effective_total(pit_loss.get("vsc"), "pit_loss.vsc"),
        legacy_sc_vsc_pit_loss=_effective_total(
            pit_loss.get("sc_vsc"), "pit_loss.sc_vsc"
        ),
    )


def save_operational_pit_losses(
    path: str | Path,
    *,
    green: float | None,
    sc: float | None,
    vsc: float | None,
    season: int | None = None,
    round_number: int | None = None,
    updated_at: datetime | None = None,
    event_identity: Mapping[str, object] | None = None,
    event_identity_event_key: tuple[int, int] | None = None,
    history_data_root: str | Path | None = None,
    event_resolver=None,
) -> OperationalRaceInputs:
    """Validate and update the config, then mirror accepted values to woData.

    A legacy ``sc_vsc`` record is retained untouched and is never copied into
    either separate field. Immutable run snapshots and empirical observations
    are not changed.
    """
    destination = Path(path)
    original_bytes = destination.read_bytes()
    payload = json.loads(original_bytes)
    if not isinstance(payload, dict):
        raise ValueError("Current-event race config must be a JSON object")
    stored_season = _optional_positive_int(payload.get("season"), "season")
    stored_round = _optional_positive_int(payload.get("round_number"), "round_number")
    if season is not None and stored_season not in (None, int(season)):
        raise ValueError(
            f"Config season {stored_season} does not match Planner season {int(season)}"
        )
    if round_number is not None and stored_round not in (None, int(round_number)):
        raise ValueError(
            f"Config round {stored_round} does not match Planner round {int(round_number)}"
        )
    pit_loss = payload.setdefault("pit_loss", {})
    if not isinstance(pit_loss, dict):
        raise ValueError("pit_loss must be a JSON object")
    timestamp = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_season = stored_season if stored_season is not None else season
    event_round = stored_round if stored_round is not None else round_number
    if event_season is None or event_round is None:
        raise ValueError(
            "season and round_number are required to save durable event history"
        )
    if stored_season is None and event_season is not None:
        payload["season"] = int(event_season)
    if stored_round is None and event_round is not None:
        payload["round_number"] = int(event_round)
    event_id = (
        f"{int(event_season):04d}-{int(event_round):02d}"
        if event_season is not None and event_round is not None
        else None
    )
    for state, value in (("green", green), ("sc", sc), ("vsc", vsc)):
        validated = _optional_pit_loss(value, f"pit_loss.{state}.total")
        existing = pit_loss.get(state)
        record = dict(existing) if isinstance(existing, Mapping) else {}
        record.update({"total": validated, "pit_in_s3": None, "pit_out_s1": None})
        record["_provenance"] = {
            "source": "manual",
            "updated_via": "woPlanner",
            "event_id": event_id,
            "updated_at": timestamp.isoformat(),
        }
        pit_loss[state] = record

    try:
        from woweekend.config.race import RaceConfig

        RaceConfig.parse(payload)
        saved = _operational_race_inputs_from_payload(payload, destination)
    except Exception as exc:
        raise ValueError(
            f"Prospective race.json is invalid; no changes were saved: {exc}"
        ) from exc
    resolved_identity = resolve_event_operational_identity(
        season=int(event_season),
        round_number=int(event_round),
        event_identity=event_identity,
        event_identity_event_key=event_identity_event_key,
        resolver=event_resolver,
    )
    history_path = event_operational_inputs_path(
        season=int(event_season),
        round_number=int(event_round),
        data_root=history_data_root,
    )
    prior_history_bytes = history_path.read_bytes() if history_path.exists() else None
    try:
        _atomic_write_json(destination, payload)
        saved = load_operational_race_inputs(destination)
        history_values = {
            "season": int(event_season),
            "round_number": int(event_round),
            "green_pit_loss": saved.green_pit_loss,
            "sc_pit_loss": saved.sc_pit_loss,
            "vsc_pit_loss": saved.vsc_pit_loss,
            "legacy_sc_vsc_pit_loss": saved.legacy_sc_vsc_pit_loss,
            "event_identity": resolved_identity,
            "source": str(destination),
            "updated_via": "woPlanner Apply pit losses",
            "updated_at": timestamp,
            "data_root": history_data_root,
        }
        if saved.total_laps is not None:
            history_values["total_laps"] = saved.total_laps
        write_event_operational_inputs(**history_values)
    except Exception as exc:
        rollback_errors = []
        try:
            _atomic_write_bytes(destination, original_bytes)
        except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
            rollback_errors.append(f"config restore={rollback_exc}")
        try:
            if prior_history_bytes is None:
                if history_path.exists():
                    history_path.unlink()
            else:
                _atomic_write_bytes(history_path, prior_history_bytes)
        except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
            rollback_errors.append(f"history restore={rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Applying operational inputs failed and rollback was incomplete: "
                f"failure={exc}; {'; '.join(rollback_errors)}"
            ) from exc
        raise RuntimeError(
            "Applying operational inputs failed; the current config was restored "
            f"and durable history remains at its prior accepted state: {exc}"
        ) from exc
    return saved


def _effective_total(value: Any, path: str) -> float | None:
    if not isinstance(value, Mapping):
        return None
    total = _optional_pit_loss(value.get("total"), f"{path}.total")
    pit_in = _optional_pit_loss(value.get("pit_in_s3"), f"{path}.pit_in_s3")
    pit_out = _optional_pit_loss(value.get("pit_out_s1"), f"{path}.pit_out_s1")
    if total is not None and (pit_in is not None or pit_out is not None):
        raise ValueError(f"{path} mixes total and split values")
    if (pit_in is None) != (pit_out is None):
        raise ValueError(f"{path} requires both split values")
    return total if total is not None else (
        None if pit_in is None else float(pit_in + pit_out)
    )


def _optional_pit_loss(value: Any, path: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be numeric or null") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 120.0:
        raise ValueError(f"{path} must be finite and between 0 and 120 seconds")
    return number


def _optional_positive_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be a positive integer or null") from exc
    if number <= 0:
        raise ValueError(f"{path} must be a positive integer or null")
    return number


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    serialized = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_bytes(path, serialized.encode("utf-8"))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "DEFAULT_EVENT_CONFIG_PATH",
    "OperationalRaceInputs",
    "load_operational_race_inputs",
    "save_operational_pit_losses",
]
