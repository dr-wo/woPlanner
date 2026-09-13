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

## Q2 T−03:00 causal Track Evolution acceptance

The causal replay cut-off is archive timestamp **00:50:24.131**, session time
**3024.131 s**, sequence
**12425**, whose effective Q2 remaining time is exactly **180.0 s**. It is the
latest processed Q2 domain state at or before T−03:00; the next processed Q2
state is below 180 seconds. The selection is driven by reducer session time,
not the presentation clock. The rendered shared view-model artifact is
[`qualifying_track_evolution_r13_q2_tminus3.png`](assets/qualifying_track_evolution_r13_q2_tminus3.png), with exact provenance in
[`qualifying_track_evolution_r13_q2_tminus3.json`](assets/qualifying_track_evolution_r13_q2_tminus3.json).

Physical ahead traffic is reconstructed from causal Position and speed samples
with the shared circular-phase evaluator. Track Evolution retains Q1, Q2 and
Q3 observations as one continuous Qualifying history. The global PUSH index
continues across part boundaries; the part remains sample provenance. The
TimingAppData `Stints` entry index identifies a tyre installation across those
boundaries. The first FAST/PUSH on each installation that passes the existing
fresh-tyre, physical clean-air (>3.0 s), and ahead-coverage (≥50%) checks is
the only evolution observation from that installation. Missing installation
identity is excluded rather than inferred. Driver slopes use the existing
modal-compound rule to avoid mixing compounds; their aggregate is weighted by
the number of fitted observations per driver, not by an equal driver vote.
There is no validated true track-state regime-reset mechanism; introducing one
requires separate evidence and is outside this change.

At the R13 cut-off there are 19 associated Q2 timed laps, 18 Q2 FAST/PUSH
samples (55 session-wide), 44 fresh samples, 43 with enough ahead coverage, 13
physically clean first-eligible installation observations, and **2 drivers / 4
laps used**. ANT's SOFT fit uses Q1 lap 3 (global PUSH 16, install 0) and Q2 lap
8 (PUSH 41, install 2), slope **−0.01696 s/push**. LAW's SOFT fit uses Q2 laps
12 and 15 (PUSH 38 and 53, installs 2 and 3), slope **+0.01560 s/push**. The
sample-weighted aggregate is **−0.00068 s/push**, with population-style,
fit-support-weighted driver-slope dispersion **σ = 0.01628 s/push**. LAW's Q1 MEDIUM lap 3
(install 0) remains visible in the eligible-observation provenance but is not
blended into the SOFT fit. Later PUSH laps on a selected installation do not
become additional observations. The previous Q2-only diagnostic used LAW alone:
1 driver / 2 laps, aggregate +0.0156 s/push. The PNG is rendered from the same
immutable diagnostic object carried by the service snapshot to the real Quali
Plan dialog; the exporter asserts pixel-identical rendering. The Qt Track
Correction view is captured in
[`qualifying_track_evolution_r13_q2_tminus3_quali_plan.png`](assets/qualifying_track_evolution_r13_q2_tminus3_quali_plan.png).

The equivalent causal R14 Q2 T−03:00 artifact is
[`qualifying_track_evolution_r14_q2_tminus3.png`](assets/qualifying_track_evolution_r14_q2_tminus3.png),
with full sample provenance in
[`qualifying_track_evolution_r14_q2_tminus3.json`](assets/qualifying_track_evolution_r14_q2_tminus3.json).
It uses archive timestamp **00:50:58.391**, session time **3058.391 s**,
sequence **13890**, effective Q2
remaining **180.0 s**: the latest processed state at or before the boundary.
Six supported SOFT driver fits each use one Q1 and one Q2 installation: HUL,
LAW, LIN, NOR, PIA and RUS. The result is **6 drivers / 12 laps used** and a
sample-weighted aggregate of **−0.03732 s/push** and **σ = 0.01286 s/push**,
versus the prior Q2-only
HAM + LEC fit of 2 drivers / 4 laps and −0.0401 s/push. HAM and LEC's second
Q2 PUSH laps were on the same SOFT installations as their first and are now
excluded as duplicate installation observations; their Q1 MEDIUM observations
cannot be blended into SOFT fits. The R14 JSON records all contributing lap
numbers, global indices, installation indices, physical gaps, coverage and
driver slopes. The real Qt Track Correction view is captured in
[`qualifying_track_evolution_r14_q2_tminus3_quali_plan.png`](assets/qualifying_track_evolution_r14_q2_tminus3_quali_plan.png).

The Track Correction top fields remain the existing projected-cut correction
estimate and manual override; that calculation is unchanged. The separate
Track Evolution line and plot show the session-wide, clean-air,
first-eligible-installation diagnostic and its weighted σ. The latter measures
driver-to-driver slope disagreement: one valid driver has σ = 0, while no fit
has an unavailable σ. No confidence label or threshold is inferred.

R14 blank numeric historical rows came from Position-only placeholders (for
example, `15`, `17`, `19`, `2`) entering the reducer's driver union despite
having no DriverList or TimingData identity. Those placeholders were then
ranked and eliminated by the service, and the table rendered the raw numeric
fallback. Timing rows now require DriverList or TimingData evidence. Known
identity and tyre metadata remain attached to eliminated service states across
Q1→Q2→Q3; timing-only unknown drivers retain the raw-ID fallback.
At both R14 part transitions the corrected snapshot has 22 rows and no blank
numeric identities. Q1 eliminations retain SAI/Williams, ALO/Aston Martin,
PER/Cadillac, BOT/Cadillac, BEA/Haas, and STR/Aston Martin. Q2 adds
HUL/Audi, BOR/Audi, OCO/Haas, GAS/Alpine, TSU/Racing Bulls, and ALB/Williams.
R13 likewise has 22 rows and no blank numeric identities at both transitions.
