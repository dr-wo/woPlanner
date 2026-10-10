# Live Track Map

Reconciled against woPlanner main b303a11 and woData main 8c2ba26 on 2026-10-03.

## Current contract

Track Map opens a reusable modeless window. Embedded Map toggles the in-planner
view. Both share the planner selection, worker snapshot and explicit current-event
window descriptors. Close/reopen synchronises current state; geometry is aspect-locked.
Real car markers are selectable, including overlapping markers using the existing
first-hit rule. Labels do not have separate selection handlers.

Both views use reference availability as the display gate. With no snapshot or
`snapshot.reference is None`, the map shows **Track map preparing — waiting for a
completed lap to establish the circuit.** Car markers, position labels and their
overlays stay hidden. Reference availability immediately restores the existing
circuit outline, current raw XY markers, windows and interactions. Clearing the
map, removing the reference or starting a new event/session without geometry
returns both views to preparing; popup reopen and embedded reopen synchronise the
current snapshot. Session/race start status is not a gate.

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

## R16 final debug closure — 2026-10-10

**DBG-3: CLOSED — Fixed.** Implemented on 2026-10-09 and independently verified
in the final audit. This was a presentation/readiness issue, not coordinate
corruption. R16 confirmed pre-reference marker movement is valid upstream
Position data, not Planner extrapolation. It is intentionally hidden because it
lacks usable circuit context. The completed non-pit lap geometry contract,
reference construction, raw XY, telemetry timing and ranking remain unchanged.

Verification: **19 focused tests passed**, including three readiness lifecycle
regressions (reference removal, clear and new session) across embedded and popup
maps, existing selection/window tests, R13 reference replay, popup reuse/collapse,
and worker position/freshness/reset tests. Qt ran offscreen with a persistent
QApplication and Agg plotting; native visual acceptance was not rerun.

**DBG-2: CLOSED — Not reproducible after adequate verification.** The original
cutoff calculation, serialization, recovered authored Markdown and figures were
verified correct. Unrestricted 0→1 and 1→2 values survived the report path;
compliant 0→1 was correctly unavailable under the rules, and historical 2→3 N/A
was explained by the original scan bounds. The exact historical rendered view
remains unavailable. Closure does not claim the original observation was impossible.

**DBG-7: CLOSED — Not reproducible after adequate verification.** Actual R16
replay through the current service and Qt rendering found no marker-coordinate
or mixed-frame discrepancy. Embedded and popup maps produced **7,920 rendered
coordinate comparisons with zero mismatches**; a forced GUI stall and reopening
retained synchronized snapshots. The original historical GUI frame remains
unavailable; closure means not reproduced with available evidence/current code.

Historical note: on 2026-10-09, DBG-2 and DBG-7 were PARK pending original
render/frame evidence. The final independent audit supersedes those statuses.

Current R16 backlog:

| Issue | Final status |
|-------|--------------|
| DBG-1 | CLOSED |
| DBG-2 | CLOSED — Not reproducible |
| DBG-3 | CLOSED — Fixed |
| DBG-4 | CLOSED |
| DBG-5 | CLOSED |
| DBG-6 | CLOSED — Diagnosed intentional traffic quarantine; automatic fitting was not restored |
| DBG-7 | CLOSED — Not reproducible |
| DBG-8 | CLOSED |
| DBG-9 | CLOSED |
| DBG-10 | CLOSED |
| DBG-11 | CLOSED |

Degradation-cutoff coverage and evidence-aware extension were committed separately:
woStrategy `a8ec804` (Add evidence-aware cutoff coverage) and woWeekend `fc0a490`
(Extend race cutoff coverage to FP evidence). No confirmed R16 debug issue remains
open; no additional production fix is required.

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
