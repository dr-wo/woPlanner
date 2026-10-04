import json
import os

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtTest, QtWidgets

from wodata.event_operational import read_event_operational_inputs
from woplanner import operational_inputs
from woplanner.standard import gui
from woplanner.standard.normalize import normalize_race_laps


@pytest.fixture
def planner(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = tmp_path / "race.json"
    history = tmp_path / "history"
    payload = {
        "season": 2026, "round_number": 16, "race": {"total_laps": 57},
        "pit_loss": {
            "green": {"total": 24.2}, "sc": {"total": 16.0},
            "vsc": {"total": None}, "sc_vsc": {"total": 12.5},
        },
    }
    config.write_text(json.dumps(payload))
    windows = []
    warnings = []
    monkeypatch.setattr(gui, "load_operational_race_inputs", lambda:
                        operational_inputs.load_operational_race_inputs(config))

    def save(path, **kwargs):
        return operational_inputs.save_operational_pit_losses(
            path, **kwargs, history_data_root=history,
            event_resolver=lambda *_args, **_kwargs: {"circuit_id": "test-r16"},
        )

    monkeypatch.setattr(gui, "save_operational_pit_losses", save)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args: warnings.append(args))

    show = QtWidgets.QMainWindow.show

    def capture(window):
        windows.append(window)
        show(window)

    monkeypatch.setattr(QtWidgets.QMainWindow, "show", capture)
    monkeypatch.setattr(QtWidgets.QApplication, "exec", lambda _app: 0)

    def open_window():
        gui.main()
        return windows[-1]

    yield open_window, config, history, payload, warnings, app
    for window in windows:
        window.close()
    app.processEvents()


def enter_loss(window, value):
    field = window.vsc_pit_loss
    field.setFocus()
    field.selectAll()
    QtTest.QTest.keyClicks(field, str(value))
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)


def attach_plot(window):
    window.selected_driver = "AAA"
    window.service._last_plot_data = pd.DataFrame([
        {"driver": "AAA", "lap": 2, "sector": sector, "segment_index": index,
         "delta_s": delta, "plot_role": "actual"}
        for sector, index, delta in [("S1", 4, 1.0), ("S2", 5, 2.0)]
    ])
    window._update_pit_window()


def test_unresolved_vsc_accepts_manual_typing_and_updates_actual_window(planner):
    open_window, config, _, original, warnings, _ = planner
    window = open_window()
    attach_plot(window)
    assert window.vsc_pit_loss.text() == "Unresolved"
    assert window._pit_optional(window.vsc_pit_loss) is None
    assert not window.vsc_pit_loss.isReadOnly()
    assert len(window.pit_window.items) == 8  # GREEN and SC only.
    enter_loss(window, 13.0)
    assert window._pit_optional(window.vsc_pit_loss) == 13.0
    assert "manual" in window.vsc_pit_loss_label.text()
    assert len(window.pit_window.items) == 12
    assert window.pit_window.items[11].toPlainText() == "VSC pit window"
    _, lower = window.pit_window.items[8].getData()
    _, upper = window.pit_window.items[9].getData()
    assert (upper - lower).tolist() == pytest.approx([26.0, 26.0])
    assert json.loads(config.read_text()) == original

    # The existing strategy-stop default consumes the same effective field.
    window.service.session = normalize_race_laps(
        pd.DataFrame({"Driver": ["AAA"], "Team": ["Team A"],
                      "LapNumber": [2], "LapTime": [91.0], "TrackStatus": ["6"]}),
        year=2026, round_number=16, session_name="R",
    )
    assert window.service.pit_loss_default_for_stop(
        stop_after_lap=1, normal_pit_loss_s=window._pit_optional(window.normal_pit_loss),
        sc_pit_loss_s=window._pit_optional(window.sc_pit_loss),
        vsc_pit_loss_s=window._pit_optional(window.vsc_pit_loss),
    ) == 13.0
    window.clear_vsc_override_action.trigger()
    assert window.vsc_pit_loss.text() == "Unresolved"
    assert len(window.pit_window.items) == 8
    assert not warnings


@pytest.mark.parametrize("base", [None, 11.5])
def test_apply_reload_event_switch_and_clear_restore_underlying_vsc(planner, base):
    open_window, config, history, original, warnings, _ = planner
    original["pit_loss"]["vsc"] = (
        {"total": None} if base is None else {"pit_in_s3": 4.0, "pit_out_s1": 7.5}
    )
    config.write_text(json.dumps(original))
    window = open_window()
    enter_loss(window, 13.0)
    assert window._pit_optional(window.vsc_pit_loss) == 13.0
    assert window._vsc_is_manual
    unsaved_reload = open_window()
    assert window._pit_optional(window.vsc_pit_loss) == 13.0
    assert window._vsc_is_manual
    assert unsaved_reload._pit_optional(unsaved_reload.vsc_pit_loss) == base
    window.apply_pit_loss_button.click()
    saved = json.loads(config.read_text())
    assert saved["pit_loss"]["vsc"]["total"] == 13.0
    assert saved["pit_loss"]["vsc"]["_provenance"]["source"] == "manual"
    for state in ("green", "sc", "sc_vsc"):
        assert saved["pit_loss"][state] == original["pit_loss"][state]
    assert read_event_operational_inputs(
        season=2026, round_number=16, data_root=history,
    )["operational"]["pit_loss"]["vsc"]["total"] == 13.0
    reopened = open_window()
    assert reopened._pit_optional(reopened.vsc_pit_loss) == 13.0
    assert "manual" in reopened.vsc_pit_loss_label.text()
    reopened.round_input.setValue(17)
    assert reopened.vsc_pit_loss.text() == "Unresolved"
    reopened.round_input.setValue(16)
    assert reopened.vsc_pit_loss.value() == 13.0
    assert "manual" in reopened.vsc_pit_loss_label.text()
    # A second Apply must retain the original underlying record.
    enter_loss(reopened, 14.0)
    reopened.apply_pit_loss_button.click()
    reopened.clear_vsc_override_action.trigger()
    assert reopened._pit_optional(reopened.vsc_pit_loss) == base
    assert "manual" not in reopened.vsc_pit_loss_label.text()
    reopened.apply_pit_loss_button.click()
    assert json.loads(config.read_text())["pit_loss"] == original["pit_loss"]
    cleared_reload = open_window()
    assert cleared_reload._pit_optional(cleared_reload.vsc_pit_loss) == base
    assert "manual" not in cleared_reload.vsc_pit_loss_label.text()
    if base is None:
        assert cleared_reload.vsc_pit_loss.text() == "Unresolved"
    assert read_event_operational_inputs(
        season=2026, round_number=16, data_root=history,
    )["operational"]["pit_loss"]["vsc"]["total"] == base
    assert window.normal_pit_loss.isReadOnly() and window.sc_pit_loss.isReadOnly()
    assert window.normal_pit_loss.value() == 24.2 and window.sc_pit_loss.value() == 16.0
    assert not warnings
