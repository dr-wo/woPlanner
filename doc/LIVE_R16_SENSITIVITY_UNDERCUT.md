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
