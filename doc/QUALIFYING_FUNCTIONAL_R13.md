# R13 Qualifying functional pass

## FP operational timing

The previous zero-sample result came from applying FastF1's full-lap
`IsAccurate`/`IsValid` flags to partial OUT/IN laps. Those flags describe a
complete timing lap, while the planner deliberately measures pit exit to lap
end and lap start to pit entry. The extractor now permits a non-deleted,
dry partial pit lap with both timing boundaries; full laps retain the accuracy
checks and run-structure validation.

R13 regenerated artifact evidence:

| Category | Candidates | Accepted | Median | P75 | P90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| OUT | 302 | 52 | 106.100 s | 115.857 s | 127.589 s |
| IN | 290 | 52 | 120.818 s | 136.262 s | 146.819 s |
| PREP/COOL | 262 | 12 | 117.895 s | 132.656 s | 141.930 s |

The accepted representative values use the unchanged IQR-filtered median:
OUT 104.380 s (51 selected), IN 120.289 s (52), and PREP/COOL 127.340 s (12).
Rejected candidates remain auditable; the main reasons are invalid run
structure (734), multiple prep/cool laps (642), deleted (25), and unusable lap
within a run (23). Only one partial pit lap is rejected by accuracy/validity.

## Lap state and track evidence

The live classifier exposes `UNKNOWN`, `NOT ACTIVE`, `FIRE-UP`, `IN-PIT`,
`PIT-LANE`, `QUEUE`, `OUT`, `SLOW`, `FAST`, and `IN`. Structural operational
evidence takes priority over pace: only `FAST` and `SLOW` may be
pace-reclassified within an eligible normal timed lap. Outbound moving pit-lane
transit is `PIT-LANE`; a stopped outbound car is `QUEUE`; inbound pit-lane
stops, including weighbridge-style stops, remain `PIT-LANE` without stronger
pit-box evidence. `IN-PIT` is not asserted from `InPit`, speed or elapsed time
alone. Insufficient evidence remains `UNKNOWN`, and late connection starts at
the strongest currently supported state without fabricating earlier transitions.

The classifier receives the common fastest valid FP1–FP3 reference, including
the start-to-5%, start-to-S1, and S1-to-S2 intervals. New on-track laps
alternate their provisional FAST/SLOW prior; 5% and S1 can correct it; S2 only
permits FAST-to-SLOW. Completed PUSH requires confirmed timed-lap identity and
`FAST`; completed SLOW laps are COOL/PREP evidence and are not PUSH/evolution
samples. The R13 evidence and deferred split-patch association backlog are in
[the operational-state regression](QUALIFYING_OPERATIONAL_STATE_R13.md).

The offline progression routine now retains driver, compound, stint, fresh-tyre
and clean-air provenance, indexes eligible field PUSH laps globally, fits each
driver independently, and averages supported driver slopes. It does not blend a
driver's compound change into one fit: R13 LAW's medium-to-soft sequence is
therefore not treated as track evolution. The Plan exposes the shared 3.0 s
ahead-gap threshold; it is a clean-air eligibility input and does not alter lap
FAST/PUSH classification.

## Conditional double-yellow release

The Plan displays a latest-safe release as *time remaining*. It sums one
invalidated OUT/PREP/PUSH attempt, IN, turnaround, replacement OUT/PREP, and
the next-PUSH margin. Timing provenance is shown alongside every component.
Red-flag waiting is excluded because a red flag pauses the qualifying clock;
the result is conditional on the local/double-yellow scenario, not a guarantee.

For the default R13 FP-derived Plan values with a prepared first run,
the required remaining countdown is 817.198 s: 315.189 invalidated attempt +
120.289 IN + 120 turnaround + 104.380 replacement OUT + 127.340 replacement
PREP + 30 s margin.

## Verification

Focused lap-state, planner, service, widget, and FP extraction tests pass.
The R13 replay determinism suite passes with batches 1/7/500, replay speeds
0/25/1, and extra GUI reads; canonical reducer/planner transitions remain
identical.
