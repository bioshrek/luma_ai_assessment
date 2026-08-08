---
name: unified-schema
description: Use when touching the canonical episode schema — SignalSpec, Channel, Capabilities, CameraSpec, Provenance, EpisodeBoundary, FrameTable — or the domain invariants that guard them, or the frames.parquet column-name contract, or anything requiring robotics semantics (action vs state, delta vs absolute, joint vs task space, gripper conventions, terminated vs truncated, multiple clocks, units and metric convertibility). Also use before adding a field to the schema, before deciding whether an upstream fact is lossless/lossy/droppable, and when a QC rule needs to know what it is allowed to assume.
---

# Unified Schema — semantics, invariants, and the code-first rule

> **Use code as the single source of truth.** Once `src/rdp/domain/` exists, the pydantic value
> objects there **are** the schema. This file explains _why_ each field exists and what it means;
> it does not duplicate the field list as an authority. Where this file and the code disagree,
> **the code wins — then update this file.**
>
> **`src/rdp/domain/` does not exist yet.** Until it does,
> [docs/technical_design.md](../../../docs/technical_design.md) §2 and §8.4 are the spec. Building the value objects is M1's
> first task.

## The thesis

**Unify the structure. Never unify the numbers.**

Four sources, four incompatible notions of "action": absolute task-space pixels (A), absolute
joint radians (B), end-effector deltas in meters mixed with an absolute gripper command (C), and
an episode-level `(verb, noun)` symbolic label (D). The only thing genuinely unifiable is
**channel-level metadata** — `role`, `space`, `is_delta`, `unit`, `metric_convertible`, `frame`,
`origin`, `arm_id`, `is_physical`, value range. Downstream can then process generically by role,
or bucket by space for training.

Squashing them into one fixed-width vector requires zero-padding and arbitrary rescaling. That is
irreversible destruction of the exact information this project exists to preserve. It is an
explicitly rejected pattern.

## Value objects and what each one is _for_

| Object            | Exists to answer                                           | Non-obvious point                                                                                      |
| ----------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `SignalSpec`      | What shape and meaning does this signal have?              | Shared by action and state; the only difference is `is_command`. `state` needs a spec too.             |
| `Channel`         | What does column _i_ actually mean?                        | **Semantics live here, not on the spec.** Spec-level `space`/`is_delta` are _derived summaries_.       |
| `Capabilities`    | What does this episode have, so which QC rules can run?    | **Per episode, not per source** — only D proves this necessary.                                        |
| `CameraSpec`      | What cameras, mounted where, stored how?                   | `mount` (static/wrist) changes what counts as anomalous. `encoding` splits `has_rgb` from `has_video`. |
| `Provenance`      | Which of these numbers are facts and which did we compute? | Drives **QC severity**, not just documentation.                                                        |
| `EpisodeBoundary` | Why did this trajectory end, and **who decided**?          | Four different mechanisms across four sources; also carries terminated vs truncated.                   |
| `FrameTable`      | Per-frame numeric payload                                  | Column-name contract below; frame clock only.                                                          |
| `IngestionStage`  | Where is this episode in the pipeline?                     | A state machine with legal transitions, not a string column.                                           |

### `SignalSpec.level` — the deepest hidden assumption

`level ∈ {per_frame_continuous, per_frame_discrete, episode_label, absent}`.

A/B/C actions are all "per-frame fixed-width numeric vectors", so the whole schema grew around
that shape without anyone writing it down. D's action is `(verb, noun) + [t_start, t_end]` —
**not "no action", but an action at another level of representation.**

Recording that as `space="none"` is worse than zero-filling: zero-fill at least shows up as an
anomaly in the numeric distribution, while `space="none"` is a plausible-looking lie. With
`level`, `has_action=True` and `physical_dim=0` coexist, and rule gating extends from
`required_capabilities` to `required_capabilities + required_level`.

### `Channel.origin` — trustworthiness is a channel-level property

`origin ∈ {measured, estimated, interpolated, annotated, synthesized}`.

A single D episode carries all three kinds at once: IMU is sensor-**measured**, camera pose is
COLMAP-**estimated** (671/700 videos, ~96% of frames registered), action labels are human-
**annotated**. A jump in an SfM pose is reconstruction failure, not data corruption — judging it
by `measured` standards is systematic friendly fire. Hence the automatic severity downgrade
(invariant 13).

### `dim` vs `physical_dim`

C's action vector is `dim=8, physical_dim=7`: one of those columns is `terminate_episode`, a
control flag — no unit, no physical limits, a 0→1 step in the final frame. Treating it as
physical makes `ACTION_RANGE` limits meaningless and fires `ACTION_JERK` on **every** C episode.
(Some OXE datasets encode `terminate_episode` as `float32[3]`; `berkeley_autolab_ur5` uses a
scalar — measured, see ADR 003.)

All cross-channel statistics (mean/std/travel/jerk/limits) run **only** over
`is_physical == True` channels, via the domain layer's `physical_view()`. Rules never receive the
full vector, so they cannot misuse it (invariant 6).

### `clock` — one episode can have several time axes

`clock ∈ {frame, own_timeline}`. `frames.parquet` holds **only** frame-clock signals. Everything
else goes to `normalized/<...>/streams/<stream_id>.parquet` with its own `t` column and its own
`SignalSpec` under `stream_specs` (invariant 17: a stream spec must declare `own_timeline`).
Forcing it into the frame table would mean either resampling at ingestion (forbidden) or
row-count explosion. Frame-aligned views are produced **at export time**, with the method
recorded.

D has three clocks, not two: the frame clock (camera pose), and `gyro` and `accel` — ~195–198 Hz,
per video, and **not the same clock as each other** (793 of `P28_101`'s 141,924 samples disagree,
by up to 15 ms). What defines a stream is one clock, not one device
([ADR 012](../../../docs/adr/012-epic-imu-is-two-streams.md)).

Because stream file names vary per episode, writing an episode must **delete stream files it does
not declare**. `normalized/` is derived data: a write leaves it equal to the episode, never merged
with the episode's own past.

## The `frames.parquet` column-name contract

```
t                     # seconds from 0 within the episode, float64
action.<channel.name> # e.g. action.left.gripper
state.<channel.name>
raw.<upstream field>  # unmodeled per-frame upstream data
```

- Physical column order equals the `channels` declaration order in the spec, **but consumers must
  select by name.** Positional indexing is not part of the contract.
- Every `raw.` column must be registered in `episode.json`'s `raw_frame_columns`. An unprefixed,
  unregistered column is a domain exception (invariant 12) — this is what prevents silent schema
  drift.
- Stream files follow the same rule: `t` + `<channel.name>`.
- `level == "episode_label"` ⟹ the corresponding columns **must not exist**. Not a column of
  NULLs — no column (invariant 3).
- **NaN is written as a genuine parquet NULL** (`pa.array(values, mask=isnan)`). Since nothing in
  this pipeline zero-fills, a missing float always means "upstream did not provide this", and the
  round trip is lossless. A zero pose is a _place_ — the world origin — and is indistinguishable
  from a measurement to every consumer downstream.

## The 17 domain invariants

These live in the value objects' validating constructors, **not** scattered through the pipeline.
Each has a unit test written before the implementation. Violations raise a domain exception at
construction time.

1. `IngestionStage.advance()` permits only `DISCOVERED → FETCHED → NORMALIZED → QC_DONE → COMMITTED`. Skipping or reversing requires an explicit `reset_to()` with a reason.
2. `CanonicalEpisode` is immutable once constructed. `SignalSpec.dim == len(channels) == the corresponding column width`, checked separately for action and state.
3. `level == "absent"` ⟺ `Capabilities.has_* == False`. `level == "episode_label"` ⟹ `dim == 0` and **no** corresponding columns. Per-frame levels with a missing value write NULL — **never zero-fill**.
4. A QC rule whose `required_capabilities` are unmet can only return `SKIPPED`. Enforced by the domain layer; rules cannot override.
5. A `SubsetPlan`'s total frames ≤ budget, and every entry is a **whole** episode (`frame_range == [0, n_frames)`). Export never truncates.
6. `physical_dim == len([c for c in channels if c.is_physical])`, and cross-channel statistics run only over the physical subset via `physical_view()`.
7. `is_truncated == True` ⇒ `end_reason != "success"`. When `success is None`, no downstream code may read it as `False` (enforced at the type level).
8. `role == "gripper"` ⟹ `channel.gripper` is non-null (carrying the original convention **and** the inverse parameters). `role != "gripper"` ⟹ it is `None`.
9. `Channel.space` / `Channel.is_delta` are the single source of truth. `SignalSpec.space` / `is_delta` are **derived from physical channels only** (necessarily `"mixed"` when they disagree); the constructor forbids setting them manually.
10. `channel.space` starting with `ee_rotation` / `camera_rotation` ⟺ `channel.rotation` is non-null. `repr` may be `"unknown"`, but the field must exist.
11. `has_video == True` ⟹ at least one `CameraSpec.encoding == "mp4_sidecar"`. `inline_frames` may only set `has_rgb`.
12. Unmodeled columns in `frames.parquet` carry the `raw.` prefix and are all registered in `raw_frame_columns`.
13. `origin != "measured"` ⟹ numeric rules on that channel are downgraded one severity level (FAIL → REVIEW), and `Verdict.reason` must state the basis. Applied by the domain layer; rules cannot bypass it.
14. `success_adjudicator == "none"` ⟹ `success is None`. The converse does **not** hold (C is `policy` + `None`). Success-rate aggregations must **exclude** `adjudicator == "none"` episodes from the denominator, not count them as failures.
15. Derived quantities carry their parameters: `frame_index_source` must look like `derived_from_seconds@<fps>`; a bare `derived` is illegal.
16. Channels sharing a `group` must agree on `space` / `frame` / `unit` / `origin`. Group-level constraints (e.g. all four `quat_wxyz` components present and normalizable) are validated once on the group.
17. `clock == "own_timeline"` ⟹ those channels must not appear in `frames.parquet`, and the stream file must have a monotonic `t`. `clock == "frame"` ⟹ column row count is exactly `n_frames`.

## Robotics semantics you must not get wrong

**Action vs state.** `action` is the **commanded** target; `state` is the **measured** readback.
In B they occupy the same space with the same channel semantics — which is exactly why
`STATE_ACTION_ECHO` (a rule that flags "action is just a copy of state") has a false-positive
trap there. `is_command` is what distinguishes them, and the rule's precondition ("same space,
same dim") must be **expressible in the data**, not guessed from equal column width.

**Delta vs absolute.** C's pose channels are deltas (magnitude ~1e-2) while its gripper channel
in the same vector is an absolute command. Any cross-source statistic must bucket by `is_delta`
**at channel granularity** first. `ACTION_RANGE` thresholds are keyed by
`(embodiment, channel.space)` — never `(embodiment, spec.space)`.

**Joint vs task space.** B's 12 joints are `role="joint"` (interpolatable); A's xy is
`role="end_effector"`. Interpolation method depends on role — joint angles interpolate, binary
grippers do not.

**Rotation representation.** Three radians could be axis-angle, a rotation vector, Euler XYZ, or
Euler ZYX. Without knowing which, the data cannot be integrated, compared, or converted — it is
effectively unreadable. `repr="unknown"` is legal; a **missing field** is not. Delta rotations
also need `compose` (`ΔR·R` vs `R·ΔR`); absolute rotations set it to `None`.

`repr="euler_rpy"` exists for the case C forces: upstream names the axes ("Delta change in roll,
pitch, yaw") but never states the order. `euler_xyz` would assert an order nobody gave, and
`unknown` would throw away the naming that _was_ given (ADR 009).

**Gripper conventions.** Normalize to `0=closed, 1=open`, and **preserve the inverse parameters**
(`scale`, `offset`) plus `original_convention`. Without the inverse parameters, "normalization is
reversible" is a slogan.

But normalize only what you can _verify_. C's gripper is a ternary **change** command
(`+1` close / `0` no change / `−1` open), so it is not an absolute opening at all and keeps its
native encoding with `is_delta=true`. B's grippers are continuous, overflow `[0, 1]`, and their
open/closed direction is published nowhere — so they are stored verbatim with
`convention: normalized_unverified_direction` and an identity inverse (ADR 008). A guessed
direction is worse than a recorded unknown, and min-max renormalization at ingestion is rejected
outright.

**Units are per channel, never per episode.** B's 14-D vector is 12 channels of `rad` plus 2
normalized apertures. A's action is in **pixels** and without a scene scale cannot become meters
— `unit="px"`, `metric_convertible=false`. D's SfM translations have **arbitrary scale**, also
`metric_convertible=false`.

**`terminated` vs `truncated` — the single easiest thing to get silently wrong.**
`is_terminal=True` means the trajectory genuinely ended, so value bootstrapping is cut off
($V(s_T)=0$). `is_last=True, is_terminal=False` means it was cut by a step limit; the final state
is ordinary and bootstrapping must continue ($V(s_T) \neq 0$). Collapsing them into one `done`
boolean makes every offline RL run on that export **silently wrong**. LeRobot's export of A/B may
already have lost the distinction — M0 must confirm, and if lost it becomes a known limitation.

**Never guess.** `space="unknown"`, `role="unknown"`, `repr="unknown"`, `state=NULL` +
`raw_extra` are all legal and correct. A plausible-looking wrong guess is more harmful than an
explicit absence, because absence is queryable and skippable while a guess propagates into every
statistic downstream.

## Lossless / lossy / droppable

| Must be lossless                                                       | May be lossy                                   | May be discarded                                |
| ---------------------------------------------------------------------- | ---------------------------------------------- | ----------------------------------------------- |
| Raw action/state values (unit conversion reversible, factors recorded) | Video (transcode, frame sample)                | Upstream debug fields (redundant `frame_index`) |
| Timestamps, episode boundaries, frame order                            | Image resolution                               | Inline padding / empty steps                    |
| Original task language instruction                                     | Depth maps (not processed; existence recorded) | Redundant `discount` / `done` mirrors           |
| **Per-frame reward** and the terminated/truncated distinction          | —                                              | License/readme prose (keep a URI)               |
| Embodiment/camera topology, control-flag channels                      | —                                              | `language_embedding` (recomputable from text)   |
| D's annotation-interval seconds, `Channel.origin` / `signal_origin`    | Frame indices (recomputable)                   | —                                               |

**Reward is lossless, and this was a correction.** pusht's `next.reward` is the polygon overlap
ratio between the T block and the goal region — and the T block's pose is stored **nowhere**. It
is unrecomputable once dropped. Behavior cloning does not need it; offline RL treats it as the
core supervision signal. The ingestion layer has no authority to make that call for downstream,
and the cost is one float per frame.

**Dropping channels is a lossy transform.** If `terminate_episode` is reduced from 10-D to 7-D,
record `provenance.transforms = [{"op": "drop_channels", "channels": [...], "reason": ...}]`.
Silent disappearance is not acceptable.

**Granularity matters for `raw`.** Episode-level unmapped facts → `raw_extra` (JSON).
Frame-level unmapped facts → `raw.<name>` columns in `frames.parquet`, registered in
`raw_frame_columns`. Stuffing per-frame data into an episode JSON blob makes it neither queryable
nor usable.

## Changing the schema

Schema changes are cheap **by design**, so do not agonize — but do follow the process:

1. `raw/` is authoritative and immutable; `normalized/` is derived and disposable.
2. Bump `schema_version` (and/or `adapter_version`). The unified staleness predicate —
   `recorded (content_hash, schema_version, adapter_version, ruleset_version) ≠ current` — marks
   affected rows stale in bulk.
3. The existing idempotent state machine re-runs normalize/QC in a targeted way. **There is no
   such thing as a migration script here.**
4. Grade the change: adding a nullable/optional field is **minor** (tolerant readers ignore
   unknown fields and default missing ones; no rebuild). Renaming, deleting, or re-meaning a field
   is **major** (triggers rebuild).
5. SQLite side: `PRAGMA user_version` + numbered migrations, expand → migrate → contract.
6. Write `docs/adr/NNN-*.md`: context / decision / rejected alternatives / does it trigger a
   rebuild.
7. The characterization-test golden diff is the review material — what gets reviewed is domain
   facts ("pusht's column 2 changed from X to Y"), not code.

**Do not generalize the schema "to make later changes easier."** EAV, generic key-value, and
unbounded `extensions` fields defer validation to runtime, leaving a schema in name only.
`unknown` / `raw_extra` / `raw.*` are already the escape hatch; facts get promoted to first-class
fields via an ADR once evidence accumulates. `SignalSpec.level` is exactly that path succeeding.

## References

- [docs/technical_design.md](../../../docs/technical_design.md) §2 — the full schema with every field and its justification
- [docs/technical_design.md](../../../docs/technical_design.md) §8.4 — the 17 invariants
- [docs/technical_design.md](../../../docs/technical_design.md) §8.7 — schema evolution process
- [docs/technical_design.md](../../../docs/technical_design.md) Appendix A — real data shapes that forced each field
- `source-adapters` skill — per-source shapes and the anti-corruption-layer protocol
