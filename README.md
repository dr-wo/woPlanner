# woPlanner

Interactive race planner built on `woStrategy` data loaders and `woData` cache paths.

## Local development

For local development from the sibling checkout layout:

```bash
pip install -e ../woData
pip install -e ../woStrategy
pip install -e .
woplanner
```

Use **Latest** to load the latest completed race FastF1 can find and set the
reference driver to the race winner. The plot uses an inverted y-axis so slower
cars appear lower, with start order on the left and current/result order on the
right.

## Strategy simulation

`woPlanner` can create strategy what-if simulations for a loaded race. This is
still a projection, not a full race simulator: it does not model overtaking,
lapping, or traffic. The simulated future is kept in the observed race time
domain by applying modelled strategy changes on top of the observed race
scaffold.

Before using simulation, run `woStrategy`'s `race_performance_review` for the
same race/session and make sure the generated CSV summaries are available under:

```text
dr-wo/cache/race_performance_review/
```

The planner deliberately reads from that shared `dr-wo/cache` location. It does
not fall back to `woStrategy/cache`.

Required source files are named like:

```text
race_performance_{year}_{round}_{session}_summary_baseline_pace.csv
race_performance_{year}_{round}_{session}_summary_fuel_rate.csv
race_performance_{year}_{round}_{session}_summary_track_rate.csv
race_performance_{year}_{round}_{session}_summary_compound_delta.csv
race_performance_{year}_{round}_{session}_summary_team_compound_degradation.csv
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
active edit. Future real pits remain active unless explicitly removed.

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

**Pure sim - model lap times** is the default. Future laps use modelled what-if
lap time directly:

```text
sim_real_domain_lap_time_A
  = modelled_what_if_lap_time_A
```

**Real-race adjusted - add observed issues** keeps the old behaviour on all
future laps. It calculates the modelled what-if-vs-real-strategy tyre delta,
adds that through the observed race residual, then plots the resulting
cumulative time against the selected reference:

```text
race_domain_residual_A
  = observed_lap_time_A
    - modelled_observed_strategy_lap_time_A

sim_real_domain_lap_time_A
  = modelled_what_if_lap_time_A
    + race_domain_residual_A
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

### SC/VSC and pit loss

Track status is classified at lap level:

- Safety Car: light yellow plot band;
- Virtual Safety Car: light orange plot band.

The same status flag is used to choose normal versus SC/VSC pit-loss defaults
for actual pit-loss removal and for the initial value in the what-if pit dialog.

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
