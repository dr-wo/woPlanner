"""Three D2 P2 reproductions and adjacent filesystem/cache regressions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest
from PySide6 import QtWidgets
from wodata.artifacts import write_pre_quali_analysis
from woplanner.quali.analysis import QualiAnalysisService
from woplanner.quali.gui import QualiWindow
from woplanner.quali.runtime_paths import ArtifactError, resolve_runtime_paths
from woplanner.quali.service import QualiLiveService
from test_quali_runtime_paths import roots, offline, saved

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/q17_quali_isolated.py"
SPEC = importlib.util.spec_from_file_location("d2_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.mark.parametrize("name", ["lap_roles.csv", "duration_summary.csv", "track_progression.csv"])
@pytest.mark.parametrize("contents", ["", "   \n", "123\n456\n", '\"unterminated'])
def test_corrupt_csv_is_artifact_error_in_both_consumers(roots, name, contents):
    saved(roots.resource_root, 123.019)
    service = QualiLiveService(runtime_paths=roots)
    service.reload_operational_artifacts(2026, 17)
    assert service.operational_timings.out_lap_s == 123.019
    destination = saved(roots.output_root, 140)
    (destination / name).write_text(contents)
    if name == "lap_roles.csv" and not contents.strip():
        # Reproduce the column-validation bypass independently of lap_count.
        manifest = json.loads((destination / "manifest.json").read_text())
        manifest["lap_count"] = 0
        (destination / "manifest.json").write_text(json.dumps(manifest))
    analysis = QualiAnalysisService(runtime_paths=roots)
    with pytest.raises(ArtifactError) as analysis_error:
        analysis.load(2026, 17)
    with pytest.raises(ArtifactError) as service_error:
        service.reload_operational_artifacts(2026, 17)
    assert str(analysis_error.value) == str(service_error.value)
    assert service.operational_artifact_root is None
    assert service.operational_artifact_error
    # Failed output loading must neither substitute resources nor clear timings
    # to the 100-second default, as the reviewed zero-byte reproduction did.
    assert service.operational_timings.out_lap_s == 123.019
    assert analysis.result is None


@pytest.mark.parametrize("empty_table", ["laps", "durations", "progression", "all"])
def test_header_only_tables_keep_supported_zero_observation_semantics(roots, empty_table):
    saved(roots.resource_root, 123.019)
    tables = {
        "laps": pd.DataFrame([{"driver":"AAA", "raw_lap_time_s":90.}]),
        "durations": pd.DataFrame([{"category":"out_lap_s", "selected_count":3, "selected_default_s":140.}]),
        "progression": pd.DataFrame([{"rate_s_per_field_push":0.01, "sample_count":3}]),
    }
    for name in tables:
        if empty_table in {name, "all"}: tables[name] = tables[name].iloc[:0]
    write_pre_quali_analysis(*tables.values(), year=2026, round_number=17,
        push_threshold_fraction=0.02, data_root=roots.output_root)
    analysis = QualiAnalysisService(runtime_paths=roots)
    result = analysis.load(2026, 17)
    assert len(result.laps) == len(tables["laps"])
    assert analysis.loaded_artifact_root == roots.output_root
    service = QualiLiveService(runtime_paths=roots)
    service.reload_operational_artifacts(2026, 17)
    assert service.operational_artifact_root == roots.output_root
    assert service.operational_artifact_error is None


@pytest.mark.parametrize("name", ["duration_summary.csv", "track_progression.csv"])
def test_gui_reports_truncation_without_plan_or_silent_default(roots, monkeypatch, name):
    saved(roots.resource_root, 123.019)
    destination = saved(roots.output_root)
    (destination / name).write_bytes(b"")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow(runtime_paths=roots)
    window.year.setValue(2026); window.round.setValue(17)
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    window._show_plan()
    assert errors and "Invalid FP artifacts" in errors[-1]
    assert window.plan_dialog is None
    monkeypatch.setattr(window.service, "poll", lambda: window.service.reload_operational_artifacts(2026, 17))
    window._poll()
    assert "FP artifact error" in window.connection.text()
    assert len(errors) == 2
    window.close(); app.processEvents()


def test_file_ancestor_refused_before_gui_or_live_capture(roots, tmp_path, monkeypatch):
    file = tmp_path / "existing-file"; file.write_text("original")
    monkeypatch.setenv("WODATA_ROOT", str(file / "missing" / "outputs"))
    with pytest.raises(ValueError, match="not a directory.*existing-file"):
        resolve_runtime_paths()
    # QualiWindow resolves paths before QMainWindow construction, even without QApplication.
    with pytest.raises(ValueError, match="not a directory"):
        QualiWindow()
    assert file.read_text() == "original"
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_ordinary_output_directories_remain_valid(roots, existing):
    if existing: roots.output_root.mkdir()
    assert resolve_runtime_paths().output_root == roots.output_root
    assert roots.output_root.exists() is existing  # Preflight itself creates nothing.


@pytest.mark.parametrize("kind", ["state", "cache", "output"])
def test_launcher_directory_preflight_rejects_file_ancestors(tmp_path, kind):
    file = tmp_path / "existing-file"; file.write_text("original")
    with pytest.raises(ValueError, match="not a directory"):
        launcher.directory_preflight(file / kind / "child", kind)
    assert list(tmp_path.iterdir()) == [file]


@pytest.fixture(scope="module")
def subprocess_sources(tmp_path_factory):
    """Tiny real Git repositories exercise the unchanged source checker in fresh Python."""
    base = tmp_path_factory.mktemp("d2-verified-bootstrap").resolve()
    files = {
        "wodata": {"__init__.py":"", "artifacts.py":
            "from pathlib import Path\ndef pre_quali_root(year, round_number, data_root=None):\n"
            " return Path(data_root)/'wostrategy/pre_quali/schema_v1'/f'year={year}'/f'round={round_number}'\n"
            "def read_pre_quali_analysis(**kwargs): raise FileNotFoundError('fixture has no saved FP')\n"},
        # An earliest-possible project import initialises REAL Matplotlib.
        "woplanner": {"__init__.py":"import matplotlib\n", "quali/__init__.py":"",
            "quali/runtime_paths.py": (SCRIPT.parents[1] / "src/woplanner/quali/runtime_paths.py").read_text(),
            "quali/gui.py": "import os\nos.environ['QT_QPA_PLATFORM']='offscreen'\n"
            "from PySide6 import QtCore, QtWidgets\n"
            "class QualiWindow(QtWidgets.QMainWindow):\n"
            " def __init__(self, runtime_paths=None):\n"
            "  super().__init__()\n  QtCore.QTimer.singleShot(0, QtWidgets.QApplication.instance().quit)\n"},
        "wostrategy": {"__init__.py":""},
    }
    sources = {}
    for package, name in launcher.PACKAGES.items():
        root = base / name; root.mkdir()
        (root / "pyproject.toml").write_text("# disposable verified test fixture\n")
        for relative, source in files[package].items():
            path = root / "src" / package / relative
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(source)
        def git(*args):
            return subprocess.check_output(["/usr/bin/git", "-C", str(root), *args], text=True).strip()
        git("init", "-q"); git("add", ".")
        git("-c", "user.name=D2 Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "pinned fixture")
        inventory = {rel:launcher.digest(root / rel) for rel in git("ls-files").splitlines()}
        sources[name] = {"root":str(root), "commit":git("rev-parse", "HEAD"), "files":inventory}
    resource = base / "resources"; resource.mkdir()
    resource_manifest = resource / "resource-manifest.json"
    resource_manifest.write_text(json.dumps({"resource_manifest_version":1,"files":[]}))
    manifest = {"manifest_version":1,"python_version":[3,11,15],"python_executable":sys.executable,
        "python_prefix":sys.prefix,"dependencies":{},"sources":sources,
        "resources":{"root":str(resource),"manifest_sha256":launcher.digest(resource_manifest)},
        "runtime_root":str(base / "state"),"output_root":str(base / "outputs")}
    return base, manifest


PROBE = r"""
import importlib.util, importlib.abc, sys, os, json
from pathlib import Path
script, manifest, report_path, mode = sys.argv[1:]
import socket
def refuse_network(*a,**k): raise RuntimeError('D2 cache probes forbid network access')
socket.socket.connect=refuse_network
socket.create_connection=refuse_network
report = {'fresh_matplotlib_not_loaded':'matplotlib' not in sys.modules}
base = Path(report_path).parent.resolve()
def audit(event,args):
    target = None
    if event == 'open':
        path, kind, flags = args
        if (isinstance(kind,str) and any(c in kind for c in 'wax+')) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT): target=path
    elif event in ('os.mkdir','os.remove','os.rmdir'): target=args[0]
    if isinstance(target,(str,bytes)) and os.fsdecode(target) != '/dev/null' and not Path(os.fsdecode(target)).resolve().is_relative_to(base):
        raise PermissionError('Unexpected subprocess write: '+os.fsdecode(target))
sys.addaudithook(audit)
class Observe(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'matplotlib': report.setdefault('first_MPLCONFIGDIR',os.environ.get('MPLCONFIGDIR'))
        return None
sys.meta_path.insert(0,Observe())
spec=importlib.util.spec_from_file_location('isolated_launcher',script)
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
code=launcher.main(['--manifest',manifest]+(['--check'] if mode=='check' else []))
report['exit_code']=code
report['project_modules']=[n for n in sys.modules if n.split('.')[0] in launcher.PACKAGES]
if 'matplotlib' in sys.modules:
    import matplotlib,tempfile
    report.update(cachedir=str(Path(matplotlib.get_cachedir()).resolve()),
        configdir=str(Path(matplotlib.get_configdir()).resolve()), tempdir=tempfile.gettempdir(),
        environment={k:os.environ.get(k) for k in ['MPLCONFIGDIR','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_RUNTIME_DIR','TMPDIR']})
    if 'PySide6.QtWidgets' in sys.modules:
        from PySide6.QtWidgets import QApplication
        app=QApplication.instance()
        if app: report['qt_platform']=app.platformName()
Path(report_path).write_text(json.dumps(report,indent=2))
raise SystemExit(code)
"""


def run_probe(base, manifest, mode, label):
    candidate = base / (label + "-manifest.json")
    candidate.write_text(json.dumps(manifest))
    report_file = base / (label + "-report.json")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", PROBE, str(SCRIPT),
        str(candidate), str(report_file), mode], cwd=base, capture_output=True, text=True, timeout=60)
    return result, json.loads(report_file.read_text())


@pytest.mark.parametrize("mode", ["check", "gui"])
def test_fresh_process_cache_initialized_before_first_project_import(subprocess_sources, mode, record_property):
    base, manifest = subprocess_sources
    result, report = run_probe(base, manifest, mode, "cache-" + mode)
    assert result.returncode == 0, result.stderr
    assert report["fresh_matplotlib_not_loaded"]
    env = report["environment"]; run = Path(env["MPLCONFIGDIR"]).parents[1]
    assert report["first_MPLCONFIGDIR"] == env["MPLCONFIGDIR"]
    assert Path(report["cachedir"]) == Path(env["MPLCONFIGDIR"]).resolve()
    assert Path(report["configdir"]) == Path(env["MPLCONFIGDIR"]).resolve()
    for value in env.values(): assert Path(value).is_relative_to(run)
    assert Path(report["tempdir"]) == run / "tmp"
    if mode == "gui": assert report["qt_platform"] == "offscreen"
    record_property("actual_cache_paths", json.dumps(report))


@pytest.mark.parametrize("kind", ["output", "state", "source-commit", "source-hash"])
def test_actual_launcher_check_refuses_before_gui_or_cache_import(subprocess_sources, kind):
    base, original = subprocess_sources
    manifest = json.loads(json.dumps(original))
    file = base / (kind + "-file"); file.write_text("original")
    if kind in {"output", "state"}:
        manifest["output_root" if kind == "output" else "runtime_root"] = str(file / "child")
    elif kind == "source-commit": manifest["sources"]["woPlanner"]["commit"] = "0"*40
    else: manifest["sources"]["woPlanner"]["files"]["src/woplanner/__init__.py"] = "0"*64
    result, report = run_probe(base, manifest, "check", "invalid-" + kind)
    assert result.returncode == 2 and "REFUSED" in result.stderr
    assert not report["project_modules"]
    assert "first_MPLCONFIGDIR" not in report
    if kind in {"output", "state"}: assert "not a directory" in result.stderr
    assert file.read_text() == "original"
    assert not (file / "child").exists()



def test_legacy_csv_reader_behavior_is_unchanged(roots, monkeypatch):
    destination = saved(roots.output_root)
    (destination / "duration_summary.csv").write_bytes(b"")
    monkeypatch.delenv("WOPLANNER_QUALI_RESOURCE_ROOT")
    legacy = QualiAnalysisService(roots.output_root)
    assert not legacy.runtime_paths.isolated
    assert legacy.load(2026, 17).durations.empty
