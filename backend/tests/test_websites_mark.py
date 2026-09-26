"""Tests for bulk website-build flagging (no Firestore access)."""
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routes.websites import mark_campaign_websites
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


if __name__ == "__main__":
    unittest.main()
