"""Task-aware verification uses the real pipeline with fake providers and LLMs."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.core.task_budget import BudgetedLLM, ReviewBudget, TaskBudgetExceeded
from mira.core.task_review import (
    TaskReviewOptions,
    TaskReviewSession,
    safe_source_path,
)
from mira.dashboard import manual_reviews as jobs
from mira.dashboard import task_reviews as api
from mira.index.context import ProviderSourceFetcher
from mira.llm.prompts.review import build_company_review_prompt, build_review_prompt
from mira.manual_review import ManualReviewCommand, parse_pull_request_reference
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider

URL = "https://github.com/target/project/pull/1"
DIFF = "diff --git a/service.py b/service.py\n--- a/service.py\n+++ b/service.py\n@@ -1,2 +1,2 @@\n class AssignmentService:\n-    pass\n+    def create(self): return None\n"
SOURCE = "class AssignmentService:\n    def create(self): return None\n"
CALLER = "from service import AssignmentService\nresult = AssignmentService().create()\nprint(result.id)\n"


def info(**kwargs):
    return PRInfo(
        title="Implement assignments",
        description="Untrusted description",
        base_branch="main",
        head_branch="feature",
        url=URL,
        number=1,
        owner="target",
        repo="project",
        head_sha="a" * 40,
        source_owner="fork",
        source_repo="project",
        base_sha="b" * 40,
        merge_base_sha="c" * 40,
        **kwargs,
    )


class FakeLLM:
    supports_tool_calling = False
    usage = {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}

    def __init__(self, assessment=None):
        self.messages = []
        self.assessment = assessment or {
            "requirements": [{"id": "R1", "status": "missing", "evidence": [], "confidence": 0.95}],
            "findings": [],
        }

    def count_tokens(self, text):
        return len(text) // 4

    async def complete(self, messages, **kwargs):
        self.messages.append(messages)
        if "Extract only explicit" in messages[0]["content"]:
            return json.dumps(
                {
                    "requirements": [
                        {"description": "Create assignments"},
                        {"description": "Delete assignments"},
                    ],
                    "search_terms": ["AssignmentService"],
                }
            )
        return json.dumps(self.assessment)

    async def review(self, messages, **kwargs):
        self.messages.append(messages)
        return '{"comments":[],"summary":"Review checked"}'

    async def walkthrough(self, messages, **kwargs):
        return '{"summary":"Changes"}'


def provider():
    p = AsyncMock()
    p.get_pr_info.return_value = info()
    p.get_task_diff.return_value = DIFF
    p.get_pr_diff.return_value = DIFF
    p.get_repo_tree.return_value = [
        "service.py",
        "unrelated/ui.py",
        "database/schema.sql",
        ".env",
        "secrets/password.json",
    ]
    p.get_file_content.side_effect = lambda metadata, path, ref: {
        "service.py": SOURCE,
        "unrelated/ui.py": CALLER,
        "database/schema.sql": "create table assignments(id int);",
    }.get(path, "")
    p.get_all_bot_threads.return_value = []
    p.get_unresolved_bot_threads.return_value = []
    p.get_file_history.return_value = {}
    p.find_bot_comment.return_value = None
    return p


def config():
    c = MiraConfig()
    c.review.walkthrough = False
    c.review.include_summary = False
    c.review.self_critique = False
    c.review.security_pass = False
    c.review.osv_scan = False
    c.review.dependency_overlap = False
    c.review.agentic_tools = False
    c.review.auto_resolve_conversations = False
    return c


@pytest.fixture
def storage():
    store = MagicMock()
    store.get_blast_radius.return_value = []
    store.get_summaries.return_value = {}
    store.get_repo_rules.return_value = []
    store.get_learned_rules.return_value = []
    store.get_learned_rules_text.return_value = []
    store.get_all_review_context_text.return_value = ""
    with patch("mira.index.store.IndexStore.open", return_value=store):
        yield store


def evidence(path="service.py", line=2, quote="def create(self): return None"):
    return {"path": path, "start_line": line, "end_line": line, "quote": quote}


@pytest.mark.asyncio
async def test_real_engine_retrieves_unchanged_caller_and_pins_fork(storage):
    p = provider()
    llm = FakeLLM()
    engine = ReviewEngine(config(), llm, p, dry_run=True)
    session = TaskReviewSession("Implement CRUD for assignments", TaskReviewOptions())
    session.configure(engine)
    with patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])):
        result = await engine.review_pr(URL)
    assert "unrelated/ui.py" in session.context
    assert "print(result.id)" in session.context
    assert len(session.requirements) == 2
    assert all(r.status == "unverified" for r in session.requirements)
    p.get_task_diff.assert_awaited_once()
    p.get_pr_diff.assert_not_awaited()
    p.get_repo_tree.assert_awaited_once_with(engine._pr_info, "a" * 40)
    assert all(call.args[2] == "a" * 40 for call in p.get_file_content.await_args_list)
    assert "secrets/password.json" not in session.sources
    p.post_review.assert_not_awaited()
    p.post_comment.assert_not_awaited()
    assert result.task_review["mode"] == "TASK_AWARE"


@pytest.mark.asyncio
async def test_standard_engine_still_uses_standard_diff(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    with patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])):
        result = await engine.review_pr(URL)
    p.get_pr_diff.assert_awaited_once()
    p.get_task_diff.assert_not_awaited()
    assert result.task_review is None


@pytest.mark.asyncio
async def test_missing_requires_evidence_and_omitted_requirements_stay_unverified(storage):
    assessment = {
        "requirements": [
            {
                "id": "R1",
                "status": "missing",
                "confidence": 0.9,
                "evidence": [evidence()],
                "uncertainty": "Expected implementation is empty",
            },
            {
                "id": "R2",
                "status": "missing",
                "confidence": 0.9,
                "evidence": [evidence(quote="invented source")],
            },
        ]
    }
    engine = ReviewEngine(config(), FakeLLM(assessment), provider(), dry_run=True)
    session = TaskReviewSession("Implement CRUD", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    await session.assess(engine, ReviewResult(), DIFF)
    assert [r.status for r in session.requirements] == ["missing", "unverified"]


@pytest.mark.asyncio
async def test_verified_missing_requirements_remain_separate_and_unverified_is_internal(storage):
    quote = "    def create(self): return None"
    def missing(path_title, requirement_id, suggestion):
        return {
            "path": "service.py",
            "line": 2,
            "title": path_title,
            "body": f"{path_title} is not implemented at the service integration point.",
            "severity": "warning",
            "category": "bug",
            "confidence": 0.96,
            "evidence": [evidence("service.py", 2, quote)],
            "impact": "The requested operation cannot be called.",
            "suggestion": suggestion,
            "agent_prompt": f"Implement {path_title} in AssignmentService without changing create(). Add focused tests.",
            "requirement_id": requirement_id,
            "introduced_by_pr": True,
        }

    assessment = {
        "requirements": [
            {"id": "R1", "status": "missing", "evidence": [evidence("service.py", 2, quote)], "confidence": 0.96},
            {"id": "R2", "status": "missing", "evidence": [evidence("service.py", 2, quote)], "confidence": 0.96},
        ],
        "findings": [
            missing("Bulk service operation is missing", "R1", "Add createAll with a collection request."),
            missing("Cache invalidation is missing", "R2", "Evict the food cache after successful writes."),
        ],
    }
    p = provider()
    llm = FakeLLM(assessment)
    engine = ReviewEngine(config(), llm, p, dry_run=True)
    session = TaskReviewSession("Add bulk create and caching", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    result = ReviewResult()
    await session.assess(engine, result, DIFF)

    assert {comment.title for comment in result.comments} == {
        "Bulk service operation is missing",
        "Cache invalidation is missing",
    }
    assert all(comment.agent_prompt for comment in result.comments)
    assert all(requirement.status == "missing" for requirement in session.requirements)
    system = llm.messages[-1][0]["content"]
    assert "separate actionable finding" in system
    assert "Do not turn unverified requirements into defects" in system


@pytest.mark.asyncio
async def test_findings_on_unchanged_files_are_summary_only(storage):
    finding = {
        "path": "unrelated/ui.py",
        "line": 3,
        "title": "Caller dereferences missing result",
        "body": "create returns None",
        "severity": "warning",
        "category": "bug",
        "confidence": 0.95,
        "evidence": [evidence("unrelated/ui.py", 3, "print(result.id)")],
        "impact": "Calling the screen raises AttributeError.",
        "requirement_id": "R1",
        "introduced_by_pr": True,
    }
    engine = ReviewEngine(config(), FakeLLM({"findings": [finding]}), provider(), dry_run=True)
    session = TaskReviewSession("Implement CRUD", TaskReviewOptions(post_comments=True))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    result = ReviewResult(summary="Task review")
    await session.assess(engine, result, DIFF)
    assert not result.comments
    assert len(result.repository_comments) == 1
    session.limitations.append("Initial content search was bounded INTERNAL_DIAGNOSTIC")
    result.summary = "Aggregated summary must not be published"
    await session.publish(engine, result)
    engine.provider.post_review.assert_not_awaited()
    body = engine.provider.post_comment.call_args.args[1]
    assert "unrelated/ui.py:3" in body
    assert "Caller dereferences" in body
    assert "**اشکال:**" in body
    assert "**راهکار:**" in body
    assert "**پرامپت رفع مشکل:**" in body
    assert "unrelated/ui.py" in body.split("```text", 1)[1]
    assert "INTERNAL_DIAGNOSTIC" not in body
    assert "Aggregated summary" not in body
    assert "[R1]" not in body


@pytest.mark.asyncio
async def test_inline_positions_and_duplicate_publication(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions(post_comments=True))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    comment = ReviewComment(
        "service.py",
        2,
        None,
        Severity.WARNING,
        "bug",
        "Incorrect result",
        "No assignment is returned",
        0.95,
        agent_prompt="In service.py, fix create() so it returns a valid assignment.",
        source_pass="task",
    )
    result = ReviewResult(comments=[comment], task_review=session.report())
    await session.publish(engine, result)
    p.post_review.assert_awaited_once()
    assert p.post_review.call_args.args[1].comments[0].line == 2
    p.get_unresolved_bot_threads.return_value = [
        SimpleNamespace(path="service.py", line=2, body="**Incorrect result**\nNo assignment is returned")
    ]
    await session.publish(engine, result)
    assert p.post_review.await_count == 1
    p.post_comment.assert_not_awaited()
    p.update_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_separate_findings_publish_individually_without_review_summary(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("Implement CRUD", TaskReviewOptions(post_comments=True))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    first = ReviewComment(
        "service.py", 2, None, Severity.WARNING, "bug", "Missing bulk service", "No bulk operation is implemented.", 0.95,
        source_pass="task",
    )
    second = ReviewComment(
        "service.py", 2, None, Severity.WARNING, "bug", "Bulk endpoint is missing", "The controller exposes no bulk route.", 0.95,
        source_pass="task",
    )
    outside = ReviewComment(
        "caller.py", 8, None, Severity.WARNING, "bug", "Caller contract mismatch", "The unchanged caller expects a collection.", 0.95,
        source_pass="task",
    )
    result = ReviewResult(
        comments=[first, second],
        repository_comments=[outside],
        summary="Combined issues and INTERNAL_TOKEN_BUDGET_DIAGNOSTIC",
        task_review={"mode": "TASK_AWARE"},
    )
    session.limitations.append("Selected source context exceeded its budget")
    await session.publish(engine, result)

    posted_review = p.post_review.call_args.args[1]
    assert len(posted_review.comments) == 2
    assert posted_review.summary == ""
    assert "Missing bulk service" != "Bulk endpoint is missing"
    assert p.post_comment.await_count == 1
    note = p.post_comment.call_args.args[1]
    assert "Caller contract mismatch" in note
    assert "INTERNAL_TOKEN_BUDGET_DIAGNOSTIC" not in note
    assert "Selected source context exceeded its budget" not in note
    assert "R1 [" not in note
    assert note.count("**اشکال:**") == 1
    assert note.count("**پرامپت رفع مشکل:**") == 1


@pytest.mark.asyncio
async def test_same_location_distinct_task_findings_survive_noise_dedup(storage):
    from mira.core.noise_filter import filter_noise

    comments = [
        ReviewComment("service.py", 2, None, Severity.WARNING, "bug", "Missing bulk service", "No bulk service operation exists.", 0.96, source_pass="task"),
        ReviewComment("service.py", 2, None, Severity.WARNING, "bug", "Bulk endpoint is missing", "The controller has no collection endpoint.", 0.96, source_pass="task"),
    ]
    assert len(filter_noise(comments, config().filter)) == 2


@pytest.mark.asyncio
async def test_standalone_findings_update_by_per_finding_marker(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("Implement bulk create", TaskReviewOptions(post_comments=True))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    comments = [
        ReviewComment(
            "caller.py", line, None, Severity.WARNING, "bug", title, title + " is missing.", 0.96,
            source_pass="task",
        )
        for line, title in ((3, "Bulk service operation"), (8, "Bulk endpoint"))
    ]
    existing = {}

    async def find_comment(_pr, marker):
        return existing.get(marker)

    async def post_comment(_pr, body):
        marker = body.rsplit("\n\n", 1)[-1]
        existing[marker] = len(existing) + 100

    p.find_bot_comment.side_effect = find_comment
    p.post_comment.side_effect = post_comment
    result = ReviewResult(repository_comments=comments)
    await session.publish(engine, result)
    await session.publish(engine, result)

    assert p.post_comment.await_count == 2
    assert p.update_comment.await_count == 2
    assert p.post_review.await_count == 0
    assert len(existing) == 2


@pytest.mark.asyncio
async def test_read_only_and_stale_head_never_publish(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    assert await session.publish(engine, ReviewResult()) == "disabled"
    p.post_comment.assert_not_awaited()
    p.update_comment.assert_not_awaited()
    session.options.post_comments = True
    p.get_pr_info.return_value = replace(info(), head_sha="new")
    assert await session.publish(engine, ReviewResult()) == "stale_head"
    p.post_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_cache_is_job_local_and_fetches_once(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("First task", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    count = p.get_file_content.await_count
    await asyncio.gather(session.fetch("service.py"), session.fetch("service.py"))
    assert p.get_file_content.await_count == count
    other = TaskReviewSession("Second task", TaskReviewOptions())
    assert not other.sources
    other_engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    other.configure(other_engine)
    await other.prepare(other_engine, replace(info(), head_sha="d" * 40), DIFF)
    assert p.get_file_content.await_count > count
    assert p.get_file_content.call_args.args[2] == "d" * 40


@pytest.mark.asyncio
async def test_company_policy_and_output_language_are_preserved(storage, tmp_path):
    storage.get_all_review_context_text.return_value = "Installed organization architecture context"
    rules = tmp_path / "policy.md"
    rules.write_text("Always use the existing AssignmentStore abstraction.")
    c = config()
    c.review.rules_file = str(rules)
    c.review.output_language = "fa"
    llm = FakeLLM()
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Implement assignments", TaskReviewOptions())
    session.configure(engine)
    with patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])):
        await engine.review_pr(URL)
    systems = "\n".join(m[0]["content"] for m in llm.messages)
    assert "Always use the existing AssignmentStore abstraction." in systems
    assert "Persian" in systems or "فارسی" in systems
    assert any("company" in m[0]["content"].lower() for m in llm.messages)
    assert any(
        "Installed organization architecture context" in m[1]["content"] for m in llm.messages
    )
    assert "Installed organization architecture context" in llm.messages[-1][1]["content"]


@pytest.mark.asyncio
async def test_context_and_overall_token_guards():
    llm = FakeLLM()
    guarded = BudgetedLLM(llm, ReviewBudget(100, 200, 10))
    with pytest.raises(TaskBudgetExceeded):
        await guarded.complete([{"role": "user", "content": "a" * 1000}])
    assert not llm.messages
    budget = ReviewBudget(3000, 3500, 1500)
    guarded = BudgetedLLM(llm, budget)
    await guarded.complete([{"role": "user", "content": "Extract only explicit"}])
    with pytest.raises(TaskBudgetExceeded):
        await guarded.complete([{"role": "user", "content": "again"}])
    assert budget.limitations


@pytest.mark.asyncio
async def test_tool_budget_and_existing_code_option(storage):
    engine = ReviewEngine(config(), FakeLLM(), provider(), dry_run=True)
    session = TaskReviewSession(
        "Create assignments", TaskReviewOptions(include_existing_code=False, max_tool_calls=0)
    )
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    assert session.tree == ["service.py"]
    output = await session.executor.execute("read_file", {"path": "service.py"})
    assert "budget exhausted" in output
    assert session.limitations


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/etc/password.py",
        "C:/secret.py",
        "src\\secret.py",
        "a/../secret.py",
        ".env",
        "secrets/token.json",
        "x.py?ref=main",
        "x%2f.py",
        ".git/config",
    ],
)
def test_unsafe_repository_paths(path):
    assert not safe_source_path(path)


@pytest.mark.asyncio
async def test_source_fetcher_rejects_path_traversal():
    p = provider()
    fetcher = ProviderSourceFetcher(p, info(), "a" * 40)
    assert await fetcher.fetch("../secret") is None
    p.get_file_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_metadata_resolves_fork_and_base():
    p = GitHubProvider.__new__(GitHubProvider)
    p._github = MagicMock()
    pr = MagicMock()
    pr.title, pr.body, pr.number, pr.html_url = "Title", "Description", 1, URL
    pr.head.ref, pr.head.sha, pr.head.repo.full_name = "fork-branch", "head", "contributor/fork"
    pr.base.ref, pr.base.sha = "main", "base"
    p._github.get_repo.return_value.get_pull.return_value = pr
    metadata = await p.get_pr_info(URL)
    assert metadata.source_repository == "contributor/fork"
    assert (metadata.head_sha, metadata.base_sha) == ("head", "base")


@pytest.mark.asyncio
async def test_github_pinned_comparison_and_source_api():
    p = GitHubProvider.__new__(GitHubProvider)
    p._token = "fake"
    metadata = info()
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if "/compare/" in request.url.path:
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=DIFF)
            return httpx.Response(
                200,
                json={
                    "merge_base_commit": {"sha": "merge-base"},
                    "commits": [{"sha": "head", "commit": {"message": "intent"}}],
                },
            )
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [{"path": "service.py", "type": "blob"}]})
        return httpx.Response(200, json={"content": "aGk="})

    client_type = httpx.AsyncClient
    with patch(
        "mira.providers.github.httpx.AsyncClient",
        lambda *a, **k: client_type(transport=httpx.MockTransport(handle)),
    ):
        assert await p.get_task_diff(metadata) == DIFF
        assert await p.get_repo_tree(metadata, metadata.head_sha) == ["service.py"]
        assert await p.get_file_content(metadata, "service.py", metadata.head_sha) == "hi"
    assert metadata.merge_base_sha == "merge-base"
    assert any("/repos/fork/project/git/trees/" in url for url in seen)
    assert all("feature" not in url for url in seen)
    assert any(f"{metadata.base_sha}...{metadata.head_sha}" in url for url in seen)


@pytest.mark.asyncio
async def test_gitlab_pinned_version_and_source_project():
    p = GitLabProvider("fake")
    metadata = replace(info(), platform="gitlab", source_project_id=42)
    p._paginate = AsyncMock(return_value=[{"id": 9, "head_commit_sha": metadata.head_sha}])

    async def response(method, url, **kwargs):
        if url.endswith("/versions/9"):
            data = {
                "head_commit_sha": metadata.head_sha,
                "start_commit_sha": "target-base",
                "base_commit_sha": "merge",
                "state": "collected",
                "commits": [{"id": "pinned-commit", "message": "Intent, not proof"}],
                "diffs": [
                    {
                        "old_path": "service.py",
                        "new_path": "service.py",
                        "diff": "@@ -1 +1 @@\n-old\n+new\n",
                    }
                ],
            }
        elif url.endswith("/projects/42"):
            data = {"path_with_namespace": "fork/team/project"}
        else:
            return SimpleNamespace(status_code=200, text=SOURCE)
        return SimpleNamespace(json=lambda: data)

    p._request = AsyncMock(side_effect=response)
    diff = await p.get_task_diff(metadata)
    assert "+new" in diff
    assert metadata.source_repository == "fork/team/project"
    assert metadata.merge_base_sha == "merge"
    assert metadata.commits[0]["sha"] == "pinned-commit"
    assert p._request.await_count == 2
    await p.get_file_content(metadata, "service.py", metadata.head_sha)
    assert "/projects/42/repository/files/" in p._request.call_args.args[1]


@pytest.mark.asyncio
async def test_gitlab_metadata_extracts_source_project_id():
    p = GitLabProvider("fake")
    p._request = AsyncMock(
        return_value=SimpleNamespace(
            json=lambda: {
                "source_project_id": 42,
                "sha": "head",
                "source_branch": "fork",
                "target_branch": "main",
                "diff_refs": {"base_sha": "merge", "start_sha": "base"},
            }
        )
    )
    metadata = await p.get_pr_info("https://gitlab.com/target/project/-/merge_requests/1")
    assert (metadata.source_project_id, metadata.head_sha, metadata.base_sha) == (
        42,
        "head",
        "base",
    )


@pytest.fixture
def api_client(monkeypatch):
    memory = {}
    db = SimpleNamespace(set_setting=memory.__setitem__, get_setting=memory.get)
    monkeypatch.setattr(api, "_db", lambda: db)
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("GITLAB_TOKEN", "fake")
    monkeypatch.setenv("MIRA_GITLAB_API_URL", "https://gitlab.com/api/v4")
    jobs._jobs.clear()
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.headers.get("x-test-user"):
            request.state.user = SimpleNamespace(id=int(request.headers["x-test-user"]))
        return await call_next(request)

    app.include_router(api.router)
    monkeypatch.setattr(api, "_run", AsyncMock())
    with TestClient(app) as client:
        yield client, memory
    jobs._jobs.clear()


@pytest.mark.parametrize(
    "body",
    [
        {"pr_url": URL, "task_prompt": "  "},
        {"pr_url": "http://127.0.0.1/a/b/pull/1", "task_prompt": "CRUD"},
        {"pr_url": "https://github.com/a/../pull/1", "task_prompt": "CRUD"},
        {"pr_url": "https://secret:token@github.com/a/b/pull/1", "task_prompt": "CRUD"},
        {"pr_url": "https://evil.test/a/b/-/merge_requests/1", "task_prompt": "CRUD"},
        {"pr_url": "https://github.com:abc/a/b/pull/1", "task_prompt": "CRUD"},
        {"pr_url": "https://github.com:444/a/b/pull/1", "task_prompt": "CRUD"},
        {"pr_url": "https://github.com/a b/c/pull/1", "task_prompt": "CRUD"},
        {"pr_url": URL, "task_prompt": "CRUD", "review_options": {"depth": "unknown"}},
        {"pr_url": URL, "task_prompt": "CRUD", "review_options": {"max_context_tokens": 1}},
    ],
)
def test_api_validation(api_client, body):
    client, _ = api_client
    assert (
        client.post("/api/reviews/task-aware", json=body, headers={"x-test-user": "1"}).status_code
        == 422
    )


def test_api_auth_async_ownership_and_persistence(api_client):
    client, memory = api_client
    body = {"pr_url": URL, "task_prompt": "Implement CRUD"}
    assert client.post("/api/reviews/task-aware", json=body).status_code == 401
    response = client.post("/api/reviews/task-aware", json=body, headers={"x-test-user": "1"})
    assert response.status_code == 202
    review_id = response.json()["id"]
    assert response.json()["mode"] == "TASK_AWARE"
    assert (
        client.get(f"/api/reviews/task-aware/{review_id}", headers={"x-test-user": "2"}).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/reviews/task-aware/{review_id}/findings", headers={"x-test-user": "1"}
        ).status_code
        == 200
    )
    assert (
        client.post("/api/reviews/task-aware", json=body, headers={"x-test-user": "1"}).status_code
        == 409
    )
    persisted = json.loads(memory["task_review:" + review_id])
    persisted["result"]["status"] = "done"
    memory["task_review:" + review_id] = json.dumps(persisted)
    jobs._jobs.clear()
    assert (
        client.get(f"/api/reviews/task-aware/{review_id}", headers={"x-test-user": "1"}).json()[
            "status"
        ]
        == "done"
    )


@pytest.mark.parametrize("prompt", ["", "  ", "\x00bad"])
def test_prompt_validation(prompt):
    with pytest.raises(ValidationError):
        api.TaskReviewRequest(pr_url=URL, task_prompt=prompt)


@pytest.mark.asyncio
async def test_model_override_rejected_when_not_advertised(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(
        jobs, "review_models", AsyncMock(return_value={"options": [{"value": "known"}]})
    )
    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=1)))
    with pytest.raises(HTTPException) as error:
        await api.start_task_review(
            api.TaskReviewRequest(pr_url=URL, task_prompt="CRUD", model="unknown"), request
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_manual_command_task_uses_existing_engine(storage):
    p = provider()
    with (
        patch("mira.manual_review.create_llm", side_effect=lambda config: FakeLLM()),
        patch.object(ReviewEngine, "_detect_overlaps_safe", AsyncMock(return_value=[])),
    ):
        command = ManualReviewCommand(config=config(), provider=p, task_prompt="Implement CRUD")
        outcome = await command.execute(parse_pull_request_reference(URL))
    assert outcome.result.task_review["mode"] == "TASK_AWARE"
    assert outcome.result.task_review["publishing_status"] == "disabled"
    assert command.engine.dry_run is True
    p.post_comment.assert_not_awaited()
    p.post_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_assessment_can_investigate_using_existing_tools(storage):
    llm = FakeLLM()
    llm.supports_tool_calling = True
    llm.complete_agentic = AsyncMock(
        side_effect=[
            {
                "tool_calls": [
                    {
                        "id": "read",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"unrelated/ui.py"}',
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "submit",
                        "function": {
                            "name": "submit_review",
                            "arguments": json.dumps(
                                {
                                    "requirements": [
                                        {
                                            "id": "R1",
                                            "status": "incorrect",
                                            "confidence": 0.95,
                                            "evidence": [
                                                evidence("unrelated/ui.py", 3, "print(result.id)")
                                            ],
                                        }
                                    ]
                                }
                            ),
                        },
                    }
                ]
            },
        ]
    )
    c = config()
    c.review.agentic_tools = True
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    await session.assess(engine, ReviewResult(), DIFF)
    assert session.requirements[0].status == "incorrect"
    assert session.executor.call_log[0]["tool"] == "read_file"
    assert session.requirements[0].uncertainty == ""
    tool_schema = llm.complete_agentic.call_args.kwargs["tools"][-1]
    assert "requirements" in tool_schema["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_execution_deadline_cancels_inflight_llm():
    llm = FakeLLM()
    cancelled = asyncio.Event()

    async def blocked(messages, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm.complete = blocked
    budget = ReviewBudget(10000, 20000, 1000, deadline=time.monotonic() + 0.02)
    with pytest.raises(TaskBudgetExceeded):
        await BudgetedLLM(llm, budget).complete([{"role": "user", "content": "Review"}])
    assert cancelled.is_set()
    assert not budget.active_tasks


@pytest.mark.asyncio
async def test_concurrent_source_reads_share_one_provider_call():
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.tree = ["service.py", "other.py"]
    session.fetcher = SimpleNamespace(fetch=AsyncMock(return_value=SOURCE))
    session.max_files = 1
    await asyncio.gather(*(session.fetch("service.py") for _ in range(8)))
    session.fetcher.fetch.assert_awaited_once_with("service.py")
    assert await session.fetch("other.py") is None
    assert any("limit" in message for message in session.limitations)


@pytest.mark.asyncio
async def test_existing_security_and_critique_passes_run_for_task_review(storage):
    c = config()
    c.review.security_pass = True
    c.review.self_critique = True
    comment = ReviewComment(
        "service.py",
        2,
        None,
        Severity.WARNING,
        "security",
        "Broken contract",
        "The returned value is None.",
        0.95,
    )
    engine = ReviewEngine(c, FakeLLM(), provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    with (
        patch(
            "mira.core.engine.security_review_pass", AsyncMock(return_value=[comment])
        ) as security,
        patch("mira.core.engine.self_critique", AsyncMock(return_value=[comment])) as critique,
        patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])),
    ):
        result = await engine.review_pr(URL)
    security.assert_awaited_once()
    critique.assert_awaited_once()
    assert result.comments[0].title == "Broken contract"


@pytest.mark.asyncio
async def test_missing_company_rules_fail_before_llm(storage, tmp_path):
    c = config()
    c.review.rules_file = str(tmp_path / "missing-policy.md")
    llm = FakeLLM()
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    with pytest.raises(Exception, match="rules"):
        await session.prepare(engine, info(), DIFF)
    assert not llm.messages


@pytest.mark.asyncio
async def test_api_worker_returns_sanitized_partial_result_on_timeout(monkeypatch):
    saved = {}
    monkeypatch.setattr(api, "_db", lambda: SimpleNamespace(set_setting=saved.__setitem__))
    engine = ReviewEngine(config(), FakeLLM(), provider(), dry_run=True)
    engine._pr_info = info()
    session = TaskReviewSession("Private task text", TaskReviewOptions())
    session.configure(engine)
    command = SimpleNamespace(
        engine=engine,
        task_session=session,
        execute=AsyncMock(side_effect=TimeoutError("private-token")),
    )
    monkeypatch.setattr(api, "ManualReviewCommand", MagicMock(return_value=command))
    monkeypatch.setattr(api, "create_manual_provider", lambda ref: engine.provider)
    monkeypatch.setattr(api, "load_config", lambda path: engine.config)
    job = jobs.Job(owner=1, result=api.TaskReviewStatus(id="a" * 32, platform="github", url=URL))
    await api._run(
        job,
        api.TaskReviewRequest(pr_url=URL, task_prompt="Private task text"),
        parse_pull_request_reference(URL),
    )
    assert job.result.status == "failed"
    assert job.result.result["partial"] is True
    assert job.result.result["requirements"][0]["status"] == "unverified"
    assert "private-token" not in json.dumps(saved)
    assert job.result.duration_seconds is not None


@pytest.mark.asyncio
async def test_different_tasks_have_different_publish_identities(storage):
    p = provider()
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    markers = []
    p.find_bot_comment.side_effect = lambda _pr, marker: markers.append(marker) or None
    first = TaskReviewSession("Create assignments", TaskReviewOptions(post_comments=True))
    first.configure(engine)
    await first.prepare(engine, info(), DIFF)
    finding = ReviewComment("caller.py", 3, None, Severity.WARNING, "bug", "Missing caller contract", "A result is not returned.", 0.95)
    await first.publish(engine, ReviewResult(repository_comments=[finding]))
    second_engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    second = TaskReviewSession("Delete assignments", TaskReviewOptions(post_comments=True))
    second.configure(second_engine)
    await second.prepare(second_engine, info(), DIFF)
    await second.publish(second_engine, ReviewResult(repository_comments=[finding]))
    assert len(markers) == 2
    assert markers[0] != markers[1]


@pytest.mark.asyncio
async def test_gitlab_overflow_fails_before_review():
    p = GitLabProvider("fake")
    metadata = replace(info(), platform="gitlab", source_project_id=42)
    p._paginate = AsyncMock(return_value=[{"id": 1, "head_commit_sha": metadata.head_sha}])
    p._request = AsyncMock(
        side_effect=[
            SimpleNamespace(json=lambda: {"path_with_namespace": "fork/project"}),
            SimpleNamespace(
                json=lambda: {
                    "head_commit_sha": metadata.head_sha,
                    "state": "overflow",
                    "diffs": [],
                }
            ),
        ]
    )
    with pytest.raises(Exception, match="incomplete"):
        await p.get_task_diff(metadata)


@pytest.mark.asyncio
async def test_github_task_publication_uses_reviewed_commit():
    p = GitHubProvider.__new__(GitHubProvider)
    p._github = MagicMock()
    repository = p._github.get_repo.return_value
    pull = repository.get_pull.return_value
    pinned = repository.get_commit.return_value
    finding = ReviewComment(
        "service.py", 2, None, Severity.WARNING, "bug", "Incorrect return", "Returns None", 0.95
    )
    result = ReviewResult(comments=[finding], task_review={"mode": "TASK_AWARE"})
    await p.post_review(info(), result)
    repository.get_commit.assert_called_once_with("a" * 40)
    pull.get_commits.assert_not_called()
    assert pull.create_review.call_args.kwargs["commit"] is pinned


@pytest.mark.asyncio
async def test_task_search_treats_regex_as_literal(storage):
    engine = ReviewEngine(config(), FakeLLM(), provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    output = await session.executor.execute("grep_repo", {"pattern": "(a+)+$"})
    assert "no content matches" in output
    assert "exceeds" in await session.executor.execute("grep_repo", {"pattern": "x" * 257})


@pytest.mark.asyncio
async def test_same_finding_identity_deduplicates_across_model_changes():
    p = provider()
    markers = []
    for model in ("model-a", "model-b"):
        llm = FakeLLM()
        llm.config = config().llm.model_copy(update={"model": model})
        engine = ReviewEngine(config(), llm, p, dry_run=True)
        session = TaskReviewSession("Same task", TaskReviewOptions(post_comments=True))
        session.configure(engine)
        session.pr_info = info()
        p.find_bot_comment.side_effect = lambda _pr, marker: markers.append(marker) or None
        finding = ReviewComment("caller.py", 3, None, Severity.WARNING, "bug", "Missing caller contract", "A result is not returned.", 0.95)
        await session.publish(engine, ReviewResult(repository_comments=[finding]))
    assert markers[0] == markers[1]


@pytest.mark.asyncio
async def test_initial_scan_reserves_capacity_for_followup(storage):
    p = provider()
    p.get_repo_tree.return_value = ["service.py"] + [f"module{i}.py" for i in range(30)]
    p.get_file_content.side_effect = lambda metadata, path, ref: SOURCE
    engine = ReviewEngine(config(), FakeLLM(), p, dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions(depth="fast"))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    assert len(session.sources) < session.max_files
    pending_path = next(path for path in session.tree if path not in session.sources)
    await session.executor.execute("read_file", {"path": pending_path})
    assert pending_path in session.sources


def test_task_prompt_builders_keep_untrusted_metadata_in_user_messages():
    from mira.core.diff_parser import parse_diff

    marker = "UNTRUSTED_METADATA_DO_NOT_EXECUTE"
    files = parse_diff(DIFF).files
    messages = build_review_prompt(
        files,
        config(),
        pr_title=marker,
        pr_description=marker,
        task_aware=True,
        file_history={
            "service.py": [SimpleNamespace(sha="head", message=marker, author="a", date="d")]
        },
    )
    assert marker not in messages[0]["content"]
    assert marker in messages[1]["content"]
    messages = build_company_review_prompt(
        files,
        config(),
        company_rules=[{"content": "Trusted policy"}],
        pr_title=marker,
        pr_description=marker,
        task_aware=True,
    )
    assert marker not in messages[0]["content"]
    assert marker in messages[1]["content"]
    assert "Trusted policy" in messages[0]["content"]


@pytest.mark.asyncio
async def test_task_verification_reservation_survives_standard_pass_exhaustion(storage):
    llm = FakeLLM(
        {
            "requirements": [
                {
                    "id": "R1",
                    "status": "incorrect",
                    "confidence": 0.95,
                    "evidence": [evidence()],
                    "uncertainty": "Creation returns None.",
                }
            ]
        }
    )
    engine = ReviewEngine(config(), llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions(max_total_tokens=20000))
    session.configure(engine)
    session.budget.verification_reserve = 19000
    with patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])):
        result = await engine.review_pr(URL)
    assert result.task_review["requirements"][0]["status"] == "incorrect"
    assert session.budget.verification_started
    assert any("reserved" in limitation for limitation in session.limitations)
    assert session.budget.reserved <= 20000


@pytest.mark.asyncio
async def test_budget_protects_verification_tokens_until_phase_switch():
    llm = FakeLLM()
    budget = ReviewBudget(3000, 5000, 1000, verification_reserve=4000)
    guarded = BudgetedLLM(llm, budget)
    with pytest.raises(TaskBudgetExceeded, match="reserved"):
        await guarded.complete([{"role": "user", "content": "Check task"}])
    assert not llm.messages
    assert budget.reserved == 0
    budget.verification_started = True
    await guarded.complete([{"role": "user", "content": "Check task"}])
    assert len(llm.messages) == 1


@pytest.mark.asyncio
async def test_final_submission_survives_agentic_budget_and_uses_new_evidence(storage):
    llm = FakeLLM()
    llm.supports_tool_calling = True
    llm.complete_agentic = AsyncMock(
        return_value={
            "tool_calls": [
                {
                    "id": "read",
                    "function": {"name": "read_file", "arguments": '{"path":"unrelated/ui.py"}'},
                }
            ]
        }
    )
    llm.complete_with_tools = AsyncMock(
        return_value=json.dumps(
            {
                "requirements": [
                    {
                        "id": "R1",
                        "status": "incorrect",
                        "confidence": 0.95,
                        "evidence": [evidence("unrelated/ui.py", 3, "print(result.id)")],
                    }
                ]
            }
        )
    )
    c = config()
    c.review.agentic_tools = True
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions(max_total_tokens=20000))
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    session.context_paths = ["service.py"]
    # Permit one investigative call, but preserve a full window for submission.
    llm.count_tokens = lambda text: 200
    session.budget.context_tokens = 10000
    session.budget.output_tokens = 1000
    session.budget.reserved = 7500
    await session.assess(engine, ReviewResult(), DIFF)
    assert llm.complete_agentic.await_count == 1
    llm.complete_with_tools.assert_awaited_once()
    assert "print(result.id)" in llm.complete_with_tools.call_args.args[0][1]["content"]
    assert session.requirements[0].status == "incorrect"
    assert session.budget.reserved <= session.budget.total_tokens
    assert not any("Overall token" in item for item in session.limitations)
