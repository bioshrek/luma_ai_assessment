# ADR 000 — Final source selection for the four-source corpus

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M0 (feasibility spike)
- **Affects:** design §1, `config/sources.yaml`

## Context

The design proposed four sources with a pre-authorised substitution for C if TFDS proved
unusable. M0's job was to confirm reachability and set data-driven `max_episodes` caps.

## Decision

**All four proposed sources are confirmed and kept. No substitution.** The only change is
_how_ C is read, which is ADR 001 and does not affect which dataset is used.

| #   | `source_id`           | Dataset                             | Reachable | Evidence                        |
| --- | --------------------- | ----------------------------------- | --------- | ------------------------------- |
| A   | `pusht`               | `lerobot/pusht`                     | yes       | `spikes/_out/probe_lerobot.txt` |
| B   | `aloha_sim_insertion` | `lerobot/aloha_sim_insertion_human` | yes       | `spikes/_out/probe_lerobot.txt` |
| C   | `berkeley_ur5`        | OXE `berkeley_autolab_ur5` 0.1.0    | yes       | `spikes/_out/probe_rlds.txt`    |
| D   | `epic100`             | EPIC-KITCHENS-100 (3 layers)        | yes       | `spikes/_out/probe_epic.txt`    |

### Measured scale, and the `max_episodes` caps that follow

Frames per episode are **measured**, not assumed:

| Source | Upstream episodes | Measured frames/episode | Cap | Expected frames |
| ------ | ----------------- | ----------------------- | --- | --------------- |
| A      | 206               | 124.5 avg (@10 Hz)      | 80  | ~10.0k          |
| B      | 50                | exactly 500 (@50 Hz)    | 50  | 25.0k           |
| C      | 896 train         | ~71 steps (episode 0)   | 80  | ~5.7k           |
| D      | 67,217 segments   | ~3.2 s ⇒ ~160 (@50 fps) | 60  | ~9.6k           |

Total ≈ **50k frames**, below design §1's stated 80k–120k target.

**This is deliberate and the design target is revised down.** The bottleneck is real: B has
only 50 episodes upstream (all taken), and C's episodes are short. Reaching 100k would mean
either inflating A (which would make the corpus 50% one trivial 2-D source) or taking
hundreds of D segments (which adds bulk without adding the representational diversity D
exists to provide). Frame count is not a project goal; **cross-source semantic heterogeneity
is**, and it is already fully covered at 50k.

### D's layer availability, verified independently

| Layer         | Source                                 | Verified                                          |
| ------------- | -------------------------------------- | ------------------------------------------------- |
| `annotations` | GitHub `epic-kitchens-100-annotations` | 700 videos, 67,217 train segments, 8.4 MB CSV     |
| `camera_pose` | EPIC-Fields                            | `P28_101`: 35,823 registered frames, 99.83% cover |
| `imu`         | `data.bris.ac.uk` GoPro metadata       | `P01_101`: present; `P01_01`: **404**             |

The `P01_101` present / `P01_01` absent split is not a hazard to work around — it is exactly
the uneven-capabilities-within-a-source property that design §1.1 point 5 requires, and both
videos are pinned in `config/sources.yaml` so the property is reproducible.

The EPIC-Fields bulk release is a single 7.5 GB tarball (Dropbox, or
`thor.robots.ox.ac.uk/epic-fields/json-format.tar.gz`); there is no per-video HTTP endpoint.
M4 must therefore treat it as a one-time local acquisition registered in
`config/sources.local.yaml`. The spike used the real per-video JSON published in the
`epic-fields-code` repo (`example_data/P28_101.json`, 18.1 MB), which is format-identical.

## Consequences

- `config/sources.yaml` is drafted with the caps above and committed.
- Licences differ per source and must ride on the `sources` row and propagate to export
  lines: `apache-2.0` (A), `mit` (B), `cc-by-4.0` (C), **`cc-by-nc-4.0` (D, non-commercial)**.
- Design §1's "80k–120k frames total" is superseded by "~50k frames"; §1 is edited accordingly.
- Three follow-on ADRs record what the data disagreed with the design about: 001 (how to read
  C), 002 (LeRobot v3.0 layout and lost termination semantics), 003 (C's action vector is 8-D,
  not 10-D), 004 (D's frame-index fps and IMU units).
