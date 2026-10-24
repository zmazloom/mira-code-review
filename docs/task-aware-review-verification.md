# Task-aware review implementation and verification

The API, analysis flow, request/response examples, budgets, usage commands, and
operational limitations are documented in [task-aware-review.md](task-aware-review.md).

## Architecture

The authenticated API creates an existing dashboard `Job` and reserves shared
manual-review slots. `ManualReviewCommand.execute` constructs the existing model
clients and `ReviewEngine`. `TaskReviewSession` supplies pinned source context
and requirement assessment to `ReviewEngine.review_pr` and
`_review_diff_internal`; it does not replace the engine. Publication goes through
the existing provider methods after review, with a task-specific summary marker.

Reused components include `create_manual_provider`, `create_llm`,
`llm_config_for`, `ProviderSourceFetcher`, `IndexStore.get_blast_radius`,
`extract_import_candidates`, `AgenticToolExecutor`, `agentic_review_loop`,
`load_company_rules`, the review/company prompt builders, security/dependency
passes, severity/noise filters, `self_critique`, diff-position validation,
`drop_already_posted`, and provider comment publishers. Application database
settings persist owner-protected job results without a schema migration.

Task input and source retrieval are isolated to the job. The source repository
can differ from the target; source/tree/history reads use the resolved source
and commit SHA. Existing indexes supply navigation hints, while code evidence
is fetched again at that SHA. Requirement extraction is grouped, source excerpts
are bounded, and deterministic scanning leaves capacity for targeted follow-up.

## Changed files

| File | Change |
|---|---|
| `README.md` | Links the new capability and API guide. |
| `docs/task-aware-review.md` | API, architecture, budgets, security, limitations, and commands. |
| `docs/task-aware-review-verification.md` | File inventory and verification evidence. |
| `src/mira/core/task_review.py` | Structured requirements, retrieval, evidence assessment, coverage, publication routing/idempotency. |
| `src/mira/core/task_budget.py` | Shared context/token admission, deadlines, provider delegation, and sanitized failures. |
| `src/mira/core/engine.py` | Optional task-session hooks into the existing pipeline; standard progress remains isolated. |
| `src/mira/core/passes.py` | Optional structured terminal schema and hop limit on the existing agentic loop. |
| `src/mira/dashboard/task_reviews.py` | Authenticated asynchronous API, results, status, persistence, model validation. |
| `src/mira/dashboard/manual_reviews.py` | Shares URL validation and job-slot allocation with the new API. |
| `src/mira/dashboard/api.py` | Registers the task-aware router. |
| `src/mira/manual_review.py` | Optional task arguments; retains existing engine/client initialization and CLI defaults. |
| `src/mira/models.py` | Optional source-snapshot metadata and task-result fields. |
| `src/mira/providers/base.py` | Explicit pinned snapshot extension point. |
| `src/mira/providers/github.py` | Fork metadata, pinned comparison/source/history, commit-pinned publication. |
| `src/mira/providers/gitlab.py` | Source project, immutable diff versions/history, incomplete-version checks, pinned publication. |
| `src/mira/index/context.py` | Rejects unsafe repository-relative source paths. |
| `src/mira/llm/agentic_tools.py` | Optional shared limits, bounded output, exposed-path tracking, literal task searches. |
| `src/mira/llm/prompts/review.py` | Task-specific separation of untrusted PR metadata/history into user messages. |
| `ui/mira/src/pages/manual-review.tsx` | Existing-page mode selector, task/depth/posting inputs, progress and evidence/results. |
| `tests/test_task_review.py` | Mocked API, provider, pipeline, requirement, security, budget, cache, and publication tests. |

No dependencies, deployment files, secrets, or user-specific configuration were
changed. No commits were pushed and no review comments were posted.

## Checks executed

The machine initially had no usable Python on PATH, Node 14/npm 6, and a stopped
Docker daemon. Isolated Python 3.12 and Node 22 tools were placed in ignored
`data/task-review-tools`; dashboard dependencies and build output are also ignored.
The project dependency files and lockfiles were not changed.

| Check | Result |
|---|---|
| Final task/manual/dashboard suite | **94 passed**, plus 16 subtests; includes final-submission budget regression. |
| Final expanded review-component regression suite | **360 passed, 3 failed**, plus 16 subtests; all three failures reproduced on HEAD. |
| Full non-eval suite, earlier implementation checkpoint | **1367 passed, 14 failed, 1 skipped, 1 deselected**; baseline details below. |
| Dashboard TypeScript project build (`tsc -b`) | Passed. |
| ESLint on the changed dashboard page | Passed. |
| Vite production build | Passed; existing config and bundle-size warnings. |
| Ruff on changed Python files | Passed. |
| Ruff formatting on changed Python files | Passed. |
| Focused mypy on the three new modules | Passed. |
| Full mypy compared with isolated HEAD | **79 errors on each; no new errors**. |
| Repository-wide Ruff lint | Three existing import-order errors in unchanged `tests/test_dashboard_manual_reviews.py`. |
| Repository-wide formatting | Existing CRLF/format differences; the installed Ruff formatter crashed while rendering the large diagnostic output. |
| `git diff --check` | Passed. |

Final focused and expanded regression runs removed platform/model credentials and
blocked external sockets. All added tests use fake providers/model responses.
An earlier full-suite test inherited a configured GitHub token and attempted a
read-only request; its expected missing-token assertion failed. It passed when
rerun with credentials removed. These mocked test runs made no paid model calls
or remote writes. Separate live testing below used the configured model service.

The remaining 13 full-suite failures were rerun against an isolated copy of the
unmodified HEAD source and reproduced:

- Existing walkthrough-heading and confidence-label expectation mismatches.
- Windows permission/path differences in admin-password mode, directory context,
  nested GitLab owner paths, and CRLF policy fixtures.
- Missing optional `boto3` for a Bedrock dispatch test.
- Four SPA fixture failures under the installed FastAPI/Starlette versions.

The expanded suite's three failures were the existing confidence-label,
directory-context, and CRLF policy-fixture cases. Existing tests were not weakened
or disabled to obtain the passing feature checks.

## Compatibility and remaining verification

Existing standard/manual APIs, the Notif MR dashboard flow, and manual CLI
read-only defaults remain covered. Task reviews do not advance standard
incremental SHA or review-rest checkpoints. Configured policy, security,
self-critique, language, custom rules, and model tiers remain in the shared engine.

Live GitHub testing used PR #5 in `zmazloom/SpringBootBase`, pinned to
`9c91e2885e44f0651d35642715eed64677f77216`, with the user's Persian food
CRUD/bulk/cache task. The real dashboard login and API were exercised through
an ASGI transport: unauthenticated POST 401, authenticated POST 202, status and
findings GET 200. `post_comments` was false throughout; no remote mutations
were performed. The configured organizational model, policy passes, Persian
output, security and self-critique settings were retained.

Two initial live runs exposed final-assessment token starvation. The correction
protects task verification and a forced structured submission inside that phase.
The final live run (`7221b928c77b4f7eaa2a91d14f0e8fb0`) completed in 461.828
seconds: 281,794 actual provider-reported tokens, 40 files retrieved, 19 paths
exposed as context, 15 tool calls, and seven raw findings. Requirements were
three implemented, one partial, and six unverified. Coverage remained explicitly
partial; bounded initial retrieval, omitted excerpts, and earlier pass-budget
exhaustion were reported. No exhaustive verification is claimed.

Independent pinned-source inspection confirmed an introduced duplicate Java
controller method and a new null-returning service stub. It found no bulk
creation path or caching implementation in the inspected integration points.
Of 46 eligible tree files, 45 had nonempty source; the remaining Arabic message
resource was empty/unavailable. This independent check did not execute target code.

The configured model's quality remains a material limitation: extraction omitted
separate read/update/delete requirements and added unsupported implementation
choices; bulk/cache stayed unverified; raw findings included an incorrect unused
import claim and repeated descriptions of the same defect. Evidence validation
checks source quotations, not semantic truth. The implementation therefore does
not yet demonstrate reliable end-to-end task completeness on this live example.
After that live run, a parser-only correction clears the initial "Not yet verified"
placeholder when an evidence-backed assessment supplies a verified status but
omits an uncertainty field; mocked regression coverage verifies this behavior.
Detailed raw results and an independently checked report are retained locally
under ignored `data/task-aware-live/20261020T100546Z/`.

Live GitLab review/publication and paid-model quality across other models remain
unverified.
Docker startup was not tested because its daemon was stopped. The dashboard
compiled and built, but no interactive browser end-to-end session was run.

Coverage is selective static investigation, not exhaustive repository execution.
Job concurrency follows the existing single-worker process model; running jobs
are not resumable after restart. Source and findings caches are job-local, while
completed database results require an operational retention policy. Token limits
use existing provider estimates and output reservations; internal retries mean
they are not exact billing caps. Remote publication remains non-transactional.
