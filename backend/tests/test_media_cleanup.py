"""Tests for Cloudinary media cleanup on campaign delete.

No Firestore or network access: the media module is faked in sys.modules.
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from app.routes.scrape import _delete_campaign_cloudinary_media


def _fake_media_module(deleter):
    mod = types.ModuleType("app.services.maps_media_scraper")
    mod.delete_cloudinary_media = deleter
    return mod


LEADS = [
    {
        "id": "l1",
        "google_listing_images": [
            {"cloudinary_public_id": "img1", "resource_type": "image"},
            {"no_public_id": True},
        ],
        "google_listing_videos": [
            {"cloudinary_public_id": "vid1", "resource_type": "video"},
        ],
        "google_reviews": [
            {"author": "a", "avatar_cloudinary_public_id": "av1"},
            {"author": "b"},
            "not-a-dict",
        ],
    },
    {"id": "l2"},
]


class DeleteCampaignCloudinaryMediaTests(unittest.TestCase):
    def test_collects_images_videos_and_avatars(self):
        deleter = MagicMock(return_value={"deleted": 3, "failed": 0, "errors": []})
        with patch.dict(
            sys.modules, {"app.services.maps_media_scraper": _fake_media_module(deleter)}
        ):
            _delete_campaign_cloudinary_media("c1", LEADS)
        deleter.assert_called_once()
        items = deleter.call_args[0][0]
        self.assertEqual(
            {i["cloudinary_public_id"] for i in items}, {"img1", "vid1", "av1"}
        )
        by_id = {i["cloudinary_public_id"]: i for i in items}
        self.assertEqual(by_id["vid1"]["resource_type"], "video")
        self.assertEqual(by_id["av1"]["resource_type"], "image")

    def test_no_media_never_calls_deleter(self):
        deleter = MagicMock()
        with patch.dict(
            sys.modules, {"app.services.maps_media_scraper": _fake_media_module(deleter)}
        ):
            _delete_campaign_cloudinary_media("c1", [{"id": "l2"}])
        deleter.assert_not_called()

    def test_deleter_failure_does_not_raise(self):
        deleter = MagicMock(side_effect=RuntimeError("cloudinary down"))
        with patch.dict(
            sys.modules, {"app.services.maps_media_scraper": _fake_media_module(deleter)}
        ):
            _delete_campaign_cloudinary_media("c1", LEADS)

    def test_missing_module_does_not_raise(self):
        bare = types.ModuleType("app.services.maps_media_scraper")
        with patch.dict(sys.modules, {"app.services.maps_media_scraper": bare}):
            _delete_campaign_cloudinary_media("c1", LEADS)


if __name__ == "__main__":
    unittest.main()
