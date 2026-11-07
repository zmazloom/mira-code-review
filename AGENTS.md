# Repository Guidelines

## Project Structure & Module Organization

Python code lives in `src/mira/`: `core/` contains the shared review engine and Task-Aware extensions; `providers/` handles hosting APIs; `platforms/` handles webhooks, authentication, and fetching; `llm/` contains model clients and Jinja2 prompts. Indexing lives in `index/`; dashboard APIs and persistence live in `dashboard/`.

The React/TypeScript dashboard is in `ui/mira/src/`, with assets in `ui/mira/public/`. Tests and fixtures live in `tests/`; helpers and documentation live in `scripts/` and `docs/`. Keep both review modes on the shared engine.

## Build, Test, and Development Commands

Run Python commands from the repository root:

- `uv sync --extra dev --extra serve --extra bedrock`: install development dependencies, including optional integrations used by CI.
- `bash scripts/start_local.sh`: launch the local API and dashboard after configuring `.env`.
- `uv run mira review <PR_OR_MR_URL>`: run a review; add `--post` to publish results.
- `uv run pytest tests/ --ignore=tests/evals --ignore=tests/test_integration.py`: run the normal regression suite.
- `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`: check lint and formatting.
- `uv run mypy src/mira/ --ignore-missing-imports`: check Python types; CI currently treats this as advisory.

From `ui/mira/`, use `npm ci`, `npm run dev`, `npm run typecheck`, and `npm run build` to install, develop, check types, and build production assets.

## Coding Style & Naming Conventions

Use four-space Python indentation, double quotes, and a 100-character target line length. Use `snake_case` for functions/modules and `PascalCase` for classes. Annotate public functions. Follow nearby TypeScript/React patterns and use ESLint and Prettier. Preserve UTF-8, LF endings, and final newlines; configuration files use two-space indentation. Explain subtle constraints in comments and avoid speculative abstractions.

## Testing Guidelines

Use pytest and pytest-asyncio; async mode is automatic. Name modules `test_*.py` and tests `test_*`. Add regression coverage for changed behavior and reuse `tests/fixtures/`. Mock external APIs in ordinary tests. Live evaluations and benchmarks require explicit marker selection (`-m eval`, `-m benchmark`); no numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Recent commits use short descriptive subjects without a consistent prefix. Write specific subjects. Branch from `main`, keep PRs focused, and follow `.github/PULL_REQUEST_TEMPLATE.md`: summarize changes, provide a test plan, and explain reviewer concerns. Link relevant issues and include screenshots for UI changes. Run applicable quality checks before submitting.

## Security & Configuration

Keep credentials in environment variables or local `.env` files. Never commit secrets, private keys, or runtime `data/`. Use example configuration files as templates. Report vulnerabilities privately through `SECURITY.md`.
