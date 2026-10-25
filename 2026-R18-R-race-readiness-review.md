# 2026-R18-R — Race Readiness Review

Reviewed on 25 October 2026, before the scheduled 20:00 GMT start. Event: United States Grand Prix, Austin; 56 laps.

## Executive verdict

**READY with AMBER limitations for the existing assisted strategy workflow.** Genuine R18 practice preparation, a supported historical tyre forecast, Selected-to-strategy propagation, the actual Planner UI, the ordinary Weekend command, and native historical Race MC-to-strategy updates have been demonstrated. No critical component remains RED.

The usable R18 tyre forecast is explicitly **prior_only**, using the saved descriptor-free historical P0/D0 models trained through R16 and the verified R18 C2/C3/C4 allocation. Current FP evidence remains diagnostic; it does not identify absolute tyre degradation independently of fuel. Green-flag pit loss is an explicit **20.5-second template assumption**, not an Austin measurement. SC/VSC losses and actual available tyre-set inventory remain unresolved. These limitations constrain the numerical recommendations.

| Component | Status | Actual evidence | Blocker |
|---|---|---|---|
| FP acquisition | GREEN | FastF1 3.9.0 authenticated acquisition established in the previous audit; current clean-process cache reload: FP1 507, FP2 589, FP3 423 laps, correct R18 identity | None locally; this review did not repeat cold downloads |
| Tyre prediction | AMBER | Public prior producer executed against the normal data root, reloaded through the canonical provider; meaningful signed values, exact historical provenance, null uncertainty | No accepted R18 descriptor preview; no identifiable absolute FP posterior |
| Tyre input to Race MC / strategy | AMBER | All five Selected coordinates reached the actual R18 UI calculation unchanged; fresh historical Race MC coordinates subsequently reached a real strategy calculation | Existing default retro MC samples observed Race laps independently of Selected; Selected is the strategy baseline, not its sampling prior |
| Race strategy simulation | AMBER | Finite one-, two- and three-stop searches at 56 laps; actual Qt result table; separate genuine Race-only replay MC at 512 samples | Conditional on pit loss and available sets; exact central rankings have no probability/ESS |
| Race UI/workflow | GREEN | Main live module starts as 2026/R18/R; Strategy UI renders genuine preparation and calculates; ordinary Weekend CLI produces a successful immutable run/report bundle | None for the verified path |
| Live startup/replay | AMBER | Real R17 packet prefix → controller → telemetry → spawned MC → GUI → fresh strategy continuations, retaining R17 identity and timestamps | Future R18 transport and observed Race posterior cannot be verified before the race |
| Cross-repository compatibility | GREEN | 499 distinct coordinator-selected tests passed, 3 documented skips; independent review; package inclusion/import smoke; protected changes preserved | Snapshot wheels require rebuilding before distribution |

This verdict establishes local operational usability. It does not establish forecast calibration, race outcome accuracy, or a live connection to tonight's future feed.

## Review findings and minimal fixes

The earlier weekend in which tyre prediction was unusable could not be tied to a recorded exception. The following are reproduced present-day defects and missing-input boundaries; they are not attributed retrospectively to that incident.

1. **A valid saved forecast was blocked by an unused missing preview.** The normal Weekend prerequisite path required an accepted Pirelli preview and invoked the producer before consuming an existing canonical forecast. Genuine R18 has no accepted local preview. [prepare_race_prerequisites](/Users/zhxutong/dr-wo/woWeekend/src/woweekend/workflows/race_prerequisites.py:24) now permits prepared-event reuse only when the preview is absent and a canonical forecast exists. It validates the forecast, event identity, complete current-contract FP caches, fresh model configuration and real FP evidence. Refresh/download/model generation is prohibited in this reuse branch. An existing preview retains the original acquisition/regeneration path. Corrupt, stale, incomplete and wrong-event inputs still fail.

2. **The existing descriptor-free historical models lacked an operational allocation-only entry point.** The ordinary predictor enumerates accepted previews; this unnecessarily prevents using its already fitted P0/D0 baseline without target descriptor ratings. [write_historical_baseline_prior](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/tyre_prediction_prior.py:29) and its [CLI](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/script/prior_tyre_prediction.py) reuse those saved model objects without fitting. They require a preceding source round, compatible model identity, matching nonempty training hash, exact pre-target training rounds, ordered dry compounds and an explicit allocation source. Descriptor-dependent models are rejected. Canonical/versioned output is immutable: equal fingerprints are reused; different existing forecasts are preserved. Uncertainty remains unavailable.

3. **Sparse but legitimate FP evidence crashed on an empty results table.** Genuine partial/single-compound experiments reproduced KeyError: contrast. [fp_tyre_evidence._variant_results / _contrast_records](/Users/zhxutong/dr-wo/woStrategy/src/wostrategy/analysis/fp_tyre_evidence.py:673) now preserve empty table schemas and explicitly report non-estimable contrasts as unsupported with null estimates. The downstream [FP evidence rendering](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/analysis/gui.py:850) now handles null/nonfinite central, interval and optional variant values. Full-data quantitative results are unchanged. No identifiability threshold was relaxed.

4. **Planning unnecessarily required a saved model snapshot.** [OfflineAnalysisService.load_strategy_summary](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/analysis/service.py:220) permits complete, finite Selected scalars to support exact central planning when an aggregate snapshot is absent. It creates no posterior parameters, quantiles or ESS. Existing corrupt/partial snapshots and nonaggregate requests still fail. Partial overrides retain the other Selected coordinates; degradation must remain nonnegative and MEDIUM delta zero. The actual MC loader remains unchanged.

5. **Unused malformed legacy data could reject complete canonical Selected inputs.** [TyreInputService._default_values](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/analysis/tyre_inputs.py:403) now consults legacy only when needed. Malformed legacy still fails when it is the required fallback.

6. **The normal launch command could not select tonight's config.** [live.gui.main](/Users/zhxutong/dr-wo/woPlanner/src/woplanner/live/gui.py:1013) now accepts an explicit event config, initializes its identity, and rejects conflicting explicit year/round values. The separate [race_2026_R18.json](/Users/zhxutong/dr-wo/woWeekend/configs/race_2026_R18.json) supplies 56 laps, no manual tyre overrides, labelled GREEN 20.5 and independent unresolved SC/VSC values. The user's R17/62-lap default config remains untouched.

The existing UI now shows historical-prior provenance and the actual pit-loss source. Only a manifest explicitly identifying joint-weekend-fp-v1 receives the label “Pre-race FP preparation — no Race MC yet”; unknown legacy models are not guessed from lap zero.

Regression coverage is in the new [tyre prior tests](/Users/zhxutong/dr-wo/woStrategy/tests/test_tyre_prediction_prior.py), [materialized FP tests](/Users/zhxutong/dr-wo/woStrategy/tests/test_fp_materialized_contract.py), [prepared Weekend reuse tests](/Users/zhxutong/dr-wo/woWeekend/tests/test_prepared_race_reuse.py), [strategy fallback/UI tests](/Users/zhxutong/dr-wo/woPlanner/tests/test_strategy_prior_only.py), [canonical fallback tests](/Users/zhxutong/dr-wo/woPlanner/tests/test_tyre_canonical_fallback.py), and [launch tests](/Users/zhxutong/dr-wo/woPlanner/tests/test_live_launch_config.py).

## Confirmed R18 tyre data and execution path

The actual consumer path is:

**Genuine FP session caches → optional FP evidence and pre-race joint model preparation; saved historical P0/D0 + verified allocation → canonical R18 prediction → canonical provider → TyreInputService Selected → effective tyre inputs → StrategyModel → exact search → Strategy UI.**

The default live-retro-v1 Race MC is a separate observed-Race-lap fit. Its supported direct/derived coordinates can subsequently inform effective strategy inputs; unsupported coordinates retain the Selected baseline. Changing Selected is not asserted to change that sampler's likelihood or prior.

### Genuine practice evidence

All 1,519 practice laps were reloaded from enriched current traffic-contract caches. The programme inventory contains 270 runs: 13 LongRun, 161 QualiSim, 14 Setting and 82 Uncertain. The strict quantitative subset contains **13 runs / 77 clean laps / 8 teams / 12 drivers**.

| Contrast | Supporting runs | Teams | Clean laps |
|---|---:|---:|---:|
| HARD−MEDIUM | 5 | 3 | 38 |
| SOFT−MEDIUM | 10 | 7 | 58 |
| SOFT−HARD | 11 | 8 | 58 |

The absolute fuel/common-degradation design has rank **3/4**, nullity **1**. Compound degradation contrasts are estimable in the full strict subset; the common additive gauge is not. Direct compound performance is unsupported. These are diagnostic contrast estimates, not independent absolute degradation or driver/team forecasts. Automatic strategy update remains false.

The canonical pre-race model producer was also actually executed: FP1/2/3 contributed **42/139/29 = 210** clean laps under its separate eligibility policy, **100,000 samples**, seed 579288821, and 22 marginal parameter rows. Manifest analysis-7544e618a75cdbc24336 is as-of 0, includes FP1–3 and excludes unavailable Race. These counts differ legitimately from the stricter LongRun diagnostic subset. The joint model's fuel/track interpretation is limited; its marginals are not substituted silently for the Selected historical baseline.

Artifacts now exist under the ordinary [R18 weekend model root](/Users/zhxutong/dr-wo/woData/wostrategy/weekend_model/schema_v1/year=2026/round=18). A separate clean-process owner check returned “reused — valid and fresh with FP evidence”.

### Actual operational forecast

Local processed preview data stop at R17. The R18 allocation **HARD C2 / MEDIUM C3 / SOFT C4** was verified from the [official Formula 1 tyre allocation article](https://www.formula1.com/en/latest/article/what-tyres-will-the-teams-and-drivers-have-for-the-2026-united-states-grand-prix.44LKddYSbUJK1LxMAa6VLI). No descriptor ratings were fabricated.

The producer used the saved source-round17 P0/D0 models trained on rounds1–16. Actual training-data SHA256 is b9ca76ed561c29fab329ccacea1260202277f4389529de1cec9e304a875e3c3f; source model SHA256 is separately d311f86409d82b65b4bc2e658ca46a6ee86509724368fc6d8f418429683fcce1.

| Selected coordinate | Actual value | Unit |
|---|---:|---|
| HARD degradation | 0.0911565829 | s/tyre-age-lap |
| MEDIUM degradation | 0.1450045932 | s/tyre-age-lap |
| SOFT degradation | 0.2085284902 | s/tyre-age-lap |
| HARD−MEDIUM performance | +0.2699040109 | s |
| SOFT−MEDIUM performance | −0.3181921688 | s |
| MEDIUM performance baseline | 0 | s |

The [canonical R18 prediction](/Users/zhxutong/dr-wo/woData/wostrategy/tyre_prediction/schema_v1/year=2026/predictions/round=18/prediction.json) has fingerprint d6bdb1d17b73597241a35a45dc88a83a2949139b94dfc78db20b4556e539c84c. It declares prior_only, independence from current FP, no automatic FP update, no target descriptors and null uncertainties. HARD-relative source performance is normalized once to MEDIUM=0 by the established provider.

All five values and independence flags survived the real provider/Planner boundary. The actual Qt Calculate callback froze exactly these Selected values. The displayed [tyre inputs](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/r18-strategy/tyre_inputs.png) and [calculation evidence](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/r18-strategy/evidence.json) establish consumption, not just display.

### Reduced-data matrix

Controlled subsets contain genuine R18 rows; none are represented as complete historical sessions.

| Case | Actual result |
|---|---|
| Full FP1–3 | 13 strict runs / 77 laps; rank 3/4; all three diagnostic contrasts estimable |
| Partial FP1+FP2 | 8 runs / 45 laps; rank 2/4; only SOFT−HARD estimable, median−0.04334 s/lap, P10–P90−0.16740…+0.08072; other contrasts unsupported |
| One genuine sparse run | 1 run / 2 laps; rank 1/4; all contrasts unsupported/null |
| MEDIUM-only | 2 runs / 19 laps; rank 1/4; no compound contrasts |
| Supported prior_only | Canonical reload and actual strategy calculation succeed; no posterior uncertainty |
| Cache present, upstream unavailable | Injected acquisition failure is never called; all three cache files are reused |
| Entirely absent input | Expected ValueError; no output |
| Invalid traffic provenance | Expected ValueError before quantitative processing; no output |

Missing optional evidence does not invalidate the supported Selected prior. Invalid required inputs remain errors. Full details are preserved in the [matrix evidence](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/reduced-matrix.json).

## Strategy, Weekend and live evidence

### R18 pre-race exact search and actual GUI

Using the new 56-lap config and GREEN 20.5 assumption:

| Stops | Model's best sequence and pit laps | Incremental tyre/pit cost |
|---|---|---:|
| 1 | H33 → M23; pit33 | 120.566943 s |
| 2 | H20 → H21 → M15; pits20,41 | 109.666669 s |
| 3 | H15 → H15 → H15 → M11; pits15,30,45 | 116.032353 s |

Ten distinct compound-sequence results reached the actual Qt table. All stints are positive, sum to56, and obey the configured minimum-two-dry-compound rule. These are central objective costs, not total race times, outcome probabilities or confidence intervals. ESS is inapplicable to this exact search.

**Set inventory is not represented by this optimiser.** The unconstrained three-stop optimum requests three HARD sets and must not be treated as an inventory-validated race plan. A supplementary fixed H16/H16/M12/M12 sequence also calculates finitely at117.552235 s, pits16/32/44; actual available sets and starting ages still require operational selection. This review did not add an inventory model.

Manual override regressions verify global/team scope, signed deltas, partial override merging and immutable calculation snapshots. Existing independent GREEN/SC/VSC handling and explicit VSC overrides pass. No R16 combined pit loss was promoted into R18 and no SC/VSC discount was invented.

### Ordinary Weekend CLI

The genuine command entry, woweekend.cli.race, completed with **17/17 SUCCESS components**, including prerequisites, tyre prediction, FP model/evidence, exact strategies, distance and report generation. Outbound HTTP requests were denied; cached real event metadata remained usable.

The controlled config preserves R18/56laps/Selected/GREEN 20.5 and only bounds the optional cutoff scan to .02… .30 with step.07/refinement.01, uses one historical VSC lookback year, and requests an English engineering report. This is a functional cutoff smoke, not a cutoff calibration. Warnings accurately retain unavailable interruption history and no crossing above the explicit scan minimum.

The immutable run is [20261025T171132.186508Z-25b7baae3e](/Users/zhxutong/dr-wo/woData/woweekend/runs/schema_v1/event=2026-18/workflow=race_preparation/runs/20261025T171132.186508Z-25b7baae3e). Its [engineering report bundle](/Users/zhxutong/dr-wo/woData/woweekend/runs/schema_v1/event=2026-18/workflow=race_preparation/runs/20261025T171132.186508Z-25b7baae3e/report_bundle.en-GB.engineering) and [CLI evidence/config differences](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/B-weekend-audit/evidence.json) are retained.

### Native historical Race MC → live strategy

The final fresh-process preflight ingested the exact genuine **R17** recording prefix through ingress sequence 39650 / leader lap 29. Event identity remains R17/62laps; original bytes, message sequences and source/receipt timestamps are preserved.

| Checkpoint | Eligible Race laps | Actual samples | ESS |
|---|---:|---:|---:|
| L14 | 16 | 512 | 508.55 |
| L27 | 100 | 512 | 489.33 |
| L28 | 107 | 512 | 486.54 |
| L29 | 115 | 512 | 481.73 |

There were 16 fresh MC publications, 569 completed-lap records, 15 pit-flagged records linked to racing pit stops, empty telemetry queues and no worker error. The spawned worker was separate from the UI. Automatic traffic used the existing sector-boundary fallback and retained its certification/clean-run limitations.

At L29 the actual Strategy Calculate callback consumed fresh model V29 plus VER's observed SOFT/age 28 state at completed lap 29. Ten finite continuations rendered; best S→M at pit46, remaining central cost46.148460 s under that historical what-if UI's 20s pit assumption. The frozen source ledger identifies MEDIUM/SOFT degradation as live_direct, HARD degradation as live_derived, SOFT performance as race_evidence and unsupported HARD performance as prior. This proves MC-to-calculation consumption.

The fresh replay initially had **zero posterior parameters**, no copied model manifest/parameter history and no ephemeral posterior. Unproven old manual overrides were omitted only from the isolated copy. The canonical R17 baseline was generated 10 October, trained through R16, before the first 11 October Race packet. Only progressively received prefix laps generated Race posterior updates. Cached MC policy/settings are reused for an integration test, not claimed as a prospective accuracy holdout.

The [final replay evidence](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/historical-race/r17_replay/evidence.json), [initial-state proof](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/historical-race/r17_replay/initial_model_evidence.json), and [fresh strategy proof](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/historical-race/r17_replay/fresh_mc_strategy.json) retain exact provenance and assertions.

High smoke-test ESS establishes numerical execution, not model accuracy. Fuel/track aliasing remains diagnosed. Before tonight's actual clean Race laps, waiting for Race MC is expected behaviour.

### Actual main entry startup

A separate clean process called the real live module main with the explicit R18 config, entered the Qt event loop, and verified year2026, round18, sessionR, distance56, idle connection and enabled Strategy control before closing. No future feed was connected. [Startup evidence and screenshot](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-live-entry/evidence.json).

## Tests, independent review and preservation

Final coordinator selections comprise **499 distinct passed tests, 3 skipped, 0 failed**:

| Selection | Result | Evidence |
|---|---|---|
| Planner: live/UI, Selected/manual/import, service/worker, launch/fallback and focused Q reliability | 258 passed | [log](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/planner-final-tests.log) |
| Planner: separate VSC override contract | 3 passed | [log](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/planner-vsc-tests.log) |
| Genuine R18 Q fixture regression | 14 passed | [log](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/quali-r18-final-tests.log) |
| Strategy: priors, provider, FP contracts/evidence, FastF1 compatibility, Race MC, exact search and Q forecast | 119 passed, 3 skipped | [log](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/strategy-final-tests.log) |
| Weekend: config, acquisition/reuse, race workflow, FP temperature, live replay and VSC | 105 passed | [log](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/weekend-final-tests.log) |

The three pre-existing Strategy skips depend on local R11 caches requiring current traffic-contract recomputation. They are not R18 failures. Controlled contract tests and genuine R18 tests passed. Exact file selections/environment are recorded in [test-selections.json](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/test-selections.json). Agent reruns overlap these selections and are not added to the total.

The final combined clean-process check passed all three jobs: R18 local/spawned-worker preflight, actual R18 strategy UI, and native historical Race MC/UI/strategy. Its machine verdict is appropriately CONDITIONAL: [readiness.json](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-final-verified/readiness.json).

Independent review caught and resolved partial-model masking, partial-override loss, flattened provenance loss, the sparse GUI null consumer and a potential change to accepted-preview regeneration. The [independent review](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/independent-release-review.md) and scope inventories are preserved.

Strategy/Planner package builds and extracted-wheel imports verified inclusion of the frozen Q prior, auth-worker/preflight modules and the new prior helper/CLI without editable-import masking. Those review wheels predate the final small schema/rendering fixes and were neither installed nor distributed. Rebuild curated packages before distribution.

No Qualifying timing, classification, fitting threshold, cutoff formula, Race likelihood or strategy objective was changed by this review. Focused Q tests and the 496-message genuine R18 fixture pass. The original 12,026-message Q recording still hashes 533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17.

Only previously absent R18 FP-cache/model directories were published, with byte/hash checks and preserved cache mtimes; the prior producer wrote previously absent canonical/versioned R18 outputs. Original source models, recordings, authentication and user changes were retained. The [publication inventory](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/published-artifacts.json) records all 25 prepared files. This review did not reinstall dependencies.

## Repeatable pre-race procedure

Use the existing production interpreter. From the Planner directory, run a fresh output location; the preflight deliberately refuses an existing run directory.

~~~sh
cd /Users/zhxutong/dr-wo/woPlanner
/Users/zhxutong/dr-wo/.venv/bin/python scripts/preflight_r18_readiness.py \
  --data-root /Users/zhxutong/dr-wo/woData \
  --event-config /Users/zhxutong/dr-wo/woWeekend/configs/race_2026_R18.json \
  --output-root outputs/r18_race_readiness_20261025/operator-preflight-01
~~~

This uses existing prepared R18 artifacts and a bounded 512-sample historical replay. It neither redownloads FP nor changes the production Race MC budget. Actual effective live settings currently resolve to 80,000 samples; the separate pre-race FP preparation used 100,000.

Launch tonight's normal visible application:

~~~sh
/Users/zhxutong/dr-wo/.venv/bin/python -m woplanner.live.gui \
  --event-config /Users/zhxutong/dr-wo/woWeekend/configs/race_2026_R18.json
~~~

The initial controls should show 2026/18/R. Open Strategy to inspect Selected and calculate. Before clean Race evidence, the FP-preparation label and waiting Race MC state are expected. Use the existing operational controls for any measured pit-loss update and the intended manual tyre scope.

The ordinary Weekend command can also consume the prepared event:

~~~sh
/Users/zhxutong/dr-wo/.venv/bin/python -m woweekend.cli.race \
  --event 2026-18 \
  --config /Users/zhxutong/dr-wo/woWeekend/configs/race_2026_R18.json
~~~

The full default command performs its configured report/context/cutoff work. To repeat the bounded report smoke instead, use the preserved [bounded config](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/B-weekend-audit/bounded-r18.json) as the config argument. The tested HTTP-denial wrapper and original invocation are retained alongside it; the normal launch above does not disable networking.

To recompute/revalidate the allocation-only prior idempotently without target descriptors or refitting:

~~~sh
/Users/zhxutong/dr-wo/.venv/bin/python -m wostrategy.script.prior_tyre_prediction \
  --year 2026 --round 18 --source-round 17 \
  --event 'United States Grand Prix' --circuit 'Circuit of the Americas' \
  --hard C2 --medium C3 --soft C4 \
  --allocation-source-url 'https://www.formula1.com/en/latest/article/what-tyres-will-the-teams-and-drivers-have-for-the-2026-united-states-grand-prix.44LKddYSbUJK1LxMAa6VLI' \
  --data-root /Users/zhxutong/dr-wo/woData
~~~

Do not replace these with the old default race config, which remains the user's R17/62-lap configuration.

## Git inventory and integration recommendation

No commit, staging, merge, push, rebase, reset or branch switch was performed. HEADs and branches are unchanged: Planner e114216, Strategy57a0a6f, Data7881cbb, Weekendb634005, Standingb22e20d on main; local Fast-F1 fork47f2725 on master. Production uses installed FastF1 3.9.0, not an edited fork.

| Scope | Release-related paths |
|---|---|
| This Race review — Planner | analysis/service.py, analysis/tyre_inputs.py, analysis/gui.py, live/gui.py; three preflight_r18 scripts; test_strategy_prior_only.py, test_tyre_canonical_fallback.py, test_live_launch_config.py; this report |
| This Race review — Strategy | analysis/tyre_prediction_prior.py, script/prior_tyre_prediction.py, analysis/pre_race_tyre_prediction.py, analysis/fp_tyre_evidence.py; test_tyre_prediction_prior.py and test_fp_materialized_contract.py |
| This Race review — Weekend | workflows/race_prerequisites.py, tests/test_prepared_race_reuse.py, configs/race_2026_R18.json |
| Earlier reviewed Q release — Planner | Existing tracked quali-module/documentation changes; new fp_access.py, fp_worker.py, preflight.py, q_only_track.py; Q tests, genuine R18 fixture, audit/evaluation scripts and the three Q reports |
| Earlier reviewed Q release — Strategy/Data | FastF1 dependency constraints in both pyproject.toml files; Strategy quali_forecast.py and its frozen JSON prior; FastF1/Q forecast tests |

Use the [full current tracked inventory](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/final-git-inventory.json) and [explicit untracked candidates](/Users/zhxutong/dr-wo/woPlanner/outputs/r18_race_readiness_20261025/coordinator-evidence/relevant-untracked-candidates.json) when preparing integration. Do not stage broad directories.

**Unrelated pre-existing changes to exclude:** Weekend configs/race.json and configs/post_race.json; the pre-existing blank-line-only race.py change; Strategy script/tyre_strategy_summary.py default and research/traffic_position_part1/README.md; Data script/fetch_replay_data.py default; Standing script/points_progression.py. Their seven protected hashes remain identical to the investigation baseline. Preserve existing research, outputs, migration/worktree folders and other untracked material; raw recordings, local auth, generated data, snapshot wheels and diagnostic output trees are not production source commits.

**Recommendation:** the reviewed source changes are suitable for a curated integration into main with the stated AMBER assumptions. Rebuild packages from that curated source rather than publishing the whole dirty working tree or the earlier snapshot wheels. Do not fold unrelated user edits into the release.

## Remaining limitations and next-prompt handoff

There is no accessible evidence blocker left for local usability. Remaining evidence is operational or statistical: measured Austin GREEN/SC/VSC losses, actual available tyre sets/ages, tonight's feed health and Race clean laps, and any historical log identifying the earlier tyre-prediction failure. The absent descriptor preview limits the forecast to the explicit historical baseline. No genuine R18 Race outcome or future Race posterior was used.

In this same conversation, the next implementation/integration prompt can:

1. Recheck current diffs against the preserved inventory and keep all seven unrelated edits separate.
2. Integrate the reviewed Q and Race source/tests/config paths explicitly, retaining the frozen Q resource and new prior CLI.
3. Rebuild curated packages and repeat the clean-process preflight from the selected production environment.
4. Keep the prepared R18 local data/auth outside Git and launch using the explicit R18 config.
5. Address measured pit losses and tyre-set availability through existing controls; defer statistical redesign and any inventory feature to separately scoped work.

No transfer of agent findings or reconstruction of this investigation is required.
