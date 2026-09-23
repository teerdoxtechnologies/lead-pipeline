from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.routes.export import _campaign_folder_name, _lead_csv_row
from app.routes.email import _sent_follow_up_updates, update_outreach_status, router as email_router
from app.routes.websites import _generated_preview_cleanup_reason
from app.routes.websites import _has_closed_outreach, _lead_static_cleanup_paths
from app.routes.scrape import (
    DELETE_CONFIRMATION,
    _campaign_with_lead_derived_maps_stats,
    _campaigns_blocking_delete,
    _ensure_delete_confirmed,
    router as campaign_router,
)
from app.services.gemini_analyzer import (
    _browser_quality_score_from_metrics,
    _browser_quality_summary_from_metrics,
    _search_claim_guardrails,
)
from app.services.notion_service import NotionSync, _database_property_metadata, _search_results_text
from app.services.email_extractor import _parse_html, extract_emails_and_team_members_from_page_data
from app.services.no_website_appraisal import (
    appraise_no_website,
    generate_no_website_email_template,
)
from app.services.audit_report_artifacts import write_audit_report_artifact
from app.services.audit_report_renderer import render_audit_report_html
from app.services.google_calendar_service import _calendar_event_body, _event_window
from app.services.gmail_service import _plain_text_to_html
from app.services.outreach_history import append_message_history
from app.services.serpapi_service import (
    _looks_like_business_owned_result,
    _organic_result_payload,
    _root_domain,
    is_search_evidence_stale,
    normalize_search_query,
)
from app.services.website_appraisal import (
    appraise_website,
    generate_audit_email_template,
)
from app.services.website_filters import is_non_official_website
from app.services.website_scraper import is_security_verification_page
from app.routes.audits import _count_result, _update_lead_contact_fields_from_page_data, router as audit_router
from app.schemas import (
    CampaignAuditSummary,
    CampaignDeleteRequest,
    DraftStatus,
    FollowUpDraftRequest,
    JobResponse,
    OutreachDraftRegenerateRequest,
    OutreachStatusUpdateRequest,
    WebsiteLeadProcessRequest,
)
from app.services.static_website_generator import (
    _contact_html,
    _landing_html,
    _landing_css,
    _logo_text,
    _site_context,
    _slugify,
    _write_site,
    eligible_for_static_website,
    remove_generated_website_path,
)
from app.workers.tasks import (
    _campaign_processing_mode,
    _campaign_scrape_settings,
    _append_draft_reference,
    _carry_existing_review_avatars,
    _create_gmail_draft_with_retries,
    _follow_up_body,
    _follow_up_subject,
    _gmail_regeneration_action,
    _generate_due_follow_up_drafts,
    _lead_dedupe_keys,
    _lead_quality_flags,
    _local_preview_url,
    _maps_repair_fields_for_lead,
    _maybe_rewrite_outreach_email,
    _normalize_maps_repair_fields,
    _normalize_raw_lead_fields,
    _regenerated_outreach_email_content,
    _report_for_outreach,
    _resolve_audit_static_path,
    _result_with_audit_publish,
    _reuse_existing_audit_for_lead,
    _sanitize_google_reviews,
    _should_process_lead_in_mode,
    bulk_delete_campaigns,
)


def _complete_listing_images(count: int = 12) -> list[dict[str, str]]:
    return [
        {"url": f"https://res.cloudinary.com/demo/image/upload/listing_{index}.jpg"}
        for index in range(count)
    ]


class CampaignHelperTests(unittest.TestCase):
    def test_delete_confirmation_rejects_wrong_token(self):
        with self.assertRaises(HTTPException) as context:
            _ensure_delete_confirmed("wrong")

        self.assertEqual(context.exception.status_code, 400)

    def test_delete_confirmation_accepts_expected_token(self):
        _ensure_delete_confirmed(DELETE_CONFIRMATION)

    def test_campaign_delete_route_is_single_scoped_endpoint(self):
        routes_by_path = {getattr(route, "path", ""): route for route in campaign_router.routes}
        paths = set(routes_by_path)

        self.assertIn("/campaigns/delete", paths)
        self.assertNotIn("/campaigns/delete-selected", paths)
        self.assertNotIn("/campaigns/{campaign_id}", paths)
        self.assertIs(routes_by_path["/campaigns/delete"].body_field.type_, CampaignDeleteRequest)
        query_params = {param.name for param in routes_by_path["/campaigns/delete"].dependant.query_params}
        self.assertIn("scope", query_params)

    def test_campaigns_blocking_delete_returns_active_campaigns(self):
        def fake_get_document(_collection, doc_id):
            return {
                "idle": {"status": "completed"},
                "running": {"status": "running"},
                "analyzing": {"status": "analyzing"},
            }.get(doc_id)

        with patch("app.routes.scrape.get_document", side_effect=fake_get_document):
            blocked = _campaigns_blocking_delete(["idle", "running", "analyzing", "missing"])

        self.assertEqual(blocked, ["running", "analyzing"])

    def test_bulk_delete_all_blocks_without_deleting_when_any_campaign_active(self):
        with patch("app.routes.scrape._all_campaign_ids", return_value=["idle", "running"]), patch(
            "app.routes.scrape._campaigns_blocking_delete",
            return_value=["running"],
        ), patch("app.routes.scrape._delete_campaign_doc") as delete_campaign:
            result = bulk_delete_campaigns.run(None)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking_campaign_ids"], ["running"])
        delete_campaign.assert_not_called()

    def test_campaign_folder_name_is_slugged_and_unique(self):
        used = set()
        first = _campaign_folder_name(
            "abc123",
            {"name": "Atlanta Hair", "niche": "Hair Salon", "location": "Atlanta, GA"},
            used,
        )
        second = _campaign_folder_name(
            "abc123",
            {"name": "Atlanta Hair", "niche": "Hair Salon", "location": "Atlanta, GA"},
            used,
        )

        self.assertEqual(first, "abc123-atlanta-hair-hair-salon-atlanta-ga")
        self.assertEqual(second, "abc123-atlanta-hair-hair-salon-atlanta-ga-2")

    def test_sanitize_google_reviews_removes_transient_urls(self):
        reviews = _sanitize_google_reviews([
            {
                "author": "A",
                "rating": 5,
                "text": "Great work.",
                "review_id": "r1",
                "url": "https://maps.google.com/review",
                "listing_reviews_url": "https://maps.google.com/reviews",
                "avatar_source_url": "https://lh3.googleusercontent.com/avatar",
                "avatar_url": "https://res.cloudinary.com/example/avatar.png",
                "avatar_cloudinary_public_id": "folder/avatar",
            }
        ])

        self.assertEqual(reviews[0]["author"], "A")
        self.assertEqual(reviews[0]["avatar_cloudinary_public_id"], "folder/avatar")
        self.assertNotIn("url", reviews[0])
        self.assertNotIn("listing_reviews_url", reviews[0])
        self.assertNotIn("avatar_source_url", reviews[0])

    def test_carry_existing_review_avatars_reuses_cloudinary_fields(self):
        reviews = _carry_existing_review_avatars(
            [
                {
                    "author": "A",
                    "text": "Great work.",
                    "review_id": "r1",
                    "avatar_source_url": "https://lh3.googleusercontent.com/new-avatar",
                }
            ],
            [
                {
                    "author": "A",
                    "text": "Great work.",
                    "review_id": "r1",
                    "avatar_url": "https://res.cloudinary.com/example/avatar.png",
                    "avatar_cloudinary_public_id": "folder/avatar",
                }
            ],
        )

        self.assertEqual(reviews[0]["avatar_url"], "https://res.cloudinary.com/example/avatar.png")
        self.assertEqual(reviews[0]["avatar_cloudinary_public_id"], "folder/avatar")

    def test_lead_csv_row_uses_latest_related_records(self):
        row = _lead_csv_row({
            "id": "lead1",
            "business_name": "Example Salon",
            "emails": ["owner@example.com"],
            "audit_reports": [
                {"id": "old", "slug": "old-slug", "overall_score": 5},
                {"id": "new", "slug": "new-slug", "overall_score": 9},
            ],
            "no_website_reports": [],
            "email_drafts": [{"id": "draft1", "gmail_draft_id": "gmail1", "subject": "Hello"}],
        })

        self.assertEqual(row["emails"], "owner@example.com")
        self.assertEqual(row["audit_report_id"], "new")
        self.assertEqual(row["audit_overall_score"], 9)
        self.assertEqual(row["email_draft_id"], "draft1")

    def test_lead_dedupe_keys_normalize_phone_and_domain(self):
        keys = _lead_dedupe_keys(
            " Example Salon ",
            phone="(404) 555-1234",
            website="https://www.example.com/path",
        )

        self.assertEqual(keys["business_name_key"], "example salon")
        self.assertEqual(keys["phone_key"], "4045551234")
        self.assertEqual(keys["website_domain"], "example.com")

    def test_lead_quality_flags_track_missing_website(self):
        self.assertEqual(_lead_quality_flags(False), {"missing_website": True})
        self.assertEqual(_lead_quality_flags(True), {"missing_website": False})

    def test_normalize_raw_lead_moves_domain_address_to_website(self):
        lead, metrics = _normalize_raw_lead_fields({
            "business_name": "Peach State Cleaning",
            "address": "peachstatecleaning.com",
            "website": None,
        })

        self.assertIsNone(lead["address"])
        self.assertEqual(lead["website"], "https://peachstatecleaning.com")
        self.assertTrue(lead["has_website"])
        self.assertFalse(lead["missing_website"])
        self.assertEqual(lead["website_domain"], "peachstatecleaning.com")
        self.assertEqual(metrics["invalid_address_domains"], 1)

    def test_non_official_website_filter_blocks_platforms_and_edu(self):
        self.assertTrue(is_non_official_website("https://bookingkoala.com/heavenscent"))
        self.assertTrue(is_non_official_website("https://client.getjobber.com/booking"))
        self.assertTrue(is_non_official_website("https://cleaning.example.edu"))
        self.assertFalse(is_non_official_website("https://heavenscentatl.com"))

    def test_normalize_raw_lead_discards_non_official_website(self):
        lead, _metrics = _normalize_raw_lead_fields({
            "business_name": "Example Cleaners",
            "website": "bookingkoala.com/example",
        })

        self.assertIsNone(lead["website"])
        self.assertFalse(lead["has_website"])
        self.assertEqual(lead["website_domain"], "")

    def test_normalize_raw_lead_moves_url_address_to_website(self):
        lead, metrics = _normalize_raw_lead_fields({
            "business_name": "Peach State Cleaning",
            "address": "https://peachstatecleaning.com/contact?src=maps",
            "website": None,
        })

        self.assertIsNone(lead["address"])
        self.assertEqual(lead["website"], "https://peachstatecleaning.com/contact?src=maps")
        self.assertTrue(lead["has_website"])
        self.assertEqual(lead["website_domain"], "peachstatecleaning.com")
        self.assertEqual(metrics["invalid_address_domains"], 1)
        self.assertEqual(metrics["parser_anomalies"], 1)

    def test_maps_repair_skips_valid_existing_data(self):
        fields = _maps_repair_fields_for_lead({
            "website": "https://examplecleaning.com",
            "address": "123 Main St, Atlanta, GA 30303",
            "phone": "+1 404-555-1234",
            "google_rating": 4.9,
            "google_review_count": 2,
            "google_reviews": [
                {"author": "A", "text": "Great work"},
                {"author": "B", "text": "Very thorough"},
            ],
            "google_listing_images": _complete_listing_images(),
        })

        self.assertEqual(fields, [])

    def test_maps_repair_only_requests_invalid_detail_rows(self):
        fields = _maps_repair_fields_for_lead({
            "website": "https://examplecleaning.com",
            "address": "examplecleaning.com/contact",
            "phone": "+1 404-555-1234",
            "google_rating": 4.9,
            "google_review_count": 0,
            "google_reviews": [],
            "google_listing_images": _complete_listing_images(),
        })

        self.assertEqual(fields, ["address"])

    def test_maps_repair_does_not_repeat_missing_details_after_check(self):
        fields = _maps_repair_fields_for_lead({
            "website": "",
            "address": "",
            "phone": "",
            "maps_data_repair_checked_fields": ["details"],
            "google_rating": 4.9,
            "google_review_count": 0,
            "google_reviews": [],
            "google_listing_images": _complete_listing_images(),
        })

        self.assertEqual(fields, [])

    def test_maps_repair_requests_reviews_without_details_when_reviews_incomplete(self):
        fields = _maps_repair_fields_for_lead({
            "website": "https://examplecleaning.com",
            "address": "123 Main St, Atlanta, GA 30303",
            "phone": "+1 404-555-1234",
            "google_rating": 4.9,
            "google_review_count": 8,
            "google_reviews": [{"author": "A", "text": "Great work"}],
            "google_listing_images": _complete_listing_images(),
        })

        self.assertEqual(fields, ["reviews"])

    def test_maps_repair_auto_field_normalization(self):
        self.assertEqual(_normalize_maps_repair_fields("auto"), ["auto"])
        self.assertEqual(
            _normalize_maps_repair_fields("details,reviews"),
            ["details", "website", "address", "phone", "reviews"],
        )
        self.assertEqual(_normalize_maps_repair_fields("website,phone"), ["website", "phone"])
        self.assertEqual(_normalize_maps_repair_fields("address"), ["address"])
        self.assertEqual(_normalize_maps_repair_fields("media"), ["media"])
        with self.assertRaises(ValueError):
            _normalize_maps_repair_fields("details,bad")

    def test_maps_repair_requested_fields_constrain_auto_detection(self):
        lead = {
            "website": "https://examplecleaning.com",
            "address": "examplecleaning.com/contact",
            "phone": "+1 404-555-1234",
            "google_rating": 4.9,
            "google_review_count": 8,
            "google_reviews": [{"author": "A", "text": "Great work"}],
            "google_listing_images": _complete_listing_images(),
        }

        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="details"), ["address"])
        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="address"), ["address"])
        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="website"), [])
        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="phone"), [])
        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="reviews"), ["reviews"])
        self.assertEqual(_maps_repair_fields_for_lead(lead, requested_fields="rating"), [])
        self.assertEqual(
            _maps_repair_fields_for_lead(lead, requested_fields="rating", force=True),
            ["rating"],
        )

    def test_maps_repair_requests_media_when_listing_images_incomplete(self):
        fields = _maps_repair_fields_for_lead({
            "website": "https://examplecleaning.com",
            "address": "123 Main St, Atlanta, GA 30303",
            "phone": "+1 404-555-1234",
            "google_rating": 4.9,
            "google_review_count": 0,
            "google_reviews": [],
            "google_listing_images": [{"url": "https://res.cloudinary.com/demo/image/upload/one.jpg"}],
        })

        self.assertEqual(fields, ["media"])

    def test_normalize_raw_lead_keeps_valid_address_and_normalizes_website(self):
        lead, metrics = _normalize_raw_lead_fields({
            "business_name": "Example Salon",
            "address": "123 Main St, Atlanta, GA",
            "website": "example.com",
        })

        self.assertEqual(lead["address"], "123 Main St, Atlanta, GA")
        self.assertEqual(lead["website"], "https://example.com")
        self.assertTrue(lead["has_website"])
        self.assertEqual(lead["website_domain"], "example.com")
        self.assertEqual(metrics["websites_normalized"], 1)

    def test_campaign_scrape_settings_use_campaign_overrides(self):
        settings = _campaign_scrape_settings({
            "scrape_settings": {
                "max_results": 25,
                "dedupe_enabled": False,
                "listing_media_enabled": False,
            }
        })

        self.assertEqual(settings["max_results"], 25)
        self.assertFalse(settings["dedupe_enabled"])
        self.assertFalse(settings["listing_media_enabled"])

    def test_campaign_maps_stats_can_be_derived_from_existing_leads(self):
        campaign = {"stats": {"maps": {"businesses_found": 20}}}
        enriched = _campaign_with_lead_derived_maps_stats(campaign, [
            {"id": "lead1", "has_website": True},
            {"id": "lead2", "website": "https://example.com"},
            {"id": "lead3", "has_website": False},
        ])

        maps = enriched["stats"]["maps"]
        self.assertEqual(maps["businesses_found"], 20)
        self.assertEqual(maps["businesses_persisted"], 3)
        self.assertEqual(maps["with_website"], 2)
        self.assertEqual(maps["missing_website"], 1)
        self.assertEqual(maps["needs_website"], 1)
        self.assertEqual(enriched["stats"]["total"], 3)

    def test_serpapi_payload_extracts_domain(self):
        payload = _organic_result_payload({
            "position": 1,
            "title": "Example Cleaners",
            "link": "https://www.examplecleaners.com/services",
            "snippet": "Cleaning services in Atlanta",
        })

        self.assertEqual(payload["domain"], "examplecleaners.com")
        self.assertEqual(payload["root_domain"], "examplecleaners.com")
        self.assertEqual(payload["position"], 1)
        self.assertGreater(payload["confidence_score"], 0)

    def test_serpapi_normalizes_query_and_root_domain(self):
        self.assertEqual(normalize_search_query("  Hair   Salon ", "Atlanta, GA"), "hair salon in atlanta ga")
        self.assertEqual(_root_domain("https://locations.example.co.uk/atlanta"), "example.co.uk")

    def test_serpapi_filter_excludes_directories(self):
        self.assertFalse(_looks_like_business_owned_result({"domain": "yelp.com"}))
        self.assertFalse(_looks_like_business_owned_result({"domain": "biz.yelp.com", "root_domain": "yelp.com"}))
        self.assertFalse(_looks_like_business_owned_result({"domain": "reddit.com"}))
        self.assertFalse(_looks_like_business_owned_result({"domain": "x.com"}))
        self.assertFalse(_looks_like_business_owned_result({"domain": "twitter.com"}))
        self.assertFalse(_looks_like_business_owned_result({"domain": "facebook.com"}))
        self.assertTrue(_looks_like_business_owned_result({"domain": "examplecleaners.com"}))

    def test_search_evidence_stale_without_refresh_date(self):
        self.assertTrue(is_search_evidence_stale({}, 30))

    def test_search_claim_guardrails_avoid_competitors_without_business_results(self):
        guardrails = _search_claim_guardrails({"business_results": []})

        self.assertIn("Not allowed: name competitors", guardrails)
        self.assertIn("no owned website", guardrails)

    def test_browser_quality_score_penalizes_visible_ux_issues(self):
        score = _browser_quality_score_from_metrics(
            {
                "horizontal_overflow": True,
                "has_visible_h1": False,
                "above_fold_cta_count": 0,
                "low_contrast_text_count": 2,
                "small_tap_target_count": 4,
                "image_failures": 1,
                "lcp_ms": 5200,
                "cls": 0.32,
                "inp_ms": 640,
                "load_ms": 6100,
            },
            {
                "horizontal_overflow": False,
                "has_visible_h1": True,
                "above_fold_cta_count": 1,
                "low_contrast_text_count": 0,
                "small_tap_target_count": 0,
                "image_failures": 0,
                "lcp_ms": 1800,
                "cls": 0.02,
                "inp_ms": None,
                "load_ms": 2400,
            },
        )

        self.assertEqual(score, 0)

    def test_browser_quality_summary_mentions_mobile_and_issues(self):
        summary = _browser_quality_summary_from_metrics(
            {
                "score": 6,
                "above_fold_cta_count": 1,
                "low_contrast_text_count": 0,
                "small_tap_target_count": 1,
                "image_failures": 0,
                "fcp_ms": 850,
                "lcp_ms": 1800,
                "cls": 0.02,
                "inp_ms": None,
                "load_ms": 2400,
            },
            {
                "score": 4,
                "above_fold_cta_count": 0,
                "low_contrast_text_count": 2,
                "small_tap_target_count": 3,
                "image_failures": 1,
                "horizontal_overflow": True,
                "fcp_ms": 1400,
                "lcp_ms": 4100,
                "lcp_element": {
                    "label": "img image: sparkling kitchen",
                    "url": "https://example.com/hero.jpg",
                    "resource": {"duration_ms": 2600},
                },
                "cls": 0.22,
                "inp_ms": 280,
                "load_ms": 6200,
            },
        )

        self.assertIn("desktop: score 6/10", summary)
        self.assertIn("mobile: score 4/10", summary)
        self.assertIn("horizontal overflow", summary)
        self.assertIn("cta not visible above the fold", summary)
        self.assertIn("LCP=4100ms", summary)
        self.assertIn("LCP culprit=img image: sparkling kitchen", summary)
        self.assertIn("CLS=0.22", summary)
        self.assertIn("INP=280ms", summary)

    def test_deterministic_appraisal_uses_browser_quality_findings(self):
        report = appraise_website(
            {
                "title": "Example Cleaners",
                "meta_description": "",
                "h1_tags": [],
                "h2_tags": [],
                "cta_texts": [],
                "body_text": "Professional cleaning for homes and offices.",
                "footer_text": "",
                "browser_quality": {
                    "score": 4,
                    "summary": "mobile: slow largest contentful paint",
                    "mobile": {
                        "lcp_ms": 4100,
                        "lcp_element": {
                            "label": "img image: cleaning team",
                            "url": "https://example.com/team.jpg",
                            "section": {"heading": "Home cleaning services"},
                            "resource": {"duration_ms": 2400},
                        },
                        "cls": 0.16,
                        "above_fold_cta_count": 0,
                    },
                    "desktop": {
                        "lcp_ms": 1800,
                        "cls": 0.02,
                        "above_fold_cta_count": 1,
                    },
                },
            },
            "Example Cleaners",
        )

        self.assertEqual(report["appraisal_method"], "deterministic")
        self.assertEqual(report["browser_quality_score"], 4)
        self.assertTrue(report["findings"])
        self.assertIn("https://example.com/team.jpg", report["weaknesses"][0])
        self.assertIn("Home cleaning services", report["weaknesses"][0])
        self.assertIn("4.1s to show up", report["weaknesses"][0])
        self.assertIn("Slow content: image/media URL https://example.com/team.jpg.", report["findings"][0]["details"])

    def test_deterministic_appraisal_keeps_all_browser_findings_with_contextual_target_copy(self):
        report = appraise_website(
            {
                "title": "Example Cleaners",
                "meta_description": "",
                "h1_tags": [],
                "h2_tags": [],
                "cta_texts": [],
                "body_text": "Professional cleaning for homes and offices.",
                "footer_text": "",
                "browser_quality": {
                    "score": 3,
                    "summary": "multiple browser-quality issues",
                    "mobile": {
                        "lcp_ms": 4100,
                        "cls": 0.16,
                        "above_fold_cta_count": 0,
                        "small_tap_target_count": 4,
                        "small_tap_target_samples": [
                            {
                                "text": "Book now",
                                "width": 32,
                                "height": 30,
                                "section": {"heading": "Hero"},
                            }
                        ],
                        "image_failures": 1,
                        "image_failure_samples": [
                            {
                                "src": "https://example.com/broken.jpg",
                                "alt": "team photo",
                                "section": {"heading": "Gallery"},
                            }
                        ],
                    },
                    "desktop": {
                        "lcp_ms": 3900,
                        "cls": 0.18,
                        "above_fold_cta_count": 0,
                        "small_tap_target_count": 3,
                        "image_failures": 2,
                    },
                },
            },
            "Example Cleaners",
        )

        findings = report["findings"]
        self.assertGreater(len(findings), 5)
        self.assertTrue(any(f["observation"] == "mobile has 4 tap/click targets below 44px" for f in findings))
        desktop_target = next(
            f for f in findings
            if f["observation"] == "desktop has 3 click/tap targets below 44px"
        )
        self.assertIn("touch laptops", desktop_target["business_impact"])
        self.assertNotIn("one-handed", desktop_target["business_impact"])
        mobile_target = next(
            f for f in findings
            if f["observation"] == "mobile has 4 tap/click targets below 44px"
        )
        self.assertIn('Small target: "Book now" (32x30px) in the section headed "Hero".', mobile_target["details"])
        visual_trust = next(f for f in findings if f["category"] == "visual trust")
        self.assertIn("https://example.com/broken.jpg", visual_trust["details"][0])
        self.assertIn("Gallery", visual_trust["details"][0])

    def test_deterministic_appraisal_skips_image_failure_without_actionable_sample(self):
        report = appraise_website(
            {
                "title": "Example Cleaners",
                "meta_description": "Professional cleaning services.",
                "h1_tags": ["Cleaning services"],
                "h2_tags": [],
                "cta_texts": ["Request a quote"],
                "body_text": "Professional cleaning with trusted reviews and reliable local service.",
                "footer_text": "",
                "browser_quality": {
                    "score": 8,
                    "summary": "desktop: failed images",
                    "desktop": {
                        "above_fold_cta_count": 1,
                        "image_failures": 2,
                        "image_failure_samples": [],
                    },
                    "mobile": {
                        "above_fold_cta_count": 1,
                        "image_failures": 0,
                    },
                },
            },
            "Example Cleaners",
        )

        self.assertFalse(any(f["category"] == "visual trust" for f in report["findings"]))

    def test_audit_report_renderer_outputs_all_issues_and_top_two_section(self):
        findings = [
            {
                "category": f"category {index}",
                "observation": f"issue {index}",
                "business_impact": f"impact {index}",
                "recommendation": f"fix {index}",
            }
            for index in range(1, 7)
        ]
        html = render_audit_report_html(
            {
                "slug": "example-cleaners",
                "source_website": "https://example.com",
                "overall_score": 6,
                "copy_score": 5,
                "seo_score": 7,
                "ux_score": 4,
                "has_clear_cta": False,
                "has_social_proof": True,
                "has_clear_value_proposition": False,
                "browser_quality_summary": "mobile: score 4/10",
                "browser_quality": {
                    "mobile": {"score": 4, "lcp_ms": 4100, "cls": 0.12, "above_fold_cta_count": 0},
                    "desktop": {"score": 6, "lcp_ms": 1800, "cls": 0.02, "above_fold_cta_count": 1},
                },
                "findings": findings,
            },
            {"business_name": "Example Cleaners", "website": "https://example.com"},
        )

        self.assertIn("All issues found", html)
        self.assertIn("The two fixes to prioritize", html)
        self.assertIn("Stats from the review", html)
        self.assertIn("Want the highest-impact fixes handled for you?", html)
        self.assertEqual(html.count('class="issue"'), 8)
        self.assertIn("Issue 1", html)
        self.assertIn("Issue 6", html)
        self.assertIn("Primary issue", html)
        self.assertIn("Secondary issue", html)
        self.assertNotIn("Priority 3", html)
        self.assertNotIn("Your audit", html)
        self.assertNotIn("I would prioritize", html)
        self.assertNotIn("This is the full issue list currently stored", html)
        self.assertNotIn("If only two things get fixed first", html)
        self.assertIn("Let us fix that for you.", html)
        self.assertNotIn("before-and-after summary", html)
        self.assertIn("Book a fix call", html)
        self.assertIn("Visit our website", html)

    def test_audit_report_renderer_outputs_finding_details(self):
        html = render_audit_report_html(
            {
                "slug": "example-cleaners",
                "source_website": "https://example.com",
                "overall_score": 6,
                "copy_score": 5,
                "seo_score": 7,
                "ux_score": 4,
                "browser_quality": {"mobile": {}, "desktop": {}},
                "findings": [
                    {
                        "category": "visual trust",
                        "observation": "mobile has 1 failed image",
                        "business_impact": "broken visuals can make the business feel less maintained.",
                        "recommendation": "Replace missing images.",
                        "details": [
                            "Broken image: https://example.com/missing.jpg in the section headed \"Gallery\"."
                        ],
                    }
                ],
            },
            {"business_name": "Example Cleaners", "website": "https://example.com"},
        )

        self.assertIn("Where to look:", html)
        self.assertIn("https://example.com/missing.jpg", html)
        self.assertIn("Gallery", html)

    def test_audit_report_artifact_writes_slug_index_html(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "audit"
            with patch("app.services.audit_report_artifacts.audit_reports_root", return_value=root):
                path = write_audit_report_artifact(
                    {
                        "id": "report-1",
                        "lead_id": "lead-1",
                        "slug": "example-cleaners-audit",
                        "source_website": "https://example.com",
                        "overall_score": 7,
                        "copy_score": 8,
                        "seo_score": 7,
                        "ux_score": 6,
                        "strengths": ["Clear service copy."],
                        "findings": [],
                    },
                    {"business_name": "Example Cleaners"},
                )

            html_path = root / "example-cleaners-audit" / "index.html"
            metadata_path = root / "example-cleaners-audit" / "metadata.json"
            self.assertEqual(path, str(html_path))
            self.assertTrue(html_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertIn("Example Cleaners", html_path.read_text(encoding="utf-8"))

    def test_resolve_audit_static_path_uses_configured_repo_path_for_stale_saved_path(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = base / "websites"
            audit_root = repo_root / "audit"
            current_path = audit_root / "example-cleaners-audit" / "index.html"
            current_path.parent.mkdir(parents=True)
            current_path.write_text("<html></html>", encoding="utf-8")
            stale_path = base / "audit" / "example-cleaners-audit" / "index.html"

            resolved = _resolve_audit_static_path(
                slug="example-cleaners-audit",
                saved_path=str(stale_path),
                repo_root=repo_root.resolve(),
                audit_root=audit_root.resolve(),
            )

            self.assertEqual(resolved, str(current_path.resolve()))

    def test_result_with_audit_publish_marks_completed_failure(self):
        result = _result_with_audit_publish(
            {"status": "completed", "campaign_id": "campaign-1"},
            {"status": "failed", "git": {"error": "working tree is dirty"}},
        )

        self.assertEqual(result["status"], "completed_with_audit_publish_failed")
        self.assertEqual(result["audit_publish_error"], "working tree is dirty")
        self.assertEqual(result["audit_publish"]["status"], "failed")

    def test_reuse_existing_audit_skips_fresh_audit_when_artifact_exists(self):
        with TemporaryDirectory() as directory:
            repo_root = Path(directory) / "websites"
            audit_root = repo_root / "audit"
            artifact_path = audit_root / "example-audit" / "index.html"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text("<html></html>", encoding="utf-8")
            updates = []

            with patch("app.workers.tasks.query_collection", return_value=[
                {
                    "id": "report-1",
                    "lead_id": "lead-1",
                    "slug": "example-audit",
                    "static_report_path": str(artifact_path),
                }
            ]), patch("app.workers.tasks.update_document", side_effect=lambda *args: updates.append(args)), patch(
                "app.services.static_website_generator.static_sites_repo_root",
                return_value=repo_root,
            ), patch("app.workers.tasks.audit_reports_root", return_value=audit_root), patch(
                "app.workers.tasks.write_audit_report_artifact"
            ) as write_artifact:
                result = _reuse_existing_audit_for_lead(
                    {"id": "lead-1", "business_name": "Example Cleaners"},
                    "campaign-1",
                )

            self.assertEqual(result["status"], "reused_local_artifact")
            self.assertEqual(result["report_id"], "report-1")
            write_artifact.assert_not_called()
            self.assertIn(
                (
                    "leads",
                    "lead-1",
                    {
                        "scrape_status": "analyzed",
                        "website_audit_status": "completed",
                        "website_audit_error": None,
                        "latest_audit_report_id": "report-1",
                        "latest_audit_slug": "example-audit",
                    },
                ),
                updates,
            )

    def test_reuse_existing_audit_regenerates_missing_artifact(self):
        with TemporaryDirectory() as directory:
            repo_root = Path(directory) / "websites"
            audit_root = repo_root / "audit"
            regenerated_path = audit_root / "example-audit" / "index.html"
            updates = []

            with patch("app.workers.tasks.query_collection", return_value=[
                {
                    "id": "report-1",
                    "lead_id": "lead-1",
                    "slug": "example-audit",
                    "source_website": "https://example.com",
                }
            ]), patch("app.workers.tasks.update_document", side_effect=lambda *args: updates.append(args)), patch(
                "app.services.static_website_generator.static_sites_repo_root",
                return_value=repo_root,
            ), patch("app.workers.tasks.audit_reports_root", return_value=audit_root), patch(
                "app.workers.tasks.write_audit_report_artifact",
                return_value=str(regenerated_path),
            ) as write_artifact:
                result = _reuse_existing_audit_for_lead(
                    {"id": "lead-1", "business_name": "Example Cleaners"},
                    "campaign-1",
                )

            self.assertEqual(result["status"], "regenerated_local_artifact")
            self.assertEqual(result["static_report_path"], str(regenerated_path))
            write_artifact.assert_called_once()
            self.assertIn(("audit_reports", "report-1", {"static_report_path": str(regenerated_path)}), updates)

    def test_audit_report_renderer_avoids_na_stat_cards_when_browser_metrics_missing(self):
        html = render_audit_report_html(
            {
                "slug": "example-cleaners",
                "source_website": "https://example.com",
                "overall_score": 6,
                "copy_score": 5,
                "seo_score": 7,
                "ux_score": 4,
                "browser_quality": {"mobile": {}, "desktop": {}},
                "findings": [
                    {
                        "category": "copy clarity",
                        "observation": "the page copy does not give enough persuasive context",
                        "business_impact": "visitors may leave without a strong reason to choose you.",
                        "recommendation": "Clarify the customer outcome.",
                    }
                ],
            },
            {"business_name": "Example Cleaners", "website": "https://example.com"},
        )

        stats_section = html.split('<h2 id="evidence">Stats from the review</h2>', 1)[1].split("</section>", 1)[0]
        self.assertNotIn(">n/a<", stats_section)
        self.assertIn("Copy clarity score", stats_section)
        self.assertIn("Search basics score", stats_section)

    def test_audit_email_template_uses_measured_finding(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "page speed",
                        "email_observation": "on mobile, the hero image takes about 4.1s to show up",
                        "business_impact": "that first impression can feel slow before a visitor decides to call or request a quote.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "Your website is slow on mobile")
        self.assertIn("noticed the homepage takes about 4.1s to load on mobile", email["body"])
        self.assertIn("that delay can sometimes mean fewer calls and quote requests", email["body"])
        self.assertIn("I put together a short audit with a few issues I found that may be impacting conversions", email["body"])
        self.assertIn("https://reports.example.com/audit/example-cleaners-atlanta", email["body"])
        self.assertIn("Best regards,\nAndrew Ezeani", email["body"])

    def test_layout_stability_email_template_uses_mobile_website_issue_copy(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "layout stability",
                        "email_observation": "mobile layout shift is 0.28",
                        "business_impact": "moving content can interrupt clicks.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "Issue affecting visitors on your website")
        self.assertIn("may be making it harder for visitors to interact with the site", email["body"])
        self.assertIn("Some content shifts around while the page is loading", email["body"])
        self.assertIn("https://reports.example.com/audit/example-cleaners-atlanta", email["body"])
        self.assertIn("Best regards,\nAndrew Ezeani", email["body"])

    def test_mobile_layout_email_template_uses_mobile_visitor_copy(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "mobile layout",
                        "email_observation": "mobile content overflows horizontally",
                        "business_impact": "visitors may need to scroll sideways before they can act.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "Website issue affecting mobile visitors")
        self.assertIn("may be affecting the experience for visitors", email["body"])
        self.assertIn("Some content extends beyond the width of the screen on mobile devices", email["body"])
        self.assertIn("https://reports.example.com/audit/example-cleaners-atlanta", email["body"])
        self.assertIn("Best regards,\nAndrew Ezeani", email["body"])

    def test_readability_email_template_uses_owner_friendly_copy(self):
        email = generate_audit_email_template(
            business_name="Brazil Master Cleaners LLC",
            audit_data={
                "findings": [
                    {
                        "category": "readability",
                        "email_observation": "mobile has low-contrast text",
                        "business_impact": "important copy can be harder to read.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://teerdoxlabs.vercel.app/",
        )

        self.assertEqual(email["subject"], "Noticed something on your website")
        self.assertEqual(email["email_template_variant"], "A")
        self.assertIn("Hi Brazil Master Cleaners LLC team,", email["body"])
        self.assertIn("text doesn't stand out clearly from the background", email["body"])
        self.assertIn("https://teerdoxlabs.vercel.app/audit/example-cleaners-atlanta", email["body"])
        self.assertIn("Feel free to share the report", email["body"])
        self.assertIn("Best regards,\nAndrew Ezeani", email["body"])

    def test_readability_email_template_can_use_b_variant(self):
        email = generate_audit_email_template(
            business_name="Brazil Master Cleaners LLC",
            audit_data={
                "findings": [
                    {
                        "category": "readability",
                        "email_observation": "mobile has low-contrast text",
                        "business_impact": "important copy can be harder to read.",
                    }
                ]
            },
            slug="brazil-master-cleaners-llc-atlanta-48bb",
            base_url="https://teerdoxlabs.vercel.app/",
        )

        self.assertEqual(email["subject"], "Something affecting your website on mobile")
        self.assertEqual(email["email_template_variant"], "B")
        self.assertIn("mobile readability issue", email["body"])
        self.assertIn("highlighting this and a couple of other improvements", email["body"])
        self.assertIn("https://teerdoxlabs.vercel.app/audit/brazil-master-cleaners-llc-atlanta-48bb", email["body"])
        self.assertIn("Best regards,\nAndrew Ezeani", email["body"])

    def test_gmail_html_body_linkifies_urls(self):
        html = _plain_text_to_html(
            "Report: https://teerdoxlabs.vercel.app/audit/example.\n\nSite: https://www.teerdox.com"
        )

        self.assertIn('<a href="https://teerdoxlabs.vercel.app/audit/example">', html)
        self.assertIn("example</a>.", html)
        self.assertIn('<a href="https://www.teerdox.com">https://www.teerdox.com</a>', html)

    def test_gmail_draft_creation_retries_until_draft_id_returned(self):
        calls = {"count": 0}
        captured = {}

        def fake_create_gmail_draft(**kwargs):
            calls["count"] += 1
            captured.update(kwargs)
            return None if calls["count"] == 1 else "draft-123"

        with patch("app.services.gmail_service.create_gmail_draft", side_effect=fake_create_gmail_draft):
            with patch("app.workers.tasks.time.sleep"):
                draft_id = _create_gmail_draft_with_retries(
                    lead_id="lead-1",
                    draft_doc_id="email-draft-1",
                    to_email="owner@example.com",
                    subject="Hello",
                    body="Body",
                    attempts=2,
                )

        self.assertEqual(draft_id, "draft-123")
        self.assertEqual(calls["count"], 2)
        self.assertIn("Body\n\nOutreach ID: email-draft-1", captured["body"])

    def test_append_draft_reference_is_idempotent(self):
        self.assertEqual(
            _append_draft_reference("Body\n\nOutreach ID: email-draft-1", "email-draft-1"),
            "Body\n\nOutreach ID: email-draft-1",
        )

    def test_gmail_draft_creation_raises_after_retries(self):
        with patch("app.services.gmail_service.create_gmail_draft", return_value=None):
            with patch("app.workers.tasks.time.sleep"):
                with self.assertRaises(RuntimeError):
                    _create_gmail_draft_with_retries(
                        lead_id="lead-1",
                        draft_doc_id="email-draft-1",
                        to_email="owner@example.com",
                        subject="Hello",
                        body="Body",
                        attempts=2,
                    )

    def test_outreach_status_route_replaces_email_draft_status_route(self):
        routes_by_path = {getattr(route, "path", ""): route for route in email_router.routes}
        paths = set(routes_by_path)

        self.assertIn("/outreach/{outreach_id}/status", paths)
        self.assertNotIn("/email-drafts/{draft_id}/status", paths)
        self.assertIs(routes_by_path["/outreach/{outreach_id}/status"].body_field.type_, OutreachStatusUpdateRequest)

    def test_outreach_status_sent_requires_email_platform(self):
        with patch(
            "app.routes.email.get_document",
            return_value={"id": "outreach-1", "lead_id": "lead-1", "status": "drafted"},
        ):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(update_outreach_status(
                    "outreach-1",
                    OutreachStatusUpdateRequest(status=DraftStatus.sent),
                ))

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("email_platform", str(context.exception.detail))

    def test_outreach_status_sent_saves_email_platform(self):
        docs = {
            "outreach-1": {
                "id": "outreach-1",
                "lead_id": "lead-1",
                "status": "drafted",
                "follow_up_interval_days": 3,
                "follow_up_count": 0,
            }
        }
        updates = []

        def fake_get_document(collection, doc_id):
            if collection == "email_drafts":
                return docs.get(doc_id)
            if collection == "leads":
                return {"id": doc_id}
            return None

        def fake_update(_collection, doc_id, payload):
            updates.append(payload)
            docs[doc_id] = {**docs[doc_id], **payload}

        with patch("app.routes.email.get_document", side_effect=fake_get_document), patch(
            "app.routes.email.update_document",
            side_effect=fake_update,
        ), patch("app.routes.email.sync_follow_up_calendar_event", return_value={}), patch(
            "app.routes.email._sync_sent_draft_to_notion",
        ):
            response = asyncio.run(update_outreach_status(
                "outreach-1",
                OutreachStatusUpdateRequest(status=DraftStatus.sent, email_platform="gmail"),
            ))

        self.assertEqual(updates[0]["email_platform"], "gmail")
        self.assertEqual(response.email_platform, "gmail")

    def test_follow_up_request_schema_hides_batch_size(self):
        schema = FollowUpDraftRequest.model_json_schema()

        self.assertIn("limit", schema["properties"])
        self.assertNotIn("batch_size", schema["properties"])
    def test_outreach_status_sent_again_only_updates_email_platform(self):
        docs = {
            "outreach-1": {
                "id": "outreach-1",
                "lead_id": "lead-1",
                "status": "sent",
                "sent_at": "2026-01-01T00:00:00+00:00",
                "follow_up_due_at": "2026-01-04T00:00:00+00:00",
            }
        }
        updates = []

        def fake_get_document(collection, doc_id):
            if collection == "email_drafts":
                return docs.get(doc_id)
            if collection == "leads":
                return {"id": doc_id}
            return None

        def fake_update(_collection, doc_id, payload):
            updates.append(payload)
            docs[doc_id] = {**docs[doc_id], **payload}

        with patch("app.routes.email.get_document", side_effect=fake_get_document), patch(
            "app.routes.email.update_document",
            side_effect=fake_update,
        ), patch("app.routes.email.sync_follow_up_calendar_event") as calendar_sync, patch(
            "app.routes.email._sync_sent_draft_to_notion",
        ):
            response = asyncio.run(update_outreach_status(
                "outreach-1",
                OutreachStatusUpdateRequest(status=DraftStatus.sent, email_platform="zoho"),
            ))

        self.assertEqual(updates, [{"status": "sent", "email_platform": "zoho"}])
        calendar_sync.assert_not_called()
        self.assertEqual(response.email_platform, "zoho")
        self.assertEqual(response.follow_up_due_at, "2026-01-04T00:00:00+00:00")

    def test_outreach_status_closed_syncs_to_notion_without_scheduling(self):
        docs = {
            "outreach-1": {
                "id": "outreach-1",
                "lead_id": "lead-1",
                "status": "sent",
                "notion_page_id": "notion-1",
            }
        }
        updates = []

        def fake_get_document(collection, doc_id):
            if collection == "email_drafts":
                return docs.get(doc_id)
            if collection == "leads":
                return {"id": doc_id, "notion_page_id": "lead-notion-1"}
            return None

        def fake_update(_collection, doc_id, payload):
            updates.append(payload)
            docs[doc_id] = {**docs[doc_id], **payload}

        class FakeNotion:
            def __init__(self):
                self.calls = []

            def sync_outreach(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return "notion-1"

        fake_notion = FakeNotion()

        with patch("app.routes.email.get_document", side_effect=fake_get_document), patch(
            "app.routes.email.update_document",
            side_effect=fake_update,
        ), patch("app.routes.email.sync_follow_up_calendar_event") as calendar_sync, patch(
            "app.routes.email.get_notion_sync",
            return_value=fake_notion,
        ):
            response = asyncio.run(update_outreach_status(
                "outreach-1",
                OutreachStatusUpdateRequest(status=DraftStatus.closed),
            ))

        self.assertEqual(updates, [{"status": "closed"}])
        calendar_sync.assert_not_called()
        self.assertEqual(len(fake_notion.calls), 1)
        self.assertEqual(response.status, DraftStatus.closed)

    def test_outreach_regenerate_route_uses_documented_request_schema(self):
        routes_by_path = {getattr(route, "path", ""): route for route in email_router.routes}

        route = routes_by_path["/campaigns/{campaign_id}/outreach/drafts/regenerate"]
        self.assertIs(route.body_field.type_, OutreachDraftRegenerateRequest)

    def test_selected_website_lead_process_route_uses_documented_request_schema(self):
        routes_by_path = {getattr(route, "path", ""): route for route in audit_router.routes}

        route = routes_by_path["/campaigns/{campaign_id}/website-leads/process"]
        self.assertIs(route.body_field.type_, WebsiteLeadProcessRequest)
        self.assertIs(route.response_model, JobResponse)

    def test_gmail_regeneration_skips_sent_outreach(self):
        self.assertEqual(
            _gmail_regeneration_action(
                {"status": "sent", "gmail_draft_id": "gmail-1"},
                update_gmail=True,
            ),
            "skipped_sent_status",
        )
        self.assertEqual(
            _gmail_regeneration_action(
                {"status": "drafted", "gmail_draft_id": "gmail-1"},
                update_gmail=True,
            ),
            "update",
        )

    def test_follow_up_subject_uses_stage_specific_templates(self):
        lead = {"business_name": "Acme Cleaning"}
        self.assertEqual(
            _follow_up_subject({"workflow_type": "has_website", "subject": "Website audit"}, lead, 1),
            "Re: Website audit",
        )
        self.assertEqual(
            _follow_up_subject({"workflow_type": "has_website"}, lead, 2),
            "Following up: closing the loop on your website audit",
        )
        self.assertEqual(
            _follow_up_subject({"workflow_type": "no_website"}, lead, 1),
            "Following up: website preview for Acme Cleaning",
        )
        self.assertEqual(
            _follow_up_subject({"workflow_type": "no_website"}, lead, 2),
            "Following up: closing the loop on the website preview",
        )

    def test_follow_up_body_uses_preview_or_audit_context(self):
        no_website_body = _follow_up_body(
            {
                "workflow_type": "no_website",
                "generated_website_url": "https://example.com/preview/acme/",
                "email_platform": "zoho",
            },
            {"business_name": "Acme Cleaning"},
            None,
            1,
        )
        audit_body = _follow_up_body(
            {"workflow_type": "has_website", "email_platform": "gmail"},
            {"business_name": "Acme Cleaning"},
            {
                "slug": "acme-audit",
                "findings": [{"category": "readability", "email_observation": "mobile has low-contrast text"}],
            },
            1,
        )

        self.assertIn("https://example.com/preview/acme/", no_website_body)
        self.assertIn("Original email platform: zoho.", no_website_body)
        self.assertIn("I wanted to follow up on the short website audit", audit_body)
        self.assertIn("doesn't stand out clearly from the background", audit_body)
        self.assertIn("/audit/acme-audit", audit_body)
        self.assertIn("Original email platform: gmail.", audit_body)

        self.assertIn("/audit/acme-audit", audit_body)

    def test_append_message_history_replaces_existing_draft_slot(self):
        notes = append_message_history(
            None,
            stage="initial",
            status="sent",
            subject="Initial subject",
            body="Initial body",
            at="2026-01-01T00:00:00+00:00",
        )
        notes = append_message_history(
            notes,
            stage="follow_up_1",
            status="drafted",
            subject="First follow up",
            body="Old draft body",
            at="2026-01-02T00:00:00+00:00",
        )
        notes = append_message_history(
            notes,
            stage="follow_up_1",
            status="regenerated",
            subject="First follow up",
            body="New draft body",
            at="2026-01-03T00:00:00+00:00",
        )

        self.assertIn("--- Initial Email Sent ---", notes)
        self.assertIn("--- Follow Up 1 Draft ---", notes)
        self.assertNotIn("Regenerated Draft", notes)
        self.assertIn("New draft body", notes)
        self.assertNotIn("Old draft body", notes)
        self.assertEqual(notes.count("--- Follow Up 1 Draft ---"), 1)

    def test_append_message_history_replaces_stage_when_sent(self):
        notes = append_message_history(
            None,
            stage="follow_up_1",
            status="drafted",
            subject="Follow up draft",
            body="Draft body",
            at="2026-01-02T00:00:00+00:00",
        )
        notes = append_message_history(
            notes,
            stage="follow_up_1",
            status="sent",
            subject="Follow up sent",
            body="Sent body",
            at="2026-01-03T00:00:00+00:00",
        )

        self.assertIn("--- Follow Up 1 Sent ---", notes)
        self.assertNotIn("--- Follow Up 1 Draft ---", notes)
        self.assertIn("Sent body", notes)
        self.assertNotIn("Draft body", notes)

    def test_regenerate_uses_follow_up_template_for_same_row_follow_up_stage(self):
        lead = {"business_name": "Acme Cleaning"}
        draft = {
            "id": "outreach-1",
            "workflow_type": "has_website",
            "stage": "follow_up_1",
            "follow_up_count": 1,
            "subject": "Re: Website audit",
            "email_platform": "gmail",
        }

        with patch("app.workers.tasks._report_for_outreach", return_value={
            "slug": "acme-audit",
            "findings": [{"category": "readability", "email_observation": "mobile has low-contrast text"}],
        }):
            result = _regenerated_outreach_email_content(draft, lead, object())

        self.assertEqual(result["subject"], "Re: Website audit")
        self.assertIn("I wanted to follow up on the short website audit", result["body"])
        self.assertIn("would you like my help with the highest-impact fix?", result["body"])
        self.assertIn("Original email platform: gmail.", result["body"])

    def test_generate_due_follow_up_drafts_sets_second_interval_on_follow_up_1_draft(self):
        updates = []

        def fake_query_collection(collection, filters=None, limit=50, offset=0, order_by=None, direction="ASCENDING"):
            if collection == "email_drafts" and filters and ("status", "==", "sent") in filters:
                return [{
                    "id": "outreach-1",
                    "lead_id": "lead-1",
                    "campaign_id": "campaign-1",
                    "report_id": "report-1",
                    "report_type": "audit",
                    "subject": "Website audit",
                    "body": "Initial body",
                    "status": "sent",
                    "workflow_type": "has_website",
                    "stage": "initial",
                    "follow_up_count": 0,
                    "follow_up_due_at": "2026-01-01T00:00:00+00:00",
                    "email_platform": "gmail",
                    "sent_at": "2026-01-01T00:00:00+00:00",
                }]
            return []

        def fake_get_document(collection, doc_id):
            if collection == "leads":
                return {"id": doc_id, "business_name": "Acme Cleaning", "emails": ["owner@example.com"]}
            if collection == "audit_reports":
                return {"id": doc_id, "slug": "acme-audit"}
            return None

        def fake_update(_collection, doc_id, payload):
            updates.append((doc_id, payload))

        class FakeNotion:
            def sync_outreach(self, *args, **kwargs):
                return None

        fake_settings = SimpleNamespace(
            outreach_follow_up_interval_days=3,
            outreach_follow_up_2_interval_days=7,
            outreach_max_follow_ups=2,
            base_url="https://teerdoxlabs.vercel.app",
        )

        with patch("app.workers.tasks.query_collection", side_effect=fake_query_collection), patch(
            "app.workers.tasks.get_document",
            side_effect=fake_get_document,
        ), patch("app.workers.tasks.get_settings", return_value=fake_settings), patch(
            "app.workers.tasks._create_gmail_draft_with_retries",
            return_value="gmail-follow-up-1",
        ), patch(
            "app.workers.tasks.update_document",
            side_effect=fake_update,
        ), patch("app.workers.tasks.get_notion_sync", return_value=FakeNotion()), patch(
            "app.workers.tasks.sync_follow_up_calendar_event",
            return_value={},
        ):
            result = _generate_due_follow_up_drafts(None, None, limit=10, batch_size=10)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(updates[0][1]["follow_up_interval_days"], 7)

    def test_follow_up_draft_updates_existing_outreach_row(self):
        updates = []

        def fake_query_collection(collection, filters=None, limit=50, offset=0, order_by=None, direction="ASCENDING"):
            if collection == "email_drafts":
                if filters and ("status", "==", "sent") in filters:
                    return [{
                        "id": "outreach-1",
                        "lead_id": "lead-1",
                        "campaign_id": "campaign-1",
                        "report_id": "report-1",
                        "report_type": "audit",
                        "subject": "Website audit",
                        "body": "Initial body",
                        "status": "sent",
                        "workflow_type": "has_website",
                        "stage": "initial",
                        "follow_up_count": 0,
                        "follow_up_due_at": "2026-01-01T00:00:00+00:00",
                        "email_platform": "gmail",
                        "sent_at": "2026-01-01T00:00:00+00:00",
                    }]
                return []
            return []

        def fake_get_document(collection, doc_id):
            if collection == "leads":
                return {"id": doc_id, "business_name": "Acme Cleaning", "emails": ["owner@example.com"]}
            if collection == "audit_reports":
                return {"id": doc_id, "slug": "acme-audit"}
            return None

        def fake_update(_collection, doc_id, payload):
            updates.append((doc_id, payload))

        class FakeNotion:
            def sync_outreach(self, *args, **kwargs):
                return None

        with patch("app.workers.tasks.query_collection", side_effect=fake_query_collection), patch(
            "app.workers.tasks.get_document",
            side_effect=fake_get_document,
        ), patch(
            "app.workers.tasks._create_gmail_draft_with_retries",
            return_value="gmail-follow-up-1",
        ), patch(
            "app.workers.tasks.update_document",
            side_effect=fake_update,
        ), patch("app.workers.tasks.get_notion_sync", return_value=FakeNotion()), patch(
            "app.workers.tasks.sync_follow_up_calendar_event",
            return_value={},
        ):
            result = _generate_due_follow_up_drafts(None, None, limit=10, batch_size=10)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(updates[0][0], "outreach-1")
        self.assertEqual(updates[0][1]["gmail_draft_id"], "gmail-follow-up-1")
        self.assertEqual(updates[0][1]["status"], "drafted")
        self.assertEqual(updates[0][1]["stage"], "follow_up_1")
        self.assertEqual(updates[0][1]["follow_up_count"], 1)
        self.assertIsNone(updates[0][1]["follow_up_due_at"])
        self.assertIn("--- Initial Email Sent ---", updates[0][1]["notes"])
        self.assertIn("--- Follow Up 1 Draft ---", updates[0][1]["notes"])

    def test_sent_follow_up_updates_uses_second_interval_for_follow_up_2(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with patch("app.routes.email.get_settings", return_value=SimpleNamespace(
            outreach_follow_up_interval_days=3,
            outreach_follow_up_2_interval_days=7,
            outreach_max_follow_ups=2,
        )):
            updates = _sent_follow_up_updates({"follow_up_count": 1}, now)

        self.assertEqual(updates["follow_up_due_at"], "2026-01-08T00:00:00+00:00")
        self.assertEqual(updates["follow_up_interval_days"], 7)

    def test_sent_follow_up_updates_schedule_next_due_date(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        updates = _sent_follow_up_updates({"follow_up_interval_days": 4}, now)

        self.assertEqual(updates["follow_up_count"], 0)
        self.assertEqual(updates["reply_status"], "no_reply")
        self.assertEqual(updates["stage"], "initial")
        self.assertEqual(updates["follow_up_due_at"], "2026-01-05T00:00:00+00:00")

    def test_calendar_event_window_uses_configured_local_hour(self):
        settings = SimpleNamespace(
            follow_up_event_timezone="Africa/Lagos",
            follow_up_event_hour=9,
        )
        due_at = datetime(2026, 1, 5, 23, tzinfo=timezone.utc)

        with patch("app.services.google_calendar_service.get_settings", return_value=settings):
            start, end = _event_window(due_at)

        self.assertEqual(start.isoformat(), "2026-01-06T09:00:00+01:00")
        self.assertEqual(end.isoformat(), "2026-01-06T09:30:00+01:00")

    def test_calendar_event_body_includes_follow_up_context(self):
        settings = SimpleNamespace(
            follow_up_event_timezone="Africa/Lagos",
            follow_up_event_hour=9,
            follow_up_reminder_minutes=1440,
        )
        due_at = datetime(2026, 1, 5, tzinfo=timezone.utc)

        with patch("app.services.google_calendar_service.get_settings", return_value=settings):
            event = _calendar_event_body(
                outreach_id="outreach-1",
                outreach={
                    "lead_id": "lead-1",
                    "campaign_id": "campaign-1",
                    "subject": "Website idea",
                    "stage": "initial",
                    "reply_status": "no_reply",
                    "generated_website_url": "https://example.com/preview/acme/",
                },
                lead={"business_name": "Acme Cleaning"},
                due_at=due_at,
            )

        self.assertEqual(event["summary"], "Follow up: Acme Cleaning")
        self.assertIn("Outreach ID: outreach-1", event["description"])
        self.assertIn("Generated website: https://example.com/preview/acme/", event["description"])
        self.assertEqual(event["reminders"]["overrides"][0]["minutes"], 1440)

    def test_ai_disabled_campaign_mode_only_processes_has_website_leads(self):
        ai_enabled = SimpleNamespace(ai_processing_enabled=True)
        ai_disabled = SimpleNamespace(ai_processing_enabled=False)

        self.assertEqual(_campaign_processing_mode(ai_enabled), "full_ai")
        self.assertEqual(_campaign_processing_mode(ai_disabled), "heuristic_has_website")
        self.assertTrue(_should_process_lead_in_mode({"has_website": True}, "heuristic_has_website"))
        self.assertFalse(_should_process_lead_in_mode({"has_website": False}, "heuristic_has_website"))
        self.assertFalse(_should_process_lead_in_mode({"has_website": False}, "full_ai"))

    def test_outreach_rewrite_is_disabled_when_global_ai_is_off(self):
        settings = SimpleNamespace(
            ai_processing_enabled=False,
            outreach_ai_rewrite_enabled=True,
        )
        original = {"subject": "Subject", "body": "Body"}

        self.assertEqual(
            _maybe_rewrite_outreach_email(settings, "Acme", original, "facts only"),
            original,
        )

    def test_generated_preview_cleanup_reason_uses_closed_outreach_status_only(self):
        self.assertEqual(
            _generated_preview_cleanup_reason({
                "generated_website_slug": "acme",
                "needs_website": True,
            }, has_closed_outreach_status=True),
            "outreach_status_closed",
        )
        self.assertEqual(
            _generated_preview_cleanup_reason({}, has_closed_outreach_status=True),
            "outreach_status_closed",
        )
        self.assertIsNone(_generated_preview_cleanup_reason({
            "generated_website_slug": "acme",
        }))

    def test_has_closed_outreach_detects_terminal_status(self):
        self.assertTrue(_has_closed_outreach([
            {"status": "sent"},
            {"status": "closed"},
        ]))
        self.assertFalse(_has_closed_outreach([
            {"status": "drafted"},
            {"status": "sent"},
        ]))

    def test_lead_static_cleanup_paths_include_generated_and_audit_artifacts(self):
        paths = _lead_static_cleanup_paths(
            {
                "generated_website_slug": "acme",
                "generated_website_path": "",
            },
            [
                ("audit_reports", {"slug": "acme-audit", "static_report_path": ""}),
                ("no_website_reports", {"slug": "ignore-me", "static_report_path": ""}),
            ],
            websites_root=lambda: Path("/tmp/websites/preview"),
            audit_reports_root=lambda: Path("/tmp/websites/audit"),
        )

        self.assertEqual(
            [Path(path).as_posix() for path in paths],
            ["/tmp/websites/preview/acme", "/tmp/websites/audit/acme-audit"],
        )

    def test_audit_email_template_handles_conversion_path_scenario(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "conversion path",
                        "email_observation": "on mobile, there is no clear CTA visible above the fold",
                        "business_impact": "ready-to-contact visitors may not see the next step immediately.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "quick website note")
        self.assertIn("the next step for visitors is not very obvious", email["body"])
        self.assertIn("make the quote/contact path clearer", email["body"])

    def test_audit_email_template_handles_mobile_usability_scenario(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "mobile usability",
                        "email_observation": "mobile has 3 tap/click targets below 44px",
                        "business_impact": "visitors may miss key actions when trying to tap on mobile.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "Mobile issue affecting bookings on your website")
        self.assertIn(
            "may be affecting how easily visitors complete key actions like booking, calling, or contacting your business",
            email["body"],
        )
        self.assertIn(
            "Some buttons and clickable elements appear too small or tightly spaced on mobile devices",
            email["body"],
        )
        self.assertIn("https://reports.example.com/audit/example-cleaners-atlanta", email["body"])
        self.assertIn("help you prioritize the highest-impact fixes first", email["body"])

    def test_audit_email_template_handles_trust_scenario(self):
        email = generate_audit_email_template(
            business_name="Example Cleaners",
            audit_data={
                "findings": [
                    {
                        "category": "trust",
                        "email_observation": "the page does not show obvious trust signals",
                        "business_impact": "new visitors have less evidence that you are safe to contact.",
                    }
                ]
            },
            slug="example-cleaners-atlanta",
            base_url="https://reports.example.com",
        )

        self.assertEqual(email["subject"], "website trust")
        self.assertIn("could use stronger trust signals", email["body"])
        self.assertIn("more credible to first-time visitors", email["body"])

    def test_security_verification_page_detection(self):
        self.assertTrue(is_security_verification_page({
            "title": "Just a moment...",
            "body_text": (
                "This website uses a security service to protect against malicious bots. "
                "Ray ID: abc123 Performance and Security by Cloudflare"
            ),
        }))
        self.assertTrue(is_security_verification_page({
            "title": "Access check",
            "h2_tags": ["Performing security verification"],
            "body_text": "",
        }))
        self.assertFalse(is_security_verification_page({
            "title": "Professional House Cleaning Services",
            "h1_tags": ["Cleaning that gives you your weekend back"],
            "body_text": "Book your cleaning online today.",
        }))

    def test_campaign_audit_summary_counts_endpoint_statuses(self):
        summary = CampaignAuditSummary()

        for status_value in [
            "completed",
            "skipped_existing",
            "skipped_no_website",
            "blocked_by_security",
            "failed",
        ]:
            _count_result(summary, status_value)

        self.assertEqual(summary.eligible, 4)
        self.assertEqual(summary.audited, 1)
        self.assertEqual(summary.skipped_existing, 1)
        self.assertEqual(summary.skipped_no_website, 1)
        self.assertEqual(summary.blocked_by_security, 1)
        self.assertEqual(summary.failed, 1)

    def test_no_website_appraisal_uses_search_evidence_without_ai(self):
        report = appraise_no_website(
            business_name="Example Cleaners",
            niche="cleaners",
            location="Atlanta",
            search_evidence={
                "query": "cleaners in atlanta",
                "business_results": [
                    {
                        "title": "First Cleaner",
                        "domain": "firstcleaner.com",
                        "link": "https://firstcleaner.com",
                    }
                ],
            },
        )

        self.assertEqual(report["appraisal_method"], "deterministic")
        self.assertEqual(report["competitors"][0]["website"], "https://firstcleaner.com")
        self.assertIn("no owned website", report["findings"][0]["observation"])

    def test_no_website_email_template_references_generated_site(self):
        email = generate_no_website_email_template(
            business_name="Example Cleaners",
            location="Atlanta",
            niche="cleaners",
            no_website_data={
                "search_evidence": {"query": "cleaners in atlanta"},
                "competitors": [
                    {"name": "First Cleaner"},
                    {"name": "Second Cleaner"},
                    {"name": "Third Cleaner"},
                ],
                "findings": [
                    {
                        "observation": "the business has no owned website",
                        "business_impact": "searchers may only see third-party listings.",
                    }
                ]
            },
            generated_website_url="https://sites.example.com/example-cleaners/",
        )

        self.assertEqual(email["subject"], "website idea")
        self.assertIn("came across Example Cleaners on Google Maps", email["body"])
        self.assertIn("First Cleaner, Second Cleaner, and Third Cleaner", email["body"])
        self.assertIn("I couldn't find a website for your business in those results.", email["body"])
        self.assertIn("https://sites.example.com/example-cleaners/", email["body"])
        self.assertIn("has Maps been handling that well enough so far?", email["body"])

    def test_search_results_text_formats_top_business_urls_for_notion(self):
        text = _search_results_text({
            "organic_results": [
                {"domain": "yelp.com", "link": "https://www.yelp.com/biz/example"},
                {"domain": "first.example.com", "link": "https://first.example.com"},
                {"domain": "reddit.com", "link": "https://www.reddit.com/r/example"},
                {"domain": "second.example.com", "link": "https://second.example.com"},
                {"domain": "facebook.com", "link": "https://www.facebook.com/example"},
                {"domain": "third.example.com", "link": "https://third.example.com"},
                {"domain": "fourth.example.com", "link": "https://fourth.example.com"},
            ],
        })

        self.assertEqual(
            text,
            "https://first.example.com\nhttps://second.example.com\nhttps://third.example.com",
        )

    def test_notion_campaign_sync_writes_summary_metrics(self):
        notion = NotionSync()
        notion.enabled = True
        notion.settings.notion_campaigns_db_id = "db1"
        notion._schemas["db1"] = {
            "Name": "title",
            "Campaign ID": "rich_text",
            "Status": "select",
            "Businesses Found": "number",
            "Businesses Persisted": "number",
            "With Website": "number",
            "Missing Website": "number",
            "Needs Website": "number",
            "Websites Published": "number",
            "Outreach Drafts": "number",
            "Audits Completed": "number",
            "Audit Failures": "number",
        }
        captured = {}

        def fake_upsert(_database_id, properties, page_id=None):
            captured["properties"] = properties
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        notion.sync_campaign("campaign-1", {
            "name": "Cleaners in Atlanta",
            "status": "completed",
            "stats": {
                "maps": {
                    "businesses_found": 30,
                    "businesses_persisted": 25,
                    "with_website": 15,
                    "missing_website": 10,
                    "needs_website": 4,
                },
                "generated_websites": {
                    "published": 3,
                    "outreach_generated": 3,
                },
                "website_audits": {
                    "completed": 12,
                    "failed": 2,
                    "failures": 4,
                },
            },
        })

        self.assertEqual(captured["properties"]["Needs Website"]["number"], 4)
        self.assertEqual(captured["properties"]["Websites Published"]["number"], 3)
        self.assertEqual(captured["properties"]["Outreach Drafts"]["number"], 3)
        self.assertEqual(captured["properties"]["Audits Completed"]["number"], 12)
        self.assertEqual(captured["properties"]["Audit Failures"]["number"], 4)

    def test_notion_search_evidence_sync_matches_trimmed_schema(self):
        notion = NotionSync()
        notion.enabled = True
        notion.search_evidence_enabled = True
        notion.settings.notion_search_evidence_db_id = "db1"
        notion._schemas["db1"] = {
            "Name": "title",
            "Campaign": "relation",
            "Query": "rich_text",
            "Provider": "select",
            "Status": "select",
            "Generated At": "date",
            "Organic Results Count": "number",
            "Business Results Count": "number",
            "Top Results": "rich_text",
        }
        captured = {}

        def fake_upsert(_database_id, properties, page_id=None):
            captured["properties"] = properties
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        notion.sync_search_evidence("campaign-1", {
            "query": "cleaners in atlanta",
            "provider": "serpapi",
            "status": "completed",
            "generated_at": "2026-06-17T12:00:00+00:00",
            "organic_results_count": 8,
            "business_results_count": 3,
            "organic_results": [
                {"domain": "first.example.com", "link": "https://first.example.com"},
            ],
        })

        self.assertIn("Top Results", captured["properties"])
        self.assertNotIn("Raw JSON", captured["properties"])

    def test_notion_properties_skip_wrong_column_type(self):
        notion = NotionSync()
        notion._schemas["db1"] = {
            "Name": "title",
            "Raw JSON": "url",
            "Count": "number",
        }

        properties = notion._properties("db1", {
            "Name": notion._title("Search evidence"),
            "Raw JSON": notion._rich_text("{}"),
            "Count": notion._number(3),
        })

        self.assertIn("Name", properties)
        self.assertIn("Count", properties)
        self.assertNotIn("Raw JSON", properties)

    def test_notion_properties_log_missing_column(self):
        notion = NotionSync()
        notion._schemas["db1"] = {
            "Name": "title",
        }

        with self.assertLogs("app.services.notion_service", level="WARNING") as logs:
            properties = notion._properties("db1", {
                "Name": notion._title("Campaign"),
                "Businesses Persisted": notion._number(10),
            })

        self.assertIn("Name", properties)
        self.assertNotIn("Businesses Persisted", properties)
        self.assertIn("property is missing", "\n".join(logs.output))

    def test_notion_schema_metadata_includes_select_and_status_options(self):
        metadata = _database_property_metadata({
            "Name": {"type": "title", "title": {}},
            "Status": {
                "type": "select",
                "select": {
                    "options": [
                        {"name": "drafted"},
                        {"name": "sent"},
                    ],
                },
            },
            "Reply Status": {
                "type": "status",
                "status": {
                    "options": [
                        {"name": "no_reply"},
                        {"name": "replied"},
                    ],
                },
            },
        })

        self.assertEqual(metadata["Name"], {"type": "title"})
        self.assertEqual(metadata["Status"]["options"], ["drafted", "sent"])
        self.assertEqual(metadata["Reply Status"]["options"], ["no_reply", "replied"])

    def test_notion_report_sync_uses_workflow_type_property(self):
        notion = NotionSync()
        notion.enabled = True
        notion.settings.notion_reports_db_id = "db1"
        notion._schemas["db1"] = {
            "Name": "title",
            "Report ID": "rich_text",
            "Lead": "relation",
            "Workflow Type": "select",
            "Status": "select",
            "Report Url": "url",
            "Overall Score": "number",
            "Primary Issue": "rich_text",
            "Created At": "date",
        }
        captured = {}

        def fake_upsert(_database_id, properties, page_id=None):
            captured["properties"] = properties
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        notion.sync_report("report-1", "audit", {
            "slug": "example-audit",
            "report_url": "https://example.com/audit/example-audit",
            "overall_score": 7,
            "audit_status": "completed",
            "findings": [{"observation": "The main call to action is hard to find."}],
            "created_at": "2026-06-17T12:00:00+00:00",
        })

        self.assertIn("Workflow Type", captured["properties"])
        self.assertNotIn("Type", captured["properties"])
        self.assertNotIn("Slug", captured["properties"])
        self.assertEqual(captured["properties"]["Workflow Type"]["select"]["name"], "audit")
        self.assertEqual(captured["properties"]["Status"]["select"]["name"], "completed")
        self.assertEqual(captured["properties"]["Report Url"]["url"], "https://example.com/audit/example-audit")
        self.assertEqual(
            captured["properties"]["Primary Issue"]["rich_text"][0]["text"]["content"],
            "The main call to action is hard to find.",
        )

    def test_notion_report_sync_skips_no_website_reports(self):
        notion = NotionSync()
        notion.enabled = True
        notion.settings.notion_reports_db_id = "db1"
        notion._schemas["db1"] = {"Name": "title"}

        called = False

        def fake_upsert(_database_id, properties, page_id=None):
            nonlocal called
            called = True
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        result = notion.sync_report("report-1", "no_website", {"slug": "example-no-web"})

        self.assertIsNone(result)
        self.assertFalse(called)

    def test_notion_lead_address_can_be_cleared(self):
        notion = NotionSync()
        notion._schemas["db1"] = {
            "Name": "title",
            "Address": "rich_text",
        }

        properties = notion._properties("db1", {
            "Name": notion._title("Lead"),
            "Address": notion._rich_text_clearable(None),
        })

        self.assertEqual(properties["Address"], {"rich_text": []})

    def test_notion_lead_properties_extract_email_updates(self):
        notion = NotionSync()
        updates = notion.lead_updates_from_properties({
            "Name": {"type": "title", "title": [{"plain_text": "Example Cleaners"}]},
            "Address": {"type": "rich_text", "rich_text": [{"plain_text": "123 Main St, Atlanta, GA 30303"}]},
            "Email": {"type": "email", "email": "owner@example.com"},
            "Emails": {"type": "rich_text", "rich_text": [{"plain_text": "ops@example.com\nowner@example.com"}]},
            "Team Members": {"type": "rich_text", "rich_text": [{"plain_text": "Jane Smith - Owner\nAlex Jones - Manager"}]},
            "Phone": {"type": "phone_number", "phone_number": "(404) 555-1234"},
            "Website": {"type": "url", "url": "https://example.com"},
            "needs_website": {"type": "checkbox", "checkbox": True},
        })

        self.assertEqual(updates["business_name"], "Example Cleaners")
        self.assertEqual(updates["address"], "123 Main St, Atlanta, GA 30303")
        self.assertEqual(updates["phone"], "(404) 555-1234")
        self.assertEqual(updates["emails"], ["owner@example.com", "ops@example.com"])
        self.assertTrue(updates["has_email"])
        self.assertEqual(updates["team_members"], ["Jane Smith - Owner", "Alex Jones - Manager"])
        self.assertEqual(updates["website"], "https://example.com")
        self.assertTrue(updates["has_website"])
        self.assertEqual(updates["website_domain"], "example.com")
        self.assertTrue(updates["needs_website"])

    def test_notion_lead_properties_extract_generated_website_url(self):
        notion = NotionSync()
        updates = notion.lead_updates_from_properties({
            "Generated website url": {"type": "url", "url": "https://example.com/"},
        })

        self.assertEqual(updates["generated_website_url"], "https://example.com/")

    def test_notion_lead_sync_uses_generated_website_url(self):
        notion = NotionSync()
        notion.enabled = True
        notion.settings.notion_leads_db_id = "db1"
        notion._schemas["db1"] = {
            "Name": "title",
            "Lead ID": "rich_text",
            "Campaign": "relation",
            "Address": "rich_text",
            "Phone": "phone_number",
            "Website": "url",
            "Email": "email",
            "Emails": "rich_text",
            "Team Members": "rich_text",
            "Generated website url": "url",
            "Google Maps URL": "url",
            "Has Website": "checkbox",
            "Has Email": "checkbox",
            "needs_website": "checkbox",
        }

        captured = {}

        def fake_upsert(_database_id, properties, page_id=None):
            captured["properties"] = properties
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        notion.sync_lead("lead-1", {
            "business_name": "Example Cleaners",
            "generated_website_url": "https://example.com/",
            "team_members": ["Jane Smith - Owner", "Alex Jones - Manager"],
        })

        self.assertIn("Generated website url", captured["properties"])
        self.assertEqual(captured["properties"]["Generated website url"]["url"], "https://example.com/")
        self.assertEqual(
            captured["properties"]["Team Members"]["rich_text"][0]["text"]["content"],
            "Jane Smith - Owner\nAlex Jones - Manager",
        )

    def test_email_extractor_returns_team_members(self):
        emails, team_members = _parse_html("""
            <html>
              <body>
                <a href="mailto:hello@example.com">Email</a>
                <section>
                  <h2>Team</h2>
                  <p>Jane Smith</p>
                  <p>Owner</p>
                  <p>Alex Jones - Operations Manager</p>
                </section>
              </body>
            </html>
        """)

        self.assertEqual(emails, ["hello@example.com"])
        self.assertIn("Jane Smith - Owner", team_members)
        self.assertIn("Alex Jones - Operations Manager", team_members)

    def test_email_extractor_uses_scraped_page_data(self):
        extracted = extract_emails_and_team_members_from_page_data({
            "body_text": "Jane Smith\nOwner\nEmail hello@acmecleaning.com",
            "footer_text": "Alex Jones\nOperations Manager",
            "links": ["mailto:ops@acmecleaning.com"],
        })

        self.assertEqual(extracted["emails"], ["hello@acmecleaning.com", "ops@acmecleaning.com"])
        self.assertIn("Jane Smith - Owner", extracted["team_members"])
        self.assertIn("Alex Jones - Operations Manager", extracted["team_members"])

    def test_email_extractor_rejects_non_official_platform_domains(self):
        extracted = extract_emails_and_team_members_from_page_data({
            "body_text": "Contact hello@heavenscentatl.com or support@bookingkoala.com",
            "links": ["mailto:owner@getjobber.com", "mailto:team@example.edu"],
        })

        self.assertEqual(extracted["emails"], ["hello@heavenscentatl.com"])

    def test_audit_contact_refresh_filters_existing_platform_emails(self):
        updates = []

        class FakeNotion:
            def sync_lead(self, lead_id, lead, campaign_notion_page_id=None):
                self.lead_id = lead_id
                self.lead = lead
                self.campaign_notion_page_id = campaign_notion_page_id
                return "notion-lead-1"

        fake_notion = FakeNotion()

        def fake_update(collection, doc_id, payload):
            updates.append((collection, doc_id, payload))

        def fake_get_document(collection, doc_id):
            if collection == "campaigns":
                return {"id": doc_id, "notion_page_id": "notion-campaign-1"}
            return None

        with patch("app.routes.audits.update_document", side_effect=fake_update), patch(
            "app.routes.audits.get_document",
            side_effect=fake_get_document,
        ), patch("app.routes.audits.get_notion_sync", return_value=fake_notion):
            lead = _update_lead_contact_fields_from_page_data(
                "campaign-1",
                {
                    "id": "lead-1",
                    "emails": ["support@bookingkoala.com", "hello@heavenscentatl.com"],
                    "team_members": [],
                },
                {
                    "body_text": "Contact owner@heavenscentatl.com or support@getjobber.com",
                    "links": ["mailto:team@example.edu"],
                },
            )

        self.assertEqual(lead["emails"], ["hello@heavenscentatl.com", "owner@heavenscentatl.com"])
        self.assertTrue(lead["has_email"])
        self.assertEqual(fake_notion.lead["emails"], ["hello@heavenscentatl.com", "owner@heavenscentatl.com"])
        self.assertIn(("leads", "lead-1", {"notion_page_id": "notion-lead-1"}), updates)

    def test_outreach_report_lookup_falls_back_to_latest_lead_audit(self):
        def fake_get_document(collection, doc_id):
            if collection == "audit_reports" and doc_id == "new-report":
                return {"id": "new-report", "slug": "new-slug"}
            return None

        with patch("app.workers.tasks.get_document", side_effect=fake_get_document):
            report = _report_for_outreach(
                {"report_id": "old-report", "report_type": "audit"},
                {"id": "lead-1", "latest_audit_report_id": "new-report"},
            )

        self.assertEqual(report["id"], "new-report")

    def test_notion_outreach_sync_uses_notes_not_separate_message_columns(self):
        notion = NotionSync()
        notion.enabled = True
        notion.settings.notion_outreach_db_id = "db1"
        notion._schemas["db1"] = {
            "Name": "title",
            "Outreach ID": "rich_text",
            "Status": "select",
            "Workflow Type": "select",
            "Reply Status": "select",
            "Email Platform": "select",
            "Notes": "rich_text",
            "Stage": "select",
            "Subject": "rich_text",
            "Body": "rich_text",
        }

        captured = {}

        def fake_upsert(_database_id, properties, page_id=None):
            captured["properties"] = properties
            return "page-123"

        notion._upsert_page = fake_upsert  # type: ignore[method-assign]

        notion.sync_outreach("outreach-1", {
            "lead_id": "lead-123",
            "subject": "Quick note",
            "body": "Body copy",
            "status": "drafted",
            "workflow_type": "no_website",
            "email_template_variant": "B",
            "stage": "initial",
            "reply_status": "no_reply",
            "email_platform": "gmail",
        })

        self.assertIn("Notes", captured["properties"])
        self.assertEqual(captured["properties"]["Email Platform"]["select"]["name"], "gmail")
        self.assertEqual(captured["properties"]["Stage"]["select"]["name"], "initial")
        self.assertIn("Template Variant: B", captured["properties"]["Notes"]["rich_text"][0]["text"]["content"])
        self.assertIn("Subject: Quick note", captured["properties"]["Notes"]["rich_text"][0]["text"]["content"])
        self.assertNotIn("Lead ID: lead-123", captured["properties"]["Notes"]["rich_text"][0]["text"]["content"])
        self.assertNotIn("Subject", captured["properties"])
        self.assertNotIn("Body", captured["properties"])

    def test_notion_lead_properties_ignore_invalid_address_updates(self):
        notion = NotionSync()
        updates = notion.lead_updates_from_properties({
            "Address": {"type": "rich_text", "rich_text": [{"plain_text": "LGBTQ+ friendly"}]},
        })

        self.assertNotIn("address", updates)

    def test_static_website_candidate_requires_needs_website_only(self):
        self.assertTrue(eligible_for_static_website({
            "needs_website": True,
        }))
        self.assertTrue(eligible_for_static_website({
            "emails": [],
            "needs_website": True,
        }))
        self.assertTrue(eligible_for_static_website({
            "emails": ["owner@example.com"],
            "needs_website": True,
            "has_website": True,
            "website": "https://example.com",
        }))
        self.assertFalse(eligible_for_static_website({
            "emails": ["owner@example.com"],
            "needs_website": False,
            "has_website": False,
            "website": None,
        }))

    def test_static_website_slug_uses_business_name(self):
        self.assertEqual(_slugify("Atlanta Eco Cleaners, LLC"), "atlanta-eco-cleaners-llc")

    def test_static_website_logo_removes_generic_words(self):
        self.assertEqual(_logo_text("White Rabbit Cleaning LLC"), "White Rabbit")
        self.assertEqual(_logo_text("Atlanta Premier Residential Cleaning Services LLC"), "APR")

    def test_static_website_preview_url_points_to_landing(self):
        self.assertEqual(
            _local_preview_url("white-rabbit-cleaning-llc", "http://localhost:8000/"),
            "http://localhost:8000/preview/white-rabbit-cleaning-llc/",
        )

    def test_static_website_service_cta_stays_below_service_text(self):
        context = _site_context(
            {"id": "lead-1", "business_name": "White Rabbit Cleaning LLC"},
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        css = _landing_css(context)

        self.assertIn(".service-grid h3{grid-column:2", css)
        self.assertIn(".service-grid p{grid-column:2", css)
        self.assertIn(".service-grid a{grid-column:2", css)
        self.assertIn(".service-grid h3,.service-grid p,.service-grid a{grid-column:1}", css)

    def test_static_website_generates_home_and_contact_pages_only(self):
        context = _site_context(
            {"id": "lead-1", "business_name": "White Rabbit Cleaning LLC"},
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "services.html").write_text("stale", encoding="utf-8")
            (root / "css").mkdir()
            (root / "css" / "services.css").write_text("stale", encoding="utf-8")
            files = _write_site(root, context)

            self.assertIn("index.html", files)
            self.assertIn("contact.html", files)
            self.assertNotIn("services.html", files)
            self.assertNotIn("css/services.css", files)
            self.assertFalse((root / "services.html").exists())
            self.assertFalse((root / "css" / "services.css").exists())

    def test_remove_generated_website_path_only_removes_inside_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "websites"
            site_dir = root / "example-cleaners"
            site_dir.mkdir(parents=True)
            (site_dir / "index.html").write_text("hello", encoding="utf-8")

            with patch("app.services.static_website_generator.websites_root", return_value=root):
                self.assertTrue(remove_generated_website_path(site_dir))
                self.assertFalse(site_dir.exists())

    def test_static_website_services_nav_points_to_landing_section(self):
        context = _site_context(
            {"id": "lead-1", "business_name": "White Rabbit Cleaning LLC"},
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertIn('href="index.html#services">Services</a>', html)
        self.assertNotIn('href="services.html">Services</a>', html)
        self.assertNotIn("Move-in / move-out cleaning", html)
        self.assertNotIn("gallery-section", html)
        self.assertNotIn("masonry", html)

    def test_static_website_gallery_uses_google_listing_images(self):
        listing_images = [
            {
                "url": f"https://res.cloudinary.com/example/image/upload/work-{index}.jpg",
                "width": 408 if index % 2 else 202,
                "height": 306 if index % 2 else 270,
            }
            for index in range(1, 7)
        ]
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "google_listing_images": listing_images,
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertIn("work-1.jpg", html)
        self.assertIn("work-2.jpg", html)
        self.assertIn('class="gallery-frame landscape frame-1"', html)
        self.assertIn('class="gallery-frame portrait frame-2"', html)

    def test_static_website_gallery_requires_at_least_six_listing_images(self):
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "google_listing_images": [
                    {
                        "url": f"https://res.cloudinary.com/example/image/upload/work-{index}.jpg",
                        "width": 408,
                        "height": 306,
                    }
                    for index in range(1, 6)
                ],
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertNotIn("gallery-section", html)
        self.assertNotIn("work-1.jpg", html)

    def test_static_website_uses_service_area_when_address_missing(self):
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "address": "",
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        contact_html = _contact_html(context)

        self.assertIn("Service area: Atlanta", contact_html)
        self.assertNotIn("<iframe", contact_html)

    def test_static_website_uses_google_rating_and_reviews(self):
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "google_maps_url": "https://maps.google.com/place",
                "google_rating": 4.7,
                "google_review_count": 31,
                "google_reviews": [
                    {
                        "author": "Real Customer\nLocal Guide ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· 12 reviews",
                        "text": "They made the office feel clean without disrupting our team.",
                        "url": "https://maps.google.com/review",
                        "avatar_url": "https://res.cloudinary.com/example/avatar.jpg",
                    },
                    {
                        "author": "Real Customer",
                        "text": "They made the office feel clean without disrupting our team.",
                        "url": "#",
                    }
                ],
                "google_listing_images": [
                    {
                        "url": f"https://res.cloudinary.com/example/image/upload/work-{index}.jpg",
                        "width": 408,
                        "height": 306,
                    }
                    for index in range(1, 7)
                ],
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertIn("4.7 Google rating from 31 reviews", html)
        self.assertIn("They made the office feel clean without disrupting our team.", html)
        self.assertIn("Real Customer", html)
        self.assertNotIn("Local Guide", html)
        self.assertEqual(html.count("They made the office feel clean without disrupting our team."), 1)
        self.assertIn("https://res.cloudinary.com/example/avatar.jpg", html)
        self.assertNotIn("4.9 rating placeholder", html)
        self.assertNotIn("See review", html)
        self.assertIn('href="https://maps.google.com/place"', html)
        self.assertIn("View more reviews", html)
        self.assertIn("See more photos", html)

    def test_static_website_review_grid_uses_three_cards_when_it_fits(self):
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "google_reviews": [
                    {"author": "One", "text": "First detailed review."},
                    {"author": "Two", "text": "Second detailed review."},
                    {"author": "Three", "text": "Third detailed review."},
                ],
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertIn("review-count-3", html)
        self.assertEqual(html.count("review-card"), 3)
        self.assertEqual(html.count('class="review-card span-third"'), 3)

    def test_static_website_review_grid_avoids_orphan_cards(self):
        context = _site_context(
            {
                "id": "lead-1",
                "business_name": "White Rabbit Cleaning LLC",
                "google_reviews": [
                    {"author": "One", "text": "First detailed review."},
                    {"author": "Two", "text": "Second detailed review."},
                    {"author": "Three", "text": "Third detailed review."},
                    {"author": "Four", "text": "Fourth detailed review."},
                ],
            },
            {"niche": "cleaners", "location": "Atlanta"},
            "white-rabbit-cleaning-llc",
        )
        html = _landing_html(context)

        self.assertIn("review-count-4", html)
        self.assertEqual(html.count("review-card"), 4)
        self.assertEqual(html.count('class="review-card span-third"'), 3)
        self.assertIn('class="review-card span-full"', html)


if __name__ == "__main__":
    unittest.main()
