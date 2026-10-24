"""Authenticated asynchronous TASK_AWARE jobs using dashboard jobs and storage."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from mira.config import load_config
from mira.core.task_review import TaskReviewOptions
from mira.dashboard import manual_reviews as jobs
from mira.manual_review import ManualReviewCommand, create_manual_provider

router = APIRouter(prefix="/api/reviews/task-aware", tags=["task-aware reviews"])


class TaskReviewRequest(BaseModel):
    pr_url: str = Field(min_length=1, max_length=2048)
    task_prompt: str = Field(min_length=3, max_length=16000)
    model: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^\S+$")
    review_options: TaskReviewOptions = Field(default_factory=TaskReviewOptions)

    @field_validator("task_prompt")
    @classmethod
    def task_not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3 or "\x00" in value:
            raise ValueError("Provide a non-empty implementation task.")
        return value


class TaskReviewStatus(jobs.ReviewStatus):
    mode: str = "TASK_AWARE"
    phase: str = "queued"
    result: dict | None = None
    duration_seconds: float | None = None
    publishing_status: str = "pending"
    errors: list[str] = Field(default_factory=list)


def _db():
    from mira.dashboard.api import _app_db

    return _app_db


def _save(job: jobs.Job) -> None:
    _db().set_setting(
        "task_review:" + job.result.id,
        json.dumps(
            {
                "owner": job.owner,
                "result": job.result.model_dump(mode="json"),
            }
        ),
    )


def structured_result(command, result) -> dict:
    session = command.task_session
    info = getattr(command.engine, "_pr_info", None)
    data = session.report()
    if info:
        data["pull_request"] = asdict(info)
        # Description and commit messages are not required by status clients.
        data["pull_request"].pop("description", None)
        data["pull_request"]["commits"] = [{"sha": c["sha"]} for c in info.commits]
        data["reviewed_commit_sha"] = info.head_sha
        data["source_repository"] = info.source_repository
        data["target_repository"] = f"{info.owner}/{info.repo}"
    if result is not None:
        comments = result.comments + result.repository_comments
        details = {(d["path"], d["line"], d["title"]): d for d in session.finding_details}
        findings = []
        for comment in comments:
            finding = {
                **asdict(comment),
                "severity": comment.severity.name.lower(),
                "location": "summary" if comment in result.repository_comments else "inline",
                "introduced_by_pr": None,
                "requirement_id": None,
                "evidence": [],
            }
            detail = details.get((comment.path, comment.line, comment.title), {})
            for key in (
                "introduced_by_pr",
                "requirement_id",
                "evidence",
                "impact",
                "consistency_kind",
                "consistency_assessment",
                "convention_dimension",
                "comparison_target",
                "convention_scope",
                "convention_evidence",
            ):
                if key in detail:
                    finding[key] = detail[key]
            findings.append(finding)
        data["findings"] = findings
        data["findings_by_severity"] = {
            s: [f for f in findings if f["severity"] == s]
            for s in {f["severity"] for f in findings}
        }
        data["findings_by_category"] = {
            c: [f for f in findings if f["category"] == c]
            for c in {f["category"] for f in findings}
        }
        data["summary"] = result.summary
        data["selected_changed_files"] = result.reviewed_paths
        data["reviewed_changed_files"] = result.reviewed_paths if not session.limitations else None
        if result.skipped_paths:
            session.note(
                "Some changed files were skipped by the standard review priority or size limits."
            )
    # Aggregate distinct configured clients, rather than counting shared clients twice.
    clients = {
        id(c.provider): c.provider
        for c in (command.engine.llm, command.engine.indexing_llm, command.engine.security_llm)
    }
    usages = [c.usage for c in clients.values()]
    data["token_usage"] = (
        {
            key: sum(u.get(key, 0) for u in usages)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        if any(u.get("total_tokens") for u in usages)
        else None
    )
    data["limitations"] = list(session.limitations)
    data["partial"] = bool(
        session.limitations or any(r.status == "unverified" for r in session.requirements)
    )
    return data


async def _run(job: jobs.Job, body: TaskReviewRequest, reference) -> None:
    status = cast(TaskReviewStatus, job.result)
    started = time.monotonic()
    command = None

    def progress(phase: str) -> None:
        status.phase = phase
        _save(job)

    try:
        status.phase = "resolving_and_reviewing"
        _save(job)
        command = ManualReviewCommand(
            config=load_config(os.environ.get("MIRA_CONFIG")),
            provider=create_manual_provider(reference),
            model_override=body.model,
            task_prompt=body.task_prompt,
            task_options=body.review_options,
            task_progress=progress,
        )
        async with asyncio.timeout(body.review_options.max_seconds):
            outcome = await command.execute(reference)
        status.result = structured_result(command, outcome.result)
        status.publishing_status = (outcome.result.task_review or {}).get(
            "publishing_status", "unknown"
        )
        status.status = "done"
        status.message = (
            "Review completed with partial verification."
            if status.result["partial"]
            else "Review completed."
        )
        status.phase = "completed"
    except (Exception, asyncio.CancelledError) as exc:
        # External errors can include credentials, model prompts and private code.
        # Expose fixed messages; never log exception text or the task prompt.
        message = (
            "Review timed out; verification is partial."
            if isinstance(exc, TimeoutError)
            else "Review interrupted or failed. Check server credentials and configuration."
        )
        status.status = "failed"
        status.phase = "failed"
        status.message = message
        status.errors = [message]
        status.publishing_status = "unknown" if body.review_options.post_comments else "disabled"
        if command and getattr(command, "task_session", None):
            session = command.task_session
            session.note(message)
            status.result = structured_result(command, getattr(command, "result", None))
    finally:
        if command and getattr(command, "task_session", None):
            await command.task_session.close()
        job.finished_at = time.monotonic()
        status.duration_seconds = job.finished_at - started
        _save(job)


@router.post("", response_model=TaskReviewStatus, status_code=202)
async def start_task_review(body: TaskReviewRequest, request: Request) -> TaskReviewStatus:
    owner = jobs._user_id(request)
    reference = jobs.validate_review_url(body.pr_url)
    if body.model:
        catalog = await jobs.review_models(request)
        if body.model not in {option["value"] for option in catalog["options"]}:
            raise HTTPException(422, "Choose a model advertised by the configured service.")
    result = TaskReviewStatus(
        id=uuid4().hex,
        platform=reference.provider,
        url=body.pr_url.strip(),
        model=body.model,
        publishing_status="pending" if body.review_options.post_comments else "disabled",
    )
    job = jobs.reserve_job(owner, result)
    try:
        _save(job)
    except Exception:
        jobs._jobs.pop(result.id, None)
        raise HTTPException(503, "Review storage is unavailable. Try again later.") from None
    task = asyncio.create_task(_run(job, body, reference))
    jobs._tasks.add(task)
    task.add_done_callback(jobs._tasks.discard)
    return result


@router.get("/{review_id}", response_model=TaskReviewStatus)
async def task_review_status(review_id: str, request: Request) -> TaskReviewStatus:
    owner = jobs._user_id(request)
    job = jobs._jobs.get(review_id)
    if job and job.owner == owner and isinstance(job.result, TaskReviewStatus):
        return job.result
    if len(review_id) == 32 and all(c in "0123456789abcdef" for c in review_id):
        raw = _db().get_setting("task_review:" + review_id)
        if raw:
            data = json.loads(raw)
            if data["owner"] == owner:
                result = TaskReviewStatus.model_validate(data["result"])
                if result.status == "reviewing":
                    result.status = "failed"
                    result.message = (
                        "Review worker unavailable. Rerun the review after a server restart."
                    )
                return result
    raise HTTPException(404, "Review not found.")


@router.get("/{review_id}/findings")
async def task_review_findings(review_id: str, request: Request) -> dict:
    status = await task_review_status(review_id, request)
    return {
        "id": status.id,
        "mode": status.mode,
        "status": status.status,
        "result": status.result,
        "publishing_status": status.publishing_status,
        "errors": status.errors,
    }
