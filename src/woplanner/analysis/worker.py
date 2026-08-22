from __future__ import annotations

import multiprocessing as mp
import time
import traceback
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .service import LiveRecalculationResult, ModelSummary, OfflineAnalysisService


@dataclass(frozen=True)
class AnalysisRequest:
    """A complete, serializable live-analysis input captured at submission time."""

    input_version: int
    year: int
    round_number: int
    snapshot: Any
    options: tuple[tuple[str, Any], ...]
    ipc_started_at: float = 0.0


@dataclass(frozen=True)
class LiveAnalysisInput:
    """Only the immutable live data consumed by Monte Carlo input preparation."""

    drivers: tuple[Any, ...]
    completed_laps: tuple[Any, ...]
    completed_sectors: tuple[Any, ...]
    telemetry_samples: tuple[Any, ...]
    traffic_rows: tuple[dict[str, Any], ...] = ()


class AnalysisQueuePolicy(Enum):
    LATEST_WINS = "latest_wins"
    FIFO_ALL = "fifo_all"


@dataclass(frozen=True)
class ModelSnapshot:
    """An effectively immutable completed model publication."""

    model_version: int
    result: LiveRecalculationResult
    summary: ModelSummary | None
    ephemeral_result: Any = None
    parameter_rows: tuple[dict[str, object], ...] = ()
    run_rows: tuple[dict[str, object], ...] = ()
    ipc_elapsed_seconds: float = 0.0


_PROCESS_SERVICE: OfflineAnalysisService | None = None


def _initialize_worker(data_root: str | None) -> None:
    global _PROCESS_SERVICE
    _PROCESS_SERVICE = OfflineAnalysisService(data_root)


def _run_request(request: AnalysisRequest) -> ModelSnapshot:
    if _PROCESS_SERVICE is None:  # pragma: no cover - protects direct misuse
        raise RuntimeError("Analysis worker was not initialized")
    request_options = dict(request.options)
    result = _PROCESS_SERVICE.recalculate_from_live_snapshot(
        year=request.year,
        round_number=request.round_number,
        snapshot=request.snapshot,
        **request_options,
    )
    summary = None
    ephemeral_result = None
    parameter_rows: tuple[dict[str, object], ...] = ()
    run_rows: tuple[dict[str, object], ...] = ()
    if result.status == "updated":
        summary = _PROCESS_SERVICE.load_model(request.year, request.round_number)
        if not bool(request_options.get("persist", True)):
            ephemeral_result = _PROCESS_SERVICE._ephemeral_result
            parameter_rows = tuple(_PROCESS_SERVICE._ephemeral_parameter_rows)
            run_rows = tuple(_PROCESS_SERVICE._ephemeral_run_rows)
    return ModelSnapshot(
        request.input_version,
        result,
        summary,
        ephemeral_result,
        parameter_rows,
        run_rows,
        max(time.perf_counter() - request.ipc_started_at, 0.0),
    )


class LiveAnalysisProcessService:
    """Persistent process lane with an explicit live or replay queue policy."""

    def __init__(
        self,
        data_root: str | Path | None = None,
        *,
        policy: AnalysisQueuePolicy = AnalysisQueuePolicy.LATEST_WINS,
    ) -> None:
        self._data_root = None if data_root is None else str(Path(data_root))
        self.policy = AnalysisQueuePolicy(policy)
        self._executor: ProcessPoolExecutor | None = None
        self._future: Future[ModelSnapshot] | None = None
        self._running_request: AnalysisRequest | None = None
        self._pending_requests: deque[AnalysisRequest] = deque()
        self._next_version = 1
        self.current_result: ModelSnapshot | None = None
        self.current_result_version: int | None = None
        self.last_success_time: datetime | None = None
        self.last_error: str | None = None
        self.completed_version: int | None = None
        self._closed = False

    @property
    def analysis_running(self) -> bool:
        return self._future is not None

    @property
    def running_input_version(self) -> int | None:
        return (
            self._running_request.input_version
            if self._running_request is not None
            else None
        )

    @property
    def pending_input_version(self) -> int | None:
        return self.newest_queued_version

    @property
    def queue_depth(self) -> int:
        return len(self._pending_requests)

    @property
    def oldest_queued_version(self) -> int | None:
        return self._pending_requests[0].input_version if self._pending_requests else None

    @property
    def newest_queued_version(self) -> int | None:
        return self._pending_requests[-1].input_version if self._pending_requests else None

    def submit(
        self,
        *,
        year: int,
        round_number: int,
        snapshot: Any,
        options: Mapping[str, Any],
    ) -> int:
        if self._closed:
            raise RuntimeError("Analysis process service is closed")
        version = self._next_version
        self._next_version += 1
        request = AnalysisRequest(
            version,
            int(year),
            int(round_number),
            _serializable_live_snapshot(
                snapshot,
                include_telemetry=not bool(options.get("use_sector_boundary_traffic", False)),
            ),
            tuple(dict(options).items()),
        )
        if self.analysis_running:
            if self.policy is AnalysisQueuePolicy.LATEST_WINS:
                self._pending_requests.clear()
            # FIFO_ALL is the bounded replay pipeline's lossless lane. Upstream
            # backpressure limits its depth; requests are never coalesced/dropped.
            self._pending_requests.append(request)
        else:
            self._start(request)
        return version

    def _start(self, request: AnalysisRequest) -> None:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=1,
                mp_context=mp.get_context("spawn"),
                initializer=_initialize_worker,
                initargs=(self._data_root,),
            )
        request = replace(request, ipc_started_at=time.perf_counter())
        self._running_request = request
        self._future = self._executor.submit(_run_request, request)

    def poll(self) -> ModelSnapshot | None:
        if self._future is None or not self._future.done():
            return None
        future = self._future
        finished_version = self.running_input_version
        self._future = None
        self._running_request = None
        completed: ModelSnapshot | None = None
        try:
            candidate = future.result()
        except Exception as exc:
            self.last_error = "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip()
        else:
            # A delayed older result can never replace a newer publication.
            if (
                candidate.result.status == "updated"
                and (
                    self.current_result_version is None
                    or candidate.model_version > self.current_result_version
                )
            ):
                self.current_result = candidate
                self.current_result_version = candidate.model_version
                self.last_success_time = datetime.now(timezone.utc)
            self.last_error = None
            completed = candidate
        self.completed_version = finished_version
        if self._pending_requests and not self._closed:
            self._start(self._pending_requests.popleft())
        return completed

    def shutdown(self, *, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_requests.clear()
        if self._executor is None:
            return
        processes = tuple(getattr(self._executor, "_processes", {}).values())
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if not wait:
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=2.0)
        self._future = None
        self._running_request = None


def _serializable_live_snapshot(
    snapshot: Any, *, include_telemetry: bool = True
) -> Any:
    """Build the minimum pickle-safe, immutable input used by live MC preparation."""
    if not hasattr(snapshot, "completed_laps"):
        return snapshot
    return LiveAnalysisInput(
        drivers=tuple(snapshot.drivers),
        completed_laps=tuple(snapshot.completed_laps),
        completed_sectors=tuple(snapshot.completed_sectors),
        telemetry_samples=tuple(snapshot.telemetry_samples) if include_telemetry else (),
        traffic_rows=tuple(getattr(snapshot, "traffic_rows", ())),
    )
