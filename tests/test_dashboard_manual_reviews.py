"""Background review job behavior, with no external API requests."""

import asyncio
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from mira.dashboard import manual_reviews as reviews


def request(user_id=1):
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=user_id)))


class DashboardManualReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reviews._jobs.clear()
        self.environment = patch.dict(os.environ, {
            "GITHUB_TOKEN": "test-token", "GITLAB_TOKEN": "test-token",
            "MIRA_GITLAB_API_URL": "https://gitlab.com/api/v4", "MIRA_CONFIG": "",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        reviews._jobs.clear()

    def test_failure_message_redacts_secrets_and_ignores_logs(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "private-model-value"}):
            message = reviews._failure_message(
                "Provider log with private-model-value\nError: Missing key private-model-value ghp_abc123\n")
        self.assertNotIn("private-model-value", message)
        self.assertNotIn("ghp_abc123", message)
        self.assertNotIn("Provider log", message)
        self.assertIn("Missing key", message)

    async def test_cli_error_is_reported_in_job(self):
        job = reviews.Job(owner=1, result=reviews.ReviewStatus(
            id="error", platform="github", url="https://github.com/owner/repo/pull/1"))
        with patch.object(reviews.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="Error: Model API key is missing.\n")):
            await reviews._run(job)
        self.assertEqual(job.result.message, "Review failed: Model API key is missing.")

    async def test_invalid_links(self):
        for platform, url in [
            ("github", "https://gitlab.com/group/project/-/merge_requests/1"),
            ("github", "owner/repo#1"),
            ("github", "https://github.com/owner/repo/pull/0"),
            ("gitlab", "https://other.example/group/project/-/merge_requests/1"),
        ]:
            with self.subTest(url=url), patch.object(reviews, "_run", new_callable=AsyncMock) as run:
                with self.assertRaises(HTTPException) as error:
                    await reviews.start_review(reviews.ReviewRequest(platform=platform, url=url), request())
                self.assertEqual(error.exception.status_code, 422)
                run.assert_not_called()

    async def test_missing_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "MIRA_GIT_TOKEN": ""}):
            with self.assertRaises(HTTPException) as error:
                await reviews.start_review(reviews.ReviewRequest(
                    platform="github", url="https://github.com/owner/repo/pull/1"), request())
            self.assertIn("Configure", error.exception.detail)

    async def test_owner_and_duplicate(self):
        with patch.object(reviews, "_run", new_callable=AsyncMock):
            body = reviews.ReviewRequest(platform="github", url="https://github.com/owner/repo/pull/1")
            result = await reviews.start_review(body, request())
            self.assertEqual(result.status, "reviewing")
            self.assertEqual((await reviews.review_status(result.id, request())).id, result.id)
            with self.assertRaises(HTTPException) as error:
                await reviews.review_status(result.id, request(2))
            self.assertEqual(error.exception.status_code, 404)
            with self.assertRaises(HTTPException) as error:
                await reviews.start_review(body, request())
            self.assertEqual(error.exception.status_code, 409)
            await asyncio.sleep(0)

    async def test_completion(self):
        for code, output, status in [
            (0, '{"posted": true}', "done"), (1, '{"posted": true}', "done"),
            (1, '', "failed"), (0, '{"posted": false}', "failed"),
            (0, '{"status": "skipped", "posted": false}', "skipped"),
        ]:
            with self.subTest(code=code, output=output):
                job = reviews.Job(owner=1, result=reviews.ReviewStatus(
                    id="job", platform="github", url="https://github.com/owner/repo/pull/1"))
                with patch.object(reviews.subprocess, "run", return_value=SimpleNamespace(
                    returncode=code, stdout=output)) as run:
                    await reviews._run(job)
                self.assertEqual(job.result.status, status)
                self.assertIsNotNone(job.finished_at)
                self.assertEqual(run.call_args.args[0][3], "review-notif")
                self.assertEqual(run.call_args.args[0][-2:], ["--output", "json"])

    async def test_background_command_is_accepted_by_real_cli(self):
        from click.testing import CliRunner
        from mira.cli import main

        outcome = SimpleNamespace(result=SimpleNamespace(comments=[], output_language="en"))
        execute = AsyncMock(return_value=outcome)

        def run_cli(command, **kwargs):
            result = CliRunner().invoke(main, command[3:])
            self.assertEqual(result.exit_code, 0, result.output)
            return SimpleNamespace(returncode=result.exit_code, stdout=result.output)

        job = reviews.Job(owner=1, result=reviews.ReviewStatus(
            id="cli", platform="github", url="https://github.com/owner/repo/pull/1", model="company-model"))
        with (
            patch("mira.cli.create_manual_provider"),
            patch("mira.manual_review.NotifReviewCommand.execute_if_matching", execute),
            patch("mira.cli._format_manual_json", return_value='{"posted": true}'),
            patch.object(reviews.subprocess, "run", side_effect=run_cli),
        ):
            await reviews._run(job)
        execute.assert_awaited_once()
        self.assertEqual(job.result.status, "done")

    async def test_title_gate_checks_metadata_before_reviewing(self):
        from unittest.mock import MagicMock

        from mira.config import MiraConfig
        from mira.manual_review import NotifReviewCommand, parse_pull_request_reference

        reference = parse_pull_request_reference("https://github.com/owner/repo/pull/1")
        for title, matches in [
            ("Notif MR: Fix delivery", True), ("notif mr fix delivery", True),
            ("NOTIF MR", True), ("nOtIf Mr: fix", True),
            ("Fix delivery", False), ("Fix Notif MR", False), ("", False),
        ]:
            with self.subTest(title=title):
                provider = MagicMock()
                provider.get_pr_info = AsyncMock(return_value=SimpleNamespace(title=title))
                provider.get_pr_diff = AsyncMock()
                provider.post_review = AsyncMock()
                command = NotifReviewCommand(config=MiraConfig(), provider=provider, post=True)
                outcome = object()
                with (
                    patch.object(command, "execute", AsyncMock(return_value=outcome)) as execute,
                    patch("mira.manual_review.create_llm") as create,
                ):
                    result = await command.execute_if_matching(reference)
                provider.get_pr_info.assert_awaited_once_with(reference.url)
                if matches:
                    execute.assert_awaited_once_with(reference)
                    self.assertIs(result, outcome)
                else:
                    execute.assert_not_awaited()
                    self.assertIsNone(result)
                create.assert_not_called()
                provider.get_pr_diff.assert_not_awaited()
                provider.post_review.assert_not_awaited()

    async def test_new_cli_skips_without_llm_or_posting(self):
        from unittest.mock import MagicMock

        from click.testing import CliRunner
        from mira.cli import main
        from mira.config import MiraConfig

        for url in ("https://github.com/owner/repo/pull/1",
                    "https://gitlab.com/group/project/-/merge_requests/1"):
            provider = MagicMock()
            provider.get_pr_info = AsyncMock(return_value=SimpleNamespace(title="Regular request"))
            with (
                patch("mira.cli.create_manual_provider", return_value=provider),
                patch("mira.cli.load_config", return_value=MiraConfig()),
                patch("mira.manual_review.create_llm") as create,
                patch("mira.manual_review.ManualReviewCommand.execute", AsyncMock()) as execute,
            ):
                result = await asyncio.to_thread(CliRunner().invoke, main, [
                    "review-notif", url, "--output", "json",
                ])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn('"status": "skipped"', result.output)
            self.assertIn('"posted": false', result.output)
            create.assert_not_called()
            execute.assert_not_awaited()

    async def test_model_options_include_company_config_and_catalog(self):
        from mira.config import MiraConfig

        config = MiraConfig()
        config.llm.model = "company-default"
        with (
            patch.object(reviews, "load_config", return_value=config),
            patch.object(reviews, "llm_config_for", return_value=config.llm),
            patch("mira.dashboard.model_catalog.fetch_catalog", AsyncMock(return_value=[
                {"value": "company-other", "label": "Company other"},
            ])),
        ):
            result = await reviews.review_models(request())
        self.assertEqual(result["default"], "company-default")
        self.assertEqual({item["value"] for item in result["options"]}, {
            "company-default", "company-other",
        })

    async def test_timeout(self):
        job = reviews.Job(owner=1, result=reviews.ReviewStatus(
            id="timeout", platform="gitlab", url="https://gitlab.com/group/repo/-/merge_requests/1"))
        with patch.object(reviews.subprocess, "run", side_effect=subprocess.TimeoutExpired("mira", 3600)):
            await reviews._run(job)
        self.assertEqual(job.result.status, "failed")
        self.assertIn("timed out", job.result.message)
        self.assertIsNotNone(job.finished_at)

    async def test_selected_model_overrides_saved_models_for_all_passes(self):
        from unittest.mock import MagicMock

        from mira.config import MiraConfig
        from mira.manual_review import ManualReviewCommand, parse_pull_request_reference
        from mira.models import PRInfo, ReviewResult

        url = "https://github.com/owner/repo/pull/1"
        engine = MagicMock()
        engine.review_pr = AsyncMock(return_value=ReviewResult())
        engine._pr_info = PRInfo(title="Test", description="", base_branch="main",
            head_branch="feature", url=url, number=1, owner="owner", repo="repo")
        config = MiraConfig()
        saved = config.llm.model_copy(update={"model": "saved-model"})
        with (
            patch("mira.manual_review.llm_config_for", return_value=saved),
            patch("mira.manual_review.create_llm") as create,
            patch("mira.manual_review.ReviewEngine", return_value=engine),
        ):
            await ManualReviewCommand(config=config, provider=MagicMock(),
                model_override="company-selected").execute(parse_pull_request_reference(url))
        self.assertEqual(len(create.call_args_list), 3)
        self.assertTrue(all(call.args[0].model == "company-selected" for call in create.call_args_list))

    async def test_api_authentication(self):
        from fastapi.testclient import TestClient
        from mira.dashboard.api import app

        with TestClient(app) as client:
            response = client.post("/api/manual-reviews", json={
                "platform": "github", "url": "https://github.com/owner/repo/pull/1"})
        self.assertEqual(response.status_code, 401)
