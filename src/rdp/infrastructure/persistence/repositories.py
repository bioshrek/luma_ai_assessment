"""SQLite-backed repositories. Hand-written SQL: we need exact control of the transactions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from rdp.domain.action_spec import SignalSpec
from rdp.domain.boundary import EpisodeBoundary
from rdp.domain.camera import CameraSpec
from rdp.domain.capabilities import Capabilities
from rdp.domain.episode import Episode, EpisodeMeta
from rdp.domain.episode_state import EpisodeState
from rdp.domain.provenance import Provenance
from rdp.domain.qc.rule import EpisodeVerdict, RuleResult, Verdict
from rdp.domain.run import IngestionRun
from rdp.domain.source import Source
from rdp.domain.stage import IngestionStage
from rdp.domain.stats import ChannelStats

_EPISODE_COLUMNS = (
    "episode_uid, source_id, upstream_id, content_hash, status, embodiment, task, "
    "action_level, action_space, action_dim, physical_dim, state_space, state_dim, n_frames, "
    "fps_nominal, fps_effective, duration_s, capabilities_json, action_spec_json, "
    "state_spec_json, camera_json, provenance_json, boundary_json, raw_extra_json, "
    "raw_columns_json, stats_json, frames_path, qc_verdict, last_error, schema_version, "
    "adapter_version, ruleset_version, first_seen_run, last_update_run, updated_at"
)

# `SignalSpec` exposes dim / physical_dim / space / is_delta as computed fields: they are stored
# so the catalog is queryable, but they must be dropped before revalidation (invariant 9 says
# they are derived, and the model forbids setting them).
_DERIVED_SPEC_FIELDS = ("dim", "physical_dim", "space", "is_delta")


def _spec_from_json(payload: str) -> SignalSpec:
    data = json.loads(payload)
    for name in _DERIVED_SPEC_FIELDS:
        data.pop(name, None)
    return SignalSpec.model_validate(data)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SqliteEpisodeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, uid: str) -> Episode | None:
        row = self._conn.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE episode_uid = ?", (uid,)
        ).fetchone()
        return _row_to_episode(row) if row is not None else None

    def upsert(self, episode: Episode) -> None:
        """Idempotent by primary key: replaying a stage advance is harmless."""
        values = _episode_to_row(episode)
        placeholders = ", ".join("?" for _ in values)
        names = _EPISODE_COLUMNS.split(", ")
        assignments = ", ".join(f"{name} = excluded.{name}" for name in names)
        self._conn.execute(
            f"INSERT INTO episodes ({_EPISODE_COLUMNS}) VALUES ({placeholders}) "
            f"ON CONFLICT(episode_uid) DO UPDATE SET {assignments}",
            values,
        )

    def list_by_stage(self, stage: IngestionStage) -> list[Episode]:
        rows = self._conn.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE status = ? ORDER BY episode_uid",
            (stage.value,),
        ).fetchall()
        return [_row_to_episode(row) for row in rows]

    def list_exportable(
        self, *, verdicts: Sequence[str], embodiment: str | None = None
    ) -> list[Episode]:
        marks = ", ".join("?" for _ in verdicts)
        sql = (
            f"SELECT {_EPISODE_COLUMNS} FROM episodes "
            f"WHERE status = ? AND qc_verdict IN ({marks})"
        )
        params: list[Any] = [IngestionStage.COMMITTED.value, *verdicts]
        if embodiment is not None:
            sql += " AND embodiment = ?"
            params.append(embodiment)
        sql += " ORDER BY episode_uid"
        return [_row_to_episode(row) for row in self._conn.execute(sql, params).fetchall()]

    def counts_by_stage(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM episodes GROUP BY status ORDER BY status"
        ).fetchall()
        return {row[0]: row[1] for row in rows}


_STATE_COLUMNS = (
    "episode_uid, stage, attempt, last_error, lease_owner, lease_expires_at, updated_at"
)


class SqliteEpisodeStateRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, uid: str) -> EpisodeState | None:
        row = self._conn.execute(
            f"SELECT {_STATE_COLUMNS} FROM episode_state WHERE episode_uid = ?", (uid,)
        ).fetchone()
        return _row_to_state(row) if row is not None else None

    def upsert(self, state: EpisodeState) -> None:
        self._conn.execute(
            f"INSERT INTO episode_state ({_STATE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(episode_uid) DO UPDATE SET stage = excluded.stage, "
            "attempt = excluded.attempt, last_error = excluded.last_error, "
            "lease_owner = excluded.lease_owner, lease_expires_at = excluded.lease_expires_at, "
            "updated_at = excluded.updated_at",
            (
                state.episode_uid,
                state.stage.value,
                state.attempt,
                state.last_error,
                state.lease_owner,
                state.lease_expires_at,
                state.updated_at,
            ),
        )

    def list_leased(self) -> list[EpisodeState]:
        rows = self._conn.execute(
            f"SELECT {_STATE_COLUMNS} FROM episode_state "
            "WHERE lease_owner IS NOT NULL ORDER BY episode_uid"
        ).fetchall()
        return [_row_to_state(row) for row in rows]


class SqliteQCResultRepository:
    def __init__(self, conn: sqlite3.Connection, ruleset_version: str, now: str) -> None:
        self._conn = conn
        self._ruleset_version = ruleset_version
        self._now = now

    def record(self, episode_uid: str, run_id: str, results: Sequence[RuleResult]) -> None:
        self._conn.executemany(
            "INSERT INTO qc_results "
            "(episode_uid, rule_id, verdict, metrics_json, reason, run_id, ruleset_version, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(episode_uid, rule_id, run_id) DO UPDATE SET "
            "verdict = excluded.verdict, metrics_json = excluded.metrics_json, "
            "reason = excluded.reason, created_at = excluded.created_at",
            [
                (
                    episode_uid,
                    result.rule_id,
                    result.verdict.value,
                    _dumps(dict(result.metrics)),
                    result.reason,
                    run_id,
                    self._ruleset_version,
                    self._now,
                )
                for result in results
            ],
        )

    def rules_hit(self, episode_uid: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT rule_id FROM qc_results "
            "WHERE episode_uid = ? AND verdict IN (?, ?) ORDER BY rule_id",
            (episode_uid, Verdict.FAIL.value, Verdict.REVIEW.value),
        ).fetchall()
        return [row[0] for row in rows]

    def verdict_counts(self, run_id: str | None = None) -> dict[str, dict[str, int]]:
        sql = "SELECT rule_id, verdict, COUNT(*) FROM qc_results"
        params: tuple[Any, ...] = ()
        if run_id:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        sql += " GROUP BY rule_id, verdict ORDER BY rule_id, verdict"
        out: dict[str, dict[str, int]] = {}
        for rule_id, verdict, count in self._conn.execute(sql, params).fetchall():
            out.setdefault(rule_id, {})[verdict] = count
        return out


_RUN_COLUMNS = "run_id, started_at, finished_at, status, args_json, stats_json, resumed_from"


class SqliteRunRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, run: IngestionRun) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, started_at, status, args_json, stats_json, resumed_from) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
            (
                run.run_id,
                run.started_at,
                run.status,
                _dumps(run.args),
                _dumps(run.stats()),
                run.resumed_from,
            ),
        )

    def finish(self, run: IngestionRun) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, stats_json = ? WHERE run_id = ?",
            (run.finished_at, run.status, _dumps(run.stats()), run.run_id),
        )

    def get(self, run_id: str) -> Mapping[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    def latest(self) -> Mapping[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    def unfinished(self) -> list[Mapping[str, Any]]:
        rows = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE finished_at IS NULL ORDER BY started_at, rowid"
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def mark_interrupted(self, run_id: str, now: str) -> None:
        """A run that never wrote `finished_at` did not stop — it died. Record that, once."""
        self._conn.execute(
            "UPDATE runs SET status = 'INTERRUPTED', finished_at = ? "
            "WHERE run_id = ? AND finished_at IS NULL",
            (now, run_id),
        )


class SqliteExportRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        *,
        export_id: str,
        run_id: str,
        strategy: str,
        budget_frames: int,
        n_episodes: int,
        n_frames: int,
        path: str,
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO exports (export_id, run_id, strategy, budget_frames, n_episodes, "
            "n_frames, path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(export_id) DO UPDATE SET n_episodes = excluded.n_episodes, "
            "n_frames = excluded.n_frames, path = excluded.path",
            (export_id, run_id, strategy, budget_frames, n_episodes, n_frames, path, created_at),
        )


class SqliteSourceRepository:
    def __init__(self, conn: sqlite3.Connection, now: str) -> None:
        self._conn = conn
        self._now = now

    def upsert(self, source: Source) -> None:
        self._conn.execute(
            "INSERT INTO sources (source_id, kind, uri, revision, embodiment, license, notes, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET kind = excluded.kind, uri = excluded.uri, "
            "revision = excluded.revision, embodiment = excluded.embodiment, "
            "license = excluded.license, notes = excluded.notes, updated_at = excluded.updated_at",
            (
                source.source_id,
                source.kind,
                source.uri,
                source.revision,
                source.embodiment,
                source.license,
                source.notes,
                self._now,
            ),
        )


def _episode_to_row(episode: Episode) -> tuple[Any, ...]:
    meta = episode.meta
    action = meta.action_spec if meta else None
    state = meta.state_spec if meta else None
    return (
        episode.uid,
        episode.source_id,
        episode.upstream_id,
        episode.content_hash,
        episode.stage.value,
        meta.embodiment if meta else None,
        meta.task if meta else None,
        action.level.value if action else None,
        action.space.value if action else None,
        action.dim if action else None,
        action.physical_dim if action else None,
        state.space.value if state else None,
        state.dim if state else None,
        meta.n_frames if meta else None,
        meta.fps_nominal if meta else None,
        meta.fps_effective if meta else None,
        meta.duration_s if meta else None,
        _dumps(meta.capabilities.model_dump(mode="json")) if meta else None,
        _dumps(action.model_dump(mode="json")) if action else None,
        _dumps(state.model_dump(mode="json")) if state else None,
        _dumps([c.model_dump(mode="json") for c in meta.cameras]) if meta else None,
        _dumps(meta.provenance.model_dump(mode="json")) if meta else None,
        _dumps(meta.boundary.model_dump(mode="json")) if meta else None,
        _dumps(meta.raw_extra) if meta else None,
        _dumps(list(meta.raw_frame_columns)) if meta else None,
        _dumps({k: v.model_dump(mode="json") for k, v in episode.channel_stats.items()}),
        episode.frames_path,
        episode.qc_verdict.value,
        episode.last_error,
        meta.schema_version if meta else None,
        episode.adapter_version,
        episode.ruleset_version,
        episode.first_seen_run,
        episode.last_update_run,
        episode.updated_at,
    )


def _row_to_episode(row: sqlite3.Row) -> Episode:
    meta = None
    if row["action_spec_json"] is not None:
        meta = EpisodeMeta(
            uid=row["episode_uid"],
            source_id=row["source_id"],
            upstream_id=row["upstream_id"],
            embodiment=row["embodiment"],
            task=row["task"],
            n_frames=row["n_frames"],
            action_spec=_spec_from_json(row["action_spec_json"]),
            state_spec=_spec_from_json(row["state_spec_json"]),
            capabilities=Capabilities(**json.loads(row["capabilities_json"])),
            cameras=tuple(CameraSpec(**c) for c in json.loads(row["camera_json"])),
            provenance=Provenance(**json.loads(row["provenance_json"])),
            boundary=EpisodeBoundary(**json.loads(row["boundary_json"])),
            raw_extra=json.loads(row["raw_extra_json"]),
            raw_frame_columns=tuple(json.loads(row["raw_columns_json"] or "[]")),
            fps_nominal=row["fps_nominal"],
            fps_effective=row["fps_effective"],
            duration_s=row["duration_s"],
            schema_version=row["schema_version"],
        )
    stats = {
        name: ChannelStats(**payload)
        for name, payload in json.loads(row["stats_json"] or "{}").items()
    }
    return Episode(
        uid=row["episode_uid"],
        source_id=row["source_id"],
        upstream_id=row["upstream_id"],
        stage=IngestionStage(row["status"]),
        qc_verdict=EpisodeVerdict(row["qc_verdict"]),
        meta=meta,
        content_hash=row["content_hash"],
        frames_path=row["frames_path"],
        adapter_version=row["adapter_version"],
        ruleset_version=row["ruleset_version"],
        channel_stats=stats,
        last_error=row["last_error"],
        first_seen_run=row["first_seen_run"] or "",
        last_update_run=row["last_update_run"] or "",
        updated_at=row["updated_at"] or "",
    )


def _row_to_state(row: sqlite3.Row) -> EpisodeState:
    return EpisodeState(
        episode_uid=row["episode_uid"],
        stage=IngestionStage(row["stage"]),
        attempt=row["attempt"],
        last_error=row["last_error"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        updated_at=row["updated_at"] or "",
    )


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "resumed_from": row["resumed_from"],
        "args": json.loads(row["args_json"] or "{}"),
        "stats": json.loads(row["stats_json"] or "{}"),
    }
