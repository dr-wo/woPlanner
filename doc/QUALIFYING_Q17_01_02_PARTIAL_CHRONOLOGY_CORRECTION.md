# Q17-01/02 — Partial Epoch Chronology Correction

Verdict: **READY FOR FINAL INDEPENDENT REVIEW**.

The known undated GREEN P1 and the credible precision/enrichment variants found
in adversarial validation are corrected. The restored isolated candidate passes
888 Planner Qualifying cases, 286 selected Data controls,
complete R16/R17 checks and all eight accepted Q17-03 recording paths.

Candidate: `/private/tmp/q17-01-02-final-review-20261011/`.
Evidence: `evidence/c3_partial_chronology/`.
No commits, merges, branch switches, resets, stashes, cherry-picks or pushes.
No shared source overwrites or Promotion integration.

## A. Partial-chronology contract

| Source evidence | Representation and certification meaning |
|---|---|
| No GREEN exists | No GREEN boundary is asserted. Existing positive ownership witnesses still apply. |
| Reliably dated GREEN | Canonical source item Utc and precision enter the epoch facts. |
| Known undated GREEN | Its original `(index, flag)` remains in messages, original-publication storage and the explicit undated set. Epoch facts retain it as unresolved evidence. |
| Coarse GREEN or closure | Its half-open source precision interval is retained. An original publication within the printed interval retains the existing narrowing witness. |
| Existing event enriched | The same identity receives canonical Utc/precision; chronology cache invalidates, unresolved evidence is revisited, and provisional ownership reconciles. |
| Genuinely unknown relative order | Incomparable coordinates and overlapping precision intervals remain unresolved. Overlapping predecessor intervals retain their full possible endpoint frontier. |

Source indices identify source entries and preserve the established numeric
same-printed-time source sequence witness. Neither numeric index alone across
undated entries nor nonnumeric first-ingress order supplies a general chronology.
Receipt time never supplies event occurrence time. A later dated redelivery cannot
replace a known unknown original publication with a fabricated historical origin.

Certification is through the existing single `_certify_race_control_owner` gate.
It requires one supported owner whose evidence survives all placements of the
already-known unresolved boundaries. An undated GREEN can invalidate an ordinary
pre-finish reaffirmation; an undated closure can also invalidate transition or
preparation evidence. These constraints cover the target finish's entire precision
interval, not only its printed lower bound.

A reliable same-part identification span covering the finish remains independent
proof. A real transition/preparation after every possible preceding closure may
identify the part without knowing exactly where a GREEN occurred. Thus unrelated
undated history does not automatically veto independently supported finishes.
When source evidence cannot establish exclusion or a unique owner, ownership stays
provisional. The existing provisional display behavior is preserved.

Once legitimately certified, ownership remains immutable. The invariant applies
to refinement of already-known source evidence; this does not invent absent future
events or permit arbitrary migration of certified identities.

## B. Implementation and resources

Incremental production change: `woData/src/wodata/live/quali_reducer.py`.
Added tests: `woPlanner/tests/test_quali_partial_chronology.py` (56 cases).
The patch against the exact reviewed reducer is `partial_chronology.patch`.
Both patch outputs were independently reconstructed in memory and matched their
actual file hashes (`patch_verification.json`).

- `_RaceControlEpoch` explicitly holds the finish, GREEN, closure, their precision
  endpoints, and unresolved source identities.
- `reset()` initializes the session's undated identity set.
- `_race_control_epoch_facts()` retains unknown/comparability uncertainty, adds
  interval-overlap relationships, and accumulates possible predecessor endpoints.
- `_certify_race_control_owner()` applies those facts to the existing positive
  identification, transition, preparation and source-span checks. Half-open interval
  endpoints are respected without arbitrary tolerances.
- `_apply_race_control()` records original publication identity even when unknown,
  prevents later envelopes from dating an existing unknown origin, and invalidates
  cached chronology on origin/time/flag changes.

The entire `_apply_message()`, `_set_part()`, ordinary ownership reconciliation,
clock acceptance, participation and Planner service remain byte-identical to the
reviewed candidate. No final-lap colour, Track Correction, projection, calibration,
causal speed, receipt/acquisition, warning, rendering or Promotion code changed.
`source_audit.json` identifies every changed and preserved method.

The new undated set has one entry per source identity and is cleared on reset or
resolved by enrichment. Cached per-finish uncertainty refers only to retained source
history; it is not expanded into chronological permutations. Reconstruction scans
retained boundary pairs, so worst-case build/cache size can be quadratic in retained
boundary count. It is cached between relevant source changes; ordinary clocks and
identical snapshots add no evidence versions. Existing session-history retention
and certified-owner storage are unchanged. The 100-redelivery test verifies stable
identity, flag, publication, order and provisional collection sizes.

## C. Reproduction and adversarial evidence

Before editing, the independent reviewer runner preserved A/B/C in all 24 paths
and reproduced the new unknown-GREEN failure in all eight source/path/duplicate
combinations. The exact original sequence is retained in `reproduce_before.py`,
`abc.json` and `green_enrichment.json`.

After correction, the same sequence leaves finish600 uncertified before enrichment
and delayed Q3 identification, then assigns it to Q3 with normal finish true.
Its final canonical messages, original publications and business state agree with
the complete initial snapshot. `reproduce_after.py`, `abc_after.json` and
`green_enrichment_after.json` contain the paired results.

The six metamorphic schedules run in both source modes through reducer and
controller/service: complete initial history; enrichment before clock; enrichment
after clock; metadata before enrichment; metadata before snapshot; and duplicate
partial/complete snapshots around enrichment and metadata. The latter schedules
also cover metadata after enrichment. Each reaches the same Q2/Q3 certified owners,
finish state, ordinary lap and genuine final-lap classifications, and downstream
driver results after a common supported Q3 restart. No equality of insufficiently
informed intermediate states is required.

Previously accepted gap reaffirmation, GREEN299/Q2 reaffirmation299.500/finish300/
activity300.200, delayed Q3 publication390, valid chronological resets, shortened
finishes, stale clocks, same-second identities, historical Q1/Q2/Q3 and reconnection
controls remain in the unmodified existing test collection.

New adjacent cases cover permanent unknown timestamps, multiple unknown GREENs,
same-part restart, next-part start, unknown closures, missing outer publication,
dated redelivery of unknown origins, enrichment-order permutations, cache invalidation,
source-span independence and duplicate memory invariance.

The read-only adversarial subagent found three additional mechanisms beyond the
reported unknown GREEN: closure precision crossing a transition, target interval
overlap with another boundary, and predecessor interval overlap losing a possible
endpoint. An unknown closure inside the target interval also required its proof
bound to cover the full interval. All are corrected in the same gate, with actual
source/path regressions and final independent rechecks. Final targeted group:
168 passed. The selected 20-case negative control against the original reviewed
reducer produces 18 failures and two preservation passes; current code passes all.
No remaining confirmed P1/P2 was found in the bounded adversarial assessment.

## D. Regression

Final frozen runtime: 888 Planner cases = 847 fast + 40 recorded + one
schedule case, plus 286 selected Data cases. No final failures or skips. The
56 new cases are included in the fast count, not additional to the total.
Interrupted pre-final runs and the missing-fixture Data failure are retained
separately and are not counted as final validation. Restoring the original tracked
fixtures resolved those four environment failures without changing test assertions.

R17: all 8,134 messages; Q2 mid-session join with 16 participants; P10 92.602;
35 ordinary Q2 completions; no premature finish/flags; eight Q2 final laps;
correct Q3 identification with ten participants; P1 91.373; seven Q3 final laps.
The complete replay result equals the prior exact evidence. Input SHA-256:
`8d084328530605780c74035c13d6ad339e0a5e6270f7c535c6761e4683012388`.

R16: all 16,496 messages compared with accepted Q17-03; 77 completed-lap events
and 362,868 projection rows unchanged; cutoffs 97.883/96.814; explicit transitions
7525/12465 preserved. The full comparison report equals the earlier evidence.
Input SHA-256: `b353aa941dcd21112f48c38b2bd558a5e1d89af799555109e21af6dfa354c71b`.

All eight fresh Q17-03 paths match both accepted Q17-03 and the immediately reviewed
candidate across the same 22 recorded fields. Input and runtime start/end hashes
are verified for each path. Causal violations and ordinary traffic control mismatches
remain zero. Existing calibration, normal/grace/hard-lock timing, provenance,
protected history, warnings and business outputs remain baseline-equivalent.

| Path | Messages | Position rows | Complete spans | Pace candidates | Grace updates |
|---|---:|---:|---:|---:|---:|
| R12_archive_direct.json | 21,621 | 348,480 | 0 | 0 | 0 |
| R13_archive_direct.json | 19,076 | 378,818 | 52 | 87 | 69 |
| R14_live_direct.json | 18,462 | 302,742 | 44 | 72 | 4 |
| R14_live_player.json | 18,462 | 302,742 | 44 | 72 | 4 |
| R16_live_copy_direct.json | 16,496 | 363,374 | 24 | 62 | 10 |
| R16_live_copy_player.json | 16,496 | 363,374 | 24 | 62 | 10 |
| R17_live_direct.json | 8,134 | 154,924 | 21 | 33 | 2 |
| R17_live_player.json | 8,134 | 154,924 | 21 | 33 | 2 |

The recorded zero eligible observations/supported fits remain preservation evidence,
not predictive-accuracy evidence. No live network or live GUI acceptance is claimed.

## E. Recovery and source integrity

All 383 entries in the original reviewed runtime manifest were restored and verified
before editing. Six missing variants were reconstructed from the retained cumulative
patch and accepted Q17-03 base, with exact expected SHA-256 equality. Missing tracked
configuration/resources/fixtures were recovered from the original Git objects only
when captured status established they were clean, or from a matching artifact hash.
72 such resources were restored. Existing files were never overwritten during recovery.

Original registered worktree metadata survived in the shared Git administration
folders. Restoring only the missing candidate `.git` pointer files reconnected the
original worktrees; no new branch, commit or substitute checkout was created.
Both branches are `codex/q17-01-02-final-review-20261011`:

- Data HEAD `5d1f67b48f79343965cc1fb2b0cfa02c2e5d643b`.
- Planner HEAD `d607c1ea1ac1a5bbb7fff9b7595156b81b7cd4bc`.

Actual reducer/controller/service/Strategy imports resolve inside the restored
candidate. The 11 recorded dependency version entries match the previous run;
no packages were installed or upgraded. All 378 accepted dependency/runtime files and 56 latest handover files
remain verified. All 404 captured shared source/test/script files and shared Git
branch/HEAD/status remain unchanged. Final runtime has 384 manifest entries: one
changed reducer and one added test module relative to the original 383.

| Artifact | SHA-256 |
|---|---|
| Reviewed reducer | `5fa63156b82add28e0b9c69ded36631b362071c198027fd93cb681b749092bdd` |
| Corrected reducer | `57e1f77a14eb1dd11d79d242c7dc4fdf912074776a5d9f5568965c3e98162738` |
| Unchanged Planner service | `f6924fee438138d2d34e87280b67a74193dd5bfb1a62a1c306a9a1dc7266864c` |
| New 56-case module | `03d04449f71440bcfe9f8ad8f1a6342618c80e45e98cf95fd4c0a35c57f88a85` |
| Final runtime manifest | `ae2e8a647dab9da93ecccb7bef06bcc955abf4b8ce5129e453ce3d9167ac97b6` |

Recovery limitation: 193 older logs/intermediate artifacts were already absent and
no hash-matching copies were found. They are explicitly listed in
`recovery/older_evidence_recovery.json`, not asserted recovered or preserved. The
latest complete handover, accepted comparison data, runnable candidate and fresh
validation are available. Another 24 older artifacts were recovered by exact hash.
No missing historical result was fabricated or replaced with a fresh result.

Recovery maps, resource provenance, source audit, frozen runtime manifest,
incremental patch, test outputs and fresh replay evidence are retained under this
handover. The candidate is still uncommitted in a temporary directory and must be
preserved with its paired Data/Planner changes and Strategy snapshot for review.

## F. Verdict

**READY FOR FINAL INDEPENDENT REVIEW**.

Implementation and bounded adversarial/historical verification are complete.
Independent acceptance and any later integration remain separate steps.
