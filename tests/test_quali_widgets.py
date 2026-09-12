import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd

from woplanner.quali.service import QualiLiveService
from woplanner.quali.widgets import QualiPlanDialog, format_duration_words, format_time


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
