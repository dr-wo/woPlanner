# Shared Live/Standard Plot Interactions

Last reviewed: 2026-08-22

This is the durable design and diagnostic handoff for functionality shared by
`woplanner` and `woplanner-live`. Read it before adding a mode-specific mouse,
axis, hover, tyre-marker, or pit-window implementation.

## Ownership and dependency rule

Shared presentation and dataframe policies belong in `woplanner.common`:

- `axis_controls.py`: Lap/Y widgets, ordering, automatic bounds, and custom-range state
- `plot_interactions.py`: curve-click binding and hover-label item lifecycle
- `plot_widget.py`: nearest-point matching, hover text, live curve selection, and rank lists
- `plot_data.py`: pure pit-window and tyre-event dataframe transforms
- `pit_window.py`: pyqtgraph pit-window items and cleanup

`standard` and `live` may import `common`; neither mode may import the other.
Mode services continue to own acquisition and their last plot dataframe. GUI
overlays consume that common plot-data contract rather than reaching into the
other service.

The standard GUI still owns simulation-only mouse behavior, track-status bands,
lap guides, export, and strategy dialogs. Live mode intentionally does not
inherit or invoke the standard simulation engine.

## Selection, reference, and clicking

Curve selection and pace reference are different state:

- clicking selects/emphasises a driver and updates the pit-window overlay;
- clicking never changes the top-bar reference driver;
- clicking empty plot space clears selection and the pit window;
- standard Command/Ctrl-click remains a simulation action after selecting the
  target driver.

This separation is deliberate. A selected driver's pit window is displayed in
the coordinate system of the currently selected reference.

`bind_driver_curve_click` is the common event adapter. Standard retains its
callback because it must inspect modifier keys; live uses the common widget's
selection callback. Do not duplicate pyqtgraph `sigClicked` lambda conventions
in either mode.

## Hover assumptions

Both modes use `nearest_relative_delta_row` and
`format_relative_delta_hover_text`. Distance is normalized by the visible X/Y
span, so zoom level affects which point is considered close. Standard adds a
mode-specific Command/Ctrl pit-target label, but ordinary hover text and label
rendering are shared.

Hover reports the nearest plotted row, not necessarily a completed lap. In live
mode that can be S1 or S2. In standard mode it can be a simulation row. Tyre age
is displayed using the planner convention `max(round(raw_age) - 1, 0)`.

## X-coordinate contract

The modes deliberately keep different internal X coordinates:

- standard: `segment_index`, with S1/S2/S3 at integer sector indices;
- live: `x_lap`, with S1/S2/S3 at `lap - 1/3`, `lap`, `lap + 1/3`.

The visible controls always show lap numbers. `AxisRangeControls` owns values and
ordering; each GUI supplies its own lap-to-plot transform. `PitWindowOverlay`
therefore requires the caller to choose `segment_index` or `x_lap`. Removing
that explicit choice will misalign the grey band in one mode.

Automatic bounds use the plotted `lap` and `delta_s` values with at least one
lap and five seconds of padding. After **Apply range**, live redraws must not
replace the controls as new sectors arrive. **Auto range** resumes data-driven
bounds. Pyqtgraph `set_plot_data` must not unconditionally enable auto-range,
because that would silently discard the user's applied range.

## Pit-window semantics and compromises

The grey overlay is a visual loss envelope around the selected driver's actual
cumulative-delta curve:

```text
normal boundaries = selected delta +/- Pit
inner boundaries  = selected delta +/- SC/VSC
```

Defaults are 20 s normal and 10 s SC/VSC. Both are user inputs. The overlay:

- filters out `plot_role == simulation` rows;
- is recalculated on selection, pit-loss edits, and plot redraw;
- uses only points currently present, so a live band grows sector by sector;
- is not a pit-stop prediction, undercut model, traffic model, or overtaking model;
- does not automatically switch the applicable loss based on current TrackStatus;
- does not account for pit-entry/exit geometry beyond the supplied loss values.

The two shades expose both user assumptions simultaneously. They do not claim
that a stop is possible or safe at every shaded point.

## Live timing and retirement assumptions

The live reducer publishes a sector as soon as a positive S1/S2/S3 time is
received. Public TimingData patches are incremental, so missing sector entries
do not clear values already held for the pending lap. If a recording contains
only completed laps, live plotting falls back to completed-lap S3 points.

Retirement cutoff uses only explicit `TimingData.Lines[*].Retired == true`.
Status strings, lack of movement, missing timing, and falling position are not
treated as DNF because each can occur temporarily. The first explicit retirement
captures the greatest completed/declared lap and the curve stops after that lap.
This is intentionally conservative: a feed that never sends `Retired` will not
be truncated automatically.

Pending-sector lap attribution is inferred from `NumberOfLaps`, `LastLapTime`,
and the last committed lap. Late connections may lack earlier sectors, and
recordings must be treated as the source of truth when diagnosing attribution.

The Year/Round/Session controls determine the recording directory before feed
metadata arrives. `SessionInfo` currently produces a mismatch warning only; it
does not rewrite those controls, rename/move a recording, or merge it into a
different event partition. A session started with stale controls can therefore
be valid raw timing stored under the wrong requested event. Diagnose identity
from the recording's `SessionInfo`, `DriverList`, and timing contents, not from
its directory name alone. Separate starts intentionally create separate files.

## Redraw and lifecycle diagnostics

When a live click, hover, or pit band disappears, check in order:

1. `LivePlannerService._last_plot_data` contains the driver and expected sector.
2. `reference_driver` still exists in the snapshot; selection must not be used as reference.
3. The plot dataframe contains `driver`, `lap`, `delta_s`, and `x_lap`.
4. `RelativeDeltaPlotWidget.selected_driver` survived the redraw.
5. `_update_pit_window` ran after `set_plot_data` (plot clearing removes overlays).
6. `PitWindowOverlay` received `x_column="x_lap"` in live or
   `x_column="segment_index"` in standard.
7. Custom range state was not reset by a reconnect or replay start.
8. Inspect the canonical raw recording before inferring a FastF1/feed problem.

Switching between replay and live is a new source lifecycle, not a continuation
of the previous plot. The controller store, queues, telemetry generation, start
ranks, and `_last_plot_data` are reset. The service's public snapshot is replaced
on the next poll, while GUI reference/selection/range controls are repopulated by
subsequent redraw logic rather than atomically at source start. This is a known
lifecycle compromise and leaves a transient stale-UI window; selection and
custom ranges are not explicitly cleared by `_prepare_start`. If data is received
but not rendered after switching, capture the snapshot, selected reference,
source mode, selection, range state, and `_last_plot_data` before closing the
window; closing destroys the most useful evidence.

## Validation baseline and known debt

Focused shared/live GUI and service checks pass (`26 passed` at this review), and
the standard GUI constructs offscreen. The whole woPlanner suite currently has
`130 passed, 17 failed`. The failures are existing standard service/model fixture
expectations around sector-expanded output, legacy lap-level assumptions, and a
missing `sample_parameters` fixture; they are not evidence of a live overlay
regression. Do not normalize the baseline by weakening shared interaction tests.

The live pit-window regression test asserts seven plot items (four boundary
curves and three fills), verifies fractional sector X coordinates, and verifies
cleanup on deselection.
