"""Tests for rank-context snapshots (Maps top 5 + organic top 5)."""
import unittest

from app.services.serpapi_service import top_organic_results
from app.workers.tasks import _top_ranked_snapshot


class TopRankedSnapshotTests(unittest.TestCase):
    def test_takes_first_five_by_rank(self):
        businesses = [
            {"business_name": f"B{n}", "maps_rank": n, "website": None}
            for n in range(1, 9)
        ]
        out = _top_ranked_snapshot(businesses, [])
        self.assertEqual([e["rank"] for e in out], [1, 2, 3, 4, 5])
        self.assertEqual(out[0]["business_name"], "B1")

    def test_website_never_excludes(self):
        businesses = [
            {"business_name": "Has Site", "maps_rank": 1, "website": "https://x.com"},
            {"business_name": "No Site", "maps_rank": 2, "website": None},
        ]
        out = _top_ranked_snapshot(businesses, [])
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["has_website"])
        self.assertFalse(out[1]["has_website"])

    def test_brand_skips_merge_by_rank(self):
        businesses = [{"business_name": "B2", "maps_rank": 2, "website": None}]
        skipped = [{"maps_rank": 1, "business_name": "Brand Co"}]
        out = _top_ranked_snapshot(businesses, skipped)
        self.assertEqual([e["rank"] for e in out], [1, 2])
        self.assertTrue(out[0]["brand_excluded"])
        self.assertFalse(out[1]["brand_excluded"])

    def test_empty(self):
        self.assertEqual(_top_ranked_snapshot([], []), [])
        self.assertEqual(_top_ranked_snapshot(None, None), [])


class TopOrganicResultsTests(unittest.TestCase):
    def _r(self, domain, position):
        return {
            "position": position,
            "title": f"{domain} page",
            "link": f"https://{domain}/x",
            "domain": domain,
            "root_domain": domain,
        }

    def test_excludes_directories_keeps_order(self):
        results = [
            self._r("yelp.com", 1),
            self._r("acmeroofs.com", 2),
            self._r("facebook.com", 3),
            self._r("bestroofs.com", 4),
            self._r("bbb.org", 5),
        ]
        out = top_organic_results(results)
        self.assertEqual([r["domain"] for r in out], ["acmeroofs.com", "bestroofs.com"])

    def test_caps_at_five(self):
        results = [self._r(f"biz{n}.com", n) for n in range(1, 9)]
        out = top_organic_results(results)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["domain"], "biz1.com")

    def test_empty(self):
        self.assertEqual(top_organic_results([]), [])
        self.assertEqual(top_organic_results(None), [])


if __name__ == "__main__":
    unittest.main()
