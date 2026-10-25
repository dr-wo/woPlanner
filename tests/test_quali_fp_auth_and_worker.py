"""Authentication diagnostics are passive; FP work never blocks live polling."""
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
import requests
from PySide6 import QtCore, QtWidgets

from woplanner.quali import fp_access, fp_worker, preflight
from woplanner.quali.gui import QualiWindow


@pytest.mark.parametrize("seconds,state", [(1000, "STORED_UNEXPIRED"), (100, "EXPIRING"), (-1, "EXPIRED")])
def test_authentication_inspection_never_authenticates_or_returns_token(monkeypatch, seconds, state):
    token = SimpleNamespace(value="SENTINEL_DO_NOT_RETURN", expires_at=time.time() + seconds,
                            is_valid=lambda: seconds > 300)
    account = SimpleNamespace(token_manager=SimpleNamespace(get_stored=lambda: token))
    monkeypatch.setattr(fp_access, "import_module", lambda _: account)
    result = fp_access.authentication_status()
    assert result["state"] == state
    assert "SENTINEL" not in json.dumps(result)
    assert "Local expiry" in result["note"]


def test_response_hooks_preserve_sources_drop_secrets_and_restore_on_error():
    session = requests.Session()
    sentinel_hook = lambda response, *a, **k: response
    session.hooks["response"].append(sentinel_hook)
    cache = SimpleNamespace(_requests_session=session, _requests_session_cached=session)
    with pytest.raises(RuntimeError):
        with fp_access.observe_archive_responses(cache) as rows:
            assert len(session.hooks["response"]) == 2
            response = requests.Response()
            response.status_code = 401
            response.url = "https://livetiming.formula1.com/static/event/TimingData.jsonStream?secret=SENTINEL"
            response.request = requests.Request("GET", response.url, headers={"Authorization": "SENTINEL"}).prepare()
            session.hooks["response"][-1](response)
            session.hooks["response"][-1](response)  # CachedSession's second dispatch.
            response = requests.Response()
            response.url = "https://livetiming-mirror.fastf1.dev/static/event/TimingData.jsonStream"
            response.status_code = 404
            session.hooks["response"][-1](response)
            response.url = "https://unrelated.example/auth?password=SENTINEL"
            session.hooks["response"][-1](response)
            raise RuntimeError("controlled")
    assert session.hooks["response"] == [sentinel_hook]
    assert len(rows) == 2
    assert "SENTINEL" not in json.dumps(rows)
    assert rows[0]["authentication_attached"] is True
    result = fp_access.archive_summary(rows)
    assert result["status"] == "AUTHENTICATION_OR_ACCESS_DENIED"
    assert result["mirror_status_counts"] == {"404": 1}


@pytest.mark.parametrize("code,cached,status", [(404, False, "RESOURCE_NOT_FOUND"),
    (403, False, "ACCESS_DENIED"),
    (200, False, "FRESH_RESPONSES_RECEIVED"), (200, True, "CACHED_RESPONSES_ONLY")])
def test_cached_responses_and_missing_resources_are_not_fresh_authentication(code, cached, status):
    assert fp_access.archive_summary([dict(host="livetiming.formula1.com", path="/resource",
        status=code, from_cache=cached)])["status"] == status
    assert fp_access.archive_summary([])["status"] == "NO_ARCHIVE_RESPONSE_OBSERVED"


def test_fresh_preflight_preserves_existing_cache_and_missing_auth_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "authentication_status", lambda: {"state": "MISSING_OR_UNREADABLE"})
    result = preflight.run_preflight(year=2026, round_number=18, run_root=tmp_path, prepare=True, fresh=True)
    assert result["status"] == "AUTHENTICATION_REQUIRED"
    assert "--authenticate --method browser" in result["recovery"]
    assert not (tmp_path / "preparation").exists()
    cache = tmp_path / "preparation/fastf1/raw_cache/schema_v1"
    cache.mkdir(parents=True)
    existing = cache / "preserve"
    existing.write_bytes(b"genuine cache")
    with pytest.raises(ValueError, match="empty cache"):
        preflight.run_preflight(year=2026, round_number=18, run_root=tmp_path, prepare=True, fresh=True)
    assert existing.read_bytes() == b"genuine cache"


def test_explicit_parsed_cache_hits_are_distinct_from_fresh_access():
    logger = logging.getLogger("fastf1.req")
    with fp_access.observe_parsed_cache_usage() as hits:
        logger.warning("Using cached data for car_data")
        logger.warning("Sensitive SENTINEL is not a cache message")
    summary = fp_access.archive_summary([], hits)
    assert summary["status"] == "PARSED_CACHE_ONLY"
    assert summary["parsed_cache_hits"] == {"car_data": 1}
    assert "SENTINEL" not in json.dumps(summary)


def test_worker_stdout_protocol_survives_noisy_producer_and_is_noninteractive(monkeypatch, capsys):
    class Analysis:
        diagnostic_path = None
        last_diagnostics = {"status": "success"}
        def __init__(self, **kwargs): pass
        def analyze(self, *args):
            assert not sys.stdin.isatty()
            print("producer diagnostic noise")
    methods = []
    monkeypatch.setattr(fp_worker, "QualiAnalysisService", Analysis)
    monkeypatch.setattr(fp_worker.fastf1, "set_auth_method", methods.append)
    monkeypatch.setattr(sys, "stdin", sys.stdin)
    monkeypatch.setattr(sys, "argv", ["worker", json.dumps({"year": 2026, "round_number": 18})])
    assert fp_worker.main() == 0
    sys.stdin.close()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "producer diagnostic noise" in captured.err
    assert methods == ["password"]


def test_successful_preflight_serializes_real_timing_values_and_reports_cache_provenance(tmp_path, monkeypatch):
    from woplanner.quali import service as service_module
    from woplanner.quali.planning import QualiTimingValues
    class Analysis:
        diagnostic_path = None
        last_diagnostics = {"archive_access": {"responses": []}}
        def __init__(self, **kwargs): pass
        def analyze(self, *args): return SimpleNamespace(laps=[1, 2, 3])
        def load(self, *args): return SimpleNamespace(laps=[1, 2, 3])
    class Live:
        operational_timings = QualiTimingValues(cool_lap_s=120.)
        def __init__(self, **kwargs): pass
        def load_operational_analysis(self, result): assert len(result.laps) == 3
        def readiness_diagnostics(self): return {"pace_reference_available": True, "cool_duration_available": True}
    from woplanner.quali import analysis as analysis_module
    monkeypatch.setattr(analysis_module, "QualiAnalysisService", Analysis)
    monkeypatch.setattr(service_module, "QualiLiveService", Live)
    monkeypatch.setattr(preflight, "authentication_status", lambda: {"state": "STORED_UNEXPIRED"})
    monkeypatch.setattr(sys, "stdin", sys.stdin)
    monkeypatch.setattr(preflight.fastf1, "set_auth_method", lambda _: None)
    report = preflight.run_preflight(year=2026, round_number=18, run_root=tmp_path, prepare=True)
    sys.stdin.close()
    assert report["status"] == "FP_PREPARATION_COMPLETE"
    assert report["artifact_reload_lap_count"] == 3
    assert report["operational_timings"]["cool_lap_s"] == 120.
    assert report["fresh_authenticated_timing_sessions"] == []


@pytest.fixture
def window(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a: messages.append(a[2]))
    widget = QualiWindow()
    yield widget, app, messages
    widget.close()
    app.processEvents()


def child_code(monkeypatch, source):
    original = QtCore.QProcess.setArguments
    monkeypatch.setattr(QtCore.QProcess, "setArguments", lambda process, _: original(process, ["-c", source]))


def drain(widget, app):
    deadline = time.monotonic() + 5
    while widget._analysis_process is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert widget._analysis_process is None


def test_slow_fp_worker_keeps_gui_polling_and_avoids_duplicate_jobs(window, monkeypatch):
    widget, app, messages = window
    child_code(monkeypatch, 'import time,json; time.sleep(.3); print(json.dumps({"ok":False,"message":"Authentication required"}))')
    polls = []
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: (widget._poll(), polls.append(True)))
    timer.start(10)
    widget._analyze()
    first = widget._analysis_process
    widget._analyze()
    assert first is widget._analysis_process
    drain(widget, app)
    timer.stop()
    assert len(polls) >= 5
    assert messages == ["Authentication required"]


def test_worker_result_for_different_event_is_never_applied(window, monkeypatch):
    widget, app, messages = window
    child_code(monkeypatch, 'import time,json; time.sleep(.2); print(json.dumps({"ok":True}))')
    widget._analyze()
    widget.round.setValue(widget.round.value() + 1)
    drain(widget, app)
    assert messages == []
    assert widget.analysis_service.result is None


def test_empty_worker_failure_does_not_reuse_old_fp_diagnostics(window, monkeypatch):
    widget, app, messages = window
    widget.analysis_service.last_diagnostics = {"phase": "OLD_SENSITIVE_CONTEXT"}
    child_code(monkeypatch, 'raise SystemExit(2)')
    widget._analyze()
    drain(widget, app)
    assert "worker phase; exit 2" in messages[0]
    assert "OLD_SENSITIVE_CONTEXT" not in messages[0]


def test_timeout_stops_child_without_stopping_gui(window, monkeypatch):
    widget, app, messages = window
    child_code(monkeypatch, "import time; time.sleep(10)")
    widget._analyze()
    process = widget._analysis_process
    assert process.waitForStarted(1000)
    widget._cancel_fp_analysis()
    assert widget._analysis_process is None
    assert process.waitForFinished(1000)
    assert "15 minutes" in messages[0]
    assert widget.analyze.isEnabled()
