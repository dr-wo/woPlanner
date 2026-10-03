# Live Race MC compound-offset reference contract

This correctness repair is limited to the live/replay resolver where latest Race
MC estimates meet resolved pre-race/manual baseline values. No model tuning,
sampling, likelihood, weighting, posterior aggregation, tyre-age, fuel, track,
clean-lap selection or MC-default changes are included.

## Production path and contract

`run_live_retro_model` emits offsets in its fitting reference (normally HARD).
`ResolvedTyreInputs.strategy_overrides` supplies MEDIUM-relative baseline values.
`OfflineAnalysisService.strategy_assumptions` previously overlaid these directly,
so retained compounds acquired incorrect differences after later rebasing.
`effective_tyre_inputs` and woWeekend's live MC replay use this resolver.

The merge now accepts two coherent additive dry-compound vectors in seconds:

- Align the incoming vector on the first shared updated compound in MEDIUM,
  HARD, SOFT order, then replace only updated entries. Unobserved fitting
  references are not synthesized. Untouched-to-pivot differences survive.
- Incoming contrasts among updated compounds replace their old contrasts. The
  pivot is a physical update policy, not merely a coordinate choice: given old
  M=0, H=0.4, S=-0.5 and new M=0, H=0.7, MEDIUM anchoring preserves S-M=-0.5
  while S-H becomes -1.2. HARD anchoring would instead preserve S-H=-0.9.
- Normalize the whole result to MEDIUM=0, otherwise HARD=0, otherwise SOFT=0.
  This final common shift is coordinate-only. A complete live fit retains every
  incoming physical contrast; a one-compound update supplies no new contrast.
- Reject disconnected nonempty vectors, unsupported compounds and nonfinite
  offsets. Before existing model-estimate aggregation, reject incompatible units
  or conflicting declared references. HARD/MEDIUM/SOFT/SUM_ZERO are supported;
  coherent in-memory summaries without metadata retain their existing support.
- Manual override OFF uses the live merge; ON returns the event-specific resolved
  baseline unchanged. Saved controls/filters and subsequent MC request handling
  remain unchanged. Actual model-estimate weights remain unchanged.

## Validation after scope reduction

Original base: `73f42aa9e82a3cd23c87b2f657b929ebcecc8040`.
Scope-reduction starting head: `e4d953310f21472257070ec39e7bc5db07f34b5a`.

The R13/lap-25 evidence has baseline H=0.3337454833569559, M=0,
S=-0.31964500240628674, and live H=0, M=-0.6083795692887073.
Executing the original service gives S-M=+0.28873456688242055; the reduced fix
returns -0.31964500240628674, eliminating 0.6083795692887073 s/lap.
All 14 R&D origins / 42 compound cells agree with the expected coherent merge
within 5.551115123125783e-17 seconds. This is an arithmetic reconstruction, not a
claim of realised race savings or forecast MAE improvement.

Evidence: `woStrategy/research/race_mc_fresh_20261002/data/data_path_reference_merge/merged_offsets.csv`,
SHA-256 `b0f8171d5ff807ba7435ce70d7b556eb4b00757c604074d235170f7ae1e8f66c`.
The read-only original/current comparison and AST scope checks are retained at
`/private/tmp/compound-offset-reduced-validation.py` with output in the matching
`.json` file.

Tests use Python 3.11, the reduced Planner worktree first on PYTHONPATH, the
isolated woStrategy source at `8d2992c5e9e30fde0a7cb8e14867ed727d2c258e`, and local
woData/woWeekend dependencies. The latter have pre-existing local changes and
were not modified by this repair. Use `PYTHONDONTWRITEBYTECODE=1`,
`QT_QPA_PLATFORM=offscreen`, `MPLBACKEND=Agg`, `OPENBLAS_NUM_THREADS=1` and a writable
`MPLCONFIGDIR`; pytest cache was disabled.

- Planner: `tests/test_compound_offset_contract.py`, `tests/test_tyre_inputs.py`,
  `tests/test_tyre_inputs_gui.py`, `tests/test_analysis_service.py`: **139 passed**.
  Coverage includes observed/reference subsets, repeated updates, untouched
  compounds, explicit pivot policy, complete-fit pairwise costs and reconstructed
  lap predictions, disconnected live rejection, manual OFF/ON precedence, and a
  raw-model artifact round trip. One existing weighted-mean fixture's offset unit
  is corrected from `s/lap` to `s`; its aggregation assertion is unchanged.
- Strategy: exact strategy search, live-retro, race-specific, pre-race, joint
  pre-race and race-performance weight-prediction tests: **28 passed**.
- Weekend: `tests/test_live_mc_replay.py`: **5 passed**.

## Scope and compatibility

GUI, saved control/filter restoration, CSV loading, history application, snapshot
serialization/hashes and general `build_strategy_model` overrides are restored
to the original base. Their compatibility tests and resolved-export metadata
were removed from this branch. Only the live resolver consumes the new helper.

Raw model artifacts keep their fitting coordinates and existing schema. No
persisted values are migrated or reinterpreted. Historical resolved artifacts can
still contain previously mixed differences; this scoped repair does not repair
or block their reuse. Multi-file CSV coordinate compatibility remains a separate
follow-up. New live merges correct reference arithmetic without claiming that
observed compound differences are statistically identified.
