"""Golden characterization tests for the EPIC-KITCHENS-100 adapter — source D.

Every constant here was measured from the committed mini fixture, which is a verbatim slice of
upstream. The point of D is not the numbers but the *shapes*: an action that is a label, three
layers that come and go independently, two clocks, and a "state" that is a reconstruction.

The fixture is deliberately uneven — `P01_01` has no pose and no IMU, `P01_103` has IMU,
`P28_101` has both — because M4's whole claim is that capabilities vary between episodes of one
source and QC reacts to that instead of averaging over it.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from tests.conftest import _EPIC, EPIC_FIXTURE, WorkspaceFactory
from tests.integration.test_adapters import _episodes, _ingest

from rdp.domain.action_spec import (
    ChannelRole,
    RotationRepr,
    SignalClock,
    SignalLevel,
    SignalOrigin,
    SpecSpace,
    Unit,
)
from rdp.domain.boundary import EndReason, SuccessAdjudicator, TerminationSource
from rdp.domain.camera import CameraEncoding, CameraMount
from rdp.domain.episode import CanonicalEpisode
from rdp.domain.qc.engine import evaluate_rule
from rdp.domain.qc.rule import Verdict
from rdp.domain.qc.rules.action_range import ActionRange
from rdp.domain.qc.rules.pose_coverage import PoseCoverage
from rdp.interfaces.wiring import Container

POSE_CHANNELS = ("cam_t.x", "cam_t.y", "cam_t.z", "cam_q.w", "cam_q.x", "cam_q.y", "cam_q.z")
# The order upstream packs a pose into its 7-element array — deliberately *not* our column order.
UPSTREAM_POSE_ORDER = ("cam_q.w", "cam_q.x", "cam_q.y", "cam_q.z", "cam_t.x", "cam_t.y", "cam_t.z")
GYRO_CHANNELS = ("gyro.x", "gyro.y", "gyro.z")
ACCEL_CHANNELS = ("accel.x", "accel.y", "accel.z")


@pytest.fixture
def container(make_workspace: WorkspaceFactory) -> Container:
    return make_workspace(blocks=(_EPIC,))


@pytest.fixture
def episodes(container: Container, tmp_path: Path) -> dict[str, CanonicalEpisode]:
    return {e.meta.upstream_id: e for e in _episodes(container, "epic100", tmp_path)}


def test_episodes_are_listed_round_robin_so_a_truncated_run_still_spans_the_videos(
    container: Container,
) -> None:
    source = container.sources["epic100"]
    refs = list(container.adapter_for(source).list_episodes(source))
    assert [ref.upstream_id for ref in refs] == [
        "P01_01_0",
        "P01_103_0",
        "P28_101_0",
        "P01_01_1",
        "P01_103_1",
        "P28_101_43",
    ]


# -- the action that is not a vector ---------------------------------------------------------


def test_the_action_is_an_episode_label_with_no_columns_at_all(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    episode = episodes["P01_01_0"]
    action = episode.meta.action_spec
    assert action.level is SignalLevel.EPISODE_LABEL
    assert (action.dim, action.physical_dim, action.space) == (0, 0, SpecSpace.NONE)
    assert not [name for name in episode.frames.column_names if name.startswith("action.")]
    # The label lives where a per-episode value belongs, not in a column repeated 195 times.
    assert episode.meta.task == "open door"
    assert episode.meta.raw_extra["epic"]["narration"] == "open door"


def test_has_action_is_true_even_though_no_frame_carries_one(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    """Invariant 3 ties the capability to `level != absent`, not to "there is a column"."""
    assert episodes["P01_01_0"].meta.capabilities.has_action is True


# -- layered availability --------------------------------------------------------------------


def test_two_episodes_of_one_source_have_different_capabilities(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    def layers(uid: str) -> tuple[bool, bool, bool]:
        capabilities = episodes[uid].meta.capabilities
        return (capabilities.has_camera_pose, capabilities.has_imu, capabilities.has_state)

    assert layers("P01_01_0") == (False, False, False)
    assert layers("P01_103_0") == (False, True, False)
    assert layers("P28_101_0") == (True, True, True)


def test_a_missing_layer_is_recorded_as_a_transform_not_silently_dropped(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    transforms = episodes["P01_01_0"].meta.provenance.transforms
    assert [t["layers"] for t in transforms] == [["camera_pose", "imu"]]
    assert episodes["P28_101_0"].meta.provenance.transforms == ()


def test_without_the_pose_layer_state_is_absent_rather_than_zero_filled(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    episode = episodes["P01_103_0"]
    assert episode.meta.state_spec.level is SignalLevel.ABSENT
    assert episode.meta.state_spec.channels == ()
    assert not [name for name in episode.frames.column_names if name.startswith("state.")]


# -- the frame clock and the two frame numberings --------------------------------------------


def test_the_frame_axis_is_derived_from_seconds_at_the_official_fps(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    meta = episodes["P28_101_0"].meta
    # P28_101 is a 50 fps video; the segment is 00:00:02.39 -> 00:00:03.46.
    assert meta.fps_nominal == 50.0
    assert meta.provenance.frame_index_source == "derived_from_seconds@50"
    assert meta.provenance.timestamp_source == "annotation_seconds"
    frames = episodes["P28_101_0"].frames
    assert frames.column("raw.frame_index")[0] == int(2.39 * 50)
    assert frames.column("raw.frame_index")[-1] == int(3.46 * 50)
    assert frames.n_frames == int(3.46 * 50) - int(2.39 * 50) + 1
    assert frames.t[0] == pytest.approx(0.0)
    assert np.allclose(np.diff(frames.t), 1 / 50.0)


def test_the_annotation_csv_frame_numbering_is_preserved_but_kept_apart(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    """ADR 010: the CSV counts frames at the *extraction* fps, which for a 59.94 fps video is a
    flat 60. Storing both under one name would silently mix two numberings."""
    epic = episodes["P01_01_0"].meta.raw_extra["epic"]
    assert epic["official_fps"] == pytest.approx(59.9400599400599)
    assert epic["extraction_numbering"] == {
        "start_frame": "8",  # == int(0.14 * 60), not int(0.14 * 59.94)
        "stop_frame": "202",
        "fps": 60.0,
        "note": (
            "annotation-CSV frame indices, at the extraction fps, not the official fps; "
            "not comparable with raw.frame_index"
        ),
    }
    assert episodes["P01_01_0"].frames.column("raw.frame_index")[0] == int(0.14 * 59.9400599400599)


# -- the camera pose layer -------------------------------------------------------------------


def test_the_pose_channels_carry_estimated_origin_and_no_unit(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    spec = episodes["P28_101_0"].meta.state_spec
    assert tuple(c.name for c in spec.channels) == POSE_CHANNELS
    assert spec.space is SpecSpace.CAMERA_POSE_ABS
    for channel in spec.channels:
        assert channel.role is ChannelRole.HEAD
        # A monocular reconstruction is scale-free: "metres" would be an invention.
        assert channel.unit is None
        assert channel.metric_convertible is False
        assert channel.origin is SignalOrigin.ESTIMATED
    assert all(c.rotation is not None for c in spec.channels if c.name.startswith("cam_q"))
    assert spec.channels[3].rotation is not None
    assert spec.channels[3].rotation.repr is RotationRepr.QUAT_WXYZ
    assert episodes["P28_101_0"].meta.provenance.signal_origin["state"] is SignalOrigin.ESTIMATED


def test_the_quaternions_are_read_in_upstream_wxyz_order(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    """A pose is 7 bare numbers; reading `[qw, qx, qy, qz, tx, ty, tz]` as xyzw would still give a
    unit quaternion. So the mapping is checked element by element against the fixture bytes."""
    images = json.loads((EPIC_FIXTURE / "camera_pose/P28_101.json").read_text())["images"]
    frames = episodes["P28_101_0"].frames
    first = int(frames.column("raw.frame_index")[0])
    pose = images[f"frame_{first + 1:010d}.jpg"]  # EPIC-Fields keys are 1-based
    assert [frames.column(f"state.{name}")[0] for name in UPSTREAM_POSE_ORDER] == pose

    quaternion = np.stack([frames.column(f"state.cam_q.{a}") for a in "wxyz"])
    registered = np.isfinite(quaternion).all(axis=0)
    assert np.allclose(np.linalg.norm(quaternion[:, registered], axis=0), 1.0, atol=1e-6)


def test_unregistered_frames_are_nan_and_never_zero(
    episodes: dict[str, CanonicalEpisode], container: Container
) -> None:
    """`P28_101_43` is a real partial reconstruction: 32 of its 48 frames were registered."""
    episode = episodes["P28_101_43"]
    values = np.stack([episode.frames.column(f"state.{c}") for c in POSE_CHANNELS])
    missing = ~np.isfinite(values).all(axis=0)
    assert (episode.frames.n_frames, int(missing.sum())) == (48, 16)
    # The distinction that matters: absent, not "at the origin, not rotated".
    assert not (values[:, missing] == 0.0).any()

    # And it survives the round trip through parquet as a genuine NULL.
    path = container.frame_store.write(episode)
    table = pq.read_table(container.frame_store.resolve(path) / "frames.parquet")
    assert table.column("state.cam_t.x").null_count == int(missing.sum())
    assert np.array_equal(
        np.isnan(container.frame_store.read_frames(path).column("state.cam_t.x")), missing
    )


# -- the IMU streams -------------------------------------------------------------------------


def test_the_imu_is_two_tables_each_on_its_own_clock(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    """Upstream ships two files with two `Milliseconds` columns, and they genuinely diverge.

    On `P28_101` 793 of 141,924 samples disagree, by up to 15 ms. Merging them by row index
    would put accelerations on the gyroscope's instants — so they stay two streams (ADR 012).
    """
    episode = episodes["P01_103_0"]
    assert set(episode.meta.stream_specs) == {"gyro", "accel"}
    gyro_spec, accel_spec = episode.meta.stream_specs["gyro"], episode.meta.stream_specs["accel"]
    assert gyro_spec.clock is SignalClock.OWN_TIMELINE
    assert accel_spec.clock is SignalClock.OWN_TIMELINE
    assert tuple(c.name for c in gyro_spec.channels) == GYRO_CHANNELS
    assert tuple(c.name for c in accel_spec.channels) == ACCEL_CHANNELS
    assert [c.unit for c in gyro_spec.channels] == [Unit.RAD_PER_S] * 3
    assert [c.unit for c in accel_spec.channels] == [Unit.M_PER_S2] * 3
    assert all(c.origin is SignalOrigin.MEASURED for c in accel_spec.channels)

    gyro = episode.streams["gyro"]
    # Nearly four samples per video frame, on a grid of its own. The rate is whatever upstream
    # shipped — ~198 Hz here, ~195 Hz elsewhere — so nothing may assume a declared IMU rate.
    assert gyro.n_frames == 680
    assert episode.streams["accel"].n_frames == 680
    assert episode.frames.n_frames == 171
    assert np.median(np.diff(gyro.t)) == pytest.approx(0.0050505050505, abs=1e-9)
    # Invariant 17: no IMU column may appear in frames.parquet.
    assert not [n for n in episode.frames.column_names if "gyro" in n or "accel" in n]


def test_each_imu_stream_lands_in_its_own_parquet_file(
    episodes: dict[str, CanonicalEpisode], container: Container
) -> None:
    episode = episodes["P01_103_0"]
    path = container.frame_store.write(episode)
    directory = container.frame_store.resolve(path)
    assert (directory / "streams/gyro.parquet").exists()
    assert (directory / "streams/accel.parquet").exists()
    restored = container.frame_store.read_streams(path)
    assert set(restored) == {"gyro", "accel"}
    assert np.array_equal(restored["gyro"].t, episode.streams["gyro"].t)
    # An episode without the layer writes no stream directory at all.
    assert container.frame_store.read_streams(
        container.frame_store.write(episodes["P01_01_0"])
    ) == {}


def test_the_imu_widens_the_content_hash(episodes: dict[str, CanonicalEpisode]) -> None:
    """A layer that arrives on a later run must change the content, not slip past the key."""
    episode = episodes["P01_103_0"]
    without = CanonicalEpisode(meta=episode.meta.model_copy(update={"stream_specs": {}}),
                               frames=episode.frames)
    assert episode.content_hash() != without.content_hash()


def test_re_normalizing_with_fewer_streams_leaves_nothing_behind(
    episodes: dict[str, CanonicalEpisode], container: Container
) -> None:
    """`normalized/` is derived data: after a write it must equal the episode, not its history.

    A real M4 run hit this — the adapter stopped declaring one merged `imu` stream and started
    declaring two, and the old file was still sitting in `streams/` on the next run.
    """
    episode = episodes["P01_103_0"]
    container.frame_store.write(episode)
    fewer = CanonicalEpisode(
        meta=episode.meta.model_copy(
            update={"stream_specs": {"gyro": episode.meta.stream_specs["gyro"]}}
        ),
        frames=episode.frames,
        streams={"gyro": episode.streams["gyro"]},
    )
    path = container.frame_store.write(fewer)
    assert set(container.frame_store.read_streams(path)) == {"gyro"}


def test_the_two_imu_clocks_really_do_diverge_upstream() -> None:
    """The measurement ADR 012 rests on, kept as a golden so nobody re-merges the two streams.

    The fixture's `P28_101` CSVs carry, besides the episode windows, a slice of 409 s where the
    two sensors drift apart. Read straight from the committed files: no adapter involved, so
    this is a statement about upstream, not about us.
    """
    meta = EPIC_FIXTURE / "imu/P28/meta_data"
    gyro = _milliseconds(meta / "P28_101-gyro.csv")
    accel = _milliseconds(meta / "P28_101-accl.csv")
    assert len(gyro) != len(accel)
    shared = min(len(gyro), len(accel))
    assert not np.array_equal(gyro[:shared], accel[:shared])


def _milliseconds(path: Path) -> np.ndarray:
    with path.open(newline="") as handle:
        return np.array([float(row["Milliseconds"]) for row in csv.DictReader(handle)])


# -- boundary, provenance, licence -----------------------------------------------------------


def test_the_boundary_is_an_annotation_bound_that_nobody_adjudicated(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    boundary = episodes["P01_01_0"].meta.boundary
    assert boundary.termination_source is TerminationSource.ANNOTATOR
    assert boundary.end_reason is EndReason.ANNOTATION_BOUND
    assert boundary.is_truncated is False
    assert boundary.success_adjudicator is SuccessAdjudicator.NONE
    assert boundary.success is None


def test_the_camera_is_declared_but_its_pixels_were_never_fetched(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    (camera,) = episodes["P01_01_0"].meta.cameras
    assert (camera.mount, camera.resolution) == (CameraMount.HEAD, (1080, 1920))
    assert camera.encoding is CameraEncoding.ABSENT
    assert camera.is_present is False
    capabilities = episodes["P01_01_0"].meta.capabilities
    assert (capabilities.has_rgb, capabilities.has_video) == (False, False)


# -- how QC reacts ---------------------------------------------------------------------------


def test_frame_level_action_rules_skip_with_a_reason_of_their_own(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    episode = episodes["P01_01_0"]
    result = evaluate_rule(ActionRange(), episode.frames, episode.meta)
    assert result.verdict is Verdict.SKIPPED
    # Not "capability_unmet:has_action": the action exists, it is just not per-frame.
    assert result.reason == "action_level_is_episode_label"


def test_pose_coverage_runs_only_where_the_layer_exists(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    with_pose = episodes["P28_101_0"]
    without = episodes["P01_103_0"]
    assert evaluate_rule(PoseCoverage(), with_pose.frames, with_pose.meta).verdict is Verdict.PASS
    skipped = evaluate_rule(PoseCoverage(), without.frames, without.meta)
    assert skipped.verdict is Verdict.SKIPPED
    assert skipped.reason == "capability_unmet:has_camera_pose"


def test_a_partial_reconstruction_is_a_review_not_a_failure(
    episodes: dict[str, CanonicalEpisode],
) -> None:
    """Two episodes of one source, same rule, different conclusions — and the weaker one is
    REVIEW because a hole in a COLMAP reconstruction is a fact about the model, not the data."""
    episode = episodes["P28_101_43"]
    result = evaluate_rule(PoseCoverage(), episode.frames, episode.meta)
    assert result.verdict is Verdict.REVIEW
    assert result.metrics["pose_coverage"] == pytest.approx(32 / 48)
    assert result.metrics["n_unregistered"] == 16


def test_a_full_run_records_the_heterogeneity_and_the_licence(container: Container) -> None:
    run = _ingest(container, "epic100")
    assert run.counters["committed"] == 6

    rows = _query(
        container,
        "SELECT json_extract(capabilities_json, '$.has_imu'), "
        "json_extract(capabilities_json, '$.has_camera_pose'), count(*) "
        "FROM episodes WHERE source_id = 'epic100' GROUP BY 1, 2 ORDER BY 1, 2",
    )
    assert rows == [(0, 0, 2), (1, 0, 2), (1, 1, 2)]

    out = container.paths.store / "subset.jsonl"
    container.export()(out=out, budget_frames=10_000, include_review=True)
    licences = {json.loads(line)["license"] for line in out.read_text().splitlines()}
    assert licences == {"cc-by-nc-4.0"}


def test_an_episode_reloaded_from_the_catalog_still_declares_its_streams(
    container: Container,
) -> None:
    """Regression: `stream_specs` was on the entity but not in the `episodes` table, so a re-QC
    — which rebuilds the episode from the row, not from the adapter — met `streams/gyro.parquet`
    on disk with an empty declaration and tripped invariant 17. It cost 40 of 60 real episodes.
    """
    _ingest(container, "epic100")
    with container.unit_of_work() as uow:
        with_imu = uow.episodes.get("epic100:P01_103_0")
        without_imu = uow.episodes.get("epic100:P01_01_0")
    assert with_imu is not None and with_imu.meta is not None
    assert set(with_imu.meta.stream_specs) == {"gyro", "accel"}
    assert without_imu is not None and without_imu.meta is not None
    assert without_imu.meta.stream_specs == {}


def _query(container: Container, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(container.paths.catalog)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()
