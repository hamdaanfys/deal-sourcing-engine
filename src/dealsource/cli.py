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
from dealsource.clock import SystemClock
from dealsource.config import Settings, user_agent
from dealsource.enrich.fetcher import PoliteFetcher, is_cacheable_page
from dealsource.enrich.pipeline import EnrichConfig
from dealsource.enrich.pipeline import enrich as run_enrich
from dealsource.enrich.prompts import PROMPT_VERSION
from dealsource.eval.labels import LabelsError
from dealsource.eval.split import (
    DEFAULT_SEED,
    DEFAULT_TEST_FRACTION,
    SplitRefused,
    format_counts,
    make_split,
)
from dealsource.httpcache import CachedHttp
from dealsource.llm.cache import LLMRunner
from dealsource.llm.ollama import OllamaBackend, RemoteHostRefused
from dealsource.models import GeoSpec
from dealsource.resolve.pipeline import resolve as run_resolve
from dealsource.sources.census_cbp import CensusCBPSource, CensusError, store_stats
from dealsource.sources.csv_source import ContactColumnError, CSVSource, store_records

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
ingest_app = typer.Typer(no_args_is_help=True, help="Load companies or market data from a source.")
app.add_typer(ingest_app, name="ingest")
labels_app = typer.Typer(no_args_is_help=True, help="Analyst labels: the one-time dev/test split.")
app.add_typer(labels_app, name="labels")


def make_http_client() -> httpx.Client:
    """Factory for outbound HTTP; tests replace it with a MockTransport-backed client."""
    return httpx.Client(timeout=30.0)


def make_llm_backend(settings: Settings):
    """Factory for the LLM backend; tests replace it with a fake."""
    if settings.llm_backend != "ollama":
        raise ValueError(
            f"LLM_BACKEND {settings.llm_backend!r} is not supported; only 'ollama' is implemented"
        )
    return OllamaBackend(
        settings.ollama_host, settings.llm_model, allow_remote=settings.allow_remote_llm
    )


def make_clock():
    return SystemClock()


def parse_age(value: str | None) -> float | None:
    """'30d', '12h' or '45m' -> seconds."""
    if not value:
        return None
    units = {"d": 86400, "h": 3600, "m": 60}
    try:
        return float(value[:-1]) * units[value[-1]]
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter("use a number followed by d, h or m, e.g. 30d") from exc


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
def enrich(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option(help="Enrich at most this many companies")] = None,
    company_id: Annotated[list[int], typer.Option(help="Only these company IDs")] = [],  # noqa: B006
    max_pages: Annotated[
        int | None, typer.Option(help="Pages per site, including the homepage")
    ] = None,
    refresh_older_than: Annotated[
        str | None, typer.Option(help="Re-fetch cached pages older than e.g. 30d")
    ] = None,
) -> None:
    """Fetch each company's website politely and extract structured facts with the local LLM."""
    settings: Settings = ctx.obj
    require_split(settings)
    if not settings.user_agent_contact:
        typer.echo(
            "Error: set DEALSOURCE_USER_AGENT_CONTACT in .env (a URL or mailbox site owners can "
            "use to reach you); it goes in the user agent of every request.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        backend = make_llm_backend(settings)
    except (RemoteHostRefused, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    max_age = parse_age(refresh_older_than)
    conn = open_db(settings)
    clock = make_clock()
    params = {
        "limit": limit,
        "company_id": company_id,
        "max_pages": max_pages,
        "refresh_older_than": refresh_older_than,
    }
    with make_http_client() as client, db.record_run(conn, "enrich", params) as stats:
        http = CachedHttp(conn, client, user_agent(settings), cacheable=is_cacheable_page)
        fetcher = PoliteFetcher(
            http, clock=clock, min_delay=settings.fetch_min_delay, page_max_age_seconds=max_age
        )
        runner = LLMRunner(conn, backend, prompt_version=PROMPT_VERSION)
        stats.update(
            run_enrich(
                conn,
                fetcher=fetcher,
                runner=runner,
                config=EnrichConfig(max_pages=max_pages or settings.fetch_max_pages),
                clock=clock,
                company_ids=company_id or None,
                limit=limit,
            )
        )
    if stats["llm_problem"]:
        typer.echo(
            f"Warning: LLM unavailable ({stats['llm_problem']}); pages were fetched and cached, extraction skipped."
        )
    summary = ", ".join(f"{k} {v}" for k, v in sorted(stats["statuses"].items()))
    typer.echo(f"Enriched {stats['companies']} companies: {summary or 'nothing to do'}")
    typer.echo(f"HTTP: {stats['requests']} requests, {stats['fetch_cache_hits']} from cache")
    if stats["median_ms"] is not None:
        typer.echo(
            f"Time per company: median {stats['median_ms'] / 1000:.1f}s, max {stats['max_ms'] / 1000:.1f}s"
        )
    typer.echo(f"LLM tokens: {stats['prompt_tokens']:,} in, {stats['completion_tokens']:,} out")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show aggregate counts per table."""
    settings: Settings = ctx.obj
    if not settings.db_path.exists():
        typer.echo("No database yet.")
        return
    conn = db.connect(settings.db_path)
    for table in ("raw_records", "companies", "market_stats", "http_cache", "enrichments", "runs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        typer.echo(f"{table:12} {n}")
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM enrichments GROUP BY status ORDER BY status"
    ):
        typer.echo(f"  enrich {status:20} {n}")
    calls = conn.execute(
        "SELECT latency_ms FROM llm_calls WHERE cache_hit = 0 AND ok = 1 AND latency_ms IS NOT NULL ORDER BY latency_ms"
    ).fetchall()
    agg = conn.execute(
        """SELECT COUNT(*), SUM(cache_hit), SUM(ok = 0), SUM(CASE WHEN cache_hit = 0 THEN prompt_tokens END),
                  SUM(CASE WHEN cache_hit = 0 THEN completion_tokens END), COUNT(DISTINCT company_id)
           FROM llm_calls"""
    ).fetchone()
    if agg[0]:
        lat = [r[0] for r in calls]
        p50 = lat[len(lat) // 2] / 1000 if lat else 0
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] / 1000 if lat else 0
        per_co = ((agg[3] or 0) + (agg[4] or 0)) / max(agg[5], 1)
        typer.echo(
            f"LLM calls {agg[0]} (cache hits {agg[1] or 0}, failed {agg[2] or 0}); latency p50 {p50:.1f}s, "
            f"p95 {p95:.1f}s; tokens {agg[3] or 0:,} in / {agg[4] or 0:,} out; {per_co:,.0f} tokens per company"
        )


@labels_app.command("split")
def labels_split(
    ctx: typer.Context,
    test_fraction: Annotated[
        float, typer.Option(help="Share of companies held out for the test set")
    ] = DEFAULT_TEST_FRACTION,
    seed: Annotated[int, typer.Option(help="Random seed for the split")] = DEFAULT_SEED,
    map_: Annotated[
        list[str],
        typer.Option("--map", help="field=Column for company_name, website, state, decision"),
    ] = [],  # noqa: B006
) -> None:
    """Split labels.csv into dev and held-out test sets, once. Prints counts only."""
    settings: Settings = ctx.obj
    try:
        result = make_split(
            labels_path=settings.labels_path,
            manifest_path=settings.split_manifest_path,
            db_path=settings.db_path,
            conflicts_path=settings.data_dir / "evals" / "label_conflicts.csv",
            seed=seed,
            test_fraction=test_fraction,
            column_map=parse_map(map_),
        )
    except (SplitRefused, LabelsError, ValueError) as exc:
        typer.echo(f"Refusing to split: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Labels split written to {result.manifest_path} (read-only; never redone). "
        f"{result.rows} label rows -> {result.companies} companies." + format_counts(result.counts)
    )
