from __future__ import annotations

from concurrent.futures import Future
from types import MappingProxyType

from wodata.live import LivePositionSample, LiveSessionSnapshot, LiveTelemetrySample
from woplanner.analysis.service import LiveRecalculationResult
from woplanner.analysis.worker import (
    AnalysisQueuePolicy,
    LiveAnalysisProcessService,
    ModelSnapshot,
)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = []
        self.futures = []
        self.shutdown_called = False

    def submit(self, function, request):
        future = Future()
        self.calls.append((function, request))
        self.futures.append(future)
        return future

    def shutdown(self, **_kwargs):
        self.shutdown_called = True


def completed(version: int, status: str = "updated") -> ModelSnapshot:
    return ModelSnapshot(
        version,
        LiveRecalculationResult(status, version, 10, analysis_id=f"model-{version}"),
        None,
    )


def process_service(
    policy: AnalysisQueuePolicy = AnalysisQueuePolicy.LATEST_WINS,
) -> tuple[LiveAnalysisProcessService, FakeExecutor]:
    service = LiveAnalysisProcessService(policy=policy)
    executor = FakeExecutor()
    service._executor = executor
    return service, executor


def submit(service, marker: int) -> int:
    return service.submit(
        year=2026,
        round_number=1,
        snapshot={"marker": marker},
        options={"sample_count": 100},
    )


def test_successful_update_keeps_old_snapshot_until_new_one_completes():
    service, executor = process_service()
    submit(service, 1)
    executor.futures[0].set_result(completed(1))
    assert service.poll().model_version == 1

    submit(service, 2)
    assert service.current_result_version == 1
    executor.futures[1].set_result(completed(2))
    assert service.poll().model_version == 2
    assert service.current_result_version == 2


def test_failed_update_retains_latest_successful_snapshot_and_records_error():
    service, executor = process_service()
    submit(service, 1)
    executor.futures[0].set_result(completed(1))
    service.poll()
    submit(service, 2)
    executor.futures[1].set_exception(ValueError("bad fit"))

    assert service.poll() is None
    assert service.current_result_version == 1
    assert "bad fit" in service.last_error


def test_requests_coalesce_to_one_latest_pending_input():
    service, executor = process_service()
    assert submit(service, 1) == 1
    assert submit(service, 2) == 2
    assert submit(service, 3) == 3
    assert submit(service, 4) == 4
    assert service.running_input_version == 1
    assert service.pending_input_version == 4
    assert service.queue_depth == 1

    executor.futures[0].set_result(completed(1))
    service.poll()

    assert [call[1].input_version for call in executor.calls] == [1, 4]
    assert service.running_input_version == 4
    assert service.pending_input_version is None


def test_fifo_policy_executes_every_request_in_submission_order():
    service, executor = process_service(AnalysisQueuePolicy.FIFO_ALL)
    for marker in range(1, 5):
        submit(service, marker)
    assert service.queue_depth == 3
    assert service.oldest_queued_version == 2
    assert service.newest_queued_version == 4

    for future_index, version in enumerate(range(1, 5)):
        executor.futures[future_index].set_result(completed(version))
        service.poll()

    assert [call[1].input_version for call in executor.calls] == [1, 2, 3, 4]
    assert service.completed_version == 4
    assert service.current_result_version == 4
    assert service.queue_depth == 0


def test_fifo_failure_continues_with_later_requests():
    service, executor = process_service(AnalysisQueuePolicy.FIFO_ALL)
    submit(service, 1)
    submit(service, 2)
    submit(service, 3)
    executor.futures[0].set_result(completed(1))
    service.poll()
    executor.futures[1].set_exception(ValueError("V2 failed"))
    service.poll()

    assert service.running_input_version == 3
    assert service.current_result_version == 1
    assert service.completed_version == 2
    assert "V2 failed" in service.last_error

    executor.futures[2].set_result(completed(3))
    service.poll()
    assert service.current_result_version == 3


def test_stale_result_cannot_overwrite_newer_authoritative_model():
    service, executor = process_service()
    service.current_result = completed(4)
    service.current_result_version = 4
    submit(service, 1)
    executor.futures[0].set_result(completed(1))

    service.poll()

    assert service.current_result_version == 4
    assert service.current_result.model_version == 4


def test_shutdown_cancels_pending_work_and_closes_executor():
    service, executor = process_service()
    submit(service, 1)
    submit(service, 2)

    service.shutdown(wait=False)

    assert executor.shutdown_called
    assert not service.analysis_running
    assert service.pending_input_version is None


def test_fifo_shutdown_clears_multiple_queued_requests():
    service, executor = process_service(AnalysisQueuePolicy.FIFO_ALL)
    for marker in range(1, 5):
        submit(service, marker)
    assert service.queue_depth == 3

    service.shutdown(wait=False)

    assert executor.shutdown_called
    assert not service.analysis_running
    assert service.queue_depth == 0


def test_request_contract_excludes_position_and_unneeded_session_state():
    service, executor = process_service(AnalysisQueuePolicy.FIFO_ALL)
    snapshot = LiveSessionSnapshot(
        MappingProxyType({"large": "metadata"}),
        "Started",
        (),
        (),
        MappingProxyType({"1": 1}),
        None,
        telemetry_samples=(LiveTelemetrySample("1", 10.0, 1, 250.0),),
        position_samples=(LivePositionSample("1", 10.0, 1.0, 2.0, 3.0),),
    )

    service.submit(
        year=2026,
        round_number=1,
        snapshot=snapshot,
        options={"sample_count": 100},
    )
    queued_input = executor.calls[0][1].snapshot

    assert queued_input.telemetry_samples == snapshot.telemetry_samples
    assert not hasattr(queued_input, "position_samples")
    assert not hasattr(queued_input, "session_info")


def test_sector_traffic_request_does_not_copy_raw_telemetry():
    service, executor = process_service()
    snapshot = LiveSessionSnapshot(
        MappingProxyType({}), "Started", (), (), MappingProxyType({}), None,
        telemetry_samples=(LiveTelemetrySample("1", 10.0, 1, 250.0),),
    )
    service.submit(
        year=2026,
        round_number=1,
        snapshot=snapshot,
        options={"sample_count": 100, "use_sector_boundary_traffic": True},
    )

    assert executor.calls[0][1].snapshot.telemetry_samples == ()
