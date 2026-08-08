# ADR 002 — LeRobot is v3.0, and `terminated` vs `truncated` is lost upstream

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M0 (feasibility spike)
- **Affects:** design §A.A, §A.B, §11; `infrastructure/sources/lerobot` (M1/M3)

## Context

Design Appendix A sketches the LeRobot on-disk layout from the v2 era and flags one question
as unverified: "LeRobot exported only `next.done`, so `terminated` (goal achieved) and
`truncated` (step limit) may already have been merged upstream — the M0 spike must confirm
this first."

Both `lerobot/pusht` and `lerobot/aloha_sim_insertion_human` were probed directly
(`spikes/probe_lerobot.py`, output in `spikes/_out/probe_lerobot.txt`).

## Finding 1 — the layout is `codebase_version: v3.0`, not what Appendix A describes

| Appendix A assumed                      | Actually observed                                                               |
| --------------------------------------- | ------------------------------------------------------------------------------- |
| `data/chunk-000/episode_000000.parquet` | `data/chunk-000/file-000.parquet` holding **many episodes** (all 206 for pusht) |
| one mp4 per episode                     | **one mp4 per camera for the whole dataset**, episodes addressed by time range  |
| `meta/episodes*` as jsonl               | `meta/episodes/chunk-000/file-000.parquet`                                      |
| `meta/tasks.*` as jsonl                 | `meta/tasks.parquet`                                                            |
| `features[*].names` is a flat list      | a **dict**: `{"motors": ["motor_0", "motor_1"]}`                                |
| pusht `robot_type: "pusht"`             | pusht `robot_type: "unknown"`                                                   |

Episode boundaries live in `meta/episodes`, as `dataset_from_index` / `dataset_to_index`
(row range into the shared data parquet) plus `videos/<key>/from_timestamp` /
`to_timestamp` (time range into the shared mp4), with `length` and `tasks` per episode.

Also observed: pusht's `observation.state` is `fixed_size_list<float>[2]` while aloha's is a
plain `list<float>` — the adapter must accept both.

## Finding 2 — `terminated` / `truncated` do not survive the export

Columns actually present:

| Dataset | Boundary columns                           |
| ------- | ------------------------------------------ |
| pusht   | `next.done`, `next.success`, `next.reward` |
| aloha   | `next.done` **only**                       |

Neither `terminated` nor `truncated` exists in either dataset. The question is answered:
**the distinction is destroyed upstream and cannot be recovered from the data.**

For aloha it is worse: there is no success and no reward column at all, and every one of the
50 episodes is _exactly_ 500 frames (10.0 s @ 50 Hz). Uniform length is itself the evidence —
these episodes end on a fixed step limit, i.e. all are `truncated`, none `terminated`.

## Decision

1. **Read the layout from `meta/info.json` + `meta/episodes`; never hardcode a path pattern.**
   `data_path` and `video_path` are format strings published by the dataset itself.
2. **Do not invent `terminated`/`truncated`.** `EpisodeBoundary.is_truncated` is set only
   where evidence exists:
   - pusht: `termination_source = "env_rule"`; `success = next.success` on the final frame;
     `is_truncated = None` (unknown — `next.done` merges both cases).
   - aloha: `termination_source = "operator"`; `success = None`;
     `is_truncated = None`, with the fixed-500-length observation recorded in `raw_extra`
     rather than promoted to a claim.
     A guessed `is_truncated=False` would be exactly the "record information that does not
     exist as if it does" failure the design forbids.
3. **Record the loss as a known limitation** (design §11), because it changes what downstream
   offline-RL consumers may assume about $V(s_T)$.
4. Per-episode video is a **time range into a shared mp4**, so `--with-video` work in later
   milestones must seek by timestamp, not open a per-episode file.

## Consequences

- The M1 adapter needs a `meta/episodes` reader on day one; per-episode slicing is by
  `dataset_from_index`/`dataset_to_index`, not by file.
- `content_hash` for A/B is computed over the normalized episode's canonical bytes, not over
  the parquet file — one file covers many episodes, so file-level hashing would mark every
  episode stale whenever any episode changed.
- Source C remains the only source that can exercise `is_truncated=True` (via
  `is_last & ~is_terminal`), which raises its value in the corpus.
- Appendix A of the design is corrected to describe v3.0.
