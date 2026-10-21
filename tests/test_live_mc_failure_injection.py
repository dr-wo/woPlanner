"""Independent race-day failure injection: user-visible errors and recovery."""
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace

import pytest
from PySide6 import QtWidgets

from woplanner.analysis.gui import StrategyPredictionMode, StrategyPredictionWindow
from woplanner.analysis.service import (
    LIVE_SECTOR_METHOD_VERSION, LiveRecalculationResult, ModelSummary, OfflineAnalysisService,
)
from woplanner.analysis.worker import LiveAnalysisProcessService, ModelSnapshot
from woplanner.live.telemetry import PreparedAnalysisResult


@pytest.fixture
def window(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = StrategyPredictionWindow(
        year=2026, round_number=17, mode=StrategyPredictionMode.HISTORICAL_WHAT_IF,
        service=OfflineAnalysisService(tmp_path), state_directory=tmp_path / 'state',
        live_operational_controls=True,
    )
    yield dialog
    dialog.close()
    app.processEvents()


def summary(identity):
    return ModelSummary(dict(
        analysis_id=identity, status='ready', as_of_leader_lap=27,
        semantics={'live_traffic_provenance': dict(
            requested_traffic_mode='automatic', effective_traffic_method='SECTOR_BOUNDARY_FALLBACK',
            traffic_method_version=LIVE_SECTOR_METHOD_VERSION,
            calculated_at='2026-10-19T12:00:00+00:00',
        )},
    ), ())


def publication(identity='new-model'):
    return ModelSnapshot(2, LiveRecalculationResult(
        'updated', 27, 100, analysis_id=identity, requested_traffic_mode='automatic',
        effective_traffic_method='SECTOR_BOUNDARY_FALLBACK', traffic_method_version=LIVE_SECTOR_METHOD_VERSION,
    ), summary(identity), prepared_leader_lap=27)


def test_temporary_publication_failure_is_visible_and_retains_old_model(window, monkeypatch):
    old = summary('previous-model')
    window._load_model(summary=old)
    window.persistence_mode = 'temporary'
    monkeypatch.setattr(window._analysis_worker, 'poll', lambda: publication())
    def fail(*args, **kwargs):
        raise OSError('injected temporary publication failure')
    monkeypatch.setattr(window.service, 'publish_temporary_worker_snapshot', fail)
    window._poll_analysis_worker()
    assert 'publication failed' in window.status_label.text().lower()
    assert 'injected temporary publication failure' in window.mc_readiness_label.text()
    assert window._current_model_summary is old
    assert 'previous-model' in window.model_traffic_label.text()
    assert getattr(window, '_mc_fresh_analysis_id', None) != 'new-model'


def test_history_publication_failure_rolls_back_visible_model(window, monkeypatch):
    old = summary('previous-model')
    window._load_model(summary=old)
    monkeypatch.setattr(window._analysis_worker, 'poll', lambda: publication())
    def fail(*args, **kwargs):
        raise OSError('injected history write failure')
    monkeypatch.setattr(window, '_record_history_point', fail)
    window._poll_analysis_worker()
    assert 'publication failed' in window.status_label.text().lower()
    assert window._current_model_summary is old
    assert 'previous-model' in window.model_traffic_label.text()
    assert 'new-model' not in window.model_traffic_label.text()


def test_prepared_input_failure_is_in_persistent_readiness_label(window):
    prepared = PreparedAnalysisResult(0, 7, 27, None, .01, error='injected malformed source packet')
    window._telemetry_build_options[7] = (27, {})
    window._telemetry_worker = SimpleNamespace(
        poll_analysis_inputs=lambda **kwargs: (prepared,),
        cancel_analysis_inputs=lambda: None,
    )
    window._poll_telemetry_inputs()
    assert 'injected malformed source packet' in window.status_label.text()
    assert 'injected malformed source packet' in window.mc_readiness_label.text()


def test_worker_submission_failure_is_visible_and_does_not_escape_timer(window, monkeypatch):
    prepared = PreparedAnalysisResult(0, 7, 27, object(), .01)
    window._telemetry_build_options[7] = (27, {})
    window._telemetry_worker = SimpleNamespace(
        poll_analysis_inputs=lambda **kwargs: (prepared,),
        cancel_analysis_inputs=lambda: None,
    )
    def fail(**kwargs):
        raise BrokenProcessPool('injected dead process')
    monkeypatch.setattr(window._analysis_worker, 'submit', fail)
    window._poll_telemetry_inputs()
    assert 'injected dead process' in window.mc_readiness_label.text()
    assert 'failed' in window.status_label.text().lower() or 'unavailable' in window.status_label.text().lower()


def test_loaded_model_is_previous_until_current_run_publishes(window):
    window._load_model(summary=summary('saved-model'))
    text = window.mc_readiness_label.text().lower()
    assert 'previous' in text or 'saved' in text
    assert getattr(window, '_mc_fresh_analysis_id', None) != 'saved-model'


def test_repeat_empty_evidence_keeps_specific_waiting_reason(window, monkeypatch):
    completed = ModelSnapshot(2, LiveRecalculationResult(
        'no-new-clean-laps', 14, 0, model_state_retained=True,
        traffic_unavailable_reason='Insufficient eligible observations: four-boundary evidence required.',
    ), None, prepared_leader_lap=14)
    monkeypatch.setattr(window._analysis_worker, 'poll', lambda: completed)
    window._poll_analysis_worker()
    assert 'Insufficient eligible observations' in window.status_label.text()
    assert 'four-boundary evidence required' in window.mc_readiness_label.text()


class Executor:
    def __init__(self, **kwargs):
        self.futures = []
        self.closed = False
    def submit(self, *args):
        future = Future()
        self.futures.append(future)
        return future
    def shutdown(self, **kwargs):
        self.closed = True


def test_broken_process_pool_is_replaced_for_next_automatic_update(monkeypatch):
    import woplanner.analysis.worker as worker_module
    created = []
    def factory(**kwargs):
        executor = Executor(**kwargs)
        created.append(executor)
        return executor
    monkeypatch.setattr(worker_module, 'ProcessPoolExecutor', factory)
    worker = LiveAnalysisProcessService()
    try:
        worker.submit(year=2026, round_number=17, snapshot={}, options={})
        created[0].futures[0].set_exception(BrokenProcessPool('injected process termination'))
        assert worker.poll() is None
        assert 'injected process termination' in worker.last_error
        worker.submit(year=2026, round_number=17, snapshot={}, options={})
        assert len(created) == 2
        assert created[0].closed
        created[1].futures[0].set_result(publication())
        assert worker.poll().result.status == 'updated'
        assert worker.last_error is None
    finally:
        worker.shutdown()


def test_next_leader_lap_restarts_failed_worker_and_queues_automatic_input(window):
    from test_live_analysis_boundary import committed_snapshot

    old_worker = window._analysis_worker
    old_worker._executor = Executor()
    old_worker.submit(year=2026, round_number=17, snapshot={}, options={})
    old_worker._executor.futures[0].set_exception(BrokenProcessPool('injected dead worker'))
    window._poll_analysis_worker()
    assert 'injected dead worker' in window.mc_readiness_label.text()
    requests = []
    def request(**kwargs):
        requests.append(kwargs)
        return 81
    window._telemetry_worker = SimpleNamespace(
        request_analysis_input=request, cancel_analysis_inputs=lambda: None,
    )
    window.auto_recalculate.setChecked(True)
    window._last_seen_leader_lap = 2
    window.consume_live_snapshot(committed_snapshot())
    assert window._analysis_worker is not old_worker
    assert old_worker._closed
    assert window._reported_analysis_error is None
    assert requests == [dict(leader_lap=3, use_sector_boundary_traffic=False)]
    assert 81 in window._telemetry_build_options
    assert 'queued' in window.mc_readiness_label.text().lower()


def test_same_event_live_reconnect_allows_recovered_laps_to_queue(window, tmp_path):
    window._last_seen_leader_lap = 27
    old_worker = window._analysis_worker
    window.configure_session_context(persistence_mode='durable', state_directory=tmp_path,
                                     auto_save_history=False)
    assert window._last_seen_leader_lap == 0
    assert window._analysis_worker is not old_worker


def test_main_routes_hidden_dialog_and_switches_event_before_observations(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import MappingProxyType
    from PySide6 import QtCore
    from wodata.live import LiveSessionSnapshot
    from woplanner.analysis import gui as analysis_gui
    from woplanner.live.gui import LivePlannerWindow
    from woplanner.live.service import LivePlannerService, LivePlannerUpdate

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    consumed = []
    class Dialog(QtWidgets.QDialog):
        session_closing = QtCore.Signal(object, object)
        traffic_readiness_changed = QtCore.Signal(str)
        def __init__(self, **kwargs):
            super().__init__(kwargs.get('parent'))
            self.year = kwargs['year']
            self.round_number = kwargs['round_number']
            self.mc_readiness_label = QtWidgets.QLabel('ready')
        def consume_live_snapshot(self, snapshot):
            consumed.append((self.round_number, snapshot))
        def set_live_feed_status(self, status):
            pass
    monkeypatch.setattr(analysis_gui, 'StrategyPredictionWindow', Dialog)
    service = LivePlannerService()
    main = LivePlannerWindow(service=service, event_config_path=tmp_path/'missing.json',
                             operational_history_data_root=tmp_path)
    monkeypatch.setattr(main, '_strategy_state_directory', lambda: tmp_path/'state')
    monkeypatch.setattr(main, '_redraw', lambda: None)
    try:
        main.round_input.setValue(16)
        main._show_strategy_prediction_window()
        hidden = main.strategy_prediction_window
        hidden.hide()
        initial = LiveSessionSnapshot.empty()
        monkeypatch.setattr(service, 'poll', lambda: LivePlannerUpdate(initial, (), True, frozenset({'timing'})))
        main._poll()
        assert consumed[-1] == (16, initial)
        main._show_strategy_prediction_window()
        assert main.strategy_prediction_window is hidden
        switched = replace(initial, session_info=MappingProxyType(dict(
            Meeting={'Name':'Japanese Grand Prix', 'Number':17}, Name='Race',
            StartDate='2026-10-11T05:00:00Z',
        )))
        service.snapshot = switched
        monkeypatch.setattr(service, 'poll', lambda: LivePlannerUpdate(switched, (), True, frozenset({'session','timing'})))
        main._poll()
        assert main.round_input.value() == 17
        assert main.strategy_prediction_window is not hidden
        assert consumed[-1] == (17, switched)
        assert not any(round_number == 16 and snapshot is switched for round_number, snapshot in consumed)
    finally:
        main.close()
        app.processEvents()


@pytest.mark.parametrize('recovered, expected', [(False, [1]), (True, [1, 2])])
def test_retirement_correction_matches_recovery_context_in_worker_and_causal_cut(recovered, expected):
    from datetime import datetime, timedelta, timezone
    import time
    from wodata.live import LiveTimingMessage
    from woplanner.live.telemetry import (
        CausalAnalysisCut, TelemetryAnalysisWorker, _CausalSnapshotBuilder,
        _analysis_store, _apply_analysis_message,
    )

    origin = datetime(2026, 10, 4, tzinfo=timezone.utc)
    messages = []
    for seq, patch in enumerate([
        dict(Position='1', NumberOfLaps=1, LastLapTime={'Value':'1:30.000'}),
        dict(Retired=True), dict(Retired=False),
        dict(NumberOfLaps=2, LastLapTime={'Value':'1:30.000'}),
    ], 1):
        messages.append(LiveTimingMessage(
            'TimingData', {'Lines':{'1':patch}}, origin+timedelta(seconds=seq*90),
            origin+timedelta(seconds=seq*90), sequence=seq, session_time_seconds=seq*90,
            historical_recovery=recovered,
        ))
    direct = _analysis_store()
    for message in messages:
        _apply_analysis_message(direct, message)
        assert direct.allow_retirement_reactivation is False
    assert [lap.lap_number for lap in direct.snapshot().completed_laps] == expected
    history = list(enumerate(messages, 1))
    builder = _CausalSnapshotBuilder()
    cut = CausalAnalysisCut(2, 360., 4, 0, origin+timedelta(seconds=360))
    assert [lap.lap_number for lap in builder.build(history, cut).completed_laps] == expected
    # Rebuilding a backward cut must consume the marker too.
    earlier = CausalAnalysisCut(1, 270., 3, 0, origin+timedelta(seconds=270))
    assert [lap.lap_number for lap in builder.build(history, earlier).completed_laps] == [1]
    worker = TelemetryAnalysisWorker()
    try:
        worker.request_analysis_input(leader_lap=2, use_sector_boundary_traffic=False)
        for message in messages:
            worker.enqueue_message(message)
        deadline = time.monotonic()+3
        prepared = ()
        while not prepared and time.monotonic() < deadline:
            prepared = worker.poll_analysis_inputs()
            time.sleep(.01)
        assert prepared and prepared[0].error is None
        assert [lap.lap_number for lap in prepared[0].analysis_input.completed_laps] == expected
    finally:
        worker.shutdown()
