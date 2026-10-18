# 26R17-R DEV-01 — manual P1 reference

Implemented and validated 2026-10-18. This resolves the domain question recorded
in [the earlier investigation](26R17_P1_MC_INVESTIGATION.md). DEV-02 was not changed.

## Behaviour and use

Open Strategy from the live window, then select **P1 Pace Reference**. Its
**Leader Pace Estimation** panel provides the **Lap range** controls and
**Calculate Reference** action.

1. Select the current published leader and an observed completed-lap interval.
2. Enable **Use sector timing to assess traffic ahead**. This
   opt-in affects only P1 calculation; the standard MC traffic setting stays as-is.
3. Explicitly check **Confirm selected laps are suitable for pace estimation**,
   which is unchecked whenever a fresh interface opens, then calculate.
   A consistently close P2 is allowed. There is no automatic yo-yo detector.
4. Review accepted laps, exclusion reasons, quantiles and sample-quality
   diagnostics. The full diagnostics dialog includes weighting and
   identifiability information.
5. To use supported values, select the leader's matching team and explicitly
   press **Apply Reference to [team]**. **Restore Previous Inputs** removes
   values still owned by that action, retaining subsequent manual edits.

Calculation runs in a separate background process using an immutable submitted
snapshot. It calls the existing Retro MC fitting algorithm with shared model
configuration; it does not introduce a new MC algorithm or filter an already
weighted posterior. Calculation alone never publishes or replaces the standard
model, caches, fingerprints or history.

Scheduled race distance is forwarded only to the P1 worker. The standard worker's
existing input serialization remains unchanged; final independent acceptance
caught and fixed accidental forwarding into that shared path before commit.

![Manual P1 reference at the original R17 L14 cut](assets/26r17_p1_reference.png)

## Selection and statistical contract

Only the selected driver's laps recorded in position 1 and inside the selected
range can enter the fit. The following existing conditions remain: valid timing,
tyre/stint metadata, allowed dry compounds, green track, quick lap, non-pit lap,
configured edge exclusions, ahead-traffic clearance, and the consecutive clean
run requirement (four laps by default).

The rear-gap exclusion and requirement for a following rear crossing are waived.
All four observed sector-boundary preceding crossings are still required for
ahead evidence; position 1 is not treated as proof of clear track ahead. Missing
ahead evidence fails closed. Nothing fills gaps from later race information.

Fastest-lap classification and stint age are prepared from the full *currently
observed* snapshot before range selection, preserving existing coordinates.
Non-P1 or invalid/missing laps split consecutive blocks; the alternative cannot
join a run across an absent lap. The standard selector is unchanged.

The result is labelled `manual_p1` and reports sample count, ESS/fraction,
maximum/top-five weight, RMSE, observed compounds, accepted runs and existing
identifiability diagnostics. Invalid or numerically degenerate weights disable
Apply. No extra thresholds or automatic tactical classification were added.

Single-driver/stint degradation is confounded with fuel and track corrections;
the estimate remains prior-dependent. High ESS is a weighting diagnostic, not
proof of independently identified pace parameters. Retained weights and
quantiles come from the existing fitter.

## Comparison and application boundaries

Standard comparison uses the model captured when calculation was requested.
Models from a later leader-lap cut are excluded. Degradation comparisons require
matching scope and units. Compound-offset comparisons additionally require the
same explicit reference compound. Raw baseline/fuel/track medians are displayed
for the P1 fit but their standard comparison is suppressed because their
coordinates may differ; marginal quantiles are not naively rebased.

The existing strategy workflow supports applying **effective team degradation
for observed compounds**. P1 evidence can enable that team even when its standard
MC result is unavailable. Applying changes only those local team manual inputs;
other compounds, global inputs and standard MC remain unchanged. Baseline pace,
fuel, track and compound offsets are not automatically applied.

P1-derived overrides are local to the current window/session. Reopening requires
recalculation and reapplication. Saved settings retain underlying prior manual
values rather than silently restoring a P1 result from a later replay cut.
Existing strategy-run audit records may still describe the inputs explicitly
used for that run. Rewind/session reset cancels stale work and restores
still-owned P1 inputs, including equal-lap replay restarts.

## Actual R17 validation

The checked-in compressed fixture contains original delivery prefixes, including
driver identities, committed laps/sectors and scheduled distance. Provenance
records source sequence/time and SHA-256 of each consumed recording prefix.
No whole-race lap cache or future corrections enter the fixture.

With range L3 through the observed cut and the existing four-lap minimum:

| Observed cut | Original sequence | Standard clean laps, all drivers | P1 accepted laps |
|---|---:|---:|---|
| L12 | 17612 | 0 | L3–L7 (5) |
| L13 | 18910 | 0 | L3–L7, L10–L13 (9) |
| L14 | 20206 | 16 | L3–L7, L10–L14 (10) |

The P1 reference is VER on SOFT. L8/L9 remain excluded for non-green status;
L9 also fails the quick-lap condition. Fresh L14 has sufficient preceding
crossings; it no longer needs P2's following crossings, which is precisely the
rear-side waiver. Selecting only L12–L14 still refuses calculation: three laps
cannot meet the default four-consecutive-lap requirement. Future interval ends
are rejected.

The [2,000-sample audit](assets/26r17_p1_reference_validation.json) compared standard
outputs against the service source saved before this task. At each cut, statuses
and eligibility matched; where a standard fit exists, candidate matrices and
cost/weight tables were exactly equal. P1 calculation left all standard service
state unchanged. Independent review repeated this audit successfully.

## Tests and review

Final combined regression: **399 passed in 17.76 seconds**. This includes P1
selection/fitting/UI, actual R17 prefixes and a real spawned worker, live analysis
boundaries, MC readiness, analysis service/worker, tyre inputs, compound offsets,
live degradation sensitivity, observed undercut, weekend import, live/Race Info
GUI, and Retro MC/sampling reproducibility.

Independent final focused run: **65 passed**. Reviewer findings and resolutions:

- Applied L14 inputs could survive a rewind to L5: reproduced and fixed by
  restoring still-owned inputs on rewind, session reset and worker restart.
- Persisted manual state could lose P1 cut ownership: saved-state sanitization
  now prevents P1 overrides from becoming unscoped active saved inputs.
- Verified independent P1 sector selection, explicit team application without
  standard support, and coordinate-compatible comparisons.

No unresolved material review findings remain. A real process → UI → Apply →
Restore smoke run passed at the R17 L14 prefix. It confirmed unchanged standard
Automatic traffic and exact restoration of prior strategy inputs, and produced
the white-background screenshot above. DEV-02 GUI files were byte-compared with
their state at the start of this follow-up and are identical. No active
authenticated race-feed connection was used during testing.

Reproduce from `woPlanner` using the shared environment:

```sh
MPLBACKEND=Agg MPLCONFIGDIR=/private/tmp/r17-mpl-cache QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m pytest tests/test_p1_reference.py tests/test_p1_reference_gui.py tests/test_p1_reference_r17.py -q
MPLCONFIGDIR=/private/tmp/r17-mpl-cache ../.venv/bin/python scripts/verify_r17_p1_reference.py
MPLCONFIGDIR=/private/tmp/r17-mpl-cache ../.venv/bin/python scripts/verify_r17_p1_ui.py
```

`build_r17_p1_fixture.py` reproduces the compact fixture from the original local
recording. The verification script accepts `--baseline-service` to additionally
compare against a preserved pre-change service file; without it, the script
checks standard-state isolation and the observed-prefix cases only. The saved
audit includes the successful pre-change comparison.

Final independent acceptance supersedes the development-stage test counts above:
see [the consolidated acceptance report](26R17_FINAL_ACCEPTANCE.md) and its linked
fresh-GUI screenshots, numerical evidence and regression results. Existing
unrelated work was preserved; no changes were pushed.
