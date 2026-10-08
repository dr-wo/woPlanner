# DBG-1: explicit pre-race report tyre import

**DBG-1: CLOSED**

R16's report/Planner stop-count disagreement came from different effective tyre
inputs, not the optimizer. woWeekend applied run-local engineering overrides;
Planner used canonical prediction unless its own manual inputs existed.

## Contract and workflow

In Planner **Tyre Inputs**, choose Global scope and **Import tyre inputs from
pre-race run…**, select a specific run ID and confirm the selection. The selector
shows S/M/H degradation for each eligible run. No run is adopted on discovery,
startup, refresh or report generation. The operation replaces all five Global
manual coordinates: three degradations and HARD/SOFT performance versus MEDIUM.

The reader uses `wodata.weekend_runs.WeekendRunStore` and the exact selected
`tyre_prediction.effective.json`. It checks run/manifest event and run identities,
workflow, race session, SUCCESS status, successful effective-input and primary
strategy components, complete finite values, nonnegative degradation, zero MEDIUM
performance reference, and existing `.artifacts.json` SHA256 records. Frozen
`config.resolved.json` supplies identity only; it never supplies tyre values.
Incomplete, failed, partial, corrupt, or wrong-event/session runs are excluded.
Import revalidates after selection and rejects an event/scope change while the
selector is open.

The existing event-specific `manual_tyre_inputs_v1.json` stores numerical values
and one optional `import_origin` object. All copied values resolve as `manual`,
including fields that were automatic in the report. Origin retains per-field
historical source, value, event/session, run ID and effective input SHA256.
Changing/clearing a field removes its imported attribution without discarding
other fields' origins. Clearing all restores normal canonical/fallback resolution.
Restart and other-window refresh/search use existing manual persistence semantics.
Import clears conflicting window-local Used edits and refreshes strategy inputs.

Tyre Inputs displays Selected/Manual/Default values and report field sources.
Both tabs identify the persisted imported baseline. Strategy Used values/sources
remain the authority for temporary edits and live overlays. Search snapshots keep
actual numerical inputs and retain origin only for matching effective manual
fields; later edits cannot rewrite historical snapshots.

Global imports do not inherit into Team. The import action is unavailable in Team
and live operational windows. Existing live resolution still overlays evidence
unless Manual Override is active; sensitivity snapshots preserve base and final
vectors under the existing transform contract.

There is deliberately no automatic propagation, shared event revision, latest
pointer authority, or new configuration framework. Imports never write woWeekend
runs/configuration or canonical predictions.

## R16 immutable evidence and parity

Source event/session: **2026-16 / R**; workflow: `race_preparation`.
Selected run: `20261003T225359.171082Z-f370e12cf1`.

Source relative to woData:
`woweekend/runs/schema_v1/event=2026-16/workflow=race_preparation/runs/20261003T225359.171082Z-f370e12cf1/tyre_prediction.effective.json`

Existing effective input SHA256:
`19c491735e0c347c4b3dfac0e13f4698b0a5f6d8c3be31706bd42bac2dfdf5ce`.

| Planner state | SOFT | MEDIUM | HARD | Stops |
|---|---:|---:|---:|---:|
| Canonical before import | 0.20460105651325672 | 0.14407032425651345 | 0.09141717826254332 | 2 |
| Explicit import | 0.062 | 0.039 | 0.033 | 1 |
| Cleared | 0.20460105651325672 | 0.14407032425651345 | 0.09141717826254332 | 2 |

Effective performance deltas are S = -0.31214405241230747, M = 0,
H = 0.2752115388974766. Report degradation origins are `manual_override`;
performance origins are `historical_baseline`.

Matched saved conditions: 56 laps, green pit loss 23.7 s, maximum four stops,
all starting compounds and minimum two distinct dry compounds. Production
`OfflineAnalysisService.generate_continuations` reproduces the report's full
**M-(31)S** plan and cost **55.390398689692304**, not merely the stop count.
No optimizer replacement or test-specific selection shortcut is used.
Before import and after clearing, Planner selects H-(20)H-(41)M, cost
116.28708761935977, under the same conditions.

`tests/fixtures/dbg1_r16` contains byte-for-byte copies of the required historical
run artifacts and a separate saved canonical prediction. Tests operate in
isolated temporary data roots; real Planner manuals and historical runs are not
changed. The original artifact index is retained even though unrelated outputs
are not copied into this focused fixture.

## Verification and review

The new service/GUI suites cover canonical defaults, valid explicit import,
partial report overrides, exact historical values/sources/hash, restart/reopen,
clear/clear-all, wrong event/session, failure/partial completion, invalid numbers,
missing/corrupt artifacts, malformed directory isolation, multiple explicit runs,
no later-run propagation, Planner-only edits, conflicting temporary Used edits,
selector cancellation/event changes, search snapshot persistence, and Team/live
resolution. Existing regression coverage also verifies sensitivity replay.

Run the focused regression set with the workspace Python environment:

```sh
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m pytest -q \
  tests/test_weekend_tyre_import.py tests/test_weekend_tyre_import_gui.py \
  tests/test_tyre_inputs.py tests/test_tyre_inputs_gui.py \
  tests/test_analysis_service.py tests/test_live_analysis_boundary.py \
  tests/test_live_degradation_sensitivity.py tests/test_compound_offset_contract.py \
  tests/test_observed_undercut_gui.py
```

A fresh read-only review checked automatic propagation, immutable source selection,
wrong/stale events, field provenance, clearing, Team/live boundaries and genuine
R16 parity. It found no remaining substantive issues; its independent run passed
all 22 new tests. The provenance label was clarified to distinguish the persisted
baseline from temporary/live effective inputs.
The final focused regression command above passed **231 tests**. Offscreen visual
inspection confirmed the selector action, effective/source/default columns and
persisted run provenance display. `git diff --check` passed. A baseline hash audit
of 37 pre-existing modified tracked files found only the intended additive GUI
edit; the other 36 remained byte-for-byte unchanged. woWeekend, woStrategy and
woData had no new git-status entries.

## Report visibility and limitations

woWeekend's existing report bundle instruction already requires a leading
“Tyre inputs used for strategy” section with effective values, exact source
families and explicit manual overrides. The saved R16 bundle has this instruction;
the delivered R13 example demonstrates the table. No finalized R16 narrative was
found locally, so its delivered presentation was not independently verified.
No report redesign or regeneration was necessary.

Only finalized SUCCESS race-preparation runs with existing integrity records and
complete required artifacts are importable. Partial runs, sprint sessions and
copied bundles outside the immutable run store are intentionally unsupported.
Import aligns tyre assumptions only. Equality of the entire strategy scenario
still depends on matching other inputs such as pit losses, start state, rules and
model context.

All production edits are in woPlanner. Closure stages only DBG-1 changes,
including its GUI hunks; unrelated workspace changes and historical artifacts
remain untouched. The staged version uses existing live-window identification
without requiring the separate, uncommitted live controls or sensitivity module.
Both DBG-1 suites passed again against an isolated export of the staged files:
**22 passed**. No unrelated suites were rerun for closure.
