# ADR 013 — Two defects the fixtures could not show: LeRobot's global row index, and QC history read as current state

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M4 (depth: source D)
- **Affects:** design §1.1 (source A/B facts), §5 (staleness), §7 (report),
  `infrastructure/sources/lerobot_adapter.py`, `infrastructure/persistence/repositories.py`,
  `scripts/make_fixtures.py`

## Context

M4's exit criteria were met offline, on fixtures, with every gate green. Then the pipeline was run
against all four upstreams with no `--max-episodes`, and two defects appeared that had been
present and invisible for two milestones. Both are recorded here because both are instances of the
same mistake — **a fixture that is a scale model of the data instead of a scale model of its
structure** — and that mistake is more valuable than either fix.

## Decision

### 1. LeRobot episode boundaries are dataset-global row ids, not positions inside a file

`meta/episodes/*.parquet` gives each episode a `dataset_from_index` / `dataset_to_index`. The
adapter used them as a `slice` into the shard it had just read:

```python
rows = pq.read_table(shard).slice(start, stop - start)   # wrong
```

That is correct for `data/chunk-000/file-000.parquet` and for nothing else. `aloha_sim_insertion`
puts its 50 episodes across two data files; every episode living in `file-001` has
`dataset_from_index` beyond that file's row count, so the slice returned **0 rows** and normalize
died at `action.shape[1]` with `IndexError: tuple index out of range`.

**Real impact: 35 of 50 aloha episodes were lost** — silently in the sense that the pipeline
reported them as failures without ever explaining why, and `pusht` (one data file) was unaffected,
so the suite stayed green.

The fix reads the boundary for what it is — a value of the dataset-global `index` column — and
verifies the count it selected:

```python
index = table.column("index").to_numpy(zero_copy_only=False)
rows = table.filter(pa.array((index >= start) & (index < stop)))
if rows.num_rows != stop - start:
    raise InvariantViolation(...)
```

The count check is the part worth keeping. The original bug produced an _empty_ result, not an
error; any selection that cannot prove it got what the metadata promised should refuse rather
than hand a short table downstream.

The fixture was the real defect, so the fixture is where the regression lives:
`make_lerobot(..., split_last=True)` now moves the last episode's rows into a second data file, so
`tests/fixtures/aloha_mini` reproduces the multi-file shape in 147 KB. Fixtures must be small in
_bytes_, never in _shapes_.

`lerobot@1.1.0`; ADR 012 §2 is what lets the already-poisoned stagings be rewritten.

### 2. `qc_results` is history; the report was reading it as state

`qc_results` keys on `(episode_uid, rule_id, run_id)` — deliberately, so a ruleset change leaves
an audit trail of what was concluded when. `rdp report` then counted **every row**, so a catalog
of 202 committed episodes reported 325 `TS_MONOTONIC` verdicts and 237 `ACTION_RANGE` passes.
Every number was a sum over re-QCs, printed next to a stage table that was a current-state count.

Unscoped queries now read the newest row per `(episode_uid, rule_id)`; `verdict_counts(run_id)`
still answers the different, equally legitimate question "what did that run conclude". The same
correction applies to `rules_hit`, where the stale reading was worse than cosmetic: an episode
re-QC'd clean still reported the FAIL a superseded ruleset had given it.

Post-fix, every rule sums to exactly 202.

## Consequences

- No schema change and no re-ingestion: the history was always right, only the read was wrong.
- Reports generated before this change over-count and should not be compared against later ones.

## Alternatives rejected

| Alternative                                                    | Why not                                                                                                                           |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Keep one row per `(episode, rule)` and overwrite on re-QC      | Destroys the audit trail — the reason the ruleset is versioned at all                                                             |
| Default `rdp report` to the latest run's verdicts              | A no-op re-run would report zero verdicts for a fully QC'd catalog                                                                |
| Read the LeRobot boundary from the file's own row count        | Assumes files are written in episode order and never re-sharded — the same assumption ADR 009 already had to abandon for source C |
| Add a `data/file_index` lookup instead of filtering on `index` | It is already used to pick the _file_; the row selection still has to be by global id                                             |
