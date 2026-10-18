# Q17 — Runtime Resource/Output Roots and Opt-in Launcher

18 October 2026, Europe/London. **READY FOR FOCUSED INDEPENDENT REVIEW**.

## A. Resource contract

Without `WOPLANNER_QUALI_RESOURCE_ROOT`, the existing GUI workspace-root
selection, explicit-root precedence and woData fallback remain intact. In
particular, merely setting `WODATA_ROOT` does not silently change the installed
GUI's historical explicit-root behavior. Direct analysis/service callers retain
their existing explicit-root/None behavior.

With the setting present, it denotes an existing immutable operational resource
snapshot; an explicit absolute `WODATA_ROOT` denotes writable runtime outputs.
Relative paths, nonexistent resources, nested/equal roots, source/shared/release
worktree overlaps, symlink components and multiply-linked runtime files are
rejected. Both ancestor and descendant overlaps are checked against imported
Data/Planner/Strategy repositories and their registered Git worktrees. Root
validation precedes window construction. Existing nested output links are also
checked before analysis/capture writes; individual FP/geometry paths are checked
before reads. This is a path contract, not an OS filesystem sandbox or a lock
against concurrent hostile filesystem changes.

`RuntimePaths` is shared by the GUI, analysis service and live service. It adds
no alternative business-state lifecycle. Geometry reads use Resource Root;
new live recordings, identity sidecars, regenerated analysis and the explicit
FastF1 raw cache use Output Root. Replay retains its explicitly chosen input
file and never creates a live recorder.

FP selection is identical in analysis and operational service reloads:

1. A complete valid Output Root set wins.
2. Only if all four output artifacts are absent, inspect Resource Root.
3. If both sets are absent, analysis raises an explicit missing-artifact error;
   service retains supported fallback timings and records the missing reason.

Presence of any incomplete/invalid set raises an explicit error rather than
mixing roots or substituting an older resource copy. Validation covers the four
files, manifest schema/event/count/threshold, consumed table columns, finite
numeric values, nonnegative integer sample counts and boolean reference masks.
The accepted finite nonnegative threshold domain is preserved, including zero
and values >=1. Existing woData schemas and readers/writers are unchanged.
After a new analysis write the GUI explicitly reloads operational artifacts;
feed identity updates and replay/live transitions preserve output precedence.
Manual overrides retain their accepted precedence.

## B. Implementation and usage

Only these production files change:

- `src/woplanner/quali/runtime_paths.py`: shared immutable configuration,
  protected-worktree discovery, descendant/link checks and FP selection.
- `src/woplanner/quali/analysis.py`: shared selection, output-only writes/cache,
  `loaded_artifact_root` provenance.
- `src/woplanner/quali/service.py`: geometry/capture separation,
  `reload_operational_artifacts`, output-first identity reload and explicit
  `operational_artifact_root`/`operational_artifact_error` diagnostics.
- `src/woplanner/quali/gui.py`: resolve once before startup, pass the same object,
  route live/replay roots and display corrupt-artifact errors.

Two opt-in launcher files are added under `scripts/`; no installed entrypoint,
editable installation or package configuration is changed. Launch from any cwd:

```sh
/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner/scripts/q17-quali-isolated.command --check
/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner/scripts/q17-quali-isolated.command --replay
```

`--check` verifies pins/resources and imports without constructing a window.
`--replay` hash-verifies and selects the accepted R17 recording; click Replay in
the window to start it. No live connection or network FP analysis starts by
default. Connect and Analyse FP remain explicit user actions.

The default manifest is immutable:
`/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/versions/resource-roots-v1/source-manifest.json`.
It pins the final feature commit, **not main**. `--manifest /absolute/file.json`
selects another explicitly prepared version. Any source HEAD/hash/inventory or
dependency mismatch refuses startup with diagnostics. Ignored extra Python
sources are rejected; every project import, including lazy imports, must resolve
to an inventoried `.py` with the pinned hash. A fresh external bytecode cache
prefix prevents adjacent stale `.pyc` files from supplying code; bytecode writes
are disabled. Actual imported paths/hashes are recorded in launch provenance.

Interpreter: `/Users/zhxutong/dr-wo/.venv/bin/python`, Python **3.11.15**, exact
venv prefix verified. Data commit: `7580ccb614d2d7d2173ac6c9fc91409bfb338559`.
Strategy commit: `336098b146c93c53b46b998ba62416aadcc0391d`. Both use unchanged
clean release worktrees under `q17-release/controlled-20261018`. Actual Planner
imports use this dedicated feature's `src/woplanner`, not shared development code.

Pinned installed dependency versions: NumPy 2.4.6; pandas 2.3.3; PySide6 6.11.1;
Matplotlib 3.11.0; FastF1 3.8.3; SciPy 1.17.1; pyqtgraph 0.14.0; signalrcore
1.0.2; requests 2.34.2. Project identity is established by Git/source bytes,
rather than editable distribution metadata versions.

External runtime layout:

```text
/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/
  versions/resource-roots-v1/source-manifest.json
  resources/r17-fp-v1/resource-manifest.json
  resources/r17-fp-v1/wostrategy/pre_quali/schema_v1/year=2026/round=17/<four files>
  outputs/<exact Planner commit>/
  launches/<manifest version>/<UTC timestamp and unique ID>/
    provenance.json
    logs/{application.log,stderr.log}
    cache/  config/  data/  tmp/  xdg-runtime/
```

Resources are exactly the four saved FP files from the original shared
`woData/wostrategy/pre_quali/schema_v1/year=2026/round=17` directory: 114,318
bytes total. Each source was checked against the prior accepted inventory before
copying, copied as a distinct regular inode, rehashed, then made read-only
(files 0444, directories 0555). The version-1 resource manifest records each
absolute source/destination, size and SHA256. No whole-store copy, symlink or
hard link is used. R17 geometry is unavailable and was not invented/downloaded.

The explicit R17 replay remains in the original read-only recording location;
SHA256 `8d084328530605780c74035c13d6ad339e0a5e6270f7c535c6761e4683012388`.
No recording copy or write is required.

For a source-version update, first independently accept an exact commit set and
clean source worktrees. Create a NEW version directory and output namespace;
record and compare every tracked file to Git bytes, regenerate the source
manifest, reverify resource provenance/dependencies/imports, repeat focused tests
and a bounded native smoke, then review the version selection. Do not overwrite
old manifests or derive launch pins from advancing main. The preserved external
`q17_pin_version.py` requires the intended exact Planner SHA argument and refuses
a different checkout. A new source checkout is needed to launch an older pin
once this feature checkout advances; mismatch refusal is intentional.

## C. Validation

Final focused runtime/launcher tests: **53 passed**. Full affected Qualifying
suite: **968 passed, 7 skipped in 143.24s (0:02:23)**. No test failures in the final runs. All external socket
connections were disabled in the full-suite runner and native bounded harness.
Tests cover all twenty requested contract areas using controlled artifacts and
actual GUI/analysis/service/woData call sites, including a real local analysis
write and local capture recorder write. Existing canonical diagnostic,
promotion, chronology, manual override, widget and Q17-04 colour tests pass.
Transition tests exercise both resource fallback and valid output selection.

Initial broad collection failed in seven existing tests because they assume a
sibling R17 recording directory. No accepted test source or data tree was
modified to fix this. The external runner remaps only that exact test recording
lookup (`glob` and existence check) to the explicit accepted read-only file.
This enables the original R17 integration test too. Remaining skips concern R13,
R14 and R16 historical fixture availability; see the exact skip list in the log.

Actual native macOS validation, not inferred from offscreen tests:

- The `.command` launcher opened the Cocoa Qualifying window with the correct
  R17 selected path. An ordinary unrestricted Replay click made desktop
  inspection time out; that dedicated attempt was terminated after about
  90 seconds. No successful full native replay is claimed.
- A second normal `.command` launch displayed Quali Plan and Track Correction.
  CUA interaction verified a nonzero `-0.0200 s / field push` manual override.
- A separate external **60-second native Cocoa harness** used the same launcher's
  verification/configuration and production replay/controller ingress. It read
  only the first 12 accepted keyframes (0..0.004 playback seconds), then the
  actual Q3 transition packet at sequence 4064 (872.52860..872.52861). No synthetic
  Q2/Q3 state was substituted. This is a bounded presentation/path smoke, not
  contiguous replay chronology validation.
- CUA observed Q2 cutoff presentation, loaded FP timings (Out 123.019s, n=3),
  Track Correction and the manual override, then Q3 P1 presentation. The sparse
  Q3 sample has no Q3 clock/time; no full-Q3 accuracy claim follows from it.
- Native evidence records `qt_platform=cocoa`, exit 0, resource-selected FP root,
  supported absent geometry, absent live recorder, 124 pinned loaded project
  modules and before/after resource identity. PNG snapshots are preserved.
  All 222 audited production/script files retain their native-launch hashes.

The geometry/capture write paths and output-only FP generation are tested locally;
no real live network connection or network-derived FP run was performed. The
native smoke does not establish full-replay performance. The observed existing
Track Correction clipping/aspect issues are unchanged as explicitly required.

## D. Git isolation and reconstruction

Branch: `codex/q17-runtime-resource-roots`.
Persistent worktree:
`/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner`.
Parent: `d75293c1e7651731b2196db106615d93f234467b`, verified to contain both
`d9c25d7109b050c13fad1ffdf398987f09579031` and
`3bdb39eb311bf35200f14e9c1448c4d7b13ff159` before implementation.
Implementation/native-tested commit: `b4bb52077c81a5e457c2beb4feb0daa7eeac3b72`.
The final follow-up adds this handover and strengthens tests only; all production
and launcher bytes are identical to the native-tested commit. Exact final SHA,
parent, final pins, manifest/patch hashes and evidence index are recorded in the
external `handover.json` and final response, avoiding a self-referential Git ID.

Independent focused review was performed by a separate reviewer. Its hardlink,
count, import/bytecode, threshold-domain and boolean-mask findings were repaired
and covered by regressions. Final re-review found no material blockers. This
report does not authorise a merge.

Preservation audit compares ten protected repository HEADs, full porcelain
statuses including ignored/untracked names, tracked file bytes, 641 inventoried
operational resources (including the R17 recording), installed launcher/editable
metadata hashes and local Planner main. Before/after comparison is identical.
This includes all three release main worktrees, shared dirty repositories,
Q17-04, P3, prior Runtime Launcher and B/C worktrees. Feature registration/commits
necessarily add common Git administrative metadata but do not alter those
working files or refs. Other untracked file contents outside the resource
inventory were not comprehensively hashed.

External evidence and reconstruction patch:
`/Users/zhxutong/.codex/visualizations/2026/10/18/01a14fda-ecd0-7061-ad1c-3fd54a068adb/q17-resource-roots/evidence/`.
Includes before/after inventories, test logs/runners, independent review,
native JSON/PNGs/harness, resource preparation/version pinning helpers,
source manifest, binary reconstruction patch and its SHA256. Source worktree
is clean after the final commit. No merge, push, deployment or default-install
change was performed.

## E. Limits

R17 geometry is absent; saved FP progression contains no supported fitted rate.
The application keeps its accepted unavailable/fallback behavior. Scientific
FP estimate quality is not revalidated by file identity checks. Live networking,
network FP data acquisition and unrestricted native full-replay responsiveness
remain unverified. The launcher pins the recorded core dependency versions;
it does not hash or freeze every third-party binary dependency. Root/link/hash
checks and read-only snapshot permissions are not a general OS write sandbox;
concurrent owner-level filesystem mutations require separate controls if that
threat model becomes relevant.

## F. Verdict

**READY FOR FOCUSED INDEPENDENT REVIEW**.

Path implementation, controlled offline tests, independent code review and bounded
native path/presentation validation are complete. The unrestricted native replay
responsiveness limitation is explicitly preserved for review, outside this
path-plumbing change. Stop at this handover; do not merge or change defaults.
