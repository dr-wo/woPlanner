"""Exercise actual woData 100a946 classes, before provenance fields existed."""
from dataclasses import fields, replace
import importlib.util
from pathlib import Path
import sys

import pytest

from test_quali_projection_identity import evidence
from woplanner.quali.service import QualiLiveService, _associated_best_sector_checkpoints


@pytest.fixture(scope="module")
def old_models():
    path = Path(__file__).parent / "fixtures/wodata_quali_models_100a946.py"
    spec = importlib.util.spec_from_file_location("wodata_prefixed_quali_models", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def old_shape(cls, current):
    return cls(**{field.name: getattr(current, field.name) for field in fields(cls)})


@pytest.mark.parametrize("old_event,old_driver,old_snapshot", [
    (True, False, False), (False, True, False), (True, True, True),
])
@pytest.mark.parametrize("warm_cache", [False, True])
def test_old_model_objects_keep_service_running_and_clear_projection_reference(
        old_models, old_event, old_driver, old_snapshot, warm_cache):
    event, owner, current, snapshot = evidence()
    service = QualiLiveService()
    expected = QualiLiveService().consume(snapshot)
    if warm_cache:
        service.consume(snapshot)
        assert service._reference_total_s == owner.best_lap_s
    if old_event:
        event = old_shape(old_models.QualiCompletedLap, event)
        assert not hasattr(event, "event_key")
    if old_driver:
        owner = old_shape(old_models.QualiRawDriverState, owner)
        current = old_shape(old_models.QualiRawDriverState, current)
        assert not hasattr(owner, "best_lap_event_key")
    snapshot = replace(snapshot, drivers=(owner, current), completed_lap_sequence=(event,))
    if old_snapshot:
        snapshot = old_shape(old_models.QualiRawSnapshot, snapshot)
    assert _associated_best_sector_checkpoints(owner, (event,), "Q1") == {}
    result = service.consume(snapshot)
    assert service._reference_total_s is None
    assert not service._driver_best_references
    assert all(driver.projected_lap_s is None for driver in result.drivers)
    oco = next(driver for driver in result.drivers if driver.driver_number == "31")
    assert oco.driver == "OCO"
    assert oco.best_lap_s == owner.best_lap_s
    assert oco.last_lap_s == owner.last_lap_s
    assert oco.sectors_s == owner.sectors_s
    # Publication provenance advances on repeated reads; compare all domain
    # results while excluding only that new publication envelope.
    without_projection = lambda state: replace(state, track_evolution_result={
        k: v for k, v in state.track_evolution_result.items() if k != "as_of"
    }, drivers=tuple(
        replace(driver, projected_lap_s=None, projected_gap_s=None) for driver in state.drivers))
    assert without_projection(result) == without_projection(expected)


def test_current_models_without_provenance_are_also_unsupported():
    _, owner, current, snapshot = evidence()
    owner = replace(owner, best_lap_event_key=None)
    service = QualiLiveService()
    result = service.consume(replace(snapshot, drivers=(owner, current)))
    assert service._reference_total_s is None
    assert all(driver.projected_lap_s is None for driver in result.drivers)


def test_old_completion_with_receive_clock_keeps_pace_sample_unsupported(monkeypatch):
    from types import SimpleNamespace
    from wodata.live.quali_models import QualiRawDriverState, QualiRawSnapshot

    monkeypatch.setattr("woplanner.quali.service.classify_lap_state", lambda *args, **kwargs: "FAST")
    service = QualiLiveService()
    driver = QualiRawDriverState("1", "AAA", "Team", completed_laps=0)
    raw = replace(QualiRawSnapshot.empty(), drivers=(driver,), session_part_established=True,
                  traffic_clock_time_s=100., traffic_clock_kind="receive")
    service.consume(raw)
    completion = SimpleNamespace(session_part="Q1", driver_number="1", lap_number=1,
                                 lap_time_s=90., session_time_s=90.)
    service.consume(replace(raw, drivers=(replace(driver, completed_laps=1, last_lap_s=90.,
                                                 best_lap_s=90., timed_lap_eligible=True),),
                            completed_lap_sequence=(completion,)))
    assert service.snapshot.drivers[0].last_lap_s == 90.
    assert len(service._track_samples) == 1
    assert service._track_samples[0].traffic_status == "unknown"
    assert not service._pending_traffic
