# ── Stage 1: build the React UI ───────────────────────────────────
FROM node:24-slim AS ui-builder
WORKDIR /ui
COPY ui/mira/package.json ui/mira/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/mira/ ./
RUN npm run build

# ── Stage 2: backend + bundled UI ─────────────────────────────────
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv
LABEL org.opencontainers.image.source="https://github.com/zmazloom/mira-code-review"
LABEL org.opencontainers.image.description="Self-hostable AI code reviewer"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    UV_HTTP_TIMEOUT=60 \
    UV_HTTP_RETRIES=5 \
    UV_PYTHON_DOWNLOADS=never
COPY . /app
# Install the recorded dependency versions and verify their hashes.
RUN uv sync --frozen --no-dev --extra serve --extra bedrock --no-cache

# Pull the built UI in from stage 1. webhooks.create_app() picks this up
# automatically and serves it at / with SPA fallback.
COPY --from=ui-builder /ui/dist /app/ui_dist

EXPOSE 8000
# ENTRYPOINT (not CMD) so `docker run … image --config /app/mira.yaml`
# appends the args to `mira serve` instead of replacing the command.
ENTRYPOINT ["mira", "serve"]
