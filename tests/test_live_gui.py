import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from woplanner.live_service import REFERENCE_MODE_AVERAGED_PACE
from woplanner.live_gui import LivePlannerWindow


def test_live_window_constructs_and_disconnects_before_connect():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = LivePlannerWindow()
    assert window.reference_mode.currentData() == REFERENCE_MODE_AVERAGED_PACE
    window._disconnect()
    assert "Disconnected" in window.connection_status.text()
    window.close()
    app.processEvents()
