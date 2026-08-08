"""`rdp stats` rendering: the measured distribution behind every threshold.

Deliberately the same `ChannelStats` shape used for channels, so "distribution" reads the same
way everywhere: count, holes, mean, sd, min, p1, p99, max.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from rich.console import Console
from rich.table import Table

from rdp.application.build_stats import MetricDistribution, Stats


def render_json(stats: Stats) -> str:
    return json.dumps(stats.as_dict(), indent=2, sort_keys=True)


def render_markdown(stats: Stats) -> str:
    lines = [
        "# QC metric distributions",
        "",
        "Every threshold in `config/qc.yaml` is justified against these numbers. They are read",
        "back out of `qc_results.metrics_json` by SQL; no rule is re-run to produce them.",
        "",
        "## Verdicts (latest per episode and rule)",
        "",
    ]
    rows = [
        (rule_id, verdict, str(count))
        for rule_id, verdicts in sorted(stats.verdicts.items())
        for verdict, count in sorted(verdicts.items())
    ]
    lines += _table(["rule", "verdict", "episodes"], rows)

    lines += ["", "## Metrics", ""]
    headers = ["source", "rule", "metric", "n", "min", "p1", "mean", "p99", "max"]
    lines += _table(
        headers,
        [
            [
                item.source_id,
                item.rule_id,
                item.metric,
                str(item.stats.count),
                *(_num(value) for value in _values(item)),
            ]
            for item in stats.distributions
        ],
    )
    return "\n".join(lines) + "\n"


def print_stats(stats: Stats, console: Console | None = None) -> None:
    console = console or Console()
    table = Table("source", "rule", "metric", "n", "min", "p1", "mean", "p99", "max", box=None)
    for item in stats.distributions:
        table.add_row(
            item.source_id,
            item.rule_id,
            item.metric,
            str(item.stats.count),
            *(_num(value) for value in _values(item)),
        )
    console.print(table)


def _values(item: MetricDistribution) -> list[float | None]:
    return [item.stats.min, item.stats.p1, item.stats.mean, item.stats.p99, item.stats.max]


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.6g}"


def _table(headers: list[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return out
