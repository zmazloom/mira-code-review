"""Load trusted installation-local company review rules."""

from __future__ import annotations

from pathlib import Path

from mira.config import MiraConfig
from mira.exceptions import ConfigError


def _display_name(configured_path: str) -> str:
    """Return safe provenance without exposing installation directory paths."""
    return Path(configured_path).name or "company rules file"


def _resolve_path(config: MiraConfig, configured_path: str, field: str) -> Path:
    path = Path(configured_path).expanduser()
    if path.is_absolute():
        return path
    base_dir = config.review._rules_base_dirs.get(field, Path.cwd())
    return base_dir / path


def load_company_rules(config: MiraConfig) -> list[dict[str, str]]:
    """Read configured UTF-8 policy files as ordered, unparsed rule blocks.

    ``rules_file`` is loaded first, followed by ``rules_files``. Duplicate
    resolved paths are loaded once. Any configured-file failure is fatal so a
    review can never silently run without expected company policy.
    """
    configured: list[tuple[str, str]] = []
    if config.review.rules_file is not None:
        configured.append((config.review.rules_file, "rules_file"))
    configured.extend((item, "rules_files") for item in config.review.rules_files)

    rules: list[dict[str, str]] = []
    seen: set[Path] = set()
    max_size = config.review.rules_max_file_size

    for configured_path, field in configured:
        path = _resolve_path(config, configured_path, field)
        display = _display_name(configured_path)
        try:
            identity = path.resolve(strict=False)
        except OSError:
            identity = path.absolute()
        if identity in seen:
            continue
        seen.add(identity)

        try:
            if not path.is_file():
                raise ConfigError(f"Company rules file '{display}' was not found or is not a file")
            size = path.stat().st_size
            if size > max_size:
                raise ConfigError(
                    f"Company rules file '{display}' is too large "
                    f"({size} bytes; limit {max_size} bytes)"
                )
            raw = path.read_bytes()
        except ConfigError:
            raise
        except OSError as exc:
            raise ConfigError(f"Company rules file '{display}' could not be read") from exc

        if len(raw) > max_size:
            raise ConfigError(
                f"Company rules file '{display}' is too large "
                f"({len(raw)} bytes; limit {max_size} bytes)"
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Company rules file '{display}' is not valid UTF-8") from exc
        if not content.strip():
            raise ConfigError(f"Company rules file '{display}' is empty")

        rules.append(
            {
                "title": f"Company rules ({display})",
                "content": content,
                "source": display,
            }
        )

    return rules
