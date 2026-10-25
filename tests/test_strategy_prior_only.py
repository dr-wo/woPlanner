"""Selected scalar inputs permit exact planning without a fictitious MC model."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6 import QtWidgets
from wodata.artifacts import weekend_model_root
from wodata.contracts import StrategySearchState
from woplanner.analysis.service import OfflineAnalysisService
from woplanner.analysis.gui import StrategyPredictionWindow, StrategyPredictionMode


VALUES = {'Deg_HARD': .08, 'Deg_MEDIUM': .12, 'Deg_SOFT': .19,
          'Perf_HARD_vs_MEDIUM': .4, 'Perf_SOFT_vs_MEDIUM': -.5}


def selected_service(tmp_path, values=VALUES):
    service = OfflineAnalysisService(tmp_path)
    for name, value in values.items():
        service.set_manual_tyre_input(2026, 18, name, value)
    return service


def test_complete_selected_supports_exact_strategy_and_signed_manual_change(tmp_path):
    service = selected_service(tmp_path)
    summary = service.load_strategy_summary(2026, 18)
    assert summary.status == 'prior_only' and summary.parameters == ()
    assert summary.manifest['semantics']['mc_posterior'] is False
    assert 'ess' not in summary.manifest
    # Existing load_model still requires a real model artifact.
    with pytest.raises(FileNotFoundError):
        service.load_model(2026, 18)
    state = StrategySearchState(0, 56, 'MEDIUM', 0, None, 3)
    resolved = service.resolve_tyre_inputs(2026, 18)
    model = service.build_strategy_model(summary, resolved.strategy_overrides())
    assert model.compound_delta == {'HARD': .4, 'MEDIUM': 0, 'SOFT': -.5}
    results = service.generate_continuations(year=2026, round_number=18, state=state,
        pit_loss=20.5, persist=False, overrides=resolved.strategy_overrides(), rules_compliant=True)
    assert results and all(len(set(result.plan.compounds)) >= 2 for result in results)
    service.set_manual_tyre_input(2026, 18, 'Perf_SOFT_vs_MEDIUM', -1.1)
    changed = service.load_strategy_summary(2026, 18)
    assert changed.manifest['selected_input_hash'] != summary.manifest['selected_input_hash']
    newer = service.generate_continuations(year=2026, round_number=18, state=state,
        pit_loss=20.5, persist=False, overrides=service.resolve_tyre_inputs(2026, 18).strategy_overrides(),
        rules_compliant=True)
    assert newer[0].remaining_cost != results[0].remaining_cost


def test_incomplete_selected_and_corrupt_snapshot_do_not_fallback(tmp_path):
    service = selected_service(tmp_path, {'Deg_MEDIUM': .12})
    with pytest.raises(ValueError, match='Missing required tyre inputs'):
        service.load_strategy_summary(2026, 18)
    service = selected_service(tmp_path)
    path = weekend_model_root(2026, 18, tmp_path) / 'manifest.json'
    path.write_text('{bad json')
    with pytest.raises(json.JSONDecodeError):
        service.load_strategy_summary(2026, 18)
    path.write_text('{}')
    with pytest.raises(FileNotFoundError):
        service.load_strategy_summary(2026, 18)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        service.load_strategy_summary(2026, 18, source_scope="FP1")


def test_prior_only_gui_is_honest_and_generate_available(tmp_path):
    service = selected_service(tmp_path)
    config = tmp_path/'r18.json'
    config.write_text(json.dumps({'season': 2026, 'round_number': 18,
        'race': {'total_laps': 56}, 'pit_loss': {'green': {'total': 20.5}}}))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(year=2026, round_number=18, service=service,
        mode=StrategyPredictionMode.CURRENT_EVENT, event_config_path=config,
        live_operational_controls=True, state_directory=tmp_path/'ui')
    try:
        assert window._current_model_summary.status == 'prior_only'
        assert window.model_table.rowCount() == 0
        assert window.generate_button.isEnabled()
        assert 'inventory' in window.strategy_inventory_label.text()
        assert 'not checked' in window.strategy_inventory_label.text()
        assert not window.strategy_inventory_label.isHidden()
        assert 'no MC posterior' in window.model_traffic_label.text()
        assert 'previously saved model' not in window.mc_readiness_label.text()
        window.rules_compliant.setChecked(True)
        window._start_search()
        import time
        deadline = time.monotonic() + 10
        while window._future is not None and time.monotonic() < deadline:
            app.processEvents()
            window._poll_future()
            time.sleep(.005)
        assert window._last_strategy_rows
        assert window._last_calculation_tyre_input_snapshot['values']['Perf_SOFT_vs_MEDIUM'] == -.5
    finally:
        window.close()
        app.processEvents()


def test_prior_only_partial_override_preserves_all_selected_coordinates(tmp_path):
    service = selected_service(tmp_path)
    state = StrategySearchState(0, 56, 'MEDIUM', 0, None, 3)
    partial = {("compound_delta", "SOFT"): -1.1}
    complete = service.resolve_tyre_inputs(2026, 18).strategy_overrides()
    complete.update(partial)
    kwargs = dict(year=2026, round_number=18, state=state, pit_loss=20.5,
                  persist=False, rules_compliant=True)
    assert service.generate_continuations(**kwargs, overrides=partial) == service.generate_continuations(**kwargs, overrides=complete)
    for invalid in ({("degradation", "HARD"): -.1},
                    {("compound_delta", "SOFT"): float('nan')},
                    {("compound_delta", "MEDIUM"): .1}):
        with pytest.raises(ValueError):
            service.generate_continuations(**kwargs, overrides=invalid)


def test_explicit_fp_architecture_label_preserves_unknown_legacy(tmp_path):
    from woplanner.analysis.service import ModelSummary
    service = selected_service(tmp_path)
    config = tmp_path/'r18.json'
    config.write_text(json.dumps({'season': 2026, 'round_number': 18,
        'race': {'total_laps': 56}, 'pit_loss': {'green': {'total': 20.5}}}))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(year=2026, round_number=18, service=service,
        mode=StrategyPredictionMode.CURRENT_EVENT, event_config_path=config,
        live_operational_controls=True, state_directory=tmp_path/'ui')
    try:
        explicit = ModelSummary({'analysis_id': 'fp-preparation', 'as_of_leader_lap': 0,
            'semantics': {'architecture': 'joint-weekend-fp-v1'}}, ())
        window._load_model(summary=explicit)
        assert window.model_traffic_label.text() == 'Pre-race FP preparation — no Race MC yet'
        assert 'displayed: pre-race FP model' in window.mc_readiness_label.text()
        legacy = ModelSummary({'analysis_id': 'unknown-legacy', 'as_of_leader_lap': 0}, ())
        window._load_model(summary=legacy)
        assert 'unrecorded (legacy model)' in window.model_traffic_label.text()
        assert 'pre-race FP model' not in window.mc_readiness_label.text()
    finally:
        window.close()
        app.processEvents()


def test_sparse_fp_artifact_renders_unsupported_and_nullable_variants(tmp_path):
    from wostrategy.analysis.fp_tyre_evidence import write_fp_tyre_evidence
    service = selected_service(tmp_path)
    evidence = {'schema_version':1,'analysis_id':'controlled-sparse','calculated_at':'2026-10-25T00:00:00+00:00',
        'sessions_used':['FP1'],'source_fingerprints':{},'longrun_laps':[],
        'degradation_contrasts':[
            {'parameter':'Delta_HM','strict_central':.02,'strict_p10':None,'strict_p90':.08,
             'one_gap_central':None,'robust_variability_central':.03,
             'supporting_runs':2,'supporting_teams':1,'supporting_clean_laps':8},
            {'parameter':'Delta_SM','support_status':'unsupported','reason':'No SOFT LongRun support',
             'strict_central':None,'strict_p10':None,'strict_p90':None,
             'one_gap_central':None,'robust_variability_central':None,
             'supporting_runs':0,'supporting_teams':0,'supporting_clean_laps':0}],
        'performance_support':[]}
    write_fp_tyre_evidence(evidence,year=2026,round_number=18,data_root=tmp_path)
    config=tmp_path/'race.json';config.write_text(json.dumps({'season':2026,'round_number':18,
        'race':{'total_laps':56},'pit_loss':{'green':{'total':20.5}}}))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window=StrategyPredictionWindow(year=2026,round_number=18,service=service,
        mode=StrategyPredictionMode.CURRENT_EVENT,event_config_path=config,
        live_operational_controls=True,state_directory=tmp_path/'ui')
    try:
        text=window.fp_evidence_summary.toPlainText()
        assert 'S − M: unsupported — No SOFT LongRun support' in text
        assert 'one-gap unavailable; robust variability +0.030' in text
        assert 'P10–P90 unavailable … +0.080' in text
        assert window.generate_button.isEnabled()
        assert window._current_model_summary.status=='prior_only'
    finally:
        window.close();app.processEvents()
