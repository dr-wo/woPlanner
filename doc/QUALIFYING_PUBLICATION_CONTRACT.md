# Qualifying publication provenance repair — 2026-10-04

The five closure findings are repaired. Publication time and playback time now have separate envelope roles. Projection mathematics, Qt formatting, completion admission, operational classification, traffic rules, track evolution, and cutoff logic were not edited in this task.

## A. Final time/provenance contract

| Field | Semantic meaning and producer | Mutable during replay? | Suitable for provenance identity? | Reducer use |
|---|---|---|---|---|
| `LiveTimingMessage.session_time_seconds` | Original session-relative publication coordinate. Archive stream/builders and explicit source-neutral callers supply it; live callback and subscription result normally leave it absent. | No; captured before fault transforms. | Positive source coordinate, not a globally unique event ID. | Preferred original coordinate when present. Existing domain clock also consumes it. |
| `source_timestamp_utc` | Parsed original feed callback timestamp, or preserved recorded timestamp. Archive `HH:MM:SS` strings are not UTC timestamps. | No. | Positive source coordinate when present. | UTC provenance only when no explicit session coordinate exists; existing domain clock retains its fallback. |
| `publication_time` / `PublicationTime(coordinate, seconds)` | Read-only projection of the preceding two immutable envelope fields. Coordinate is `session` or `utc`; unknown remains `None`. | No. | Source coordinate plus coordinate type; multiple occurrences at one coordinate can remain ambiguous. | Completion/packet ledgers, ownership checks, cycle lower bounds, and part fence. Different coordinate types are never numerically joined or ordered. |
| `received_at_utc` | `datetime.now(UTC)` at live adapter callback execution; per-topic iteration for subscription snapshots; recorded arrival for v2; replay wall clock for archives/legacy without preserved arrival. Not the wire's source occurrence time. | Recorded arrival is preserved; other replay envelopes get playback wall-clock receipt. | No. | Existing general clock fallback and last-message display only; never historical projection ownership. |
| Message `sequence` | Local adapter callback counter or replay delivery index. Synthetic source also enumerates delivery. | Fault processing can renumber it. | No upstream occurrence guarantee. | Ordering/diagnostics; not a projection provenance key. |
| `ArchiveRecord.session_time_seconds` | Parsed archive stream prefix, or scheduler zero for a non-stream snapshot. | No. | Only if `publication_time_known=True`. | Builder transfers genuine publication coordinates to the replay message clock. |
| `ArchiveRecord.publication_time_known` | Marks whether the archive actually supplied a timestamp. Non-stream snapshot responses explicitly set false. | No. | Prevents synthetic scheduling zero from becoming source evidence. | Unknown snapshots retain no provenance. |
| Archive `original_timestamp` / `source_order` | Original textual prefix and index within one topic stream, produced by `FastF1ArchiveClient`. | Preserved. | Timestamp supplies its own coordinate; index is a merge tie-breaker, not a global event ID. | Raw timestamp may parse as UTC; archive relative time travels separately. |
| `ReplayEvent.session_time_seconds` | Playback schedule. Initially archive time, recorded playback time, v2 recorded session time, receive-relative time, or legacy fallback index. | **Yes:** topic delay adds to this field; range filtering, waits, and result timeline use it. | No after transformation; receive/index schedules were never source evidence. | Not passed as publication time. |
| `ReplayEvent.message_session_time_seconds` / `preserve_message_session_time` | Original message session coordinate, including explicitly absent `None`. Existing legacy archive events capture their original coordinate when constructed; v2 and snapshot loaders explicitly preserve absence. | No during faults; replacement scheduling preserves the captured value. | Yes when genuinely supplied. | `ReplayPlayer` transfers it unchanged to `LiveTimingMessage.session_time_seconds`. |
| `ReplayEvent.original_received_at` | Preserved recorded arrival, also retained through canonical recovery. | No. | No. | Arrival field only. |
| `LiveTimingMessage.playback_time_seconds` | Delivery schedule exposed by `ReplayPlayer` and preserved by v2 recording/recovery. | A later replay may schedule it with additional faults; each emitted envelope is frozen. | No. | No qualifying provenance use. |
| Reducer `_latest_session_time_s` | Maximum of existing message session/source/receive fallback. | Advances with processing. | No source-occurrence guarantee. | Existing session clock, operational evidence, admission, and event-key coordinates remain unchanged. |
| `QualiCompletedLap.session_time_s` / public event key | Existing accepted-completion identity includes the reducer's accumulated time. | Accepted key is retained. | Identifies the accepted event; its numeric component is **not** presumed to be original publication time. | Original publication ledger maps source coordinates to this exact accepted owner. |
| `_projection_part_started_at` | Publication coordinate of the message that actually changes the part. | Changes only at a part transition/reset. | Source fence, not an event ID. | Rejects older/incompatible-coordinate projection evidence; no accumulated-clock substitution. |
| Controller queue order | FIFO enqueue order; `drain`, `apply`, and `apply_many` reduce supplied order without timestamp sorting. | Delivery may be reordered upstream by faults. | Ordering only. | Lifecycle actions follow delivery order; historical ownership follows original provenance. |

Both source timestamps can be present; explicit session time takes precedence consistently. The envelope is frozen, and the replay event captures its message clock before scheduling replacement. No general session-clock behavior was changed in the reducer.

### Actual producer guarantees and unknown-origin policy

The inspected FastF1 client registers both `feed` callbacks and `Subscribe` completion callbacks with SignalR. The latter returns a mapping of topic snapshots; the adapter iterates that mapping and emits no source timestamp. SignalR invokes handlers in its received message loop. The local callback counter and queue preserve observed delivery order; neither layer provides a stable upstream publication ID, de-duplication guarantee, or proof that snapshot registers exclude historical values. Feed timestamps can be older than an already-delivered message. There is no justified historical ordering guarantee for the subscription result's fields.

Unknown-origin values continue to update live registers and current projection checkpoints. A current checkpoint can still project against an already certified reference (112.5 s control). They cannot attach historical sectors, establish an alias, or recover a historical reference by equality. Unknown clears and rejected boundaries retire pending provenance without deleting the last genuine lower bound; a known clear or accepted publication can re-establish supported evidence. An unknown part-transition boundary remains unresolved for cross-part historical association. Receive time is never promoted into source time.

The actual transition authority remains the existing store semantics: `SessionInfo`'s explicit/derived part, or the qualifying clock's observed zero-to-reset transition while an explicit part is absent. Both now pass their own publication coordinate to the provenance fence. General session clocks and completion-admission boundaries keep their existing semantics.

## B. Files and layers changed

Paths in this section are relative to their named repository.

- **woData source/envelope:** `src/wodata/live/models.py` and `__init__.py` expose `PublicationTime` and the playback field; `fastf1_source.py` documents the subscription snapshot contract; `recorder.py` and `recovery.py` preserve both envelope roles and recorded arrival.
- **woData archive/replay:** `replay/archive.py` distinguishes timestamped streams from synthesized snapshot schedule zero; `builder.py` writes preserved original coordinates (builder version 1.1); `schema.py` captures the original message clock on event creation, including old archive inputs; `player.py` emits original and playback times separately; `faults.py` documents its scheduler-only replacement. Delays, duplicate/drop faults, sorting, waits, start/end filters, pause/stop behavior remain active.
- **woData reducer:** `quali_reducer.py` consumes typed envelope provenance, retains unknown-clear lower bounds, uses actual transition publication fences, evaluates contradictory original-publication evidence before all alias branches, and admits equal sector content from a distinct accepted publication. Existing bound cycles never rebind.
- **Tests:** woData adds `tests/test_publication_time_contract.py`; its existing archive fault test now asserts original time and delayed playback separately. woPlanner adds `tests/test_quali_publication_contract.py`. Existing 50-case lifecycle matrix and earlier identity/version-skew tests are retained unchanged.
- **Verification:** woPlanner's existing `scripts/verify_quali_r16_projection_identity.py` additionally compares provenance/cache states and supports `--require-identical`. Its task-start uncommitted contents were preserved before editing. New R16 JSON and this report record evidence.

No woPlanner production source file was changed by this task. Its existing uncommitted mathematics/presentation/service changes were preserved byte-for-byte.

## C. Finding closure matrix

| Latest finding | Closed? | Baseline reproduction and repaired evidence |
|---|---|---|
| A: contradictory alias enrichment | Yes | Baseline aliases `(None, 2)` incorrectly attached A's alias to B and recovered 86.4 s. Repaired original-publication resolution precedes established-owner, known-alias, first-association, and same-completion branches. A can receive its historical alias; retracted A cannot regain reference eligibility. Valid B is preserved until explicitly cleared. Covered via apply, singleton and whole-stream apply_many, FIFO queue, callback adapter and v2 recording. |
| B: unknown-origin Q1 clear | Yes | Baseline old `(21,31,38)` populated B and projected 107.14285714285714 s. Repaired B stays sectorless/unprojected. Includes actual supported subscription-result adapter, UTC/session coordinates, rejected-boundary retirement, and a known-clear recovery control. |
| C: replay delay rewrites provenance | Yes | Baseline Q1 190/200 became 250/260, seeding Q2 and 107.14285714285714 s. Repaired publications stay 190/200, delivery stays 250/260, and Q2 remains sectorless/unprojected. Download → archive adapter → builder → fault → player and repeated fault transforms are tested. |
| D: accumulated part fence | Yes | Baseline heartbeat 150 caused legitimate clear/S1 120/145 after transition 110 to lose their reference. Repaired Q1→Q2 and Q2→Q3 produce `(25,26,39)` and 86.4 s. Both SessionInfo and inferred clock-reset authorities are covered; old-part evidence remains rejected. |
| E: distinct accepted equal values | Yes | Baseline B lost identical `(20,30,40)` despite its own publication/alias. Repaired A and B retain their own sectors and aliases, with B providing 112.5 s. Same-publication duplicates and the separate upward-counter-recognition control cannot transfer sectors. |

The retained blockers remain covered: rejected-boundary retirement independently of packet lookup, stale clear/value handling in both transitions, bound-once ownership, invalid equal-time ambiguity, old-model/version-skew safety, unsupported ambiguous metadata, and reset isolation. The `NumberOfLaps 3→4` completion-recognition ambiguity was not redesigned; its second event must remain sectorless when it only repeats the first publication.

## D. Supported-input matrix

| Input path | Publication semantics | Delivery semantics / outcome |
|---|---|---|
| Timestamped live feed callback | Original UTC feed timestamp; no fabricated session clock. | Local callback time and sequence; queue FIFO. Contradictory historical metadata cannot enrich B. |
| Source-unknown live subscription result | `publication_time=None`; topic state is not a dated occurrence. | Arrival order supports live display/checkpoints; no historical reference inference. Unknown clear cannot authorize old values. |
| Preserved v2 recording | Original UTC/session/absence and recorded arrival preserved. | Existing recorded session or receive-relative schedule; optional recorded playback field for replay captures. Faults do not rewrite original provenance. |
| Ordinary archive replay | Original stream session coordinate; non-stream snapshots explicitly unknown. | Ordinary source and playback coordinates agree. |
| Delayed archive replay | Original coordinate unchanged, including old-format replay JSONL. | Configured topic delay still changes delivery order and wait duration. |
| Legacy text replay | Recorded UTC/epoch coordinate if present; absence stays unknown. | Fallback row index is scheduling only. |
| Canonical recovery | Preserved original message clock plus recorded arrival. | Existing monotonic scheduling normalization remains scheduling only. |
| Synthetic source | Supplied immutable publication fields retained. | Local enumeration/source tag changes do not create provenance. |
| Controller / apply / apply_many | Consume the same envelope contract. | No timestamp sorting or receive-time historical join. Batch and direct outcomes agree. |

## E. R16 regression

The preserved 16,496-message recording was compared with the task-start working-tree reducer and planner, including their uncommitted changes. Both direct-envelope verification and the supported v2 `ReplayPlayer` path compare all states. Projection values, raw completion provenance, aliases, event sectors, best keys, cached references, and non-projection domain output are unchanged. No expectations were revised.

| Part | Message states | States with reference | Non-None projections |
|---|---:|---:|---:|
| Q1 | 7,530 | 5,846 | 47,324 |
| Q2 | 4,936 | 3,526 | 20,123 |
| Q3 | 4,030 | 2,882 | 14,907 |

OCO sequence 1685 retains key `("Q1","31",3,1791014637.756)`, accepted count 1, boundary 3, alias 2, sectors `(25.488,32.895,40.496)`, and reference 98.879 s. VER sequence 2465 retains `24.932 × 98.879 / 25.488 = 96.72203499686125 s`; Qt displays `1:36.722`.

Machine-readable evidence: `doc/assets/qualifying_publication_contract_r16.json` and `doc/assets/qualifying_publication_contract_player_r16.json`.

## F. Remaining limitations

- Upstream does not expose original occurrence times/IDs for subscription snapshot fields. Local receive time, callback sequence, dictionary order, and content equality do not fill that gap.
- Multiple publications can share a timestamp. Ambiguous source ownership, incompatible coordinates without an explicit conversion, and unknown part-transition chronology remain deliberately unsupported for historical association.
- Legacy archive files that already encoded a fabricated zero as an ordinary timestamp cannot retroactively reveal that it was synthesized. Newly downloaded non-stream snapshots preserve that distinction. Old unannotated archives retain their declared stream-coordinate interpretation.
- Completion recognition for `NumberOfLaps 3→4` remains the separate pre-existing ambiguity; provenance does not transfer to the extra event.
- An optional broader script test has a pre-existing failure: `test_download_resolves_session_by_round_without_event` expects `R`, while task-start `SCRIPT_CONFIG` uses `FP1`. It fails identically on the frozen baseline; that unrelated user configuration was preserved.

This is bounded regression evidence, not a proof for arbitrary streams or a reconstruction of information absent upstream.

## G. Validation and Git state

Task-start snapshot: `/private/tmp/quali-publication-contract-baseline`. It contains working-tree `src`, `tests`, `scripts`, binary Git diffs, statuses, HEADs, and SHA-256 manifests. Baseline reproductions include the five numerical outcomes and the supported subscription-result failure. Task-owned diffs are generated against these snapshots, not HEAD alone.

HEADs (unchanged):

- woData: `100a9461826d5dbf97c2df4f662050261ca43f05`
- woPlanner: `a4a42e7822074edc1d5da9e001eeac39001d125e`

Exact commands and final counts are recorded in the validation appendix below. All existing modified/untracked source, tests, documentation, worktrees, Pirelli data, research archives, event inputs, and recording files outside this task's diff were preserved. woStrategy and woWeekend were not edited. No commit, push, or merge was performed.

### Validation appendix

Commands ran from `/Users/zhxutong/dr-wo/woData` unless marked woPlanner. No network-dependent feed was required; source adapters were exercised with deterministic transport fixtures.

```sh
PYTHONPATH=src /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q tests/test_quali*.py tests/test_live*.py tests/test_archive_replay.py tests/test_publication_time_contract.py
```

**177 passed in 0.98 s.** Includes 11 new envelope/source/replay contract cases, inferred clock fences, live adapters, recorder/recovery, generic live reducer, archive player, deterministic faults, pause/stop/filter behavior, and all qualifying reducer identity/lifecycle tests.

From woPlanner:

```sh
PYTHONPATH=src:../woData/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q tests/test_quali*.py
```

**278 passed in 134.94 s.** Includes 49 new cross-layer cases, the unchanged 50-case lifecycle matrix, identity/version-skew tests, R13/R14/R16 qualifying replays, and qualifying UI/formatting tests.

Strict task-start R16 comparison, from woPlanner:

```sh
PYTHONPATH=src:../woData/src QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python scripts/verify_quali_r16_projection_identity.py --baseline-service /private/tmp/quali-publication-contract-baseline/woPlanner/src/woplanner/quali/service.py --baseline-reducer /private/tmp/quali-publication-contract-baseline/woData/src/wodata/live/quali_reducer.py --require-identical --output doc/assets/qualifying_publication_contract_r16.json
```

**PASS:** 16,496 non-projection states compared; 0 changed projection driver states; 0 changed provenance/reference-cache states. All three exact reference/projection counts pass, together with the Qt `1:36.722` anchor.

Supplemental full supported-player comparison, from woPlanner:

```sh
PYTHONPATH=src:../woData/src:scripts QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/quali-mpl /Users/zhxutong/dr-wo/.venv/bin/python /private/tmp/quali-publication-contract-baseline/verify_player.py
```

**PASS:** both player versions replay all 16,496 messages; 0 changed original envelopes (excluding the newly exposed playback field), 0 changed raw states, 0 changed service states, identical reference caches/counts and VER numerical anchor.

Extra optional script test, first in current woData then the captured woData baseline:

```sh
PYTHONPATH=src /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q tests/test_quali*.py tests/test_live*.py tests/test_archive_replay.py tests/test_publication_time_contract.py tests/test_fetch_replay_data_script.py
PYTHONPATH=/private/tmp/quali-publication-contract-baseline/woData/src /Users/zhxutong/dr-wo/.venv/bin/python -m pytest -q /private/tmp/quali-publication-contract-baseline/woData/tests/test_fetch_replay_data_script.py
```

The broader run (before adding the last clock-reset case) returned **179 passed, 1 failed**; the isolated task-start script suite returned **3 passed, the same 1 failed**. The failure is the existing `FP1` versus `R` configuration expectation described above, not an introduced regression.

```sh
git -C /Users/zhxutong/dr-wo/woData diff --check
git -C /Users/zhxutong/dr-wo/woPlanner diff --check
```

Both pass. Task-owned diffs are saved at `/private/tmp/quali-publication-contract-baseline/woData/task-owned.diff` and `/private/tmp/quali-publication-contract-baseline/woPlanner/task-owned.diff`; changed-file manifests accompany them. They include additions to pre-existing uncommitted files without attributing earlier work to this task.
