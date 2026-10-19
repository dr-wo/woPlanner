# woPlanner

[dr-wo project home](https://github.com/dr-wo/dr-wo) · [Analysis engine](https://github.com/dr-wo/woStrategy) · [Weekend reports](https://github.com/dr-wo/woWeekend)

`woPlanner` is an interactive Formula 1 decision-support application for
qualifying and race sessions. It combines public timing data with `woStrategy`
models and `woData` recordings to monitor qualifying, inspect completed races,
follow live timing, replay a session deterministically, update tyre assumptions,
search strategies, and compare race what-ifs.

The examples below are real **2026-R13 Italian Grand Prix** data rendered through
the application. They are historical analysis/replay views, not a mocked UI.

## Choose a workflow

| Application | Use it to |
| --- | --- |
| `woplanner` | Inspect completed races and compare strategy what-ifs. |
| `woplanner-live` | Follow timing or replay recordings with cumulative deltas and pit windows. |
| `woplanner-quali` | Inspect qualifying state, sectors, cutoff projections and run plans. |

Strategy Prediction connects the views to ranked compound sequences, pit timing,
tyre assumptions and supported team models. The Track Map adds spatial context;
its time-offset markers are approximate visual aids, not rejoin-order forecasts.

## Qualifying, race and model views

### Qualifying monitor

![2026-R13 Q2 qualifying monitor with projected cutoff, driver state, sectors and tyre history](doc/assets/qualifying_monitor_2026_r13_q2.png)

The Q2 replay combines the projected elimination cut with current sectors,
run/pit state, gaps and each driver's tyre history. Track evolution is refitted
as eligible push laps complete.

### Race live / replay cumulative delta

![2026-R13 Race replay cumulative delta with ranking panels, tyre markers and pit-loss windows](doc/assets/race_replay_cumulative_delta_2026_r13.png)

The same reduction and rendering path is used for live data and deterministic
replay. This completed-race view uses Russell as the selected actual-pace
reference and retains start/result rankings, compound markers, retirement
treatment, and pit-loss bands. This historical capture predates the current
separate GREEN, SC and VSC controls.

### Strategy Prediction update history

![2026-R13 Strategy Prediction tyre-model and sampler-quality history](doc/assets/strategy_prediction_update_history_2026_r13.png)

The model layer records how degradation and compound-delta assumptions evolved
at causal leader-lap cuts. Dashed/solid source styling distinguishes assumed,
derived and directly Race-informed values; ESS is shown as sampler-adequacy
diagnostic, not inference confidence.

On main, automatic Race traffic remains unavailable and retains the existing
tyre model. Live recalculation requires explicit sector-boundary mode and enough
eligible clean laps; see [live-mode operation](doc/USER_GUIDE.md#live-mode).

## What to inspect

- [`src/woplanner/quali`](src/woplanner/quali) — qualifying projection, run state
  and track-evolution monitoring.
- [`src/woplanner/live`](src/woplanner/live) — live/replay ingestion boundary,
  cumulative-delta view and telemetry worker.
- [`src/woplanner/analysis`](src/woplanner/analysis) — tyre inputs, live model
  updates and exact Strategy Prediction searches.
- [`src/woplanner/standard`](src/woplanner/standard) — completed-race analysis
  and strategy what-if editing.
- [`doc/`](doc) — calculation rules, architecture, deliberate compromises and
  validation notes.

All analysis uses public F1 data. Strategy outputs depend on model assumptions
and incomplete public timing context; they are engineering decision support,
not claims of team-grade ground truth.

## Installation and operating guides

Use Python 3.10+ in an active environment, with sibling checkouts. From woPlanner:

```bash
python -m pip install -e "../woData[live]" -e ../woStrategy -e .
woplanner
```

FastF1 version and available recording content determine supported acquisition
and replay behaviour. Live ingestion requires authentication; existing recordings
can be inspected offline. Detailed setup and controls are in the guides below.

- [User guide](doc/USER_GUIDE.md): [live/replay](doc/USER_GUIDE.md#live-mode),
  [Strategy Prediction](doc/USER_GUIDE.md#strategy-prediction-window),
  [strategy simulation](doc/USER_GUIDE.md#strategy-simulation) and
  [PNG/SVG export](doc/USER_GUIDE.md#exporting-plots).
- [Qualifying monitor](doc/QUALIFYING_MONITOR.md): operating rules and interpretation.
- [Track Map](doc/LIVE_TRACK_MAP.md): position evidence and mapping limits.
- [Completed R13 weekend](https://github.com/dr-wo/woWeekend/tree/main/examples/2026-R13):
  preparation and retrospective reports using the companion analysis tools.

Shared recording and replay contracts belong to [woData](https://github.com/dr-wo/woData).
The [main dr-wo homepage](https://github.com/dr-wo/dr-wo) connects all five components.
