"""Throwaway probe 2: the questions probe 1 raised.

- pusht's `next.done` fires once mid-episode on all 80 episodes — where exactly?
- what does a within-episode jump look like relative to a ROBUST statistic (median |da|)
  rather than the episode's own p99.9, which is essentially its max?
- what is the largest span of any physical channel, per episode, per unit?
- how many C episodes have a gripper command that never changes?
- EPIC segment durations, video bounds and neighbour overlaps.
"""

from __future__ import annotations

import csv
import glob
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


def episodes(source: str):
    for path in sorted(glob.glob(f"store/normalized/{source}/*/episode.json")):
        meta = json.loads(Path(path).read_text())
        table = pq.read_table(path.replace("episode.json", "frames.parquet"))
        yield meta, table


def q(values, name: str) -> str:
    a = np.asarray(values, dtype=np.float64)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return f"{name}: no finite values"
    return (
        f"{name}: n={a.size} min={a.min():.6g} p1={np.percentile(a, 1):.6g} "
        f"p50={np.percentile(a, 50):.6g} p99={np.percentile(a, 99):.6g} max={a.max():.6g}"
    )


def unit_of(meta, column: str) -> str | None:
    signal, _, channel = column.partition(".")
    spec = meta["action_spec"] if signal == "action" else meta["state_spec"]
    for entry in spec["channels"]:
        if entry["name"] == channel and entry["is_physical"]:
            return entry.get("unit") or "none"
    return None


def main() -> None:
    print("### pusht next.done positions")
    positions = defaultdict(int)
    for _meta, table in episodes("pusht"):
        done = np.asarray(table.column("raw.next.done").to_numpy(zero_copy_only=False)) != 0
        idx = np.flatnonzero(done)
        n = done.size
        positions[tuple((n - 1 - i) for i in idx)] += 1
    print("  offsets from the END of the episode -> count:", dict(positions))

    print("\n### robust jump ratios and per-unit spans")
    for source in ("pusht", "aloha_sim_insertion", "berkeley_ur5", "epic100"):
        ratio_med: list[float] = []
        ratio_p99: list[float] = []
        span_by_unit: dict[str, list[float]] = defaultdict(list)
        n_const_gripper = 0
        n_gripper = 0
        for meta, table in episodes(source):
            best_med, best_p99 = 0.0, 0.0
            per_unit: dict[str, float] = defaultdict(float)
            for name in table.column_names:
                if not name.startswith(("action.", "state.")):
                    continue
                unit = unit_of(meta, name)
                if unit is None:
                    continue  # non-physical: excluded exactly as invariant 6 requires
                values = np.asarray(table.column(name).to_numpy(zero_copy_only=False), dtype=float)
                finite = values[np.isfinite(values)]
                if finite.size < 3:
                    continue
                per_unit[unit] = max(per_unit[unit], float(finite.max() - finite.min()))
                d = np.abs(np.diff(finite))
                med = float(np.median(d))
                p99 = float(np.percentile(d, 99))
                if med > 0:
                    best_med = max(best_med, float(d.max() / med))
                if p99 > 0:
                    best_p99 = max(best_p99, float(d.max() / p99))
                if "gripper" in name and name.startswith("action."):
                    n_gripper += 1
                    if np.unique(finite).size == 1:
                        n_const_gripper += 1
            if best_med:
                ratio_med.append(best_med)
            if best_p99:
                ratio_p99.append(best_p99)
            for unit, value in per_unit.items():
                span_by_unit[unit].append(value)
        print(f"-- {source}")
        print("   " + q(ratio_med, "max|da| / median|da| (worst channel per episode)"))
        print("   " + q(ratio_p99, "max|da| / p99|da| (worst channel per episode)"))
        for unit, values in sorted(span_by_unit.items()):
            print("   " + q(values, f"largest span among channels in {unit}"))
        if n_gripper:
            print(f"   gripper channels: {n_gripper}, constant throughout: {n_const_gripper}")

    print("\n### EPIC segments")
    root = Path("store/cache/epic100/master")
    rows = list(csv.DictReader((root / "EPIC_100_train.csv").read_text().splitlines()))
    info = {
        r["video_id"]: r
        for r in csv.DictReader((root / "EPIC_100_video_info.csv").read_text().splitlines())
    }

    def secs(text: str) -> float:
        h, m, s = text.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    by_video: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        by_video[row["video_id"]].append(
            (secs(row["start_timestamp"]), secs(row["stop_timestamp"]))
        )
    durations, overlaps, over_end = [], [], 0
    for video, segments in by_video.items():
        duration = float(info[video]["duration"]) if video in info else None
        segments.sort()
        for index, (start, stop) in enumerate(segments):
            durations.append(stop - start)
            if duration is not None and stop > duration:
                over_end += 1
            if index + 1 < len(segments):
                nxt = segments[index + 1]
                overlap = min(stop, nxt[1]) - max(start, nxt[0])
                if overlap > 0:
                    overlaps.append(overlap / max(1e-9, min(stop - start, nxt[1] - nxt[0])))
    print("   " + q(durations, "segment duration (s), all 67k train segments"))
    print(f"   segments shorter than 0.4 s: {sum(1 for d in durations if d < 0.4)}")
    print(f"   segments ending past the video duration: {over_end}")
    print("   " + q(overlaps, "overlap fraction with the next segment, when > 0"))
    print(f"   overlapping pairs: {len(overlaps)}; over 50%: {sum(1 for o in overlaps if o > 0.5)}")


if __name__ == "__main__":
    main()
