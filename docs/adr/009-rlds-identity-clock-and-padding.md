# ADR 009 — RLDS identity survives a re-shard; its clock, padding and rotation are ours to name

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M3 (breadth: sources B and C)
- **Affects:** design §2.2b (`RotationRepr`), §2.2f (`timestamp_source`), §4 (identity and
  staleness), Appendix A.C, `domain/action_spec.py`, `infrastructure/sources/rlds_adapter.py`,
  `infrastructure/sources/tfrecord.py`, `config/sources.yaml`

## Context

Source C (`berkeley_autolab_ur5`, OXE / RLDS) is the first source that is not a table. It is a
sequence of `tf.train.Example` records inside TFRecord shards, and it withholds three things
every previous source supplied for free: a stable episode identifier, a clock, and any
statement about where an episode actually ends.

Each of the three has exactly one honest answer, and none of them is "pick something sensible".

## Findings — measured from the cached 150 MB shard prefix

`spikes/_out/probe_m3.txt`, plus the bucket's `dataset_info.json` and `features.json`.

1. **There is no episode id.** A record's only handle is its ordinal position inside a shard
   file whose name encodes the _current_ shard count: `…-train.tfrecord-00000-of-00412`.
   `dataset_info.json` gives `shardLengths` — 412 entries for `train`, summing to 896 episodes —
   which is a property of this release's packing, not of the data.
2. **There is no timestamp.** No field named `timestamp`, `time`, `t`, or `dt` exists at any
   nesting level, in `features.json` or in the records.
3. **Every episode ends with two placeholder steps.** For episode 0 (71 steps) and episode 1 (76
   steps) alike, `is_last`, `is_terminal` and `action/terminate_episode` are all set on the
   **final two** steps, and both of those steps carry `world_vector == [0,0,0]` and
   `rotation_delta == [0,0,0]`. The non-zero reward lands on the first of the two.
4. **Zero actions also occur mid-episode** — episode 0 at step 68, episode 1 at step 0 — where
   `is_last` is _not_ set. So "all-zero action" alone is not a padding signal.
5. **`rotation_delta` is documented as "Delta change in roll, pitch, yaw".** The axes are named;
   the composition order is not, anywhere.
6. **`robot_state` is 15 float32 with no documentation** beyond a link to a project web page.
7. **`gripper_closedness_action` is ternary**: "1 if close gripper, -1 if open gripper, 0 if no
   change" — a _change_ command, not a position.
8. **`action/terminate_episode` is a scalar**, not the `float32[3]` some OXE datasets use. The
   action is therefore 8-D, of which 7 are physical (ADR 003).
9. **An episode costs ~55 MB**, almost entirely inline camera frames: `image` 31 MB,
   `image_with_depth` 12 MB, `hand_image` 11 MB, everything else under 150 KB combined.

## Decision

### 1. Identity is the index within the split, not the location in a shard

`upstream_id = f"{split}#{global_index:06d}"`, where `global_index` is the cumulative episode
index across the split's shards, derived from `shardLengths`. The shard file name and the offset
inside it are carried in `EpisodeRef.extra` → `raw_extra` as **locators**, never as identity.

The separator is `#`, not `/`: `IngestEpisodes` derives a staging directory from `upstream_id`
verbatim, so a slash would silently invent a directory level.

### 2. The shard layout is a staleness key, not an identity key

`config/sources.yaml` declares `shard_layout_revision: "train:412-shards@0.1.0"`, which the
adapter folds into `adapter_version` as `rlds@1.0.0+layout=…`. Consequences follow from
machinery that already exists:

- Upstream re-shards → identities are unchanged, so no episode is ever **discovered** twice.
- The operator declares the new layout → `adapter_version` changes →
  `Staleness.REDO_NORMALIZE` → every episode is re-normalized and its `content_hash`
  re-verified against the new packing.

`list_episodes` also computes the layout it actually _measured_ from `dataset_info.json` and
records both values per episode. It deliberately does **not** raise on a mismatch: a re-shard
that failed the entire run would make the corrective config edit impossible to reach.

This is the whole mechanism. No "did the sharding move" query, no new column, no application
change.

### 3. The clock is synthesized from a declared rate, and the adapter refuses to guess one

`t = arange(n) / control_hz`, with `control_hz` **required** in `config/sources.yaml` (5 Hz for
this dataset, from the OXE dataset card). A missing value raises `InvariantViolation` rather
than defaulting, because a default control rate would fabricate the entire time axis and every
duration, `fps_effective` and rate-based statistic derived from it.

`timestamp_source = "synthesized@5Hz"`, so `TS_MONOTONIC` resolves to
`SKIPPED(synthetic_timestamp)` — as it does for A and B, for the same reason.

### 4. Trailing boundary steps are trimmed, and what they carried is hoisted

Trim the trailing run of steps where `is_last` is truthy **and** both `world_vector` and
`rotation_delta` are exactly zero. Finding 4 is why the flag is part of the test: genuine
mid-episode zero actions are untouched.

Trimming is a lossy transform, so it is recorded rather than performed silently:
`provenance.transforms` gains `{"op": "trim_trailing_steps", "n": …}`, and `raw_extra["rlds"]`
keeps `n_steps_upstream`, `n_trailing_boundary_steps_trimmed`, the trimmed steps' rewards and
`is_terminal` values, and the episode's `terminal_reward`.

The reward is **not** promoted to `boundary.success`. A reward of 1.0 is what the environment
paid, not a verdict anyone rendered; `success` stays `None` with
`success_adjudicator = POLICY`, which reads as "an adjudicator exists but published no verdict"
— materially different from source D's "no adjudicator exists at all".

### 5. `RotationRepr.EULER_RPY` is added to the domain

The existing members force a false choice: `EULER_XYZ` / `EULER_ZYX` assert an order upstream
never states, and `UNKNOWN` throws away the axis naming upstream _does_ state. `EULER_RPY` says
exactly what is known — roll, pitch, yaw, order unspecified — and the channels carry
`compose: unknown` alongside it.

This is the **only** change to `domain/` in M3. Adding an enum member cannot invalidate any
existing episode, so `SCHEMA_VERSION` stays at `1.0`.

### 6. Undocumented state is 15 channels of `unknown`, all `is_physical`

`robot_state` becomes `robot_state_00 … robot_state_14` with `role=unknown`, `space=unknown`,
`metric_convertible=false`. Naming them "joint 0..5, pose 6..12, …" would be a guess the rest of
the pipeline would then treat as fact. `is_physical` stays `true` — they _are_ measurements, we
simply cannot say of what — which is also what makes `state_spec.space` compute to `unknown`
rather than `none`.

### 7. Pixels are cameras, not video, and presence is measured

Three `CameraSpec`s with `encoding=inline_frames`: `image` and `image_with_depth` (static),
`hand_image` (wrist). `has_rgb=true`, `has_depth=true`, **`has_video=false`** — there is no
decodable video file, so every video-dependent rule must degrade to `SKIPPED`, and the schema's
split between the two flags earns its keep here for the first time.

`is_present` is measured from whether the staged record actually holds bytes, not declared from
`features.json`. The committed fixture strips the payloads, and it must say `false`.

### 8. Fetch streams the shard and stages the record verbatim

`UpstreamFetcher.open_stream` reads the shard as a stream and stops at the wanted record, so a
55 MB episode does not require buffering a 180 MB shard. The record is staged **byte for byte**;
the 31 MB of image data is not pruned at fetch time, because `raw/` is authoritative and
immutable, and a pruned raw is not a raw.

`natural_language_embedding` (512-D per step, recomputable from the instruction text) is dropped
during _normalization_, via `drop_channels` in config, and recorded in `provenance.transforms`.

`max_episodes` is set to **12**, down from the 80 the plan sketched: at ~55 MB staged per
episode that is already ~660 MB, and breadth here is a property of having four sources, not of
having many episodes of one.

### 9. The TFRecord reader is ours, and skips CRCs

`infrastructure/sources/tfrecord.py` implements TFRecord framing and the `tf.train.Example`
wire format in ~130 lines of stdlib Python, per ADR 001. CRC32C is not verified: corruption
surfaces as a parse error, and integrity downstream is `content_hash` over normalized bytes.
(The fixture builder writes _correct_ CRCs anyway, so the committed fixture is a genuine
TFRecord.)

## Consequences

- **`application/` is untouched by source C.** Identity, staleness, resume and idempotency all
  work because C's facts were routed into existing concepts — `adapter_version`, `raw_extra`,
  `provenance.transforms` — instead of new ones.
- **Sequential streaming re-reads.** Fetching episode _k_ of a shard reads records 0.._k_, so
  ingesting a 3-episode shard transfers ~330 MB rather than ~165 MB. Accepted: throughput is
  explicitly not a goal this round, and the alternative (caching record byte offsets and issuing
  HTTP range requests) adds cross-episode state to an adapter whose per-episode isolation is
  what makes crash-resume simple. If it ever matters, that is the fix, and it is confined to
  `RLDSAdapter._read_record`.
- **`language_instruction` cannot be a frame column.** `FrameTable.canonical_digest` casts every
  column to `<f8`, so strings are structurally impossible there. It is constant per episode, so
  it becomes `EpisodeMeta.task` plus a `raw_extra` copy — no schema change needed.
- **A fourth `Unit` was not needed, and no new `ChannelSpace` was added.** The ternary gripper
  command is `space=gripper, is_delta=true` with its ternary meaning in
  `gripper.convention`; the terminate flag is `space=flag, is_physical=false`. Both existed
  already. The one gap the schema really had was the rotation representation.
- Source C is the source that would have broken a spec-level `space` or `is_delta`: within one
  8-D vector it holds a translation delta, a rotation delta, a gripper change command and a
  non-physical flag. `SpecSpace.MIXED` with `dim=8, physical_dim=7` is the honest summary, and
  it is _derived_ from the channels, never declared.
