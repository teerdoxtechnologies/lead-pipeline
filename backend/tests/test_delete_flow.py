"""Tests for the tombstone-first campaign delete flow.

No Firestore access: these cover the pure/read-path pieces (hiding
tombstones, start guards, reconcile selection).
"""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routes.scrape import (
    _ensure_campaign_can_run,
    _list_campaign_docs,
    _stale_deleting_ids,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

LIST_DOCS = [
    {"id": "live", "status": "scraped"},
    {"id": "arch", "status": "archived"},
    {"id": "gone", "status": "deleting"},
]


class ListCampaignDocsHidesTombstonesTests(unittest.TestCase):
    def test_hides_archived_and_deleting(self):
        with patch("app.routes.scrape.query_collection", return_value=LIST_DOCS):
            out = _list_campaign_docs(status_filter=None)
        self.assertEqual([d["id"] for d in out], ["live"])

    def test_status_filter_deleting_returns_nothing(self):
        with patch("app.routes.scrape.query_collection", return_value=LIST_DOCS):
            out = _list_campaign_docs(status_filter="deleting")
        self.assertEqual(out, [])

    def test_active_filter_excludes_deleting(self):
        with patch("app.routes.scrape.query_collection", return_value=LIST_DOCS):
            out = _list_campaign_docs(status_filter="active")
        self.assertEqual([d["id"] for d in out], ["live"])


class EnsureCampaignCanRunTests(unittest.TestCase):
    def test_refuses_tombstoned_campaign(self):
        with (
            patch(
                "app.routes.scrape.get_document",
                return_value={"id": "c1", "status": "deleting"},
            ),
            patch("app.routes.scrape.update_document") as update,
        ):
            with self.assertRaises(HTTPException) as ctx:
                _ensure_campaign_can_run("c1", action="run")
        self.assertEqual(ctx.exception.status_code, 409)
        update.assert_not_called()

    def test_refuses_running_campaign(self):
        with patch(
            "app.routes.scrape.get_document",
            return_value={"id": "c1", "status": "running"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                _ensure_campaign_can_run("c1", action="run")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_missing_campaign_is_404(self):
        with patch("app.routes.scrape.get_document", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                _ensure_campaign_can_run("missing", action="run")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_pending_campaign_is_allowed(self):
        with (
            patch(
                "app.routes.scrape.get_document",
                return_value={"id": "c1", "status": "pending"},
            ),
            patch("app.routes.scrape.update_document") as update,
        ):
            _ensure_campaign_can_run("c1", action="run")
        update.assert_called_once()


class StaleDeletingIdsTests(unittest.TestCase):
    def test_past_deadline_is_stale(self):
        doc = {"id": "a", "status": "deleting", "requeue_at": NOW - timedelta(seconds=1)}
        self.assertEqual(_stale_deleting_ids([doc], NOW), ["a"])

    def test_future_deadline_is_not_stale(self):
        doc = {"id": "a", "status": "deleting", "requeue_at": NOW + timedelta(minutes=5)}
        self.assertEqual(_stale_deleting_ids([doc], NOW), [])

    def test_missing_requeue_at_is_stale(self):
        doc = {"id": "a", "status": "deleting"}
        self.assertEqual(_stale_deleting_ids([doc], NOW), ["a"])

    def test_naive_datetime_is_treated_as_utc(self):
        doc = {
            "id": "a",
            "status": "deleting",
            "requeue_at": (NOW - timedelta(minutes=1)).replace(tzinfo=None),
        }
        self.assertEqual(_stale_deleting_ids([doc], NOW), ["a"])

    def test_unreadable_requeue_at_is_stale(self):
        doc = {"id": "a", "status": "deleting", "requeue_at": "not-a-timestamp"}
        self.assertEqual(_stale_deleting_ids([doc], NOW), ["a"])

    def test_non_deleting_status_is_skipped(self):
        doc = {"id": "a", "status": "running", "requeue_at": NOW - timedelta(minutes=1)}
        self.assertEqual(_stale_deleting_ids([doc], NOW), [])

    def test_missing_id_is_skipped(self):
        doc = {"status": "deleting", "requeue_at": NOW - timedelta(minutes=1)}
        self.assertEqual(_stale_deleting_ids([doc], NOW), [])


if __name__ == "__main__":
    unittest.main()
