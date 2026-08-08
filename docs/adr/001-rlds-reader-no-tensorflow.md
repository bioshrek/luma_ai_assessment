# ADR 001 — Read RLDS/OXE by direct TFRecord parsing, not TensorFlow Datasets

- **Status:** Accepted
- **Date:** 2026-08-08
- **Milestone:** M0 (feasibility spike)
- **Affects:** design §1 (source C), §A.C; `infrastructure/sources/rlds` (M3)

## Context

The implementation plan set a decision point for source C: "if TFDS/TensorFlow does not
install cleanly on the target Python, fall back to either direct TFRecord parsing or
replacing C with an HDF5 source."

Measured on the pinned interpreter (CPython 3.12.11, macOS arm64, `uv sync --group spike`):

- `tensorflow==2.19.1` installs, and constrains `protobuf<6`.
- `tensorflow-datasets` imports `tensorflow_metadata`, whose `*_pb2` modules are generated
  with protobuf **gencode 6.31.1** while the resolved runtime is **5.29.6**.
- Result: `google.protobuf.runtime_version.VersionError` at import time, before any data is
  touched. `import tensorflow_datasets` itself does not raise (the protos are lazy-imported),
  so the module appears importable but every real API call fails:
  `module 'tensorflow_datasets' has no attribute 'features'`.
- Pinning `tensorflow-metadata` backwards does not help: `uv run` re-syncs the environment
  from the lockfile and reverts any manual pin, and the constraint set has no solution that
  satisfies both `tensorflow<6-protobuf` and the newer generated protos.

Per the plan, the spike budget for fighting this is deliberately small.

## Decision

**Take the pre-authorised fallback: parse the RLDS shards directly. Source C stays
`berkeley_autolab_ur5`; TensorFlow leaves the dependency set entirely.**

Two facts make this cheap:

1. A TFRecord is a trivially framed stream: `[uint64 length][uint32 crc][payload][uint32 crc]`.
2. Each payload is a `tf.train.Example` protobuf whose wire format needs only three message
   types (`Features` map, `BytesList`/`FloatList`/`Int64List`). `spikes/probe_rlds.py`
   decodes it in ~80 lines of stdlib code, with no protobuf runtime at all.

Per-channel semantics (dtype, shape, and the human-written `description` strings) come from
`features.json`, fetched over plain HTTPS from the same public GCS bucket — so no `gs://`
filesystem plugin is required either.

Verified end to end: episode 0 of
`berkeley_autolab_ur5-train.tfrecord-00000-of-00412` decodes to 71 steps with all 14
flattened feature keys and plausible values (see `spikes/_out/probe_rlds.txt`).

## Consequences

Good:

- The production pipeline never depends on TensorFlow (~600 MB, and the direct cause of this
  failure). Install time, CI time, and dependency risk all drop.
- Byte-range reads become possible: the spike pulls the first 150 MB of a 178 MB shard rather
  than the full 76 GB dataset. The M3 adapter can use the same trick.
- We own the reader, so the `(split, shard, index-in-shard)` identity scheme required by
  design §5 is expressible directly.

Bad / accepted risks:

- We must decode `uint8` image payloads ourselves if `--with-video` is ever used for C. Not
  needed by default: C's images are `inline_frames` and are not written out.
- CRC32C checks are skipped. A corrupted shard would surface as a protobuf parse error rather
  than a checksum error. Acceptable: the shard is re-downloadable and `content_hash` is
  computed over normalized episode bytes anyway.
- A future RLDS change to `file_format` (e.g. ArrayRecord) would need new reader code. The
  format is recorded in `dataset_info.json` (`fileFormat: "tfrecord"`) and the adapter must
  assert it rather than assume it.

## Rejected alternatives

| Alternative                                 | Why not                                                                                       |
| ------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Downgrade Python to 3.11 to appease TFDS    | The pin is `>=3.12,<3.13` for the whole project; a source adapter must not dictate it         |
| Vendor/patch `tensorflow_metadata` protos   | Patching a transitive dependency's generated code is unmaintainable                           |
| Replace C with an HDF5 source (robomimic)   | C is the only source with delta actions + control flags in one vector — design §1 rests on it |
| Pre-convert C offline and commit the result | Hides the adapter seam the project is meant to demonstrate                                    |
