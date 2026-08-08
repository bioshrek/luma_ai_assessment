# ADR 003 — Source C's action vector is 8-D, and its gripper channel is a discrete command

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M0 (feasibility spike)
- **Affects:** design §A.C, §2.2a; `docs/implementation_plan.md` M3 exit criteria

## Context

Design Appendix A.C describes `berkeley_autolab_ur5`'s action as `dim=10, physical_dim=7,
space="mixed"`, with `terminate_episode` occupying 3 non-physical channels and `gripper` being
an "absolute command, not a delta". That table is the design's central argument for pushing
`space` / `is_delta` / `frame` down to the channel level, so its correctness matters.

`features.json` for `berkeley_autolab_ur5/0.1.0`, fetched from the public bucket and printed
by `spikes/probe_rlds.py`, disagrees on three points.

## Findings

| Field                              | Design said               | Bucket `features.json` says                                        |
| ---------------------------------- | ------------------------- | ------------------------------------------------------------------ |
| `action/terminate_episode`         | `float32[3]`              | **`float32` scalar** (confirmed: 71 values for 71 steps)           |
| `action/gripper_closedness_action` | absolute command          | **"1 if close gripper, -1 if open gripper, 0 if no change"**       |
| `action/rotation_delta`            | repr unknown              | **"Delta change in roll, pitch, yaw"**                             |
| `observation/state`                | `state[15]`               | key is `observation/robot_state`, `float32[15]`, semantics offsite |
| cameras                            | 2 (`image`, `hand_image`) | **3**: also `image_with_depth` `float32[480,640,1]`                |

Values from episode 0 corroborate: `world_vector` |v| p99 = 0.020 (metres, delta-scale),
`rotation_delta` p99 = 0.027 (radians), `terminate_episode` is 0.0 through the episode and
1.0 on the final step, and the final step's `world_vector` / `rotation_delta` are exactly
zero — the RLDS padding step the design already anticipated.

## Decision

1. **`ActionSpec` for C is `dim=8, physical_dim=7, space="mixed"`**, laid out as:

   | idx | name                     | role         | channel.space          | is_delta | unit       | is_physical |
   | --- | ------------------------ | ------------ | ---------------------- | -------- | ---------- | ----------- |
   | 0-2 | `ee.dx/dy/dz`            | end_effector | `ee_translation_delta` | true     | m          | true        |
   | 3-5 | `ee.drx/dry/drz`         | end_effector | `ee_rotation_delta`    | true     | rad        | true        |
   | 6   | `gripper`                | gripper      | `gripper_command`      | **true** | normalized | true        |
   | 7   | `flag.terminate_episode` | control_flag | `flag`                 | false    | None       | **false**   |

2. **The gripper channel is a ternary _change_ command (-1/0/+1), not an absolute opening.**
   It must **not** be normalized to the "0=closed, 1=open" absolute convention used for B;
   doing so would silently reinterpret "no change" (0) as "fully closed". `is_delta=true` and
   the inverse-transform parameters record the original encoding verbatim.
   This is a stronger result than the design expected: `is_delta` differs between the gripper
   channel of B and the gripper channel of C **within the same `role`**, which no
   spec-level attribute could express.

3. **`Channel.rotation = {"repr": "euler_rpy", "compose": "unknown"}`** for channels 3–5.
   The description names roll/pitch/yaw, so the representation is known; the composition
   order (intrinsic vs extrinsic, XYZ vs ZYX) is _not_ stated anywhere and stays `unknown`
   rather than being guessed.

4. **`observation/robot_state[15]` maps to `state_spec.space = "unknown"`** with the raw
   vector preserved. The description defers to an external web page; per the design's rule, a
   wrong role guess is worse than an absence.

5. **Three camera streams, all `encoding="inline_frames"`**: `image` (static),
   `hand_image` (wrist), `image_with_depth` (static, depth). `has_rgb=True`, `has_video=False`.

6. `natural_language_embedding` (512-D per step) is dropped as a recomputable derivative and
   listed in `drop_channels` in `config/sources.yaml`; `natural_language_instruction` is
   preserved losslessly.

## Consequences

- Design Appendix A.C's table and the M3 exit criterion "`dim=10, physical_dim=7`" are both
  corrected to `dim=8, physical_dim=7`.
- The `is_delta` gripper finding is added to design §3's false-positive traps: a
  `GRIPPER_STUCK` rule that assumes absolute openings would fire on every C episode, because
  "no change" is the normal value.
- One observation deferred to M3 rather than decided here: in episode 0, `is_last` and
  `is_terminal` are both set on the **final two** steps, not just the last. The adapter must
  trim by `is_last` defensively and record the count it trimmed in `raw_extra`.
