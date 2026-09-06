# woPlanner

Interactive race planner built on `woStrategy` data loaders and `woData` cache paths.

Team and driver plotting colours come from `wodata.colours`, the shared palette
derived from FastF1's live-timing `DriverList`.

## Package layout

The source package is organized by ownership:

```text
woplanner/
├── common/    # presentation and policies reused by planner modes
├── analysis/  # model analysis, workers, tyre inputs, prediction UI
├── standard/  # completed-session race planner
├── live/      # live race planner and telemetry pipeline
└── quali/     # qualifying planner and live monitor
```

Planner-specific code should remain in its planner package. A component moves to
`common` only when multiple planner modes reuse the same behavior; analysis
functionality belongs in `analysis` even when standard and live GUIs both open it.

## Qualifying monitor

Run `woplanner-quali` for the live Qualifying/Sprint Qualifying timing table and
run-plan editor. It supports FastF1 live timing and deterministic raw-recording
replay, automatically follows Q1/Q2/Q3 or SQ1/SQ2/SQ3, projects the elimination
cutoff, shows pit/track state and real tyre age at stint start, and recalculates
linear track evolution as qualifying push laps finish.

The complete operating guide, state definitions, calculation rules, replay
example, and deliberate compromises are documented in
[`doc/QUALIFYING_MONITOR.md`](doc/QUALIFYING_MONITOR.md).

## Local development

For local development from the sibling checkout layout:

```bash
pip install -e ../Fast-F1
pip install -e "../woData[live]"
pip install -e ../woStrategy
pip install -e .
woplanner
```

## Exporting plots

The completed-session planner's **Export** menu supports:

- PNG at the current on-screen size, saved to a file or copied to the clipboard.
- UHD 4K PNG at 3840 × 2160, saved to a file or copied to the clipboard.
- SVG vector plot output for scalable editing and publication.

The 4K export preserves the entire visible plot. When the 16:9 output requires
more plotting range, woPlanner expands the narrower axis equally on both sides;
it never crops or contracts the selected range. The original window size and
plot range are restored after export.

SVG export contains the graph, axes, labels, curves, tyre markers, and track-
status bands as vector content without embedded raster images. It intentionally
excludes the Start and Current/Result ranking panels. All export formats hide
temporary hover and pit-target overlays from the exported image and then restore
them on screen.

## Live mode

Run `woplanner-live` for the live observation view. It uses FastF1's browser
authentication, subscribes only to the Phase 1 timing topics, and records every
canonical raw message before applying it. The replay picker accepts both merged
archive `*.jsonl` files and legacy live `*.txt` recordings for max-speed, 1x, 5x,
or 20x replay.

Live mode plots sector-level deltas as soon as S1, S2, or S3 arrives in
`TimingData.Lines[*].Sectors`; it does not wait for the whole lap to complete. If
a recording only contains completed-lap timing, or if the app connects late and
earlier sector timing is unavailable, it falls back to completed-lap S3 points.
After the feed explicitly marks a running driver `Retired`, that driver's curve
ends at the last completed lap instead of accumulating later placeholder timing.
Both live and post-race planners default to averaged reference mode; the mode
selector can switch back to actual reference pace.

Hovering near a Live curve highlights the driver and shows driver, lap, sector,
cumulative delta, compound, and tyre age. Incremental `TimingAppData` is retained
by the shared reducer: compound changes are drawn as colour-coded tyre markers,
and the start/current rank lists include compact compound and tyre-age labels.
Tyre markers are reconstructed from the current authoritative lap history after
every Cumulative Delta redraw. Consequently, a known stint change remains on the
plot as later laps arrive, after pan/zoom or Auto range, and after live-session
recovery; a corrected compound assignment replaces the corresponding marker.
Live and standard mode share the same detailed hover matching. Clicking a live
curve selects and emphasises that driver without changing the top-bar reference.
Both modes use the common curve-click binding, hover-label overlay, pit-window
overlay, and Lap/Y range-control component. Selecting a driver therefore draws
the same grey normal and SC/VSC pit-loss windows in either mode; the **Pit** and
**SC/VSC** inputs control their widths. Applied ranges remain fixed across timing
updates and **Auto range** resumes data-driven bounds.

Live Cumulative Delta uses one fixed intra-lap geometry for every driver and
lap. The current live snapshot has no circuit sector-distance boundaries, so it
uses the selected reference driver's earliest complete lap to derive cumulative
S1/S2/S3 time fractions; equal thirds are the final fallback. The averaged
reference uses the selected driver's positive observed mean time for each sector
and repeats those same three means after that driver retires. It does not switch
to a surviving-driver population.

To replay a completed archive, click **Browse**, select
`merged_replay_v1.jsonl` from the matching
`wodata/live_archive/schema_v1/year=.../round=.../session=.../replay/` directory,
choose Max/1x/5x/20x, and click **Replay**. The same reducers and rendering path are
used for JSONL archive replay and live SignalR messages.

The live timing plot does not correct measured lap or sector durations. Before
calculating cumulative deltas, it does remove inactive suspension gaps that may
be present in source cumulative timing, so a red-flag delay is not plotted as
race time. The shared **Strategy Prediction** window can consume the same
canonical live snapshot: when the leader lap advances it checks for newly
eligible sector-filtered clean laps. Replay expands batched leader-lap jumps
into point-in-time snapshots and processes them through a FIFO recalculation
queue. Replay-state reduction and Cumulative Delta rendering do not wait for
that FIFO: the UI reports the number of historical cuts still awaiting input
preparation while the race view continues to advance. Prepared inputs and the
process queue are bounded, but lightweight causal cut requests are retained
until every historical result has been evaluated. Live mode does not queue an
update received while calculation is active; the newest accumulated data is picked
up by the next leader-lap trigger. Unchanged clean-lap fingerprints are skipped
quickly. Updated models record their version and refresh sequence-level strategy
predictions after replay's queued model updates drain. Missing internal lap
timing still invalidates later cumulative deltas
for that driver instead of assuming the missing lap took zero time.

Use **Latest** to load the latest completed race FastF1 can find and set the
reference driver to the race winner. The plot uses an inverted y-axis so slower
cars appear lower, with start order on the left and current/result order on the
right.

## Strategy Prediction window

Both `woplanner` and `woplanner-live` use the same modeless **Strategy Prediction**
window. It reads
canonical schema-v1 weekend models, displays uncertainty/support diagnostics, runs
up-to-10 compound-sequence searches in a worker thread, optimizes pit timing once
per sequence, renders proportional stint bars, persists results, shows parameter
history plots, supports editable degradation/delta overrides, and allows up to
three pinned comparisons. In live mode, selecting a driver fills the current lap,
compound, and tyre age from the feed.

The shared **Tyre Inputs** tab and the live/replay worker architecture are
documented in
[`doc/FP_TYRE_LIVE_INTEGRATION.md`](doc/FP_TYRE_LIVE_INTEGRATION.md). In
particular, FP analysis is explicit-only: opening either application, this
window, the tab, history, or replay never calculates or downloads FP data.

The result-count control chooses how many distinct compound sequences the exact
strategy search should return. This search does not use Monte Carlo samples. The
separate sample-count control on **Live recalculation** is initialized from the
latest compatible resolved race-review settings when available, otherwise from
the pre-race model configuration, and remains editable. The value shown in the UI
is passed explicitly to recalculation and therefore wins over a stored value.

**Rules compliant** is an opt-in filter for dry strategy searches. When checked,
every returned plan must use at least two distinct compounds from SOFT, MEDIUM,
and HARD across the complete strategy, including the current stint during an
in-race continuation. It can therefore return fewer rows than the requested
result count, or no result when the remaining laps/stops cannot satisfy the
constraint. Unchecked retains the unrestricted historical search behavior.

The label is intentionally a narrow convenience rather than a complete sporting-
regulations engine. It implements only the two-distinct-dry-compound constraint;
it does not infer wet-race exemptions, tyre-set availability, event-specific
mandates, race-control declarations, or other legality conditions. This keeps the
deterministic optimizer independent of incomplete live sporting context, at the
cost of requiring the user to decide when the checkbox is applicable.

Resolved fitting settings use this precedence: successful event cache metadata,
event-specific `latest_resolved_settings.json`, global
`latest_resolved_settings.json`, then the pre-race model configuration. The first
choice reproduces the settings behind an existing event result; the invocation
artifacts allow a race-review command to communicate its intended settings even
when no Monte Carlo result was produced. Only fields with a direct joint-model
meaning are overlaid. Race-review-only controls are intentionally ignored because
the live joint model does not implement the same parameterization.

Strategy Prediction remembers its controls, selected tab, and live settings in
`strategy_prediction_state.json`. Event-specific manual tyre inputs are instead
shared through `manual_tyre_inputs_v1.json` under the event weekend-model root.
Replay state is stored
beside the selected replay file; live state is stored beside that connection's
raw live-timing recording. The offline planner falls back to the event's canonical
weekend-model directory.

Replay uses a fresh in-memory value-versus-lap plot every time Replay is pressed.
It restores settings but does not retain generated strategies or the working plot
unless **Save snapshot long-term** is pressed. Live mode automatically updates one
durable whole-plot snapshot. The Update history selector reads only these saved
whole plots; it starts at **Select saved plot**, leaving the table and chart empty.
Legacy per-analysis history CSVs are no longer used by this selector.

Update history displays tyre degradation and compound delta in separate labelled
charts. Hovering either chart snaps a synchronized vertical cursor to the nearest
recorded leader lap and shows every compound's degradation and relative pace.
Hovering near a compound curve temporarily uses that compound as the `0.000 s`
pace reference; elsewhere the configured default reference compound is used.
Hovering never changes the stored model values. Left-clicking opens **Use lap N tyre values for strategy
calculation**, which copies that lap's degradation and compound-delta values into
the editable **Used** inputs and opens the Strategy predictions tab.

Degradation keeps the session's initial HARD/MEDIUM/SOFT ratios fixed. Before
any compound is directly informed it uses its resolved base value. Once a live
MC result directly informs a compound, uninformed compounds are derived from
that live scale and the fixed initial ratios; directly informed MEDIUM is the
preferred scale anchor. History renders base as dashed, live-derived as
dashed with hollow markers, and live-direct as solid with filled markers. This
resolver supplies Strategy Prediction as well as the chart. Compound delta is
not ratio-derived and retains its independent base-to-direct behaviour.
Each source segment owns the connector into the first point of the next segment,
so the obsolete baseline is not drawn beneath later history. Coordinates that
never become Race-informed remain dashed through the latest recorded lap, and a
missing compound delta remains absent rather than becoming zero.

The Live recalculation page's **Clean-lap details** dialog exposes required
tyre/stint metadata, allowed compounds, driver-relative quick-lap threshold,
consecutive-run minimum, run-start/end exclusions, and configurable clear-ahead
and clear-behind gaps. **Traffic source** defaults to **Automatic telemetry with
sector fallback**. Live and archive `CarData.z` samples are normalized and passed
to the same continuous direct and circular-physical traffic evaluator used by
retrospective analysis. `Position.z` can be archived but is not required by this
calculation. Completed-lap direct, physical, and selected values remain separate;
the selected telemetry value currently uses the versioned conservative
`compatibility_min` rule.

If telemetry is unavailable or below its coverage requirements, the calculation
automatically uses lap-start/S1/S2/lap-end crossing gaps and marks the result as
`SECTOR_BOUNDARY_FALLBACK`; sector gaps are never blended into telemetry means.
**Force sector-boundary only** is a diagnostic override, not the normal fallback
switch. Each processed leader-lap update logs previous and new clean-lap counts
and the FP/R source breakdown, total wall-clock calculation time, and remaining
queue length. The elapsed time can be compared directly with a normal lap time
when tuning the live Monte Carlo sample count. That sample count affects fitting,
not the number of telemetry traffic comparisons.

The **Tyre Inputs** tab separates editable Strategy inputs from read-only FP
LongRun evidence. Each Selected value is Manual when explicitly supplied and
otherwise the exact Legacy FP-derived fallback. FP contrasts are advisory and do
not automatically update Strategy values.

The **Strategy predictions** tab shows the exact tyre inputs used by the search.
The Tyre Inputs tab is the authoritative editor for the persisted pre-race
baseline; current Race-informed values continue to follow live/replay inference.
With **Manual Override** enabled, both direct and derived live degradation are
ignored and the resolved manual/pre-race/default values are used unchanged.
Physical-order checks are advisory only and never reorder or clamp manual values.
Baseline is displayed as seconds relative to the quickest compound, where
`0.0000` is quickest.

## Strategy simulation

`woPlanner` can create strategy what-if simulations for a loaded race. This is
still a projection, not a full race simulator: it does not model overtaking,
lapping, or traffic. By default, simulated futures are kept in the observed race
time domain by applying modelled strategy changes on top of the observed race
scaffold.

Before using simulation, run `woStrategy`'s `race_performance_review` for the
same race/session. By default, woPlanner discovers its outputs from the canonical
woData event directory:

```text
woData/wostrategy/race_performance_review/schema_v1/
  year={year}/round={round}/session={session}/
```

`woData` owns cache path conventions for normalized planner artifacts. With the
usual sibling-checkout workflow from the workspace root, generated planner caches
are written under `woData/`, not inside the `woPlanner` or `woStrategy` source
trees. `WODATA_ROOT` can override that location. The planner-facing tyre summary
is `tyre_information.csv`; it holds global and team compound baseline speed,
compound delta, and degradation values in one file. The detailed source and
Monte Carlo files remain beside it for strategy modelling and reproducibility.

Older flat files under `cache/race_performance_review/` remain readable for
backward compatibility, but new woStrategy writes use woData.

Required source files are named like:

```text
tyre_information.csv
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

In `woplanner`, open **Retro Analysis → Strategy deltas** to inspect the
sector-level detail table and simulated
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
last pre-edit lap. Observed cumulative time excludes inactive red-flag delays in
both standard and live mode, so applying or editing a pit simulation cannot
switch the plot between elapsed session time and racing time. The plotted y-axis
is always:

```text
display_cumulative_time(driver) - display_cumulative_time(selected_reference)
```

If the selected reference driver has an active simulation, all plotted deltas
are recalculated against that simulated reference trajectory.

The observed strategy baseline includes future real pit losses. When a real pit
is removed, the dialog's pit-loss estimate replaces the default loss in the
modelled observed-strategy baseline, but is not added to the simulated no-pit
strategy. In real-race-adjusted mode, any observed residual beyond that entered
loss is retained rather than treating the full observed pit sequence as pit loss.
The same residual retention applies when an earlier what-if stop causes a later
real stop to be skipped; SC/VSC pit loss is removed through the model delta.

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
