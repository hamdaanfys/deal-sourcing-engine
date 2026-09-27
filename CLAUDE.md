# CLAUDE.md

A deal-sourcing engine for middle-market private equity. It turns an investment thesis (YAML)
into a ranked, explained CSV of acquisition targets:
ingest → resolve (entity resolution) → enrich (polite web fetch + local LLM) → score → export.
The full architecture is in `DESIGN.md`. Read it before any non-trivial change and keep it
up to date when the design changes.

## How to work in this repo

- **Explain before changing.** Before editing or creating files, say what you plan to change
  and why (files, approach, trade-offs). For anything beyond a trivial fix, wait for the user
  to confirm.
- Keep changes scoped to the request. Raise design deviations instead of making them silently.
- Don't commit or push unless asked.

## Confidentiality (hard rules)

The firm's thesis, target lists, labels, outputs and database are confidential.

- **Never commit anything from `private/`, and never commit `.env`.** Both are gitignored.
  Never use `git add -f` on them, and never edit `.gitignore` to un-ignore them.
- Stage files by explicit path. Don't use `git add -A`, `git add .` or `git commit -a`. Run
  `git status` and check the staged list before every commit.
- Don't open, read, grep or print files under `private/` or `.env` unless the user explicitly
  asks for it in that session. Don't copy their contents into code, tests, fixtures, docs,
  commit messages or examples.
- The public repo ships **only** `examples/thesis.example.yaml` and synthetic sample data.
  Examples and test fixtures use fictional companies only.
- Default paths for the DB, caches, logs, exports, real theses and overrides all point under
  `private/`. Don't add defaults that write data into tracked directories.
- `.env.example` documents variables with empty or placeholder values only.

## Data and privacy rules

- **Never collect personal contact information**: no emails, phone numbers, personal
  addresses, LinkedIn profiles or contact-person fields in schemas, DB tables, LLM output or
  exports. Keep the safeguards from DESIGN.md §10 working (CSV column drop, skipping
  contact/team pages, PII scrubbing before storage and before the LLM, no person fields in
  the extraction schema).
- **All LLM inference is local.** Ollama is the only implemented backend. Don't add remote or
  hosted LLM backends or SDKs, and don't weaken the loopback-only `OLLAMA_HOST` guard.
- **Polite fetching**: always respect robots.txt, per-host rate limits and Crawl-delay; use
  the identifying user agent with a contact value; stay on the company's own domain; no JS
  rendering and no form submission.
- Cache every web fetch and every LLM call (SQLite). Reruns must hit the cache. Changing a
  prompt, schema or model must change the cache key (bump `PROMPT_VERSION`).
- Track per-call LLM latency and prompt/completion tokens in `llm_calls`, cache hits included.

## Tech stack and conventions

- Python 3.12, `src/` layout, package `dealsource`, Typer CLI, Pydantic v2, SQLite (stdlib
  `sqlite3`, numbered migrations, no ORM), `httpx`, `rapidfuzz`, `tldextract` (bundled suffix
  list, no network refresh).
- New data sources implement the `CompanySource` or `MarketDataSource` protocol in
  `src/dealsource/sources/` and register themselves. Don't change downstream stages to
  accommodate one source.
- New LLM backends implement `LLMBackend` in `src/dealsource/llm/`. Only Ollama exists, and
  only local ones are allowed.
- Scoring stays deterministic and rule-based, and every scored company gets a written reason.
- Inject dependencies (HTTP client, LLM backend, clock) so they can be faked in tests.

## Testing

- Tests are **fully offline**: no network, no live Ollama. `pytest-socket` blocks sockets.
  Use `httpx.MockTransport` with fixtures in `tests/fixtures/` and `FakeLLMBackend`.
- Add or update tests with every behavior change. Run `pytest` before saying work is done,
  and report failures honestly.

## Commands

(Filled in once the project is scaffolded.)

```
pytest
ruff check .
dealsource --help
```
