# woPlanner Code Status Quo

Last reviewed: 2026-08-22

The package is split into `standard`, `live`, `analysis`, `quali`, and `common`
ownership areas. Live transport, recording, replay, completed-lap state, and
sector timing state belong to `woData.live`; the live service does not load the
standard planner service. Both race GUIs default to averaged reference mode and
share interaction/data components through `woplanner.common`.

This document is a compact map of the current `woPlanner` package so future
coding agents can orient quickly without rereading every module. It describes
the repository as observed at the review date above.

For the current Strategy Prediction tyre resolver, explicit FP evidence flow,
scheduled-race-length rule, causal telemetry worker, bounded replay pipeline and
debug checklist, read [`FP_TYRE_LIVE_INTEGRATION.md`](FP_TYRE_LIVE_INTEGRATION.md).
For shared plot interaction ownership, coordinate contracts, live-sector/DNF
assumptions, and the current validation baseline, read
[`LIVE_PLOT_INTERACTIONS.md`](LIVE_PLOT_INTERACTIONS.md).

## Package Purpose

`woPlanner` is an interactive Formula 1 race planner GUI and service layer. It
loads normalized race-lap data through `woStrategy`/`woData`, plots cumulative
lap-time deltas against a selected reference driver, shows pit-window guides,
ranks drivers by start and current/latest order, highlights SC/VSC laps, and
supports what-if pit strategy simulation.

The simulation is still intentionally not a full race simulator:

- no overtaking model
- no lapping model
- no traffic model
- future strategy effects come from `woStrategy` race-performance-review cache
  summaries
- real-race adjusted mode keeps observed residuals only where they belong on the
  plotted lap-sector timeline
- the original real-data trajectory remains visible after the first simulated
  edit, faded and thinner, while the simulated trajectory overlays the main
  cumulative delta plot

## Repository Layout

```text
woPlanner/
  README.md
  pyproject.toml
  doc/
    CODE_STATUS_QUO.md
    LIVE_PLOT_INTERACTIONS.md
  src/woplanner/
    common/              # Shared plot widgets, interactions, policies, styles
    standard/            # Completed-session service, models, simulation, GUI
    live/                # Live/replay service, telemetry worker, GUI
    analysis/            # Shared strategy analysis and tyre-input UI
    quali/               # Qualifying planner and monitor
  tests/
    test_service.py      # Focused service, adapter, and scenario tests
```

The package is configured in `pyproject.toml`:

- Build backend: `hatchling`
- Package source: `src/woplanner`
- Python requirement: `>=3.9`
- Runtime dependencies: `numpy`, `pandas`, `PySide6`, `pyqtgraph`, `wodata`,
  `wostrategy`
- CLI entry points: `woplanner`, `woplanner-live`, and `woplanner-quali`

## Dependency Direction

Current intended dependency direction:

```text
common <- standard
common <- live
analysis <- standard GUI / live GUI
woData.live -> live service -> live GUI
```

- `standard.normalize` converts raw loader output into the stable planner lap schema. It
  prefers explicit lap time, then sector-summed lap time, then positive elapsed
  `Time - LapStartTime`; cumulative time uses absolute lap-end time relative to
  the earliest race start when available, falling back to per-driver lap-time
  cumsum. For consecutive laps after a FastF1 track-status `5` red flag, it
  derives the inactive interval from the leading classified car and removes
  that same race-wide offset from every driver. This preserves the raw restart
  order while excluding suspension time from the plotted coordinate.
- `standard.models` contains simple dataclasses only.
- `standard.strategy_model` reads `woStrategy` cache artifacts and exposes prediction
  rows. It should not import GUI code.
- Mode services own application state and dataframe composition. They should stay
  testable without Qt.
- Mode GUIs own their distinct source lifecycle and dialogs. Shared Qt controls,
  overlays, and pure plot transforms belong in `common`.
- `standard` and `live` must not import one another.

## Public API Surface

`woplanner.__init__` exports:

- `PlannerRaceSession`
- `StrategyEdit`
- `DriverStrategyScenario`
- `PitStopScenario` (compatibility alias)
- `ScenarioTrajectory`
- `RacePlannerService`
- `WoStrategyRaceModelAdapter`
- `MissingStrategyModelError`

The race CLIs open `woplanner.standard.gui:main` and `woplanner.live.gui:main`.

## Data And Cache Discipline

`RacePlannerService.load_race` calls `wostrategy.load_race_laps_for_planner`
with the optional `data_root` passed into the service. `woData` remains the
owner of cache path conventions.

Strategy simulation model inputs come from:

```text
cache/race_performance_review/
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
- `sample_parameters`
- `pit_loss_summary`
- `clean_laps`

Important discipline:

- Do not hard-code a fallback to `woStrategy/cache`.
- Users should run `woStrategy`'s `race_performance_review` first and place or
  generate the outputs under `dr-wo/cache/race_performance_review`.
- `WoStrategyRaceModelAdapter` normalizes those source summaries into a
  `woData` cache path under namespace `woplanner`, dataset `strategy_model`,
  schema `schema_v3`.
- `wostrategy.load_race_laps_for_planner` writes planner lap caches through
  `woData.get_cache_path` under namespace `wostrategy`, dataset
  `planner_race_laps`, schema `schema_v2`.
- In the usual sibling-checkout workflow, run tools from the workspace root so
  the default `woData` root resolves to `<workspace-root>/woData`. Passing
  `data_root` explicitly redirects generated caches to that path.
- The normalized cache is reused only when it is newer than all source summary
  files.

## Strategy Model Adapter

`src/woplanner/standard/strategy_model.py`

`WoStrategyRaceModelAdapter` is the bridge between `woStrategy` Monte Carlo
summary artifacts and the planner simulation.

It reads:

- driver or team baseline pace medians
- scalar fuel-rate and track-rate medians
- compound delta medians
- team-compound degradation medians
- sample parameter references for fuel, race lap, and tyre age
- clean-lap fuel proxy values
- pit-loss split summaries

If baseline pace is team-level, it expands team rows to drivers using
`clean_laps` driver/team pairs.

`predict_future_laps` produces a dataframe with:

- `driver`
- `team`
- `lap`
- `compound`
- `tyre_age`
- `predicted_clean_lap_time_s`

`predict_strategy_plan` accepts ordered strategy segments and pit-loss rows, and
returns the same modelled lap-time shape for a full reconstructed strategy.

Prediction formula:

```text
baseline pace
+ compound delta
+ team-compound degradation * (tyre age - tyre age reference)
+ fuel rate * (fuel proxy remaining - fuel reference)
+ track rate * (lap - reference lap)
```

The service applies pit loss at sector level: pit-in loss on the previous lap's
S3 and pit-out loss on the first new-stint lap's S1. If `pit_loss_summary` is
available, the default split comes from woStrategy median S3/S1 pit-loss
estimates; otherwise the manual total is split evenly. For manually entered
what-if pit losses, the entered total is preserved even when `pit_loss_summary`
exists; the woStrategy split is scaled to that total.

## Service Layer

`src/woplanner/standard/service.py`

`RacePlannerService` owns loaded session state, active scenarios, and the last
plot dataframe. The GUI should ask the service for data instead of composing
planner dataframes itself.

Key methods:

- `load_race` / `load_latest_race`
- `build_plot_data`
- `build_pit_window_data`
- `build_lap_delta_guide_data`
- `build_tyre_event_data`
- `build_track_status_event_data`
- `build_strategy_delta_data`
- `create_pit_stop_simulation`
- `create_remove_observed_pit_simulation`
- `clear_simulation`
- `clear_driver_simulation`
- `clear_all_simulations`

`ScenarioManager` stores one active `DriverStrategyScenario` per driver. A
scenario contains an ordered list of `StrategyEdit` objects and one
`ScenarioTrajectory` dataframe. Supported edit types are:

- `add_pit`: add or replace a what-if pit after `stop_after_lap`
- `remove_observed_pit`: remove the real pit before marker lap `L`, represented
  as an edit after lap `L - 1`

Only one pit action is allowed at the same `stop_after_lap` for the same driver.
Creating another action for the same lap updates or replaces the existing edit.
If all edits are removed, the scenario is deleted and the driver returns to the
observed trajectory.

After any edit, the service rebuilds the full simulated strategy from the
earliest active edit:

```text
observed race
+ ordered strategy edits
+ modelled strategy delta
+ selected projection mode
```

The plotted line is never patched incrementally.

### Simulation Display Algorithm

The main plot y-axis remains cumulative time delta to the selected reference
driver. The displayed simulated trajectory is controlled by each scenario's
lap-time mode.

Real-race adjusted - add observed issues is the default. For each future segment
after the first active edit, it preserves observed race-domain residuals on
matching plotted race-event sectors:

```text
race_domain_residual_A[j]
    = observed_sector_time_A[j]
      - modelled_observed_strategy_sector_time_A[j]

sim_display_sector_time_A[j]
    = modelled_what_if_sector_time_A[j]
      + race_domain_residual_A[j]
```

Pure sim - model lap times uses the modelled what-if sector time directly and
drops observed residuals:

```text
sim_display_sector_time_A[j]
    = modelled_what_if_sector_time_A[j]
```

Then cumulative time is rebuilt from the observed cumulative time at the edit
anchor. For a driver with no simulation, display cumulative time is just
observed cumulative time. For a simulated driver, display cumulative time uses
observed time before and including the anchor lap, then the rebuilt simulated
future. Standard normalization and live plot preparation both collapse inactive
suspension gaps before this calculation. Consequently observed and simulated
trajectories use one racing-time coordinate, including when a pit edit is
applied after a red-flag restart.

Finally, every plotted row is recalculated against the selected reference:

```text
delta_to_reference_s
    = display_cumulative_time_s(driver)
      - display_cumulative_time_s(reference_driver)
```

If the selected reference driver has an active simulation, all driver deltas are
recalculated against the simulated reference trajectory.

In pure simulation, ordinary observed driver errors or damage do not bleed into
the simulated line. In real-race adjusted mode, observed race-domain residuals
are preserved, including physical SC/VSC-affected sectors that fall outside the
leader-aligned shaded race status. Actual future pit stops do not leak into a
no-pit simulation because their modelled pit-loss effect is included in
the observed-strategy baseline and removed by the model delta; residual beyond
the configured normal or SC/VSC loss remains in the adjusted display. If a
what-if pit is added on the same stop lap as an observed pit, that observed stop
is treated as replaced: residual carry-over for the old observed pit-in and
pit-out sectors is suppressed and the simulated pit uses the edit's designated
pit loss.
An explicit remove-observed-pit edit is different: its configured loss is split
between the physical pit-in lap/S3 and pit-out lap/S1, and residual beyond that
configured loss remains part of the real-race-adjusted display.

The Strategy deltas dialog plots simulated cumulative time minus the selected
reference cumulative time. Same-driver model delta is retained in separate
columns only as diagnostic detail.

### Pit Loss And Race Events

`normalize` preserves lap-level `track_status` and sector-level
`track_status_s1`, `track_status_s2`, and `track_status_s3` where available.
Planner lap cache schema `schema_v2` provides sector status as
`Sector1TrackStatus`, `Sector2TrackStatus`, and `Sector3TrackStatus`.

The raw sector status describes the driver's physical sector interval. woPlanner
also aligns displayed sector status to the leader/race lap-sector timeline, so
the plot and the real-race residual gate use the same x-axis definition.

The service classifies status values as normal, VSC, or SC. Track-status events
are exposed to the GUI for sector-width background bands:

- SC: light yellow
- VSC: light orange

Pit-loss source priority:

1. woStrategy `pit_loss_summary` median split for the applicable status
2. known or estimated real pit loss if available
3. SC/VSC pit-loss default when track status applies
4. normal pit-loss default

For add-pit edits, the woStrategy split summary supplies the initial dialog
value and split ratio only. The applied edit stores the user's requested total.

If track status is unavailable where it is needed, the normal default is used
and the simulation records a warning.

For `remove_observed_pit`, the removed pit-loss estimate is included in the
modelled observed-strategy baseline. It is not added to the simulated no-pit
strategy. The simulated strategy continues the previous compound and tyre age
from the removed pit lap unless a later edit changes it.

For `add_pit` on an observed pit's stop lap, the scenario still contains a
single add edit at that `stop_after_lap`. The comparison layer handles the
implicit replacement by marking baseline pit sectors as skipped whenever their
pit-loss components differ from the simulated pit components.

### Plot Rows And Markers

`merge_scenario_plot_data` combines:

- actual reference-delta plot rows
- simulated overlay rows for the rebuilt future trajectory
- a simulation anchor at the first edited lap
- a faded actual anchor at the same lap, so the original real-data branch stays
  visually connected after it fades

Plot roles and flags:

- `plot_role == "actual"`: real recorded laps
- `plot_role == "simulation"`: calculated strategy branch
- `is_simulated == True`: calculated future laps after the first edit
- `is_simulated == False` with `plot_role == "simulation"`: visual anchor row
- `is_faded == True`: original actual branch after the first edit, drawn thin
  and faded

Tyre event extraction deliberately ignores simulation anchor rows. It emits:

- solid-ring markers for actual tyre changes
- dashed-ring markers for simulated tyre changes
- solid actual markers with a dashed cross overlay when a real pit was removed

## GUI Layer

`src/woplanner/standard/gui.py`

The GUI is a PySide6 main window with a pyqtgraph plot. It loads races,
switches reference driver, draws cumulative deltas, pit-window guides, tyre
labels, driver rank lists, SC/VSC background bands, and strategy simulations.

Simulation interaction:

- Hold Command on macOS or Ctrl on Windows/Linux while hovering the plot.
- The target pit lap is highlighted as "pit after lap N".
- Click while holding the modifier to open the pit-stop simulation dialog.
- Choose compound, tyre age, and pit-loss seconds.
- Choose lap-time mode; new scenarios default to real-race adjusted.
- The reference mode selector starts on actual pace.
- Applying on an already simulated driver adds or replaces an edit inside that
  driver's active scenario and rebuilds the full simulated future.
- Right-click an actual solid tyre marker to remove that real pit from the
  strategy.
- Right-click a removed-pit marker to edit the removed pit-loss estimate,
  restore the real pit, or delete the edit.
- Right-click a simulated tyre marker to edit or delete that simulation edit.
- `Strategy deltas` opens the detail table for inspection; it is not the main
  simulation display.
- `Clear sims` removes all active scenarios.

Modal dialog handling is intentionally deferred with `QTimer.singleShot(0, ...)`
after plot mouse events. This avoids crashes caused by opening a modal dialog
inside pyqtgraph/Shiboken mouse event handling.

Visual conventions:

- First driver in a team uses a solid line; second driver uses Qt's built-in
  dashed line.
- Simulated branches use custom dot-dash patterns:
  - solid base driver: `[1, 2, 8, 2]`
  - dashed base driver: `[1, 3, 4, 3]`
- Faded original branches use lower alpha and thinner pens.
- Real tyre labels use solid rings.
- Simulated tyre labels use dashed rings.
- Removed real pits keep the solid tyre marker and draw a dashed cross overlay.
- Faded tyre labels use lower alpha for both ring and text.
- The Start rank column shows compact real line samples and, when a driver has
  an active simulation, a comma followed by the simulated line sample.
- Exported PNGs and clipboard images hide transient hover/pit-target overlays
  during the grab, then restore the on-screen plot state.
- The Export menu also offers UHD 4K (3840 x 2160) file and clipboard output.
  Its 16:9 framing only expands plot ranges, equally on both sides; it never
  crops an existing visible range.
- SVG file export renders the graph scene as vector paths, including temporary
  vector replacements for pixel-cached tyre symbols. Axes, labels, curves,
  markers, and status bands are included; ranking panels are not.

## Tests

`tests/test_service.py` covers:

- lap normalization, including lap and sector `track_status`
- reference-delta plot data and simulated reference recalculation
- pit-window guide data
- SC/VSC event extraction
- tyre event extraction, simulated markers, and removed-pit markers
- driver ranks and tyre labels
- team colour and teammate line-style assignment
- strategy-model adapter cache reads and missing-cache errors
- strategy plan prediction across multiple segments
- scenario creation without mutating the base session
- multi-edit scenario rebuilds
- observed race scaffold preservation on SC/VSC laps
- sector-level SC/VSC residual suppression against the plotted race status
- model-delta and residual-based simulated display construction
- future real pit removal from the displayed simulation
- same-lap add-pit replacement of an observed pit loss
- manual add-pit loss preservation when woStrategy split defaults exist
- dedicated `remove_observed_pit` edits and restore behavior
- connected simulation and faded-real anchors
- faded tyre marker state propagation

`pytest` is configured in `pyproject.toml`, but the current local venv may not
have `pytest` installed. The focused direct test-function smoke used during
this review imports `woPlanner/src` on `sys.path` and calls the test functions
directly.

## Current Caveats

- The strategy simulation is not a race simulator; it only projects strategy
  effects from summary model coefficients and observed race residuals.
- It assumes the selected driver/team/compound exists in the normalized model
  artifact.
- It does not persist scenarios across app restarts.
- GUI behavior is best verified manually because there is no Qt integration
  test harness yet.
