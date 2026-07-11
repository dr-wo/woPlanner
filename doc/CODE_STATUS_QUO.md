# woPlanner Code Status Quo

Last reviewed: 2026-07-11

This document is a compact map of the current `woPlanner` package so future
coding agents can orient quickly without rereading every module. It describes
the repository as observed at the review date above.

## Package Purpose

`woPlanner` is an interactive Formula 1 race planner GUI and service layer. It
loads normalized race-lap data through `woStrategy`/`woData`, plots lap deltas
against a reference driver, shows pit-window guides, ranks drivers by start and
current/latest order, and supports an MVP pit-stop strategy simulation.

The simulation is intentionally simplified:

- no overtaking model
- no lapping model
- no traffic model
- future laps are predicted from `woStrategy` race-performance-review cache
  summaries
- the original real-data trajectory remains visible after the simulated stop,
  faded at 50% opacity

## Repository Layout

```text
woPlanner/
  README.md
  pyproject.toml
  doc/
    CODE_STATUS_QUO.md
  src/woplanner/
    __init__.py          # Public package exports
    gui.py               # PySide6/pyqtgraph desktop app and interaction logic
    model.py             # Session and scenario dataclasses
    normalize.py         # Raw lap normalization into planner schema
    service.py           # Race loading, plot data, ranks, scenarios, guides
    strategy_model.py    # Adapter from woStrategy cache summaries to predictions
  tests/
    test_service.py      # Focused service, adapter, and scenario tests
```

The package is configured in `pyproject.toml`:

- Build backend: `hatchling`
- Package source: `src/woplanner`
- Python requirement: `>=3.9`
- Runtime dependencies: `numpy`, `pandas`, `PySide6`, `pyqtgraph`, `wodata`,
  `wostrategy`
- CLI entry point: `woplanner = "woplanner.gui:main"`

## Dependency Direction

Current intended dependency direction:

```text
normalize/model -> strategy_model -> service -> gui
```

- `normalize` converts raw loader output into the stable planner lap schema.
- `model` contains simple dataclasses only.
- `strategy_model` reads `woStrategy` cache artifacts and exposes prediction
  rows. It should not import GUI code.
- `service` owns application state and dataframe composition. It should stay
  testable without Qt.
- `gui` owns PySide6 widgets, pyqtgraph rendering, mouse/keyboard interaction,
  and dialogs.

## Public API Surface

`woplanner.__init__` exports:

- `PlannerRaceSession`
- `PitStopScenario`
- `ScenarioTrajectory`
- `RacePlannerService`
- `WoStrategyRaceModelAdapter`
- `MissingStrategyModelError`

The CLI is `woplanner`, which opens the GUI from `woplanner.gui:main`.

## Data And Cache Discipline

`RacePlannerService.load_race` calls `wostrategy.load_race_laps_for_planner`
with the optional `data_root` passed into the service. `woData` remains the
owner of cache path conventions.

Strategy simulation model inputs come from:

```text
<workspace-root>/cache/race_performance_review/
```

Required files use this prefix:

```text
race_performance_{year}_{round}_{session}_*.csv
```

Required suffixes:

- `summary_baseline_pace`
- `summary_fuel_rate`
- `summary_track_rate`
- `summary_compound_delta`
- `summary_team_compound_degradation`
- `clean_laps`

Important discipline:

- Do not hard-code a fallback to `woStrategy/cache`.
- Users should run `woStrategy`'s `race_performance_review` first and place or
  generate the outputs under `dr-wo/cache/race_performance_review`.
- `WoStrategyRaceModelAdapter` normalizes those source summaries into a
  `woData` cache path under namespace `woplanner`, dataset `strategy_model`,
  schema `schema_v1`.
- The normalized cache is reused only when it is newer than all source summary
  files.

## Strategy Model Adapter

`src/woplanner/strategy_model.py`

`WoStrategyRaceModelAdapter` is the bridge between `woStrategy` Monte Carlo
summary artifacts and the planner simulation.

It reads:

- driver or team baseline pace medians
- scalar fuel-rate and track-rate medians
- compound delta medians
- team-compound degradation medians
- clean-lap references for lap, fuel proxy, and tyre age

If baseline pace is team-level, it expands team rows to drivers using
`clean_laps` driver/team pairs.

`predict_future_laps` produces a dataframe with:

- `driver`
- `team`
- `lap`
- `compound`
- `tyre_age`
- `predicted_clean_lap_time_s`

Prediction formula:

```text
baseline pace
+ compound delta
+ team-compound degradation * (tyre age - tyre age reference)
+ fuel rate * (fuel reference - lap)
+ track rate * (lap - reference lap)
```

The service then adds the configured pit-loss seconds to the first simulated
lap.

## Service Layer

`src/woplanner/service.py`

`RacePlannerService` owns loaded session state, active scenarios, and the last
plot dataframe. The GUI should ask the service for data instead of composing
planner dataframes itself.

Key methods:

- `load_race` / `load_latest_race`
- `build_plot_data`
- `build_pit_window_data`
- `build_tyre_event_data`
- `create_pit_stop_simulation`
- `clear_simulation`
- `clear_driver_simulation`
- `clear_all_simulations`
- `build_lap_delta_guide_data`

`ScenarioManager` stores active `PitStopScenario` objects and their
`ScenarioTrajectory` dataframes. It currently allows one active scenario per
driver; creating a new scenario for a driver clears that driver's previous
scenario.

`merge_scenario_plot_data` combines:

- actual reference-delta plot rows
- simulated branch rows
- a simulation anchor at `stop_after_lap`, so the calculated branch connects to
  the previous real lap
- a faded actual anchor at `stop_after_lap`, so the original real-data branch
  also remains connected after it fades

Plot roles and flags:

- `plot_role == "actual"`: real recorded laps
- `plot_role == "simulation"`: calculated strategy branch
- `is_simulated == True`: calculated future laps after the pit stop
- `is_simulated == False` with `plot_role == "simulation"`: visual anchor row
- `is_faded == True`: original actual branch after the simulated stop, drawn at
  50% opacity

Tyre event extraction deliberately ignores simulation anchor rows. For
simulations it emits only the first calculated lap for each scenario so every
future lap does not get a tyre label.

## GUI Layer

`src/woplanner/gui.py`

The GUI is a PySide6 main window with a pyqtgraph plot. It loads races,
switches reference driver, draws lap deltas, pit-window guides, tyre labels,
driver rank lists, and strategy simulations.

Simulation interaction:

- Hold Command on macOS or Ctrl on Windows/Linux while hovering the plot.
- The target pit lap is highlighted as “pit after lap N”.
- Click while holding the modifier to open the pit-stop simulation dialog.
- Choose compound, tyre age, and pit-loss seconds.
- Apply creates or replaces the active scenario for that driver.
- `Clear sims` removes all active scenarios.
- Clicking a simulated tyre label opens a context menu that can clear that
  scenario.

Modal dialog handling is intentionally deferred with `QTimer.singleShot(0, ...)`
after plot mouse events. This avoids crashes caused by opening a modal dialog
inside pyqtgraph/Shiboken mouse event handling.

Visual conventions:

- First driver in a team uses a solid line; second driver uses Qt's built-in
  dashed line.
- Simulated branches use custom dot-dash patterns:
  - solid base driver: `[1, 2, 8, 2]`
  - dashed base driver: `[1, 3, 4, 3]`
- Faded original branches use 50% alpha.
- Real tyre labels use solid rings.
- Simulated tyre labels use dotted rings.
- Faded tyre labels use 50% alpha for both ring and text.
- The Start rank column shows compact real line samples and, when a driver has
  an active simulation, a comma followed by the simulated line sample.

## Tests

`tests/test_service.py` covers:

- lap normalization
- reference-delta plot data
- pit-window guide data
- tyre event extraction
- driver ranks and tyre labels
- team colour and teammate line-style assignment
- strategy-model adapter cache reads and missing-cache errors
- scenario creation without mutating the base session
- active scenario replacement per driver
- connected simulation and faded-real anchors
- faded tyre marker state propagation

`pytest` is configured in `pyproject.toml`, but the current local venv may not
have `pytest` installed. The focused direct test-function smoke used during this
review imports `woPlanner/src` and `woData/src` on `sys.path` and calls the test
functions directly.

## Current Caveats

- The strategy simulation is not a race simulator; it only projects lap times
  from summary model coefficients.
- It assumes the selected driver/team/compound exists in the normalized model
  artifact.
- It currently supports one active scenario per driver.
- It does not persist scenarios across app restarts.
- GUI behavior is best verified manually because there is no Qt integration
  test harness yet.
