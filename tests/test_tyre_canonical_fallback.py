"""A complete canonical prediction does not depend on unused legacy data."""
import json
from types import SimpleNamespace

import pytest
from wodata.artifacts import weekend_model_root
from woplanner.analysis import tyre_inputs


def test_complete_canonical_ignores_malformed_unused_legacy(monkeypatch, tmp_path):
    legacy = weekend_model_root(2026, 18, tmp_path) / 'pre_race_prior_v1.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{not valid json')
    compounds = {compound: SimpleNamespace(degradation_seconds_per_lap=deg,
                    performance_delta_to_medium=delta)
                 for compound, deg, delta in [('HARD', .08, .4), ('MEDIUM', .1, 0), ('SOFT', .15, -.4)]}
    monkeypatch.setattr(tyre_inputs, 'get_pre_race_tyre_prediction', lambda **kw:
        SimpleNamespace(compounds=compounds, artifact_id='canonical-control', provenance={
            'prediction_status': 'prior_only', 'classification': 'prior_only',
            'independent_of_current_fp': True}))
    resolved = tyre_inputs.TyreInputService(tmp_path).resolve(2026, 18)
    assert resolved.strategy_overrides() == {('degradation', 'HARD'): .08,
        ('degradation', 'MEDIUM'): .1, ('degradation', 'SOFT'): .15,
        ('compound_delta', 'HARD'): .4, ('compound_delta', 'MEDIUM'): 0,
        ('compound_delta', 'SOFT'): -.4}
    assert all(item.selected_source == 'pre_race_prediction' for item in resolved.parameters)
    assert all(item.independent_of_current_fp for item in resolved.parameters)
    assert resolved.semantics['classification'] == 'prior_only'
    assert resolved.semantics['prediction_status'] == 'prior_only'


def test_required_malformed_legacy_still_fails(monkeypatch, tmp_path):
    legacy = weekend_model_root(2026, 18, tmp_path) / 'pre_race_prior_v1.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{not valid json')
    def unavailable(**kwargs):
        raise tyre_inputs.MissingPreRaceTyrePrediction('controlled missing prediction')
    monkeypatch.setattr(tyre_inputs, 'get_pre_race_tyre_prediction', unavailable)
    with pytest.raises(json.JSONDecodeError):
        tyre_inputs.TyreInputService(tmp_path).resolve(2026, 18)


def test_real_canonical_provider_preserves_explicit_prior_provenance(tmp_path):
    # Controlled stored artifact uses the real woStrategy provider boundary.
    path = tmp_path/'wostrategy/tyre_prediction/schema_v1/year=2026/predictions/round=18/prediction.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        'season': 2026, 'round': 18, 'event': 'controlled-artifact',
        'prediction_timestamp': '2026-10-25T00:00:00+00:00',
        'prediction_fingerprint': 'controlled-prior', 'prediction_status': 'prior_only',
        'provenance': {'classification': 'prior_only', 'independent_of_current_fp': True},
        'compound_allocation': {'SOFT': 'C4', 'MEDIUM': 'C3', 'HARD': 'C2'},
        'descriptor_domain': {},
        'compounds': {compound: {'compound_role': role, 'performance_mean': perf,
            'performance_std': None, 'degradation_mean': deg, 'degradation_std': None}
            for compound, role, perf, deg in [('C4','SOFT',-.4,.15), ('C3','MEDIUM',0,.1), ('C2','HARD',.4,.08)]}}))
    resolved = tyre_inputs.TyreInputService(tmp_path).resolve(2026, 18)
    assert resolved.semantics['classification'] == 'prior_only'
    assert resolved.semantics['prediction_status'] == 'prior_only'
    assert all(item.independent_of_current_fp for item in resolved.parameters)
    assert all(item.selected_source == 'pre_race_prediction' for item in resolved.parameters)
    assert resolved.strategy_overrides()[('compound_delta', 'SOFT')] == -.4
