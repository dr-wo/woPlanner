# Track Map reconciliation decisions

Base: woPlanner b303a11; source: integration 44b13b7. No Team Mode port.

| Decision | Leftovers | Reason |
| --- | --- | --- |
| KEEP | main map/widget/mapper/telemetry, map/live GUI/R13 replay tests | Already ported by d52f722; preserve subsequent timing and retention repairs. |
| PORT | LIVE_TRACK_MAP.md and decision record | Rewrite for independent SC/VSC and current main; remove stale branch requirements. |
| PORT | render_r13_track_map_evidence.py | Config-copy isolation, current API, SC-only and independent VSC assertions. |
| PORT | validate_r13_integration_evidence.py as validate_r13_track_map_evidence.py | Track-only LAW pit, reference sensitivity and Max replay evidence; current visibility API. |
| PORT | fresh Track Map screenshots/JSON, Dutch evidence JSON | Regenerate main evidence; retain Dutch JSON explicitly as historical provenance. |
| DROP | old SC-derived combined display and old window tests | Superseded by independent SC/VSC contract; equal values remain independent too. |
| DROP | production replay read-only constructor/CLI/draft disclosure changes | Not required for map; collector isolation solves evidence needs without unrelated UI work. |
| DROP | old integrated screenshots/reports and LIVE_STRATEGY_TRACK_MAP_INTEGRATION.md | Contain Team acceptance and obsolete combined-window claims. |
| DROP from this candidate | Team code/tests/R12 tools/artifacts and cross-feature state test | Owned by the separate Team Mode work; no removal from source branch. |

Original Planner integration is not deletable solely on Track Map completion:
its Team production patch equals ad6d0f0, but integration-only cross-feature tests,
R12 validator changes and acceptance provenance must first be preserved or explicitly
retired by the Team owner. Track Map itself no longer needs that branch after this
candidate merges. No branch or worktree is deleted by this reconciliation.

Validation: 65 focused tests pass with a persistent QApplication and Agg plotting.
Both evidence collectors ran against the local R13 recording; LAW/Max gates are
true and reference sensitivity is ok. Offscreen captures were inspected at both
popup sizes; native visual acceptance was not rerun. Early plain pytest runs
encountered Qt lifecycle crashes; the persistent QApplication harness resolved
them without changing production code.
