# ADR 010 — EPIC's two frame numberings: store the official-fps one, preserve the CSV's

- **Status:** Accepted
- **Date:** 2026-08-09
- **Milestone:** M4 (depth: source D)
- **Affects:** design §2.2f (`frame_index_source`), §2.4 (`frames.parquet` column contract),
  Appendix A.D, `infrastructure/sources/epic_adapter.py`, `config/sources.yaml`,
  `scripts/make_fixtures.py`

## Context

EPIC-KITCHENS-100 annotates an action segment four times over: as two `HH:MM:SS.ff` timestamps
and as two frame indices. ADR 004 measured what nobody documents — the frame indices are counted
on the rate the released JPEG dumps were **extracted** at, which is 50 for 50 fps videos and a
flat **60** for everything else, including the 59.94 fps EPIC-55 era videos. Reproduced on all
67,217 train segments.

M4 adds a second consumer of frame numbers: the EPIC-Fields camera poses. Its keys are
`frame_0000000123.jpg`, and they are **1-based** indices at the video's **official** fps.

So one segment of `P01_01` (official 59.9400599… fps) has two different, both-correct frame
ranges — `8..202` from the CSV, `8..201` on the official clock — and they drift further apart the
later in the video the segment sits. Merging them is a silent, plausible-looking corruption: the
numbers are close enough that nothing ever crashes.

## Decision

### 1. `frames.parquet` is numbered on the official fps

`t` is derived from the annotator's seconds at the official rate, and `raw.frame_index` carries
the resulting absolute, 0-based index into the video:

```
first = int(start_s * official_fps)
last  = max(first, int(stop_s * official_fps))
t     = (arange(first, last + 1) - first) / official_fps
```

The official fps wins because it is the only clock the **pose layer can be joined on**. A frame
table indexed on the extraction fps would need a resampling step to attach a pose — that is,
fabricated data — for exactly the sources where the pose is the only state we have.

`t` starts at 0.0 for every episode, like every other source. The segment's offset in the video
is not lost: it is `raw.frame_index[0] / official_fps`, and it is also what the IMU stream's `t`
is measured against, so the two clocks share an origin without either being resampled.

### 2. The CSV's numbering is preserved verbatim, under a different name

`raw_extra.epic.extraction_numbering` carries `start_frame`, `stop_frame`, the `fps` they were
counted at, and a note stating they are not comparable with `raw.frame_index`. Anyone who wants
to fetch the official RGB frame dumps needs those exact integers; anyone who joins them against
our frame table without reading the note is wrong, and the note is the only thing that can tell
them so.

This is the general rule of design §8.5 applied: an upstream fact the canonical schema cannot
express is **preserved next to it, named for what it is**, never coerced into the field it
resembles.

### 3. `frame_index_source` states the rate

`derived_from_seconds@59.9401` — not `derived`. Invariant 15 already rejects a bare `derived`
(`Provenance._check`), because a derivation whose parameter is unrecorded cannot be checked or
undone. This is the first source that actually exercises that rule.

### 4. The extraction-fps rule stays in config, not in code

`frame_extraction_fps: {when_official_fps_is_50: 50, otherwise: 60}` is an observed property of
one upstream release, not a law. It lives in `config/sources.yaml` where a future release can
change it without a code change, and the adapter reads it only to record it.

## Consequences

- A pose is attached by name (`frame_{i+1:010d}.jpg`), converting 0-based to 1-based exactly
  once, in one function (`_pose_key`).
- Episode length differs by up to one frame from `stop_frame - start_frame + 1`. That is correct,
  not an off-by-one: they are lengths measured on two different clocks.
- Golden test: `raw.frame_index[0] == int(0.14 * 59.9400599…) == 8` while the CSV says `8` and
  the _stop_ indices differ (`201` vs `202`) — the drift is asserted, not tolerated.

## Alternatives rejected

| Alternative                                    | Why not                                                                                    |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Use the CSV's `start_frame`/`stop_frame` as-is | Cannot be joined to the pose layer without resampling; strands the only state source D has |
| Store both as two columns in `frames.parquet`  | Two columns of near-identical integers is an invitation to use the wrong one               |
| Round the official fps to 60                   | Invents a 0.1% clock error that compounds to ~1.6 s at the end of a 27-minute video        |
| Drop the CSV numbering                         | It is the key to the official RGB release; dropping it makes the frames unfetchable        |
