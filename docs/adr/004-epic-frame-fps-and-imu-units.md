# ADR 004 — EPIC-KITCHENS: measured frame-extraction fps, and measured IMU units

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M0 (feasibility spike)
- **Affects:** design §1.1 points 3 and 6, §A.D, §2.4; `config/sources.yaml`

## Context

Two design-flagged unknowns for source D had to be closed **by measurement, not by reading
documentation** (design §1.1: "measure it, do not copy the doc"):

1. How do EPIC's `start_frame`/`stop_frame`, and EPIC-Fields' pose frame indices, map to a
   video's official fps?
2. Are the GoPro IMU units `rad/s` / `m/s²`, or `deg/s` / `g`?

All numbers below come from `spikes/probe_epic.py`, output in `spikes/_out/probe_epic.txt`.

## Finding 1 — there are five official fps values, not two

`EPIC_100_video_info.csv`, 700 videos:

| Official fps | Videos |
| ------------ | ------ |
| 59.940060    | 423    |
| 50.0         | 268    |
| 29.970030    | 4      |
| 47.952048    | 4      |
| 90.0         | 1      |

The design assumed "50 or 59.94". Any code that branches on two values is wrong.

## Finding 2 — the annotation frame indices are at the _extraction_ fps, which is not the official fps

Tested over **all 67,217 train segments**, accepting `floor(seconds × rate)` within ±1:

| Hypothesised rate               | Segments reproduced |
| ------------------------------- | ------------------- |
| each video's official fps       | 58.12%              |
| a flat 60 fps                   | 44.28%              |
| a flat 50 fps                   | 55.72%              |
| a flat 59.94 fps                | 2.42%               |
| **50 fps videos @50, rest @60** | **100.00%**         |

The split is exact: 37,455 segments are in 50 fps videos and 29,762 are not, and those two
counts are precisely the flat-50 and flat-60 hit sets. So EPIC extracted frames at 50 fps for
natively-50 fps videos and at a flat **60** fps for everything else — including the 59.94 fps
videos, where 60 ≠ the official rate.

Using the official fps, as the design's `derived_from_seconds@<fps>` implied, would misplace
**42%** of all segments.

## Finding 3 — EPIC-Fields pose indices _are_ at the official fps, and are 1-based with gaps

For `P28_101` (official 50.0 fps, 717.7 s ⇒ ~35,885 frames): 35,823 registered poses, max
index 35,889, ratio to expected frame count **1.0001**. Keys look like
`frame_0000000001.jpg` ⇒ **1-based at the official fps**.

Coverage is **99.83%**, and the gap between consecutive registered indices has median 1 but
**max 22** — COLMAP failed to register runs of frames.

Camera intrinsics are published for **456×256** frames (`OPENCV` model,
`fx=239.61, fy=243.08, cx=228.0, cy=128.0` + 4 distortion terms), _not_ for the 1920×1080
source video.

## Finding 4 — IMU units, measured

`P01_101`, 362,865 rows each, columns `Milliseconds` + 3 axes, sample step a constant
5.128205 ms (**195 Hz** nominal; ≈198 Hz averaged over the 1,829 s span):

| Signal | Measured magnitudes                  | Verdict          |
| ------ | ------------------------------------ | ---------------- |
| accl   | p50 = 2.91, mean \|a\| = **9.8998**  | **`m/s²`** (≈ g) |
| gyro   | p50 = 0.0825, p99 = 1.85, max = 4.74 | **`rad/s`**      |

The accelerometer is decisive: a mostly-stationary head-mounted camera must read ≈9.81, and it
reads 9.8998 — so the unit is `m/s²`, not `g`. The gyro peaks at 4.74; `deg/s` for real head
motion would reach the hundreds, so it is `rad/s`.

Both match the documentation — but they are now _ours_, reproducible from
`spikes/probe_epic.py`, which is the point.

`P01_01` returns **404** for both IMU files: `has_imu=False`. Availability is per video.

## Decision

1. `frame_index_source` records the **extraction** fps, not the official fps:
   `"derived_from_seconds@50"` when the video's official fps is 50.0, else
   `"derived_from_seconds@60"`. Encoded in `config/sources.yaml` under
   `epic100.frame_extraction_fps`, and asserted by the adapter against
   `EPIC_100_video_info.csv` — never hardcoded per video.
2. **Seconds stay authoritative.** `start_timestamp`/`stop_timestamp` define the boundary;
   frame indices are a derived convenience. This ADR strengthens that rule rather than
   weakening it: the frame indices turned out to be a per-corpus artefact, and only seconds
   are portable across the official video, the 30 fps mirror, and the pose JSON.
3. IMU channels carry `unit: "rad/s"` (gyro) and `unit: "m/s^2"` (accel), `is_physical=true`,
   `is_delta=false`, `frame: "sensor"`, `origin: "measured"`, on the IMU clock at ~195 Hz —
   a **third** clock alongside video frames and annotation seconds.
4. `CameraSpec` for the pose layer records `intrinsics_resolution: [456, 256]` explicitly.
   Intrinsics without their resolution are unusable; this is the "never guess" rule applied
   to a calibration.
5. Unregistered pose frames are **NULL** in `frames.parquet`, never zero-filled, and the
   99.83% figure becomes a per-episode `pose_coverage` QC metric.
6. `Capabilities.has_imu` is resolved **per video** by probing for the file; a 404 is a fact
   to record, not an error to retry.

## Consequences

- Design §1.1 point 3 (IMU units) and point 6 (fps) are updated with the measured values and
  cite this ADR.
- Design §A.D gains the extraction-fps rule and the 456×256 intrinsics note.
- The three-clock situation (IMU 195 Hz / video 50–60 fps / annotation seconds) is confirmed
  real, which is the justification for `SignalSpec.rate_hz` being per-signal rather than
  per-episode.
- M4 must acquire the EPIC-Fields JSONs from the 7.5 GB bulk tarball; there is no per-video
  HTTP endpoint. The path is registered in the gitignored `config/sources.local.yaml`.
