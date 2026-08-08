"""The `rdp` command line. Thin: parse, delegate to a use case, present the result."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from rdp.domain.curation.sampler import SEQUENTIAL
from rdp.domain.errors import BudgetTooSmall
from rdp.domain.run import IngestionRun
from rdp.infrastructure.storage.atomic_fs import atomic_write_text
from rdp.interfaces.presenters.report_md import (
    FileRunReporter,
    print_report,
    render_json,
    render_markdown,
)
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
    console.print(f"[green]run {ingestion.run_id} {ingestion.status}[/green]")
    for name, value in sorted(ingestion.stats()["counters"].items()):
        console.print(f"  {name:<28} {value}")


def _finish(container: Container, ingestion: IngestionRun) -> None:
    with container.unit_of_work() as uow:
        uow.runs.finish(ingestion)
        uow.commit()
    FileRunReporter(container.paths.reports).publish(ingestion)


@app.command()
def export(
    budget: Annotated[int, typer.Option("--budget", help="Maximum total frames.")],
    out: Annotated[Path, typer.Option("--out", help="Destination JSONL manifest.")],
    strategy: Annotated[str, typer.Option("--strategy")] = SEQUENTIAL,
    include_review: Annotated[bool, typer.Option("--include-review")] = False,
    embodiment: Annotated[str | None, typer.Option("--embodiment")] = None,
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
        )
    except BudgetTooSmall as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    console.print(
        f"[green]{result.n_episodes} episodes, {result.n_frames}/{budget} frames "
        f"-> {result.path}[/green]"
    )


@app.command()
def report(
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Defaults to the latest run.")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Also write markdown here.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
    store: StoreOption = DEFAULT_STORE,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Summarise a run and the catalog. Recomputed from the database, never cached."""
    container = Container(store=store, config=config)
    result = container.report()(run_id)
    if as_json:
        console.print_json(render_json(result))
    else:
        print_report(result, console)

    markdown = render_markdown(result)
    if result.run is not None:
        # Sits next to the run's JSON, so a run is always documented in both forms.
        published = container.paths.reports / f"{result.run['run_id']}.md"
        atomic_write_text(published, markdown)
        console.print(f"wrote {published}")
    if out is not None:
        atomic_write_text(out, markdown)
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
