# ADR 018 — Delivery documents in Chinese, the engineering record in English

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M8 (documentation, hardening, and delivery)
- **Affects:** `AGENTS.md` (Conventions), `README.md`, `docs/design_answers_zh.md`,
  `docs/ai/index.md`, `scripts/render_ai_sessions.py`

## Context

Every document in this repository is written in English, by a convention stated in `AGENTS.md`
and followed since M0. The two exceptions are archival originals that must not be touched:
`docs/assessment.md` (the brief as it was received) and `docs/ai_chat_sessions/*.json`.

M8 introduces a reader the earlier milestones did not have. M0–M7 were written for two audiences
that both prefer English: coding agents, and the code itself. M8's deliverables — `README.md`
and the answers to the assessment's documentation questions — are written for a **reviewer**, who
is a Chinese speaker, and whose stated evaluation criteria are about judgement and tradeoffs
rather than about code. The brief asks for the answers 在文档里说明; making that reviewer read
900 lines of English to find them adds friction to precisely the artifact that is being graded.

The naive fixes are both bad. Translating everything makes eighteen ADRs and a 1 457-line design
document into two copies that will disagree within a milestone — and doc/code drift is the single
failure mode this project is most graded on (the brief lists 文档和代码对不上 as an automatic
downgrade). Translating nothing leaves the graded documents in the wrong language.

## Decision

### 1. The boundary is audience, not topic

> **对外交付文档用中文；工程记录用英文。**

| Chinese                                                                | English                                                   |
| ---------------------------------------------------------------------- | --------------------------------------------------------- |
| `README.md`                                                            | `docs/technical_design.md`, `docs/implementation_plan.md` |
| `docs/design_answers_zh.md`                                            | `docs/adr/*.md`                                           |
| `docs/ai/index.md` and the generated chrome of `docs/ai/session_NN.md` | `AGENTS.md`, all skills under `.agents/`                  |
| `docs/ai/rejected.md`                                                  | code, comments, commit messages, CLI output, test names   |

The rule is a line drawn through the reader, not through the subject matter: a document a
reviewer is expected to read to form a judgement is Chinese; a document that exists to keep the
implementation honest is English. The Chinese documents carry the argument and link down into the
English ones for the evidence, and they say so explicitly, so the boundary reads as a decision
rather than as an unfinished translation.

### 2. No parallel English README, and no parallel Chinese design document

A `README.en.md` alongside `README.md` is the drift failure mode by construction: two files with
the same claims, one of which will silently stop being true. There is exactly one README.

The same argument forbids the mirror image. `technical_design.md` is the design authority — the
document `AGENTS.md` says wins over the code when a component has no code yet. Authority cannot
be held jointly by two files in two languages; one of them would become the real one and the
other a stale copy with equal apparent standing.

### 3. Quoted material is never translated

`docs/ai/index.md` derives each session's topic from the first line of that session's prompt.
Those strings are **quotations of the raw transcripts**, and the transcripts are the evidence for
a separately assessed dimension whose explicit anti-pattern is 看得出是重建版. A translated quote
in an artifact whose purpose is proving the record is original would undermine the artifact.

So the index localises its own prose — headings, column names, the reading notes — and leaves the
quoted prompts and the session bodies exactly as they are. The one addition is a `里程碑` column
parsed out of the prompt text (`milestone (\d)` → M0–M7). That is derived from the quotation, not
substituted for it.

### 4. The answers are split from the README

`README.md` is the entry point: what this is, one command to run it, what it produced, how to
reproduce the two acceptance scenarios, and an index table with a one-sentence conclusion per
review topic. `docs/design_answers_zh.md` holds the eight documentation answers in full.

Eight thorough answers is 400+ lines, and putting them in the README buries the quickstart — the
one thing a reviewer needs in the first thirty seconds — under prose. The brief also lists 文档
as an artifact alongside the repository, which is the shape being asked for. The split is made
invisible by the index table: every answer's conclusion is visible on the front page, with the
depth one link away.

## Consequences

- `AGENTS.md`'s Conventions section is amended in the same change as this ADR; the previous
  wording ("Everything in this repo is written in English … the one exception is
  `docs/assessment.md` and `docs/ai_chat_sessions/*.json`") no longer holds.
- The Chinese strings for the AI index live in `scripts/render_ai_sessions.py`, since that file
  generates them. Headings, table columns, `N 次工具调用`, `推理过程` and the reading notes are
  localised; the quoted prompts and the response bodies stay in whatever language the
  conversation was.
- The leak-scan command in the M8 verification block must keep excluding `docs/ai`, and now also
  has to tolerate Chinese filenames nowhere — filenames stay ASCII (`design_answers_zh.md`, not
  `设计答辩.md`) so that archives, `grep`, and non-UTF-8 shells all behave.
- A future reader who edits `technical_design.md` incurs **no** translation obligation. That is
  the point of the boundary: the English documents remain cheap to keep true.
