"""Deterministic website appraisal and audit-email templates.

This module keeps factual website findings separate from AI wording. It uses
scraped page structure plus browser-quality metrics to produce an audit report
shape compatible with the existing app.
"""
from __future__ import annotations

import json
import hashlib
import re
from typing import Any, Dict, List, Optional


_CTA_RE = re.compile(
    r"\b(book|schedule|appointment|contact|get started|free|quote|call|sign up|"
    r"register|buy|order|consult|try|request|estimate|enquire|book now)\b",
    re.IGNORECASE,
)
_PROOF_RE = re.compile(
    r"\b(review|reviews|testimonial|testimonials|rated|rating|stars?|clients?|"
    r"customers?|trusted|licensed|insured|certified|years? experience|award)\b",
    re.IGNORECASE,
)
_VALUE_RE = re.compile(
    r"\b(save|faster|reliable|professional|local|same day|guarantee|quality|"
    r"trusted|affordable|clean|secure|easy|convenient|expert|specialist)\b",
    re.IGNORECASE,
)


def appraise_website(page_data: Dict[str, Any], business_name: str) -> Dict[str, Any]:
    """Return a deterministic audit report for a lead with an existing website."""
    title = _text(page_data.get("title"))
    meta = _text(page_data.get("meta_description"))
    h1s = [_text(value) for value in page_data.get("h1_tags", []) if _text(value)]
    h2s = [_text(value) for value in page_data.get("h2_tags", []) if _text(value)]
    ctas = [_text(value) for value in page_data.get("cta_texts", []) if _text(value)]
    body = _text(page_data.get("body_text"))
    footer = _text(page_data.get("footer_text"))
    combined_text = " ".join(part for part in [title, meta, " ".join(h1s), " ".join(h2s), body, footer] if part)
    browser_quality = page_data.get("browser_quality") or {}
    browser_score = _bounded_int(browser_quality.get("score"), default=5)
    browser_summary = browser_quality.get("summary") or "No browser quality data collected."

    has_clear_cta = bool(ctas) or bool(_CTA_RE.search(combined_text))
    has_social_proof = bool(_PROOF_RE.search(combined_text))
    has_clear_value_proposition = bool(_VALUE_RE.search(" ".join([title, meta, " ".join(h1s), body[:1500]])))

    copy_score = _copy_score(body, title, meta, h1s, ctas, has_social_proof, has_clear_value_proposition)
    seo_score = _seo_score(title, meta, h1s, h2s)
    ux_score = _ux_score(browser_score, has_clear_cta)
    overall_score = round(copy_score * 0.4 + ux_score * 0.35 + seo_score * 0.25)

    findings = _rank_findings(
        browser_quality=browser_quality,
        copy_score=copy_score,
        seo_score=seo_score,
        has_clear_cta=has_clear_cta,
        has_social_proof=has_social_proof,
        has_clear_value_proposition=has_clear_value_proposition,
        title=title,
        meta=meta,
        h1s=h1s,
    )
    strengths = _strengths(
        has_clear_cta=has_clear_cta,
        has_social_proof=has_social_proof,
        has_clear_value_proposition=has_clear_value_proposition,
        browser_score=browser_score,
        seo_score=seo_score,
    )
    weaknesses = [finding["observation"] for finding in findings[:3]]
    recommendations = [finding["recommendation"] for finding in findings[:3]]

    raw = {
        "method": "deterministic_website_appraisal_v1",
        "business_name": business_name,
        "findings": findings,
        "browser_quality_summary": browser_summary,
    }
    return {
        "overall_score": _bounded_int(overall_score),
        "copy_score": copy_score,
        "seo_score": seo_score,
        "ux_score": ux_score,
        "has_clear_cta": has_clear_cta,
        "has_social_proof": has_social_proof,
        "has_clear_value_proposition": has_clear_value_proposition,
        "strengths": strengths,
        "weaknesses": weaknesses or ["The site has no obvious high-priority issues from the automated checks."],
        "recommendations": recommendations or ["Keep the main conversion path clear and easy to act on."],
        "browser_quality": browser_quality,
        "browser_quality_score": browser_score,
        "browser_quality_summary": browser_summary,
        "appraisal_method": "deterministic",
        "findings": findings,
        "raw_analysis": json.dumps(raw, ensure_ascii=True, default=str),
    }


def generate_audit_email_template(
    business_name: str,
    audit_data: Dict[str, Any],
    slug: str,
    base_url: str,
) -> Dict[str, str]:
    """Build a plain-text cold email from deterministic audit findings."""
    report_url = f"{base_url.rstrip('/')}/audit/{slug}"
    finding = _primary_email_finding(audit_data)
    subject = _subject_for_finding(finding)
    variant = _email_template_variant(subject, slug)
    body = _body_for_finding(business_name, finding, report_url, variant=variant)
    if variant == "B" and subject == "Noticed something on your website":
        subject = "Something affecting your website on mobile"
    return {"subject": subject, "body": body, "email_template_variant": variant}


def _primary_email_finding(audit_data: Dict[str, Any]) -> Dict[str, str]:
    findings = audit_data.get("findings") or []
    if findings:
        finding = findings[0]
        return {
            "category": _text(finding.get("category")) or "website friction",
            "email_observation": _text(finding.get("email_observation"))
            or _text(finding.get("observation"))
            or "a few points where the first visit could be clearer",
            "business_impact": _text(finding.get("business_impact"))
            or "That can make it harder for a visitor to take the next step.",
            "recommendation": _text(finding.get("recommendation")),
        }
    weakness = (audit_data.get("weaknesses") or ["a few points where the first visit could be clearer"])[0]
    return {
        "category": "website friction",
        "email_observation": _text(weakness),
        "business_impact": "That can make it harder for a visitor to take the next step.",
        "recommendation": "",
    }


def _body_for_finding(
    business_name: str,
    finding: Dict[str, str],
    report_url: str,
    variant: str = "A",
) -> str:
    category = finding.get("category", "")
    observation = finding["email_observation"].rstrip(".")
    impact = finding["business_impact"].rstrip(".")
    greeting = f"Hi {business_name} team,"
    site_reference = f"the website for {business_name}"

    if category in {"page speed", "page load"}:
        load_seconds = _load_seconds_from_text(observation)
        timing = f"about {load_seconds}s" if load_seconds else "longer than it should"
        return "\n\n".join([
            greeting,
            (
                "I was looking through your website and noticed the homepage "
                f"takes {timing} to load on mobile."
            ),
            (
                "For people searching for cleaning services on their phones, "
                "that delay can sometimes mean fewer calls and quote requests."
            ),
            "I put together a short audit with a few issues I found that may be impacting conversions:",
            report_url,
            "Feel free to share the report with whoever manages the site. If you'd like help implementing any of the recommendations, I'd be happy to help.",
            "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
        ])
    if category == "layout stability":
        return "\n\n".join([
            greeting,
            (
                "I noticed an issue on your mobile website that may be making it harder "
                "for visitors to interact with the site."
            ),
            (
                "Some content shifts around while the page is loading, which can interrupt "
                "visitors as they're trying to tap buttons, read information, or submit forms. "
                "That extra friction can make it less likely that visitors take the next step and contact you."
            ),
            f"I put together a short audit highlighting this and a few other improvements: {report_url}",
            "Feel free to share it with whoever manages the website. If you'd like help addressing any of the recommendations, I'd be happy to help.",
            "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
        ])
    if category == "conversion path":
        return "\n\n".join([
            greeting,
            f"I looked through {site_reference} and noticed the next step for visitors is not very obvious. {_capitalize_sentence(observation)}.",
            f"That matters because {impact}.",
            f"I also put together a short report with this and a few other things I noticed: {report_url}",
            "You can share it with whoever built your site, or if it's easier, I can help make the quote/contact path clearer.",
            "Would it be useful if I pointed out what I'd fix first?",
        ])
    if category == "readability":
        if variant == "B":
            return "\n\n".join([
                greeting,
                (
                    "I noticed a mobile readability issue on your website that may be "
                    "making some text harder to read on phones."
                ),
                (
                    "On mobile, some text doesn't stand out clearly from the background, "
                    "which can reduce how easily visitors find information and contact you."
                ),
                f"I put together a short audit highlighting this and a couple of other improvements: {report_url}",
                "If it's helpful, I can also explain how to implement the fixes or help you handle them.",
                "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
            ])
        return "\n\n".join([
            greeting,
            (
                "I looked through your website and noticed some important information "
                "can be hard to read on mobile devices because the text doesn't stand "
                "out clearly from the background. That matters because some visitors "
                "may leave before finding the information they need to contact your business."
            ),
            f"I also put together a short report with this and a few other things I noticed: {report_url}",
            "Feel free to share the report with whoever manages the site. If you'd like help addressing any of the recommendations, I'd be happy to help.",
            "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
        ])
    if category == "mobile layout":
        return "\n\n".join([
            greeting,
            (
                "I noticed an issue on your mobile website that may be affecting "
                "the experience for visitors."
            ),
            (
                "Some content extends beyond the width of the screen on mobile devices, "
                "which can make it harder for visitors to view information and navigate the site. "
                "When people are comparing cleaning services, small usability issues like this can "
                "create unnecessary friction before they contact you."
            ),
            f"I put together a short audit highlighting this and a few other improvements: {report_url}",
            "Feel free to share it with whoever manages the website. If you'd like help addressing any of the recommendations, I'd be happy to help.",
            "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
        ])
    if category == "mobile usability":
        return "\n\n".join([
            greeting,
            (
                "I noticed a mobile issue on your website that may be affecting "
                "how easily visitors complete key actions like booking, calling, "
                "or contacting your business."
            ),
            (
                "Some buttons and clickable elements appear too small or tightly "
                "spaced on mobile devices, which can make it easy for users to tap "
                "the wrong item or miss key actions altogether. This kind of friction "
                "can reduce the number of visitors who complete bookings or inquiries."
            ),
            f"I put together a short report with this and a few other improvements: {report_url}",
            "Feel free to share it with whoever built your site. If you'd like, I can also help you prioritize the highest-impact fixes first.",
            "Best regards,\nAndrew Ezeani\nTeerdox Technologies\nhttps://www.teerdox.com",
        ])
    if category == "trust":
        return "\n\n".join([
            greeting,
            f"I looked through {site_reference} and noticed it could use stronger trust signals. {_capitalize_sentence(observation)}.",
            f"That matters because {impact}.",
            f"I also put together a short report with this and a few other things I noticed: {report_url}",
            "You can share it with whoever built your site, or if it's easier, I can help make the page feel more credible to first-time visitors.",
            "Would it be useful if I pointed out what I'd fix first?",
        ])
    if category in {"message clarity", "copy clarity"}:
        return "\n\n".join([
            greeting,
            f"I looked through {site_reference} and noticed the message could be clearer for first-time visitors. {_capitalize_sentence(observation)}.",
            f"That matters because {impact}.",
            f"I also put together a short report with this and a few other things I noticed: {report_url}",
            "You can share it with whoever built your site, or if it's easier, I can help sharpen the highest-impact sections first.",
            "Would it be useful if I pointed out what I'd fix first?",
        ])
    if category == "search basics":
        return "\n\n".join([
            greeting,
            f"I looked through {site_reference} and noticed some basic search signals could be stronger. {_capitalize_sentence(observation)}.",
            f"That matters because {impact}.",
            f"I also put together a short report with this and a few other things I noticed: {report_url}",
            "You can share it with whoever built your site, or if it's easier, I can help clean up the highest-impact search basics first.",
            "Would it be useful if I pointed out what I'd fix first?",
        ])
    return "\n\n".join([
        greeting,
        f"I looked through {site_reference} and noticed {observation}.",
        impact,
        f"I also put together a short report with this and a few other things I noticed: {report_url}",
        "You can share it with whoever built your site, or if it's easier, I can help fix the highest-impact items first.",
        "Would it be useful if I pointed out what I'd fix first?",
    ])


def _rank_findings(
    *,
    browser_quality: Dict[str, Any],
    copy_score: int,
    seo_score: int,
    has_clear_cta: bool,
    has_social_proof: bool,
    has_clear_value_proposition: bool,
    title: str,
    meta: str,
    h1s: List[str],
) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    for label, metrics in (("mobile", browser_quality.get("mobile") or {}), ("desktop", browser_quality.get("desktop") or {})):
        lcp_ms = _to_float(metrics.get("lcp_ms"))
        cls = _to_float(metrics.get("cls"))
        load_ms = _to_float(metrics.get("load_ms"))
        if lcp_ms and lcp_ms > 2_500:
            culprit = _lcp_culprit_phrase(metrics.get("lcp_element"))
            lcp_details = _lcp_evidence(metrics.get("lcp_element"))
            observation = (
                f"on {label}, {culprit} takes about {round(lcp_ms / 100) / 10:.1f}s to show up"
                if culprit
                else f"on {label}, the main visible content takes about {round(lcp_ms / 100) / 10:.1f}s to show up"
            )
            findings.append(_finding(
                "page speed",
                observation,
                "that first impression can feel slow before a visitor decides to call or request a quote.",
                "Speed up that above-the-fold content and reduce render-blocking assets.",
                details=lcp_details,
            ))
        if cls and cls > 0.1:
            findings.append(_finding(
                "layout stability",
                f"{label} layout shift is {cls:.2f}",
                "moving content can make the page feel less polished and interrupt form or CTA clicks.",
                "Reserve space for images, embeds, and late-loading sections to keep the layout stable.",
            ))
        if load_ms and load_ms > 5_000:
            findings.append(_finding(
                "page load",
                f"{label} full page load is about {round(load_ms / 100) / 10:.1f}s",
                "long waits can cost calls from people comparing providers quickly.",
                "Reduce heavy scripts, compress images, and defer non-critical assets.",
            ))
        if metrics.get("horizontal_overflow"):
            findings.append(_finding(
                "mobile layout",
                f"{label} content overflows horizontally",
                "visitors may need to pinch or scroll sideways before they can act.",
                "Fix wide elements so the page fits cleanly on the viewport.",
            ))
        if int(metrics.get("low_contrast_text_count") or 0) > 0:
            findings.append(_finding(
                "readability",
                f"{label} has low-contrast text",
                "important copy can be harder to read, especially on mobile or in bright light.",
                "Raise text/background contrast on key sections and buttons.",
                details=_contrast_evidence(metrics.get("low_contrast_samples")),
            ))
        small_targets = int(metrics.get("small_tap_target_count") or 0)
        if small_targets > 0:
            if label == "mobile":
                findings.append(_finding(
                    "mobile usability",
                    f"mobile has {small_targets} tap/click targets below 44px",
                    "customers using a phone one-handed may struggle to hit booking, phone, menu, or contact actions cleanly.",
                    "Increase target height, width, and spacing around important navigation, booking, phone, and contact actions.",
                    details=_tap_target_evidence(metrics.get("small_tap_target_samples")),
                ))
            else:
                findings.append(_finding(
                    "usability",
                    f"desktop has {small_targets} click/tap targets below 44px",
                    "small links and buttons are easier to miss, especially on touch laptops, tablets, or for customers with vision or motor difficulty.",
                    "Increase target height, width, and spacing around important navigation, booking, phone, and contact actions.",
                    details=_tap_target_evidence(metrics.get("small_tap_target_samples")),
                ))
        failed_images = int(metrics.get("image_failures") or 0)
        failed_image_details = _image_failure_evidence(metrics.get("image_failure_samples"))
        if failed_images > 0 and failed_image_details:
            findings.append(_finding(
                "visual trust",
                f"{label} has {failed_images} failed image(s)",
                "broken visuals can make the business feel less maintained before a visitor decides to trust the page.",
                "Replace missing images, fix bad image URLs, and keep important visuals loading reliably.",
                details=failed_image_details,
            ))
        if int(metrics.get("above_fold_cta_count") or 0) == 0:
            findings.append(_finding(
                "conversion path",
                f"{label} has no clear CTA visible above the fold",
                "ready-to-contact visitors may not see the next step immediately.",
                "Put one specific action near the first screen, such as requesting a quote.",
            ))

    if not has_clear_cta:
        findings.append(_finding(
            "conversion path",
            "the page does not show a clear call to action in the scraped content",
            "visitors may understand what you do but still not know what to do next.",
            "Use one repeated primary CTA tied to the buying step, not a generic link.",
        ))
    if not has_clear_value_proposition:
        findings.append(_finding(
            "message clarity",
            "the main message does not make the customer outcome obvious",
            "visitors may compare on price instead of understanding why to choose you.",
            "Lead with the result customers get, then support it with service proof.",
        ))
    if not has_social_proof:
        findings.append(_finding(
            "trust",
            "the page does not show obvious trust signals in the scraped content",
            "new visitors have less evidence that you are safe to contact.",
            "Add reviews, credentials, years in business, or a clear local proof point.",
        ))
    if seo_score < 6:
        missing = "page title, description, or main heading"
        if not title:
            missing = "a page title"
        elif not meta:
            missing = "a meta description"
        elif not h1s:
            missing = "a visible main page heading"
        findings.append(_finding(
            "search basics",
            f"the page is weak on {missing}",
            "Google and visitors may get a less clear first impression of the service.",
            "Tighten the title, meta description, and visible page heading around the service.",
        ))
    if copy_score < 6:
        findings.append(_finding(
            "copy clarity",
            "the page copy does not give enough persuasive context",
            "visitors may leave without a strong reason to choose you over another provider.",
            "Clarify who you help, what outcome you deliver, and why customers trust you.",
        ))
    return _dedupe_findings(findings)


def _finding(
    category: str,
    observation: str,
    impact: str,
    recommendation: str,
    *,
    details: Optional[List[str]] = None,
) -> Dict[str, Any]:
    finding: Dict[str, Any] = {
        "category": category,
        "observation": observation,
        "email_observation": observation,
        "business_impact": impact,
        "recommendation": recommendation,
    }
    if details:
        finding["details"] = details
    return finding


def _dedupe_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    for finding in findings:
        key = (finding.get("category"), finding.get("observation"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _lcp_evidence(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return []
    details = []
    section = _section_phrase(value.get("section"))
    if section:
        details.append(f"Where to look: {section}.")
    url = _text(value.get("url"))
    text = _text(value.get("text"))
    alt = _text(value.get("alt"))
    if url:
        details.append(f"Slow content: image/media URL {url}.")
    elif text:
        details.append(f"Slow content: text block \"{text[:180]}\".")
    elif alt:
        details.append(f"Slow content: image with alt text \"{alt[:180]}\".")
    selector = _text(value.get("selector"))
    if selector:
        details.append(f"Page selector: {selector}.")
    return details[:4]


def _contrast_evidence(samples: Any) -> List[str]:
    if not isinstance(samples, list):
        return []
    details = []
    for sample in samples[:5]:
        if not isinstance(sample, dict):
            continue
        text = _text(sample.get("text")) or _text(sample.get("selector")) or "text element"
        ratio = sample.get("ratio")
        section = _section_phrase(sample.get("section"))
        line = f"Low-contrast text: \"{text[:120]}\""
        if ratio is not None:
            line += f" (contrast ratio {ratio})"
        if section:
            line += f" in {section}"
        details.append(line + ".")
    return details


def _tap_target_evidence(samples: Any) -> List[str]:
    if not isinstance(samples, list):
        return []
    details = []
    for sample in samples[:5]:
        if not isinstance(sample, dict):
            continue
        text = _text(sample.get("text")) or _text(sample.get("href")) or _text(sample.get("selector")) or "clickable element"
        width = sample.get("width")
        height = sample.get("height")
        section = _section_phrase(sample.get("section"))
        line = f"Small target: \"{text[:120]}\""
        if width is not None and height is not None:
            line += f" ({width}x{height}px)"
        if section:
            line += f" in {section}"
        details.append(line + ".")
    return details


def _image_failure_evidence(samples: Any) -> List[str]:
    if not isinstance(samples, list):
        return []
    details = []
    for sample in samples[:8]:
        if not isinstance(sample, dict):
            continue
        src = _text(sample.get("src")) or _text(sample.get("selector")) or "image element"
        alt = _text(sample.get("alt"))
        section = _section_phrase(sample.get("section"))
        line = f"Broken image: {src}"
        if alt:
            line += f" (alt text: \"{alt[:100]}\")"
        if section:
            line += f" in {section}"
        details.append(line + ".")
    return details


def _section_phrase(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    heading = _text(value.get("heading"))
    container = _text(value.get("container"))
    if heading:
        return f"the section headed \"{heading[:120]}\""
    if container:
        return f"the {container[:120]} section"
    return ""


def _lcp_culprit_phrase(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    text = _text(value.get("text"))
    alt = _text(value.get("alt"))
    url = _text(value.get("url"))
    section = _section_phrase(value.get("section"))
    resource = value.get("resource") if isinstance(value.get("resource"), dict) else {}
    duration = _to_float(resource.get("duration_ms")) if resource else None
    location = f" in {section}" if section else ""
    if text:
        phrase = f"the text \"{text[:120]}\"{location}"
    elif url:
        phrase = f"the image/media file {url[:160]}{location}"
    elif alt:
        phrase = f"the image with alt text \"{alt[:120]}\"{location}"
    else:
        phrase = f"the main visible content{location}"
    if duration and duration > 1_000:
        return f"{phrase} after a {round(duration / 100) / 10:.1f}s resource load"
    return phrase


def _capitalize_sentence(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _trim_leading_context(value: str) -> str:
    text = _text(value)
    for prefix in ("on mobile, ", "on desktop, "):
        if text.lower().startswith(prefix):
            return text[len(prefix):]
    return text


def _speed_content_sentence(value: str) -> str:
    text = _text(value)
    lower = text.lower()
    marker = " takes "
    if marker in lower:
        index = lower.index(marker)
        subject = text[:index].strip()
        timing = text[index + len(marker):].strip()
        return (
            f"The main content, especially {subject}, takes {timing}, "
            "which can make the first impression feel delayed for people visiting from Google."
        )
    return (
        f"The main content, {text}, can make the first impression feel delayed "
        "for people visiting from Google."
    )


def _load_seconds_from_text(value: str) -> str:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b", _text(value), re.IGNORECASE)
    return match.group(1) if match else ""


def _copy_score(
    body: str,
    title: str,
    meta: str,
    h1s: List[str],
    ctas: List[str],
    has_social_proof: bool,
    has_clear_value_proposition: bool,
) -> int:
    score = 4
    if len(body.split()) >= 120:
        score += 1
    if title and meta and h1s:
        score += 1
    if ctas:
        score += 1
    if has_social_proof:
        score += 1
    if has_clear_value_proposition:
        score += 2
    return _bounded_int(score)


def _seo_score(title: str, meta: str, h1s: List[str], h2s: List[str]) -> int:
    score = 2
    if 10 <= len(title) <= 70:
        score += 2
    elif title:
        score += 1
    if 50 <= len(meta) <= 170:
        score += 2
    elif meta:
        score += 1
    if h1s:
        score += 2
    if h2s:
        score += 1
    if len(h1s) == 1:
        score += 1
    return _bounded_int(score)


def _ux_score(browser_score: int, has_clear_cta: bool) -> int:
    score = browser_score
    if has_clear_cta:
        score += 1
    else:
        score -= 2
    return _bounded_int(score)


def _strengths(
    *,
    has_clear_cta: bool,
    has_social_proof: bool,
    has_clear_value_proposition: bool,
    browser_score: int,
    seo_score: int,
) -> List[str]:
    strengths = []
    if has_clear_cta:
        strengths.append("The site includes a visible conversion action.")
    if has_social_proof:
        strengths.append("The page includes trust or proof signals.")
    if has_clear_value_proposition:
        strengths.append("The page gives visitors some customer-facing value context.")
    if browser_score >= 8:
        strengths.append("The browser-quality checks did not find major usability friction.")
    if seo_score >= 8:
        strengths.append("The page covers the core SEO basics.")
    return strengths or ["The website is live and gives visitors an owned place to learn about the business."]


def _subject_for_finding(finding: Dict[str, str]) -> str:
    category = finding.get("category", "")
    if category in {"page speed", "page load"}:
        return "Your website is slow on mobile"
    if category == "conversion path":
        return "quick website note"
    if category == "layout stability":
        return "Issue affecting visitors on your website"
    if category == "mobile layout":
        return "Website issue affecting mobile visitors"
    if category == "mobile usability":
        return "Mobile issue affecting bookings on your website"
    if category == "readability":
        return "Noticed something on your website"
    if category in {"message clarity", "copy clarity"}:
        return "website message"
    if category == "trust":
        return "website trust"
    if category == "search basics":
        return "website search basics"
    return "quick website note"


def _email_template_variant(subject: str, slug: str) -> str:
    if subject != "Noticed something on your website":
        return "A"
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return "B" if int(digest[:2], 16) % 2 else "A"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return default
