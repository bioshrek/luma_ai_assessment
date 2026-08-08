"""Throwaway probe: measure the distributions M5's thresholds must be derived from.

Reads the already-normalized corpus under `store/normalized/` directly, so it needs neither
the network nor the catalog. Output is captured in `spikes/_out/probe_qc_thresholds.txt`.
"""

from __future__ import annotations

import glob
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SOURCES = ("pusht", "aloha_sim_insertion", "berkeley_ur5", "epic100")


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
        f"p50={np.percentile(a, 50):.6g} p99={np.percentile(a, 99):.6g} "
        f"p99.9={np.percentile(a, 99.9):.6g} max={a.max():.6g}"
    )


def main() -> None:
    for source in SOURCES:
        print(f"\n{'=' * 78}\n{source}\n{'=' * 78}")
        n_frames: list[int] = []
        termination: dict[str, int] = defaultdict(int)
        echo_max_abs: list[float] = []
        echo_bit_equal: list[float] = []
        echo_corr: list[float] = []
        jerk_ratio: dict[str, list[float]] = defaultdict(list)
        span: dict[str, list[float]] = defaultdict(list)
        travel: dict[str, list[float]] = defaultdict(list)
        gripper_unique: dict[str, list[int]] = defaultdict(list)
        dt_drift: list[float] = []
        gap_frac: list[float] = []

        for meta, table in episodes(source):
            n_frames.append(meta["n_frames"])
            names = table.column_names
            col = {name: np.asarray(table.column(name).to_numpy(zero_copy_only=False), dtype=float)
                   for name in names}

            # --- termination signal ------------------------------------------------------
            for flag in ("raw.next.done", "raw.is_terminal", "raw.is_last"):
                if flag in col:
                    values = col[flag] != 0
                    last = bool(values[-1])
                    mid = int(np.count_nonzero(values[:-1]))
                    termination[f"{flag}: last={last} n_mid={mid}"] += 1

            # --- action channel statistics ----------------------------------------------
            action = [n for n in names if n.startswith("action.")]
            state = [n for n in names if n.startswith("state.")]
            for name in action + state:
                values = col[name]
                finite = values[np.isfinite(values)]
                if finite.size < 2:
                    continue
                span[name].append(float(finite.max() - finite.min()))
                d = np.abs(np.diff(values[np.isfinite(values)]))
                travel[name].append(float(d.sum()))
                if d.size > 4 and d.max() > 0:
                    p999 = float(np.percentile(d, 99.9))
                    if p999 > 0:
                        jerk_ratio[name].append(float(d.max() / p999))

            # --- state/action echo -------------------------------------------------------
            common = [n[len("action."):] for n in action if f"state.{n[len('action.'):]}" in names]
            if common:
                a = np.stack([col[f"action.{c}"] for c in common])
                s = np.stack([col[f"state.{c}"] for c in common])
                mask = np.isfinite(a) & np.isfinite(s)
                if mask.any():
                    diff = np.abs(a - s)
                    echo_max_abs.append(float(np.nanmax(diff[mask])))
                    echo_bit_equal.append(float(np.mean(diff[mask] == 0.0)))
                    if a[mask].std() > 0 and s[mask].std() > 0:
                        echo_corr.append(float(np.corrcoef(a[mask], s[mask])[0, 1]))

            # --- gripper -----------------------------------------------------------------
            for name in action:
                if "gripper" in name:
                    gripper_unique[name].append(int(np.unique(col[name]).size))

            # --- clock -------------------------------------------------------------------
            t = col["t"]
            if t.size > 2:
                dt = np.diff(t)
                median = float(np.median(dt))
                nominal = 1.0 / float(meta["fps_nominal"])
                dt_drift.append(abs(median - nominal) / nominal)
                gap_frac.append(float(np.count_nonzero(dt > 3 * median)) / dt.size)

        print(q(n_frames, "n_frames"))
        print("  short episodes (<20 frames):", sum(1 for n in n_frames if n < 20))
        print("  termination patterns:", dict(termination))
        if echo_max_abs:
            print(q(echo_max_abs, "echo max|a-s|"))
            print(q(echo_bit_equal, "echo bit-equal fraction"))
            print(q(echo_corr, "echo corr"))
        for name, values in sorted(gripper_unique.items()):
            print(q(values, f"gripper unique values {name}"))
        print(q(dt_drift, "median_dt drift vs nominal"))
        print(q(gap_frac, "fraction of dt > 3x median"))
        for name in sorted(span):
            print("  " + q(span[name], f"span {name}"))
        for name in sorted(travel):
            print("  " + q(travel[name], f"travel {name}"))
        for name in sorted(jerk_ratio):
            print("  " + q(jerk_ratio[name], f"max|da| / p99.9|da| {name}"))


if __name__ == "__main__":
    main()
