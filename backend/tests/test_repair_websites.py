"""Tests for scoped website repair (no browser needed)."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from app.workers.tasks import _select_repair_website_leads


def _run(coro):
    return asyncio.run(coro)


class SelectRepairWebsiteLeadsTests(unittest.TestCase):
    LEADS = [{"id": "l1"}, {"id": "l2"}, {"id": "l3"}]

    def test_no_selection_returns_all(self):
        self.assertEqual(_select_repair_website_leads(self.LEADS, None), self.LEADS)
        self.assertEqual(_select_repair_website_leads(self.LEADS, []), self.LEADS)

    def test_selection_narrows(self):
        out = _select_repair_website_leads(self.LEADS, ["l2", "l3", "ghost", "  "])
        self.assertEqual([l["id"] for l in out], ["l2", "l3"])


class RepairWebsitesEndpointTests(unittest.TestCase):
    def test_passes_lead_ids_to_task(self):
        from app.routes import scrape as routes

        task = MagicMock()
        task.delay.return_value = MagicMock(id="job1")
        with (
            patch.object(routes, "_ensure_campaign_can_run", return_value=None),
            patch.object(routes, "repair_campaign_websites", task),
            patch.object(routes, "update_document"),
        ):
            out = _run(
                routes.repair_campaign_websites_endpoint(
                    "c1", reset_status=True, lead_ids="l1, l2"
                )
            )
        task.delay.assert_called_once_with("c1", True, ["l1", "l2"])
        self.assertEqual(out.job_id, "job1")

    def test_no_ids_passes_empty(self):
        from app.routes import scrape as routes

        task = MagicMock()
        task.delay.return_value = MagicMock(id="job1")
        with (
            patch.object(routes, "_ensure_campaign_can_run", return_value=None),
            patch.object(routes, "repair_campaign_websites", task),
            patch.object(routes, "update_document"),
        ):
            _run(
                routes.repair_campaign_websites_endpoint(
                    "c1", reset_status=True, lead_ids=None
                )
            )
        task.delay.assert_called_once_with("c1", True, [])


if __name__ == "__main__":
    unittest.main()
