"""Tests for Notion sync pacing and threaded merge (no network needed)."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.routes.scrape import _merge_lead_sync_results
from app.services.notion_service import NotionSync


class PaceRequestsTests(unittest.TestCase):
    def _sync(self, delay):
        sync = NotionSync.__new__(NotionSync)
        sync.settings = SimpleNamespace(notion_request_delay_seconds=delay)
        return sync

    def test_concurrent_calls_respect_interval(self):
        sync = self._sync(0.05)
        stamps = []
        lock = threading.Lock()

        def call():
            sync._pace_requests()
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=call) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 6)
        self.assertTrue(all(gap >= 0.04 for gap in gaps), gaps)

    def test_zero_delay_skips(self):
        sync = self._sync(0)
        started = time.monotonic()
        sync._pace_requests()
        self.assertLess(time.monotonic() - started, 0.05)


class MergeLeadSyncResultsTests(unittest.TestCase):
    def test_applies_updates_and_counts(self):
        updated = []
        leads = [{"id": "l1"}, {"id": "l2"}, {"id": "l3"}]
        synced = {"l1": "page1", "l2": None}
        with patch(
            "app.routes.scrape.update_document",
            side_effect=lambda coll, doc_id, data: updated.append(
                (coll, doc_id, data)
            ),
        ):
            ids, counts = _merge_lead_sync_results("c1", leads, synced)
        self.assertEqual(ids, {"l1": "page1"})
        self.assertEqual(counts, {"leads_synced": 1, "skipped": 1})
        self.assertEqual(
            updated, [("leads", "l1", {"notion_page_id": "page1"})]
        )

    def test_empty_sync_is_noop(self):
        with patch("app.routes.scrape.update_document") as update:
            ids, counts = _merge_lead_sync_results("c1", [{"id": "l1"}], {})
        update.assert_not_called()
        self.assertEqual(ids, {})
        self.assertEqual(counts, {"leads_synced": 0, "skipped": 0})


if __name__ == "__main__":
    unittest.main()
