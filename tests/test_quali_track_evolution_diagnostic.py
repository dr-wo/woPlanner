import time

import pytest
from PySide6 import QtGui, QtWidgets

from woplanner.quali.track_evolution import (
    TrackEvolutionSample,
    build_track_evolution_diagnostic,
    render_track_evolution_diagnostic,
)
from woplanner.quali.widgets import TrackEvolutionDiagnosticView
from woplanner.quali.service import QualiLiveService


def _sample(part, driver, push_index, install, *, compound="SOFT", gap=4., lap_time=None):
    return TrackEvolutionSample(
        part, driver, "Ferrari", push_index,
        float(lap_time if lap_time is not None else 90. - push_index),
        push_index, compound, 0, True, gap, .8, "ok",
        tyre_install_index=install,
    )


def test_session_wide_history_and_global_push_indices_survive_q1_q2_q3():
    samples = (
        _sample("Q1", "AAA", 1, 0),
        _sample("Q2", "AAA", 5, 1),
        _sample("Q3", "AAA", 9, 2),
    )
    service = QualiLiveService()
    from test_quali_canonical_track_rate import seed
    seed(service, *samples[:2])
    service.session_part = "Q2"
    q2 = service.track_evolution_diagnostic()
    assert [(s.part, s.global_push_index) for s in q2.series[0].samples] == [
        ("Q1", 1), ("Q2", 5),
    ]
    seed(service, samples[2])
    service.session_part = "Q3"
    q3 = service.track_evolution_diagnostic()
    assert [(s.part, s.global_push_index) for s in q3.series[0].samples] == [
        ("Q1", 1), ("Q2", 5), ("Q3", 9),
    ]
    assert q3.lap_count == 3


def test_first_eligible_push_per_installation_and_cross_part_identity():
    samples = (
        _sample("Q1", "AAA", 1, 0, gap=2.),  # Rejected: traffic.
        _sample("Q1", "AAA", 2, 0),            # First eligible on install 0.
        _sample("Q2", "AAA", 3, 0),            # Same install after part change.
        _sample("Q2", "AAA", 4, 1),            # New installation.
        _sample("Q3", "AAA", 5, 1),            # Later PUSH on install 1.
        _sample("Q3", "AAA", 6, 2),            # Another new installation.
    )
    diagnostic = build_track_evolution_diagnostic(samples, clean_air_gap_s=3.)
    assert [s.global_push_index for s in diagnostic.observations] == [2, 4, 6]
    assert diagnostic.lap_count == 3
    assert all(s.first_eligible_push_for_installation for s in diagnostic.observations)
    assert [(s.part, s.tyre_install_index) for s in diagnostic.series[0].samples] == [
        ("Q1", 0), ("Q2", 1), ("Q3", 2),
    ]


def test_driver_fit_keeps_compound_change_separate_and_weights_support():
    samples = (
        _sample("Q1", "LAW", 1, 0, compound="MEDIUM", lap_time=95.),
        _sample("Q2", "LAW", 3, 1, lap_time=91.),
        _sample("Q2", "LAW", 5, 2, lap_time=89.),
        _sample("Q1", "BBB", 2, 0, lap_time=80.),
        _sample("Q2", "BBB", 4, 1, lap_time=80.),
        _sample("Q3", "BBB", 6, 2, lap_time=80.),
    )
    diagnostic = build_track_evolution_diagnostic(samples, clean_air_gap_s=3.)
    law = next(series for series in diagnostic.series if series.driver == "LAW")
    bbb = next(series for series in diagnostic.series if series.driver == "BBB")
    assert law.compound == "SOFT"
    assert [s.global_push_index for s in law.samples] == [3, 5]
    assert len(bbb.samples) == 3
    assert law.slope_s_per_push == pytest.approx(-1.)
    assert bbb.slope_s_per_push == pytest.approx(0.)
    assert diagnostic.aggregate_slope_s_per_push == pytest.approx(-.4)
    assert diagnostic.weighted_slope_std_s_per_push == pytest.approx((.24) ** .5)


def test_slope_dispersion_identical_opposing_single_and_unavailable():
    def fit(second_start, second_end):
        return build_track_evolution_diagnostic((
            _sample("Q1", "AAA", 1, 0, lap_time=90),
            _sample("Q2", "AAA", 2, 1, lap_time=89),
            _sample("Q1", "BBB", 3, 0, lap_time=second_start),
            _sample("Q2", "BBB", 4, 1, lap_time=second_end),
        ), clean_air_gap_s=3.)

    assert fit(90, 89).weighted_slope_std_s_per_push == pytest.approx(0., abs=1e-12)
    opposing = fit(90, 91)
    assert opposing.aggregate_slope_s_per_push == pytest.approx(0., abs=1e-12)
    assert opposing.weighted_slope_std_s_per_push == pytest.approx(1.)
    single = build_track_evolution_diagnostic((
        _sample("Q1", "AAA", 1, 0), _sample("Q2", "AAA", 2, 1),
    ), clean_air_gap_s=3.)
    assert single.weighted_slope_std_s_per_push == 0.
    empty = build_track_evolution_diagnostic((), clean_air_gap_s=3.)
    assert empty.weighted_slope_std_s_per_push is None


def test_missing_install_identity_is_not_invented_from_compound_or_start_laps():
    samples = (
        _sample("Q1", "AAA", 1, None),
        _sample("Q2", "AAA", 4, None),
    )
    diagnostic = build_track_evolution_diagnostic(samples, clean_air_gap_s=3.)
    assert not diagnostic.observations
    assert diagnostic.driver_count == 0


def test_qt_view_and_export_share_the_exact_diagnostic_object():
    samples = (
        TrackEvolutionSample("Q2", "AAA", "Team", 4, 82.4, 4, "SOFT", 0, True, 4.0, .8, "ok", tyre_install_index=0),
        TrackEvolutionSample("Q2", "AAA", "Team", 7, 82.1, 10, "SOFT", 0, True, 4.0, .8, "ok", tyre_install_index=1),
        TrackEvolutionSample("Q2", "LAW", "Red Bull Racing", 4, 84.0, 2, "MEDIUM", 0, True, 4.0, .8, "ok", tyre_install_index=0),
        TrackEvolutionSample("Q2", "LAW", "Red Bull Racing", 8, 83.0, 9, "SOFT", 0, True, 4.0, .8, "ok", tyre_install_index=1),
    )
    diagnostic = build_track_evolution_diagnostic(samples, clean_air_gap_s=3.0)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = TrackEvolutionDiagnosticView()
    view.set_diagnostic(diagnostic)
    assert view.diagnostic is diagnostic
    deadline = time.monotonic() + 3
    while view._plot.isNull() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not view._plot.isNull()
    exported = QtGui.QImage.fromData(render_track_evolution_diagnostic(diagnostic))
    assert view._plot.toImage().convertToFormat(QtGui.QImage.Format.Format_RGB32) == \
        exported.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    assert diagnostic.driver_count == 1
    assert diagnostic.series[0].driver == "AAA"
    view.close()
    app.processEvents()
