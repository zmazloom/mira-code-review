<p align="center">
  <img src=".github/assets/logo.png" alt="Mira logo" width="120" />
</p>

<h1 align="center">Mira</h1>

<p align="center">
  <strong>Self-hosted AI code review. Your code, your dashboard, your LLM key.</strong>
</p>

<p align="center">
  <a href="https://docs.miracode.ai"><img src="https://img.shields.io/badge/Docs-docs.miracode.ai-orange?style=flat&logo=readthedocs&logoColor=white" alt="Documentation" /></a>
  <a href="https://discord.gg/uEU6qvYhgm"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat&logo=discord&logoColor=white" alt="Join our Discord" /></a>
</p>

<p align="center">
  <a href="https://docs.miracode.ai">Docs</a> ·
  <a href="https://discord.gg/uEU6qvYhgm">Community</a> ·
  <a href="https://docs.miracode.ai/deployment"><strong>Self-Host Guide »</strong></a> ·
  <a href="#benchmark">Benchmark</a>
</p>

Self-host every feature: full review engine, codebase indexing, vulnerability scanning, custom rules, org-wide package search, dashboard, learning loop. No paid tier, no license key, no SaaS upsell.

Mira reviews your pull requests using your choice of LLM (via [OpenRouter](https://openrouter.ai), which fronts Anthropic, OpenAI, Google, DeepSeek, and more) and posts concise, actionable feedback. The noise filter, confidence clamping, and learning loop ensure you only see comments that matter. See [`FEATURES.md`](FEATURES.md) for the full surface.

## Why Teams Choose Mira

- **Model agnostic** — Run Claude, GPT, Gemini, DeepSeek, Llama, or any OpenAI-compatible endpoint: OpenRouter, vLLM, Ollama, Together, Groq, Fireworks, or AWS Bedrock direct. Per-provider quirks are config, not code, so adding a provider is a one-line entry.
- **Zero markup on LLM costs** — Bring your own key. You pay the model provider directly; Mira never proxies your spend or adds a multiplier. The dashboard shows real per-repo, per-model cost — not estimates.
- **Learns from your context** — Mira synthesizes rules from your merged PRs: rejected comments and human review patterns become team rules that shape future reviews.
- **You set the rules** — Define custom and org-wide review rules in plain language, per-repo via `.mira.yaml` or from the dashboard.
- **Privacy first** — Self-hosted by default. Diffs, indexes, review history, and CVE data live in your SQLite or Postgres, on infra you own. No phone-home, no required telemetry, no "is this used for training?"
- **Low-noise reviews** — Confidence thresholds, dedup, a self-critique pass, and per-PR caps mean every comment is one worth reading — and Mira is the fastest tool on the public [Code Review Bench](#benchmark).
- **Catches PRs stepping on each other** — While reviewing, Mira checks the repo's other open PRs and flags merge-conflict risk and duplicate effort right in the walkthrough.
- **Indexed, cross-file context** — A full-repo code index gives the model real project context, not just the diff — plus org-wide package search and hourly OSV.dev CVE scanning across every repo.
- **GitHub, GitLab, and Forgejo** — Auto-reviews every PR and merge request and answers `@miracodeai` questions inline, with full feature parity across GitHub, GitLab, and Forgejo (incl. Codeberg). A Bitbucket adapter is next; the engine, indexer, and dashboard are provider-agnostic, so a new host is a data entry plus one provider class.
- **Self-host on day one** — Docker image with Railway / Fly.io / Render configs, SQLite or Postgres. Every feature included.

## Dashboard

![Mira dashboard](.github/assets/Dashboard.png)

## Your data, your dashboard

Most AI reviewers are SaaS: your diffs (and often the full surrounding code) leave for a third-party server, and the only "view" you get is the comments that come back on a PR. Mira flips both halves of that:

- **Your code never leaves your infra.** Diffs, embeddings, indexes, review history, vulnerability data, all stored in your SQLite or Postgres, on infrastructure you own. No phone-home, no required telemetry, no "is this used for training?" question.
- **The dashboard you see above is yours.** It's not a marketing screenshot of someone else's view of your code. CodeRabbit, Greptile, and similar SaaS reviewers don't expose anything like it. Mira's dashboard surfaces signals you don't get anywhere else:
  - **Org-wide package inventory**: answer "which repos use `lodash@4.17.20`?" in one query. Stack it next to your CVE feed for instant blast-radius checks.
  - **CVE alerts on every dependency**: hourly OSV.dev poll, severity + advisory link + fix version surfaced inline next to the package.
  - **Dependency + blast-radius graphs**: see exactly which files and repos depend on a symbol before you change it.
  - **Per-repo review event stream**: every webhook, every chunk, every cost figure, in one place for live troubleshooting.
  - **Cost & token telemetry**: actual spend per repo and per model, not estimates, because you control the LLM key.
  - **Review-health page**: stale/waiting PRs, a reviewer-responsiveness leaderboard, throughput trends, and rubber-stamp detection (approvals with no substantive review) — plus per-contributor analytics with a year-long heatmap and Mira's review-quality signal.
  - **Coming soon, change-frequency heatmaps**: surface the files that bug fixes keep landing on so you can target review attention.

If your engineering team needs answers like *"which of our repos are exposed to this CVE?"* or *"what's the blast radius of changing this function?"*, those questions stop being multi-day investigations and start being one-click dashboard pages.

## Benchmark

Mira is **the fastest tool measured** on the public [Code Review Bench](https://codereview.withmartian.com/?mode=offline), and the only one on the speed/quality Pareto frontier: every tool that scores higher on F1 takes **5–14× longer per PR**.

![Median review time per PR, Mira vs every published competitor](.github/assets/benchmark-frontier.svg)

Plotted against every published competitor on the same subset, Mira sits in the upper-left corner: everything to the right is slower; everything above it pays 5–14× the wall time for the extra F1.

![Speed vs quality: Mira on the Pareto frontier](.github/assets/benchmark-by-language.svg)

Measured on the same 50-PR offline benchmark, judged by Claude Sonnet 4.6.

| | **Mira** | Cubic-v2 | Greptile | CodeRabbit | GitHub Copilot |
|---|---:|---:|---:|---:|---:|
| F1 | **44** | 56 | 35 | 32 | 31 |
| Precision | **43%** | 50% | 32% | 24% | 24% |
| Recall | **46%** | 65% | 40% | 50% | 43% |
| Median time / PR | **~77s** | ~9m | ~5m | ~5m | ~10m |

> Methodology: scores measured against the [Martian Code Review Bench](https://codereview.withmartian.com/?mode=offline) offline dataset with Claude Sonnet 4.6 as the judge.

## Quick Start

Run Mira self-hosted to auto-review every PR and merge request and answer `@miracodeai` questions inline. GitHub (as a GitHub App), GitLab (via a group/project access token), and Forgejo/Codeberg (via an access token) are all fully supported; Bitbucket is next.

**1. Deploy** — one-click on Railway, or with Docker:

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/workspace/templates/05874bad-2d98-43f4-aa93-332f394e9ebd)

```yaml
# mira.yaml — deployment-wide defaults. Every key is optional.
llm:
  model: "anthropic/claude-sonnet-4-6"
  indexing_model: "anthropic/claude-haiku-4-5"
```

```bash
# .env — secrets only.
MIRA_GITHUB_APP_ID=123456
MIRA_GITHUB_PRIVATE_KEY="$(cat private-key.pem)"
MIRA_WEBHOOK_SECRET=your-secret
OPENROUTER_API_KEY=sk-or-...
```

```bash
docker run -p 8000:8000 --env-file .env \
  -v "$(pwd)/mira.yaml:/app/mira.yaml" \
  ghcr.io/miracodeai/mira:latest --config /app/mira.yaml
```

**2. Install the app** on your repos — every PR gets reviewed.

→ Full walkthrough: [creating the GitHub App & quickstart](https://docs.miracode.ai/quickstart) · [GitLab setup](https://docs.miracode.ai/gitlab) · [deploy options](https://docs.miracode.ai/deployment) · [choosing models, custom endpoints & AWS Bedrock](https://docs.miracode.ai/configuration/models)

## Manual PR/MR Review

Review an existing GitHub pull request or GitLab merge request directly from
the terminal. Manual reviews use the same configured review engine as webhook
reviews, including repository context, rules, self-critique, security passes,
and agentic tools. No webhook or GitHub App installation is required.

```bash
export GITHUB_TOKEN=github-personal-access-token
export COMPANY_LLM_API_KEY=company-model-key

# Display the review locally; this is read-only and does not modify the PR.
mira review https://github.com/org/repo/pull/123

# Display and publish the walkthrough, summary, and inline findings.
mira review https://github.com/org/repo/pull/123 --post

# Explicitly guarantee that no remote writes occur.
mira review https://github.com/org/repo/pull/123 --dry-run

# Publish the summary/walkthrough without inline findings.
mira review https://github.com/org/repo/pull/123 --post --summary-only
mira review https://github.com/org/repo/pull/123 --post --no-inline
```

`GITHUB_TOKEN` may be a fine-grained personal access token. It needs read
access to repository contents and pull requests; when `--post` is used, it
also needs permission to write pull-request comments. The token is never
printed by the command.

GitLab uses the same command architecture and the existing GitLab provider:

```bash
export GITLAB_TOKEN=gitlab-access-token
mira review https://gitlab.example.com/group/project/-/merge_requests/42
mira review https://gitlab.example.com/group/project/-/merge_requests/42 --post
```

For self-managed GitLab, set `MIRA_GITLAB_API_URL` to the REST v4 API base,
for example `https://gitlab.example.com/api/v4`. The legacy
`MIRA_GITLAB_TOKEN` and generic `MIRA_GIT_TOKEN` environment variables remain
supported. `--config ./mira.yaml` uses Mira's normal configuration resolution,
including custom OpenAI Chat Completions-compatible LLM endpoints and their
normal multi-step tool-calling workflow.

Mira reconciles the marked walkthrough/summary comment when the provider can
find an existing one. Existing provider behavior for inline findings is
unchanged: manually running `--post` more than once can create duplicate inline
comments.

## Configuration

`mira.yaml` (loaded via `--config`) holds deployment-wide defaults. Drop a `.mira.yaml` in any repo — or use the dashboard — to override per-repo; both deep-merge over `mira.yaml` for that repo only:

```yaml
# .mira.yaml — optional per-repo override
filter:
  confidence_threshold: 0.5  # noisier repo → lower bar
  max_comments: 10
```

→ Full schema and every key: [Configuration docs](https://docs.miracode.ai/configuration).

### Review output language

Set `review.output_language` to make the review model generate user-facing
prose directly in the requested language. No translation model or second LLM
call is used. Omitting the setting preserves Mira's existing English behavior.

```yaml
llm:
  base_url: "https://company-llm.example.com/v1"
  api_key_env: "COMPANY_LLM_API_KEY"
  model: "assistance-model"

review:
  output_language: "fa"
  rules_file: "./company-rules.md"
```

`en` and `fa` are supported explicitly; other BCP-47-style language codes are
passed through as a shared model instruction. Persian output uses Mira's
existing Markdown structure and localized fixed labels. Source-code
identifiers, annotations, paths, exception names, framework/library names,
configuration keys, API names, code snippets, and company rule IDs are
instructed to remain unchanged. Company rule text is loaded as UTF-8 and sent
to the review pipeline exactly as written, independently of the output
language.

### Company-wide review rules

An installation can load organization policy from one or more trusted local
UTF-8 Markdown or plain-text files. Paths are resolved relative to the YAML
file that defines them; Mira reads these local files directly and never reads
their contents from the pull request branch.

```yaml
# mira.yaml
review:
  rules_file: "./company-rules.md"
  # Optional additional files, kept in this order after rules_file:
  rules_files:
    - "./security-rules.md"
    - "./quality-rules.md"
  rules_max_file_size: 256000  # per file, in bytes
```

Each file is included as one compact rule block without an extra LLM parsing
call. Repeating the same resolved path loads it once. A configured file that
is missing, unreadable, empty, invalid UTF-8, or over the size limit stops the
review with a clear configuration error; Mira does not silently omit policy.

All rule sources remain additive. When instructions conflict, precedence is:
company files, repository contributor conventions, repository custom rules,
global dashboard rules, learned preferences, then Mira's built-in behavior.
Company-file prompts include only the source filename as provenance, never the
resolved installation path.

The terminal command uses the same loader, main review prompt, agentic loop,
and self-critique path as webhook reviews:

```bash
mira review https://github.com/org/repo/pull/123 --config ./mira.yaml
```

## Development

```bash
git clone https://github.com/miracodeai/mira.git
cd mira
pip install -e ".[dev,serve]"

# Run tests
pytest tests/ -v

# Run the regression suite (hits real GitHub + LLM, ~$1, ~3 min).
# Pinned PRs whose findings have flickered across iterations. Run before
# merging changes that touch prompts, the noise filter, or the engine.
OPENROUTER_API_KEY=... GITHUB_TOKEN=... pytest -m eval -v

# Lint
ruff check src/ tests/

# Type check
mypy src/mira/ --ignore-missing-imports
```

## License

Apache 2.0. See [LICENSE](LICENSE).
