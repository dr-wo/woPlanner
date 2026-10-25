"""The ordinary launch path must select the explicit race config coherently."""
import json
from types import SimpleNamespace

import pytest
from woplanner.live import gui


def test_main_passes_config_and_initialises_its_event(monkeypatch, tmp_path):
    config = tmp_path / "r18.json"
    config.write_text(json.dumps({"season": 2026, "round_number": 18,
                                  "race": {"total_laps": 56}}))
    calls = {}
    class Window:
        def __init__(self, **kwargs):
            calls.update(kwargs)
            self.year_input = SimpleNamespace(setValue=lambda value: calls.update(year=value))
            self.round_input = SimpleNamespace(setValue=lambda value: calls.update(round=value))
        def show(self):
            calls["shown"] = True
    monkeypatch.setattr(gui, "LivePlannerWindow", Window)
    monkeypatch.setattr(gui.QtWidgets, "QApplication", SimpleNamespace(
        instance=lambda: SimpleNamespace(exec=lambda: 0)))
    assert gui.main(["--event-config", str(config)]) == 0
    assert calls == {"event_config_path": config, "year": 2026, "round": 18, "shown": True}


def test_main_rejects_conflicting_event_before_opening_window(tmp_path):
    config = tmp_path / "r18.json"
    config.write_text(json.dumps({"season": 2026, "round_number": 18,
                                  "race": {"total_laps": 56}}))
    with pytest.raises(SystemExit) as raised:
        gui.main(["--event-config", str(config), "--round", "17"])
    assert raised.value.code == 2
