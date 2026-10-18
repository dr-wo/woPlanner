# Q17-03 implementation and cross-round validation

The approved revised validation gate has been completed. **READY FOR INDEPENDENT REVIEW** for Q17-03. The broader project's preexisting Race/strategy failures remain separately documented below.

Validation scope is the exact Q17-03 runtime in `temp/q17_03_implementation/frozen_q17_runtime/manifest.json`. During verification a separate chat added optional shadow analysis to the shared service and Track Evolution file. Those changes were preserved. Only the isolated copy has those foreign additions removed; its 17 module/runner byte hashes exactly match the seven completed pre-shadow replays. R12 was rerun against this pinned copy. The consolidated audit proves content equivalence across all eight runs. No shared file was reverted. The current shared runtime, with shadow disabled, separately passed the same 86 integration controls with stable source hashes; enabled shadow behaviour belongs to that other task.

## 1. Implementation and changed files

The implementation covers causal speed matching, acquisition-time lap ownership, bounded traffic finalisation, rendering recovery, and a non-blocking telemetry-delivery warning. The approved revised gate uses three genuine Live Q receive-time recordings (R14/R16/R17) and five cross-round inputs (R12/R13/R14/R16/R17). Phase 0 evidence is retained unchanged; its earlier gate decision is superseded by the user's approval.

Q17-03 production changes:

| Repository | Files | Purpose |
| --- | --- | --- |
| woData | `src/wodata/live/quali_models.py`, `quali_reducer.py` | Immutable received speed history; explicit position acquisition coordinate; traffic delivery-clock metadata. |
| woData | `src/wodata/live/models.py`, `recorder.py`, `recovery.py`, `replay/schema.py`, `replay/player.py` | Preserve provenance of genuine recorded receive timestamps through capture, recovery and ReplayPlayer. |
| woPlanner | `src/woplanner/quali/physical_traffic.py` | Independently proven acquisition-time lap bounds, ordered/deduplicated evidence, receive-deadline filtering, bounded retention. |
| woPlanner | `src/woplanner/quali/traffic_finalisation.py`, `service.py` | Normal publication, finite grace, same-index replacement, bounded observed-delay monitor and live polling. |
| woPlanner | `src/woplanner/quali/models.py`, `widgets.py` | Warning snapshot/UI; asynchronous render and PNG decode failures; explicit retry. |

Validation additions include `scripts/verify_q17_03_pipeline.py`, woData `tests/test_quali_causal_speed_history.py`, and woPlanner `tests/test_quali_physical_traffic_late.py`, `test_quali_late_telemetry.py`, `test_quali_live_traffic_clock.py`, `test_quali_delay_dedup_retention.py`, `test_quali_render_recovery.py`, `test_quali_telemetry_warning_ui.py`. Compatibility controls in `test_quali_projection_version_skew.py` and superseded ownership expectations in `test_quali_traffic_review_blockers.py` were updated. The R16 historical-cutoff validation serializer now supports immutable mappings and excludes added traffic-only speed history from its ordinary-state digest.

This workspace already contained substantial uncommitted work. The file list above describes Q17-03 areas, not ownership of every current Git diff. Concurrent Q17-01/02 review corrections to clock publication, finish-state latching and participation bootstrap were preserved and are not credited to this task. No commits were made.

## 2. Finalisation and exceptional grace

Ordinary completion, timing, rank and planning publish immediately. Only traffic evidence remains pending. At completion reception +2 seconds, the service publishes the available traffic result using evidence received by that cutoff. Unsupported evidence stays unsupported. It does not routinely wait four seconds.

Additional relevant evidence received in `(2s, 4s]` can replace the same existing fitting sample, invalidate the cached diagnostic, and rebuild the result. Duplicate observations do not append fitting samples or repeatedly recompute an unchanged result. At the inclusive four-second boundary the result locks; subsequent evidence cannot reopen it. Later arrivals may still contribute to bounded delivery diagnostics.

Genuine live polling uses UTC receipt deadlines anchored to monotonic elapsed time. Delayed processing does not restart the wait at processing time. A capped controller drain must reduce already-received queued evidence before wall-clock freezing; source-end finalisation is deferred until that backlog is reduced. Explicit stop also reduces the received backlog before resolving pending traffic. Segment changes and terminal session states resolve pending results with the available evidence. Replay advances only through recorded receive/playback clocks, including sparse callbacks and EOF; accelerated replay has no wall-clock sleep.

Memory is bounded: speed history retains a 20-second horizon (or freshness +4 seconds if greater), at most 512 samples per driver and 64 drivers. Traffic rows/states retain 240 seconds and at most 4,096 rows per driver; independent bounds retain 600 seconds, at most 128 publications per driver and 1,024 completion records. Pending results and finalisation records are capped at 1,024.

## 3. Telemetry latency measurement

Speed matching selects a sample already received, acquired no later than the position, and within the unchanged five-second freshness limit. Later CarData does not retroactively supply a speed for a previously processed position. Acquisition coordinates determine lap ownership via independent timing publications and unique `(start, end]` bounds. Missing or conflicting bounds stay unknown.

Delivery diagnostics use `telemetry receive UTC - completion receive UTC`, only after a genuinely timestamped observation arrives and its acquisition can be reliably associated with that completed lap. A deadline expiring, an absent observation, or replay execution duration is not a measured late arrival. Archive scheduler timestamps do not become claimed live receipt measurements. ReplayPlayer retains trusted original live receipts rather than substituting accelerated wall time.

The accepted Phase 0 arrival evidence remains applicable: maximum useful post-completion position delays are 1.249s/1.140s/0.907s for R14/R16/R17; speed delays are 1.249s/1.030s/0.981s. These recordings contain no measured valid position arrival beyond two seconds. Grace and warning controls therefore use explicitly synthetic cases; they do not estimate real-world late-arrival frequency.

## 4. Warning logic and UI

One amber, non-modal status line appears in **Quali Plan → Track Correction** when at least three distinct driver-laps have actual observed delays greater than two seconds within the rolling ten-minute window. Threshold/window are service constructor parameters; no settings panel was added. It clears when the count drops below threshold and works even when no supported fit exists.

First-receipt identities deduplicate batched/repeated speed and position observations. A driver-lap counts once; further late samples do not refresh its warning-window age. Trusted receipt provenance is required. Diagnostic state is capped at 1,024 completions/late laps, 131,072 observation identities and 256 forgotten-stream floors, with bounded expiry indexes. Under extreme retention pressure, forgotten acquisitions are conservatively suppressed to avoid treating retransmissions as new late arrivals; this can undercount warnings.

The warning describes delivery quality. It is not a claim about the mathematical estimate's predictive accuracy.

## 5. Per-round comparison

The baseline is the accepted Phase 0 runtime, not Git HEAD. Available positions are unique observations; matched/retained rows and admitted spans are cumulative over the session, not simultaneous resident buffer sizes.

| Round | Available positions, unchanged | Valid speed matches / retained rows, before → after | Admitted complete spans, before → after | Pace candidates, unchanged | Traffic / clean-air eligible / driver fits, before and after | Track Correction, before and after |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| R12 archive | 349,602 | 200,860 → 348,480 | 0 → 0 | 0 | 0 / 0 / 0 | Unavailable |
| R13 archive | 381,722 | 235,642 → 378,818 | 0 → 52 | 87 | 0 / 0 / 0 | Unavailable |
| R14 Live | 302,764 | 70,114 → 302,742 | 0 → 44 | 72 | 0 / 0 / 0 | Unavailable |
| R16 Live | 363,528 | 107,206 → 363,374 | 0 → 24 | 62 | 0 / 0 / 0 | Unavailable |
| R17 Live | 154,946 | 37,268 → 154,924 | 0 → 21 | 33 | 0 / 0 / 0 | Unavailable |

Sampled downstream blockers are preserved in `temp/q17_03_implementation/traffic_support_blockers.json`. R14 NOR lap 4 has a 1.020s interior gap and R17 HUL lap 16 has a 1.140s gap, exceeding the unchanged 1.0s limit. R14 OCO/GAS and R17 HAM/PIA target traces pass complete-span validation but the shared evaluator returns `physical phase unavailable` because competing streams still lack complete phase anchors. For example, at GAS lap 3 finalisation OCO lap 3 is valid for 303 of 356 target timestamps, while its ongoing lap 4 lacks its end anchor for the remaining 53. PIA lap 15 similarly sees HAM lap 16 valid at 58 of 356 timestamps and the ongoing lap 17 unproven at 298. The sampled physical/shared-evaluator hashes match the final runtime; the diagnostic used earlier direct replay reports whose only later runtime changes were the service's inactive-on-replay live timer and validation instrumentation.

These are improved retention and independently validated target spans. They are not improved traffic-supported candidate counts, successful fitting, or evidence of predictive accuracy. The full-field phase veto and gap/coverage criteria were retained.

## 6. Regression and replay results

All eight mandatory paths passed: five direct recording replays and three production `ReplayPlayer(speed=0)` replays for R14/R16/R17. Inputs match Phase 0 byte hashes. There are no causal-match violations, unresolved pending results, observed late samples, grace revisions or warning events in these recordings. Synthetic cases establish grace/warning behaviour only.

The authoritative evidence set is listed in `temp/q17_03_implementation/after_frozen_q17_final/consolidated_frozen_runtime_audit.json`: seven stable pre-shadow reports from `after_live_clock_final`, plus the freshly pinned R12 report from `after_frozen_q17_final`. All 17 recorded module/runner byte hashes at start/end equal the frozen manifest, irrespective of path location. The earlier R12 run that detected on-disk source drift is explicitly excluded, as are earlier implementation labels.

| Verification | Result |
| --- | --- |
| woData complete suite | 289 passed, 7.02s (`data_verified.txt`). |
| woPlanner complete collected suite | 941 passed, 21 failed, 1078.70s (`planner_live_clock_final.txt`); two cached pre-repair participation fixtures plus 19 outside-Qualifying failures. |
| Current-fixture partition, excluding three costly replay controls | 940 passed, 19 outside-Qualifying failures, 3 deselected, 551.46s (`planner_partition_final.txt`). The three excluded R16/R13 replay controls passed in the complete execution. |
| Projection fixture repair | 22 passed, original assertions retained (`projection_fixture_final.txt`); fixes the two cached-fixture failures. |
| Frozen Q17-03 critical controls | 86 passed, 40.89s (`frozen_controls_final.txt`). |
| Shared runtime with concurrent shadow disabled | Same 86 controls passed, 40.56s; three changed shared-source hashes stable (`shared_shadow_disabled_controls.txt`, `shared_shadow_integration_start.json`). |
| Mandatory recording replays | 8 completed; 126,889 ordinary-state control checks, zero mismatches. |

There are 467 unique Qualifying test cases in the recorded 962-case collection. Across the full execution and corrected-fixture rerun, all 467 passed. Overlapping targeted counts above are not additional unique cases. The broader project suite is not green: its 19 remaining Race/strategy failures are listed in the complete/partition logs. All 19 reproduce without the relevant Q17 changes, with identical per-test failure signatures and locations (`head_negative_control/summary.json`). Seventeen synthetic Race/strategy failures reproduce against isolated Git HEAD packages; their fixtures are byte-identical to HEAD. They concern lap-based fixtures versus sector outputs, a missing strategy cache file, and fake adapters lacking multi-segment support.

The two R12 Race failures reproduce in a compatibility-preserving control using HEAD recovery/player/schema, exact Phase 0 models with the new receipt field removed, and exact Phase 0 shared traffic. This is a hybrid control, not an all-HEAD R12 replay. Assertions remain clean laps 0 versus 204 and tyre markers 1 versus 2. The zero clean-lap result comes from the existing default `_live_traffic_gaps` quarantine when `use_sector_boundary_traffic=False`, not the Q17 receive metadata or an executed shared phase evaluator. These unrelated failures were preserved, not repaired or suppressed.

Recorded Live replay results use genuine original receive clocks in direct and Player modes. Every normal evidence cutoff is exactly completion reception +2s. Publication in callback-only replay occurs at the next message advance:

| Live round | Publication-delay P50 | P95 | P99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| R14 | 2.140s | 2.726s | 2.929s | 2.945s |
| R16 | 2.193s | 2.701s | 3.147s | 3.471s |
| R17 | 2.254s | 2.490s | 2.553s | 2.581s |

Direct and Player clocks produce identical values. These are first-advance replay publication clocks, not measured historical feature publication times or telemetry-arrival delays. Active live polling and queued/sparse delivery are tested separately. Archives supply processing/ownership regression evidence only, with no invented receive-latency measurements.

Tests cover batched/causal channels, missing/stale/out-of-order/duplicate data, independently proven lap ownership, mixed UTC/session retention and capacity eviction, normal +2s publication, valid 2–4s revisions, after-4s locking, same-index idempotence, sparse/segment/end/stop paths, accelerated replay, warning thresholds/clearing/trust/dedup retention, and rendering failures/retry/recovery/no-fit presentation. Live-clock tests additionally cover delayed processing and a received queue backlog at source end.

## 7. Preservation evidence

All five direct replays preserve the complete ordered nine-field pace-candidate identities, including global PUSH index, tyre installation and lap identity. Their segment transition records match Phase 0 exactly. All Player runs preserve part/sequence transitions and candidate identities. R17 Player retains the existing source-label countdown difference: its initial Q2 remaining time is 512.0s versus direct live 509.955102s. This is explicitly reported rather than claimed to be exact direct/Player clock equality.

All eight traffic-disabled controls passed 126,889 per-message/EOF comparisons with zero differences. Existing Q17-01/02 clock freshness, reconnect participation and finish-latching regressions are included in the passing frozen/current controls and Qualifying suite. No unsupported traffic result was promoted to a fit. All retained/candidate/fit metrics, input hashes, source hashes and current shared-source differences are in the consolidated audit.

In the validated Q17 runtime the Track Correction implementation (`track_evolution.py`), shared physical traffic evaluator (`wostrategy/analysis/traffic.py`) and pre-qualifying analysis (`pre_quali.py`) are byte-identical to Phase 0. Their SHA-256 values are respectively `2588b7bd1ea0c3badd6b5f4fa36fc48276ea6e3b8c462cb0db145f370ef85263`, `16f44199b8e515aa048bed684e524dcb6f259ead5d4892fc31b8f5275dafb933`, and `70d5ceda3c914eb040e085692f234e8cd9bf684715edc8ba45d9b90308a1dd73`. This preserves OLS, PUSH denominator, clean-air eligibility, 1.0s telemetry gap limit, 50% ahead coverage, and cutoff mathematics. The separately added shared shadow option is not part of this byte-equivalence assertion. Lap-status colour mappings were not changed.

Phase 0 did not retain every per-message ordinary state or freeze the original dirty runtime. Exact original per-message timing/finish equality therefore cannot be reconstructed honestly. The new runner instead compares final-runtime traffic-enabled and traffic-disabled services receiving identical immutable raw snapshots at every message and EOF. It compares ordinary lap state, timing, rank/classification, finish/planning state and current/actual cutoffs; intended Track Correction forecasts are excluded. This control establishes non-interference in the final runtime, not an original Phase 0 full-state before/after comparison.

## 8. Limitations and verdict

Improved telemetry retention, independent complete-lap support, traffic-supported candidates, supported fitting, and predictive accuracy are distinct outcomes. More retained observations or admitted spans do not by themselves establish valid ahead-car gaps, clean-air eligibility, or a fit. No predictive-accuracy evaluation is claimed.

R12 lacks a comparable independently established acquisition/session time mapping and the older FP input lacks the needed pace-reference columns; it remains unsupported. R17 recording coverage begins in Q2, so it cannot validate Q1. The historical R17 completely blank tab's root cause remains unverified: the rendering changes cover confirmed exception/decode/retry failure paths only.

The two-second evidence cutoff must also be distinguished from callback publication time. Replay callbacks may first advance after two seconds in a sparse stream; those callback delays are not historical feature publication measurements. Active live polling resolves the deadline on its next poll once received backlog is reduced; it cannot guarantee real-time publication during a blocked event loop.

The exact Q17-03 runtime passed the approved five-session/three-live-session gate, all mandatory deadline and recovery controls, and all 467 collected Qualifying cases after the existing fixture repair. Its production mathematics remain byte-identical to Phase 0. No unresolved Q17-03 failure remains. The broader project still has 19 independently reproduced failures outside this task, and enabled concurrent shadow analysis requires its own validation.

**Verdict: READY FOR INDEPENDENT REVIEW.** No commits or publication were performed. Work stops after this report.
