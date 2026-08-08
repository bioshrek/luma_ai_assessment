# ADR 005 — pusht timestamps are synthesized, so `TS_MONOTONIC` degrades to SKIPPED

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M1 (walking skeleton)
- **Affects:** design §2.2f (`timestamp_source`), §3 (QC gating), `config/qc.yaml`,
  `infrastructure/sources/lerobot_adapter.py`

## Context

Design §2.2f gives `timestamp_source ∈ {real, synthesized@<hz>Hz, annotation_seconds}` and lists
`"real"` as the typical value for sources A and B, on the reasoning that LeRobot ships an
explicit `timestamp` column while EPIC ships only seconds. That reasoning was never measured.

It matters because `TS_MONOTONIC` — the single QC rule in M1 — is gated on
`requires_real_timestamps`. If we call a fabricated clock "real", the rule passes on every
episode by construction, and a green QC report becomes a statement about arithmetic rather than
about the data.

## Finding — `timestamp` is exactly `frame_index / fps`, in float32

Measured over the **whole** `lerobot/pusht` shard (`spikes/_out/probe_pusht_timestamps.txt`):

```
rows 25650
timestamp == float32(frame_index/fps) exactly: True
max abs diff: 0.0
```

Not "within tolerance" — bit-for-bit equal for all 25,650 rows, at the declared `fps = 10`
from `meta/info.json`. The visible jitter in the per-step differences
(`0.09999943 … 0.10000038`) is float32 rounding of an exact rational, not measurement noise:
the column is `float32`, and `n/10` is not representable in binary.

`timestamp` also restarts at `0.0` for each episode, confirming it is derived from the
per-episode `frame_index`, not from any wall clock.

There is therefore **no recorded acquisition time anywhere in the dataset.** pusht is a
simulator rollout; a real timestamp was never captured, so none can be recovered.

## Decision

1. The LeRobot adapter **measures** rather than assumes. It compares the `timestamp` column
   against `float32(frame_index / fps)`; on exact equality it emits
   `timestamp_source = "synthesized@10Hz"`, otherwise `"real"`. The test is a property of the
   data, so a future LeRobot dataset with genuine timestamps is classified correctly with no
   code change and no per-source table.
2. `Provenance.has_real_timestamps` is consequently `False` for every pusht episode, so
   `TS_MONOTONIC` resolves to `SKIPPED(reason="synthetic_timestamp")` and the episode rolls up
   to `PASS` on the strength of the remaining (currently zero) applicable rules.
3. `SKIPPED` is reported as its own outcome with its reason, never folded into `PASS`. The M1
   report prints `TS_MONOTONIC:synthetic_timestamp → 3` under "Skipped rules".
4. Design §2.2f's "typical value" for source A is corrected from `real` to
   `synthesized@10Hz`, citing this ADR.

## Consequences

- **The one rule M1 ships is skipped on the one source M1 ingests.** That is the correct
  outcome and it is the reason this rule was chosen for M1: the walking skeleton now
  demonstrates the gating path end to end, which is the part of the QC design most likely to be
  got wrong, rather than demonstrating a rule that trivially passes.
- Source B (`aloha_sim_insertion_human`) is the same LeRobot layout and is expected to classify
  the same way; M3 must confirm by measurement, not by inheriting this conclusion.
- Any future rule that reasons about wall-clock spacing — dropped frames, jitter, duty cycle —
  must declare `requires_real_timestamps` and will skip here too. Rules that reason about the
  *nominal* rate (e.g. declared-vs-effective fps) must not, since `fps_nominal` is an upstream
  fact independent of the timestamp column.
- Do **not** "fix" this by widening `TS_MONOTONIC` to run on synthesized clocks. A rule that
  cannot fail is not a check.
