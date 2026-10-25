# R18 qualifying packet subset

`packets.jsonl` contains 496 complete, unchanged JSONL lines selected from the
12,026-message qualifying capture. Tests need no private recording, network,
external FP artifacts, telemetry downloads or dataset-dependent skips.
`provenance.json` records the original capture SHA256, each copied line's hash,
selection rule, preserved original sequence numbers and verified checkpoints.

The selection keeps the initial identity/roster, clocks/status/race-control,
ALB timing through pit entry 1446, RUS/ANT timing and tyre history through 6623,
and field best/ranking/knockout packets through the first Q2 cutoff 4350.
Six original CarData packets immediately preceding the tested events retain
speed evidence required to clear outbound pit-state latches. Their complete
compressed payloads are unchanged. Dropping all telemetry changes RUS/ANT's
structural state and would produce a misleading eligibility reproduction.

No FP pace reference or COOL duration is embedded or inferred. Tests explicitly
label the synthetic 40 s common S1/S2 reference and manual COOL 120 s controls. The
four combinations prove independent prerequisite gates; they are not historical
R18 FP settings. The exact four RUS/ANT Q2 completion keys are retained.

The subset intentionally omits most other field timing, most telemetry and
post-4350 field updates. Its global PUSH indices and fitted slope magnitudes
belong to the **reduced stream**, not the original full-session indices
28/29/31/32. Tests assert per-driver first-installation selection, signed units
and support progression, rather than full-session fit values or traffic coverage.
The Q2 field/target checkpoint is verified at 4350: 16 active drivers, target
rank 10, 96.031 s. Later numeric cutoffs are used only to check consumption formulas.

Native `ReplayPlayer` changes the delivery source to `f1_archive_replay` and
renumbers packets 1..496 even without faults. The test harness maps delivery
ordinal to preserved original sequence solely for checkpoint labels. Another
path directly feeds the original `signalr_live` envelopes through the same
controller and preserves their sequence numbers. These paths retain the same
completion identities but exercise an existing clock difference: ALB 520 shows
58 s in native replay versus 55.936523 s with the original live clock's receipt
latency correction. Neither path changes the missing-prerequisite diagnosis.

Regenerate from the original local capture:

```bash
python tests/fixtures/quali_r18/extract.py /path/to/live_timing_e664885c31e14b29a0a2c3633090c1fd.jsonl
```

The extractor verifies the original SHA256 before copying selected lines. Run
the portable tests from woPlanner with `python -m pytest -q
tests/test_quali_r18_integration.py`.
