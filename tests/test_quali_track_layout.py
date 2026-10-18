"""Rendered geometry regressions for the Track Correction page (no live-fit fiction)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets
from woplanner.quali.analysis import QualiAnalysisService
from woplanner.quali.service import QualiLiveService
from woplanner.quali.widgets import QualiPlanDialog


def settle(app):
    for _ in range(12):
        app.processEvents()


@pytest.fixture
def ui(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    old_palette = app.palette()
    live = QualiLiveService()
    dialog = QualiPlanDialog(QualiAnalysisService(data_root=tmp_path), live)
    dialog.tabs.setCurrentIndex(1)
    dialog.show()
    deadline = time.monotonic() + 10
    while dialog.track_diagnostic._plot.isNull():
        app.processEvents()
        assert time.monotonic() < deadline
        time.sleep(.01)
    yield app, dialog, live
    dialog.track_diagnostic.close()
    dialog.close()
    app.setPalette(old_palette)
    settle(app)


@pytest.mark.parametrize("dark", [False, True])
def test_resizing_text_scroll_and_controls(ui, dark):
    app, dialog, live = ui
    palette = app.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#242424" if dark else "#f6f6f6"))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("white" if dark else "black"))
    app.setPalette(palette)
    for width, height, details in [(1376, 832, False), (720, 600, False),
                                   (540, 480, True), (1600, 900, False), (720, 600, True)]:
        dialog.track_detail_toggle.setChecked(details)
        dialog.resize(width, height)
        settle(app)
        assert (dialog.width(), dialog.height()) == (width, height)
        scroll = dialog.track_scroll
        assert scroll.horizontalScrollBar().maximum() == 0
        for label in (dialog.track_messages, dialog.track_source, dialog.track_quality,
                      dialog.track_estimated, dialog.track_diagnostic_summary,
                      dialog.track_diagnostic._support_status):
            assert label.height() >= label.heightForWidth(label.width())
        for control in (dialog.track_override_enabled, dialog.track_override,
                        dialog.track_detail_toggle, dialog.clean_air_gap,
                        dialog.track_diagnostic._support_status):
            scroll.ensureWidgetVisible(control, 20, 20)
            settle(app)
            rect = QtCore.QRect(control.mapTo(scroll.viewport(), QtCore.QPoint()), control.size())
            assert scroll.viewport().rect().contains(rect), (width, height, control, rect)
        # Inspect actual painted white image bounds against the PNG's original ratio.
        view = dialog.track_diagnostic
        image = view.grab().toImage()
        dpr = image.devicePixelRatio()
        x = image.width() // 2
        white_rows = [y for y in range(image.height()) if image.pixelColor(x, y).name() == "#ffffff"]
        assert white_rows
        painted_height = (max(white_rows) - min(white_rows) + 1) / dpr
        assert abs(view.width() / painted_height - view._plot.width() / view._plot.height()) < .04
        assert not view.diagnostic.series
        assert "0 fitted push laps / 0 drivers" in dialog.track_quality.text()
        assert "Insufficient live support" in dialog.track_messages.text()
        assert "no supported driver fits" in view._support_status.text()


def test_override_details_and_wrapped_status_changes(ui):
    app, dialog, live = ui
    dialog.resize(540, 480)
    dialog.track_override_enabled.click()
    dialog.track_override.setValue(-.0123)
    dialog.track_detail_toggle.click()
    settle(app)
    assert dialog.track_effective.text() == "-0.0123 s / field push"
    assert dialog.track_source.text() == "Manual override"
    assert live.track_override_s_per_field_push == -.0123
    assert not dialog.track_result_detail.isHidden()
    assert '"manual_override_s_per_global_push": -0.0123' in dialog.track_result_detail.toPlainText()
    dialog.track_override_enabled.click()
    settle(app)
    assert dialog.track_source.text() == "Configured fallback"
    assert live.track_override_s_per_field_push is None
    # Longer status updates at the same width must also reserve enough height.
    dialog.track_messages.setText(dialog.track_messages.text() * 4)
    settle(app)
    assert dialog.track_messages.height() >= dialog.track_messages.heightForWidth(dialog.track_messages.width())
    dialog.tabs.setCurrentIndex(0)
    settle(app)
    assert dialog.tabs.widget(0).sizePolicy().horizontalPolicy() == QtWidgets.QSizePolicy.Policy.Preferred
    dialog.tabs.setCurrentIndex(1)
    settle(app)
    dialog.resize(720, 600)
    settle(app)
    assert dialog.width() == 720
