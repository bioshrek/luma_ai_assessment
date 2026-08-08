# Session 4 — according to plan at `docs/plan.md`, help me understand dataset source C from a…

[← 回目录](index.md) · 由 `scripts/render_ai_sessions.py` 从 [session_4.json](../ai_chat_sessions/session_4.json) 生成，请勿手改。

| | |
| --- | --- |
| 开始时间 | 2026-08-08 07:36 UTC |
| 阶段 | 源 C |
| 轮次 | 4 |
| 模型 | claude-opus-5 |
| 工具调用 | 15 |
| 改动文件 | 1（17 处）|
| 输出 token | 46,341 |
| 墙钟时间 | 12m 45s |

---

## 第 1 轮 — 2026-08-08 07:36 UTC

> according to plan at `docs/plan.md`, help me understand dataset source C from a perspective of whom don't have prior knowledge of robotics, but with knowledge of the dataset source A and B.

<sub>claude-opus-5 · 输出 5,434 tokens · 1m 36s</sub>

I'll read the plan first.

<details>
<summary><b>2 次工具调用</b> — copilot_readFile x2</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 1 to 400
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 400 to 867

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to explain dataset C to someone familiar with A and B, highlighting how it differs—it uses RLDS/TFDS format with a real UR5 robot, end-effector delta control instead of joint angles, lacks timestamps, has a nested action dictionary structure, includes a terminate_episode flag, follows a specific gripper convention, stores language instructions with embeddings, and tracks episode boundaries with is_first, is_last, and is_terminal fields. So the key differences come down to three things: C is the Open X-Embodiment dataset, specifically the Berkeley UR5 subset, which uses real robot hardware with a 6-joint arm and gripper. The data format is fundamentally different — while A and B store data as columnar Parquet tables where each row is a frame with cheap random access, C uses serialized TFRecord episodes where each record contains a full episode with nested steps and embedded images, requiring sequential scanning and TensorFlow. Most importantly, the action representation is conceptually different: A specifies absolute 2D pixel positions for the pusher, B specifies absolute joint angles in radians, while C uses delta movements — relative displacements in meters for translation and what appears to be rotation components.

The end-effector (the robot's hand) has a 6-DoF pose combining position and orientation. Delta actions are fundamentally different from absolute ones because they're only meaningful relative to the current state, which is why C's action values cluster near zero with tiny magnitudes around 1e-2, whereas A and B's values spread across the workspace. This also means you can't reconstruct an absolute trajectory from C without integrating and knowing the initial state. Additionally, B controls joint angles directly while C controls where the hand goes in task space, letting the controller solve inverse kinematics — so the 14 versus 7 dimensionality difference isn't about robot size but about the control representation. The flattening order of nested action dictionaries becomes a contract, and A and B come with a names list to specify that order.

C doesn't provide a flat vector at all. The terminate_episode channel is a one-hot flag that isn't physical — in A the environment decides when done, in B the human stops recording, and in C the policy outputs "I'm done" — which adds three extra columns that look like actions but are really metadata. There are no timestamps in C; it's just an ordered list at ~5 Hz, so we have to synthesize them and mark provenance, meaning timestamp validation rules must be skipped rather than passed. The observation.state has 15 undocumented dimensions, unlike B where state mirrors action exactly, so the space is unknown and needs special handling.

Real robot data means



</details>

Nothing to change in code — here's the explainer. (Source C = the OXE / RLDS slice, first choice `berkeley_autolab_ur5`, described in `docs/plan.md#L18` and appendix `docs/plan.md#L659`.)

## The one-sentence version

You already know A (2D pusher, absolute pixel targets, Parquet) and B (ALOHA, absolute joint angles, Parquet). C is a **real** robot arm where the recorded action is not "where to be" but **"how far to move from wherever you are right now"**, stored in a **nested, stream-oriented** format instead of a flat table.

---

## 1. The hardware, in non-robotics terms

**UR5** = a single industrial arm, roughly the shape of a human arm: shoulder → elbow → wrist, 6 rotating joints, with a gripper (a two-finger clamp) bolted on the end. The gripper end is called the **end-effector** — think "the hand".

Two things a non-roboticist should internalize:

- **DoF (degrees of freedom)** = how many independent numbers you need to describe a configuration. UR5 has 6 joints → 6 DoF, plus 1 for the gripper.
- **Pose** = position + orientation = 3 numbers (x, y, z in meters) + 3 numbers (rotation, in radians). That's why "6".

B (ALOHA) is two arms of 6+1 each → 14 numbers. C is one arm → 7. The dimension difference is about arm count, not about C being "simpler".

---

## 2. The conceptual jump: joint space vs task space, absolute vs delta

There are two independent axes here, and A/B/C sit at three different corners:

| | what the numbers mean | analogy |
|---|---|---|
| **A** | absolute position of the pusher tip, in **pixels** | "put the cursor at (222, 97)" |
| **B** | absolute angle of **every joint**, in radians | "set shoulder to 0.4 rad, elbow to 1.1 rad, …" |
| **C** | **change** in the hand's pose since the last frame, in meters/radians | "move the hand 1 cm right and 0.5 cm up, rotate slightly" |

Two separate distinctions:

**(a) Joint space (B) vs task space (A, C).** B commands the arm's internal geometry directly. A and C command *where the tip should be* and let a controller figure out the joint angles (that inverse computation is called inverse kinematics — you don't need to care, just know that a layer below the data is doing it). The very same UR5 could have been recorded either way; which one you get is a recording choice, not a property of the robot.

**(b) Absolute (A, B) vs delta (C).** This is the one that actually breaks things:

- An absolute action is **self-contained**: `[222.0, 97.0]` means the same thing on frame 3 and frame 300.
- A delta action is **only meaningful relative to the current state**. To know where the arm actually was, you must integrate deltas from a starting pose.

Practical consequences you'd notice immediately if you loaded C after A/B:

- The numbers are tiny — magnitude ~`1e-2` and centered near **0** — whereas A's cluster around 256 (image center) and B's around whatever pose the arm rests at. Pooling mean/std across A, B, C produces a number that describes nothing. This is why the plan buckets every threshold by `(embodiment, space)` and stores `is_delta` explicitly (2.2a).
- Frame-to-frame difference (the `ACTION_JERK` rule) means *acceleration* for C but *velocity* for A/B. Same formula, different physics.

---

## 3. The storage format: RLDS/TFDS vs Parquet

A and B are Parquet: a **flat table**, one row per frame, columns typed, video in separate `.mp4` files. You can seek to row 100 of episode 47 in one operation.

C is **RLDS** (Reinforcement Learning Datasets), serialized as TFRecord — a sequential stream of protobuf records. One record = **one entire episode**, containing a list of `steps`, and each step is a nested dict:

```python
step = {
  "observation": {"image": uint8[480,640,3], "hand_image": ..., "state": float32[15]},
  "action": {"world_vector": [3], "rotation_delta": [3],
             "gripper_closedness_action": [1], "terminate_episode": [3]},
  "reward": 0.0, "is_first": True, "is_last": False, "is_terminal": False,
  "language_instruction": "put the block in the bowl",
}
```

Differences that matter:

1. **No random access.** "Give me frames 100–160 of episode 47" is a full scan. In Parquet it's a seek. This asymmetry is the #1 argument in 2.5 for materializing a `normalized/` copy instead of converting on read.
2. **Images are inline**, embedded as raw arrays inside the records, not separate video files. So "don't download the video" is not a switch you can flip the way you can for A/B.
3. **Reading it drags in TensorFlow.** A/B need only `pyarrow`. This is flagged as the project's biggest install risk (macOS + Python 3.11), with HDF5 as the fallback (2.3).
4. **`action` is a dict, not a vector.** A and B hand you a flat array *plus* a `names` list. C hands you four named sub-arrays and no ordering. **You** decide the flattening order — and once written down, that order is a public contract, which is exactly why `ActionSpec.channels` must be persisted.

---

## 4. Four surprises with no counterpart in A or B

### 4.1 `terminate_episode` — a non-physical channel hiding inside the action vector

In A, "the episode is over" is decided by the **simulator** (block coverage > 0.95). In B, by the **human operator** hitting stop. In C, the **policy itself emits it**: 3 extra numbers sitting in the same action dict as the physical ones, a one-hot flag that goes 0 → 1 on the last frame.

Why this is a trap rather than trivia: those 3 columns have no unit, no physical limits, and a step-function shape. Run the usual numeric checks over the whole vector and **every C episode gets flagged** — the jump at the last frame dwarfs the p99.9 of a "channel" that's constant otherwise. Hence `dim=10, physical_dim=7`, `role="control_flag"`, `is_physical=False`, and the domain-level rule that cross-channel statistics only ever see the physical subset ([invariant 6](docs/plan.md#L515)).

### 4.2 There are no timestamps

A and B have a real `timestamp` column. C's steps are just an ordered list; the recording rate (~5 Hz) is documented prose, not data. So time has to be **synthesized** as `i / 5.0`.

The subtle part: if you synthesize and then run "are timestamps monotonic?", it passes 100% of the time — a meaningless green check. That's why the plan records `provenance.timestamp_source = "synthesized@5Hz"` and forces those rules to return `SKIPPED`, not `PASS`. "Degraded ≠ passed."

### 4.3 `observation.state` is 15 numbers of unknown meaning

In B, `state` is the perfect mirror of `action` — same 14 channels, measured instead of commanded. In C, the dataset card doesn't reliably say what the 15 entries are (some mix of pose, joint angles, gripper width, possibly more, varying across sub-datasets).

The plan's rule: **don't guess** — `StateSpec.space = "unknown"`, channels `role="unknown"`, raw values preserved in `raw_extra`. A wrong guess is worse than a declared gap, because a wrong `role` silently propagates into every downstream statistic. It also makes the `STATE_ACTION_ECHO` check cleanly skip itself on C instead of comparing incomparable vectors.

### 4.4 It's a real robot, with a camera on the wrist

A and B are simulation: noiseless, perfectly uniform stepping, fixed cameras. C is physical hardware — sensor noise, jitter, occasional dropped steps, and **two cameras**: an external one plus `hand_image` from a camera mounted on the wrist that **moves with the gripper**. So "the camera view changed a lot" is normal behavior here, not a fault.

---

## 5. Smaller things worth knowing

- **Gripper convention.** B stores a continuous opening width (two of them, left and right arm). C often uses `-1 = close / +1 = open`. Both get normalized to `0 = closed, 1 = open`, with the inverse transform stored so the original is recoverable.
- **`language_instruction` is per-step.** A and B have one task for the whole dataset; C carries the sentence on every step (usually constant within an episode). Text is kept losslessly; the accompanying `language_embedding[512]` is **discarded** — it's recomputable from the text and tied to one encoder version. The plan calls this the cleanest teaching example of the lossless/droppable boundary.
- **`is_first` / `is_last` / `is_terminal`.** `is_last & ~is_terminal` means "cut off by a step limit", not "finished". RLDS also commonly appends a **padding step** at the end with a zero/placeholder action — include it in statistics and you pollute the jerk and static-episode checks, so normalization trims it and records that in `raw_extra`.

---

## 6. Why C is in the lineup at all

A gave you: pixels, a non-arm body, absolute task-space targets.
B gave you: radians, two arms, absolute joint-space targets, mixed units inside one vector.
C adds the three axes neither of them can test:

1. a **different storage paradigm** (nested stream vs flat table) — proves the adapter abstraction isn't just "two Parquet readers";
2. **delta semantics + a non-physical channel inside the action vector** — proves that "unify the action space" cannot mean "concatenate into one tensor";
3. **missing/unknown metadata on a real robot** (no timestamps, undocumented state) — proves the provenance and capability declarations do real work rather than being decoration.

---

## 第 2 轮 — 2026-08-08 07:58 UTC

> according to the above understanding, does the unified schema design address it properly? do we need to update the design?

<sub>claude-opus-5 · 输出 8,974 tokens · 2m 09s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to check whether the unified schema in section 2 actually handles source C's structure correctly. Looking at what we know about C from the appendix, the action field is a nested dictionary with specific components—world vector, rotation delta, gripper closedness, and terminate episode flags—that flatten to 10 columns total, and there's an observation state with 15 dimensions of unclear semantics, plus images stored directly in the TFRecord. I should map these against the current schema design to spot any misalignments or missing pieces.

Now examining the schema coverage more carefully: C has two camera streams without timestamps, per-step reward and discount signals, language instruction and embedding at each step, delta actions, and comes from a real robot. The SignalSpec structure defines space as a single enum value per spec, but C's action vector is actually a mixture—the world vector is pose delta, rotation is rotation, gripper is gripper control, and terminate episode is a flag. This is the same problem we'd have with B's 14-dim vector containing joints plus grippers. The space field can't accurately represent a heterogeneous action like this, so I need to decide whether to expand the schema to handle mixed-type vectors or find another way to express this constraint.

The recommendation is to define space as applying only to the dominant physical channel group (those with role in joint or end_effector), which is a small clarification rather than a full redesign. But there's a related issue: is_delta is also spec-level, yet C's gripper_closedness_action is absolute, not delta, while the world vector and rotation are deltas. So is_delta needs to be per-channel or per-group as well, not just a single boolean for the entire spec.

Similarly, frame (reference frame) is spec-level but only meaningful for world_vector deltas in base or tool frame—it's meaningless for joint angles and grippers. So the real fix is to introduce a ChannelGroup abstraction layer that lets space, is_delta, and frame be defined per-group rather than forcing them all to be spec-level attributes.

There's also a critical gap: rotation representation is completely unspecified. A 3-element rotation_delta could be axis-angle, Euler angles, or rotation-vector, but the schema doesn't say which. Without a rotation_convention field, downstream code can't interpret or compose these deltas. This only shows up in dataset C, but it's a genuine missing piece that needs to be added at either the channel or group level.

Beyond that, there's the question of whether rotation deltas are pre- or post-multiplied during composition—probably worth recording even if just as "unknown" in the metadata. And critically, delta-only actions need an anchor point to be invertible; if we're storing deltas without the initial pose, we can't reconstruct the absolute trajectory, especially when the reference state itself is marked unknown. This limitation should be explicitly documented.

The `terminate_episode` field being a 3-vector rather than a scalar also needs clarification—which index represents the actual termination signal, and how does that map to the QC rule requiring the flag to stay zero throughout and flip to one only at the final frame? Either we need to specify which channel is the terminate signal with the raw values preserved, or normalize it to a derived boolean column.

Actually, in RT-1 and OXE datasets, `terminate_episode` is typically a 3-dimensional one-hot encoding, but the semantics vary by dataset. Given the plan's own principle about not assuming unknown semantics, the better approach is to keep the 3 channels as a control flag and derive `EpisodeBoundary` from RLDS's `is_terminal` or `is_last` fields instead, which are well-defined. This way we use RLDS as the authoritative source and cross-check against `terminate_episode`—any mismatch becomes a valuable QC finding.

Now I'm noticing the schema has no camera specification at all.  Dataset C introduces a wrist-mounted camera that moves with the gripper, whereas A and B use fixed cameras, and downstream training needs to know the mount type. More critically, C stores images inline in the TFRecord rather than as separate mp4 files, so the distinction between `has_video` and `has_images` matters—we need a `MediaSpec` that describes per-camera encoding (mp4, inline frames, etc.) to handle this variation.

The `VIDEO_FRAME_MISMATCH` QC rule and the cost tradeoff around skipping video downloads don't apply to C the same way since inline images can't be omitted. I should introduce a `CameraSpec` value object capturing name, mount type, resolution, encoding, and presence. Additionally, C's per-step metadata like discount, reward, and language instruction are frame-level rather than episode-level, which conflicts with the current `raw_extra` structure.

The plan groups unmapped upstream fields into `raw_extra_json` at the episode level, but C's per-frame extras need a home—frame-level unmodeled columns should stay in frames.parquet with a `raw.` prefix while episode-level data goes to `raw_extra_json`. For `language_instruction`, which C carries per-step but the schema treats as episode-level, I need to establish a rule: if the instruction remains constant across the episode, promote it to episode-level; otherwise keep it per-frame.

`fps_effective` is measured from timestamp intervals, but C synthesizes timestamps so it's always equal to `fps_nominal` by construction—this needs to be NULL or marked as derived to avoid false measurement claims. For `content_hash` and idempotency in C, since one TFRecord shard contains multiple episodes, I'd need to hash the deserialized episode bytes rather than the upstream file, and for RLDS the upstream_id mapping is still unclear—likely the episode index within a split, though RLDS does provide `episode_metadata.file_path`.

The real issue is that C lacks a stable upstream episode ID; if the upstream re-shards, indices shift and break idempotency. I need to construct `upstream_id = f"{split}/{shard_file}#{index_in_shard}"` plus content_hash over normalized content, and document this fragility since the whole acceptance criterion depends on detecting whether re-runs produce new data.

For `space="unknown"` on state[15], that's already handled, but invariant 3 says if `has_state` is False then state should be NULL—here `has_state` is True but semantics are unknown, so I'm weighing whether to set `has_state=False` instead, though the plan's appendix suggests preferring `state=NULL`.

The contradiction is that keeping the values losslessly with `has_state=True, space="unknown"` preserves information, whereas dropping them would be lossy, so I should keep them and clarify the inconsistency in the spec.

For RLDS padding, the plan says to trim the last step, which is a lossy transform that must go into `provenance.transforms`, not `raw_extra`—there's a minor inconsistency here since the plan's own rule 2.2d requires dropping data to be recorded in transforms.

On `reward` and `discount`, the plan calls discount "redundant" but that's not quite right—discount is derivable from `is_terminal` in offline RL contexts (where discount=0 at terminal signals termination), so the justification should be framed as "derivable from is_terminal" rather than "redundant mirror." For sampling at 5Hz with sqrt smoothing by embodiment, that's already handled.

I need to recommend concrete design updates, but I should be careful not to over-engineer since the plan explicitly warns against that.

Let me categorize the issues by severity: must-fix problems where the schema can't represent C truthfully, should-fix internal contradictions, and limitations to note. The must-fix tier includes `is_delta`/`space`/`frame` being spec-level when C's vectors span multiple frames (needs channel-level or group layer), unspecified rotation representation making data uninterpretable, missing CameraSpec for inline images, no place for per-frame unmodeled columns, and no stable `upstream_id` for RLDS which threatens idempotency.

The should-fix tier covers `terminate_episode` vs RLDS `is_terminal` authority, the state contradiction between `has_state=True` and A.C.7's "state=NULL", trimming padding belonging in transforms not raw_extra, `fps_effective` being meaningless with synthesized timestamps, and per-step language_instruction conflicting with episode-level task. For limitations, I'd document that delta without absolute anchor makes absolute trajectory unrecoverable and that C can't honor `--no-video` since images are inline.

Now I need to decide whether to actually edit plan.md or if the user's question is asking me to evaluate whether the schema addresses these issues properly. ich ich should give a concrete assessment with specific proposed changes rather than just answering yes or no, then offer to apply them to the document. Since the user framed this as a direct question, I'll respect that by providing a clear verdict and concrete diffs, ending with an offer to implement — that's more useful than making 10 unasked edits to a 900-line doc. Let me verify the line numbers I'll reference.



</details>

## Verdict

The parts of C that the plan **chose** C for are already handled well — `is_delta`, `physical_dim` / `control_flag`, `timestamp_source="synthesized@5Hz"` → `SKIPPED`, `state.space="unknown"`, `language_embedding` as droppable, drop-channels-as-transform. Those were clearly written with C in mind and they hold up.

But the same "one vector, multiple semantics" pressure that B applied to `unit` and `gripper`, **C applies one level higher — to `space`, `is_delta`, and `frame` themselves**. Those are still spec-level fields, and C's action vector spans all three. That's the same class of bug the B review already fixed once, just not carried far enough.

Five things the schema **cannot express truthfully** today, plus five internal contradictions.

---

## Must fix — schema can't represent C without lying

### P1. `space` / `is_delta` / `frame` must move to channel (or group) level

C's `action` = `world_vector[3]` (delta, meters, base frame) + `rotation_delta[3]` (delta, radians) + `gripper_closedness_action[1]` (**absolute**, unitless) + `terminate_episode[3]` (a flag).

Setting `is_delta=True` on the spec asserts the gripper command is a delta. It isn't — it's an absolute open/close command. And §2.2a's own rule ("cross-source statistics must bucket by `is_delta` first") would then bucket that channel wrongly. Same for `space`: `ee_pose_delta` is false for 4 of the 10 columns. Same for `frame`: meaningless on gripper and flag channels.

Note this is already broken for B too — `space="joint_position"` is false for its 2 gripper channels — it just wasn't load-bearing there. C makes it load-bearing.

```python
Channel = {
  ...,
  "space": "joint_position" | "ee_translation_delta" | "ee_rotation_delta"
           | "cartesian_2d" | "gripper" | "flag" | "unknown",
  "is_delta": bool,
  "frame": "base" | "tool" | "world" | "camera" | None,
}

SignalSpec = {
  ...,
  "space": ...,        # derived: space of the dominant physical group; "mixed" if not unique
  "is_delta": ...,     # derived: any(c.is_delta for physical channels)
}
```

Keep spec-level fields as **derived summaries** so `STATE_ACTION_ECHO`'s gate (`action.space == state.space`) still reads cleanly, but make the channel the source of truth. Add invariant: spec-level `space` is `"mixed"` iff physical channels disagree.

### P2. Rotation representation is unspecified — C's rotation data is currently uninterpretable

`rotation_delta[3]` with `unit="rad"` is not enough. Three numbers could be axis-angle / rotation vector / Euler XYZ / Euler ZYX. You cannot integrate, compare, or convert deltas without knowing which. A has no rotation and B's radians are joint angles (no convention needed), so **only C surfaces this** — which is exactly why it was missed.

```python
Channel.rotation = {                      # non-null iff space startswith "ee_rotation"
  "repr": "axis_angle" | "rotvec" | "euler_xyz" | "euler_zyx" | "quat_wxyz" | "unknown",
  "compose": "pre" | "post" | "unknown",  # for deltas: R_new = ΔR·R vs R·ΔR
} | None
```

Per the plan's own "don't guess" principle, `unknown` is a legitimate value here — but it must be *recorded*, not absent.

### P3. No `CameraSpec`, and `has_video` doesn't describe C

`camera_json` exists in the SQLite table and `VIDEO_FRAME_MISMATCH` depends on "the camera count declared by the source," but §2.2 never defines the value object. C forces the issue on two axes:

- **Mount type.** `hand_image` is wrist-mounted — it *moves with the gripper*. A/B's cameras are fixed. Downstream this is a first-class distinction (wrist views are what makes manipulation policies work), and "the view changed drastically" is normal for wrist cams and anomalous for fixed ones. QC needs to know which.
- **Encoding.** C's images are **inline arrays inside the TFRecord**, not `.mp4` files. `has_video=True` is misleading, `VIDEO_FRAME_MISMATCH`'s premise (compare mp4 frame count vs parquet rows) has no counterpart, and — importantly — **§1's `--no-video` default doesn't apply to C at all**: you cannot skip the pixels, they're in the same records as the actions. The cost tradeoff written in §1 is silently wrong for one of the four sources.

```python
CameraSpec = {
  "name": "image" | "hand_image" | ...,
  "mount": "static" | "wrist" | "head" | "unknown",
  "resolution": [h, w], "channels": int,
  "encoding": "mp4_sidecar" | "inline_frames" | "absent",
  "is_present": bool,
}
```

Plus `Capabilities.has_video` → split into `has_rgb` (any camera) and keep `has_video` for "decodable sidecar video exists," since the QC rules key off the latter.

### P4. There is no defined home for **per-frame** unmodeled columns

§2.2d says "fields that don't fit go into `raw_extra` (JSON)" and the SQLite column is `raw_extra_json` **on `episodes`** — i.e. episode-level. But C's unmodeled data is overwhelmingly **per-step**: `discount`, `is_first/is_last/is_terminal`, `language_instruction`, `language_embedding`. Writing 500-dim embeddings and per-step flags into a single episode-level JSON blob is not viable.

Rule to add: frame-level unmodeled upstream columns stay as **prefixed columns in `frames.parquet`** (`raw.discount`, `raw.is_terminal`, …), listed in `episode.json` under `raw_frame_columns`; only episode-level leftovers go to `raw_extra_json`. Without this, "we don't lose information we don't understand" has no mechanism on the source that needs it most.

### P5. C is the only source with **no stable upstream episode ID** — this threatens the acceptance criterion

§5's idempotency key is `(source_id, upstream_id)`, with `content_hash` = "upstream file sha256, or size+mtime+revision." For A/B, `episode_000042.parquet` is a stable, per-episode file. For C, **one TFRecord shard holds many episodes**, and an episode's only identifier is its ordinal position in the shard. Hash the shard file and every episode in it shares one hash; re-shard upstream and every `upstream_id` shifts.

This is not a nicety — "re-run detects no new data" is one of the two things the reviewer will literally execute.

```
upstream_id  = f"{split}/{shard_basename}#{index_in_shard}"
content_hash = sha256(canonical bytes of the normalized episode)   # not the shard file
```

and record `shard_layout_revision` in `sources`, so a re-shard is detectable as "stale" rather than appearing as 800 brand-new episodes.

---

## Should fix — contradictions C exposes inside the existing text

| # | Issue | Fix |
|---|---|---|
| 6 | `TERMINATION_CONSISTENCY` treats `terminate_episode` as authoritative, but its 3-dim one-hot semantics vary across OXE sub-datasets — and RLDS already gives you `is_terminal` / `is_last`, which *are* well-defined. | Make `is_terminal`/`is_last` authoritative for `EpisodeBoundary`; treat `terminate_episode` as a **cross-check**. Disagreement between them is a genuinely valuable QC finding, and strictly better than trusting the ambiguous one. |
| 7 | §2.2b' says C gets `StateSpec.space="unknown"` (values retained); appendix A.C.7 says "prefer `state=NULL`". These contradict, and NULL would be **lossy** — violating §2.2d. | Resolve explicitly: `has_state=True`, `space="unknown"`, all 15 values retained, channel `role="unknown"`. Delete the "prefer NULL" wording. |
| 8 | Appendix A.C.4 says trim the RLDS padding step and "record in `raw_extra`" — but §2.2d requires dropped data to go in `provenance.transforms`. | `transforms += [{"op": "drop_padding_step", ...}]`. Same rule as `drop_channels`. |
| 9 | `fps_effective` is defined as "measured from timestamp median interval." With synthesized timestamps it equals `fps_nominal` **by construction** — a fabricated measurement that will appear in reports as if verified. | `fps_effective = NULL` when `timestamp_source != "real"`. Same "degraded ≠ passed" logic already applied to the QC rules. |
| 10 | `task` is episode-level, but C carries `language_instruction` **per step**. Usually constant; not guaranteed across OXE. | Promote to episode-level `task` only if constant; otherwise keep the per-frame column and set `task=None` + a `LANGUAGE_INSTRUCTION_VARIES` REVIEW verdict. |

---

## Register as limitations (document, don't build)

- **Delta actions have no absolute anchor.** §2.2d claims action values are losslessly preserved because unit conversion is invertible — true per-value, but for C the *trajectory* is not reconstructable: you'd need the initial absolute pose, and `state[15]` is `unknown`, so we may not have it. Anyone integrating C's deltas gets a trajectory in an arbitrary frame. Worth one line, because it's a real consumer-facing constraint that A/B don't have.
- **`--no-video` is a no-op for C** (inline images). §1's cost paragraph needs a sentence saying so, otherwise the stated tradeoff is wrong for a quarter of the sources.

---

## Bottom line

The design is directionally right and C mostly confirms it — but **P1 and P5 are load-bearing**: P1 means the schema currently makes false assertions about 4 of C's 10 action channels, and P5 means the incremental/idempotency acceptance test could fail on the one source where episode identity is fragile. P2–P4 are cheap additions that prevent unrecoverable ambiguity.

Want me to fold these into `docs/plan.md` — same treatment as the "three corrections forced by source B" block, i.e. a §2.2 amendment listing the corrections C forced, plus the targeted fixes to §1, §3, §5 and appendix C?

---

## 第 3 轮 — 2026-08-08 08:07 UTC

> proceed with proposal fixes from P1 to P5

<sub>claude-opus-5 · 输出 30,439 tokens · 7m 49s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to apply the P1–P5 changes to the plan documentation, starting with reading the exact text in the areas I'm targeting. The main edits involve updating the SignalSpec/Channel block with P1 and P2 changes, inserting a new corrections block for source C, splitting the has_rgb capability in the Capabilities section, and figuring out where to place the new CameraSpec definition within section 2.2.



</details>

<details>
<summary><b>2 次工具调用</b> — copilot_findTextInFiles x1, copilot_readFile x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex ``SignalSpec = \{|Channel = \{|Capabilities = \{|幂等键|无法归入统一字段|默认 `--no-video`|VIDEO_FRAME_MISMATCH|三处相对初稿的修正`` (`**/docs/plan.md`), 10 results
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 100 to 200

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm mapping out the edits needed: first, I'll reorganize the SignalSpec and Channel sections, then add a new paragraph about corrections from source C after the B block, move the raw_extra sentence to P4, and restructure the Capabilities block to split out the RGB and video capabilities into P3.

Then I need to handle several more targeted updates: add a note about the --no-video flag as a C exception, add the VIDEO_FRAME_MISMATCH row dependency, move the idempotency key to P5, and expand the invariants section to cover channel-level properties like space, is_delta, rotation, and camera specs.

For the database schema, I'll add CameraSpec to the ubiquitous language section, and update the sources table to include shard_layout_revision alongside the existing camera_json field in episodes.

Defining the SignalSpec value object...

Defining the Channel value object... 完成了 gripper 角色的配置结构，包括归一化约定和反变换参数。现在需要更新后续说明，强调统一的通道级元信息包括 role、space、is_delta、unit 等必需字段。然后补充 C 版本的修正说明，重点是 space、is_delta、frame 从规范级下沉到通道级，因为单个 action 向量中可能混合了不同类型的增量和绝对值。

spec 级字段改为派生汇总用途，通道级才是真实来源。还要新增 Channel.rotation 字段来表示旋转方式。

三个数值加上弧度单位还不够确定语义——可能是轴角、旋转向量、欧拉角等多种表示，只有 C 会暴露这个问题。按照"拿不准不猜"的原则，`unknown` 是合法取值但必须显式写出。另外 `raw_extra` 需要区分 episode 级和 frame 级，`Capabilities.has_video` 也要拆分。

现在明确了这四个要点，最后两个指向具体描述位置。

对于无法归入统一字段的上游数据，按粒度分开存放：episode 级的放进 `raw_extra` JSON，frame 级的以 `raw.` 前缀作为额外列保留在 `frames.parquet` 里，列名清单写进 `episode.json` 的 `raw_frame_columns`。这个方案是被来源 C 逼出来的，因为它的上游字段几乎全是逐 step 的数据。

现在定义 Capabilities 和 CameraSpec... `CameraSpec` 必须同时定义 `mount` 和 `encoding` 字段，这两个约束都是由来源 C 的特性决定的。来源 C 的图像数据是直接嵌入在 TFRecord 记录中的数组，而不是独立的 mp4 文件，所以设置 `has_video=True` 会错误地触发视频帧数校验逻辑，而且第一阶段的 `--no-video` 默认值对 C 完全不适用，因为像素数据和动作数据在同一批记录里无法跳过。另外，C 的 `hand_image` 来自腕部相机，会随着夹爪运动，这与 A/B 来源的固定相机不同。

我需要在文档中明确说明这个例外：C 的图像内嵌在 TFRecord 里与低维信号一起读取，`--no-video` 对它没有效果，只能省去解码和磁盘写入的开销，而不能省带宽。还要更新第三部分表格中 VIDEO_FRAME_MISMATCH 这一行的说明。

对于幂等性，我用 `(source_id, upstream_id)` 作为唯一键，`content_hash` 来检测上游是否有更新。A/B/D 这三个来源都有明确的上游标识符和文件哈希值可用，但 C（RLDS）比较特殊——它没有稳定的上游 ID，需要单独处理。

一个 TFRecord shard 文件里包含多条 episode，每条 episode 的身份只能通过它在 shard 内的序号来确定。如果直接对 shard 文件做哈希，那么同一 shard 内的所有 episode 会共享同一个哈希值；一旦上游重新分片，所有序号就会平移，导致 `upstream_id` 全部失效，第二轮运行时会把所有旧数据都当成新增，这直接违反了验收标准。所以我需要为每条 episode 单独生成 `upstream_id`（包含 shard 文件名和序号），对规范化后的 episode 字节流计算哈希而不是 shard 文件本身，并在 sources 表里加一列 `shard_layout_revision` 来追踪分片方案的变化。

对于 C 类型的数据，`content_hash` 必须在归一化之后才能计算，所以无法靠哈希值来跳过下载，只能依赖 `upstream_id` 来判断是否已处理过，`content_hash` 则用于事后验证和检测陈旧数据。还要更新 sources 表的字段定义，加入 `shard_layout_revision`，以及在不变量部分明确 `Channel.space` 是语义真相，`SignalSpec.space` 和 `is_delta` 只能从物理通道派生，不允许手工赋值。

另外补充几条规则：`channel.space` 以 `ee_rotation` 开头当且仅当 `channel.rotation` 非空，`has_video` 为真时至少要有一个摄像头编码为 mp4 sidecar 格式，这样依赖 `has_video` 的规则在 C 上就能干净地跳过。

现在需要更新 §2.2a 的结尾句子，把 camera.py 加入到 domain/ 的



</details>

Applying P1–P5 to the plan.

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 36 lines with 54 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 2 处修改，第 109-145 行

```
Now P4 (raw_extra granularity) and P3 (CameraSpec / capability split).

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 15 lines with 28 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 197-197 行

```
Now §1 `--no-video` exception, the QC rule dependency, and P5 (idempotency key).

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 5 lines with 17 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 4 处修改，第 42-387 行

```

<details>
<summary><b>2 次工具调用</b> — copilot_readFile x1, copilot_multiReplaceString x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 519 to 545
2. **`copilot_multiReplaceString`** — Replacing 3 lines with 8 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 3 处修改，第 472-580 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to update several appendices to align with the new schema changes. For Appendix C, I'm expanding the channel table to show per-channel details like space, is_delta, frame, and rotation alongside the camera spec and upstream_id. Appendix D needs the action_spec updated to include has_rgb alongside has_video. The cross-source comparison table should get mount and encoding details added to the camera row, and I'll make sure Appendices A and B have the camera entries included.



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 12 lines with 37 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 5 处修改，第 848-908 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm checking the cross-source table details for dimension C, which should be 10 (7 physical + 3 control flags), and noting that the incremental status varies per channel with a specific notation. The section 2.2a intro looks good as written with the description of compressing 2D xy, 14D joint angles, and 7D delta pose into one space. I should also flag that the M4/M5 milestone risks mention channel semantic mapping errors.

Now I'm realizing the markdown structure needs fixing — the "2b." item in the ordered list won't render as a list marker since markdown requires digits only, so it'll break list continuity. I should convert it to a nested bullet point indented under item 2 instead. Also need to verify that the table inserted into the ordered list item 1 is properly indented to stay within that list item, otherwise it'll terminate the list prematurely.



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 3 lines with 3 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 2 处修改，第 860-930 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_getErrors x1</summary>

1. **`copilot_getErrors`** — Checked `docs/plan.md`, no problems found

</details>

P1–P5 are folded into `docs/plan.md`.

**§2.2a — P1 + P2**
- `space` / `is_delta` / `frame` moved down to `Channel`; spec-level versions are now explicitly **derived summaries** (`"mixed"` when physical channels disagree).
- New `Channel.rotation = {repr, compose}`, mandatory (possibly `unknown`) for `ee_rotation*` channels.
- New block "四处相对上一版的修正，由来源 C 逼出来", mirroring the existing B block, with the reasoning (including that B already had the same latent bug on its gripper channels).

**§2.2d — P4**
- `raw_extra` split by granularity: episode-level → `raw_extra` JSON; frame-level → `raw.`-prefixed columns in `frames.parquet`, registered in `episode.json:raw_frame_columns`.

**§2.2e — P3**
- `has_video` split into `has_rgb` (any RGB, incl. C's inline frames) and `has_video` (decodable sidecar file, which the QC rules key off).
- `CameraSpec` value object defined for the first time (`mount`, `encoding`, resolution), with the wrist-camera and inline-frames rationale.

**§1 — P3 fallout**
- Explicit note that `--no-video` is a **no-op for C** (pixels are in the same records as the actions), so the stated tradeoff no longer misdescribes a quarter of the sources.

**§3 / §4 / §5 — P5**
- `VIDEO_FRAME_MISMATCH` dependency narrowed to sidecar video → cleanly `SKIPPED` on C.
- `sources` table gains `shard_layout_revision`.
- Idempotency section rewritten: A/B/D keep file-based hashing; C gets `upstream_id = "{split}/{shard}#{i}"` and `content_hash` over **normalized episode bytes**, plus the acknowledged cost that "skip download by hash" doesn't work for C.

**§8.1 / §8.3 / §8.4**
- `CameraSpec` added to the ubiquitous language and `domain/camera.py`.
- Four new domain invariants (9–12): channel-level semantics are authoritative and spec-level fields are derived-only; `ee_rotation*` ⟺ `rotation` non-null; `has_video` ⟹ at least one `mp4_sidecar`; `raw.`-prefix + registration enforced.

**Appendix C / D / comparison table**
- Concrete 10-channel expansion table for C showing that no column is homogeneous, camera spec block, the per-step `raw.*` note, and the no-stable-ID item; D's capabilities updated; comparison table's camera and delta rows corrected.

Not applied (they were in the "limitations / should-fix" tiers): the `terminate_episode` vs `is_terminal` authority swap, the `has_state` contradiction with A.C.7, padding-step → `transforms`, `fps_effective` nulling, per-step language variance, and the delta-without-anchor limitation. Say the word and I'll do those too.

---

## 第 4 轮 — 2026-08-08 08:24 UTC

> commit the changes

<sub>claude-opus-5 · 输出 1,494 tokens · 1m 11s</sub>

<details>
<summary><b>3 次工具调用</b> — run_in_terminal x3</summary>

1. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git status --short && echo '---' && git --no-pager diff --stat
    ```
2. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    git --no-pager log --oneline -10
    ```
3. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    git add docs/plan.md && git commit -F - <<'EOF'
    docs(plan): 由来源 C 逼出的 schema 修正（通道级语义 / 相机 / RLDS 幂等键）

    来源 C（OXE RLDS，berkeley_autolab_ur5）的形态暴露出上一版 schema 的五处硬伤，
    逐条修掉：

    - space / is_delta / frame 从 SignalSpec 下沉到 Channel。C 的 action 一个向量里
      同时有增量位姿、绝对夹爪命令和标志位，spec 级 is_delta=True 会对夹爪通道说谎，
      并传导进所有按 is_delta 分桶的统计。spec 级字段保留为派生汇总（不唯一时 mixed）。
    - 新增 Channel.rotation（repr / compose）。rotation_delta[3] + unit=rad 不足以
      确定语义，不知表示就无法积分与对齐；repr 允许 unknown，但字段不得缺席。
    - raw_extra 按粒度拆分：episode 级进 JSON，frame 级以 raw.* 列留在 parquet 并
      登记 raw_frame_columns。C 未建模的上游字段几乎全是逐 step 的。
    - 补上一直缺失的 CameraSpec（mount / encoding），并把 has_video 拆成
      has_rgb / has_video。C 的画面是内嵌帧而非 mp4，且带一路腕部相机；
      连带修正第 1 节 --no-video 取舍对 C 不成立。
    - RLDS 是唯一没有稳定上游 episode ID 的来源：一个 shard 装多条 episode。
      改为 upstream_id = split/shard#index，content_hash 算在归一化后的 episode
      字节上，sources 增列 shard_layout_revision，避免上游重新分片时整批老数据
      被误判为新增（直接影响“再跑一轮无新增”的验收）。

    同步更新：领域不变量 9~12、通用语言与 domain 布局、QC 规则依赖、附录 C/D 与
    四源对照表。
    EOF
    ```

</details>

Committed as `94f912f` (docs/plan.md only).

Left untracked deliberately:
- `` — should probably go in `.gitignore`
- `` — the plan calls for committing raw AI transcripts; want me to add it (and a `.gitignore` entry for `.DS_Store`) in a follow-up commit?
