"""Tests for campaign creation: slugified names as document IDs.

No Firestore access: the endpoint's collaborators are patched out.
"""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists

from app.routes.scrape import (
    _delete_campaign_doc,
    _slug_campaign_name,
    create_campaign,
)
from app.schemas import CampaignCreate


def _run(coro):
    return asyncio.run(coro)


class SlugCampaignNameTests(unittest.TestCase):
    def test_joins_niche_and_location(self):
        self.assertEqual(
            _slug_campaign_name("cleaners", "atlanta"), "cleaners-atlanta"
        )

    def test_spaces_become_dashes(self):
        self.assertEqual(
            _slug_campaign_name("house cleaners", "new york"),
            "house-cleaners-new-york",
        )

    def test_trims_collapses_and_lowercases(self):
        self.assertEqual(
            _slug_campaign_name("  Plumbers ", " Austin  "), "plumbers-austin"
        )
        self.assertEqual(_slug_campaign_name("a  b", "c"), "a-b-c")

    def test_strips_non_letters_like_search_queries(self):
        self.assertEqual(
            _slug_campaign_name("house cleaners!", "New York"),
            "house-cleaners-new-york",
        )
        self.assertEqual(
            _slug_campaign_name("cleaners!", "atlanta"), "cleaners-atlanta"
        )
        self.assertEqual(_slug_campaign_name("!!!", "???"), "campaign")

    def test_english_letters_only(self):
        self.assertEqual(
            _slug_campaign_name("24 Hour Plumbers", "Area 51"),
            "hour-plumbers-area",
        )
        self.assertEqual(_slug_campaign_name("Café", "Zürich"), "caf-z-rich")


class CampaignCreateBlankTests(unittest.TestCase):
    def test_blank_niche_or_location_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            CampaignCreate(niche="   ", location="atlanta")
        with self.assertRaises(ValidationError):
            CampaignCreate(niche="cleaners", location="  ")

    def test_padded_values_stripped(self):
        body = CampaignCreate(niche="  cleaners  ", location=" atlanta ")
        self.assertEqual(body.niche, "cleaners")
        self.assertEqual(body.location, "atlanta")


class CreateCampaignUniquenessTests(unittest.TestCase):
    def test_unique_name_creates_doc_with_slug_id(self):
        body = CampaignCreate(niche="House Cleaners", location="New York")
        notion = MagicMock()
        notion.sync_campaign.return_value = None
        db = MagicMock()
        with (
            patch("app.routes.scrape.get_db", return_value=db),
            patch(
                "app.routes.scrape.get_document",
                return_value={
                    "id": "house-cleaners-new-york",
                    "name": "house-cleaners-new-york",
                    "status": "pending",
                },
            ),
            patch("app.routes.scrape.get_notion_sync", return_value=notion),
        ):
            out = _run(create_campaign(body))
        db.collection.assert_called_once_with("campaigns")
        ref = db.collection.return_value.document
        ref.assert_called_once_with("house-cleaners-new-york")
        data_arg = ref.return_value.create.call_args[0][0]
        self.assertEqual(data_arg["name"], "house-cleaners-new-york")
        self.assertIn("created_at", data_arg)
        self.assertIn("updated_at", data_arg)
        notion.sync_campaign.assert_called_once()
        self.assertEqual(notion.sync_campaign.call_args[0][0], "house-cleaners-new-york")
        self.assertEqual(out.name, "house-cleaners-new-york")
        self.assertEqual(out.status.value, "pending")

    def test_duplicate_name_returns_409(self):
        body = CampaignCreate(niche="cleaners", location="atlanta")
        db = MagicMock()
        db.collection.return_value.document.return_value.create.side_effect = (
            AlreadyExists("exists")
        )
        with patch("app.routes.scrape.get_db", return_value=db):
            with self.assertRaises(HTTPException) as ctx:
                _run(create_campaign(body))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("cleaners-atlanta", str(ctx.exception.detail))
        self.assertIsInstance(ctx.exception.__cause__, AlreadyExists)

    def test_stores_website_filter_in_scrape_settings(self):
        body = CampaignCreate(
            niche="cleaners", location="atlanta", website_filter="with_website"
        )
        notion = MagicMock()
        notion.sync_campaign.return_value = None
        db = MagicMock()
        with (
            patch("app.routes.scrape.get_db", return_value=db),
            patch(
                "app.routes.scrape.get_document",
                return_value={
                    "id": "cleaners-atlanta",
                    "name": "cleaners-atlanta",
                    "status": "pending",
                },
            ),
            patch("app.routes.scrape.get_notion_sync", return_value=notion),
        ):
            _run(create_campaign(body))
        ref = db.collection.return_value.document
        ref.assert_called_once_with("cleaners-atlanta")
        data_arg = ref.return_value.create.call_args[0][0]
        self.assertEqual(
            data_arg["scrape_settings"]["website_filter"], "with_website"
        )


class DeleteCampaignDocTests(unittest.TestCase):
    def test_deletes_campaign_doc(self):
        deleted = []
        with (
            patch(
                "app.routes.scrape._ensure_campaign_mutable",
                return_value={"id": "cleaners-atlanta", "name": "cleaners-atlanta"},
            ),
            patch("app.routes.scrape._query_all", return_value=[]),
            patch("app.routes.scrape._archive_notion_page"),
            patch(
                "app.routes.scrape.delete_document",
                side_effect=lambda coll, doc_id: deleted.append((coll, doc_id)),
            ),
        ):
            self.assertTrue(_delete_campaign_doc("cleaners-atlanta"))
        self.assertIn(("campaigns", "cleaners-atlanta"), deleted)


if __name__ == "__main__":
    unittest.main()
