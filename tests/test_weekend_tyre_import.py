"""DBG-1: immutable R16 evidence through the production Planner import/search path."""
from pathlib import Path
import hashlib
import json
import shutil

import pytest

from wodata.artifacts import strategy_search_root
from wodata.contracts import StrategySearchState
from woplanner.analysis.service import OfflineAnalysisService, ModelSummary
from woplanner.analysis.tyre_inputs import TyreInputService, tyre_snapshot_from_overrides
from woplanner.analysis.weekend_tyre_import import load_pre_race_tyre_import

FIXTURE = Path(__file__).parent / 'fixtures/dbg1_r16'
RUN_ID = '20261003T225359.171082Z-f370e12cf1'
CANONICAL = {'Deg_SOFT': .20460105651325672, 'Deg_MEDIUM': .14407032425651345,
             'Deg_HARD': .09141717826254332}
IMPORTED = {'Deg_SOFT': .062, 'Deg_MEDIUM': .039, 'Deg_HARD': .033}


def install_evidence(root, run_id=RUN_ID):
    run = root / 'woweekend/runs/schema_v1/event=2026-16/workflow=race_preparation/runs' / run_id
    run.mkdir(parents=True)
    for file in FIXTURE.iterdir():
        if file.name != 'canonical_prediction.json':
            shutil.copyfile(file, run / file.name)
    prediction = root / 'wostrategy/tyre_prediction/schema_v1/year=2026/predictions/round=16/prediction.json'
    prediction.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE / 'canonical_prediction.json', prediction)
    if run_id != RUN_ID:
        change(run, 'run.json', lambda data: data.update(run_id=run_id))
        change(run, 'manifest.json', lambda data: data.update(run_id=run_id))
    return run


def change(run, filename, edit, *, rehash=True):
    path = run / filename
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    if rehash and filename != 'run.json':
        index = json.loads((run / '.artifacts.json').read_text())
        index['artifacts'][filename]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        (run / '.artifacts.json').write_text(json.dumps(index))


def numbers(service):
    return {row.parameter: row.selected_value for row in service.resolve(2026, 16).parameters}


def test_r16_real_import_search_restart_clear_and_snapshot(tmp_path):
    run = install_evidence(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    tyres = service.tyre_inputs
    before = numbers(tyres)
    assert {key: before[key] for key in CANONICAL} == pytest.approx(CANONICAL)
    assert {row.selected_source for row in tyres.resolve(2026, 16).parameters} == {'pre_race_prediction'}
    config = json.loads((run / 'config.resolved.json').read_text())
    summary = ModelSummary({'analysis_id': 'r16-parity', 'config_hash': 'matched'}, ())

    def search():
        resolved = tyres.resolve(2026, 16)
        return service.generate_continuations(
            year=2026, round_number=16,
            state=StrategySearchState(0, config['resolved_total_laps'], 'MEDIUM', 0,
                                      None, config['max_stops']),
            pit_loss=config['pit_loss']['green']['total'], k=3, rules_compliant=True,
            model_summary=summary, overrides=resolved.strategy_overrides(),
            tyre_input_snapshot=tyre_snapshot_from_overrides(resolved.strategy_overrides(), resolved),
        )[0]

    baseline = search()
    assert len(baseline.plan.pit_laps) == 2
    tyres.import_pre_race_run(2026, 16, RUN_ID)
    imported = numbers(tyres)
    assert {key: imported[key] for key in IMPORTED} == IMPORTED
    resolved = tyres.resolve(2026, 16)
    assert {row.selected_source for row in resolved.parameters} == {'manual'}
    origin = resolved.semantics['manual_import_origin']
    assert origin['run_id'] == RUN_ID and origin['session'] == 'R'
    assert origin['effective_input_sha256'] == '19c491735e0c347c4b3dfac0e13f4698b0a5f6d8c3be31706bd42bac2dfdf5ce'
    assert origin['fields']['Deg_SOFT']['source'] == 'manual_override'
    assert origin['fields']['Perf_SOFT_vs_MEDIUM']['source'] == 'historical_baseline'
    result = search()
    saved = json.loads((run / 'strategy_rules_compliant.json').read_text())[0]
    assert len(result.plan.pit_laps) == len(saved['pit_laps']) == 1
    assert result.remaining_cost == pytest.approx(saved['cost'], abs=1e-10)
    assert list(result.plan.compounds) == saved['compounds']
    assert list(result.plan.pit_laps) == saved['pit_laps']
    restarted = TyreInputService(tmp_path)
    assert numbers(restarted) == imported
    assert restarted.resolve(2026, 16).semantics['manual_import_origin'] == origin
    snapshots = list((strategy_search_root(2026, 16, 'global', tmp_path) / 'tyre_input_snapshots').glob('*.json'))
    captured = [json.loads(p.read_text()) for p in snapshots]
    adopted = next(s for s in captured if s['semantics'].get('manual_import_origin'))
    assert adopted['values'] == imported
    tyres.clear_all_manual_values(2026, 16)
    assert numbers(tyres) == before
    assert 'manual_import_origin' not in tyres.resolve(2026, 16).semantics
    assert len(search().plan.pit_laps) == 2
    assert adopted['values'] == imported


def test_partial_source_copy_no_rebinding_local_edit_and_team_isolation(tmp_path):
    run = install_evidence(tmp_path)
    # A report overriding only SOFT: other fields are saved automatic effective values.
    def partial(data):
        for c in ('MEDIUM', 'HARD'):
            row = data['compounds'][c]['degradation_seconds_per_lap']
            row.update(effective_value=row['automatic_value'], source='historical_baseline')
    change(run, 'tyre_prediction.effective.json', partial)
    frozen = (run / 'tyre_prediction.effective.json').read_bytes()
    tyres = TyreInputService(tmp_path)
    tyres.set_manual_value(2026, 16, 'Deg_HARD', .4, team='Ferrari')
    team_before = tyres.resolve(2026, 16, team='Ferrari').as_dict()
    tyres.import_pre_race_run(2026, 16, RUN_ID)
    assert numbers(tyres)['Deg_HARD'] == CANONICAL['Deg_HARD']
    assert numbers(tyres)['Deg_SOFT'] == .062
    assert tyres.resolve(2026, 16, team='Ferrari').content_hash == team_before['content_hash']
    assert 'manual_import_origin' not in tyres.resolve(2026, 16, team='Ferrari').semantics
    assert all(row.manual_value is None for row in tyres.resolve(2026, 15).parameters)
    adopted = numbers(tyres)
    later = install_evidence(tmp_path, 'later-report')
    change(later, 'tyre_prediction.effective.json', lambda d: d['compounds']['SOFT']['degradation_seconds_per_lap'].update(effective_value=.8))
    assert numbers(TyreInputService(tmp_path)) == adopted
    tyres.set_manual_value(2026, 16, 'Deg_SOFT', .09)
    assert numbers(tyres)['Deg_SOFT'] == .09
    assert (run / 'tyre_prediction.effective.json').read_bytes() == frozen
    origin = tyres.resolve(2026, 16).semantics['manual_import_origin']
    assert 'Deg_SOFT' not in origin['fields'] and 'Deg_HARD' in origin['fields']
    tyres.clear_manual_value(2026, 16, 'Deg_HARD')
    assert tyres.resolve(2026, 16).by_parameter()['Deg_HARD'].selected_source == 'pre_race_prediction'


@pytest.mark.parametrize('failure', ['failed', 'partial', 'wrong_event', 'wrong_session', 'run_identity',
                                    'missing', 'nan', 'hash', 'component', 'reference', 'medium', 'incomplete'])
def test_invalid_run_rejected_without_changing_manuals(tmp_path, failure):
    run = install_evidence(tmp_path)
    tyres = TyreInputService(tmp_path)
    tyres.set_manual_value(2026, 16, 'Deg_SOFT', .5)
    before = tyres.manual_path(2026, 16).read_bytes()
    if failure in ('failed', 'partial'):
        change(run, 'manifest.json', lambda d: d.update(status=failure.upper()))
    elif failure == 'wrong_event':
        change(run, 'manifest.json', lambda d: d.update(event='2026-15'))
    elif failure == 'wrong_session':
        change(run, 'config.resolved.json', lambda d: d.update(session='S'))
    elif failure == 'run_identity':
        change(run, 'run.json', lambda d: d.update(run_id='another-run'))
    elif failure == 'missing':
        (run / 'tyre_prediction.effective.json').unlink()
    elif failure == 'component':
        change(run, 'manifest.json', lambda d: d['outputs'].update(strategy_rules_compliant='FAILED'))
    elif failure == 'reference':
        change(run, 'tyre_prediction.effective.json', lambda d: d.update(reference_compound='HARD'))
    elif failure == 'medium':
        change(run, 'tyre_prediction.effective.json', lambda d: d['compounds']['MEDIUM']['performance_delta_to_medium'].update(effective_value=.1))
    elif failure == 'incomplete':
        change(run, 'tyre_prediction.effective.json', lambda d: d['compounds'].pop('HARD'))
    else:
        change(run, 'tyre_prediction.effective.json', lambda d: d['compounds']['SOFT']['degradation_seconds_per_lap'].update(effective_value=float('nan') if failure == 'nan' else .9), rehash=failure != 'hash')
    with pytest.raises(ValueError):
        tyres.import_pre_race_run(2026, 16, RUN_ID)
    assert tyres.manual_path(2026, 16).read_bytes() == before
    assert tyres.available_pre_race_runs(2026, 16) == []


def test_explicit_selection_listing_and_effective_snapshot_attribution(tmp_path):
    install_evidence(tmp_path)
    install_evidence(tmp_path, 'second-run')
    tyres = TyreInputService(tmp_path)
    malformed = tmp_path / 'woweekend/runs/schema_v1/event=2026-16/workflow=race_preparation/runs/broken'
    malformed.mkdir()
    assert {r.run_id for r in tyres.available_pre_race_runs(2026, 16)} == {RUN_ID, 'second-run'}
    assert not tyres.manual_path(2026, 16).exists()
    with pytest.raises(ValueError):
        load_pre_race_tyre_import(2026, 15, RUN_ID, tmp_path)
    with pytest.raises(ValueError):
        load_pre_race_tyre_import(2026, 16, '../' + RUN_ID, tmp_path)
    tyres.import_pre_race_run(2026, 16, RUN_ID)
    resolved = tyres.resolve(2026, 16)
    actual = resolved.strategy_overrides()
    actual['degradation', 'SOFT'] = .9
    snapshot = tyre_snapshot_from_overrides(actual, resolved, manual_values={('degradation', 'SOFT'): .9})
    assert snapshot['values']['Deg_SOFT'] == .9
    assert 'Deg_SOFT' not in snapshot['semantics']['manual_import_origin']['fields']
    assert 'Deg_SOFT' in resolved.semantics['manual_import_origin']['fields']


@pytest.mark.parametrize('manual_override', [False, True])
def test_import_preserves_existing_live_and_team_resolution(tmp_path, manual_override):
    from test_tyre_inputs import team_summary
    install_evidence(tmp_path)
    service = OfflineAnalysisService(tmp_path)
    selected = load_pre_race_tyre_import(2026, 16, RUN_ID, tmp_path)
    service.tyre_inputs.save_manual_inputs(2026, 16, selected.values)
    summary = team_summary()
    plain = service.resolve_tyre_inputs(2026, 16)
    plain_effective, _ = service.effective_tyre_inputs(summary, plain, manual_override=manual_override)
    team_before = service.resolve_tyre_inputs(2026, 16, team='Ferrari')
    team_effective, _ = service.effective_tyre_inputs(summary, team_before, manual_override=manual_override)
    service.tyre_inputs.import_pre_race_run(2026, 16, RUN_ID)
    resolved = service.resolve_tyre_inputs(2026, 16)
    effective, assumptions = service.effective_tyre_inputs(summary, resolved, manual_override=manual_override)
    assert effective == plain_effective
    after_team, _ = service.effective_tyre_inputs(
        summary, service.resolve_tyre_inputs(2026, 16, team='Ferrari'), manual_override=manual_override,
    )
    assert after_team == team_effective
    snapshot = tyre_snapshot_from_overrides(effective, resolved, assumptions=assumptions)
    assert snapshot['values']['Deg_SOFT'] == effective['degradation', 'SOFT']
    if not manual_override:
        assert 'manual_import_origin' not in snapshot['semantics']
