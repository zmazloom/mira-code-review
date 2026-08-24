"""Tests for provider-neutral manual PR/MR review orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from mira.cli import main
from mira.config import MiraConfig
from mira.exceptions import LLMError, ProviderError
from mira.manual_review import (
    ManualReviewCommand,
    ManualReviewError,
    ManualReviewOutcome,
    PullRequestReference,
    create_manual_provider,
    parse_pull_request_reference,
)
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity

GITHUB_URL = "https://github.com/octocat/hello/pull/42"
GITLAB_URL = "https://gitlab.example.com/group/sub/project/-/merge_requests/7"


def _reference() -> PullRequestReference:
    return parse_pull_request_reference(GITHUB_URL)


def _pr_info() -> PRInfo:
    return PRInfo(
        title="Improve parser",
        description="",
        base_branch="main",
        head_branch="parser-fix",
        url=GITHUB_URL,
        number=42,
        owner="octocat",
        repo="hello",
        author="alice",
    )


def _outcome(*, posted: bool = False, inline_posted: bool = False) -> ManualReviewOutcome:
    return ManualReviewOutcome(
        reference=_reference(),
        pr_info=_pr_info(),
        result=ReviewResult(summary="Looks good.", reviewed_files=2, total_paths=["a.py", "b.py"]),
        posted=posted,
        inline_posted=inline_posted,
    )


class TestPullRequestReferenceParser:
    def test_github_pr_url(self) -> None:
        ref = parse_pull_request_reference(GITHUB_URL)
        assert (ref.provider, ref.owner, ref.repo, ref.number) == (
            "github",
            "octocat",
            "hello",
            42,
        )

    def test_self_hosted_gitlab_nested_group(self) -> None:
        ref = parse_pull_request_reference(GITLAB_URL)
        assert (ref.provider, ref.owner, ref.repo, ref.number) == (
            "gitlab",
            "group/sub",
            "project",
            7,
        )

    def test_existing_github_shorthand_remains_supported(self) -> None:
        ref = parse_pull_request_reference("octocat/hello#42")
        assert (ref.provider, ref.owner, ref.repo, ref.number) == (
            "github",
            "octocat",
            "hello",
            42,
        )

    def test_existing_forgejo_url_remains_supported(self) -> None:
        ref = parse_pull_request_reference("https://codeberg.org/o/r/pulls/9")
        assert (ref.provider, ref.owner, ref.repo, ref.number) == ("forgejo", "o", "r", 9)

    @pytest.mark.parametrize("value", ["", "not-a-url", "github.com/o/r/pull/1"])
    def test_invalid_url(self, value: str) -> None:
        with pytest.raises(ManualReviewError, match="Invalid PR/MR URL"):
            parse_pull_request_reference(value)

    def test_unsupported_provider(self) -> None:
        with pytest.raises(ManualReviewError, match="Unsupported Git provider"):
            parse_pull_request_reference("https://bitbucket.org/o/r/pull-requests/1")

    @pytest.mark.parametrize(
        "value",
        [
            "https://github.com/o/r/pull/not-a-number",
            "https://gitlab.com/o/r/-/merge_requests/nope",
        ],
    )
    def test_malformed_number(self, value: str) -> None:
        with pytest.raises(ManualReviewError, match="Malformed"):
            parse_pull_request_reference(value)


class TestManualAuthentication:
    def test_missing_github_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("GITHUB_TOKEN", "MIRA_GIT_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ManualReviewError, match="GITHUB_TOKEN"):
            create_manual_provider(_reference())

    def test_missing_gitlab_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("GITLAB_TOKEN", "MIRA_GITLAB_TOKEN", "MIRA_GIT_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ManualReviewError, match="GITLAB_TOKEN"):
            create_manual_provider(parse_pull_request_reference(GITLAB_URL))


@pytest.mark.asyncio
async def test_manual_command_reuses_review_engine_default_is_read_only() -> None:
    engine = MagicMock()
    engine._pr_info = _pr_info()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="done"))

    with (
        patch("mira.manual_review.create_llm", side_effect=[MagicMock(), MagicMock(), MagicMock()]),
        patch("mira.manual_review.ReviewEngine", return_value=engine) as engine_cls,
    ):
        outcome = await ManualReviewCommand(
            config=MiraConfig(), provider=MagicMock(), post=False
        ).execute(_reference())

    engine.review_pr.assert_awaited_once_with(GITHUB_URL)
    assert engine_cls.call_args.kwargs["dry_run"] is True
    assert outcome.posted is False


@pytest.mark.asyncio
async def test_manual_command_post_enables_publishing() -> None:
    engine = MagicMock()
    engine._pr_info = _pr_info()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="done"))

    with (
        patch("mira.manual_review.create_llm", side_effect=[MagicMock(), MagicMock(), MagicMock()]),
        patch("mira.manual_review.ReviewEngine", return_value=engine) as engine_cls,
    ):
        outcome = await ManualReviewCommand(
            config=MiraConfig(), provider=MagicMock(), post=True
        ).execute(_reference())

    assert engine_cls.call_args.kwargs["dry_run"] is False
    assert engine_cls.call_args.kwargs["post_inline_comments"] is True
    assert engine_cls.call_args.kwargs["resolve_threads"] is True
    assert outcome.posted is True and outcome.inline_posted is True


@pytest.mark.asyncio
async def test_manual_command_summary_only_suppresses_inline_and_thread_writes() -> None:
    engine = MagicMock()
    engine._pr_info = _pr_info()
    engine.review_pr = AsyncMock(return_value=ReviewResult(summary="done"))

    with (
        patch("mira.manual_review.create_llm", side_effect=[MagicMock(), MagicMock(), MagicMock()]),
        patch("mira.manual_review.ReviewEngine", return_value=engine) as engine_cls,
    ):
        outcome = await ManualReviewCommand(
            config=MiraConfig(),
            provider=MagicMock(),
            post=True,
            post_inline_comments=False,
        ).execute(_reference())

    assert engine_cls.call_args.kwargs["dry_run"] is False
    assert engine_cls.call_args.kwargs["post_inline_comments"] is False
    assert engine_cls.call_args.kwargs["resolve_threads"] is False
    assert outcome.posted is True and outcome.inline_posted is False


def _invoke_with_fake_command(args: list[str], outcome: ManualReviewOutcome):
    command = MagicMock()
    command.execute = AsyncMock(return_value=outcome)
    with (
        patch("mira.cli.load_config", return_value=MiraConfig()),
        patch("mira.cli.create_manual_provider", return_value=MagicMock()),
        patch("mira.cli.ManualReviewCommand", return_value=command) as command_cls,
    ):
        result = CliRunner().invoke(main, ["review", GITHUB_URL, *args])
    return result, command_cls


class TestManualReviewCLI:
    def test_default_mode_performs_no_remote_writes(self) -> None:
        result, command_cls = _invoke_with_fake_command([], _outcome())
        assert result.exit_code == 0
        assert command_cls.call_args.kwargs["post"] is False
        assert "Completed (not posted)" in result.output

    def test_dry_run_performs_no_remote_writes(self) -> None:
        result, command_cls = _invoke_with_fake_command(["--dry-run"], _outcome())
        assert result.exit_code == 0
        assert command_cls.call_args.kwargs["post"] is False

    def test_post_invokes_publishing(self) -> None:
        result, command_cls = _invoke_with_fake_command(
            ["--post"], _outcome(posted=True, inline_posted=True)
        )
        assert result.exit_code == 0
        assert command_cls.call_args.kwargs["post"] is True
        assert command_cls.call_args.kwargs["post_inline_comments"] is True

    @pytest.mark.parametrize("flag", ["--summary-only", "--no-inline"])
    def test_summary_modes_do_not_post_inline_findings(self, flag: str) -> None:
        result, command_cls = _invoke_with_fake_command(
            ["--post", flag], _outcome(posted=True, inline_posted=False)
        )
        assert result.exit_code == 0
        assert command_cls.call_args.kwargs["post_inline_comments"] is False

    def test_provider_api_failure_surfaces_cleanly(self) -> None:
        command = MagicMock()
        command.execute = AsyncMock(side_effect=ProviderError("GitHub API failed"))
        with (
            patch("mira.cli.load_config", return_value=MiraConfig()),
            patch("mira.cli.create_manual_provider", return_value=MagicMock()),
            patch("mira.cli.ManualReviewCommand", return_value=command),
        ):
            result = CliRunner().invoke(main, ["review", GITHUB_URL, "--token", "tok"])
        assert result.exit_code != 0
        assert "GitHub API failed" in result.output

    def test_llm_pipeline_failure_is_nonzero_and_safe(self) -> None:
        command = MagicMock()
        command.execute = AsyncMock(
            side_effect=LLMError("completion_failed", model="secret-model", error="private")
        )
        with (
            patch("mira.cli.load_config", return_value=MiraConfig()),
            patch("mira.cli.create_manual_provider", return_value=MagicMock()),
            patch("mira.cli.ManualReviewCommand", return_value=command),
        ):
            result = CliRunner().invoke(main, ["review", GITHUB_URL, "--token", "tok"])
        assert result.exit_code != 0
        assert "LLM completion failed" in result.output
        assert "secret-model" not in result.output
        assert "private" not in result.output

    def test_auth_token_is_redacted_from_errors(self) -> None:
        secret = "ghp_super_secret_value"
        with (
            patch("mira.cli.load_config", return_value=MiraConfig()),
            patch(
                "mira.cli.create_manual_provider",
                side_effect=ProviderError(f"request failed for {secret}"),
            ),
        ):
            result = CliRunner().invoke(main, ["review", GITHUB_URL, "--github-token", secret])
        assert result.exit_code != 0
        assert secret not in result.output
        assert "***" in result.output

    def test_terminal_output_includes_metadata_and_findings(self) -> None:
        comment = ReviewComment(
            path="src/parser.py",
            line=10,
            end_line=12,
            severity=Severity.WARNING,
            category="bug",
            title="Unchecked input",
            body="Validate the input before parsing.",
            confidence=0.91,
            suggestion="guard(value)",
        )
        outcome = _outcome()
        outcome.result.comments.append(comment)
        result, _ = _invoke_with_fake_command([], outcome)
        assert result.exit_code == 0
        assert "Repository: octocat/hello" in result.output
        assert "PR/MR number: 42" in result.output
        assert "src/parser.py:10-12" in result.output
        assert "Confidence: 0.91" in result.output
