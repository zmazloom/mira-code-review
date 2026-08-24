"""Provider-neutral orchestration for terminal-triggered PR/MR reviews."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.dashboard.models_config import llm_config_for
from mira.exceptions import MiraError
from mira.llm import create_llm
from mira.models import PRInfo, ReviewResult
from mira.providers import create_provider
from mira.providers.base import BaseProvider


class ManualReviewError(MiraError):
    """A user-facing error preparing a manual review."""


@dataclass(frozen=True)
class PullRequestReference:
    """A parsed, provider-independent pull/merge request reference."""

    provider: str
    owner: str
    repo: str
    number: int
    url: str


@dataclass(frozen=True)
class ManualReviewOutcome:
    """Metadata and pipeline output returned to the terminal layer."""

    reference: PullRequestReference
    pr_info: PRInfo
    result: ReviewResult
    posted: bool
    inline_posted: bool


def parse_pull_request_reference(value: str) -> PullRequestReference:
    """Parse a canonical GitHub/Forgejo PR or GitLab MR reference.

    GitLab hostnames are intentionally not hard-coded: the ``/-/merge_requests/``
    route identifies GitLab, while its API endpoint remains controlled by the
    existing ``MIRA_GITLAB_API_URL`` platform configuration.
    """
    raw = value.strip()

    # Preserve the shorthand accepted by the existing provider parsers and
    # the pre-positional ``mira review --pr`` command.
    github_short = re.fullmatch(r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s#!]+)#(?P<number>\d+)", raw)
    if github_short:
        return PullRequestReference(
            provider="github",
            owner=github_short.group("owner"),
            repo=github_short.group("repo"),
            number=int(github_short.group("number")),
            url=raw,
        )
    gitlab_short = re.fullmatch(r"(?P<owner>.+)/(?P<repo>[^/\s!]+)!(?P<number>\d+)", raw)
    if gitlab_short:
        return PullRequestReference(
            provider="gitlab",
            owner=gitlab_short.group("owner"),
            repo=gitlab_short.group("repo"),
            number=int(gitlab_short.group("number")),
            url=raw,
        )

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ManualReviewError(f"Invalid PR/MR URL: {value}") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ManualReviewError(
            "Invalid PR/MR URL. Expected a full https:// GitHub PR or GitLab MR URL."
        )

    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path).strip("/")
    parts = path.split("/") if path else []

    if host == "github.com":
        if len(parts) >= 3 and parts[2] == "pull":
            if len(parts) != 4 or not re.fullmatch(r"\d+", parts[3]):
                raise ManualReviewError("Malformed GitHub pull request number in URL.")
            return PullRequestReference(
                provider="github",
                owner=parts[0],
                repo=parts[1],
                number=int(parts[3]),
                url=raw,
            )
        raise ManualReviewError(
            "Invalid GitHub pull request URL. Expected https://github.com/owner/repo/pull/123."
        )

    marker = ["-", "merge_requests"]
    marker_index = next(
        (i for i in range(len(parts) - 1) if parts[i : i + 2] == marker),
        None,
    )
    if marker_index is not None:
        if marker_index < 2:
            raise ManualReviewError("Invalid GitLab merge request URL: missing group or project.")
        if len(parts) != marker_index + 3 or not re.fullmatch(r"\d+", parts[marker_index + 2]):
            raise ManualReviewError("Malformed GitLab merge request number in URL.")
        project_parts = parts[:marker_index]
        return PullRequestReference(
            provider="gitlab",
            owner="/".join(project_parts[:-1]),
            repo=project_parts[-1],
            number=int(parts[marker_index + 2]),
            url=raw,
        )

    if len(parts) >= 3 and parts[2] == "pulls":
        if len(parts) != 4 or not re.fullmatch(r"\d+", parts[3]):
            raise ManualReviewError("Malformed Forgejo pull request number in URL.")
        return PullRequestReference(
            provider="forgejo",
            owner=parts[0],
            repo=parts[1],
            number=int(parts[3]),
            url=raw,
        )

    raise ManualReviewError(
        "Unsupported Git provider. Manual review supports GitHub and Forgejo pull requests "
        "and GitLab merge requests."
    )


def create_manual_provider(
    reference: PullRequestReference,
    *,
    token: str | None = None,
    github_token: str | None = None,
    gitlab_token: str | None = None,
) -> BaseProvider:
    """Authenticate the provider using CLI values and established env aliases."""
    if reference.provider == "github":
        resolved = (
            token
            or github_token
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("MIRA_GIT_TOKEN")
        )
        if not resolved:
            raise ManualReviewError(
                "GITHUB_TOKEN is required for a manual GitHub pull request review."
            )
    elif reference.provider == "gitlab":
        resolved = (
            token
            or gitlab_token
            or os.environ.get("GITLAB_TOKEN")
            or os.environ.get("MIRA_GITLAB_TOKEN")
            or os.environ.get("MIRA_GIT_TOKEN")
        )
        if not resolved:
            raise ManualReviewError(
                "GITLAB_TOKEN is required for a manual GitLab merge request review."
            )
    elif reference.provider == "forgejo":
        resolved = token or os.environ.get("MIRA_FORGEJO_TOKEN") or os.environ.get("MIRA_GIT_TOKEN")
        if not resolved:
            raise ManualReviewError(
                "A token is required for a manual Forgejo pull request review; "
                "use --token or MIRA_FORGEJO_TOKEN."
            )
    else:  # Defensive: parser currently returns only the two providers above.
        raise ManualReviewError(f"Unsupported Git provider: {reference.provider}")

    return create_provider(reference.provider, resolved)


class ManualReviewCommand:
    """Run a terminal review through Mira's existing ``ReviewEngine`` pipeline."""

    def __init__(
        self,
        *,
        config: MiraConfig,
        provider: BaseProvider,
        post: bool = False,
        post_inline_comments: bool = True,
        bot_name: str = "miracodeai",
    ) -> None:
        self.config = config
        self.provider = provider
        self.post = post
        self.post_inline_comments = post_inline_comments
        self.bot_name = bot_name

    async def execute(self, reference: PullRequestReference) -> ManualReviewOutcome:
        """Construct configured LLMs and delegate the full review to ``ReviewEngine``."""
        llm = create_llm(llm_config_for("review", self.config.llm))
        indexing_llm = create_llm(llm_config_for("indexing", self.config.llm))
        security_llm = create_llm(llm_config_for("security", self.config.llm))
        engine = ReviewEngine(
            config=self.config,
            llm=llm,
            provider=self.provider,
            bot_name=self.bot_name,
            dry_run=not self.post,
            post_inline_comments=self.post_inline_comments,
            resolve_threads=self.post_inline_comments,
            indexing_llm=indexing_llm,
            security_llm=security_llm,
        )
        result = await engine.review_pr(reference.url)
        pr_info = getattr(engine, "_pr_info", None)
        if not isinstance(pr_info, PRInfo):
            raise ManualReviewError("Review completed without pull request metadata.")
        return ManualReviewOutcome(
            reference=reference,
            pr_info=pr_info,
            result=result,
            posted=self.post,
            inline_posted=self.post and self.post_inline_comments,
        )
