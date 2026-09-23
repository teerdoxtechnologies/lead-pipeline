import unittest

from app.services.maps_media_scraper import (
    _dedupe_urls,
    _google_image_dimensions,
    _looks_like_listing_image_url,
)


class MapsMediaScraperTests(unittest.TestCase):
    def test_listing_image_filter_accepts_googleusercontent_photos(self):
        self.assertTrue(
            _looks_like_listing_image_url(
                "https://lh3.googleusercontent.com/p/AF1QipExample=w408-h306-k-no"
            )
        )

    def test_listing_image_filter_rejects_non_google_images(self):
        self.assertFalse(
            _looks_like_listing_image_url(
                "https://example.com/photo.jpg"
            )
        )

    def test_listing_image_filter_rejects_google_profile_avatars(self):
        self.assertFalse(
            _looks_like_listing_image_url(
                "https://lh3.googleusercontent.com/a-/ALV-UjVWWt52-rHo=w36-h36-p-rp-mo-ba3-br100"
            )
        )
        self.assertFalse(
            _looks_like_listing_image_url(
                "https://lh3.googleusercontent.com/a/ACg8ocLU5bhY=s48-p-k-no-mo"
            )
        )

    def test_google_image_dimensions_reads_width_height_and_square_size(self):
        self.assertEqual(
            _google_image_dimensions(
                "https://lh3.googleusercontent.com/gps-cs-s/example=w397-h298-k-no"
            ),
            (397, 298),
        )
        self.assertEqual(
            _google_image_dimensions(
                "https://lh3.googleusercontent.com/a/example=s48-p-k-no-mo"
            ),
            (48, 48),
        )

    def test_dedupe_treats_google_size_variants_as_same_media(self):
        urls = _dedupe_urls([
            "https://lh3.googleusercontent.com/p/AF1QipExample=w408-h306-k-no",
            "https://lh3.googleusercontent.com/p/AF1QipExample=w1200-h900-k-no",
            "https://lh3.googleusercontent.com/p/Other=w408-h306-k-no",
        ])

        self.assertEqual(len(urls), 2)


if __name__ == "__main__":
    unittest.main()
