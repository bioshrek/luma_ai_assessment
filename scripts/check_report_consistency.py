#!/usr/bin/env python
"""Re-derive every number in `rdp report` with independent SQL, and diff.

The report reaches its numbers through repositories, `BuildReport` and a presenter. This script
reaches them again through hand-written SQL against `store/catalog.sqlite` and a walk of the
store, then compares the two, cell by cell, by parsing the markdown the presenter emitted.

Two deliberate properties:

- **It parses the rendered markdown, not the `Report` object.** A number that the presenter
  formats wrongly is exactly the kind of drift this is here to catch.
- **It fails on a section it does not recognise.** Adding a table to the report without adding
  a query here is a failure, not a silent gap. The only exemptions are
  `report_md.MEASURED_SECTIONS`: a wall-clock duration, the filesystem findings of a recovery
  pass, and free-text error strings, none of which the catalog can be asked for again.

Exit code 0 when the report and the SQL agree, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from rdp.interfaces.presenters.report_md import MEASURED_SECTIONS, render_markdown
from rdp.interfaces.wiring import DEFAULT_CONFIG, DEFAULT_STORE, Container

Rows = list[tuple[str, ...]]

# A different formulation of "the latest verdict per (episode, rule)" than the repository's
# window function, on purpose: two spellings of the same intent that must agree.
_LATEST = """
    SELECT q.rule_id, q.verdict, q.reason FROM qc_results q
    WHERE q.rowid = (
        SELECT q2.rowid FROM qc_results q2
        WHERE q2.episode_uid = q.episode_uid AND q2.rule_id = q.rule_id
        ORDER BY q2.created_at DESC, q2.rowid DESC LIMIT 1
    )
"""


def parse_sections(markdown: str) -> dict[str, Rows]:
    """`{section title: [row cells]}`, header and separator lines dropped."""
    sections: dict[str, Rows] = {}
    current = ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif line.startswith("|") and current:
            cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
            if set("".join(cells)) <= {"-"}:
                continue
            sections[current].append(cells)
    # Drop each table's header row; every table in the report has exactly one.
    return {title: rows[1:] for title, rows in sections.items()}


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> Rows:
    return [tuple(str(cell) for cell in row) for row in conn.execute(sql, tuple(params))]


def _hit_rate(verdicts: dict[str, int]) -> tuple[int, int, int, int]:
    skipped = verdicts.get("SKIPPED", 0)
    errors = verdicts.get("ERROR", 0)
    hits = verdicts.get("FAIL", 0) + verdicts.get("REVIEW", 0)
    return sum(verdicts.values()) - skipped, hits, skipped, errors


def expected(
    conn: sqlite3.Connection, usage: dict[str, int], run_id: str | None
) -> dict[str, Rows]:
    out: dict[str, Rows] = {}

    if run_id is not None:
        out["QC verdicts (this run)"] = _rows(
            conn,
            "SELECT rule_id, verdict, COUNT(*) FROM qc_results WHERE run_id = ? "
            "GROUP BY rule_id, verdict ORDER BY rule_id, verdict",
            (run_id,),
        )
        out["Skipped rules (this run)"] = _rows(
            conn,
            "SELECT rule_id, COALESCE(reason, 'unspecified'), COUNT(*) FROM qc_results "
            "WHERE run_id = ? AND verdict = 'SKIPPED' "
            "GROUP BY rule_id, reason ORDER BY rule_id, reason",
            (run_id,),
        )
        out["This run"] = _rows(
            conn,
            "SELECT key, value FROM runs, json_each(runs.stats_json, '$.counters') "
            "WHERE run_id = ? ORDER BY key",
            (run_id,),
        )
        out["Failure reasons (this run)"] = _rows(
            conn,
            "SELECT key, value FROM runs, json_each(runs.stats_json, '$.failure_reasons') "
            "WHERE run_id = ? ORDER BY key",
            (run_id,),
        )

    out["Catalog totals"] = _rows(
        conn, "SELECT status, COUNT(*) FROM episodes GROUP BY status ORDER BY status"
    )
    totals = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n_frames), 0), COALESCE(SUM(duration_s), 0.0) "
        "FROM episodes WHERE status = 'COMMITTED'"
    ).fetchone()
    out["Corpus (committed)"] = [
        ("episodes", str(totals[0])),
        ("frames", str(totals[1])),
        ("duration_s", f"{float(totals[2]):.3f}"),
    ]
    out["Source x embodiment"] = _rows(
        conn,
        "SELECT source_id, COALESCE(embodiment, ''), COUNT(*), COALESCE(SUM(n_frames), 0) "
        "FROM episodes WHERE status = 'COMMITTED' GROUP BY source_id, embodiment "
        "ORDER BY source_id, embodiment",
    )
    out["QC verdicts (cumulative)"] = _rows(
        conn,
        f"SELECT rule_id, verdict, COUNT(*) FROM ({_LATEST}) "
        "GROUP BY rule_id, verdict ORDER BY rule_id, verdict",
    )
    out["Skip reasons (cumulative)"] = _rows(
        conn,
        f"SELECT rule_id, COALESCE(reason, 'unspecified'), COUNT(*) FROM ({_LATEST}) "
        "WHERE verdict = 'SKIPPED' GROUP BY rule_id, reason ORDER BY rule_id, reason",
    )

    counts: dict[str, dict[str, int]] = {}
    for rule_id, verdict, count in out["QC verdicts (cumulative)"]:
        counts.setdefault(rule_id, {})[verdict] = int(count)
    out["QC rule rates (cumulative)"] = []
    for rule_id, verdicts in sorted(counts.items()):
        evaluated, hits, skipped, errors = _hit_rate(verdicts)
        out["QC rule rates (cumulative)"].append(
            (
                rule_id,
                str(evaluated),
                str(hits),
                str(skipped),
                str(errors),
                f"{(hits / evaluated if evaluated else 0.0):.4f}",
                f"{(skipped / (evaluated + skipped) if evaluated + skipped else 0.0):.4f}",
            )
        )

    out["Disk usage"] = [(layer, str(size)) for layer, size in sorted(usage.items())]
    return out


def disk_usage(store: Path) -> dict[str, int]:
    usage = {
        layer: sum(p.stat().st_size for p in (store / layer).rglob("*") if p.is_file())
        for layer in ("raw", "normalized", "cache")
    }
    catalog = store / "catalog.sqlite"
    usage["catalog"] = catalog.stat().st_size if catalog.is_file() else 0
    usage["total"] = sum(usage.values())
    return usage


def check(store: Path, config: Path, run_id: str | None = None) -> list[str]:
    """Return one line per disagreement; an empty list means the report is reproducible."""
    problems, _ = check_verbose(store, config, run_id)
    return problems


def check_verbose(
    store: Path, config: Path, run_id: str | None = None
) -> tuple[list[str], list[str]]:
    """`(problems, sections compared)`. The second half is what stops a vacuous pass."""
    container = Container(store=store, config=config)
    try:
        report = container.report()(run_id)
        markdown = render_markdown(report)
        # Taken before the catalog is closed: closing the last WAL connection checkpoints, and
        # a `catalog.sqlite` that grew between the two measurements is not a disagreement.
        usage = disk_usage(store)
    finally:
        container.catalog.close()

    scoped = str(report.run["run_id"]) if report.run is not None else None
    conn = sqlite3.connect(store / "catalog.sqlite")
    try:
        reference = expected(conn, usage, scoped)
    finally:
        conn.close()

    problems = []
    compared = []
    actual = parse_sections(markdown)
    for title, rows in actual.items():
        if title in MEASURED_SECTIONS:
            continue
        if title not in reference:
            problems.append(f"{title}: no SQL reproduces this section")
            continue
        compared.append(title)
        if rows != reference[title]:
            problems.append(f"{title}: report {rows} != sql {reference[title]}")
    for title in reference:
        if title not in actual:
            problems.append(f"{title}: expected by SQL but absent from the report")
    return problems, compared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run", dest="run_id", default=None)
    args = parser.parse_args()

    problems, compared = check_verbose(args.store, args.config, args.run_id)
    for problem in problems:
        print(f"MISMATCH {problem}")
    for title in compared:
        print(f"  ok  {title}")
    if problems:
        print(f"{len(problems)} problems")
        return 1
    print(
        f"{len(compared)} sections reproduced from the catalog; "
        f"{len(MEASURED_SECTIONS)} measured sections exempt"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
