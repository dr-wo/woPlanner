# Qualifying Monitor

Last reviewed: 2026-09-06

This document is the operating and maintenance guide for `woplanner-quali`.
The monitor combines FastF1 live timing, qualifying-only state reduction,
elimination/cutoff projection, FP-derived planning defaults, and a run timeline.
It supports conventional Qualifying and Sprint Qualifying without importing the
standard or live-race planner implementations.

## Run it

Install the sibling projects in dependency order and launch the entry point:

```bash
pip install -e ../Fast-F1
pip install -e "../woData[live]"
pip install -e ../woStrategy
pip install -e .
woplanner-quali
```

**Connect** starts authenticated FastF1 SignalR ingestion and records every raw
message under the shared `woData` root before reduction. **Replay** accepts a
legacy live `*.txt` recording or versioned replay input and sends it through the
same reducer/service path. The live source disables the old 60-second idle
timeout so connecting shortly before a session does not discard the feed during
a quiet period.

The following screenshot shows the recovered Round 12 Q2 replay used to verify
the live calculations and display states:

![Qualifying monitor showing the Round 12 Q2 replay](assets/qualifying_monitor_r12_q2.png)

`SessionInfo` updates the displayed year, round, event, and session type. A Race,
Practice, or other non-qualifying session is rejected, its timing table is
cleared, and the analysis/plan actions remain unavailable. Sprint Qualifying is
labelled SQ1/SQ2/SQ3 and uses its own segment durations.

## Timing table

The table shows position, driver/team, status, sectors, last/best lap, gaps, and
current/projected cutoff values. Team colours come from `wodata.colours`.

Status backgrounds are deliberately operational rather than copies of FastF1's
display strings:

| Status | Background | Meaning |
| --- | --- | --- |
| Inactive | default/white | No current fired-up or moving evidence |
| Fired-up | amber | Fresh non-zero RPM while still in the garage/pit state |
| Queue | red | Moving through pit lane but not yet confirmed past pit exit |
| On track / leave pit | green | Position or speed confirms the car left pit lane |
| Inlap | blue | The car is approaching/entering pit lane |
| In-pit | white | Position confirms the car is inside pit lane |

Queue ordering uses circuit pit-lane geometry when a cached centreline exists.
Without geometry, speed and incremental `InPit` transitions provide the fallback.
Telemetry has a freshness window, so stale RPM is not treated as a running car.

### Tyre age

Each circle represents one `TimingAppData` stint. Its colour is the compound and
the number is FastF1 `StartLaps`: the physical tyre set's completed-lap age when
that stint began. A new set therefore shows `0`; the number is not the driver's
qualifying lap number and is not the set's age at the end of the stint. Stints
without `StartLaps` are omitted. If no driver has genuine start-age data, the
entire **Tyre age** column is hidden instead of fabricating a value.

### Finished and chequered state

A driver's position cell receives a chequered background after either:

- a completed push leaves less than the configured cool/preparation-lap time for
  another push; or
- any on-track lap is completed after the clock reaches `0:00`, including a slow
  lap completed under the real chequered flag.

The marker is applied only after the lap completes, not while the driver remains
on the final lap. A driver already in the pit with insufficient out-lap time is
finished but does not receive the chequered position background. Advancing
drivers receive fresh state at the next Q/SQ segment, which removes the marker;
eliminated drivers are frozen with an explicit eliminated reason.

## Clock and session-part handling

FastF1's `ExtrapolatedClock` is an anchor, not a high-frequency countdown. The
qualifying reducer subtracts elapsed monotonic time locally and the controller
publishes refreshed snapshots even when no new feed message arrives. On a late
connection, the feed's UTC anchor and canonical segment duration recover the
current part where possible. A near-zero-to-full-duration reset advances the
part during an uninterrupted connection.

Default planning durations are:

| Session | Q1 | Q2 | Q3 |
| --- | ---: | ---: | ---: |
| Qualifying | 18 min | 15 min | 13 min |
| Sprint Qualifying | 12 min | 10 min | 8 min |

The Plan window formats latest-start offsets as minutes and seconds and supports
multiple arrangements of out, prep, push, cool, in, and turnaround segments.
**Analyse FP1-FP3** derives operational timing defaults and an initial track prior;
the timing inputs remain editable.

## Live track evolution and cutoff projection

A completed lap is treated as a push when it is within 2% of the fastest active
qualifying time known immediately before that lap. Push times are appended in
observed order. As soon as two pushes exist in the current Q/SQ part, every new
push refits the established linear `woStrategy` model; calculation no longer
waits for the segment transition. The source changes from **FP prior** to, for
example, **Q2 live estimate**, and the Plan window shows the effective live value
and evidence count.

The model reports positive seconds per field push as improvement. Projected lap
time therefore subtracts a positive improvement. The cutoff forecast currently
uses one future push for every active unfinished driver:

```text
projected correction = -track rate * unfinished driver count
projected cutoff     = current cutoff + projected correction
```

The Plan window's **Use override** value affects the effective projection but
does not stop or replace automatic fitting. Clearing the override restores the
newest live estimate. Reloading FP analysis cannot overwrite a qualifying fit
that already has support.

## Round 12 replay reference

The Dutch GP Round 12 recordings exposed two old lifecycle problems: the live
source could time out before Q1, and a recording directory could reflect stale
pre-connect controls even though embedded `SessionInfo` identified the correct
event. The substantial Round 12 capture was consequently stored below a
`round=1` directory. Always trust embedded session identity when diagnosing an
old recording.

Replaying the Q1/Q2 capture after the fixes produced:

| Evidence | Result |
| --- | ---: |
| Q1 push laps | 18 |
| Q2 push laps | 27 |
| Effective source before Q3 | Q2 live estimate |
| Q2 rate | -0.00416 s/field push |

Mercedes driver 63's two new SOFT stints both expose `StartLaps=0`, which is the
expected displayed starting age. The shorter file actually stored under the
Round 12 directory ends before Q1 with empty stint lists and cannot validate Q2
behavior.

## Deliberate compromises and limits

- Push classification is a live 2%-of-field-fastest heuristic. It does not use
  an official push/prep label and can admit an unusually quick non-push lap or
  reject a compromised push.
- The linear fit orders heterogeneous drivers' push times without driver,
  compound, traffic, wind, or fuel fixed effects. Two points are sufficient for
  responsiveness, so the earliest estimate can be noisy. It is operational
  guidance, not a causal track-only measurement.
- `unfinished driver count` is a one-more-push approximation, not a simulation of
  each driver's remaining stint plan. The projected cutoff can converge to the
  current P16/P15 value when no unfinished driver remains.
- Chequer state is inferred from clock/lap completion because the subscribed
  qualifying topic set does not require an explicit RaceControlMessages flag.
- Tyre history is only as complete as public `TimingAppData`. Missing
  `StartLaps` remains missing; no qualifying-lap fallback is allowed.
- Session-part recovery relies on standard duration/reset patterns. Red flags,
  shortened segments, or a late feed with an already-decremented anchor can
  reduce certainty until the next unambiguous reset.
- The recording path is selected before live `SessionInfo` arrives. The UI can
  correct its round display afterward, but it does not rename or move an open
  recording. Inspect embedded identity for older/misfiled captures.
- Disabling the idle timeout keeps pre-session connections alive but does not add
  automatic SignalR reconnection after a genuine network close; reconnect from
  the UI when the source reports disconnection.
- Pit queue/on-track distinction is strongest with cached pit-lane geometry.
  Speed-only fallback cannot locate the exact pit-exit line.

These choices favour transparent missing data, low live latency, and reuse of
the existing strategy model over silently invented tyre/session values or a
separate qualifying-only statistical stack.

## Validation

Focused tests cover standard and sprint session transitions, clock extrapolation,
late connection, non-qualifying rejection, status colours, true tyre starting
age, hidden missing-data columns, chequer reset, live track refitting, manual
override isolation, cutoff sign, planning durations, and NaN-safe formatting.

Run the relevant suites from the workspace root:

```bash
PYTHONPATH=woData/src .venv/bin/python -m pytest woData/tests -q
PYTHONPATH=woPlanner/src:woData/src:woStrategy/src \
  .venv/bin/python -m pytest woPlanner/tests --ignore=woPlanner/tests/test_service.py -q
```
