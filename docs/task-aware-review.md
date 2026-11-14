# Task-aware repository review

`TASK_AWARE` verifies a PR/MR against an implementation task using selected code
from the actual source repository at a pinned head commit. Choose **Task-aware
repository review** on the existing **Start review** dashboard.

It also checks material coding and architectural consistency against selected
unchanged peers, inside the existing final assessment and context budget. See
[coding handwriting and convention evidence](codebase-consistency.md) for the
selection rules, false-positive guards, structured fields, and limitations.

## API

Use the existing `mira_session` login cookie. Repository access uses the same
server credentials and permissions as manual reviews. GitHub URLs must use
github.com; GitLab URLs must use the configured `MIRA_GITLAB_API_URL` host.
Submitted URLs never configure a provider endpoint.

```http
POST /api/reviews/task-aware
Content-Type: application/json
Cookie: mira_session=...

{
  "pr_url": "https://github.com/zmazloom/SpringBootBase/pull/5",
  "task_prompt": "Implement a complete CRUD service for event assignments.",
  "model": null,
  "review_options": {
    "depth": "balanced",
    "post_comments": false,
    "include_existing_code": true,
    "max_context_tokens": 120000,
    "max_total_tokens": 2000000,
    "max_tool_calls": 24,
    "max_seconds": 900
  }
}
```

The response is **202 Accepted**, before review work begins:

```json
{
  "id": "7e60e6442c284f34a45d66d38423ee1d",
  "platform": "github",
  "url": "https://github.com/zmazloom/SpringBootBase/pull/5",
  "model": null,
  "mode": "TASK_AWARE",
  "status": "reviewing",
  "phase": "queued",
  "message": "Reviewing…",
  "publishing_status": "disabled",
  "result": null,
  "duration_seconds": null,
  "errors": []
}
```

Poll `GET /api/reviews/task-aware/{id}`. Retrieve findings with
`GET /api/reviews/task-aware/{id}/findings`. Both enforce job ownership.
Status is `reviewing`, `done`, or `failed`; `done` may have `result.partial: true`.
Phases identify requirement extraction, context retrieval, review, verification,
and publishing. Results include:

- Source/target metadata, branches, base/head/merge-base SHAs, commit identifiers,
  reviewed SHA, mode, summary, and duration.
- Requirements with IDs, descriptions, statuses, exact source evidence and line
  ranges, symbols, confidence, uncertainty, and linked finding titles.
- Findings grouped by severity and category, requirement linkage, evidence,
  impact, introduced-by-PR uncertainty, and inline/summary location.
- Retrieved paths, paths supplied as context, eligible tree count, tool count,
  unverified requirements, limitations, and partial-coverage indicator.
- Actual aggregate `token_usage` (null when unavailable), a separate
  `token_admission_estimate`, publishing status, and sanitized errors.

Example requirement within a completed result:

```json
{
  "id": "R2",
  "description": "Delete an existing assignment",
  "status": "unverified",
  "evidence": [],
  "related_symbols": [],
  "confidence": 0,
  "uncertainty": "No verifiable source evidence was supplied.",
  "finding_titles": []
}
```

Statuses are `implemented`, `partially_implemented`, `missing`, `incorrect`, and
`unverified`. Missing functionality requires positive evidence at its expected
integration point. Missing context cannot establish absence. Quotes and line
ranges are validated against retrieved source.

Validation errors return 422, missing authentication 401, another active job
owned by the caller 409, and four occupied shared review slots 429. Model
overrides must be configured or advertised by the existing model catalog.
A null model uses the configured model tiers and existing language settings.

## Architecture and analysis flow

```text
Dashboard/API → shared dashboard job slots → ManualReviewCommand
  → ReviewEngine.review_pr → existing GitHub/GitLab provider
  → TaskReviewSession context → existing review/policy/security passes
  → task verification with existing tools → existing comment publishers
```

`TaskReviewSession` adds context and requirement verification to the existing
engine. `BudgetedLLM` delegates to clients initialized by `create_llm`; it owns
no transport, authentication, retry mechanism, or separate model integration.
Company rules retain `load_company_rules` and the focused policy passes.
Custom rules, conventions, severity classification, noise filtering,
self-critique, security/dependency checks, and output language remain active.

GitHub resolves `head.repo`, head/base SHAs, and three-dot comparison metadata.
GitLab resolves `source_project_id` and an immutable MR diff version matching
the head SHA, including that version's commit history. Source, tree, and
history reads use the fork/source repository. Missing forks, unavailable diff
versions, and incomplete snapshots fail rather than substitute another branch.
Commit messages express intent; final source remains authoritative.
Commit metadata is bounded to 100 entries; existing per-file history retrieval
retains its five-entry limit.

1. Retrieve changed source first and extract at most 20 task-grounded
   requirements with a structured submission tool when supported. Validate the
   result and allow one repair for invalid requirement JSON or schema values.
2. Reuse language import resolution and index dependency edges as navigation
   hints. Re-fetch evidence at head rather than trust stale index summaries.
3. Search contents for task terms and changed symbols, including callers in
   modules with unrelated filenames; prioritize dependencies and task-related paths.
4. Supply numbered matching excerpts and declarations to the standard engine.
5. Reuse `read_file`/`grep_repo` and the existing agentic loop for on-demand
   checks, sharing caches and tool allowances across passes and chunks.
6. Verify requirements together, validate evidence, and filter/critique findings.

Initial content search reserves retrieval capacity for targeted follow-up tools.
No clone, re-index, per-file requirement call, or persistent findings cache is
added. Concurrent identical reads share one source request. Caches are job-local,
preventing reuse across tasks, users, repositories, commits, and models.

| Depth | Retrieved source files | Maximum tool calls | Verification tool hops |
|---|---:|---:|---:|
| fast | 20 | 6 | 2 |
| balanced | 60 | 24 | 4 |
| deep | 150 | 100 | 6 |

The request also caps tool calls; its default is 24 even for deep reviews.
Standard agentic passes retain their existing six-hop ceiling and share the
same task-wide tool allowance. When investigation ends without a submission,
force the terminal tool with the collected tool results and conversation,
instead of restarting from the original prompt. No configured standard checks
are disabled. An output-limit failure stops the affected pass immediately;
it does not trigger a second expensive forced submission of the same workload.

Review complexity is bounded separately from the model context window:
`review.max_chunk_tokens` defaults to 16,000 (including the chunker's 2,000-token
overhead reservation), and `review.max_files_per_chunk` defaults to 8. Task-aware
chunks receive excerpts for their own changed files and up to eight related
source peers, bounded by `review.context_token_budget`, rather than repeating
all changed-source excerpts in every chunk. Requirements and configured review
documents remain available. `review.max_chunks_per_review` caps the chunk count;
omitted files are listed as skipped, never silently marked reviewed. The
`review.chunk_plan` event reports the actual partition and omitted paths.

`max_context_tokens` is a per-call admission limit, capped by model configuration.
Excerpts reserve room for rules, diff, and output. `max_total_tokens` separately
reserves estimated input plus configured maximum output across concurrent
calls and clients. Existing provider token counters may be heuristic;
provider retries/fallbacks mean this is not a precise billing cap. Actual
provider-reported usage is returned separately. Time limits cancel pending work.

The final `min(300, max_seconds / 3)` seconds are reserved for task verification.
For the default 900-second job, earlier model passes stop at 600 seconds and
verification can use the remaining 300 seconds. Expired phases are rejected
before another model request starts. Token reserves and time reserves are
independent; increasing a request timeout does not extend the job deadline.

Up to one third of the total allowance (capped at three context windows) is
protected for final task verification. Earlier passes report partial coverage
when they reach their share, preserving already-produced findings.
Optional final investigation also preserves one complete context window for a
forced structured submission. That submission rebuilds bounded source excerpts
from the shared cache when no valid investigation conversation is available;
otherwise it retains the conversation and newly read evidence.

The company Messages adapter records response `stop_reason`, content-block
types, and input/output usage without recording prompt text, model reasoning,
or raw response bodies. A missing tool submission gets one explicit repair;
truncated responses fail immediately rather than using the network retry
allowance. Network retries record their exception type, reason, and delay.
For this endpoint, text and forced-tool submissions explicitly request
`thinking: {type: disabled}`. Investigation also defaults to thinking disabled;
an explicit reasoning effort enables a budget of 1,024/2,048/4,096 for
low/medium/high, capped at half the output allowance. Signed thinking blocks
from investigative tool turns are replayed verbatim, not logged. These controls
require the internal gateway to honor the Anthropic-compatible thinking fields;
the model's context window is not an output-thinking budget. The configured
output limit is unchanged, and output exhaustion remains an explicit limitation.

Rejected candidates emit `review.finding_rejected` with their path, line,
source pass, pipeline stage, and reason (confidence/severity floor, duplicate,
comment cap, critique, invalid task evidence, or invalid diff anchor). Source
bodies, suggestions, and model reasoning are not included. Rejections during
filtering and critique are also retained in the result audit.
Completed passes retain their findings if another pass fails. The final
`review.completed` event includes `partial` and `limitations`, and partial
coverage produces a warning in both the admin and user logs.
Empty partial reviews use an incomplete-review summary, not "No issues found,"
including when partial verification is established only by the final assessment.
Resource limits and failed passes produce explicit partial verification.

## Publishing, persistence, and compatibility

`post_comments: false` performs no remote mutations. If enabled, Mira rechecks
the head and skips publishing if it changed. Existing inline publishers receive
valid diff positions. Each actionable finding is published independently;
findings without a valid changed-line location use separate conversation notes.
The review-level body is empty, and internal verification statuses and retrieval
diagnostics are not published. GitHub publishing pins the commit; GitLab verifies
the diff references.

Stable per-finding markers and existing thread deduplication prevent repeated
conversation notes and inline findings on reruns. Remote writes are not
transactional: a crash between writes may leave partial publication. Duplicate
prevention follows the existing single-process job model.

Each published finding uses the shared comment formatter and includes its own
actionable AI remediation prompt. The prompt is retained in structured results;
when analysis did not provide one, deterministic rendering builds it from the
finding context without another model call. Task-aware comments use concise
Persian headings for severity, issue, recommendation, and remediation prompt.

The API shares dashboard authentication, owner checks, concurrency limits,
model catalog, and application database. Structured results use existing
settings storage without a migration and survive restarts. Active work is not a
durable queue: rerun interrupted jobs after a restart. Use one API worker for
the existing in-memory controls. Durable results currently have no automatic
expiration; apply the same access, backup, and retention policy as dashboard data.

Task-aware jobs do not advance standard incremental-review or review-rest
checkpoints. Existing APIs, webhook reviews, `mira review`, and `mira review-notif`
remain available. The CLI stays read-only by default. Pass `--task-prompt` to
`mira review` to run this task-aware flow from the terminal; add `--post` to
publish findings to the PR/MR. The dashboard's standard option retains its
existing Notif MR title gate. No dependencies or deployment configuration
changes are needed.

Provider contracts follow the official [GitHub compare API](https://docs.github.com/en/rest/commits/commits#compare-two-commits)
and [GitLab diff-version API](https://docs.gitlab.com/api/merge_requests/#retrieve-a-merge-request-diff-version).

Task prompts and repository content are untrusted data, never shell commands or
tool permissions. Fixed system instructions preserve policy and language.
Retrieval rejects traversal, absolute paths, unsupported source types, secrets
directories, environment files, and configured excluded paths. New job errors
use fixed messages and never log prompts, source text, or raw provider errors.

Coverage is selective and static. Mira does not execute the submitted
repository's builds/tests, prove business completeness, or guarantee every
affected caller is found. Unsupported/excluded files, provider limits, truncated
content, resource budgets, and unresolved requirements can prevent verification.

## Commands

Run a task-aware review from the terminal. Posting is opt-in, just like regular
CLI reviews:

```bash
mira review https://github.com/org/repo/pull/123 \
  --task-prompt "Implement a complete CRUD service for event assignments."
mira review https://github.com/org/repo/pull/123 \
  --task-prompt "Implement a complete CRUD service for event assignments." \
  --post
```

The first command prints findings without remote writes. The second publishes
task findings as PR/MR review comments using the configured Git provider token.

Start the existing Mira server with its usual credentials/configuration and sign
in. Save the request above as `task-review.json` and use an authenticated cookie jar:

```bash
curl -b cookies.txt -H 'Content-Type: application/json' \
  --data @task-review.json http://localhost:8100/api/reviews/task-aware
curl -b cookies.txt http://localhost:8100/api/reviews/task-aware/REVIEW_ID
curl -b cookies.txt http://localhost:8100/api/reviews/task-aware/REVIEW_ID/findings

uv run pytest tests/test_task_review.py tests/test_manual_review.py \
  tests/test_dashboard_manual_reviews.py tests/test_engine.py \
  tests/test_github_provider.py tests/test_gitlab_provider.py \
  tests/test_company_rules.py tests/test_output_language.py
uv run ruff check src/ tests/
uv run mypy src/mira/ --ignore-missing-imports
cd ui/mira
npm ci
npm run build
```

Normal tests use mocked providers and model outputs; no live PR comments or paid
model calls are required. Coverage includes API validation/auth/ownership,
persistence, fork resolution, pinned snapshots, requirement extraction,
missing/unverified distinctions, unchanged callers, policy/language, shared
security/critique, budgets, cache isolation, tools, publication routing,
deduplication, stale heads, timeouts, and standard/manual compatibility.
