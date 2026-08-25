"""Trusted local company review-rule loading and prompt integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import mira.config as config_module
from mira.config import MiraConfig, ReviewConfig, load_config, set_global_defaults
from mira.core.company_rules import load_company_rules
from mira.core.passes import self_critique
from mira.exceptions import ConfigError
from mira.llm.prompts.review import build_review_prompt
from mira.models import FileChangeType, FileDiff, HunkInfo, ReviewComment, Severity


def _config_file(directory: Path, review_yaml: str) -> Path:
    path = directory / "mira.yaml"
    path.write_text(f"review:\n{review_yaml}", encoding="utf-8")
    return path


def _diff_file() -> FileDiff:
    return FileDiff(
        path="app.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(1, 1, 1, 1, "@@ -1 +1 @@\n-old\n+new")],
        added_lines=1,
        deleted_lines=1,
    )


class TestCompanyRuleLoading:
    def test_unconfigured_is_backward_compatible(self):
        assert load_company_rules(MiraConfig()) == []

    def test_single_relative_file_uses_config_directory_and_preserves_content(self, tmp_path):
        content = "# Security\n\nNever log tokens.\n"
        (tmp_path / "company-rules.md").write_bytes(content.encode("utf-8"))
        config = load_config(_config_file(tmp_path, '  rules_file: "company-rules.md"\n'))

        assert load_company_rules(config) == [
            {
                "title": "Company rules (company-rules.md)",
                "content": content,
                "source": "company-rules.md",
            }
        ]

    def test_multiple_files_preserve_configured_order(self, tmp_path):
        (tmp_path / "security.md").write_text("Security policy", encoding="utf-8")
        (tmp_path / "quality.txt").write_text("Quality policy", encoding="utf-8")
        config = load_config(
            _config_file(
                tmp_path,
                "  rules_files:\n    - security.md\n    - quality.txt\n",
            )
        )

        rules = load_company_rules(config)
        assert [rule["source"] for rule in rules] == ["security.md", "quality.txt"]
        assert [rule["content"] for rule in rules] == ["Security policy", "Quality policy"]

    def test_singular_then_list_and_duplicate_loaded_once(self, tmp_path):
        (tmp_path / "first.md").write_text("First", encoding="utf-8")
        (tmp_path / "second.md").write_text("Second", encoding="utf-8")
        config = load_config(
            _config_file(
                tmp_path,
                "  rules_file: first.md\n  rules_files:\n    - first.md\n    - second.md\n",
            )
        )

        assert [rule["content"] for rule in load_company_rules(config)] == ["First", "Second"]

    def test_deployment_defaults_keep_their_rules_directory(self, tmp_path):
        (tmp_path / "company.md").write_text("Deployment policy", encoding="utf-8")
        deployment_config = _config_file(tmp_path, "  rules_file: company.md\n")
        saved_defaults = config_module._global_defaults
        saved_dir = config_module._global_defaults_dir
        try:
            set_global_defaults(deployment_config)
            assert load_company_rules(load_config())[0]["content"] == "Deployment policy"
        finally:
            config_module._global_defaults = saved_defaults
            config_module._global_defaults_dir = saved_dir

    def test_missing_file_fails_without_exposing_absolute_path(self, tmp_path):
        missing = tmp_path / "secret-install" / "missing.md"
        config = MiraConfig(review=ReviewConfig(rules_file=str(missing)))

        with pytest.raises(ConfigError) as caught:
            load_company_rules(config)

        assert "missing.md" in str(caught.value)
        assert str(tmp_path) not in str(caught.value)

    def test_invalid_utf8_fails_clearly(self, tmp_path):
        path = tmp_path / "rules.md"
        path.write_bytes(b"\xff\xfe")
        config = MiraConfig(review=ReviewConfig(rules_file=str(path)))

        with pytest.raises(ConfigError, match="not valid UTF-8"):
            load_company_rules(config)

    def test_file_over_configured_size_fails(self, tmp_path):
        path = tmp_path / "rules.md"
        path.write_text("12345", encoding="utf-8")
        config = MiraConfig(review=ReviewConfig(rules_file=str(path), rules_max_file_size=4))

        with pytest.raises(ConfigError, match="too large.*limit 4 bytes"):
            load_company_rules(config)

    def test_empty_file_fails_clearly(self, tmp_path):
        path = tmp_path / "rules.md"
        path.write_text(" \n", encoding="utf-8")
        config = MiraConfig(review=ReviewConfig(rules_file=str(path)))

        with pytest.raises(ConfigError, match="is empty"):
            load_company_rules(config)

    def test_unreadable_file_fails_clearly(self, tmp_path, monkeypatch):
        path = tmp_path / "rules.md"
        path.write_text("policy", encoding="utf-8")
        config = MiraConfig(review=ReviewConfig(rules_file=str(path)))
        original_read_bytes = Path.read_bytes

        def deny_read(candidate: Path) -> bytes:
            if candidate == path:
                raise PermissionError("denied")
            return original_read_bytes(candidate)

        monkeypatch.setattr(Path, "read_bytes", deny_read)
        with pytest.raises(ConfigError, match="could not be read"):
            load_company_rules(config)

    @pytest.mark.parametrize(
        "review",
        [ReviewConfig(rules_file=None), ReviewConfig(rules_files=[])],
    )
    def test_empty_configuration_forms_are_noop(self, review):
        assert load_company_rules(MiraConfig(review=review)) == []

    @pytest.mark.parametrize(
        "kwargs",
        [{"rules_file": ""}, {"rules_files": ["ok.md", "  "]}],
    )
    def test_blank_configured_paths_are_rejected(self, kwargs):
        with pytest.raises(ValueError, match="must not be empty"):
            ReviewConfig(**kwargs)


class TestCompanyRulePrompts:
    def test_main_prompt_is_additive_and_company_policy_is_first(self):
        company = [
            {
                "title": "Company rules (company.md)",
                "content": "COMPANY POLICY",
                "source": "company.md",
            }
        ]
        messages = build_review_prompt(
            [_diff_file()],
            MiraConfig(),
            company_rules=company,
            team_conventions="REPOSITORY CONVENTIONS",
            custom_rules=[
                {"title": "Repository", "content": "REPOSITORY CUSTOM"},
                {"title": "Global", "content": "GLOBAL DASHBOARD"},
            ],
            learned_rules=["LEARNED PREFERENCE"],
        )

        prompt = messages[0]["content"]
        ordered = [
            "COMPANY POLICY",
            "REPOSITORY CONVENTIONS",
            "REPOSITORY CUSTOM",
            "GLOBAL DASHBOARD",
            "LEARNED PREFERENCE",
        ]
        assert all(item in prompt for item in ordered)
        assert [prompt.index(item) for item in ordered] == sorted(prompt.index(i) for i in ordered)
        assert "Source: company.md" in prompt

    @pytest.mark.asyncio
    async def test_self_critique_receives_company_and_other_rules(self):
        critic = AsyncMock()
        critic.complete_with_tools.return_value = json.dumps(
            {"verdicts": [{"index": 0, "evidence": "proven", "reason": "policy"}]}
        )
        comment = ReviewComment(
            path="app.py",
            line=1,
            end_line=None,
            severity=Severity.WARNING,
            category="security",
            title="Token leak",
            body="Do not log this value",
            confidence=0.95,
        )

        kept = await self_critique(
            AsyncMock(),
            [comment],
            company_rules=[{"content": "COMPANY POLICY", "source": "company.md"}],
            custom_rules=[{"title": "Repository", "content": "REPOSITORY CUSTOM"}],
            learned_rules=["LEARNED PREFERENCE"],
            indexing_llm=critic,
        )

        prompt = critic.complete_with_tools.await_args.kwargs["messages"][0]["content"]
        assert kept == [comment]
        assert "Company review rules (highest priority)" in prompt
        assert prompt.index("COMPANY POLICY") < prompt.index("REPOSITORY CUSTOM")
        assert prompt.index("REPOSITORY CUSTOM") < prompt.index("LEARNED PREFERENCE")
