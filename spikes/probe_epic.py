"""M0 spike: probe source D (EPIC-KITCHENS-100), by layer.

Throwaway code. Answers the questions the design flags as unverified:

1. What are the *official* fps values per video (50 vs 59.94)?
2. How do EPIC-Fields pose frame indices map to those fps? (iron rule: seconds are
   authoritative, frame indices are derived)
3. **What unit is the IMU in — rad/s or deg/s?** Measured, never copied from documentation.
4. Are the three layers (annotations / camera_pose / imu) independently available? A video
   without IMU must degrade that layer only.

Usage:
    uv run --group spike python spikes/probe_epic.py
"""

from __future__ import annotations

import io
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA_DIR = Path(__file__).parent / "_data" / "epic"

ANNOTATIONS_BASE = (
    "https://raw.githubusercontent.com/epic-kitchens/epic-kitchens-100-annotations/master"
)
# EPIC-Fields is distributed as a single 7.5 GB tarball; the repo ships one real per-video
# JSON, which is enough to characterise the format for the spike.
EPIC_FIELDS_EXAMPLE = (
    "https://raw.githubusercontent.com/epic-kitchens/epic-fields-code/main/example_data/P28_101.json"
)
# GoPro metadata, EPIC-100 extension videos only (3-digit video number).
EPIC_100_BASE = "https://data.bris.ac.uk/datasets/2g1n6qdydwa9u22shpxqzp0t8m"

POSE_VIDEO = "P28_101"
IMU_VIDEO_WITH = "P01_101"  # extension video: expected to have GoPro metadata
IMU_VIDEO_WITHOUT = "P01_01"  # EPIC-55 era video: expected to have none


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def cached_get(url: str, dest: Path) -> bytes | None:
    """Download once into `spikes/_data`. Returns None on 404 — a missing layer is a fact."""
    if dest.exists():
        return dest.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=600)
    if response.status_code == 404:
        print(f"  404 (layer absent): {url}")
        return None
    response.raise_for_status()
    dest.write_bytes(response.content)
    print(f"  {len(response.content) / 1e6:.2f} MB <- {url}")
    return response.content


def probe_annotations() -> pd.DataFrame:
    rule("layer: annotations (epic-kitchens-100-annotations, CC BY-NC 4.0)")

    video_info_bytes = cached_get(
        f"{ANNOTATIONS_BASE}/EPIC_100_video_info.csv", DATA_DIR / "EPIC_100_video_info.csv"
    )
    assert video_info_bytes is not None, "EPIC_100_video_info.csv is required"
    video_info = pd.read_csv(io.BytesIO(video_info_bytes))
    print(f"\n  EPIC_100_video_info.csv: {len(video_info)} videos, columns={list(video_info)}")
    print(f"  distinct official fps values: {sorted(video_info['fps'].unique())}")
    print(f"  fps histogram:\n{video_info['fps'].value_counts().to_string()}")
    print(f"\n  sample rows:\n{video_info.head(3).to_string(index=False)}")

    train_bytes = cached_get(
        f"{ANNOTATIONS_BASE}/EPIC_100_train.csv", DATA_DIR / "EPIC_100_train.csv"
    )
    assert train_bytes is not None, "EPIC_100_train.csv is required"
    train = pd.read_csv(io.BytesIO(train_bytes))
    print(f"\n  EPIC_100_train.csv: {len(train)} action segments, columns={list(train)}")
    segment = train.iloc[0]
    print(f"\n  first segment:\n{segment.to_string()}")

    fps_row = video_info.loc[video_info["video_id"] == segment["video_id"]].iloc[0]
    official_fps = float(fps_row["fps"])
    start_s = seconds(segment["start_timestamp"])
    stop_s = seconds(segment["stop_timestamp"])
    print("\n  -- seconds are authoritative, frame indices are derived --")
    print(f"    official fps for {segment['video_id']} = {official_fps}")
    print(f"    start={start_s:.3f}s stop={stop_s:.3f}s duration={stop_s - start_s:.3f}s")
    print(f"    derived_from_seconds@{official_fps}: "
          f"frames {round(start_s * official_fps)}..{round(stop_s * official_fps)}")
    print(f"    csv's own start_frame/stop_frame: "
          f"{segment['start_frame']}..{segment['stop_frame']}")
    print(f"    same segment at the local 30 fps mirror: "
          f"frames {round(start_s * 30)}..{round(stop_s * 30)}")

    measure_frame_index_convention(train, video_info)
    return video_info


def measure_frame_index_convention(train: pd.DataFrame, video_info: pd.DataFrame) -> None:
    """Which fps reproduces the CSV's own start_frame/stop_frame? Measure, do not assume."""
    fps_by_video = dict(zip(video_info["video_id"], video_info["fps"], strict=True))
    start_s = train["start_timestamp"].map(seconds).to_numpy()
    stop_s = train["stop_timestamp"].map(seconds).to_numpy()
    official = train["video_id"].map(fps_by_video).to_numpy(dtype=float)
    start_frame = train["start_frame"].to_numpy()
    stop_frame = train["stop_frame"].to_numpy()

    print("\n  -- which fps reproduces the CSV's own frame indices? (all 67k segments) --")
    # Hypothesis from the split below: EPIC extracted frames at 50 fps for the 50 fps videos
    # and at a flat 60 fps for everything else — NOT at each video's official fps.
    extraction = np.where(np.isclose(official, 50.0), 50.0, 60.0)
    candidates = (
        ("official per-video fps", official),
        ("a flat 60 fps", 60.0),
        ("a flat 50 fps", 50.0),
        ("a flat 59.94 fps", 59.94005994005994),
        ("50 fps videos @50, rest @60", extraction),
    )
    for label, rate in candidates:
        hits = np.isclose(np.floor(start_s * rate), start_frame, atol=1) & np.isclose(
            np.floor(stop_s * rate), stop_frame, atol=1
        )
        print(f"    {label:28s}: {100 * hits.mean():6.2f}% of segments reproduced")

    subset = np.isclose(official, 50.0)
    print(f"    ({int(subset.sum())} segments are in 50 fps videos, {int((~subset).sum())} are not "
          f"— a single flat rate cannot fit both)")
    print("    => frame_index_source must carry the EXTRACTION fps, which is not the "
          "video's official fps.")


def seconds(timestamp: str) -> float:
    hours, minutes, secs = timestamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


def probe_camera_pose(video_info: pd.DataFrame) -> None:
    rule("layer: camera_pose (EPIC-Fields, COLMAP world-to-camera)")

    raw = cached_get(EPIC_FIELDS_EXAMPLE, DATA_DIR / f"{POSE_VIDEO}.json")
    if raw is None:
        print("  SKIP: EPIC-Fields example JSON unreachable.")
        return
    data = json.loads(raw)
    print(f"  top-level keys: {sorted(data.keys())}")
    print(f"  camera: {json.dumps(data['camera'])[:300]}")
    print(f"  points: {len(data.get('points', []))}")

    images = data["images"]
    frame_names = sorted(images.keys())
    indices = np.array([int(name.split("_")[-1].split(".")[0]) for name in frame_names])
    first_key = frame_names[0]
    print(f"\n  registered frames: {len(images)}")
    print(f"  frame key format: {first_key!r}")
    print(f"  pose vector (qw qx qy qz tx ty tz): {images[first_key]}")
    print(f"  frame index range: {indices.min()}..{indices.max()}")

    row = video_info.loc[video_info["video_id"] == POSE_VIDEO]
    if row.empty:
        print(f"  {POSE_VIDEO} not in EPIC_100_video_info.csv")
        return
    fps = float(row.iloc[0]["fps"])
    duration = float(row.iloc[0]["duration"])
    total_frames = math.floor(duration * fps)
    print("\n  -- mapping pose frame indices to official fps --")
    print(f"    official fps={fps}  duration={duration}s  => total frames ~= {total_frames}")
    print(f"    max pose frame index = {indices.max()}")
    print(f"    ratio max_index/total_frames = {indices.max() / total_frames:.4f}")
    print(f"    coverage = {100 * len(images) / total_frames:.2f}% of official frames")
    gaps = np.diff(indices)
    print(f"    index step: min={gaps.min()} median={int(np.median(gaps))} max={gaps.max()}")
    print("    => unregistered frames exist; they must be NULL in parquet, never zero-filled.")


def probe_imu() -> None:
    rule("layer: imu (GoPro metadata, EPIC-100 extension videos only)")

    for video_id in (IMU_VIDEO_WITH, IMU_VIDEO_WITHOUT):
        participant = video_id.split("_")[0]
        print(f"\n  -- {video_id} --")
        frames: dict[str, pd.DataFrame] = {}
        for kind in ("gyro", "accl"):
            url = f"{EPIC_100_BASE}/{participant}/meta_data/{video_id}-{kind}.csv"
            payload = cached_get(url, DATA_DIR / f"{video_id}-{kind}.csv")
            if payload is None:
                continue
            frames[kind] = pd.read_csv(io.BytesIO(payload))
        if not frames:
            print(f"    capabilities: has_imu=False  (layer absent for {video_id})")
            continue
        print(f"    capabilities: has_imu=True  layers present: {sorted(frames)}")
        for kind, frame in frames.items():
            report_imu(kind, frame)


def report_imu(kind: str, frame: pd.DataFrame) -> None:
    print(f"\n    {kind}: rows={len(frame)} columns={list(frame)}")
    print(f"{frame.head(3).to_string(index=False)}")

    time_col = next(
        (c for c in frame.columns if any(k in c.lower() for k in ("time", "milli", "sec"))), None
    )
    if time_col is not None:
        times = frame[time_col].to_numpy(dtype=float)
        span = times[-1] - times[0]
        unit = "ms" if "milli" in time_col.lower() else "s"
        span_s = span / 1000.0 if unit == "ms" else span
        print(f"      time column {time_col!r} spans {span:.1f} {unit} "
              f"=> sample rate ~= {len(frame) / span_s:.1f} Hz")

    numeric = frame.select_dtypes("number").drop(columns=[time_col], errors="ignore")
    print(f"      signal columns: {list(numeric)}")
    values = numeric.to_numpy(dtype=float)
    magnitude = np.abs(values)
    p50, p99, vmax = np.percentile(magnitude, 50), np.percentile(magnitude, 99), magnitude.max()
    print(f"      |value|: p50={p50:.4f} p99={p99:.4f} max={vmax:.4f}")

    if kind == "gyro":
        verdict = "rad/s" if vmax < 40 else "deg/s"
        print(f"      MEASURED UNIT VERDICT: {verdict}  "
              f"(hand motion peaks are a few rad/s; deg/s would reach hundreds)")
    else:
        gravity = np.linalg.norm(values, axis=1).mean()
        verdict = "m/s^2" if 7.0 < gravity < 12.0 else f"NOT m/s^2 (mean |a|={gravity:.3f})"
        print(f"      mean |accel| = {gravity:.4f}  MEASURED UNIT VERDICT: {verdict}  "
              f"(a mostly-stationary head-mounted camera should read ~9.81)")


def main() -> int:
    video_info = probe_annotations()
    probe_camera_pose(video_info)
    probe_imu()
    return 0


if __name__ == "__main__":
    sys.exit(main())
