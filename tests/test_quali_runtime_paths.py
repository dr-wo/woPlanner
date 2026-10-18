"""Offline path contract tests exercising real Planner and woData call sites."""
import json
import os
import socket
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pandas as pd
import pytest
from PySide6 import QtWidgets
from wodata.artifacts import pre_quali_root, write_pre_quali_analysis, write_pit_lane_geometry
from wodata.live import LiveConfig, ReplayConfig, QualiRawSnapshot
from woplanner.quali.analysis import QualiAnalysisService
from woplanner.quali.gui import QualiWindow, WORKSPACE_DATA_ROOT
from woplanner.quali.runtime_paths import ArtifactError, resolve_runtime_paths
from woplanner.quali.service import QualiLiveService


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("WOPLANNER_QUALI_RESOURCE_ROOT", raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))


@pytest.fixture
def roots(tmp_path, monkeypatch):
    resource, output = tmp_path / "resources", tmp_path / "outputs"
    resource.mkdir()
    monkeypatch.setenv("WOPLANNER_QUALI_RESOURCE_ROOT", str(resource))
    monkeypatch.setenv("WODATA_ROOT", str(output))
    return resolve_runtime_paths()


def saved(root, value=120., year=2026, round_number=17):
    return write_pre_quali_analysis(
        pd.DataFrame([{"driver": "AAA", "raw_lap_time_s": 90.}]),
        pd.DataFrame([{"category": "out_lap_s", "selected_count": 3, "selected_default_s": value}]),
        pd.DataFrame([{"rate_s_per_field_push": 0.01, "sample_count": 3}]),
        year=year, round_number=round_number, push_threshold_fraction=0.02, data_root=root,
    )


def identity(round_number=17):
    return replace(QualiRawSnapshot.empty(), session_info={
        "Meeting": {"Name": "Test Grand Prix", "Number": round_number},
        "Name": "Qualifying", "StartDate": "2026-10-18T15:00:00Z",
    })


def inventory(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_legacy_gui_analysis_service_paths_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("WODATA_ROOT", str(tmp_path / "unused"))
    legacy = tmp_path / "legacy"
    paths = resolve_runtime_paths(legacy)
    assert not paths.isolated and paths.resource_root == paths.output_root == legacy
    assert QualiAnalysisService(legacy).data_root == legacy
    assert QualiAnalysisService().data_root is None
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    assert window.analysis_service.data_root == (WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None)
    configs = []
    monkeypatch.setattr(window.service.controller, "start_live", configs.append)
    window._connect()
    assert configs[0].data_root == WORKSPACE_DATA_ROOT
    window.close(); app.processEvents()


def test_resource_only_and_complete_output_precedence(roots):
    saved(roots.resource_root, 120)
    analysis = QualiAnalysisService(runtime_paths=roots)
    assert analysis.load(2026, 17).planning_defaults["out_lap_s"] == 120
    assert analysis.loaded_artifact_root == roots.resource_root
    saved(roots.output_root, 130)
    assert analysis.load(2026, 17).planning_defaults["out_lap_s"] == 130
    assert analysis.loaded_artifact_root == roots.output_root


@pytest.mark.parametrize("damage", ["partial", "manifest", "identity", "count", "csv"])
def test_bad_output_is_explicit_in_analysis_and_service(roots, damage):
    saved(roots.resource_root)
    dest = saved(roots.output_root)
    if damage == "partial": (dest / "track_progression.csv").unlink()
    elif damage == "manifest": (dest / "manifest.json").write_text("not-json")
    elif damage in {"identity", "count"}:
        p = dest / "manifest.json"; m = json.loads(p.read_text())
        m["round" if damage == "identity" else "lap_count"] = 999
        p.write_text(json.dumps(m))
    else: (dest / "duration_summary.csv").write_text("wrong\n1\n")
    with pytest.raises(ArtifactError, match="Invalid FP"):
        QualiAnalysisService(runtime_paths=roots).load(2026, 17)
    service = QualiLiveService(runtime_paths=roots)
    with pytest.raises(ArtifactError): service._consume_message_snapshot(identity())
    assert service.operational_artifact_root is None
    assert service.operational_artifact_error


def test_absent_optional_fp_and_geometry(roots):
    with pytest.raises(FileNotFoundError, match="No saved FP"):
        QualiAnalysisService(runtime_paths=roots).load(2026, 17)
    service = QualiLiveService(runtime_paths=roots)
    service.configure_event_geometry(2026, 17)
    service._consume_message_snapshot(identity())
    assert service.controller.store.pit_lane_geometry is None
    assert service.operational_timings.out_lap_s == 100
    assert "No saved FP" in service.operational_artifact_error


def test_real_analysis_writes_only_output_and_survives_identity_reload(roots):
    saved(roots.resource_root, 120)
    before = inventory(roots.resource_root)
    rows = []
    cursor = 0
    for number, (role, duration) in enumerate((("OUT", 110), ("PUSH", 90), ("IN", 100)), 1):
        start = pd.Timedelta(seconds=cursor)
        rows.append(dict(SessionName="FP1", Driver="AAA", LapNumber=number,
            LapTime=pd.Timedelta(seconds=duration), LapStartTime=start,
            Time=start + pd.Timedelta(seconds=duration), Compound="SOFT",
            PitOutTime=start + pd.Timedelta(seconds=10) if role == "OUT" else pd.NaT,
            PitInTime=start + pd.Timedelta(seconds=duration-8) if role == "IN" else pd.NaT))
        cursor += duration
    analysis = QualiAnalysisService(runtime_paths=roots)
    result = analysis.analyze(2026, 17, laps=pd.DataFrame(rows))
    assert inventory(roots.resource_root) == before
    expected = result.planning_defaults["out_lap_s"]
    assert expected != 120
    service = QualiLiveService(runtime_paths=roots)
    service.load_operational_analysis(result)
    service._consume_message_snapshot(identity())
    assert service.operational_timings.out_lap_s == expected
    assert service.operational_artifact_root == roots.output_root
    service._consume_message_snapshot(identity(18))
    service._consume_message_snapshot(identity())
    assert service.operational_timings.out_lap_s == expected
    service.set_manual_operational_timing("out_lap_s", 144)
    service._consume_message_snapshot(identity(18))
    service._consume_message_snapshot(identity())
    assert service.operational_timings.out_lap_s == 144
    assert inventory(roots.resource_root) == before


@pytest.mark.parametrize("from_output", [False, True])
def test_geometry_capture_replay_and_transitions(roots, tmp_path, monkeypatch, from_output):
    saved(roots.resource_root)
    if from_output:
        saved(roots.output_root, 130)
    selected = roots.output_root if from_output else roots.resource_root
    write_pit_lane_geometry(((0, 0), (10, 10)), year=2026, round_number=17, data_root=roots.resource_root)
    before = inventory(roots.resource_root)
    service = QualiLiveService(runtime_paths=roots)
    starts = []
    monkeypatch.setattr(service.controller, "_start_source", lambda source, **kw: starts.append(source))
    replay = tmp_path / "selected.jsonl"; replay.write_text("")
    service.configure_event_geometry(2026, 17)
    service.start_replay(ReplayConfig(replay))
    assert service.controller._source is None  # Source start is intercepted, no network/thread.
    assert starts[-1].path == replay
    service.start_live(LiveConfig(2026, 17, "Q", data_root=tmp_path / "wrong"))
    assert service.controller._recorder.data_root == roots.output_root
    assert service._geometry_data_root == roots.resource_root
    assert service.controller.store.pit_lane_geometry is not None
    service._consume_message_snapshot(identity())
    assert service.operational_artifact_root == selected
    service.stop()
    service.start_replay(ReplayConfig(replay))
    assert service.controller._recorder is None
    assert starts[-1].path == replay
    service._consume_message_snapshot(identity())
    assert service._geometry_data_root == roots.resource_root
    assert service.operational_artifact_root == selected
    assert inventory(roots.resource_root) == before


def test_gui_passes_one_shared_configuration(roots, tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    assert window.runtime_paths is window.service.runtime_paths is window.analysis_service.runtime_paths
    assert window.analysis_service.data_root == roots.output_root
    configs = []
    monkeypatch.setattr(window.service.controller, "start_live", configs.append)
    window.year.setValue(2026); window.round.setValue(17)
    window._connect()
    assert configs[-1].data_root == roots.output_root
    replay = tmp_path / "recording.jsonl"; replay.write_text("")
    monkeypatch.setattr(window.service.controller, "start_replay", configs.append)
    window.replay_path.setText(str(replay)); window._start_replay()
    assert configs[-1].path == replay
    assert window.service._geometry_data_root == roots.resource_root
    window.close(); app.processEvents()


@pytest.mark.parametrize("case", ["no-output", "missing-resource", "relative", "overlap", "reverse-overlap", "source-output", "shared-output", "release-output", "source-resource", "output-file"])
def test_rejected_root_configurations(roots, monkeypatch, tmp_path, case):
    if case == "no-output": monkeypatch.delenv("WODATA_ROOT")
    elif case == "missing-resource": monkeypatch.setenv("WOPLANNER_QUALI_RESOURCE_ROOT", str(tmp_path / "missing"))
    elif case == "relative": monkeypatch.setenv("WODATA_ROOT", "relative")
    elif case == "overlap": monkeypatch.setenv("WODATA_ROOT", str(roots.resource_root / "out"))
    elif case == "reverse-overlap": monkeypatch.setenv("WODATA_ROOT", str(roots.resource_root.parent))
    elif case == "source-output": monkeypatch.setenv("WODATA_ROOT", str(Path(__file__).resolve().parents[1] / "output"))
    elif case == "source-resource": monkeypatch.setenv("WOPLANNER_QUALI_RESOURCE_ROOT", str(Path(__file__).resolve().parents[1]))
    elif case == "shared-output":
        import wodata, subprocess
        shared = subprocess.check_output(["git", "-C", str(Path(wodata.__file__).parents[2]), "rev-parse", "--git-common-dir"], text=True).strip()
        monkeypatch.setenv("WODATA_ROOT", str(Path(shared).parent / "output"))
    elif case == "release-output":
        import wodata
        monkeypatch.setenv("WODATA_ROOT", str(Path(wodata.__file__).parents[2] / "output"))
    elif case == "output-file": roots.output_root.write_text("file")
    with pytest.raises(ValueError): resolve_runtime_paths()


@pytest.mark.parametrize("where", ["root", "nested-output", "nested-resource", "geometry"])
def test_symlink_escape_rejected(roots, tmp_path, monkeypatch, where):
    outside = tmp_path / "outside"; outside.mkdir()
    if where == "root":
        link = tmp_path / "link"; link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("WODATA_ROOT", str(link / "out"))
        with pytest.raises(ValueError, match="Symlink"): resolve_runtime_paths()
    elif where == "nested-output":
        roots.output_root.mkdir(); (roots.output_root / "wostrategy").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="Symlink"): QualiAnalysisService(runtime_paths=roots).analyze(2026, 17, laps=pd.DataFrame())
    elif where == "nested-resource":
        (roots.resource_root / "wostrategy").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="Symlink"): QualiAnalysisService(runtime_paths=roots).load(2026, 17)
    else:
        (roots.resource_root / "wodata").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="Symlink"): QualiLiveService(runtime_paths=roots).configure_event_geometry(2026, 17)


def test_configuration_is_cwd_independent(roots, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    assert resolve_runtime_paths() == roots


@pytest.mark.parametrize("operation", ["analysis", "capture", "read"])
def test_hardlink_output_alias_rejected(roots, tmp_path, monkeypatch, operation):
    source = saved(roots.resource_root) / "lap_roles.csv"
    destination = pre_quali_root(2026, 17, roots.output_root) / "lap_roles.csv"
    destination.parent.mkdir(parents=True); os.link(source, destination)
    original = source.read_bytes()
    if operation == "analysis":
        with pytest.raises(ValueError, match="Hard-linked"):
            QualiAnalysisService(runtime_paths=roots).analyze(2026, 17, laps=pd.DataFrame())
    elif operation == "capture":
        service = QualiLiveService(runtime_paths=roots)
        with pytest.raises(ValueError, match="Hard-linked"):
            service.start_live(LiveConfig(2026, 17, "Q"))
    else:
        with pytest.raises(ValueError, match="Hard-linked"):
            QualiAnalysisService(runtime_paths=roots).load(2026, 17)
    assert source.read_bytes() == original


def test_gui_reports_corrupt_saved_output(roots, monkeypatch):
    saved(roots.resource_root)
    dest = saved(roots.output_root); (dest / "manifest.json").write_text("invalid")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow(); window.year.setValue(2026); window.round.setValue(17)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    window._show_plan()
    assert errors and "Invalid FP" in errors[-1] and window.plan_dialog is None
    monkeypatch.setattr(window.service, "poll", lambda: (_ for _ in ()).throw(ArtifactError("bad output")))
    window._poll()
    assert "bad output" in window.connection.text()
    window.close(); app.processEvents()


def test_partial_resource_cannot_mix_with_empty_output(roots):
    dest = saved(roots.resource_root); (dest / "duration_summary.csv").unlink()
    with pytest.raises(ArtifactError, match="incomplete"):
        QualiAnalysisService(runtime_paths=roots).load(2026, 17)


def test_invalid_numeric_output_is_not_replaced_with_resource(roots):
    saved(roots.resource_root)
    dest = saved(roots.output_root)
    (dest / "duration_summary.csv").write_text("category,selected_count,selected_default_s\n"
        "out_lap_s,nonsense,broken\n")
    with pytest.raises(ArtifactError): QualiAnalysisService(runtime_paths=roots).load(2026, 17)


@pytest.mark.parametrize("count", ["", "-1", "1.5", "inf"])
def test_invalid_output_sample_counts_are_explicit_artifact_errors(roots, count):
    saved(roots.resource_root)
    dest = saved(roots.output_root)
    (dest / "duration_summary.csv").write_text("category,selected_count,selected_default_s\n"
        f"out_lap_s,{count},120\n")
    with pytest.raises(ArtifactError): QualiLiveService(runtime_paths=roots)._consume_message_snapshot(identity())


def test_real_capture_recorder_writes_only_output(roots, monkeypatch):
    from datetime import datetime, timezone
    from wodata.live import LiveTimingMessage
    saved(roots.resource_root)
    before = inventory(roots.resource_root)
    service = QualiLiveService(runtime_paths=roots)
    monkeypatch.setattr(service.controller, "_start_source", lambda *args, **kwargs:None)
    service.start_live(LiveConfig(2026, 17, "Q"))
    service.controller._recorder.write(LiveTimingMessage("SessionInfo", identity().session_info,
        None, datetime.now(timezone.utc), source="synthetic_test"))
    service.stop()
    files = list(roots.output_root.rglob("*.jsonl"))
    assert len(files) == 1 and "SessionInfo" in files[0].read_text()
    assert list(roots.output_root.rglob("*.identity.json"))
    assert inventory(roots.resource_root) == before



@pytest.mark.parametrize("threshold", [0., 1., 2.])
def test_nonnegative_threshold_roundtrip_preserves_accepted_parameter_domain(roots, threshold):
    rows = pd.DataFrame([dict(SessionName="FP1", Driver="AAA", LapNumber=1,
        LapTime=pd.Timedelta(seconds=90), LapStartTime=pd.Timedelta(seconds=0),
        PitOutTime=pd.NaT, PitInTime=pd.NaT, Compound="SOFT")])
    analysis = QualiAnalysisService(runtime_paths=roots)
    expected = analysis.analyze(2026, 17, push_threshold_fraction=threshold, laps=rows)
    assert len(analysis.load(2026, 17).laps) == len(expected.laps)
    assert analysis.loaded_artifact_root == roots.output_root


@pytest.mark.parametrize("flag", [0, 1, "not-a-bool"])
def test_reference_flags_must_be_boolean_masks(roots, flag):
    saved(roots.resource_root)
    dest = saved(roots.output_root)
    (dest / "lap_roles.csv").write_text("driver,raw_lap_time_s,is_driver_fp_reference\n"
        f"AAA,90,{flag}\n")
    with pytest.raises(ArtifactError, match="boolean"):
        QualiLiveService(runtime_paths=roots)._consume_message_snapshot(identity())
