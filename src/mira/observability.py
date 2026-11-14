"""Shared review events, structured admin traces and safe user progress.

Context is local to an async task and follows asyncio.to_thread. Each execution
writes its own append-only segments, so workers never share a file handle.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SECRET = re.compile(r"token|secret|password|authorization|api.?key|cookie", re.I)
_SAFE_PARAMS = {"page", "per_page", "state", "ref", "sha", "recursive", "limit", "offset"}
_context: ContextVar[Session | None] = ContextVar("mira_review", default=None)
_operation: ContextVar[str | None] = ContextVar("mira_operation", default=None)
_attempt: ContextVar[int] = ContextVar("mira_retry_attempt", default=1)
_setup_lock = threading.Lock()


def redact(value: Any) -> Any:
    """Redact credentials recursively; never serialize arbitrary object reprs."""
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if _SECRET.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        session = _context.get()
        for secret in session.secrets if session else ():
            value = value.replace(secret, "[redacted]")
        for name, secret in os.environ.items():
            if len(secret) >= 4 and _SECRET.search(name):
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+", "[redacted]", value)
        value = re.sub(r"(?i)(bearer|basic)\s+\S+", r"\1 [redacted]", value)
        value = re.sub(r"(?i)((?:token|password|api_key|secret)=)[^\s&]+", r"\1[redacted]", value)
        value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
        return value[:16000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def safe_url(url: str) -> str:
    try:
        parts = urlsplit(str(url))
        host = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode(
            [(k, v if k in _SAFE_PARAMS else "[redacted]") for k, v in parse_qsl(parts.query)]
        )
        return redact(urlunsplit((parts.scheme, host, parts.path, query, "")))
    except ValueError:
        return "[invalid URL]"


def current_review_id() -> str | None:
    session = _context.get()
    return session.review_id if session else None


def _valid_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("review_id must be a UUID hex string")
    return value


def log_directory() -> Path:
    return Path(os.environ.get("MIRA_REVIEW_LOG_DIR", "data/review-logs"))


@dataclass
class Session:
    review_id: str
    timezone: Any
    directory: Path
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    segment: str = field(default_factory=lambda: f"{os.getpid()}-{uuid4().hex}")
    lock: Any = field(default_factory=threading.RLock)
    closed: bool = False
    storage_failed: bool = False
    secrets: set[str] = field(default_factory=set)
    terminal: bool = False
    partial: bool = False
    limitations: list[str] = field(default_factory=list)

    def write(self, stream: str, event: dict) -> None:
        with self.lock:
            if self.closed:
                return
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.directory / f"{stream}-{self.segment}.jsonl"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as output:
                    output.write(json.dumps(event, ensure_ascii=True) + "\n")
            except OSError:
                if not self.storage_failed:
                    self.storage_failed = True
                    sys.stderr.write("Mira review log storage unavailable; using stderr.\n")
                sys.stderr.write(json.dumps(event, ensure_ascii=True) + "\n")


class ReviewHandler(logging.Handler):
    """Bridge existing Mira logging into the current review's admin trace."""

    def emit(self, record: logging.LogRecord) -> None:
        if _context.get() is None or record.name.startswith("mira.review."):
            return
        try:
            # Existing root console handlers must not mix raw diagnostics into
            # the user progress stream. Preserve custom handlers and caplog.
            for handler in logging.getLogger().handlers:
                if type(handler) is logging.StreamHandler and not any(
                    isinstance(f, ReviewConsoleFilter) for f in handler.filters
                ):
                    handler.addFilter(ReviewConsoleFilter())
            details = {}
            if record.exc_info and record.exc_info[1]:
                details = _error(record.exc_info[1])
            event(
                "diagnostic",
                record.getMessage(),
                level=record.levelname,
                component=record.name,
                **details,
            )
        except Exception:
            # Logging must never interrupt a review or recurse through logging.
            pass


class ReviewConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_context.get() and record.name.startswith("mira."))


def _setup() -> None:
    with _setup_lock:
        logger = logging.getLogger("mira")
        if not any(isinstance(h, ReviewHandler) for h in logger.handlers):
            logger.addHandler(ReviewHandler())
        logger.setLevel(logging.DEBUG)
        for name in ("mira.review.admin", "mira.review.user"):
            stream_logger = logging.getLogger(name)
            stream_logger.propagate = False
            if not stream_logger.handlers:
                stream_logger.addHandler(logging.NullHandler())


def _error(exc: BaseException) -> dict:
    # Stack frames without source lines, locals or provider response bodies.
    frames = traceback.extract_tb(exc.__traceback__)
    return {
        "error": {"type": type(exc).__name__, "message": redact(str(exc))},
        "stack_trace": [{"file": f.filename, "line": f.lineno, "function": f.name} for f in frames],
    }


def event(
    name: str,
    message: str,
    *,
    user_message: str | None = None,
    level: str = "INFO",
    component: str = "mira",
    **fields: Any,
) -> None:
    session = _context.get()
    if session is None or session.closed:
        return
    if name in {"review.completed", "review.failed", "review.cancelled"}:
        session.terminal = True
    common = {
        "timestamp": datetime.now(session.timezone).isoformat(timespec="milliseconds"),
        "level": level,
        "review_id": session.review_id,
        "event": name,
    }
    admin = {
        **common,
        "message": redact(message),
        "event_id": uuid4().hex,
        "correlation_id": session.correlation_id,
        "parent_operation_id": _operation.get(),
        "component": component,
        **redact(fields),
    }
    threshold = getattr(
        logging, os.environ.get("MIRA_ADMIN_LOG_LEVEL", "DEBUG").upper(), logging.DEBUG
    )
    if getattr(logging, level, logging.INFO) >= threshold:
        session.write("admin", admin)
        logging.getLogger("mira.review.admin").log(
            getattr(logging, level, logging.INFO), admin["message"], extra={"review_event": admin}
        )
        if os.environ.get("MIRA_ADMIN_LOG_STDERR") == "1":
            sys.stderr.write(json.dumps(admin, ensure_ascii=True) + "\n")
    if user_message:
        user = {**common, "message": user_message}
        session.write("user", user)
        from mira.dashboard.events import bus

        bus.emit("review.progress", user)
        logging.getLogger("mira.review.user").info(user_message, extra={"review_event": user})
        if os.environ.get("MIRA_USER_LOG_STDERR", "1") == "1":
            sys.stderr.write(
                f"{user['timestamp']} {level} [{session.review_id}] {name}: {user_message}\n"
            )


def _complete_review(session: Session, started: float) -> None:
    event(
        "review.completed",
        "Review completed with partial coverage" if session.partial else "Review completed",
        user_message="Code review completed with partial coverage. See review limitations."
        if session.partial
        else "Code review completed.",
        level="WARNING" if session.partial else "INFO",
        partial=session.partial,
        limitations=session.limitations,
        duration_ms=(time.monotonic() - started) * 1000,
    )


def record_review_result(result: Any) -> None:
    """Carry result coverage through nested entry points to the terminal event."""
    session = _context.get()
    result = getattr(result, "result", result)
    report = getattr(result, "task_review", None)
    if session is not None and isinstance(report, dict):
        limitations = report.get("limitations") or []
        session.partial = session.partial or bool(
            limitations or report.get("unverified_requirements")
        )
        session.limitations = list(dict.fromkeys([*session.limitations, *limitations]))


@contextmanager
def review_execution(mode: str = "standard", review_id: str | None = None):
    """Nested entry points share one execution, including task publishing."""
    if _context.get() is not None:
        yield _context.get()
        return
    _setup()
    zone = os.environ.get("MIRA_LOG_TIMEZONE", "UTC")
    try:
        timezone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = UTC
    identifier = _valid_id(review_id or os.environ.get("MIRA_REVIEW_ID") or uuid4().hex)
    session = Session(identifier, timezone, log_directory() / identifier)
    token = _context.set(session)
    op_token = _operation.set(None)
    started = time.monotonic()
    try:
        event("review.request_received", "Review request received")
        event("review.id_created", "Review ID assigned")
        event("review.mode_selected", "Review mode selected", mode=mode)
        event("review.started", "Review started", user_message="Starting the code review.")
        yield session
    except BaseException as exc:
        cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        # CLI exit 1 means findings with blockers, not a failed execution.
        if isinstance(exc, SystemExit) and exc.code in (None, 0, 1):
            _complete_review(session, started)
        else:
            event(
                "review.cancelled" if cancelled else "review.failed",
                "Review interrupted",
                user_message="Code review cancelled."
                if cancelled
                else "Code review failed. Check the review logs.",
                level="WARNING" if cancelled else "ERROR",
                duration_ms=(time.monotonic() - started) * 1000,
                **_error(exc),
            )
        raise
    else:
        if not session.terminal:
            _complete_review(session, started)
    finally:
        session.closed = True
        _operation.reset(op_token)
        _context.reset(token)


@contextmanager
def operation(name: str, user_message: str | None = None, **metadata: Any):
    identifier = uuid4().hex
    parent = _operation.get()
    started = time.monotonic()
    event(
        "stage.started",
        name,
        user_message=user_message,
        operation=name,
        operation_id=identifier,
        **metadata,
    )
    token = _operation.set(identifier)
    retry_token = _attempt.set(1)
    try:
        yield
    except BaseException as exc:
        event(
            "stage.cancelled" if isinstance(exc, asyncio.CancelledError) else "stage.failed",
            name,
            level="ERROR",
            operation=name,
            operation_id=identifier,
            parent_operation_id=parent,
            duration_ms=(time.monotonic() - started) * 1000,
            **_error(exc),
        )
        raise
    else:
        event(
            "stage.completed",
            name,
            operation=name,
            operation_id=identifier,
            parent_operation_id=parent,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    finally:
        _attempt.reset(retry_token)
        _operation.reset(token)


def traced(name: str, user_message: str | None = None, *, review: bool = False):
    """Instrument an existing sync/async boundary without changing its contract."""

    def decorate(fn):
        def register(args, kwargs):
            session = _context.get()
            if session:
                bound = inspect.signature(fn).bind_partial(*args, **kwargs)
                for key, value in bound.arguments.items():
                    if _SECRET.search(key) and isinstance(value, str) and value:
                        session.secrets.add(value)
                if args:
                    secret = getattr(args[0], "_token", None)
                    if isinstance(secret, str) and secret:
                        session.secrets.add(secret)

        def scope(args, kwargs):
            mode = (
                "task_aware"
                if kwargs.get("task_prompt")
                or (
                    args
                    and (
                        getattr(args[0], "task_prompt", None) is not None
                        or getattr(args[0], "task_session", None) is not None
                    )
                )
                else "standard"
            )
            return (
                review_execution(mode)
                if review
                else operation(name, user_message, component=fn.__module__)
            )

        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with scope(args, kwargs):
                    register(args, kwargs)
                    result = await fn(*args, **kwargs)
                    if hasattr(result, "review_id"):
                        result.review_id = current_review_id() or ""
                    record_review_result(result)
                    return result

            return async_wrapper

        @wraps(fn)
        def wrapper(*args, **kwargs):
            with scope(args, kwargs):
                register(args, kwargs)
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def retry_started(state) -> None:
    _attempt.set(state.attempt_number)
    if state.attempt_number > 1:
        event("request.retry", "Retrying external operation", retry_attempt=state.attempt_number)


def retry_scheduled(state) -> None:
    """Record the actual retry cause without logging prompts or response bodies."""
    exc = state.outcome.exception() if state.outcome else None
    event(
        "request.retry_scheduled",
        "External operation will be retried",
        retry_attempt=state.attempt_number,
        next_attempt=state.attempt_number + 1,
        delay_seconds=state.next_action.sleep if state.next_action else 0,
        error_type=type(exc).__name__ if exc else None,
        error_message=str(exc) if exc else None,
        error_code=exc.code if hasattr(exc, "code") else None,
    )


def trace_github_requester(requester) -> None:
    """Instrument this SDK instance, including its threaded REST calls."""
    for method_name in ("requestJson", "requestBlob"):
        original = getattr(requester, method_name)

        def wrap(call):
            @wraps(call)
            def request(verb, url, *args, **kwargs):
                metadata = {
                    "provider": "github",
                    "http_method": verb,
                    "url": safe_url(url),
                    "request_id": uuid4().hex,
                    "retry_attempt": _attempt.get(),
                    "parameters": {},
                    "timeout": redact(getattr(requester, "timeout", "SDK default")),
                    "operation": _operation.get() or "github.request",
                }
                started = time.monotonic()
                event("request.started", "Sending GitHub SDK request", **metadata)
                try:
                    result = call(verb, url, *args, **kwargs)
                except BaseException as exc:
                    event(
                        "request.failed",
                        "GitHub SDK request failed",
                        level="ERROR",
                        **metadata,
                        duration_ms=(time.monotonic() - started) * 1000,
                        **_error(exc),
                    )
                    raise
                event(
                    "request.completed",
                    "GitHub SDK response received",
                    **metadata,
                    status_code=result[0],
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                return result

            return request

        setattr(requester, method_name, wrap(original))


async def http_request(
    client, method: str, trace_provider: str, url: str, *, use_request: bool = False, **kwargs
):
    """Record safe metadata around each HTTP call, including failed requests."""
    request_id = uuid4().hex
    metadata = {
        "provider": trace_provider,
        "http_method": method.upper(),
        "url": safe_url(url),
        "request_id": request_id,
        "operation": _operation.get() or "http.request",
        "retry_attempt": _attempt.get(),
        "parameters": {k: v for k, v in (kwargs.get("params") or {}).items() if k in _SAFE_PARAMS},
        "timeout": redact(kwargs.get("timeout", str(getattr(client, "timeout", "default")))),
    }
    started = time.monotonic()
    event("request.started", "Sending external request", **metadata)
    try:
        response = (
            await client.request(method, url, **kwargs)
            if use_request
            else await getattr(client, method.lower())(url, **kwargs)
        )
    except BaseException as exc:
        event(
            "request.failed",
            "External request failed",
            level="ERROR",
            **metadata,
            duration_ms=(time.monotonic() - started) * 1000,
            **_error(exc),
        )
        raise
    event(
        "request.completed",
        "External response received",
        **metadata,
        status_code=response.status_code,
        duration_ms=(time.monotonic() - started) * 1000,
        response_metadata={
            k: response.headers[k]
            for k in ("x-request-id", "x-github-request-id", "x-ratelimit-remaining", "retry-after")
            if k in response.headers
        },
    )
    return response


def read_events(review_id: str, stream: str = "user", *, limit: int = 500) -> list[dict]:
    """Read worker segments; tolerate the final line of an in-flight write."""
    from collections import deque

    _valid_id(review_id)
    if stream not in {"user", "admin"}:
        raise ValueError("Unknown log stream")
    events: list[dict] = []
    for path in (log_directory() / review_id).glob(f"{stream}-*.jsonl"):
        with path.open(encoding="utf-8") as source:
            for line in deque(source, maxlen=min(max(limit, 1), 2000)):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return sorted(events, key=lambda e: e["timestamp"])[-min(max(limit, 1), 2000) :]
