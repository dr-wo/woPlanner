# R16-Q final two closure blockers — 2026-10-04

Both blockers are repaired. Production changes are confined to `woData/src/wodata/live/quali_reducer.py` and `woData/src/wodata/live/recovery.py`. The established message/publication contract and all woPlanner production source remain unchanged.

## A. Stale alias repair

The source-unknown early return updated `_projection_best_event_key` but left `_projection_official_lap` active. A later Position-only message could therefore use that retained current alias to recover a historical event, although the live best metadata was unproven and could name another alias.

Any explicit source-unknown `BestLapTime` or `BestLap` update now clears both the current best key and its current alias-recovery token, even when the numerical best is unchanged. The live register still receives its patch. Historical `QualiCompletedLap.official_lap_number`, accepted event keys, and sector ownership remain intact. Later positively identified publication metadata can still recover A or B through original-publication or explicit known-alias reconciliation. Unrelated Position/timing updates cannot do so.

No extra provenance clock, identity model, or permanent disable flag was introduced. A source-unknown checkpoint containing no best update keeps an already-certified reference available.

Reviewed negative controls cover one and two events through `apply`, whole-stream `apply_many`, controller FIFO, and the actual timestamped/Subscribe-result adapter. Positive controls cover original A/B publication recovery, timestamped explicit known-alias recovery, value-only/same-alias/alias-only/clear patches, and current unknown checkpoints. Historical A/B aliases and sectors are asserted unchanged.

The captured baseline reproduced 108 s automatic recovery in the one-event case and false 86.4 s recovery in the two-event case. Both are now unresolved and unprojected after the Position-only message.

## B. Recovery deduplication repair

Old fingerprint: `(topic, payload)`. The envelope merge also dropped adjacent equal fingerprints inside a single capture. This discarded distinct lifecycle publications even when their source times differed.

The new rule is:

| Input evidence | Recovery identity | Preservation/dedup policy |
|---|---|---|
| Genuine source publication available | Topic + payload + typed `PublicationTime(coordinate, seconds)` | Different source coordinates survive, including numerically equal UTC/session coordinates of different types. Playback time, receive time, and local sequence do not identify the publication. |
| Unknown source, complete recorded envelope | Fingerprint of the original JSON envelope, excluding `playback_time_seconds`; requires an actually recorded parseable receive timestamp and integer sequence | Exact copies across recovery inputs can overlap. This is evidence of a recorded-envelope copy, **not** a historical publication coordinate. Distinct receive/sequence/content metadata remain separate. |
| Unknown source without complete recorded metadata | Resolved recording path + physical source-row index | Only a repeated recovery input of that physical record can overlap. Equal content in separate files is retained; synthesized receive/sequence values do not prove a duplicate. |

The merge retains the maximum observed multiplicity per identity across inputs. Repetitions within any one capture are preserved, including two identical unknown envelopes; overlapping copies of those same records do not multiply them. This preserves lifecycle observations without disabling duplicate-source recovery. The existing receive-order merge and legacy/mixed phase ordering are unchanged.

Recovery cache schema is now **3**, so a schema-2 canonical cache produced by content-only dedup is rebuilt even when its input files have not changed. The replay file schema, publication fields, scheduler, canonical writer, and JSON serialization are unchanged.

Coverage includes overlapping fragments, duplicated sources, nonadjacent duplicate copies, known and unknown publications, legacy/v2/mixed inputs, equal content at different source coordinates, playback-only differences, incomplete/invalid recorded arrival metadata, and old-cache invalidation. Within-capture duplicate deliveries are conservatively retained; the task permits deduplication of one publication but does not require deleting repeated observed lifecycle transitions.

## C. Closure matrix

| Finding | Closed? | Evidence |
|---|---|---|
| Stale alias recovery | Yes | One-/two-event adapter/controller and direct/batch cases remain unresolved after unknown best state and Position-only delivery; no false 86.4 s projection. |
| Recovery drops distinct publication | Yes | Both known clears survive; unknown and later known clear remain distinct; both reviewed end-to-end outcomes match direct playback. |
| Recovery preserves true duplicate dedup | Yes | Overlapping and duplicate inputs retain one copy per observed occurrence; R16 duplicate captures reduce 32,992 inputs to 16,496 messages. |
| Unknown-origin live projection still available | Yes | Unknown current 24 s checkpoint against certified A yields 108 s; prior 112.5 s availability control also remains passing. |
| All prior publication blockers retained | Yes | Existing lifecycle matrix, publication-time separation, part fences, contradictory source precedence, repeated accepted values, identity/version-skew, bound-once and old-model tests are unchanged and pass. |

## D. End-to-end recovery evidence

All cases exercise `LiveTimingRecorder → recover_replay_directory → ReplayPlayer → QualiSessionStore → QualiLiveService`, including duplicate-input variants and canonical cache reuse. Expected owners/sectors/projections are explicit fixture outcomes, not production candidate-selection results.

| Sequence | Captured task-start canonical result | Repaired direct and canonical result |
|---|---|---|
| A@100; clear@110; identical clear@190; delayed sectors `(21,31,38)`@150; B@300 | 7/8 messages; B sectors `(21,31,38)`; false **107.14285714285714 s** | 8/8 messages; B sectors `(None,None,None)`; projection **None** |
| A@100; unknown clear; identical known clear@120; S1@145/S2@171; B@220; S3@221 | 9/10 messages; B sectorless; valid projection lost | 10/10 messages; B sectors **`(25,26,39)`**; projection **86.4 s** |
| Unknown clear, known clear, S1, another distinct unknown clear, S2/completion/S3 | A lifecycle occurrence was omitted | All 11 messages retained; current cycle retired; B sectorless and unprojected |

Adjacent byte-identical unknown envelopes inside one capture also retain their multiplicity; duplicate copies of the capture do not multiply them. The following known clear still allows valid B sectors/projection.

Before: `/private/tmp/quali-final-two-baseline/numerical-before.json`. After: `doc/assets/qualifying_final_two_sequences.json`. The initial 21-case reproduction run against the frozen task-start source produced 20 failures and one passing current-checkpoint control; the final expanded 25-case matrix passes.

## E. R16 regression

Recording SHA-256: `b353aa941dcd21112f48c38b2bd558a5e1d89af799555109e21af6dfa354c71b`.

All **16,496 states** were compared on each required path:

1. Direct preserved envelopes against the task-start reducer/service, including Qt rendering.
2. Supported `ReplayPlayer` against task-start player/reducer/service.
3. Canonical recovery, including duplicate recording inputs, against the full task-start recovery result and direct-player outputs.

| Part | Message states | States with reference | Non-None projections |
|---|---:|---:|---:|
| Q1 | 7,530 | 5,846 | 47,324 |
| Q2 | 4,936 | 3,526 | 20,123 |
| Q3 | 4,030 | 2,882 | 14,907 |

All paths retain OCO sequence 1685 key `("Q1","31",3,1791014637.756)`, reference 98.879 s, accepted count 1, boundary 3, alias 2, and sectors `(25.488,32.895,40.496)`. VER sequence 2465 retains **96.72203499686125 s** and Qt **`1:36.722`**.

Projection values, event sectors, aliases, best keys, reference caches, and non-projection service outputs are identical. Raw state is identical to each corresponding task-start path. No numerical/count expectations changed.

One existing cross-path distinction is explicitly measured: canonical JSON serialization sorts keys, so **177 messages** have a different ordering of per-driver evidence within that message compared with direct playback. Each evidence multiset is identical; canonical evidence order is exactly the same as the task-start canonical writer. There are no completed-event-order differences and no service/reference differences. The writer was not modified to hide or remove this pre-existing distinction.

For this recording both old and new recovery yield 16,496 messages from two identical captures (32,992 input messages). The two blockers are established by the focused fixtures, not inferred from an R16 count change.

Evidence: `doc/assets/qualifying_final_two_direct_r16.json` and `doc/assets/qualifying_final_two_recovery_r16.json`.

## F. Exact validation commands and results

From `/Users/zhxutong/dr-wo/woData`:

```sh
PYTHONPATH=src /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q tests/test_quali*.py tests/test_live*.py tests/test_archive_replay.py tests/test_publication_time_contract.py tests/test_recovery_publication_identity.py
```

**190 passed in 1.02 s.** This includes 13 new recovery identity tests, qualifying reducers, live source/controller, recorder/recovery, archive/replay/faults, and the previous publication-contract tests.

From `/Users/zhxutong/dr-wo/woPlanner`:

```sh
PYTHONPATH=src:../woData/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q tests/test_quali*.py
```

**303 passed in 133.78 s.** Includes 25 new final-closure cases, the unchanged 50-case lifecycle matrix, all prior publication/identity/version-skew tests, R13/R14/R16 replay tests, and UI/formatting tests.

Direct-envelope R16 comparison, from woPlanner:

```sh
PYTHONPATH=src:../woData/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python scripts/verify_quali_r16_projection_identity.py --baseline-service /private/tmp/quali-final-two-baseline/woPlanner/src/woplanner/quali/service.py --baseline-reducer /private/tmp/quali-final-two-baseline/woData/src/wodata/live/quali_reducer.py --require-identical --output doc/assets/qualifying_final_two_direct_r16.json
```

**PASS:** 16,496 states; zero changed projection/provenance/cache states; exact expected counts and Qt anchor.

Supported player plus full canonical recovery, from woPlanner:

```sh
PYTHONPATH=src:../woData/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python scripts/verify_quali_r16_final_closure.py --baseline /private/tmp/quali-final-two-baseline --output doc/assets/qualifying_final_two_recovery_r16.json
```

**PASS:** 16,496 states per path; zero changed raw states versus the corresponding task-start path, zero changed service/reference states; canonical/direct evidence-order distinction explicitly verified above.

```sh
git -C /Users/zhxutong/dr-wo/woData diff --check
git -C /Users/zhxutong/dr-wo/woPlanner diff --check
```

Both pass. Baseline reproduction scripts/logs and final numerical evidence are retained with the task-start snapshot.

## G. Scope

Only the best-state early return and recovery deduplication/cache identity changed in production. No changes were made to publication/playback separation, `PublicationTime`, replay faults/scheduling, general reducer provenance architecture, projected-lap scaling, Qt formatting, planner identity, admission/eligibility, lap-state classification, track-evolution source/sign, traffic/3-second logic, cutoff forecasting/finalization, red-line presentation, or `NumberOfLaps 3→4` completion recognition.

Unknown-origin content still has no historical occurrence identity. Exact envelope-copy matching is limited to recovery inputs and never grants reducer ownership. Incomplete or unprovable copies are retained conservatively; this is not an arbitrary-stream identity proof.

## H. Git state and task-owned changes

Verified unchanged HEADs:

- woData: `100a9461826d5dbf97c2df4f662050261ca43f05`
- woPlanner: `a4a42e7822074edc1d5da9e001eeac39001d125e`

Fresh task-start working-tree snapshot: `/private/tmp/quali-final-two-baseline`. It contains source/tests/scripts/docs as they existed at task start, Git binary diffs/status, HEADs and SHA-256 manifests. Relevant source was captured before any edit; comparisons include all existing uncommitted work.

Task-owned files:

- woData: `src/wodata/live/quali_reducer.py` (current recovery eligibility), `src/wodata/live/recovery.py` (publication/copy identity, overlap multiplicity and cache version), new `tests/test_recovery_publication_identity.py`.
- woPlanner: new `tests/test_quali_final_two_blockers.py`, new `scripts/verify_quali_r16_final_closure.py`, this report, and three `doc/assets/qualifying_final_two_*.json` evidence files.

The existing R16 verifier and every woPlanner production file remain byte-for-byte unchanged from this task's start. Previous publication-contract work, qualifying changes, tests/docs, Pirelli assets, research archives, worktrees and operational inputs were preserved. woStrategy and woWeekend were not edited.

Task-only patches and file hashes: `/private/tmp/quali-final-two-baseline/woData/task-owned.diff`, `/private/tmp/quali-final-two-baseline/woPlanner/task-owned.diff`, and the adjacent `task-owned-files.json` manifests. These isolate the new hunks from prior uncommitted work.

No reset, stash, clean, revert, stage, commit, push or merge was performed.
