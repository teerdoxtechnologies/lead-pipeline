"""Tests for single-lead delete (no Firestore, network, or git access)."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routes.leads import LEAD_DELETE_CONFIRMATION, delete_lead
from app.routes.scrape import _delete_lead_doc, _delete_outreach_draft


def _run(coro):
    return asyncio.run(coro)


class DeleteOutreachDraftTests(unittest.TestCase):
    def test_fires_all_removers(self):
        with (
            patch("app.routes.scrape._delete_gmail_draft", return_value=True) as g,
            patch(
                "app.routes.scrape._delete_calendar_event_for_draft",
                return_value=True,
            ) as c,
            patch("app.routes.scrape._archive_notion_page") as archive,
            patch("app.routes.scrape.delete_document") as delete,
        ):
            out = _delete_outreach_draft({"id": "d1"})
        g.assert_called_once()
        c.assert_called_once()
        archive.assert_called_once()
        delete.assert_called_once_with("email_drafts", "d1")
        self.assertEqual(out, {"gmail_deleted": True, "calendar_deleted": True})

    def test_missing_ids_skipped(self):
        with (
            patch(
                "app.routes.scrape._delete_gmail_draft", return_value=False
            ) as g,
            patch(
                "app.routes.scrape._delete_calendar_event_for_draft",
                return_value=False,
            ) as c,
            patch("app.routes.scrape._archive_notion_page") as archive,
            patch("app.routes.scrape.delete_document") as delete,
        ):
            out = _delete_outreach_draft({"id": "d1"})
        g.assert_called_once()
        c.assert_called_once()
        archive.assert_called_once()
        delete.assert_called_once_with("email_drafts", "d1")
        self.assertEqual(out, {"gmail_deleted": False, "calendar_deleted": False})


class DeleteLeadDocTests(unittest.TestCase):
    def _docs(self, with_website=False):
        lead = {
            "id": "l1",
            "business_name": "Biz",
            "campaign_id": "c1",
            "has_website": with_website,
        }
        drafts = [{"id": "d1"}, {"id": "d2"}]
        reports = {"audit_reports": [{"id": "r1"}], "no_website_reports": []}
        return lead, drafts, reports

    def _query_all(self, drafts, reports):
        def fake(collection, filters=None):
            if collection == "email_drafts":
                return drafts
            return reports.get(collection, [])

        return fake

    def test_full_purge_order_and_summary(self):
        lead, drafts, reports = self._docs()
        calls = []
        with (
            patch("app.routes.scrape.get_document") as get,
            patch(
                "app.routes.scrape._query_all",
                side_effect=self._query_all(drafts, reports),
            ),
            patch(
                "app.routes.scrape._delete_outreach_draft",
                side_effect=lambda d: calls.append(("draft", d["id"]))
                or {"gmail_deleted": True, "calendar_deleted": False},
            ),
            patch(
                "app.routes.scrape._delete_campaign_cloudinary_media"
            ) as media,
            patch(
                "app.routes.scrape._delete_campaign_static_paths"
            ) as static,
            patch(
                "app.routes.scrape._delete_campaign_export_bundles"
            ) as exports,
            patch("app.routes.scrape._archive_notion_page") as archive,
            patch(
                "app.routes.scrape.delete_document",
                side_effect=lambda coll, doc_id: calls.append(("doc", coll, doc_id)),
            ),
            patch("app.routes.scrape.update_document") as update,
        ):
            get.side_effect = lambda coll, doc_id: (
                {"id": "c1"} if coll == "campaigns" else lead
            )
            out = _delete_lead_doc("l1")
        self.assertEqual(out["lead_id"], "l1")
        self.assertEqual(out["business_name"], "Biz")
        self.assertEqual(out["reports"], 1)
        self.assertEqual(out["drafts"], 2)
        self.assertEqual(out["gmail_deleted"], 2)
        self.assertEqual(out["calendar_deleted"], 0)
        media.assert_called_once()
        static.assert_called_once()
        exports.assert_called_once_with("c1")
        # Lead doc deleted after its children.
        doc_calls = [c for c in calls if c[0] == "doc"]
        self.assertEqual(doc_calls[-1], ("doc", "leads", "l1"))
        self.assertIn(("doc", "audit_reports", "r1"), doc_calls)
        draft_calls = [c for c in calls if c[0] == "draft"]
        self.assertEqual(draft_calls, [("draft", "d1"), ("draft", "d2")])
        # Stats decremented for a no-website lead.
        update.assert_called_once()
        args = update.call_args[0]
        self.assertEqual(args[0], "campaigns")
        self.assertEqual(args[1], "c1")
        self.assertIn("stats.total", args[2])
        self.assertIn("stats.maps.businesses_persisted", args[2])
        self.assertIn("stats.maps.missing_website", args[2])
        self.assertNotIn("stats.maps.with_website", args[2])
        archive_calls = [c[0][0].get("id") for c in archive.call_args_list]
        for expected in ("r1", "l1"):
            self.assertIn(expected, archive_calls)

    def test_missing_lead_is_404(self):
        with patch("app.routes.scrape.get_document", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                _delete_lead_doc("missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_campaign_skips_stat_decrement(self):
        lead, drafts, reports = self._docs()
        with (
            patch("app.routes.scrape.get_document") as get,
            patch(
                "app.routes.scrape._query_all",
                side_effect=self._query_all(drafts, reports),
            ),
            patch("app.routes.scrape._delete_outreach_draft") as helper,
            patch("app.routes.scrape._delete_campaign_cloudinary_media"),
            patch("app.routes.scrape._delete_campaign_static_paths"),
            patch("app.routes.scrape._delete_campaign_export_bundles"),
            patch("app.routes.scrape._archive_notion_page"),
            patch("app.routes.scrape.delete_document"),
            patch("app.routes.scrape.update_document") as update,
        ):
            helper.return_value = {"gmail_deleted": False, "calendar_deleted": False}
            get.side_effect = lambda coll, doc_id: (
                None if coll == "campaigns" else lead
            )
            out = _delete_lead_doc("l1")
        update.assert_not_called()
        self.assertEqual(out["drafts"], 2)


class DeleteLeadEndpointTests(unittest.TestCase):
    def test_bad_confirm_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _run(delete_lead("l1", confirm="nope"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_good_confirm_delegates(self):
        sentinel = {"lead_id": "l1"}
        with patch(
            "app.routes.scrape._delete_lead_doc", return_value=sentinel
        ) as helper:
            out = _run(delete_lead("l1", confirm=LEAD_DELETE_CONFIRMATION))
        helper.assert_called_once_with("l1")
        self.assertIs(out, sentinel)


class DeleteCalendarEventTests(unittest.TestCase):
    def test_deletes_by_id(self):
        from app.services import google_calendar_service as cal

        service = MagicMock()
        with (
            patch.object(cal, "build", return_value=service),
            patch.object(
                cal, "get_google_credentials", return_value=MagicMock()
            ),
        ):
            self.assertTrue(cal.delete_calendar_event("ev1"))
        service.events.return_value.delete.assert_called_once()

    def test_empty_id_skips_without_client(self):
        from app.services import google_calendar_service as cal

        with patch.object(cal, "build") as build:
            self.assertFalse(cal.delete_calendar_event(""))
        build.assert_not_called()

    def test_failure_returns_false(self):
        from app.services import google_calendar_service as cal

        with patch.object(cal, "build", side_effect=RuntimeError("down")):
            self.assertFalse(cal.delete_calendar_event("ev1"))


if __name__ == "__main__":
    unittest.main()
