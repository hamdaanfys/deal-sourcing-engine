"""dealsource command-line interface.

Prints aggregate counts only; record-level output goes to files under the data dir.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Annotated

import httpx
import typer
from dotenv import load_dotenv

from dealsource import db
from dealsource.config import Settings, user_agent
from dealsource.httpcache import CachedHttp
from dealsource.models import GeoSpec
from dealsource.resolve.pipeline import resolve as run_resolve
from dealsource.sources.census_cbp import CensusCBPSource, CensusError, store_stats
from dealsource.sources.csv_source import ContactColumnError, CSVSource, store_records

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
ingest_app = typer.Typer(no_args_is_help=True, help="Load companies or market data from a source.")
app.add_typer(ingest_app, name="ingest")


def make_http_client() -> httpx.Client:
    """Factory for outbound HTTP; tests replace it with a MockTransport-backed client."""
    return httpx.Client(timeout=30.0)


@app.callback()
def main(ctx: typer.Context) -> None:
    if not os.environ.get("DEALSOURCE_SKIP_DOTENV"):
        load_dotenv()
    ctx.obj = Settings.from_env()


def require_split(settings: Settings) -> None:
    """Pipeline commands only run once the labels dev/test split exists (DESIGN.md §11.3)."""
    if settings.split_manifest_path.exists():
        return
    if settings.labels_path.exists():
        step = "run `dealsource labels split` first"
    else:
        step = f"create {settings.labels_path} and then run `dealsource labels split`"
    typer.echo(
        f"Refusing to run: no labels split found at {settings.split_manifest_path}. "
        f"The split must be made before any pipeline run; {step}.",
        err=True,
    )
    raise typer.Exit(2)


def open_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_data_dir()
    return db.connect(settings.db_path)


def parse_map(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        fld, sep, col = p.partition("=")
        if not sep or not fld.strip() or not col.strip():
            raise typer.BadParameter(f"--map expects field=Column, got {p!r}")
        out[fld.strip()] = col.strip()
    return out


@ingest_app.command("csv")
def ingest_csv(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    map_: Annotated[
        list[str], typer.Option("--map", help="field=Column, e.g. name='Company Name'")
    ] = [],  # noqa: B006
    source_name: Annotated[str, typer.Option(help="Source label stored with each record")] = "csv",
    revenue_unit: Annotated[
        str, typer.Option(help="Unit of the revenue column: usd, usd_k, usd_m")
    ] = "usd_m",
) -> None:
    """Import companies from a CSV file. Contact-looking columns are dropped, never stored."""
    settings: Settings = ctx.obj
    require_split(settings)
    try:
        source = CSVSource(
            path, source_name=source_name, column_map=parse_map(map_), revenue_unit=revenue_unit
        )
        records = list(source.iter_records())
    except (ValueError, ContactColumnError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn = open_db(settings)
    params = {
        "path": str(path),
        "source": source_name,
        "map": parse_map(map_),
        "revenue_unit": revenue_unit,
    }
    with db.record_run(conn, "ingest_csv", params) as stats:
        stats.update(store_records(conn, iter(records)))
        stats["dropped_contact_columns"] = len(source.plan.dropped)
        stats["skipped_rows_without_name"] = source.skipped_rows
    if source.plan.dropped:
        typer.echo(
            f"Dropped {len(source.plan.dropped)} contact-looking column(s): {', '.join(source.plan.dropped)}"
        )
    typer.echo(
        f"Records: {stats['inserted']} new, {stats['updated']} updated, {stats['unchanged']} unchanged"
        + (f", {source.skipped_rows} skipped (no name)" if source.skipped_rows else "")
    )


@ingest_app.command("cbp")
def ingest_cbp(
    ctx: typer.Context,
    naics: Annotated[str, typer.Option(help="Comma-separated NAICS codes, e.g. 3323,3327")],
    geo: Annotated[
        list[str], typer.Option(help="us, state:13,37, state:* or county:*/state:13")
    ] = ["us"],  # noqa: B006
    year: Annotated[int, typer.Option(help="CBP year")] = 2022,
    refresh: Annotated[bool, typer.Option(help="Ignore the cache and re-fetch")] = False,
) -> None:
    """Fetch Census County Business Patterns market data (report-only; never affects scores)."""
    settings: Settings = ctx.obj
    require_split(settings)
    try:
        geos = [GeoSpec.parse(g) for g in geo]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    codes = [c.strip() for c in naics.split(",") if c.strip()]
    conn = open_db(settings)
    with make_http_client() as client:
        source = CensusCBPSource(
            CachedHttp(conn, client, user_agent(settings)), settings.census_api_key, refresh
        )
        params = {"naics": codes, "geo": geo, "year": year, "refresh": refresh}
        with db.record_run(conn, "ingest_cbp", params) as stats:
            try:
                results = list(source.fetch(codes, geos, year))
            except CensusError as exc:
                stats["error"] = str(exc)
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1) from exc
            stats.update(
                rows=store_stats(conn, results),
                requests=source.requests_made,
                cache_hits=source.cache_hits,
            )
    typer.echo(
        f"Stored {len(results)} market rows ({source.requests_made} requests, {source.cache_hits} from cache)"
    )
    for s in sorted(results, key=lambda s: (s.naics, s.geo_code)):
        estab = f"{s.establishments:,}" if s.establishments is not None else "n/a"
        emp = f"{s.employees:,}" if s.employees is not None else "n/a"
        typer.echo(
            f"  {s.year} NAICS {s.naics} {s.geo_name or s.geo_code}: {estab} establishments, {emp} employees"
        )


@app.command()
def resolve(
    ctx: typer.Context,
    review: Annotated[
        bool, typer.Option(help="Write possible matches to review/possible_matches.csv")
    ] = False,
) -> None:
    """Merge records that describe the same company (entity resolution)."""
    settings: Settings = ctx.obj
    require_split(settings)
    conn = open_db(settings)
    review_path = settings.review_dir / "possible_matches.csv" if review else None
    with db.record_run(conn, "resolve", {"review": review}) as stats:
        try:
            stats.update(
                run_resolve(
                    conn,
                    source_priority=settings.source_priority,
                    overrides_path=settings.overrides_path,
                    review_path=review_path,
                )
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
    typer.echo(
        f"{stats['records']} records -> {stats['companies']} companies "
        f"({stats['multi_record_companies']} merged from several records, {stats['new_companies']} new)"
    )
    typer.echo(f"Items needing review: {stats['review_items']}")
    if stats["unknown_override_refs"]:
        typer.echo(
            f"Warning: {stats['unknown_override_refs']} override pair(s) refer to unknown records"
        )
    if review_path:
        typer.echo(f"Review file: {review_path}")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show aggregate counts per table."""
    settings: Settings = ctx.obj
    if not settings.db_path.exists():
        typer.echo("No database yet.")
        return
    conn = db.connect(settings.db_path)
    for table in ("raw_records", "companies", "market_stats", "http_cache", "runs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        typer.echo(f"{table:12} {n}")
