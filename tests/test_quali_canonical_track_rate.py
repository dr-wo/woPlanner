"""Canonical diagnostic construction, signed consumption and precedence."""

from dataclasses import asdict, replace

import pytest

from test_quali_service import driver, raw
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution import TrackEvolutionSample
import woplanner.quali.track_evolution as diagnostic_module
from wostrategy.analysis.live_qualifying import calculate_live_qualifying_track_evolution


LIVE_SOURCE = "Live per-driver diagnostic (session-wide)"


def sample(index, time, *, part="Q1", code="AAA", install=None, gap=4.0):
    return TrackEvolutionSample(
        part, code, "Ferrari", index, time, index, "SOFT", 0, True,
        gap, .8, "ok", tyre_install_index=index if install is None else install,
    )


def seed(service, *samples):
    for item in samples:
        service.track_evolution_policy.samples[(item.part, item.driver, item.lap_number, item.global_push_index)] = item
    service.track_evolution_policy._dirty = True


def forbid_pooled_live_fit(monkeypatch):
    def forbidden(*_):
        pytest.fail("Pooled raw-driver estimator entered the live forecast path")
    monkeypatch.setattr(
        "wostrategy.analysis.live_qualifying.calculate_live_qualifying_track_evolution",
        forbidden,
    )
    monkeypatch.setattr(
        "wostrategy.analysis.pre_quali.estimate_push_sequence_track_progression", forbidden,
    )


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
@pytest.mark.parametrize("rate", [-.1, 0.0, .1])
def test_signed_diagnostic_drives_cutoff_and_q3_p1(rate, part, monkeypatch):
    forbid_pooled_live_fit(monkeypatch)
    service = QualiLiveService(eliminated_counts={part: 1})
    seed(service, sample(1, 90), sample(5, 90 + 4 * rate))
    snapshot = service.consume(raw([driver(1, 90), driver(2, 91), driver(3, 92)], part=part))
    aggregate = snapshot.track_evolution_diagnostic.aggregate_slope_s_per_push
    assert aggregate == pytest.approx(rate, abs=1e-12)
    assert service.automatic_track_rate_s_per_field_push == aggregate
    assert snapshot.automatic_track_rate_s_per_field_push == aggregate
    assert snapshot.cutoff.track_rate_s_per_field_push == aggregate
    assert snapshot.track_source == snapshot.automatic_track_source == LIVE_SOURCE
    cutoff = snapshot.cutoff
    assert cutoff.target_kind == ("P1" if part == "Q3" else "CUTOFF")
    assert cutoff.current_cutoff_s == (90 if part == "Q3" else 91)
    assert cutoff.projected_correction_s == pytest.approx(rate * 3, abs=1e-12)
    assert cutoff.projected_cutoff_s == pytest.approx(cutoff.current_cutoff_s + min(0, rate * 3))


def test_driver_fit_support_weighting_and_actual_aggregate_return_are_consumed(monkeypatch):
    samples = (
        sample(1, 90), sample(3, 89),  # AAA: slope -.5, support 2.
        sample(2, 100, code="BBB"), sample(4, 100, code="BBB"),
        sample(6, 100, code="BBB"),  # BBB: slope 0, support 3.
    )
    legacy = calculate_live_qualifying_track_evolution([s.lap_time_s for s in samples])
    forbid_pooled_live_fit(monkeypatch)
    service = QualiLiveService()
    seed(service, *samples)
    snapshot = service._build_snapshot()
    diagnostic = snapshot.track_evolution_diagnostic
    assert [(s.driver, len(s.samples)) for s in diagnostic.series] == [("AAA", 2), ("BBB", 3)]
    assert diagnostic.series[0].slope_s_per_push == pytest.approx(-.5)
    assert diagnostic.series[1].slope_s_per_push == pytest.approx(0, abs=1e-12)
    aggregate = sum(len(s.samples) * s.slope_s_per_push for s in diagnostic.series) / 5
    assert aggregate == pytest.approx(-.2)
    assert snapshot.automatic_track_rate_s_per_field_push == aggregate
    assert snapshot.cutoff.track_rate_s_per_field_push != pytest.approx(legacy.rate_s_per_field_push)

    # A distinct sentinel returned at the already-fitted aggregate seam proves
    # consumption of that exact return value, independent of numeric agreement.
    calls = []
    def traced_aggregate(values):
        calls.append(tuple(values))
        return -.314159
    monkeypatch.setattr(diagnostic_module, "aggregate_driver_track_progression", traced_aggregate)
    service.track_evolution_policy._dirty = True
    snapshot = service._build_snapshot()
    assert calls[0][0][0] == pytest.approx(-.5)
    assert [count for _, count in calls[0]] == [2, 3]
    assert snapshot.automatic_track_rate_s_per_field_push == -.314159
    assert snapshot.cutoff.track_rate_s_per_field_push == -.314159


def test_manual_override_preserves_separate_automatic_snapshot_and_clear_restores_it():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    seed(service, sample(1, 90), sample(5, 89.6))
    service.consume(raw([driver(1, 90)]))
    diagnostic = service.snapshot.track_evolution_diagnostic
    service.set_track_override(.25)
    exported = asdict(service.snapshot)
    assert exported["cutoff"]["track_rate_s_per_field_push"] == .25
    assert exported["cutoff"]["projected_correction_s"] == .25
    assert exported["cutoff"]["projected_cutoff_s"] == 90
    assert exported["track_source"] == "Manual override"
    assert exported["automatic_track_rate_s_per_field_push"] == diagnostic.aggregate_slope_s_per_push
    assert exported["automatic_track_source"] == LIVE_SOURCE
    assert service.snapshot.track_evolution_diagnostic is diagnostic
    service.set_fp_track_rate(-.7)
    assert service.track_rate_s_per_field_push == .25
    assert service.automatic_track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
    service.set_track_override(None)
    assert service.snapshot.cutoff.track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
    assert service.snapshot.track_source == LIVE_SOURCE
    assert service.snapshot.track_evolution_diagnostic is diagnostic


@pytest.mark.parametrize("prior", [0.0, -.03, .02])
@pytest.mark.parametrize("unsupported", ["empty", "single", "same_install", "missing_install"])
def test_unsupported_diagnostic_keeps_fp_or_zero_fallback(prior, unsupported, monkeypatch):
    forbid_pooled_live_fit(monkeypatch)
    service = QualiLiveService(track_rate_s_per_field_push=prior)
    if unsupported == "single":
        seed(service, sample(1, 90))
    elif unsupported == "same_install":
        seed(service, sample(1, 90, install=0), sample(5, 89, install=0))
    elif unsupported == "missing_install":
        seed(service, replace(sample(1, 90), tyre_install_index=None),
             replace(sample(5, 89), tyre_install_index=None))
    service.snapshot = service._build_snapshot()
    assert service.snapshot.track_evolution_diagnostic.aggregate_slope_s_per_push is None
    assert service.automatic_track_estimate is None
    assert service.snapshot.automatic_track_rate_s_per_field_push == prior
    assert service.snapshot.track_source == "Configured fallback"
    service.set_track_override(.15)
    service.set_fp_track_rate(-.04)
    assert service.snapshot.cutoff.track_rate_s_per_field_push == .15
    assert service.snapshot.automatic_track_rate_s_per_field_push == -.04
    service.set_track_override(None)
    assert service.snapshot.cutoff.track_rate_s_per_field_push == -.04
    assert service.snapshot.track_source == "Configured fallback"


def test_session_wide_support_transitions_manual_persistence_and_reset():
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0},
                               track_rate_s_per_field_push=-.03)
    seed(service, sample(1, 90))
    service.consume(raw([driver(1, 90)]))
    service.transition_to("Q2")
    assert service.snapshot.automatic_track_source == "Configured fallback"
    assert service.snapshot.automatic_track_rate_s_per_field_push == -.03
    seed(service, sample(5, 89.6, part="Q2"))
    service.consume(raw([driver(1, 89.6)], part="Q2"))
    diagnostic = service.snapshot.track_evolution_diagnostic
    assert {s.part for s in diagnostic.series[0].samples} == {"Q1", "Q2"}
    service.set_track_override(.25)
    service.transition_to("Q3")
    assert service.snapshot.track_evolution_diagnostic is diagnostic
    assert service.snapshot.automatic_track_rate_s_per_field_push == diagnostic.aggregate_slope_s_per_push
    assert service.snapshot.automatic_track_source == LIVE_SOURCE
    assert service.snapshot.cutoff.track_rate_s_per_field_push == .25
    assert service.snapshot.track_source == "Manual override"
    service.finalise()
    assert service.snapshot.track_evolution_diagnostic is diagnostic
    assert service.snapshot.cutoff.track_rate_s_per_field_push == .25
    service.set_track_override(None)
    assert service.snapshot.track_source == LIVE_SOURCE
    service.reset()
    assert service.track_override_s_per_field_push is None
    assert service.snapshot.track_evolution_diagnostic.aggregate_slope_s_per_push is None
    assert service.snapshot.automatic_track_source == "Configured fallback"
    assert service.snapshot.cutoff.track_rate_s_per_field_push == -.03


def test_clean_air_setting_cannot_veto_canonical_rate():
    service = QualiLiveService(track_rate_s_per_field_push=-.03)
    seed(service, sample(1, 90), sample(5, 89.6))
    service.snapshot = service._build_snapshot()
    assert service.snapshot.automatic_track_source == LIVE_SOURCE
    service.set_clean_air_gap_s(5)
    assert service.snapshot.track_evolution_diagnostic.aggregate_slope_s_per_push == pytest.approx(-.1)
    assert service.snapshot.automatic_track_source == LIVE_SOURCE
    assert service.snapshot.cutoff.track_rate_s_per_field_push == pytest.approx(-.1)
    service.set_clean_air_gap_s(3)
    assert service.snapshot.automatic_track_source == LIVE_SOURCE
    assert service.snapshot.cutoff.track_rate_s_per_field_push == pytest.approx(-.1)
