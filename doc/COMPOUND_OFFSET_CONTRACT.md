# Compound-offset reference contract

Correctness repair, 2026-10-03. No fitting, sampling, likelihood, weighting,
aggregation, tyre-age, fuel, track, selection, MC-default or forecast-model
changes. No R&D pace model was promoted and no forecast MAE was used.

## Production defect and paths

`OfflineAnalysisService.strategy_assumptions` previously assigned observed
`compound_delta` coordinates directly into the retained baseline. The baseline
from `ResolvedTyreInputs.strategy_overrides` is MEDIUM-relative, while
`run_live_retro_model` writes offsets with the configured fitting reference
(default HARD). Subtracting MEDIUM after that overlay cannot recover the
original physical differences for retained compounds.

The production path is live-retro `ParameterEstimate` rows -> `ModelSummary` ->
`effective_tyre_inputs` / `strategy_assumptions` -> GUI strategy overrides ->
`build_strategy_model` -> exact strategy search. woWeekend's live MC replay also
calls `strategy_assumptions`; it receives the repair without a workflow change.

The same arithmetic defect existed in partial strategy-model overrides and in
CSV/history GUI imports, which overlaid subset-relative fastest-pace values.
These now use the same merge helper. CSVs or summary rows declaring incompatible
references are refused before the existing weighted aggregation; importing
coherent files separately remains supported. Aggregation weights are unchanged.

## Contract: shared_compound_alignment_v1

- Each input is one coherent additive dry-compound offset vector in seconds.
  Its fitting reference can be HARD, MEDIUM, SOFT or SUM_ZERO, or can be omitted
  by legacy callers. An omitted label means a caller-supplied coherent vector,
  not an inferred HARD/MEDIUM zero. An unobserved reference is never synthesized.
- Align on the first shared **observed/updated** compound in MEDIUM, HARD, SOFT
  order. Shift the incoming vector by retained[pivot] - incoming[pivot], then
  replace only incoming entries. Untouched-to-pivot differences are preserved;
  incoming contrasts among updated compounds replace their old contrasts.
  Pivot preference is a physical update policy, not just a choice of coordinates:
  with retained M=0, H=0.4, S=-0.5 and incoming M=0, H=0.7, MEDIUM anchoring
  preserves S-M=-0.5 and changes S-H to -1.2. HARD anchoring would instead
  preserve S-H=-0.9 and change S-M to -0.8. One observed compound provides no
  new contrast. Only the final whole-vector normalization is coordinate-only.
- Rebase the result to MEDIUM = 0, otherwise HARD = 0, otherwise SOFT = 0.
  This is reference selection only; degradation ordering and sampling are untouched.
- Empty updates retain contrasts; full updates replace all contrasts. Nonempty
  disconnected vectors, invalid compounds, nonfinite offsets, incompatible units
  and conflicting declared source references are refused.
- The GUI may display a common fastest-compound shift of this vector; it always
  shifts the whole retained state together. Manual individual edits continue to
  use the explicitly displayed reference.

## Concrete before/after evidence

Source: `woStrategy/research/race_mc_fresh_20261002/data/data_path_reference_merge/merged_offsets.csv`.
SHA-256: `b0f8171d5ff807ba7435ce70d7b556eb4b00757c604074d235170f7ae1e8f66c`.
R13/lap 25 has a pre-cutoff saved baseline (unlike the R11 arithmetic-only cases).

| Coordinate | Baseline, MEDIUM-relative | Live, HARD-relative |
| --- | ---: | ---: |
| HARD | 0.3337454833569559 | 0 |
| MEDIUM | 0 | -0.6083795692887073 |
| SOFT | -0.31964500240628674 | unobserved |

The new `test_rd_r13_l25_unobserved_soft_keeps_its_physical_difference` was run
against the unmodified production service at base commit
`73f42aa9e82a3cd23c87b2f657b929ebcecc8040`: **1 failed**,
actual SOFT-MEDIUM `+0.28873456688242055`, expected `-0.31964500240628674`.
It passes after the repair. HARD-MEDIUM remains `0.6083795692887073`.

Using the R&D's unchanged degradation values in the real `stint_cost` function,
the ten-lap SOFT-minus-MEDIUM cost (starting age zero) changes from
`+3.9813865634146524` to `-2.102409129472422` seconds. The eliminated discrepancy
is `6.083795692887074` seconds, exactly ten times the reference error.
Replaying all 14 affected R&D origins / 42 compound cells through the repaired
production resolver agrees with the R&D coherent merge to `5.56e-17` seconds.
These are arithmetic reconstructions, not realised race savings.

## Persistence and compatibility

Read-only inspection found mixed resolved history in four saved plots, including
R15 lap 6: SOFT `-0.32380563412992786` (retained baseline), MEDIUM
`-0.6001978479198112` (race evidence), HARD `0.3258440819351384` (retained baseline).
The R15 file is `woData/wostrategy/weekend_model/schema_v1/year=2026/round=15/saved_history_plots/Live_2026_R15_2026-09-26_11-59-24/plot.json`,
SHA-256 `a56f205bd21123e073fd88b74548827e1259ad80dae05fe4421b44ec146fd6c9`.

- Raw canonical model snapshots retain fitting-reference metadata and contain
  raw model values, not the faulty overlay. Their schema and values are unchanged;
  tests round-trip them through the real artifact writer/reader and new resolver.
- Resolved history plots and exact tyre-input snapshots can already encode the
  erroneous differences. Subtracting a new zero cannot repair those differences.
  The inspected root has 8 saved plots, 64 tyre-input snapshots, 4 named snapshots
  and 6 dialog-state files; this does not establish that every artifact is wrong.
- New resolved history rows carry the contract and reference; new exact tyre-input
  snapshots carry the contract in both payload and content-hash input. Schema v1
  remains readable; the new metadata is additive.
- Legacy saved plots remain viewable unchanged, but their unverified compound
  offsets cannot be applied to a new strategy calculation. The GUI explains that
  recalculation from original model/baseline inputs is required. Even an old plot
  that happens to be coherent is unverified without provenance.
- Old strategy results/snapshots remain historical records. Dialog state stores
  controls/results and already ignores old dialog-local tyre copies. Manual
  values remain explicit MEDIUM contrasts. Nothing is migrated, reinterpreted or
  rewritten automatically; there is no reliable way to infer past manual intent.

Partial API overrides now denote a coherent vector: a lone compound value cannot
change a physical contrast. Supply a shared compound plus the desired contrast.
Mixed-reference CSV batches are rejected rather than guessed or reweighted.

CSV provenance is checked before aggregation in all three supported layouts.
Only compound-offset rows contribute offset references; blank/pandas-missing
reference cells are absent metadata, including unrelated degradation rows.
Raw canonical and race-review exports may declare their fitting reference
without the resolved contract. Resolved exports (identified by the
`compound_delta_source` or `compound_offset_contract` column) must carry the
current contract on every offset row, using the same contract check as saved
history. A reference label alone does not certify legacy resolved values.
Anonymous external offsets with neither a reference nor a current contract are
rejected; internal caller-supplied coherent vectors keep their metadata-free API.
No legacy values are rebased into trust or automatically repaired.

## Validation

Python 3.11: `/Users/zhxutong/dr-wo/.venv/bin/python`. PYTHONPATH puts this Planner
worktree first, then the isolated woStrategy source at
`8d2992c5e9e30fde0a7cb8e14867ed727d2c258e`, then local woData and woWeekend sources.
Set `PYTHONDONTWRITEBYTECODE=1`, `QT_QPA_PLATFORM=offscreen`,
`OPENBLAS_NUM_THREADS=1`, `MPLBACKEND=Agg`, and a writable `MPLCONFIGDIR`.

- Planner: `pytest tests/test_compound_offset_contract.py tests/test_tyre_inputs.py tests/test_tyre_inputs_gui.py tests/test_analysis_service.py -q`: **153 passed**.
  Includes all original 137 cases plus 16 import/provenance/pivot regressions:
  real-shaped blank/NaN canonical metadata; R15/lap-6 legacy resolved CSVs with
  and without a reference; current contract CSVs with blank/NaN/valid references;
  anonymous external versus internal coherent-vector semantics; invalid/obsolete
  declarations; combined/review CSV provenance; and conflicting pivot contrasts.
- woStrategy: `pytest tests/test_exact_strategy_search.py tests/test_live_retro_performance_model.py tests/test_race_specific_performance_model.py tests/test_pre_race_performance_model.py tests/test_joint_pre_race_performance_model.py tests/test_race_performance_weight_predict.py -q`: **28 passed**.
- woWeekend: `pytest tests/test_live_mc_replay.py -q`: **5 passed**.
- The latter two runs initially aborted in macOS's interactive Matplotlib backend;
  both passed with Agg. The Strategy run reported only an unwritable pytest-cache
  warning. No production code was changed for these environment limitations.
- An existing test fixture incorrectly labelled a compound offset `s/lap`; its
  fixture unit was corrected to `s`, preserving the weighted-mean assertion.

The import follow-up was reproduced against preceding head `baf5e865` and the
fixed service. Real R13/R15 aggregate CSVs previously raised unsupported `NAN`;
both now load two compound rows with reference HARD. CSV-encoding the actual
R15/lap-6 saved rows previously accepted S-M=+0.27639221378988343; it now raises
unverified imported compound offsets before aggregation. The R13/lap-25 result
remains -0.31964500240628674, and all 14 R&D origins / 42 cells retain the
5.56e-17 maximum discrepancy from the expected coherent merge. Input hashes and
the read-only reproduction runner are retained outside the repository at
`/private/tmp/compound-offset-import-fix-validation.json` and the matching `.py`.

woData HEAD was `df04eef9235b77a4d64382ec93178fa966e8be81` and woWeekend HEAD was
`20dce7b59a4affc00ddc86ebfa07639923793512`. Both had pre-existing local changes;
these dependencies were not claimed to be clean or modified by this repair.
The Planner primary checkout also had unrelated work; it was not edited.
