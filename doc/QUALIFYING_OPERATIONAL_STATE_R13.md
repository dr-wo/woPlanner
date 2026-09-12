# R13 Qualifying operational-state regression

## Implemented operational-state contract

The user-facing state vocabulary is `UNKNOWN`, `NOT ACTIVE`, `FIRE-UP`,
`IN-PIT`, `PIT-LANE`, `QUEUE`, `OUT`, `SLOW`, `FAST`, and `IN`.
`NOT ACTIVE`, `FIRE-UP`, `IN-PIT`, `PIT-LANE`, `QUEUE`, `OUT`, and `IN` are
structural operational states. Structural evidence always takes precedence over
pace classification. Only `FAST` and `SLOW` may be pace-reclassified, and only
within an eligible normal timed lap.

An outbound moving car in pit-lane transit is `PIT-LANE`; a stopped outbound
car is `QUEUE`; resuming movement returns it to `PIT-LANE`; confirmed departure
then establishes `OUT`. Inbound pit-lane stops, including weighbridge-style
stops and hot swaps, remain `PIT-LANE` without stronger pit-box evidence.
`IN-PIT` is therefore not asserted for R13 solely from `InPit`, zero speed, or
stationary duration. Insufficient evidence is deliberately `UNKNOWN`. A late
connection or segment acquisition starts at the strongest current supported
state and does not invent unobserved historical transitions.

A completed PUSH requires both a confirmed normal timed-lap identity and a
`FAST` classification. `SLOW` observations remain operational COOL/PREP
evidence and cannot enter PUSH, track-evolution, or effective-finish inputs.

The full 19,076-message R13 archive was replayed through the message-driven
reducer and service. Time values below are archive session seconds. The previous
replay logged 237 completed-lap events; those were **accepted events**, not an
independently validated count of normal timed laps. The corrected reducer emits
105 associated normal timed laps: 98 classified FAST and 7 SLOW at completion.
It rejects 293 **association evidence events**, not 293 distinct laps: 123
counter updates without a lap time in that patch, 91 lap-time publications
without a counter update, 53 intervals crossing a pit boundary, and 26
intervals crossing a segment boundary. The service admits 87 completed PUSH
events (Q1 37, Q2 32, Q3 18); rejected intervals cannot enter the PUSH count,
track evolution, or effective-finish trigger.

Against the earlier stored R13 replay artifact, PUSH support (37/32/18),
evolution estimates (Q1 0.0283411569, Q2 0.0031284824, Q3 0.0054716202
seconds per field PUSH), and the 33 driver/segment effective-finish memberships
are unchanged. No material change to those downstream outputs was observed;
their input contract now explicitly excludes unassociated lap times.

| Driver / segment | Corrected chronological operational evidence |
| --- | --- |
| PIA Q1 | UNKNOWN → NOT ACTIVE → FIRE-UP 890.682 → PIT-LANE 900.562 → OUT 919.562 → FAST lap 3, 82.924 s at 1097.686 → SLOW → IN 1212.398 → PIT-LANE 1212.564 → OUT 1677.685 → UNKNOWN at boundary 1770.359 → SLOW lap 6, 107.848 s at 1878.132 → FAST lap 7, 82.987 s at 1961.152 → IN 2061.146 → PIT-LANE. |
| STR Q1 | UNKNOWN → NOT ACTIVE → FIRE-UP 839.322 → PIT-LANE 852.402 → OUT 865.522 → FAST → SLOW lap 3, 86.947 s at 1049.183 → IN 1158.964 → PIT-LANE → OUT 1315.204 → UNKNOWN at boundary 1416.003 → FAST lap 6, 85.407 s at 1501.205 → IN → PIT-LANE → OUT 1713.806 → UNKNOWN → FAST lap 9, 85.222 s at 1907.119 → IN → PIT-LANE. |
| ANT Q3 | PIT-LANE 3564.407 → OUT 3638.213 → UNKNOWN at boundary 3730.760 → SLOW in live on-track S1 evidence, then IN 3841.132 → PIT-LANE → OUT 3873.014 → UNKNOWN at boundary 3967.602 → FAST lap 16, 82.093 s at 4049.759 → IN → PIT-LANE → OUT 4285.136 → UNKNOWN at boundary 4380.492 → SLOW at later S1, then IN → PIT-LANE. |

Earlier target traces had moving QUEUE-labelled pit departures. Corrected
target traces have **zero QUEUE transitions** and show PIT-LANE movement instead.
The following older observations are explicitly unassociated: PIA 120.017 s
(time without same-patch counter) and 552.559 s (pit boundary inside interval);
STR 113.676 s (time without counter) and 253.109 s (pit boundary); ANT
383.639 s (starts before Q3), 114.259 s (time without counter), and 122.737 s
(pit boundary). Neither 552.559, 253.109 nor 383.639 is a completed SLOW lap.
UNKNOWN is visible at the rejected counter boundaries until a current-lap
checkpoint supports pace. Later SLOW states arise from current S1 evidence,
not from classifying the rejected duration itself.

The reducer keeps event provenance for pit entry/exit, counter updates,
LastLapTime publication, sector updates, rejected associations and segment
starts. It requires counter and lap time in the same TimingData patch, a known
event time, no intervening pit boundary, no cross-segment interval, and no
pit-arrival state. A sector status-only patch cannot make stale sector values
eligible for FAST/SLOW. Missing pit-box geometry leaves IN-PIT unasserted;
PIT-LANE can persist at a weighbridge or during a hot swap. A late connection
may acquire FAST directly from a supported timed-lap checkpoint. Requiring
same-patch publication conservatively excludes real laps if the feed sends
their counter and time separately; the 123/91 evidence counts make that
association policy a useful review point.

## Known limitation / evidence-triggered backlog item

**Deferred lap-completion association:** The current implementation
conservatively requires `NumberOfLaps` and `LastLapTime` to identify the same
completion within the same timing patch. This prevents the stale-time
misassociation observed in R13, but may reject genuine laps if the feed
legitimately publishes the counter and lap time in separate patches. Do not
relax this using a generic timing window. If future live/replay evidence
confirms legitimate split-patch publication, replace the strict rule with
identity-aware pending lap-completion matching and add the observed case as a
regression test.

This is intentional conservative behaviour. There is currently no evidence
requiring implementation; revisit it only when a real live or replay case
demonstrates legitimate split-patch publication.

Focused tests: 61 passed. The full R13 deterministic replay test passed with
batch sizes 1, 7 and 500, speeds 0, 25× and 1× virtual scheduling, and extra
snapshot/poll reads 0, 3 and 7 respectively. It compared exact canonical
domain sequences and unchanged evolution-estimate outputs.

Review approximately 54 key lines in these hunks:

1. `../woData/src/wodata/live/quali_reducer.py:262-275` — sector identity after a boundary (14 lines).
2. `../woData/src/wodata/live/quali_reducer.py:278-293` — normal-lap association rejection (16 lines).
3. `../woData/src/wodata/live/quali_reducer.py:488-504` — inbound/outbound state priority (17 lines).
4. `src/woplanner/quali/service.py:411-417` — completed PUSH eligibility (7 lines).
