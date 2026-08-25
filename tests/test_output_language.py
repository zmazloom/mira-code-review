"""Persian review output without changing internal review schemas."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.cli import _format_json, _format_manual_text
from mira.config import MiraConfig, ReviewConfig, load_config
from mira.core.company_rules import load_company_rules
from mira.core.passes import agentic_review_loop, regenerate_summary, self_critique
from mira.llm.prompts.review import (
    build_dependency_review_prompt,
    build_review_prompt,
    build_security_review_prompt,
    build_walkthrough_prompt,
)
from mira.localization import language_instruction
from mira.manual_review import ManualReviewCommand, ManualReviewOutcome, PullRequestReference
from mira.models import (
    FileChangeType,
    FileDiff,
    HunkInfo,
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
)
from mira.platforms.handlers import run_pr_review
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider

_FIXTURES = Path(__file__).parent / "fixtures"


def _file() -> FileDiff:
    return FileDiff(
        path="src/FoodController.java",
        change_type=FileChangeType.MODIFIED,
        hunks=[
            HunkInfo(
                source_start=10,
                source_length=1,
                target_start=10,
                target_length=1,
                content="@@ -10 +10 @@\n-return food;\n+return createFood();",
            )
        ],
        language="java",
        added_lines=1,
        deleted_lines=1,
    )


def _comment() -> ReviewComment:
    return ReviewComment(
        path="src/FoodController.java",
        line=10,
        end_line=None,
        severity=Severity.WARNING,
        category="bug",
        title="خروجی createFood ممکن است null باشد",
        body=(
            "متد createFood در FoodController می‌تواند NullPointerException ایجاد کند؛ "
            "رفتار @PostMapping را بررسی کنید."
        ),
        confidence=0.93,
        suggestion="return createFood();",
        agent_prompt="رفتار createFood را در src/FoodController.java اصلاح کنید.",
    )


def _pr_info() -> PRInfo:
    return PRInfo(
        title="Update FoodController",
        description="",
        base_branch="main",
        head_branch="food-fix",
        url="https://example.test/o/r/pull/7",
        number=7,
        owner="o",
        repo="r",
        head_sha="abc",
        author="alice",
    )


def _result() -> ReviewResult:
    return ReviewResult(
        comments=[_comment()],
        summary="این تغییر مسیر ساخت غذا را اصلاح می‌کند.",
        reviewed_files=1,
        total_paths=["src/FoodController.java"],
        output_language="fa",
    )


def test_default_english_output_and_prompt_are_unchanged() -> None:
    config = MiraConfig()
    prompt = build_review_prompt([_file()], config)[0]["content"]
    result = ReviewResult(
        comments=[_comment()],
        summary="English summary.",
        output_language="en",
    )

    assert config.review.output_language == "en"
    assert config.review.prompt_output_language is None
    assert "## Output Language" not in prompt
    assert result.comments[0].severity.name.lower() == "warning"


def test_explicit_english_and_arbitrary_language_codes_receive_instructions() -> None:
    english = MiraConfig(review=ReviewConfig(output_language="en"))
    english_prompt = build_review_prompt([_file()], english)[0]["content"]
    other = ReviewConfig(output_language="pt-BR")

    assert english.review.prompt_output_language == "en"
    assert "Write all human-readable review content in English." in english_prompt
    assert other.output_language == "pt-br"
    assert "BCP 47 code `pt-br`" in language_instruction(other.prompt_output_language)


def test_persian_config_parsing() -> None:
    assert load_config(_FIXTURES / "persian_config.yml").review.output_language == "fa"


def test_persian_company_rules_are_preserved_exactly() -> None:
    rules_text = (
        "# قواعد شرکت\n\n"
        "COMPANY_RULE_RETURN_NULL_TEST: متد createFood نباید null برگرداند.\n"
    )
    config = load_config(_FIXTURES / "persian_config.yml")

    rules = load_company_rules(config)
    prompt = build_review_prompt([_file()], config, company_rules=rules)[0]["content"]

    assert rules[0]["content"] == rules_text
    assert rules_text in prompt
    assert "COMPANY_RULE_RETURN_NULL_TEST" in prompt
    assert "createFood" in prompt


def test_persian_terminal_labels_severity_and_identifiers() -> None:
    outcome = ManualReviewOutcome(
        reference=PullRequestReference(
            provider="github",
            owner="o",
            repo="r",
            number=7,
            url="https://example.test/o/r/pull/7",
        ),
        pr_info=_pr_info(),
        result=_result(),
        posted=False,
        inline_posted=False,
    )

    text = _format_manual_text(outcome)

    assert "خلاصه\n-------" in text
    assert "یافته‌ها:" in text
    assert "- شدت: هشدار" in text
    assert "فایل: src/FoodController.java:10" in text
    assert "عنوان: خروجی createFood ممکن است null باشد" in text
    assert "دلیل:" in text
    assert "پیشنهاد: return createFood();" in text
    assert "اطمینان: 0.93" in text
    for identifier in (
        "FoodController",
        "createFood",
        "@PostMapping",
        "NullPointerException",
    ):
        assert identifier in text
    assert _result().comments[0].severity is Severity.WARNING
    assert json.loads(_format_json(_result()))["comments"][0]["severity"] == "warning"


def test_persian_main_security_and_walkthrough_prompts_receive_instruction() -> None:
    config = MiraConfig(review=ReviewConfig(output_language="fa"))
    prompts = [
        build_review_prompt([_file()], config)[0]["content"],
        build_security_review_prompt([_file()], output_language="fa")[0]["content"],
        build_dependency_review_prompt([_file()], output_language="fa")[0]["content"],
        build_walkthrough_prompt([_file()], config)[0]["content"],
    ]

    for prompt in prompts:
        assert "Write all human-readable review content in Persian (Farsi)." in prompt
        assert "Preserve the existing output schema and structure." in prompt
        assert "class names, method names, variable names" in prompt
        assert "rule identifiers unchanged" in prompt


@pytest.mark.asyncio
async def test_manual_review_uses_configured_output_language() -> None:
    config = MiraConfig(review=ReviewConfig(output_language="fa"))
    engine = MagicMock()
    engine._pr_info = _pr_info()
    engine.review_pr = AsyncMock(return_value=_result())

    with (
        patch("mira.manual_review.create_llm", side_effect=[MagicMock()] * 3),
        patch("mira.manual_review.ReviewEngine", return_value=engine) as engine_cls,
    ):
        await ManualReviewCommand(config=config, provider=MagicMock()).execute(
            PullRequestReference(
                provider="github",
                owner="o",
                repo="r",
                number=7,
                url=_pr_info().url,
            )
        )

    assert engine_cls.call_args.kwargs["config"].review.output_language == "fa"


@pytest.mark.asyncio
async def test_webhook_review_uses_configured_output_language() -> None:
    config = MiraConfig(review=ReviewConfig(output_language="fa"))
    engine = MagicMock()
    engine.review_pr = AsyncMock(return_value=_result())
    tracker = MagicMock()
    tracker.try_start.return_value = True
    database = MagicMock()
    database.get_repo.return_value = None

    with (
        patch("mira.platforms.handlers.load_config", return_value=config),
        patch("mira.platforms.handlers.create_llm", side_effect=[MagicMock()] * 3),
        patch("mira.platforms.handlers.ReviewEngine", return_value=engine) as engine_cls,
        patch("mira.platforms.handlers.review_tracker", tracker),
        patch("mira.dashboard.api._app_db", database),
        patch("mira.outbound_webhooks.dispatch_event", new=AsyncMock()),
    ):
        await run_pr_review(
            MagicMock(),
            "o",
            "r",
            7,
            _pr_info().url,
            False,
            "miracodeai",
        )

    assert engine_cls.call_args.kwargs["config"].review.output_language == "fa"


@pytest.mark.asyncio
async def test_self_critique_receives_language_instruction() -> None:
    critic = AsyncMock()
    critic.complete_with_tools.return_value = json.dumps(
        {"verdicts": [{"index": 0, "evidence": "proven", "reason": "درست است"}]}
    )

    kept = await self_critique(
        AsyncMock(),
        [_comment()],
        indexing_llm=critic,
        output_language="fa",
    )

    prompt = critic.complete_with_tools.await_args.kwargs["messages"][0]["content"]
    assert kept == [_comment()]
    assert "Write all human-readable review content in Persian (Farsi)." in prompt


@pytest.mark.asyncio
async def test_agentic_review_receives_language_instruction() -> None:
    llm = AsyncMock()
    llm.complete_agentic.return_value = {
        "content": "",
        "tool_calls": [
            {
                "id": "1",
                "function": {"name": "submit_review", "arguments": '{"comments": []}'},
            }
        ],
    }

    raw = await agentic_review_loop(
        llm,
        [{"role": "system", "content": "Review this diff."}],
        AsyncMock(),
        output_language="fa",
    )

    sent = llm.complete_agentic.await_args.args[0][0]["content"]
    assert raw == '{"comments": []}'
    assert "Write all human-readable review content in Persian (Farsi)." in sent


@pytest.mark.asyncio
async def test_summary_is_generated_directly_in_persian() -> None:
    summary_llm = AsyncMock()
    summary_llm.complete.return_value = "این تغییر یک هشدار در src/FoodController.java دارد."

    summary = await regenerate_summary(
        AsyncMock(),
        [_comment()],
        [],
        "Update FoodController",
        "",
        fallback="",
        indexing_llm=summary_llm,
        output_language="fa",
    )

    prompt = summary_llm.complete.await_args.kwargs["messages"][0]["content"]
    assert summary.startswith("این تغییر")
    assert "src/FoodController.java" in summary
    assert "Write all human-readable review content in Persian (Farsi)." in prompt


@pytest.mark.asyncio
async def test_github_comment_localization() -> None:
    provider = GitHubProvider.__new__(GitHubProvider)
    provider._token = "token"
    review = MagicMock()
    review.get_comments.return_value = []
    pr = MagicMock()
    pr.get_commits.return_value = [MagicMock()]
    pr.create_review.return_value = review
    repo = MagicMock()
    repo.get_pull.return_value = pr
    provider._github = MagicMock()
    provider._github.get_repo.return_value = repo

    await provider.post_review(_pr_info(), _result())

    payload = pr.create_review.call_args.kwargs
    body = payload["comments"][0]["body"]
    assert "**اشکال**" in body
    assert "⚠️ هشدار" in body
    assert "**خلاصه مرور Mira**" in payload["body"]
    assert "createFood" in body


@pytest.mark.asyncio
async def test_gitlab_comment_localization() -> None:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider._api = "https://gitlab.example.test/api/v4"
    provider._changes = AsyncMock(
        return_value={"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
    )
    provider._request = AsyncMock()

    await provider.post_review(_pr_info(), _result())

    bodies = [
        call.kwargs.get("data", {}).get("body", "")
        for call in provider._request.await_args_list
    ]
    assert any("**اشکال**" in body and "⚠️ هشدار" in body for body in bodies)
    assert any("**خلاصه مرور Mira**" in body for body in bodies)
    assert any("FoodController" in body and "createFood" in body for body in bodies)
