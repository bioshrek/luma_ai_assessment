"""`EpisodeState` — the persisted stage machine and its lease (design §4, §5).

`Episode` says *what an episode is*; `EpisodeState` says *what is currently being done to it*.
They are separate rows because they change for different reasons: the episode row is the
catalog, the state row is the scheduler. Keeping the lease out of the aggregate is also what
lets a future durable-execution engine (design §10.2) own scheduling without touching the
catalog.

The stage recorded here is always the **last durably completed** stage. A worker that dies
mid-stage therefore needs no rollback: expiring its lease is enough, because the work it lost
was never recorded in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from rdp.domain.stage import IngestionStage


@dataclass(frozen=True)
class EpisodeState:
    episode_uid: str
    stage: IngestionStage
    attempt: int = 0
    last_error: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    updated_at: str = ""

    @property
    def is_leased(self) -> bool:
        return self.lease_owner is not None

    def lease_expired(self, now: str) -> bool:
        """ISO-8601 UTC timestamps from one `Clock` compare correctly as strings."""
        if self.lease_owner is None:
            return False
        return self.lease_expires_at is None or self.lease_expires_at <= now

    def lease_reclaimable(self, *, now: str, owner: str) -> bool:
        """True when nobody can still be holding this lease.

        Either the TTL has run out, or the lease names the caller's own worker slot — and a
        lease bearing our own slot id at startup can only have been left by a predecessor that
        died, because one catalog admits one writer at a time. The TTL is what will carry this
        when the slot becomes many workers (design §10.2).
        """
        return self.lease_expired(now) or self.lease_owner == owner

    def held_by(
        self, *, owner: str, expires_at: str, stage: IngestionStage, now: str
    ) -> EpisodeState:
        return replace(
            self,
            stage=stage,
            lease_owner=owner,
            lease_expires_at=expires_at,
            last_error=None,
            updated_at=now,
        )

    def released(
        self, *, stage: IngestionStage, now: str, error: str | None = None
    ) -> EpisodeState:
        return replace(
            self,
            stage=stage,
            lease_owner=None,
            lease_expires_at=None,
            last_error=error,
            updated_at=now,
        )

    def next_attempt(self) -> int:
        return self.attempt + 1

    @classmethod
    def start(
        cls,
        *,
        episode_uid: str,
        stage: IngestionStage,
        attempt: int,
        owner: str,
        expires_at: str,
        now: str,
    ) -> EpisodeState:
        return cls(
            episode_uid=episode_uid,
            stage=stage,
            attempt=attempt,
            lease_owner=owner,
            lease_expires_at=expires_at,
            updated_at=now,
        )
