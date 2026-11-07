"""Review traces must stay isolated, safe and usable across execution modes."""

import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mira.observability import (
    current_review_id,
    event,
    http_request,
    operation,
    read_events,
    review_execution,
    trace_github_requester,
    traced,
)


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_REVIEW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MIRA_USER_LOG_STDERR", "0")
    monkeypatch.delenv("MIRA_REVIEW_ID", raising=False)
    return tmp_path / "logs"


async def test_concurrent_reviews_and_threads_are_isolated():
    async def run():
        with review_execution() as session:
            with operation("context", "Gathering context."):
                await asyncio.sleep(0)
                await asyncio.to_thread(logging.getLogger("mira.test").warning, "thread diagnostic")
            return session.review_id

    identifiers = await asyncio.gather(*(run() for _ in range(8)))
    assert len(set(identifiers)) == 8
    for identifier in identifiers:
        events = read_events(identifier, "admin")
        assert {e["review_id"] for e in events} == {identifier}
        assert sum(e["event"] == "review.started" for e in events) == 1
        assert any(e["event"] == "diagnostic" for e in events)
        assert all(datetime.fromisoformat(e["timestamp"]).tzinfo for e in events)
        assert events[-1]["event"] == "review.completed"
    assert current_review_id() is None


async def test_request_has_before_after_safe_metadata_and_user_separation(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "super-secret-value")

    async def send(url, **kwargs):
        assert read_events(identifier, "admin")[-1]["event"] == "request.started"
        return httpx.Response(429, headers={"retry-after": "2", "set-cookie": "secret"})

    client = SimpleNamespace(get=send, timeout=30)
    with review_execution() as session:
        identifier = session.review_id
        with operation("provider.fetch", "Fetching the code changes."):
            await http_request(
                client,
                "get",
                "gitlab",
                "https://u:p@example.org/api?token=super-secret-value&page=2",
                headers={"Authorization": "super-secret-value"},
                params={"page": 2, "private_token": "super-secret-value"},
            )
    events = read_events(identifier, "admin")
    request = next(e for e in events if e["event"] == "request.completed")
    assert request["status_code"] == 429
    assert request["duration_ms"] >= 0
    assert request["response_metadata"] == {"retry-after": "2"}
    assert "super-secret-value" not in json.dumps(events)
    assert "u:p@" not in json.dumps(events)
    assert all(
        set(e) == {"timestamp", "level", "review_id", "event", "message"}
        for e in read_events(identifier)
    )


async def test_failure_and_cancellation_restore_context(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "private-key-value")
    with pytest.raises(ValueError), review_execution() as session, operation("validate"):
        raise ValueError("failed with private-key-value")
    events = read_events(session.review_id, "admin")
    assert events[-1]["event"] == "review.failed"
    assert events[-1]["stack_trace"]
    assert "private-key-value" not in json.dumps(events)
    assert current_review_id() is None
    with pytest.raises(asyncio.CancelledError), review_execution() as cancelled:
        raise asyncio.CancelledError()
    assert read_events(cancelled.review_id, "admin")[-1]["event"] == "review.cancelled"


async def test_nested_review_includes_publishing_once():
    @traced("review", review=True)
    async def engine():
        event("engine.done", "Engine finished")

    with review_execution("task_aware") as session:
        await engine()
        with operation("task.publish", "Publishing results."):
            pass
    events = read_events(session.review_id, "admin")
    assert sum(e["event"] == "review.started" for e in events) == 1
    assert events[-1]["event"] == "review.completed"
    assert any(e.get("operation") == "task.publish" for e in events)


async def test_request_failure_logged_and_propagated():
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadTimeout("slow")), timeout=2)
    with pytest.raises(httpx.ReadTimeout), review_execution() as session:
        await http_request(client, "get", "github", "https://example.org")
    assert any(e["event"] == "request.failed" for e in read_events(session.review_id, "admin"))


def test_sdk_calls_and_replay_after_context_closed():
    sdk = SimpleNamespace(
        requestJson=lambda *a, **k: (200, {}, "private body"),
        requestBlob=lambda *a, **k: (201, {}, "blob"),
        timeout=15,
    )
    trace_github_requester(sdk)
    with review_execution() as session:
        sdk.requestJson("GET", "/repos/o/r", headers={"Authorization": "secret"})
    events = read_events(session.review_id, "admin")
    assert any(e.get("status_code") == 200 for e in events)
    assert "private body" not in json.dumps(events)


def test_storage_failure_does_not_abort_review(monkeypatch, capsys):
    monkeypatch.setattr(
        "pathlib.Path.mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with review_execution():
        event("test", "Still working", user_message="Still working.")
    assert "storage unavailable" in capsys.readouterr().err


def test_invalid_ids_cannot_escape_storage():
    with pytest.raises(ValueError):
        read_events("../secret", "admin")


def test_worker_processes_write_independent_replayable_segments(monkeypatch):
    import os
    import subprocess
    import sys

    identifier = "b" * 32
    for _ in range(2):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from mira.observability import review_execution; "
                "scope = review_execution(); scope.__enter__(); scope.__exit__(None, None, None)",
            ],
            env={**os.environ, "MIRA_REVIEW_ID": identifier},
            check=True,
            capture_output=True,
        )
    events = read_events(identifier, "admin")
    assert sum(e["event"] == "review.started" for e in events) == 2
    assert len({e["correlation_id"] for e in events}) == 2


def test_cli_json_stdout_is_separate_from_progress(monkeypatch):
    from unittest.mock import patch

    from click.testing import CliRunner

    from mira.cli import main
    from mira.config import MiraConfig
    from mira.core.engine import ReviewEngine
    from mira.models import ReviewResult

    monkeypatch.setenv("MIRA_USER_LOG_STDERR", "1")
    with (
        patch("mira.cli.load_config", return_value=MiraConfig()),
        patch("mira.cli.create_llm"),
        patch.object(
            ReviewEngine, "review_diff", AsyncMock(return_value=ReviewResult(summary="Complete"))
        ),
    ):
        result = CliRunner().invoke(main, ["review", "--stdin", "--output", "json"], input="diff")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["summary"] == "Complete"
    assert "Starting the code review" in result.stderr


async def test_existing_event_bus_receives_progress_from_thread():
    from mira.dashboard.events import bus

    queue = await bus.subscribe()
    try:
        with review_execution():
            await asyncio.to_thread(
                event,
                "thread.progress",
                "Technical detail",
                user_message="Reading repository files.",
            )
        received = []
        while not queue.empty():
            received.append(queue.get_nowait())
        # Delivery is scheduled on the subscriber loop.
        if not received:
            received.append(await asyncio.wait_for(queue.get(), timeout=2))
        assert all(item.type == "review.progress" for item in received)
        assert "Technical detail" not in json.dumps([item.data for item in received])
    finally:
        await bus.unsubscribe(queue)


def test_parent_operations_and_invalid_timezone_fallback(monkeypatch):
    monkeypatch.setenv("MIRA_LOG_TIMEZONE", "invalid/timezone")
    with review_execution() as session, operation("parent"), operation("child"):
        pass
    events = read_events(session.review_id, "admin")
    parent = next(e for e in events if e.get("operation") == "parent")
    children = [e for e in events if e.get("operation") == "child"]
    assert all(e["parent_operation_id"] == parent["operation_id"] for e in children)
    assert events[0]["timestamp"].endswith("+00:00")


async def test_user_logs_are_owner_only_and_admin_logs_require_admin():
    from fastapi import HTTPException

    from mira.dashboard import manual_reviews
    from mira.dashboard.routers.admin import admin_review_logs

    identifier = "a" * 32
    owner = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=7, is_admin=False)))
    other = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=8, is_admin=False)))
    job = manual_reviews.Job(
        owner=7,
        result=manual_reviews.ReviewStatus(
            id=identifier, platform="github", url="https://example.org"
        ),
    )
    manual_reviews._jobs[identifier] = job
    try:
        with review_execution(review_id=identifier):
            event("private", "Admin technical details", user_message="Reviewing changes.")
        result = await manual_reviews.review_logs(identifier, owner, limit=500)
        assert "Admin technical details" not in json.dumps(result)
        with pytest.raises(HTTPException) as denied:
            await manual_reviews.review_logs(identifier, other, limit=500)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied:
            await admin_review_logs(identifier, owner)
        assert denied.value.status_code == 403
    finally:
        manual_reviews._jobs.pop(identifier, None)
