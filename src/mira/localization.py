"""Centralized review-output language instructions and render-time labels."""

from __future__ import annotations

OutputLanguage = str

_PERSIAN_LANGUAGE_INSTRUCTION = (
    "Write all human-readable review content in Persian (Farsi).\n"
    "This includes finding titles, rationales, explanations, summaries, walkthrough "
    "text, recommendations, and suggested fixes expressed in prose.\n"
    "Keep source-code identifiers, class names, method names, variable names, "
    "annotations, package names, file paths, exception names, framework and library "
    "names, code snippets, configuration keys, API names, and rule identifiers unchanged.\n"
    "Preserve the existing output schema and structure."
)

_ENGLISH_LANGUAGE_INSTRUCTION = (
    "Write all human-readable review content in English.\n"
    "This includes finding titles, rationales, explanations, summaries, walkthrough "
    "text, recommendations, and suggested fixes expressed in prose.\n"
    "Keep source-code identifiers, class names, method names, variable names, "
    "annotations, package names, file paths, exception names, framework and library "
    "names, code snippets, configuration keys, API names, and rule identifiers unchanged.\n"
    "Preserve the existing output schema and structure."
)

_LABELS: dict[str, dict[OutputLanguage, str]] = {
    "summary": {"en": "Summary", "fa": "خلاصه"},
    "findings": {"en": "Findings", "fa": "یافته‌ها"},
    "severity": {"en": "Severity", "fa": "شدت"},
    "file": {"en": "File", "fa": "فایل"},
    "title": {"en": "Title", "fa": "عنوان"},
    "rationale": {"en": "Rationale", "fa": "دلیل"},
    "suggestion": {"en": "Suggestion", "fa": "پیشنهاد"},
    "confidence": {"en": "Confidence", "fa": "اطمینان"},
}

_SEVERITY_LABELS: dict[str, dict[OutputLanguage, str]] = {
    "blocker": {"en": "blocker", "fa": "بحرانی"},
    "warning": {"en": "warning", "fa": "هشدار"},
    "suggestion": {"en": "suggestion", "fa": "پیشنهاد"},
    "nitpick": {"en": "nitpick", "fa": "نکته جزئی"},
    "critical": {"en": "critical", "fa": "بحرانی"},
    "high": {"en": "high", "fa": "بحرانی"},
    "moderate": {"en": "moderate", "fa": "هشدار"},
    "low": {"en": "low", "fa": "پیشنهاد"},
    "unknown": {"en": "unknown", "fa": "نامشخص"},
}


def language_instruction(output_language: OutputLanguage | None) -> str:
    """Return the instruction for non-default review output.

    ``None`` means the setting was omitted and intentionally returns an empty
    string so the historical English prompts remain byte-for-byte unchanged.
    """
    if output_language is None:
        return ""
    if is_persian(output_language):
        return _PERSIAN_LANGUAGE_INSTRUCTION
    if output_language == "en":
        return _ENGLISH_LANGUAGE_INSTRUCTION
    return (
        "Write all human-readable review content in the language identified by "
        f"BCP 47 code `{output_language}`.\n"
        "This includes finding titles, rationales, explanations, summaries, walkthrough "
        "text, recommendations, and suggested fixes expressed in prose.\n"
        "Keep source-code identifiers, class names, method names, variable names, "
        "annotations, package names, file paths, exception names, framework and library "
        "names, code snippets, configuration keys, API names, and rule identifiers unchanged.\n"
        "Preserve the existing output schema and structure."
    )


def append_language_instruction(content: str, output_language: OutputLanguage | None) -> str:
    """Append the centralized language instruction without duplicating it."""
    instruction = language_instruction(output_language)
    if not instruction or instruction in content:
        return content
    return f"{content}\n\n## Output Language\n\n{instruction}"


def label(name: str, output_language: OutputLanguage | str = "en") -> str:
    """Return a localized human-readable review label."""
    values = _LABELS[name]
    return values["fa"] if is_persian(output_language) else values["en"]


def severity_label(value: str, output_language: OutputLanguage | str = "en") -> str:
    """Localize a severity for display while leaving its internal value unchanged."""
    normalized = value.strip().lower()
    values = _SEVERITY_LABELS.get(normalized)
    if values is None:
        return value
    return values["fa"] if is_persian(output_language) else values["en"]


def is_persian(output_language: OutputLanguage | None) -> bool:
    return bool(output_language and output_language.split("-", 1)[0] == "fa")
