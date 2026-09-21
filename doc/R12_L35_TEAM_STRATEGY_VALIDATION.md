# R12 L35 team-strategy validation

Status: **PASS**. This is acceptance evidence for the current uncommitted
`live-strategy-team-mode` feature worktree, not for the base commit by itself.
The compact machine-readable companion is
[`assets/r12_l35_team_strategy_validation.json`](assets/r12_l35_team_strategy_validation.json).

## Code identity

Both base and `HEAD` are `9a5cd47ae308e59ae1e9d2dd47601ce4e7aaf0d6`.
That commit does **not** identify the feature: the tested code identity is the
combination of that commit, the current tracked binary diff SHA-256
`61aaf780428bd238994867e148d29dcfa06a77821898d5b58ed2a5cf858bddec`,
and the relevant file hashes below. The untracked validation script is separately
identified because it is absent from the tracked diff.

| File | SHA-256 |
| --- | --- |
| `tools/validate_r12_l35_team_strategy.py` | `ba8b8f2f669137180f7d3d4f719841a9430c315ab51e067292149e304b4425dd` |
| `src/woplanner/analysis/gui.py` | `0dd5e63d2a92daa95fa985719c7e4c9126195ee056ec467ee35477f8be077138` |
| `src/woplanner/analysis/service.py` | `738ee923c8255c9a323060ede3e4da21420178583a26c73d2dfd1b46548ac660` |
| `src/woplanner/analysis/tyre_inputs.py` | `f381881a2a6d64551846296c13ae86cc09f5004e57ad74b5c49b2c3a93690433` |
| `src/woplanner/live/gui.py` | `d18f1015d7dcbc0692d1ae8a4d5cbd1472dc4e3ec0bc1e6da6dc2cb9d6918da5` |
| `src/woplanner/live/service.py` | `7d497c95b9685cf96d8701b361b4847bed36c56e869359aa92e69aeb5e8b2f43` |

Final `git status --short` at evidence capture:

```text
 M README.md
 M src/woplanner/analysis/gui.py
 M src/woplanner/analysis/service.py
 M src/woplanner/analysis/tyre_inputs.py
 M src/woplanner/live/gui.py
 M tests/test_live_gui.py
 M tests/test_tyre_inputs.py
 M tests/test_tyre_inputs_gui.py
?? doc/R12_L35_TEAM_STRATEGY_VALIDATION.md
?? doc/assets/r12_l35_team_strategy_validation.json
?? tools/validate_r12_l35_team_strategy.py
```

The rerun printed and checked that `woplanner.analysis.service`,
`woplanner.analysis.tyre_inputs`, and `woplanner.live.service` came from the
feature worktree `src/`; the final colour-SSOT rerun loaded `wodata` from
`/Users/zhxutong/dr-wo/woData/.worktrees/distinct-team-colours/src`.
This prevents an installed package or the base checkout from silently supplying
old planner code.

## Causal input and configuration

The input is the existing recovery manifest
`/Users/zhxutong/dr-wo/woData/wodata/live_timing/schema_v1/year=2026/round=12/session=R/recovered_replay_manifest.json`.
Its accepted sources are `live_timing_raw.txt`, `live_timing_raw_2.txt`,
`live_timing_raw_3.txt`, and `live_timing_raw_4.txt` in the same directory.
The replay had 92,309 messages and zero invalid records. The script reduced only
the 49,004-message prefix ending with TimingData sequence 49,004 at clock
1787494783.883, then applied `snapshot_as_of_leader_lap(..., 35)`. Maximum
completed lap is 35 and maximum clean lap is 34; no post-cutoff messages feed the
fit.

The run uses 5,000 Latin-hypercube samples, seed 1725593179, team baseline,
Gaussian weights, accelerated evaluation, and `persist=False`. Clean selection
uses quick threshold 1.1, at least four consecutive laps, H/M/S, no extra stint
edge exclusions, sector-boundary traffic, and gaps of 2.5 s ahead / 1.0 s behind.
It yields 95 laps in 14 runs: Global H 43/7, M 28/4, S 24/3. Mercedes has
H 8/1 (L24–31), M 10/1 (L11–20), and no S support. McLaren has H 10/1
(L8–17), with no M or S support. The JSON preserves the manifest paths, cutoff,
filtering, run configuration, selected-team support, and model identifiers without
copying the 67 KB recording detail or a clean-lap CSV.

## Posterior and operational inputs

Rates are seconds per tyre-age lap. P10/P50/P90 are marginal posterior summaries.

| Scope | Compound | P10 | P50 | P90 |
| --- | --- | ---: | ---: | ---: |
| Global | HARD | 0.030214663741 | 0.151996269892 | 0.374360235318 |
| Global | MEDIUM | 0.092639796139 | 0.244796215223 | 0.447514970205 |
| Global | SOFT | 0.157692310815 | 0.336094355140 | 0.483308561452 |
| Mercedes | HARD | 0.026821944290 | 0.147747611532 | 0.384772277526 |
| Mercedes | MEDIUM | 0.078820875412 | 0.213748987640 | 0.442880139657 |
| Mercedes | SOFT | unavailable | unavailable | unavailable |
| McLaren | HARD | 0.031853774573 | 0.144135043519 | 0.380964774363 |
| McLaren | MEDIUM | unavailable | unavailable | unavailable |
| McLaren | SOFT | unavailable | unavailable | unavailable |

ESS 813.388190927183 (16.2678% of 5,000) is the shared global joint-sampler
ESS repeated on parameter rows. It is not Mercedes-, McLaren-, compound-, or
run-specific evidence strength.

For Global degradation, the rerun preserves H < M < S in every reported
marginal: P10 is 0.030214663741 < 0.092639796139 < 0.157692310815, P50 is
0.151996269892 < 0.244796215223 < 0.336094355140, and P90 is
0.374360235318 < 0.447514970205 < 0.483308561452. The actual Strategy build
boundary uses the same ordered P50 values. This is consistent with the existing
model recalculation setting `enforce_compound_order=True`; the current UI-only
change did not add or alter a fit/search constraint. Likewise,
`physical_order_warnings` remains warning-only for manual values: the GUI labels
the result as an advisory, while manual Strategy inputs are not clamped or
rewritten. That is existing code semantics, not a change to the algorithm.

The script called the actual `resolve_tyre_inputs`, `effective_tyre_inputs`, and
`generate_continuations(..., persist=False)` paths. A wrapper captured the actual
`build_strategy_model` call and then invoked the original method. Effective and
captured `StrategyModel.degradation` values were identical:

| Selector | HARD | MEDIUM | SOFT | Provenance |
| --- | ---: | ---: | ---: | --- |
| Global | 0.151996269892 | 0.244796215223 | 0.336094355140 | all `live_direct` |
| Mercedes | 0.147747611532 | 0.213748987640 | 0.318542701041 | H/M `team_live_direct`; S `team_live_derived` |
| McLaren | 0.144135043519 | 0.209037755984 | 0.311521715943 | H direct; M/S derived |

Mercedes H/M and McLaren H equal the absolute team posterior medians (tolerance
1e-14), rather than a global median plus an offset. Missing compounds use the
existing non-manual fixed initial ratios. All three selectors use the same
Global Race performance inputs, rebased to MEDIUM: H
`0.29727929071969816`, M `0`, S `-0.36801556225045107`. The actual build capture
contains those exact values. Each real search returned three plans.

## Exact reproduction and test closure

Run from the feature worktree. The validator rejects repository output paths,
installs a write audit hook for all three repository roots, and writes only the
explicit JSON output. It does not create manual/durable model or strategy
artifacts, clean CSVs, or workspace caches.

```sh
PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/private/tmp/r12-team-matplotlib /Users/zhxutong/dr-wo/.venv/bin/python tools/validate_r12_l35_team_strategy.py --planner-root /Users/zhxutong/dr-wo/woPlanner/.worktrees/live-strategy-team-mode --strategy-root /Users/zhxutong/dr-wo/woStrategy --data-root /Users/zhxutong/dr-wo/woData --input-manifest /Users/zhxutong/dr-wo/woData/wodata/live_timing/schema_v1/year=2026/round=12/session=R/recovered_replay_manifest.json --expected-feature-src /Users/zhxutong/dr-wo/woPlanner/.worktrees/live-strategy-team-mode/src --output /private/tmp/r12_l35_team_strategy_validation.json
```

Latest result: PASS; 49,004 causal messages, 95 clean laps / 14 runs, 5,000
samples, and all Global/Mercedes/McLaren build-boundary assertions passed. The
key values match the latest disposable evidence.

After the planner consumer's colour-SSOT-only adjustment, the validator was run
again with `PYTHONPATH` ordered as planner feature `src`, `woStrategy/src`, then
`woData/.worktrees/distinct-team-colours/src`. Because model/replay artifacts are
outside that woData code worktree, the run used a disposable `/private/tmp`
data-root facade: its `src` linked to the distinct-team-colours source while its
model/replay directories linked read-only to the existing main woData data. The
printed import path confirmed the distinct-team-colours `wodata` module. Result:
**PASS**; the actual Global, Mercedes, and McLaren Strategy inputs remained
exactly those recorded above. The facade was removed after the run.

The earlier three-test failure was an import-environment problem. Closure ran
from `/private/tmp` in one Python process, put feature `src`, `woStrategy/src`,
and `woData/src` on `PYTHONPATH`, imported `woplanner.live.service`, asserted its
exact feature-worktree path, and only then invoked `pytest.main(...)` in that
same process. The printed `FEATURE_MODULE` line makes the locked module identity
visible alongside the test result:

```sh
PYTHONPATH=/Users/zhxutong/dr-wo/woPlanner/.worktrees/live-strategy-team-mode/src:/Users/zhxutong/dr-wo/woStrategy/src:/Users/zhxutong/dr-wo/woData/src PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/private/tmp/r12-team-matplotlib FEATURE_SRC=/Users/zhxutong/dr-wo/woPlanner/.worktrees/live-strategy-team-mode/src /Users/zhxutong/dr-wo/.venv/bin/python -c 'import os; from pathlib import Path; import pytest; import woplanner.live.service as feature_module; actual=Path(feature_module.__file__).resolve(); expected=(Path(os.environ["FEATURE_SRC"]).resolve()/"woplanner/live/service.py"); print(f"FEATURE_MODULE={actual}", flush=True); assert actual == expected, (actual, expected); raise SystemExit(pytest.main(["-q", "-c", "/dev/null", "-p", "no:cacheprovider", "/Users/zhxutong/dr-wo/woPlanner/tests/test_live_service.py"]))'
```

Result: `FEATURE_MODULE=/Users/zhxutong/dr-wo/woPlanner/.worktrees/live-strategy-team-mode/src/woplanner/live/service.py`, then **17 passed in 0.53 s**.

The current offscreen feature closure command was:

```sh
QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/private/tmp/r12-team-matplotlib PYTHONPATH=src:/Users/zhxutong/dr-wo/woStrategy/src:/Users/zhxutong/dr-wo/woData/src /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tyre_inputs.py tests/test_analysis_service.py tests/test_tyre_inputs_gui.py tests/test_live_gui.py
```

Result after the current UI diff: **102 passed in 5.64 s**.

## Limits

This is a single historical R12/L35 replay and a controlled common search state
(lap 35/72, HARD age 10, pit loss 22 s, at most two future stops). It validates
causality, fit/support reporting, selector provenance, and actual strategy-model
input consumption; it does not claim that the returned plan is sporting-optimal,
that one-lap pit-placement differences are material, or that the same posterior
will result from other events/cutoffs. The automated validator itself does not
cover GUI visual inspection, a live network feed, persistence round trip, or new
statistical calibration.
Native user feedback confirms the layout, ordering, grey disabled-team state,
bright enabled-team colours, and visual distinction between teams. It does
**not** confirm numeric consistency: the native Global/team values have a
separate, unexplained difference from this offline validator. This evidence must
not mark those values as numerically consistent; the discrepancy remains a
separate issue. No numeric fix was attempted in this colour-SSOT evidence update.
