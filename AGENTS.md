# Robot Demonstration Pipeline (`rdp`) — Agent Spec

## Project Overview

A pipeline that ingests **robot demonstration data from four heterogeneous sources**, normalizes
it into a **unified schema that preserves each embodiment's native semantics**, runs **quality
control**, and **exports curated training subsets** — with crash-resumable, idempotent ingestion.

The hard problem is **not** throughput. It is that the four sources disagree about what an
"action" even is:

| Source | Dataset                             | Format                 | Action semantics                                                       |
| ------ | ----------------------------------- | ---------------------- | ---------------------------------------------------------------------- |
| A      | `lerobot/pusht`                     | Parquet + MP4          | Absolute task-space xy, **in pixels**                                  |
| B      | `lerobot/aloha_sim_insertion_human` | Parquet + MP4          | Absolute joint angles (rad) + gripper, 14-D dual-arm                   |
| C      | OXE / RLDS (`berkeley_autolab_ur5`) | Nested TFRecord        | End-effector **delta** pose (m/rad) + absolute gripper + control flags |
| D      | EPIC-KITCHENS-100                   | CSV + JSON + IMU + MP4 | **Episode-level symbolic label** `(verb, noun)` — no per-frame action  |

So: **unify the structure, never the numbers.** Any instinct to squash everything into one
fixed-width vector is wrong and is explicitly rejected by the design.

## Current State

**M0–M4 are complete. M5 is next: the full QC ruleset with data-driven thresholds.**

`rdp run && rdp export && rdp report` works end to end on real data for **all four** sources —
`pusht`, `aloha_sim_insertion`, `berkeley_ur5` and `epic100`: **202 episodes committed, 0
failed**. **Both acceptance scenarios are green and machine-checked**: a crash at any of the
eight checkpoints resumes to a byte-identical result, and a re-run on unchanged upstream writes
nothing at all. `scripts/demo_crash_resume.sh` reproduces the reviewer's scenario with a real
`kill -9`.

M3 proved the adapter seam. Adding **B cost no Python at all** (one `sources.yaml` entry, one
`embodiments.yaml` entry, one fixture, sharing `LeRobotAdapter` with A); adding **C** cost one
adapter, a 130-line stdlib TFRecord reader and one streaming method on the fetcher — with
`application/` untouched and exactly one line added to `domain/`.

M4 was the one that pushed back on the schema. D forced `stream_specs` / `streams` (invariant 17:
a signal whose clock is not the frame clock gets its own `streams/<id>.parquet`), `Channel.origin`
with a QC severity downgrade (invariant 13), and per-**episode** capabilities. It also produced
the milestone's most useful lesson: after every gate was green on fixtures, the first unlimited
run against real servers found **four** defects, one of them two milestones old and costing 35 of
50 aloha episodes. See ADR 012 and ADR 013 — the theme is that a fixture must be a scale model of
the data's _structure_, not of its _size_.

```
docs/technical_design.md      # the design authority — domain model, schema, QC, layering
docs/implementation_plan.md   # milestones M0–M8, each with verification commands
docs/adr/000..004             # M0's decisions: source selection, no-TF RLDS reader,
                              #   LeRobot v3.0 + lost termination, C's 8-D action, EPIC fps/IMU
docs/adr/005..006             # M1's: pusht's synthesized clock, catalog schema + store layout
docs/adr/007                  # M2's: resume, leases, and the honest crash criteria
docs/adr/008..009             # M3's: B's channel-level units + unverifiable gripper direction;
                              #   C's re-shard-proof identity, required clock, padding trimming
docs/adr/010..013             # M4's: EPIC's two frame numberings; layered availability and
                              #   signal origins; gyro/accel are two clocks (supersedes 011 §3);
                              #   LeRobot's global row index + qc_results is history, not state
docs/assessment.md            # original requirements (Chinese)
docs/assessment_for_ai.md     # requirements, agent-facing
docs/ai_chat_sessions/*.json  # raw AI transcripts — archival, do not edit or translate
pyproject.toml                # uv project; `spike` dependency group is M0-only and throwaway
config/{sources,embodiments,qc}.yaml   # sources, per-embodiment channel semantics, ruleset
spikes/probe_*.py             # throwaway probes; _out/*.txt is their captured output
src/rdp/{domain,application,infrastructure,interfaces}/
tests/{unit,integration,acceptance,fakes,fixtures}/     # 172 tests, ~25 s; domain coverage 97.7%
scripts/make_fixtures.py      # regenerates the four mini fixtures (~505 KB total)
scripts/demo_crash_resume.sh  # real kill -9 against a throwaway store under .demo/
```

**Read `docs/adr/000`–`013` before touching an adapter or the resume logic.** M0 measured four
things the design had guessed wrong, M1 a fifth, M2 corrected one exit criterion that was
physically unachievable as written, M3 corrected C's episode identity, its padding rule and the
rotation representation, and M4 reversed which of EPIC's two frame numberings we store and split
its IMU into two streams; all are reconciled in `docs/technical_design.md`.

Only what a milestone needs gets built; do not scaffold ahead of the plan.

## Prime Directive: the two acceptance scenarios

Every design and implementation decision is subordinate to these two, which a reviewer will
execute by hand:

1. **Run once → `kill -9` during the QC stage → restart.** It must resume from the checkpoint,
   not start over.
2. **Run again.** It must detect that there is **no new data** and not re-ingest.

Consequences that are non-negotiable:

- The stage machine is persisted **outside the process** (SQLite). Each episode's stage advance
  is **its own transaction** — never "write everything at the end of the run".
- Every write is idempotent. Idempotency key: `(source_id, upstream_id, content_hash)`.
- Artifacts are written `*.tmp` → fsync file → fsync dir → `os.replace()`. **File first, DB
  state second.** A crash in between must leave a state the re-run can idempotently redo.

If a change makes either scenario harder to satisfy, the change is wrong regardless of how clean
it looks.

## Design Authority

- `docs/technical_design.md` is the spec. **Read the relevant section before implementing.**
- **While a component has no code, the design wins.** Once code exists, the code is the truth for
  _what is_ and the design is the truth for _what should be_.
- A conflict between them is **never resolved silently**. Either fix the code, or update the
  design **and** add `docs/adr/NNN-*.md` in the same change. Drift between doc and code is the
  single failure mode this project is most graded on.
- Skills under `.agents/skills/` restate design facts for fast loading. **Where a skill and the
  code disagree, the code wins — then update the skill.**

## Repository Layout and the Dependency Rule

DDD + Clean Architecture, four layers. **Arrows point inward only.**

```
src/rdp/
  domain/          # entities, value objects, pure domain services. Pure Python + a little numpy.
                   # ZERO IO. Imports no sqlite3 / pyarrow / requests / tfds / application / infrastructure.
  application/     # use-case orchestration + port Protocols. Depends on domain only.
  infrastructure/  # source adapters, SQLite repo, parquet store, atomic FS, ffprobe, config loading.
  interfaces/      # typer CLI + presenters.
tests/{unit,integration,acceptance,fakes,fixtures}
config/{sources.yaml, sources.local.yaml, qc.yaml, embodiments.yaml}
scripts/demo_crash_resume.sh
```

The four ports are the whole scale-out story: `SourcePort`, `EpisodeRepository` + `UnitOfWork`,
`FrameStore` / `BlobStore`, and `FaultInjector`. Adding a source = one `SourcePort`
implementation + one config entry, with zero change to `domain/` and `application/`.

**`import-linter` enforces this from day one.** An unenforced layering rule decays within a week.

For layer placement, bounded contexts, and the port catalogue, load the `architecture` skill.

## Stack and Commands

- **Python 3.12**, pinned as `requires-python = ">=3.12,<3.13"`.
  Do **not** "upgrade" this pin. The system `python3` is 3.14 and will not work: the M0 spike
  needs TensorFlow/TFDS for source C, and TensorFlow ≥ 2.16 supports 3.12 but not 3.13+.
- **`uv` for everything** — never `pip`, `virtualenv`, or `python -m venv` directly.
- Libraries: `pydantic` v2, `numpy`, `pyarrow`, `typer`, `rich`, `pytest`, stdlib `sqlite3`.
- Tooling: `ruff`, `mypy`, `import-linter`, `pytest-cov`.

```bash
uv sync                                  # install
uv run rdp run --source pusht            # ingest; --source is required, there is no all-sources mode
uv run rdp export --budget 50000 --strategy sequential --out exports/subset.jsonl
uv run rdp report

uv run pytest                            # all tests
uv run pytest tests/acceptance -q        # the two acceptance scenarios
uv run pytest --cov=src/rdp/domain --cov-fail-under=90
uv run ruff check . && uv run mypy src/rdp && uv run lint-imports
bash scripts/demo_crash_resume.sh        # real kill -9, reproduces the reviewer's scenario
```

`sequential` is the only export strategy so far; `balanced` is M6's, and `rdp doctor` is not
implemented. Do not cite either as if it existed.

## Test Strategy

Bug fixes and features are **regression-first**: write the failing test, watch it fail, then fix.

| Layer         | Count | Dependencies                                         | Budget |
| ------------- | ----- | ---------------------------------------------------- | ------ |
| `unit`        | 94    | none — domain only, all fakes                        | < 2 s  |
| `integration` | 52    | real SQLite in tmpdir, real parquet, mini fixtures   | < 20 s |
| `acceptance`  | 26    | in-process crash matrix + real subprocess `os._exit` | < 60 s |

- Crash resume is tested through the `FaultInjector` port (a **production port created for
  testability**; the production implementation is a no-op). `pytest.mark.parametrize` covers all
  8 crash points — `fetch.before/after`, `normalize.after_write_before_commit`, `qc.mid_rule`,
  `commit.after_file_before_db`, etc. Manual testing cannot do this.
- Fakes test **exhaustiveness**; the real `kill -9` script tests **realism**. Both are required.
- Assert resume the strong way: after restart, `FakeSource.fetch`/`normalize` call counts must
  **not increase**, and the final DB + parquet hashes must equal the uninterrupted baseline.
  "It didn't crash" is not an assertion.
- QC rules are pure functions — test each one against hand-constructed bad data first.
- Adapters get **characterization tests** against committed mini fixtures (`tests/fixtures`,
  < 1 MB total), not strict TDD; a wrong channel mapping is the most insidious bug in this
  project and must be locked down by golden files.
- Domain coverage target is **90%+ for `domain/` only**. Do not chase 100% overall.

## Conventions

- **Everything in this repo is written in English** — code, comments, docs, commit messages, CLI
  output. The one exception is `docs/assessment.md` and `docs/ai_chat_sessions/*.json`, which are
  archival originals and stay as-is.
- **Ubiquitous language, no synonym drift.** It is an `Episode` — never `trajectory`, `demo`, or
  `rollout`. The full term table is design §8.1 and it governs code, DB columns, docs, and CLI
  output alike.
- **Never trust upstream field names.** pusht's channels are named `motor_0`/`motor_1` and are
  not motors. Semantics are asserted by our own `embodiments.yaml`, in the adapter layer.
- **Never zero-fill missing data, never guess a unit, never guess a role.** `NULL` +
  `raw_extra` is always better than a plausible-looking invention. A wrong guess is more harmful
  than an absence.
- **Semantics live at the channel level**, not the spec level — `unit`, `space`, `is_delta`,
  `frame`, `origin`, `is_physical` are per-channel. Source C alone disproves every spec-level
  shortcut.
- **Unmodeled upstream data is preserved, not dropped**: per-episode → `raw_extra`; per-frame →
  `raw.<name>` columns in `frames.parquet`, all registered in `raw_frame_columns`.
- `raw/` is authoritative and immutable; `normalized/` is derived and disposable. Schema changes
  are therefore a targeted re-normalization, never a migration script.
- Config: `config/sources.yaml` is committed; `config/sources.local.yaml` is gitignored.
  **No absolute local paths and no credentials in committed files** — `rdp doctor` checks this.

## Explicitly Rejected Patterns

Do not introduce these. Each was considered and rejected in design §8.6; reintroducing one is a
regression, not an improvement.

| Rejected                                         | Why                                                                                      |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| Domain events / event bus / event sourcing       | Single-process sequential pipeline; a state machine + one `episode_state` table suffices |
| CQRS / read-write model separation               | The read side is a handful of aggregate SQL queries                                      |
| A `Service`/`Manager` layer above `Repository`   | The use-case classes already are the application services                                |
| An ORM (SQLAlchemy)                              | Small schema; we need exact control of transactions and PRAGMAs                          |
| A Factory/Builder per value object               | pydantic validating constructors are enough                                              |
| A generic "ETL framework" abstraction            | Four sources. YAGNI. There is exactly one plug-in point: `SourcePort`                    |
| Unifying all embodiments into one fixed vector   | Destroys the semantics this project exists to preserve                                   |
| Min-max normalization at ingestion time          | Ingestion preserves; normalization is a downstream concern                               |
| Multiprocessing for throughput                   | Not a goal this round; it trades away crash-resume clarity                               |
| EAV / generic key-value / unbounded `extensions` | Defers validation to runtime — a schema in name only                                     |
| Truncating episodes to hit an export budget      | Manufactures boundaries that do not exist upstream (design §6)                           |
| A web dashboard                                  | ~100 episodes; the REVIEW queue is one SQL query                                         |

## Key Files

| File                                                            | What it is                                                                                                 |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| [docs/technical_design.md](docs/technical_design.md)            | The design authority. Sections 2 (schema), 3 (QC), 5 (resume), 6 (export), 8 (methodology), 10 (scale-out) |
| [docs/implementation_plan.md](docs/implementation_plan.md)      | M0–M8, Definition of Done, verification commands                                                           |
| [docs/technical_design.md](docs/technical_design.md) Appendix A | Real data shapes for all four sources — read before touching an adapter                                    |
| [docs/assessment_for_ai.md](docs/assessment_for_ai.md)          | The requirements                                                                                           |

## Available Skills

Load these when working in the relevant domain:

| Skill                                                        | When to use                                                                                                                                                                                                                                                           |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`architecture`](.agents/skills/architecture/SKILL.md)       | Where code belongs; adding a use case / port / adapter; layer and dependency rules; bounded contexts; test placement; structural bug fixes                                                                                                                            |
| [`unified-schema`](.agents/skills/unified-schema/SKILL.md)   | Anything touching `SignalSpec` / `Channel` / `Capabilities` / `Provenance` / `EpisodeBoundary`, the domain invariants, the `frames.parquet` column contract, or robotics semantics (action vs state, delta vs absolute, gripper conventions, terminated vs truncated) |
| [`source-adapters`](.agents/skills/source-adapters/SKILL.md) | Writing or debugging an adapter for source A/B/C/D; the anti-corruption-layer protocol for upstream facts the schema cannot express; per-source traps and identity/hash rules                                                                                         |
