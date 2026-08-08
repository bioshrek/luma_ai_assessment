# ADR 015 — What QC needed and the catalog did not store: three columns, and a latent M4 defect the first ruleset bump exposed

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M5 (full QC ruleset with data-driven thresholds)
- **Affects:** design §2.2 (schema), §5 (staleness), `domain/episode.py`,
  `infrastructure/persistence/schema.sql`, `infrastructure/persistence/catalog.py`,
  `infrastructure/persistence/repositories.py`, `infrastructure/sources/*_adapter.py`,
  `config/qc.yaml`

## Context

M5's new rules ask questions earlier milestones never asked, and two of them need facts about the
episode rather than about its frames: `TERMINATION_CONSISTENCY` needs to know **which raw column
carries the upstream end-of-episode marker**, and `SEGMENT_BOUNDS` needs the **annotation interval
the episode was cut from**. Both were reachable from the adapter and neither was reachable from
the catalog, which is where the QC stage reads its episode after a resume.

That distinction is the whole of this ADR. A re-normalize re-runs the adapter, so an unpersisted
field appears to work. A **re-QC does not** — it rebuilds the episode from the `episodes` row —
so an unpersisted field is silently empty exactly when a rule needs it.

## Decision

### 1. `EpisodeMeta` gains `segment` and `termination_column`; `SCHEMA_VERSION` becomes `1.1`

```python
segment: EpisodeSegment | None          # the annotation interval, when the episode was cut from one
termination_column: str | None          # the raw.* column carrying the end marker, when one survived
```

Both are `None` where the fact does not exist, never a default that a rule could mistake for
data. Invariant 19 ties the second to its capability: `termination_column` may be set only when
`has_termination_signal` is true, so a rule can never be handed a column name the source does not
actually publish.

`SCHEMA_VERSION` `1.0` → `1.1` rewinds every committed episode to `FETCHED` and re-normalizes
without re-fetching (design §5). All 202 episodes were re-normalized in one pass, 0 failures.

Per-source outcome:

| source                | `has_termination_signal` | `termination_column` |
| --------------------- | ------------------------ | -------------------- |
| `pusht`               | true (80/80)             | `raw.next.done`      |
| `aloha_sim_insertion` | true (50/50)             | `raw.next.done`      |
| `berkeley_ur5`        | false (0/12), see §2     | `None`               |
| `epic100`             | false (0/60)             | `None`               |

This corrects design §3's claim that _"B and D have no explicit end signal"_. **B does**:
LeRobot's v3.0 export of `aloha_sim_insertion` carries `next.done` even though it carries nothing
else about the ending (ADR 002). What B lacks is `terminated` vs `truncated`, which is a different
loss, and conflating the two would have made the rule skip on 50 episodes it can in fact judge.

`max_terminal_run: 2` is a measurement, not a tolerance: **pusht sets `next.done` on the last two
frames of all 80 episodes**, aloha on the last one of all 50. A trailing run of markers is not a
concatenation error — the FAIL condition is a marker with ordinary frames _after_ it — so the
corpus maximum becomes the ceiling, and a longer run reads as a flag that got stuck on.

### 2. C's termination capability is measured on the rows we keep, not the rows upstream sent

`berkeley_ur5`'s trailing boundary steps are trimmed during normalization: `is_last` is set on the
final two steps, both carrying an all-zero pose action, and keeping them would end every episode
with a fabricated "the robot stopped" motion (ADR 009). But `is_terminal` lives on those same
trailing steps. Declaring `has_termination_signal` from the upstream record would promise a marker
that the stored `frames.parquet` does not contain, and `TERMINATION_CONSISTENCY` would then
correctly report every C episode as truncated — a defect manufactured entirely by our own
trimming.

`_has_end_marker(example, keep)` therefore reads only the first `keep` steps. Measured, **no
marker survives the trim on any of the 12 `berkeley_ur5` episodes**: `is_terminal` is set only on
the two boundary steps we drop. So C declares `has_termination_signal=False`, the rule resolves
to a clean `SKIPPED`, and the alternative — 12 episodes reported as truncated by a defect our own
trimming introduced — never happens. The capability describes **the artifact**, not the upstream,
which is the same principle as content-hashing what we stored rather than what we downloaded.

### 3. `stream_specs` must be persisted — catalog schema 4

The first `ruleset_version` bump against the real corpus (`2.0` → `2.1`) failed **40 of 60**
`epic100` episodes with:

```
InvariantViolation: epic100:P01_103_0: stream tables ['accel', 'gyro'] != declared []
```

Invariant 17 requires that the stream tables on disk equal the streams the episode declares. M4
added `EpisodeMeta.stream_specs` and wrote `streams/<id>.parquet` for each, but `_row_to_episode`
never restored it and no column ever held it. Every run until now had re-derived the episode from
the adapter, so the gap was invisible for a whole milestone; the re-QC path — the one the
acceptance scenarios exist to protect — was the first to read the field back from the catalog and
find it empty.

`episodes.stream_specs_json` is added at `user_version = 4`, applied to existing catalogs by
`ALTER TABLE ... ADD COLUMN` like every schema change so far. The 40 failed episodes recovered on
the next run with no manual intervention, which is the stage machine working as designed: a
`FAILED` episode restarts from `fetch` and re-derives everything.

The regression test lives where the shape does — `tests/integration/test_epic_adapter.py` ingests
the mini fixture, reloads two episodes through the repository, and asserts one declares
`{gyro, accel}` and the other declares `{}`. The generic acceptance rig could not have caught it:
its `FakeSource` emits no streams, so the field was always empty on both sides of the round trip.

## Consequences

- Catalog `user_version` is 4; `SCHEMA_VERSION` is `1.1`; `ruleset_version` is `2.1`.
- A field on `EpisodeMeta` that is not in `_EPISODE_COLUMNS` is a latent bug of exactly this
  shape. The round trip — not the write — is what must be tested, and it must be tested with a
  fixture that has the field set.
- `raw/` was untouched throughout: every recovery above was a re-derivation of `normalized/`,
  which is the point of keeping raw authoritative and immutable (design §2.4).

## Alternatives rejected

| Alternative                                                         | Why not                                                                                                                 |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Re-run the adapter on the QC path so `stream_specs` is always fresh | Turns a threshold edit into a re-normalization of the corpus, and breaks the guarantee that a verdict describes bytes   |
| Store the streams as a list of ids instead of the full `SignalSpec` | A rule reading a stream needs its channels; the ids alone would move the same gap one layer down                        |
| Relax invariant 17 to "declared streams must be a subset of files"  | It exists to catch a stale `streams/` directory after re-normalization (ADR 012); weakening it hides that instead       |
| Infer `termination_column` in the rule from a naming convention     | "Never trust upstream field names" — `next.done`, `is_terminal` and nothing at all are three different upstream choices |
| Declare C's `has_termination_signal` from the upstream record       | Promises a marker the stored frames do not contain, and fails every C episode for a defect of our own making            |
| Rebuild the catalog instead of `ALTER TABLE`                        | Every change so far has been additive, and a rebuild would discard `qc_results` history for no benefit                  |
