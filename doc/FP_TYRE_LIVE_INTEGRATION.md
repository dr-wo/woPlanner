# FP Tyre Inputs and Live/Replay Debugging

Last reviewed: 2026-09-06

This document is the durable woPlanner handoff for the FP tyre integration and
the current live/replay calculation pipeline. It records the boundaries,
assumptions and compromises that are easy to lose when debugging a single lap.

## Dependency and call boundaries

The intended dependency direction is:

```text
StrategyPredictionWindow / LivePlannerWindow
  -> OfflineAnalysisService
  -> TyreInputService
  -> public wostrategy FP evidence API
```

GUI code must not import FP estimator internals or launch a woStrategy CLI.
`woplanner-live` opens the same `StrategyPredictionWindow` used by offline
woPlanner, so both applications use the same widgets, service, resolver and event
artifacts. There is no live-only tyre model.

## Tyre-input resolution

The five external engineering parameters are:

```text
Deg_HARD
Deg_MEDIUM
Deg_SOFT
Perf_HARD_vs_MEDIUM
Perf_SOFT_vs_MEDIUM
```

Performance means first-compound pace minus MEDIUM pace. Positive is slower;
negative is faster.

`TyreInputService` resolves every field independently:

```text
manual value when non-null
otherwise the exact pre_race_prior_v1.json value
```

FP LongRun contrasts are advisory and never enter this selection. Manual values
are authoritative even when their physical ordering looks unusual; warnings do
not reorder, clamp or replace them. Only non-numeric, NaN and infinite values are
rejected. A blank edit does not clear an override—Clear or Clear All must be an
explicit user action.

The legacy fallback has two important limitations:

- its absolute degradation shares the pre-race Fuel/common-D gauge and is not a
  uniquely identified physical level;
- its performance fields had zero/negligible direct FP likelihood sensitivity.

Those limitations are provenance, not reasons to substitute new defaults.
Missing legacy data with no manual replacement is a required-input error.

## Event artifacts

Artifacts live below the existing event weekend-model root:

```text
WODATA_ROOT/wostrategy/weekend_model/v1/year=<year>/round=<round>/
  pre_race_prior_v1.json             # unchanged legacy fallback
  fp_tyre_evidence_v1.json           # read-only advisory evidence
  manual_tyre_inputs_v1.json         # nullable engineering overrides
```

Manual state is event-specific. It is shared across offline/live processes by
the persisted file and is reloaded on event/window/tab boundaries and immediately
before a Strategy search. No real-time file watcher is required. FP recalculation,
GUI refresh, Race updates and application restart do not clear manual values.

## Explicit-only FP calculation

Cache read and calculation are deliberately separate operations. None of these
actions may call woStrategy FP analysis or cause a download:

- starting woPlanner or woplanner-live;
- loading an event, live session or replay;
- opening Strategy Prediction, Tyre Inputs, evidence details or history;
- switching tabs or refreshing;
- editing/clearing a manual value;
- starting Strategy search or live Race recalculation.

Only **Calculate FP Evidence** and **Recalculate FP Evidence** are authorized to
call the explicit service method. The calculation runs in a dedicated background
executor, disables duplicate actions and atomically replaces the cache only after
success. A failed recalculation displays the error and retains the previous cache.

Staleness is local-only. Changed source hashes or a newly present local FP session
produce a warning while the old evidence remains visible. No remote freshness
query occurs automatically.

## Strategy and live handoff

The values in the Tyre Inputs **Selected** column are converted once to the
existing MEDIUM-reference override structure and passed unchanged into exact
strategy search. Downstream code does not resolve manual-versus-legacy again.

The Strategy Prediction **Rules compliant** checkbox is orthogonal to tyre-input
resolution. It passes `minimum_distinct_dry_compounds=2` to the exact search and
filters complete plans, counting the current compound for an in-race
continuation. It defaults off for backward compatibility. The compromise is
explicit: this is only a dry-compound-count rule, not a complete legality model;
wet-weather exemptions, available tyre sets, race-control state and other
event-specific regulations remain outside woPlanner's current inputs.

At lap zero the resolved package is the pre-race model. During a Race, existing
`strategy_assumptions` logic remains authoritative:

- `live_direct` means the compound has a valid direct Race MC result;
- `live_derived` means the compound is inferred from a direct compound using
  the fixed initial degradation ratios;
- `base` means no live degradation information is available yet;
- directly informed MEDIUM is the preferred scale anchor; before that, the
  best-supported directly informed compound is converted to an equivalent
  MEDIUM scale using its fixed initial ratio;
- direct always overrides derived, and Manual Override ignores both live states;
- FP evidence itself is never passed into Race inference.

The initial ratios are captured when the Strategy Prediction session resolves
its starting tyre package and are not recalculated from later derived/direct
values or subsequent artifact refreshes. This applies only to degradation.
Compound delta retains independent base-to-direct resolution and a missing delta
is never synthesized as zero.

Every persisted search has an immutable
`tyre_input_snapshots/<search_id>.json` sidecar containing the five actual values,
sources and content hashes. Named snapshots also embed this payload. Later manual
edits cannot rewrite old snapshots, so replay/debug consumers must use the stored
snapshot rather than today's event artifact.

Update History stores the exact effective values handed to Strategy Prediction.
Degradation source metadata is `base`, `live_derived`, or `live_direct`; derived
rows also record their direct anchor and initial ratio to MEDIUM. Base and
derived segments are dashed, derived markers are hollow, and direct segments
are solid with filled markers. The previous flat base line ends at the first
derived/direct transition. Compound delta keeps the simpler dashed-base then
solid-direct presentation. Clicking a history lap remains an explicit temporary
Strategy override; it does not mutate the event manual artifact.

## Live/replay pipeline

High-volume `CarData.z` and `Position.z` packets are routed from woData's
controller to `TelemetryAnalysisWorker`. The Qt/controller snapshot remains
focused on planner timing and tyre state. Telemetry reduction and traffic-input
construction therefore happen outside the GUI thread.

`TelemetryAnalysisWorker` keeps packet ingress sequence numbers. Each leader-lap
analysis request receives a causal cut at that lap's completion; a max-speed
replay cannot accidentally include packets from a later lap. The worker retains
raw messages for the active replay generation so it can rebuild that immutable
cut. Reset increments the generation, making late results from an older replay
ineligible. Track-map and causal replay stores share one position policy: the
latest 1,200 sample-time seconds and at most 6,000 samples per driver. Embedded
timestamps order delayed batches. Diagnostic cuts additionally filter sample
time after ingress-bounded reconstruction; later materialized snapshots cannot
establish arrival causality. Raw position history is omitted from MC inputs.

`LiveAnalysisProcessService` owns the CPU-heavy Monte Carlo process lane:

- live mode uses latest-wins queueing because accumulated current data supersede
  an intermediate unstarted update;
- replay uses FIFO-all because every leader-lap result is part of the diagnostic
  history;
- raw telemetry reduction retains its high/low watermarks (256/128 by default),
  while the prepared-result handoff and MC process lane are separately bounded;
- by default there is at most one prepared handoff and one queued MC request;
- MC capacity does not pause replay-state production. Race timing, Cumulative
  Delta and other state-driven views can reach the current lap while historical
  MC cuts catch up;
- the UI exposes the number of lightweight historical cuts awaiting preparation.

These queue policies are semantic. Do not replace replay FIFO with coalescing to
improve apparent speed; that changes the output history.

The deliberate compromise is latency rather than history loss. A max-speed full
race replay can finish visually many minutes before all historical recalculations
complete. Bounding large prepared snapshots prevents memory growth without
coupling MC throughput back into the GUI/replay clock.

## Cumulative Delta geometry and retirement reference

Sector points use a single fixed geometry for the complete session. Preferred
geometry is cumulative physical sector-boundary distance divided by lap distance,
but `LiveSessionSnapshot` and `CompletedSector` currently expose no such boundary
metadata. The implemented fallback derives fractions from the earliest complete
lap of the selected reference driver, or the earliest complete lap of another
driver if necessary:

```text
S1 = reference S1 / reference lap time
S2 = (reference S1 + reference S2) / reference lap time
S3 = 1.0
```

Equal thirds are used only when no complete representative lap exists. Fractions
are stored in plot-data attributes with source `reference_time_fraction` or
`legacy_equal_thirds`; they are never recalculated per driver or per lap. Under
the completed-lap coordinate convention, lap N occupies X=`N-1` through X=`N`.

In averaged-reference mode, only the selected driver's positive observed sector
times contribute. Its mean S1, S2 and S3 are repeated through the complete plot
timeline, including after retirement. The population never changes, so retirement
introduces no reference-definition jump. The compromise is that
`CompletedSector` does not carry pit, SC/VSC, lapping or validity flags: those
conditions cannot be filtered independently here, and all positive selected-
driver sectors participate. Live and Replay share this service definition.

## Race model and scheduled distance

The production default is `race_baseline_architecture="retro"`, using
woStrategy's accelerated evaluator with exact legacy/retro semantics. The
alternative Race-specific architectures remain diagnostic, not default.

The fuel proxy's total-lap value is resolved in this order:

```text
snapshot.scheduled_total_laps from LapCount.TotalLaps
manual/configured override
current leader lap fallback
```

The source is stored in sampler-quality metadata. The leader-lap fallback is an
incomplete-input compromise and is especially weak early in a Race. Never infer
scheduled distance from maximum completed `LapNumber` when `TotalLaps` exists.

## Traffic assumptions

Automatic live Race traffic uses Sector timing boundaries v2 with causally
bounded sector input recovery. Provenance records requested mode `automatic`,
effective method `SECTOR_BOUNDARY_FALLBACK` and version
`sector-boundaries-v2-input-recovery`. New eligible clean laps trigger MC;
insufficient evidence retains existing model state and history with a waiting
reason. The old zero-origin telemetry reconstruction remains disabled.

**Sector timing boundaries** remains available explicitly. Its crossing gaps
and Automatic's Sector gaps are not blended into telemetry
means. Position-derived traffic remains diagnostic and is not activated for
clean-lap decisions. Correct physical neighbours alone do not validate time-gap
threshold decisions. Position.z history supports the track map and a diagnostic
position-traffic shadow; position-derived gaps are not production-active. Raw
live recordings remain the full-fidelity source after memory windows trim data.

Fresh traffic-derived FP evidence requires `unified-traffic-v4` with
`compatibility_min`, including cached and materialized lap frames, diagnostic
inputs and reused calibration history. Incompatible or unversioned evidence must
be genuinely recomputed, never relabelled. Saved models and calibrations remain
historical artefacts and are not retroactively certified as v4.

## Numerical quality versus confidence

The Model Quality panel must keep these separate:

- sampler quality: ESS/fraction, max and top-five weight, seed/backend stability,
  calculation timings;
- evidence/confidence: clean laps/runs, tyre-age span, observed compounds,
  connector coverage and P10-P90 width.

High ESS can coexist with an uninformative broad posterior. A narrow interval can
also be misleading if a parameter is structurally unidentifiable or pinned by a
constraint/prior.

## Debugging a lap-to-lap jump

Capture and compare both snapshots before changing code:

1. leader lap, scheduled total laps and its source;
2. clean-lap fingerprint/count and traffic source;
3. accepted runs, compounds and team-compound pairs;
4. Race model architecture, evaluator backend, sample count and seed;
5. ESS, max weight and full P10-P90 widths;
6. Race-informed support state for each compound;
7. exact five-value tyre-input snapshot and provenance;
8. telemetry ingress/cut sequence, worker generation and queue depths;
9. accelerated-versus-legacy evaluator equality with a fixed sample bank.

Diagnostic scripts live under `tools/` for reproducibility. They are not imported
by production and their generated reports/CSVs belong under an ignored `temp/`
directory rather than the repository root.

## Regression references and known compromises

- With no manual values, all five numbers are exactly the legacy artifact values;
  the integration is a numerical no-op.
- The unchanged R11 FP evidence fixture has 8 strict LongRuns, 58 clean laps,
  rank 3/nullity 1, H-M about -0.0561 s/lap and S-M about -0.1897 s/lap; direct
  H/M and S/M performance are unsupported.
- Local cache freshness cannot detect a newer remote FP source.
- Pre-race marginal artifacts do not contain a joint covariance adequate for a
  fully faithful informative Race prior.
- Current cross-process consistency is refresh-boundary based, not instantaneous.
- Strategy projection still does not model overtaking, lapping or traffic.
- Live sector-distance metadata is unavailable, so Cumulative Delta uses fixed
  reference-lap time fractions rather than true physical distance fractions.
- Replay preserves every causal MC cut. Full-history catch-up can consequently
  remain substantially slower than the already completed visual replay.

Production code must not depend on `temp/`, `tools/`, prototype modules, or
diagnostic CSV output.

## R12 replay and visualisation closure

The canonical 2026 R12 recovered replay was rerun on 2026-09-06. Recovery still
produces leader lap 72, 1,343 completed laps, 4,002 completed sectors and 188
clean laps. Race state and Cumulative Delta reached lap 72 in approximately
28.5 seconds at maximum replay speed. The lossless MC tail completed in
approximately 696.3 seconds and published its final successful result for leader
lap 72. The apparent lap-39-to-45 freeze no longer occurs; normal race-state lap
advancement remained below roughly one second after startup while the backlog
was visible separately.

Profiling showed that MC sampling was not the stall source: after process startup
the MC operation normally took about 0.08–0.13 seconds. Historical physical
traffic-gap reconstruction during MC-input preparation grew to several seconds
per cut and dominated total catch-up time. The chosen fix preserves the 5,000
configured samples, clean-lap rules, tyre precedence and every causal historical
result. It accepts a long asynchronous tail and bounds large handoff objects
instead of reducing inference work or coalescing history.

R12 has no physical sector-boundary distances in its live snapshot. Its fixed
reference-time fractions are S1=`0.2183413557`, S2=`0.5434953773`, S3=`1.0`.
The resulting plot reaches lap 72. A focused STR retirement regression verifies
that the same selected-driver sector increments continue immediately before and
after retirement rather than switching reference population.

The saved R12 tyre history validates these direct source changes:

| Coordinate | Initial value | First direct live lap | First direct value |
| --- | ---: | ---: | ---: |
| SOFT degradation | 0.830 | 65 | 0.385815 |
| MEDIUM degradation | 0.070 | 64 | 0.259445 |
| HARD degradation | 0.045 | 31 | 0.217373 |
| SOFT compound delta | -0.160 | 65 | -0.711407 |
| MEDIUM compound delta | 0.000 | 64 | -0.672437 |
| HARD compound delta | 0.450 | 31 | 0.000000 |

Base and ratio-derived degradation remain visually distinct: base and derived
segments are dashed (derived uses hollow markers), while direct Race evidence is
solid with filled markers. This is a deliberate extra state beyond a binary
prior/live legend because a ratio-derived value is Race-informed but is not a
direct observation for that compound. Compound delta has only dashed-base and
solid-direct states. Old segments stop at the first point of the next source;
they do not persist beneath it.

The full woPlanner suite closes at 172 passed and the same 18 failing standard-
planner tests seen at clean `bff2263` and its parent. Seventeen also fail at
`5bb4f41`, before live-planner integration. They cover older lap-level fixtures
versus sector-expanded output, pit-default precedence, a now-required sample
parameter artifact and incomplete strategy-model test doubles. None was caused
by the R12 Replay/visualisation changes, so repairing them remains outside this
focused change.

## R13 live closure validation

The completed 2026 R13 live session was used as the final production smoke test
on 2026-09-06. Its raw recording contains 69,695 contiguous messages and ends at
leader lap 53 with `SessionStatus=Ends`, `Started=Finished`, a finalised session,
and a complete archive. The reconstructed Cumulative Delta plot also reaches lap
53.

Live model history contains 31 successful update laps (persisted as 62 matching
root/aggregate history rows) and no failed/stale publication. Effective
degradation progressed as follows:

- MEDIUM: `base` at lap 0, then `live_direct` from lap 17;
- HARD: `base`, `live_derived` at lap 17, then `live_direct` from lap 18;
- SOFT: `base`, then `live_derived` from lap 17 because no direct SOFT Race
  evidence became available.

History continued through lap 53. The final values were HARD 0.03310,
MEDIUM 0.07895 and derived SOFT 0.08428 s/lap. Strategy Prediction's last
actionable calculation was at lap 52 of 53; it had auto-recalculation enabled,
Manual Override disabled, and consumed all three effective degradation values
exactly from Update History lap 52. A final-lap strategy recalculation is not
useful once the race has finished, while model history still records lap 53.

The R13 marker smoke reconstructed the overlay through leader laps
10/20/30/40/50/53. At lap 53 the authoritative history produced 32 tyre-change
events, 32 scatter points and 33 managed plot items (one scatter layer plus 32
labels). All 33 items remained attached after an unchanged second redraw. This
confirms that normal Live and Replay use the same authoritative marker path and
that marker lifetime is independent of MC publication.

The marker correctness compromise is deliberate: `set_plot_data()` clears the
managed pyqtgraph lifecycle, so `_redraw()` rebuilds the complete, currently
authoritative marker overlay instead of keeping a second incremental marker
cache. This repeats work proportional to the small number of stint events, but
ensures corrections, recovery, range changes and curve replacement cannot leave
stale or missing overlays. Preventing plot clears or creating unmanaged permanent
items was rejected because either would weaken the existing redraw/range
lifecycle.

The live/replay queue compromise is shared by R12 and R13: Live is latest-wins
for unstarted analysis, while Replay is causal FIFO. Raw telemetry still uses
high/low-watermark flow control, but MC backlog no longer controls the replay
source. Historical max-speed Replay can therefore take longer to catch up;
intermediate causal results are not dropped and the GUI remains decoupled from
the CPU-heavy worker.
