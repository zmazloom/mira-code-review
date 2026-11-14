"""Regressions for incomplete model responses and exhausted review phases."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mira.config import COMPANY_MESSAGES_URL, LLMConfig
from mira.core.engine import ReviewEngine
from mira.core.noise_filter import filter_noise
from mira.core.passes import agentic_review_loop, regenerate_summary
from mira.core.task_budget import BudgetedLLM, ReviewBudget, TaskBudgetExceeded
from mira.core.task_review import TaskReviewOptions, TaskReviewSession
from mira.exceptions import NonRetriableLLMError, OutputLimitLLMError, ResponseParseError
from mira.llm.company_messages import CompanyMessagesProvider
from mira.models import ReviewComment, ReviewResult, Severity
from tests.test_task_review import DIFF, URL, FakeLLM, config, info, provider
from tests.test_task_review import storage as storage


def model_client(data):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        200, json=data, request=httpx.Request("POST", COMPANY_MESSAGES_URL)
    )
    return client


def model_provider():
    return CompanyMessagesProvider(
        LLMConfig(base_url=COMPANY_MESSAGES_URL, max_retries=5, retry_min_wait=0)
    )


@pytest.mark.asyncio
async def test_missing_tool_submission_gets_only_one_explicit_repair():
    client = model_client(
        {
            "content": [{"type": "thinking", "thinking": "private reasoning"}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 12},
        }
    )
    llm = model_provider()
    with (
        patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client),
        patch("mira.llm.company_messages.event") as events,
        pytest.raises(NonRetriableLLMError, match="Invalid model response"),
    ):
        await llm.review([{"role": "user", "content": "Review this diff"}])

    assert client.post.await_count == 2  # Not five transport retries.
    assert (
        "Submit the result now" in client.post.call_args.kwargs["json"]["messages"][-1]["content"]
    )
    assert llm.usage["completion_tokens"] == 24
    assert "private reasoning" not in str(events.call_args_list)
    metadata = next(c.kwargs for c in events.call_args_list if c.args[0] == "llm.response_received")
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["block_types"] == ["thinking"]
    assert metadata["usage"]["output"] == 12


@pytest.mark.asyncio
async def test_truncated_success_response_does_not_repeat_generation():
    client = model_client(
        {"content": [{"type": "text", "text": '{"comments":['}], "stop_reason": "max_tokens"}
    )
    with (
        patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client),
        pytest.raises(NonRetriableLLMError, match="Output limit reached"),
    ):
        await model_provider().review([{"role": "user", "content": "Review"}])
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_agentic_output_limit_does_not_force_a_second_expensive_submission():
    llm = FakeLLM()
    llm.complete_agentic = AsyncMock(
        side_effect=OutputLimitLLMError(
            "invalid_response", reason="Output limit reached before final submission"
        )
    )
    llm.complete_with_tools = AsyncMock()
    budget = ReviewBudget(10_000, 30_000, 1000)
    with pytest.raises(OutputLimitLLMError):
        await agentic_review_loop(
            BudgetedLLM(llm, budget),
            [{"role": "user", "content": "Review"}],
            SimpleNamespace(execute=AsyncMock()),
        )
    llm.complete_agentic.assert_awaited_once()
    llm.complete_with_tools.assert_not_awaited()
    assert any("Model output limit exhausted" in item for item in budget.limitations)


@pytest.mark.asyncio
async def test_company_investigation_bounds_thinking_and_preserves_signed_tool_turn():
    thinking = {"type": "thinking", "thinking": "private", "signature": "signed-block"}
    tool = {
        "type": "tool_use",
        "id": "read-1",
        "name": "read_file",
        "input": {"path": "service.py"},
    }
    client = model_client({"content": [thinking, tool], "stop_reason": "tool_use"})
    response = client.post.return_value
    final = httpx.Response(
        200,
        json={
            "content": [{"type": "tool_use", "name": "submit_review", "input": {"comments": []}}],
            "stop_reason": "tool_use",
        },
        request=httpx.Request("POST", COMPANY_MESSAGES_URL),
    )
    client.post.side_effect = [response, final]
    llm = CompanyMessagesProvider(
        LLMConfig(base_url=COMPANY_MESSAGES_URL, max_tokens=16_000, reasoning_effort="high")
    )
    with patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client):
        result = await agentic_review_loop(
            llm,
            [{"role": "user", "content": "Review"}],
            SimpleNamespace(execute=AsyncMock(return_value="Source evidence")),
            max_hops=1,
        )
    assert json.loads(result) == {"comments": []}
    investigation = client.post.call_args_list[0].kwargs["json"]
    submission = client.post.call_args_list[1].kwargs["json"]
    assert investigation["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert submission["thinking"] == {"type": "disabled"}
    assert next(m for m in submission["messages"] if m["role"] == "assistant")["content"] == [
        thinking,
        tool,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("language, expected", [("en", "Review incomplete"), ("fa", "بررسی ناقص")])
async def test_empty_partial_summary_never_claims_a_clean_review(language, expected):
    llm = SimpleNamespace(complete=AsyncMock())
    summary = await regenerate_summary(
        llm, [], [], "", "", "", output_language=language, partial=True
    )
    assert expected in summary
    assert "No issues found" not in summary and "مشکلی یافت نشد" not in summary
    llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_assessment_replaces_stale_clean_summary(storage):
    c = config()
    c.review.include_summary = True
    llm = FakeLLM()
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Add action type", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    result = ReviewResult(summary="No issues found.")
    await session.assess(engine, result, DIFF)
    assert "Review incomplete" in result.summary


def test_rejected_security_finding_records_reason_and_origin():
    from mira.config import FilterConfig

    finding = ReviewComment(
        "service.py",
        2,
        None,
        Severity.WARNING,
        "security",
        "Potential defect",
        "Private source details",
        0.5,
        source_pass="security",
    )
    audit = []
    with patch("mira.core.noise_filter.event") as events:
        assert filter_noise([finding], FilterConfig(confidence_threshold=0.7), audit=audit) == []
    details = events.call_args.kwargs
    assert details["source_pass"] == "security"
    assert details["reason"] == "confidence_below_threshold: 0.5 < 0.7"
    assert "Private source details" not in str(events.call_args_list)
    assert audit[0]["reason"] == details["reason"]


@pytest.mark.asyncio
async def test_engine_enforces_chunk_cap_and_reports_skipped_paths(storage):
    c = config()
    c.review.max_files_per_chunk = 2
    c.review.max_chunks_per_review = 1
    llm = FakeLLM()
    engine = ReviewEngine(c, llm, dry_run=True)
    diff = "\n".join(DIFF.replace("service.py", f"module{i}.py") for i in range(5))
    with patch("mira.core.engine.event") as events:
        result = await engine.review_diff(diff)
    assert len(result.reviewed_paths) == 2
    assert len(result.skipped_paths) == 3
    assert set(result.reviewed_paths + result.skipped_paths) == set(result.total_paths)
    plan = next(c for c in events.call_args_list if c.args[0] == "review.chunk_plan")
    assert plan.kwargs["files_per_chunk"] == [2]


@pytest.mark.asyncio
async def test_chunk_cap_keeps_high_priority_paths_instead_of_largest_diff(storage):
    c = config()
    c.review.max_files_per_chunk = 1
    c.review.max_chunks_per_review = 1
    engine = ReviewEngine(c, FakeLLM(), dry_run=True)
    docs = DIFF.replace("service.py", "docs/guide.md").replace(
        "return None", "return " + "x" * 1000
    )
    sensitive = DIFF.replace("service.py", "security/auth.py")
    result = await engine.review_diff(docs + "\n" + sensitive)
    assert result.reviewed_paths == ["security/auth.py"]
    assert result.skipped_paths == ["docs/guide.md"]


def test_chunk_context_excludes_other_changed_files_and_keeps_requirements_and_documents():
    session = TaskReviewSession("Add action type", TaskReviewOptions())
    session.pr_info = info()
    session.changed_paths = ["first.py", "second.py"]
    session.context_paths = ["first.py", "second.py", "peer.py"]
    session.sources = {
        "first.py": "FIRST",
        "second.py": "UNRELATED_CHANGED_SOURCE",
        "peer.py": "RELATED_PEER",
    }
    session.review_documents = "Organizational review documents"
    context = session.review_context(FakeLLM(), ["first.py"], max_tokens=1000)
    assert "FIRST" in context and "RELATED_PEER" in context
    assert "UNRELATED_CHANGED_SOURCE" not in context
    assert "Add action type" in context and session.review_documents in context


@pytest.mark.asyncio
async def test_missing_submission_repair_can_recover():
    client = model_client({"content": [{"type": "thinking", "thinking": "private"}]})
    incomplete = client.post.return_value
    submitted = httpx.Response(
        200,
        json={
            "content": [{"type": "tool_use", "name": "submit_review", "input": {"comments": []}}],
            "stop_reason": "tool_use",
        },
        request=httpx.Request("POST", COMPANY_MESSAGES_URL),
    )
    client.post.side_effect = [incomplete, submitted]
    with patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client):
        result = await model_provider().review([{"role": "user", "content": "Review"}])
    assert json.loads(result) == {"comments": []}
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_transport_retry_records_its_cause():
    client = model_client({"content": [{"type": "text", "text": "ok"}]})
    response = client.post.return_value
    client.post.side_effect = [httpx.ReadTimeout("upstream read timed out"), response]
    with (
        patch("mira.llm.company_messages.httpx.AsyncClient", return_value=client),
        patch("mira.observability.event") as events,
    ):
        assert await model_provider().complete([{"role": "user", "content": "hello"}]) == "ok"
    metadata = next(
        c.kwargs for c in events.call_args_list if c.args[0] == "request.retry_scheduled"
    )
    assert metadata["error_type"] == "ReadTimeout"
    assert metadata["error_message"] == "upstream read timed out"
    assert metadata["next_attempt"] == 2


@pytest.mark.asyncio
async def test_hop_limit_submits_with_the_collected_tool_evidence():
    llm = SimpleNamespace(
        complete_agentic=AsyncMock(
            return_value={
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {"name": "read_file", "arguments": '{"path":"caller.py"}'},
                    }
                ]
            }
        ),
        complete_with_tools=AsyncMock(return_value='{"comments":[]}'),
    )
    executor = SimpleNamespace(execute=AsyncMock(return_value="VERIFIED_CALLER_EVIDENCE"))
    original = [{"role": "system", "content": "Review"}, {"role": "user", "content": "Diff"}]
    assert await agentic_review_loop(llm, original, executor, max_hops=1) == '{"comments":[]}'
    final = llm.complete_with_tools.call_args.args[0]
    assert any(m.get("tool_call_id") == "read-1" for m in final)
    assert any(m.get("content") == "VERIFIED_CALLER_EVIDENCE" for m in final)
    assert "Investigation is finished" in final[-1]["content"]
    assert len(llm.complete_with_tools.call_args.kwargs["tools"]) == 1
    assert len(original) == 2


@pytest.mark.asyncio
async def test_unavailable_agentic_interface_keeps_plain_review_fallback():
    llm = SimpleNamespace(
        complete_agentic=AsyncMock(return_value=None),
        complete_with_tools=AsyncMock(),
    )
    result = await agentic_review_loop(
        llm,
        [{"role": "system", "content": "Review"}],
        SimpleNamespace(execute=AsyncMock()),
    )
    assert result == ""
    llm.complete_with_tools.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_time_cutoff_preserves_verification_window():
    llm = FakeLLM()
    budget = ReviewBudget(
        10_000, 30_000, 1000, deadline=time.monotonic() + 60, verification_seconds=120
    )
    guarded = BudgetedLLM(llm, budget)
    with pytest.raises(TaskBudgetExceeded, match="remaining time reserved"):
        await guarded.complete([{"role": "user", "content": "review"}])
    assert not llm.messages
    budget.verification_started = True
    await guarded.complete([{"role": "user", "content": "verify"}])
    assert len(llm.messages) == 1


@pytest.mark.parametrize("seconds, reserve", [(900, 300), (60, 20), (3600, 300)])
def test_review_configures_bounded_verification_time_reserve(seconds, reserve):
    engine = ReviewEngine(config(), FakeLLM(), provider(), dry_run=True)
    session = TaskReviewSession("Add action type", TaskReviewOptions(max_seconds=seconds))
    session.configure(engine)
    assert session.budget.verification_seconds == reserve
    assert session.budget.deadline - session.budget.phase_deadline() == reserve


@pytest.mark.asyncio
async def test_expired_job_does_not_start_another_model_request():
    llm = FakeLLM()
    budget = ReviewBudget(10_000, 30_000, 1000, deadline=time.monotonic() - 1)
    with pytest.raises(TaskBudgetExceeded, match="Execution time budget"):
        await BudgetedLLM(llm, budget).complete([{"role": "user", "content": "review"}])
    assert not llm.messages
    assert budget.reserved == 0


@pytest.mark.asyncio
async def test_standard_inflight_call_is_cancelled_before_verification_deadline():
    import asyncio

    llm = FakeLLM()
    cancelled = asyncio.Event()

    async def blocked(messages, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm.complete = blocked
    budget = ReviewBudget(
        10_000, 30_000, 1000, deadline=time.monotonic() + 1.05, verification_seconds=1
    )
    with pytest.raises(TaskBudgetExceeded, match="remaining time reserved"):
        await BudgetedLLM(llm, budget).complete([{"role": "user", "content": "Review"}])
    assert cancelled.is_set()
    assert budget.deadline > time.monotonic()
    assert not budget.active_tasks


@pytest.mark.asyncio
async def test_requirement_extraction_uses_schema_and_repairs_invalid_output(storage):
    llm = FakeLLM()
    llm.supports_tool_calling = True
    llm.complete_with_tools = AsyncMock(
        side_effect=[
            '{"requirements":[]}',
            json.dumps(
                {
                    "requirements": [{"description": "Add the requested action type"}],
                    "search_terms": ["ActionType"],
                }
            ),
        ]
    )
    engine = ReviewEngine(config(), llm, provider(), dry_run=True)
    session = TaskReviewSession("Add action type", TaskReviewOptions())
    session.configure(engine)
    await session.prepare(engine, info(), DIFF)
    assert llm.complete_with_tools.await_count == 2
    tool = llm.complete_with_tools.call_args.kwargs["tools"][0]["function"]
    assert tool["name"] == "submit_requirements"
    assert "requirements" in tool["parameters"]["required"]
    assert session.requirements[0].description == "Add the requested action type"
    assert not any("extraction failed" in limitation for limitation in session.limitations)


@pytest.mark.asyncio
async def test_requirement_extraction_failure_is_bounded_and_keeps_task(storage):
    llm = FakeLLM()
    llm.complete = AsyncMock(return_value="unusable response")
    engine = ReviewEngine(config(), llm, provider(), dry_run=True)
    session = TaskReviewSession("Add action type", TaskReviewOptions())
    session.configure(engine)
    with patch("mira.core.task_review.event") as events:
        await session.prepare(engine, info(), DIFF)
    assert llm.complete.await_count == 2
    assert session.requirements[0].description == "Add action type"
    assert any("extraction failed" in limitation for limitation in session.limitations)
    failure = next(
        c for c in events.call_args_list if c.args[0] == "task.requirement_extraction_failed"
    )
    assert failure.kwargs["error_type"]
    assert "unusable response" not in str(events.call_args_list)


@pytest.mark.asyncio
async def test_failed_general_review_retains_security_findings(storage):
    c = config()
    c.review.security_pass = True
    llm = FakeLLM()
    llm.review = AsyncMock(side_effect=RuntimeError("Configured LLM call failed"))
    engine = ReviewEngine(c, llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    finding = ReviewComment(
        "service.py",
        2,
        None,
        Severity.WARNING,
        "security",
        "Verified security finding",
        "Evidence from the security pass.",
        0.99,
    )
    with (
        patch("mira.core.engine.security_review_pass", AsyncMock(return_value=[finding])),
        patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])),
    ):
        result = await engine.review_pr(URL)
    assert [c.title for c in result.comments] == [finding.title]
    assert any("completed passes were retained" in item for item in session.limitations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("LLM failed"), ResponseParseError("Bad policy JSON")]
)
async def test_failed_policy_pass_retains_general_review_findings(storage, failure):
    llm = FakeLLM()
    llm.review = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "comments": [
                        {
                            "path": "service.py",
                            "line": 2,
                            "severity": "warning",
                            "category": "bug",
                            "title": "Confirmed defect",
                            "body": "Returns None.",
                            "confidence": 0.99,
                        }
                    ],
                    "summary": "Found a defect",
                }
            ),
            failure,
        ]
    )
    engine = ReviewEngine(config(), llm, provider(), dry_run=True)
    session = TaskReviewSession("Create assignments", TaskReviewOptions())
    session.configure(engine)
    with (
        patch(
            "mira.core.engine.load_company_rules",
            return_value=[{"content": "Check company policy"}],
        ),
        patch.object(engine, "_detect_overlaps_safe", AsyncMock(return_value=[])),
    ):
        result = await engine.review_pr(URL)
    assert [comment.title for comment in result.comments] == ["Confirmed defect"]
    assert session.limitations
