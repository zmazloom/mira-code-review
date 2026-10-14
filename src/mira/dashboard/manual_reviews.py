"""Background jobs for dashboard-triggered `mira review --post` commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mira.config import load_config
from mira.dashboard.models_config import llm_config_for
from mira.manual_review import ManualReviewError, parse_pull_request_reference

router = APIRouter(prefix="/api/manual-reviews", tags=["manual reviews"])
logger = logging.getLogger(__name__)


class ReviewRequest(BaseModel):
    platform: Literal["github", "gitlab"]
    url: str = Field(min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^\S+$")


class ReviewStatus(BaseModel):
    id: str
    platform: str
    url: str
    model: str | None = None
    status: Literal["reviewing", "done", "skipped", "failed"] = "reviewing"
    message: str = "Reviewing…"


@dataclass
class Job:
    owner: int
    result: ReviewStatus
    finished_at: float | None = None


_jobs: dict[str, Job] = {}
_tasks: set[asyncio.Task] = set()


def _user_id(request: Request) -> int:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user.id


def _failure_message(stderr: str) -> str:
    """Expose only the CLI's user-facing error, never raw provider logs."""
    errors = [line[7:].strip() for line in stderr.splitlines() if line.startswith("Error: ")]
    if not errors:
        return "Review failed. Check server logs and platform/model credentials."
    message = errors[-1]
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            message = message.replace(value, "[redacted]")
    message = re.sub(r"(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+", "[redacted]", message)
    return "Review failed: " + message[:1000]


async def _run(job: Job) -> None:
    try:
        command = [sys.executable, "-c", "from mira.cli import main; main()",
                   "review-notif", job.result.url, "--output", "json"]
        if os.environ.get("MIRA_CONFIG"):
            command.extend(["--config", os.environ["MIRA_CONFIG"]])
        if job.result.model:
            command.extend(["--model", job.result.model])
        # Argument arrays avoid interpreting user input as shell commands.
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True, text=True, timeout=3600, check=False,
        )
        # CLI exit code 1 also means a completed review found blockers.
        # Only the structured posting result proves completion in that case.
        try:
            outcome = json.loads(completed.stdout)
        except (ValueError, TypeError):
            outcome = {}
        if completed.returncode == 0 and outcome.get("status") == "skipped" and outcome.get("posted") is False:
            job.result.status = "skipped"
            job.result.message = "Skipped. The request title must start with 'Notif MR' (case-insensitive)."
        elif completed.returncode in (0, 1) and outcome.get("posted") is True:
            job.result.status = "done"
            job.result.message = "Done. Review completed and posted."
        else:
            message = _failure_message(getattr(completed, "stderr", ""))
            logger.warning("Manual review %s failed (CLI exit %s): %s", job.result.id, completed.returncode, message)
            job.result.status = "failed"
            job.result.message = message
    except subprocess.TimeoutExpired:
        job.result.status = "failed"
        job.result.message = "Review timed out. Check the request for any comments already posted."
    except Exception:
        logger.exception("Unable to run manual review %s", job.result.id)
        job.result.status = "failed"
        job.result.message = "Unable to run the review command. Check server configuration."
    finally:
        job.finished_at = time.monotonic()


@router.get("/models")
async def review_models(request: Request) -> dict:
    _user_id(request)
    from mira.dashboard.model_catalog import fetch_catalog

    config = load_config(os.environ.get("MIRA_CONFIG")).llm
    default = llm_config_for("review", config).model
    models = list(dict.fromkeys(model for model in (
        config.model, config.review_model, config.indexing_model,
        config.security_model, default,
    ) if model))
    catalog = await fetch_catalog(config)
    options = {model: {"value": model, "label": model} for model in models}
    for option in catalog or []:
        options[option["value"]] = option
    return {"default": default, "options": list(options.values())}


@router.post("", response_model=ReviewStatus, status_code=202)
async def start_review(body: ReviewRequest, request: Request) -> ReviewStatus:
    owner = _user_id(request)
    url = body.url.strip()
    try:
        parsed = urlparse(url)
        reference = parse_pull_request_reference(url)
    except (ManualReviewError, ValueError) as exc:
        raise HTTPException(422, "Enter a valid GitHub PR or GitLab MR URL.") from exc
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        raise HTTPException(422, "Enter a full PR/MR URL without embedded credentials.")
    if reference.provider != body.platform or reference.number < 1:
        raise HTTPException(422, "The link does not match the selected platform.")
    token_names = (
        ("GITHUB_TOKEN", "MIRA_GIT_TOKEN") if body.platform == "github"
        else ("GITLAB_TOKEN", "MIRA_GITLAB_TOKEN", "MIRA_GIT_TOKEN")
    )
    if not any(os.environ.get(name) for name in token_names):
        raise HTTPException(422, f"Configure {' or '.join(token_names)} on the server first.")
    if body.platform == "gitlab":
        api_host = urlparse(os.environ.get("MIRA_GITLAB_API_URL", "https://gitlab.com/api/v4")).hostname
        if parsed.hostname != api_host:
            raise HTTPException(422, "The MR link must use the configured GitLab server.")
    now = time.monotonic()
    for key, job in list(_jobs.items()):
        if job.finished_at is not None and now - job.finished_at > 86400:
            del _jobs[key]
    active = [job for job in _jobs.values() if job.finished_at is None]
    if any(job.owner == owner for job in active):
        raise HTTPException(409, "Your previous review is still running.")
    if len(active) >= 4:
        raise HTTPException(429, "All review slots are busy. Try again later.")
    result = ReviewStatus(id=uuid4().hex, platform=body.platform, url=url, model=body.model)
    job = Job(owner=owner, result=result)
    _jobs[result.id] = job
    task = asyncio.create_task(_run(job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return result


@router.get("/{job_id}", response_model=ReviewStatus)
async def review_status(job_id: str, request: Request) -> ReviewStatus:
    owner = _user_id(request)
    job = _jobs.get(job_id)
    if job is None or job.owner != owner:
        raise HTTPException(404, "Review not found. The server may have restarted.")
    return job.result
