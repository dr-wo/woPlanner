"""Bounded genuine historical Race Replay -> automatic MC -> actual Qt UI smoke.

Uses the established native replay harness, exact R17 packet prefix through L29,
and 512 samples selected in an isolated copy of model_config. Original recordings
and model artifacts remain unchanged. This is historical transport/UI validation,
not evidence of an R18 Race posterior before tonight's race.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import hashlib
import math
import time
from dataclasses import asdict
from pathlib import Path
from wodata.artifacts import weekend_model_root
from wostrategy.core.race_performance_settings import resolve_race_performance_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=512)
    args = parser.parse_args()
    if args.samples < 128:
        parser.error('Use at least 128 samples for this smoke; quality remains diagnostic')
    spec = importlib.util.spec_from_file_location('native_race_smoke', Path(__file__).with_name('verify_live_mc_workflow.py'))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    harness.OUT = args.output_root.resolve()
    NativeWindow = harness.live_gui.LivePlannerWindow
    class EvidenceWindow(NativeWindow):
        def closeEvent(self, event):
            snapshot = self.service.snapshot
            laps = snapshot.completed_laps
            observed = [{"driver": lap.driver_code, "lap": lap.lap_number,
                         "pit_in": lap.pit_in, "pit_out": lap.pit_out,
                         "racing_pit_stop_id": lap.racing_pit_stop_id}
                        for lap in laps if lap.pit_in or lap.pit_out]
            destination = harness.OUT/'r17_replay/native_pit_ingestion.json'
            if destination.parent.is_dir():
                destination.write_text(json.dumps({"completed_lap_count": len(laps),
                    "observed_pit_laps": observed,
                    "paired_racing_pit_laps": sum(lap.racing_pit_stop_id is not None for lap in laps),
                    "note": "Snapshot consumed by the real Live/Strategy window before close"}, indent=2))
            strategy = self.strategy_prediction_window
            if strategy is not None and not getattr(self, '_strategy_evidence_captured', False):
                self._strategy_evidence_captured = True
                try:
                    summary = strategy._current_model_summary
                    assert summary.manifest['as_of_leader_lap'] == 29
                    leader = max((lap for lap in laps if lap.position == 1 and lap.compound),
                                 key=lambda lap: lap.lap_number)
                    index = strategy.driver.findData(leader.driver_code)
                    assert index >= 1
                    strategy.driver.setCurrentIndex(index)
                    strategy.completed_lap_override_enabled.setChecked(False)
                    strategy._apply_latest_race_state()
                    assert strategy.total_laps.value() == 62
                    assert strategy._effective_completed_lap() == leader.lap_number
                    assert strategy.compound.currentText() == leader.compound
                    strategy.rules_compliant.setChecked(True)
                    strategy._start_search()
                    deadline = time.monotonic()+15
                    while strategy._future is not None and time.monotonic() < deadline:
                        harness.QtWidgets.QApplication.processEvents()
                        strategy._poll_future()
                        time.sleep(.005)
                    assert strategy._last_strategy_rows
                    assert all(math.isfinite(row['remaining_cost']) for row in strategy._last_strategy_rows)
                    assert strategy._current_model_summary.manifest['analysis_id'] == summary.manifest['analysis_id']
                    assert all(row['current_lap'] == leader.lap_number and row['total_laps'] == 62
                               for row in strategy._last_strategy_rows)
                    proof = {'status':'passed', 'model_manifest':summary.manifest,
                        'model_version':strategy._analysis_worker.current_result_version,
                        'selected_driver':strategy.driver.currentData(),
                        'actual_latest_driver_lap':asdict(leader),
                        'state':{'completed_lap':strategy._effective_completed_lap(),
                                 'compound':strategy.compound.currentText(),
                                 'tyre_age':strategy.tyre_age.value(),'total_laps':62,
                                 'pit_loss':strategy.pit_loss.value()},
                        'calculation_tyre_inputs':strategy._last_calculation_tyre_input_snapshot,
                        'results':strategy._last_strategy_rows,
                        'note':'Actual Strategy Calculate callback after native Race MC publication; original R17 identity retained.'}
                    (destination.parent/'fresh_mc_strategy.json').write_text(json.dumps(proof,indent=2,default=str))
                    strategy.tabs.setCurrentWidget(strategy.strategy_tab)
                    strategy.grab().save(str(destination.parent/'fresh_mc_strategy.png'))
                except Exception as exc:
                    (destination.parent/'fresh_mc_strategy_error.txt').write_text(repr(exc))
            super().closeEvent(event)
    harness.live_gui.LivePlannerWindow = EvidenceWindow
    original_prepare = harness.prepare
    def prepare(rnd, lane, through_lap=29):
        prepared = original_prepare(rnd, lane, through_lap)
        dest, source, recording, event = prepared
        model_root = weekend_model_root(2026, rnd, dest)
        manual = model_root/'manual_tyre_inputs_v1.json'
        removed_manual_hash = hashlib.sha256(manual.read_bytes()).hexdigest() if manual.is_file() else None
        if manual.is_file():
            manual.unlink()  # Only the isolated copy; original manual inputs are untouched.
        assert not (model_root/'manifest.json').exists()
        assert not (model_root/'aggregate/manifest.json').exists()
        assert not (model_root/'history').exists()
        initial_service = harness.OfflineAnalysisService(dest)
        initial_service.activate_temporary_replay_model(2026, rnd)
        assert initial_service._ephemeral_summary is None
        initial = initial_service.load_strategy_summary(2026, rnd)
        assert initial.status == 'prior_only' and not initial.parameters
        prediction = dest/f'wostrategy/tyre_prediction/schema_v1/year=2026/predictions/round={rnd}/prediction.json'
        prior = json.loads(prediction.read_text())
        assert max(prior['training_rounds']) < rnd
        (dest/'initial_model_evidence.json').write_text(json.dumps({
            'status':initial.status,'manifest':initial.manifest,'parameter_count':len(initial.parameters),
            'copied_model_manifest':False,'copied_parameter_history':False,
            'isolated_manual_copy_removed':removed_manual_hash is not None,
            'removed_manual_sha256':removed_manual_hash,
            'canonical_training_rounds':prior['training_rounds'],
            'canonical_prediction_timestamp':prior['prediction_timestamp'],
            'note':'No initial Race posterior; unproven manual values excluded. Cached MC policy/settings are not claimed to be a prospective holdout.'},indent=2,default=str))
        config = model_root / 'model_config.json'
        payload = json.loads(config.read_text())
        original_count = payload['sample_count']
        payload['sample_count'] = args.samples
        config.write_text(json.dumps(payload, indent=2))
        settings = resolve_race_performance_settings(year=2026, round_number=rnd, data_root=dest)
        if settings is not None:
            effective = json.loads(settings.path.read_text())
            effective.setdefault('settings', {})['sample_count'] = args.samples
            settings.path.write_text(json.dumps(effective, indent=2))
        (dest/'smoke_control.json').write_text(json.dumps({
            'purpose': 'bounded historical native Race UI/MC integration',
            'original_sample_count': original_count, 'smoke_sample_count': args.samples,
            'source_recording': str(source), 'through_leader_lap': through_lap,
            'no_future_r18_race_data': True,
            'note': 'Isolated smoke budget changed and unproven manual overrides omitted; no model/selector mocking.'}, indent=2))
        return prepared
    harness.prepare = prepare
    harness.run(17, 'replay', 29)
    evidence = json.loads((harness.OUT/'r17_replay/evidence.json').read_text())
    strategy_proof = json.loads((harness.OUT/'r17_replay/fresh_mc_strategy.json').read_text())
    assert strategy_proof['status'] == 'passed'
    actual = dict(evidence['final_result']['sampler_quality'])['sample_count']
    if actual != args.samples:
        raise AssertionError(f'Expected smoke budget {args.samples}; actual {actual}')


if __name__ == '__main__':
    main()
