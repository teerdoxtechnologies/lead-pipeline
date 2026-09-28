"""Tests for bulk website-build flagging + cleanup stat adjustment (no Firestore)."""
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routes.websites import _apply_cleanup_stat_adjustment, mark_campaign_websites
from app.schemas import WebsiteMarkRequest


def _run(coro):
    return asyncio.run(coro)


class MarkCampaignWebsitesTests(unittest.TestCase):
    def test_missing_campaign_is_404(self):
        with patch("app.routes.websites.get_document", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    mark_campaign_websites(
                        "missing", WebsiteMarkRequest(lead_ids=["l1"])
                    )
                )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_empty_selection_is_400(self):
        with patch(
            "app.routes.websites.get_document", return_value={"id": "c1"}
        ):
            with self.assertRaises(HTTPException) as ctx:
                _run(mark_campaign_websites("c1", WebsiteMarkRequest(lead_ids=[])))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_marks_owned_and_skips_foreign(self):
        updated = []
        with (
            patch(
                "app.routes.websites.get_document", return_value={"id": "c1"}
            ),
            patch(
                "app.routes.websites.query_collection",
                return_value=[{"id": "l1"}, {"id": "l2"}],
            ),
            patch(
                "app.routes.websites.update_document",
                side_effect=lambda coll, doc_id, data: updated.append(
                    (coll, doc_id, data)
                ),
            ),
        ):
            out = _run(
                mark_campaign_websites(
                    "c1",
                    WebsiteMarkRequest(lead_ids=["l1", "l2", "stale", "  "]),
                )
            )
        self.assertEqual(out["campaign_id"], "c1")
        self.assertTrue(out["needs_website"])
        self.assertEqual(out["updated"], 2)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(
            updated,
            [
                ("leads", "l1", {"needs_website": True}),
                ("leads", "l2", {"needs_website": True}),
            ],
        )

    def test_unmark(self):
        updated = []
        with (
            patch(
                "app.routes.websites.get_document", return_value={"id": "c1"}
            ),
            patch(
                "app.routes.websites.query_collection",
                return_value=[{"id": "l1"}],
            ),
            patch(
                "app.routes.websites.update_document",
                side_effect=lambda coll, doc_id, data: updated.append(
                    (coll, doc_id, data)
                ),
            ),
        ):
            out = _run(
                mark_campaign_websites(
                    "c1",
                    WebsiteMarkRequest(lead_ids=["l1"], needs_website=False),
                )
            )
        self.assertFalse(out["needs_website"])
        self.assertEqual(out["updated"], 1)
        self.assertEqual(updated, [("leads", "l1", {"needs_website": False})])


class ApplyCleanupStatAdjustmentTests(unittest.TestCase):
    def test_decrements_buckets(self):
        campaign = {
            "id": "c1",
            "stats": {
                "total": 10,
                "maps": {"with_website": 4, "missing_website": 6},
            },
        }
        items = [
            {"lead_id": "l1", "removed": True, "has_website": True},
            {"lead_id": "l2", "removed": True, "has_website": False},
            {"lead_id": "l3", "removed": False, "has_website": False},
        ]
        with (
            patch(
                "app.routes.websites.get_document", return_value=campaign
            ),
            patch("app.routes.websites.update_document") as update,
        ):
            _apply_cleanup_stat_adjustment("c1", items)
        update.assert_called_once()
        args = update.call_args[0]
        self.assertEqual(args[0], "campaigns")
        self.assertEqual(args[1], "c1")
        self.assertEqual(
            args[2],
            {
                "stats.total": 8,
                "stats.maps.with_website": 3,
                "stats.maps.missing_website": 5,
            },
        )

    def test_clamps_at_zero(self):
        campaign = {
            "id": "c1",
            "stats": {"total": 1, "maps": {"with_website": 0, "missing_website": 0}},
        }
        items = [
            {"lead_id": "l1", "removed": True, "has_website": False},
            {"lead_id": "l2", "removed": True, "has_website": False},
        ]
        with (
            patch(
                "app.routes.websites.get_document", return_value=campaign
            ),
            patch("app.routes.websites.update_document") as update,
        ):
            _apply_cleanup_stat_adjustment("c1", items)
        args = update.call_args[0][2]
        self.assertEqual(
            args,
            {
                "stats.total": 0,
                "stats.maps.with_website": 0,
                "stats.maps.missing_website": 0,
            },
        )

    def test_no_removals_no_write(self):
        with (
            patch("app.routes.websites.get_document") as get,
            patch("app.routes.websites.update_document") as update,
        ):
            _apply_cleanup_stat_adjustment(
                "c1", [{"lead_id": "l1", "removed": False}]
            )
        get.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
