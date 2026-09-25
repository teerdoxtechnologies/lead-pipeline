"""Tests for the campaign website filter (no-website focus).

Covers the pure pieces: filter resolution, the allow gate, settings
plumbing, and request validation. No Firestore access.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas import CampaignCreate
from app.workers.tasks import (
    _campaign_scrape_settings,
    _resolve_website_filter,
    _website_allowed,
)


def _settings(**overrides):
    base = {
        "max_maps_results": 500,
        "global_lead_dedupe_enabled": False,
        "maps_listing_media_enabled": True,
        "website_filter_default": "no_website",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class WebsiteAllowedTests(unittest.TestCase):
    def test_no_website_only(self):
        self.assertTrue(_website_allowed(False, "no_website"))
        self.assertFalse(_website_allowed(True, "no_website"))

    def test_with_website_only(self):
        self.assertTrue(_website_allowed(True, "with_website"))
        self.assertFalse(_website_allowed(False, "with_website"))

    def test_all_listings(self):
        self.assertTrue(_website_allowed(True, "all"))
        self.assertTrue(_website_allowed(False, "all"))


class ResolveWebsiteFilterTests(unittest.TestCase):
    def test_valid_values_pass_through(self):
        for value in ("no_website", "with_website", "all"):
            self.assertEqual(_resolve_website_filter(value, "all"), value)

    def test_missing_and_garbage_fall_back_to_default(self):
        self.assertEqual(_resolve_website_filter(None, "with_website"), "with_website")
        self.assertEqual(_resolve_website_filter("sometimes", "all"), "all")

    def test_bad_default_falls_back_to_no_website(self):
        self.assertEqual(_resolve_website_filter(None, "sometimes"), "no_website")
        self.assertEqual(_resolve_website_filter("sometimes", "sometimes"), "no_website")


class CampaignScrapeSettingsTests(unittest.TestCase):
    def test_stored_filter_wins(self):
        campaign = {"scrape_settings": {"website_filter": "all"}}
        with patch("app.workers.tasks.get_settings", return_value=_settings()):
            out = _campaign_scrape_settings(campaign)
        self.assertEqual(out["website_filter"], "all")

    def test_missing_filter_uses_global_default(self):
        with patch("app.workers.tasks.get_settings", return_value=_settings()):
            out = _campaign_scrape_settings({})
        self.assertEqual(out["website_filter"], "no_website")

    def test_garbage_filter_uses_global_default(self):
        campaign = {"scrape_settings": {"website_filter": "sometimes"}}
        with patch(
            "app.workers.tasks.get_settings",
            return_value=_settings(website_filter_default="with_website"),
        ):
            out = _campaign_scrape_settings(campaign)
        self.assertEqual(out["website_filter"], "with_website")


class CampaignCreateWebsiteFilterTests(unittest.TestCase):
    def test_accepts_valid_filters(self):
        for value in ("no_website", "with_website", "all"):
            body = CampaignCreate(
                niche="cleaners", location="atlanta", website_filter=value
            )
            self.assertEqual(body.website_filter, value)

    def test_defaults_to_none(self):
        body = CampaignCreate(niche="cleaners", location="atlanta")
        self.assertIsNone(body.website_filter)

    def test_rejects_invalid_filter(self):
        with self.assertRaises(Exception):
            CampaignCreate(
                niche="cleaners", location="atlanta", website_filter="sometimes"
            )


if __name__ == "__main__":
    unittest.main()
