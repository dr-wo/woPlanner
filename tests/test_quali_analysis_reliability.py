"""Offline FP-loading failures must retain useful lap data and diagnostics."""
from pathlib import Path
from types import SimpleNamespace
import json
import socket

import fastf1
from fastf1.exceptions import DataNotLoadedError
import pandas as pd
import pytest

import woplanner.quali.analysis as analysis_module
from woplanner.quali.analysis import QualiAnalysisService
from woplanner.quali.runtime_paths import RuntimePaths
import wostrategy.core.session as session_module


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))


@pytest.fixture
def analysis(tmp_path):
    resources, output = tmp_path / "resources", tmp_path / "output"
    resources.mkdir()
    (resources / "keep.txt").write_text("read-only input")
    return QualiAnalysisService(runtime_paths=RuntimePaths(resources, output, True))


def practice_laps():
    rows = []
    cursor = 0
    for number, (role, seconds) in enumerate((("OUT", 110), ("PUSH", 90), ("IN", 100)), 1):
        start = pd.Timedelta(seconds=cursor)
        rows.append(dict(Driver="AAA", LapNumber=number, SessionName="FP1",
            LapTime=pd.Timedelta(seconds=seconds), LapStartTime=start,
            Time=start + pd.Timedelta(seconds=seconds), Compound="SOFT",
            Sector1Time=pd.Timedelta(seconds=30), Sector2Time=pd.Timedelta(seconds=30),
            PitOutTime=start + pd.Timedelta(seconds=10) if role == "OUT" else pd.NaT,
            PitInTime=start + pd.Timedelta(seconds=seconds-8) if role == "IN" else pd.NaT))
        cursor += seconds
    return pd.DataFrame(rows)


def loaded_session(error=None):
    class MatchingLaps(pd.DataFrame):
        @property
        def _constructor(self):
            return MatchingLaps

        def iterlaps(self):
            def telemetry():
                if error is not None:
                    raise error
                return pd.DataFrame({"Distance": [0., 5., 100.], "Time": pd.to_timedelta([0, 4, 90], unit="s")})
            yield 0, SimpleNamespace(get_telemetry=telemetry)
    frame = practice_laps()
    return SimpleNamespace(laps=frame, data=SimpleNamespace(laps=MatchingLaps(frame)))


def sessions(monkeypatch, outcomes):
    def construct(year, round_number, name):
        assert (year, round_number) == (2026, 18)
        outcome = outcomes.get(name, FileNotFoundError("session unavailable"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(session_module, "Session", construct)
    monkeypatch.setattr(fastf1.Cache, "enable_cache", lambda *_: None)
    monkeypatch.setattr(fastf1.Cache, "configure", lambda **_: None, raising=False)


def persisted(analysis):
    assert analysis.diagnostic_path.is_file()
    return json.loads(analysis.diagnostic_path.read_text())


def test_fresh_cache_is_writable_before_fastf1_enable_and_resource_is_untouched(analysis, monkeypatch):
    enable = []
    def check_cache(path):
        cache = Path(path)
        assert cache.is_dir()
        assert list(cache.iterdir()) == []  # Probe has been closed and removed.
        enable.append(cache)
    monkeypatch.setattr(fastf1.Cache, "enable_cache", check_cache)
    monkeypatch.setattr(fastf1.Cache, "configure", lambda **kw: check_cache(kw["cache_dir"]), raising=False)
    monkeypatch.setattr(session_module, "Session", lambda *args: loaded_session())
    before = {p.name: p.read_bytes() for p in analysis.runtime_paths.resource_root.iterdir()}
    result = analysis.analyze(2026, 18)
    assert len(result.laps) == 9
    assert enable == [analysis.data_root / "fastf1/raw_cache/schema_v1"]
    assert {p.name: p.read_bytes() for p in analysis.runtime_paths.resource_root.iterdir()} == before
    assert not (analysis.runtime_paths.resource_root / "fastf1").exists()
    data = persisted(analysis)
    assert data["status"] == "success" and data["phase"] == "complete"
    assert all(v["status"] == "success" for v in data["sessions"].values())


def test_unwritable_cache_probe_fails_before_fastf1_enable_with_original_exception(analysis, monkeypatch):
    error = PermissionError("controlled read-only cache")
    def denied(**kwargs):
        raise error
    monkeypatch.setattr(analysis_module.tempfile, "TemporaryFile", denied)
    monkeypatch.setattr(fastf1.Cache, "enable_cache", lambda *_: pytest.fail("cache enabled before write check"))
    monkeypatch.setattr(fastf1.Cache, "configure", lambda **_: pytest.fail("cache enabled before write check"), raising=False)
    with pytest.raises(PermissionError) as raised:
        analysis.analyze(2026, 18)
    assert raised.value is error
    data = persisted(analysis)
    assert data["phase"] == "cache_setup" and data["error"]["type"] == "PermissionError"
    assert "controlled read-only cache" in data["error"]["traceback"]
    assert data["roots"]["cache"].startswith(str(analysis.data_root))
    assert analysis.result is None


def test_existing_cache_data_is_preserved(analysis, monkeypatch):
    cache = analysis.data_root / "fastf1/raw_cache/schema_v1"
    cache.mkdir(parents=True)
    existing = cache / "existing-data.bin"
    existing.write_bytes(b"do not touch")
    sessions(monkeypatch, {"FP1": loaded_session()})
    analysis.analyze(2026, 18)
    assert existing.read_bytes() == b"do not touch"
    assert list(cache.iterdir()) == [existing]


def test_optional_telemetry_absence_retains_laps_sector_reference_and_warning(analysis, monkeypatch, caplog):
    sessions(monkeypatch, {"FP1": loaded_session(DataNotLoadedError("no position/car data"))})
    result = analysis.analyze(2026, 18)
    reference = result.laps.loc[result.laps.is_driver_fp_reference].iloc[0]
    assert len(result.laps) == 3
    assert reference.fp_start_to_s1_s == 30 and reference.fp_s1_to_s2_s == 30
    assert pd.isna(reference.fp_start_to_5pct_s)
    data = persisted(analysis)
    warning = data["warnings"][0]
    assert warning["session"] == "FP1" and warning["driver"] == "AAA" and warning["lap_number"] == 2
    assert warning["phase"] == "reference_telemetry" and warning["error"]["type"] == "DataNotLoadedError"
    assert data["status"] == "success" and "optional reference telemetry" in caplog.text
    assert (analysis.data_root / "wostrategy/pre_quali/schema_v1/year=2026/round=18/lap_roles.csv").is_file()


def test_optional_telemetry_result_restores_live_prerequisites_through_saved_artifacts(analysis, monkeypatch):
    from test_quali_service import driver, raw
    from woplanner.quali.models import FinishedReason
    from woplanner.quali.service import QualiLiveService

    live = QualiLiveService(runtime_paths=analysis.runtime_paths)
    live.reload_operational_artifacts(2026, 18)
    assert not live.readiness_diagnostics()["pace_reference_available"]
    assert not live.readiness_diagnostics()["cool_duration_available"]
    live.consume(raw([driver(1, None, laps=0)], remaining=50.))

    # These are explicit controlled FP laps, not historical R18 measurements.
    sessions(monkeypatch, {"FP1": loaded_session(DataNotLoadedError("no position/car data"))})
    analysis.analyze(2026, 18)
    live.reload_operational_artifacts(2026, 18)
    readiness = live.readiness_diagnostics()
    assert readiness["pace_reference_available"] and readiness["cool_duration_available"]
    assert live._common_lap_state_reference.start_to_5pct_s is None
    assert live._common_lap_state_reference.start_to_s1_s == 30.
    assert live.cool_lap_s == pytest.approx(90. * 1.30)

    snapshot = live.consume(raw([driver(1, 90., laps=1)], remaining=50.))
    assert snapshot.drivers[0].lap_state == "FAST"
    assert snapshot.drivers[0].effective_finished
    assert snapshot.drivers[0].finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
    assert snapshot.track_evolution_result["readiness"]["pace_reference_available"]


def test_unexpected_telemetry_error_is_not_swallowed(analysis, monkeypatch):
    error = RuntimeError("unexpected telemetry defect")
    sessions(monkeypatch, {"FP1": loaded_session(error)})
    with pytest.raises(RuntimeError) as raised:
        analysis.analyze(2026, 18)
    assert raised.value is error
    data = persisted(analysis)
    assert data["phase"] == "reference_enrichment" and data["session"] == "FP1"
    assert data["error"]["message"] == "unexpected telemetry defect"
    assert data["sessions"]["FP1"]["status"] == "success"
    assert not data["warnings"] and analysis.result is None


def test_all_failures_keep_original_aggregate_and_individual_tracebacks(analysis, monkeypatch, caplog):
    sessions(monkeypatch, {"FP1": FileNotFoundError("missing FP1"), "FP2": ValueError("bad FP2"),
                           "FP3": DataNotLoadedError("empty FP3")})
    expected = "Unable to load FP1/FP2/FP3: FP1: missing FP1; FP2: bad FP2; FP3: empty FP3"
    with pytest.raises(RuntimeError, match="Unable to load FP1/FP2/FP3") as raised:
        analysis.analyze(2026, 18)
    assert str(raised.value) == expected
    data = persisted(analysis)
    assert data["event_id"] == "2026-R18" and data["year"] == 2026 and data["round"] == 18
    assert data["phase"] == "practice_loading" and data["status"] == "failed"
    assert [v["error"]["type"] for v in data["sessions"].values()] == ["FileNotFoundError", "ValueError", "DataNotLoadedError"]
    assert all("construct" in v["error"]["traceback"] for v in data["sessions"].values())
    summary = analysis.failure_message(raised.value)
    assert expected in summary and "2026-R18" in summary and "practice_loading" in summary
    assert str(analysis.diagnostic_path) in summary
    assert "FP analysis failed for 2026-R18" in caplog.text and "phase=practice_loading" in caplog.text
    assert any(record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records)


def test_partial_success_records_skipped_sessions_and_real_reference(analysis, monkeypatch):
    sessions(monkeypatch, {"FP2": loaded_session()})
    result = analysis.analyze(2026, 18)
    data = persisted(analysis)
    assert data["sessions"]["FP2"] == {"status": "success", "lap_count": 3}
    assert data["sessions"]["FP1"]["status"] == data["sessions"]["FP3"]["status"] == "failed"
    assert result.laps.session.tolist() == ["FP2"] * 3
    assert result.laps.loc[result.laps.is_driver_fp_reference, "fp_start_to_5pct_s"].iloc[0] == 4


def test_each_attempt_has_unique_diagnostics_and_resets_state(analysis, monkeypatch):
    sessions(monkeypatch, {})
    with pytest.raises(RuntimeError):
        analysis.analyze(2026, 18)
    first = analysis.diagnostic_path
    before = first.read_bytes()
    sessions(monkeypatch, {"FP1": loaded_session()})
    analysis.analyze(2026, 18)
    assert analysis.diagnostic_path != first and first.read_bytes() == before
    assert analysis.last_diagnostics["status"] == "success" and "error" not in analysis.last_diagnostics
    assert analysis.last_diagnostics["warnings"] == []


@pytest.mark.parametrize("analysis_fails", [False, True])
def test_diagnostic_write_failure_does_not_replace_failure_or_success(analysis, monkeypatch, caplog, analysis_fails):
    original = Path.open
    def deny_diagnostics(path, *args, **kwargs):
        if "quali_analysis_diagnostics" in path.parts:
            raise PermissionError("diagnostic location denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", deny_diagnostics)
    sessions(monkeypatch, {} if analysis_fails else {"FP1": loaded_session()})
    if analysis_fails:
        with pytest.raises(RuntimeError, match="Unable to load FP1/FP2/FP3"):
            analysis.analyze(2026, 18)
        assert analysis.last_diagnostics["error"]["type"] == "RuntimeError"
    else:
        assert analysis.analyze(2026, 18) is analysis.result
        assert analysis.last_diagnostics["status"] == "success"
    assert analysis.diagnostic_path is None
    assert analysis.last_diagnostics["persistence_error"]["type"] == "PermissionError"
    assert "Unable to save FP analysis diagnostics" in caplog.text


def test_unsafe_output_is_rejected_without_diagnostic_or_cache_write(analysis, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    analysis.data_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        analysis.analyze(2026, 18)
    assert list(outside.iterdir()) == [] and analysis.diagnostic_path is None
    assert analysis.last_diagnostics["phase"] == "output_validation"
    assert analysis.last_diagnostics["persistence_error"]["type"] == "OutputRootNotValidated"


def test_symlinked_cache_is_rejected_without_writing_resource_target(analysis, monkeypatch):
    cache_parent = analysis.data_root / "fastf1/raw_cache"
    cache_parent.mkdir(parents=True)
    (cache_parent / "schema_v1").symlink_to(analysis.runtime_paths.resource_root, target_is_directory=True)
    monkeypatch.setattr(fastf1.Cache, "enable_cache", lambda *_: pytest.fail("unsafe cache enabled"))
    with pytest.raises(ValueError, match="Symlink"):
        analysis.analyze(2026, 18)
    assert sorted(p.name for p in analysis.runtime_paths.resource_root.iterdir()) == ["keep.txt"]
    assert analysis.diagnostic_path is None


def test_supplied_laps_skip_cache_and_persist_to_output_only(analysis, monkeypatch):
    monkeypatch.setattr(fastf1.Cache, "enable_cache", lambda *_: pytest.fail("supplied laps should not load FP"))
    result = analysis.analyze(2026, 18, laps=practice_laps())
    data = persisted(analysis)
    assert data["input"] == "supplied_laps" and data["roots"]["cache"] is None
    assert all(v["status"] == "not_requested" for v in data["sessions"].values())
    assert len(result.laps) == 3 and analysis.diagnostic_path.is_relative_to(analysis.data_root)
    assert not (analysis.data_root / "fastf1").exists()
