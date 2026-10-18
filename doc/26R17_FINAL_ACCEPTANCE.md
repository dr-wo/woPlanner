# 26R17-R final acceptance

Decision, 2026-10-18: **DEV-01 PASS; DEV-02 PASS**. No unresolved material defect.
All GUI acceptance was automated; no authenticated live connection was used.

## DEV-01 — P1 Pace Reference: PASS

A fresh actual StrategyPredictionWindow was instantiated with isolated settings.
The confirmation checkbox was unchecked both immediately after construction and
when its tab was first displayed. An attempted unconfirmed calculation started
no worker; Calculate and Apply were disabled. A Qt mouse click on the indicator
explicitly confirmed suitability. Clearing confirmation invalidates the result
and disables Apply, covered by the fresh GUI regression.

The final labels are P1 Pace Reference, Leader Pace Estimation, Lap range and
Calculate Reference. R17 L14, range L3–L14, returned ten accepted VER laps:
L3–L7 and L10–L14. R17 L12/L13 historical prefixes returned five/nine accepted
laps. L12–L14 alone fails the four-consecutive-lap rule; future ranges fail.
Only rear-gap clearance/rear-crossing support is waived. Ahead evidence, timing,
P1 position, dry compound, green status, pit, quick-lap, run-edge and sample
quality requirements remain active.

The actual spawned calculation, comparison, matching-team Apply and Restore
passed. Only observed-compound effective team degradation was applied; standard
MC state and other inputs remained unchanged. Rewind restores still-owned inputs;
reset and reopening require a new confirmation/calculation. Saved settings retain
prior manual values rather than P1-derived overrides. Later manual edits are
preserved by Restore. See [GUI evidence](assets/26r17_final_acceptance/p1_gui.json)
and [exact direct-service numerical comparison](assets/26r17_final_acceptance/isolated_numeric.json).

## DEV-02 — Race Info: PASS

Race Info displays Race Control Events above the original Pit Delta controls.
The eight archived R17 L52–L55 notices, including three lap deletions, pass through
recorder → replay → controller → service → GUI. Filtering, source notification
lap versus affected lap, chronological ordering, sparse/old corrections,
immutable snapshots and duplicate suppression pass. Penalties are explicitly
synthetic because the archive contains no penalty notice.

Recovery displays cached events without any new feed message. Repeated replay
is unchanged, reset clears the table, and returning to Live refreshes the map
while the existing plot update path remains active. Existing Pit Delta calculation,
selection, persistence, unresolved values and controls pass their regression
coverage. See [GUI evidence](assets/26r17_final_acceptance/race_info_gui.json).

## Headless GUI and visual inspection

Actual Qt windows used Fusion, QT_QPA_PLATFORM=offscreen, MPLBACKEND=Agg and a white
palette. The coordinator opened and inspected fresh screenshots from the isolated
commit candidate, including the unchecked initial state and calculated comparison.
Relevant labels and controls are legible, present and unclipped; no material
rendering issue remains. Parameter-table scrolling is intentional. Deleted-lap
and synthetic-penalty views were also generated and inspected.

- [Fresh P1 interface, unchecked](assets/26r17_final_acceptance/p1_initial_unchecked.png)
- [Calculated P1 reference and matching team](assets/26r17_final_acceptance/p1_calculated.png)
- [Archived Race Info and Pit Delta](assets/26r17_final_acceptance/race_info_archived.png)
- [Deleted-lap filter](assets/26r17_final_acceptance/race_info_deleted.png)
- [Synthetic penalty filter](assets/26r17_final_acceptance/race_info_penalty.png)

## Regression and baseline exceptions

| Scope | Final result |
|---|---|
| Current woPlanner affected analysis/live/UI suites and concurrent compatibility | 444 passed |
| Isolated HEAD plus selected feature changes, woPlanner | 334 passed |
| Current woData full suite | 450 passed, 1 baseline failure |
| Isolated selected woData source and committed tests | 303 passed, 1 baseline failure, 5 optional-data skips |
| woStrategy MC/review/weighting/identifiability/undercut/traffic | 102 passed, 1 baseline failure |
| woWeekend live replay/race/config/traffic/undercut | 80 passed |
| Standard planner service/VSC regression | 40 passed, 17 baseline failures |
| Additional telemetry/position-retention/VSC boundary checks | 25 passed |

Counts overlap and must not be summed. All 19 failure identities were reproduced
using exported committed sources. The original 18 comprise 17 older standard
planner expectations and one exact-floating-point assertion; the additional
woData download-script test already disagrees with committed SCRIPT_CONFIG's
session default. Concurrent local script defaults differ but fail the same test.
No feature regression was classified as a baseline exception. Details, source
revisions and failing test names are in [regression_summary.json](assets/26r17_final_acceptance/regression_summary.json);
individual logs and JUnit XML files are alongside it. The isolated export includes
the committed R14 operational-input fixture; no missing-export-fixture failure
remains in the final results.

## Independent review and resolved finding

Separate independent reviewers covered MC/domain correctness and live/data/UI
integration. Race Info review passed 93 woData and 146 woPlanner checks. Domain
review passed P1 and MC suites, exact 2,000-sample historical comparisons and the
final actual-worker boundary checks.

One material issue was found and fixed before commit: forwarding P1's scheduled
race distance through the shared standard-worker path shifted standard baseline
pace by approximately 2.6 seconds. Standard submit now explicitly retains its
original distance contract; P1 submit explicitly retains the schedule. The generic
serializer remains compatible with concurrent work. A regression captures the
actual standard AnalysisRequest rather than inferring the boundary from a helper.

The reviewer recaptured actual Automatic/Sector standard requests and P1 requests,
then verified exact candidate arrays, costs, weights and all parameter quantiles
against the original standard worker contract at R17 L14; L12/L13 waiting states
and eligibility also match. See [worker_numeric.json](assets/26r17_final_acceptance/worker_numeric.json).
No unresolved material findings remain. The final woData candidate also removes
any dependency on the unrelated publication-time refactor by using existing
source-envelope coordinates directly.

## Commit scope and limitations

woData contains only Race Control history, its additive snapshot field, reducer
and controller wiring, and tests. woPlanner contains P1 reference, Race Info,
related integration/tests/fixtures/scripts, corrected reports and this evidence.
Selected hunks exclude concurrent Automatic/Sector policy, readiness/provenance,
qualifying, telemetry, observed-undercut diagnostics, configurations and research.
woStrategy and woWeekend receive no commits. See [preservation.json](assets/26r17_final_acceptance/preservation.json).

The original R17 raw recordings contain no RaceControlMessages topic; messages
never captured cannot be recovered. Historical CSV notices and synthetic cases
therefore validate that path. There was no authenticated live-feed test, and no
push. Single-driver/stint estimates remain prior-dependent and fuel/track
confounded, as documented in the feature diagnostics.
