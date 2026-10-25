# 2026-R18-Q — Prediction Resilience

Investigation, implementation and review completed 25 October 2026. All changes remain uncommitted. This report continues the preserved R18 diagnosis; it does not replace its historical evidence.

## 1. Executive verdict

**Qualifying now has an integrated FP-independent observation path and an experimental forward-looking final P16/P10 forecast. The intended FastF1-based R18 FP preparation still cannot complete with the currently returned upstream responses. The original live-session FP exception remains historically unresolved.**

The current reproducible acquisition failure begins upstream: installed FastF1 3.8.3 resolves the correct Austin FP1, FP2 and FP3 sessions, but F1 timing resources return HTTP 401 and its mirror returns HTTP 404. FastF1 logs these failures and returns an unloaded session. Strategy subsequently raises `DataNotLoadedError` when accessing its laps; Planner records and aggregates the three session failures. Writable clean-cache experiments and independent endpoint requests reproduce this chain. This establishes the currently failing layer; it does not establish why CloudFront returns 401 or what caused the handwritten original “cannot find” error.

**Genuine alternative FP data do exist now.** A bounded public OpenF1 check retrieved matching R18 FP1/2/3 lap/sector rows: 507/589/423 rows, 22 drivers each. They are not a validated replacement for the existi     ng FastF1-based preparation schema: in-lap/pit-entry/run-clock mappings remain unverified, several durations/sectors are absent, and complete run classification and operational defaults were not regenerated. Therefore neither “all R18 FP data are unavailable” nor “the complete FP workflow is restored” is supported.

What changed in this implementation:

- Credible Q completions can enter a separate analysis ledger without an FP pace reference. Operational UNKNOWN/FAST and Effective Finish remain unchanged.
- Exact late sector updates recover seven genuine R18 observations that the initial once-only admission implementation would have lost. Original tyre, physical-eligibility and reference context is frozen; future metadata is not backfilled.
- A final-cutoff model forecasts each driver's eventual best under joint historical field scenarios, then sorts the field for P16/P10. It works independently of FP, live Track Evolution and an already populated observed cutoff.
- Incomplete fields can receive a conditional scenario envelope without inventing unknown drivers' pace or publishing a point estimate.
- The normal replay/controller/service/UI pipeline displays the new products, source, uncertainty and support. Legacy Current and R&D Half-live remain distinct.

In the complete no-FP R18 replay, the new analysis retains **53 accepted observations, 45 comparable observations and 38 fitted observations from 14 drivers**, versus no supported legacy observations. The first Q2 forecast appears at sequence 2285, before observed P10 appears at sequence 4350. On 53 paired checkpoints from five chronologically held-out events, forecast MAE falls from **0.9647 s to 0.3492 s**. This is useful measured predictive value, not proof of prospective calibration: early-Q2 nominal 80% scenario ranges contain the target in only 40% of the five holdout cases at each early lead. The product remains **Experimental**.

### Acceptance gates

| Gate | Verdict | Evidence / remaining limit |
| -- | -- | -- |
| A — FP root cause | Current failure located; original incident unresolved; full workflow not restored | Direct, Strategy and Planner attempts share F1 401/mirror 404. Alternative OpenF1 rows exist, but full preparation mapping is unvalidated. No new local acquisition defect was demonstrated. |
| B — dependencies | Addressed | Raw-data, identifiability and implementation restrictions are separated below; genuine-data ablations quantify their effects. |
| C — resilient Track Evolution | Implemented and replay-verified | No-FP Q admission, distinct support counts, genuine RUS/ANT pairs, eventual signed live fit, late-sector recovery and UI publication. |
| D — resilient projected cutoff | Implemented in experimental mode | Whole-field forecasts before observed P10; conditional ranges for incomplete Q1; no zero-improvement substitution or fabricated driver pace. |
| E — predictive performance | Useful holdout improvement demonstrated; general calibration unresolved | Frozen R1–10 prior, R11–15 holdout, paired baseline comparisons and genuine-FP R14 audit. Five holdout events and completion-time reconstruction limit conclusions. |
| F — integration | Addressed for selected suites and recorded pipeline | 12,026-envelope service/UI replay, genuine late-FP recovery, 1,209 Planner regressions and focused cross-repository suites. Expensive historical suites excluded explicitly below. |

### Relationship to D01–D05

| Incident | Current classification |
| -- | -- |
| D01 — ALB Effective Finish | Existing prerequisite-dependent behavior preserved. No new race/polling defect established; pit-based finish remains independent. |
| D02 — FP analysis | Original exception historically unresolved. Current FastF1 source-access failure reproduced; earlier cache/optional-telemetry boundary fixes retained and tested. |
| D03 — Track Evolution failure | Missing-supported-observation mechanism established. The new analysis removes its unnecessary FP gate; the legacy path remains unchanged. The uncertain handwritten error is not treated as an exact exception. |
| D04 — RUS/ANT contributions | Genuine first/second Q2 pairs now contribute to the separate Q-only path when comparable support exists. Operational UNKNOWN is retained without FP. |
| D05 — R&D cutoff | Original zero-prior Half-live behavior is expected under its definition and remains visible. A separate forward-looking field forecast provides the new capability; no speculative Half-live correction was made. |

## 2. Data provenance and information loss

### FP acquisition: ownership and exact failure path

Interpreter: `/Users/zhxutong/dr-wo/.venv/bin/python`. FastF1 imports from its `lib/python3.11/site-packages/fastf1` directory, version 3.8.3. Inspection of the local fork's relevant request/header/fallback code supplied no concrete reason to replace the installed package. No package or fork was changed.

The direct test separately attempted discovery, `load(telemetry=False, weather=False, messages=False)`, lap/sector extraction and optional fastest-lap telemetry where possible. Strategy and Planner were tested afterward with equivalent temporary data/cache roots. Requests were bounded to relevant public endpoints with 5 s connection and 15 s read timeouts. This timeout wrapper was diagnostic, not a change to production request settings.

| Layer | Direct result | Integrated result | Failure evidence / conclusion |
| -- | -- | -- | -- |
| Upstream F1 source | Correct session paths, HTTP 401 with empty bodies | Same shared request path | 69 resource requests returned 401 across direct/wrapper/Planner attempts; 69 mirror requests returned 404. Independent exact-header FP1 SessionInfo/TimingData probes show no redirect and `FunctionGeneratedResponse from cloudfront`. Cause of that service response is unknown. |
| FastF1 discovery | Correct conventional United States GP FP1/2/3 identity and dates | Same event/session selection | Schedule/Jolpica requests succeed. Discovery is not evidence that lap data loaded. |
| FastF1 loading/parsing | Component retrieval fails; `Session.load()` returns, then `.laps` raises `DataNotLoadedError` | Same unloaded state | No lap parser or telemetry defect demonstrated because resource payloads never arrive. Telemetry availability cannot be established independently here. |
| Runtime/cache | Clean temporary directory writable; HTTP cache writes succeed | Planner uses the same redirected root and persists diagnostics | Existing 630-row HTTP cache inspected read-only via pickle opcodes, without unpickling. Only the 2026 schedule is relevant; no R18 timing payload is present. Three existing parser directories are empty. No cache permission defect reproduced. |
| woStrategy | Calls FastF1 load, then accesses laps | `_fill_missing_outlap_laptimes` raises before assigning `self._session` | Consequence of upstream failure, not an independently confirmed Strategy defect. |
| woPlanner | — | All three session loads fail; aggregate RuntimeError | Aggregate phase `practice_loading`; per-session phase `session_load`. Existing error/partial-session semantics work as designed. Operational extraction cannot run without a result. |
| Public OpenF1 alternative | Correct Austin sessions 11389/11390/11391, meeting 1297; lap/sector rows returned | No existing integration | Shows alternative observations are obtainable, not that the complete intended preparation can execute. |

The app path is [Strategy `Session._load_session` / `_fill_missing_outlap_laptimes`](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/core/session.py:36) → [Planner `_load_fp_laps`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:157) → aggregate diagnostic. Earlier defenses still create/probe the cache before enabling it and tolerate expected `DataNotLoadedError` only during optional telemetry enrichment, preserving valid sectors. They cannot create missing upstream payloads.

OpenF1 completeness checks found explicit outlap flags in every row, but one missing lap start per session; 22 missing lap durations per session; and missing S1 counts 74/67/73. S2/S3 are present in the fetched rows. A one-driver pit/stint audit supplies pit event dates/lap numbers and compound/age-at-stint-start, but no established mapping to exact `PitInTime`, `PitOutTime`, per-lap freshness and the current valid `OUT [PREP] PUSH (COOL PUSH)* IN` run schema. Missing fields were not fabricated. A separately validated alternative-source adapter is a follow-up opportunity, not an implemented repair or a demonstrated impossibility.

Exact request metadata, raw alternative responses, exceptions, roots and reproduction script are preserved under [FP acquisition evidence](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025/evidence/A/findings.md). The original live-session exception was not found in the preserved logs/artifacts; current HTTP failures cannot be retroactively assigned to D02.

### Dependency matrix

| Input | Source / actual R18 availability | Genuine requirement | Earlier restriction / implemented degradation |
| -- | -- | -- | -- |
| Event, part, driver, exact completion identity | SessionInfo and TimingData; available | Essential for causal ownership, deduplication and withdrawals | Preserved; unsupported old producers lacking exact identity receive an explicit exclusion. |
| Completed lap and owned sectors | 88 completion identities; 52 complete at arrival, seven others enriched later | Positive, consistent timing evidence for credible Q admission | No FP needed. One gross-sanity rejection; incomplete evidence stays pending or explicitly rejected at part transition. |
| FP pace-sector reference | No usable original R18 artifact; current FastF1 acquisition fails | Helpful operational reference, not mathematically necessary for a Q-only measurement | Legacy operational FAST gate retained; separate Q-only admission removes this modeling dependency. |
| COOL/PREP duration | Missing in the original no-FP configuration | Needed to certify some operational future-push/Effective Finish decisions | Not a prerequisite for accepted pace, repeated-driver fitting or historical opportunity scenarios. No duration is invented. |
| Tyre compound, freshness, installation | Known for all four RUS/ANT Q2 completions | Comparable conditions needed to reduce tyre/run confounding | Unknown freshness preserves accepted pace but excludes comparable slope support. |
| Auxiliary traffic | Often unavailable | Additional context, not a canonical slope prerequisite | Remains auxiliary; absent traffic does not erase Q observations. |
| Repeated observations per driver | First RUS/ANT Q2 pair gives one each; second pair gives another fresh installation | One point cannot identify that driver's slope | Accepted and fitted counts remain separate; no naive pooling of driver pace differences. |
| Current observed P16/P10 | Q2 P10 first appears at 4350 | Not required to sort simulated final driver bests | New forecast uses individual pace evidence and a prior before a boundary exists. |
| Current/previous driver pace | All 16 causal Q1 anchors available for Q2; GAS missing during captured positive-clock Q1 | Required for this V1 point model | Use Q1→Q2 transfer only when already known. Unknown Q1 pace receives no invented point; finite conditional rank envelope may still be available. |
| Historical future-best residuals | Genuine earlier Q tables and status/RC caches | Needed to estimate future improvement without a live slope | Frozen training-only empirical prior, with identity and source hashes. Missing/unsupported prior produces an explicit limitation. |
| Live Track Evolution slope | Eventually supported by repeated Q observations | Useful separate live diagnostic | Not an input gate or extra additive correction in the field model; empirical future-best residuals already contain typical common evolution. |

These distinguish **data unavailable** (e.g. no usable FastF1 FP payload), **information insufficient** (e.g. one comparable point for a driver) and **implementation restriction** (requiring operational FAST/FP before a physically credible Q completion can be analyzed).

### Quantified R18 ablations

Counts below are the end-of-recording active ledger unless stated otherwise. “Fitted” means actually used in per-driver regression, not merely admitted.

| Configuration / controlled ablation | Accepted pace | Comparable / fitted | Consequence |
| -- | --: | --: | -- |
| Original no-FP legacy | 0 | 0 / 0 | Operational FAST prerequisite prevents supported observations. |
| Genuine full R18 FP preparation | Not available | Not evaluated | FastF1 preparation fails; alternative raw OpenF1 rows are not misrepresented as equivalent prepared artifacts. |
| Q-only, no FP | 53 | 45 / 38 | Signed live fit from 14 drivers becomes identifiable. |
| Q pace, COOL absent | 53 | 45 / 38 | Same fit and field forecast; operational finish remains separate. |
| Only first observation per driver retained | 21 | 16 / 0 | Useful observations, no identifiable per-driver slopes. |
| Freshness removed | 53 | 0 / 0 | Pace retained; comparable evolution unavailable. |
| Auxiliary traffic absent | 53 | 45 / 38 | Same canonical fit. |
| Live fit withheld, driver pace retained | Unchanged | No slope supplied to field model | Field forecast unchanged by API/design. |
| Before observed Q2 P10 | — | — | 40 sampled prior-assisted forecasts available before the boundary exists. |

The prior genuine-data fixture also retains the reference × COOL operational prerequisite matrix at ALB 520. With a reference but no COOL, operational push-based finish remains unavailable; both explicit controls enable it. ALB 1446 independently finishes through the pit path. Those controlled numbers isolate mechanics; they are not genuine R18 FP calibration. Across eight before/after cases—four prerequisite configurations × original-envelope/native replay ingress—**all 3,968 selected pre-existing domain/numerical states match the preserved pre-change baseline**, including clock, lap ownership, driver states, legacy track rate and Half-live. New analysis products and diagnostic wording are excluded from that equality on purpose, not from their separate tests.

Raw source: `/Users/zhxutong/dr-wo/woData/wodata/live_timing/schema_v1/year=2026/round=18/session=Q/live_timing_e664885c31e14b29a0a2c3633090c1fd.jsonl`. SHA256: `533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17`. It contains 12,026 envelopes and starts late in Q1. The original recording, 496-envelope portable fixture, message identity and source/receipt timestamps remain intact. Native replay's existing source/clock interpretation differs from original SignalR delivery; the ALB 520 native countdown is 58 s, versus approximately 55.94 s through the original-envelope path. Neither was changed to match the handwritten ~48 s observation.

## 3. Track Evolution redesign

[`q_lap_quality` and `QOnlyTrackEvolution`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/q_only_track.py:17) compose the established TrackEvolutionPolicy without writing operational lap state. Admission requires an exact normal timed completion, driver identity, physical timing eligibility, no pit/outlap exclusion, finite positive lap time, three owned finite positive sectors whose sum agrees within 0.01 s, and the existing field +7% gross-pace guard. The contemporaneous best valid field time supplies the guard; if none exists, the first otherwise credible completion can establish its own reference. No fitted slope enters admission. This is a conservative gross check, not proof every accepted lap was an optimal push.

Accepted pace and tyre comparability are separate. The shared estimator retains the first eligible observation per fresh installation, same-modal-compound selection and at least two comparable observations per fitted driver. Driver-specific slopes use the established global qualifying-push coordinate and support weighting. One point remains visible without creating a slope. Existing support-quality thresholds remain unchanged; an identifiable fit can still be `LIVE_LIMITED`.

An exact-key pending ledger handles owned sectors arriving after the lap completion. It freezes original raw-driver/tyre/reference context, admits only when that same completion's sectors become sufficient, caps pending entries at 256, and closes pending work on part transition or explicit invalidation. Admission gets its actual causal publication index; earlier global indices are not renumbered. Published deletions/retractions withdraw observations through the shared lifetime ledger.

Full R18 results:

- 88 candidate completion identities; 36 initially lack complete owned sectors.
- 51 admissions at arrival, seven on later exact-key enrichment: 58 admitted over time.
- Five admitted identities subsequently withdrawn: **53 remain active**.
- Eight active pace observations fail tyre comparability: **45 comparable, 38 fitted**, 14 fitted drivers.
- One gross-sanity rejection; 29 incomplete candidates closed at segment transition; zero pending at recording end.
- First fit: **3966 / 21:30:26.236806 UTC / 576 s Q2 remaining**, two supported observations from one driver, correctly limited evidence. It precedes observed P10.
- Final session-wide signed slope, including subsequent Q3 evidence: **+0.008613943 s/global push**, `LIVE_LIMITED` with `RECENT_ESTIMATE_JUMP`. A positive estimate is retained rather than forced into improvement. Earlier Q2 values are negative; these are measured associations with remaining run/driver confounding, not isolated causal track changes.

RUS and ANT's Q2 completions are explicitly checked at 4568/4644 and 6555/6623. At the first pair, each contributes one comparable Q2 observation and cannot yet define its own slope. The second installations supply their repeated support; by 6623 the whole session ledger shows 31 accepted / 13 fitted observations, including their pairs. They remain operational UNKNOWN without FP, as required by the preserved operational contract.

The pipeline is native ReplayPlayer → controller queue/reducer → `QualiLiveService.consume` → Q-only publication → snapshot → main-window Q-only row and Plan Track Correction. The Plan plot uses the Q-only diagnostic when the legacy live fit is unavailable, clearly labels that source, and retains legacy correction separately. The new slope is not silently installed into Effective Finish, Current or Half-live.

## 4. Projected cutoff redesign

### Field-level model

[`ForecastPrior.from_records` and `forecast_final_cutoff`](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/quali_forecast.py:84) implement a small deterministic empirical field-scenario model. Runtime inputs contain only event/part, observed remaining time, participant identity, current valid best, already-known previous Q1 best for Q2, and any explicitly certified no-further-attempt constraint. There is no observed-cutoff argument or final-result input.

For each earlier training event, the model:

1. Selects/interpolates that event's empirical residual vector at the current remaining time and rank-matches causal pace anchors.
2. Applies current-best→final-best fractional residuals, or Q1→Q2 residuals where a Q2 current best is not yet available.
3. Keeps the whole historical field vector together, preserving empirical common improvement instead of independently drawing Gaussian driver changes. Zero-future-attempt cases remain in those historical outcomes.
4. Bounds each scenario by already valid current bests, and pins a driver only when the caller has actual no-further-attempt evidence.
5. Sorts all scenario driver finals and reads fixed P16/P10; publishes the median and central empirical 80% scenario range across at least two supported earlier events.

The result responds to non-boundary drivers' pace/opportunity changes; unit tests check that explicitly. Remaining-time opportunity is a historical marginal, **not** a certification that another push fits a fabricated COOL/outlap duration. At runtime only genuine normal-finish plus pit/final-lap evidence currently certifies no further attempt. Zero clock or a bare Inactive status does not finalize classification; already-running laps may arrive afterward. Explicit finalized classification returns its observed order statistic.

FP and the live track slope are deliberately not added to this residual model: doing so without a separately trained conditional model would double-count typical improvement. Genuine FP still supports operational readiness and the legacy estimator. V1's new field model is the **same reduced-input model when FP is present**, not a falsely advertised richer full-input model.

### Incomplete fields and source hierarchy

[`forecast_partial_field_bounds`](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/quali_forecast.py:389) preserves information when a driver lacks a causal pace anchor. With m unknown cars and target rank r, the known-field order statistic can shift between r−m and r. The helper encloses every allowed unknown-car rank placement, all relevant piecewise-linear residual knots and every supported historical field scenario. Per-car extrema conservatively overenclose joint placement constraints.

It returns **no point, no unknown-driver pace, no probabilities and no nominal interval coverage**. Its envelope is conditional on the empirical model, not a guaranteed bound on actual future results. If a finite endpoint cannot be established, the result remains unavailable.

| Evidence state | Published result |
| -- | -- |
| Complete causal field anchors + supported earlier-event prior | Experimental prior-assisted final P16/P10, empirical range; independent of a live slope or observed boundary. |
| Incomplete anchors + finite conditional bounds | Partial-field scenario envelope; no point estimate. |
| Too little evidence, missing/invalid prior, unsupported lead/identity | Explicit limitation; no copied current cutoff. |
| Genuine finalized classification | Observed final classification, labeled as such. |
| Credible Q observations but insufficient repeated tyre-comparable support | Accepted observations remain visible; no unsupported live slope. |
| Sufficient repeated comparable Q observations | Separately labeled Q-only live fit, with quality/support status. |

The prior is bundled in [quali_forecast_prior_v1.json](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/quali_forecast_prior_v1.json), revision `empirical-r1-r10-v1-frozen-20261025`. Its SHA256 is `46d8a38fc0dbf4b318c91394f7d1597b30bcc165c455dd9a6ba127b91f7e0cfb`, identical to the evaluated artifact. Runtime rejects same/future-event calibration. Source event lists, training-record/source hashes and experimental validation status are preserved in its metadata.

### Genuine R18 checkpoints

The following native service/UI values use only information published by each sequence. All times are seconds; the UI rounds the experimental forecast to 0.1 s. Existing means observed cutoff with the original zero-prior legacy correction/Half-live. Point ranges are empirical historical scenarios; Q1 partial ranges have a different, nonprobabilistic interpretation.

| Checkpoint / receipt UTC | Inputs and Q support | Existing output | New output | Source / uncertainty |
| -- | -- | --: | -- | -- |
| Q1 initial 6 / 21:15:47.057882, 135 s | 21 current bests; GAS unknown; no supported fit | 96.770 | No point; 95.662–96.732 | Conditional partial-field envelope, seven source events |
| ALB 520 / 21:17:06.963911, 58 s | ALB completion; 1 accepted / 0 fitted; GAS still unknown | 96.770 | No point; 95.7471–96.7700 | Same conditional source; no future GAS backfill |
| ALB pit 1446 / 21:19:22.965611, 0 s | Pit-based finish independent of FP; late laps possible | 96.240 | 96.175 [96.175, 96.240] | Historical field prior; not prematurely finalized |
| Q2 start 2285 / 21:24:01.071961, 900 s | All 16 causal Q1 anchors; no observed P10 or live slope | Unavailable | **95.2521 [95.0527, 95.3046]** | Joint historical field prior |
| First Q fit 3966 / 21:30:26.236806, 576 s | 16 accepted / 2 fitted; no observed P10 | Unavailable | 95.0917 [94.9262, 95.2484] | Field prior independent of that limited live slope |
| First P10 4350 / 21:31:19.214990, 523 s | Current field boundary now available | 96.031 | 95.2489 [94.6877, 95.3269] | Whole-field historical scenarios |
| RUS first 4568 / 21:31:51.986370, 491 s | RUS one comparable Q2 point | 95.494 | 95.1093 [94.4728, 95.2836] | Whole-field historical scenarios |
| ANT first 4644 / 21:32:03.863059, 479 s | ANT one comparable Q2 point | 95.397 | 94.9917 [94.4206, 95.2830] | Whole-field historical scenarios |
| RUS second 6555 / 21:39:43.740812, 19 s | 30 accepted / 11 fitted, RUS repeated installation | 95.397 | 95.397 [95.2317, 95.3970] | Historical median predicts no boundary change; not a zero-prior fallback |
| ANT second 6623 / 21:39:52.647650, 10 s | 31 accepted / 13 fitted, ANT repeated installation | 95.397 | 95.397 [95.2470, 95.3970] | Same model, uncertainty remains |

Q2's eventual P10 is 95.058 s. Across 104 sampled Q2 checkpoints, new point availability is 104/104, MAE 0.12962 s, RMSE 0.15933 s, bias +0.10431 s, descriptive scenario-range containment 91.3%. Observed P10 is available at 64/104 and has all-available MAE 0.37069 s; these differing available sets are **not** a paired accuracy comparison. Recorded green-clock coverage is 100% of 900 s for the new forecast versus 58.1% for observed P10.

The recording captures only Q1's last 135 positive-clock seconds. GAS has no causal best during that span; point availability is correctly zero, rather than reconstructed from later data. Sixteen of 17 sampled point-unavailable Q1 states have finite conditional ranges, all 16 containing the eventual cutoff, mean width 1.002 s. The initial range covers nearly the whole captured positive-clock span. Four sampled postflag states have point forecasts. Neither result describes the uncaptured earlier Q1 period.

Exact full-service checkpoint/UI payloads are in [native-r18/checkpoints.json](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025/evidence/coordinator/native-r18/checkpoints.json); sampled metrics and raw admission manifests remain alongside the evaluation outputs.

## 5. Historical predictive evaluation

### Protocol and leakage safeguards

Fifteen genuine local Q lap tables, 2026 R1–R15, plus corresponding raw SessionStatus/DriverList/race-control caches, support this evaluation. Training is fixed to R1–10; R11–15 are an untouched chronological event holdout. There are 269 checkpoint records: 179 training, 90 held out, across Q1/Q2 leads 900, 780, 600, 420, 300, 180, 120, 60 and 0 s. Fifty-five training records lack complete anchors/final targets and are excluded, leaving 124 supported templates. No parameters were revised after inspecting held-out errors or R18.

Started/Aborted/Finished spans reconstruct the green clock, excluding red-flag pauses. No finish time is inferred from the last lap. Austria Q2 zero is omitted because its Finished status occurs about 1.37 s before nominal zero. The first already-known UTC/Started bridge maps race-control notices; later bridges only audit spread. Most spreads are under 0.05 s; Austria is 1.372 s.

An identity inconsistency is recorded rather than hidden: Miami raw SessionInfo declares Meeting.Number 6 although its cached lap partition is Round 4, and Monaco also declares 6. Cached sources are joined by verified EventName within the year; the original declarations remain in provenance.

Feature construction does not gate earlier laps on terminal Deleted/IsAccurate/FreshTyre flags. Matching timed race-control notices remove laps only once known. An independent audit checked 48 Q1 deletion/reinstatement notices across the 15 events; none relevant to these Q2 anchors occurred after Q2's first checkpoint. Final classifications/future-attempt labels are training targets or held-out scoring targets, never runtime forecast features. The runtime input allowlist contains only causal current/previous bests and identities.

**Historical evaluation is completion-time reconstruction, not receipt-exact replay.** Retrospectively stored lap rows do not reveal original sector-publication latency or all correction arrival times. Real R18 packet replay and R14 packet/recovery audits separately exercise actual native message processing. This distinction limits the historical causal claim.

### Frozen holdout results

Accuracy below uses the **same 53 checkpoints where both forecasts exist**. The original no-FP/default-zero legacy projection equals the observed-cutoff baseline there.

| Metric | Observed / existing no-FP baseline | New field point forecast |
| -- | --: | --: |
| Paired MAE | 0.964717 s | **0.349226 s** |
| Paired RMSE | 2.905060 s | **1.407789 s** |
| Paired bias | +0.964717 s | +0.191075 s |
| Point availability, all 90 | 69/90 (76.7%) | 65/90 (72.2%) |
| Nominal 80% scenario containment, all new points | — | 49/65 (75.4%) |
| Scenario containment, paired 53 | — | 43/53 (81.1%) |

The new model improves 37 paired checkpoints, worsens 11 and matches five. Each of the five events improves in paired average error, but this is not 53 independent races. Event-level observed/new MAEs are R11 0.7808/0.2503, R12 1.4089/0.1105, R13 0.2814/0.1992, R14 0.6069/0.1314 and R15 1.4572/0.9045 s. Available Q1 point forecasts have MAE 0.16172 s and availability 20/45; Q2 has MAE 0.38700 s and availability 45/45. Conditional accuracy must not conceal unavailable states.

| Remaining lead | Paired observed MAE | Paired new MAE | New point availability | Nominal 80% containment |
| -- | --: | --: | --: | --: |
| 900 s | No observed boundary | — | 5/10 | **40.0%** |
| 780 s | No observed boundary | — | 5/10 | **40.0%** |
| 600 s | 10.4920 s | 3.6197 s | 5/10 | 100.0% |
| 420 s | 0.7155 s | **0.1514 s** | 8/10 | 100.0% |
| 300 s | 0.5073 s | **0.1661 s** | 8/10 | 75.0% |
| 180 s | 0.3919 s | **0.1582 s** | 8/10 | 62.5% |
| 120 s | 0.3919 s | **0.2072 s** | 8/10 | 75.0% |
| 60 s | 0.2606 s | **0.1956 s** | 9/10 | 77.8% |
| 0 s | 0.1397 s | 0.0474 s | 9/10 | 88.9% |

Early Q2's five predictions at each 900/780 s lead have MAE about 0.206 s, but their ranges are too narrow. The worst held-out case is R15 Q2/600 s: observed 118.309, new 114.8123, eventual 104.683 s. An official slow/cool early best is a poor pace anchor. Individual deterioration includes R13 Q1/300 s, baseline error 0.275 → forecast error 0.408 s, and R13 Q2/180 or 120 s, 0.050 → 0.182 s. These failures were not tuned away using holdout results.

For the 25 point-unavailable holdout states, the separate partial-field helper yields 14 finite conditional ranges; 13 contain the target, mean width 4.916 s. Combined point-or-range availability is **79/90**, not 79 point estimates. Containment of these nonprobabilistic envelopes must not be reported as nominal 80% interval coverage or folded into point MAE/RMSE.

### Genuine FP baseline and recovery

A real R14 recording audit compares the same 20 Q2 updates with preserved matching genuine-FP legacy outputs. Observed, genuine-FP legacy and new field MAEs are **0.27295 / 0.26073 / 0.06203 s**; RMSEs are 0.60845 / 0.42076 / 0.11597 s. New Q2 time coverage is 100% versus 67.4% observed. R14 is already one of the five holdout events, not extra independent validation. There is no claim of multi-event full-FP legacy evaluation.

A separate native R14 recovery run starts with empty FP resources, loads the genuine matching R14 artifact at sequence 3000, and processes through 4000. The real 132.118 s COOL value and FP pace reference become available. Q-only observation identities/counts survive; repeating the same raw state adds no duplicates; the field forecast is preserved; another 1,000 original messages proceed normally. R14 FP data are never passed off as R18. Artifact hashes and assertions are in [r14_genuine_fp_recovery.json](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025/r14_genuine_fp_recovery.json).

### Interpretation

The model meets the useful-point-prediction comparison on this holdout, including relevant 420–60 s leads. It has not established prospective calibration, all-circuit/weather transfer, useful early Q1 point availability or superiority to every supported legacy configuration. Weather, compound, run tactics and future deletions are not explicitly modeled. Current valid bests bound scenarios even though future retractions could remove them. These limitations require experimental presentation and more independent evaluation, not automatic promotion.

Reproduce from woPlanner with the project interpreter and writable plotting cache:

```bash
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
  /Users/zhxutong/dr-wo/.venv/bin/python scripts/evaluate_quali_resilience.py \
  --evaluate --r18 --round14-legacy --analyse-collected --genuine-fp-recovery --partial-bounds
```

The script reads local source data; the bounded R14 recovery stop is intentional. Inventory/hashes, training records, prior, predictions, per-lead/source metrics, ablations and partial-range scoring are preserved in [evaluation outputs](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025).

## 6. Implementation review

### Ownership and files

The coordinator delegated acquisition to Agent A, independent dependency/evaluation work to Agent B and pure model design/tests to Agent C. The coordinator implemented the Q-only admission/service/UI integration and reviewed the pure model; C independently reviewed the integration and calibration evidence. File ownership was kept separate. No further agents were created.

| File / function | Change this round |
| -- | -- |
| [woStrategy `quali_forecast.py`](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/quali_forecast.py) | New frozen prior schema, joint field scenarios, chronological guards, point forecast and partial-field envelope. |
| [woStrategy `quali_forecast_prior_v1.json`](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/quali_forecast_prior_v1.json) | Frozen R1–10 calibration with source/training provenance; no R11–15/R18 training. |
| [woStrategy `test_quali_forecast.py`](/Users/zhxutong/dr-wo/woStrategy/tests/test_quali_forecast.py) | 19 deterministic cases, including non-boundary influence, common dependence, no-prior behavior, leakage guards, clock-zero/finalization and partial-envelope brute-force containment. |
| [Planner `q_only_track.py`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/q_only_track.py) | New causal quality/pending-sector adapter around the existing estimator and lifetime ledger. |
| [Planner `service.py`: consume / `_final_cutoff_payload`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:506) | Independent Q-only publications, causal pace extraction, bundled prior loading/caching, finalization guards and forecast snapshot payloads. |
| [Planner `models.py`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/models.py:95) | Defaulted optional fields for the two analysis products; existing constructor compatibility retained. |
| [Planner `track_evolution_policy.py`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution_policy.py) | Optional diagnostic-builder injection isolates the two policies; estimator thresholds and original default behavior unchanged. |
| [Planner `gui.py`: `_render_resilient_predictions`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:433) | Explicit experimental final forecast/range and Q-only support row, reset handling, source tooltips, observed-final labeling. |
| [Planner `widgets.py`: `refresh_live_track`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/widgets.py:833) | Q-only graph/source when legacy fit is unavailable; both products retained in detail JSON with legacy top-level keys preserved. |
| [Planner `track_evolution.py`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution.py) | Plot axis says selected qualifying laps, avoiding the false claim that Q-only samples are operational FAST. |
| [Planner `test_quali_resilient_forecast.py`](/Users/zhxutong/dr-wo/woPlanner/tests/test_quali_resilient_forecast.py) | 12 cases: genuine no-FP pairs, early Q2 point, partial Q1 range, recovery, quality/tyre/sector lifecycle, finalization/cache/bad-clock guards and GUI. Reuses the preserved minimal fixture. |
| Existing GUI/version-skew tests | Expected legacy label updated; old producer's exact-identity exclusion asserted separately while retaining the original domain comparison. |
| [Planner evaluation script](/Users/zhxutong/dr-wo/woPlanner/scripts/evaluate_quali_resilience.py) | Reproducible chronological inventory/calibration/evaluation, packet audits, ablations and genuine-FP recovery. |
| [QUALIFYING_MONITOR.md](/Users/zhxutong/dr-wo/woPlanner/doc/QUALIFYING_MONITOR.md:322) | Documents separate products, uncertainty, evidence ownership, partial fields and acquisition limits. |

The earlier uncommitted FP reliability/readiness changes in analysis, Half-live, presentation and documentation were preserved. This round does not claim them as newly repaired acquisition defects. No production woData, woWeekend, unrelated Strategy algorithm or FastF1 code was changed.

### Tests and integration evidence

| Validation | Result |
| -- | -- |
| Planner qualifying + SessionInfo focused regression selection | **1,209 passed, 3 deselected**, 73.56 s. |
| Final GUI/resilience selection after observed-final labeling review | **69 passed**, 4.36 s; overlaps the broader selection, not additive unique coverage. |
| Strategy forecast/pre-quali/performance/phase suites | **66 passed**, 1.34 s; one existing pandas FutureWarning in a test fixture. Includes 19 new model cases. |
| woData reducer/ownership/history/speed-coordinate suites | **168 passed**, 1.97 s. |
| woWeekend qualifying suite | **3 passed**, 0.60 s. |
| Entire R18 recording through native replay/controller/service/main GUI/Plan | **12,026 envelopes**, no dropped/delayed/duplicated replay injections; checkpoint assertions passed. |
| Legacy domain/numerical before/after fixture comparison | Eight cases × 496 messages; every selected state matches preserved baseline. |
| Genuine R14 FP recovery | Native first 4,000 envelopes, real artifact at 3,000; all four recovery checks passed. |
| Independent calibration audit | Evaluated/packaged prior equal; training-record hash and all 40 training source hashes match; split and Q1 notice causality checked. |
| Git whitespace check and preservation checks | Passed; raw R18 recording/original diagnosis and unrelated pre-existing modified files retain their hashes; branches/HEAD unchanged. |

The broad selection intentionally omits `test_quali_replay_determinism.py`, `test_quali_r14_replay.py`, `test_quali_r17_replay.py`, `test_quali_r16_projection_identity.py`, and `test_quali_r16_replay.py`, plus three expensive R16 full-recording nodes in historical-cutoff/red-line files. These were not run and are not described as passing. The exact selection is preserved in [regression-command.json](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025/evidence/coordinator/regression-command.json). The complete R18 and bounded genuine R14 native passes complement those exclusions. A distribution wheel build was not verified because hatchling is absent; no build dependency was downloaded. Source-package resource loading works in the installed editable runtime.

Initial development regression failures exposed duplicate-publication timestamps in the compact Q result, missing old-producer identity, cross-policy mocked diagnostic builders and legacy detail JSON compatibility. These were corrected, then the broad selection passed. They are not outstanding failures. An initial native harness used an unresolved `/tmp` symlink with isolated-path checking; the harness was corrected to a resolved temporary root. No product safety check was relaxed.

Independent review also found two substantive implementation issues, both corrected and tested: bare Inactive incorrectly marking classification final, and once-only admission losing seven later sector-enriched completions. Additional finite-clock protection makes the new adapter return unavailable for NaN/inf without rewriting the underlying domain clock. The final GUI review ensures observed-final output is not mislabeled as a historical forecast.

### Runtime and operational risks

Pure point/range calculations take approximately 2.1–2.3 ms in local probes. The roughly 500 KB prior is read once, with validation at loading and model boundaries; results are cached until causal pace/roster/opportunity, part, genuine finalization or a 30 s remaining-time bucket changes. Calculated remaining time is exposed in diagnostics. No large Monte Carlo or online recalibration runs per message, and no external requests are added to the live forecast path.

The complete 69-minute R18 capture processes through the native UI harness in **83.80 s**, with 139 field calculations. The largest measured poll of up to 37 queued messages takes **0.559 s**; this is an end-to-end batch measurement, not a claim of sub-100 ms GUI latency or isolated model overhead. Existing GUI work and large replay bursts still merit profiling if responsiveness is a concern. The existing synchronous FP analysis action was not refactored into a worker.

The biggest modeling risks are weak early-Q2 range calibration, a slow current-best anchor, limited historical event count, cross-circuit transfer, omitted weather/tyre/tactics and unmodeled future deletions. Q-only evolution still contains run/driver error confounding, and the final positive signed slope demonstrates why its quality warning matters. None of these are hidden by a numeric zero or an optimistic source label.

All preserved evidence is under [outputs/r18_prediction_resilience_20261025](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_prediction_resilience_20261025). The coordinator's full native harness, raw checkpoint snapshots, screenshots, test commands/logs, comparisons and preservation manifest are included there. Temporary clean caches are not duplicated into the repository. Original large recordings are referenced and hashed, not copied.

## 7. Next qualifying weekend

If FP acquisition fails again, the failure dialog identifies event/session, failing phase, cache/resource/output roots and diagnostic JSON. Available successful FP sessions still survive under the earlier partial-success behavior. The tool does not invent FP pace or COOL defaults.

Live timing, completed-lap identities, owned sectors, current bests, historical Q1 results and tyre messages continue through the normal reducer. Operational FAST/Effective Finish retains its existing prerequisite contract, with missing-input explanations. Independent pit-based finish still works.

The Q-only row first shows accepted observations without claiming a slope. Complete credible laps remain useful even with unknown tyres. As fresh, same-compound repeated installations support a driver fit, a signed Q-only estimate appears, with limited-evidence status where applicable. Exact late-sector updates can admit a pending lap once; explicit invalidations withdraw it. No observation is promoted into operational FAST merely to enable modeling.

The final P16/P10 row is explicitly Experimental. With complete causal anchors and supported earlier-event scenarios, it shows a forward-looking point and historical scenario range, even before observed P10 or a live track slope. Q2 can use already-published Q1 pace. If a driver is untimed, a finite conditional envelope can be shown without a point; with insufficient evidence it explains the limitation. It never universally falls back to current P10/P16 or zero improvement.

Current (legacy projection) and R&D Half-live stay visible for comparison, with their own original formulas and zero-prior explanation. The new field prior is an explicit historical source, not a live measurement. Uncertainty is shown in the main forecast row; detailed source/training/support assumptions remain accessible in tooltips and Plan details.

If genuine matching FP preparation arrives later, ordinary artifact reload supplies operational references/defaults and may improve the legacy path. Q-only observation identities and the independent field forecast are preserved; no restart, observation reset or duplicate backfill is required. V1 does not pretend that adding FP silently gives its field forecast a newly calibrated full-input model. Finalization requires real evidence, allowing laps already in flight to complete after zero.

## 8. Remaining decisions

No user decision is required to use the implemented experimental products. **Promotion to the primary/default forecast is not justified yet**: it should wait for additional independent event and prospective interval validation. That is the substantive future release decision, not a permission request to finish this work.

Remaining evidence gaps are specific:

- The original D02 exception/traceback is absent; current source failures cannot identify it retrospectively.
- The reason for F1's CloudFront-generated 401 is not established. The default FastF1 FP workflow cannot be declared restored until real payloads and full downstream artifacts succeed.
- OpenF1 offers a possible alternative acquisition route, but exact pit/run timing, validity and freshness mappings need documentation and genuine-source validation before integration. It is not necessary to invent them to use Q-only forecasts.
- More receipt-exact historical recordings and independent events are needed for general accuracy and interval calibration; the cached reconstruction cannot recover unpublished message timing.

Actionable continuation in this same conversation, with all evidence already saved:

1. Keep the bundled point model and conditional ranges experimental; collect the next qualifying predictions, sources and eventual targets without changing the frozen R1–10 benchmark.
2. Validate an alternative FP adapter against genuine known FastF1 practice sessions: exact pit-entry/exit times, lap validity, compound/freshness and run-role/default agreement. Implement only the mappings supported by that comparison, with recorded source provenance. Separately retest the default F1 endpoints when access changes.
3. Evaluate robust early current-best anchoring and early-Q2 uncertainty on additional training events, then score on a new untouched event set. Do not tune using the five holdout errors reported here and present that as independent improvement.
4. Consider a richer FP/tyre/weather/live-evolution conditional field model only if an independent comparison establishes added predictive value without double-counting empirical improvement. Preserve legacy timing, fixed target ranks, signed units and exact observation lifetime throughout.

No commits, pushes, branch changes, raw-recording replacements or unrelated algorithm changes were made.
