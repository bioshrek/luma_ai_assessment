"""The `rdp` command line. Thin: parse, delegate to a use case, present the result."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from rdp.domain.curation.sampler import BALANCED
from rdp.domain.errors import BudgetTooSmall
from rdp.domain.run import IngestionRun
from rdp.infrastructure.storage.atomic_fs import atomic_write_text
from rdp.interfaces.presenters.report_md import (
    print_report,
    render_json,
    render_markdown,
)
from rdp.interfaces.presenters.stats_md import print_stats
from rdp.interfaces.presenters.stats_md import render_json as render_stats_json
from rdp.interfaces.presenters.stats_md import render_markdown as render_stats_markdown
from rdp.interfaces.wiring import DEFAULT_CONFIG, DEFAULT_STORE, Container

app = typer.Typer(help="Robot Demonstration Pipeline", no_args_is_help=True)
console = Console()

StoreOption = Annotated[Path, typer.Option("--store", help="Catalog and artifact root.")]
ConfigOption = Annotated[Path, typer.Option("--config", help="Configuration directory.")]


@app.command()
def run(
    source: Annotated[str, typer.Option("--source", help="source_id from sources.yaml")],
    max_episodes: Annotated[
        int | None, typer.Option("--max-episodes", help="Override the configured cap.")
    ] = None,
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Ingest a source: discover, fetch, normalize, QC, commit."""
    container = Container(store=store, config=config)
    definition = container.sources.get(source)
    if definition is None:
        known = ", ".join(sorted(container.sources))
        raise typer.BadParameter(f"unknown source {source!r}; known sources: {known}")

    ingestion = IngestionRun(
        run_id=container.new_run_id(),
        started_at=container.clock.now_iso(),
        args={"source": source, "max_episodes": max_episodes},
    )
    # Before anything is ingested: close out runs that died, release dead leases, delete orphan
    # `*.tmp` files, and demote episodes whose artifacts no longer open (design §5).
    recovery = container.recover()(ingestion.run_id)
    ingestion.resumed_from = recovery.resumed_from
    ingestion.recovery = recovery.as_dict()
    if not recovery.is_clean:
        console.print(f"[yellow]recovered: {recovery.as_dict()}[/yellow]")

    with container.unit_of_work() as uow:
        uow.sources.upsert(definition)
        uow.runs.start(ingestion)
        uow.commit()

    try:
        container.ingest()(
            definition, container.adapter_for(definition), ingestion, max_episodes=max_episodes
        )
        ingestion.finish(container.clock.now_iso())
    except BaseException:
        # An interrupted run is recorded as interrupted; the episodes it did commit stay
        # committed, which is what makes the next run a resume rather than a restart.
        ingestion.finish(container.clock.now_iso(), status="INTERRUPTED")
        _finish(container, ingestion)
        raise

    _finish(container, ingestion)


def _finish(container: Container, ingestion: IngestionRun) -> None:
    with container.unit_of_work() as uow:
        uow.runs.finish(ingestion)
        uow.commit()
    for reporter in container.run_reporters():
        reporter.publish(ingestion)


@app.command()
def export(
    budget: Annotated[int, typer.Option("--budget", help="Maximum total frames.")],
    out: Annotated[Path, typer.Option("--out", help="Destination JSONL manifest.")],
    strategy: Annotated[str, typer.Option("--strategy")] = BALANCED,
    include_review: Annotated[bool, typer.Option("--include-review")] = False,
    embodiment: Annotated[str | None, typer.Option("--embodiment")] = None,
    seed: Annotated[
        int | None, typer.Option("--seed", help="Fixes the within-group order; replayable.")
    ] = None,
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Export a curated training subset. Episodes are whole; the budget is a ceiling."""
    container = Container(store=store, config=config)
    try:
        result = container.export()(
            out=out,
            budget_frames=budget,
            strategy=strategy,
            include_review=include_review,
            embodiment=embodiment,
            seed=seed,
        )
    except BudgetTooSmall as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]{result.n_episodes} episodes, {result.n_frames}/{budget} frames "
        f"-> {result.path}[/green]"
    )
    for group in result.plan.groups:
        console.print(
            f"  {group.embodiment:<24} quota {group.quota_frames:>8}  "
            f"took {group.selected_frames:>8} in {group.selected_episodes:>4} episodes "
            f"(of {group.eligible_frames} eligible)"
        )


@app.command()
def report(
    run_id: Annotated[
        str | None, typer.Option("--run", "--run-id", help="Defaults to the latest run.")
    ] = None,
    cumulative: Annotated[
        bool,
        typer.Option("--cumulative", help="The catalog only, with no run scoped section."),
    ] = False,
    fmt: Annotated[
        str, typer.Option("--format", help="table | md | json", case_sensitive=False)
    ] = "table",
    out: Annotated[Path | None, typer.Option("--out", help="Also write markdown here.")] = None,
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Summarise a run and the catalog. Recomputed from the database, never cached."""
    if fmt.lower() not in {"table", "md", "json"}:
        raise typer.BadParameter(f"unknown format {fmt!r}; expected table, md or json")
    container = Container(store=store, config=config)
    result = container.report()(run_id, cumulative_only=cumulative)
    markdown = render_markdown(result)

    if fmt.lower() == "json":
        # Plain stdout, not a rich table: `rdp report --format json > x.json` must be valid JSON.
        typer.echo(render_json(result))
    elif fmt.lower() == "md":
        typer.echo(markdown, nl=False)
    else:
        print_report(result, console)

    if result.run is not None:
        # Sits next to the run's JSON, so a run is always documented in both forms.
        published = container.paths.reports / f"{result.run['run_id']}.md"
        atomic_write_text(published, markdown)
        if fmt.lower() == "table":
            console.print(f"wrote {published}")
    if out is not None:
        atomic_write_text(out, markdown)
        if fmt.lower() == "table":
            console.print(f"wrote {out}")


@app.command()
def stats(
    out: Annotated[Path | None, typer.Option("--out", help="Also write markdown here.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Show the measured distribution of every QC metric — the evidence behind the thresholds."""
    container = Container(store=store, config=config)
    result = container.stats()()
    if as_json:
        console.print_json(render_stats_json(result))
    else:
        print_stats(result, console)
    if out is not None:
        atomic_write_text(out, render_stats_markdown(result))
        console.print(f"wrote {out}")


@app.command()
def sources(
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """List the configured sources."""
    container = Container(store=store, config=config)
    for source_id, definition in sorted(container.sources.items()):
        cap = definition.max_episodes if definition.max_episodes is not None else "all"
        console.print(
            f"{source_id:<22} {definition.kind:<14} {definition.embodiment:<16} max={cap}"
        )


if __name__ == "__main__":
    app()
