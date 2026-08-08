"""The same crash, but through a real process boundary.

The in-process matrix proves the pipeline handles every checkpoint; it cannot prove that the
*process* leaves a recoverable state behind, because a raised exception still unwinds the stack,
flushes buffers and runs `finally` blocks. `os._exit` does none of that — no `runs.finish`, no
SQLite cleanup, nothing but whatever already reached the disk. That is the property under test
here, on the real adapter and the committed fixture.

`scripts/demo_crash_resume.sh` goes one step further and has an external process send SIGKILL.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.conftest import _SOURCES, REPO

from rdp.application.ingest_episodes import QC_AFTER_EPISODE
from rdp.infrastructure.faults import FAULT_ENV

N_FIXTURE_EPISODES = 3


def _workspace(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    fixture = REPO / "tests/fixtures/lerobot_pusht_mini"
    (config / "sources.yaml").write_text(_SOURCES.format(uri=fixture))
    for name in ("embodiments.yaml", "qc.yaml"):
        shutil.copy(REPO / "config" / name, config / name)
    return config


def _rdp(tmp_path: Path, fault: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if fault is not None:
        env[FAULT_ENV] = fault
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "rdp",
            "run",
            "--source",
            "pusht",
            "--store",
            str(tmp_path / "store"),
            "--config",
            str(_workspace(tmp_path)),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        check=False,
    )


def _query(tmp_path: Path, sql: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(tmp_path / "store/catalog.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def _counter(stdout: str, name: str) -> int:
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == name:
            return int(parts[1])
    raise AssertionError(f"counter {name!r} not printed:\n{stdout}")


def test_a_killed_process_is_resumed_by_the_next_one(tmp_path: Path) -> None:
    crashed = _rdp(tmp_path, fault=f"{QC_AFTER_EPISODE}:2")
    assert crashed.returncode == 1
    assert "FAULT_INJECT" in crashed.stderr

    committed_before = _query(
        tmp_path, "SELECT COUNT(*) AS n FROM episodes WHERE status = 'COMMITTED'"
    )[0]["n"]
    assert 0 < int(str(committed_before)) < N_FIXTURE_EPISODES
    # The trace `kill -9` leaves: a run that never wrote its own ending.
    assert _query(tmp_path, "SELECT * FROM runs")[0]["finished_at"] is None

    resumed = _rdp(tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert "resumed from" in resumed.stdout

    runs = _query(tmp_path, "SELECT * FROM runs ORDER BY started_at, rowid")
    assert len(runs) == 2
    assert runs[0]["status"] == "INTERRUPTED", "recovery closes the dead run out"
    assert runs[1]["resumed_from"] == runs[0]["run_id"]

    episodes = _query(tmp_path, "SELECT status FROM episodes")
    assert len(episodes) == N_FIXTURE_EPISODES
    assert {row["status"] for row in episodes} == {"COMMITTED"}


def test_a_third_run_finds_nothing_to_do(tmp_path: Path) -> None:
    assert _rdp(tmp_path).returncode == 0
    before = _query(tmp_path, "SELECT episode_uid, updated_at FROM episodes ORDER BY episode_uid")

    again = _rdp(tmp_path)

    assert again.returncode == 0
    assert _counter(again.stdout, "skipped_already_processed") == N_FIXTURE_EPISODES
    assert _counter(again.stdout, "committed") == 0
    assert "resumed from" not in again.stdout
    assert (
        _query(tmp_path, "SELECT episode_uid, updated_at FROM episodes ORDER BY episode_uid")
        == before
    ), "a no-op run must not rewrite a single row"
