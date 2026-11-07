# Review logging

Every CLI, manual, Task-Aware, webhook and direct engine review gets a UUID
`review_id`. Nested calls share the same execution, including Task-Aware
assessment and publishing. CLI JSON includes `review_id`; API jobs use their
existing job ID. CLI progress goes to stderr, preserving JSON on stdout.

Two projections of shared events run together:

* **Admin:** JSON lines with timestamp, level, review ID, event, message, event ID,
  correlation ID, parent operation ID and component. Stage events include
  operation IDs and elapsed milliseconds measured with a monotonic clock.
  Request events include sanitized URLs, methods, safe parameters, timeout,
  retry attempt, request ID, status and selected response headers.
* **User:** timestamp, level, review ID, event and a fixed, plain progress message.
  Technical errors and request metadata are excluded from this projection.

Existing Python module logs join the admin stream within a review context.
Additional integrations can attach handlers to `mira.review.admin` and
`mira.review.user`; the structured dictionary is `LogRecord.review_event`.
Prompts, source bodies, response bodies and authentication headers are not
recorded by request instrumentation. Credentials in structured fields and
exceptions are redacted. Stack traces contain frame locations without locals
or source lines. Treat admin logs as private operational data.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MIRA_REVIEW_LOG_DIR` | `data/review-logs` | Durable log root |
| `MIRA_LOG_TIMEZONE` | `UTC` | IANA timezone for ISO 8601 timestamps |
| `MIRA_ADMIN_LOG_LEVEL` | `DEBUG` | Minimum admin log level |
| `MIRA_ADMIN_LOG_STDERR` | `0` | Set to `1` for admin JSON on stderr |
| `MIRA_USER_LOG_STDERR` | `1` | Set to `0` to suppress terminal progress |
| `MIRA_REVIEW_ID` | generated | Correlation passed to a CLI subprocess |

Invalid or unavailable timezone names fall back to UTC. Install the system
timezone database or Python `tzdata` for named zones on systems without one.

Each review has a directory containing `admin-<pid>-<segment>.jsonl` and
`user-<pid>-<segment>.jsonl`. Writes are serialized within an execution, flushed
on each event, and use separate segments across processes. Async tasks inherit
context, as do `asyncio.to_thread` workers. Use `contextvars.copy_context()` when
integrating a custom thread executor. Disk errors fall back to stderr and do
not fail the review. Abrupt process termination can leave an unmatched stage
start; inspect its last request or operation to locate a stuck execution.

Mount the same log directory on all workers and retain it across deployments.
Use your deployment's retention policy to archive or delete completed review
directories; the application does not automatically delete audit logs.
SDK transport retries inside PyGithub are included in its request duration;
Mira's own retry attempts are recorded individually. No new network transport
or retry policy is introduced by this feature.

## Dashboard and API

The manual review page polls safe progress history while a job runs. The
existing SSE bus also emits `review.progress` events (safe user fields only).
SSE delivery is local to a worker; persisted history is the replay source.

* `GET /api/manual-reviews/{job_id}/logs`: job owner's user progress.
* `GET /api/reviews/task-aware/{review_id}/logs`: task job owner's user progress.
* `GET /api/admin/review-logs/{review_id}`: admin-only technical trace.

Readers merge worker segments and tolerate incomplete final lines. Responses
are bounded to 500 events by default. Manual job authorization retains the
existing process-local job registry; use sticky routing for manual jobs.
Task job authorization reuses the existing persisted task job records.

To instrument another boundary, use `@traced("component.operation", "Simple progress.")`
or `with operation(...)`. Use `review_execution()` only at a review entry point;
nested scopes automatically reuse the active review. Never pass full config,
prompts, source or HTTP bodies as event metadata.
