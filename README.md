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

## Strategy simulation MVP

`woPlanner` can create a simplified pit-stop simulation for a loaded race. This
is an MVP projection, not a full race simulator: it does not model overtaking,
lapping, traffic, or safety-car timing beyond the user-provided pit-loss value.

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
5. Choose compound, starting tyre age, and pit-loss seconds.
6. Apply the simulation.

Each driver currently has at most one active simulation. Applying a new
simulation for the same driver replaces the previous one.

### Visual conventions

- Recorded driver lines stay visible.
- After a simulation is applied, that driver's original post-stop trajectory is
  faded to 50% transparency.
- The calculated trajectory connects from the last real lap before the stop.
- Simulated branches use custom dot-dash patterns:
  - solid base driver: `[1, 2, 8, 2]`
  - dashed base driver: `[1, 3, 4, 3]`
- Real tyre labels use solid rings.
- Simulated tyre labels use dotted rings.
- Faded real tyre labels use 50% transparency.
- The Start column shows compact line samples for the real driver style and,
  when present, the simulated style.

Use **Clear sims** to remove all active simulations. A simulated tyre label can
also be clicked to clear that specific scenario.

## Development notes

See [doc/CODE_STATUS_QUO.md](doc/CODE_STATUS_QUO.md) for the current package
map, cache discipline, strategy simulation data flow, and future coding-agent
orientation notes.
