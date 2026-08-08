"""Golden characterization tests for the source adapters.

These are not TDD tests. A wrong channel mapping produces numbers that look entirely
plausible — the only defence is to assert the mapping against real committed bytes and let the
diff scream when it moves (design §8.4). Every constant below was measured, not chosen; the
measurements live in `spikes/_out/probe_m3.txt`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import _ALOHA, _PUSHT, _RLDS, WorkspaceFactory

from rdp.domain.action_spec import ChannelRole, SpecSpace, Unit
from rdp.domain.camera import CameraEncoding, CameraMount
from rdp.domain.episode import CanonicalEpisode
from rdp.domain.errors import InvariantViolation
from rdp.domain.run import IngestionRun
from rdp.domain.stage import IngestionStage
from rdp.interfaces.wiring import Container

ALOHA_JOINTS = (
    "left.waist",
    "left.shoulder",
    "left.elbow",
    "left.forearm_roll",
    "left.wrist_angle",
    "left.wrist_rotate",
)
UR5_ACTION_CHANNELS = (
    "ee.dx",
    "ee.dy",
    "ee.dz",
    "ee.drx",
    "ee.dry",
    "ee.drz",
    "gripper",
    "flag.terminate_episode",
)


def _episodes(container: Container, source_id: str, tmp_path: Path) -> list[CanonicalEpisode]:
    """Drive the adapter directly: discovery -> fetch -> normalize, with no catalog in the way."""
    source = container.sources[source_id]
    adapter = container.adapter_for(source)
    out = []
    for index, ref in enumerate(adapter.list_episodes(source)):
        if index >= (source.max_episodes or 0):
            break
        dest = tmp_path / "staged" / source_id / ref.upstream_id
        dest.mkdir(parents=True, exist_ok=True)
        out.append(adapter.normalize(adapter.fetch(ref, source, dest), source))
    return out


def _ingest(container: Container, source_id: str) -> IngestionRun:
    source = container.sources[source_id]
    run = IngestionRun(run_id=container.new_run_id(), started_at=container.clock.now_iso())
    with container.unit_of_work() as uow:
        uow.sources.upsert(source)
        uow.runs.start(run)
        uow.commit()
    return container.ingest()(source, container.adapter_for(source), run)


@pytest.fixture
def aloha(make_workspace: WorkspaceFactory, tmp_path: Path) -> list[CanonicalEpisode]:
    return _episodes(make_workspace(blocks=(_ALOHA,)), "aloha_sim_insertion", tmp_path)


@pytest.fixture
def ur5(make_workspace: WorkspaceFactory, tmp_path: Path) -> list[CanonicalEpisode]:
    return _episodes(make_workspace(blocks=(_RLDS,)), "berkeley_ur5", tmp_path)


# -- B: aloha_sim_insertion_human ------------------------------------------------------------


def test_aloha_action_is_twelve_radian_joints_and_two_grippers(
    aloha: list[CanonicalEpisode],
) -> None:
    """The exit criterion for source B: two units inside one 14-D vector.

    Anything that flattens a spec-level `unit` would have to pick one of them and be wrong
    about the other two channels.
    """
    spec = aloha[0].meta.action_spec
    assert spec.dim == 14
    joints = [c for c in spec.channels if c.role is ChannelRole.JOINT]
    grippers = [c for c in spec.channels if c.role is ChannelRole.GRIPPER]

    assert len(joints) == 12
    assert all(c.unit is Unit.RAD and c.metric_convertible for c in joints)
    assert len(grippers) == 2
    assert not any(c.metric_convertible for c in grippers)
    # The direction of travel is published nowhere, and a plausible guess is worse than an
    # explicit absence (ADR 008).
    assert all(c.gripper is not None for c in grippers)
    assert all(
        c.gripper.convention == "normalized_unverified_direction"  # type: ignore[union-attr]
        for c in grippers
    )


def test_aloha_channels_are_split_across_two_arms(aloha: list[CanonicalEpisode]) -> None:
    spec = aloha[0].meta.action_spec
    names = [c.name for c in spec.channels]
    assert names == [
        *ALOHA_JOINTS,
        "left.gripper",
        *(n.replace("left.", "right.") for n in ALOHA_JOINTS),
        "right.gripper",
    ]
    assert [c.arm_id.value for c in spec.channels[:7]] == ["left"] * 7
    assert [c.arm_id.value for c in spec.channels[7:]] == ["right"] * 7


def test_aloha_state_mirrors_action(aloha: list[CanonicalEpisode]) -> None:
    """Identical layout is what lets `STATE_ACTION_ECHO` compare them channel-wise at all."""
    meta = aloha[0].meta
    assert meta.state_spec is not None
    assert [c.name for c in meta.state_spec.channels] == [
        c.name for c in meta.action_spec.channels
    ]
    # 12 joint angles plus 2 normalized grippers: even a single-embodiment arm is `mixed`,
    # which is why the spec-level space is derived from the channels and never declared.
    assert meta.action_spec.space is SpecSpace.MIXED
    assert meta.state_spec.space is SpecSpace.MIXED


def test_aloha_clock_is_synthesized_like_pusht(aloha: list[CanonicalEpisode]) -> None:
    """Measured: `timestamp` is bit-identical to `float32(frame_index / fps)` (ADR 005/008).

    So B's timestamps prove nothing about the real clock, and `TS_MONOTONIC` must degrade to
    SKIPPED rather than pass by construction.
    """
    provenance = aloha[0].meta.provenance
    assert provenance.timestamp_source.startswith("synthesized")
    assert aloha[0].meta.fps_nominal == 50.0


def test_aloha_keeps_its_one_unmodelled_column(aloha: list[CanonicalEpisode]) -> None:
    assert aloha[0].meta.raw_frame_columns == ("raw.next.done",)


def test_aloha_has_one_top_camera_and_a_task(aloha: list[CanonicalEpisode]) -> None:
    meta = aloha[0].meta
    assert [c.name for c in meta.cameras] == ["observation.images.top"]
    assert meta.cameras[0].mount is CameraMount.UNKNOWN
    assert meta.cameras[0].encoding is CameraEncoding.MP4_SIDECAR
    assert meta.task == "Insert the peg into the socket."
    assert meta.n_frames == 500


# -- C: berkeley_autolab_ur5 -----------------------------------------------------------------


def test_ur5_action_is_mixed_eight_dimensional_with_one_non_physical_channel(
    ur5: list[CanonicalEpisode],
) -> None:
    """The exit criterion for source C: `dim != physical_dim`.

    The terminate flag rides in the action vector because the policy emits it, but it is not a
    quantity — no QC rule may treat it as one.
    """
    spec = ur5[0].meta.action_spec
    assert [c.name for c in spec.channels] == list(UR5_ACTION_CHANNELS)
    assert spec.space is SpecSpace.MIXED
    assert spec.dim == 8
    assert spec.physical_dim == 7

    flag = spec.channels[-1]
    assert flag.role is ChannelRole.CONTROL_FLAG
    assert flag.is_physical is False


def test_ur5_pose_channels_are_deltas_in_the_base_frame(ur5: list[CanonicalEpisode]) -> None:
    spec = ur5[0].meta.action_spec
    translation, rotation = spec.channels[:3], spec.channels[3:6]
    assert all(c.is_delta and c.unit is Unit.M for c in translation)
    assert all(c.is_delta and c.unit is Unit.RAD for c in rotation)
    assert all(c.frame is not None and c.frame.value == "base" for c in spec.channels[:6])
    # Roll/pitch/yaw are named upstream; the composition order is not (ADR 009).
    assert all(c.rotation is not None for c in rotation)
    assert all(c.rotation.repr.value == "euler_rpy" for c in rotation)  # type: ignore[union-attr]


def test_ur5_state_is_fifteen_undocumented_numbers(ur5: list[CanonicalEpisode]) -> None:
    """Upstream documents `robot_state` only as a link to a web page. `unknown` is the honest
    answer; inventing joint names would be a lie the whole pipeline would then believe."""
    spec = ur5[0].meta.state_spec
    assert spec is not None
    assert spec.dim == 15
    assert spec.space is SpecSpace.UNKNOWN
    assert all(c.role is ChannelRole.UNKNOWN for c in spec.channels)
    assert not any(c.metric_convertible for c in spec.channels)


def test_ur5_trims_the_trailing_boundary_steps(ur5: list[CanonicalEpisode]) -> None:
    """Measured: `is_last` is set on the final two steps and both carry an all-zero pose.

    Keeping them would append a fabricated "the robot stopped" motion to every episode.
    """
    meta = ur5[0].meta
    rlds = meta.raw_extra["rlds"]
    assert rlds["n_steps_upstream"] == 14  # the fixture slice
    assert rlds["n_trailing_boundary_steps_trimmed"] == 2
    assert meta.n_frames == 12
    # Nothing the trimmed steps carried is lost.
    assert rlds["trimmed_step_is_terminal"] == [True, True]
    assert rlds["terminal_reward"] == 1.0
    assert any(t["op"] == "trim_trailing_steps" for t in meta.provenance.transforms)


def test_ur5_clock_is_synthesized_from_the_declared_control_rate(
    ur5: list[CanonicalEpisode],
) -> None:
    meta = ur5[0].meta
    assert meta.provenance.timestamp_source == "synthesized@5Hz"
    assert meta.fps_nominal == 5.0
    assert ur5[0].frames.t[1] == pytest.approx(0.2)


def test_ur5_refuses_to_invent_a_control_rate(
    make_workspace: WorkspaceFactory, tmp_path: Path
) -> None:
    container = make_workspace(blocks=(_RLDS.replace("    control_hz: 5\n", ""),))
    with pytest.raises(InvariantViolation, match="control_hz"):
        _episodes(container, "berkeley_ur5", tmp_path)


def test_ur5_records_pixels_as_cameras_not_video(ur5: list[CanonicalEpisode]) -> None:
    """Three inline camera streams and no decodable video file: `has_rgb` and `has_video` are
    genuinely different questions, and only C forces them apart."""
    meta = ur5[0].meta
    assert [c.name for c in meta.cameras] == ["hand_image", "image", "image_with_depth"]
    assert [c.mount for c in meta.cameras] == [
        CameraMount.WRIST,
        CameraMount.STATIC,
        CameraMount.STATIC,
    ]
    assert all(c.encoding is CameraEncoding.INLINE_FRAMES for c in meta.cameras)
    # The fixture strips the pixel payloads, so presence must be measured, not declared.
    assert not any(c.is_present for c in meta.cameras)
    assert meta.capabilities.has_rgb and meta.capabilities.has_depth
    assert not meta.capabilities.has_video


def test_ur5_keeps_the_unmodelled_step_flags_and_the_instruction(
    ur5: list[CanonicalEpisode],
) -> None:
    meta = ur5[0].meta
    assert meta.raw_frame_columns == (
        "raw.is_first",
        "raw.is_last",
        "raw.is_terminal",
        "raw.reward",
    )
    # A string cannot live in the frame table, and it is constant anyway.
    assert meta.task == "sweep the green cloth to the left side of the table"
    assert meta.capabilities.has_language


def test_ur5_records_the_dropped_embedding_as_a_lossy_transform(
    ur5: list[CanonicalEpisode],
) -> None:
    """Dropping is allowed; dropping silently is not (design §2.4, iron rule 5)."""
    dropped = [t for t in ur5[0].meta.provenance.transforms if t["op"] == "drop_channels"]
    assert dropped and "steps/observation/natural_language_embedding" in dropped[0]["columns"]
    assert not any("embedding" in name for name in ur5[0].frames.columns)


def test_ur5_boundary_is_a_policy_flag_with_no_verdict(ur5: list[CanonicalEpisode]) -> None:
    boundary = ur5[0].meta.boundary
    assert boundary.termination_source.value == "policy_flag"
    assert boundary.success_adjudicator.value == "policy"
    # The policy said "stop", never "succeeded". `None` here means unknown.
    assert boundary.success is None
    assert boundary.is_truncated is False


# -- C: identity survives a re-shard ----------------------------------------------------------


def test_ur5_identity_is_independent_of_the_shard_layout(
    make_workspace: WorkspaceFactory,
) -> None:
    """An episode's handle is its index in the *split*, so re-sharding cannot rename it."""
    container = make_workspace(blocks=(_RLDS,))
    source = container.sources["berkeley_ur5"]
    refs = list(container.adapter_for(source).list_episodes(source))
    assert [r.upstream_id for r in refs] == ["train#000000", "train#000001"]
    # `/` would collide with the staging directory layout, so it must not appear.
    assert not any("/" in r.upstream_id for r in refs)
    assert refs[0].extra["shard_name"] == "berkeley_autolab_ur5-train.tfrecord-00000-of-00001"


def test_declaring_a_new_shard_layout_marks_episodes_stale_not_new(
    make_workspace: WorkspaceFactory,
) -> None:
    """The exit criterion: after a re-shard the corpus is re-verified, never duplicated.

    The layout revision rides in `adapter_version`, so the existing staleness machinery does
    all the work — no application change, no bespoke "did the sharding move" query.
    """
    first = make_workspace(blocks=(_RLDS,), store="shared")
    before = _ingest(first, "berkeley_ur5")
    assert before.counters["discovered"] == 2
    first.catalog.close()

    second = make_workspace(blocks=(_RLDS,), store="shared", layout="train:824-shards@0.1.0")
    after = _ingest(second, "berkeley_ur5")
    assert after.counters.get("discovered", 0) == 0
    assert after.counters["stale_renormalize"] == 2
    assert _uids(second) == ["berkeley_ur5:train#000000", "berkeley_ur5:train#000001"]


def test_pusht_and_aloha_share_one_adapter(make_workspace: WorkspaceFactory) -> None:
    """The seam works only if adding B changed no adapter code — same class, two embodiments."""
    container = make_workspace(blocks=(_PUSHT, _ALOHA))
    adapters = {
        source_id: type(container.adapter_for(source)).__name__
        for source_id, source in container.sources.items()
    }
    assert adapters == {"pusht": "LeRobotAdapter", "aloha_sim_insertion": "LeRobotAdapter"}


def _uids(container: Container) -> list[str]:
    with container.unit_of_work() as uow:
        return sorted(e.uid for e in uow.episodes.list_by_stage(IngestionStage.COMMITTED))
