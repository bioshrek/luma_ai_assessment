# ADR 016 — Balanced curation: clamped square-root weights, residual redistribution, and a seed that is not a shuffle

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M6 (curation depth: the real sampling strategy)
- **Affects:** design §6 (training subset export), `domain/curation/sampler.py`,
  `domain/subset.py`, `application/export_subset.py`, `application/ports.py`
  (`ExportRepository`), `infrastructure/persistence/schema.sql` (user_version 5),
  `interfaces/cli.py`

## Context

Design §6 specifies the strategy in prose: stratify by embodiment, square-root smooth the
between-group weights, apply a floor and a cap, order by quality, round-robin over
`(source, task)`, never truncate, and make it reproducible with a seed. Turning that into code
raised four questions the prose does not answer, and answering them changed what the strategy
does at the margins. This ADR records those four answers; §6 has been updated to match.

The corpus this runs against: **196 PASS episodes / 41 418 frames** — `aloha_bimanual` 25 000 (50
episodes), `pusht_planar` 9 775 (80), `human_ego` 5 905 (59), `ur5_single_arm` 738 (7, because 5
of C's 12 are REVIEW and REVIEW is excluded by default).

## Decision

### 1. Clamping is iterative, and both bounds give way to the equal share

A floor and a cap cannot simply be applied to the smoothed weights: clamping one group changes
what the others must sum to, and renormalising them can push a second group out of bounds. The
weights are therefore **clamped and renormalised repeatedly** until no group violates either
bound — each round pins at least one group, so it terminates in at most one round per group.

Two degenerate cases are resolved in favour of a valid allocation rather than in favour of the
configured bound:

- with more than 20 groups, `floor = 5%` cannot hold for all of them;
- with two groups, `cap = 40%` cannot hold for either — the two caps sum to 0.8 and 20% of the
  budget would be allocated to nobody.

So the effective bounds are `floor = min(5%, 1/n)` and `cap = max(40%, 1/n)`. A bound whose only
achievable outcome is "weights that do not sum to one" is not a bound worth honouring.

Measured on the real corpus, this turns the raw frame shares into:

| embodiment       | eligible frames | proportional share | sqrt-smoothed | after clamping |
| ---------------- | --------------: | -----------------: | ------------: | -------------: |
| `aloha_bimanual` |          25 000 |              60.4% |         43.8% |      **40.0%** |
| `pusht_planar`   |           9 775 |              23.6% |         27.4% |      **29.2%** |
| `human_ego`      |           5 905 |              14.3% |         21.3% |      **22.7%** |
| `ur5_single_arm` |             738 |               1.8% |          7.5% |       **8.0%** |

ALOHA is 50 Hz and pusht is 10 Hz; without smoothing, ALOHA's clock alone would buy it 60% of
every budget.

### 2. Residual redistribution ignores the cap

A group can be unable to spend its quota — `ur5_single_arm` has 738 eligible frames against an
8% quota of 1 606 at a 20 000-frame budget. The unspent remainder is **re-offered to the groups
that still have episodes**, in weight order, repeatedly, until nothing fits anywhere. The cap is
**not** re-applied during redistribution: a cap exists to stop one embodiment crowding out
others that want the budget, and once nobody else wants it, honouring the cap would mean
discarding real training frames to keep a table tidy.

This is what makes the shortfall guarantee true in the stratified case as well: when the loop
ends, **every unselected episode is longer than the remaining budget**, so the shortfall is
strictly smaller than the shortest episode left behind (still never truncating — design §6.5).

The consequence, visible in the run above at a 20 000-frame budget: quotas of 8 380 / 6 347 /
4 934 / 1 606 against takes of 8 000 / 6 329 / 4 933 / 738, summing to exactly 20 000.

### 3. The seed is a keyed digest, not a random shuffle

`--seed` orders the episodes inside each `(source, task)` bucket by
`blake2b(f"{seed}:{episode_uid}")` rather than by seeding an RNG and shuffling. An RNG's output
depends on how many values were drawn before it and in what order, which makes the export's
result depend on dictionary iteration order and on how many groups happened to be processed
first. A keyed digest is a **pure function of the episode's identity**, so the same seed selects
the same episodes no matter what else the process did. Without a seed the order is the episode
uid, which makes an unseeded export just as reproducible as a seeded one.

Verified on the real corpus: two `--seed 7` exports are byte-identical, and `--seed 8` selects
130 episodes / 19 985 frames where `--seed 7` selects 128 / 20 000.

### 4. `balanced` is the default strategy, and `sequential` is kept as its control

Design §6 calls the cross-embodiment mix the default; the CLI now agrees. `sequential` is not
dead code — it is the measurement that shows what the stratification is worth. On the same
20 000-frame budget:

| strategy     | episodes | composition                                                      |
| ------------ | -------: | ---------------------------------------------------------------- |
| `sequential` |       40 | `aloha_bimanual` 20 000 — **one embodiment, 100% of the budget** |
| `balanced`   |      128 | 8 000 / 6 329 / 4 933 / 738 across **all four**                  |

`sequential` orders by `episode_uid`, so an alphabetically early source consumes the entire
budget. That is not a strawman — it is precisely what M1's export did, and it is the reason the
strategy needed replacing rather than tuning.

## Consequences

- **The catalog gained four `exports` columns** (`seed`, `embodiment`, `include_review`,
  `stats_json`), user_version 5, additive as every bump before it. An export whose seed and
  filters are not recorded is not reproducible, and "reproducible" was the requirement — this is
  the same lesson as ADR 015: **persist every field the decision was made from.**
- `SubsetPlan` gained `groups: tuple[GroupAllocation, ...]` and `stats()`. The CLI prints the
  quota-versus-take table, so a surprising split is visible at the moment of export rather than
  discovered later in training.
- **The assessment's example budget does not exercise the strategy.** At
  `--budget 50000` the whole 41 418-frame corpus fits, so every eligible episode is exported and
  every strategy returns the same answer. `rdp export --budget 50000` therefore proves
  reproducibility but not stratification; the numbers above are all taken at 20 000, where the
  budget actually binds. Worth stating plainly rather than presenting a no-op as a demonstration.
- Round-robin over `(source, task)` is still a **degenerate identity** on this corpus: each
  embodiment has exactly one source, and only `human_ego` has more than one task. It is unit
  tested against a synthetic two-task group instead, because the case it defends against — two
  UR5 datasets sharing an embodiment — is the one that arrives with the fifth source.

## Rejected

- **Truncating an episode to close the 8 582-frame gap at a 50 000-frame budget.** Design §6.5's
  reasoning is unchanged and the option still does not exist in the CLI.
- **Re-applying the cap during redistribution** — see §2; it converts unspendable budget into
  wasted budget.
- **Making floor/cap configurable in `config/qc.yaml`.** They are curation parameters, not QC
  thresholds, and there is no measurement behind a different value; they are named keyword
  arguments on the pure function, which is where a future `config/curation.yaml` would bind them.
- **Manufacturing a fifth embodiment to make the corpus look more balanced.** Same principle as
  ADR 014's refusal to manufacture a FAIL.
