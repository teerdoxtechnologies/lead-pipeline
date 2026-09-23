import unittest

from app.services.maps_scraper import (
    _clean_review_author,
    _listing_key,
    _parse_fields,
    _parse_rating_value,
    _parse_review_count,
    _review_key,
)


class MapsScraperParsingTests(unittest.TestCase):
    def test_parse_fields_extracts_basic_business_data(self):
        parsed = _parse_fields([
            "123 Main St, Atlanta, GA",
            "(404) 555-1234",
            "example.com",
            "Example Salon",
        ])

        self.assertEqual(parsed["business_name"], "Example Salon")
        self.assertEqual(parsed["address"], "123 Main St, Atlanta, GA")
        self.assertEqual(parsed["phone"], "(404) 555-1234")
        self.assertEqual(parsed["website"], "example.com")

    def test_parse_fields_extracts_maps_detail_website_domain(self):
        parsed = _parse_fields([
            "544 Angier Ave NE, Atlanta, GA 30308",
            "(404) 555-1234",
            "peachstatecleaning.com",
            "Peach State Cleaning",
        ])

        self.assertEqual(parsed["website"], "peachstatecleaning.com")

    def test_parse_fields_does_not_treat_website_as_address_when_address_missing(self):
        parsed = _parse_fields([
            "peachstatecleaning.com",
            "(404) 555-1234",
            "Peach State Cleaning",
        ])

        self.assertIsNone(parsed["address"])
        self.assertEqual(parsed["website"], "peachstatecleaning.com")
        self.assertEqual(parsed["phone"], "(404) 555-1234")

    def test_parse_fields_ignores_booking_sites_as_websites(self):
        parsed = _parse_fields([
            "123 Main St, Atlanta, GA",
            "booking.com/example",
            "bookingkoala.com/example",
            "getjobber.com/example",
            "Example Salon",
        ])

        self.assertIsNone(parsed["website"])

    def test_parse_fields_prefers_official_website_over_booking_platform(self):
        parsed = _parse_fields([
            "bookingkoala.com/heavenscent",
            "heavenscentatl.com",
            "We're Heaven Scent Cleaning Services L.L.C",
        ])

        self.assertEqual(parsed["website"], "heavenscentatl.com")

    def test_parse_fields_ignores_non_address_business_attributes(self):
        parsed = _parse_fields([
            "Identifies as women-owned",
            "Identifies as Black-owned",
            "LGBTQ+ friendly",
            "Open 24 hours",
            "(404) 555-1234",
            "New to Neat Cleaning Company",
        ])

        self.assertIsNone(parsed["address"])
        self.assertEqual(parsed["phone"], "(404) 555-1234")

    def test_parse_fields_ignores_hyphen_variants_in_non_address_attributes(self):
        parsed = _parse_fields([
            "Identifies as Black\u2011owned",
            "Identifies as women\u2011owned",
            "Example Cleaner",
        ])

        self.assertIsNone(parsed["address"])

    def test_parse_fields_never_saves_website_url_as_address(self):
        parsed = _parse_fields([
            "https://www.example-cleaning.com/contact",
            "www.example-cleaning.com",
            "example-cleaning.com/services",
            "Example Cleaner",
        ])

        self.assertIsNone(parsed["address"])
        self.assertEqual(parsed["website"], "https://www.example-cleaning.com/contact")

    def test_listing_key_prefers_name_and_address(self):
        key = _listing_key({
            "business_name": "Example Salon",
            "address": "123 Main St",
            "phone": "(404) 555-1234",
            "google_maps_url": "https://maps.google.com/?cid=1",
        })

        self.assertEqual(key, "example salon|123 main st")

    def test_listing_key_falls_back_to_name_and_phone(self):
        key = _listing_key({
            "business_name": "Example Salon",
            "address": None,
            "phone": "(404) 555-1234",
            "google_maps_url": "https://maps.google.com/?cid=1",
        })

        self.assertEqual(key, "example salon|4045551234")

    def test_parse_rating_value_from_maps_text(self):
        self.assertEqual(_parse_rating_value("4.8 stars"), 4.8)
        self.assertEqual(_parse_rating_value("Rated 5.0 out of 5"), 5.0)
        self.assertIsNone(_parse_rating_value("No rating"))

    def test_parse_review_count_from_maps_text(self):
        self.assertEqual(_parse_review_count("1,234 reviews"), 1234)
        self.assertEqual(_parse_review_count("58 Reviews"), 58)
        self.assertEqual(_parse_review_count("5.0\n(8)"), 8)
        self.assertIsNone(_parse_review_count("No reviews yet"))

    def test_clean_review_author_removes_google_metadata_lines(self):
        self.assertEqual(
            _clean_review_author("Ashley Sharpe\nLocal Guide · 37 reviews · 131 photos"),
            "Ashley Sharpe",
        )

    def test_review_key_dedupes_by_id_or_author_and_text(self):
        self.assertEqual(
            _review_key({"review_id": "abc", "author": "A", "text": "One"}),
            "abc",
        )
        self.assertEqual(
            _review_key({"author": "Ashley\nLocal Guide", "text": "Great clean"}),
            "ashley|great clean",
        )


if __name__ == "__main__":
    unittest.main()
