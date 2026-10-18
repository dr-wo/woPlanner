# Q17-01/02 — C3 Epoch-Witness Structural Correction

Verdict: **READY FOR FINAL INDEPENDENT REVIEW**.

The confirmed gap-reaffirmation defect is corrected in the isolated candidate.
All three required reproductions pass through reducer and controller/service in
both supported source modes, with and without duplicate history. The frozen
runtime passes 832 Planner Qualifying cases, 286 selected Data controls, complete
R16/R17 checks and all eight accepted Q17-03 historical paths.

This is an implementation handover for independent review. The candidate remains
uncommitted under `/private/tmp/q17-01-02-final-review-20261011/`.
Evidence: `evidence/c3_epoch_witness/`. Earlier reports and evidence are retained.

## A. Witness contract

The pre-edit checkpoint is `OWNERSHIP_CONTRACT_CHECKPOINT.md`. A part label can
identify a finish when it identifies the relevant GREEN epoch, when an explicit
source range spans that finish, or when an existing real transition/preparation
witness establishes that epoch. A later-part upper bound restricts a candidate;
it cannot independently identify an otherwise unsupported epoch.

The old rule used `last > lower` plus the mere existence of a previous closure.
Thus Q2 reaffirmation at 370, after Q2 finish at 350 but before GREEN at 400, could
wrongly identify the later finish at 600. A label in that gap describes the completed part.
It now requires additional evidence before it can identify the later epoch.

Q3 metadata at 390 legitimately identifies preparation for the next part before
GREEN at 400 because it is an explicit part advance. Q2 reaffirmation at 299.500 after
GREEN at 299 still identifies the active Q2 epoch. Supported same-part restarts use
an identifying label in the restarted epoch or independently established
preparation. GREEN itself is not treated as a part transition.

GREEN precision is retained. A label inside a whole-second GREEN interval is not
assumed to be after GREEN. The existing original-publication ordering witness is
preserved when publication falls inside the printed interval. Receipt never
supplies historical chronology. Missing or insufficient identity stays provisional;
genuinely certified `(source index, flag)` ownership remains immutable.

## B. Implementation

Incremental production change: `woData/src/wodata/live/quali_reducer.py`.
New tests: `woPlanner/tests/test_quali_epoch_witness_scope.py` (62 cases).
No earlier tests, fixtures or expected historical values were edited.

- `_certify_race_control_owner()` requires positive epoch eligibility before the
  common certification gate can accept a candidate, including bounded candidates.
- `_race_control_epoch_facts()` retains the preceding GREEN precision endpoint.
- `_race_control_event_time()` preserves later GREEN chronology alongside retained
  current-part owned finishes, so a first post-restart clock cannot resurrect an
  earlier finish. Unowned superseded timestamp variants are excluded.
- `_race_control_item_time()` centralizes the existing own-Utc/precision extraction;
  `_apply_race_control()`, epoch facts and presentation use the same canonical
  source item. Legacy original-publication fallback remains unchanged.

This extends the existing witness/identity/provisional machinery. There is one
certification gate, no event-specific index or timestamp, no duration tolerance,
and no change to displayed-part semantics. `scope_audit.json` verifies 34 existing
methods remain byte-identical, including the entire `_apply_message()` method,
`_set_part()` and reconciliation. Planner service is byte-identical.

## C. Three reproductions

`reproductions.json` and its reproducible runner compare reviewed and corrected
implementations. Each row is exercised through reducer and controller/service,
in live and archive modes, with and without duplicate history. Live checks include
trusted monotonic receipt timestamps; source dates remain unchanged on redelivery.

| Scenario | Before delayed metadata / transition | After metadata / transition |
|---|---|---|
| A: Q2 finish350, gap reaffirm370, GREEN400, finish600, clock620, delayed Q3 publication390 | Earlier finish350 owned Q2; finish600 provisional; displayed Q2 finished | Finish600 owned Q3; Q3 finished |
| B: GREEN299, Q2 reaffirm299.500, finish300, activity300.200 | Finish300 owned Q2 without redelivery; Q2 finished | Explicit Q3 begins unfinished; its subsequent ordinary lap remains ordinary |
| C: Delayed Q3 publication390 without gap reaffirmation | Finish600 provisional despite fresh unlabelled clock620 | Finish600 owned Q3; Q3 finished |

Duplicate/no-duplicate results agree in all paths. Scenario B preserves the genuine
Q2 `FINAL_LAP_COMPLETED` classification and ordinary Q3 completion. Six delivery
schedules also converge on ownership and finish state, then on common ordinary
and final-lap classifications through a supported Q3 restart. Intermediate states
are compared only where the necessary source evidence is available.

## D. Adversarial validation

The exact gap test was demonstrated before editing. The final 62-case module
produces **24 failures / 38 preservation passes** against the reviewed defective
reducer; all 62 pass after correction. The broader focused group passes 185 cases.
These groups overlap the complete collection and are not additional distinct tests.

The matrix covers before-finish, gap and after-GREEN reaffirmations; pre-/post-GREEN
new-part metadata; delayed and missing metadata; supported/unsupported same-part
restarts; preparation; multiple closed epochs; Q1/Q2/Q3 histories; stale/fresh
activity; duplicates; precision and timestamp enrichment. Earlier retained tests
preserve distinct same-second events, shortened finishes and valid clock resets.

The focused independent read-only adversary found coarse-GREEN ambiguity and
first-clock-after-restart presentation issues. Both were corrected. The initial
GREEN-retention change also exposed a superseded legacy-GREEN variant; canonical
own-Utc and precision filtering corrected that introduced intermediate regression.
Details and retained intermediate logs are in `ADVERSARIAL_CORRECTIONS.md`.

The final independent recheck passed 90 cases and reconstructed those counterexamples
in both source modes on reducer hash `5fa63156b82add28e0b9c69ded36631b362071c198027fd93cb681b749092bdd`.
No remaining confirmed material failure was found within that bounded assessment.

Evidence limitations remain: missing original source dates or part identity cannot
be recovered from receipts, and whole-second precision cannot establish an exact
physical occurrence. Ambiguous events remain provisional. These checks do not
claim live network or live GUI acceptance.

## E. Historical and collection regression

Final frozen-source tests: **832 Planner Qualifying cases across 52 modules**,
partitioned without overlap into 791 fast, 40 recorded and one schedule case.
All **286 selected Data controls across 18 modules** pass. No final failures or
skips remain. Broader unrelated Data tests are not represented as validated.

R17 replays all 8,134 original messages: 16 Q2 participants; P10 target 92.602;
35 ordinary Q2 completions; no premature flags; eight Q2 final laps; ten Q3
participants; P1 target 91.373; seven Q3 final laps. Input SHA-256:
`8d084328530605780c74035c13d6ad339e0a5e6270f7c535c6761e4683012388`.

R16 compares all 16,496 messages to accepted Q17-03: 77 completed events and
362,868 projection rows unchanged, including absent outputs. Cutoffs 97.883/96.814
and explicit transitions 7525/12465 are preserved. Input SHA-256:
`b353aa941dcd21112f48c38b2bd558a5e1d89af799555109e21af6dfa354c71b`.

All eight fresh pipeline replays match accepted Q17-03 and the immediately reviewed
candidate across all 22 comparison fields. Every recording hash and replay start/end
source stamp was verified. No historical output or expectation changed.

| Path | Messages | Position rows | Complete spans | Pace candidates | Grace updates |
|---|---:|---:|---:|---:|---:|
| R12_archive_direct | 21,621 | 348,480 | 0 | 0 | 0 |
| R13_archive_direct | 19,076 | 378,818 | 52 | 87 | 69 |
| R14_live_direct | 18,462 | 302,742 | 44 | 72 | 4 |
| R14_live_player | 18,462 | 302,742 | 44 | 72 | 4 |
| R16_live_copy_direct | 16,496 | 363,374 | 24 | 62 | 10 |
| R16_live_copy_player | 16,496 | 363,374 | 24 | 62 | 10 |
| R17_live_direct | 8,134 | 154,924 | 21 | 33 | 2 |
| R17_live_player | 8,134 | 154,924 | 21 | 33 | 2 |

Causal violations and ordinary traffic-enabled/disabled mismatches remain zero.
Calibration, acquisition/receipt provenance, protected history, normal/grace/hard-lock
timing, warnings and replay outputs remain accepted-baseline equivalent. The existing
R17 direct/Player bootstrap-clock difference remains unchanged. Zero supported fits
and eligible observations are preservation results, not predictive-accuracy evidence.

## F. Source, Git and handover

Data and Planner remain on `codex/q17-01-02-final-review-20261011`, with HEADs:

- Data: `5d1f67b48f79343965cc1fb2b0cfa02c2e5d643b`.
- Planner: `d607c1ea1ac1a5bbb7fff9b7595156b81b7cd4bc`.

| Artifact | SHA-256 |
|---|---|
| Reviewed reducer | `3afad17f46c07e3b0ce38b6b5b4925f8325fe0ee58d037e4350f16fda4f78061` |
| Corrected reducer | `5fa63156b82add28e0b9c69ded36631b362071c198027fd93cb681b749092bdd` |
| Unchanged Planner service | `f6924fee438138d2d34e87280b67a74193dd5bfb1a62a1c306a9a1dc7266864c` |
| New 62-case module | `5a1e9f42643dc8d5c229f9695f7772f81c3db9a86b04d92fcf16cf896c655eff` |
| Final 383-file runtime manifest | `dda43a885ebf15a2ea151348d8f43a0345d06342aba40c83e1ca06642dd1222a` |

`epoch_witness.patch` contains exactly the reducer and new test module relative to
the exact reviewed candidate, not Git HEAD. `cumulative_accepted_q17_03.patch`
contains eight runtime/test files relative to the accepted dirty Q17-03 snapshot.
Both patches were independently reconstructed in memory; all output hashes match.
This report and new evidence are additional handover artifacts.

All 378 accepted runtime files, 280 prior evidence/documents,
and 1171 captured shared source/test/script files remain unchanged. Shared
Git status, branches and HEADs match this task's preflight. All 383 candidate
runtime files remain frozen. Isolated imports and unchanged package versions were
verified before editing; each pipeline replay verifies actual imported module paths.
The existing virtual environment was reused without installs or upgrades.

The candidate still requires its paired Data/Planner changes and accepted Strategy
dependency snapshot. Preserve the exact candidate, manifests, patches and evidence
in a durable integration location after review and before temporary cleanup. No
relocation was performed during this task. No commit, merge, cherry-pick, reset,
stash, clean, branch switch or push was performed. Promotion is not integrated.

## G. Final verdict

**READY FOR FINAL INDEPENDENT REVIEW**.

The bounded correction, competing cases, adversarial checks and full preservation
validation are complete. Independent acceptance and any later integration remain
separate steps.
