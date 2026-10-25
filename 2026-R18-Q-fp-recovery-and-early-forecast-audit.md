# 2026-R18-Q — FP Recovery and Early Forecast Audit

Completed 25 October 2026. Coordinator reviewed the acquisition, forecast-research and independent integration workstreams. Previous reports, frozen benchmarks, recordings and user changes are preserved. No commits, pushes or branch changes were made.

## A. FastF1 recovery verdict

**The authenticated FastF1 workflow is restored and verified in the intended interpreter.** Genuine R18 FP1, FP2 and FP3 data were fetched from the archive with FastF1 3.9.0, processed through the complete application analysis, saved, reloaded and used in native qualifying service/UI replay. This is supported by real fresh responses as well as separate cache, mock and integration evidence.

| Required question | Verdict |
| -- | -- |
| Is 3.9.0 compatible? | Yes for the inspected runtime and tested consumers. Python 3.11.15 and requests-cache 1.3.2 meet its requirements. Isolated tests preceded the runtime upgrade; actual-runtime tests followed it. |
| Does authenticated archive access work? | Yes. Official browser authentication completed; a later process recognized the stored token. Fresh archive responses returned 200, including timing, car/position and subsequently weather resources. |
| Can genuine R18 FP1/2/3 load? | Yes: 507 / 589 / 423 lap rows and 22 drivers in each session. |
| Does complete FP analysis execute? | Yes: Strategy Session → Planner analysis → run segmentation → duration/reference/progression artifacts; 1,519 rows saved and reloaded. |
| Are operational prerequisites produced/reloaded? | Yes: usable common pace checkpoints and measured OUT/IN/COOL/PUSH durations. Artifacts are now in the normal R18 workspace location. |
| Was a parser/wrapper rewrite necessary? | No. Installed FastF1 was upgraded; the local fork, generic Strategy Session wrapper and qualifying algorithms were not rewritten. The active cache call and diagnostics were updated; GUI acquisition was isolated in a subprocess. |
| What remains uncertain about the original error? | The exact handwritten “cannot find” exception and original UI/runtime settings remain unrecorded. Authentication explains the reproduced unauthenticated access failure, but does not reconstruct that historical exception. |
| Ready for the next qualifying weekend? | **READY for the verified FP preparation/integration/fallback workflow**, subject to routine preflight and sign-in renewal. The forecast remains experimental; READY does not mean its intervals are calibrated. |

### What caused the reproduced acquisition failure

The prior installed 3.8.3 runtime had no archive authentication. The documented October archive-access change explains the reproduced F1 401/mirror 404 pattern. FastF1 3.9.0 supplies the supported authentication mechanism. This was a missing authenticated-access capability/configuration, rather than evidence that our lap parser needed replacing. [Official issue](https://github.com/theOehrly/Fast-F1/issues/978), [3.9.0 release](https://github.com/theOehrly/Fast-F1/releases/tag/v3.9.0).

The official browser/Companion flow completed using the existing local F1 TV session. Its CLI exited successfully after the local callback. A separate process recognized a stored, locally unexpired token, and real archive requests established access. Neither subscription absence nor a regional/tier incompatibility was assumed; successful requests demonstrate adequate access for this account and environment now. No password, cookie, token value or callback payload was requested in chat or copied to the report/evidence.

### Environment and compatibility

The intended executable is `/Users/zhxutong/dr-wo/.venv/bin/python`; FastF1 imports from that environment's own `site-packages`, not the fork or temporary overlay. Package inventories contain 59 entries before and after: **only FastF1 changed, 3.8.3 → 3.9.0**. `pip check` reports no broken requirements.

Isolated testing first placed the exact 3.9.0 wheel ahead of the existing dependencies. It verified the consumed cache/session/telemetry/private API interfaces and focused repository tests. The production wheel was installed only after those checks. Existing requests-cache 1.3.2 satisfies the new minimum 1.3.0; numpy, pandas, matplotlib, scipy and other active requirements already satisfy upstream constraints.

Minimal dependency declarations now require `fastf1>=3.9,<4` in Strategy and woData's live extra. Strategy's Python minimum is raised to 3.10 to match FastF1. Planner/Weekend already require Python 3.10 and use the dependency transitively. Other deprecated cache/utilities still work in 3.9; no global migration or unsolicited dependency upgrades were performed.

The 107%/results audit found no need to change live qualifying ranking: it uses the raw reducer, not FastF1 Session.results. Compatibility tests use the real SessionResults class to cover retained out-of-107% Q1 times, `ClassifiedPosition='N'`, and populated practice Position/Time. The generic loader prefers numeric Position, so N is not converted to a numeric rank. Newly populated practice ranks can activate its existing official-rank preference; this is supplied upstream information, not a new ranking algorithm. Removed `delta_time` has no production consumer; remaining deprecated utilities/private interfaces are recorded for future upgrades.

### Real acquisition versus cache/mocks

| Experiment | Actual result | What it establishes |
| -- | -- | -- |
| Cold direct 3.9.0, R18 FP1/2/3 + 2025 R18 Singapore FP3 reference | 36 HTTP responses, all 200: 28 fresh F1 archive, four schedule and four results responses | Genuine timing acquisition and parsing in the production interpreter. |
| Telemetry-enabled follow-up for the same four sessions | 16 archive responses, all 200; 34,225,440 fresh bytes | Car and position data for all 22/22/22/20 drivers; fastest-lap telemetry is accessible. |
| Separate warm lap process | Eight metadata responses, all cache hits; same counts/results | Cache reuse and process independence, **not fresh access**. |
| Separate warm telemetry process | Cached CarData/Position products and same fastest-lap telemetry | Optional telemetry also survives process restart/cache reuse. |
| Full Planner preflight on the genuine acquisition cache | All three FP sessions succeed; 1,519 artifact rows reload; pace/COOL ready | Complete real application preparation beyond direct Session.load. |
| Parsed-cache provenance check | Explicit cache-hit counts for timing, tyre, car/position and other functions; no fresh timing responses | Warm operation is identified, not advertised as a fresh acquisition. |
| Network-enabled application preflight | Three weather resources return authenticated 200; full preparation succeeds | Application acquisition handles real signed responses too. Early sandbox DNS failures were environmental, not parser defects. |
| Final fresh metadata probe after response-hook review | One uncached R18 SessionInfo response, 200, authentication attached | Current token acceptance and final observer correctness; distinct from the broader cold timing experiment. |
| Missing/expired/corrupt/rejected token and 401/403/404 tests | Controlled temporary token managers/HTTP mocks | Failure handling and bounded renewal; **not real entitlement or fresh-acquisition proof**. |

The preliminary telemetry call made after deliberately loading with telemetry disabled correctly raised DataNotLoadedError. Proper telemetry-enabled loads then succeeded; it was not treated as a parser defect.

Direct raw FastF1 completeness:

| Session | Rows / drivers | Non-null lap times and S1 | S2 / S3 | PitOut / PitIn | Compound / Stint / TyreLife |
| -- | --: | --: | --: | --: | -- |
| R18 FP1 | 507 / 22 | 433 | 507 / 507 | 94 / 94 | All 507 rows |
| R18 FP2 | 589 / 22 | 522 | 589 / 589 | 84 / 84 | All 589 rows |
| R18 FP3 | 423 / 22 | 350 | 423 / 423 | 92 / 92 | All 423 rows |
| 2025 R18 FP3 compatibility reference | 440 / 20 | 367 | 425 / 421 | 97 / 97 | All 440 rows |

Missing raw fields are not fabricated. The existing Strategy wrapper's timestamp-based outlap completion and existing FP validity/run rules remain in force. All practice result rows contain Position/Time. Fastest-lap telemetry has 721/715/730 rows in R18 FP1/2/3 and 696 in the earlier reference; these are lap telemetry row counts, not total session samples.

### Full application result and qualifying recovery

The genuine common reference is HAM, FP3 lap 12, 94.893 s: first 5% checkpoint **3.630 s**, S1 **25.661 s**, S2 **37.550 s**. The existing run parser retains 115 valid-run rows and derives:

| Duration | Selected value | Selected support |
| -- | --: | --: |
| OUT | 117.3325 s | 34 |
| IN | 134.426 s | 37 |
| COOL/PREP | 140.862 s | 3 |
| PUSH | 96.243 s | 38 |

These are measured artifact values, not defaults inserted to force readiness. COOL's three samples are sparse. The existing FP progression prior has four fitted observations and rate −0.2018333 s/field push; restoring that input does not validate its predictive accuracy or remove its sparse-support limitation.

Four newly generated files—lap_roles.csv, duration_summary.csv, track_progression.csv and manifest.json—were copied into the previously absent normal location:

`/Users/zhxutong/dr-wo/woData/wostrategy/pre_quali/schema_v1/year=2026/round=18`

No existing artifact set was replaced. A normal-root reload independently returns 1,519 laps, the real pace reference and supported COOL duration.

Native replay/controller/service/main-window/Plan verification:

- **All 12,026 original envelopes pass with genuine FP preparation loaded.** ALB 520 now triggers the existing effective-finish rule using genuine prerequisites. Supported legacy live Track Evolution appears; Plan correctly prefers that supported legacy diagnostic while the separate Q-only row remains visible.
- Q-only results remain **53 accepted / 45 comparable / 38 fitted**, matching the preserved no-FP result. The field forecast matches the previous no-FP values at every compared checkpoint; its 139 calculation count is unchanged. No operational classifier/clock/Half-live formula was edited.
- A separate **7,000-envelope native prefix** starts with no FP artifacts. Analyse starts at 520 and completes through the real QProcess worker at 1816. All 13 observations then present retain their identities/counts, a repeated raw snapshot adds no duplicates, and the field forecast is unchanged across application of FP preparation. Operational pace and COOL inputs become available; subsequent Q2 messages and UI continue normally.

The complete replay took 212.74 s; the largest measured batch of up to 37 messages took 1.44 s. The late-recovery prefix took 89.56 s. These are accelerated end-to-end GUI batch measurements, not latency guarantees. The subprocess removes acquisition/login blocking from the polling thread; it does not eliminate all existing replay-burst costs.

### Authentication failures and fallback

The GUI acquisition child closes stdin and selects FastF1's password method **inside that child only**. Stored browser tokens remain usable regardless of sign-in method. Missing/expired/rejected tokens cannot open an implicit browser or ask for a password on the polling thread. Explicit browser sign-in is a separate official CLI action.

Diagnostics report local expiry separately from server access; a locally unexpired token is not called verified authentication. Safe response metadata distinguishes primary 401/403, primary 404, mirror status, HTTP cache hits and parsed-function cache use. Only authentication-header presence is recorded, never its value; query strings, bodies and account claims are omitted. Duplicate requests-cache hook dispatch is counted once without retaining response payloads.

Partial FP success and optional-telemetry semantics remain intact. A failed acquisition leaves Q-only observations and field forecasts usable. Duplicate jobs are suppressed; a changed event prevents stale preparation application; startup failures, malformed worker results and timeout errors identify the worker phase. A 15-minute limit and window-close handling bound the child lifetime. FastF1's rejected-token renewal path is tested with temporary synthetic tokens, including both successful renewal and failure; the user's actual token is never cleared by those tests.

The authenticated primary works, so a full OpenF1 adapter is unnecessary here. An independent partial-reference fallback could still help future source failures, but pit timing, freshness and run classification need independently validated mappings before use.

## B. Early-session forecasting verdict

**The observed errors are mixed. R15's extreme error is mainly model-limited; early Q2 interval undercoverage is calibration-limited; incomplete early Q1 can be information-limited under the current feature set. No exact irreducible-error decomposition has been established.**

### Protocol and independence

The original production forecast, R1–10 prior and benchmark artifacts remain byte-identical. R11–15 and R18 are development evidence, not fresh holdouts. New research is confined to [audit_early_quali_forecast.py](/Users/zhxutong/dr-wo/woPlanner/scripts/audit_early_quali_forecast.py) and [its separate outputs](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_early_forecast_audit_20261025).

Selection uses nested chronological validation within R1–10: R4–10 are seven outer events, and each prior/rule/calibration uses only preceding events. No leave-one-out fold trains on later races. Candidate choices, code/input hashes and outcome-audit declarations were frozen before scoring R16/R17.

R16/R17 raw data and legacy behavior were inspected earlier; only these field-model outcome comparisons were newly unblinded. They are not universally untouched datasets. R16 supplies 18 captured horizons; R17 starts during Q2 at 512 s and supplies six, with no new 900/780/600 s evidence. Cached R1–15 evaluation remains completion-time reconstruction; native R16/R17 processing preserves source/receipt times and does not backfill uncaptured history. Final results are calibration/scoring targets, never causal runtime features.

### Controlled models with the same causal information

| Method | Input/use |
| -- | -- |
| Frozen | Existing current-best-first field model, Q1 anchor when Q2 current best is absent. |
| Historical field-shape prior | Normalized historical cutoff shape scaled by available causal circuit pace; ignores individual Q2 anchor detail. An absolute-seconds cross-circuit prior without any scale is not defensible. |
| Current-only | Removes Q1 anchors and reports missing-driver unavailability; availability loss is explicit. |
| Q1→Q2 transfer | Uses historical Q1 transfer even when a current Q2 time exists, retaining consistency with any faster observed current best. |
| Reliability candidate | Preserves the normal current branch but switches suspiciously slow current Q2 anchors toward the transfer scenario, using the same historical joint vectors. |

Training-only inner selection compared thresholds 0.5/1/2/3/5% and current-vector weights 0/0.25/0.5/0.75/1. Event-weighted normalized error selected a **1% slower-than-Q1 threshold, current weight zero** for suspicious anchors. This is a small-sample trained rule, not a universal physical boundary or a change to official lap validity/operational states.

Primary prequential comparison, 125 checkpoints across seven outer events:

| Method | Point availability | MAE / RMSE / bias (s) | Raw 80% range containment | Mean width |
| -- | --: | --: | --: | --: |
| Frozen | 92/125 | 0.370 / 1.552 / +0.230 | 48.9% | 0.449 s |
| Historical field shape | 117/125 | 0.258 / 0.916 / +0.119 | 59.0% | 0.499 s |
| Current-only | 50/125 | 0.180 / 0.250 / −0.027 | 48.0% | 0.304 s |
| Q1 transfer | 92/125 | 0.171 / 0.219 / −0.040 | 41.3% | 0.243 s |
| Reliability | 92/125 | 0.193 / 0.342 / +0.052 | 47.8% | 0.303 s |

Conditional errors are not a reason to conceal unavailable predictions. Reliability preserves all 92 frozen points. Its event-mean MAE difference is −0.155 s, event-cluster bootstrap 95% range [−0.373, 0.000] s: improvement is concentrated in two events, five unchanged. This is mechanism evidence, not decisive general superiority.

### R15: avoidable anchor failure

At Q2/600 s, the model has ten completed/current Q2 anchors, six untimed drivers and all 16 causal Q1 anchors. Every current Q2 anchor is more than 3% slower than its Q1 reference. Eventual cutoff: **104.683 s**.

| Method | Prediction / absolute error | Interpretation |
| -- | -- | -- |
| Frozen | 114.812 / 10.129 s | Trusts unrepresentative slow current anchors. |
| Historical field shape | 104.576 / 0.107 s | Causal aggregate alternative; not generally best elsewhere. |
| Current-only | Unavailable | Removes difficult states rather than solving them. |
| Q1 transfer / reliability | 104.317 / 0.366 s | Same causal evidence and frozen templates avoid most of the error. |
| Future representative-lap oracle | 104.512 / 0.171 s | **Noncausal diagnostic**, uses laps arriving 44–409 s later. |

This is predominantly a **model-limited** error despite scarce representative Q2 laps. Useful Q1 information already exists. The oracle is neither a valid prediction nor a lower bound on irreducible error; at 900 s it can worsen error by double-counting progress when later pace is inserted into an early-horizon residual model.

The same failure category appears in four training slow-anchor checkpoints: frozen/reliability MAE 4.732/0.658 s, and four development checkpoints: 2.970/0.238 s. Normal complete/incomplete anchor groups are unchanged by the targeted rule. On the already-inspected R11–15 development set, both produce 65/90 points; MAE changes 0.318→0.146 s. This is explicitly development evidence.

### Calibration and information limits

At Q2 900/780 s, there are no current Q2 pace observations but 16 Q1 anchors. Across the five development events, point MAE is about 0.206 s, yet only **two of five events** fall in the raw nominal 80% range. The two horizons repeat the same prediction per event; they are not ten independent observations. The primary rolling experiment similarly covers three of seven events at those leads. This establishes under-calibration, not that all early point error is unavoidable.

Sparse historical joint scenarios and anchor/circuit variation understate between-event uncertainty. Correcting point anchors does not automatically correct intervals: R15's improved point has range [104.196, 104.515], missing the 104.683 target, whereas its poorly centered frozen range happens to contain it by being very wide.

Research uncertainty adjustments use **past-event block scores**: the largest outside-interval residual within a part/lead regime per preceding event, pace-normalized, followed by a finite-sample empirical 80% quantile. A quality-conditional version also groups slow/incomplete/complete anchors and falls back to marginal scores with fewer than three calibration events. No arbitrary width multiplier is used. These evolving prequential scores do not establish a formal exchangeable conformal guarantee.

In primary rolling evaluation, quality-conditional correction changes frozen coverage 48.9→77.2%, width 0.449→1.015 s; reliability coverage 47.8→77.2%, width 0.303→0.650 s. Point estimates and availability do not change. On development events, marginal early scores are contaminated by slow-anchor outliers: early width reaches 9.597 s; quality conditioning reduces it to 0.731 s. Both give 100% observed coverage on those five already-inspected events, which is not proof of calibrated 80% future coverage.

Some early Q1 states remain **information-limited conditional on these inputs**. Unknown driver pace cannot be recovered by pretending it is the field median. Existing partial-field envelopes retain useful constraints without a point/probability claim. They do not prove that extra genuine FP, circuit or driver evidence could never help.

### Additional field-outcome evidence

Locked methods on 24 captured R16/R17 horizons:

| Method | Point availability | MAE / RMSE / bias (s) | Raw containment / width |
| -- | --: | --: | --: |
| Frozen | 21/24 | 0.144 / 0.177 / −0.034 | 81.0% / 0.509 s |
| Historical field shape | 22/24 | 0.183 / 0.292 / +0.011 | 59.1% / 0.394 s |
| Current-only | 12/24 | 0.132 / 0.165 / +0.060 | 66.7% / 0.478 s |
| Q1 transfer | 21/24 | 0.133 / 0.176 / −0.108 | 57.1% / 0.335 s |
| Reliability | 21/24 | 0.136 / 0.169 / −0.026 | 81.0% / 0.486 s |

R16 paired MAE changes 0.13661→0.12506 s; R17 is unchanged at 0.16165 s. Event-mean difference is −0.00577 s, cluster 95% range [−0.01155, 0.00000] s. Neither event contains the >3% slow-anchor condition; only R16 adds early-Q2 evidence. They offer limited corroboration, not independent replication of the R15 extreme.

Marginal correction gives 100% containment but widths 2.606/1.355 s for frozen/reliability; quality-conditional correction gives 100% with widths 1.138/1.115 s. These are conservative on two events, insufficient to select a definitive operational calibration.

Availability remains separate: development frozen/reliability have 65 points plus 14 finite conditional ranges, 79/90 informative states. Current-only has 34 points plus 33 ranges, 67/90. Additional outcomes have 21 frozen/reliability points and no finite envelope for the three earliest states; current-only gives 12 points plus six ranges, 18/24. Conditional envelope containment is not nominal probability coverage.

**Decision:** retain the frozen production model and benchmark. The research reliability candidate demonstrates feasible same-information improvement, but prospective slow-anchor and early-Q2 events are needed before a live replacement or calibration claim. Research implementation is saved for comparison; it was not promoted into service/UI behavior.

Full per-lead/quality/source metrics, confidence intervals, frozen-choice hashes and oracle provenance are in [Agent B's audit](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_early_forecast_audit_20261025/Agent-B-early-forecast-audit.md). Reproduction:

```bash
/Users/zhxutong/dr-wo/.venv/bin/python scripts/audit_early_quali_forecast.py --unblind-new-outcomes
/Users/zhxutong/dr-wo/.venv/bin/python outputs/r18_early_forecast_audit_20261025/finish_analysis.py
```

These reproduce now-inspected outcomes; rerunning does not make them untouched again.

## C. Changes and validation

### Production changes

| Location | Change |
| -- | -- |
| [woStrategy pyproject.toml](/Users/zhxutong/dr-wo/woStrategy/pyproject.toml), [woData pyproject.toml](/Users/zhxutong/dr-wo/woData/pyproject.toml) | Minimum FastF1 3.9; compatible Strategy Python minimum. Only installed FastF1 changed. |
| [analysis.py: `_load_fp_laps` / `_load_practice_sessions`](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:165) | Cache.configure keyword API; scoped access/cache diagnostics around existing session loading; original partial/error/optional-telemetry behavior preserved. |
| [fp_access.py](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/fp_access.py) | Passive expiry status, safe response/cache provenance, 401/403 versus 404 attribution and official recovery command. Private compatibility seams bounded/tested against 3.9. |
| [fp_worker.py](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/fp_worker.py) | Noninteractive child preparation; clean JSON protocol despite producer stdout; existing artifact schema. |
| [gui.py](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:295) | QProcess acquisition, responsive polling, duplicate/stale-event protection, main-thread application, timeout/close cleanup and worker-phase errors. |
| [preflight.py](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/preflight.py) | Repeatable version/auth status and isolated cold/warm preparation/reload/readiness checks; fresh-mode cache guard and session/fresh-response metadata. |
| [monitor documentation](/Users/zhxutong/dr-wo/woPlanner/doc/QUALIFYING_MONITOR.md) | Auth/preflight procedure and current recovered status, preserving historical report snapshots. |
| R18 normal-root data | Four genuine preparation files added where no set existed; hashes/provenance saved. |

No production forecasting model/prior, Q-only policy, generic parser, lap state machine, clock, Effective Finish, Half-live formula, local fork or unrelated strategy algorithm was changed. Existing working-tree modifications from previous rounds remain present.

### Research and tests

Research changes are the new audit script/output directory only. Production timing/forecast artifacts are not recalibrated from development errors.

| Validation | Result |
| -- | -- |
| Broad Planner qualifying + SessionInfo selection | **1,222 passed, three deselected**, 91.15 s. |
| Final focused auth/analysis/UI/runtime/readiness/resilience selection | **110 passed**, 7.75 s after final worker/provenance changes. Overlaps the broad selection. |
| Final new auth/worker module, independent review | **16 passed**, 2.71 s. |
| Actual-runtime Strategy compatibility/session/telemetry/pre-quali/forecast | **63 passed**. |
| Actual-runtime woData archive/controller/events/interruption | **59 passed**. |
| Actual-runtime woWeekend prerequisites/qualifying/VSC | **43 passed**. |
| Supplemental woStanding public points consumer | **20 passed**; no source changes. |
| Genuine native R18 GUI/service replay with FP | **12,026/12,026**, all checkpoint assertions pass. |
| Actual background-worker late-FP recovery | **7,000 original envelopes**, identity/count/duplicate/forecast checks pass. |
| Cold/warm upstream, telemetry, full preparation and artifact reload | Actual results in section A; separate from mocks. |
| pip check; Git whitespace; preserved source/benchmark/recording hashes | Passed. |

The broad selection excludes five expensive full historical replay files and three R16 full-recording nodes, as in the preserved focused strategy. They were not run and are not claimed passing. Exact commands are saved with the new evidence. Counts across overlapping selections are not additive. Small subsequent diagnostic/worker cases are covered by the final focused run.

Independent review checked API consumption, results changes, request-cache credential redaction, passive auth state, hooks/protocol cleanup and event guards. It caught a dataclass serialization error during development, corrected and covered by a positive preflight test. Real requests-cache double hook dispatch is now deduplicated; actual fresh metadata verifies one response counted once. Test harness assumptions about Plan preferring the legacy supported fit and the bounded replay stop were corrected; final native assertions pass. No outstanding production regression was identified.

### Preservation and remaining risks

Original R18 recording SHA256 remains `533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17`. Frozen forecast/prior/benchmark hashes and unrelated pre-existing modifications are unchanged; all original branches/HEADs remain unchanged. No auth file, token, callback or raw cache database is copied into the repository evidence. Requests-cache defaults redact Authorization values; actual marker checks and independent source review support that behavior. Synthetic auth tests use temporary stores only.

The audit detected a concurrent five-space insertion inside a word at line 11 of the previous resilience report; removing those spaces in memory matches its exact starting hash. Attribution is unknown, so that file was left as found. This is separate from the verified unchanged model, benchmark and recording data. The preservation manifest records both report hashes.

One exploratory schedule lookup occurred before explicit cache setup and may have touched metadata in FastF1's default user cache; its prior mtime was not captured. No payload replacement there was established, and all subsequent acquisition used explicit temporary caches. We therefore do not claim every historical cache metadata byte is unchanged. Temporary sandbox DNS failures did not establish upstream missing data; the network-enabled actual requests succeeded.

Remaining operational risks are ordinary token expiration/access changes, future private API changes, limited COOL/FP-prior support and replay-burst GUI cost. The early forecast still has weak interval calibration and missing-driver limits. These are visible limitations, not outstanding failures of the restored FP pipeline.

## D. Pre-weekend preflight

Use the same intended interpreter. Change event flags, not source code.

1. **Check version and passive token state.**

   ```bash
   /Users/zhxutong/dr-wo/.venv/bin/python -m woplanner.quali.preflight --year 2026 --round 18
   ```

   Confirm FastF1 3.9.0 and locally stored/unexpired status. This does not prove current server acceptance.

2. **If missing/expiring/expired, authenticate explicitly outside the monitor.**

   ```bash
   /Users/zhxutong/dr-wo/.venv/bin/python -m fastf1 auth f1tv --authenticate --method browser
   ```

   Open the CLI's official sign-in link, use the Companion flow, and wait for FastF1's successful exit. Repeat step 1 in a new process. Keep all credentials/cookies/tokens local. Token lifetime is typically around four days and renewal requires another supported sign-in; local expiry alone cannot detect remote revocation. [Version-pinned auth documentation](https://raw.githubusercontent.com/theOehrly/Fast-F1/v3.9.0/docs/api_reference/accounts_auth.rst).

3. **Prove fresh acquisition plus complete preparation in a separate run root.**

   ```bash
   F1_PREFLIGHT_ROOT=$(mktemp -d /private/tmp/quali-preflight.XXXXXX)
   /Users/zhxutong/dr-wo/.venv/bin/python -m woplanner.quali.preflight \
     --year 2026 --round 18 --prepare --fresh --run-root "$F1_PREFLIGHT_ROOT"
   ```

   Require all three successful sessions, `fresh_authenticated_timing_sessions` containing FP1/FP2/FP3, equal analysis/reloaded lap counts and usable pace/COOL readiness. A 200 metadata response alone is insufficient. Fresh mode refuses a populated raw cache. Generated artifacts are below `<run-root>/preparation`; they do not silently overwrite the monitor's normal data.

4. **Check warm operation and normal application readiness.** Repeat step 3's preparation command without `--fresh`, using the same run root. Cache provenance must be explicit; no new response is not new acquisition proof. In the normal monitor use Analyse FP1–FP3 with the correct received event identity, or an explicitly configured prepared runtime root. Confirm its loaded pace/COOL source and selected support. This task already installed/verified the genuine R18 set in the normal workspace location.

5. **Check fallback/startup without changing real authentication.** Run the controlled auth/worker and genuine R18 resilience suites:

   ```bash
   /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q \
     tests/test_quali_fp_auth_and_worker.py tests/test_quali_resilient_forecast.py \
     tests/test_quali_r18_integration.py
   ```

   Use the normal writable plotting/offscreen test settings when headless. These tests cover missing/expiry/access diagnostics and Q-only fallback without clearing the user's token. Actual upstream failure should leave Q-only observation/forecast rows available; do not lower fit thresholds or insert pace/COOL defaults. Re-authenticate through step 2 when an actual token expires or is rejected.

These commands are repeatable. Network failure, 401/403, missing resource, cached preparation and unsupported numerical evidence remain distinct outcomes. The current implementation performs no login on the real-time polling thread.

## E. Readiness verdict

**READY.** Real authenticated cold acquisition, all R18 FP sessions and telemetry, full application preparation/reload, normal-root operational prerequisites, complete qualifying replay/UI and late-FP/Q-only fallback have been verified in the actual upgraded interpreter. No user-authentication action remains pending.

This verdict applies to the operational workflow. It does not promote the experimental forecast or claim that early-session uncertainty is calibrated. Keep the frozen model for comparison; collect prospective slow-anchor and early-Q2 event evidence before promoting the researched reliability/uncertainty alternatives.

The exact original “cannot find” exception remains historically unresolved. All saved evidence and reusable code remain in this conversation's workspace; no transfer to another agent is required.

Operational evidence: [outputs/r18_fp_recovery_20261025](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_fp_recovery_20261025), including cold/warm acquisition, final native replay summaries, generated artifact hashes, independent review, test commands/logs and preservation checks. Forecast research remains in its separate directory linked in section B. Cache databases and authentication stores are not included.
