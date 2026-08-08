# ADR 014 — Thresholds from measured distributions: three statistics the design specified wrong, and a corpus that does not fail

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M5 (full QC ruleset with data-driven thresholds)
- **Affects:** design §3 (QC rules), `config/qc.yaml`, `domain/qc/rules/action_jerk.py`,
  `domain/qc/rules/static_episode.py`, `domain/qc/rules/_support.py`,
  `application/build_stats.py`, `interfaces/presenters/stats_md.py`

## Context

M5's instruction is that thresholds are **measured, not guessed**. Carrying that out against the
202-episode corpus falsified three of design §3's criteria — not their intent, but the specific
statistic each names. Each was written before any data had been read, which is exactly what M5
exists to correct; recording the correction is worth more than the corrected number.

`rdp stats` is the mechanism: it reads `qc_results.metrics_json` back out with SQL alone and
prints the distribution of every metric per `(source, rule)`. No rule is re-run to produce it, so
the evidence behind a threshold is always the evidence the verdicts were actually made from.
Grouping is per source deliberately — a jerk ratio is comparable across embodiments and a travel
fraction in pixels is not.

## Decision

### 1. `ACTION_JERK` measures against p99, not p99.9

Design §3: _"single-channel `abs(delta_a)` exceeds 5× that channel's p99.9"_. Within one episode
that is arithmetically unreachable. Episodes here are 69–500 steps, so p99.9 falls between the
top two order statistics and `numpy` interpolates: `max / p99.9` is bounded near 1 by
construction. **Measured corpus-wide, it never exceeds 1.57** — a rule with a 5× threshold on
that statistic can never fire, on any data, which is worse than a wrong threshold because it
looks like a passing rule.

The rule therefore uses `max |step| / p99 |step|` per channel per episode. Measured worst case:

| source                | max jerk_ratio |
| --------------------- | -------------- |
| `pusht`               | 3.62           |
| `aloha_sim_insertion` | 3.50           |
| `berkeley_ur5`        | 1.53           |

`max_ratio: 8.0` sits well above the corpus maximum, so a hit is an outlier by the data's own
standard rather than by ours. A second condition survives from the design and does real work: the
jump must also be **isolated** — at least `min_isolation` times the median step of its own
neighbourhood — which is what separates packet loss from hard but genuine acceleration.

`min_steps: 20` guards the statistic itself: below that, p99 is not an order statistic.

### 2. `STATIC_EPISODE`'s travel test measures positional channels only

Design §3 asks for _"total travel over action physical channels below threshold"_ without saying
in what units. Travel in pixels, radians and unitless SfM coordinates cannot share a threshold,
so the rule expresses travel as a fraction of **the channel's own declared range**. That much was
in place before the corpus ran. The corpus then failed 5 `berkeley_ur5` episodes and put 12
`epic100` episodes into REVIEW, and **all 17 were false positives with the same cause**: the only
channels that happened to declare limits were the ones whose range is not a distance.

- C's arm channels are end-effector deltas with no declared limits; its **gripper** declares
  `[-1, 1]`. So "the busiest channel covered 0.0000 of its declared range" was a statement about
  a gripper that never actuated — on episodes whose arm demonstrably moved on every step
  (`still_fraction` 0.0, 6–7 channels examined by `ACTION_JERK`). It also duplicated
  `GRIPPER_STUCK`, which had already flagged the same 5 episodes, at a higher severity.
- D's only bounded channel is the camera **quaternion**, `[-1, 1]` per component. The fraction of
  that interval a quaternion component sweeps is not a distance in any frame.

A channel is therefore eligible for the travel test only if its space is neither a rotation
(`ROTATION_SPACES`) nor `GRIPPER`. After the restriction, `pusht` is the only source in this
corpus with an eligible bounded channel; its measured `travel_fraction` is **0.588–3.25** (mean
1.52) over 80 episodes, and `min_travel_fraction: 0.05` sits an order of magnitude below the
quietest real episode. The other three sources declare no bounded positional channel, so the
test is **not attempted** rather than silently passed — the same degradation discipline the rest
of the ruleset uses.

Two smaller rules fell out of the same pass, both found by tests before the corpus confirmed
them:

- **A hole is not stillness.** `np.nansum` over an all-NaN channel returns 0.0, so an episode
  whose pose the reconstruction never registered read as "travelled zero" and FAILed. Steps with
  no finite value are excluded; a channel that is nothing but holes reports no travel rather
  than no movement.
- **The one ungated rule applies invariant 13 itself.** The engine's severity downgrade keys off
  `required_levels`, which `STATIC_EPISODE` leaves empty so that it can run on any episode. It
  therefore checks the origins of what it read: a motion complaint about channels that are all
  `estimated` is a REVIEW, while "this episode has 4 frames" is a fact about the episode and
  stays a FAIL. `derived_basis()` in `_support.py` is that check, and the reason string names the
  basis so the REVIEW is explainable.

### 3. The corpus-wide FAIL rate is 0%, and that is the honest answer

M5's exit criteria ask for a FAIL rate that is _"not 0%, not >30%"_. After the 17 false positives
above were fixed, the 202-episode corpus is:

| verdict | episodes | which                                                                    |
| ------- | -------- | ------------------------------------------------------------------------ |
| PASS    | 196      |                                                                          |
| REVIEW  | 6        | 5 × `GRIPPER_STUCK` on `berkeley_ur5`, 1 × `SEGMENT_BOUNDS` on `epic100` |
| FAIL    | 0        |                                                                          |

Every non-PASS was inspected individually and every one is a real finding:

- `berkeley_ur5:train#{000000,000001,000003,000008,000011}` — the gripper's cumulative delta
  command never leaves 0 across 69–99 frames. In a pick-and-place dataset that is either a
  failed grasp or a dropped signal; it is exactly the case worth a human's time, and REVIEW
  (not FAIL) is correct because C's gripper direction is unverifiable (ADR 008).
- `epic100:P01_01_10` — a 1.23 s segment whose predecessor overlaps it by 2.97 s, i.e. an
  overlap fraction of 2.41. The annotation is real; whether the pair is usable is a judgement.

We are **not** tightening a threshold below the measured distribution to manufacture a FAIL. The
project's own rule for this milestone is "loosen from measured distributions, never from
intuition", and inventing a failure to satisfy a rate would be the same mistake in the other
direction. Four curated public datasets containing zero hard failures is a plausible and
verifiable outcome; the ruleset's ability to fail is proven by the unit suite, where every rule
has at least one case that fires and one that stays silent, and by the fact that the same
ruleset produced 17 FAIL/REVIEWs an hour earlier and each was traced to a cause.

Three rules are `SKIPPED` corpus-wide (`TS_MONOTONIC`, `FPS_DRIFT`, `VIDEO_FRAME_MISMATCH`).
That is also reported rather than hidden: no source in this corpus publishes a real clock, and
none is configured to download video. A `SKIPPED` with a reason is a conclusion.

## Consequences

- `ruleset_version` is `2.1`. A threshold or rule-logic change rewinds every committed episode to
  `NORMALIZED` and re-runs QC only — no re-fetch, no re-normalize. Re-QC of all 202 episodes is
  seconds, which is what makes "measure, adjust, re-measure" a practical loop at all.
- Verdicts produced under `2.0` are superseded but retained: `qc_results` is history (ADR 013).
- `rdp stats` is now the first thing to run after any threshold edit, and `reports/qc_stats.md`
  is committed so the before/after is reviewable.
- Design §3's table is amended in place for the two statistics above, with this ADR cited.

## Alternatives rejected

| Alternative                                                              | Why not                                                                                                                    |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Keep p99.9 and lower the multiplier until something fires                | Fits the threshold to one corpus rather than to the phenomenon; the statistic is still degenerate at these episode lengths |
| Compute p99.9 across the whole source instead of within an episode       | A per-episode defect is then hidden by the corpus's own variance, and the rule stops being a pure function of one episode  |
| Let `STATIC_EPISODE` keep judging grippers                               | Duplicates `GRIPPER_STUCK` at a higher severity, and calls an episode with a moving arm motionless                         |
| Give each source its own `min_travel_fraction`                           | The fraction-of-declared-range form exists precisely so one threshold works; per-source constants would hide that it broke |
| Normalize the quaternion into an angle so D gets a travel number         | An SfM rotation is `estimated`; a distance derived from it would be a claim the data cannot support (invariant 13's point) |
| Tighten a threshold until the corpus FAILs, to satisfy the exit criteria | Manufactures a defect. The criterion is a smoke test for calibration, not a quota                                          |
