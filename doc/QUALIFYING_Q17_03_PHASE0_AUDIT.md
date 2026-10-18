# Q17-03 Track Correction cross-round evidence audit

Validation gate: **INSUFFICIENT EVIDENCE**. Final verdict: **BLOCKED**.

Seven independent Qualifying sessions were inventoried and five were measured/replayed. Only R14, R16 and R17 preserve genuine message receive timestamps. This is insufficient to assess a general-purpose 2-second finalisation window against the requested minimum R17 plus three other comparable independently recorded sessions. No production fixes were implemented; the requested stop at the unmet validation gate applies to the implementation phase. Existing uncommitted session-state, traffic, projection and rendering work was preserved.

## A. Cross-round evidence

| Round | Available data | Observed late-position arrival delays (P50 / P95 / P99 / max, seconds) | 2-second coverage | Data limitations |
|---|---|---|---|---|
| R8 | 18,049 messages; timing, position, speed | Not measurable | Unknown | Archive Q1–Q3; inventory only; no receive clock. |
| R11 | 18,637 messages; timing, position, speed | Not measurable | Unknown | Archive Q1–Q3; inventory only; no receive clock. |
| R12 | 21,621 messages; timing, position, speed | Not measurable | Unknown | Archive Q1–Q3; no receive clock or ExtrapolatedClock; older FP artifact lacks pace-reference columns; sample UTC and lap session clocks are incomparable. |
| R13 | 19,076 messages; timing, position, speed | Not measurable | Unknown | Archive Q1–Q3; no receive clock. Misfiled live Q files actually identify Practice 3. |
| R14 | 18,462 messages; timing, position, speed | 0.259 / 0.914 / 1.074 / 1.249 | 100% (258/258) | Live mid-Q1 join; Q2/Q3 and finalised/ends observed. |
| R16 | 16,496 messages; timing, position, speed | 0.230 / 0.844 / 1.064 / 1.140 | 100% (194/194) | Live before Q1; Q1–Q3 and finalised observed. Diagnostic copy is byte-identical and counts once. |
| R17 | 8,134 messages; timing, position, speed | 0.286 / 0.771 / 0.818 / 0.907 | 100% (94/94) | Live mid-Q2 join, declared SessionPart=2 then 3; no Q1; Q3 Finished present, capture ends before finalised/ends. |

Delay is **message receive time of a position packet minus message receive time of the accepted timing completion** for observations whose acquisition timestamp belongs to an independently anchored completed lap. Acquisition time determines lap ownership; it does not measure arrival delay. Unique causal late-position evidence counts the first delivered occurrence of a sample timestamp with a received speed at or before that timestamp, no older than 5 seconds. No playback wall-clock measurements were used. Archive timestamps and sorted replay schedules are excluded from arrival distributions.

The cohorts include only independent start/end counter publications that agree with completed duration within the unchanged 1-second boundary-consistency tolerance. Conflicting publication ownership, retracted completions and incompatible clock coordinates are excluded. This is an evidence audit, not a replacement lap-attribution or traffic implementation.

**Late speed observations**, using the same independent lap boundaries and actual receive clocks:

| Round | Count | P50 | P95 | P99 | Max |
|---|---:|---:|---:|---:|---:|
| R14 | 252 | 0.240 | 0.839 | 1.054 | 1.249 |
| R16 | 171 | 0.226 | 0.695 | 0.834 | 1.030 |
| R17 | 87 | 0.286 | 0.782 | 0.835 | 0.981 |

**Sampling characteristics** (positive consecutive acquisition intervals per driver, seconds; all captured states, including pit/idle):

| Round | Position samples | Position P50 / P95 / P99 / max | Speed samples | Speed P50 / P95 / P99 / max |
|---|---:|---|---:|---|
| R12 | 349,602 | 0.260 / 0.440 / 0.520 / 1.380 | 361,636 | 0.240 / 0.401 / 0.481 / 1.321 |
| R13 | 381,722 | 0.240 / 0.420 / 0.500 / 1.360 | 371,888 | 0.240 / 0.440 / 0.481 / 1.360 |
| R14 | 302,764 | 0.240 / 0.420 / 0.500 / 1.340 | 298,848 | 0.240 / 0.440 / 0.520 / 1.400 |
| R16 | 363,528 | 0.240 / 0.420 / 0.501 / 1.320 | 356,620 | 0.240 / 0.440 / 0.520 / 1.360 |
| R17 | 154,946 | 0.240 / 0.420 / 0.500 / 1.181 | 153,076 | 0.240 / 0.440 / 0.480 / 1.321 |

All measured feeds have 22 driver streams, zero duplicate timestamps and zero backwards timestamps within each driver/channel delivery stream. Channels remain asynchronous and batched: median 88 driver observations per packet; P95 110; maximum 132 in R12–R16 and 110 in R17. Synthetic audit controls exercise duplicates and causal delivery ordering despite their absence in these recordings.

**Synchronisation evidence** (position observations, distinct from traffic-supported laps):

| Round | Current temporal matches | Future-speed rejections | Those rejections with a valid earlier received speed | Causal-history temporal matches | Remaining unmatched |
|---|---:|---:|---:|---:|---:|
| R12 | 200,860 | 147,620 | 147,620 | 348,480 | 1,122 |
| R13 | 235,642 | 143,176 | 143,176 | 378,818 | 2,904 |
| R14 | 70,114 | 232,650 | 232,628 | 302,742 | 22 |
| R16 | 107,206 | 256,190 | 256,168 | 363,374 | 154 |
| R17 | 37,268 | 117,678 | 117,656 | 154,924 | 22 |

The audit reproduces R17's 117,678 future-speed rejections (75.95% of 154,946 positions). Suitable already-received earlier speed exists for 117,656 of them; 22 remain without a usable causal speed. Recovery is therefore neither complete nor specific to R17. The history comparison is an offline causal counterfactual; no bounded production speed-history implementation has been installed or validated.

**Temporal completeness attainable with causal speed matches**, evaluated at acquisition-time lap boundaries with unchanged 0.5-second endpoint tolerance and maximum 1.0-second interior interval:

| Round | Comparable anchored laps | Immediate | +0.5s | +1.0s | +2.0s | +5.0s audit | +10.0s audit | Recorded interior gaps: laps / intervals |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| R12 | Incomparable clocks | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown | Not assessable within lap boundaries |
| R13 | 105 | Unknown | Unknown | Unknown | Unknown | Unknown | Unknown | 42 / 53 |
| R14 | 107 | 12 | 50 | 60 | 60 | 60 | 60 | 46 / 59 |
| R16 | 77 | 6 | 27 | 31 | 31 | 31 | 31 | 44 / 66 |
| R17 | 47 | 10 | 23 | 32 | 32 | 32 | 32 | 14 / 21 |

R13 has 61 eventually temporally complete raw/causally speed-matched lap traces, but their real arrival deadlines cannot be measured. R12 has 155 otherwise anchored durations with incompatible sample/lap clocks and four unanchored or inconsistent completions. It is excluded from lap-span/window inference rather than reported as 155 genuine telemetry failures.

The temporal support opportunity is 12→60 laps in R14, 6→31 in R16 and 10→32 in R17 after causal speed matching and corrected acquisition-time ownership. These counts do **not** certify route validity, circuit-length sanity, candidate eligibility, 50% ahead coverage, clean air or fitting eligibility. The production complete-span admission count remains zero.

The 5/10-second columns are evidence-only comparisons, not authorised policy changes. R14 has six additional positions and R16 five between 1 and 2 seconds, without changing temporal complete-lap counts. No measured additional useful positions arrive between 2 and 10 seconds in the comparable cohorts. Longer waits cannot fill recorded acquisition-time gaps. These files establish missing observations; they cannot prove whether the cause was upstream acquisition, transmission or incomplete recording.

## B. Validation gate

**INSUFFICIENT EVIDENCE**. The three live sessions show useful late-position coverage of 100% within 2 seconds and a benefit outside R17. They do not meet the minimum comparable independent-session set needed for a general-purpose delay decision. The four additional archive sessions cannot replace a missing receive clock. Multiple files, copies, Q parts or synthetic latency injections do not count as additional independent sessions.

At least one additional independently recorded Qualifying weekend is needed with original message receive timestamps, source timestamps, full timing boundaries, CarData and Position batches, and preserved delivery order. Broader sampling/quality differences would strengthen representativeness because the current three live feeds share nearly identical sampling and batching characteristics. Without implementing and comparing the change, the audit also cannot prove that correctly handled sessions and ordinary live timing are unaffected.

## C. Changes made

Production files and thresholds were not modified. The mathematical model, OLS, PUSH denominator, clean-air criteria, 1.0-second completeness threshold, 50% ahead coverage, cutoff mathematics, Q17-01/02 fixes and lap-status colours remain unchanged. Telemetry synchronisation, deferred finalisation and rendering recovery implementations are withheld at the unmet validation gate.

New audit files:

- `scripts/audit_q17_03_cross_round.py`: causal speed-availability audit, explicit acquisition/publication/arrival clock separation, independent boundary cohorts, percentile/window/gap evidence, immutable input/source hashes.
- `tests/test_q17_03_evidence_audit.py`: nine controls for audit methodology.
- `temp/q17_03_phase0/inventory.json`: complete recording inventory and exclusions.
- `temp/q17_03_phase0/cross_round.json`: detailed raw evidence including per-lap windows and source hashes.
- `temp/q17_03_phase0_baseline/run_baseline.py`, `summary.json` and five round JSON files: direct production store/service replay instrumentation, exact admission/fit gates and per-part results.
- This report.

## D. Before and after replay

All five baseline replays completed using identical production source hashes and speed=0 equivalent direct synchronous delivery in recorded order. Original live message source types and receipts were preserved; archive receipt coordinates were synthetic and excluded from arrival measurements. Replay used no real-time sleep. The existing ReplayPlayer relabels envelopes as archive; direct envelope reconstruction avoids silently changing live ExtrapolatedClock receipt adjustment during this evidence audit. This is a documented limitation of equating player and live-envelope semantics, not a production change.

| Round | Messages | Positions retained / valid temporal matches | Production admitted complete spans | Pace candidate laps | Traffic-supported candidates | Eligible clean-air observations | Supported driver fits | Track Correction | After / regression comparison |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| R12 | 21,621 | 200,860 / 200,860 | 0 | 0 | 0 | 0 | 0 | Unavailable throughout | Not executed: gate unmet |
| R13 | 19,076 | 235,642 / 235,642 | 0 | 87 | 0 | 0 | 0 | Unavailable throughout | Not executed: gate unmet |
| R14 | 18,462 | 70,114 / 70,114 | 0 | 72 | 0 | 0 | 0 | Unavailable throughout | Not executed: gate unmet |
| R16 | 16,496 | 107,206 / 107,206 | 0 | 62 | 0 | 0 | 0 | Unavailable throughout | Not executed: gate unmet |
| R17 | 8,134 | 37,268 / 37,268 | 0 | 33 | 0 | 0 | 0 | Unavailable throughout | Not executed: gate unmet |

Baseline finalisation calls are synchronous with candidate-lap admission, hence their intentionally deferred-window duration is 0 seconds. This is distinct from the measured telemetry arrival delays and is not a proposed bounded-deadline implementation. There is no after-finalisation distribution because the change was not implemented. Baseline source hashes did not change during any replay; source consistency and observed segment transitions are recorded, but no post-fix regression verdict is claimed.

R12's older FP `lap_roles.csv` schema lacks reference columns needed by the production FAST classifier, so it contributes no pace candidate laps. This is a companion-data limitation; no artificial prior or fitted sample was injected. Per-round JSON includes per-part pace samples, finalisations, diagnostic checkpoints and transitions.

## E. Tests

Passed: **9 audit controls** and **84 existing production regression tests**, **93 total**. Failed: **0**.

Audit controls cover batched future-last versus received earlier speed, receive-clock latency independently of acquisition time, positions received after completion, exclusion of later received speed from causal availability, duplicates in useful-delay distributions, exact inclusive 2-second deadline measurement, evidence beyond the deadline, unchanged endpoint/interior tolerances, archive receipt exclusion and incomparable archive sample/lap clocks.

Commands run:

```sh
PYTHONPATH=../woData/src:../woStrategy/src:src ../.venv/bin/python -m pytest -q tests/test_q17_03_evidence_audit.py
QT_QPA_PLATFORM=offscreen PYTHONPATH=../woData/src:../woStrategy/src:src ../.venv/bin/python -m pytest -q tests/test_quali_physical_traffic.py tests/test_quali_traffic_lifecycle.py tests/test_quali_track_evolution_diagnostic.py tests/test_quali_widgets.py tests/test_quali_service.py
PYTHONPATH=../woData/src:../woStrategy/src:src ../.venv/bin/python scripts/audit_q17_03_cross_round.py --output temp/q17_03_phase0/cross_round.json
PYTHONPATH=../woData/src:../woStrategy/src:src ../.venv/bin/python temp/q17_03_phase0_baseline/run_baseline.py
```

Not executed: post-implementation replay and new production tests for bounded speed history, missing/stale speed handling, out-of-order insertion, corrected lap attribution, pending finalisation deadlines, sparse message/deadline resolution, segment end/interruption, accelerated playback of pending finalisation, or rendering failure/decode failure/retry/recovery. The audit deadline and duplicate tests validate measurement code only. Existing passing widget tests do not establish rendering recovery. The gate prevents the corresponding implementation.

## F. Remaining uncertainty

The historical R17 completely blank tab was not reproduced and its actual cause remains unknown. Inspection confirms the existing equal-diagnostic early return, swallowed RuntimeError in the completion callback and unchecked QPixmap PNG decoding; these remain unfixed at the validation gate.

Recorded >1-second observation gaps remain after hypothetical causal matching and waiting. Their upstream cause is not established. Missing acquisition data must remain unsupported; waiting cannot invent it. Improved temporal coverage does not establish valid physical-traffic support or reliable track-evolution fitting. The mathematical forecasting model has not been validated by these data-pipeline audit or regression tests.

## G. Final verdict

**BLOCKED** on the cross-round arrival-evidence gate. Available evidence is encouraging for a bounded 2-second window, but a fourth independent receive-clock Qualifying recording is missing. No implementation or claim of issue resolution is made.
