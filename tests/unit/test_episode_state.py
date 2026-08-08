"""`EpisodeState` is the scheduler's row: a stage, an attempt count, and a lease."""

from __future__ import annotations

from rdp.domain.episode_state import EpisodeState
from rdp.domain.stage import IngestionStage

OWNER = "rdp"
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:10:00Z"
T2 = "2026-01-01T00:20:00Z"


def leased(expires_at: str | None = T1, owner: str | None = OWNER) -> EpisodeState:
    return EpisodeState(
        episode_uid="fake:e0",
        stage=IngestionStage.FETCHED,
        attempt=1,
        lease_owner=owner,
        lease_expires_at=expires_at,
        updated_at=T0,
    )


def test_a_lease_within_its_ttl_has_not_expired() -> None:
    assert leased().lease_expired(T0) is False


def test_a_lease_past_its_ttl_has_expired() -> None:
    assert leased().lease_expired(T2) is True


def test_an_unleased_state_never_expires() -> None:
    """`expired` describes a lease, not a row; without one there is nothing to reclaim."""
    assert leased(owner=None, expires_at=None).lease_expired(T2) is False


def test_a_lease_with_no_deadline_is_treated_as_expired() -> None:
    """A lease that cannot expire could wedge an episode forever; fail towards recovery."""
    assert leased(expires_at=None).lease_expired(T0) is True


def test_our_own_slots_lease_is_reclaimable_even_before_the_ttl() -> None:
    """One catalog admits one writer, so finding our own slot's lease at startup means it died."""
    assert leased().lease_reclaimable(now=T0, owner=OWNER) is True


def test_another_owners_live_lease_is_not_reclaimable() -> None:
    assert leased(owner="other").lease_reclaimable(now=T0, owner=OWNER) is False


def test_another_owners_expired_lease_is_reclaimable() -> None:
    assert leased(owner="other").lease_reclaimable(now=T2, owner=OWNER) is True


def test_releasing_clears_the_lease_and_records_the_stage_reached() -> None:
    released = leased().released(stage=IngestionStage.NORMALIZED, now=T1)
    assert released.lease_owner is None
    assert released.lease_expires_at is None
    assert released.stage is IngestionStage.NORMALIZED
    assert released.is_leased is False


def test_releasing_can_carry_the_error_that_ended_the_attempt() -> None:
    released = leased().released(stage=IngestionStage.FAILED, now=T1, error="boom")
    assert released.last_error == "boom"


def test_taking_a_lease_clears_the_previous_attempts_error() -> None:
    """A retry starts clean, or a stale message would outlive the failure it described."""
    state = leased().released(stage=IngestionStage.FETCHED, now=T1, error="boom")
    retaken = state.held_by(
        owner=OWNER, expires_at=T2, stage=IngestionStage.FETCHED, now=T1
    )
    assert retaken.last_error is None
    assert retaken.is_leased is True


def test_attempts_count_up_so_a_poison_episode_is_visible() -> None:
    assert leased().next_attempt() == 2


def test_start_records_the_stage_already_completed_not_the_one_beginning() -> None:
    """The invariant the whole resume story rests on: no stage is recorded before it is done."""
    state = EpisodeState.start(
        episode_uid="fake:e0",
        stage=IngestionStage.FETCHED,
        attempt=1,
        owner=OWNER,
        expires_at=T1,
        now=T0,
    )
    assert state.stage is IngestionStage.FETCHED
    assert state.is_leased is True
    assert state.updated_at == T0
