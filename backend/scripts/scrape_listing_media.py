"""Scrape Google Maps listing photos/videos for one lead and save Cloudinary URLs.

Usage:
    python scripts/scrape_listing_media.py LEAD_ID

This is a standalone verification script. It does not run the campaign scraper.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from firebase_admin import firestore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.firebase import LEADS, get_document, update_document  # noqa: E402
from app.services.maps_media_scraper import (  # noqa: E402
    delete_cloudinary_media,
    scrape_upload_listing_media,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Google Maps listing media for a lead and upload it to Cloudinary."
    )
    parser.add_argument("lead_id", help="Firestore lead document ID.")
    parser.add_argument(
        "--max-images",
        type=int,
        default=12,
        help="Maximum listing images to upload. Hard-capped at 12.",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=1,
        help="Maximum listing videos to upload. Hard-capped at 1.",
    )
    parser.add_argument(
        "--clear-existing",
        action="store_true",
        help="Delete existing Cloudinary listing media for the lead before scraping again.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    lead = get_document(LEADS, args.lead_id)
    if not lead:
        logging.error("Lead %s was not found in Firestore.", args.lead_id)
        return 1

    maps_url = (lead.get("google_maps_url") or "").strip()
    if not maps_url:
        logging.error("Lead %s has no google_maps_url.", args.lead_id)
        return 1

    if args.clear_existing:
        existing_media = [
            *(lead.get("google_listing_images") or []),
            *(lead.get("google_listing_videos") or []),
        ]
        delete_summary = delete_cloudinary_media(existing_media)
        update_document(LEADS, args.lead_id, {
            "google_listing_images": [],
            "google_listing_videos": [],
            "google_listing_source_image_urls": [],
            "google_listing_source_video_urls": [],
            "google_listing_media_status": "cleared",
            "google_listing_media_error": None,
            "google_listing_media_updated_at": firestore.SERVER_TIMESTAMP,
        })
        logging.info(
            "Cleared existing Cloudinary listing media for lead %s: deleted=%d failed=%d",
            args.lead_id,
            delete_summary["deleted"],
            delete_summary["failed"],
        )

    try:
        result = await scrape_upload_listing_media(
            lead_id=args.lead_id,
            business_name=lead.get("business_name") or "",
            google_maps_url=maps_url,
            max_images=args.max_images,
            max_videos=args.max_videos,
        )
    except Exception as exc:
        update_document(LEADS, args.lead_id, {
            "google_listing_media_status": "failed",
            "google_listing_media_error": str(exc),
            "google_listing_media_updated_at": firestore.SERVER_TIMESTAMP,
        })
        logging.exception("Lead %s media scrape failed: %s", args.lead_id, exc)
        return 1

    payload: dict[str, Any] = {
        "google_listing_images": result.images,
        "google_listing_videos": result.videos,
        "google_listing_source_image_urls": result.source_image_urls,
        "google_listing_source_video_urls": result.source_video_urls,
        "google_listing_media_status": result.status,
        "google_listing_media_error": result.error,
        "google_listing_media_updated_at": firestore.SERVER_TIMESTAMP,
    }
    update_document(LEADS, args.lead_id, payload)

    logging.info(
        "Lead %s updated: status=%s images=%d videos=%d",
        args.lead_id,
        result.status,
        len(result.images),
        len(result.videos),
    )
    for index, image in enumerate(result.images, start=1):
        logging.info("Image %d: %s", index, image.get("url"))
    for index, video in enumerate(result.videos, start=1):
        logging.info("Video %d: %s", index, video.get("url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
