# ADR 006 — M1 catalog schema and normalized-store layout, as built

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M1 (walking skeleton)
- **Affects:** design §4 (catalog schema), §3 (QC roll-up), §5 (artifact layout)

## Context

Design §4 sketches the `episodes` table. Implementing M1 against real pusht bytes turned up
columns the sketch omits, one layout choice the sketch would have made illegal on a filesystem,
and one roll-up rule the design left implicit. Per the repo's prime rule, the drift is resolved
here rather than silently.

## Decision 1 — columns the sketch omitted, and why each is not optional

| Column                                     | Why it exists                                                                                                                                                                                             |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `provenance_json`                          | `Provenance` is part of `EpisodeMeta` and is what QC gating reads. Without it the catalog cannot answer "why was `TS_MONOTONIC` skipped" without reopening the parquet sidecar.                            |
| `raw_columns_json`                          | Invariant 12 requires every `raw.*` column to be registered. The registry must round-trip, or a reloaded `CanonicalEpisode` fails its own invariant.                                                        |
| `stats_json`                               | Per-channel statistics are computed once at normalize time and consumed by both the export manifest (design §6) and future threshold-calibrating rules. Recomputing them per export means reading parquet. |
| `action_level`, `state_space`, `state_dim` | Design §3's gating operates on levels and on `state_spec.space`; the sketch stored only the action side.                                                                                                    |
| `physical_dim`                             | `action_dim` alone cannot answer "how wide is the physical part" — C's 8-D action has 7 physical channels. Exports and QC both need the physical width.                                                     |
| `task`                                     | The episode-level language instruction. `has_language` says one exists; the report and the export manifest need the string.                                                                                |
| `last_error`                               | `Episode.failed()` carries the error. Storing it is what makes a failed episode diagnosable without re-running.                                                                                            |

`stream_specs_json` from the sketch is **not** created in schema version 1. No M1 source has an
own-timeline signal; it arrives with D in M4, along with the first `SignalClock.OWN_TIMELINE`
data. Adding an always-NULL column now would be schema theatre.

`episode_state`, `attempt` and the lease columns are likewise deferred to M2, which is where
resume and staleness are specified; M1's `episodes.status` is the whole state machine.

## Decision 2 — the normalized store is keyed by `upstream_id`, not by `episode_uid`

`episode_uid` is `"<source_id>:<upstream_id>"`. A colon is a legal POSIX filename character but
is an alternate-data-stream separator on Windows and is routinely mangled by object-storage
tooling and by shell completion. The artifact path is therefore

```
store/normalized/<source_id>/<upstream_id>/frames.parquet
                                          /episode.json
```

which carries exactly the same information, splits into the natural prefix for the object-store
migration in §10, and needs no escaping. `frames_path` is stored **relative to the store root**,
so moving or copying a store does not invalidate the catalog.

`upstream_id` is still sanitised (`/` → `__`, `:` → `_`) because source C's identity is a
composite of split, shard and index, and source D's is a video plus a segment id.

## Decision 3 — an `ERROR` verdict rolls up to `REVIEW`, not `FAIL`

`Verdict.ERROR` means *our rule raised an exception*, not *the data is bad*. Rolling it up to
`FAIL` would silently discard an episode on the strength of a bug in our own code, which is the
same category of error as zero-filling missing data. It rolls up to `REVIEW`, so the episode is
withheld from the default export but a human is pointed at it. The rule's exception type and
message are stored in `qc_results.reason`.

## Decision 4 — an absent upstream fact stays absent

Two places where M1 could have guessed and does not:

- `CameraSpec.mount` is `WRIST` only when the upstream camera name says so, and `UNKNOWN`
  otherwise. LeRobot's pusht has one camera named `observation.image` with no mount
  information anywhere in `meta/info.json`; `FIXED` is the likely answer and is still a guess.
- `EpisodeBoundary.success` is `None` with `success_adjudicator = NONE` when the dataset has no
  success column at all — as distinct from `success = False`, which asserts a failed attempt.

## Consequences

- Design §4's `episodes` sketch is superseded by
  `src/rdp/infrastructure/persistence/schema.sql`, which carries `PRAGMA user_version = 1`.
- A schema change is a re-normalization, not a migration: `store/raw/` is authoritative and
  immutable, `store/normalized/` is derived and disposable, so M2's staleness predicate can
  rebuild any episode from bytes we already hold.
