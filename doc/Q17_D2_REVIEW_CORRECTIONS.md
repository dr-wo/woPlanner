# Q17 D2 — Three independent-review corrections

18 October 2026, Europe/London. Candidate for **FOCUSED INDEPENDENT RE-REVIEW**.
This supersedes the three P2 defects in the v1 handover; it does not change the
resource-root design or accepted Qualifying algorithms.

## A. Corrections and before/after evidence

Reviewed source was exactly `7feafae4b0a4541c13d02aeb1704c5d2650d5d05`, clean on
`codex/q17-runtime-resource-roots`; all 672 pinned files matched their manifests
and Git bytes before editing. Actual pinned Data/Planner/Strategy imports and
protected Git/data/install state were recorded before edits.

1. **FP CSV validation.** woData returns a zero-column DataFrame for zero-byte
   or whitespace-only input. Planner formerly skipped required-column checks in
   that case. The reviewer reproduction was repeated using copies of the
   verified R17 files: zero-byte duration was selected as Output, replacing
   123.019s with the default 100s; zero-byte progression also passed. A zero-byte
   lap file with consistent zero lap_count passed too. Required columns are now
   checked unconditionally. Blank, headerless and parser-invalid inputs raise
   `ArtifactError` in both analysis and service without resource substitution or
   a silent reset to default timings. Valid required headers with zero rows
   remain supported. Manifest, numeric, count and boolean checks are retained.

2. **Filesystem preflight.** A regular-file ancestor formerly passed both the
   resolver and actual `--check`; a real recorder write then raised
   `NotADirectoryError`. Every existing ancestor, including the root itself,
   must now be a directory. Missing ordinary components remain allowed without
   preflight creating them. The launcher's pure standard-library preflight
   checks output, runtime-state and intended cache paths before creating state
   or importing project code. Existing overlap, nested-link, hardlink and shared
   resolver checks remain active; invalid configuration cannot construct GUI.

3. **Cache ordering.** The original fresh-process reproduction observed no
   MPLCONFIGDIR/XDG settings at first Matplotlib import. Its actual cachedir was
   in an earlier temporary tree, despite later assigning the launch cache path.
   A probe write audit blocked the attempted default `~/.matplotlib` directory
   creation, so no external cache was written to reproduce the defect.
   Source/resource checks and Git worktree preflight now run using only standard
   library code. Next, a unique state tree is created and MPLCONFIGDIR, XDG,
   temporary-file and logging locations are configured. Only then is the pinned
   project import guard installed and shared runtime initialization imported.
   Fresh-process tests exercise REAL Matplotlib and both `--check` and ordinary
   GUI initialization branches; they compare actual cachedir/configdir/tempdir,
   not just environment variables. The isolated GUI fixture uses offscreen Qt;
   actual native results are recorded separately after the source commit.

All three pre-edit reproductions, actual import paths and after-fix regressions
are preserved externally. No woData or Strategy production file/schema changes.
Only `runtime_paths.py` and `q17_quali_isolated.py` change production behavior.

## B. Preserved contract

Complete valid Output artifacts win. Four absent output artifacts allow Resource
fallback. Partial/invalid Output artifacts fail explicitly through the shared
loader and GUI error path. A header-only, zero-observation set is not corrupt if
its required schema and manifest count are consistent. Legacy readers retain
prior behavior when opt-in separation is absent.

Geometry remains on Resource Root; captures/analysis on Output Root; replay is an
explicit read-only input. Identity reload, replay/live transitions, manual
overrides, Q17-04 colours, canonical publication/promotion and finalisation are
covered by the affected existing tests. No business-state lifecycle, fitting,
promotion, clock, traffic or presentation algorithm changes.

## C. Version and launcher usage

The old immutable v1 source manifest is preserved at:
`/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/versions/resource-roots-v1/source-manifest.json`.
Its SHA256 is `99298b2856795e85ac9c6ecfb4ff244a3c4a1cc58130805707d38e6cc243dfcc`. It describes reviewed commit
`7feafae4b0a4541c13d02aeb1704c5d2650d5d05`, not these corrections.

The corrected launcher default is the new immutable version:
`/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/versions/resource-roots-v2-d2/source-manifest.json`.
It is generated AFTER this source commit and pins that exact commit; its exact
SHA, tracked-file hashes, output namespace and actual imported/cache paths are
recorded in the external final handover. Output namespace is
`/Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/outputs/<corrected commit>`.
The existing verified `resources/r17-fp-v1` snapshot and resource manifest remain
unchanged. Historical v1 provenance is retained. Advancing main is never a
source-selection mechanism; old manifests correctly refuse a different HEAD.

```sh
/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner/scripts/q17-quali-isolated.command --check
/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner/scripts/q17-quali-isolated.command --manifest /Users/zhxutong/dr-wo/q17-runtime/resource-roots-20261018/versions/resource-roots-v2-d2/source-manifest.json --replay
```

Python 3.11.15 stays fixed at `/Users/zhxutong/dr-wo/.venv/bin/python`.
Data stays at `7580ccb614d2d7d2173ac6c9fc91409bfb338559`; Strategy stays at
`336098b146c93c53b46b998ba62416aadcc0391d`, from unchanged clean release sources.
Source HEAD/hash/inventory/dependency checks and hash-verified lazy project
imports are preserved. Existing editable installs/default launcher are unchanged.

## D. Tests and bounded GUI scope

- Initial new P2 tests: 30 passed; the added legacy regression brings new tests
  to 31. Final full focused collection: **84 passed**, zero failures/skips.
- Affected Qualifying collection after production source stabilized:
  **992 passed, 2 skipped, 12 deselected**, zero failures.
- R13/R14 historical inputs are missing: these are skips, not successful tests.
  Original R17 and R16 historical replay cases are explicitly deselected for
  this path-only correction. Full historical/promotion golden runs are not
  repeated. Controlled canonical/promotion/finalisation tests still run.
- The external runner retains the prior exact read-only recording lookup adapter
  needed by accepted test module imports; it does not alter test source or data.
- Fresh Python probes run with unchanged source verification against disposable
  real Git fixture sources, real Matplotlib, and network/external-write refusal.
  Their actual paths are saved in JUnit properties. After the final source
  commit, actual production `--check`, refusal probes and bounded native Cocoa
  smoke are recorded externally against the new manifest, with source/resource
  preservation checks. Those results, rather than v1 results, are the acceptance
  evidence for this correction.

The native smoke is restricted to saved FP reads, two narrow windows from the
accepted R17 recording, Q2/Q3 presentation, Track Correction and manual override.
It does not claim unrestricted full-replay acceptance. The independent review's
same-input comparison showed comparable parent/D2 replay performance, supporting
a pre-existing concern; the replay engine and unrelated UI issues are untouched.

## E. Git and evidence

Continue existing persistent worktree:
`/Users/zhxutong/dr-wo/q17-worktrees/runtime-resource-roots/woPlanner`.
Original accepted parent remains `d75293c1e7651731b2196db106615d93f234467b`.
Correction parent is `7feafae4b0a4541c13d02aeb1704c5d2650d5d05`.
Final correction SHA and reconstruction patch are external to avoid embedding a
self-referential Git ID. The feature must be clean at handover. No merge, push,
cherry-pick, reset, rebase, deployment or editable-install change.

New correction evidence:
`/Users/zhxutong/.codex/visualizations/2026/10/18/01a14fda-ecd0-7061-ad1c-3fd54a068adb/q17-d2-corrections/evidence/`.
Includes preflight, three pre-edit reproductions, fresh-process cache paths,
focused/affected-suite logs, actual launcher refusals, native snapshots and
before/after preservation inventories. See external `handover.json` and
`HANDOVER.md` for final validation results and exact source/cache/version paths.

## F. Boundaries

R17 geometry is still absent; no geometry or network FP data was invented or
downloaded. Live-network FP acquisition/capture is unverified. Startup filesystem
preflight does not prevent hostile concurrent filesystem mutations or provide
an OS write sandbox. Independent re-review is required before any merge.
