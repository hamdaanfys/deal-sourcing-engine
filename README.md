# dealsource

[![CI](https://github.com/hamdaanfys/deal-sourcing-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/hamdaanfys/deal-sourcing-engine/actions/workflows/ci.yml)

**Status:** pipeline built and tested; labeling and evaluation in progress.

Turns an investment thesis into a ranked, explained list of acquisition targets. It runs
entirely on one machine: SQLite storage and local LLM inference (Ollama). See `DESIGN.md`
for the architecture and `CLAUDE.md` for the working rules.

## Setup

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
git config core.hooksPath .githooks   # blocks committing private/, .env and DB files
cp .env.example .env                  # then fill in CENSUS_API_KEY etc.
```

Firm-specific data lives in `private/` (gitignored). The repo ships only synthetic examples
(`examples/`).

## Usage

Discovery, resolution and the labeling export run before your labels exist. Enrichment,
scoring and evaluation only run after the one-time labels split (`private/labels_split.json`;
see DESIGN.md §11).

```sh
# 1. Find candidates (real theses live in private/theses/, never committed)
dealsource discover usaspending --thesis private/theses/thesis.yaml   # federal contractors, no key
dealsource discover osm --thesis private/theses/thesis.yaml           # OpenStreetMap makers (large downloads, resumable)
dealsource discover sam --thesis private/theses/thesis.yaml           # optional: needs SAM_API_KEY
dealsource ingest csv private/inputs/licensed-list.csv                # optional: lists you're licensed to use
dealsource resolve

# 1b. Find websites for companies without one (strict; resumable; run overnight)
dealsource websites find --thesis private/theses/thesis.yaml   # rerun to continue after a stop
dealsource resolve
dealsource websites sample          # 30 matches -> private/review/website_sample.csv to hand-check

# 2. Label a sample (only company_name, website, state, decision; nothing the tool inferred)
dealsource labels export --thesis private/theses/thesis.yaml        # -> private/to_label.csv
#    fill in decision (pursue/pass), save as private/labels.csv, then, once:
dealsource labels split

# 3. After the split
dealsource enrich
dealsource stats
```

## Getting a SAM.gov API key (free, optional)

Discovery works without it (USAspending + website finder + OpenStreetMap). `discover sam` downloads SAM.gov's public monthly entity extract with one request per month.

1. Go to https://sam.gov and choose **Sign In**. Create a Login.gov account (email plus
   two-factor authentication) if you don't have one; SAM.gov uses Login.gov for sign-in.
2. Finish the SAM.gov profile it asks for. You don't need to register an entity or request a
   role.
3. Open **Workspace → Profile → Account Details**
   (https://sam.gov/workspace/profile/account-details) and find the **Public API Key** field.
4. Click the eye icon, enter the one-time password SAM.gov emails you, and submit. The key
   appears.
5. Put it in `.env` (gitignored): `SAM_API_KEY=...`. Never commit it or paste it anywhere else.
6. Without a SAM.gov role the key allows **10 requests a day**. The tool uses one per monthly
   file, keeps the file in `private/cache/sam/`, and refuses beyond
   `SAM_DAILY_REQUEST_BUDGET` (default 8).
7. If SAM.gov later rejects the key (HTTP 401/403), generate a new one on the same page.

No key yet? Download the public monthly extract ZIP from SAM.gov's Data Services page yourself
and run `dealsource discover sam --thesis ... --file path/to/SAM_PUBLIC_UTF-8_MONTHLY_V2_*.ZIP`.

## Data attribution

OpenStreetMap data used by `discover osm` is © OpenStreetMap contributors and available under
the Open Database License (ODbL): https://www.openstreetmap.org/copyright. Extracts are
downloaded from Geofabrik (https://download.geofabrik.de/). USAspending.gov and SAM.gov data
are U.S. government data.

## Limitations

- **Discovery skews toward federal contractors.** SAM.gov and USAspending only contain
  companies registered to do business with the U.S. federal government. Candidates therefore
  skew industrial, defense and government-supplier, and consumer-facing companies (food and
  beverage brands, consumer products, retail-oriented manufacturers) are under-represented.
  The labeling sample and every metric built on it inherit this skew. Add lists you're licensed
  to use with `dealsource ingest csv` to widen coverage.
- **Website finding is by guessing.** `websites find` only finds sites whose `.com` domain
  resembles the company name, and it rejects anything it can't verify, so many companies get
  no website. Hand-check the sample before labeling.
- **The local 7B model is imperfect.** It invents numbers, which grounding against the page
  text removes, and it marks ownership signals inconsistently (DESIGN.md §8.5–8.6).

## Tests

```sh
.venv/bin/pytest          # fully offline: sockets are blocked
.venv/bin/ruff check .
```
