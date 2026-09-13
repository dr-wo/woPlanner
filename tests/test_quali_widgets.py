import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd

from woplanner.quali.service import QualiLiveService
from woplanner.quali.widgets import QualiPlanDialog, format_duration_words, format_time
from woplanner.quali.gui import QualiWindow
from woplanner.quali.track_evolution import TrackEvolutionSample, render_track_evolution_diagnostic


def test_format_time_renders_missing_and_non_finite_values_as_dash():
    assert format_time(None) == "—"
    assert format_time(float("nan")) == "—"
    assert format_time(float("inf")) == "—"
    assert format_time(pd.NA) == "—"


def test_format_time_formats_lap_times_and_gaps():
    assert format_time(90.1234) == "1:30.123"
    assert format_time(1.2345, gap=True) == "+1.234"


def test_latest_start_duration_uses_minutes_and_seconds():
    assert format_duration_words(230.0) == "3 min 50 s"
    assert format_duration_words(-70.5) == "-1 min 10.5 s"


def test_sprint_qualifying_plan_uses_sq_parts_and_defaults():
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None

        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live_service = QualiLiveService()
    live_service.received_session_name = "Sprint Qualifying"
    dialog = QualiPlanDialog(AnalysisService(), live_service)

    assert tuple(dialog.part_editors) == ("SQ1", "SQ2", "SQ3")
    assert [dialog.part_editors[part][0].value() for part in dialog.part_editors] == [
        12.0,
        10.0,
        8.0,
    ]
    dialog.close()
    app.processEvents()


def test_plan_cooldown_edits_update_the_effective_finish_policy():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    dialog = QualiPlanDialog(AnalysisService(), live)
    assert live.cool_lap_s == 105
    dialog.timing_inputs["cool_lap_s"][0].setValue(117)
    assert live.cool_lap_s == 117
    assert live.operational_timing_sources["cool_lap_s"] == "manual override"
    dialog._reset_timings()
    assert live.cool_lap_s == 105
    dialog.close()
    app.processEvents()


def test_real_plan_track_view_uses_snapshot_diagnostic_through_transition_and_finalise():
    from PySide6 import QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    service = window.service
    service.session_is_qualifying = True

    def add(part, index, install):
        service._track_samples.append(TrackEvolutionSample(
            part, "AAA", "Ferrari", index, 90. - index, index,
            "SOFT", 0, True, 4., .8, "ok", tyre_install_index=install,
        ))
        service.snapshot = service._build_snapshot()

    add("Q1", 1, 0)
    window._show_plan()
    dialog = window.plan_dialog
    assert dialog.track_diagnostic.diagnostic is service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic.driver_count == 0

    service.transition_to("Q2")
    add("Q2", 2, 1)
    window._poll()
    diagnostic = service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic is diagnostic
    assert (diagnostic.driver_count, diagnostic.lap_count) == (1, 2)
    assert "1 drivers / 2 laps" in dialog.track_diagnostic_summary.text()

    service.transition_to("Q3")
    add("Q3", 3, 2)
    window._poll()
    assert dialog.track_diagnostic.diagnostic is service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic.lap_count == 3
    service.finalise()
    window._poll()
    diagnostic = service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic is diagnostic
    deadline = time.monotonic() + 3
    while dialog.track_diagnostic._plot.isNull() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not dialog.track_diagnostic._plot.isNull()
    rendered = QtGui.QImage.fromData(render_track_evolution_diagnostic(diagnostic))
    assert dialog.track_diagnostic._plot.toImage().convertToFormat(QtGui.QImage.Format.Format_RGB32) == \
        rendered.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    window.close()
    app.processEvents()
