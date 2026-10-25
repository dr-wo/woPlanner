# 2026-R18-Q — Debug Diagnosis

Investigation completed on 25 October 2026. Lead coordinator reviewed three parallel investigations: A (D01/D05), B (D02), and C (D03/D04), and independently checked event identity, runtime roots, update publication, repository preservation and integration tests. This was a diagnostic investigation; no production implementation was changed.

## Executive summary

The strongest common explanation is missing FP preparation, which leaves two operational prerequisites unavailable: the common lap-pace reference and the cool/preparation-lap duration. A genuine R18 recording reproduces the reported downstream symptoms under the accessible workspace configuration. It does **not** record the original FP exception or live operator settings, so this is not a confirmed historical root cause for all five incidents.

| ID | Diagnosis status | Root cause / hypothesis | Evidence | Recommended action |
| -- | ---------------- | ----------------------- | -------- | ------------------ |
| D01 | Strong hypothesis; mechanism reproduced on R18 | Without an FP pace reference, ALB's completion remains `UNKNOWN`; without a cool duration, the on-track finish threshold cannot be evaluated. Later pit entry triggers a different finish rule. | Lap accepted at sequence 520; no effective finish. Both prerequisite controls enable it immediately. Default replay sets it at sequence 1446, 136.002 seconds later, on pit entry. | Restore valid FP prerequisites and expose unavailable finish prerequisites. Preserve existing timing semantics; add the R18 prefix regression. |
| D02 | Unresolved historical exception | Practice acquisition/loading failure is the leading hypothesis. Two separate defensive gaps are reproduced, but neither is established as the R18 cause. | No R18 FP payload/artifact set found; separate FP1 archive attempt captured zero topics. Cached actual schedule resolves FP1/2/3. Fresh cache-root and optional telemetry failure controls reproduce. | Preserve phase/event/root-aware exception diagnostics; initialize writable cache directories; tolerate specifically expected optional telemetry absence. Do not invent an acquisition fix without its error. |
| D03 | Confirmed fallback interpretation; historical attribution remains a strong hypothesis | `NO_SUPPORTED_LIVE_FIT` is a status reason, not an exception. Missing operational `FAST` classification can leave no supported observations. | Exact string located in policy. Default R18 replay has no live fit; controlled reference restores fits. | Explain the upstream missing prerequisite and fitted support. Do not weaken support thresholds or change the renderer merely to remove this status. |
| D04 | Confirmed ingestion; exclusion reproduced; exact noted pair unresolved | RUS/ANT laps are present with fresh SOFT metadata. Missing references reject them as `NOT_OPERATIONAL_FAST`. Separately, one observation per driver cannot yield a slope. | Four Q2 completions identified; tyre data present at each. All four admit with a synthetic reference. Early pair alone lacks per-driver fit support. | Fix/shared-diagnose reference availability, then retain admission versus observation versus fit counters. Test both Q2 pairs and the one-point case. |
| D05 | Unable to reproduce a calculation defect | The actual feature is experimental `half_live`. With no live fit and the default prior of zero, it correctly equals the observed cutoff; before an observed target exists it is unavailable. | Full native R18 replay, all 12,026 envelopes, no exception. Q2 has 2,065 unavailable snapshots then 3,615 fallback snapshots; formula verified at every recorded cutoff transition. | Keep the formula; explain the fallback/target prerequisites. Add an R18 replay integration assertion rather than a speculative numerical change. |

“Reproduced” below means current source plus genuine recorded input, with the stated runtime configuration. Synthetic references and durations are explicitly labelled controls, never reconstructed FP measurements.

## Scope, source identity and evidence

### Ownership and preservation

Actual entry points establish these boundaries:

- **woPlanner:** qualifying UI, FP-analysis orchestration, operational lap states/effective finish, canonical admission policy, diagnostic presentation and experimental cutoff.
- **woStrategy:** FastF1 session wrapper, FP analysis and shared per-driver regression/aggregation mathematics.
- **woData:** event identity, raw recording, replay, message reduction, clocks, tyre/installation metadata and artifact paths.
- **woWeekend:** optional workflow orchestration that calls the Planner service. The GUI does not require a weekend workflow to have run first; no evidence makes this an orchestration failure.
- **FastF1:** practice discovery/loading. The available interpreter imports installed **FastF1 3.8.3**, not the sibling fork. Relevant discovery/cache behavior was checked against the installed source and fork.

Project documentation and relevant tests were read, including the [woWeekend development guide](/Users/zhxutong/dr-wo/woWeekend/docs/AI_DEVELOPMENT_GUIDE.md). No filesystem `AGENTS.md` was found in the inspected repository trees; the user-supplied white-background instruction was retained. No figure was needed.

All repositories remained on `main`. Investigated revisions:

| Repository | HEAD |
| -- | -- |
| woPlanner | `e1142169930c737f1f881f7ec4708071c56ce028` |
| woWeekend | `b634005e7433c6c7540fec2ddd42957df28bfd5c` |
| woStrategy | `57a0a6fbf09faf6069d5564ef5479bc19bccda84` |
| woData | `7881cbb4762c724f5befa6fc70341434d7924516` |
| Local Fast-F1 fork | `47f2725d1851b32b5ad7895b685d2a666a565a43` |

Existing modified files were preserved: woWeekend `configs/post_race.json`, `configs/race.json`, `src/woweekend/workflows/race.py`; woStrategy `research/traffic_position_part1/README.md`, `src/wostrategy/script/tyre_strategy_summary.py`. Existing untracked research, output and migration/worktree directories were preserved. Baseline status/diffs were captured before experiments. No branch changes, commits, pushes, downloads or credential-dependent requests were made.

Planner HEAD was committed after the capture. Coordinator and C reviewed its parent diff: it caches immutable diagnostic modal-summary counts; it does not change admission, lap classification or fitting. Nevertheless, the recording contains no source-revision manifest, so an exact as-run deployment claim is unavailable.

### Genuine R18 data

The recording is [live_timing_e664885c31e14b29a0a2c3633090c1fd.jsonl](/Users/zhxutong/dr-wo/woData/wodata/live_timing/schema_v1/year=2026/round=18/session=Q/live_timing_e664885c31e14b29a0a2c3633090c1fd.jsonl):

- 8,620,056 bytes; 12,026 envelopes; SHA-256 `533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17`.
- Receipt coverage: **2026-10-24 21:15:47.055398Z–22:24:40.379584Z**. All event times in this report use UTC.
- It starts during Q1, rather than at session start. Initial keyframes retain current results but cannot supply all earlier completion provenance.
- Recorded topics include 5,179 TimingData, 377 TimingAppData, 2,883 CarData.z and 2,884 Position.z envelopes. The native replay reader preserves recording order and source/receipt semantics.
- Native replay identifies Q2 at sequence 2285 and Q3 at sequence 7965. Earlier RUS/ANT Q1 completions are absent from this capture.
- It does not contain FP button errors, UI actions, manual timing/reference settings or screenshots.

No R18 saved pre-quali analysis or derived FP lap payload was found in the inspected shared roots. The actual R18 FP1/2/3 raw-cache directories exist but are empty. A separate [FP1 archive manifest](/Users/zhxutong/dr-wo/woData/wodata/live_archive/schema_v1/year=2026/round=18/session=FP1/manifest.json), created at 21:32:29.736669Z, records all 13 requested topics missing and all counts zero. That archive is **not** directly consumed by the FP-analysis button; it corroborates unavailable local evidence, not a specific acquisition defect.

### Validation performed

| Workstream | Validation | Result |
| -- | -- | -- |
| A | Existing service, experimental-cutoff and experimental-GUI suites | 193 passed |
| A | Native controller + ReplayPlayer + production service over the genuine R18 capture | 12,026/12,026 envelopes completed; no replay error |
| A | Exact first-520-envelope reference × cool-duration controls; saved-result assertions | All four outcomes and cutoff formula assertions passed |
| B | Existing Planner runtime-path/D2 suites | 75 passed |
| B | Existing woStrategy pre-quali suite | 5 passed |
| B | Four isolated diagnostic tests | 4 passed |
| B | Actual cached R18 schedule through FastF1 discovery, without loading session data | FP1, FP2 and FP3 resolve successfully |
| C | Focused policy, diagnostic, shadow and payload-cache suites | 75 passed |
| C | Reference-availability and late-tyre controls; actual R18 reducer and service replays | See D03/D04; controls distinguish admission from fitted support |
| Coordinator | Session identity, runtime paths, queued live evidence and publication-contract suites | 100 passed in 6.36 seconds |
| Coordinator | Actual initial SessionInfo through GUI/service identity paths | R1 selection becomes R18; no external event lookup; R18 prerequisites absent |

Suite selections overlap; counts must not be added as a unique-test total. Existing tests mostly verify controlled contracts. They are not evidence that the original live UI state was reproduced exactly.

## Detailed findings

### D01 — ALB effective finish

**Observation:** final Q1 lap reportedly completed with about 48 seconds left; effective finish appeared only later.

**Definition and execution path.** The feature predicts inability to reach the **start** of another push lap. It is distinct from the real session chequered flag and does not require the next push to finish before zero. At [service.py:622](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:622), `_consume_driver` joins a newly accepted completion to the raw driver state and classifies operational lap pace. At [service.py:731](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:731), it evaluates two opportunities:

1. In pit, insufficient remaining time for the configured OUT duration: `remaining < out_lap_s`.
2. Newly completed eligible operational FAST lap, insufficient remaining time for COOL/PREP: `remaining < cool_lap_s`, only if that duration exists.

The comparison is strict. Effective finish latches within a part; qualifying transitions reset advancing-driver state. A recognized completion after the real flag sets `FINAL_LAP_COMPLETED`, which supersedes an earlier predictive finished reason. UI explanation/highlight is in [gui.py:532](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:532).

The service's defaults are OUT=100 seconds and COOL unavailable. FP analysis supplies sector/checkpoint pace references and duration evidence; if a measured cool duration is missing but a usable FP lap reference exists, `_apply_operational_sources` permits the existing FP-best × 1.30 fallback. It does not manufacture this duration from a current qualifying result. See [service.py:331](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:331), `load_operational_analysis`, and [service.py:381](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:381), `_apply_operational_sources`.

**R18 reproduction.** At sequence **520**, receipt **21:17:06.963911Z**, source **21:17:04.848Z**, ALB's final 96.967-second lap is accepted with event key `('Q1','23',9,1792876624.848)`. The normalized completed count differs from the feed boundary lap number; it is not evidence of a lost lap. Sectors are 25.870/38.490/32.607, timed eligibility is true and ALB is on track. Under current available prerequisites:

- Domain remaining time is **58 seconds**, not the handwritten approximate 48.
- Lap state is `UNKNOWN`; the common FP reference is absent.
- `cool_lap_s=None`; on-track effective finish is false.

The four exact-stream controls isolate the gates:

| Pace reference | Cool duration | State / effective finish at sequence 520 |
| -- | -- | -- |
| Absent | Absent | UNKNOWN / false |
| Absent | Synthetic 120 s | UNKNOWN / false |
| Synthetic sector reference | Absent | FAST / false |
| Synthetic sector reference | Synthetic 120 s | FAST / true, `NO_TIME_FOR_NEXT_PUSH` |

The synthetic reference uses ALB's observed sector values solely to isolate behavior; neither it nor 120 seconds is proposed as a production calibration.

In the default full replay, effective finish first becomes true at sequence **1446**, receipt **21:19:22.965611Z**, **136.0017 seconds later**. The feed now says `InPit=True`; remaining time is zero and the separate pit rule sets `IN_PIT_NO_OUTLAP_TIME` using OUT=100. This closely reproduces “resolved itself” without assuming a delayed completion, polling race or event loss.

**Clock checks.** The reducer advances an event-driven countdown from published clock/source evidence; [quali_reducer.py:323](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_reducer.py:323) handles live clock latency and anchors. `PresentationCountdown` at [gui.py:43](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:43) advances the UI display locally and cannot change domain finish state. No original screen timestamp establishes the reason for the 48/58-second discrepancy. Both values are well below the test's 120-second recovery threshold, so that discrepancy does not explain the demonstrated suppression.

**Diagnosis/confidence.** High confidence in accepted ingestion, the two prerequisite gates and delayed pit-path recovery. Strong historical hypothesis that the unavailable FP setup caused the observed behavior. Actual live manual configuration and source revision are unrecorded, preventing unconditional historical attribution.

**Minimal correction/test.** Repair availability/loading of genuine FP prerequisites and expose which finish prerequisite is missing. Preserve existing unavailable/manual semantics; do not introduce an arbitrary cool duration or use the UI clock as a state trigger. Retain the four-way prefix test plus a checkpoint at sequence 1446; assert one completion, no invented finish with either input absent, immediate finish when both are supplied, and independent normal chequered behavior.

### D02 — Analyse FP1–FP3 failed

**Observation:** approximate “cannot find”; missing object unknown.

**Complete path.** [gui.py:276](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:276), `_analyze`, synchronously calls [analysis.py:22](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:22), `QualiAnalysisService.analyze`. `_load_fp_laps` at [analysis.py:58](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:58) enables FastF1's raw cache, constructs FP1/2/3 `Session` objects and aggregates failures only if none loaded. [woStrategy session.py:36](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/core/session.py:36), `_load_session`, invokes `fastf1.get_session` and full `data.load()`. It does not fall back to derived woData lap files or the live-archive manifest.

Loaded sessions are concatenated and enriched with a fastest valid FP reference; [analysis.py:89](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:89), `_add_fastest_driver_lap_references`, selects the lap, real sectors and optional first-5%-distance telemetry. [pre_quali.py:39](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/pre_quali.py:39), `analyze_pre_quali`, computes roles, durations and progression. [artifacts.py:16](/Users/zhxutong/dr-wo/woData/src/wodata/artifacts.py:16) writes four event-scoped pre-quali files. Success then supplies operational references/timings and the fallback rate to the live service.

The GUI only displays `str(exc)` in a modal dialog. No traceback, exception class, failure phase or selected root is persisted there. Failures before the per-session loop or after it during enrichment bypass its aggregated session labels. A missing individual FP session is intentionally tolerated. The button performs analysis; it does not first reuse saved FP analysis as the Plan dialog does.

**What was established.** The cached actual 2026 R18 schedule identifies a conventional United States GP at Austin, with FP1/2 on 23 October, FP3/Q on 24 October and F1 API support. Actual offline FastF1 discovery resolves all three FP sessions. Thus a sprint-weekend/unsupported-FP2-or-FP3 explanation is unsupported here.

The existing HTTP cache was inspected read-only: the matching available response is the season schedule; no matching R18 session response was identified. Together with the empty parser-cache directories, absent FP artifacts and zero-topic archive manifest, this supports an acquisition/load problem. Missing data alone is not a software defect and does not identify the original exception.

The installed FastF1 `InvalidSessionError` definition contains “No matching session can be found,” but no reachable raising callsite was found in that package. A similarly worded class definition cannot be assigned as the incident's exception. No exact reachable “cannot find” origin was established.

**Controlled defensive gaps.** Four diagnostic tests passed:

1. Fresh isolated output: `get_fastf1_raw_cache_dir` returns a nonexistent directory; installed `fastf1.req.Cache.enable_cache` requires it to exist and raises **`NotADirectoryError: Cache directory does not exist! Please check for typos or create it first.`** before any Session call. This is a confirmed fresh-root gap. The current normal shared cache directory already exists, so this is not established as the R18 cause.
2. Three injected Session `FileNotFoundError`s produce correctly labelled `Unable to load FP1/FP2/FP3: FP1: ...; FP2: ...; FP3: ...`.
3. FP1 available with FP2/3 unavailable still returns usable FP1 laps.
4. Optional fastest-reference telemetry raising FastF1 **`DataNotLoadedError`** aborts otherwise usable analysis. The handler at [analysis.py:126](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/analysis.py:126) catches KeyError/IndexError/TypeError/ValueError, not that expected telemetry exception. This is a confirmed controlled defensive gap, not an observed R18 telemetry error.

**Diagnosis/confidence.** Historical missing object and exact root cause remain unresolved. Leading hypothesis: upstream practice acquisition/loading failed. Optional telemetry failure is a plausible alternative; fresh isolated-root initialization is a proven defect with low incident relevance under the observed normal roots.

**Minimal correction/test.** Persist event/session/phase/root-aware exception diagnostics, preserving partial-success behavior. Create the validated writable raw-cache directory before enabling FastF1 cache. Specifically tolerate expected unavailable optional telemetry while retaining genuine lap/sector evidence and an unavailable 5% checkpoint; avoid a blanket exception swallow. Regress fresh output, all unavailable, partial available and missing optional telemetry independently. Do not claim any of these guarantees a repair of the unrecorded R18 exception.

### D03 — Track Evolution failure

**Observation:** approximate `no_supported_live_F1?`; transcription uncertain.

**Actual matching status.** [track_evolution_policy.py:39](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution_policy.py:39), `quality`, emits **`NO_SUPPORTED_LIVE_FIT`** when `diagnostic.lap_count` is zero and sets status `FALLBACK`. This is not a raised exception. The message is a strong match to the note, but the original text is not preserved. [widgets.py:836](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/widgets.py:836), `refresh_live_track`, displays quality/support and detailed results. Rendering has its own error path at [widgets.py:187](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/widgets.py:187); no recording evidence establishes a rendering failure.

**Execution and selection.** Live input passes through reducer completion ownership, service operational classification, [track_evolution_policy.py:155](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution_policy.py:155), `consider`, and [track_evolution.py:79](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution.py:79), `build_track_evolution_diagnostic`:

- Operational FAST, identified normal timed completion, finite lap and field reference, then field+7% gross sanity.
- Fresh tyres, known compound and installation identity.
- One first eligible push per driver/installation across the session.
- Per-driver dominant compound; at least two distinct observations/global push coordinates for a fitted driver slope.
- Shared [pre_quali.py:283](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/pre_quali.py:283) regression and support-weighted aggregation. Fewer than three fitted drivers creates limited-support warnings; it is **not** the threshold for any live estimate.

Canonical `refresh` calls the diagnostic with `require_traffic=False` at [track_evolution_policy.py:268](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/track_evolution_policy.py:268). Traffic coverage, gap threshold and delayed physical telemetry do not veto canonical admission or source choice. They should not be “fixed” to resolve this status.

**Reproduction.** With absent references, [lap_state.py:24](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/lap_state.py:24), `classify_lap_state`, cannot bootstrap a new on-track state from UNKNOWN to FAST. Two controlled fresh SOFT completions produce two `NOT_OPERATIONAL_FAST` decisions, zero samples and `FALLBACK/NO_SUPPORTED_LIVE_FIT`. Identical completions plus a controlled common reference produce two fitted points for one driver and `LIVE_LIMITED`.

The native default full R18 replay has no live estimate in any part. C's independent absent-reference replay processes all 12,026 envelopes, accepts **88 completion events**, and records **88 `NOT_OPERATIONAL_FAST` policy rejections**, with zero observations or fitted drivers. A separate full actual-stream control using permissive synthetic S1/S2 references produces live support; at the end it has 45 selected observations, 38 fitted observations and 14 fitted drivers, with status `LIVE_LIMITED`. The synthetic 40-second references are a mechanism-isolation control, not genuine FP data or a model-quality validation. C uses direct reducer/service consumption to control reference availability; A separately exercises the native controller/replay/artifact-discovery path.

**Diagnosis/confidence.** Confirmed status semantics and missing-reference failure mechanism. Strong hypothesis that D02/unavailable FP preparation caused the noted fallback. An actual thrown error, if there was one in addition to the status, cannot be identified from the approximate note.

**Minimal correction/test.** Make missing pace references explicit in the existing readiness/admission diagnostics and restore genuine preparation. Preserve signed rate units, exact completion identity, one-per-installation selection, support rules and traffic independence. Add a missing-reference integration regression asserting the precise reason, plus a supported-reference control. No threshold reduction or alternative fitter is justified.

### D04 — RUS/ANT “SN” laps not contributing

**Observation:** two Q2 laps associated with RUS/ANT, described as “SN.”

No production symbol establishes `SN` as an estimator input or named lap class. The implementation consumes compound, `fresh` and installation identity; raw `TimingAppData.New` supplies freshness. “SOFT/new” is consistent with the captured candidates but remains an interpretation of the shorthand, not an assumed definition. The exact two noted laps are unidentified.

**Actual Q2 completions.** Raw reducer replay identifies these candidates. Receipt times are UTC; boundary numbers are internal completion identities, distinct from the feed's official lap number.

| Driver | Sequence / receipt | Boundary / official lap | Lap time | Tyre at completion | Installation |
| -- | -- | -- | -- | -- | -- |
| RUS | 4568 / 21:31:51.986370 | 9 / 8 | 94.780 s | SOFT, fresh=true, StartLaps=0 | 1 |
| ANT | 4644 / 21:32:03.863059 | 9 / 8 | 94.756 s | SOFT, fresh=true, StartLaps=0 | 1 |
| RUS | 6555 / 21:39:43.740812 | 12 / 11 | 94.305 s | SOFT, fresh=true, StartLaps=0 | 2 |
| ANT | 6623 / 21:39:52.647650 | 12 / 11 | 94.278 s | SOFT, fresh=true, StartLaps=0 | 2 |

All four are present as `normal_timed`, `timed_lap_eligible=True`, physically on track, with fresh-tyre metadata already available. They are not missing from the capture, and delayed tyre freshness is not supported for these completions.

**Where contribution disappears.** In the no-reference replay, operational UNKNOWN blocks all four with `NOT_OPERATIONAL_FAST` before tyre/fit selection. With synthetic references, all four are admitted; the first pair has global push indices 28/29 and the later pair 31/32. Both drivers eventually have fit support. There is no specific RUS/ANT name exclusion.

Even with correct references, the early pair alone supplies only **one point per driver**. Because earlier RUS/ANT Q1 completion events were not captured, those two points do not yet produce a per-driver slope or appear as fitted series. This is an independent expected-support explanation if the notes refer to the first Q2 runs. The later pair supplies the second installation per driver. An ingested/admitted observation and a lap used in an aggregate fit are different stages.

**Other filters checked.** Same-installation later pushes and nonmodal compounds can be deliberately selected out. Once-seen admission decisions are immutable: a controlled `fresh=None` rejection does not recover merely by repeating the completion with `fresh=True`. That is a real mechanism, but the R18 candidates already had `fresh=True`; it is not a supported cause here. Explicit deletions/retractions can revoke exact observations; no such explanation was established for these four. Traffic is auxiliary and cannot explain their canonical exclusion.

**Diagnosis/confidence.** High confidence that the laps were ingested, had fresh SOFT metadata and can contribute with references/support. Strong missing-reference hypothesis if they remained excluded through the later Q2 runs. If observed only after the first pair, insufficient per-driver support is a valid alternative. Exact historical pair/time and meaning of SN remain unresolved.

**Minimal correction/test.** Share D03's prerequisite correction; do not force fresh laps into a fit or relax two-point support. Show admitted observations and fitted observations separately through existing diagnostics. Regress all four sequence checkpoints: absent-reference reasons, present-reference admission, first-pair no slope, second-installation contribution, and unchanged results when auxiliary traffic metadata changes. Preserve official versus internal lap identities in assertions.

### D05 — R&D cutoff

**Actual implementation.** The UI label is **`R&D · Half-live [Experimental]`**, at [gui.py:376](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/gui.py:376), `_render_cutoff_predictions`. It calls [experimental_cutoff.py:32](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/experimental_cutoff.py:32), `calculate_half_live`, over an already published snapshot on each GUI poll. It is a read-only presentation calculation with no separate analysis button, background job, persisted result or writeback.

It uses the observed target and active unfinished-driver count from [service.py:1194](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:1194), `_build_snapshot`:

```text
rate = 0.5 × automatic live slope, if a supported or limited live slope exists
       otherwise configured FP/default prior unchanged
R&D projected cutoff = min(observed cutoff, observed cutoff + rate × unfinished drivers)
```

Rates are signed lap-time change per global field push. Manual overrides are intentionally excluded. No observed target, invalid/missing estimates or invalid counts produce a named `UNAVAILABLE` reason. A positive rate cannot make the projected cutoff slower than its observed value.

**R18 replay result.** No numerical or invocation failure reproduced. Q2 snapshots are `UNAVAILABLE` for 2,065 messages because there is no observed cutoff target. At sequence **4350**, receipt **21:31:19.214990Z**, a target becomes available; the remaining 3,615 Q2 snapshots use `FALLBACK`. The configured prior is zero, so R&D equals the observed current cutoff and the production projected cutoff; observed targets still update (including 96.031 down to 95.058 seconds). No live-supported/limited result appears with absent references. Every saved cutoff transition obeys the formula.

**Diagnosis/confidence.** Unable to reproduce a calculation defect. High confidence in the feature definition and replay's expected unavailable/fallback behavior. The note does not establish whether the live symptom was absent text, an unavailable target, unchanged numbers, an exception or a different output. Shared lack of live track support explains why no distinct half-live correction appears; that is a hypothesis about the observation, not a confirmed formula bug.

**Minimal correction/test.** Preserve the formula and manual-override independence. Surface the existing reason clearly when target/support/prior inputs are unavailable, especially a zero default prior. Add a real-stream assertion for the Q2 target transition, zero-prior equality, nonzero live slope halving, positive-slope clamp, invalid inputs and source/status publication. Existing unit/GUI tests already cover much of the formula; the new value is cross-component prerequisite coverage.

## Cross-incident analysis

### A shared prerequisite mechanism is demonstrated

Successful FP preparation supplies a common sector/checkpoint reference, timing defaults and an FP fallback estimate. Without that preparation:

1. New operational lap states remain UNKNOWN, even when official qualifying lap times/sectors exist.
2. D01's completed-push gate fails, and its cool duration is independently unavailable. Pit entry can later satisfy a separate rule.
3. D03/D04's canonical admission sees `NOT_OPERATIONAL_FAST`; no admitted support means `NO_SUPPORTED_LIVE_FIT`.
4. D05 consumes that no-live-fit publication. With default prior zero, its numeric output is equal to the observed cutoff.

This relationship was checked independently in the coordinator's actual-SessionInfo readiness control, A's two-by-two R18 prefix test and C's reference-only admission controls. It is a causal mechanism under the tested configuration. It does not prove which original D02 error produced missing preparation, nor prove every live control was left at its default.

D04 also has a separate support distinction: even restored references do not make a single point per driver into a fitted slope. Its exact observation time therefore matters.

### Update, identity and workflow alternatives checked

- The sidecar records provisional **R1** and validated **R18** with `identity_mismatch=true`. Coordinator fed actual SessionInfo through `_sync_session_context` and the service: GUI selection becomes 2026/R18, artifact identity is R18 and no external event lookup is needed. This sidecar is not evidence that FP analysis kept searching R1.
- [controller.py:244](/Users/zhxutong/dr-wo/woData/src/wodata/live/controller.py:244), `drain`, caps a batch at 500 but calls the service callback after **each** applied message. The investigation did not assume only the last snapshot was consumed. Native replay used this path, and queued/publication contract tests passed.
- The GUI's FP analysis is synchronous, so a lengthy load can block GUI polling and paint updates. This is a possible latency mechanism, but there is no original callback-duration/backlog trace. It does not need to be invoked to reproduce ALB's suppression, so it is not diagnosed as the root cause and no asynchronous refactor is recommended here.
- Active-live physical traffic uses source-captured monotonic receipt deadlines; recorded replay progresses from recorded evidence. Physical telemetry context cannot veto the canonical estimator. No traffic/polling race was established.
- The optional woWeekend historical qualifying transition workflow uses accurate completed historical laps and a different summary adapter. It neither reconstructs every operational UI action nor tests the live FP-reference gate. Historical workflow success would not disprove the live failure mechanism.
- Replaying with fabricated permissive references tests causality, not R18 predictive accuracy. Missing early Q1 completion provenance and unrecorded manual settings remain genuine live/replay limits.

There is no evidence of five independent calculation bugs, a common network race, or a need to change all five numerical paths. The highest-confidence shared work is preparation readiness/error visibility and regression coverage across its consumers.

## Recommended fix plan

Ordered by dependency, functional impact and confidence. These are proposed changes, not implemented changes.

1. **Preserve the FP failure evidence and prerequisites.** In `QualiAnalysisService._load_fp_laps` / `analyze` and `QualiWindow._analyze`, report the event, phase, selected roots, session exception classes and available/skipped sessions, with a saved traceback for failures. At `QualiLiveService._load_operational_artifact` / `load_operational_analysis`, expose whether pace reference, cool duration and prior are available. Preserve valid partial FP success and the current conservative absence semantics. Test all-fail, partial success, absent artifacts and valid artifact loading. This addresses the main diagnosability gap and avoids treating downstream fallback as a separate numerical failure.
2. **Repair the two demonstrated FP boundary defects.** Create the validated writable raw-cache directory before `Cache.enable_cache`. Handle expected `DataNotLoadedError` only in optional telemetry enrichment, retaining real sector evidence. Keep these as small independently testable changes. Neither warrants promising that the original R18 acquisition problem is fixed.
3. **Add one shared R18 prerequisite regression fixture.** Extract minimal captured packets around SessionInfo, ALB sequences 520/1446, RUS/ANT Q2 completions and the Q2 target publication, preserving source/receipt/official-boundary identity. Compare absent prerequisites with explicitly supplied controlled inputs. Check no completion loss, both finish gates, admission reasons, two-point fit support and half-live fallback. Never promote synthetic references into a saved real FP artifact. Reuse the shared fixture instead of duplicating five tests with disconnected assumptions.
4. **Clarify existing output states.** Use existing Track Correction status/detail and R&D reason surfaces to distinguish missing pace reference, admitted-but-unfitted observations, no observed target and zero default fallback. Preserve `NO_SUPPORTED_LIVE_FIT` as an honest status and `LIVE_LIMITED` as a usable estimate where supported. No new fitter, admission threshold, invented cooling default or traffic-veto change is justified.
5. **Close historical attribution only with actual missing evidence.** If the original FP exception or genuine R18 FP payload becomes available, replay the same checkpoints with the real preparation output and fix the indicated acquisition/load path. Until then, retain the evidence-qualified diagnosis; do not add speculative polling retries, broad cache invalidation or asynchronous architecture changes.

## Outstanding blockers

Accessible investigation is complete. Only the following evidence prevents final historical attribution:

- Original FP button exception/traceback and its phase/missing resource, plus any separate actual Track Evolution exception. The approximate notes cannot recover these.
- As-run launch revision, interpreter/roots and manual/reference/timing/prior settings. Current editable imports and saved files establish the tested configuration, not a complete historical process snapshot.
- Genuine R18 FP lap/telemetry preparation and pre-capture Q1 completion history, required for a fully calibrated replay with original support.
- Which of the two RUS/ANT Q2 pairs the notes refer to, the original meaning of `SN`, and the exact time/visible failure mode of the R&D observation.

No manual code search, command execution or transfer between agents is needed to proceed with the confidence-supported fixes.

## Implementation handoff

Implementation can continue **in this same conversation** from the inspected working trees and saved evidence. Keep the current user edits intact. First implement the FP diagnostics/cache-directory/optional-telemetry boundary changes in `woPlanner/src/woplanner/quali/analysis.py` and its GUI error path; then add the shared missing-prerequisite/R18 checkpoint regressions and reason presentation in `service.py`, `track_evolution_policy.py`/`widgets.py` and the existing experimental display. Run the focused suites below and review the resulting diff. Keep calculation/admission changes out unless a new failing regression establishes their necessity. No branch, commit or push is part of this handoff.

Evidence is preserved under [outputs/r18_debug_diagnosis_20261025](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_debug_diagnosis_20261025). It contains scripts, compact findings, replay/control outputs, test logs, recording inventory and repository-preservation results. The original recording stays in its existing woData location; it was not rewritten or duplicated.

Representative commands already executed (run from the relevant repository; temporary scripts are also retained in the evidence bundle):

```sh
# Coordinator integration contracts, from woPlanner
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
  /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_session_info.py tests/test_quali_runtime_paths.py \
  tests/test_quali_queued_live_evidence.py tests/test_quali_publication_contract.py

# A: actual-stream replay and explicit prerequisite controls
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl QT_QPA_PLATFORM=offscreen \
  /Users/zhxutong/dr-wo/.venv/bin/python /tmp/r18-debug-A/replay_a.py
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl QT_QPA_PLATFORM=offscreen \
  /Users/zhxutong/dr-wo/.venv/bin/python /tmp/r18-debug-A/controls_a.py

# B: isolated offline diagnostic tests
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-debug-B/mpl QT_QPA_PLATFORM=offscreen \
  /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q -s \
  /tmp/r18-debug-B/test_d02_diagnostics.py --disable-warnings

# C: raw candidate inventory and reference availability controls
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl \
  /Users/zhxutong/dr-wo/.venv/bin/python /tmp/r18-debug-C/raw_laps.py
env MPLBACKEND=Agg MPLCONFIGDIR=/tmp/r18-mpl \
  /Users/zhxutong/dr-wo/.venv/bin/python /tmp/r18-debug-C/controls.py
```

Some diagnostics intentionally write only under `/tmp/r18-debug-*`; the saved copies can be inspected or rerun there without changing production data. Baseline full replay can take minutes because it processes physical histories; the proposed regression fixture should use the smallest causal packet prefixes/checkpoints.

Begin implementation with the two independently reproduced FP boundary defects and persistent error context. Add the shared prerequisite fixture next, then verify finish, admission and Half-live behavior together. Keep historical attribution qualified until the original exception or genuine FP preparation is available.
