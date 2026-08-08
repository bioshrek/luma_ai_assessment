---
name: source-adapters
description: Use when writing, debugging, or extending a source adapter in infrastructure/sources/ — LeRobot (pusht, aloha), RLDS/OXE (berkeley_autolab_ur5), EPIC-KITCHENS-100 — or when adding a fifth source. Covers the anti-corruption-layer protocol for upstream facts the schema cannot express, per-source concrete data shapes and traps, upstream identity and content_hash rules (especially RLDS re-sharding), layered availability and graceful degradation, and the SourcePort contract.
---

# Source Adapters — the anti-corruption layer

> **Source of truth is code:** once `src/rdp/infrastructure/sources/` exists, the adapters and
> their golden fixtures are the truth. Where this file and the code disagree, **code wins** — then
> update this file. Until then, [docs/technical_design.md](../../../docs/technical_design.md) §1.1, §2.3, and Appendix A are the spec.
>
> **`src/rdp/infrastructure/sources/` does not exist yet.** Adapters arrive per milestone: A in
> M1, B and C in M3, D in M4.

## The contract

```python
class SourcePort(Protocol):
    source_id: str
    def list_episodes(self) -> Iterator[EpisodeRef]: ...   # list only, no download
    def fetch(self, ref: EpisodeRef, dest: Path) -> RawEpisode: ...
    def normalize(self, raw: RawEpisode) -> CanonicalEpisode: ...
```

Adapters live in `infrastructure/`. They are the **only** place that knows an upstream format.
Adding a source = one class + one entry in `config/sources.yaml` + characterization tests.
`domain/` and `application/` change **zero lines**.

Planned implementations: `LeRobotAdapter` (shared by A and B, driven by `meta/info.json`),
`RLDSAdapter` (C), `EpicKitchensAdapter` (D), and `HDF5Adapter` held in reserve (robomimic/ALOHA)
in case C's TFDS environment proves unworkable.

## Iron rules for every adapter

1. **Never trust upstream field names.** pusht names its channels `motor_0`/`motor_1`; they are
   not motors, they are task-space xy in pixels. Semantics are asserted by our own
   `config/embodiments.yaml`, in the adapter.
2. **Never guess.** `space="unknown"`, `role="unknown"`, `repr="unknown"`, `state=NULL` +
   `raw_extra` are all legal outputs. A plausible wrong guess is worse than an explicit absence.
3. **Never zero-fill.** Missing per-frame values are NULL. Missing signals mean
   `level="absent"` + `has_* = False`.
4. **Never discard what you do not understand.** Episode-level → `raw_extra`; per-frame →
   `raw.<name>` columns registered in `raw_frame_columns`.
5. **Dropping channels is a lossy transform** and must be recorded in `provenance.transforms`.
6. **Degrade, do not error.** A missing layer degrades that layer's capabilities; the source
   still ingests.
7. **Characterization tests with golden fixtures**, not strict TDD — adapters depend on real
   formats that must be explored first. Fixtures are a few dozen frames per source, committed,
   < 1 MB total. A wrong channel mapping is the most insidious bug in this project.
8. **Adapter behavior changes bump `adapter_version`**, which feeds the staleness predicate and
   triggers a targeted re-normalization.

## The ACL protocol: upstream fact the schema cannot express

Do **not** widen the schema reflexively. Route it:

```
Can the fact be expressed by an existing field?           → express it. Done.
Is it per-episode and unmodeled?                          → raw_extra (JSON). No version bump.
Is it per-frame and unmodeled?                            → raw.<name> column + raw_frame_columns. No version bump.
Are the semantics unclear?                                → "unknown" enum value + raw_extra. No version bump.
Is it a genuinely new *dimension* of meaning, with evidence?
                                                          → ADR + schema change + schema_version bump.
```

The pressure stays at the ACL boundary and never seeps into the domain. Every real schema change
in this project's history took the last path only after a source proved the dimension existed:
B forced `unit`/`gripper` down to channel level, C forced `space`/`is_delta`/`frame` down and
added `rotation`, D forced `level`, `origin`, `clock`, and `success_adjudicator`.

---

## Source A — `lerobot/pusht` (2D planar pushing, pixel units)

LeRobot `codebase_version: v3.0` — **all** episodes share one data file and one video; per-episode
files do not exist ([ADR 002](../../../docs/adr/002-lerobot-v3-layout-and-lost-termination.md)):

```
pusht/
  meta/info.json                        # robot_type, fps, totals, features dtype/shape/names
  meta/tasks.parquet                    # task_index -> natural-language task
  meta/episodes/chunk-000/file-000.parquet   # per-episode length + dataset_from/to_index
  data/chunk-000/file-000.parquet       # every episode's rows, concatenated
  videos/...                            # one mp4 covering all episodes
```

10 Hz · 206 episodes · ~25650 frames · `action`/`observation.state` both `float32[2]`.

One row: `action = [222.0, 97.0]` (pusher target xy, **pixels**, range ~[0, 512]);
`observation.state = [221.4, 98.7]`; plus `next.reward`, `next.done`, `next.success`,
`timestamp`, `frame_index`, `episode_index`, `index`, `task_index`.

**Traps**

- `names: ["motor_0", "motor_1"]` is **misleading** — it is task-space xy, `role="end_effector"`,
  `space="cartesian_2d"`.
- `unit="px"`, `metric_convertible=false`. There is no scene scale; converting to meters is
  fabrication.
- No gripper, no joints, no orientation. This is the "non-standard, non-single-arm" source.
- **`next.reward` is lossless.** It is the T-block/goal polygon overlap ratio, and the T block's
  pose is stored **nowhere** — unrecomputable once dropped.
- **`timestamp` is not a clock.** It is bit-for-bit `float32(frame_index / fps)`, so
  `timestamp_source="synthesized@10Hz"` and every timestamp rule is `SKIPPED`
  ([ADR 005](../../../docs/adr/005-pusht-timestamps-are-synthesized.md)). The adapter *measures*
  this per episode rather than hardcoding it per source.
- Termination is decided by the **environment** (`coverage > 0.95`):
  `termination_source="env_rule"`, `success_adjudicator="simulator"`. LeRobot exported only
  `next.done`, so `is_truncated=None` — a known, recorded limitation, not a guess.
- `next.success` is absent from some exports; when it is, `success=None` **and**
  `success_adjudicator="none"`, never `success=False`.

`upstream_id` = `episode_{index:06d}` (stable). `content_hash` = the **canonical digest of the
normalized columns**, not the upstream file's sha256: one shared shard covers 206 episodes, so a
file hash cannot distinguish them.

---

## Source B — `lerobot/aloha_sim_insertion_human` (dual-arm 14-DoF, mixed units)

Same directory structure as A, so `LeRobotAdapter` is shared and driven by `meta/info.json`.

50 Hz · 50 episodes · ~20000 frames · `action`/`observation.state` both `float32[14]`, named
`left_waist … left_gripper, right_waist … right_gripper`.

Row: `action` = target joint angles (rad) + gripper opening; `observation.state` = measured joint
angles (rad) + gripper opening.

**Traps**

- **Two units in one vector**: 12 joints in `rad` (`metric_convertible=true`), 2 grippers
  normalized (`metric_convertible=false`).
- **`arm_id` is mandatory.** `left_*` / `right_*` must become `arm_id="left"/"right"`, or dual-arm
  data cannot be split or aligned with single-arm data.
- **Two grippers**, potentially with different conventions and inverse parameters — which is why
  `gripper` is a channel-level field, not a spec-level one.
- action and state are the **same space, target vs measured** → the `STATE_ACTION_ECHO` false
  positive lives here. `is_command` is the distinguishing field.
- Camera count is **data-driven**: sim usually has only `top`; real ALOHA commonly has 4
  (top / low / left_wrist / right_wrist). Never hardcode.
- 50 Hz vs A's 10 Hz: the same 8-second trajectory differs 5× in frame count. This is why export
  sampling cannot be proportional to frame count.

---

## Source C — OXE / RLDS `berkeley_autolab_ur5` (nested, no timestamps)

`episode → steps` nesting. Per step:

```python
"observation": {"image": uint8[480,640,3], "hand_image": uint8[480,640,3], "state": float32[15]}
"action": {                                   # a dict, not a flat vector
   "world_vector":              float32[3],   # ee position delta (m), magnitude ~1e-2
   "rotation_delta":            float32[3],   # ee orientation delta (rad)
   "gripper_closedness_action": float32[1],   # maybe -1=open / +1=closed
   "terminate_episode":         float32[3],
}
"reward", "discount", "is_first", "is_last", "is_terminal",
"language_instruction", "language_embedding": float32[512]
```

**Flattening is our decision and becomes a public contract** — `dim=10, physical_dim=7,
space="mixed"`:

| idx | name                        | role         | channel.space          | is_delta  | frame  | unit       | is_physical |
| --- | --------------------------- | ------------ | ---------------------- | --------- | ------ | ---------- | ----------- |
| 0-2 | `ee.dx/dy/dz`               | end_effector | `ee_translation_delta` | **true**  | `base` | m          | true        |
| 3-5 | `ee.drx/dry/drz`            | end_effector | `ee_rotation_delta`    | **true**  | `base` | rad        | true        |
| 6   | `gripper`                   | gripper      | `gripper`              | **false** | None   | normalized | true        |
| 7-9 | `flag.terminate_episode[i]` | control_flag | `flag`                 | false     | None   | None       | **false**   |

**Not one column of that table is homogeneous.** This is the entire argument for channel-level
semantics.

**Traps**

- **Deltas and absolutes in one vector**: poses are deltas, the gripper is an absolute command.
  Bucket by `is_delta` at channel granularity before any statistic.
- **`rotation_delta`'s representation is declared nowhere.** Axis-angle / rotvec / Euler XYZ /
  Euler ZYX are all 3 radians. Check the dataset card in M0; failing that write
  `rotation.repr="unknown"` — the field must exist.
- **No timestamps.** Only an implied control rate (~5 Hz). Synthesize:
  `provenance.timestamp_source="synthesized@5Hz"`, and all timestamp rules become `SKIPPED`.
- **Trailing padding step.** RLDS's last step often carries a zero/placeholder action; counting it
  pollutes `ACTION_JERK` and static detection. Trim by `is_last`, record in `raw_extra`.
- **`terminate_episode` is a control flag, not a physical quantity.** `is_physical=False`,
  excluded from `ACTION_RANGE`/`ACTION_JERK`/`STATIC_EPISODE`. Here the ending is decided by the
  **policy**: `termination_source="policy_flag"`, `success_adjudicator="policy"`, `success=None`
  (meaning "unknown", unlike D).
- **Read `is_last` and `is_terminal` separately.** `is_last & ~is_terminal` ⟹ truncated.
- **`observation.state[15]` semantics are undocumented and inconsistent across sub-datasets.**
  Write `StateSpec.space="unknown"`, channel `role="unknown"`, preserve verbatim in `raw_extra`.
  Do not invent roles.
- **Per-step extras go to `raw.` columns**, not `raw_extra`: `discount`, `is_first/is_last/
is_terminal`, `language_instruction`. `language_instruction` is **lossless**;
  `language_embedding` (512-D) is **droppable** — recomputable from text and bound to an encoder
  version.
- **Cameras are inline frames, not mp4**, and one of them is a **wrist** camera:

```json
"cameras": [
  {"name": "image",      "mount": "static", "resolution": [480, 640], "encoding": "inline_frames"},
  {"name": "hand_image", "mount": "wrist",  "resolution": [480, 640], "encoding": "inline_frames"}
]
```

So `has_rgb=true, has_video=false`, `VIDEO_FRAME_MISMATCH` resolves to `SKIPPED`, and `--no-video`
is a no-op for C. Violent frame-to-frame change is **normal** on `wrist`, anomalous on `static`.

### C's identity problem — this one lands directly on an acceptance criterion

One TFRecord shard holds **many** episodes, and an episode's only identity is its index in the
shard. Hashing the shard gives every episode in it the same hash; and when upstream re-shards,
every index shifts and the second run treats the entire old corpus as new.

```
upstream_id  = f"{split}/{shard_basename}#{index_in_shard}"
content_hash = sha256(canonical bytes of the NORMALIZED episode)   # not the shard file
sources.shard_layout_revision                                       # re-sharding = stale, not new
```

**Canonical bytes are not parquet file bytes.** Compressor, row-group layout, and writer version
all change file bytes for identical content. Definition: in the spec's declared channel order,
convert each column to float64 little-endian raw bytes, concatenate, prefix with a key-sorted
metadata JSON (column names, dtypes, row count), sha256 the whole.

**Documented cost**: C's `content_hash` can only be computed _after_ normalization, so
"skip early based on the hash" does not apply to C. Skipping relies on `upstream_id`;
`content_hash` serves post-hoc verification and staleness detection.

### C's environment risk

Installing TFDS/TensorFlow is the project's largest technical uncertainty. Python is pinned to
**3.12** precisely because TF ≥ 2.16 supports it and 3.13+ does not. Spike this on day one (M0);
if it fails, fall back to direct TFRecord parsing or swap C for an HDF5 source, and record an ADR.

---

## Source D — EPIC-KITCHENS-100 (human egocentric, layered)

**The official release is authoritative.** A local 512×288/30fps copy is a re-encode from another
project and is demoted to an optional mirror recorded in `provenance.mirrors`.

**Episode granularity = one action segment, not a whole video.** `P01_01` runs 1652 s (~80k
frames); a segment of a few seconds is what corresponds to a demonstration trajectory.
`episode_uid = "epic100:P01_01_16"` using the official `narration_id` — a **stable upstream ID**,
so unlike C no synthesis is needed.

### Layers (each independently available; a missing layer degrades only itself)

| Layer                           | Contents                                                      | This round                                     |
| ------------------------------- | ------------------------------------------------------------- | ---------------------------------------------- |
| `epic-kitchens-100-annotations` | 89.9K action segments, 97 verbs / 300 nouns, 20.5K narrations | Required                                       |
| **EPIC-Fields**                 | 6-DoF camera extrinsics + intrinsics, 671/700 videos, COLMAP  | Required                                       |
| **IMU**                         | Gyro + accel, EK-100 extension only (EK-55 videos lack it)    | A few videos                                   |
| VISOR / EPIC-SOUNDS             | Masks + hand-object relations / audio events                  | Not taken (§11)                                |
| Video                           | 700 videos, 1080p @ 50/59.94 fps, hundreds of GB              | Not by default; `--with-video` uses the mirror |

Declared as `layers: [annotations, camera_pose, imu]` in `config/sources.yaml`.

**Traps**

- **`level="episode_label"`, not `space="none"`.** `has_action=True`, `physical_dim=0`, and
  `frames.parquet` has **no** action column at all. `task = "open door"` (verb + noun); original
  narration and `verb_class`/`noun_class` go to `raw_extra`. Per-frame numeric rules resolve to
  `SKIPPED(reason=action_level_is_episode_label)` — a specific reason, not "no action".
- **Mixed provenance inside one episode** — the only source with this:

| Group      | `role` | `space`                   | `unit`  | `metric_convertible`               | `origin`    | clock        |
| ---------- | ------ | ------------------------- | ------- | ---------------------------------- | ----------- | ------------ |
| `gyro[3]`  | head   | `imu_angular_velocity`    | `rad/s` | true                               | `measured`  | own_timeline |
| `accel[3]` | head   | `imu_linear_acceleration` | `m/s^2` | true                               | `measured`  | own_timeline |
| `cam_t[3]` | head   | `camera_translation_abs`  | `None`  | **false** (SfM scale is arbitrary) | `estimated` | frame        |
| `cam_q[4]` | head   | `camera_rotation_abs`     | `None`  | false                              | `estimated` | frame        |

`cam_q` carries `rotation={"repr": "quat_wxyz", "compose": None}`, `frame="world"`.
SfM has **mathematically no scale** — this is not "we could not find the unit". Hard-coding
`unit="m"` would make consumers treat reconstruction coordinates as meters.

- **Measure the IMU unit convention empirically in M0** (`rad/s` vs `deg/s`). Do not copy the
  documentation.
- **Two clocks.** IMU ~200 Hz vs video 50/59.94 fps. IMU goes to
  `normalized/<...>/streams/imu.parquet` with its own `t`. Never resample into the frame table.
- **Unregistered pose frames are NULL** — not zero, not interpolated. EPIC-Fields registered
  ~18.7M of ~20M frames.
- **QC severity downgrade applies here.** A COLMAP pose jump is reconstruction failure, not data
  corruption; `origin != "measured"` ⟹ FAIL becomes REVIEW with the basis stated. On A/B/C this
  rule is an identity transform — **only D verifies it works.**
- **Capabilities differ per episode within the source.** IMU covers only EK-100 extension videos;
  EPIC-Fields covers 671/700 with possible per-frame gaps. Sample selection must deliberately
  include **at least one video with IMU and one without**. The acceptance assertion: two episodes
  under one `source_id` with different `capabilities_json` and correspondingly different QC
  outcomes (one `PASS`, one `SKIPPED`).
- **Seconds are authoritative; frame indices are derived.**
  `timestamp_source="annotation_seconds"`, `frame_index_source="derived_from_seconds@<official_fps>"`.
  Official videos are 50 or 59.94 fps; the mirror is 30 fps — the same segment has different frame
  indices in each. Derived quantities must carry their parameters or staleness is undecidable.
- **Boundary**: `termination_source="annotator"`, `end_reason="annotation_bound"`, `success=None`,
  **`success_adjudicator="none"`**. D's `None` and C's `None` are opposites: C means "unknown but
  adjudicable", D means "no adjudicator exists". Success-rate denominators must exclude D.
- **`content_hash` must cover all enabled layers.** Otherwise "EPIC-Fields was downloaded later"
  looks like no change and gets skipped.
- **License is CC BY-NC 4.0 (non-commercial)**, unlike A/B/C. Record it in `sources.license` and
  on every export line; any subset containing D is bound by it.
- **No absolute local paths in committed files.** The mirror path lives only in
  `config/sources.local.yaml` (gitignored); the repo has a `${EPIC_KITCHENS_MIRROR}` placeholder.

---

## Adding a fifth source

1. Spike the raw data first. Dump the metadata and the first few rows. Do not write the adapter
   from documentation.
2. List every fact the current schema cannot express. Route each one through the ACL protocol
   above.
3. Write characterization tests with a committed mini fixture, asserting channel names, units, and
   gripper conventions.
4. Implement `SourcePort`. Add the config entry. Add `embodiments.yaml` assertions.
5. If `domain/` had to change, that is an ADR — not a quick edit.

## References

- [docs/technical_design.md](../../../docs/technical_design.md) Appendix A — full data shapes and the four-source comparison table
- [docs/technical_design.md](../../../docs/technical_design.md) §1.1 — source D's layered design in full
- [docs/technical_design.md](../../../docs/technical_design.md) §2.3 — the adapter contract; §5 — identity, hashing, staleness
- `unified-schema` skill — field semantics and the 17 invariants
- `architecture` skill — where adapters sit and what they may not import
