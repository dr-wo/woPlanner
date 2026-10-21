"""Cold Qt Live -> ordinary Replay button -> automatic MC -> Qt, in isolated data.

No manually prepared observations, sampler mocks, worker submits, or selector changes.
The second lane uses durable live persistence with the same replay source (no network).
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

from PySide6 import QtWidgets
from wodata.artifacts import weekend_model_root
from wostrategy.core.race_performance_settings import resolve_race_performance_settings
from woplanner.analysis import gui as analysis_gui
from woplanner.live import gui as live_gui
from woplanner.analysis.service import OfflineAnalysisService, LIVE_SECTOR_METHOD_VERSION, live_traffic_provenance

DATA = Path(__file__).resolve().parents[2] / 'woData'
OUT = Path(__file__).resolve().parents[1] / 'research/live_mc_race_readiness_20261019/workflow'


def prepare(rnd, lane, through_lap=29):
    dest = OUT / (f'r{rnd}_{lane}' + (f'_through{through_lap}' if through_lap != 29 else ''))
    if dest.exists():
        raise RuntimeError(f'Refusing to mix a previous run: {dest}')
    dest.mkdir(parents=True)
    source = DATA / f'wodata/live_timing/schema_v1/year=2026/round={rnd}/session=R/live_timing_raw.jsonl'
    recording = dest / 'recording/live_timing_raw.jsonl'
    recording.parent.mkdir()
    # Exact byte prefix, ending after leader L29. Original files are read-only.
    limit = ({16:30816,17:20206} if through_lap==14 else {16:48700,17:39650})[rnd]
    with source.open('rb') as src, recording.open('wb') as dst:
        for line in src:
            if json.loads(line)['sequence'] > limit:
                break
            dst.write(line)
    root = weekend_model_root(2026, rnd, DATA)
    local_root = dest / root.relative_to(DATA)
    local_root.mkdir(parents=True)
    for name in ('model_config.json', 'manual_tyre_inputs_v1.json', 'pre_race_prior_v1.json'):
        if (root/name).is_file():
            shutil.copy2(root/name, local_root/name)
    settings = resolve_race_performance_settings(year=2026, round_number=rnd, data_root=DATA)
    if settings:
        target = dest / settings.path.relative_to(DATA)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.path, target)
    prediction = Path(f'wostrategy/tyre_prediction/schema_v1/year=2026/predictions/round={rnd}/prediction.json')
    (dest/prediction).parent.mkdir(parents=True)
    shutil.copy2(DATA/prediction, dest/prediction)
    # Use real target identity/distance, no invented tyre/observation data.
    event = dest/'race.json'
    event.write_text(json.dumps({'season':2026, 'round_number':rnd, 'session':'R',
                                'race':{'total_laps':{16:56,17:62}[rnd]}}))
    return dest, source, recording, event


def run(rnd, lane, through_lap=29):
    dest, source, recording, event = prepare(rnd, lane, through_lap)
    analysis_gui.WORKSPACE_DATA_ROOT = dest
    live_gui.WORKSPACE_DATA_ROOT = dest
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = live_gui.LivePlannerWindow(event_config_path=event, operational_history_data_root=dest)
    window.resize(1600,1000)
    window.show()
    window.year_input.setValue(2026)
    window.round_input.setValue(rnd)
    window.replay_path.setText(str(recording))
    # This is the production Replay button callback, with its real source/controller.
    window._replay()
    if lane == 'durable':
        # Substitute only the unavailable live transport with the real replay
        # source. Open the production Current-event/durable UI used by Connect.
        window._active_source_mode = 'live'
    window._show_strategy_prediction_window()
    strategy = window.strategy_prediction_window
    assert strategy is not None
    if lane == 'durable':
        assert strategy.strategy_mode is analysis_gui.StrategyPredictionMode.CURRENT_EVENT
        assert strategy.persistence_mode == 'durable'
        assert strategy._operational_race_inputs.total_laps == {16:56,17:62}[rnd]
        assert strategy._operational_input_error is None
    assert strategy.traffic_source.currentData() == 'automatic'
    assert strategy.auto_recalculate.isChecked()
    cases = {}
    processes = set()
    successful_ids = set()
    started = time.monotonic()
    exception_log = []
    old_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: exception_log.append(str(value))
    try:
        deadline = started + 300
        while time.monotonic() < deadline:
            app.processEvents()
            assert not exception_log, exception_log
            assert strategy._reported_analysis_error is None, strategy.mc_readiness_label.text()
            executor = strategy._analysis_worker._executor
            if executor is not None:
                processes.update(getattr(executor, '_processes', {}))
            publication = strategy._analysis_worker.current_result
            if publication is not None:
                successful_ids.add(publication.result.analysis_id)
            lap = strategy._mc_processed_lap
            if lap in (14,27,28,29) and lap not in cases:
                pub = strategy._analysis_worker.current_result
                row = dict(lap=lap, eligible=strategy._mc_eligible_count,
                    status=strategy._mc_state, readiness=strategy.mc_readiness_label.text(),
                    main_readiness=window.mc_readiness_label.text(),
                    model_label=strategy.model_traffic_label.text(), rows=strategy.model_table.rowCount(),
                    runtime=window.mc_runtime_identity, elapsed=time.monotonic()-started)
                if pub is not None:
                    row.update(result=asdict(pub.result), provenance=live_traffic_provenance(pub.summary.manifest))
                    assert row['provenance']['requested_traffic_mode']=='automatic'
                    assert row['provenance']['traffic_method_version']==LIVE_SECTOR_METHOD_VERSION
                    assert row['provenance']['effective_traffic_method']=='SECTOR_BOUNDARY_FALLBACK'
                    assert strategy._current_model_summary.manifest['analysis_id']==pub.result.analysis_id
                    assert dict(pub.result.sampler_quality)['fuel_proxy_total_laps']=={16:56,17:62}[rnd]
                    assert strategy.model_table.rowCount()>0
                if rnd==16 and lap==14:
                    assert row['eligible']==0 and pub is None
                if lap==27 or (rnd==17 and lap==14):
                    assert pub is not None and pub.result.leader_lap==lap, row
                    assert 'fresh calculation' in row['readiness']
                cases[lap]=row
                strategy.tabs.setCurrentWidget(strategy.live_tab)
                strategy.grab().save(str(dest/f'L{lap}.png'))
                (dest/'cases.json').write_text(json.dumps(cases, indent=2, default=str))
                print(json.dumps({'round':rnd,'lane':lane,'lap':lap,'eligible':row['eligible'],
                                  'model_lap':pub.result.leader_lap if pub else None}),flush=True)
            finished = not window.service.controller.is_running and window.service.controller._messages.empty()
            if finished and not strategy._analysis_worker.analysis_running and not strategy._telemetry_build_options:
                break
            time.sleep(.005)
        else:
            raise TimeoutError(strategy.calculation_log.toPlainText()[-5000:])
        if lane=='replay' and through_lap==29:
            assert cases.get(27), cases.keys()
            assert {14,27,28,29} <= cases.keys(), cases.keys()
        assert processes and window.mc_runtime_identity['pid'] not in processes
        latest = strategy._analysis_worker.current_result
        if through_lap==29:
            assert latest and latest.result.leader_lap>=28
            assert len(successful_ids)>=2, successful_ids
        else:
            assert 14 in cases
            if rnd==16:
                assert latest is None
                assert 'Insufficient eligible observations' in strategy.mc_readiness_label.text()
            strategy.grab().save(str(dest/'settled.png'))
            window.grab().save(str(dest/'main_settled.png'))
        if lane=='durable' and latest is not None:
            loaded = OfflineAnalysisService(dest).load_model(2026,rnd)
            assert loaded.manifest['analysis_id']==latest.result.analysis_id
            history = OfflineAnalysisService(dest).model_history(2026,rnd)
            assert any(row['analysis_id']==latest.result.analysis_id for row in history)
            assert (weekend_model_root(2026,rnd,dest)/'aggregate/candidate_costs.csv').is_file()
        elif latest is not None:
            # Normal replay intentionally keeps calculations temporary. Prove
            # its existing explicit long-term promotion persists exact provenance.
            strategy.service.save_named_snapshot(2026,rnd,'readiness replay', [])
        (dest/'ui.log').write_text(strategy.calculation_log.toPlainText())
        evidence=dict(round=rnd,lane=lane,source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            prefix_sha256=hashlib.sha256(recording.read_bytes()).hexdigest(),worker_pids=sorted(processes),
            successful_analysis_ids=sorted(successful_ids),
            runtime=window.mc_runtime_identity,cases=cases,final_result=asdict(latest.result) if latest else None,
            settled_readiness=strategy.mc_readiness_label.text(),through_lap=through_lap,
            diagnostics=asdict(window.service.telemetry_worker.diagnostics()),
            persistence='automatic durable' if lane=='durable' else 'temporary replay + explicit named promotion',
            note='Real Qt offscreen, normal Replay ingestion; no future live network validation')
        (dest/'evidence.json').write_text(json.dumps(evidence,indent=2,default=str))
    finally:
        sys.excepthook=old_hook
        window.close()
        app.processEvents()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--round',type=int,choices=[16,17],required=True)
    parser.add_argument('--lane',choices=['replay','durable'],default='replay')
    parser.add_argument('--output-root',type=Path,default=OUT)
    parser.add_argument('--through-lap',type=int,choices=[14,29],default=29)
    args=parser.parse_args()
    OUT=args.output_root.resolve()
    run(args.round,args.lane,args.through_lap)
