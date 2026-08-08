# ADR 007 — M2 resume, leases, and the honest form of the "no repeated work" criterion

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M2 (resume and idempotency)
- **Affects:** design §5 (resume, staleness), §4 (catalog schema), §8.5 (test strategy),
  `docs/implementation_plan.md` M2 exit criteria

## Context

M2 makes the two acceptance scenarios real: crash mid-run and resume from the checkpoint, and
re-run to find nothing new. Building it settled five questions the design either left open or
stated in a form that turns out to be unachievable. Per the repo's prime rule, each is recorded
here rather than absorbed silently into the code.

## Decision 1 — the recorded stage is the last _durably completed_ stage, so recovery never rolls back

`episode_state.stage` is only ever written **after** the work it names has been done and the
artifact is on disk. Nothing is ever marked "in progress" durably; in-progress is expressed by
the lease, not by the stage.

The consequence is the whole reason the resume logic is small: **there is nothing to undo.** A
worker that dies mid-stage lost work that was never recorded, so recovery has no rollback path,
no compensating action and no partially-applied state to reason about. It only has to release
the lease. The alternative — recording `NORMALIZING` before normalizing — would have forced a
rollback routine per stage, and every one of those routines is a place to get crash-safety wrong.

## Decision 2 — a resume reconstructs the raw handle instead of re-fetching

An episode found at `FETCHED` has, by decision 1, its raw bytes already on disk. Calling
`SourcePort.fetch` again would be a no-op for correctness (the adapters are idempotent, via
`STAGED_MARKER`) but would still re-walk and possibly re-download a dataset that the catalog has
already accounted for. So `IngestEpisodes` rebuilds the `RawEpisode` handle from
`(ref, staging_dir, source.revision)` and goes straight to `normalize`.

This is what makes `Staleness.REDO_NORMALIZE` rewind to `FETCHED` rather than `DISCOVERED`: raw
bytes are immutable and re-fetching them can only cost bandwidth.

## Decision 3 — the exit criterion is "at most one in-flight stage is redone", not exact call-count equality

`docs/implementation_plan.md` originally required, for a crash at **any** of the eight
checkpoints, that `FakeSource.fetch` and `.normalize` call counts across the crashed run plus
the resume equal the counts of a single uninterrupted run. Two checkpoints cannot satisfy that,
and should not:

| Checkpoint                            | Extra calls on resume | Why                                                      |
| ------------------------------------- | --------------------- | -------------------------------------------------------- |
| `fetch.after`                         | `fetch` +1            | crashed after fetching, before the `FETCHED` row existed |
| `normalize.after_write_before_commit` | `normalize` +1        | crashed after writing parquet, before the row existed    |
| the other six                         | none                  | the crash fell outside a side effect                     |

These are precisely the "file written, DB state not yet advanced" windows that the file-first
ordering creates on purpose. Redoing that one unit of work is the _correct_ behaviour: the
alternative is trusting an artifact that no transaction ever vouched for. Making the cost
explicit and per-checkpoint is strictly more informative than a blanket equality that would have
to be weakened to pass.

The criterion is therefore: **a crash costs at most the single stage that was in flight, and
never a stage the catalog recorded as complete.** `tests/acceptance/test_resume.py` encodes this
as a `REDONE` table, parametrized over `ingest_episodes.CHECKPOINTS`, so a new checkpoint cannot
be added without declaring its cost. The plan has been amended to match.

## Decision 4 — a lease is reclaimable when its TTL has passed _or_ when it names our own worker slot

A pure TTL makes M2 untestable in the small: after a `kill -9` the dead process's 15-minute
lease is still nominally valid, so a restart one second later would refuse to touch its own
episodes.

The single-node fact that resolves this: one SQLite catalog admits one writer at a time
(`BEGIN IMMEDIATE`), and `owner` names a worker _slot_, not a process. Finding a lease bearing
our own slot id at startup can therefore only mean the previous holder of that slot died.
`EpisodeState.lease_reclaimable` returns true in that case, and falls back to the TTL otherwise.

The TTL is not dead weight: it is exactly the predicate that survives the move to Postgres and
per-worker owner ids (design §10.2), where "our own slot" stops being a safe assumption.

## Decision 5 — in-process crashes for exhaustiveness, `os._exit` for the process boundary, SIGKILL for realism

Three mechanisms, each proving something the others cannot:

| Mechanism                          | Where                                | Proves                                                                   |
| ---------------------------------- | ------------------------------------ | ------------------------------------------------------------------------ |
| `RaisingFaultInjector`             | `tests/acceptance/test_resume.py`    | every one of the 8 checkpoints recovers, with byte-identical results     |
| `EnvFaultInjector` → `os._exit(1)` | `tests/acceptance/test_cli_crash.py` | a real process death, through the CLI, on the real adapter               |
| external `kill -9`                 | `scripts/demo_crash_resume.sh`       | the reviewer's literal scenario; SIGKILL, which no handler can intercept |

The fake injector raises `Crash(BaseException)` rather than `Exception` on purpose: the
per-episode handler in `IngestEpisodes` catches `Exception` and writes a `FAILED` row. A crash
must write nothing at all, and that difference is the property under test. `EnvFaultInjector`
uses `os._exit` rather than `sys.exit` for the same reason at the process level — no unwinding,
no `finally`, no buffer flush, no `atexit`.

## Decision 6 — schema version 2 is an additive `ALTER TABLE`, not a migration script

M2 adds `episode_state`, `episodes.ruleset_version` and `runs.resumed_from`. All three are
additive, so `catalog.py` compares `PRAGMA user_version`, reads `PRAGMA table_info`, and issues
`ALTER TABLE ... ADD COLUMN` for what is missing. An existing M1 catalog keeps working.

This is affordable because of design §8.7's split: `raw/` is authoritative and immutable, and
`normalized/` is derived and disposable. A change that were _not_ additive would not be migrated
either — it would bump `schema_version`, which the staleness predicate already reads, and the
next run would re-normalize. Schema evolution here is incremental ingestion, not DDL surgery.

## Consequences

- `docs/implementation_plan.md` M2 exit criteria amended (decision 3).
- `docs/technical_design.md` §5 gains the checkpoint catalogue, the lease-reclaim rule and the
  recovery pass; §4 gains `episode_state` and the two new columns.
- Recovery is unconditional and runs before every ingestion, not behind a `--recover` flag: it
  is cheap, and a flag that must be remembered after a crash is a flag that will not be.
