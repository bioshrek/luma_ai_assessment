# ADR 012 — EPIC's gyroscope and accelerometer are two streams, not one six-column table

- **Status:** Accepted — **supersedes ADR 011 §3's join rule**
- **Date:** 2026-08-09
- **Milestone:** M4 (depth: source D)
- **Affects:** design §1.1 (source D facts), §2.2 (`stream_specs`), §2.4 (store layout),
  `config/embodiments.yaml`, `infrastructure/sources/epic_adapter.py`,
  `infrastructure/sources/staging.py`, `infrastructure/storage/parquet_frame_store.py`

## Context

ADR 011 §3 decided that gyro and accelerometer would be **joined into one `imu` stream when
their `Milliseconds` arrays are identical, and the adapter would raise otherwise**. That rule was
written against the fixture, where the slices matched, and against `P01_103`, whose two files are
bit-identical in their time column.

The first unlimited run over real data raised. The measurement on `P28_101`:

| Fact                                               | Value                                                |
| -------------------------------------------------- | ---------------------------------------------------- |
| samples in `P28_101-gyro.csv` / `P28_101-accl.csv` | 141,924 / 141,924                                    |
| `Milliseconds` values that disagree                | **793**                                              |
| of those, disagreeing by more than 1 ms            | 742 (worst case ~15 ms)                              |
| where                                              | contiguous, sample 81,238 → 133,397 (≈409 s → 673 s) |
| step through that window                           | gyro 4.975 ms, accl 5.0505 ms                        |

The two files agree exactly at the head and the tail and drift apart in the middle. That is not
corruption and it is not an off-by-one: it is two sensors inside one GoPro, each with its own
sampling loop, each timestamped by its own clock. `P01_103` happens to agree, which is why the
original rule survived until a second video was ingested in full.

So the join rule was wrong in both of its branches. Joining is wrong because the two arrays are
not one array; raising is wrong because there is nothing defective about the data.

## Decision

### 1. Two streams, each on its own timeline

`human_ego` declares `streams: {gyro: [gyro.x, gyro.y, gyro.z], accel: [accel.x, accel.y,
accel.z]}` — two `SignalSpec`s, both `clock = own_timeline`, both `origin = measured`, both
`frame = sensor`, units `rad/s` and `m/s^2`. The store writes `streams/gyro.parquet` and
`streams/accel.parquet`, each carrying the `Milliseconds` column its own file shipped, rebased on
the episode origin. `_pair_imu` is deleted.

This is the same argument invariant 17 already makes about the IMU versus the frame clock,
applied one level down. Nothing about "an IMU" makes it one table; what makes a table is one
clock. Had we resampled gyro onto accl's timeline — the only way to keep one table — we would
have rotated every angular velocity onto the wrong instant by up to 15 ms, silently, in exactly
the middle of the episode, and no downstream assertion in this project would have noticed.

The fixture keeps this honest: `scripts/make_fixtures.py` retains the extra window
`[409.0, 409.5]` on `P28_101` **as committed evidence**, and the two committed CSVs have
different row counts (500 and 499). `test_the_two_imu_clocks_really_do_diverge_upstream` reads
those bytes directly and asserts the divergence, so the fact that motivated this ADR cannot
quietly stop being true.

### 2. A staging directory belongs to the adapter version that wrote it

`raw/` holds upstream bytes, which do not change — but the _layout_ of a staging directory is the
adapter's own format. Splitting the IMU changed `imu.json` from one merged object to
`{"gyro": …, "accel": …}`, and every already-staged episode then failed deep inside `normalize`
with `KeyError: 'gyro'`, permanently: the `.staged.json` marker said "done", so no re-run would
ever rewrite it.

`.staged.json` now records `adapter_version`, and `fetch` re-stages when it differs. The check is
shared by all three adapters (`infrastructure/sources/staging.py`) because it is a property of
the port, not of one source — and because the LeRobot defect in ADR 013 needed exactly the same
escape hatch to become repairable.

### 3. A `normalized/` write removes stream files the episode no longer declares

Re-normalizing an episode that used to have `streams/imu.parquet` left that file on disk beside
the new `gyro`/`accel` pair, and the next read raised
`stream tables ['accel', 'gyro', 'imu'] != declared ['accel', 'gyro']`.

`normalized/` is derived and disposable (design §2.4). A write must therefore leave the directory
**equal to the episode**, not merged with its own history, so the frame store now unlinks any
`streams/*.parquet` the episode does not declare. `frames.parquet` never had this problem because
its name is fixed; the bug arrived with the first variable-named artifact.

## Consequences

- `epic@1.1.0`. Existing EPIC stagings and normalizations are invalidated and rebuilt on the next
  run — by the mechanism in §2, without manual intervention.
- `content_hash` for EPIC episodes changes (two stream digests instead of one). Sources A–C are
  untouched: `content_hash` still folds streams in only when there are any.
- `has_imu` still means "the IMU layer was present", covering both streams. They are published as
  one file pair by one device; a capability per sensor would be inventing a distinction upstream
  does not make.

## Alternatives rejected

| Alternative                                            | Why not                                                                                                           |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| Keep joining, tolerate a small mismatch                | "Small" is 15 ms here — three video frames. A tolerance would have hidden exactly the error it was meant to catch |
| Resample one sensor onto the other's clock             | Fabricates samples (design §8.5), and the fabrication is invisible after the fact                                 |
| Keep raising, and treat these videos as unusable       | Two independently clocked sensors are the normal case, not a defect                                               |
| One `imu` table with two time columns                  | A table with two clocks is not a table; every consumer would have to know which column is which                   |
| Version the staging directory name instead of a marker | Leaks the version into paths, and orphans the old bytes instead of replacing them                                 |
| Wipe `normalized/<episode>` before every write         | Loses atomicity: a crash mid-write would leave the episode with nothing rather than with its previous good state  |
