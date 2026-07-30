# woPlanner

Interactive race planner built on `woStrategy` data loaders and `woData` cache paths.

Team and driver plotting colours come from `wodata.colours`, the shared palette
derived from FastF1's live-timing `DriverList`.

## Local development

For local development from the sibling checkout layout:

```bash
pip install -e ../Fast-F1
pip install -e "../woData[live]"
pip install -e ../woStrategy
pip install -e .
woplanner
```

## Live mode

Run `woplanner-live` for the live observation view. It uses FastF1's browser
authentication, subscribes only to the Phase 1 timing topics, and records every
canonical raw message before applying it. The replay picker accepts both merged
archive `*.jsonl` files and legacy live `*.txt` recordings for max-speed, 1x, 5x,
or 20x replay.

Live mode plots sector-level deltas when the feed provides
`TimingData.Lines[*].Sectors`. If a recording only contains completed-lap timing,
or if the app connects late and earlier sector timing is unavailable, it falls
back to completed-lap S3 points. Both live and post-race planners default to
averaged reference mode; the mode selector can switch back to actual reference
pace.

Hovering near a Live curve highlights the driver and shows driver, lap, sector,
cumulative delta, compound, and tyre age. Incremental `TimingAppData` is retained
by the shared reducer: compound changes are drawn as colour-coded tyre markers,
and the start/current rank lists include compact compound and tyre-age labels.

To replay a completed archive, click **Browse**, select
`merged_replay_v1.jsonl` from the matching
`wodata/live_archive/schema_v1/year=.../round=.../session=.../replay/` directory,
choose Max/1x/5x/20x, and click **Replay**. The same reducers and rendering path are
used for JSONL archive replay and live SignalR messages.

Live mode performs no strategy modelling, correction, prediction, or
interpolation. Missing internal lap timing invalidates later cumulative deltas for
that driver instead of assuming the missing lap took zero time.

Use **Latest** to load the latest completed race FastF1 can find and set the
reference driver to the race winner. The plot uses an inverted y-axis so slower
cars appear lower, with start order on the left and current/result order on the
right.

## Strategy simulation

`woPlanner` can create strategy what-if simulations for a loaded race. This is
still a projection, not a full race simulator: it does not model overtaking,
lapping, or traffic. By default, simulated futures are kept in the observed race
time domain by applying modelled strategy changes on top of the observed race
scaffold.

Before using simulation, run `woStrategy`'s `race_performance_review` for the
same race/session and make sure the generated CSV summaries are available under
the shared race-performance-review cache:

```text
cache/race_performance_review/
```

`woData` owns cache path conventions for normalized planner artifacts. With the
usual sibling-checkout workflow from the workspace root, generated planner caches
are written under `woData/`, not inside the `woPlanner` or `woStrategy` source
trees. `WODATA_ROOT` can override that location.

Required source files are named like:

```text
race_performance_{year}_{round}_{session}_summary_baseline_pace.csv
race_performance_{year}_{round}_{session}_summary_fuel_rate.csv
race_performance_{year}_{round}_{session}_summary_track_rate.csv
race_performance_{year}_{round}_{session}_summary_compound_delta.csv
race_performance_{year}_{round}_{session}_summary_team_compound_degradation.csv
race_performance_{year}_{round}_{session}_sample_parameters.csv
race_performance_{year}_{round}_{session}_pit_loss_summary.csv
race_performance_{year}_{round}_{session}_clean_laps.csv
```

### Interaction

1. Load a race.
2. Hold Command on macOS or Ctrl on Windows/Linux while hovering a driver line.
3. The plot highlights the target as “pit after lap N”.
4. Click while still holding the modifier.
5. Choose compound, starting tyre age, pit-loss seconds, and lap-time mode.
6. Apply the simulation.

Each driver has one active scenario containing ordered strategy edits. Adding a
new what-if pit to a driver that already has a scenario appends or replaces the
edit at that lap, then rebuilds the whole simulated future from the earliest
active edit. Future real pits remain active unless explicitly removed or
replaced by an added pit on the same stop lap.

Clicking tyre markers opens additional actions:

- simulated tyre marker: update the edit, inspect strategy deltas, or delete the
  edit;
- actual tyre marker: remove that real pit from the simulated strategy after
  confirming pit-loss estimate and lap-time mode;
- removed actual pit marker: edit the removed pit-loss estimate, restore the
  real pit, delete the edit, or inspect strategy deltas.

Use **Strategy deltas** to inspect the sector-level detail table and simulated
delta-to-reference chart. Same-driver model deltas remain in the table for
debugging, but the charted cumulative delta is relative to the selected
reference.

### Simulation algorithm

For a simulated driver A, two lap-time modes are available.

**Real-race adjusted - add observed issues** is the default. It calculates the
modelled what-if-vs-real-strategy tyre/strategy delta, preserves the observed
race residual for matching race conditions, then plots the resulting cumulative
time against the selected reference:

```text
race_domain_residual_A[sector]
  = observed_sector_time_A[sector]
    - modelled_observed_strategy_sector_time_A[sector]

sim_real_domain_lap_time_A
  = modelled_what_if_lap_time_A
    + race_domain_residual_A
```

For SC/VSC timing, residual carry-over is sector-aware. `woStrategy` exports
physical per-driver sector status, while `woPlanner` aligns the plotted status to
the race/leader lap-sector timeline. A driver's observed SC/VSC residual remains
on the physical sector where that driver experienced it, even when that sector
falls just outside the leader-aligned shaded SC/VSC period.

**Pure sim - model lap times** uses modelled what-if lap time directly and drops
observed race residuals:

```text
sim_real_domain_lap_time_A
  = modelled_what_if_lap_time_A
```

The simulated cumulative time connects to the observed cumulative time at the
last pre-edit lap. The plotted y-axis is always:

```text
display_cumulative_time(driver) - display_cumulative_time(selected_reference)
```

If the selected reference driver has an active simulation, all plotted deltas
are recalculated against that simulated reference trajectory.

The observed strategy baseline includes future real pit losses. When a real pit
is removed, its pit-loss estimate remains in the modelled observed-strategy
baseline, but is not added to the simulated no-pit strategy.

Adding a what-if pit on the same lap as an observed stop is treated as replacing
that observed stop: the old observed pit residual is removed from the pit-in and
pit-out sectors, and the simulated pit uses the designated pit-loss seconds.

### SC/VSC and pit loss

Track status is classified at sector level when the planner lap cache includes
`Sector1TrackStatus`, `Sector2TrackStatus`, and `Sector3TrackStatus`:

- Safety Car: light yellow plot band;
- Virtual Safety Car: light orange plot band.

The same status classification is used to choose normal versus SC/VSC pit-loss
defaults for actual pit-loss removal and for the initial value in the what-if
pit dialog. When `race_performance_review` has produced `pit_loss_summary`, the
planner uses the median woStrategy split for pit-in S3 loss and pit-out S1 loss
as the dialog default and split ratio. If the user enters a different pit-loss
total, the manual total is preserved and the woStrategy split is scaled to that
total. Manual pit-loss settings remain the fallback when no split summary is
available.

### Visual conventions

- Recorded driver lines stay visible.
- After a simulation is applied, that driver's original post-stop trajectory is
  faded to 50% transparency.
- The calculated trajectory connects from the last real lap before the earliest
  active edit.
- Simulated branches use custom dot-dash patterns:
  - solid base driver: `[1, 2, 8, 2]`
  - dashed base driver: `[1, 3, 4, 3]`
- Real tyre labels use solid rings.
- Simulated tyre labels use dotted rings.
- Removed real pit labels keep the solid ring and add a dashed cross overlay.
- Faded real tyre labels use 50% transparency.
- The Start column shows compact line samples for the real driver style and,
  when present, the simulated style.

Use **Clear sims** to remove all active simulations. A simulated tyre label can
also be clicked to update or delete that specific edit.

## Development notes

See [doc/CODE_STATUS_QUO.md](doc/CODE_STATUS_QUO.md) for the current package
map, cache discipline, strategy simulation data flow, and future coding-agent
orientation notes.
