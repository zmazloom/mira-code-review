# Coding handwriting in Task-Aware Review

Task-aware reviews now compare changed code with selected unchanged source
components at the same pinned source-branch SHA. This runs automatically, with
the existing API request and dashboard. Standard reviews are unchanged.

## Integration and reuse

`TaskReviewSession.prepare` retains changed-first retrieval, existing import
resolution, index dependency hints, and the job-local provider/source cache.
It prioritizes at most eight navigation candidates for nearby peers and relevant
configuration within the existing retrieval loop and depth/file limits. Candidate
ranking favors the same language and package with a matching component role;
configuration from unrelated modules does not consume that window. It does
not add another repository scan, clone, index, provider, or review engine.

`mira.index.coding_conventions.CodingConventions` uses the existing heuristic
`extract_symbols` implementation to select comparable operation excerpts. Source
annotations, declared classes, and methods establish a provisional architectural
role; filenames are only navigation hints. Test components are kept separate.
Same-package peers take precedence, followed by peers within a manifest-defined
module and comparable components where no local examples are known. Existing
local examples are not overridden simply to obtain three examples elsewhere.
Within a peer scope, declared operations with matching action names are sampled
first, so unrelated methods in otherwise similar services consume fewer slots.
Constructors are excluded from that ranking.

The existing `extract_conventions` handles contributor documentation. Selected
cached lint/formatter configuration supplies additional evidence; configuration
is not assumed to be enforced unless supported by repository evidence. No
repository commands, formatters, or configuration scripts are executed.

Only the existing final task-assessment prompt gains the consistency instruction
and profile. Main review, company-policy, security, and other passes retain their
existing prompts. There is no new extraction or full review LLM call. Existing
on-demand tools can read further evidence under the shared tool/time budget;
newly read peers are eligible even when the agent submits directly without a
forced fallback. Existing configured self-critique still applies to new findings.

## Evidence and precedence

The assessment considers naming, operation structure, DI, layering/interfaces,
shared mappings/utilities, exceptions, API contracts, validation, persistence,
transactions, null handling, collections, logging, documentation, and tests.
These are investigation dimensions, not assumptions about any particular repo.

The instruction order is explicit:

1. Installation-local company rules, loaded by the existing mechanism.
2. Demonstrably enforced formatting/lint/static-analysis rules.
3. Established patterns in the same package/module.
4. Functionally comparable existing components.
5. Broader repository patterns.
6. General framework/language practices.

All repository excerpts, configuration, and contributor documents remain
untrusted user-context data. They cannot override company/system instructions.
Observed legacy practices do not establish a requirement to weaken correctness,
security, or maintainability. Formatting and personal preferences are excluded.

Findings use the existing `ReviewComment` and free-string category schema with
`codebase_consistency`; no enum, API, or database migration is needed. Additional
optional structured result fields are:

```json
{
  "category": "codebase_consistency",
  "consistency_kind": "architectural",
  "consistency_assessment": "violation",
  "convention_dimension": "error_handling",
  "comparison_target": "app/services/food.py",
  "convention_scope": "package:app/services",
  "convention_evidence": [
    {"path": "app/services/order.py", "start_line": 6, "end_line": 6,
     "quote": "            raise DomainError(\"invalid request\")"}
  ]
}
```

The abbreviated example shows one reference; acceptance requires valid evidence
from **three distinct selected unchanged peer files**, as well as source evidence
at the changed trigger and finding location. The change must include an added or
modified line, not merely unchanged hunk context. A finding also needs a concrete
impact, recommendation, kind/dimension/scope, and confidence of at least 0.85.
General task findings retain their existing validation and thresholds.

The model must distinguish `violation`, `justified_deviation`, `legacy_pattern`,
`ambiguous`, and `preference`; only `violation` can produce a consistency finding.
Mixed observed method naming or generic/specific exception styles suppress that
inferred dimension. Three samples support a scoped comparison, not a claim of a
universal repository standard. Insufficient evidence produces no inferred violation.

Stylistic findings are capped at `nitpick` after normal severity classification.
Architectural/behavioral findings use the existing classifier and filter. Company
findings keep their policy path and priority. Existing composite deduplication
suppresses overlap with Clean Code, architecture, and policy findings. Inline
positions and summary routing use the existing diff-line validator and publisher.
The dashboard and published body receive the recommendation and peer references;
API clients also receive structured evidence and grouping by category/severity.

## Cost and caching

The profile occupies at most 1,200 estimated tokens and at most one quarter of
the existing source-excerpt allowance. It replaces part of that allowance rather
than increasing it. Each peer contributes one operation excerpt of at most twelve
lines, capped at 4,000 characters. Fast/balanced/deep profiles contain at most
one/three/five changed-component comparisons, further restricted by token fit.
The fixed assessment instructions/schema add a small bounded prompt overhead;
existing per-call and overall admission guards still apply.

No extra provider operation or LLM call is introduced. Peer prioritization can
change which unchanged files are retrieved within the existing cap. Symbol spans
are cached by path/content hash in a single pinned job instance, preventing reuse
across reviews, users, repositories, or commits. Profiles are reconstructed from
current cached source/configuration so changed configuration is never hidden by
a stale profile. No persistent convention/findings cache is added.

Results include `coding_conventions.status` (`not_evaluated`, `selective`, or
`insufficient_evidence`), `compared_files`, and a `profile_token_estimate` when
rendered. These report selected context, not exhaustive convention compliance or
provider-billed token usage.

## Tests and limitations

Run offline mocked tests:

```sh
pytest tests/test_coding_conventions.py tests/test_task_review.py \
  tests/test_manual_review.py tests/test_dashboard_manual_reviews.py -q
ruff check src/mira/index/coding_conventions.py src/mira/core/task_review.py \
  src/mira/dashboard/task_reviews.py tests/test_coding_conventions.py
```

Tests cover naming, exception contracts, mapping reuse, architecture, comparable
operation selection, module precedence, ambiguous evidence, safer deviations,
legacy/preference suppression, cache/config invalidation, token/depth limits,
on-demand evidence, rule/language preservation, deduplication, malformed outputs,
inline/summary publication, API metadata, read-only behavior, and existing reviews.
They use fake providers and model responses; no live service or remote write is
required. Semantic examples verify the prompt/validation integration with controlled
model output, not real-model accuracy.

Role/symbol extraction and module boundaries are heuristic. Small modules with
fewer than three eligible peers and large components that do not fit the profile
are deliberately left without inferred-convention findings. Naming/exception
ambiguity detection is conservative and does not prove consistency in other
dimensions. The LLM must still judge functional comparability, legitimate design
improvements, and actual impact. Source-quote validation alone cannot prove these
semantic judgments. Live model quality and interactive browser behavior were not
retested for this extension.
