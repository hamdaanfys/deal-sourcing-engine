# dealsource

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

Pipeline commands refuse to run until the labels dev/test split exists
(`private/labels_split.json`; see DESIGN.md §11.3).

```sh
dealsource ingest csv examples/companies.example.csv
dealsource ingest cbp --naics 332710 --geo state:13,37 --year 2022
dealsource resolve --review
dealsource stats
```

## Tests

```sh
.venv/bin/pytest          # fully offline: sockets are blocked
.venv/bin/ruff check .
```
