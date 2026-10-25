"""Genuine available R18 Selected prior -> exact 1/2/3-stop -> actual Strategy UI.

No FP fit, download, or Race posterior is manufactured. Data source and pit-loss
assumptions are retained verbatim in evidence; results are central tyre costs.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from PySide6 import QtWidgets
from wodata.contracts import StrategySearchState
from wostrategy.algorithm.exact_strategy_search import search_best_fixed_stop_count, StrategyRules
from woplanner.analysis.service import OfflineAnalysisService
from woplanner.analysis.gui import StrategyPredictionWindow, StrategyPredictionMode
from woplanner.operational_inputs import load_operational_race_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--event-config', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    out = args.output_root.resolve()
    out.mkdir(parents=True, exist_ok=False)
    operational = load_operational_race_inputs(args.event_config)
    assert (operational.season, operational.round_number, operational.total_laps) == (2026, 18, 56)
    assert operational.green_pit_loss is not None
    service = OfflineAnalysisService(args.data_root)
    resolved = service.resolve_tyre_inputs(2026, 18)
    summary = service.load_strategy_summary(2026, 18)
    overrides = resolved.strategy_overrides()
    model = service.build_strategy_model(summary, overrides)
    state = StrategySearchState(0, 56, 'MEDIUM', 0, None, 3)
    fixed = {str(stops): [asdict(item) for item in search_best_fixed_stop_count(
        state, model, operational.green_pit_loss, stop_count=stops, k=1,
        allow_any_start=True, strategy_rules=StrategyRules(minimum_distinct_dry_compounds=2))]
        for stops in (1, 2, 3)}
    assert all(fixed.values())
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = StrategyPredictionWindow(year=2026, round_number=18, service=service,
        mode=StrategyPredictionMode.CURRENT_EVENT, event_config_path=args.event_config,
        state_directory=out/'ui-state', persistence_mode='temporary',
        initial_model_summary=summary, live_operational_controls=True)
    window.resize(1400, 1000)
    window.show()
    try:
        assert window.generate_button.isEnabled(), window.operational_status_label.text()
        window.max_stops.setValue(3)
        window.rules_compliant.setChecked(True)
        window._start_search()
        deadline = time.monotonic()+20
        while window._future is not None and time.monotonic()<deadline:
            app.processEvents()
            window._poll_future()
            time.sleep(.005)
        assert window._last_strategy_rows
        frozen = window._last_calculation_tyre_input_snapshot
        assert frozen['values'] == {name: row.selected_value for name, row in resolved.by_parameter().items()}
        window.tabs.setCurrentWidget(window.strategy_tab)
        window.grab().save(str(out/'strategy.png'))
        window.tabs.setCurrentWidget(window.tyre_inputs_tab)
        window.grab().save(str(out/'tyre_inputs.png'))
        evidence = {'event':'2026-R18', 'race_laps':56, 'data_root':str(args.data_root.resolve()),
            'event_config':str(args.event_config.resolve()), 'event_config_sha256':hashlib.sha256(args.event_config.read_bytes()).hexdigest(),
            'pit_inputs':asdict(operational), 'resolved_tyre_inputs':resolved.as_dict(),
            'strategy_model':asdict(model), 'summary':asdict(summary),
            'fixed_stop_best':fixed, 'ui_results':window._last_strategy_rows,
            'actual_ui_calculation_inputs':frozen, 'model_label':window.model_traffic_label.text(),
            'readiness':window.mc_readiness_label.text(), 'posterior_rows':window.model_table.rowCount(),
            'uncertainty':'Unavailable for exact strategy central ranks; no strategy ESS/quantiles/probabilities',
            'race_only_mc':'Waiting for actual Race laps; historical native replay validated separately'}
        (out/'evidence.json').write_text(json.dumps(evidence, indent=2, default=str))
        print(json.dumps({'output':str(out), 'status':summary.status,'ui_results':len(window._last_strategy_rows),
                          'fixed_stops':list(fixed),'posterior_rows':window.model_table.rowCount()}))
    finally:
        window.close()
        app.processEvents()


if __name__ == '__main__':
    main()
