"""Rendering failures remain visible and recover with the same diagnostic."""

import os
import time
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from woplanner.quali import widgets
from woplanner.quali.track_evolution import build_track_evolution_diagnostic


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def png(app):
    image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor("blue"))
    buffer = QtCore.QBuffer()
    assert buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


@pytest.fixture
def view(app):
    view = widgets.TrackEvolutionDiagnosticView()
    yield view
    view.close()
    app.processEvents()


def _settle(app, view):
    deadline = time.monotonic() + 3
    while view._plot.isNull() and view._render_error is None:
        app.processEvents()
        assert time.monotonic() < deadline, "The renderer did not deliver a result or error"
        time.sleep(.001)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_worker_exception_is_visible_and_retry_button_recovers(app, view, png, monkeypatch, error_type):
    calls = []

    def render(diagnostic):
        calls.append(diagnostic)
        if len(calls) == 1:
            raise error_type("synthetic render failure")
        return png

    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", render)
    diagnostic = build_track_evolution_diagnostic((), clean_air_gap_s=3.)
    view.set_diagnostic(diagnostic)
    _settle(app, view)
    assert view._render_error == f"{error_type.__name__}: synthetic render failure"
    assert view._plot.isNull()
    assert not view.retry_button.isHidden()
    # Unsupported fitting status remains meaningful during a rendering error.
    assert "no supported driver fits" in view._support_status.text()
    assert not view._support_status.isHidden()

    view.retry_button.click()
    _settle(app, view)
    assert calls == [diagnostic, diagnostic]
    assert not view._plot.isNull()
    assert view._render_error is None
    assert view.retry_button.isHidden()
    assert view.diagnostic is diagnostic
    assert view.diagnostic.aggregate_slope_s_per_push is None


@pytest.mark.parametrize("reuse_same_object", [True, False])
def test_unchanged_diagnostic_keeps_error_until_explicit_retry(app, view, png, monkeypatch, reuse_same_object):
    outputs = iter((b"not a PNG", png))
    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", lambda _: next(outputs))
    diagnostic = build_track_evolution_diagnostic((), clean_air_gap_s=3.)
    view.set_diagnostic(diagnostic)
    _settle(app, view)
    assert "PNG could not be decoded" in view._render_error
    assert not view.retry_button.isHidden()
    failed_request = view._render_request

    unchanged = diagnostic if reuse_same_object else replace(diagnostic)
    # Normal UI polling must not clear the error or resubmit a failed plot.
    for _ in range(10):
        view.set_diagnostic(unchanged)
        app.processEvents()
    assert view.diagnostic is unchanged
    assert view._render_request == failed_request
    assert "PNG could not be decoded" in view._render_error
    assert not view.retry_button.isHidden()

    view.retry_render()
    _settle(app, view)
    assert view.diagnostic is unchanged
    assert view._render_error is None
    assert not view._plot.isNull()
    assert view.retry_button.isHidden()


def test_non_bytes_renderer_output_produces_a_visible_error(app, view, monkeypatch):
    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", lambda _: None)
    view.set_diagnostic(build_track_evolution_diagnostic((), clean_air_gap_s=3.))
    _settle(app, view)
    assert "did not return PNG bytes" in view._render_error
    assert not view.retry_button.isHidden()


def test_old_request_cannot_replace_new_image_or_create_error(app, view, png, monkeypatch):
    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", lambda _: png)
    diagnostic = build_track_evolution_diagnostic((), clean_air_gap_s=3.)
    view.set_diagnostic(diagnostic)
    _settle(app, view)
    old_request = view._render_request
    view.set_diagnostic(replace(diagnostic, clean_air_gap_s=4.))
    _settle(app, view)
    recovered_image = view._plot.toImage()
    view._accept_render_error(old_request, "stale worker failure")
    view._accept_image(old_request, b"invalid stale PNG")
    assert view._render_error is None
    assert view._plot.toImage() == recovered_image
    assert view.retry_button.isHidden()


def test_unchanged_successful_diagnostic_does_not_render_again(app, view, png, monkeypatch):
    calls = []

    def render(diagnostic):
        calls.append(diagnostic)
        return png

    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", render)
    diagnostic = build_track_evolution_diagnostic((), clean_air_gap_s=3.)
    view.set_diagnostic(diagnostic)
    _settle(app, view)
    view.set_diagnostic(diagnostic)
    view.set_diagnostic(replace(diagnostic))
    view.retry_render()
    app.processEvents()
    assert calls == [diagnostic]


def test_close_ignores_pending_result_and_failure(app, view, png, monkeypatch):
    monkeypatch.setattr(widgets, "render_track_evolution_diagnostic", lambda _: png)
    view.set_diagnostic(build_track_evolution_diagnostic((), clean_air_gap_s=3.))
    _settle(app, view)
    request = view._render_request
    image = view._plot.toImage()
    view.close()
    view._accept_render_error(request, "finished after close")
    view._accept_image(request, b"invalid PNG")
    assert view._render_error is None
    assert view._plot.toImage() == image
