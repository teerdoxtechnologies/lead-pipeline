"""Tests for the failed-status guard (no worker needed)."""
import asyncio
import unittest
from unittest.mock import patch

from app.workers.tasks import (
    CampaignCancelled,
    _fail_campaign,
    _with_failed_status,
)


class FailCampaignTests(unittest.TestCase):
    def test_writes_failed_payload(self):
        with patch("app.workers.tasks.update_document") as update:
            out = _fail_campaign("c1", "boom happened")
        update.assert_called_once()
        args = update.call_args[0]
        self.assertEqual(args[0], "campaigns")
        self.assertEqual(args[1], "c1")
        self.assertEqual(args[2]["status"], "failed")
        self.assertIn("boom happened", args[2]["progress"]["message"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["campaign_id"], "c1")

    def test_update_failure_still_returns(self):
        with patch(
            "app.workers.tasks.update_document", side_effect=RuntimeError("db down")
        ):
            out = _fail_campaign("c1", "boom")
        self.assertEqual(out["status"], "failed")


class WithFailedStatusTests(unittest.TestCase):
    def _wrapped(self, fn):
        return _with_failed_status(fn)

    def test_success_passes_through_without_update(self):
        def fn(self, campaign_id):
            return {"status": "completed"}

        with patch("app.workers.tasks.update_document") as update:
            out = self._wrapped(fn)(None, "c1")
        self.assertEqual(out, {"status": "completed"})
        update.assert_not_called()

    def test_exception_marks_failed(self):
        def fn(self, campaign_id):
            raise RuntimeError("kaboom")

        with patch("app.workers.tasks.update_document") as update:
            out = self._wrapped(fn)(None, "c1")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["campaign_id"], "c1")
        self.assertIn("kaboom", out["error"])
        args = update.call_args[0]
        self.assertEqual(args[0], "campaigns")
        self.assertEqual(args[2]["status"], "failed")

    def test_cancellation_propagates(self):
        def fn_cancelled(self, campaign_id):
            raise CampaignCancelled("stop")

        def fn_async_cancelled(self, campaign_id):
            raise asyncio.CancelledError()

        with patch("app.workers.tasks.update_document") as update:
            with self.assertRaises(CampaignCancelled):
                self._wrapped(fn_cancelled)(None, "c1")
            with self.assertRaises(asyncio.CancelledError):
                self._wrapped(fn_async_cancelled)(None, "c1")
        update.assert_not_called()

    def test_preserves_name_and_signature(self):
        def fn(self, campaign_id, extra=None):
            return extra

        wrapped = self._wrapped(fn)
        self.assertEqual(wrapped.__name__, "fn")
        self.assertEqual(wrapped(None, "c1", extra="x"), "x")


if __name__ == "__main__":
    unittest.main()
