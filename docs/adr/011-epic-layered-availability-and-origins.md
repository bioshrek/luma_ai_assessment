# ADR 011 — Source D is layered: availability is data, and capabilities vary per episode

- **Status:** Accepted — **§3's gyro/accel join rule is superseded by ADR 012**
- **Date:** 2026-08-09
- **Milestone:** M4 (depth: source D)
- **Affects:** design §2.2 (`Capabilities`, `SignalOrigin`, `stream_specs`), §2.4 (store layout),
  §3 (QC gating and invariant 13), §6 (export licence), Appendix A.D,
  `domain/{episode,embodiment}.py`, `domain/qc/engine.py`, `domain/qc/rules/{action_range,
pose_coverage}.py`, `infrastructure/sources/epic_adapter.py`,
  `infrastructure/storage/parquet_frame_store.py`, `config/{sources,embodiments,qc}.yaml`

## Context

Sources A–C each arrive as one artifact with one set of fields: every episode of `pusht` has
exactly the signals every other episode of `pusht` has. EPIC-KITCHENS-100 does not. It is three
independent publications, by different teams, on different servers, covering different subsets of
the same 700 videos:

| Layer       | Where                                     | Coverage                                                                 |
| ----------- | ----------------------------------------- | ------------------------------------------------------------------------ |
| annotations | `epic-kitchens-100-annotations` (GitHub)  | all videos                                                               |
| camera_pose | EPIC-Fields COLMAP reconstructions        | published as a multi-GB archive; the public example URI serves one video |
| IMU         | GoPro metadata on the Bristol data server | EPIC-100 extension videos only                                           |

Measured by HTTP probe (`curl -r 0-100`, a 206 means present):

- `P01_01-gyro.csv` → **404**. EPIC-55 era video; no GoPro metadata exists.
- `P01_103-gyro/accl.csv` → **206**. `P28_101-gyro/accl.csv` → **206**.
- `P28_101.json` is the published EPIC-Fields example; `P01_01.json` / `P01_103.json` are not.

So a run over three videos produces three _different shapes of episode_ under one `source_id`.
This is the case the unified schema was designed for, and the one that would have been invisible
if D had been treated as "a fourth table".

## Decision

### 1. A missing layer is a capability fact, never an ingestion failure

Each enabled layer is probed **once, during `fetch`**, and the measured result is written into
`ref.json` and the `.staged.json` marker. `normalize` reads that record and never re-probes.

Two consequences that matter more than the code:

- **Resume is deterministic and offline.** A crash between fetch and normalize cannot change what
  the episode is, because availability was already decided and persisted.
- **A layer published later looks like a content change, not like nondeterminism.** Re-fetching
  after EPIC-Fields covers a new video yields a different `content_hash`, which is exactly the
  signal the staleness machinery already knows how to act on.

A probe that 404s degrades that layer's capabilities and nothing else:

| Video     | pose | IMU | `has_state` | `has_camera_pose` | `has_imu` |
| --------- | ---- | --- | ----------- | ----------------- | --------- |
| `P01_01`  | no   | no  | false       | false             | false     |
| `P01_103` | no   | yes | false       | false             | true      |
| `P28_101` | yes  | yes | true        | true              | true      |

Without the pose layer, `state_spec.level = "absent"` — not an empty column, and above all not a
zero-filled one. Invariant 3 then forces `has_state = false`, so the two statements cannot drift.

The absence is also written down positively: `provenance.transforms` gains a
`{"kind": "layer_unavailable", "layers": [...]}` entry. A capability that is `false` because we
looked and it was not there is a different fact from one that is `false` because nobody looked.

### 2. Episodes are listed round-robin across videos

`list_episodes` interleaves the videos rather than draining one before starting the next. With
video-major ordering, `--max-episodes 20` would return twenty episodes of one video and the
heterogeneity above — the entire point of this source — would never appear in a truncated run.

### 3. The IMU is a stream, not more columns

The IMU samples at ~5.05 ms on `P01_103` and ~5.13 ms on `P01_101` against a 50 fps video. Poses
are on the frame clock; the IMU is not. Merging them into one table would require resampling one
of them, i.e. fabricating samples, which design §8.5 forbids outright.

So the domain gains `stream_specs` on `EpisodeMeta` and `streams` on `CanonicalEpisode`
(invariant 17: a stream spec must declare `clock = own_timeline`), and the store writes
`streams/<stream_id>.parquet` beside `frames.parquet`, with its own `t` measured from the same
origin as the frame table. `content_hash` folds the streams in only when there are any, so every
hash computed for sources A–C before M4 is unchanged byte for byte.

**No nominal IMU rate is declared anywhere.** ADR 004 measured a fixed 5.128205 ms step on
`P01_101` and it was tempting to write `imu_hz: 195` in config; `P01_103` samples at 5.0505 ms.
The stream therefore carries the timestamps upstream shipped and nothing derives a rate from a
constant. The `imu_hz` key has been removed from `config/sources.yaml`.

Gyro and accelerometer arrive as two files with no upstream promise that they share a timeline.
They are joined only if their `Milliseconds` arrays are **identical**; otherwise the adapter
raises. A silent off-by-one here would rotate every acceleration onto the wrong instant, and that
is not a bug any downstream test would catch.

> **Superseded by ADR 012.** The first unlimited run proved the two files genuinely disagree —
> 793 of `P28_101`'s 141,924 samples, by up to 15 ms — so neither branch of that rule was right.
> They are now **two streams**, `gyro` and `accel`, each on its own clock. The paragraph above is
> kept as written because the reasoning it gives for refusing to guess is exactly what forced the
> correction.

### 4. The camera pose is `estimated`, and that changes what QC is allowed to conclude

EPIC-Fields poses are a monocular COLMAP reconstruction. Three things follow, and all three are
recorded rather than assumed:

- **No unit, `metric_convertible: false`.** A monocular reconstruction is scale-free. Writing
  `m` would be inventing a unit — the precise failure design §8.5 names.
- **Unregistered frames are NaN**, and the parquet writer stores NaN as a genuine **NULL**
  (`pa.array(values, mask=isnan)`). A zero pose is a _place_ — the world origin, unrotated — and
  is indistinguishable from a real measurement to every consumer downstream.
- **`origin: estimated`** on every pose channel, and `provenance.signal_origin["state"] =
estimated`. The engine's `downgrade_basis` then turns a `FAIL` from any rule that reads _only_
  non-`measured` channels into a `REVIEW`, and appends the basis to the reason (invariant 13).
  A jump in a reconstructed pose is a reconstruction failure; failing the episode over it uses
  model error to discredit data that may be perfectly good.

The downgrade lives in `evaluate_rule`, not in the rules, so no rule can bypass it.

### 5. M4 ships two QC rules, and one of them cannot fail

- `ACTION_RANGE` (FAIL) — the frame-level action rule whose entire role for D is to resolve to
  `SKIPPED(reason=action_level_is_episode_label)`, a conclusion the report counts separately from
  "this source has no action".
- `POSE_COVERAGE` (REVIEW) — coverage plus the longest continuous hole **in seconds**, because
  20% missing spread evenly is a usable trajectory and 20% missing in one run is a discontinuity.
  It reports REVIEW by construction, for the reason above.

Consequently **no rule shipped in M4 can produce a FAIL over an estimated channel**, so the
invariant-13 downgrade is proven by unit test against a stub FAIL-severity rule rather than by a
production rule. The pose-jerk rule that will exercise it end to end belongs to M5, which owns the
data-driven thresholds it needs. Recorded here so the gap is visible rather than implied.

### 6. Pixels are declared and deliberately not fetched

The RGB release is ~700 GB and nothing in this pipeline reads it. The head camera is declared as
a `CameraSpec` with `encoding = absent` and `is_present = false`, and `has_rgb = has_video =
false`. Every pixel-reading rule is then honestly `SKIPPED` instead of failing on a file we chose
not to download, and the embodiment's topology stays legible.

### 7. Nobody adjudicated success, and the licence travels with the data

`termination_source = annotator`, `end_reason = annotation_bound`, `is_truncated = false` (the
segment ends where the annotator said the action ended — nothing was lost), `success = None` with
`success_adjudicator = none`. Invariant 14 already forbids `success = false` without an
adjudicator; D is the source that makes the distinction real.

`license: cc-by-nc-4.0` is on the source row and is emitted on every export line. A
non-commercial term that lives only in a config file is an unwritten assumption of whoever trains
on the manifest.

## Fixture

`tests/fixtures/epic_kitchens_mini/` (~230 KB) is a verbatim slice of all three layers with the
availability matrix above baked in as real filesystem structure, so the integration suite runs
offline and layer absence is a real 404-equivalent rather than a stub.

Two facts corrected while building it, both by measurement:

- **`P01_101` is not in the train split.** The earlier video selection listed it; `EPIC_100_train.csv`
  contains `P01_102`…`P01_109` but no `P01_101`. Replaced by `P01_103`.
- **`P28_101_43` is a real partial reconstruction** — 32 of its 48 frames registered, 66.7%
  coverage. It is in the fixture deliberately: it is the only honest evidence that unregistered
  frames stay NaN and that `POSE_COVERAGE` can reach REVIEW on real data. Its sibling
  `P28_101_0` is fully registered and PASSes, which is exit criterion 1 — two episodes, one
  source, one rule, different conclusions — met without constructing anything.

## Alternatives rejected

| Alternative                                    | Why not                                                                                     |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Fail the episode when a layer 404s             | Turns a publication schedule into a data-quality verdict; two thirds of D would be unusable |
| Declare capabilities per _source_ in config    | They differ per _episode_; a config-level answer would be wrong for most episodes           |
| Resample the IMU onto the frame clock          | Fabricates samples, and destroys the one source that proves multi-clock storage is needed   |
| Zero-fill unregistered poses                   | A zero pose is a place. Indistinguishable from a measurement downstream                     |
| Give the pose channels metres                  | Monocular reconstruction is scale-free; the unit would be invented                          |
| Let `POSE_COVERAGE` FAIL below some coverage   | The hole is a property of the reconstruction, not of the data (invariant 13)                |
| Re-probe layer availability during `normalize` | Makes a resumed run depend on the network and on when it resumed                            |
