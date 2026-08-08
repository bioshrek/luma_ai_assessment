# ADR 008 — ALOHA forces channel-level units, and its gripper direction is unverifiable

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M3 (breadth: sources B and C)
- **Affects:** design §2.2b (`Channel`), §2.2e (gripper conventions), Appendix A.B,
  `config/embodiments.yaml`, `tests/fixtures/lerobot_aloha_mini`

## Context

Source B (`lerobot/aloha_sim_insertion_human`) is the second source, and the first test of the
claim that `LeRobotAdapter` is driven by `meta/info.json` rather than by per-dataset branching.

It is also the first source whose action vector is **not homogeneous**: 14 numbers, of which 12
are joint angles in radians and 2 are gripper positions in some normalized unit. If `unit` had
been a property of the _spec_ rather than of each _channel_, B would already be unrepresentable
at source number two.

## Findings — measured over all 7,500 rows of the cached shard

`spikes/_out/probe_m3.txt`.

1. **`action` and `observation.state` are both `(N, 14)` with the same channel order.** The
   difference is intent, not layout: one is commanded, the other measured.
2. **The 12 joint channels stay inside ±1.3 rad.** Consistent with radians; nothing suggests
   degrees.
3. **The 2 gripper channels are _approximately_ normalized and overflow both bounds.**

   | channel         | min      | max     | distinct values |
   | --------------- | -------- | ------- | --------------- |
   | `left_gripper`  | 0.08270  | 1.16246 | 487             |
   | `right_gripper` | -0.04621 | 0.95406 | 499             |

   State overflows too (`right_gripper` state reaches 1.07580). So the range is neither `[0,1]`
   nor a documented physical span such as metres of finger separation.

4. **Which end is "open" is stated nowhere.** Not in `meta/info.json`, not in the dataset card,
   not in the column names. The upstream names are `left_gripper` / `right_gripper` — a role,
   not a convention.
5. **`timestamp` is bit-identical to `float32(frame_index / fps)`**, at `fps = 50`. B's clock is
   synthesized exactly as pusht's is (ADR 005), which the adapter's existing measurement
   detects with no new code.
6. **The only unmodelled per-frame column is `next.done`.** No `next.reward`, no `next.success`.
7. **One camera** (`observation.images.top`, 480×640×3, MP4 sidecar), one task string, 50
   episodes of exactly 500 rows each.

## Decision

1. **`unit` and `metric_convertible` stay per channel.** B's `ActionSpec` carries 12 channels
   with `unit=rad, metric_convertible=true` and 2 with `unit=normalized,
metric_convertible=false`. No spec-level unit is introduced, and none may be added later —
   B alone disproves it, and source C disproves it again for `space` and `is_delta`.
2. **Every channel carries `arm_id`.** With two arms, `wrist_angle` is ambiguous without it, and
   a QC rule that reasons about one arm's range needs to know which arm it is looking at. The
   channels are additionally grouped `left_arm` / `right_arm`.
3. **`state` is declared as a YAML anchor alias of `action`.** The two are the same 14 channels;
   writing them twice invites drift, and their being _identical by construction_ is what will
   let `STATE_ACTION_ECHO` (M5) compare them channel-wise at all.
4. **The gripper convention is recorded as unverified, not guessed.**

   ```yaml
   gripper:
     convention: normalized_unverified_direction
     original_convention: normalized_unverified_direction
     inverse: { scale: 1.0, offset: 0.0 }
   ```

   Values are stored exactly as upstream emitted them, so our convention _is_ the original one
   and the recorded inverse is the identity. No `min`/`max` is declared, because the measured
   data does not respect `[0, 1]` and declaring bounds the data violates would turn a documented
   unknown into a silent invariant failure.

5. **No renormalization at ingestion.** Rescaling the observed range to `[0, 1]` would be
   min-max normalization at ingestion time, which the design rejects outright: ingestion
   preserves, and normalization is a downstream concern that must be able to see the true range.
6. **B's `timestamp_source` is `synthesized@50Hz`**, measured, not inherited from ADR 005.
   `TS_MONOTONIC` therefore resolves to `SKIPPED(synthetic_timestamp)` for B as well.

## Consequences

- **Adding B changed zero lines of adapter code.** It is one `config/sources.yaml` entry, one
  `config/embodiments.yaml` entry and one fixture. That is the entire claim M3 exists to test,
  and it held for the source that shares an adapter.
- A downstream consumer that needs the gripper's open/closed direction must resolve it from the
  upstream project and then update `embodiments.yaml` — at which point every already-ingested
  episode is correctly marked stale, because `embodiments.yaml` feeds the specs that
  `content_hash` covers. The absence is repairable; a wrong guess baked into 25,000 rows is not.
- `metric_convertible=false` on the gripper channels means any future rule that compares a
  gripper value against a physical threshold must skip B rather than fail it. This is the same
  degradation path as `has_video` and `requires_real_timestamps`.
- Two sources now share `LeRobotAdapter` and disagree about units, dimensionality, action space
  and arm count. The next LeRobot dataset needs no adapter change either — unless it breaks the
  `meta/info.json` contract, which is the only thing the adapter actually reads.
