# Q17-03 calibration contribution lifecycle correction

**READY FOR FINAL INDEPENDENT REVIEW.** The approved recoverable calibration lifecycle is implemented in the existing isolated runtime. This report supersedes the prior report at this path; its exact text and source remain in [reviewed_runtime](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/reviewed_runtime). Verification date: 11 October 2026. The [design-review reference](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/design_review_reference.json) records the review returning READY FOR TARGETED IMPLEMENTATION.

## A. Implementation

Production changes are limited to `woPlanner/src/woplanner/quali/physical_traffic.py` and nine added lines in `woPlanner/src/woplanner/quali/service.py`. One new regression module and two existing test modules change. No Data or Strategy production code changes.

`SpanContribution` consolidates the previous admission/value histories. Each ordered `SpanVersion` carries a receipt clock/domain, validity, value and separate terminal retirement reason. An unseen completion represents NOT_ADMITTED; ever-admitted identities survive bounded metadata expiry and prevent accidental readmission. The current 32-member estimator is a projection of VALID, nonretired versions; original admission order survives revision and recovery. Explicit ownership withdrawal and capacity retirement remain terminal and distinct from physical INVALIDATED state. The old `_span_updates` representation is removed.

Ingestion reconciles only certified/recoverable contributions whose acquisition bounds contain changed, causally related observations. Reconciliation runs before raw retention trimming and continues after the contributor's fitting sample locks. Authoritative OffTrack adds a durable source blocker; correction of that same observation can clear it. A later acquisition's OnTrack return cannot clear an earlier blocker. Recovery still requires a positively supported complete traversal. A complete revised traversal failing the unchanged minimum/range gates invalidates its old contribution. A validator returning None preserves the prior decision: missing rows, anchors, mapping or retained support is not negative proof. Status-only evidence supplies eligibility, never distance.

The same receipt-filtered physical trace builder supports reconciliation and final evaluation. Independently proven UTC/session aliases are captured at reception and used only in evaluation copies. Duplicate representations preserve the original integrated distance row; a distinct causally matched row can revise distance. Loss of mapping alone preserves certification. A known specific dependency can still carry a later correction after mapping or state-cache loss; an unknown foreign coordinate cannot establish one. D2's observation ledger and D3's Player provenance retain their reviewed bytes.

Unclocked legacy adapter producers retain compatibility through an explicit `ingest_order` domain: each actual ingress establishes an ordinal receipt, distinct from seconds and acquisition coordinates. It cannot be compared with real-time deadlines or reported as a measured delay. Partial explicit clock metadata gets no such fallback. Existing legacy numeric and uncertainty assertions remain unchanged. Historical queries select the applicable validity/value version at the requested receipt cutoff and never mutate today's calibration. Different corrections with equal receipt timestamps retain causal delivery order. Every genuine admission, revision, invalidation, recovery or capacity retirement emits the existing field-wide calibration notification, even if the median is unchanged. Pending results retain same-index fitting replacement, cache invalidation and same-cutoff stabilization; +2 normal, `(2,4]` grace and +4 hard lock remain unchanged.

The service supplies both deadlines of every pending record to protected compaction. The baseline at/before the earliest protected cutoff in each domain and all subsequent transitions survive. Compaction preserves original admission and current projections; history visible to pending cutoffs cannot be blindly evicted. The usual 1,024 metadata budget has a finite reserve: at most 2,048 protected cutoffs, 66,560 retained contribution records and 132,160 versions. Blockers are capped at 4,096 per contribution and 132,160 globally, source provenance at 8,192 per contribution, and the derived state index at twice retained state count per driver. An unsatisfiable protected reserve raises an explicit structural error before discarding required transition history. The artificial reduced-budget conflict control passes; no normal-budget conflict occurred. Existing session-growing admitted/revoked identity sets remain unchanged. This is bounded pending-history support, not arbitrary full-session history or universal constant lifetime memory.

## B. State-transition validation

All mandatory entries **A–W PASS**. The [matrix](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/state_transition_matrix.json) links each entry to collected tests and accepted logs. Tests primarily exercise reducer → service → evaluator in replay and native monotonic receipt modes; capacity and artificially reduced budget controls supplement that pipeline coverage.

| Entries | Validated behavior |
| --- | --- |
| A–E | Admission; valid revision; authoritative invalidation; recovery; multiple validity intervals under the same completion identity. |
| F–H | Explicit withdrawal; permanent boundary conflict plus independently supported corrected ownership; duplicate negative idempotence. |
| I–M | Before normal deadline, during grace and after lock; multiple pending targets; nonoverlapping historical contributor notification. |
| N–P | Retention/capacity distinctions; prompt/delayed callback equivalence; same-acquisition eligibility correction in both directions, including verified coordinate switches. |
| Q–T | Supported below-minimum and out-of-range revision; post-lock invalidation/recovery affecting future independent targets. |
| U–W | Repeated compaction-pressure transitions; equal-receipt causal corrections; ordinary evidence/mapping disappearance without fabricated invalidation. |

Two existing expectations are deliberately updated to the approved contract. The old private `_span_updates` size assertion becomes unified-history assertions, additionally preserving cutoff 112 under pressure; existing numerical checks remain. The final same-receipt OffTrack correction in `test_real_return_and_delayed_states_use_source_time` now excludes B and selects physically supported C at 2 seconds, instead of retaining superseded eligibility. No unrelated expected value, threshold or mathematical rule is relaxed. The existing field helper adds optional inputs while preserving its defaults.

## C. Historical evidence consistency

Same-key controls admit A at cutoff 112 with 1,000 m, invalidate at receipt 113, then recover at 113.5. A remains present at 112, absent at 113/113.25 and present from 113.5. A later acquisition return leaves it invalid until the original observation is corrected. Duplicate negatives add neither a version nor a fitting revision; distinct same-receipt corrections create distinct causal transitions. An additional [two-source blocker control](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/multiple_source_blockers_adversarial.json) confirms that correcting one of two negative source observations leaves the contribution invalid until both are corrected, in both temporal paths.

For old nonoverlapping C, invalidation at 113 changes the pending target's circuit estimate **1,100 → 1,000 m**, while cutoff 112 still contains C's 1,200 m contribution. Corrective recovery at 113.5 restores the pending calibration. Supported revised lengths of **975 m** and **1,700 m** invalidate the old valid contribution; later genuine causal rows can positively restore supported **1,000 m** or **1,337.5 m** support under unchanged gates.

After hard lock, a supported revision changes current calibration to **1,025 m** without changing locked fitting. Post-lock invalidation/recovery likewise preserves the locked A sample while a future independent D receives **1,100 m / 3.19 s / eligible** when A is invalid versus **1,000 m / 2.90 s / ineligible** after recovery, with **15/21** ahead coverage in both. Negative evidence before initial admission and invalidation during grace produce equal later independent results.

Metamorphic controls use identical source messages, receipt timestamps and causal order. Prompt callbacks and delayed/batched callbacks agree on historical contributions, current calibration, traffic/clean-air business results and locked samples for receipts 111.5, 112, 113, 114 and 114.1, including cross-domain corrections and multiple pending deadlines. Publication times may differ. Eight validity flips under a reduced soft history budget preserve protected 112, 113 and 113.05 views. Capacity recovery preserves original order and retires the oldest member rather than treating it as a new admission. Missing full retained distance support cannot manufacture recovery.

## D. Tests

| Validation | Result and evidence |
| --- | --- |
| New lifecycle regressions | **69 cases**, included in current Planner collection. |
| Reviewed-source reproductions | **14 confirmed behavioral failures**, plus **4 coordinate-switch failures**, recorded in [before](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/reviewed_before.txt) and [bridge before](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/reviewed_bridge_before.txt). |
| Final targeted controls | **165 passed**, including all 69 new cases, legacy physical-phase controls and accepted boundary, schedule, D2/D3 controls. [Final targeted log](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/targeted_final.txt). |
| Planner Qualifying | **697 passed**: disjoint **656 + 40 + 1** partitions covering the complete current collection. [656 cases](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/planner_fast_final.txt), [40 cases](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/planner_recorded_final.txt), [schedule replay](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/planner_determinism_final.txt), [partition audit](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/test_partition_audit.json). |
| Data Qualifying | **168 passed**, [final log](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/data_qualifying_final.txt). |
| Data Player/provenance compatibility | **57 passed**, [final controls](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/data_provenance_final.txt). |
| Mandatory history | **8 paths completed / PASS**, [audit](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/eight_path_final_audit.json). |

The current 697-node collection is covered exactly once by the three final partitions. Production and test bytes remained frozen throughout this final qualifying run and all eight final replays. The partition audit records exact node IDs and hashes; focused totals overlap with the suite.

Superseded exploratory logs, interrupted pre-bridge runs and pre-legacy validation are preserved, excluded from acceptance. The first complete run found **11 failed / 681 passed** in legacy producers without an explicit clock; the admission guard had incorrectly removed previously supported numeric results. The distinct ingress-order fallback and an explicit domain-isolation regression fix this without changing the 11 existing assertions. Full qualifying coverage and all eight paths were rerun after this last source change. A confirmed UTC/session correction counterexample drove the reception-proven alias fix before final source freezing. Intermediate fixture mistakes concerning mapping-loss input and a still-on-track original target were corrected in test setup; production thresholds did not change.

The prior full Data result had one known download-script failure expecting R instead of preserved FP1 configuration, reproduced in [baseline evidence](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/data_unrelated_baseline.txt). Previously documented 19 broader Planner Race/strategy failures remain separate. Those broad suites were not rerun or claimed green in this lifecycle round. D2/D3 and previous Q17-03 controls remain in the full qualifying collection.

## E. Cross-round metrics

All required direct paths and R14/R16/R17 Player paths preserve the reviewed metrics; direct/Player results agree. Fitting observations, traffic-supported candidates, clean-air eligible observations and driver fits are all zero in these recordings. No assertion forces zero; the synthetic real-pipeline controls demonstrate supported physical business results.

| Round | Causal speed matches = retained positions | Complete spans | Pace candidates | Traffic supported | Clean air | Fitting observations | Driver fits | Pending | Track Correction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| R12 | 348,480 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | Unavailable |
| R13 | 378,818 | 52 | 87 | 0 | 0 | 0 | 0 | 0 | Unavailable |
| R14 | 302,742 | 44 | 72 | 0 | 0 | 0 | 0 | 0 | Unavailable |
| R16 | 363,374 | 24 | 62 | 0 | 0 | 0 | 0 | 0 | Unavailable |
| R17 | 154,924 | 21 | 33 | 0 | 0 | 0 | 0 | 0 | Unavailable |

The [eight-path audit](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/eight_path_final_audit.json) verifies each recording hash against the actual input, reviewed run and Phase 0, and all 20 runner/module hashes against start, finish and current bytes. Ordinary per-message traces, transitions, pace identities, admission identities/receipts and active membership/order match reviewed history exactly. All pace business fields also match except the declared circuit-length precision difference below. All paths have zero causal violations and pending records, +2 normal cutoffs and bounded grace. Grace update counts remain **0/69/4/10/2** for R12/13/14/16/17.

R13 has a verified numerical exception: calibration-copy coordinates use the established one-microsecond precision, eliminating epoch-conversion float residue. Across all 52 admissions, the maximum span difference is **0.000033852 m**; 78 pace circuit estimates differ by at most **0.000022775 m**. The final median changes **5,741.463170548 → 5,741.463178056 m** (0.000007507 m). The [precision audit](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/r13_coordinate_precision_audit.json) enumerates every difference and proves these are the only changed fields; gates, integration mathematics, membership, receipts and support/clean-air/fitting outcomes are unchanged. Other rounds retain exact admission values and current calibration. No replay was rerun to force reference values.

The **126,889 ordinary traffic-enabled/disabled controls** compare behavior within this final runtime, supplemented by exact reviewed traces. R17 Player retains the documented Phase 0 bootstrap countdown difference, 512.0 versus 509.955102; its Phase 0 transition flag remains false while its reviewed Player transitions match. That Q17-01/02 bootstrap issue is not changed. Preserved causal telemetry and contribution lifecycle correctness do not establish predictive accuracy.

## F. Git and integration

The actual Planner and Data worktrees remain on `codex/q17-03-review-corrections`, at HEADs `d607c1ea1ac1a5bbb7fff9b7595156b81b7cd4bc` and `5d1f67b48f79343965cc1fb2b0cfa02c2e5d643b`. `woStrategy` is a source dependency snapshot, not a third Git worktree; all its source/test/runner bytes are preserved. Shared Strategy HEAD remains `a8ec8041718cb8e12cf677766afeb5c1c0e6e490`.

[Preflight](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/preflight.json) matched all **377** reviewed source/test/runner files before edits. [Final manifest](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/final_runtime_manifest.json) records **378** files, and [source delta](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/source_delta.json) records exactly two production edits, two existing test edits and one new test module. Data, Strategy, OLS/PUSH, physical/clean-air/coverage thresholds, ordinary clock/session state, warning policy, rendering, disabled shadow, D2/D3 and Promotion retain reviewed source. The three outstanding Q17-01/02 findings remain separately owned.

| Production file | SHA-256 |
| --- | --- |
| Planner physical_traffic.py | `f4bf9a6f1d049aa9c2ee48909c36038c544d4b896c82e56e5190b4e4c9d5a4b9` |
| Planner service.py | `aadde19adca3a55599d8b91d644f437d2de676fad010ba4c54539930ed84999e` |
| Preserved Data player.py | `29763e6a3c194737189bfb8368755ba89d752a4615b92ad1fe2d2c731d1e963c` |
| Preserved Planner traffic_finalisation.py | `5ebcb326f8b4b9d103a102594aea2c5ffd61422a1c343cb1d71bc0b1fb34e7e1` |

All **521** recorded shared source/test/script/document files, shared status, branches and HEADs match [preservation audit](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/shared_preservation_check.json). No shared edit, commit, push, switch, merge or cherry-pick occurred. The [six-file patch](/Users/zhxutong/dr-wo/woPlanner/.worktrees/q17-03-review-corrections/evidence/calibration_lifecycle/q17_03_calibration_lifecycle.patch) includes this report and applies against the exact reviewed dirty runtime snapshot, not Git HEAD. Previous-round evidence remains intact.

Integration must preserve the consolidated `SpanContribution`/`SpanVersion` representation, private history-protection hook and reception-proven calibration aliases. Existing private consumers of `_span_updates` need the updated representation. `ingest_order` is only a legacy adapter receipt-order coordinate and must never be merged into wall/monotonic seconds. Overlapping Promotion service changes require a separate coordinated review retaining one canonical pipeline; no Promotion integration or conflict resolution was performed. Standard-library additions introduce no dependency upgrade.

## G. Final verdict

**READY FOR FINAL INDEPENDENT REVIEW.** Mandatory lifecycle transitions, causal historical reconstruction, recovery, post-lock dependencies, bounded protected history, actual-pipeline scheduling controls and eight historical paths pass. No unresolved counterexample remains within the approved and retained-evidence contract. This is an implementation handover, not closure of Track Correction, predictive validation or integration approval. Work remains isolated and uncommitted.
