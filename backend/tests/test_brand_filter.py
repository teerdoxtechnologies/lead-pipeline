"""Tests for big-brand exclusion (exact match + the-strip)."""
import unittest

from app.workers.brand_filter import (
    EXCLUDED_BRANDS,
    is_excluded_brand,
    normalize_brand_name,
    strip_leading_the,
)


class NormalizeTests(unittest.TestCase):
    def test_compacts_like_search_queries(self):
        self.assertEqual(normalize_brand_name("  Home   Depot! "), "home depot")
        self.assertEqual(normalize_brand_name("McDonald\u2019s #4521"), "mcdonald s")
        self.assertEqual(normalize_brand_name(""), "")

    def test_strips_leading_the_only(self):
        self.assertEqual(strip_leading_the("the home depot"), "home depot")
        self.assertEqual(strip_leading_the("home depot"), "home depot")
        self.assertEqual(strip_leading_the("theater district"), "theater district")


class IsExcludedBrandTests(unittest.TestCase):
    def test_flagship_hits(self):
        self.assertTrue(is_excluded_brand("The Home Depot"))
        self.assertTrue(is_excluded_brand("HOME DEPOT"))

    def test_exact_only(self):
        self.assertTrue(is_excluded_brand("Roto-Rooter"))
        self.assertFalse(is_excluded_brand("Roto-Rooter of Atlanta"))
        self.assertFalse(is_excluded_brand("Atlanta Roofing Specialists"))
        self.assertFalse(is_excluded_brand(""))
        self.assertFalse(is_excluded_brand("   "))

    def test_list_is_sizable(self):
        self.assertGreaterEqual(len(EXCLUDED_BRANDS), 50)


class WebsitePredicateParityTests(unittest.TestCase):
    """The card-time skip must agree with the persist filter.

    maps_scraper cannot import tasks (circular), so both layers keep
    their own website check. This pins them to identical verdicts on
    realistic inputs; a failure here means one side changed and the
    skip/filter pair must be realigned, otherwise kept leads would
    lose reviews or dropped leads would keep them.
    """

    SAMPLES = [
        "https://example.com",
        "http://example.com/services",
        "https://www.example.com",
        "example.com",
        "www.example.com",
        "https://facebook.com/somebiz",
        "https://www.yelp.com/biz/some-biz",
        "https://mcdonalds.com",
        "not a site",
        "",
    ]

    def test_predicates_agree(self):
        from app.services.maps_scraper import _is_own_website
        from app.workers.tasks import _looks_like_valid_website

        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(
                    bool(_is_own_website(sample)),
                    bool(_looks_like_valid_website(sample)),
                )


if __name__ == "__main__":
    unittest.main()
