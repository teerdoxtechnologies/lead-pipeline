"""Tests for maps-repair website graduation (no browser needed)."""
import unittest

from app.workers.tasks import _repair_website_graduation


class RepairWebsiteGraduationTests(unittest.TestCase):
    def test_flip_graduates(self):
        self.assertTrue(
            _repair_website_graduation({"has_website": False}, "https://example.com")
        )
        self.assertTrue(
            _repair_website_graduation({}, "https://example.com")
        )

    def test_no_flip_no_graduation(self):
        self.assertFalse(
            _repair_website_graduation({"has_website": True}, "https://example.com")
        )
        self.assertFalse(
            _repair_website_graduation({"has_website": False}, "")
        )
        self.assertFalse(
            _repair_website_graduation({"has_website": False}, None)
        )
        self.assertFalse(
            _repair_website_graduation({}, None)
        )


if __name__ == "__main__":
    unittest.main()
