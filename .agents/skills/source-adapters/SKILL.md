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

Implementations: `LeRobotAdapter` (shared by A and B, driven by `meta/info.json`),
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
   **A fixture must be a scale model of the data's _structure_, not of its _size_.** Every defect
   the M4 shakedown found (ADR 012, ADR 013) got through because a fixture reproduced the bytes
   but flattened a shape: one data file instead of two, one IMU window instead of a diverging one.
8. **Adapter behavior changes bump `adapter_version`**, which feeds the staleness predicate and
   triggers a targeted re-normalization — **and re-staging**. `raw/.../.staged.json` records the
   `adapter_version` that wrote the directory (`infrastructure/sources/staging.py`), because the
   staging _layout_ is the adapter's format even though the bytes inside it are upstream's.
   Without that check, an episode staged by a buggy version is unrepairable for the life of the
   store.

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
  ([ADR 005](../../../docs/adr/005-pusht-timestamps-are-synthesized.md)). The adapter _measures_
  this per episode rather than hardcoding it per source.
- Termination is decided by the **environment** (`coverage > 0.95`):
  `termination_source="env_rule"`, `success_adjudicator="simulator"`. LeRobot exported only
  `next.done`, so `is_truncated=None` — a known, recorded limitation, not a guess.
- **`dataset_from_index` / `dataset_to_index` are dataset-GLOBAL row ids, never offsets into the
  shard you just opened.** `pusht` has one data file so the two coincide there; `aloha` spans
  two, and slicing by position selected **0 rows** for all 35 episodes in `file-001`. Select with
  `table.filter(index >= start & index < stop)` on the parquet's own `index` column, and assert
  the row count equals `stop - start`
  ([ADR 013](../../../docs/adr/013-lerobot-global-index-and-qc-history.md)).
- `next.success` is absent from some exports; when it is, `success=None` **and**
  `success_adjudicator="none"`, never `success=False`.

`upstream_id` = `episode_{index:06d}` (stable). `content_hash` = the **canonical digest of the
normalized columns**, not the upstream file's sha256: one shared shard covers 206 episodes, so a
file hash cannot distinguish them.

---

## Source B — `lerobot/aloha_sim_insertion_human` (dual-arm 14-DoF, mixed units)

Same directory structure as A, so `LeRobotAdapter` is shared and driven by `meta/info.json`.
**Adding B needed zero adapter code** — one `sources.yaml` entry, one `embodiments.yaml` entry,
one fixture (M3).

50 Hz · 50 episodes · 25000 frames (exactly 500 rows each) · `action`/`observation.state` both
`float32[14]`, named `left_waist … left_gripper, right_waist … right_gripper`.

Row: `action` = target joint angles (rad) + gripper opening; `observation.state` = measured joint
angles (rad) + gripper opening.

**Traps**

- **Two units in one vector**: 12 joints in `rad` (`metric_convertible=true`), 2 grippers
  normalized (`metric_convertible=false`). Spec-level space is therefore `mixed`, not
  `joint_position` — it is _derived_ from the channels, never declared.
- **`arm_id` is mandatory.** `left_*` / `right_*` must become `arm_id="left"/"right"`, or dual-arm
  data cannot be split or aligned with single-arm data.
- **The gripper direction is unverifiable, so do not invent one.** Measured over 7,500 rows the
  values overflow `[0, 1]` in both directions (`left` reaches 1.162, `right` dips to −0.046), and
  open-vs-closed is stated nowhere. Recorded as `convention:
normalized_unverified_direction` with an identity inverse and **no `min`/`max`** (ADR 008).
  Never renormalize at ingestion.
- **B's clock is synthesized too.** `timestamp` is bit-identical to `float32(frame_index / fps)`
  at 50 Hz, so `timestamp_source="synthesized@50Hz"` and `TS_MONOTONIC` is `SKIPPED` — measured
  in M3, not inherited from A (ADR 008).
- action and state are the **same space, target vs measured** → the `STATE_ACTION_ECHO` false
  positive lives here. `is_command` is the distinguishing field, and the two channel lists are a
  YAML anchor alias of one another so they cannot drift.
- Camera count is **data-driven**: sim has only `observation.images.top`; real ALOHA commonly has
  4 (top / low / left_wrist / right_wrist). Never hardcode.
- The only unmodelled per-frame column is `next.done` → `raw.next.done`. There is no
  `next.reward` and no `next.success`, so `success_adjudicator=NONE` and `success=None`.
- 50 Hz vs A's 10 Hz: the same 8-second trajectory differs 5× in frame count. This is why export
  sampling cannot be proportional to frame count.

---

## Source C — OXE / RLDS `berkeley_autolab_ur5` (nested, no timestamps)

`episode → steps` nesting. Per step, as **measured** (`spikes/_out/probe_m3.txt`, `features.json`):

```python
"observation": {"image": uint8[480,640,3], "hand_image": uint8[480,640,3],
                "image_with_depth": float32[480,640,1], "robot_state": float32[15],
                "natural_language_instruction": string,
                "natural_language_embedding": float32[512]}
"action": {                                   # a dict, not a flat vector
   "world_vector":              float32[3],   # ee position delta (m), magnitude ~1e-2
   "rotation_delta":            float32[3],   # ee orientation delta (rad), "roll, pitch, yaw"
   "gripper_closedness_action": float32,      # SCALAR. +1 close / -1 open / 0 no change
   "terminate_episode":         float32,      # SCALAR, not [3] — ADR 003
}
"reward", "is_first", "is_last", "is_terminal"
```

**Flattening is our decision and becomes a public contract** — `dim=8, physical_dim=7,
space="mixed"` (the flattening order is `ACTION_KEYS` in `infrastructure/sources/rlds_adapter.py`):

| idx | name                     | role         | channel.space          | is_delta | frame  | unit       | is_physical |
| --- | ------------------------ | ------------ | ---------------------- | -------- | ------ | ---------- | ----------- |
| 0-2 | `ee.dx/dy/dz`            | end_effector | `ee_translation_delta` | **true** | `base` | m          | true        |
| 3-5 | `ee.drx/dry/drz`         | end_effector | `ee_rotation_delta`    | **true** | `base` | rad        | true        |
| 6   | `gripper`                | gripper      | `gripper`              | **true** | None   | normalized | true        |
| 7   | `flag.terminate_episode` | control_flag | `flag`                 | false    | None   | None       | **false**   |

**Not one column of that table is homogeneous.** This is the entire argument for channel-level
semantics.

**Traps**

- **Deltas and absolutes in one vector**: the poses are deltas, and so is the gripper — it is a
  ternary _change_ command, not an opening. Bucket by `is_delta` at channel granularity before
  any statistic.
- **`rotation_delta`'s composition order is declared nowhere.** The axes are named ("roll, pitch,
  yaw") but the order is not, so `rotation.repr="euler_rpy"` with `compose="unknown"`. Do not
  reach for `euler_xyz` — it asserts an order nobody stated (ADR 009).
- **No timestamps.** `control_hz` is **required** in `config/sources.yaml`; the adapter raises
  rather than defaulting. `provenance.timestamp_source="synthesized@5Hz"`, and all timestamp
  rules become `SKIPPED`.
- **Trailing padding steps, plural.** `is_last`/`is_terminal` are set on the final **two** steps,
  both with an all-zero pose action. But zero actions also occur _mid_-episode, so trim only the
  trailing run where `is_last` is truthy **and** `world_vector` and `rotation_delta` are both
  zero. Record the count, the trimmed rewards and the terminal reward in `raw_extra`, plus a
  `trim_trailing_steps` entry in `provenance.transforms` (ADR 009).
- **`terminate_episode` is a control flag, not a physical quantity.** `is_physical=False`,
  excluded from `ACTION_RANGE`/`ACTION_JERK`/`STATIC_EPISODE`. Here the ending is decided by the
  **policy**: `termination_source="policy_flag"`, `success_adjudicator="policy"`, `success=None`
  (meaning "unknown", unlike D). The terminal reward of 1.0 is what the environment paid, not a
  verdict — it goes to `raw_extra`, never to `boundary.success`.
- **Read `is_last` and `is_terminal` separately.** `is_last & ~is_terminal` ⟹ truncated.
- **`observation.robot_state[15]` semantics are undocumented** — the description literally defers
  to a web page. Write `StateSpec.space="unknown"`, channel `role="unknown"`, `is_physical=True`
  (they _are_ measurements; we just cannot say of what — and `False` would make the spec space
  compute to `none` instead of `unknown`). Do not invent roles.
- **Per-step extras go to `raw.` columns**, not `raw_extra`: `reward`, `is_first/is_last/
is_terminal`. `natural_language_instruction` is **lossless** but cannot be a frame column —
  `FrameTable.canonical_digest` casts every column to `<f8`, so it becomes `EpisodeMeta.task`
  plus a `raw_extra` copy. `natural_language_embedding` (512-D) is **droppable** — recomputable
  from text and bound to an encoder version — via `drop_channels` in config, recorded in
  `provenance.transforms`.
- **Cameras are inline frames, not mp4**, and one of them is a **wrist** camera:

```json
"cameras": [
  {"name": "image",            "mount": "static", "resolution": [480, 640], "encoding": "inline_frames"},
  {"name": "hand_image",       "mount": "wrist",  "resolution": [480, 640], "encoding": "inline_frames"},
  {"name": "image_with_depth", "mount": "static", "resolution": [480, 640], "encoding": "inline_frames"}
]
```

So `has_rgb=true, has_depth=true, has_video=false`, `VIDEO_FRAME_MISMATCH` resolves to `SKIPPED`,
and `--no-video` is a no-op for C. Violent frame-to-frame change is **normal** on `wrist`,
anomalous on `static`. `is_present` is **measured** from the staged record's bytes, not declared
from `features.json` — the committed fixture strips the payloads and must report `false`.

### C's identity problem — this one lands directly on an acceptance criterion

One TFRecord shard holds **many** episodes, and a record's only handle is its position inside a
shard file whose name encodes the _current_ shard count. Hashing the shard gives every episode in
it the same hash; and an identity built from the shard makes a re-shard look like new data.

So identity is **layout-independent** — the episode's cumulative index within the split, derived
from `dataset_info.json`'s `shardLengths`:

```
upstream_id  = f"{split}#{global_index:06d}"          # NOT f"{split}/{shard}#{i}"
content_hash = sha256(canonical bytes of the NORMALIZED episode)   # not the shard file
adapter_version = f"rlds@1.0.0+layout={shard_layout_revision}"     # re-sharding = stale, not new
```

Two things to keep straight:

- **No `/` in `upstream_id`.** `IngestEpisodes._staging_dir` uses it verbatim, so a slash would
  silently invent a directory level.
- **The layout is a staleness key, never an identity key.** It rides in `adapter_version`, so
  declaring a new `shard_layout_revision` in `config/sources.yaml` yields
  `Staleness.REDO_NORMALIZE` — every episode re-normalized and re-hashed, none re-discovered.
  `list_episodes` records both the declared and the measured layout in `raw_extra`, and
  deliberately does **not** raise on a mismatch: a re-shard that failed the whole run would make
  the corrective config edit unreachable.

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

- **The IMU unit convention was measured, not read** (ADR 004): accel is `m/s^2` (mean |accel| =
  9.8998 on `P01_101`), gyro is `rad/s`.
- **Three clocks, and two of them are the IMU.** Gyro and accel are shipped as two files
  (`-gyro.csv`, `-accl.csv` — upstream spells it `accl`) with two `Milliseconds` columns that
  **measurably disagree**: 793 of `P28_101`'s 141,924 samples, by up to 15 ms. They therefore go
  to `streams/gyro.parquet` and `streams/accel.parquet`, each on its own timeline (ADR 012).
  Never resample either into the frame table, and never join them to each other by row index.
- **The IMU rate is per-video and is not declared anywhere**: 5.128205 ms on `P01_101`, ~5.05 ms
  on `P01_103` and `P28_101`. There is no `imu_hz` key in config — the stream carries the
  timestamps upstream shipped.
- **Two frame numberings** (ADR 010). The annotation CSV's `start_frame`/`stop_frame` are counted
  at the JPEG **extraction** fps (50 for 50 fps videos, a flat 60 otherwise); EPIC-Fields pose
  keys are 1-based at the **official** fps. `frames.parquet` uses the **official** one, because
  it is the only clock the pose layer joins on; the CSV's is preserved verbatim under
  `raw_extra.epic.extraction_numbering` with a note that the two are not comparable.
- **Unregistered pose frames are NULL** — not zero, not interpolated. EPIC-Fields registered
  ~18.7M of ~20M frames.
- **QC severity downgrade applies here.** A COLMAP pose jump is reconstruction failure, not data
  corruption; `origin != "measured"` ⟹ FAIL becomes REVIEW with the basis stated. On A/B/C this
  rule is an identity transform — **only D verifies it works.**
- **Capabilities differ per episode within the source.** IMU covers only EK-100 extension videos;
  EPIC-Fields covers 671/700 with possible per-frame gaps. The three pinned videos give three
  profiles: `P01_01` neither (its IMU files 404), `P01_103` IMU only, `P28_101` both. Layer
  availability is probed **once, in `fetch`**, and persisted into `ref.json` — never re-probed in
  `normalize`, or a resumed run's result would depend on the network.
- **List round-robin across videos**, not video-major: with video-major ordering a
  `--max-episodes 20` run returns twenty episodes of one video and the heterogeneity above never
  appears.
- **`P01_101` is not in `EPIC_100_train.csv`.** The split jumps `P01_102`…`P01_109`. It appears in
  M0's ADR 004 measurements (which read the IMU files directly) but can never produce an episode.
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
- `unified-schema` skill — field semantics and the 19 invariants
- `architecture` skill — where adapters sit and what they may not import
