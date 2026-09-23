"""
Gemini AI analyser service.

Uses ``gemini-2.0-flash`` via the google-generativeai SDK.

Two public async functions:
  - ``analyse_website(page_data, business_name)``  → audit report dict
  - ``analyse_no_website(...)``                    → no-website report dict

Both functions parse the model's JSON response defensively and return
safe defaults if the model output is malformed.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Dict, List

import google.generativeai as genai

from app.config import get_settings

logger = logging.getLogger(__name__)

def _is_quota_error(exc: Exception) -> bool:
    """Return True if the exception is a Gemini / Google API quota exhaustion."""
    try:
        from google.api_core.exceptions import ResourceExhausted
        return isinstance(exc, ResourceExhausted)
    except ImportError:
        return "ResourceExhausted" in type(exc).__name__ or "429" in str(exc)


_MODEL_NAME = "gemini-2.0-flash"

# Gemini generation config — low temperature for consistent JSON
_GEN_CONFIG = {
    "temperature": 0.3,
    "top_p": 0.95,
    "max_output_tokens": 2048,
}


@lru_cache(maxsize=1)
def _get_model() -> genai.GenerativeModel:
    """Initialise and cache the Gemini model."""
    settings = get_settings()
    genai.configure(api_key=settings.gemini_api_key)
    return genai.GenerativeModel(
        model_name=_MODEL_NAME,
        generation_config=_GEN_CONFIG,
    )


# ---------------------------------------------------------------------------
# Website audit analysis
# ---------------------------------------------------------------------------


async def analyse_website(
    page_data: Dict[str, Any],
    business_name: str,
) -> Dict[str, Any]:
    """
    Send scraped page content to Gemini and return a structured audit dict.

    Scoring weights:
    - copy_score  40% of overall
    - ux_score    35% of overall
    - seo_score   25% of overall
    """
    prompt = _build_audit_prompt(page_data, business_name)

    try:
        model = _get_model()
        response = await _async_generate(model, prompt)
        raw_text = response.text
        data = _extract_json(raw_text)
        validated = _validate_audit_response(data, raw_text)
        browser_quality = _browser_quality_context(page_data)
        validated.update(browser_quality)
        return validated
    except Exception as exc:
        if _is_quota_error(exc):
            raise
        logger.error("Gemini audit analysis failed for '%s': %s", business_name, exc, exc_info=True)
        return _default_audit_response()


def _build_audit_prompt(page_data: Dict[str, Any], business_name: str) -> str:
    title = page_data.get("title", "")
    meta_desc = page_data.get("meta_description", "")
    h1s = "\n".join(page_data.get("h1_tags", []))
    h2s = "\n".join(page_data.get("h2_tags", []))
    ctas = "\n".join(page_data.get("cta_texts", []))
    body = page_data.get("body_text", "")[:5_000]
    footer = page_data.get("footer_text", "")[:500]
    browser_quality_summary = _browser_quality_summary(page_data)

    return f"""You are an expert digital marketing auditor. Analyse the following website content for the business "{business_name}" and return ONLY a valid JSON object — no markdown, no explanation, no code fences.

WEBSITE CONTENT:
Title: {title}
Meta Description: {meta_desc}
H1 Tags: {h1s}
H2 Tags: {h2s}
CTA Buttons: {ctas}
Body Text (excerpt):
{body}
Footer: {footer}

Browser quality checks:
{browser_quality_summary}

SCORING CRITERIA:
- copy_score (40% weight): Clarity, persuasiveness, emotional resonance, grammar, pain point targeting, compelling offer. Score 0–10.
- ux_score (35% weight): CTA visibility, navigation clarity, above-the-fold content quality, and browser-level usability signals. Score 0–10.
- seo_score (25% weight): Meta description quality, H1/H2 usage, title tag relevance, keyword presence. Score 0–10.
- overall_score: Weighted average — (copy_score * 0.4) + (ux_score * 0.35) + (seo_score * 0.25), rounded to nearest integer.

Return EXACTLY this JSON structure (no other text):
{{
  "overall_score": 7,
  "copy_score": 6,
  "seo_score": 8,
  "ux_score": 7,
  "has_clear_cta": true,
  "has_social_proof": false,
  "has_clear_value_proposition": true,
  "strengths": ["Strength 1", "Strength 2"],
  "weaknesses": ["Weakness 1", "Weakness 2"],
  "recommendations": ["Recommendation 1", "Recommendation 2", "Recommendation 3"]
}}"""


def _validate_audit_response(data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """Clamp scores and ensure all required keys are present."""
    def clamp(val: Any, default: int = 5) -> int:
        try:
            return max(0, min(10, int(val)))
        except (TypeError, ValueError):
            return default

    copy_score = clamp(data.get("copy_score", 5))
    ux_score = clamp(data.get("ux_score", 5))
    seo_score = clamp(data.get("seo_score", 5))
    overall = round(copy_score * 0.4 + ux_score * 0.35 + seo_score * 0.25)

    return {
        "overall_score": clamp(data.get("overall_score", overall)),
        "copy_score": copy_score,
        "seo_score": seo_score,
        "ux_score": ux_score,
        "has_clear_cta": bool(data.get("has_clear_cta", False)),
        "has_social_proof": bool(data.get("has_social_proof", False)),
        "has_clear_value_proposition": bool(data.get("has_clear_value_proposition", False)),
        "strengths": _ensure_list(data.get("strengths", [])),
        "weaknesses": _ensure_list(data.get("weaknesses", [])),
        "recommendations": _ensure_list(data.get("recommendations", [])),
        "raw_analysis": raw_text,
    }


def _default_audit_response() -> Dict[str, Any]:
    return {
        "overall_score": 0,
        "copy_score": 0,
        "seo_score": 0,
        "ux_score": 0,
        "has_clear_cta": False,
        "has_social_proof": False,
        "has_clear_value_proposition": False,
        "strengths": [],
        "weaknesses": ["Website analysis could not be completed."],
        "recommendations": [],
        "raw_analysis": "",
    }


def _browser_quality_context(page_data: Dict[str, Any]) -> Dict[str, Any]:
    browser_quality = page_data.get("browser_quality") or {}
    desktop = browser_quality.get("desktop") or {}
    mobile = browser_quality.get("mobile") or {}
    summary = browser_quality.get("summary") or _browser_quality_summary_from_metrics(desktop, mobile)
    score = browser_quality.get("score")
    if score is None:
        score = _browser_quality_score_from_metrics(desktop, mobile)
    return {
        "browser_quality": browser_quality,
        "browser_quality_summary": summary,
        "browser_quality_score": max(0, min(10, int(score or 0))),
    }


def _browser_quality_summary(page_data: Dict[str, Any]) -> str:
    browser_quality = page_data.get("browser_quality") or {}
    desktop = browser_quality.get("desktop") or {}
    mobile = browser_quality.get("mobile") or {}
    return browser_quality.get("summary") or _browser_quality_summary_from_metrics(desktop, mobile)


def _browser_quality_summary_from_metrics(desktop: Dict[str, Any], mobile: Dict[str, Any]) -> str:
    parts: list[str] = []
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


def _browser_quality_score_from_metrics(desktop: Dict[str, Any], mobile: Dict[str, Any]) -> int:
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


def _browser_quality_issues(metrics: Dict[str, Any]) -> list[str]:
    issues: list[str] = []
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


def _to_float(value: Any) -> float | None:
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


# ---------------------------------------------------------------------------
# No-website competitor analysis
# ---------------------------------------------------------------------------


async def analyse_no_website(
    business_name: str,
    niche: str,
    location: str,
    competitor_pages: List[Dict[str, Any]] | None = None,
    search_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Produce a no-website report using campaign-level organic search evidence.
    """
    competitor_pages = competitor_pages or []
    search_evidence = search_evidence or {}
    prompt = _build_no_website_prompt(
        business_name,
        niche,
        location,
        competitor_pages,
        search_evidence,
    )

    try:
        model = _get_model()
        response = await _async_generate(model, prompt)
        raw_text = response.text
        data = _extract_json(raw_text)
        return _validate_no_website_response(data, niche, location)
    except Exception as exc:
        if _is_quota_error(exc):
            raise
        logger.error(
            "Gemini no-website analysis failed for '%s': %s", business_name, exc, exc_info=True
        )
        return _default_no_website_response(niche, location, competitor_pages, search_evidence)


def _build_no_website_prompt(
    business_name: str,
    niche: str,
    location: str,
    competitor_pages: List[Dict[str, Any]],
    search_evidence: Dict[str, Any],
) -> str:
    business_results = search_evidence.get("business_results") or []
    organic_results = search_evidence.get("organic_results") or []
    selected_results = business_results[:3]
    result_summaries = []
    for result in selected_results:
        result_summaries.append(
            f"Organic result position {result.get('position')}:\n"
            f"  Title: {result.get('title', '')}\n"
            f"  Domain: {result.get('domain', '')}\n"
            f"  Link: {result.get('link', '')}\n"
            f"  Snippet: {result.get('snippet', '')}\n"
            f"  Confidence score: {result.get('confidence_score', 'unknown')}/100"
        )

    results_text = "\n\n".join(result_summaries) if result_summaries else "No useful organic search results were available."
    query = search_evidence.get("query") or f"{niche} in {location}"
    business_result_count = search_evidence.get("business_results_count", len(business_results))
    organic_result_count = search_evidence.get("organic_results_count", len(organic_results))
    has_useful_business_results = bool(business_results)
    claim_guardrails = _search_claim_guardrails(search_evidence)

    return f"""You are a data-driven local search strategist. A business called "{business_name}" in the {niche} industry in {location} has NO website.

Search evidence:
- Query checked: "{query}"
- Organic results found: {organic_result_count}
- Business-owned organic results kept: {business_result_count}
- Useful business-owned examples available: {has_useful_business_results}

Top organic visibility examples:
{results_text}

Claim guardrails:
{claim_guardrails}

Your job is to explain the search visibility gap. Do NOT claim that a competitor's website is good unless the title/snippet supports it. Do NOT claim these are ads or Maps rankings. The evidence is organic Google search result data. Focus on the fact that a business with no website has no owned page that can rank organically, educate customers, or convert search demand.

If no useful business-owned examples are available, return an empty competitors list and focus on the absence of an owned website as the main missed opportunity.

Return ONLY a valid JSON object — strictly following this structure:
{{
  "competitors": [
    {{
      "name": "Visible organic result title or business name",
      "website": "https://...",
      "estimated_online_presence_score": <integer 1-10 based only on organic result position, domain ownership, title relevance, and snippet relevance>,
      "why_they_win_online": "Specific explanation based on their organic result title/snippet and why that helps customers find or trust them"
    }}
  ],
  "missed_opportunities": [
    "3 specific missed opportunities tied to organic search visibility, customer trust, and quote/contact conversion."
  ],
  "potential_revenue_impact": "Write a 1-sentence realistic projection of revenue risk specific to the {niche} industry in {location}. Avoid pretending to know exact revenue.",
  "recommendations": [
    "3-4 actionable recommendations for a simple website that can support organic visibility: homepage positioning, service pages, location/service-area content, proof/trust elements, and clear contact/quote CTA."
  ]
}}"""

    competitor_summaries = []
    for i, cp in enumerate(competitor_pages, 1):
        biz = cp.get("business", {})
        page = cp.get("page", {})
        competitor_summaries.append(
            f"Competitor {i}:\n"
            f"  Name: {biz.get('business_name', 'Unknown')}\n"
            f"  Website: {biz.get('website', '')}\n"
            f"  Title: {page.get('title', '')}\n"
            f"  H1: {'; '.join(page.get('h1_tags', []))}\n"
            f"  Body excerpt: {page.get('body_text', '')[:1500]}"
        )

    competitors_text = "\n\n".join(competitor_summaries) if competitor_summaries else "No competitor data available."

    return f"""You are a ruthless but data-driven digital marketing strategist. A business called "{business_name}" in the {niche} industry in {location} has NO website. 

Here is content scraped from their top local competitors who DO have websites:
{competitors_text}

Your job is to show "{business_name}" exactly what they are losing by directly referencing what their competitors are doing right (or wrong, but still capturing traffic). Do not use generic marketing cliches (e.g. "24/7 storefront" or "word of mouth"). Be hyper-specific to the {niche} industry in {location}.

Analyse the competitors' copy, H1s, and messaging to extract unique insights.

Return ONLY a valid JSON object — strictly following this structure:
{{
  "competitors": [
    {{
      "name": "Competitor Business Name",
      "website": "https://...",
      "estimated_online_presence_score": <integer 1-10, vary this realistically based on the quality of their scraped content, not just an 8 for everyone>,
      "why_they_win_online": "Specific insight into their messaging, SEO title, or offer based on their actual body text"
    }}
  ],
  "missed_opportunities": [
    "3 highly specific, distinct missed opportunities. Base these on the exact services or features the competitors are marketing."
  ],
  "potential_revenue_impact": "Write a 1-sentence urgent, realistic projection of revenue loss specific to the {niche} industry in {location}, using realistic local pricing metrics. Avoid generic templates — make it sound like a bespoke consultation.",
  "recommendations": [
    "3–4 actionable, non-generic recommendations. Rather than 'build a website', tell them exactly what kind of website, what the H1 should be to beat these specific competitors, and what unique value proposition they should highlight."
  ]
}}"""


def _validate_no_website_response(
    data: Dict[str, Any],
    niche: str,
    location: str,
) -> Dict[str, Any]:
    competitors = []
    for c in _ensure_list(data.get("competitors", [])):
        if isinstance(c, dict):
            competitors.append({
                "name": str(c.get("name", "")),
                "website": str(c.get("website", "")),
                "estimated_online_presence_score": max(
                    0, min(10, int(c.get("estimated_online_presence_score", 5)))
                ),
                "why_they_win_online": str(c.get("why_they_win_online", "")),
            })

    return {
        "competitors": competitors,
        "missed_opportunities": _ensure_list(data.get("missed_opportunities", [])),
        "potential_revenue_impact": str(data.get("potential_revenue_impact", "")),
        "recommendations": _ensure_list(data.get("recommendations", [])),
    }


def _default_no_website_response(
    niche: str,
    location: str,
    competitor_pages: List[Dict[str, Any]],
    search_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    search_evidence = search_evidence or {}
    competitors = []
    for result in (search_evidence.get("business_results") or [])[:3]:
        competitors.append({
            "name": result.get("title", "Visible organic result"),
            "website": result.get("link", ""),
            "estimated_online_presence_score": 6,
            "why_they_win_online": "This result has an owned page visible in organic Google results.",
        })
    return {
        "competitors": competitors,
        "missed_opportunities": [
            f"The business has no owned website page that can rank for '{niche} in {location}'.",
            "Customers have less information to verify services before calling.",
        ],
        "potential_revenue_impact": "Analysis unavailable — please regenerate.",
        "recommendations": ["Build a simple service-area website to support organic visibility and customer trust."],
    }

    competitors = [
        {
            "name": cp["business"].get("business_name", "Unknown"),
            "website": cp["business"].get("website", ""),
            "estimated_online_presence_score": 7,
            "why_they_win_online": "Has an established online presence.",
        }
        for cp in competitor_pages
        if cp.get("business", {}).get("website")
    ]
    return {
        "competitors": competitors,
        "missed_opportunities": [
            f"Potential customers searching for '{niche} in {location}' cannot find you online.",
            "Competitors with websites are capturing leads around the clock.",
        ],
        "potential_revenue_impact": "Analysis unavailable — please regenerate.",
        "recommendations": ["Build a professional website to compete online."],
    }


def _search_claim_guardrails(search_evidence: Dict[str, Any]) -> str:
    business_results = search_evidence.get("business_results") or []
    if business_results:
        domains = [
            str(result.get("domain") or result.get("root_domain") or result.get("link") or "")
            for result in business_results[:3]
            if result.get("domain") or result.get("root_domain") or result.get("link")
        ]
        return (
            "- Allowed: say business-owned websites appear in organic Google results for the checked query.\n"
            f"- Allowed: mention these visible domains only if useful: {', '.join(domains)}.\n"
            "- Allowed: say a no-website business lacks an owned page that can rank organically or convert searchers.\n"
            "- Not allowed: claim anything about Google Ads, Google Maps rankings, traffic volume, revenue certainty, or website quality not supported by the snippet."
        )
    return (
        "- Allowed: say the search did not provide verified business-owned organic examples after filtering directories/social/forums.\n"
        "- Allowed: focus on the risk of having no owned website that can rank organically or convert searchers.\n"
        "- Not allowed: name competitors, imply specific competitors are winning, or claim ads/Maps rankings, traffic volume, revenue certainty, or website quality."
    )


# ---------------------------------------------------------------------------
# Cold email generation
# ---------------------------------------------------------------------------


async def generate_audit_email(
    business_name: str,
    audit_data: Dict[str, Any],
    slug: str,
    base_url: str,
) -> Dict[str, str]:
    """Generate a cold email for a lead WITH a website. Returns {subject, body}."""
    report_url = f"{base_url.rstrip('/')}/audit/{slug}"
    weaknesses = audit_data.get("weaknesses", [])[:2]
    overall_score = audit_data.get("overall_score", 0)
    copy_score = audit_data.get("copy_score", 0)
    browser_quality_summary = audit_data.get("browser_quality_summary") or "No browser quality signal was captured."
    browser_quality_score = audit_data.get("browser_quality_score")

    prompt = f"""You are a senior digital marketing consultant writing a cold email to the owner of "{business_name}".

Context:
- Their website scored {overall_score}/10 overall, {copy_score}/10 on copy clarity.
- Specific weaknesses: {'; '.join(weaknesses) if weaknesses else 'unclear messaging and weak calls to action'}.
- Browser quality signal: {browser_quality_summary}
- Browser quality score: {browser_quality_score if browser_quality_score is not None else 'unknown'}/10
- Their full audit report is at: {report_url}

Write a cold email that:
1. Opens with their business name — no generic "Hello there"
2. References their audit score (e.g. "your site scored {copy_score}/10 on copy clarity")
3. Calls out 1–2 specific weaknesses by name
4. Positions our agency as the fix — confident, not desperate
5. Includes the report link naturally in the body
6. Uses the browser quality signal to speak about what the business owner would care about: mobile friction, readability, contrast, CTA visibility, or layout issues if relevant
7. Closes with a clear, low-friction CTA (e.g. "Got 15 minutes this week?")
8. Tone: direct, confident, like a colleague giving honest feedback — NOT salesy
9. Maximum 150 words

Return ONLY valid JSON:
{{"subject": "...", "body": "..."}}"""

    return await _generate_email(prompt, business_name)


async def generate_no_website_email(
    business_name: str,
    location: str,
    niche: str,
    no_website_data: Dict[str, Any],
    slug: str,
    base_url: str,
    generated_website_url: str | None = None,
) -> Dict[str, str]:
    """Generate a cold email for a lead WITHOUT a website. Returns {subject, body}."""
    report_url = f"{base_url.rstrip('/')}/no-website/{slug}"
    competitors = no_website_data.get("competitors", [])[:2]
    competitor_names = [c.get("name", "") for c in competitors if c.get("name")]
    missed = no_website_data.get("missed_opportunities", [])[:1]
    search_evidence = no_website_data.get("search_evidence") or {}
    query = search_evidence.get("query") or f"{niche} in {location}"
    claim_guardrails = _search_claim_guardrails(search_evidence)

    prompt = f"""You are a senior digital marketing consultant writing a cold email to the owner of "{business_name}", a {niche} business in {location} that has NO website.

Context:
- Search query checked: "{query}"
- Evidence source: organic Google search results, not ads or Maps rankings
- Organic business-owned examples visible for this search: {', '.join(competitor_names) if competitor_names else 'none verified'}
- Key missed opportunity: {missed[0] if missed else f"Every month someone in {location} searches for {niche} and your competitors get that call, not you"}
- Their competitor analysis report is at: {report_url}
- Preview website we already prepared for them: {generated_website_url or 'not available'}

Claim guardrails:
{claim_guardrails}

Write a cold email that:
1. Opens with their business name and location — no generic openers
2. References the organic Google visibility gap without claiming ads or Maps rankings
3. Uses a specific missed opportunity tied to not having an owned website
4. Positions our agency as the team to build and launch their site fast
5. References the prepared preview website if available; otherwise reference the competitor report link naturally
6. Closes with a low-friction CTA
7. Tone: empathetic but urgent — they are losing real money every day
8. Maximum 150 words

Return ONLY valid JSON:
{{"subject": "...", "body": "..."}}"""

    return await _generate_email(prompt, business_name)


async def _generate_email(prompt: str, business_name: str) -> Dict[str, str]:
    try:
        model = _get_model()
        response = await _async_generate(model, prompt)
        data = _extract_json(response.text)
        return {
            "subject": str(data.get("subject", f"Your online presence — {business_name}")),
            "body": str(data.get("body", "")),
        }
    except Exception as exc:
        if _is_quota_error(exc):
            raise
        logger.error("Gemini email generation failed for '%s': %s", business_name, exc)
        return {
            "subject": f"Quick note about {business_name}'s online presence",
            "body": "",
        }


async def rewrite_outreach_email(
    business_name: str,
    subject: str,
    body: str,
    factual_guardrails: str,
) -> Dict[str, str]:
    """Optionally polish deterministic outreach copy without adding new claims."""
    prompt = f"""Rewrite this cold email so it sounds like a sharp peer wrote it.

Rules:
- Keep it under 150 words.
- Keep exactly one low-friction ask.
- Do not add claims, metrics, competitor names, guarantees, or facts not present below.
- Keep the report/preview URL if one appears in the draft.
- Avoid sales jargon and generic openers.

Business: {business_name}
Allowed facts:
{factual_guardrails}

Draft subject: {subject}
Draft body:
{body}

Return ONLY valid JSON:
{{"subject": "...", "body": "..."}}"""
    return await _generate_email(prompt, business_name)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> Dict[str, Any]:
    """
    Robustly extract the first JSON object from a Gemini response.
    Handles cases where the model wraps the JSON in markdown fences.
    """
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?", "", text).strip()

    # Find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as exc:
            logger.warning("JSON decode error in Gemini response: %s\nRaw: %s", exc, text[:500])
    return {}


def _ensure_list(val: Any) -> List[Any]:
    if isinstance(val, list):
        return val
    return []


async def _async_generate(model: genai.GenerativeModel, prompt: str):
    """
    Run the synchronous Gemini generate_content call in a thread pool
    so it doesn't block the asyncio event loop.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, model.generate_content, prompt)
