# Deal-Sourcing Engine: Design

Status: **approved design (rev 3)**. Nothing here is implemented yet. Decisions made during
review are listed in §17.

## 1. Purpose

Take an investment thesis (sectors, size range, geography, exclusions) and output a ranked,
explained list of acquisition targets. Everything runs on one analyst machine:

- storage is a local SQLite file
- LLM inference runs locally through Ollama, so no company data goes to a third-party API
- the only outbound network traffic is (a) the configured data sources, such as the Census API,
  and (b) polite fetches of the target companies' own public websites

Scoring quality is measured against the analyst's own labels, using a dev/test split that is
fixed before any pipeline run (§11).

Non-goals: a CRM, a UI, contact discovery, or any collection of personal contact information.

## 2. Pipeline overview

```
 sources ──► ingest ──► raw_records ──► resolve ──► companies ──► enrich ──► enrichments
 (CSV, CBP, …)                          (entity                  (fetch site,
                                         resolution)              extract text,
                                                                  local LLM)
                                                                      │
 thesis.yaml ─────────────────────────────────────────────► score ◄──┘
                                                               │
                                                            export ──► ranked CSV
                                                               │
 labels.csv ──► labels split (once, before any run) ──────► eval ──► aggregate metrics (terminal)
                                                                  └─► company-level file (private/)
```

Each stage is a separate CLI command that reads from and writes to SQLite. Stages are
**idempotent** and **incremental**: rerunning a stage only does work for new or changed
inputs, and anything fetched or inferred before is served from cache. `dealsource run`
chains ingest → export.

## 3. Repository layout

```
deal-sourcing-engine/
├── CLAUDE.md
├── DESIGN.md
├── README.md
├── pyproject.toml
├── .env.example                  # documents every env var, with no real values
├── .githooks/
│   └── pre-commit                # blocks staging of private/, .env*, *.db (see §4.2)
├── examples/
│   ├── thesis.example.yaml       # the only thesis that ships publicly
│   └── companies.example.csv     # synthetic/fictional companies
├── src/dealsource/
│   ├── cli.py                    # Typer app: ingest / resolve / enrich / score / export / run /
│   │                             #            labels / eval / stats
│   ├── config.py                 # settings from .env + defaults; path resolution for private/
│   ├── db.py                     # SQLite connection, migrations, small repository helpers
│   ├── models.py                 # Pydantic models shared across stages
│   ├── sources/
│   │   ├── base.py               # CompanySource / MarketDataSource protocols + registry
│   │   ├── csv_source.py
│   │   └── census_cbp.py
│   ├── resolve/
│   │   ├── normalize.py          # name + domain normalization
│   │   └── matcher.py            # blocking, fuzzy scoring, clustering
│   ├── enrich/
│   │   ├── fetcher.py            # robots.txt, rate limiting, HTTP cache
│   │   ├── extract.py            # HTML → clean text, page selection, contact-info scrubbing
│   │   ├── mask.py               # person-name masking for evidence quotes / summaries
│   │   ├── prompts.py            # versioned prompt templates
│   │   └── schema.py             # Pydantic model for the LLM's extraction output
│   ├── llm/
│   │   ├── base.py               # LLMBackend protocol + LLMResult
│   │   ├── ollama.py             # the only implemented backend
│   │   └── cache.py              # LLM response cache + call metrics
│   ├── score/
│   │   ├── thesis.py             # thesis YAML schema (Pydantic) + loader
│   │   └── scorer.py             # component scores, exclusions, written reasons
│   ├── eval/
│   │   ├── labels.py             # load + validate labels.csv, match labels to companies
│   │   ├── split.py              # one-time grouped, stratified dev/test split + manifest
│   │   └── metrics.py            # aggregate metrics; company-level report writer
│   └── export/
│       └── csv_export.py
├── tests/
│   ├── conftest.py               # tmp DB, fake LLM backend, mock HTTP transport, network block
│   ├── fixtures/                 # synthetic HTML, CBP JSON, CSVs, labels; no real firm data
│   └── test_*.py
└── private/                      # gitignored: real thesis, target lists, labels, DB, exports
```

## 4. Configuration and confidentiality

### 4.1 What goes where

| Item | Location | Committed? |
|---|---|---|
| Example thesis, synthetic sample CSV | `examples/` | yes |
| Real thesis(es) | `private/theses/*.yaml` | **no** |
| Real target lists / CRM exports | `private/inputs/` | **no** |
| Analyst labels | `private/labels.csv` | **no** |
| Dev/test split record (manifest + assignments) | `private/labels_split.json` | **no** |
| Evaluation log and company-level eval reports | `private/evals/` | **no** |
| SQLite DB (includes all caches) | `private/dealsource.db` | **no** (also `*.db` is ignored) |
| Ranked CSV exports | `private/exports/` | **no** |
| Entity-resolution overrides | `private/overrides.yaml` | **no** |
| Secrets (Census API key), user-agent contact, Ollama host/model | `.env` | **no** |
| Documentation of env vars | `.env.example` | yes |

### 4.2 Safe defaults

- Every path setting defaults to somewhere under `private/`. If `private/` is missing, the CLI
  creates it instead of falling back to the repo root, so outputs can't land in tracked
  directories by accident.
- The `--thesis` flag is required for `score`, `run` and `eval`. Nothing silently falls back
  to the example thesis, so a run on the real thesis is always deliberate.
- Logs never print thesis contents, company lists or label contents at any level. Logs go to
  `private/logs/`.
- `eval` prints aggregate metrics only. Anything at company level goes to a file under
  `private/evals/` (§11.5).
- **Pre-commit guard**: `.githooks/pre-commit` rejects any staged path under `private/`, any
  `.env*` file other than `.env.example`, and any `*.db`/`*.sqlite*` file. This backs up
  `.gitignore` and catches `git add -f`. Git doesn't enable tracked hooks automatically, so
  every clone has to run `git config core.hooksPath .githooks` once. `git commit --no-verify`
  bypasses the hook, so it's a guard against mistakes, not a security boundary.

### 4.3 Settings (`.env`)

```
DEALSOURCE_DATA_DIR=private
CENSUS_API_KEY=                     # optional; CBP works without a key at low volume
DEALSOURCE_USER_AGENT_CONTACT=      # URL or mailbox that site owners can contact; required for enrich
OLLAMA_HOST=http://127.0.0.1:11434
LLM_BACKEND=ollama
LLM_MODEL=qwen2.5:7b
FETCH_MIN_DELAY_SECONDS=2.0         # per-host delay
FETCH_MAX_PAGES_PER_SITE=5
```

## 5. Storage (SQLite)

One file, WAL mode, schema managed by numbered SQL migrations in `db.py`
(a `schema_version` table; no ORM). Main tables:

| Table | Purpose |
|---|---|
| `raw_records` | One row per record ingested from a source: `source`, `source_record_id`, `payload_json`, `payload_hash`, `ingested_at`. Unique on (`source`, `source_record_id`). |
| `companies` | Resolved entities: `id`, `canonical_name`, `domain`, `city`, `state`, `country`, `naics_codes`, `employee_count`, `employee_count_source`, `revenue_usd_m` (CSV-supplied only), `created_at`. |
| `company_records` | Mapping from raw record to company, plus `match_method`, `match_score`, and `evidence_json`. |
| `market_stats` | CBP data: `naics`, `geo_level`, `geo_code`, `year`, `establishments`, `employees`, `annual_payroll`, and establishment counts by size class. |
| `http_cache` | `url`, `final_url`, `status`, `headers_json`, `body` (compressed), `fetched_at`, `content_hash`. Also holds robots.txt responses. |
| `llm_cache` | `cache_key` (PK), `backend`, `model`, `prompt_version`, `schema_hash`, `response_json`, `created_at`. |
| `llm_calls` | One row per LLM request, including cache hits: `company_id`, `stage`, `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `cache_hit`, `ok`, `error`, `created_at`. |
| `enrichments` | Latest structured extraction per company (after masking): `company_id`, `extraction_json`, `pages_used_json`, `llm_cache_key`, `mask_version`, `status`, `updated_at`. |
| `scores` | `run_id`, `company_id`, `thesis_hash`, `total`, `components_json`, `excluded`, `reason`, `confidence`. |
| `labels` | A working copy of the labels, loaded from `private/labels.csv` and `private/labels_split.json`: `label_key`, `decision`, `split` (`dev`/`test`), `matched_company_id`, `match_method`. Rebuilt from the files on every `eval`. The files are the source of truth, never this table. |
| `eval_runs` | `eval_id`, `split`, `thesis_hash`, `code_version` (git commit), `metrics_json`, `report_path`, `created_at`. |
| `runs` | `run_id`, `stage`, `started_at`, `finished_at`, `params_json`, `stats_json`. |

Since the DB holds cached page bodies, company data, labels and scores, it is confidential and
lives under `private/`. The DB can be treated as disposable, since it can be rebuilt from
inputs and caches. The dev/test split and the record of evaluations on the test set must
survive a rebuild, so they live in files (§11.3, §11.4), not only in the DB.

## 6. Stage 1: Ingest (pluggable sources)

There are two kinds of source, because they produce different things:

```python
class CompanySource(Protocol):
    name: str                                   # "csv", later "state_registry", ...
    def iter_records(self) -> Iterator[RawCompanyRecord]: ...

class MarketDataSource(Protocol):
    name: str                                   # "census_cbp"
    def fetch(self, naics: list[str], geos: list[GeoSpec], year: int) -> Iterator[MarketStat]: ...
```

`RawCompanyRecord` is a Pydantic model with typed optional fields (`name`, `website`, `city`,
`state`, `country`, `naics`, `employees`, `revenue_usd_m`, `description`) plus `extra: dict`
for columns specific to one source. Sources register themselves in a registry keyed by
`name`, so adding a source means one new module and one registry entry, with no changes to
later stages.

### 6.1 CSV source
- The column mapping is configurable (`--map name=Company,website=URL,revenue_usd_m=Rev`) with
  sensible auto-detection of common headers.
- **Revenue comes only from CSV.** If a CSV supplies a revenue column, it's stored in
  `revenue_usd_m` (the unit is declared in the mapping, e.g. `--revenue-unit usd|usd_k|usd_m`).
  No other source, including the LLM, produces revenue.
- It drops and warns about columns that look like personal contact info (email, phone,
  contact name, LinkedIn URLs), so they never enter the DB. See §12.
- `source_record_id` is the value of a configured ID column, or else a hash of the normalized row.

### 6.2 Census County Business Patterns source
**CBP is aggregate market data, not a company list.** It returns counts of establishments,
employees and payroll by NAICS code × geography (US, state, county, metro), including
establishment counts by employee-size class. It therefore feeds `market_stats`, not
`raw_records`, and is used for market sizing per thesis sector and geography, reported
alongside the export. **In v1, CBP data is report-only: it never affects scores.**

Implementation: `GET https://api.census.gov/data/{year}/cbp` with `get=ESTAB,EMP,PAYANN,...`,
`for=state:*` or `county:*&in=state:XX`, and `NAICS2017=...`. The field names and NAICS
vintage vary by year, so the adapter keeps a small per-year mapping table. Responses go
through the same HTTP cache as website fetches, and the Census API key comes from `.env` when
set.

## 7. Stage 2: Resolve (entity resolution)

Goal: merge the raw records that refer to the same real company into one `companies` row,
without merging distinct companies.

### 7.1 Normalization
- **Name key**: Unicode NFKC, lowercase, `&`→`and`, strip punctuation, drop legal suffixes
  (inc, incorporated, llc, l.l.c., corp, corporation, co, company, ltd, lp, llp, plc, pllc,
  holdings, group†), and collapse whitespace. Keep both the full normalized name and the
  suffix-stripped key. (†"group" and "holdings" are stripped only for matching, never for
  display.)
- **Domain key**: parse the URL, lowercase, strip scheme, `www.`, path and port, and reduce to
  the registrable domain (eTLD+1) using `tldextract` **with its bundled suffix list and network
  refresh disabled**, which keeps tests offline and runs deterministic. Generic domains
  (facebook.com, linkedin.com, gmail.com, …) are blocklisted so they can't act as a shared
  key. On shared-hosting platforms (wixsite.com, squarespace.com, …) the full host is kept
  instead of eTLD+1.

The same normalization is used to key labels (§11.2).

### 7.2 Matching
1. **Blocking**, to avoid O(n²) comparisons: candidate pairs share a domain key, or share the
   first name-key token plus state, or a sorted-token prefix.
2. **Pair scoring** (`rapidfuzz`):
   - same domain key → match (strong evidence), unless the names are wildly different
     (score < 50), in which case the pair is flagged for review
   - different, non-empty domain keys → **never** auto-merge (a hard negative)
   - otherwise use `token_sort_ratio` and `partial_ratio` on the name keys, plus agreement on
     city/state. Auto-merge at ≥ 93 with a location match, flag 85–93 as a "possible match",
     and treat anything lower as distinct. The thresholds live in config.
3. **Clustering**: union-find over the auto-merge edges. A cluster can't contain two
   different domain keys; if it would, the weakest edge is cut.
4. **Canonical record**: prefer values from the most trusted source (configurable source
   priority), then the most complete record.
5. **Overrides**: `private/overrides.yaml` holds analyst-forced `merge` and `split` pairs,
   which are applied after automatic matching. `dealsource resolve --review` writes the
   "possible match" pairs to `private/review/possible_matches.csv` and prints only the count.

Every merge stores its evidence (method, scores, fields that agreed) in `company_records`.

## 8. Stage 3: Enrich

### 8.1 Polite fetching
- **User agent**: `dealsource/<version> (+<DEALSOURCE_USER_AGENT_CONTACT>)`. `enrich` refuses
  to run without a contact value set.
- **robots.txt**: fetched once per host (cached, 24 h TTL) and checked with
  `urllib.robotparser` for our UA before every request. A disallowed URL is skipped and
  recorded as `blocked_by_robots`. `Crawl-delay` is honoured when it's longer than our
  default.
- **Rate limiting**: a per-host minimum delay (default 2 s), a global concurrency cap
  (default 4 hosts in parallel, one request at a time per host), exponential backoff on
  429/503, and `Retry-After` is respected.
- **Scope**: only the company's own registrable domain. Homepage first, then up to
  `FETCH_MAX_PAGES_PER_SITE` same-domain links whose path or anchor text matches
  about/company/history/products/services/solutions/industries/markets/capabilities/facilities/locations.
  Pages matching careers, contact, team, leadership, staff, people, privacy, login and cart
  are skipped. No forms, no JS rendering, no following off-domain links.
- **Limits**: 2 MB max body, HTML content types only, 15 s timeout, redirects followed only
  within the same registrable domain (cross-domain redirects are recorded, e.g. as a sign of
  an acquisition, but not followed).
- **Cache**: every response (including 404s and robots.txt) goes into `http_cache`, keyed by
  normalized URL. Reruns read from the cache. `--refresh-older-than 30d` re-fetches stale
  entries. HTTP is done with `httpx`, which lets tests inject a `MockTransport`.

### 8.2 Text extraction
- HTML → text with `selectolax`: remove script, style, nav,
  footer and form elements, keep headings and paragraphs, and collapse whitespace.
- **Contact-info scrubbing** before storage and before the LLM: regex-remove email addresses,
  phone numbers and street addresses in contact blocks. The scrubbed text is what gets hashed
  and sent to the LLM, so the cache never holds contact details either.
- Pages are concatenated with `### <path>` headers and truncated to a token budget
  (default about 6k tokens, configurable) with a per-page cap, so one long page can't crowd
  out the others.
- Person names are **not** removed from the text sent to the LLM. It stays local, and phrases
  like "founded by Jane Doe and her sons" are what the ownership signals depend on. Names are
  masked in what the LLM *outputs* (§8.4).

### 8.3 LLM extraction

**Backend interface** (`llm/base.py`):

```python
@dataclass
class LLMResult:
    data: dict            # parsed JSON matching the schema
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cache_hit: bool

class LLMBackend(Protocol):
    name: str
    def generate_structured(self, *, system: str, user: str,
                            schema: dict, options: GenOptions) -> LLMResult: ...
```

**Ollama backend**: `POST {OLLAMA_HOST}/api/chat` with `stream: false`,
`format: <JSON schema>`, `options: {temperature: 0, seed: 0, num_ctx: 8192}`. Tokens come from
`prompt_eval_count` and `eval_count`. Latency is measured wall-clock on our side (and
`total_duration` is recorded too). **Locality guard**: at startup, the backend refuses an
`OLLAMA_HOST` that doesn't resolve to a loopback address, unless `DEALSOURCE_ALLOW_REMOTE_LLM=1`
is set explicitly. This enforces the "no data leaves the machine" rule instead of just
documenting it.

**Extraction schema** (`enrich/schema.py`, Pydantic → JSON schema):

```python
class Extraction(BaseModel):
    summary: str                                  # ≤ 2 sentences, what the company does
    product_lines: list[str]
    end_markets: list[str]                        # industries/customers served
    business_model: Literal["manufacturer","distributor","services","software","mixed","unknown"]
    size_signals: SizeSignals
    ownership: OwnershipSignals
    evidence: list[Evidence]                      # short quotes backing key claims (page path + quote)

class SizeSignals(BaseModel):
    employee_count: int | None                    # only if the site states a number/range
    employee_count_quote: str | None
    facility_count: int | None                    # plants, branches, service locations
    facility_sqft_total: int | None
    founded_year: int | None
    # deliberately no revenue field: revenue comes only from CSV inputs (§6.1)

class OwnershipSignals(BaseModel):
    founder_led: Literal["yes","no","unknown"]
    family_owned: Literal["yes","no","unknown"]
    generation: int | None                        # "third-generation family business" → 3
    pe_or_strategic_backed: Literal["yes","no","unknown"]   # "a portfolio company of…", "part of …"
    publicly_traded: Literal["yes","no","unknown"]
```

The schema has **no fields for people's names, titles, emails or phone numbers**. Ownership is
captured as categorical signals backed by an evidence quote.

**Validation**: the response is parsed and validated with Pydantic. On failure there is one
retry with a "fix to match schema" nudge. After that, the failure is recorded in
`enrichments.status` and `llm_calls.error`, and the pipeline moves on.

**Cache key**: `sha256(backend, model, model_digest?, prompt_version, schema_hash, options, scrubbed_input_text)`.
Changing the model, prompt or schema invalidates the cache naturally. Prompts carry an
explicit `PROMPT_VERSION` constant.

**Metrics**: every call, cache hits included, writes an `llm_calls` row. `dealsource stats`
reports p50/p95 latency, tokens in/out per company, total tokens, cache hit rate and failure
rate.

### 8.4 Person-name masking (evidence quotes and summary)

Person names in `evidence[].quote`, `employee_count_quote` and `summary` are replaced with
`[PERSON]` before anything is written to `enrichments`, exports or eval reports. For example,
"founded by John Smith in 1985" becomes "founded by [PERSON] in 1985". There are two layers:

1. **Prompt instruction**: the LLM is told to write `[PERSON]` in place of any individual's
   name in quotes and in the summary.
2. **Deterministic post-pass** (`enrich/mask.py`, which is the layer that is guaranteed):
   - honorific + capitalized tokens (`Mr./Mrs./Ms./Dr.` + Name)
   - capitalized 2–3-token sequences next to person cues ("founded by", "started by",
     "owned by", "led by", "son/daughter/wife/husband of", "CEO", "President", "Owner",
     "Founder", "Principal", "and his/her …"), including possessives ("John Smith's")
   - `First Last` sequences whose first token is in a bundled list of common given names
     (shipped as a static data file, with no network)
   - **Protected spans**: tokens from the company's own canonical name and known place names
     (US states, plus the company's city) are never masked, so "Smith Brothers Machining" and
     "Smith & Sons" survive.

The raw LLM response in `llm_cache` is kept unmasked so that masking can be changed and
re-applied without re-running inference. That cache lives only in the private DB and is never
exported. Masking is applied on write to `enrichments` and carries a `MASK_VERSION`, so
`dealsource enrich --remask` updates all rows from the cache. The masker is best-effort and
biased toward over-masking. It's tested against a synthetic table of positive and negative
cases.

## 9. Stage 4: Score

### 9.1 Thesis YAML (`examples/thesis.example.yaml`)

```yaml
name: "Example: Niche Industrial Services, Southeast US"
sectors:
  naics_prefixes: ["3323", "3327", "8113"]       # match company NAICS if known
  include_keywords: ["precision machining", "industrial maintenance", "fabrication"]
  end_markets: ["aerospace", "medical devices", "food processing"]
size:
  employees: {min: 20, max: 250}
  facilities: {min: 1, max: 8}                   # optional secondary signal
  revenue_usd_m: {min: 5, max: 75}               # applied ONLY when a CSV supplied revenue
geography:
  countries: ["US"]
  states: ["GA", "FL", "NC", "SC", "TN", "AL"]
ownership:
  prefer: ["founder_led", "family_owned"]
exclusions:
  keywords: ["franchise", "staffing agency"]
  ownership: ["pe_or_strategic_backed", "publicly_traded"]
  domains: []                                    # e.g. existing portfolio, known passes
weights:
  sector: 0.40
  size: 0.20
  geography: 0.20
  ownership: 0.20
shortlist_threshold: 60                          # score at or above = "shortlisted"; used by eval
```

It's validated by a Pydantic model with clear error messages. The thesis hash is stored with
every score, so results always trace back to the exact thesis that produced them.

### 9.2 Scoring logic
Scoring is deterministic and rule-based, with no LLM involved. That keeps it reproducible,
fast and auditable. **Scoring never reads labels.**

- **Exclusions first**: any hit means `excluded = true`, score 0, and a reason naming the rule
  and the evidence (e.g. "Excluded: ownership signal pe_or_strategic_backed — 'a portfolio
  company of X Capital' (/about)"). Excluded rows are still exported, at the bottom, so
  nothing silently disappears.
- **Components**, each 0–1:
  - *sector*: NAICS prefix match (strong), keyword / end-market overlap against the extracted
    product lines, end markets and summary (graded)
  - *size*: see §9.3
  - *geography*: allowed state = 1, allowed country but other state = partial, unknown = 0.5
  - *ownership*: preferred signal present = 1, unknown = 0.5, "no" = 0.25
- `total = 100 × Σ weightᵢ × componentᵢ`.
- **Confidence** (high/medium/low) reflects how many components rest on actual evidence as
  opposed to "unknown" defaults. It's reported separately so a thinly documented company
  can't outrank a well-documented one without that being visible.
- **Written reason**: a template-built sentence or two per company, listing the strongest
  positive factors, the main gaps and any unknowns. For example: "Strong sector fit: precision
  machining for aerospace and medical end markets (NAICS 332710). In-geography (GA).
  ~60 employees (site: 'team of 60'); 2 facilities. Family-owned, 2nd generation."

### 9.3 Size scoring: employee and facility signals; revenue only from CSV

The size component uses signals in this order of precedence:

1. **Employee count**. Use the CSV-supplied value if there is one, otherwise the employee
   count stated on the website (`size_signals.employee_count`). Inside `size.employees` = 1,
   with a soft linear decay to 0 at 50% beyond either bound. The source is recorded in
   `companies.employee_count_source` and cited in the reason.
2. **Facility signals**, used only when no employee count exists: `facility_count` against
   `size.facilities`, with square footage as a tiebreak hint. Because this is a weaker proxy,
   the component is capped at 0.8 and confidence is lowered.
3. **Revenue**, used **only if a CSV supplied `revenue_usd_m` and the thesis sets
   `size.revenue_usd_m`**. Then the size component is the mean of the revenue fit and the
   employee/facility fit. Missing revenue is never penalized and is never listed as
   "unknown" in the reason, because it isn't expected to be known.
4. No signal at all gives 0.5 (neutral), marked as unknown in the reason and in confidence.

The LLM never estimates revenue, and employee counts are never converted into revenue
estimates.

## 10. Stage 5: Export

`dealsource export --thesis private/theses/x.yaml --out private/exports/…csv`

Columns: `rank, score, confidence, excluded, company, domain, city, state, naics,
employees, employees_source, facilities, revenue_usd_m (blank unless CSV-supplied),
business_model, product_lines, end_markets, founder_led, family_owned, pe_backed, reason,
sources, company_id, scored_at, thesis_name`. List fields are joined with `; `. It's UTF-8
with a BOM, so it opens cleanly in Excel. The export also writes a small `*.market.csv`
sidecar with CBP stats for the thesis sectors and geographies. The export never contains
label or split information.

## 11. Evaluation against analyst labels

### 11.1 Labels file

`private/labels.csv` is written by the analyst. Expected columns (any different column names
can be mapped with `--map`, as for CSV ingest):

| Column | Required | Notes |
|---|---|---|
| `company_name` | yes | |
| `website` | strongly recommended | primary match key |
| `state` | optional | helps name-based matching |
| `decision` | yes | **binary**: the analyst's decision on the company (positive = would pursue, negative = pass). Accepted values: `1/0`, `yes/no`, `pursue/pass`, `target/pass` (case-insensitive, and the mapping is configurable). Any other value is an error. |
| `notes` | optional | never loaded; ignored by the code |

Any contact-info-looking columns are rejected, the same as in CSV ingest (§6.1). Labels are
binary only; graded labels are out of scope for v1. Elsewhere in this document, "label" means
this binary decision.

**The file doesn't exist yet.** The analyst creates it before the first pipeline run (§11.3).
The repo ships only a synthetic `tests/fixtures/labels.csv`.

### 11.2 Label keys and grouping

Each label row gets a stable **label key** that doesn't depend on pipeline state
(`company_id` changes when resolution changes, so it can't be used):

- `d:<domain_key>` when a website is present, else
- `n:<name_key>|<state>` using the §7.1 normalization.

Rows with the same key are one **group** and always land in the same split. That prevents
leakage from duplicate rows for one company. A group with conflicting decisions is an error that
the analyst has to fix; the split command refuses to proceed and reports only the *count* of
conflicts, with details written to `private/evals/label_conflicts.csv`.

### 11.3 One-time split, before any pipeline run

Order of operations:

1. The analyst creates `private/labels.csv`.
2. The analyst runs `dealsource labels split` once. The defaults are the approved parameters:
   `--test-fraction 0.3 --seed 20260927`.
3. Only then can the pipeline run.

`labels split` behaviour:

- **Refuses if `private/labels.csv` doesn't exist**, with a message saying to create it first.
  It writes nothing in that case, so running it early is harmless.
- **Refuses if `private/labels_split.json` already exists**, i.e. a second run. There is no
  `--force`. Redoing a split means deleting the file by hand, which is deliberately
  inconvenient. The refusal prints the existing manifest's aggregate counts and creation time.
- **Refuses if the DB already has any `runs` rows**, so pipeline output can't influence the
  split. It records `db_had_runs: false` in the manifest.
- **Stratified by decision, grouped by label key**: groups are first partitioned by decision
  (positive / negative). Within each stratum the sorted group keys are shuffled with
  `random.Random(seed)` (sorting first means file row order has no effect), and
  `round(0.3 × n_stratum)` groups go to `test`. Each stratum gets at least one test group when
  it has at least two groups. The dev and test sets therefore have the same pursue/pass
  balance as the full file, up to rounding.
- **Written once** to `private/labels_split.json`, which is then set read-only
  (`chmod 0444`):
  ```json
  {
    "version": 1,
    "created_at": "…",
    "seed": 20260927,
    "test_fraction": 0.3,
    "stratified_by": "decision",
    "labels_sha256": "<hash of labels.csv at split time>",
    "db_had_runs": false,
    "rule_for_new_keys": "sha256(seed:key) < test_fraction",
    "counts": {"dev": {"pursue": …, "pass": …}, "test": {"pursue": …, "pass": …}},
    "assignments": {"d:example.com": "dev", "n:acme tool|ga": "test", …}
  }
  ```
- The command prints aggregate counts only (per split × decision), never keys or names.

**Pipeline gating**: `ingest`, `resolve`, `enrich`, `score`, `run` and `eval` refuse to start
while `private/labels_split.json` is missing. That covers both the case where `labels.csv`
hasn't been created yet and the case where it exists but hasn't been split. The error message
gives the next step. The check applies to the configured data dir, so tests and demos with
their own temporary data dir (and synthetic labels and split) aren't affected.

**Labels added later**: existing assignments never change. A key that isn't in `assignments`
is assigned deterministically by `sha256(f"{seed}:{key}") / 2**256 < test_fraction`, which is
independent of order and of other rows. This isn't stratified, but it's unbiased and stable.
`dealsource labels status` reports aggregate counts per split × decision, including how many
keys were assigned by that rule. It never rewrites the manifest. A changed `labels.csv` hash is
reported, not treated as an error, because appending labels is expected. A key whose decision
*changed* after the split is reported as a count, and details go to the private conflicts file.

### 11.4 Dev vs. test discipline

- `dealsource eval --thesis PATH` evaluates the **dev** set (the default). Use it freely while
  tuning weights, thresholds, keywords and exclusions.
- `dealsource eval --thesis PATH --set test --final` evaluates the **held-out test** set. It
  requires `--final`, and it runs **once**:
  - it appends a record (timestamp, thesis hash, git commit, metrics) to the append-only
    `private/evals/test_eval_log.jsonl`
  - if that log already has an entry, it refuses to run again and instead reprints the
    aggregate metrics recorded in the log
  - since the log is a file and not the DB, rebuilding the DB doesn't reset the "used" state.
    Starting a new held-out set means a new labels file and split, which is a deliberate
    manual step.
- No code path used for tuning reads test-split labels. `eval.labels.load(split=...)` requires
  an explicit split, and only the `--set test --final` path passes `"test"`.

### 11.5 Metrics and outputs

Labels are matched to scored companies with the §7 matcher (domain first, then name + state).
Unmatched labels count against coverage; they are not dropped silently.

**Terminal (aggregate only, with no company names, domains or per-row values):**
- the split, n labels, n matched (coverage %), pursue/pass balance
- average precision and ROC AUC, each with a bootstrap 95% CI
- precision@k and recall@k for k ∈ {10, 25, 50} (clipped to n), each with a Wilson 95% CI
- a confusion matrix at `shortlist_threshold` (TP/FP/FN/TN counts), with precision and
  recall (Wilson 95% CI) and F1 (bootstrap 95% CI)
- the count of positives removed by exclusion rules, broken down by rule name (rule names come
  from the thesis, not from company data)
- the path of the company-level report

**File (company-level, for the analyst's own review):**
`private/evals/<eval_id>_<split>.csv` with `label_key, company_name, domain, decision, matched,
company_id, score, rank, shortlisted, excluded, exclusion_rule, confidence, components,
reason, disagreement`, where `disagreement` ∈ {`false_negative`, `false_positive`,
`excluded_positive`, `unmatched`, ``}. Disagreements sort first.

A test captures `eval`'s stdout and asserts that no fixture company name or domain appears in
it.

### 11.6 Confidence intervals

Label sets will be small, so every precision/recall-type number is shown with its uncertainty,
e.g. `precision 0.62 [0.45, 0.77] (TP=18, n=29)`.

- **Wilson score interval (95%)** for precision, recall, precision@k and recall@k. These are
  binomial proportions (successes / denominator). Wilson is closed-form and deterministic, and
  unlike the normal approximation it behaves well at small n and near 0 or 1. Formula, with
  z = 1.96:
  `(p̂ + z²/2n ± z·√(p̂(1−p̂)/n + z²/4n²)) / (1 + z²/n)`.
  For precision at the threshold, n is the number of shortlisted companies. The interval is
  conditional on that count, which is the standard reading.
- **Stratified bootstrap (95% percentile, 2,000 resamples)** for AP, ROC AUC and F1, which
  aren't simple proportions. It resamples label *groups* with replacement within each
  decision stratum, so every resample keeps the class balance. The RNG is seeded
  (`bootstrap_seed`, default 20260927, recorded in `eval_runs` and in the test-eval log), so
  the intervals are reproducible run to run.
- **Small-n warnings**: when a denominator is under 10, or a stratum has fewer than 5 groups,
  the line is marked `(small n)`. A bootstrap interval is then shown as `n/a` rather than a
  misleadingly narrow interval.
- The company-level report file includes the same aggregate block as a header section, so the
  file is self-contained.

## 12. Personal-information policy

The engine never collects personal contact information. It's enforced at several layers:
1. CSV and labels ingest drop or reject columns that look like contact fields.
2. The fetcher skips contact, team and leadership pages.
3. Extracted text is scrubbed of emails and phone numbers before it's stored or sent to the
   LLM.
4. The LLM schema has no person fields.
5. Person names in evidence quotes and summaries are masked to `[PERSON]` (§8.4).
6. The CSV export has a fixed column list with no contact columns.
7. A test asserts that no stored enrichment, export or eval report contains an email or phone
   pattern, or an unmasked person name from the synthetic fixtures.

## 13. CLI

```
dealsource labels split [--test-fraction 0.3] [--seed N]   # once, before any pipeline run
dealsource labels status                                   # aggregate counts only
dealsource ingest csv PATH [--map ...] [--source-name NAME] [--revenue-unit usd_m]
dealsource ingest cbp --naics 3323,3327 --geo state:13,37 --year 2022
dealsource resolve [--review]
dealsource enrich [--limit N] [--company-id ID] [--refresh-older-than 30d] [--remask]
dealsource score --thesis PATH
dealsource export --thesis PATH --out PATH
dealsource run --thesis PATH [--input CSV ...]
dealsource eval --thesis PATH [--set dev]                  # aggregate to terminal, detail to private/evals/
dealsource eval --thesis PATH --set test --final           # once
dealsource stats                                           # LLM latency/tokens, cache hit rates, stage counts
```

## 14. Testing (offline only)

- `pytest` with `pytest-socket` (`--disable-socket --allow-unix-socket`) in `pyproject.toml`,
  so any accidental network call fails the test run.
- HTTP goes through an injectable `httpx` client. Tests use `httpx.MockTransport` serving
  fixture HTML, robots.txt and CBP JSON from `tests/fixtures/`.
- The LLM uses a `FakeLLMBackend` that returns canned `Extraction` JSON keyed by input, with
  configurable token counts and latency, so metrics and caching are testable. Separately,
  Ollama request and response handling is tested against recorded JSON through
  `MockTransport`, never a live Ollama.
- Time and sleep are injected (a clock object), so rate-limit and backoff tests run instantly.
- Each test gets a fresh temporary SQLite DB and a temporary data dir standing in for
  `private/`.
- All fixtures are fictional companies and synthetic labels. No real firm theses, targets or
  labels ever go in `tests/` or `examples/`.

Key test areas:
- **Resolution**: name and domain normalization tables, matcher pairs (positive, negative,
  conflicting domains), union-find edge cutting.
- **Fetching and extraction**: robots allow/deny and crawl-delay, same-domain redirect
  handling, contact-info scrubbing, person-name masking (a positive/negative table that
  includes protected company-name tokens).
- **Caching and the LLM**: cache hits on rerun (zero HTTP and LLM calls the second time), the
  schema-validation retry path, the locality guard.
- **Scoring**: thesis validation errors; size precedence (CSV employees > site employees >
  facilities; revenue only when CSV-supplied; missing revenue is not penalized); scoring
  components and exclusions; reason text.
- **Export**: the column list.
- **Labels and eval**:
  - the same seed gives the same split, and row order has no effect
  - groups are never split across dev/test
  - stratification by decision holds (per-stratum test counts equal the rounded fraction)
  - `labels split` refuses when `labels.csv` is missing (and writes nothing), refuses a second
    run, and refuses when the DB has runs
  - pipeline commands refuse while no split manifest exists
  - invalid decision values are rejected
  - Wilson intervals match known reference values (including p = 0 and p = 1 edge cases)
  - the bootstrap is deterministic for a fixed seed and preserves stratum sizes
  - small-n cases print `(small n)` / `n/a`
  - new keys get stable hash assignments
  - the test eval runs once and then only reprints
  - eval stdout contains no company-level data, and the report file does

## 15. Dependencies

Runtime: `httpx`, `pydantic>=2`, `pyyaml`, `python-dotenv`, `typer`, `rapidfuzz`,
`tldextract`, `selectolax`.
Dev: `pytest`, `pytest-socket`, `ruff`.
Standard library: `sqlite3`, `urllib.robotparser`, `hashlib`, `zlib`, `random`.
Metrics and confidence intervals (AP, ROC AUC, P@k, Wilson, bootstrap) are implemented
directly (they're small functions) to avoid pulling in scikit-learn, scipy or numpy.

## 16. Open questions

None blocking. Possible later work: a `tune` command that grid-searches weights on the dev set
only; ranking metrics if graded labels are ever introduced; CBP market context as a scoring
component (after v1).

## 17. Decision log

| Date | Decision |
|---|---|
| 2026-09-27 | Person names in evidence quotes (and summaries) are masked to `[PERSON]`. The text sent to the local LLM is left unmasked (§8.4). |
| 2026-09-27 | Size scoring uses employee and facility signals. Revenue is used only when a CSV supplies it; the LLM never extracts or estimates revenue (§9.3). |
| 2026-09-27 | Labels are split into dev (tuning) and held-out test (evaluated once at the end) with a fixed seed, before any pipeline run. The split is recorded in a read-only file and can't change later (§11). |
| 2026-09-27 | `eval` prints only aggregate metrics to the terminal. Company-level results and disagreements go to `private/evals/` for the analyst's own review (§11.5). |
| 2026-09-27 | The rule that Claude doesn't read `private/` or `.env` unless explicitly asked stays in CLAUDE.md. |
| 2026-09-27 | CBP market data is report-only in v1 and doesn't affect scores (§6.2). |
| 2026-09-27 | Split: 30% test, seed 20260927, stratified by the binary decision and grouped by label key. `labels split` runs once `private/labels.csv` exists, refuses a second run, and the pipeline is gated on the split existing (§11.3). |
| 2026-09-27 | Precision and recall (and @k) are reported with Wilson 95% CIs; AP, ROC AUC and F1 with seeded, stratified bootstrap 95% CIs (§11.6). |
| 2026-09-27 | Labels are binary (pursue/pass) only. |
| 2026-09-27 | HTML text extraction uses `selectolax`. |
| 2026-09-27 | A tracked pre-commit hook (`.githooks/pre-commit`) blocks `private/`, `.env*` (except `.env.example`) and DB files. It's enabled per clone with `git config core.hooksPath .githooks` (§4.2). |
