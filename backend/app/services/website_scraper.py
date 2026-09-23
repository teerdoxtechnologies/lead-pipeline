"""
Website scraper using Playwright.

Extracts structured marketing content needed by the Gemini analyser:
  - Page title, meta description
  - H1 / H2 headings
  - CTA button text (above-the-fold first)
  - Full visible body text (truncated to ~8 000 chars)
  - Footer text
  - Links (for contact page discovery)

15 second navigation timeout. Returns a structured dict.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
]

# Max text we send to Gemini — keep context window under control
_MAX_TEXT_CHARS = 8_000


async def scrape_website(
    url: str,
    manual_verification: bool = False,
    manual_verification_timeout_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Scrape a business website and return a structured dict suitable for
    passing to the Gemini analyser.

    Keys returned:
    ``url``, ``title``, ``meta_description``, ``h1_tags``, ``h2_tags``,
    ``cta_texts``, ``body_text``, ``footer_text``, ``page_text``
    """
    settings = get_settings()

    result: Dict[str, Any] = {
        "url": url,
        "title": None,
        "meta_description": None,
        "h1_tags": [],
        "h2_tags": [],
        "cta_texts": [],
        "body_text": "",
        "footer_text": "",
        "page_text": "",
        "browser_quality": {},
        "security_verification_detected": False,
        "security_verification_status": None,
    }

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=False if manual_verification else settings.playwright_headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            viewport={"width": 1280, "height": 900},
        )
        page: Page = await context.new_page()
        await page.add_init_script(_WEB_VITALS_INIT_JS)

        try:
            # Normalize URL — bare domains like "setmore.com" have no scheme
            if url and not url.startswith(("http://", "https://")):
                url = "https://" + url
                result["url"] = url

            await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(1.5)  # Let JS settle and metrics flush

            result = await _extract_page_data(page, url)
            if is_security_verification_page(result):
                result["security_verification_detected"] = True
                if not manual_verification:
                    result["security_verification_status"] = "blocked"
                    logger.warning("Security verification detected for %s; manual verification disabled.", url)
                    return result
                logger.warning(
                    "Security verification detected for %s; waiting up to %ss for manual completion.",
                    url,
                    manual_verification_timeout_seconds,
                )
                verified = await _wait_for_manual_verification(
                    page,
                    url,
                    manual_verification_timeout_seconds,
                )
                result = await _extract_page_data(page, url)
                result["security_verification_detected"] = True
                result["security_verification_status"] = "passed" if verified else "timeout"
                if not verified:
                    logger.warning("Manual verification timed out for %s.", url)
                    return result

            desktop_quality = await _collect_browser_quality(page, url, "desktop")

            # Also try the /contact or /about page for additional context
            contact_url = _find_contact_page_url(result.get("links", []), url)
            if contact_url and contact_url != url:
                try:
                    await page.goto(contact_url, wait_until="domcontentloaded", timeout=10_000)
                    await asyncio.sleep(0.5)
                    contact_data = await _extract_page_data(page, contact_url)
                    # Append contact page text to help email extractor
                    extra = contact_data.get("body_text", "")
                    result["body_text"] = (result["body_text"] + "\n" + extra)[
                        :_MAX_TEXT_CHARS
                    ]
                    result["page_text"] = result["body_text"]
                except Exception as exc:
                    logger.debug("Could not scrape contact page %s: %s", contact_url, exc)

            mobile_quality = None
            try:
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                await asyncio.sleep(1.5)
                mobile_data = await _extract_page_data(page, url)
                if is_security_verification_page(mobile_data):
                    result["security_verification_detected"] = True
                    if not manual_verification:
                        result["security_verification_status"] = "blocked"
                        logger.warning("Mobile security verification detected for %s; manual verification disabled.", url)
                        return result
                    logger.warning(
                        "Mobile security verification detected for %s; waiting up to %ss for manual completion.",
                        url,
                        manual_verification_timeout_seconds,
                    )
                    verified = await _wait_for_manual_verification(
                        page,
                        url,
                        manual_verification_timeout_seconds,
                    )
                    result["security_verification_status"] = "passed" if verified else "timeout"
                    if not verified:
                        logger.warning("Manual mobile verification timed out for %s.", url)
                        return result
                mobile_quality = await _collect_browser_quality(page, url, "mobile")
            except Exception as exc:
                logger.debug("Could not collect mobile browser quality for %s: %s", url, exc)

            result["browser_quality"] = {
                "desktop": desktop_quality,
                "mobile": mobile_quality,
                "summary": _browser_quality_summary(desktop_quality, mobile_quality),
                "score": _browser_quality_score(desktop_quality, mobile_quality),
            }

        except PlaywrightTimeout:
            logger.warning("Timeout scraping %s (15 s limit reached)", url)
        except Exception as exc:
            logger.error("Error scraping %s: %s", url, exc, exc_info=True)
        finally:
            await context.close()
            await browser.close()

    return result


def is_security_verification_page(page_data: Dict[str, Any]) -> bool:
    """Return True when scraped content is an interstitial security challenge."""
    title = str(page_data.get("title") or "")
    body = str(page_data.get("body_text") or page_data.get("page_text") or "")
    combined = f"{title}\n{body}".lower()
    security_markers = (
        "just a moment",
        "security verification",
        "verify you are not a bot",
        "checking your browser",
        "cf-browser-verification",
        "cloudflare",
        "ray id:",
        "captcha",
        "recaptcha",
        "hcaptcha",
        "access denied",
        "unusual traffic",
    )
    if any(marker in combined for marker in security_markers):
        return True
    h1_tags = " ".join(str(value or "") for value in page_data.get("h1_tags", []))
    h2_tags = " ".join(str(value or "") for value in page_data.get("h2_tags", []))
    headings = f"{h1_tags} {h2_tags}".lower()
    return any(marker in headings for marker in security_markers)


async def _wait_for_manual_verification(page: Page, url: str, timeout_seconds: int) -> bool:
    """Poll the visible page until the security interstitial is gone."""
    deadline = asyncio.get_event_loop().time() + max(10, timeout_seconds)
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        try:
            data = await _extract_page_data(page, url)
            if not is_security_verification_page(data):
                logger.info("Manual verification completed for %s.", url)
                return True
        except Exception as exc:
            logger.debug("Manual verification poll failed for %s: %s", url, exc)
    return False


async def _collect_browser_quality(page: Page, url: str, viewport: str) -> Dict[str, Any]:
    """Collect browser-rendered quality signals for a loaded page."""
    try:
        metrics = await page.evaluate(_BROWSER_QUALITY_JS)
        metrics["viewport"] = viewport
        metrics["url"] = url
        metrics["score"] = _browser_quality_score(metrics, None)
        metrics["issues"] = _browser_quality_issues(metrics)
        return metrics
    except Exception as exc:
        logger.debug("Browser quality collection failed for %s (%s): %s", url, viewport, exc)
        return {
            "viewport": viewport,
            "url": url,
            "score": 0,
            "issues": ["browser quality check failed"],
            "error": str(exc),
        }


async def _extract_page_data(page: Page, url: str) -> Dict[str, Any]:
    """Pull all relevant content from the currently loaded page."""
    data: Dict[str, Any] = {
        "url": url,
        "title": None,
        "meta_description": None,
        "h1_tags": [],
        "h2_tags": [],
        "cta_texts": [],
        "body_text": "",
        "footer_text": "",
        "page_text": "",
        "links": [],
    }

    # Title
    try:
        data["title"] = await page.title()
    except Exception:
        pass

    # Meta description
    try:
        meta = await page.query_selector('meta[name="description"]')
        if meta:
            data["meta_description"] = await meta.get_attribute("content")
    except Exception:
        pass

    # H1 / H2
    try:
        data["h1_tags"] = [
            await el.inner_text()
            for el in await page.query_selector_all("h1")
        ]
        data["h2_tags"] = [
            await el.inner_text()
            for el in await page.query_selector_all("h2")
        ]
    except Exception:
        pass

    # CTA buttons — prioritise above-the-fold buttons and common CTA text
    try:
        button_elements = await page.query_selector_all("a, button")
        cta_candidates = []
        cta_keywords = re.compile(
            r"(book|schedule|appointment|contact|get started|free|quote|"
            r"call|sign up|register|buy|order|consult|try)",
            re.IGNORECASE,
        )
        for el in button_elements:
            text = (await el.inner_text()).strip()
            if text and cta_keywords.search(text) and len(text) < 80:
                cta_candidates.append(text)
        data["cta_texts"] = list(dict.fromkeys(cta_candidates))[:10]  # deduplicate
    except Exception:
        pass

    # Body text
    try:
        body = await page.query_selector("body")
        if body:
            raw = await body.inner_text()
            data["body_text"] = raw[:_MAX_TEXT_CHARS]
            data["page_text"] = data["body_text"]
    except Exception:
        pass

    # Footer
    try:
        footer = await page.query_selector("footer")
        if footer:
            data["footer_text"] = (await footer.inner_text())[:2_000]
    except Exception:
        pass

    # All links (for contact page discovery)
    try:
        anchors = await page.query_selector_all("a[href]")
        links = []
        for a in anchors:
            href = await a.get_attribute("href")
            if href:
                links.append(urljoin(url, href))
        data["links"] = links[:100]
    except Exception:
        pass

    return data


def _find_contact_page_url(links: List[str], base_url: str) -> Optional[str]:
    """
    Scan ``links`` for a contact or about page on the same domain.
    Returns the first match found, or None.
    """
    base_domain = urlparse(base_url).netloc
    patterns = re.compile(r"/(contact|about|about-us|contact-us)(/|$)", re.IGNORECASE)

    for link in links:
        parsed = urlparse(link)
        if parsed.netloc == base_domain and patterns.search(parsed.path):
            return link
    return None


def _browser_quality_score(desktop: Optional[Dict[str, Any]], mobile: Optional[Dict[str, Any]]) -> int:
    score = 10
    for metrics in (desktop or {}, mobile or {}):
        if not metrics:
            score -= 1
            continue
        if metrics.get("horizontal_overflow"):
            score -= 2
        if not metrics.get("has_visible_h1"):
            score -= 2
        if int(metrics.get("above_fold_cta_count") or 0) == 0:
            score -= 2
        if int(metrics.get("low_contrast_text_count") or 0) > 0:
            score -= 2
        if int(metrics.get("small_tap_target_count") or 0) > 2:
            score -= 1
        if int(metrics.get("image_failures") or 0) > 0:
            score -= 1
        lcp_ms = _to_float(metrics.get("lcp_ms"))
        cls = _to_float(metrics.get("cls"))
        inp_ms = _to_float(metrics.get("inp_ms"))
        load_ms = _to_float(metrics.get("load_ms"))
        if lcp_ms is not None:
            if lcp_ms > 4_000:
                score -= 2
            elif lcp_ms > 2_500:
                score -= 1
        if cls is not None:
            if cls > 0.25:
                score -= 2
            elif cls > 0.1:
                score -= 1
        if inp_ms is not None:
            if inp_ms > 500:
                score -= 2
            elif inp_ms > 200:
                score -= 1
        if load_ms is not None and load_ms > 5_000:
            score -= 1
    return max(0, min(10, score))


def _browser_quality_issues(metrics: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    if metrics.get("horizontal_overflow"):
        issues.append("horizontal overflow")
    if not metrics.get("has_visible_h1"):
        issues.append("missing visible h1")
    if int(metrics.get("above_fold_cta_count") or 0) == 0:
        issues.append("cta not visible above the fold")
    if int(metrics.get("low_contrast_text_count") or 0) > 0:
        issues.append("low-contrast text")
    if int(metrics.get("small_tap_target_count") or 0) > 2:
        issues.append("small tap targets")
    if int(metrics.get("image_failures") or 0) > 0:
        issues.append("failed images")
    lcp_ms = _to_float(metrics.get("lcp_ms"))
    cls = _to_float(metrics.get("cls"))
    inp_ms = _to_float(metrics.get("inp_ms"))
    load_ms = _to_float(metrics.get("load_ms"))
    if lcp_ms is not None and lcp_ms > 2_500:
        issues.append("slow largest contentful paint")
    if cls is not None and cls > 0.1:
        issues.append("layout shift")
    if inp_ms is not None and inp_ms > 200:
        issues.append("slow interaction response")
    if load_ms is not None and load_ms > 5_000:
        issues.append("slow page load")
    return issues


def _browser_quality_summary(desktop: Optional[Dict[str, Any]], mobile: Optional[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for label, metrics in (("desktop", desktop), ("mobile", mobile)):
        if not metrics:
            continue
        issues = _browser_quality_issues(metrics)
        issue_text = ", ".join(issues) if issues else "no obvious browser issues"
        parts.append(
            f"{label}: score {metrics.get('score', 0)}/10; "
            f"CTAs above fold={metrics.get('above_fold_cta_count', 0)}; "
            f"low contrast elements={metrics.get('low_contrast_text_count', 0)}; "
            f"tap targets below 44px={metrics.get('small_tap_target_count', 0)}; "
            f"FCP={_format_ms(metrics.get('fcp_ms'))}; "
            f"LCP={_format_ms(metrics.get('lcp_ms'))}; "
            f"LCP culprit={_format_lcp_culprit(metrics.get('lcp_element'))}; "
            f"CLS={_format_decimal(metrics.get('cls'))}; "
            f"INP={_format_ms(metrics.get('inp_ms'))}; "
            f"Load={_format_ms(metrics.get('load_ms'))}; "
            f"issues={issue_text}"
        )
    return " | ".join(parts) if parts else "No browser quality data collected."


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_ms(value: Any) -> str:
    numeric = _to_float(value)
    if numeric is None:
        return "n/a"
    return f"{round(numeric)}ms"


def _format_decimal(value: Any) -> str:
    numeric = _to_float(value)
    if numeric is None:
        return "n/a"
    return f"{numeric:.2f}"


def _format_lcp_culprit(value: Any) -> str:
    if not isinstance(value, dict):
        return "n/a"
    label = str(value.get("label") or value.get("tag") or "").strip()
    text = str(value.get("text") or "").strip()
    url = str(value.get("url") or "").strip()
    resource = value.get("resource") if isinstance(value.get("resource"), dict) else {}
    duration = _format_ms(resource.get("duration_ms")) if resource else "n/a"
    if url:
        return f"{label or 'media'} ({url[:120]}, resource load {duration})"
    if text:
        return f"{label or 'text'} ({text[:120]})"
    return label or "n/a"


_WEB_VITALS_INIT_JS = r"""
(() => {
  if (window.__agencyWebVitalsInstalled) return;
  window.__agencyWebVitalsInstalled = true;

  function navEntry() {
    return performance.getEntriesByType('navigation')[0] || null;
  }

  function resetVitals() {
    const nav = navEntry();
    window.__agencyWebVitals = {
      fcp_ms: null,
      lcp_ms: null,
      lcp_element: null,
      cls: 0,
      inp_ms: null,
      dom_content_loaded_ms: nav ? nav.domContentLoadedEventEnd : null,
      load_ms: nav ? nav.loadEventEnd : null,
      ttfb_ms: nav ? nav.responseStart : null,
    };
  }

  resetVitals();
  document.addEventListener('readystatechange', () => {
    if (document.readyState === 'loading') {
      resetVitals();
    }
  });
  window.addEventListener('DOMContentLoaded', () => {
    const nav = navEntry();
    if (nav) {
      window.__agencyWebVitals.dom_content_loaded_ms = nav.domContentLoadedEventEnd;
      window.__agencyWebVitals.ttfb_ms = nav.responseStart;
    }
  }, { once: true });
  window.addEventListener('load', () => {
    const nav = navEntry();
    if (nav) {
      window.__agencyWebVitals.load_ms = nav.loadEventEnd;
      window.__agencyWebVitals.dom_content_loaded_ms = nav.domContentLoadedEventEnd;
      window.__agencyWebVitals.ttfb_ms = nav.responseStart;
    }
  }, { once: true });

  try {
    new PerformanceObserver((entryList) => {
      for (const entry of entryList.getEntries()) {
        if (entry.name === 'first-contentful-paint') {
          window.__agencyWebVitals.fcp_ms = entry.startTime;
        }
      }
    }).observe({ type: 'paint', buffered: true });
  } catch (error) {}

  function elementText(el) {
    return (el.innerText || el.textContent || el.getAttribute('alt') || el.getAttribute('aria-label') || '').trim();
  }

  function elementSelector(el) {
    if (!el) return null;
    const tag = (el.tagName || '').toLowerCase();
    const id = el.id ? `#${el.id}` : '';
    const className = typeof el.className === 'string' ? el.className.trim() : '';
    const classes = className
      ? `.${className.split(/\s+/).filter(Boolean).slice(0, 3).join('.')}`
      : '';
    return `${tag}${id}${classes}` || null;
  }

  function sectionContext(el) {
    if (!el) return null;
    const container = el.closest('section,article,header,footer,main,nav,aside');
    if (!container) return null;
    const heading = container.querySelector('h1,h2,h3,[aria-label]');
    const headingText = heading
      ? (heading.innerText || heading.textContent || heading.getAttribute('aria-label') || '').trim()
      : '';
    const containerLabel = container.getAttribute('aria-label') || container.id || container.className || container.tagName || '';
    return {
      container: String(containerLabel).trim().slice(0, 100) || null,
      heading: headingText.slice(0, 120) || null,
    };
  }

  function resourceTiming(url) {
    if (!url) return null;
    const entries = performance.getEntriesByName(url);
    const entry = entries && entries.length ? entries[entries.length - 1] : null;
    if (!entry) return null;
    return {
      initiator_type: entry.initiatorType || null,
      duration_ms: entry.duration || null,
      response_end_ms: entry.responseEnd || null,
      transfer_size: entry.transferSize || null,
      encoded_body_size: entry.encodedBodySize || null,
      decoded_body_size: entry.decodedBodySize || null,
    };
  }

  function lcpElementSummary(entry) {
    const el = entry && entry.element ? entry.element : null;
    const url = entry && entry.url ? entry.url : (el ? (el.currentSrc || el.src || el.poster || '') : '');
    const rect = el ? el.getBoundingClientRect() : null;
    const tag = el && el.tagName ? el.tagName.toLowerCase() : null;
    const text = el ? elementText(el).slice(0, 140) : '';
    const alt = el ? (el.getAttribute('alt') || '').trim().slice(0, 140) : '';
    let label = tag || 'content';
    if (tag === 'img' || tag === 'picture' || tag === 'video') {
      label = alt ? `${tag} image: ${alt}` : `${tag} image/media`;
    } else if (text) {
      label = `${tag || 'text'} text block`;
    }
    return {
      tag,
      selector: el ? elementSelector(el) : null,
      label,
      text,
      alt,
      url: url || null,
      size: entry && entry.size ? entry.size : null,
      render_time_ms: entry && entry.startTime ? entry.startTime : null,
      rect: rect ? {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        top: Math.round(rect.top),
        left: Math.round(rect.left),
      } : null,
      section: el ? sectionContext(el) : null,
      resource: resourceTiming(url),
    };
  }

  try {
    new PerformanceObserver((entryList) => {
      const entries = entryList.getEntries();
      const last = entries[entries.length - 1];
      if (last) {
        window.__agencyWebVitals.lcp_ms = last.startTime;
        window.__agencyWebVitals.lcp_element = lcpElementSummary(last);
      }
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (error) {}

  try {
    new PerformanceObserver((entryList) => {
      for (const entry of entryList.getEntries()) {
        if (!entry.hadRecentInput) {
          window.__agencyWebVitals.cls += entry.value;
        }
      }
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (error) {}

  try {
    new PerformanceObserver((entryList) => {
      for (const entry of entryList.getEntries()) {
        const duration = entry.duration || ((entry.processingEnd || 0) - (entry.startTime || 0));
        if (!duration) continue;
        window.__agencyWebVitals.inp_ms = Math.max(window.__agencyWebVitals.inp_ms || 0, duration);
      }
    }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
  } catch (error) {}
})();
"""


_BROWSER_QUALITY_JS = r"""
(() => {
  const ctaRegex = /(book|schedule|appointment|contact|get started|free|quote|call|sign up|register|buy|order|consult|try|request|estimate|enquire|book now)/i;
  const textSelectors = 'h1,h2,h3,h4,h5,h6,p,li,span,a,label,button,summary';
  const interactSelectors = 'a,button,input,select,textarea';

  function isVisible(el) {
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
  }

  function parseColor(color) {
    if (!color) return null;
    const rgba = color.match(/rgba?\(([^)]+)\)/i);
    if (!rgba) return null;
    const parts = rgba[1].split(',').map(part => part.trim());
    const r = Number(parts[0]);
    const g = Number(parts[1]);
    const b = Number(parts[2]);
    if ([r, g, b].some(Number.isNaN)) return null;
    return [r, g, b];
  }

  function luminance([r, g, b]) {
    const channels = [r, g, b].map(value => {
      const normalized = value / 255;
      return normalized <= 0.03928
        ? normalized / 12.92
        : Math.pow((normalized + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  function contrastRatio(fg, bg) {
    const l1 = luminance(fg);
    const l2 = luminance(bg);
    const bright = Math.max(l1, l2);
    const dark = Math.min(l1, l2);
    return (bright + 0.05) / (dark + 0.05);
  }

  function backgroundColor(el) {
    let node = el;
    while (node) {
      const style = getComputedStyle(node);
      const parsed = parseColor(style.backgroundColor);
      if (parsed && !style.backgroundColor.includes('rgba(0, 0, 0, 0)') && style.backgroundColor !== 'transparent') {
        return parsed;
      }
      node = node.parentElement;
    }
    return [255, 255, 255];
  }

  function textValue(el) {
    return (el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '').trim();
  }

  function elementSelector(el) {
    if (!el) return null;
    const tag = (el.tagName || '').toLowerCase();
    const id = el.id ? `#${el.id}` : '';
    const className = typeof el.className === 'string' ? el.className.trim() : '';
    const classes = className
      ? `.${className.split(/\s+/).filter(Boolean).slice(0, 3).join('.')}`
      : '';
    return `${tag}${id}${classes}` || null;
  }

  function sectionContext(el) {
    if (!el) return null;
    const container = el.closest('section,article,header,footer,main,nav,aside');
    if (!container) return null;
    const heading = container.querySelector('h1,h2,h3,[aria-label]');
    const headingText = heading
      ? (heading.innerText || heading.textContent || heading.getAttribute('aria-label') || '').trim()
      : '';
    const containerLabel = container.getAttribute('aria-label') || container.id || container.className || container.tagName || '';
    return {
      container: String(containerLabel).trim().slice(0, 100) || null,
      heading: headingText.slice(0, 120) || null,
    };
  }

  const textEls = [...document.querySelectorAll(textSelectors)].filter(isVisible);
  const interactEls = [...document.querySelectorAll(interactSelectors)].filter(isVisible);
  const lowContrast = [];
  for (const el of textEls.slice(0, 120)) {
    const text = textValue(el);
    if (!text) continue;
    const fg = parseColor(getComputedStyle(el).color);
    const bg = backgroundColor(el);
    if (!fg || !bg) continue;
    const ratio = contrastRatio(fg, bg);
    if (ratio < 4.5) {
      lowContrast.push({
        tag: el.tagName.toLowerCase(),
        selector: elementSelector(el),
        section: sectionContext(el),
        text: text.slice(0, 80),
        ratio: Number(ratio.toFixed(2)),
      });
    }
  }

  const aboveFoldCtas = interactEls.filter(el => {
    const text = textValue(el);
    if (!ctaRegex.test(text)) return false;
    const rect = el.getBoundingClientRect();
    return rect.top >= 0 && rect.top < Math.max(window.innerHeight * 1.15, 1);
  });

  const smallTapTargets = interactEls.filter(el => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && (rect.width < 44 || rect.height < 44);
  });

  function imageSource(img) {
    return (img.currentSrc || img.src || img.getAttribute('src') || '').trim();
  }

  function hasRealImageSource(img) {
    const src = imageSource(img);
    if (!src) return false;
    if (/^data:image\/(svg\+xml|gif|png);/i.test(src)) return false;
    if (/^(about:|blob:|javascript:)/i.test(src)) return false;
    return true;
  }

  function isNearViewport(el) {
    const rect = el.getBoundingClientRect();
    return rect.bottom >= -200 && rect.top <= window.innerHeight + 800;
  }

  function isDefinitelyBrokenImage(img) {
    if (!isVisible(img) || !isNearViewport(img) || !hasRealImageSource(img)) return false;
    const rect = img.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return false;
    if (!img.complete) return false;
    return img.naturalWidth === 0 || img.naturalHeight === 0;
  }

  const images = [...document.querySelectorAll('img')].filter(isVisible);
  const imageFailures = images.filter(isDefinitelyBrokenImage);
  const imageFailureSamples = imageFailures.slice(0, 8).map(img => ({
    src: img.currentSrc || img.src || img.getAttribute('src') || '',
    alt: (img.getAttribute('alt') || '').trim().slice(0, 120),
    selector: elementSelector(img),
    section: sectionContext(img),
  }));
  const smallTapTargetSamples = smallTapTargets.slice(0, 8).map(el => {
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      selector: elementSelector(el),
      text: textValue(el).slice(0, 80),
      href: el.href || null,
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      section: sectionContext(el),
    };
  });
  const overflow = document.documentElement.scrollWidth > window.innerWidth + 8;
  const visibleH1 = [...document.querySelectorAll('h1')].some(isVisible);
  const vitals = window.__agencyWebVitals || {};

  return {
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    horizontal_overflow: overflow,
    has_visible_h1: visibleH1,
    above_fold_cta_count: aboveFoldCtas.length,
    low_contrast_text_count: lowContrast.length,
    small_tap_target_count: smallTapTargets.length,
    image_failures: imageFailures.length,
    visible_text_count: textEls.length,
    interactive_count: interactEls.length,
    low_contrast_samples: lowContrast.slice(0, 5),
    small_tap_target_samples: smallTapTargetSamples,
    image_failure_samples: imageFailureSamples,
    fcp_ms: vitals.fcp_ms ?? null,
    lcp_ms: vitals.lcp_ms ?? null,
    lcp_element: vitals.lcp_element ?? null,
    cls: vitals.cls ?? null,
    inp_ms: vitals.inp_ms ?? null,
    dom_content_loaded_ms: vitals.dom_content_loaded_ms ?? null,
    load_ms: vitals.load_ms ?? null,
    ttfb_ms: vitals.ttfb_ms ?? null,
  };
})()
"""
