"""M0 spike: probe sources A (`lerobot/pusht`) and B (`lerobot/aloha_sim_insertion_human`).

Throwaway code. Answers two questions the design flags as unverified:

1. What does `meta/info.json` actually declare (fps, features, dtypes, shapes, names)?
2. Does LeRobot's export preserve `terminated` vs `truncated`, or only `next.done`?

Usage:
    uv run --group spike python spikes/probe_lerobot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download

DATA_DIR = Path(__file__).parent / "_data" / "lerobot"

REPOS = ["lerobot/pusht", "lerobot/aloha_sim_insertion_human"]

# The question from design Appendix A, A.5 / B: which boundary columns survived the export.
BOUNDARY_COLUMNS = [
    "next.done",
    "next.success",
    "next.reward",
    "terminated",
    "truncated",
    "done",
    "success",
    "reward",
]


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def list_files(repo_id: str) -> list[str]:
    return HfApi().list_repo_files(repo_id, repo_type="dataset")


def fetch(repo_id: str, filename: str) -> Path:
    return Path(
        hf_hub_download(
            repo_id,
            filename,
            repo_type="dataset",
            local_dir=DATA_DIR / repo_id.replace("/", "__"),
        )
    )


def print_info_json(info: dict[str, Any]) -> None:
    for key in ("codebase_version", "robot_type", "fps", "total_episodes", "total_frames",
                "total_tasks", "total_videos", "chunks_size", "data_path", "video_path"):
        if key in info:
            print(f"  {key:20s} = {info[key]!r}")
    print("  features:")
    for name, spec in info.get("features", {}).items():
        dtype = spec.get("dtype")
        shape = spec.get("shape")
        names = spec.get("names")
        print(f"    {name:32s} dtype={dtype!s:10s} shape={shape!s:12s} names={names!r}")


def probe(repo_id: str) -> dict[str, Any]:
    rule(f"{repo_id}")

    files = list_files(repo_id)
    print(f"  repo has {len(files)} files; meta/*:")
    for f in sorted(f for f in files if f.startswith("meta/")):
        print(f"    {f}")

    info = json.loads(fetch(repo_id, "meta/info.json").read_text())
    print("\n-- meta/info.json --")
    print_info_json(info)

    for meta_file in ("meta/episodes/chunk-000/file-000.parquet", "meta/tasks.parquet"):
        if meta_file in files:
            meta_table = pq.read_table(fetch(repo_id, meta_file))
            print(f"\n-- {meta_file}: rows={meta_table.num_rows} --")
            print(f"  columns: {meta_table.schema.names}")
            for row in meta_table.slice(0, 3).to_pylist():
                trimmed = {k: v for k, v in row.items() if not k.startswith("stats/")}
                print(f"    {json.dumps(trimmed, default=str)[:400]}")

    video_files = sorted(f for f in files if f.endswith(".mp4"))
    print(f"\n-- video files: {len(video_files)} --")
    for f in video_files[:4]:
        print(f"    {f}")

    data_files = sorted(f for f in files if f.startswith("data/") and f.endswith(".parquet"))
    if not data_files:
        raise SystemExit(f"FAIL: no data parquet found in {repo_id}")
    first = data_files[0]
    print(f"\n-- first data file: {first}  (of {len(data_files)} parquet files) --")

    table = pq.read_table(fetch(repo_id, first))
    print(f"  rows={table.num_rows}  columns={table.num_columns}")
    print("  schema:")
    for field in table.schema:
        print(f"    {field.name:32s} {field.type}")

    print("\n  first 5 rows:")
    head = table.slice(0, 5).to_pylist()
    for row in head:
        print(f"    {json.dumps(row, default=str)}")

    columns = set(table.schema.names)
    present = [c for c in BOUNDARY_COLUMNS if c in columns]
    print("\n-- boundary / termination columns present --")
    print(f"    {present}")
    verdict = (
        "terminated/truncated PRESERVED"
        if {"terminated", "truncated"} <= columns
        else "terminated vs truncated LOST upstream (only merged done/success survives)"
    )
    print(f"    verdict: {verdict}")

    return {
        "repo_id": repo_id,
        "fps": info.get("fps"),
        "robot_type": info.get("robot_type"),
        "total_episodes": info.get("total_episodes"),
        "total_frames": info.get("total_frames"),
        "boundary_columns": present,
        "terminated_truncated_preserved": {"terminated", "truncated"} <= columns,
    }


def main() -> int:
    summaries = [probe(repo_id) for repo_id in REPOS]
    rule("SUMMARY")
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
