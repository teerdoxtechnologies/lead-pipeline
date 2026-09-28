"""Tests for website takedown (no Firestore, git, or network)."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


HOSTED = {
    "id": "l1",
    "business_name": "Biz",
    "generated_website_url": "https://example.com/biz/",
    "generated_website_path": "/repo/sites/biz",
    "generated_website_slug": "biz",
}
PREVIEW_ONLY = {
    "id": "l2",
    "business_name": "Other",
    "generated_website_slug": "other",
}


class UnpublishTaskTests(unittest.TestCase):
    def _task(self):
        from app.workers.tasks import unpublish_campaign_static_websites

        return unpublish_campaign_static_websites

    def test_takes_down_hosted_skips_rest(self):
        task = self._task()
        notion = MagicMock()
        notion.sync_lead.return_value = "page1"
        with (
            patch(
                "app.workers.tasks.get_document",
                return_value={"id": "c1", "notion_page_id": None},
            ),
            patch(
                "app.workers.tasks.query_collection",
                return_value=[HOSTED, PREVIEW_ONLY],
            ),
            patch(
                "app.services.static_website_generator.remove_generated_website_path",
                return_value=True,
            ),
            patch(
                "app.services.static_website_generator.git_commit_and_push_static_paths",
                return_value={"pushed": True, "committed": True},
            ) as git,
            patch("app.workers.tasks.update_document") as update,
            patch("app.workers.tasks.get_notion_sync", return_value=notion),
        ):
            out = task.run("c1")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["unpublished"], 1)
        self.assertEqual(out["skipped"], 1)
        git.assert_called_once()
        cleared = [
            call for call in update.call_args_list if call[0][1] == "l1"
        ]
        self.assertTrue(cleared)
        payload = cleared[0][0][2]
        self.assertIsNone(payload["generated_website_url"])
        self.assertIsNone(payload["generated_website_path"])
        self.assertEqual(payload["generated_website_status"], "unpublished")

    def test_missing_campaign_fails(self):
        task = self._task()
        with patch("app.workers.tasks.get_document", return_value=None):
            out = task.run("c1")
        self.assertEqual(out["status"], "failed")

    def test_git_failure_marks_failed(self):
        task = self._task()
        notion = MagicMock()
        notion.sync_lead.return_value = None
        with (
            patch(
                "app.workers.tasks.get_document",
                return_value={"id": "c1", "notion_page_id": None},
            ),
            patch(
                "app.workers.tasks.query_collection", return_value=[HOSTED]
            ),
            patch(
                "app.services.static_website_generator.remove_generated_website_path",
                return_value=True,
            ),
            patch(
                "app.services.static_website_generator.git_commit_and_push_static_paths",
                return_value={"pushed": False, "error": "nope"},
            ),
            patch("app.workers.tasks.update_document"),
            patch("app.workers.tasks.get_notion_sync", return_value=notion),
        ):
            out = task.run("c1")
        self.assertEqual(out["status"], "failed")


class UnpublishEndpointTests(unittest.TestCase):
    def test_missing_campaign_is_404(self):
        from app.routes import websites as routes

        with patch.object(routes, "get_document", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                _run(routes.unpublish_campaign_websites("c1", MagicMock()))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delegates_to_task(self):
        from app.routes import websites as routes

        task = MagicMock()
        task.delay.return_value = MagicMock(id="job9")
        with (
            patch.object(routes, "get_document", return_value={"id": "c1"}),
            patch.object(routes, "unpublish_campaign_static_websites", task),
        ):
            out = _run(
                routes.unpublish_campaign_websites("c1", MagicMock(lead_ids=["l1"]))
            )
        task.delay.assert_called_once()
        self.assertEqual(out.job_id, "job9")


if __name__ == "__main__":
    unittest.main()
