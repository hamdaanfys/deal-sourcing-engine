"""dealsource command-line interface.

Prints aggregate counts only; record-level output goes to files under the data dir.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from datetime import date
from pathlib import Path
from typing import Annotated

import httpx
import typer
from dotenv import load_dotenv

from dealsource import db
from dealsource.clock import SystemClock
from dealsource.config import Settings, user_agent
from dealsource.enrich.fetcher import PoliteFetcher, SiteGate, is_cacheable_page
from dealsource.enrich.pipeline import EnrichConfig
from dealsource.enrich.pipeline import enrich as run_enrich
from dealsource.enrich.prompts import PROMPT_VERSION
from dealsource.eval.evaluate import EvalRefused, append_test_log, evaluate, read_test_log
from dealsource.eval.label_export import DEFAULT_N, ExportRefused, export_for_labeling
from dealsource.eval.labels import LabelsError
from dealsource.eval.metrics import DEFAULT_BOOTSTRAP_SEED
from dealsource.eval.split import (
    DEFAULT_SEED,
    DEFAULT_TEST_FRACTION,
    TEST,
    SplitRefused,
    format_counts,
    make_split,
)
from dealsource.export.csv_export import ExportRefused as RankedExportRefused
from dealsource.export.csv_export import export_ranked
from dealsource.httpcache import CachedHttp
from dealsource.llm.cache import LLMRunner
from dealsource.llm.ollama import OllamaBackend, RemoteHostRefused
from dealsource.models import GeoSpec
from dealsource.resolve.pipeline import resolve as run_resolve
from dealsource.score.scorer import latest_score_run, score_all
from dealsource.score.thesis import Thesis, ThesisError, load_thesis
from dealsource.sources.census_cbp import CensusCBPSource, CensusError, store_stats
from dealsource.sources.csv_source import ContactColumnError, CSVSource, store_records
from dealsource.sources.osm import OsmDownloadError, OsmSource, download_state
from dealsource.sources.sam_extract import (
    SamError,
    SamExtractSource,
    download_extract,
    latest_month,
)
from dealsource.sources.usaspending import UsaSpendingError, UsaSpendingSource
from dealsource.websites import finder as website_finder
from dealsource.websites.review import SampleRefused, refresh_sample, write_sample

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
ingest_app = typer.Typer(no_args_is_help=True, help="Load companies or market data from a source.")
app.add_typer(ingest_app, name="ingest")
discover_app = typer.Typer(
    no_args_is_help=True, help="Find candidate companies in public sources (SAM.gov, USAspending)."
)
app.add_typer(discover_app, name="discover")
websites_app = typer.Typer(
    no_args_is_help=True,
    help="Find company websites (guess and verify) and hand-check the matches.",
)
app.add_typer(websites_app, name="websites")
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


def make_resolver():
    """DNS check for candidate domains; tests replace it."""
    return website_finder.dns_resolves


def require_contact(settings: Settings) -> None:
    if not settings.user_agent_contact:
        typer.echo(
            "Error: set DEALSOURCE_USER_AGENT_CONTACT in .env (a URL or mailbox site owners can "
            "use to reach you); it goes in the user agent of every request.",
            err=True,
        )
        raise typer.Exit(1)


def make_today() -> date:
    return date.today()


def make_code_version() -> str | None:
    """The git commit of this code, recorded with every evaluation; None outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def get_thesis(path: Path) -> tuple[Thesis, str]:
    try:
        return load_thesis(path)
    except ThesisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def thesis_filters(
    thesis: Thesis, naics: str | None, state: str | None
) -> tuple[list[str], list[str]]:
    prefixes = [c.strip() for c in naics.split(",")] if naics else thesis.sectors.naics_prefixes
    states = [s.strip().upper() for s in state.split(",")] if state else thesis.geography.states
    return prefixes, states


def echo_per_state(per_state: dict[str, int]) -> None:
    if per_state:
        typer.echo("  per state: " + ", ".join(f"{k} {v}" for k, v in sorted(per_state.items())))


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
    """Stages that could influence labels (enrich, score, export, eval) only run once the labels
    dev/test split exists. Discovery, ingest, resolve and the labeling export may run before it
    (DESIGN.md §11.3)."""
    if settings.split_manifest_path.exists():
        return
    if settings.labels_path.exists():
        step = "run `dealsource labels split` first"
    else:
        step = (
            "run `dealsource labels export`, fill in the decision column, save the file as "
            f"{settings.labels_path}, then run `dealsource labels split`"
        )
    typer.echo(
        f"Refusing to run: no labels split found at {settings.split_manifest_path}. "
        f"Enrichment, scoring and evaluation only run after the split; {step}.",
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


@discover_app.command("sam")
def discover_sam(
    ctx: typer.Context,
    thesis_path: Annotated[
        Path, typer.Option("--thesis", help="Thesis YAML (NAICS prefixes and states)")
    ],
    month: Annotated[
        str | None, typer.Option(help="Extract month MM/YYYY (default: latest)")
    ] = None,
    file: Annotated[
        Path | None, typer.Option(help="Use a public monthly extract ZIP you downloaded yourself")
    ] = None,
    naics: Annotated[str | None, typer.Option(help="Override the thesis NAICS prefixes")] = None,
    state: Annotated[
        str | None, typer.Option(help="Override the thesis states, e.g. GA,NC")
    ] = None,
) -> None:
    """Candidates from the SAM.gov public monthly entity extract (one request per month)."""
    settings: Settings = ctx.obj
    thesis, thesis_hash = get_thesis(thesis_path)
    prefixes, states = thesis_filters(thesis, naics, state)
    conn = open_db(settings)
    today = make_today()
    params = {
        "thesis_sha256": thesis_hash,
        "naics": prefixes,
        "states": states,
        "month": month,
        "file": bool(file),
    }
    with db.record_run(conn, "discover_sam", params) as stats:
        try:
            if file is not None:
                zip_path, downloaded = file, False
            else:
                if not settings.sam_api_key:
                    raise SamError(
                        "Set SAM_API_KEY in .env (see README: 'Getting a SAM.gov API key'), "
                        "or pass --file with a public extract you downloaded"
                    )
                if month:
                    mm, _, yyyy = month.partition("/")
                    year, mon = int(yyyy), int(mm)
                else:
                    year, mon = latest_month(today)
                with make_http_client() as client:
                    zip_path, downloaded = download_extract(
                        conn,
                        client,
                        api_key=settings.sam_api_key,
                        dest_dir=settings.sam_dir,
                        year=year,
                        month=mon,
                        user_agent=user_agent(settings),
                        today=today,
                        daily_budget=settings.sam_daily_budget,
                    )
            source = SamExtractSource(zip_path, naics_prefixes=prefixes, states=states)
            counts = store_records(conn, source.iter_records())
        except (SamError, ValueError) as exc:
            stats["error"] = str(exc)
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        st = source.stats
        stats.update(
            counts,
            file=zip_path.name,
            downloaded=downloaded,
            scanned=st.scanned,
            kept=st.kept,
            with_website=st.with_website,
        )
    typer.echo(
        f"SAM.gov extract {zip_path.name} ({'downloaded now' if downloaded else 'already on disk'})"
    )
    typer.echo(
        f"Scanned {st.scanned:,} registrations: {st.kept:,} active in thesis NAICS/states "
        f"({st.with_website:,} with a website); skipped {st.inactive:,} inactive, "
        f"{st.not_public:,} not public, {st.excluded_entities:,} excluded, {st.dnb_era:,} D&B-era"
    )
    echo_per_state(st.per_state)
    typer.echo(
        f"Records: {counts['inserted']} new, {counts['updated']} updated, {counts['unchanged']} unchanged"
    )


@discover_app.command("usaspending")
def discover_usaspending(
    ctx: typer.Context,
    thesis_path: Annotated[
        Path, typer.Option("--thesis", help="Thesis YAML (NAICS prefixes and states)")
    ],
    fiscal_years: Annotated[int, typer.Option(help="How many recent federal fiscal years")] = 5,
    naics: Annotated[str | None, typer.Option(help="Override the thesis NAICS prefixes")] = None,
    state: Annotated[
        str | None, typer.Option(help="Override the thesis states, e.g. GA,NC")
    ] = None,
) -> None:
    """Candidates from USAspending.gov: federal contract recipients (no API key; no websites)."""
    settings: Settings = ctx.obj
    thesis, thesis_hash = get_thesis(thesis_path)
    prefixes, states = thesis_filters(thesis, naics, state)
    conn = open_db(settings)
    params = {
        "thesis_sha256": thesis_hash,
        "naics": prefixes,
        "states": states,
        "fiscal_years": fiscal_years,
    }
    with make_http_client() as client, db.record_run(conn, "discover_usaspending", params) as stats:
        http = CachedHttp(conn, client, user_agent(settings))
        source = UsaSpendingSource(
            http,
            naics_prefixes=prefixes,
            states=states,
            today=make_today(),
            fiscal_years=fiscal_years,
            clock=make_clock(),
        )
        try:
            counts = store_records(conn, source.iter_records())
        except UsaSpendingError as exc:
            stats["error"] = str(exc)
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        st = source.stats
        stats.update(
            counts, requests=st.requests, cache_hits=st.cache_hits, recipients=st.recipients
        )
    typer.echo(
        f"USAspending contracts {source.start}..{source.end}: {st.recipients:,} recipients "
        f"({st.requests} requests, {st.cache_hits} from cache)"
    )
    echo_per_state(st.per_state)
    if st.retries:
        typer.echo(f"Note: {st.retries} request(s) were retried after timeouts or server errors")
    if st.unknown_prefixes:
        typer.echo(
            f"Warning: no NAICS codes found under prefix(es) {', '.join(st.unknown_prefixes)}"
        )
    if st.truncated_states:
        typer.echo(f"Warning: results truncated for {', '.join(st.truncated_states)} (page limit)")
    typer.echo(
        f"Records: {counts['inserted']} new, {counts['updated']} updated, {counts['unchanged']} unchanged"
    )


@discover_app.command("osm")
def discover_osm(
    ctx: typer.Context,
    thesis_path: Annotated[Path, typer.Option("--thesis", help="Thesis YAML (states)")],
    state: Annotated[
        str | None, typer.Option(help="Override the thesis states, e.g. GA,NC")
    ] = None,
    refresh: Annotated[
        bool, typer.Option(help="Download fresh extracts even if some are on disk")
    ] = False,
) -> None:
    """Makers with a website from OpenStreetMap (Geofabrik state extracts; resumable downloads)."""
    settings: Settings = ctx.obj
    thesis, thesis_hash = get_thesis(thesis_path)
    _, states = thesis_filters(thesis, None, state)
    conn = open_db(settings)

    def progress(p) -> None:
        total = f"{p.total / 1e6:,.0f} MB" if p.total else "? MB"
        resumed = f" (resumed at {p.resumed_from / 1e6:,.0f} MB)" if p.resumed_from else ""
        typer.echo(f"  {p.state}: {p.done / 1e6:,.0f} / {total}{resumed}")

    params = {"thesis_sha256": thesis_hash, "states": states, "refresh": refresh}
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    with make_http_client() as client, db.record_run(conn, "discover_osm", params) as stats:
        for st in states:
            try:
                path, fresh = download_state(
                    client,
                    st,
                    settings.osm_dir,
                    user_agent=user_agent(settings),
                    refresh=refresh,
                    progress=progress,
                )
            except OsmDownloadError as exc:
                stats["error"] = str(exc)
                typer.echo(f"Error ({st}): {exc}", err=True)
                raise typer.Exit(1) from exc
            source = OsmSource(path, state=st)
            counts = store_records(conn, source.iter_records())
            for k in totals:
                totals[k] += counts[k]
            s = source.stats
            stats[st] = {"file": path.name, "kept": s.kept, "with_naics": s.with_naics}
            typer.echo(
                f"{st}: {path.name} ({'downloaded now' if fresh else 'on disk'}): {s.with_website:,} features "
                f"with a website -> {s.kept:,} makers ({s.with_naics:,} with a NAICS from tags); skipped "
                f"{s.skipped_chains:,} chains, {s.skipped_not_maker:,} non-makers, {s.skipped_no_name:,} unnamed"
            )
        stats.update(totals)
    typer.echo(
        f"Records: {totals['inserted']} new, {totals['updated']} updated, {totals['unchanged']} unchanged"
    )
    typer.echo("OSM data © OpenStreetMap contributors, ODbL.")


@app.command()
def resolve(
    ctx: typer.Context,
    review: Annotated[
        bool, typer.Option(help="Write possible matches to review/possible_matches.csv")
    ] = False,
) -> None:
    """Merge records that describe the same company (entity resolution)."""
    settings: Settings = ctx.obj
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
    require_contact(settings)
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


@labels_app.command("export")
def labels_export(
    ctx: typer.Context,
    thesis_path: Annotated[
        Path, typer.Option("--thesis", help="Thesis YAML (NAICS prefixes and states)")
    ],
    n: Annotated[int, typer.Option(help="How many companies to sample")] = DEFAULT_N,
    per_state_cap: Annotated[
        int | None, typer.Option(help="Max companies per state (default: 1.5x an even share)")
    ] = None,
    seed: Annotated[int, typer.Option(help="Random seed for the sample")] = DEFAULT_SEED,
    out: Annotated[
        Path | None, typer.Option(help="Output CSV (default: <data dir>/to_label.csv)")
    ] = None,
) -> None:
    """Write a sample of candidates to label: company_name, website, state and an empty decision.

    Nothing the pipeline infers is included, so labels aren't influenced by the tool. Prints
    counts only."""
    settings: Settings = ctx.obj
    thesis, thesis_hash = get_thesis(thesis_path)
    conn = open_db(settings)
    raw_n = conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
    last_resolve = conn.execute(
        "SELECT MAX(finished_at) FROM runs WHERE stage = 'resolve'"
    ).fetchone()[0]
    last_ingest = conn.execute("SELECT MAX(ingested_at) FROM raw_records").fetchone()[0]
    if raw_n and (last_resolve is None or (last_ingest and last_ingest > last_resolve)):
        typer.echo(
            "Refusing to export: new records since the last `dealsource resolve`; run it first.",
            err=True,
        )
        raise typer.Exit(1)
    out_path = out or settings.to_label_path
    params = {"thesis_sha256": thesis_hash, "n": n, "per_state_cap": per_state_cap, "seed": seed}
    with db.record_run(conn, "labels_export", params) as stats:
        try:
            result = export_for_labeling(
                conn,
                thesis,
                out_path=out_path,
                labels_path=settings.labels_path,
                n=n,
                per_state_cap=per_state_cap,
                seed=seed,
            )
        except (ExportRefused, ValueError) as exc:
            stats["error"] = str(exc)
            typer.echo(f"Refusing to export: {exc}", err=True)
            raise typer.Exit(1) from exc
        stats.update(eligible=result.eligible, sampled=result.sampled, per_state=result.per_state)
    typer.echo(
        f"Wrote {result.sampled} companies to {result.path} (columns: company_name, website, state, decision)"
    )
    typer.echo(
        f"  {result.in_thesis_states:,} in thesis states -> {result.eligible:,} eligible; skipped "
        f"{result.without_website:,} without a website, {result.naics_unknown:,} without NAICS, "
        f"{result.naics_outside_thesis:,} outside thesis NAICS, {result.already_labeled:,} already labeled"
    )
    typer.echo(f"  per-state cap {result.per_state_cap}")
    echo_per_state(result.per_state)
    if result.sampled < n:
        typer.echo(f"Note: only {result.sampled} of the requested {n} were available.")
    typer.echo(
        f"Next: fill in the decision column (pursue/pass), save as {settings.labels_path}, run `dealsource labels split`."
    )


@websites_app.command("find")
def websites_find(
    ctx: typer.Context,
    thesis_path: Annotated[
        Path, typer.Option("--thesis", help="Only companies in the thesis states and NAICS")
    ],
    limit: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Check only the next N unchecked companies (seeded sample, spread across states "
            "in proportion); a later run continues with the rest",
        ),
    ] = None,
    workers: Annotated[
        int,
        typer.Option(
            min=1,
            max=32,
            help="Companies checked in parallel (still one request at a time per site)",
        ),
    ] = 8,
    seed: Annotated[
        int, typer.Option(help="Seed for the sampling order (keep it the same across runs)")
    ] = website_finder.DEFAULT_SEED,
    retry_errors: Annotated[
        bool, typer.Option(help="Re-check companies whose check errored")
    ] = False,
) -> None:
    """Guess .com domains for companies without a website and keep only strictly verified ones.

    Resumable: each company's result is saved as soon as it is checked, and a rerun skips them."""
    settings: Settings = ctx.obj
    require_contact(settings)
    thesis, thesis_hash = get_thesis(thesis_path)
    conn = open_db(settings)
    targets = website_finder.targets(conn, thesis, seed=seed)

    def progress(st: website_finder.FinderStats) -> None:
        found = st.statuses.get("found", 0)
        typer.echo(
            f"  [{st.already_done + st.checked:,}/{st.total:,}] checked this run {st.checked:,}; "
            f"found {found:,}, not found {st.statuses.get('not_found', 0):,}, "
            f"too generic {st.statuses.get('too_generic', 0):,}, ambiguous {st.statuses.get('ambiguous', 0):,}, "
            f"errors {st.statuses.get('error', 0):,}"
        )

    clock = make_clock()
    params = {
        "thesis_sha256": thesis_hash,
        "limit": limit,
        "workers": workers,
        "seed": seed,
        "retry_errors": retry_errors,
    }
    gate = SiteGate()  # shared by all workers: robots.txt, per-host delays, one request per site
    worker_conns: list[sqlite3.Connection] = []
    fetchers: list[PoliteFetcher] = []
    try:
        with make_http_client() as client, db.record_run(conn, "websites_find", params) as stats:
            for _ in range(workers):
                wconn = db.connect(settings.db_path, check_same_thread=False)
                worker_conns.append(wconn)
                http = CachedHttp(wconn, client, user_agent(settings), cacheable=is_cacheable_page)
                fetchers.append(
                    PoliteFetcher(
                        http,
                        gate=gate,
                        clock=clock,
                        min_delay=settings.fetch_min_delay,
                        timeout=10.0,
                    )
                )
            typer.echo(
                f"{len(targets):,} companies without a website in thesis states/NAICS; "
                f"{workers} worker(s)"
            )
            result = website_finder.run_finder(
                conn,
                targets,
                fetchers=fetchers,
                resolver=make_resolver(),
                retry_errors=retry_errors,
                limit=limit,
                progress=progress,
            )
            if result.already_done:
                typer.echo(
                    f"Resumed: {result.already_done:,} were already checked and were skipped."
                )
            stats.update(
                total=result.total,
                already_done=result.already_done,
                checked=result.checked,
                statuses=result.statuses,
                requests=sum(fp.requests_made for fp in fetchers),
                cache_hits=sum(fp.cache_hits for fp in fetchers),
            )
    finally:
        for wconn in worker_conns:
            wconn.close()
    remaining = result.total - result.already_done - result.checked
    totals = dict(
        conn.execute("SELECT status, COUNT(*) FROM website_search GROUP BY status").fetchall()
    )
    typer.echo(
        "All runs so far: "
        + ", ".join(f"{k} {v:,}" for k, v in sorted(totals.items()))
        + f" | HTTP this run: {stats['requests']:,} requests, {stats['cache_hits']:,} from cache"
    )
    if remaining:
        typer.echo(f"{remaining:,} companies left; rerun the same command to continue.")
    typer.echo(
        "Next: `dealsource resolve`, then `dealsource websites sample` to hand-check matches."
    )


@websites_app.command("recheck")
def websites_recheck(ctx: typer.Context) -> None:
    """Re-verify found and ambiguous websites with the current rules, from the cache only.

    No network requests. Websites that no longer pass lose their record; run `resolve` next."""
    settings: Settings = ctx.obj
    conn = open_db(settings)
    with db.record_run(conn, "websites_recheck", {}) as stats:
        result = website_finder.recheck(conn)
        stats.update(vars(result))
    down = sum(result.downgraded.values())
    typer.echo(f"Rechecked {result.rechecked:,} results offline")
    typer.echo(
        f"  found, no longer accepted: {down:,}"
        + (
            " (" + ", ".join(f"now {k} {v:,}" for k, v in sorted(result.downgraded.items())) + ")"
            if down
            else ""
        )
    )
    typer.echo(
        f"  found, different domain: {result.domain_changed:,}; "
        f"confidence changed: {result.confidence_changed:,}; unchanged: {result.unchanged:,}"
    )
    typer.echo(
        f"  ambiguous, now found: {result.upgraded:,}; still ambiguous: {result.still_ambiguous:,}"
    )
    if result.not_replayable:
        typer.echo(
            f"  not replayable from the cache (left as they were): {result.not_replayable:,}"
        )
    typer.echo("Next: `dealsource resolve` so companies pick up the changes.")


@websites_app.command("refresh-sample")
def websites_refresh_sample(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="A sample CSV to refresh")
    ],
) -> None:
    """Rewrite a sample's evidence columns from the current database (counts only here).

    Rows and the 'correct' column are kept; only confidence, match types, page title and
    location snippet are replaced."""
    settings: Settings = ctx.obj
    conn = open_db(settings)
    try:
        result = refresh_sample(conn, path)
    except SampleRefused as exc:
        typer.echo(f"Refusing: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Refreshed {path}: {result.rows} rows, {result.changed} with changed evidence, "
        f"{result.no_longer_found} no longer found"
    )


@websites_app.command("sample")
def websites_sample(
    ctx: typer.Context,
    n: Annotated[int, typer.Option(help="How many found websites to sample")] = 30,
    seed: Annotated[int, typer.Option(help="Random seed")] = 20260927,
    out: Annotated[
        Path | None, typer.Option(help="Output CSV (default: <data dir>/review/website_sample.csv)")
    ] = None,
    exclude: Annotated[
        list[Path] | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="An earlier sample CSV whose companies to leave out (repeatable)",
        ),
    ] = None,
) -> None:
    """Write a random sample of found websites to a private CSV for hand-checking (counts only here)."""
    settings: Settings = ctx.obj
    conn = open_db(settings)
    out_path = out or settings.review_dir / "website_sample.csv"
    try:
        result = write_sample(conn, out_path, n=n, seed=seed, exclude=exclude)
    except SampleRefused as exc:
        typer.echo(f"Refusing: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Wrote {result.sampled} of {result.found:,} found websites to {result.path}")
    if exclude:
        typer.echo(f"  left out {result.excluded} found websites that were in earlier samples")
    typer.echo(
        "  by confidence: " + ", ".join(f"{k}: {v}" for k, v in result.by_confidence.items())
    )
    typer.echo("  Fill in the 'correct' column (y/n) to measure precision before labeling.")


@app.command()
def score(
    ctx: typer.Context,
    thesis_path: Annotated[Path, typer.Option("--thesis", help="Thesis YAML")],
) -> None:
    """Score every company against the thesis (deterministic rules; never reads labels)."""
    settings: Settings = ctx.obj
    require_split(settings)
    thesis, thesis_hash = get_thesis(thesis_path)
    conn = open_db(settings)
    with db.record_run(conn, "score", {"thesis_sha256": thesis_hash}) as stats:
        result = score_all(conn, thesis, thesis_hash)
        stats.update(vars(result))
    typer.echo(
        f"Scored {result.scored:,} companies: {result.shortlisted:,} at or above the shortlist "
        f"threshold ({thesis.shortlist_threshold:g}), {result.excluded:,} excluded"
    )
    typer.echo("  confidence: " + ", ".join(f"{k} {v:,}" for k, v in result.confidence.items()))
    if result.exclusions:
        typer.echo("  exclusions: " + ", ".join(f"{k} {v:,}" for k, v in result.exclusions.items()))


@app.command("export")
def export_cmd(
    ctx: typer.Context,
    thesis_path: Annotated[Path, typer.Option("--thesis", help="Thesis YAML (as scored)")],
    out: Annotated[
        Path | None,
        typer.Option(help="Output CSV (default: <data dir>/exports/<thesis>_<date>_<run>.csv)"),
    ] = None,
) -> None:
    """Write the ranked, explained CSV of the latest score run for this thesis (counts only here)."""
    settings: Settings = ctx.obj
    require_split(settings)
    thesis, thesis_hash = get_thesis(thesis_path)
    conn = open_db(settings)
    with db.record_run(conn, "export", {"thesis_sha256": thesis_hash}) as stats:
        if out is None:
            run = latest_score_run(conn, thesis_hash) or "none"
            slug = re.sub(r"[^a-z0-9]+", "-", thesis_path.stem.lower()).strip("-") or "thesis"
            out = settings.exports_dir / f"{slug}_{make_today().isoformat()}_{run[:8]}.csv"
        try:
            result = export_ranked(conn, thesis, thesis_hash, out_path=out)
        except RankedExportRefused as exc:
            stats["error"] = str(exc)
            typer.echo(f"Refusing to export: {exc}", err=True)
            raise typer.Exit(1) from exc
        stats.update(rows=result.rows, excluded=result.excluded, market_rows=result.market_rows)
    typer.echo(
        f"Wrote {result.rows:,} companies to {result.path} ({result.shortlisted:,} shortlisted, "
        f"{result.excluded:,} excluded at the bottom)"
    )
    if result.market_path:
        typer.echo(f"  market stats: {result.market_rows:,} rows in {result.market_path}")
    else:
        typer.echo("  market stats: none for the thesis sectors and states (run `ingest cbp`)")


@app.command("eval")
def eval_cmd(
    ctx: typer.Context,
    thesis_path: Annotated[Path, typer.Option("--thesis", help="Thesis YAML (as scored)")],
    set_: Annotated[
        str, typer.Option("--set", help="dev (default, for tuning) or test (once, needs --final)")
    ] = "dev",
    final: Annotated[
        bool, typer.Option(help="Required with --set test: the one-time held-out evaluation")
    ] = False,
    bootstrap_seed: Annotated[
        int, typer.Option(help="Seed for the bootstrap intervals")
    ] = DEFAULT_BOOTSTRAP_SEED,
) -> None:
    """Evaluate the latest score run against the labels. Aggregate metrics only; company-level
    detail goes to a file under <data dir>/evals/."""
    settings: Settings = ctx.obj
    require_split(settings)
    if set_ not in ("dev", TEST):
        raise typer.BadParameter("--set must be dev or test")
    if final and set_ != TEST:
        raise typer.BadParameter("--final only applies to --set test")
    if set_ == TEST and not final:
        typer.echo(
            "Refusing: the held-out test set is evaluated once, at the end. Tune on the dev set; "
            "when you are done, run `dealsource eval --set test --final`.",
            err=True,
        )
        raise typer.Exit(2)
    if set_ == TEST:
        # Checked before any test label is read.
        logged = read_test_log(settings.test_eval_log_path)
        if logged:
            typer.echo(
                f"Refusing: the test set was already evaluated ({logged[0]['created_at']}); it runs "
                "once. The recorded metrics:",
                err=True,
            )
            for line in logged[0]["summary_lines"]:
                typer.echo(f"  {line}")
            raise typer.Exit(1)
    thesis, thesis_hash = get_thesis(thesis_path)
    conn = open_db(settings)
    code_version = make_code_version()
    params = {"thesis_sha256": thesis_hash, "set": set_, "bootstrap_seed": bootstrap_seed}
    with db.record_run(conn, "eval", params) as stats:
        try:
            result = evaluate(
                conn,
                thesis,
                thesis_hash,
                split=set_,
                labels_path=settings.labels_path,
                manifest_path=settings.split_manifest_path,
                evals_dir=settings.evals_dir,
                code_version=code_version,
                bootstrap_seed=bootstrap_seed,
            )
        except (EvalRefused, LabelsError) as exc:
            stats["error"] = str(exc)
            typer.echo(f"Refusing to evaluate: {exc}", err=True)
            raise typer.Exit(1) from exc
        stats.update(eval_id=result.eval_id, matched=result.matched, unmatched=result.unmatched)
        if set_ == TEST:
            append_test_log(settings.test_eval_log_path, result, thesis_hash, code_version)
    for line in result.summary_lines:
        typer.echo(line)
    typer.echo(f"company-level report: {result.report_path}")
