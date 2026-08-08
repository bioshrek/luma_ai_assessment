# ADR 017 — The report as one statistical vocabulary: measured vs derived, and a checker that re-derives it

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M7 (reporting depth and observability seams)
- **Affects:** design §7 (reporting), `domain/run.py`, `application/build_report.py`,
  `application/ports.py` (`Clock.monotonic`, `StoreInspector`, three repository queries),
  `infrastructure/persistence/repositories.py`, `infrastructure/storage/maintenance.py`,
  `interfaces/presenters/report_md.py`, `interfaces/cli.py`,
  `scripts/check_report_consistency.py`

## Context

M7's goal is that the report becomes "the single statistical vocabulary of the system — the same
numbers a future metrics exporter would emit". Up to M6 there were three places a number could
come from: the live `IngestionRun` accumulator, an ad-hoc SQL aggregate in `BuildReport`, and a
presenter that did its own arithmetic on the way to the screen. Nothing stopped them disagreeing,
and nothing checked that any of them was right.

Making them agree raised five questions the design's four-line §7 does not answer.

## Decision

### 1. The domain owns the definitions; presenters format and never compute

`domain/run.py` now holds the _names and definitions_ of every statistic: the stage constants,
`failure_reason()`, `RuleRate` and `rule_rates()`. Markdown, JSON and the console table all read
the same `Report`/`IngestionRun` and format it. Three presenters that each divided `hits` by
something slightly different is exactly how a metrics dashboard ends up disagreeing with the
report it was built from.

Two definitions were contested and are now fixed:

- **`hit_rate` divides by the episodes the rule actually ran on**, not by the corpus. Dividing by
  the corpus flatters a rule that skipped most of it: `POSE_COVERAGE` skips 182 of 202 episodes,
  and 0 hits out of 20 evaluations is a very different statement from 0 out of 202.
- **A failure is bucketed by its exception type, not its message.** Messages carry episode uids
  and paths, so counting them verbatim gives one bucket per episode and a "top failure reasons"
  table that names nothing.

### 2. Skip reasons are `{rule_id: {reason: n}}`, never a flattened key

The pre-M7 accumulator counted skips under the single string `f"{rule_id}:{reason}"`. That is a
bucket, not a breakdown: it cannot answer "how often did `ACTION_JERK` skip?" without string
surgery, and it invites a future caller to concatenate a _different_ separator. The counter is
now nested, at every layer — accumulator, `stats_json`, SQL (`GROUP BY rule_id, reason`), and both
presenters.

The corpus shows why the breakdown has to survive: `STATE_ACTION_ECHO` skips for **three distinct
reasons** — 40 episodes have no state, 20 have an episode-label action, and 12 are "not
comparable: action is mixed, state is unknown". One bucket of 72 would have hidden all three.

### 3. Wall time is measured; everything else in the report is derived

The report now has exactly one class of number the catalog cannot be asked for again, and it is
labelled as such in code (`report_md.MEASURED_SECTIONS`): stage wall time, the recovery
findings (what the filesystem looked like after a crash), and the free-text failure strings.
Everything else — counters, verdicts, skip reasons, corpus totals, the source × embodiment
cross-tab, rule rates, disk usage — is a query.

Timing is taken with `Clock.monotonic()`, added to the port beside `now_iso()` because wall time
can step backwards and a negative stage duration is worse than none. The measurement wraps each
stage in a `try/finally` context manager, so a stage that fails _slowly_ is still visible: the
common production symptom is not "fetch broke" but "fetch got slow, then broke".

`stage_calls` is counted separately from the episode counters and does not equal them. The
crash-resume demo makes the difference legible: the resumed run shows `fetch` over **66**
episodes and `normalize` over **67**, because one episode was already fetched when the process
was killed and resumed at the next stage.

| stage     | seconds | episodes | s/episode |
| --------- | ------: | -------: | --------: |
| fetch     |   0.399 |       66 |     0.006 |
| normalize |   0.176 |       67 |     0.003 |
| qc        |   0.105 |       67 |     0.002 |
| commit    |   0.016 |       67 |     0.000 |

**A run that predates the measurement says so.** Rendering `0.000` for the 30 runs already in
`reports/` would read as "instant" rather than "not measured", which is the project's
never-zero-fill rule applied to its own telemetry. The presenter emits
`_Not measured: this run predates stage timing._` when `stats_json` has no `stage_seconds` key.

### 4. Disk usage is a new port, not a new duty for `ArtifactMaintenance`

Store size is the one cumulative number that is not in the catalog. It could have been bolted
onto `ArtifactMaintenance`, which already walks the store — but that port exists to find and
remove crash debris, and giving it a reporting method would mean the reporting use case depends
on a recovery interface. `StoreInspector` is a separate one-method port; `StoreMaintenance`
satisfies both structurally, so there is no new class, only a narrower contract for the caller.

This forced one small move: `StoreMaintenance` needs the catalog's filename to size it, and only
`interfaces/wiring.py` knew it. `CATALOG_FILE` now lives in
`infrastructure/persistence/catalog.py` and both import it — infrastructure may not import
interfaces, and a second hard-coded `"catalog.sqlite"` would have been the kind of duplication
that survives until someone renames the file.

**Disk usage is measured, and therefore not stable across processes.** `catalog.sqlite` grew from
4 KB to 168 KB between two renders of the same report, with no row changed: closing the last WAL
connection checkpoints the write-ahead log into the main file. So the "identical output twice,
days apart" guarantee holds for every _derived_ number, and disk usage tracks the store as it
actually is. The consistency checker takes its own measurement inside the same process, before
the catalog is closed, for the same reason.

### 5. The consistency checker parses the rendered markdown, and an unknown section is a failure

`scripts/check_report_consistency.py` re-derives every non-measured number with its own SQL and
diffs it against the report — but against the **rendered markdown**, not the `Report` object. A
number the presenter formats wrongly is exactly the drift the checker exists to catch, and
comparing objects would compare the report to itself.

The queries are deliberately spelled differently from the repositories'. The latest verdict per
`(episode, rule)` is a `ROW_NUMBER()` window in production and a correlated `rowid` subquery in
the checker: two spellings of one intent that must agree. The checker also prints the sections it
compared (11 of them) — a checker that silently compares nothing also passes.

Most importantly, **a section the checker does not recognise is a mismatch**, not a gap. Adding a
table to the report without adding a query for it fails the check. The only exemptions are the
three `MEASURED_SECTIONS`, and the exemption list lives in the presenter, so it is visible at the
point where a new section would be added.

## Consequences

- `rdp report` grew `--run`, `--cumulative` and `--format table|md|json`. JSON and markdown go to
  plain stdout via `typer.echo`, not through `rich`: `rdp report --format md > run.md` must
  produce the same bytes the presenter rendered, unwrapped and unstyled.
- `RunReporter` now has three implementations — JSON file, markdown file, console table. The
  console one is the milestone's evidence for the port being a seam: it required **no change in
  `application/` or `domain/`**, only a class in `interfaces/presenters/` and one line in
  `wiring.run_reporters()`. The CLI no longer prints counters itself; it publishes to the port.
- `Report.rule_counts` is gone, replaced by `run_verdicts` (scoped to the run) and
  `cumulative.verdicts` (the catalog's current opinion, latest per episode and rule). The two were
  conflated before, which is how M4's 202 episodes once reported 325 `TS_MONOTONIC` verdicts.

## What the cumulative view immediately revealed

The rule-rate table is the first thing in the project that reports on the _ruleset_ rather than on
the data, and it says something uncomfortable: **three of eleven rules have never evaluated a
single episode.**

| rule                   | evaluated | skipped | why                                      |
| ---------------------- | --------: | ------: | ---------------------------------------- |
| `TS_MONOTONIC`         |         0 |     202 | every source's clock is synthesized      |
| `FPS_DRIFT`            |         0 |     202 | same                                     |
| `VIDEO_FRAME_MISMATCH` |         0 |     202 | `with_video: false` for the whole corpus |

This is not a defect and it is deliberately **not** being fixed by manufacturing data — the same
judgement as M5's refusal to manufacture a FAIL. It is a measured statement about the corpus:
four sources, none of which ships a real per-frame clock, and a configuration that does not
download video. It is recorded here so that M8's "known limitations" can state it rather than
leave a reviewer to discover that a third of the ruleset is untested against real data.

## Alternatives considered

- **Cache the report in a `reports` table.** Rejected: a cached report can be stale, and the
  design's own claim is that the report _is_ a query. Recomputation costs milliseconds over 202
  episodes and a handful of aggregate queries.
- **Let the checker call the repositories.** Rejected: it would then verify that the code equals
  itself. The value is entirely in the second, independent formulation.
- **Emit Prometheus metrics now.** Rejected as scaffolding ahead of the plan. The point of
  putting the definitions in `domain/run.py` is that an exporter would read them rather than
  invent a fourth set.
