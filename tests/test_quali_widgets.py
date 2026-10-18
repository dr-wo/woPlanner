import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest

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
    assert live.cool_lap_s is None
    assert not live._manual_timing_values
    assert dialog.timing_inputs["cool_lap_s"][0].text() == "Manual required"
    dialog.timing_inputs["cool_lap_s"][0].setValue(117)
    assert live.cool_lap_s == 117
    assert live.operational_timing_sources["cool_lap_s"] == "manual override"
    dialog._reset_timings()
    assert live.cool_lap_s is None
    dialog.close()
    app.processEvents()


def test_plan_displays_rounded_value_but_calculates_with_live_precision():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    values = {
        "out_lap_s": 113.186,
        "in_lap_s": 141.7465,
        "cool_lap_s": 132.118,
        "push_lap_s": 94.865,
    }
    live._fp_timing_values.update(values)
    live._fp_timing_sources.update({key: "FP-derived (n=4)" for key in values})
    live._apply_operational_sources()
    dialog = QualiPlanDialog(AnalysisService(), live)
    spin, source = dialog.timing_inputs["cool_lap_s"]
    assert spin.value() == 132.1
    assert {key: field.value() for key, (field, _) in dialog.timing_inputs.items()} == {
        "out_lap_s": 113.2, "in_lap_s": 141.7,
        "cool_lap_s": 132.1, "push_lap_s": 94.9,
    }
    assert source.text() == "FP-derived (n=4)"
    assert all(label.text() == "FP-derived (n=4)"
               for _, label in dialog.timing_inputs.values())
    assert vars(dialog._current_timings()) == values
    spin.setValue(133.2)
    assert live.cool_lap_s == 133.2
    assert source.text() == "manual override"
    dialog.close()
    dialog = QualiPlanDialog(AnalysisService(), live)
    assert dialog._current_timings().cool_lap_s == 133.2
    assert dialog.timing_inputs["cool_lap_s"][1].text() == "manual override"
    dialog._reset_timings()
    assert vars(live.operational_timings) == values
    dialog.close()
    app.processEvents()


def test_plan_open_and_refresh_are_read_only_with_stale_cached_analysis():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    cached = SimpleNamespace(
        durations=pd.DataFrame([{
            "category": "cool_lap_s", "selected_default_s": 120.0,
            "selected_count": 2,
        }]),
        laps=pd.DataFrame(columns=["session", "driver", "lap_number"]),
    )

    class AnalysisService:
        result = cached
        def effective_fp_track_rate(self):
            return 0.0, "FP prior", 0

    live = QualiLiveService()
    live._fp_timing_values["cool_lap_s"] = 132.118
    live._fp_timing_sources["cool_lap_s"] = "FP-derived (n=3)"
    live._apply_operational_sources()
    live.set_manual_operational_timing("cool_lap_s", 133.25)
    live.set_track_override(-0.05)
    before = dict(live._manual_timing_values)
    dialog = QualiPlanDialog(AnalysisService(), live)
    dialog.load_analysis(cached)
    dialog.refresh_live_track()
    assert live.cool_lap_s == 133.25
    assert live._manual_timing_values == before
    assert live.track_override_s_per_field_push == -0.05
    assert live.track_rate_s_per_field_push == -0.05
    assert dialog.track_override_enabled.isChecked()
    assert dialog.track_override.value() == -0.05
    assert dialog.timing_inputs["cool_lap_s"][0].value() == 133.3

    dialog.timing_inputs["cool_lap_s"][0].setValue(134.5)
    assert live.cool_lap_s == 134.5
    assert live.operational_timing_sources["cool_lap_s"] == "manual override"
    dialog._reset_timings()
    assert live.cool_lap_s == 132.118
    assert not live._manual_timing_values
    assert live.track_override_s_per_field_push == -0.05
    dialog.close()
    app.processEvents()


def test_plan_fp_best_fallback_and_unavailable_share_service_policy():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    live.load_operational_analysis(SimpleNamespace(
        durations=pd.DataFrame(),
        laps=pd.DataFrame([{
            "is_driver_fp_reference": True, "raw_lap_time_s": 92.797,
            "driver": "ANT",
        }]),
    ))
    dialog = QualiPlanDialog(AnalysisService(), live)
    value, source = live._effective_cool_timing(80)
    assert value == 92.797 * 1.30
    assert dialog._current_timings().cool_lap_s == value
    assert dialog.timing_inputs["cool_lap_s"][0].value() == 120.6
    assert dialog.timing_inputs["cool_lap_s"][1].text() == source == "FP best × 1.30 fallback"
    dialog.close()

    live.load_operational_analysis(SimpleNamespace(
        durations=pd.DataFrame(), laps=pd.DataFrame(),
    ))
    dialog = QualiPlanDialog(AnalysisService(), live)
    assert live._effective_cool_timing(80)[0] is None
    assert dialog._current_timings().cool_lap_s is None
    assert dialog.timing_inputs["cool_lap_s"][0].text() == "Manual required"
    assert all("manual override required" in editor.release_summary.text()
               for _, _, editors in dialog.part_editors.values() for editor in editors)
    assert all(editor.timeline.timing is None
               for _, _, editors in dialog.part_editors.values() for editor in editors)
    dialog.close()
    app.processEvents()


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
def test_plan_dy_is_independent_per_arrangement_and_toggle_restores_timing(part):
    from PySide6 import QtWidgets
    from woplanner.quali.planning import QualiTimingValues
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    live.set_operational_timings(QualiTimingValues(100, 100, 110, 90), source="test policy")
    dialog = QualiPlanDialog(AnalysisService(), live)
    dialog._add_arrangement(part)
    editors = [editor for _, _, group in dialog.part_editors.values() for editor in group]
    before = {editor: editor.timeline.timing for editor in editors}
    assert all(editor.double_yellow.text() == "DY" for editor in editors)
    assert all(not editor.double_yellow.isChecked() for editor in editors)
    selected = dialog.part_editors[part][2][-1]
    selected.double_yellow.setChecked(True)
    assert selected.arrangement().double_yellow
    assert selected.timeline.timing.latest_start_s == before[selected].latest_start_s - 290
    assert selected.timeline.timing.double_yellow_allowance_s == 290
    assert "4 min 50 s earlier" in selected.release_summary.text()
    assert "without DY" in selected.release_summary.text()
    assert "leave with at least 7 min 0 s remaining" in selected.release_summary.text()
    assert "without DY, leave with at least 2 min 10 s remaining" in selected.release_summary.text()
    for editor in editors:
        if editor is not selected:
            assert editor.timeline.timing == before[editor]
            assert "DY off" in editor.release_summary.text()
    selected.double_yellow.setChecked(False)
    assert selected.timeline.timing == before[selected]
    assert "DY off" in selected.release_summary.text()
    dialog.close()
    app.processEvents()


def test_plan_dy_recalculates_first_prep_and_timing_edits_with_full_precision():
    from PySide6 import QtWidgets
    from woplanner.quali.planning import QualiTimingValues
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    live.set_operational_timings(
        QualiTimingValues(113.186, 141.7465, 132.118, 94.865), source="test policy",
    )
    dialog = QualiPlanDialog(AnalysisService(), live)
    editor = dialog.part_editors["Q1"][2][0]
    editor.double_yellow.setChecked(True)
    assert editor.timeline.timing.double_yellow_allowance_s == pytest.approx(349.7975)
    editor.runs[0][0].setChecked(True)
    assert editor.timeline.timing.double_yellow_allowance_s == pytest.approx(481.9155)
    assert "OUT + PREP + PUSH + IN" in editor.release_summary.text()
    editor.add_run()
    editor.runs[1][0].setChecked(True)
    editor.runs[1][1].setValue(3)
    assert editor.timeline.timing.double_yellow_allowance_s == pytest.approx(481.9155)
    dialog.timing_inputs["out_lap_s"][0].setValue(120)
    assert editor.timeline.timing.double_yellow_allowance_s == pytest.approx(488.7295)
    editor.remove_run(editor.runs[0][2])
    editor.runs[0][0].setChecked(False)
    assert editor.timeline.timing.double_yellow_allowance_s == pytest.approx(356.6115)
    dialog.close()
    app.processEvents()


def test_plan_labels_actively_used_defaults_as_fallback():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = None
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    live = QualiLiveService()
    dialog = QualiPlanDialog(AnalysisService(), live)
    assert dialog.timing_inputs["cool_lap_s"][1].text() == "unavailable — manual override required"
    assert all(source.text() == "built-in fallback"
               for key, (_, source) in dialog.timing_inputs.items() if key != "cool_lap_s")
    dialog.close()
    app.processEvents()


def test_offline_plan_keeps_artifact_precision_until_manual_edit():
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class AnalysisService:
        result = SimpleNamespace(
            planning_defaults={"cool_lap_s": 132.118},
            durations=pd.DataFrame([{
                "category": "cool_lap_s", "selected_default_s": 132.118,
                "selected_count": 4,
            }]),
            laps=pd.DataFrame(columns=["session", "driver", "lap_number"]),
        )
        def effective_fp_track_rate(self):
            return 0.0, "No usable FP estimate", 0

    dialog = QualiPlanDialog(AnalysisService())
    spin, source = dialog.timing_inputs["cool_lap_s"]
    assert spin.value() == 132.1
    assert dialog._current_timings().cool_lap_s == 132.118
    assert source.text() == "FP-derived (n=4)"
    spin.setValue(133.2)
    assert dialog._current_timings().cool_lap_s == 133.2
    assert source.text() == "manual override"
    dialog._reset_timings()
    assert dialog._current_timings().cool_lap_s == 132.118
    assert source.text() == "FP-derived (n=4)"
    dialog.close()
    app.processEvents()


def test_real_plan_track_view_uses_snapshot_diagnostic_through_transition_and_finalise():
    from PySide6 import QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    service = window.service
    service.session_is_qualifying = True

    def add(part, index, install):
        from test_quali_canonical_track_rate import seed
        seed(service, TrackEvolutionSample(
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
    assert dialog.track_estimated.text() == "-1.0000 s / field push (Live per-driver estimate)"
    assert dialog.track_effective.text() == "-1.0000 s / field push"
    assert dialog.track_source.text() == "Live per-driver estimate"
    assert "2 fitted push laps / 1 drivers" in dialog.track_quality.text()
    dialog.track_override.setValue(.25)
    dialog.track_override_enabled.setChecked(True)
    assert dialog.track_estimated.text() == "-1.0000 s / field push (Live per-driver estimate)"
    assert dialog.track_effective.text() == "+0.2500 s / field push"
    assert dialog.track_source.text() == "Manual override"
    assert "Live — limited" in dialog.track_quality.text()

    service.transition_to("Q3")
    add("Q3", 3, 2)
    window._poll()
    assert dialog.track_diagnostic.diagnostic is service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic.lap_count == 3
    assert dialog.track_effective.text() == "+0.2500 s / field push"
    assert service.snapshot.cutoff.track_rate_s_per_field_push == .25
    service.finalise()
    window._poll()
    diagnostic = service.snapshot.track_evolution_diagnostic
    assert dialog.track_diagnostic.diagnostic is diagnostic
    assert dialog.track_estimated.text() == "-1.0000 s / field push (Live per-driver estimate)"
    assert dialog.track_effective.text() == "+0.2500 s / field push"
    dialog.track_override_enabled.setChecked(False)
    assert dialog.track_effective.text() == "-1.0000 s / field push"
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
