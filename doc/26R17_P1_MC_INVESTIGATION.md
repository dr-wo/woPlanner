# 26R17 DEV-01 — P1 Monte Carlo filter investigation

Follow-up: the user resolved the tactical definition and the manual reference is
now implemented. See [implementation and validation](26R17_P1_REFERENCE.md).
The investigation below is retained as the original evidence record.

Original investigation status, 2026-10-18 (**superseded by the implemented manual
P1 reference**): the domain definition was deferred and no production P1 filter
or eligibility indicator had yet been added. At that investigation stage,
existing MC, clean-lap policy, saved models, sampling order and strategy
calculations were unchanged by DEV-01.

## What the current MC actually models

The production live path is `OfflineAnalysisService.recalculate_from_live_snapshot`
in `src/woplanner/analysis/service.py`, calling woStrategy's
`run_live_retro_model` and `MonteCarloRacePerformanceAlgorithm`.

- Each candidate samples fuel rate, track evolution, compound degradation,
  non-reference compound offsets and team/compound degradation variations.
  Driver or team baseline intercepts are profiled for each candidate.
- Candidate costs and weights are computed against the selected observed Race
  laps. The output contains parameter samples, fitted baselines and weighted
  marginal summaries. A sample is **not a simulated race trajectory**, predicted
  overtaking sequence or leader-change history.
- P1 has the ordinary race meaning: published position 1. Leader-lap helpers use
  completed laps with `position == 1`. Nothing in this architecture establishes
  a different meaning or a P1-specific sampling predicate.
- Live strategy search consumes resolved tyre inputs and enumerates strategy
  costs; it does not supply per-MC-candidate physical position trajectories.

The existing input filters select valid, dry, green, quick, non-pit laps with
supported traffic gaps, optional stint-edge exclusions and a minimum consecutive
clean run. Default gap settings are ahead >2.5 s and behind >1 s; the default
consecutive-run requirement is four laps. These are existing rules, not proposed
P1 or yo-yo criteria.

The default live traffic path is deliberately `traffic-unavailable`, retaining
the prior model. Sector-boundary traffic is an explicit alternative. Position.z
history and reconstructed physical traffic are diagnostic-only and are not
inputs to production clean-lap selection. See woStrategy's
`doc/RACE_MC_AND_FP_ARCHITECTURE.md` and `doc/traffic_evaluation.md`.

## Existing yo-yo terminology

No production yo-yo predicate was found in woPlanner, woStrategy or woWeekend.
There is a related exploratory rear-filter study:

- `woStrategy/research/rear_filter_part2_20261003/descriptive_protocol.json`
- `woStrategy/research/rear_filter_part2_20261003/REPORT.md`

That study describes exchanges and alternating swings in observed five-second
rear-headway medians, with explicit identity, ordering, coverage and gap-support
requirements. Its tolerances are explicitly **exploratory observability criteria,
not validated production tolerances**. Its conclusion is to retain the current
policy: added evidence was confounded by compound support and an altered
effective prior, with no matched independent validation. It does not define a
race-wide non-yo-yo flag, a P1 exception or an MC-sample filter. Promoting those
thresholds here would incorrectly present research choices as established logic.

Stable published rank by itself cannot establish quiet driving: a car can vary
its gap or defend without losing its position. Conversely, absent observations
cannot certify a non-yo-yo interval. Available position samples could support a
future observed-interaction diagnostic, but do not establish a validated decision
rule or uncertainty/coverage policy.

## Actual R17 L12–L14 evidence

Local inputs, relative to `woData`:

- `wodata/live_timing/schema_v1/year=2026/round=17/session=R/live_timing_raw.jsonl`
- The adjacent `live_timing_raw_2.jsonl` is a short recording and contains no
  observed completed-leader L12–L14 cut when replayed independently.
- `wostrategy/planner_race_laps/schema_v2/year=2026/round=17/session=R/laps.pkl`
- `wostrategy/race_performance_review/schema_v1/year=2026/round=17/session=R/`

The main recording was reduced in original delivery order with
`LiveSessionStore.reduce`, retaining the recorded source timestamp, receive
timestamp, session coordinate and receipt-trust field. All topics, including
CarData.z and Position.z, were retained. The cuts below are the first recorded
message where a completed P1 lap reaches the specified lap. They exclude later
messages and corrections. `_live_analysis_laps` was evaluated with explicit
sector traffic, quick threshold 1.1, four consecutive laps, standard dry
compounds, no edge exclusions and the existing 2.5 s / 1 s gap settings. This is
an explicit alternative-path audit, not a claim that default traffic is enabled.

| Leader lap | Ingress sequence | Source time (UTC, 2026-10-11) | Leader / lap time | Completed laps | Selected clean laps, all drivers | Selected VER laps |
|---|---:|---|---|---:|---:|---:|
| 12 | 17612 | 12:23:40.857 | VER / 97.505 s | 217 | 0 | 0 |
| 13 | 18910 | 12:25:18.365 | VER / 97.426 s | 238 | 0 | 0 |
| 14 | 20206 | 12:26:55.570 | VER / 97.197 s | 259 | 16 | 0 |

At the L14 cut, VER's L12 and L13 are individually clean under the existing
sector rule (rear gaps 1.342 and 1.586 s), but cannot form a four-lap run. The
freshly published L14 has no supported sector gap yet at that exact delivery
cut. VER's L3–L11 sector rear gaps are 0.320–0.925 s; L8/L9 additionally fail
the green condition, and L9 also fails the quick condition. These observations
make a P1 rear-filter exception a plausible *interpretation* of the original
request, but do not prove that this is the intended selection rule or that the
laps are unaffected by defensive driving.

The main prefix retained 100,892 / 100,980 / 100,914 position samples at the
three cuts, respectively. Presence of samples is not a coverage certificate or
proof that any non-yo-yo gate passes.

The cached retrospective lap data also identify VER as P1 on all three laps.
The whole-race retrospective clean-lap output already contains VER L12–L14,
with rear-gap means 1.403 / 1.881 / 1.948 s. Those values use different evidence
and a later information cut; they are not prefix-live measurements. This cache
cannot justify retroactively admitting evidence at L12 or replacing the live
gap estimator. No cache or persisted model was changed during the audit.

## Smallest clarification needed

**Should the P1 analysis admit the current leader's observed laps despite the
rear-gap rule, and which observed behaviour over what interval makes those laps
yo-yo/ineligible?**

For example, repeated position exchanges and repeated gap oscillation describe
different behaviours. A labelled eligible/ineligible lap sequence plus its
intended observation interval can resolve the intended behaviour without asking
for an arbitrary numerical threshold. If the request instead means conditioning
existing MC candidates, the event/property to condition on must be specified;
the existing candidates have no leader trajectories to test.

This is the remaining domain question, not an implementation-permission request.
DEV-02 can ship independently. There is intentionally no disabled/stub P1 control
that could suggest an analysis exists before its mathematical contract is known.

## Statistical constraints for a later implementation

Reuse existing samples only when they represent the same candidate space and
proposal. A diagnostic projection for the current leader can retain original
weights; it does not become a leader-only fitted posterior. A new observation
selection requires appropriate cost evaluation and baseline profiling. Simply
discarding other drivers' output rows does not refit the likelihood.

For a genuine specified conditioning event, preserve sample IDs and original
results, normalize retained nonnegative weights separately, and report retained
count, retained original probability mass, ESS (`1 / sum(normalized_weight**2)`),
weight concentration, evidence support and unavailable/empty selection states.
Do not treat a high ESS as proof of informative data, fabricate uniform weights
for an unsupported filtered result, or multiply a likelihood contribution twice.
Adding a previously unseen compound can alter candidate dimensions and ordered
priors; identical seeds alone do not establish comparable samples.

The current live model already exposes sample count, ESS/fraction, maximum/top-five
weight, best/weighted RMSE, observed compounds, clean laps/runs and identifiability
diagnostics. Any future P1 result must remain distinctly labelled and must not
overwrite those standard results or silently feed strategy calculations.

## Validation executed

The read-only audit is preserved in
[`scripts/audit_r17_p1_mc.py`](../scripts/audit_r17_p1_mc.py), with the verified
eight-record output in
[`doc/assets/26r17_p1_mc_audit.jsonl`](assets/26r17_p1_mc_audit.jsonl).
Unavailable numeric gaps are encoded as JSON `null`; they are not filled or
treated as clean. The script defaults to the sibling `woData` directory and
accepts `--data-root` for another location. To reproduce from `woPlanner`:

```sh
MPLCONFIGDIR=/private/tmp/r17-mpl-cache ../.venv/bin/python scripts/audit_r17_p1_mc.py --data-root ../woData > /private/tmp/26r17_p1_mc_audit.jsonl
```

From `woStrategy`, using the shared project environment:

```sh
MPLCONFIGDIR=/private/tmp/r17-mpl-cache /Users/zhxutong/dr-wo/.venv/bin/python -m pytest tests/test_live_retro_performance_model.py tests/test_race_performance_acceleration.py tests/test_sampling.py -q
```

Result: **14 passed in 15.69 s**. Coverage includes the established live-retro
contract, fixed-seed accelerated/legacy evaluator agreement and sampler behavior.
An initial invocation with the repository-local `.venv` could not collect because
`wodata` was not installed there; rerunning in the shared editable-project
environment resolved that environment issue.

No new filter tests were added because no filter was implemented. The R17 prefix
audit is evidence about existing behavior, not independent validation of a new
P1/yo-yo policy.
