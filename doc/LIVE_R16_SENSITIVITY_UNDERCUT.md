# Live degradation sensitivity and observed undercut

These controls belong to the modern Live Planner and its Live replay window.
Standard Planner and report calculations do not adopt them.

## Degradation sensitivity

In Strategy, the Live degradation sensitivity multiplies the final effective
degradation vector after existing Global/Team/manual/Used resolution. It also
scales effective manual degradation. Performance differences are unchanged.
The editable Used values remain in their original coordinates; the read-only
Final degradation column shows what strategy costing consumes. A non-1 factor
has an active indicator, and Reset to 1.0 restores baseline sensitivity.

The factor is saved in Live window state. Strategy calculation input snapshots
store the base vector, factor, final vector and existing source provenance.
`replay_live_tyre_inputs` reconstructs and validates those inputs for replay;
legacy snapshots have identity sensitivity. MC fitting and canonical Tyre Inputs
remain unchanged, as do the existing Global model-history plots.
Saved Live window state without a sensitivity field also defaults to 1.0;
loading historical state or calculation snapshots requires no migration.

## Observed full-lap pace

The Live tab shows each confirmed racing pit visit with a replacement stint.
Data certifies pit entry and exit through a paired `racing_pit_stop_id`;
inferred tyre-change pit flags alone are insufficient. A visit during a red
flag or suspension is not certified. Older snapshots lacking the evidence
remain unavailable.

The measurement reuses the shared individual clean-full-lap classification.
MC minimum consecutive-run lengths and optional edge exclusions do not apply.
Select the latest suitable lap before the in-lap and earliest suitable lap
after the out-lap within the adjacent stints. Another pit visit bounds the
search. Missing references remain pending. Existing traffic evidence and
availability rules remain in force.

Observed delta is pre duration minus post duration: positive means post-stop
was faster. Durations are the existing unmodelled completed-lap values; this
is an observed pace comparison, not an isolated tyre effect.

The separate MC track column supports the current fitted Live Retro model.
It evaluates `rate * (lap - reference)` for both references from one snapshot;
the displayed change is post term minus pre term. Positive means the later
reference is modelled slower. Unsupported architectures or absent/stale model
provenance show unavailable. No corrected observed number or posterior interval
is calculated. Retro fuel/track identifiability metadata remains in the audit.

Hover a table row for durations, stop/reference identity, clean-lap settings,
and model rate/reference/analysis provenance. The latest observation records
are also saved under `live_observed_undercut` in Live window state with event
identity. Live replay recomputes observations from its canonical snapshot and
current fitted model. Standard windows preserve inactive Live state but neither
apply the sensitivity nor display these observations.
The observation audit is optional when loading older state. Raw replay messages
reconstruct stop evidence; older completed-lap snapshots without it remain safely
unavailable. Session metadata changes invalidate displayed observations, and the
saved audit retains the parsed race/sprint session identity.

## B2 extension: S2-derived lap-equivalent delta

The primary observed full-lap result and its reference selection remain unchanged.
The auxiliary diagnostic reads canonical `CompletedSector` S2 durations for the
**exact same driver and pre/post reference laps**, within the same certified stop
and adjacent stints. It does not detect another stop or select fallback sector
laps. Missing, ambiguous, non-finite, non-positive, mismatched-stint or impossible
(S2 >= full lap) evidence makes only the auxiliary diagnostic unavailable.

No existing sector-to-lap extrapolation convention was found in the four projects.
Qualifying sector correction preserves within-lap proportions when applying a
whole-lap correction; it does not specify how to extrapolate a sector delta.
This diagnostic therefore uses the symmetric mean of the two reference ratios:

```
raw_S2_delta = pre_S2 - post_S2
scale_factor = 0.5 * (pre_full_lap / pre_S2 + post_full_lap / post_S2)
S2_lap_equivalent_delta = raw_S2_delta * scale_factor
```

Each reference has equal weight. Swapping pre and post preserves the scale and
reverses the delta's sign. Positive means the post-stop S2 was faster. This is an
extrapolation of sector evidence, not a directly measured whole-lap improvement.

Live displays `Observed full-lap delta (s)` and
`S2-derived lap-equivalent delta (s)` together, followed by the existing MC track
interpretation. The explanation identifies the full-lap result as primary and
S2 as auxiliary. Disagreement may reflect tyre warm-up, sector-specific effects,
traffic or other lap variation; the values are never reconciled or averaged.
The row tooltip and saved `live_observed_undercut` audit include nested `s2`
status/reason, pre/post sector durations, raw delta, scale factor, equivalent
delta, formula, sign and source. The outer record retains the stop/stint identity,
reference laps, full-lap durations, separation, clean settings and MC provenance.
Sector-only snapshot changes already invalidate the Live observation signature.

### Real replay verification

`tools/verify_observed_undercut_s2.py` replays the local 2026 R16 Race recording
using a 1.1 quick threshold and the existing sector-boundary traffic mode with
2.5/1.0 s ahead/behind limits. It compares the current records, excluding only
the additive `s2` field, against the accepted implementation at commit
`479e0f09c6e3f6ed2da8f5bd3b3959b60ee8228d`. Every original audit field is exactly
equal across all 68 eligible stops: 12 have available full-lap and S2 pairs.
The remaining 56 retain their original pending status.

Example: VER stop ID 53366, pit-in/out laps 33/34, adjacent stints 2/3, clean reference
laps 32/35 (separation 3). The full-lap references are 102.190/100.888 s,
so observed full-lap delta remains **+1.302 s** before and after. S2 values are
34.315/34.456 s, raw S2 delta **-0.141 s**, scale **2.9530110534068186**, giving
S2-derived lap-equivalent **-0.4163745585303772 s** (UI **-0.416 s**).
This disagreement is retained as diagnostic evidence.

Recording/source hashes, options and all 12 reproducible examples are in
[`assets/observed_undercut_s2_r16.json`](assets/observed_undercut_s2_r16.json).
Focused analysis and GUI tests cover sign, both-reference scaling and symmetry,
missing/unusable S2, selection/stint boundaries, unchanged original fields,
worker serialization, historical cutoff, sector-only refresh, persistence and
the unchanged sensitivity/MC separation. No degradation sensitivity, MC track,
modelled/report undercut-power, tyre-life repair or other R16-R work is changed.

Verification results: `tests/test_observed_undercut.py` and
`tests/test_observed_undercut_gui.py` pass **59 tests**, including the existing
observed-undercut regressions. `tests/test_live_degradation_sensitivity.py` passes
**23 tests**. The real replay completes all 80,384 messages; all 68 original
records compare exactly and all 12 available S2 diagnostics reproduce from audit
inputs. The real VER row was also rendered and visually checked with both signed
values visible. `git diff --check` passes.
