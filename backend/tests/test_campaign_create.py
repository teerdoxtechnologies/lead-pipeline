"""Tests for campaign creation: slugified, unique names.

No Firestore access: the endpoint's collaborators are patched out.
"""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routes.scrape import (
    _campaign_name_key,
    _delete_campaign_doc,
    _insert_campaign_with_unique_name,
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


class CreateCampaignUniquenessTests(unittest.TestCase):
    def test_duplicate_name_returns_409_without_creating(self):
        body = CampaignCreate(niche="cleaners", location="atlanta")
        with (
            patch(
                "app.routes.scrape.query_collection",
                return_value=[{"id": "old"}],
            ),
            patch(
                "app.routes.scrape._insert_campaign_with_unique_name"
            ) as insert,
        ):
            with self.assertRaises(HTTPException) as ctx:
                _run(create_campaign(body))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("cleaners-atlanta", str(ctx.exception.detail))
        insert.assert_not_called()

    def test_unique_name_creates_with_slug(self):
        body = CampaignCreate(niche="House Cleaners", location="New York")
        notion = MagicMock()
        notion.sync_campaign.return_value = None
        with (
            patch("app.routes.scrape.query_collection", return_value=[]),
            patch("app.routes.scrape.get_db") as get_db,
            patch(
                "app.routes.scrape._insert_campaign_with_unique_name",
                return_value="new-id",
            ) as insert,
            patch(
                "app.routes.scrape.get_document",
                return_value={
                    "id": "new-id",
                    "name": "house-cleaners-new-york",
                    "status": "pending",
                },
            ),
            patch("app.routes.scrape.get_notion_sync", return_value=notion),
        ):
            out = _run(create_campaign(body))
        insert.assert_called_once()
        transaction_arg, data_arg, name_arg = insert.call_args[0]
        self.assertIs(transaction_arg, get_db.return_value.transaction.return_value)
        self.assertEqual(name_arg, "house-cleaners-new-york")
        self.assertEqual(data_arg["name"], "house-cleaners-new-york")
        self.assertEqual(out.name, "house-cleaners-new-york")
        self.assertEqual(out.status.value, "pending")

    def test_stores_website_filter_in_scrape_settings(self):
        body = CampaignCreate(
            niche="cleaners", location="atlanta", website_filter="with_website"
        )
        notion = MagicMock()
        notion.sync_campaign.return_value = None
        with (
            patch("app.routes.scrape.query_collection", return_value=[]),
            patch("app.routes.scrape.get_db"),
            patch(
                "app.routes.scrape._insert_campaign_with_unique_name",
                return_value="new-id",
            ) as insert,
            patch(
                "app.routes.scrape.get_document",
                return_value={
                    "id": "new-id",
                    "name": "cleaners-atlanta",
                    "status": "pending",
                },
            ),
            patch("app.routes.scrape.get_notion_sync", return_value=notion),
        ):
            _run(create_campaign(body))
        data_arg = insert.call_args[0][1]
        self.assertEqual(
            data_arg["scrape_settings"]["website_filter"], "with_website"
        )


class _FakeSnapshot:
    def __init__(self, exists):
        self.exists = exists


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self.id = doc_id

    def get(self, transaction=None):
        return _FakeSnapshot(self.id in self._store)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id=None):
        if doc_id is None:
            doc_id = f"auto-{len(self._store) + 1}"
        return _FakeDocRef(self._store, doc_id)


class _FakeTransaction:
    """Minimal stand-in for a Firestore Transaction (commit path only)."""

    _read_only = False
    _max_attempts = 1
    _id = "fake-txn"

    def set(self, ref, data):
        ref._store[ref.id] = dict(data)

    def _commit(self):
        return None

    def _rollback(self):
        return None

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        return None


class _FakeDB:
    def __init__(self):
        self.stores = {}

    def collection(self, name):
        return _FakeCollection(self.stores.setdefault(name, {}))

    def transaction(self):
        return _FakeTransaction()


class InsertCampaignWithUniqueNameTests(unittest.TestCase):
    def test_writes_campaign_and_reservation(self):
        db = _FakeDB()
        with patch("app.routes.scrape.get_db", return_value=db):
            doc_id = _insert_campaign_with_unique_name(
                db.transaction(),
                {"name": "cleaners-atlanta", "status": "pending"},
                "cleaners-atlanta",
            )
        self.assertEqual(doc_id, "auto-1")
        self.assertEqual(
            db.stores["campaigns"]["auto-1"]["name"], "cleaners-atlanta"
        )
        self.assertEqual(
            db.stores["campaign_names"]["cleaners-atlanta"]["campaign_id"],
            "auto-1",
        )

    def test_reservation_conflict_returns_409_without_writing(self):
        db = _FakeDB()
        db.stores["campaign_names"] = {
            "cleaners-atlanta": {"campaign_id": "old"}
        }
        with patch("app.routes.scrape.get_db", return_value=db):
            with self.assertRaises(HTTPException) as ctx:
                _insert_campaign_with_unique_name(
                    db.transaction(),
                    {"name": "cleaners-atlanta"},
                    "cleaners-atlanta",
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertNotIn("campaigns", db.stores)

    def test_name_key_quotes_slashes(self):
        self.assertEqual(
            _campaign_name_key("cleaners-atlanta"), "cleaners-atlanta"
        )
        self.assertEqual(_campaign_name_key("a/b"), "a%2Fb")


class DeleteCampaignDocReleasesNameTests(unittest.TestCase):
    def test_releases_reservation(self):
        deleted = []
        with (
            patch(
                "app.routes.scrape._ensure_campaign_mutable",
                return_value={"id": "c1", "name": "cleaners-atlanta"},
            ),
            patch("app.routes.scrape._query_all", return_value=[]),
            patch("app.routes.scrape._archive_notion_page"),
            patch(
                "app.routes.scrape.delete_document",
                side_effect=lambda coll, doc_id: deleted.append((coll, doc_id)),
            ),
        ):
            self.assertTrue(_delete_campaign_doc("c1"))
        self.assertIn(("campaigns", "c1"), deleted)
        self.assertIn(("campaign_names", "cleaners-atlanta"), deleted)


if __name__ == "__main__":
    unittest.main()
