"""
Google Maps scraper using Playwright.

Strategy (based on proven working approach):
- Use a persistent browser context (user_data_dir) so cookies/consent persist
  across runs — avoids the consent.google.com hang on every cold start.
- Navigate to google.com/maps first, handle consent if needed, then use
  the search input rather than navigating directly to a search URL.
- Scroll the feed, click each article card, extract structured data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from app.config import get_settings
from app.services.website_filters import is_non_official_website
from app.workers.brand_filter import is_excluded_brand

logger = logging.getLogger(__name__)

# Persistent browser profile stored alongside the project so cookies survive
# restarts and Google doesn't re-show the consent page on every run.
_USER_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "user_data")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Field helpers (from the working reference script)
# ---------------------------------------------------------------------------

def _is_phone(text: str) -> bool:
    if not re.search(r"\d", text):
        return False
    digits = re.sub(r"\D", "", text)
    if not (7 <= len(digits) <= 15):
        return False
    return bool(re.search(r"[\d\-\+\(\)\s]{7,}", text))


def _is_website(text: str) -> bool:
    return bool(re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", text))


def _is_own_website(text: str) -> bool:
    return _is_website(text) and not is_non_official_website(text)


_NON_ADDRESS_DETAIL_MARKERS = (
    "identifies as",
    "identifies as black-owned",
    "identifies as women-owned",
    "women-owned",
    "black-owned",
    "latino-owned",
    "asian-owned",
    "lgbtq",
    "transgender",
    "wheelchair accessible",
    "online appointments",
    "online estimates",
    "onsite services",
    "on-site services",
    "appointment required",
    "service options",
    "amenities",
    "crowd",
    "from the business",
    "payments",
    "hours",
    "open 24 hours",
    "open 24",
)


def _normalize_detail_text(text: str) -> str:
    return re.sub(r"[\u2010-\u2015]", "-", (text or "").strip().lower())


def _is_valid_address(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    lowered = _normalize_detail_text(text)
    if any(marker in lowered for marker in _NON_ADDRESS_DETAIL_MARKERS):
        return False
    if _is_website(text) or _is_phone(text):
        return False
    address_markers = (
        " st", " street", " ave", " avenue", " rd", " road", " blvd", " boulevard",
        " dr", " drive", " ln", " lane", " ct", " court", " cir", " circle",
        " pkwy", " parkway", " hwy", " highway", " way", " suite", " ste ",
        " unit", " apt", ",",
    )
    has_digit = bool(re.search(r"\d", text))
    has_state_zip = bool(re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text))
    return has_state_zip or (has_digit and any(marker in f" {lowered} " for marker in address_markers))


def _parse_fields(data: List[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "business_name": None,
        "address": None,
        "phone": None,
        "website": None,
    }
    if not data:
        return result

    result["business_name"] = data[-1]  # name is always appended last

    # Maps uses the same Io6YTe class for address, phone, and website rows.
    # Some profiles omit the address, so classify each detail row by content
    # instead of assuming the first row is always an address.
    for item in data[:-1]:
        item = item.strip()
        if not item:
            continue
        if result["phone"] is None and _is_phone(item):
            result["phone"] = item
            continue
        if _is_website(item):
            if result["website"] is None and _is_own_website(item):
                result["website"] = item
            continue
        if result["address"] is None and _is_valid_address(item):
            result["address"] = item

    return result


# ---------------------------------------------------------------------------
# Public scrape function
# ---------------------------------------------------------------------------

async def scrape_google_maps(
    niche: str,
    location: str,
    max_results: Optional[int] = None,
    cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    """
    Scrape Google Maps for businesses matching ``niche`` in ``location``.
    Returns a list of dicts: business_name, address, phone, website, google_maps_url.
    """
    businesses, _metrics = await scrape_google_maps_with_metrics(
        niche=niche,
        location=location,
        max_results=max_results,
        cancel_check=cancel_check,
    )
    return businesses


async def repair_google_maps_listing_details(
    listings: List[Dict[str, Any]],
    cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    """Revisit saved Google Maps listing URLs and re-extract requested fields."""
    settings = get_settings()
    repaired: List[Dict[str, Any]] = []

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
            total_listings = len(listings)
            for index, listing in enumerate(listings, start=1):
                await _check_cancelled(cancel_check, {})
                lead_id = listing.get("lead_id")
                business_name = listing.get("business_name") or ""
                maps_url = (listing.get("google_maps_url") or "").strip()
                repair_fields = set(listing.get("repair_fields") or ["details", "rating", "reviews"])
                logger.info(
                    "[Website Repair] Checking lead %s (%s/%s): %s fields=%s",
                    lead_id,
                    index,
                    total_listings,
                    business_name,
                    ",".join(sorted(repair_fields)),
                )
                if not maps_url:
                    logger.info(
                        "[Website Repair] Lead %s skipped: missing Google Maps URL.",
                        lead_id,
                    )
                    repaired.append({**listing, "repair_status": "missing_google_maps_url"})
                    continue

                try:
                    await page.goto(maps_url, wait_until="domcontentloaded", timeout=60_000)
                    await _dismiss_consent_if_present(page)
                    await page.wait_for_selector("h1.DUwDvf", timeout=10_000)
                    await page.wait_for_timeout(3_000)
                    name = await _safe_inner_text(page.locator("h1.DUwDvf").first, timeout=3_000)
                    parsed: Dict[str, Any] = {
                        "business_name": name or listing.get("business_name") or "",
                    }
                    if repair_fields.intersection({"details", "website", "address", "phone"}):
                        logger.info("[Website Repair] Fetching detail rows for lead %s (%s).", lead_id, business_name)
                        raw_data = await _extract_detail_texts(page)
                        raw_data.append(parsed["business_name"])
                        parsed.update(_parse_fields(raw_data))
                    if "rating" in repair_fields or "reviews" in repair_fields:
                        logger.info(
                            "[Website Repair] Fetching Google %s for lead %s (%s).",
                            "rating/reviews" if "reviews" in repair_fields else "rating",
                            lead_id,
                            business_name,
                        )
                        rating_data = await _extract_rating_and_reviews(
                            page,
                            business_name=business_name,
                            include_reviews="reviews" in repair_fields,
                        )
                        parsed.update(rating_data)
                    parsed["google_maps_url"] = page.url
                    parsed["repair_fields"] = sorted(repair_fields)
                    logger.info(
                        "[Website Repair] Lead %s checked: website=%s address=%s phone=%s rating=%s reviews=%s",
                        lead_id,
                        parsed.get("website") or "none",
                        parsed.get("address") or "none",
                        parsed.get("phone") or "none",
                        parsed.get("google_rating") or "none",
                        len(parsed.get("google_reviews") or []),
                    )
                    repaired.append({**listing, **parsed, "repair_status": "checked"})
                except Exception as exc:
                    logger.warning(
                        "Google Maps repair failed for lead %s (%s): %s",
                        listing.get("lead_id"),
                        maps_url,
                        exc,
                    )
                    repaired.append({
                        **listing,
                        "repair_status": "failed",
                        "repair_error": str(exc),
                    })
        finally:
            await context.close()

    return repaired


async def scrape_google_maps_with_metrics(
    niche: str,
    location: str,
    max_results: Optional[int] = None,
    cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
    skip_reviews_for_with_website: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Scrape Google Maps and return businesses plus run metrics."""
    settings = get_settings()
    query = f"{niche} in {location}"
    metrics: Dict[str, Any] = {
        "cards_seen": 0,
        "cards_attempted": 0,
        "cards_skipped_previously_attempted": 0,
        "cards_without_key": 0,
        "duplicate_listings": 0,
        "extraction_failures": 0,
        "detail_panel_failures": 0,
        "name_failures": 0,
        "failed_card_samples": [],
        "stop_reason": None,
        "brand_skipped": 0,
        "reviews_skipped_website": 0,
        "result_limit": max_results or settings.max_maps_results,
        "max_maps_results_used": max_results or settings.max_maps_results,
    }
    logger.info("Starting Google Maps scrape for '%s'", query)

    async with async_playwright() as pw:
        # Persistent context — cookies survive between runs, no repeat consent
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

        # Prevent webdriver detection
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        try:
            # Step 1: Navigate to Maps homepage (not the search URL directly)
            await _check_cancelled(cancel_check, metrics)
            logger.info("Navigating to Google Maps homepage")
            await page.goto(
                "https://www.google.com/maps",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            # Step 2: Handle consent screen (explicit URL check — no hanging locators)
            if "consent.google.com" in page.url:
                logger.info("Consent screen detected — dismissing.")
                try:
                    reject_btn = page.locator(
                        "button[aria-label*='Reject']:has-text('Reject'), "
                        "button:has-text('Reject all')"
                    ).first
                    await reject_btn.click(timeout=8_000)
                    await page.wait_for_url(
                        "**/maps**",
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    logger.info("Consent dismissed, back on Maps.")
                except Exception as exc:
                    logger.warning("Consent dismiss failed: %s — continuing anyway", exc)

            # Step 3: Type into the Maps search box and search
            await _check_cancelled(cancel_check, metrics)
            logger.info("Searching Maps for: %s", query)
            input_selector = "input[name='q']"
            try:
                await page.wait_for_selector(input_selector, timeout=20_000)
            except PlaywrightTimeout:
                logger.error("Maps search input not found — page may not have loaded.")
                metrics["stop_reason"] = "search_input_missing"
                return [], metrics

            await page.fill(input_selector, query)
            await page.press(input_selector, "Enter")

            if await _is_google_blocked(page):
                logger.warning("Google Maps appears to be showing a block or CAPTCHA page.")
                metrics["stop_reason"] = "blocked"
                businesses = []
                return businesses, metrics

            # Step 5: Scroll and collect all results
            businesses = await _scroll_and_collect(
                page,
                max_results=metrics["result_limit"],
                metrics=metrics,
                cancel_check=cancel_check,
                skip_reviews_for_with_website=skip_reviews_for_with_website,
            )

        except asyncio.CancelledError:
            logger.info("Maps scrape cancelled for '%s'.", query)
            metrics["stop_reason"] = "cancelled"
            raise
        except Exception as exc:
            logger.error("Maps scrape failed: %s", exc, exc_info=True)
            businesses = []
            metrics["stop_reason"] = "error"
        finally:
            await context.close()

    metrics["businesses_found"] = len(businesses)
    logger.info("Maps scrape complete: %d businesses found.", len(businesses))
    return businesses, metrics


# ---------------------------------------------------------------------------
# Scroll + collect
# ---------------------------------------------------------------------------

async def _scroll_and_collect(
    page: Page,
    max_results: int,
    metrics: Dict[str, Any],
    cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
    skip_reviews_for_with_website: bool = False,
) -> List[Dict[str, Any]]:
    """Scroll the feed and collect visible cards as Google Maps virtualizes them."""
    feed = page.locator("div[role='feed']")
    listings: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    attempted_card_keys: set[str] = set()
    no_progress_rounds = 0
    max_no_progress_rounds = 8
    reached_end = False

    try:
        await page.wait_for_selector("div[role='article']", timeout=15_000)
    except PlaywrightTimeout:
        if await _is_google_blocked(page):
            logger.warning("No article cards appeared because Google appears to be blocking the session.")
            metrics["stop_reason"] = "blocked"
            return []
        logger.warning("No article cards appeared before timeout.")
        metrics["stop_reason"] = "no_articles"
        return []

    while no_progress_rounds < max_no_progress_rounds:
        await _check_cancelled(cancel_check, metrics)
        before_count = len(listings)
        await _extract_visible_articles(
            page,
            listings,
            seen_keys,
            attempted_card_keys,
            max_results,
            metrics,
            cancel_check=cancel_check,
            skip_reviews_for_with_website=skip_reviews_for_with_website,
        )
        added_count = len(listings) - before_count
        logger.info(
            "Collected %d new businesses this pass (%d total).",
            added_count,
            len(listings),
        )

        if len(listings) >= max_results:
            logger.info("Reached configured Google Maps result limit: %d.", max_results)
            metrics["stop_reason"] = "result_limit"
            break

        if await _has_end_of_results(page):
            logger.info("Reached end of Google Maps results.")
            reached_end = True
            metrics["stop_reason"] = "end_of_results"
            break

        await feed.evaluate(
            "(el) => el.scrollBy(0, Math.floor(Math.random() * 900 + 700));"
        )
        await _check_cancelled(cancel_check, metrics)
        await page.wait_for_timeout(random.randint(2000, 6000))

        if len(listings) == before_count:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0

    # One final pass catches cards rendered after the last scroll.
    if not reached_end and len(listings) < max_results:
        before_count = len(listings)
        await _extract_visible_articles(
            page,
            listings,
            seen_keys,
            attempted_card_keys,
            max_results,
            metrics,
            cancel_check=cancel_check,
            skip_reviews_for_with_website=skip_reviews_for_with_website,
        )
        logger.info(
            "Collected %d new businesses in final pass (%d total).",
            len(listings) - before_count,
            len(listings),
        )

    if no_progress_rounds >= max_no_progress_rounds:
        logger.info(
            "Stopped Maps scrolling after %d rounds without new listings.",
            max_no_progress_rounds,
        )
        metrics["stop_reason"] = "no_progress"

    if metrics.get("stop_reason") is None:
        metrics["stop_reason"] = "completed"

    return listings[:max_results]


async def _extract_visible_articles(
    page: Page,
    listings: List[Dict[str, Any]],
    seen_keys: set[str],
    attempted_card_keys: set[str],
    max_results: int,
    metrics: Dict[str, Any],
    cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
    skip_reviews_for_with_website: bool = False,
) -> None:
    """Extract currently mounted article cards and append unseen businesses."""
    article_locator = page.locator("div[role='article']")

    count = await article_locator.count()
    metrics["cards_seen"] = max(int(metrics.get("cards_seen", 0)), count)
    logger.info("Found %d article cards; extracting new cards.", count)

    skipped_attempted = 0
    skipped_unkeyed_cards = 0
    skipped_duplicate_listings = 0

    for i in range(count):
        await _check_cancelled(cancel_check, metrics)
        if len(listings) >= max_results:
            break

        try:
            # Re-query by index every iteration — avoids stale element refs
            article = page.locator("div[role='article']").nth(i)

            card_key = await _article_card_key(article)
            if not card_key:
                skipped_unkeyed_cards += 1
                metrics["cards_without_key"] += 1
                continue
            if card_key in attempted_card_keys:
                skipped_attempted += 1
                metrics["cards_skipped_previously_attempted"] += 1
                continue
            attempted_card_keys.add(card_key)
            metrics["cards_attempted"] += 1

            logger.info("Opening Google Maps result card %s/%s.", i + 1, count)
            await article.click()
            await page.wait_for_timeout(500)

            # Wait for the detail panel h1 to appear
            try:
                await page.wait_for_selector("h1.DUwDvf", timeout=5_000)
            except PlaywrightTimeout:
                logger.warning("Article %d: detail panel did not open", i)
                metrics["detail_panel_failures"] += 1
                await _add_failed_card_sample(metrics, article, "detail_panel_timeout")
                continue

            # Business name
            name_el = page.locator("h1.DUwDvf")
            name = await _safe_inner_text(name_el.first, timeout=3_000)
            if not name:
                logger.warning("Article %d: business name not readable", i)
                metrics["name_failures"] += 1
                await _add_failed_card_sample(metrics, article, "name_not_readable")
                continue
            logger.info("Google Maps detail panel opened for %s.", name)

            # Big-brand exclusion: never prospects, skip all further work.
            if is_excluded_brand(name):
                metrics["brand_skipped"] = int(metrics.get("brand_skipped") or 0) + 1
                logger.info("Skipping excluded brand %s.", name)
                continue

            # Detail fields (address, phone, website all come from these divs).
            # Maps often renders these rows progressively after the H1 appears,
            # so wait for the set to stabilize before parsing.
            raw_data = await _extract_detail_texts(page)
            raw_data.append(name)

            parsed = _parse_fields(raw_data)
            parsed["google_maps_url"] = page.url
            skip_reviews = skip_reviews_for_with_website and _is_own_website(
                parsed.get("website") or ""
            )
            if skip_reviews:
                metrics["reviews_skipped_website"] = int(
                    metrics.get("reviews_skipped_website") or 0
                ) + 1
                logger.info(
                    "Skipping reviews for with-website listing %s.", name
                )
            else:
                logger.info("Fetching Google rating and reviews for %s.", name)
                rating_data = await _extract_rating_and_reviews(page, business_name=name)
                parsed.update(rating_data)
                logger.info(
                    "Fetched Google rating/reviews for %s: rating=%s total_reviews=%s fetched_reviews=%s.",
                    name,
                    parsed.get("google_rating") or "none",
                    parsed.get("google_review_count") if parsed.get("google_review_count") is not None else "unknown",
                    len(parsed.get("google_reviews") or []),
                )

            key = _listing_key(parsed)
            if not key:
                skipped_unkeyed_cards += 1
                metrics["cards_without_key"] += 1
                continue
            if key in seen_keys:
                skipped_duplicate_listings += 1
                metrics["duplicate_listings"] += 1
                continue

            seen_keys.add(key)
            logger.debug("Article %d extracted: %s", i, parsed.get("business_name"))
            listings.append(parsed)

            await asyncio.sleep(0.5)

        except Exception as exc:
            logger.warning("Article %d extraction failed: %s", i, exc)
            metrics["extraction_failures"] += 1
            try:
                article = page.locator("div[role='article']").nth(i)
                await _add_failed_card_sample(metrics, article, type(exc).__name__)
            except Exception:
                pass
            continue

    if skipped_attempted:
        logger.info("Skipped %d previously attempted article cards.", skipped_attempted)
    if skipped_duplicate_listings:
        logger.info("Skipped %d duplicate business listings.", skipped_duplicate_listings)
    if skipped_unkeyed_cards:
        logger.info("Skipped %d cards without a usable key.", skipped_unkeyed_cards)


async def _check_cancelled(
    cancel_check: Optional[Callable[[], Awaitable[None]]],
    metrics: Dict[str, Any],
) -> None:
    if not cancel_check:
        return
    try:
        await cancel_check()
    except asyncio.CancelledError:
        metrics["stop_reason"] = "cancelled"
        raise


async def _dismiss_consent_if_present(page: Page) -> None:
    if "consent.google.com" not in page.url:
        return
    logger.info("Consent screen detected; dismissing.")
    try:
        reject_btn = page.locator(
            "button[aria-label*='Reject']:has-text('Reject'), "
            "button:has-text('Reject all')"
        ).first
        await reject_btn.click(timeout=8_000)
        await page.wait_for_url(
            "**/maps**",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        logger.info("Consent dismissed, back on Maps.")
    except Exception as exc:
        logger.warning("Consent dismiss failed: %s; continuing anyway", exc)


async def _extract_detail_texts(page: Page) -> List[str]:
    """Read detail fields after Maps has finished progressively rendering them."""
    locator = page.locator("div.Io6YTe.fontBodyMedium.kR99db.fdkmkc")

    stable_texts: List[str] = []
    previous_texts: List[str] = []
    stable_reads = 0
    for _ in range(12):
        try:
            texts = await locator.evaluate_all(
                """els => els
                    .map(el => (el.innerText || el.textContent || '').trim())
                    .filter(Boolean)
                """
            )
            current_texts = [str(text).strip() for text in texts if str(text).strip()]
        except Exception as exc:
            logger.debug("Bulk detail extraction poll failed: %s", exc)
            current_texts = []

        if current_texts and current_texts == previous_texts:
            stable_reads += 1
        else:
            stable_reads = 0

        if current_texts:
            stable_texts = current_texts
        if stable_reads >= 2:
            return current_texts

        previous_texts = current_texts
        await page.wait_for_timeout(250)

    if stable_texts:
        return stable_texts

    try:
        texts = await locator.evaluate_all(
            """els => els
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(Boolean)
            """
        )
        if texts:
            return [str(text).strip() for text in texts if str(text).strip()]
    except Exception as exc:
        logger.debug("Bulk detail extraction failed: %s", exc)

    raw_data: List[str] = []
    count = await locator.count()
    for idx in range(count):
        text = await _safe_inner_text(locator.nth(idx), timeout=1_500)
        if text:
            raw_data.append(text)
    return raw_data


async def _extract_rating_and_reviews(
    page: Page,
    max_reviews: int = 9,
    business_name: str = "",
    include_reviews: bool = True,
) -> Dict[str, Any]:
    """Best-effort extraction of the listing's average rating and visible reviews."""
    result: Dict[str, Any] = {
        "google_rating": None,
        "google_review_count": None,
        "google_reviews": [],
    }
    result["google_rating"] = await _extract_average_rating(page)
    result["google_review_count"] = await _extract_review_count(page)
    if include_reviews:
        result["google_reviews"] = await _extract_customer_reviews(
            page,
            max_reviews=max_reviews,
            business_name=business_name,
            total_review_count=result["google_review_count"],
        )
    return result


async def _extract_average_rating(page: Page) -> Optional[float]:
    selectors = [
        "div.F7nice span[aria-hidden='true']",
        "div.F7nice",
        "span.MW4etd",
        "span[aria-label*='stars']",
        "span[aria-label*='star']",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        text = await _safe_inner_text(locator, timeout=1_000)
        if not text:
            try:
                text = await locator.get_attribute("aria-label", timeout=1_000) or ""
            except Exception:
                text = ""
        rating = _parse_rating_value(text)
        if rating is not None:
            return rating
    return None


async def _extract_review_count(page: Page) -> Optional[int]:
    selectors = [
        "div.F7nice",
        "span.UY7F9",
        "button[aria-label^='Reviews for']",
        "button[aria-label*='reviews']",
        "button[aria-label*='Reviews']",
        "span[aria-label*='reviews']",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        text = await _safe_inner_text(locator, timeout=1_000)
        if not text:
            try:
                text = await locator.get_attribute("aria-label", timeout=1_000) or ""
            except Exception:
                text = ""
        count = _parse_review_count(text)
        if count is not None:
            return count
    return None


def _review_stagnation_update(
    *, new_cards_mounted: bool, saved_grew: bool, stagnant_passes: int
) -> int:
    """Next stagnant-pass count for the review fetch loop.

    Textless cards must not stall the fetch, so any progress resets the
    counter: newly mounted cards OR newly saved text reviews.
    """
    return 0 if (new_cards_mounted or saved_grew) else stagnant_passes + 1


async def _extract_customer_reviews(
    page: Page,
    max_reviews: int = 9,
    business_name: str = "",
    total_review_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    opened_reviews = False
    business_label = business_name or "selected business"
    logger.info(
        "[Maps Reviews] Fetching reviews for %s: total_reviews=%s max_reviews=%s",
        business_label,
        total_review_count if total_review_count is not None else "unknown",
        max_reviews,
    )
    try:
        review_button = page.locator(
            "button[aria-label^='Reviews for'], "
            "button:has-text('Reviews'), "
            "button[jsaction*='pane.rating.moreReviews'], "
            "button[aria-label*='Reviews'], "
            "button[aria-label*='reviews']"
        ).first
        if await review_button.count():
            logger.info("[Maps Reviews] Opening reviews panel for %s.", business_label)
            await review_button.click(timeout=2_000)
            opened_reviews = True
            await page.wait_for_timeout(900)
            logger.info("[Maps Reviews] Reviews panel opened for %s.", business_label)
            await _expand_reviews_panel(page, max_reviews=max_reviews, business_name=business_label)
        else:
            logger.info("[Maps Reviews] No reviews button found for %s.", business_label)
    except Exception as exc:
        logger.info("[Maps Reviews] Failed opening reviews panel for %s: %s", business_label, exc)

    reviews: List[Dict[str, Any]] = []
    seen: set[str] = set()
    seen_ids: set[str] = set()
    max_visible = 0
    try:
        stagnant_passes = 0
        for pass_index in range(1, 13):
            before_count = len(reviews)
            cards = await _review_cards_locator(page)
            visible_count = await cards.count()
            new_cards = visible_count > max_visible
            max_visible = max(max_visible, visible_count)
            logger.info(
                "[Maps Reviews] %s: review fetch pass %s visible_cards=%s saved_reviews=%s/%s.",
                business_label,
                pass_index,
                visible_count,
                len(reviews),
                max_reviews,
            )
            for index in range(visible_count):
                if len(reviews) >= max_reviews:
                    break
                card = cards.nth(index)
                try:
                    rid = await card.get_attribute("data-review-id", timeout=500) or ""
                except Exception:
                    rid = ""
                if rid and rid in seen_ids:
                    continue
                review = await _extract_review_card(card, business_name=business_label, visible_index=index + 1)
                if rid:
                    seen_ids.add(rid)
                if not review or not review.get("text"):
                    continue
                key = _review_key(review)
                if key in seen:
                    logger.info(
                        "[Maps Reviews] %s: skipped duplicate review from %s.",
                        business_label,
                        review.get("author") or "unknown customer",
                    )
                    continue
                seen.add(key)
                reviews.append(review)
                logger.info(
                    "[Maps Reviews] %s: successfully fetched review %s/%s from %s.",
                    business_label,
                    len(reviews),
                    max_reviews,
                    review.get("author") or "unknown customer",
                )
            if len(reviews) >= max_reviews:
                break
            stagnant_passes = _review_stagnation_update(
                new_cards_mounted=new_cards,
                saved_grew=len(reviews) > before_count,
                stagnant_passes=stagnant_passes,
            )
            if stagnant_passes >= 4:
                logger.info(
                    "[Maps Reviews] %s: stopping review fetch after %s stagnant passes.",
                    business_label,
                    stagnant_passes,
                )
                break
            if not await _scroll_reviews_panel(page, business_name=business_label):
                break
    except Exception as exc:
        logger.info("[Maps Reviews] Failed extracting reviews for %s: %s", business_label, exc)
    finally:
        if opened_reviews:
            logger.info("[Maps Reviews] Closing reviews panel for %s.", business_label)
            await _close_reviews_panel(page)

    logger.info(
        "[Maps Reviews] Completed fetching reviews for %s: fetched=%s total_reviews=%s.",
        business_label,
        len(reviews),
        total_review_count if total_review_count is not None else "unknown",
    )
    return reviews[:max_reviews]


async def _review_cards_locator(page: Page) -> Locator:
    cards = page.locator("div[data-review-id]")
    if await cards.count() == 0:
        cards = page.locator("div.jftiEf")
    return cards


async def _expand_reviews_panel(page: Page, max_reviews: int = 9, business_name: str = "") -> None:
    business_label = business_name or "selected business"
    more_reviews_button = page.locator(
        "button:has-text('More reviews'), "
        "button[aria-label*='More reviews'], "
        "button[aria-label*='more reviews'], "
        "button[jsaction*='pane.reviewChart.moreReviews']"
    ).first
    for _ in range(3):
        try:
            if await more_reviews_button.count() == 0:
                logger.info("[Maps Reviews] %s: no More reviews button visible.", business_label)
                break
            label = (
                await more_reviews_button.get_attribute("aria-label", timeout=600)
                or await more_reviews_button.inner_text(timeout=600)
                or "More reviews"
            )
            logger.info("[Maps Reviews] %s: clicking %s button.", business_label, label.strip())
            await more_reviews_button.click(timeout=1_500)
            await page.wait_for_timeout(900)
            logger.info("[Maps Reviews] %s: clicked More reviews button successfully.", business_label)
        except Exception as exc:
            logger.info("[Maps Reviews] %s: More reviews click failed: %s", business_label, exc)
            break


async def _scroll_reviews_panel(page: Page, business_name: str = "") -> bool:
    business_label = business_name or "selected business"
    try:
        cards = await _review_cards_locator(page)
        count = await cards.count()
        if count:
            last_card = cards.nth(count - 1)
            await last_card.scroll_into_view_if_needed(timeout=1_500)
            try:
                await last_card.evaluate(
                    """el => {
                        let node = el.parentElement;
                        while (node) {
                            if (node.scrollHeight > node.clientHeight + 20) {
                                node.scrollTop = node.scrollTop + Math.max(600, node.clientHeight * 0.85);
                                return true;
                            }
                            node = node.parentElement;
                        }
                        return false;
                    }"""
                )
            except Exception as exc:
                logger.debug("[Maps Reviews] %s: DOM scroll helper failed: %s", business_label, exc)
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(750)
        logger.info("[Maps Reviews] %s: scrolled reviews panel.", business_label)
        return True
    except Exception as exc:
        logger.info("[Maps Reviews] %s: failed to scroll reviews panel: %s", business_label, exc)
        return False


async def _extract_review_card(
    card: Locator,
    business_name: str = "",
    visible_index: int = 0,
) -> Optional[Dict[str, Any]]:
    business_label = business_name or "selected business"
    author = await _safe_inner_text(card.locator(".d4r55").first, timeout=800)
    if not author:
        author = await _safe_inner_text(card.locator(".WNxzHc").first, timeout=800)
    clean_author = _clean_review_author(author) or "Google reviewer"
    logger.info(
        "[Maps Reviews] Fetching %s's review for %s.",
        clean_author if clean_author != "Google reviewer" else f"visible review {visible_index}",
        business_label,
    )
    await _expand_review_card_text(card, business_name=business_label, author=clean_author)
    text = await _safe_inner_text(card.locator(".wiI7pd, .MyEned span").first, timeout=800)
    rating_text = ""
    try:
        rating_text = await card.locator("span.kvMYJc, span[aria-label*='star']").first.get_attribute(
            "aria-label",
            timeout=800,
        ) or ""
    except Exception:
        rating_text = ""
    rating = _parse_rating_value(rating_text)
    review_id = ""
    try:
        review_id = await card.get_attribute("data-review-id", timeout=800) or ""
    except Exception:
        review_id = ""
    avatar_source_url = await _extract_review_avatar_url(card)

    if not text:
        logger.info(
            "[Maps Reviews] Failed fetching %s's review for %s: empty review text.",
            clean_author,
            business_label,
        )
        return None
    return {
        "author": clean_author,
        "rating": rating,
        "text": text,
        "review_id": review_id,
        "avatar_source_url": avatar_source_url,
    }


async def _expand_review_card_text(card: Locator, business_name: str = "", author: str = "") -> None:
    business_label = business_name or "selected business"
    author_label = author or "reviewer"
    more_buttons = card.locator(
        "button[aria-label*='More'], "
        "button[aria-label*='more'], "
        "button:has-text('More'), "
        "button:has-text('more')"
    )
    try:
        count = min(await more_buttons.count(), 3)
    except Exception:
        return

    for index in range(count):
        button = more_buttons.nth(index)
        try:
            label = (
                await button.get_attribute("aria-label", timeout=500)
                or await button.inner_text(timeout=500)
                or ""
            )
        except Exception:
            label = ""
        if "more" not in label.lower():
            continue
        try:
            logger.info(
                "[Maps Reviews] %s: clicking More text button for %s.",
                business_label,
                author_label,
            )
            await button.click(timeout=900)
            await asyncio.sleep(0.12)
            logger.info(
                "[Maps Reviews] %s: expanded truncated review text for %s.",
                business_label,
                author_label,
            )
        except Exception as exc:
            logger.info(
                "[Maps Reviews] %s: failed expanding review text for %s: %s",
                business_label,
                author_label,
                exc,
            )


async def _extract_review_avatar_url(card: Locator) -> str:
    selectors = [
        "img.NBa7we",
        "button img[src*='googleusercontent.com']",
        "img[src*='googleusercontent.com']",
    ]
    for selector in selectors:
        try:
            src = await card.locator(selector).first.get_attribute("src", timeout=800) or ""
        except Exception:
            src = ""
        if src.startswith("http"):
            return src
    return ""


def _clean_review_author(author: str) -> str:
    first_line = (author or "").splitlines()[0].strip()
    return re.sub(r"\s+", " ", first_line)


def _review_key(review: Dict[str, Any]) -> str:
    author = _clean_review_author(str(review.get("author") or "")).lower()
    text = re.sub(r"\s+", " ", str(review.get("text") or "").strip().lower())
    review_id = str(review.get("review_id") or "").strip()
    if review_id:
        return review_id
    return f"{author}|{text[:180]}"


async def _close_reviews_panel(page: Page) -> None:
    try:
        back_button = page.locator(
            "button[aria-label='Back'], "
            "button[aria-label*='Back to'], "
            "button[jsaction*='pane.reviewChart.back']"
        ).first
        if await back_button.count():
            await back_button.click(timeout=1_500)
            await page.wait_for_timeout(400)
            return
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
    except Exception:
        pass


def _parse_rating_value(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"\b([0-5](?:\.\d)?)\b", text.replace(",", "."))
    if not match:
        return None
    rating = float(match.group(1))
    if 0 <= rating <= 5:
        return rating
    return None


def _parse_review_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"([\d,]+)\s+reviews?", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\(([\d,]+)\)", text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


async def _article_card_key(article: Locator) -> str:
    """Fingerprint a result card before clicking so old cards are not retried."""
    try:
        text = await article.inner_text(timeout=1_500)
    except Exception:
        return ""

    key = re.sub(r"\s+", " ", text).strip().lower()
    return key[:500]


async def _add_failed_card_sample(
    metrics: Dict[str, Any],
    article: Locator,
    reason: str,
) -> None:
    """Keep a few card text samples for diagnosing non-fatal extraction failures."""
    samples = metrics.setdefault("failed_card_samples", [])
    if len(samples) >= 5:
        return

    try:
        text = await article.inner_text(timeout=1_000)
    except Exception:
        text = ""

    samples.append({
        "reason": reason,
        "text": re.sub(r"\s+", " ", text).strip()[:300],
    })


async def _safe_inner_text(locator: Locator, timeout: int) -> str:
    """Return locator text, or empty string if Google Maps re-renders it."""
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception:
        return ""


async def _is_google_blocked(page: Page) -> bool:
    """Best-effort detection for Google block/CAPTCHA/interstitial pages."""
    url = page.url.lower()
    if "sorry" in url or "recaptcha" in url:
        return True

    try:
        body_text = (await page.locator("body").inner_text(timeout=2_000)).lower()
    except Exception:
        return False

    blocked_markers = (
        "unusual traffic",
        "not a robot",
        "our systems have detected",
        "to continue, please type the characters",
        "sorry,",
        "captcha",
    )
    return any(marker in body_text for marker in blocked_markers)


def _listing_key(listing: Dict[str, Any]) -> str:
    """Build a stable-enough key for deduping virtualized Maps cards."""
    name = (listing.get("business_name") or "").strip().lower()
    address = (listing.get("address") or "").strip().lower()
    phone = re.sub(r"\D", "", listing.get("phone") or "")
    url = (listing.get("google_maps_url") or "").strip().lower()

    if name and address:
        return f"{name}|{address}"
    if name and phone:
        return f"{name}|{phone}"
    if url:
        return url
    return name


async def _has_end_of_results(page: Page) -> bool:
    """Detect Maps' end marker without depending on scrollHeight."""
    marker = page.locator(
        "text=/You've reached the end of the list|You\\u2019ve reached the end of the list/"
    )
    try:
        return await marker.count() > 0
    except Exception:
        return False
