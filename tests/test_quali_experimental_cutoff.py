"""Frozen research arithmetic and independent production/experimental sources."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_quali_canonical_track_rate import sample, seed
from test_quali_service import driver, raw
from woplanner.quali.experimental_cutoff import calculate_half_live
from woplanner.quali.service import QualiLiveService


RESEARCH = json.loads((Path(__file__).parent / "fixtures" / "half_live_research_checkpoints.json").read_text())


@pytest.mark.parametrize("checkpoint", RESEARCH["checkpoints"], ids=lambda c: f"{c['stream']}-{c['part']}-{c['message_index']}")
def test_frozen_research_checkpoint(checkpoint):
    # Outcomes are scoring-only and are never provided to the predictor.
    snapshot = SimpleNamespace(cutoff=SimpleNamespace(**checkpoint["cutoff"]),
                               track_evolution_result=checkpoint["result"])
    prediction = calculate_half_live(snapshot)
    assert prediction.projected_cutoff_s == pytest.approx(
        checkpoint["expected_half_live_s"], rel=0, abs=1e-10,
    )
    assert prediction.status == checkpoint["result"]["status"]


def test_primary_300_second_historical_mae_is_reproduced():
    rows = [c for c in RESEARCH["checkpoints"] if c["primary_stream"] and c["threshold_s"] == 300]
    assert len(rows) == 11
    errors = {"zero": [], "canonical": [], "half_live": []}
    for row in rows:
        cutoff = row["cutoff"]
        prediction = calculate_half_live(SimpleNamespace(cutoff=SimpleNamespace(**cutoff),
                                                        track_evolution_result=row["result"]))
        for name, value in (("zero", cutoff["current_cutoff_s"]),
                            ("canonical", cutoff["projected_cutoff_s"]),
                            ("half_live", prediction.projected_cutoff_s)):
            errors[name].append(abs(value - row["outcome_s"]))
    assert {k: sum(v) / len(v) for k, v in errors.items()} == pytest.approx({
        "zero": .2966363636363628, "canonical": .2271590097001967,
        "half_live": .20527511470396706,
    }, rel=0, abs=1e-12)


@pytest.mark.parametrize("part", ["Q1", "Q2", "Q3"])
@pytest.mark.parametrize("rate", [-.1, 0.0, .1])
@pytest.mark.parametrize("manual", [None, -.8, .25])
def test_live_is_halved_independently_of_manual_target_and_signed_clamp(part, rate, manual):
    service = QualiLiveService(eliminated_counts={part: 1})
    seed(service, sample(1, 90), sample(5, 90 + rate * 4))
    service.consume(raw([driver(1, 90), driver(2, 91), driver(3, 92)], part=part))
    service.set_track_override(manual)
    snapshot = service.snapshot
    before = deepcopy(asdict(snapshot))
    prediction = calculate_half_live(snapshot)
    current = 90 if part == "Q3" else 91
    assert prediction.candidate == "half_live"
    assert prediction.source == "LIVE_PER_DRIVER"
    assert prediction.rate_s_per_field_push == pytest.approx(rate / 2, abs=1e-12)
    assert prediction.projected_cutoff_s == pytest.approx(current + min(0, rate * 1.5))
    effective = manual if manual is not None else rate
    assert snapshot.cutoff.projected_cutoff_s == pytest.approx(current + min(0, effective * 3))
    assert asdict(snapshot) == before


@pytest.mark.parametrize("fallback", [-.4, 0.0, .2])
@pytest.mark.parametrize("manual", [None, -.8, .3])
def test_fallback_is_not_halved_and_never_inherits_manual(fallback, manual):
    service = QualiLiveService(track_rate_s_per_field_push=fallback, eliminated_counts={"Q1": 0})
    service.consume(raw([driver(1, 90)]))
    service.set_track_override(manual)
    prediction = calculate_half_live(service.snapshot)
    assert prediction.status == "FALLBACK"
    assert prediction.source == "CONFIGURED_FP_DEFAULT_PRIOR"
    assert prediction.rate_s_per_field_push == fallback
    assert prediction.projected_cutoff_s == 90 + min(0, fallback)


@pytest.mark.parametrize("status", ["LIVE_SUPPORTED", "LIVE_LIMITED"])
def test_support_status_describes_available_fit_without_changing_rate(status):
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    snapshot = service.consume(raw([driver(1, 90)]))
    snapshot = replace(snapshot, track_evolution_result={
        "status": status, "live_estimate_s_per_global_push": -.2,
        "configured_fallback_s_per_global_push": -.7,
    })
    prediction = calculate_half_live(snapshot)
    assert prediction.status == status
    assert prediction.projected_cutoff_s == 89.9


@pytest.mark.parametrize("payload", [None, {}, {"live_estimate_s_per_global_push": None},
    {"live_estimate_s_per_global_push": float("nan"), "status": "LIVE_LIMITED"},
    {"live_estimate_s_per_global_push": float("inf"), "status": "LIVE_SUPPORTED"},
    {"live_estimate_s_per_global_push": -.1},
    {"live_estimate_s_per_global_push": None, "configured_fallback_s_per_global_push": float("nan")},
])
def test_missing_or_invalid_publication_is_explicitly_unavailable(payload):
    snapshot = QualiLiveService(eliminated_counts={"Q1": 0}).consume(raw([driver(1, 90)]))
    prediction = calculate_half_live(replace(snapshot, track_evolution_result=payload))
    assert prediction.status == "UNAVAILABLE"
    assert prediction.projected_cutoff_s is None
    assert prediction.reason


def test_missing_target_and_no_unfinished_drivers():
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    assert calculate_half_live(service.snapshot).status == "UNAVAILABLE"
    snapshot = service.consume(raw([driver(1, 90)]))
    snapshot = replace(snapshot, cutoff=replace(snapshot.cutoff, unfinished_driver_count=0))
    assert calculate_half_live(snapshot).projected_cutoff_s == 90


def test_session_wide_fit_survives_part_transition_but_reset_has_no_stale_prediction():
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
    seed(service, sample(1, 90))
    service.consume(raw([driver(1, 90)]))
    assert calculate_half_live(service.snapshot).status == "FALLBACK"
    service.transition_to("Q2")
    seed(service, sample(5, 89.6, part="Q2"))
    service.consume(raw([driver(1, 89.6)], part="Q2"))
    live = calculate_half_live(service.snapshot)
    service.set_track_override(-.8)
    assert calculate_half_live(service.snapshot) == live
    service.transition_to("Q3")
    assert service.snapshot.cutoff.target_kind == "P1"
    assert calculate_half_live(service.snapshot).status == "UNAVAILABLE"
    service.consume(raw([driver(1, 89.3)], part="Q3"))
    assert calculate_half_live(service.snapshot).rate_s_per_field_push == live.rate_s_per_field_push
    service.finalise()
    assert calculate_half_live(service.snapshot).projected_cutoff_s == service.snapshot.cutoff.current_cutoff_s
    service.reset()
    assert calculate_half_live(service.snapshot).status == "UNAVAILABLE"


def test_calculation_never_refits_or_calls_service_and_snapshot_retains_asof(monkeypatch):
    service = QualiLiveService(eliminated_counts={"Q1": 0})
    seed(service, sample(1, 90), sample(5, 89.6))
    snapshot = service.consume(raw([driver(1, 90)]))
    publication = deepcopy(service.track_evolution_payload())
    def forbidden(*args, **kwargs):
        pytest.fail("Experimental rendering entered accepted processing pipeline")
    monkeypatch.setattr(service.track_evolution_policy, "refresh", forbidden)
    monkeypatch.setattr(service, "_build_snapshot", forbidden)
    for _ in range(5):
        assert calculate_half_live(snapshot).rate_s_per_field_push == pytest.approx(-.05)
    assert snapshot.track_evolution_result["as_of"] == publication["as_of"]
