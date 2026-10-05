"""Prompt builder for PR review."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig
from mira.core.context import build_file_context_string
from mira.llm.prompts.footguns import get_footguns_for_files
from mira.llm.prompts.verify_fixes import _extract_issue_description
from mira.localization import append_language_instruction
from mira.models import FileDiff, UnresolvedThread

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_COMPANY_PASS_MAX_CHARS = 6000


def _get_template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_review_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
    existing_comments: list[UnresolvedThread] | None = None,
    code_context: str = "",
    learned_rules: list[str] | None = None,
    custom_rules: list[dict[str, str]] | None = None,
    file_history: dict | None = None,
    review_round: int = 1,
    resolved_threads: list[dict] | None = None,
    team_conventions: str = "",
    company_rules: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build the review prompt messages for the LLM.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("review.jinja2")

    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]

    # Pre-clean existing comment bodies so the template gets concise descriptions
    cleaned_comments = None
    if existing_comments:
        cleaned_comments = [
            {"path": c.path, "line": c.line, "description": _extract_issue_description(c.body)}
            for c in existing_comments
        ]

    # Decision archaeology — flatten history dict into a list of (path, entries)
    history_for_template = None
    if file_history:
        history_for_template = [
            {
                "path": path,
                "commits": [
                    {
                        "sha": e.sha,
                        "message": e.message,
                        "author": e.author,
                        "date": e.date,
                    }
                    for e in entries
                ],
            }
            for path, entries in file_history.items()
            if entries
        ]

    footguns = get_footguns_for_files(files)

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        file_contexts=file_contexts,
        file_paths=file_paths,
        confidence_threshold=config.filter.confidence_threshold,
        max_comments=config.filter.max_comments,
        focus_only_on_problems=config.review.focus_only_on_problems,
        existing_comments=cleaned_comments,
        has_code_context=bool(code_context),
        learned_rules=learned_rules,
        custom_rules=custom_rules,
        file_history=history_for_template,
        review_round=review_round,
        resolved_threads=resolved_threads,
        team_conventions=team_conventions,
        company_rules=company_rules,
        footguns=footguns,
    )
    system_content = append_language_instruction(
        system_content, config.review.prompt_output_language
    )

    # Build user message with optional code context before diffs
    user_parts = []
    if code_context:
        user_parts.append(code_context)
    user_parts.extend(file_contexts)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def build_company_review_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    company_rules: list[dict[str, str]],
    pr_title: str = "",
    pr_description: str = "",
    code_context: str = "",
) -> list[dict[str, str]]:
    """Build a dedicated pass that checks every company rule.

    Keeping this pass separate from the broad quality review prevents a long
    installation policy from being diluted by generic review instructions.
    """
    file_paths = [f.path for f in files]
    rules = "\n\n".join(
        f"### Source: {rule.get('source', 'company rules')}\n{rule.get('content', '')}"
        for rule in company_rules
    )
    system_content = f"""You are Mira's mandatory company-policy reviewer.

Your only task is to check the changed code against EVERY applicable rule in
the company policy below. Evaluate the rules one by one; do not sample them,
summarize them, or focus only on bugs and security. Report every confirmed
violation, including naming, clean-code, framework, architecture, testing,
formatting, and required companion changes in other files.

If a rule requires a corresponding change outside the diff, use `grep_repo`
and `read_file` to locate and verify it. In particular, for every new API or
endpoint, verify that its corresponding Rest-over-Async class contains the
required async-call method. If it is missing, anchor the finding to the added
endpoint line. An unchanged companion file is not a reason to omit a finding.

Every company-policy violation is at least `warning` severity and should use
confidence >= {config.filter.confidence_threshold}. Do not suppress findings
as style preferences, optional improvements, or because of comment limits.
Only cite exact added or modified code in `existing_code`.

Valid comment paths:
{chr(10).join(f'- `{path}`' for path in file_paths)}

Pull request title: {pr_title}
Pull request description: {pr_description}

## Mandatory Company Policy

{rules}
"""
    system_content = append_language_instruction(
        system_content, config.review.prompt_output_language
    )
    user_parts = [code_context] if code_context else []
    user_parts.extend(build_file_context_string(f) for f in files)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def partition_company_rules(
    company_rules: list[dict[str, str]],
    max_chars: int = _COMPANY_PASS_MAX_CHARS,
) -> list[list[dict[str, str]]]:
    """Split long policy documents on Markdown sections for focused passes."""
    sections: list[dict[str, str]] = []
    for rule in company_rules:
        content = rule.get("content", "")
        parts = [part.strip() for part in re.split(r"(?=^##\s+)", content, flags=re.MULTILINE)]
        parts = [part for part in parts if part]
        for part in parts or [content]:
            sections.append({**rule, "content": part})

    partitions: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_size = 0
    for section in sections:
        size = len(section.get("content", ""))
        if current and current_size + size > max_chars:
            partitions.append(current)
            current = []
            current_size = 0
        current.append(section)
        current_size += size
    if current:
        partitions.append(current)
    return partitions


def build_security_review_prompt(
    files: list[FileDiff],
    pr_title: str = "",
    output_language: str | None = None,
) -> list[dict[str, str]]:
    """Build the security-focused review prompt messages for the LLM.

    Runs in parallel with the main review pass. Output is merged into the
    main review's comments list and goes through the same noise filter
    (dedup against any overlap with main-pass findings).
    """
    env = _get_template_env()
    template = env.get_template("security_review.jinja2")
    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]
    system_content = template.render(pr_title=pr_title, file_paths=file_paths)
    system_content = append_language_instruction(system_content, output_language)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(file_contexts)},
    ]


def build_dependency_review_prompt(
    files: list[FileDiff],
    existing_packages: list[str] | None = None,
    pr_title: str = "",
    output_language: str | None = None,
) -> list[dict[str, str]]:
    """Build the dependency-overlap review prompt messages for the LLM.

    Runs in parallel with the main review pass over only the changed manifest
    files. ``existing_packages`` is the set of dependency names already declared
    in the repo (from the index) so the model can spot a newly-added package
    that duplicates the functionality of one already present. Output is merged
    into the main review's comments list and goes through the same noise filter.
    """
    env = _get_template_env()
    template = env.get_template("dependency_review.jinja2")
    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]
    system_content = template.render(
        pr_title=pr_title,
        file_paths=file_paths,
        existing_packages=existing_packages or [],
    )
    system_content = append_language_instruction(system_content, output_language)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(file_contexts)},
    ]


_HUNK_HEADER_RE = re.compile(r"^@@\s.*@@", re.MULTILINE)


def _extract_hunk_headers(f: FileDiff) -> list[str]:
    """Extract @@ ... @@ header lines from a file's hunks."""
    headers: list[str] = []
    for hunk in f.hunks:
        for m in _HUNK_HEADER_RE.finditer(hunk.content):
            headers.append(m.group(0))
    return headers


def build_walkthrough_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
) -> list[dict[str, str]]:
    """Build the walkthrough prompt messages for the LLM.

    Uses only file metadata (not full diffs) to keep the prompt compact.
    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("walkthrough.jinja2")

    files_metadata = [
        {
            "path": f.path,
            "change_type": f.change_type.value,
            "language": f.language,
            "added_lines": f.added_lines,
            "deleted_lines": f.deleted_lines,
            "hunk_headers": _extract_hunk_headers(f),
        }
        for f in files
    ]

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        files_metadata=files_metadata,
        include_sequence_diagram=config.review.walkthrough_sequence_diagram,
    )
    system_content = append_language_instruction(
        system_content, config.review.prompt_output_language
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Generate the walkthrough for this PR."},
    ]


def build_conversation_prompt(
    question: str,
    diff_text: str,
    pr_title: str = "",
    pr_description: str = "",
    output_language: str | None = None,
) -> list[dict[str, str]]:
    """Build prompt messages for a conversational reply about a PR."""
    env = _get_template_env()
    template = env.get_template("conversation.jinja2")

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
    )
    system_content = append_language_instruction(system_content, output_language)

    user_content = f"## Diff\n\n```diff\n{diff_text}\n```\n\n## Question\n\n{question}"

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
