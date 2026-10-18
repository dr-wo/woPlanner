# Q17 P3 test-quality follow-up

Status: READY FOR FOCUSED TEST REVIEW. Test-only; no merge or push.

Branch: `codex/q17-test-quality-followup`
Worktree: `/Users/zhxutong/dr-wo/q17-worktrees/test-quality/woPlanner`
Parent: `12aacb0392a422eb57440ff13300326a5005c641`

## P3-1: real consume, fitting, revocation and publication

The earlier combined integration test calls the policy `candidate()` helper,
which begins publication, considers the second candidate and refreshes the fit
before the consume that withdraws C's calibration evidence. That remains a valid
coexistence control but cannot establish that fitting happens inside that consume.

`tests/test_quali_real_consume_publication.py` adds eight permanent cases. An
independent driver is introduced through DriverList, TimingAppData and TimingData.
Its first completed push yields an earlier fallback snapshot. Its second tyre
installation is staged, but the second completed lap is absent from the policy.
A single real TimingData packet then both completes that second lap and retracts
C's old completion. Production `store.apply()` generates the completion and exact
invalidation identities; production `service.consume()` processes both together.
No direct policy admission, refresh, synthetic completion or snapshot replacement
is used in this new test.

Spies observe the guard and exact sequence: begin, physical ingest, admission,
finish, one effective fit, final snapshot. The diagnostic stays unchanged through
ingest/admission; any intermediate build must retain the old diagnostic and omit
the compact result. After finish, the canonical slope and selected automatic/cutoff
rate are -0.1 s/global push, the compact payload agrees, and the old calibration
identity has exactly one lifetime revocation. An earlier snapshot is compared in
full with its deep copy after subsequent consume and timer activity.

Archive replay and genuine FastF1LiveTimingSource receipt provenance each cover
+2, +3, +4 and +4.1 seconds. Explicit finalisation records prove normal +2 and
exceptional grace +3/+4; +4 is inclusive and +4.1 retains the historical normal
result. Pending traffic closes at hard lock. The physical target locks to 1000 m
for in-window withdrawal and 1100 m for post-lock withdrawal. Later withdrawal
and duplicate delivery cannot reopen traffic or append finalisation records.
Duplicate consume gets its accepted single begin/finish envelope but performs
zero effective fits or new admissions/revocations. Timer finalisation has no
consume begin/finish envelope. Canonical admission/fitting remains independent
of physical traffic eligibility and grace deadlines.

Read sources: the accepted integration review in
`q17-integration/core-promotion-20261018/evidence/independent_integration_review.txt`
and independent `REPORT.txt`, `probes.py`, `probes.json` under
`/private/tmp/q17-independent-20261018-review`. The permanent test strengthens the
preserved probes by sourcing both candidates through the reducer as well as service.

## P3-2: credible historical negative controls

The original harness and retained `negative_control.txt`, `source_audit.json`,
and `baseline_quali_reducer.py` were inspected under
`q17-integration/core-promotion-20261018/inputs/core/candidate/evidence/c3_partial_chronology`.
The preserved reducer hashes to
`5fa63156b82add28e0b9c69ded36631b362071c198027fd93cb681b749092bdd`,
matching the original source audit's before-reducer hash. No obsolete source is
committed or modified.

The original 20-case selection was freshly reproduced with the parent commit's
unmodified partial-chronology tests in an isolated external directory:

| Control | Business assertion failures | Private field incompatibility | Collection/setup errors | Pass | Skip | Deselected |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Original tests / verified old reducer | 14 | 4 | 0 | 2 | 0 | 36 |
| Corrected tests / same verified old reducer | 18 | 0 | 0 | 2 | 0 | 36 |
| Corrected tests / pinned accepted reducer | 0 | 0 | 0 | 20 | 0 | 36 |

The four original `_race_control_undated` AttributeErrors are implementation
incompatibility, not independent proof of business defects. Only the cache-empty
assertion in the delivery-order equivalence test is replaced, with externally
observable Q3 and normal-finish checks in reducer/service snapshots. Existing
ownership, downstream ordinary/final-lap and schedule equivalence assertions stay.

On the old reducer those four tests now reach a later schedule and fail an existing
business assertion: before delayed Q3 metadata arrives, CHEQUERED identity
`('2', 'CHEQUERED')` is incorrectly owned by Q2. The other 14 failures concern
coarse closure precision and overlapping/undated predecessor ownership. The two
preservation passes are the transition=351 closure-enrichment cases. This is an
observed continuation after removing the incompatible assertion, not a rebranding
of the four historical AttributeErrors.

`tests/review_support/q17_partial_chronology_control.py` is a standalone external
execution harness. It hash-verifies the preserved baseline, loads it under a unique
module name in a fresh interpreter, patches only the reducer export before test
collection, and emits per-node categories and provenance. It neither edits nor
installs production source. Nonzero exit is expected for the defective control.
The JSON evidence retains individual failure details for original and corrected
runs. No missing or invented defective source is needed. These results apply to
this one preserved version and 20-case selection; other historical variants are
not inferred. Other private-field assertions that specifically test internal
retention mechanics remain outside this narrowly corrected control.

## Validation and provenance

Current pinned runtime: **792 distinct passed, 0 failed, 0 errors, 0 skipped**:
Planner 541; Data 190; Strategy 61. The final standalone 8-case run and 20-case
accepted preservation control overlap the 541 Planner cases and are not added.
The old baseline's expected 18 failures are reported separately above.

Planner source is the dedicated worktree, with all production bytes unchanged
from the parent. Read-only dependency roots are
`/Users/zhxutong/dr-wo/q17-release/controlled-20261018/{woData,woStrategy}`:

- woData: `7580ccb614d2d7d2173ac6c9fc91409bfb338559`.
- woStrategy: `336098b146c93c53b46b998ba62416aadcc0391d`.

All 125 actual project modules loaded by the final Planner run were checked
against these roots and source hashes. Detailed paths, source inventory digests,
critical source hashes, exact expanded Planner command, all result logs and
per-node negative-control classifications are in
[`assets/q17_test_quality_validation.json`](assets/q17_test_quality_validation.json).

The tests' unchanged R17 import-time path initially produced one collection error
in the first control attempt. This is setup evidence, not a business failure.
A verification-only directory at `/private/tmp/q17-test-quality/verification`
contains byte-identical test copies with adjacent read-only resource links, as in
the accepted Controlled Merge recovery. Imports still use the new Planner source
and pinned Data/Strategy. R17 recording SHA-256 is
`8d084328530605780c74035c13d6ad339e0a5e6270f7c535c6761e4683012388`.
No source-layout or assertion bypass, affected-test skip, or full historical replay
was used. The existing final-review recorded-message sample ran as part of the
selected suite. An initial launcher preflight also caught a nonexistent suite
filename before tests started; the actual final suite is explicitly recorded.

Commands use the Python 3.11 environment `/Users/zhxutong/dr-wo/.venv/bin/python`,
`-B`, `PYTHONDONTWRITEBYTECODE=1`, the three pinned `src` paths in `PYTHONPATH`,
`QT_QPA_PLATFORM=offscreen`, and external `MPLCONFIGDIR`. The evidence embeds the
exact environment wrapper. Planner's expanded module list is recorded in JSON.
Data command arguments:

```text
-m pytest tests/test_quali_foundation.py tests/test_quali_reducer.py tests/test_quali_projection_ownership_repair.py tests/test_quali_causal_speed_history.py tests/test_quali_speed_coordinate_domains.py tests/test_quali_reducer_projection_identity.py tests/test_publication_time_contract.py tests/test_replay_receipt_provenance_corrections.py tests/test_recovery_publication_identity.py tests/test_live_controller.py -q --tb=short -p no:cacheprovider --junitxml=/private/tmp/q17-test-quality/data.xml
```

Strategy command arguments:

```text
-m pytest tests/test_quali_traffic_phase_validity.py tests/test_traffic_evaluator.py tests/test_pre_quali.py tests/test_traffic_review_remediation.py tests/test_position_traffic.py -q --tb=short -p no:cacheprovider --junitxml=/private/tmp/q17-test-quality/strategy.xml
```

Historical control, after copying tests/helpers into the external layout:

```text
/private/tmp/q17-test-quality/python /private/tmp/q17-test-quality/verification/woPlanner/tests/review_support/q17_partial_chronology_control.py --reducer /Users/zhxutong/dr-wo/q17-integration/core-promotion-20261018/inputs/core/candidate/evidence/c3_partial_chronology/baseline_quali_reducer.py --output /private/tmp/q17-test-quality/negative-control.json
```

Omit `--reducer` for the accepted preservation control. The original-test
reproduction substitutes the parent commit's original test file in a separate
`original-verification` directory; no application source is changed.

## Isolation and diff inventory

Changed paths are exactly:

- `tests/test_quali_real_consume_publication.py`
- `tests/test_quali_partial_chronology.py`
- `tests/review_support/q17_partial_chronology_control.py`
- `doc/QUALIFYING_Q17_TEST_QUALITY.md`
- `doc/assets/q17_test_quality_validation.json`

No production file, dependency source, algorithm, policy, GUI, or model changes.
Main is not merged, pushed or deployed. Before/after audit covers 18 other existing
worktrees: 17 retain identical HEAD/branch/status/production hashes. The separate
active Q17-04 worktree changed concurrently from its pre-existing dirty state to
commit `d9c25d7109b050c13fad1ffdf398987f09579031`, with its production source hashes
identical to the initial capture. This task never writes to that worktree; the
concurrent commit is reported rather than claiming all worktrees were static.
All captured main/shared/candidate worktrees are preserved. The final feature
commit ID is provided by the delivery report and can be recovered from this branch.
