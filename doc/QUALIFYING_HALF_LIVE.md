# Experimental Half-live cutoff

The Qualifying Planner status area shows **Current** above **R&D · Half-live
[Experimental]**. Current retains its accepted projected cutoff (or Q3 P1),
formatting and source precedence. The second row is a read-only comparison,
not an input to planning, driver projections, telemetry, publication or exports.

## Exact candidate

The candidate identifier is `half_live`. It reproduces the frozen
`track-correction-research/scripts/evaluate.py::frozen_predictions` candidate:

```python
rate = 0.5 * live if live is not None else configured_fallback
prediction = min(current_cutoff + rate * unfinished_driver_count, current_cutoff)
```

`live` is the already fitted `live_estimate_s_per_global_push`, in signed
lap-time seconds per canonical session-wide field PUSH. Its coordinate and
per-driver support weighting remain owned by the accepted estimator. The
experiment does not refit, renumber observations or reset the coordinate at a
part transition. A negative slope improves lap time; positive slopes cannot
worsen an already observed result.

The existing cutoff target, observed time and unfinished-driver count are taken
verbatim from the same immutable snapshot used by Current. Q1/Q2 retain their
configured elimination target; Q3 retains P1. No final outcome or future
checkpoint information enters the calculation.

## Source and unavailable states

| Situation | Experimental behavior |
| --- | --- |
| Live supported | Half of the automatic live fit; label `Live supported` |
| Live limited | Same half-rate calculation; label `Live limited` |
| Numeric zero live fit | Valid live estimate, not fallback |
| No live fit | Full configured fallback, including nonzero values; explicit `Fallback · FP/default prior` |
| No live fit and fallback prior zero | Same fallback calculation; explicitly says `Zero fallback; target unchanged` |
| Manual override | Current alone uses the override; experiment uses automatic live or unchanged fallback |
| Missing observed target or published estimate | `Unavailable`, no inherited Current prediction |
| Invalid/nonfinite required input | `Unavailable` |
| New session or non-qualifying input | Clear the experimental row to `Unavailable` |

The tooltip identifies `half_live`, source, support status, manual independence
and the limited historical evidence. Fallback is not an independent live R&D
estimate, even if its numerical value equals Current. Limited-support status
describes the underlying live fit, not prospective validation of Half-live.
`No observed cutoff target` remains a separate unavailable reason in the tooltip.
The zero-prior explanation changes presentation only; no formula, rate unit,
support threshold or manual-override rule changes.

## Isolation and baseline

Development starts at accepted Planner `ac03ee3`, on
`codex/quali-half-live-experimental`. The research-pinned Planner `ce67da8` and
this baseline have identical `src/woplanner/quali` code. The research-pinned
Data and Strategy commits are `7580ccb` and `336098b`.

Only the GUI imports `experimental_cutoff.py`; the calculation runs once while
rendering an existing snapshot. No service, policy, estimator, ingestion,
snapshot contract, historical golden or downstream publication is modified.
No new computation is added to telemetry ingestion. The existing Current
rendering branch is retained in `_render_cutoff_predictions`.

## Historical reproduction

The new portable fixture `tests/fixtures/half_live_research_checkpoints.json`
contains 63 scored frozen checkpoints, saved predictions and original SHA256
provenance. Existing historical fixtures remain untouched. Tests compare each
calculated value to the saved prediction with absolute tolerance `1e-10`
and zero relative tolerance. Outcomes are used only by the scoring test.

Primary 300-second horizon, 11 scored checkpoints:

| Candidate | MAE (seconds) |
| --- | ---: |
| Zero | 0.2966363636363628 |
| Canonical | 0.2271590097001967 |
| Half-live | 0.20527511470396706 |

At saved R17 Q3 message 6334, observed P1 is 91.373 s, unfinished count is 10,
and live slope is -0.015432407407406623 s/PUSH. Current is
91.21867592592594 s; Half-live is 91.29583796296298 s.

These are limited historical results, not validated prospective performance.
Half-live's primary RMSE is 0.2893 s versus canonical 0.2643 s: lower MAE does
not mean improvement under every metric. Historical runs contain no nonzero
fallback or manual override experiment; separate synthetic integration tests
cover those cases.

## Validation

`tests/test_quali_experimental_cutoff.py` covers frozen checkpoint reproduction,
signed/zero rates, manual overrides, nonzero fallback, support states, missing
data, Q1/Q2/Q3 transitions, finalisation, reset, immutability and no-refit behavior.
`tests/test_quali_experimental_gui.py` covers same-snapshot rendering, unchanged
Current text, all display states, transition clearing and responsive geometry.

Independent accepted-baseline comparison and GUI screenshots are recorded in
`doc/assets/half_live/`. The screenshots show the real application populated
with saved research checkpoint inputs; they are not a new live session.

Final focused run: **168 passed**, including 112 calculation tests and 56 new
and existing GUI/widget/layout tests. See `final_focused.log` and
`final_focused.xml`.

The independent preservation verifier compares accepted and feature code in
separate interpreters with an identical external monotonic-clock input. Across
**137 checkpoints**, entire production snapshots, detailed track publications,
traffic finalisation records, track samples and pit groups are exactly equal.
All **411** repeated experimental reads preserve those outputs and never invoke
a fit. The comparison covers both research-pinned dependencies and the current
accepted dependency stack; both produce the same output digest. See
`independent_preservation_report.json`, `accepted_dependencies_preservation.json`
and `independent_dependency_audit.json`.

Reproduce the preservation check with the project interpreter:

```bash
python scripts/verify_half_live_preservation.py \
  --accepted /path/to/accepted/woPlanner \
  --feature /path/to/feature/woPlanner \
  --research /path/to/track-correction-research \
  --output /path/to/new/evidence
```

The verifier sets source import paths for each subprocess, asserts actual
module provenance and compresses the full comparison traces. The new tests
need only the checked-in fixture; original research files are needed only for
the provenance verifier and screenshot reproduction script.

Broader qualifying regression, deduplicating overlapping runs: accepted
**1003 passed / 5 skipped**, feature **1139 passed / 5 skipped**. All 1008 shared
completed test outcomes agree; all 136 additional tests pass. The original R14
and R17 recording tests completed. Five R16 tests skip because their dedicated
recording path is absent in the isolated copies; R16 saved research checkpoints
are separately covered above. The expensive multi-pass R13 determinism test was
interrupted and is explicitly **not claimed as passed**. Remaining tests were
run separately. JUnit's unnamed interruption placeholders are excluded.

One path-validation test initially failed equally on both versions because the
research snapshots do not contain Git metadata. It passed for both versions
when rerun against real accepted dependency worktrees, without source changes.
See `independent_regression_report.json` and compressed raw logs/JUnit files.
The independent reviewer completed source, numerical and visual review with
no blocking findings; the primary agent collected final regression results
after the reviewer reached its usage limit.

## Release integration replay verification

The final integration run uses the native macOS Qt application, its Replay
button, background replay source, controller queue, normal timer polling,
resource loader and GUI rendering. No research snapshot is injected. The
read-only resource copy contains the original R14/R17 FP artifacts, with file
hashes recorded separately. Accepted woData/woStrategy worktrees are used.

R14's original live recording has 18,462 messages and covers Q1/Q2/Q3; R17 has
8,134 messages and starts in Q2, covering Q2/Q3 only. At every established replay
snapshot the integration observer independently checks the active-field target,
Q1/Q2 elimination rank or Q3 P1, unfinished count and the two projection
formulas. Every eligible observed target has a finite Current prediction.
Unavailable states at segment boundaries clear when that segment has a valid
observed target. Both labels are checked against the actual rendered texts.

R14 Current changes 19/20/17 times across Q1/Q2/Q3. R17 changes 13/14 times
across Q2/Q3. Genuine replay covers configured-default fallback, live-limited
and live-supported states. Representative R17 Q3 predictions are Current
91.21867592592594 s and Half-live 91.29583796296298 s. The actual Plan dialog's
manual override changes Current on an unfinished Q2 field while leaving
Half-live unchanged. Native window captures verify both rows at wide and
minimum supported widths. Nonzero fallback and invalid/nonfinite inputs remain
supplementary fixture-based tests, rather than claims about these recordings.

Independent review discovered a pre-existing stale-display edge: restart failure
or a non-Qualifying session could leave the old Current label visible. The
integration fix clears both labels, countdown and chequered indication and
stops the old polling timer on restart. Four regression cases cover restart,
rejection and both replay startup failure stages. Estimator mathematics, source
precedence, historical goldens and production publications are unchanged.

The compact release evidence and genuine replay screenshots are kept in the
local workspace output directory `outputs/quali_half_live_release_20261019`.
They are distinct from the older injected-checkpoint screenshots above. The
release completion report records the final merge commit and post-merge tests.
