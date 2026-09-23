"""Standalone Google Maps listing media extraction helpers.

This module is intentionally separate from the main Maps scraper. It revisits a
saved listing URL, extracts listing-owned media, uploads it to Cloudinary, and
returns the hosted URLs that can be stored on the lead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright

from app.config import get_settings
from app.services.maps_scraper import (
    _USER_AGENT,
    _USER_DATA_DIR,
    _dismiss_consent_if_present,
    _is_google_blocked,
)

logger = logging.getLogger(__name__)


@dataclass
class ListingMediaResult:
    images: list[dict[str, Any]]
    videos: list[dict[str, Any]]
    source_image_urls: list[str]
    source_video_urls: list[str]
    status: str
    error: str | None = None


def delete_cloudinary_media(media_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Delete previously uploaded Cloudinary resources from stored lead media metadata."""
    _validate_cloudinary_settings()
    summary = {"deleted": 0, "failed": 0, "errors": []}
    for item in media_items:
        public_id = item.get("cloudinary_public_id")
        if not public_id:
            continue
        resource_type = item.get("resource_type") or "image"
        try:
            _delete_cloudinary_resource(public_id, resource_type=resource_type)
            summary["deleted"] += 1
            logger.info("[Maps Media] Deleted Cloudinary %s: %s", resource_type, public_id)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append({"public_id": public_id, "error": str(exc)})
            logger.warning(
                "[Maps Media] Cloudinary delete failed for %s (%s): %s",
                public_id,
                resource_type,
                exc,
            )
    return summary


def cloudinary_configured() -> bool:
    """Return True when Cloudinary settings needed for upload/delete are present."""
    try:
        _validate_cloudinary_settings()
        return True
    except RuntimeError:
        return False


def upload_review_avatars(
    *,
    lead_id: str,
    business_name: str,
    reviews: list[dict[str, Any]],
    campaign_id: str | None = None,
) -> list[dict[str, Any]]:
    """Upload reviewer avatar source URLs to Cloudinary and return updated reviews."""
    if not reviews:
        return []
    _validate_cloudinary_settings()
    settings = get_settings()
    log_context = _log_context(lead_id, business_name, campaign_id)
    updated: list[dict[str, Any]] = []
    for index, review in enumerate(reviews, start=1):
        if not isinstance(review, dict):
            continue
        item = dict(review)
        source_url = str(item.get("avatar_source_url") or item.get("avatar_url") or "").strip()
        if not source_url:
            logger.info(
                "[Maps Media] %s: review %d has no reviewer avatar source URL.",
                log_context,
                index,
            )
            updated.append(item)
            continue
        if item.get("avatar_url") and "res.cloudinary.com" in str(item.get("avatar_url")):
            updated.append(item)
            continue
        try:
            logger.info(
                "[Maps Media] %s: uploading reviewer avatar %d/%d to Cloudinary: %s",
                log_context,
                index,
                len(reviews),
                _short_url(source_url),
            )
            upload = _upload_remote_media(
                source_url,
                resource_type="image",
                folder=f"{settings.cloudinary_maps_media_folder}/{lead_id}/review-avatars",
                public_id=f"avatar_{index:02d}",
            )
            item["avatar_url"] = upload.get("secure_url") or upload.get("url")
            item["avatar_cloudinary_public_id"] = upload.get("public_id")
            logger.info(
                "[Maps Media] %s: uploaded reviewer avatar %d/%d: %s",
                log_context,
                index,
                len(reviews),
                item.get("avatar_url"),
            )
        except Exception as exc:
            logger.warning(
                "[Maps Media] %s: reviewer avatar %d upload failed: %s",
                log_context,
                index,
                exc,
            )
        updated.append(item)
    return updated


async def scrape_upload_listing_media(
    *,
    lead_id: str,
    business_name: str,
    google_maps_url: str,
    max_images: int = 12,
    max_videos: int = 1,
    campaign_id: str | None = None,
) -> ListingMediaResult:
    """Extract listing media from Google Maps and upload selected media to Cloudinary."""
    log_context = _log_context(lead_id, business_name, campaign_id)
    if not google_maps_url:
        logger.info("[Maps Media] %s skipped: missing Google Maps URL.", log_context)
        return ListingMediaResult([], [], [], [], "missing_google_maps_url")

    _validate_cloudinary_settings()
    max_images = max(0, min(max_images, 12))
    max_videos = max(0, min(max_videos, 1))
    settings = get_settings()

    logger.info(
        "[Maps Media] %s: opening Google Maps listing: %s",
        log_context,
        google_maps_url,
    )

    async with async_playwright() as pw:
        context: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=_USER_DATA_DIR,
            headless=settings.playwright_headless,
            user_agent=_USER_AGENT,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )
        page: Page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        try:
            await page.goto(google_maps_url, wait_until="domcontentloaded", timeout=60_000)
            await _dismiss_consent_if_present(page)
            if await _is_google_blocked(page):
                logger.warning("[Maps Media] %s: Google block/CAPTCHA detected.", log_context)
                return ListingMediaResult([], [], [], [], "blocked")

            try:
                await page.wait_for_selector("h1.DUwDvf", timeout=15_000)
            except Exception:
                logger.warning("[Maps Media] %s: listing heading was not found.", log_context)

            image_urls = await _collect_listing_image_urls(
                page,
                max_images=max_images,
                log_context=log_context,
            )
            video_urls = await _collect_listing_video_urls(
                page,
                max_videos=max_videos,
                allow_panel=len(image_urls) < max_images,
                log_context=log_context,
            )

            logger.info(
                "[Maps Media] %s: collected %d accepted image URL(s), %d video URL(s).",
                log_context,
                len(image_urls),
                len(video_urls),
            )
        finally:
            await context.close()

    uploaded_images = _upload_media_urls(
        lead_id=lead_id,
        business_name=business_name,
        campaign_id=campaign_id,
        media_urls=image_urls,
        resource_type="image",
        folder=f"{settings.cloudinary_maps_media_folder}/{lead_id}/images",
    )
    uploaded_videos = _upload_media_urls(
        lead_id=lead_id,
        business_name=business_name,
        campaign_id=campaign_id,
        media_urls=video_urls,
        resource_type="video",
        folder=f"{settings.cloudinary_maps_media_folder}/{lead_id}/videos",
    )

    status = "uploaded" if uploaded_images or uploaded_videos else "no_media_found"
    logger.info(
        "[Maps Media] %s: uploaded %d image(s), %d video(s) to Cloudinary.",
        log_context,
        len(uploaded_images),
        len(uploaded_videos),
    )
    return ListingMediaResult(
        images=uploaded_images,
        videos=uploaded_videos,
        source_image_urls=image_urls,
        source_video_urls=video_urls,
        status=status,
    )


async def _collect_listing_image_urls(page: Page, max_images: int, log_context: str) -> list[str]:
    urls = await _visible_google_image_urls(page, log_context=log_context)
    if len(urls) < max_images:
        try:
            opened = await _open_photos_panel(page, log_context=log_context)
            if opened:
                urls = _dedupe_urls([
                    *urls,
                    *await _scroll_and_collect_images(page, max_images, log_context),
                ])
                await _close_media_panel(page)
        except PlaywrightError as exc:
            logger.warning(
                "[Maps Media] %s: photo panel image scan failed after %d accepted image URL(s); "
                "keeping partial media. Error: %s",
                log_context,
                len(urls),
                exc,
            )
        except Exception as exc:
            logger.warning(
                "[Maps Media] %s: photo panel image scan failed after %d accepted image URL(s); "
                "keeping partial media. Error: %s",
                log_context,
                len(urls),
                exc,
            )
    return urls[:max_images]


async def _collect_listing_video_urls(
    page: Page,
    max_videos: int,
    log_context: str,
    allow_panel: bool = True,
) -> list[str]:
    if max_videos <= 0:
        return []
    urls = await _visible_video_urls(page)
    if len(urls) < max_videos and allow_panel:
        opened = await _open_photos_panel(page, log_context=log_context)
        if opened:
            urls = _dedupe_urls([*urls, *await _scroll_and_collect_videos(page, max_videos)])
            await _close_media_panel(page)
    elif len(urls) < max_videos:
        logger.info(
            "[Maps Media] %s: skipped extra video panel scan because image target was already met.",
            log_context,
        )
    return urls[:max_videos]


async def _open_photos_panel(page: Page, log_context: str) -> bool:
    selectors = [
        "button[jsaction*='heroHeaderImage']",
        "button[aria-label*='Photo']",
        "button[aria-label*='photo']",
        "button[aria-label*='Photos']",
        "button[aria-label*='photos']",
        "button:has(img[src*='googleusercontent.com'])",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                await locator.click(timeout=2_500)
                await _wait_for_maps_stable(page, log_context=log_context)
                logger.info("[Maps Media] Opened listing photos panel with selector: %s", selector)
                return True
        except Exception as exc:
            logger.debug("[Maps Media] Photos panel selector failed (%s): %s", selector, exc)
    logger.info("[Maps Media] Could not open listing photos panel; using visible media only.")
    return False


async def _scroll_and_collect_images(page: Page, max_images: int, log_context: str) -> list[str]:
    urls: list[str] = []
    stable_rounds = 0
    for round_index in range(18):
        before = len(urls)
        try:
            urls = _dedupe_urls([*urls, *await _visible_google_image_urls(page, log_context=log_context)])
        except PlaywrightError as exc:
            logger.warning(
                "[Maps Media] %s: image scan round %d failed after %d accepted image URL(s); "
                "keeping partial media. Error: %s",
                log_context,
                round_index + 1,
                len(urls),
                exc,
            )
            break
        logger.info(
            "[Maps Media] %s: image scan round %d accepted %d/%d image URL(s).",
            log_context,
            round_index + 1,
            len(urls),
            max_images,
        )
        if len(urls) >= max_images:
            break
        await _scroll_media_panel(page, log_context=log_context)
        await page.wait_for_timeout(900)
        stable_rounds = stable_rounds + 1 if len(urls) == before else 0
        if stable_rounds >= 5:
            break
    return urls[:max_images]


async def _scroll_and_collect_videos(page: Page, max_videos: int) -> list[str]:
    urls: list[str] = []
    stable_rounds = 0
    for _ in range(6):
        before = len(urls)
        urls = _dedupe_urls([*urls, *await _visible_video_urls(page)])
        if len(urls) >= max_videos:
            break
        await _scroll_media_panel(page)
        await page.wait_for_timeout(700)
        stable_rounds = stable_rounds + 1 if len(urls) == before else 0
        if stable_rounds >= 3:
            break
    return urls[:max_videos]


async def _safe_page_evaluate(
    page: Page,
    script: str,
    *,
    log_context: str | None = None,
    action: str = "evaluate page script",
    retries: int = 2,
) -> Any:
    """Run page.evaluate with recovery for Maps SPA context swaps."""
    context = f"{log_context}: " if log_context else ""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await page.evaluate(script)
        except PlaywrightError as exc:
            last_error = exc
            message = str(exc)
            if "Execution context was destroyed" not in message and "navigation" not in message:
                raise
            logger.info(
                "[Maps Media] %s%s interrupted by Maps navigation/context reset; retry %d/%d.",
                context,
                action,
                attempt + 1,
                retries,
            )
            await _wait_for_maps_stable(page, log_context=log_context)
        except Exception:
            raise
    if last_error:
        raise last_error
    return None


async def _wait_for_maps_stable(page: Page, log_context: str | None = None) -> None:
    context = f"{log_context}: " if log_context else ""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        logger.debug("[Maps Media] %sdomcontentloaded wait timed out while stabilizing Maps.", context)
    try:
        await page.wait_for_load_state("networkidle", timeout=3_000)
    except Exception:
        logger.debug("[Maps Media] %snetworkidle wait timed out while stabilizing Maps.", context)
    await page.wait_for_timeout(900)


async def _visible_google_image_urls(page: Page, log_context: str) -> list[str]:
    urls = await _safe_page_evaluate(
        page,
        """
        () => {
          const urls = [];
          for (const img of document.images) {
            const src = img.currentSrc || img.src || '';
            if (src) urls.push(src);
          }
          for (const el of document.querySelectorAll('*')) {
            const bg = getComputedStyle(el).backgroundImage || '';
            for (const match of bg.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)) {
              urls.push(match[1]);
            }
          }
          return urls;
        }
        """,
        log_context=log_context,
        action="collect visible image URLs",
    )
    if not urls:
        return []
    accepted = []
    for raw_url in urls:
        url = str(raw_url or "").strip()
        accepted_media, reason = _listing_image_rejection_reason(url)
        if accepted_media:
            accepted.append(url)
            logger.info(
                "[Maps Media] %s: accepted listing image candidate: %s",
                log_context,
                _short_url(url),
            )
        elif reason:
            logger.info(
                "[Maps Media] %s: discarded image candidate (%s): %s",
                log_context,
                reason,
                _short_url(url),
            )
    return _dedupe_urls(accepted)


async def _scroll_media_panel(page: Page, log_context: str | None = None) -> None:
    try:
        await _safe_page_evaluate(
            page,
            """
            () => {
              const scrollables = Array.from(document.querySelectorAll('*'))
                .filter(el => {
                  const style = getComputedStyle(el);
                  return /(auto|scroll)/.test(style.overflowY)
                    && el.scrollHeight > el.clientHeight + 20;
                })
                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
              for (const el of scrollables.slice(0, 4)) {
                el.scrollBy(0, Math.max(700, Math.floor(el.clientHeight * 0.85)));
              }
              window.scrollBy(0, 900);
            }
            """,
            log_context=log_context,
            action="scroll media panel",
        )
    except Exception:
        await page.mouse.wheel(0, 1000)


async def _visible_video_urls(page: Page) -> list[str]:
    urls = await _safe_page_evaluate(
        page,
        """
        () => Array.from(document.querySelectorAll('video, video source'))
          .map(el => el.currentSrc || el.src || el.getAttribute('src') || '')
          .filter(Boolean)
        """,
        action="collect visible video URLs",
    )
    if not urls:
        return []
    return _dedupe_urls(
        url
        for url in urls
        if str(url).startswith("http")
    )


async def _close_media_panel(page: Page) -> None:
    for selector in (
        "button[aria-label='Back']",
        "button[aria-label*='Back to']",
        "button[aria-label='Close']",
        "button[aria-label*='Close']",
    ):
        try:
            button = page.locator(selector).first
            if await button.count():
                await button.click(timeout=1_500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
    except Exception:
        pass


def _looks_like_listing_image_url(url: str) -> bool:
    accepted, _reason = _listing_image_rejection_reason(url)
    return accepted


def _listing_image_rejection_reason(url: str) -> tuple[bool, str | None]:
    if not url.startswith("http"):
        return False, "not_http"
    lowered = url.lower()
    if "googleusercontent.com" not in lowered:
        return False, "not_googleusercontent"
    if re.search(r"googleusercontent\.com/(?:a-|a/)", lowered):
        return False, "google_profile_avatar"
    blocked_fragments = (
        "maps/vt",
        "marker",
        "searchbox",
        "streetviewpixels",
        "profile_mask",
        "transparent",
        "gen_204",
        "-rp-",
        "rp-mo",
        "br100",
    )
    blocked_fragment = next((fragment for fragment in blocked_fragments if fragment in lowered), None)
    if blocked_fragment:
        return False, f"blocked_fragment:{blocked_fragment}"
    if "/gps-cs-s/" not in lowered and "/p/" not in lowered:
        return False, "not_listing_photo_family"
    width, height = _google_image_dimensions(url)
    if width and height and (width < 180 or height < 140):
        return False, f"too_small:{width}x{height}"
    if width and height and width == height and width <= 300:
        return False, f"small_square:{width}x{height}"
    return True, None


def _dedupe_urls(urls: Any) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url:
            continue
        key = _media_url_key(url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(url)
    return deduped


def _media_url_key(url: str) -> str:
    return re.sub(r"=[whs]\d+.*$", "", url.split("?")[0])


def _google_image_dimensions(url: str) -> tuple[int | None, int | None]:
    width = height = None
    width_match = re.search(r"(?:=|-)w(\d+)", url)
    height_match = re.search(r"(?:=|-)h(\d+)", url)
    square_match = re.search(r"(?:=|-)s(\d+)", url)
    if width_match:
        width = int(width_match.group(1))
    if height_match:
        height = int(height_match.group(1))
    if square_match and not width and not height:
        width = height = int(square_match.group(1))
    return width, height


def _upload_media_urls(
    *,
    lead_id: str,
    business_name: str,
    campaign_id: str | None,
    media_urls: list[str],
    resource_type: str,
    folder: str,
) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    log_context = _log_context(lead_id, business_name, campaign_id)
    for index, media_url in enumerate(media_urls, start=1):
        try:
            logger.info(
                "[Maps Media] %s: uploading %s %d/%d to Cloudinary: %s",
                log_context,
                resource_type,
                index,
                len(media_urls),
                _short_url(media_url),
            )
            upload = _upload_remote_media(
                media_url,
                resource_type=resource_type,
                folder=folder,
                public_id=f"{resource_type}_{index:02d}",
            )
            uploaded.append({
                "url": upload.get("secure_url") or upload.get("url"),
                "cloudinary_public_id": upload.get("public_id"),
                "resource_type": upload.get("resource_type") or resource_type,
                "format": upload.get("format"),
                "width": upload.get("width"),
                "height": upload.get("height"),
                "duration": upload.get("duration"),
                "source_url": media_url,
            })
            logger.info(
                "[Maps Media] %s: uploaded %s %d/%d: %s",
                log_context,
                resource_type,
                index,
                len(media_urls),
                uploaded[-1].get("url"),
            )
        except Exception as exc:
            logger.warning(
                "[Maps Media] %s: %s upload %d failed: %s",
                log_context,
                resource_type,
                index,
                exc,
            )
    return uploaded


def _log_context(lead_id: str, business_name: str, campaign_id: str | None = None) -> str:
    campaign_part = f"Campaign {campaign_id} " if campaign_id else ""
    name_part = business_name or "unknown business"
    return f"{campaign_part}Lead {lead_id} ({name_part})"


def _short_url(url: str, limit: int = 180) -> str:
    if len(url) <= limit:
        return url
    return f"{url[:limit]}..."


def _upload_remote_media(
    media_url: str,
    *,
    resource_type: str,
    folder: str,
    public_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    endpoint = f"https://api.cloudinary.com/v1_1/{settings.cloudinary_cloud_name}/{resource_type}/upload"
    response = requests.post(
        endpoint,
        auth=(settings.cloudinary_api_key, settings.cloudinary_api_secret),
        data={
            "file": media_url,
            "folder": folder,
            "public_id": public_id,
            "overwrite": "true",
            "resource_type": resource_type,
        },
        timeout=90,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Cloudinary upload failed ({response.status_code}): {response.text[:500]}")
    return response.json()


def _delete_cloudinary_resource(public_id: str, *, resource_type: str) -> dict[str, Any]:
    settings = get_settings()
    endpoint = f"https://api.cloudinary.com/v1_1/{settings.cloudinary_cloud_name}/{resource_type}/destroy"
    response = requests.post(
        endpoint,
        auth=(settings.cloudinary_api_key, settings.cloudinary_api_secret),
        data={
            "public_id": public_id,
            "invalidate": "true",
        },
        timeout=45,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Cloudinary delete failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    result = payload.get("result")
    if result not in {"ok", "not found"}:
        raise RuntimeError(f"Cloudinary delete returned {result!r}: {payload}")
    return payload


def _validate_cloudinary_settings() -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("CLOUDINARY_CLOUD_NAME", settings.cloudinary_cloud_name),
            ("CLOUDINARY_API_KEY", settings.cloudinary_api_key),
            ("CLOUDINARY_API_SECRET", settings.cloudinary_api_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing Cloudinary environment variable(s): " + ", ".join(missing)
        )
