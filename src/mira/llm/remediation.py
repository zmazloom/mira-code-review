"""Shared, deterministic fallback for missing per-finding AI remediation prompts."""

from __future__ import annotations

from dataclasses import replace

from mira.models import ReviewComment

_SHARED_GUIDANCE = (
    "Preserve existing behavior and public contracts. Follow established repository conventions "
    "and company rules. Keep the change limited to this finding, and add or update relevant tests."
)


def ensure_agent_prompt(comment: ReviewComment) -> ReviewComment:
    """Keep an LLM-authored fix prompt or construct a finding-specific fallback."""
    prompt = (comment.agent_prompt or "").strip()
    if not prompt:
        correction = comment.suggestion or (
            f"Implement the correction needed to resolve {comment.title!r} "
            "while preserving the component's current contract."
        )
        prompt = (
            f"Update `{comment.path}` near line {comment.line} to address this finding: "
            f"{comment.title}.\n\n"
            f"Problem: {comment.body}\n\n"
            f"Expected correction: {correction}"
        )
    if _SHARED_GUIDANCE not in prompt:
        prompt = f"{prompt}\n\n{_SHARED_GUIDANCE}"
    return comment if comment.agent_prompt == prompt else replace(comment, agent_prompt=prompt)
