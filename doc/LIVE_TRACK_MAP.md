# Live Track Map

Reconciled against woPlanner main b303a11 and woData main 8c2ba26 on 2026-10-03.

## Current contract

Track Map opens a reusable modeless window. Embedded Map toggles the in-planner
view. Both share the planner selection, worker snapshot and explicit current-event
window descriptors. Close/reopen synchronises current state; geometry is aspect-locked.
Real car markers are selectable, including overlapping markers using the existing
first-hit rule. Labels do not have separate selection handlers.

Pit/GREEN, SC and VSC are independent explicit inputs. Missing VSC stays absent;
equal explicit SC and VSC remain separate descriptors. Legacy sc_vsc is context
only. There is no SC-derived VSC fallback. Map offsets do not estimate pit loss.

The approximate reference is one actual completed non-pit Position.z lap with
at least 30 samples and a closure within 20% of trajectory extent. This is a
driven trajectory, not an official centreline. Mapping projects XY onto the
reference and wraps sample-time offsets around the lap. Only DriverList identities
render. Stale (>5 seconds), zero-position and pit/off-track states suppress
projected windows. The clock follows recorded sample chronology, not playback speed.
Current position history retains 1,200 seconds with a 6,000-sample per-driver cap.
Raw diagnostic positions do not become MC traffic inputs.

## Reproduce evidence

Run from this checkout with its src first on PYTHONPATH and current woData /
woStrategy src available. Supply the local R13 recording and its matching
race-preparation config.resolved.json:

```sh
QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg python tools/render_r13_track_map_evidence.py \
  --recording /path/to/R13/live_timing_raw.jsonl \
  --event-config /path/to/R13/config.resolved.json \
  --output /tmp/track-map-r13.png
QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg python tools/validate_r13_track_map_evidence.py \
  --recording /path/to/R13/live_timing_raw.jsonl \
  --event-config /path/to/R13/config.resolved.json \
  --output /tmp/track-map-validation.json
```

Collectors use an isolated temporary config copy and disable Apply in their
window. They assert the source bytes are unchanged. This is collector isolation,
not a new production read-only CLI contract. The second collector requires output
outside the repository and reports private diagnostic access explicitly.

`assets/track_map_main_r13*` records new offscreen selection, window reuse, resize,
SC-only/separate-VSC, stale and event-mismatch evidence. These captures are not
native macOS visual acceptance. The LAW pit cycle / five-lap reference / Max
replay report is `assets/track_map_main_r13_validation.json`.

## Historical VSC boundary

woWeekend M2 is a separate five-year historical evidence query. Numbers retain
the existing GREEN-relative pit-sector excess semantics. They are not operational
VSC pit windows. No aggregation, candidate, Apply or operational promotion exists.
M3/live empirical updates are outside scope.

`assets/dutch_vsc_evidence_2026.json` is retained unchanged from old integration
44b13b7 as historical provenance only. It is not a newly executed Dutch validation
or proof of identical layout versions. Old integrated screenshots and reports
with combined SC/VSC or Team acceptance are not current-main acceptance artifacts.
