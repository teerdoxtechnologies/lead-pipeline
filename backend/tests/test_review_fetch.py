"""Tests for review fetch completeness: text guarantee + stop semantics."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from app.services.maps_scraper import _review_stagnation_update
from app.workers.tasks import _upload_and_sanitize_google_reviews


class ReviewStagnationTests(unittest.TestCase):
    def test_any_progress_resets(self):
        self.assertEqual(
            _review_stagnation_update(
                new_cards_mounted=True, saved_grew=False, stagnant_passes=3
            ),
            0,
        )
        self.assertEqual(
            _review_stagnation_update(
                new_cards_mounted=False, saved_grew=True, stagnant_passes=3
            ),
            0,
        )

    def test_no_progress_increments(self):
        self.assertEqual(
            _review_stagnation_update(
                new_cards_mounted=False, saved_grew=False, stagnant_passes=2
            ),
            3,
        )

    def test_textless_pass_with_new_cards_does_not_stagnate(self):
        stagnant = 0
        for _ in range(6):
            stagnant = _review_stagnation_update(
                new_cards_mounted=True, saved_grew=False, stagnant_passes=stagnant
            )
        self.assertEqual(stagnant, 0)


class UploadPrefilterTests(unittest.TestCase):
    def _run(self, reviews, uploader):
        mod = types.ModuleType("app.services.maps_media_scraper")
        mod.cloudinary_configured = lambda: True
        mod.upload_review_avatars = uploader
        with patch.dict(sys.modules, {"app.services.maps_media_scraper": mod}):
            return _upload_and_sanitize_google_reviews(
                lead_id="l1", business_name="Biz", reviews=reviews
            )

    def test_textless_never_reaches_uploader(self):
        uploader = MagicMock()
        out = self._run(
            [
                {"author": "A", "rating": 4, "text": ""},
                {"author": "B", "rating": 5},
            ],
            uploader,
        )
        uploader.assert_not_called()
        self.assertEqual(out, [])

    def test_text_reviews_flow_through(self):
        uploader = MagicMock(
            side_effect=lambda **kwargs: kwargs.get("reviews", [])
        )
        out = self._run([{"author": "A", "rating": 5, "text": "Great."}], uploader)
        uploader.assert_called_once()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "Great.")


if __name__ == "__main__":
    unittest.main()
