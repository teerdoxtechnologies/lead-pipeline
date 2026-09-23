"""
SerpAPI organic search evidence.

Used once per campaign search context to support no-website reports without
re-scraping Google Maps or competitor websites per lead.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_SERPAPI_URL = "https://serpapi.com/search.json"
_DIRECTORY_DOMAINS = (
    "google.com",
    "google.co",
    "yelp.com",
    "angi.com",
    "angieslist.com",
    "thumbtack.com",
    "homeadvisor.com",
    "bbb.org",
    "yellowpages.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "mapquest.com",
    "tripadvisor.com",
    "nextdoor.com",
    "wikipedia.org",
    "foursquare.com",
    "trustpilot.com",
)


def collect_organic_search_evidence(niche: str, location: str) -> Dict[str, Any]:
    settings = get_settings()
    query = f"{niche} in {location}"
    now = _utc_now_iso()
    evidence: Dict[str, Any] = {
        "provider": "serpapi",
        "query": query,
        "normalized_query": normalize_search_query(niche, location),
        "generated_at": now,
        "last_search_evidence_refreshed_at": now,
        "search_evidence_age_days": 0,
        "organic_results": [],
        "business_results": [],
        "status": "not_configured",
        "has_useful_business_results": False,
    }

    if not settings.serpapi_api_key:
        logger.warning("SERPAPI_API_KEY is not configured; organic evidence skipped.")
        return evidence

    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                _SERPAPI_URL,
                params={
                    "engine": "google",
                    "q": query,
                    "api_key": settings.serpapi_api_key,
                    "num": settings.serpapi_results_limit,
                    "hl": "en",
                    "gl": "us",
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("SerpAPI organic search failed for '%s': %s", query, exc)
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        return evidence

    organic_results = [
        _organic_result_payload(result, niche=niche, location=location)
        for result in data.get("organic_results", [])
        if result.get("link")
    ]
    business_results = [
        result for result in organic_results
        if _looks_like_business_owned_result(result) and result.get("confidence_score", 0) >= 50
    ][:3]

    evidence.update({
        "status": "completed",
        "organic_results": organic_results,
        "business_results": business_results,
        "organic_results_count": len(organic_results),
        "business_results_count": len(business_results),
        "has_useful_business_results": bool(business_results),
    })
    logger.info(
        "SerpAPI evidence collected for '%s': %d organic results, %d business-owned results.",
        query,
        len(organic_results),
        len(business_results),
    )
    return evidence


def normalize_search_query(niche: str, location: str) -> str:
    return _compact_text(f"{niche} in {location}")


def evidence_age_days(evidence: Dict[str, Any]) -> int | None:
    refreshed_at = evidence.get("last_search_evidence_refreshed_at") or evidence.get("generated_at")
    if not refreshed_at:
        return None
    try:
        refreshed = datetime.fromisoformat(str(refreshed_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((datetime.now(timezone.utc) - refreshed).days, 0)


def is_search_evidence_stale(evidence: Dict[str, Any], stale_after_days: int) -> bool:
    age = evidence_age_days(evidence)
    if age is None:
        return True
    return age >= max(int(stale_after_days), 0)


def with_freshness_metadata(evidence: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(evidence)
    age = evidence_age_days(enriched)
    if age is not None:
        enriched["search_evidence_age_days"] = age
    return enriched


def _organic_result_payload(
    result: Dict[str, Any],
    niche: str = "",
    location: str = "",
) -> Dict[str, Any]:
    link = str(result.get("link", ""))
    payload = {
        "position": result.get("position"),
        "title": result.get("title"),
        "link": link,
        "domain": _domain(link),
        "root_domain": _root_domain(link),
        "snippet": result.get("snippet"),
    }
    payload["confidence_score"] = _business_result_confidence(payload, niche, location)
    return payload


def _looks_like_business_owned_result(result: Dict[str, Any]) -> bool:
    domain = str(result.get("root_domain") or result.get("domain") or "").lower()
    if not domain:
        return False
    return not any(_domain_matches(domain, blocked) for blocked in _DIRECTORY_DOMAINS)


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _root_domain(url_or_domain: str) -> str:
    domain = urlparse(url_or_domain).netloc or url_or_domain
    domain = domain.lower().lstrip("www.").strip(".")
    parts = [part for part in domain.split(".") if part]
    if len(parts) <= 2:
        return domain
    if len(parts[-1]) == 2 and len(parts[-2]) <= 3 and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _business_result_confidence(result: Dict[str, Any], niche: str, location: str) -> int:
    if not _looks_like_business_owned_result(result):
        return 0

    score = 40
    position = result.get("position")
    try:
        position_int = int(position)
    except (TypeError, ValueError):
        position_int = None
    if position_int is not None:
        if position_int <= 3:
            score += 25
        elif position_int <= 10:
            score += 15

    searchable_text = _compact_text(
        f"{result.get('title', '')} {result.get('snippet', '')} {result.get('domain', '')}"
    )
    niche_tokens = _meaningful_tokens(niche)
    location_tokens = _meaningful_tokens(location)
    if niche_tokens and any(token in searchable_text for token in niche_tokens):
        score += 20
    if location_tokens and any(token in searchable_text for token in location_tokens):
        score += 10
    if result.get("link", "").startswith("https://"):
        score += 5
    return min(score, 100)


def _domain_matches(domain: str, blocked: str) -> bool:
    return domain == blocked or domain.endswith(f".{blocked}")


def _meaningful_tokens(value: str) -> List[str]:
    return [token for token in _compact_text(value).split() if len(token) >= 3]


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value).lower())).strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
