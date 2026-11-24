# job-scout

Automated IT job aggregator, deterministic rule filter, cloud LLM semantic evaluator, and interactive Markdown/HTML reporter for **SOLID.Jobs**, **Just Join IT**, **No Fluff Jobs**, **Bulldogjob**, **the:protocol**, and **Pracuj.pl (dział IT)** — designed to run locally and fully automated on **GitHub Actions**.

## Architecture

```
SOLID.Jobs (REST JSON) ─────────┐
Just Join IT (REST JSON) ───────┤
No Fluff Jobs (Search+Detail) ──┼──► Fail-Soft Runner + Cross-Portal Dedup
Bulldogjob (SSR __NEXT_DATA__) ─┤    (SQLite + 30d Normalized Fingerprint)
the:protocol (SSR __NEXT_DATA__)┤
Pracuj.pl IT (SSR __NEXT_DATA__)┘
                                ▼
                 Stage 1: Deterministic Rules (config/profile.yaml)
                    ├── Rejected ──► SQLite (rejected_rule) + data/rejected_log.jsonl
                    ▼
                 Stage 2: Concurrent Cloud LLM Evaluation (config/base_cv.md)
                    ├── Score < threshold ──► SQLite (rejected_llm) + data/rejected_log.jsonl
                    ▼
                 Shortlisted ──► Rich Terminal + YYYY-MM-DD_HH-MM_report.{md,html}
                                               + YYYY-MM-DD_master.{md,html}
                                               (with Source Health table at top)
```

## Key Features

- **Async Multi-Portal Scrapers (pure `httpx`, fail-soft isolation & per-source config):**
  - **SOLID.Jobs (`solid.jobs`):** Public IT offers REST API (`?campaign=job-scout`) with automatic pagination and retry/backoff.
  - **Just Join IT (`justjoin.it`):** Targeted categories and seniority queries with lightweight Stage 1 metadata filtering *before* detail fetches.
  - **No Fluff Jobs (`nofluffjobs`):** `POST /api/search/posting` filtered by profile (`city`, `category`, `seniority`) + `GET /api/posting/{id}` with polite `0.5–1.5s` delay, UoP/B2B salary normalization, and `musts`/`nices` mapping.
  - **Bulldogjob (`bulldogjob`):** Next.js SSR `__NEXT_DATA__` paginated listings filtered by city and experience level + detail page enrichment.
  - **the:protocol (`theprotocol`):** Next.js SSR `__NEXT_DATA__` targeted queries (city + remote Poland) with required/optional tech and UoP/B2B salary parsing.
  - **Pracuj.pl IT (`pracuj.pl`):** Tightly scoped `it.pracuj.pl` SSR `__NEXT_DATA__` queries (`its=backend,ai-ml,big-data-science`, Junior/Mid `et` levels, Warsaw & remote) to avoid pulling irrelevant listings.
- **Cross-Portal Deduplication (`sqlite3`):** Skips seen `(source, external_id)` offers and detects cross-posted duplicates within 30 days via a normalized `(company, title)` SHA-256 fingerprint (stripping legal suffixes like `Sp. z o.o.`, `S.A.`, city/remote/gender tags, punctuation, and extra spaces) with safe automatic SQLite schema migration (`ALTER TABLE`).
- **Two-Stage Filtering & Calibration Log:**
  - **Stage 1 (`filter.py`):** Deterministic filtering (`must_have_any_tech` with word boundaries, `exclude_keywords`, `allowed_workplace_types`, `allowed_cities`, and `min_salary_pln` while preserving undisclosed salaries).
  - **Stage 2 (`evaluator.py`):** Concurrent evaluation via OpenAI-compatible cloud endpoints (Google Gemini, OpenRouter, or OpenAI) using Structured Outputs (`JobEvaluation` Pydantic schema) with `reasoning_effort` support and exponential backoff retries.
  - **Auditability:** Every rule and LLM rejection is saved to `data/rejected_log.jsonl` with its exact reason to spot false negatives and calibrate `config/profile.yaml`.
- **Interactive Reports (`reporter.py`):**
  - **Source Health Table:** Displayed at the top of terminal, Markdown, and HTML reports (`Portal | Pobrane | Po pre-filtrze | Duplikaty | Ocenione LLM | Status/Błąd`).
  - **Timestamped Run Snapshots** (`reports/YYYY-MM-DD_HH-MM_report.{md,html}`) so multiple scans in a day never overwrite earlier results.
  - **Daily Master Reports** (`reports/YYYY-MM-DD_master.{md,html}`) consolidating all findings for that day.
  - **Standalone HTML Dashboard:** Dark/light CSS, client-side filtering by match score (`Poziom dopasowania`), work mode (`remote` / `hybrid` / `onsite`), normalized city (`Miasto`), and portal (`Portal`), sorting by score or publication date (`Najnowsze`), plus a collapsible calibration section.

## Quick Start (Local)

1. **Install dependencies with `uv` (Python 3.12+):**

   ```bash
   uv sync
   ```

2. **Configure environment variables:**

   ```bash
   cp .env.example .env
   # Edit .env and set LLM_API_KEY (plus optional LLM_BASE_URL, LLM_MODEL, LLM_CONCURRENCY)
   ```

3. **Create your personal config & CV from the example templates:**

   ```bash
   cp config/profile.example.yaml config/profile.yaml
   cp config/base_cv.example.md config/base_cv.md
   ```
   - `config/profile.yaml` — Stage 1 deterministic rules, `min_match_score`, and Stage 2 expectations (gitignored).
   - `config/base_cv.md` — your baseline Markdown CV used for Stage 2 scoring and tailored `cv_focus_tips` (gitignored).

## CLI Usage

```bash
# Dry-run: fetch + Stage 1 deterministic filter + dedup + terminal preview (no LLM calls, no DB writes)
uv run job-scout run --dry-run

# Test a single portal scraper in dry-run mode
uv run job-scout run --source nofluffjobs --dry-run
uv run job-scout run --source bulldogjob --dry-run
uv run job-scout run --source theprotocol --dry-run
uv run job-scout run --source pracuj.pl --dry-run

# Cheap prompt calibration: evaluate at most 5 Stage 1 survivors
uv run job-scout run --limit 5

# Full scan across all enabled sources
uv run job-scout run

# View database summary & top rejection reasons from data/rejected_log.jsonl
uv run job-scout stats

# Regenerate daily reports/YYYY-MM-DD_master.{md,html} from jobs.db
uv run job-scout report
uv run job-scout report --date 2026-11-16
```

## Privacy-Preserving GitHub Actions Setup (Public Code + Private Data Repo)

This repository keeps **100% of the source code and reusable workflow** ([`.github/workflows/daily_scan.yml`](.github/workflows/daily_scan.yml)) public, while keeping your CV, `profile.yaml`, SQLite database, reports, and Actions run logs private in a separate **private repository** (e.g., `job-scout-data`):

1. Create a **private** GitHub repository containing:
   - `config/profile.yaml` and `config/base_cv.md`
   - `data/` and `reports/`
   - `.github/workflows/daily_scan.yml` copied from [`config/private_workflow.example.yml`](config/private_workflow.example.yml) (updating `uses:` to `<your-username>/job-scout/.github/workflows/daily_scan.yml@master`).
2. In the **private** repository's **Settings → Secrets and variables → Actions**:
   - Add **Secret** `LLM_API_KEY` (required).
   - Optionally add **Variables** `LLM_BASE_URL`, `LLM_MODEL`, `LLM_RPM_DELAY`, `LLM_REASONING_EFFORT`, `LLM_CONCURRENCY`.
3. In the **private** repository's **Settings → Actions → General → Workflow permissions**, enable **Read and write permissions**.

Because the reusable workflow executes inside your private repository via `workflow_call`, `GITHUB_TOKEN` commits updated `data/jobs.db`, `data/rejected_log.jsonl`, and `reports/` back to the private repo, and all logs + `$GITHUB_STEP_SUMMARY` remain private.

> Anyone who forks this public repository can also run `.github/workflows/daily_scan.yml` directly via `workflow_dispatch`.

## Project Structure

```text
job-scout/
├── .github/workflows/daily_scan.yml   # Reusable workflow (workflow_call + workflow_dispatch)
├── config/
│   ├── base_cv.example.md             # Anonymized baseline CV template
│   ├── private_workflow.example.yml   # Caller workflow template for private data repo
│   └── profile.example.yaml           # Annotated profile.yaml schema template
├── data/.gitkeep                      # Local/private SQLite DB & rejection log directory
├── reports/.gitkeep                   # Local/private Markdown & HTML reports directory
├── src/job_scout/
│   ├── cli.py                         # CLI commands (run, stats, report)
│   ├── config.py                      # .env settings & profile.yaml loader
│   ├── database.py                    # SQLite storage, 30-day fingerprint dedup & audit log
│   ├── evaluator.py                   # Concurrent Stage 2 cloud LLM evaluator
│   ├── filter.py                      # Stage 1 deterministic rule filter
│   ├── models.py                      # Pydantic v2 schemas
│   ├── reporter.py                    # Rich terminal + Markdown + interactive HTML generators
│   └── scrapers/                      # Async SOLID.Jobs & Just Join IT API clients
├── tests/test_pipeline.py             # Unit tests with mocked HTTP & LLM responses
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

## Development & Tests

```bash
uv run pytest -v
```
