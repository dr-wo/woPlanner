# Qualifying deterministic state foundation — 2026-09-12

> Historical foundation report. The operational-state contract and corrected
> lap-association evidence supersede its old queue labels and raw completion
> counts; see [R13 operational-state regression](QUALIFYING_OPERATIONAL_STATE_R13.md).

Implemented in woData and woPlanner; woStrategy has no changes. No evolution,
PUSH threshold/classifier, FP estimator, projected-cut equation or release model
was redesigned. Corrected input scoping can change their outputs.

## Implementation and ownership

| Correctness change | Actual implementation |
| --- | --- |
| Feed event round excludes session Number; one parser for UI and capture | [woData session_identity.py](../../woData/src/wodata/live/session_identity.py); [planner compatibility import](../src/woplanner/common/session_info.py) |
| Qualifying-only exclusive provisional capture, authoritative relocation, identity sidecar, mismatch log; no merging | [recorder.py](../../woData/src/wodata/live/recorder.py), [controller.py](../../woData/src/wodata/live/controller.py) |
| One segment-duration table, explicit feed part precedence, zero-to-reset segment lifecycle and zero-preserving message clock | [quali_clock.py](../../woData/src/wodata/live/quali_clock.py), [quali_reducer.py](../../woData/src/wodata/live/quali_reducer.py), [planning.py](../src/woplanner/quali/planning.py) |
| Current timing reset; separate historical results; official empty best stays empty; lap completions recorded from fresh timing updates | [raw models](../../woData/src/wodata/live/quali_models.py), [reducer](../../woData/src/wodata/live/quali_reducer.py), [planner models](../src/woplanner/quali/models.py) |
| Message-driven queue inference; stationary/stale evidence expiry; cached read-only snapshots | [quali_reducer.py](../../woData/src/wodata/live/quali_reducer.py) |
| Every intermediate message reaches the qualifying service; geometry loads on feed identity, not GUI polls | [controller.py](../../woData/src/wodata/live/controller.py), [service.py](../src/woplanner/quali/service.py) |
| Session-level normal finish; independent per-car effective finish, source hierarchy and captured timing provenance | [service.py](../src/woplanner/quali/service.py), [gui.py](../src/woplanner/quali/gui.py), [qualifying-only extra subscription](../../woData/src/wodata/live/fastf1_source.py) |
| Q1/Q2 cutoff and Q3 P1 targets; display-only locally extrapolated countdown | [service.py](../src/woplanner/quali/service.py), [gui.py](../src/woplanner/quali/gui.py) |
| Plan edits/FP analysis and effective finish share the service's operational timing values; artifacts load automatically on authoritative event identity | [widgets.py](../src/woplanner/quali/widgets.py), [service.py](../src/woplanner/quali/service.py) |

Each effective finish records the value, source and remaining time used at its
transition. COOL/PREP comes from a manual Plan edit, then an FP artifact, then
the current fastest lap × 1.30; without a current fastest lap it is unavailable.
Timing policy changes are explicit inputs; identical streams assume identical
policies and geometry artifacts.

Snapshots no longer extrapolate domain time using the local monotonic clock.
Message timestamps advance it, including Heartbeat/telemetry between clock
anchors. Complete feed silence leaves the last observed domain countdown in
place; no GUI timer can change finish or queue decisions.

## R13 evidence

The archive has 19,076 messages, SHA-256
`36e14cc0d3b46f360252fa5f834f2b0b9b6ea222656323217e42c1c0c629855d`.
Times below are archive offsets, not session countdowns.

| Evidence | Result |
| --- | --- |
| Segment sequence | Q1 at 0.000; Q2 at 2243.408; Q3 at 3564.407 seconds |
| Q3 duration | 780 seconds / 13:00 |
| ANT immediately at Q3 | Current best, last and sectors empty |
| ANT references | Q1 1:22.758 and Q2 1:21.882 retained separately |
| ANT before genuine Q3 result | Current best remains empty, including the untimed out lap |
| ANT first genuine Q3 result | 1:22.093 at 4049.759 seconds |
| Zero countdown | Preserved at Q1 1883.406, Q2 3203.399, Q3 4403.402; no old-anchor rebound |
| Explicit normal chequered | Reduced at Q1 1883.646, Q2 3203.642, Q3 4403.624 |
| Effective finish example | NOR after a completed PUSH in Q1: 9 seconds remaining < 107.3956-second `fastest × 1.30` COOL/PREP input; normal session finish still false |
| ALB queue baseline | Pre-operational-contract observation retained for audit only; it is not the current QUEUE policy. Moving outbound pit-lane transit is now `PIT-LANE`, while `QUEUE` requires a stopped outbound car. |

## Replay determinism

[Integration implementation](../tests/test_quali_replay_determinism.py) exercises
ReplayPlayer, the real controller drain loop, reducer and service. Waiting is
virtualized for speeds 1 and 25, so this checks their scheduling code without
waiting 77 real minutes. Additional empty service polls and snapshot reads are
included. Every canonical transition sequence is compared by exact equality;
[JSON evidence](assets/qualifying_correctness_r13.json) also stores per-sequence hashes.

| Drain size | Replay speed | Extra reads/polls | All canonical sequences |
| --- | --- | --- | --- |
| 1 | 0 (unlimited) | 0 | Match |
| 7 | 25× (virtual scheduler) | 3 | Match |
| 500 | 1× (virtual scheduler) | 7 | Match |

Compared historical transition counts: identity=1, segment=3,
countdown_finish=2765, laps=237, best=950, queue=340, effective=37,
planner_target=101. These are not current validated normal-lap counts; the
operational-state R13 replay emits 105 associated timed laps.

The unchanged downstream live evolution estimates also matched across all
configurations. This is a determinism observation, not validation of that model's
compound filtering, statistical support or forecasting accuracy.

## UI width and visual check

The built-in display reports 1728×1010 logical pixels usable, at 2× scale. With
native Mac fonts, the current 1500-pixel window has a 1474-pixel table viewport
and existing data columns of 98–99 pixels. The combined Q1/Q2 reference text
requires approximately 169 pixels, which would materially compress existing
columns. The historical column was deliberately omitted; model data remains
available separately. No unrelated table redesign was made.

[Native UI rendering](assets/qualifying_foundation_ui.png) uses **synthetic rows,
not race results**, to check the amber per-car effective marker and independent
SESSION CHEQUERED label. Horizontal scrollbar maximum is zero at 1500 pixels.

## Tests and results

- **176 passed in 36.02 seconds**: all woData tests plus affected qualifying,
  session-identity, live-service, R12-recovery and telemetry-worker tests.
- **R13 integration runner passed**, all three configurations and all canonical
  sequence assertions, including ANT before/at his first genuine Q3 result.
- Both repositories: `git diff --check` clean. woStrategy remains unchanged.

Added [woData foundation regressions](../../woData/tests/test_quali_foundation.py)
cover event/session numbering, authoritative relocation and provenance,
nonempty and empty destination collisions, incomplete identity, Q1/Q2/Q3,
13-minute Q3, zero, history/scoping, untimed out-lap fallback, explicit feed
precedence, red-flag freeze/restart, snapshot purity, stationary/stale queue
expiry and normal finish subscription/reduction. Existing clock tests now
assert message-time semantics rather than consumer wall-clock mutation.

Updated [service tests](../tests/test_quali_service.py),
[GUI tests](../tests/test_quali_gui.py), [widget tests](../tests/test_quali_widgets.py)
and [identity tests](../tests/test_session_info.py) cover effective-finish
conditions/independence/provenance and shared operational policy/UI semantics.

Commands (from woPlanner):

```sh
PYTHONPATH=src:../woData/src:../woStrategy/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/quali-mpl ../.venv/bin/python -m pytest -q -p no:cacheprovider ../woData/tests tests/test_quali_service.py tests/test_quali_planning.py tests/test_quali_widgets.py tests/test_quali_gui.py tests/test_session_info.py tests/test_live_service.py tests/test_r12_live_recovery.py tests/test_telemetry_worker.py
PYTHONPATH=src:../woData/src:../woStrategy/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/tmp/quali-mpl ../.venv/bin/python tests/test_quali_replay_determinism.py
```

## Review excerpts and diff

The [full source/test diff](assets/qualifying_correctness_review.diff) includes
new files. The following 56 source lines are exact implementation excerpts,
linked to the corresponding files; review them with the surrounding diff.

**Event round excludes session Number** — [session_identity.py:30](/Users/zhxutong/dr-wo/woData/src/wodata/live/session_identity.py:30)

```python
    round_number = _positive_int(
        session_info.get("RoundNumber")
        or meeting.get("RoundNumber")
        or meeting.get("Number")
    )
```

**Collision-safe relocation** — [recorder.py:164](/Users/zhxutong/dr-wo/woData/src/wodata/live/recorder.py:164)

```python
        while True:
            try:
                os.link(self.path, destination)
                break
            except FileExistsError:
                destination = destination.with_name(f"live_timing_{uuid4().hex}.jsonl")
        old_path = self.path
        self.path = destination
        old_path.unlink()
```

**Zero-valid clock parser** — [quali_clock.py:17](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_clock.py:17)

```python
        seconds = 0.0
        for part in str(value).split(":"):
            seconds = seconds * 60 + float(part)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
```

**Read-only snapshot** — [quali_reducer.py:338](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_reducer.py:338)

```python
    def snapshot(self) -> QualiRawSnapshot:
        """Read the last message state; polling never advances domain time."""
        return self._snapshot
```

**Historical capture and segment reset** — [quali_reducer.py:503](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_reducer.py:503)

```python
        for number, timing in self._timing.items():
            best = (_lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                    or timing.get("_segment_best_s") or timing.get("_best_seen_s"))
            self._history.setdefault(number, {})[self._session_part] = best
            # Preserve physical pit state and cumulative feed lap number only.
            retained = {key: value for key, value in timing.items() if key in {
                "InPit", "Stopped", "NumberOfLaps", "_pit_arriving", "_pit_queue",
                "_departure_unknown",
            }}
            retained["_last_recorded_lap"] = _as_int(timing.get("NumberOfLaps")) or 0
            timing.clear()
            timing.update(retained)
```

**Queue expiry on later evidence** — [quali_reducer.py:381](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_reducer.py:381)

```python
            if progress is None and not moving_in_pit and timing.get("_pit_queue"):
                # A brief movement is not indefinite evidence of a queue.
                timing.pop("_pit_queue", None)
                uncertain_departure = in_pit is False and not departed_by_position
                if uncertain_departure:
                    timing["_departure_unknown"] = True
```

**Normal session chequered reduction** — [quali_reducer.py:178](/Users/zhxutong/dr-wo/woData/src/wodata/live/quali_reducer.py:178)

```python
            messages = payload.get("Messages", {})
            values = messages.values() if isinstance(messages, Mapping) else messages
            if isinstance(values, (list, tuple)) or isinstance(messages, Mapping):
                for item in values:
                    if isinstance(item, Mapping) and item.get("Flag") == "CHEQUERED" and item.get("Scope", "Track") == "Track":
                        self._normal_finished = True
                        self._normal_finish_source = "race_control"
```

**Independent per-car effective finish** — [service.py:373](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/quali/service.py:373)

```python
        cool_requirement, cool_source = self._effective_cool_timing(current_fastest)
        if (not effective and completed_push and remaining is not None
                and cool_requirement is not None and remaining < cool_requirement):
            effective = True
            effective_remaining, recovery = remaining, cool_requirement
            timing_source = cool_source
        if remaining is not None and not finished:
            if completed_lap and (remaining <= 0 or self.raw_snapshot.normal_finished):
                finished, reason = True, FinishedReason.FINAL_LAP_COMPLETED
```

## git diff --stat

These are ordinary unstaged tracked-file stats; new files are listed separately.

```text
woData: git diff --stat
 src/wodata/live/controller.py    |  18 +++-
 src/wodata/live/fastf1_source.py |   5 +-
 src/wodata/live/quali_models.py  |  15 ++++
 src/wodata/live/quali_reducer.py | 176 ++++++++++++++++++++++++++++++++-------
 src/wodata/live/recorder.py      |  99 ++++++++++++++++++++++
 tests/test_live_controller.py    |   6 +-
 tests/test_quali_reducer.py      |  10 ++-
 7 files changed, 285 insertions(+), 44 deletions(-)

woPlanner: git diff --stat
 doc/QUALIFYING_MONITOR.md            |  71 +++++++++++++++--------
 src/woplanner/common/session_info.py |  78 ++-----------------------
 src/woplanner/quali/gui.py           |  49 ++++++----------
 src/woplanner/quali/models.py        |   7 +++
 src/woplanner/quali/planning.py      |  14 +----
 src/woplanner/quali/service.py       | 108 ++++++++++++++++++++++++++++-------
 src/woplanner/quali/widgets.py       |  43 +++++++++-----
 tests/test_quali_gui.py              |  20 ++++++-
 tests/test_quali_service.py          |  21 +++++++
 tests/test_quali_widgets.py          |  21 +++++++
 tests/test_session_info.py           |   5 ++
 11 files changed, 256 insertions(+), 181 deletions(-)
```

New source/test files:

- `woData/src/wodata/live/quali_clock.py` (22 lines)
- `woData/src/wodata/live/session_identity.py` (78 lines)
- `woData/tests/test_quali_foundation.py` (194 lines)
- `woPlanner/tests/test_quali_replay_determinism.py` (153 lines)

Additional new review artifacts: this report, replay JSON, source/test diff, and synthetic native UI PNG.

## Boundaries

- Captures with incomplete feed identity remain explicitly provisional. A later
  conflicting validated identity stops the capture with an error instead of
  silently mixing events; reconnect creates a separate capture.
- Effective finish still uses the existing completed-PUSH heuristic and an
  operational cooldown duration. This pass makes those decisions deterministic
  and sourced; it does not claim a guaranteed recovery model.
- No later planning/evolution features were implemented. Existing estimator and
  projection limitations remain for their separate tasks.
