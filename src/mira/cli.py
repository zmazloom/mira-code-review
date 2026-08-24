"""Click CLI for Mira."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC

import click

from mira import __version__
from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.exceptions import MiraError
from mira.llm import create_llm
from mira.manual_review import (
    ManualReviewCommand,
    ManualReviewOutcome,
    create_manual_provider,
    parse_pull_request_reference,
)
from mira.models import ReviewResult, Severity


def _format_text(result: ReviewResult) -> str:
    """Format review result as human-readable text."""
    lines: list[str] = []

    if result.thread_decisions:
        from mira.llm.prompts.verify_fixes import _extract_issue_description

        lines.append("Thread resolution:")
        for d in result.thread_decisions:
            status = "RESOLVE" if d.fixed else "KEEP"
            desc = _extract_issue_description(d.body)
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"  [{status}] {d.path}:{d.line} — {desc}")
        fixed = sum(1 for d in result.thread_decisions if d.fixed)
        lines.append(f"  {fixed}/{len(result.thread_decisions)} thread(s) would be resolved.")
        lines.append("")

    if result.walkthrough:
        lines.append(result.walkthrough.to_markdown())
        lines.append("")
        lines.append("---")
        lines.append("")

    if result.summary:
        lines.append(result.summary)
        lines.append("")

    if not result.comments:
        lines.append("No issues found.")
        return "\n".join(lines)

    for i, c in enumerate(result.comments, 1):
        lines.append(f"{i}. [{c.severity.name}] {c.path}:{c.line} — {c.title}")
        lines.append(f"   {c.body}")
        if c.suggestion:
            lines.append(f"   Suggestion: {c.suggestion}")
        lines.append("")

    lines.append(f"Reviewed {result.reviewed_files} files, {len(result.comments)} comments.")
    if result.token_usage:
        lines.append(f"Tokens used: {result.token_usage.get('total_tokens', 0)}")

    return "\n".join(lines)


def _format_json(result: ReviewResult) -> str:
    """Format review result as JSON."""
    walkthrough_data = None
    if result.walkthrough:
        # Group file changes by their group label for JSON output
        groups: dict[str, list[dict[str, str]]] = {}
        for fc in result.walkthrough.file_changes:
            label = fc.group or "Other"
            groups.setdefault(label, []).append(
                {
                    "path": fc.path,
                    "change_type": fc.change_type.value,
                    "description": fc.description,
                }
            )
        effort_data = None
        if result.walkthrough.effort:
            effort_data = {
                "level": result.walkthrough.effort.level,
                "label": result.walkthrough.effort.label,
                "minutes": result.walkthrough.effort.minutes,
            }
        walkthrough_data = {
            "summary": result.walkthrough.summary,
            "change_groups": [{"label": label, "files": files} for label, files in groups.items()],
            "effort": effort_data,
            "sequence_diagram": result.walkthrough.sequence_diagram,
        }

    data = {
        "summary": result.summary,
        "walkthrough": walkthrough_data,
        "comments": [
            {
                "path": c.path,
                "line": c.line,
                "end_line": c.end_line,
                "severity": c.severity.name.lower(),
                "category": c.category,
                "title": c.title,
                "body": c.body,
                "confidence": c.confidence,
                "suggestion": c.suggestion,
            }
            for c in result.comments
        ],
        "reviewed_files": result.reviewed_files,
        "token_usage": result.token_usage,
    }
    return json.dumps(data, indent=2)


def _format_manual_text(outcome: ManualReviewOutcome) -> str:
    """Format a manual PR/MR review with provider metadata for the terminal."""
    info = outcome.pr_info
    result = outcome.result
    total_files = len(result.total_paths) if result.total_paths else result.reviewed_files
    if result.skipped_reason:
        status = f"Completed with note: {result.skipped_reason}"
    elif outcome.posted:
        status = "Completed and posted"
        if not outcome.inline_posted:
            status += " (summary only; inline findings suppressed)"
    else:
        status = "Completed (not posted)"

    lines = [
        f"Repository: {info.owner}/{info.repo}",
        f"PR/MR number: {info.number}",
        f"Title: {info.title}",
        f"Author: {info.author or '(unknown)'}",
        f"Base branch: {info.base_branch}",
        f"Head branch: {info.head_branch}",
        f"Files changed: {total_files}",
        f"Review status: {status}",
        "",
        "Summary",
        "-------",
        result.summary or "No summary was generated.",
        "",
        "Findings:",
    ]

    if not result.comments:
        lines.append("No findings. The review completed successfully.")
    else:
        for comment in result.comments:
            location = f"{comment.path}:{comment.line}"
            if comment.end_line and comment.end_line != comment.line:
                location += f"-{comment.end_line}"
            lines.extend(
                [
                    f"- Severity: {comment.severity.name.lower()}",
                    f"  File: {location}",
                    f"  Title: {comment.title}",
                    f"  Rationale: {comment.body}",
                ]
            )
            if comment.suggestion:
                lines.append(f"  Suggestion: {comment.suggestion}")
            lines.append(f"  Confidence: {comment.confidence:.2f}")

    return "\n".join(lines)


def _format_manual_json(outcome: ManualReviewOutcome) -> str:
    """Add PR/MR metadata and posting state to the established JSON result."""
    review_data = json.loads(_format_json(outcome.result))
    info = outcome.pr_info
    data = {
        "repository": f"{info.owner}/{info.repo}",
        "provider": outcome.reference.provider,
        "number": info.number,
        "title": info.title,
        "author": info.author,
        "base_branch": info.base_branch,
        "head_branch": info.head_branch,
        "files_changed": len(outcome.result.total_paths)
        if outcome.result.total_paths
        else outcome.result.reviewed_files,
        "posted": outcome.posted,
        "inline_posted": outcome.inline_posted,
        **review_data,
    }
    return json.dumps(data, indent=2)


def _redact_cli_secrets(message: str, *values: str | None) -> str:
    """Keep provider/LLM credentials out of terminal error output."""
    secrets = {
        value
        for value in (
            *values,
            os.environ.get("MIRA_GIT_TOKEN"),
            os.environ.get("MIRA_GITLAB_TOKEN"),
            os.environ.get("MIRA_FORGEJO_TOKEN"),
        )
        if value
    }
    for secret in secrets:
        message = message.replace(secret, "***")
    return message


@click.group()
@click.version_option(version=__version__, prog_name="mira")
def main() -> None:
    """Mira — AI-powered PR reviewer."""


@main.command()
@click.argument("url", required=False)
@click.option("--pr", "pr_url", default=None, help="PR/MR URL (GitHub PR or GitLab MR)")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read diff from stdin")
@click.option("--model", envvar="MIRA_MODEL", default=None, help="LLM model to use")
@click.option("--max-comments", envvar="MIRA_MAX_COMMENTS", type=int, default=None)
@click.option("--confidence", envvar="MIRA_CONFIDENCE_THRESHOLD", type=float, default=None)
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="Git platform API token")
@click.option(
    "--github-token",
    envvar="GITHUB_TOKEN",
    default=None,
    help="GitHub API token (alias for --token)",
)
@click.option(
    "--gitlab-token",
    envvar="GITLAB_TOKEN",
    default=None,
    help="GitLab API token (alias for --token)",
)
@click.option("--post", is_flag=True, help="Publish the summary and inline findings")
@click.option("--dry-run", is_flag=True, help="Explicitly guarantee no remote writes")
@click.option(
    "--summary-only",
    is_flag=True,
    help="With --post, publish only the summary and suppress inline findings",
)
@click.option(
    "--no-inline",
    is_flag=True,
    help="With --post, publish the summary and suppress inline findings",
)
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option(
    "--no-walkthrough",
    is_flag=True,
    help="Skip walkthrough generation. Useful in dry-run loops where only the "
    "inline review is needed and the extra LLM call should be saved.",
)
def review(
    url: str | None,
    pr_url: str | None,
    use_stdin: bool,
    model: str | None,
    max_comments: int | None,
    confidence: float | None,
    token: str | None,
    github_token: str | None,
    gitlab_token: str | None,
    post: bool,
    dry_run: bool,
    summary_only: bool,
    no_inline: bool,
    output_format: str,
    verbose: bool,
    config_path: str | None,
    no_walkthrough: bool,
) -> None:
    """Review a pull/merge request URL or a diff from stdin.

    URL reviews are read-only by default. Pass --post to publish the result.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if url and pr_url:
        raise click.UsageError("Provide the PR/MR URL either positionally or with --pr, not both")
    target_url = url or pr_url
    if not target_url and not use_stdin:
        raise click.UsageError("Provide <PR_OR_MR_URL>, --pr <url>, or --stdin")
    if target_url and use_stdin:
        raise click.UsageError("A PR/MR URL cannot be combined with --stdin")
    if post and dry_run:
        raise click.UsageError("--post and --dry-run are incompatible")
    if (summary_only or no_inline) and not post:
        raise click.UsageError("--summary-only and --no-inline require --post")
    if use_stdin and post:
        raise click.UsageError("--post requires a PR/MR URL")

    overrides: dict[str, object] = {}
    if model:
        overrides["llm.model"] = model
    if max_comments is not None:
        overrides["filter.max_comments"] = max_comments
    if confidence is not None:
        overrides["filter.confidence_threshold"] = confidence
    if no_walkthrough:
        overrides["review.walkthrough"] = False

    try:
        config = load_config(config_path, overrides)
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    try:
        if use_stdin:
            from mira.dashboard.models_config import llm_config_for

            llm = create_llm(llm_config_for("review", config.llm))
            indexing_llm = create_llm(llm_config_for("indexing", config.llm))
            security_llm = create_llm(llm_config_for("security", config.llm))
            engine = ReviewEngine(
                config=config,
                llm=llm,
                dry_run=True,
                indexing_llm=indexing_llm,
                security_llm=security_llm,
            )
            diff_text = sys.stdin.read()
            result = asyncio.run(engine.review_diff(diff_text))
        else:
            assert target_url is not None
            reference = parse_pull_request_reference(target_url)
            provider = create_manual_provider(
                reference,
                token=token,
                github_token=github_token,
                gitlab_token=gitlab_token,
            )
            command = ManualReviewCommand(
                config=config,
                provider=provider,
                post=post,
                post_inline_comments=not (summary_only or no_inline),
            )
            outcome = asyncio.run(command.execute(reference))
            result = outcome.result
    except MiraError as e:
        message = _redact_cli_secrets(e.safe_message, token, github_token, gitlab_token)
        raise click.ClickException(message) from e
    except Exception as e:
        raise click.ClickException(f"Review failed: {type(e).__name__}") from e

    if not use_stdin and output_format == "json":
        click.echo(_format_manual_json(outcome))
    elif not use_stdin:
        click.echo(_format_manual_text(outcome))
    elif output_format == "json":
        click.echo(_format_json(result))
    else:
        click.echo(_format_text(result))

    # Exit with non-zero if blockers found
    if any(c.severity >= Severity.BLOCKER for c in result.comments):
        sys.exit(1)


@main.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", envvar="PORT", default=8000, type=int, help="Port to bind to")
@click.option(
    "--app-id",
    envvar="MIRA_GITHUB_APP_ID",
    default=None,
    help="GitHub App ID (enables the GitHub webhook route)",
)
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    default=None,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--webhook-secret",
    envvar="MIRA_WEBHOOK_SECRET",
    default=None,
    help="Webhook secret from GitHub App settings",
)
@click.option(
    "--gitlab-token",
    envvar="MIRA_GITLAB_TOKEN",
    default=None,
    help="GitLab group/project access token (enables the GitLab webhook route)",
)
@click.option(
    "--gitlab-webhook-secret",
    envvar="MIRA_GITLAB_WEBHOOK_SECRET",
    default=None,
    help="Secret string configured on the GitLab project webhook (X-Gitlab-Token)",
)
@click.option(
    "--gitlab-base-url",
    envvar="MIRA_GITLAB_API_URL",
    default=None,
    help="GitLab API base for self-managed instances, e.g. https://gitlab.acme.com/api/v4",
)
@click.option(
    "--forgejo-token",
    envvar="MIRA_FORGEJO_TOKEN",
    default=None,
    help="Forgejo access token (enables the Forgejo webhook route)",
)
@click.option(
    "--forgejo-webhook-secret",
    envvar="MIRA_FORGEJO_WEBHOOK_SECRET",
    default=None,
    help="HMAC-SHA256 secret for verifying Forgejo webhook signatures (X-Forgejo-Signature header)",
)
@click.option(
    "--forgejo-base-url",
    envvar="MIRA_FORGEJO_API_URL",
    default=None,
    help="Forgejo API base, e.g. https://forgejo.example.com/api/v1",
)
@click.option(
    "--bot-name",
    envvar="MIRA_BOT_NAME",
    default=None,
    help="Bot @mention name. If unset, auto-detected from the platform's own identity.",
)
@click.option(
    "--config",
    "config_path",
    envvar="MIRA_CONFIG",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Path to a deployment-wide config YAML (model defaults, filter, review). "
        "Per-repo `.mira.yaml` files, when present, deep-merge over these defaults."
    ),
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def serve(
    host: str,
    port: int,
    app_id: str | None,
    private_key: str | None,
    webhook_secret: str | None,
    gitlab_token: str | None,
    gitlab_webhook_secret: str | None,
    gitlab_base_url: str | None,
    forgejo_token: str | None,
    forgejo_webhook_secret: str | None,
    forgejo_base_url: str | None,
    bot_name: str | None,
    config_path: str | None,
    verbose: bool,
) -> None:
    """Run the Mira webhook server for GitHub, GitLab, and/or Forgejo."""
    try:
        import asyncio

        import uvicorn

        from mira.config import set_global_defaults
        from mira.platforms.forgejo.auth import ForgejoTokenAuth
        from mira.platforms.github.auth import GitHubAppAuth
        from mira.platforms.gitlab.auth import GitLabTokenAuth
        from mira.platforms.server import create_app
    except ImportError as exc:
        raise click.ClickException(
            f"Missing dependency: {exc}. Install with: pip install mira-reviewer[serve]"
        ) from exc

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if config_path:
        try:
            set_global_defaults(config_path)
            click.echo(f"Loaded deployment config: {config_path}")
        except Exception as exc:
            raise click.ClickException(f"Invalid --config file: {exc}") from exc

    github_configured = bool(app_id and private_key and webhook_secret)
    gitlab_configured = bool(gitlab_token and gitlab_webhook_secret)
    forgejo_configured = bool(forgejo_token and forgejo_webhook_secret)
    if not github_configured and not gitlab_configured and not forgejo_configured:
        raise click.ClickException(
            "No platform configured. Provide GitHub App creds (--app-id, --private-key, "
            "--webhook-secret) and/or GitLab creds (--gitlab-token, --gitlab-webhook-secret)."
        )

    app_auth = None
    gitlab_auth = None
    forgejo_auth = None

    if github_configured:
        assert private_key is not None
        assert app_id is not None
        if private_key.startswith("@"):
            key_path = private_key[1:]
            try:
                with open(key_path) as f:
                    private_key = f.read()
            except FileNotFoundError:
                raise click.ClickException(f"Private key file not found: {key_path}") from None
        app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    if gitlab_configured:
        assert gitlab_token is not None
        gitlab_auth = GitLabTokenAuth(gitlab_token, gitlab_base_url or "https://gitlab.com/api/v4")

    if forgejo_configured:
        assert forgejo_token is not None
        forgejo_auth = ForgejoTokenAuth(
            forgejo_token, forgejo_base_url or "https://codeberg.org/api/v1"
        )

    # Auto-detect the bot @mention from whichever platform's own identity when
    # the user didn't override it. Falls back to "miracodeai" on a lookup blip.
    if not bot_name:
        identity_auth = app_auth or gitlab_auth or forgejo_auth
        if identity_auth is not None:
            bot_name = asyncio.run(identity_auth.get_bot_identity()) or "miracodeai"
            click.echo(f"Detected bot @mention: @{bot_name}")
        else:
            bot_name = "miracodeai"

    # Persist the resolved name so the dashboard UI can show the real handle.
    try:
        from mira.dashboard.api import _app_db

        _app_db.set_setting("bot_name", bot_name)
    except Exception as exc:
        click.echo(f"Warning: could not persist bot_name for the dashboard: {exc}")

    app = create_app(
        app_auth=app_auth,
        webhook_secret=webhook_secret,
        bot_name=bot_name,
        gitlab_auth=gitlab_auth,
        gitlab_webhook_secret=gitlab_webhook_secret,
        forgejo_auth=forgejo_auth,
        forgejo_webhook_secret=forgejo_webhook_secret,
    )

    platforms = ", ".join(
        p
        for p, on in [
            ("GitHub", github_configured),
            ("GitLab", gitlab_configured),
            ("Forgejo", forgejo_configured),
        ]
        if on
    )
    click.echo(f"Starting Mira webhook server ({platforms}) on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


@main.command("backfill-contributors")
@click.option(
    "--repo",
    "repo_spec",
    default=None,
    help="owner/repo to backfill. Omit to backfill every registered repo.",
)
@click.option("--app-id", envvar="MIRA_GITHUB_APP_ID", required=True, help="GitHub App ID")
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    required=True,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--since",
    default=None,
    help="Only events on/after this ISO date (e.g. 2024-01-01). Enables an incremental top-up.",
)
@click.option(
    "--no-commits",
    is_flag=True,
    help="Skip the per-commit phase (much lighter on the GitHub API).",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def backfill_contributors(
    repo_spec: str | None,
    app_id: str,
    private_key: str,
    since: str | None,
    no_commits: bool,
    verbose: bool,
) -> None:
    """Backfill historical contributor activity (PRs, reviews, commits) from GitHub."""
    import asyncio
    from datetime import datetime

    from mira.platforms.github.auth import GitHubAppAuth
    from mira.platforms.github.contributor_backfill import (
        backfill_all_repos,
        backfill_repo_contributions,
    )

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if private_key.startswith("@"):
        key_path = private_key[1:]
        try:
            with open(key_path) as f:
                private_key = f.read()
        except FileNotFoundError:
            raise click.ClickException(f"Private key file not found: {key_path}") from None

    app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    since_epoch: float | None = None
    if since:
        try:
            since_epoch = datetime.fromisoformat(since).replace(tzinfo=UTC).timestamp()
        except ValueError:
            raise click.ClickException("--since must be an ISO date, e.g. 2024-01-01") from None

    include_commits = not no_commits
    if repo_spec:
        if "/" not in repo_spec:
            raise click.UsageError("--repo must be in the form owner/repo")
        owner, repo = repo_spec.split("/", 1)
        counts = asyncio.run(
            backfill_repo_contributions(
                owner, repo, app_auth, since=since_epoch, include_commits=include_commits
            )
        )
        click.echo(f"Backfilled {owner}/{repo}: {counts}")
    else:
        totals = asyncio.run(
            backfill_all_repos(app_auth, since=since_epoch, include_commits=include_commits)
        )
        click.echo(f"Backfill complete: {totals}")
