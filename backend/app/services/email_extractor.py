"""
Email and team-member extractor using BeautifulSoup4 + regex.

Scrapes common contact/about/team pages on a domain and extracts:
  - All email addresses (deduplicated, cleaned)
  - Team members formatted as ``Name - Role`` when both values are visible
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.services.website_filters import is_non_official_website

logger = logging.getLogger(__name__)

_EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Titles that indicate a real team member, not generic page copy.
_TEAM_ROLE_RE = re.compile(
    r"\b(founder|co[- ]?founder|ceo|chief executive|owner|director|"
    r"managing director|principal|partner|president|head of|manager|"
    r"operations manager|general manager|office manager|supervisor|"
    r"lead technician|technician|specialist|consultant)\b",
    re.IGNORECASE,
)

_NAME_REGEX = re.compile(r"\b([A-Z][a-z]{1,20}(?:\s[A-Z][a-z]{1,20}){1,2})\b")
_EXTRA_PATHS = ["/contact", "/about", "/contact-us", "/about-us", "/team"]
_REQUEST_TIMEOUT = 10
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def extract_emails_and_team_members(website_url: str) -> Dict[str, Any]:
    """
    Fetch multiple pages from ``website_url`` and return:
    ``{ "emails": [...], "team_members": ["Jane Smith - Owner", ...] }``.
    """
    base_domain = _get_base_url(website_url)
    urls_to_check = [website_url] + [urljoin(base_domain, path) for path in _EXTRA_PATHS]

    all_emails: List[str] = []
    all_team_members: List[str] = []

    for url in urls_to_check:
        try:
            html = _fetch_html(url)
            if html is None:
                continue

            emails, team_members = _parse_html(html)
            all_emails.extend(emails)
            all_team_members.extend(team_members)
        except Exception as exc:
            logger.debug("Error extracting from %s: %s", url, exc)

    return {
        "emails": _deduplicate_emails(all_emails),
        "team_members": _deduplicate_team_members(all_team_members),
    }


def extract_emails_and_team_members_from_page_data(page_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract contact fields from the Playwright website scrape payload."""
    text_parts = [
        page_data.get("title"),
        page_data.get("meta_description"),
        page_data.get("body_text"),
        page_data.get("footer_text"),
        page_data.get("page_text"),
    ]
    text = "\n".join(str(part or "") for part in text_parts if part)
    links = [str(link or "") for link in page_data.get("links") or []]
    emails = _EMAIL_REGEX.findall(text)
    for link in links:
        if link.startswith("mailto:"):
            email = link.replace("mailto:", "").split("?")[0].strip()
            if _EMAIL_REGEX.match(email):
                emails.append(email)
        else:
            emails.extend(_EMAIL_REGEX.findall(link))

    return {
        "emails": _deduplicate_emails(emails),
        "team_members": _extract_team_members_from_text(text),
    }


def _fetch_html(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, headers=_HEADERS)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException as exc:
        logger.debug("HTTP fetch failed for %s: %s", url, exc)
    return None


def _parse_html(html: str) -> Tuple[List[str], List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_text = soup.get_text(separator="\n", strip=True)
    emails = _EMAIL_REGEX.findall(raw_text)

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"]
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip()
            if _EMAIL_REGEX.match(email):
                emails.append(email)

    return emails, _extract_team_members(soup, raw_text)


def _extract_team_members(soup: BeautifulSoup, text: str) -> List[str]:
    team_members: List[str] = []

    for person in soup.find_all(attrs={"itemtype": re.compile(r"Person", re.I)}):
        name_el = person.find(attrs={"itemprop": "name"})
        role_el = person.find(attrs={"itemprop": re.compile(r"(jobTitle|role)", re.I)})
        formatted = _format_team_member(
            name_el.get_text(" ", strip=True) if name_el else "",
            role_el.get_text(" ", strip=True) if role_el else "",
        )
        if formatted:
            team_members.append(formatted)

    for name_el in soup.find_all(attrs={"itemprop": "name"}):
        parent = name_el.parent
        role_el = parent.find(attrs={"itemprop": re.compile(r"(jobTitle|role)", re.I)}) if parent else None
        formatted = _format_team_member(
            name_el.get_text(" ", strip=True),
            role_el.get_text(" ", strip=True) if role_el else "",
        )
        if formatted:
            team_members.append(formatted)

    team_members.extend(_extract_team_members_from_text(text))
    return _deduplicate_team_members(team_members)


def _extract_team_members_from_text(text: str) -> List[str]:
    team_members: List[str] = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines):
        role = _extract_role(line)
        if not role:
            continue
        context = lines[max(0, index - 2): index + 1]
        for context_line in context:
            for name in _NAME_REGEX.findall(context_line):
                formatted = _format_team_member(name, role)
                if formatted:
                    team_members.append(formatted)

    return _deduplicate_team_members(team_members)


def _extract_role(text: str) -> Optional[str]:
    match = _TEAM_ROLE_RE.search(text)
    if not match:
        return None
    return _normalise_role(match.group(0))


def _normalise_role(role: str) -> str:
    clean = re.sub(r"\s+", " ", role.strip())
    replacements = {
        "ceo": "CEO",
        "co-founder": "Co-Founder",
        "co founder": "Co-Founder",
    }
    return replacements.get(clean.lower(), clean.title())


def _format_team_member(name: str, role: str) -> Optional[str]:
    name = re.sub(r"\s+", " ", name.strip())
    role = _normalise_role(role) if role else ""
    if not _looks_like_name(name) or not role:
        return None
    if _TEAM_ROLE_RE.fullmatch(name) or name.lower() == role.lower():
        return None
    return f"{name} - {role}"


def _looks_like_name(text: str) -> bool:
    if not text or len(text) > 60 or len(text) < 4:
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    if text == text.upper():
        return False
    return True


def _deduplicate_emails(emails: List[str]) -> List[str]:
    return filter_valid_lead_emails(emails)


def filter_valid_lead_emails(emails: List[str]) -> List[str]:
    """Deduplicate and remove placeholder/platform emails before persisting leads."""
    seen = set()
    result = []
    ignore_domains = {"example.com", "sentry.io", "wixpress.com", "squarespace.com"}
    for email in emails:
        clean = email.lower().strip()
        domain = clean.split("@")[-1] if "@" in clean else ""
        if clean in seen or domain in ignore_domains or is_non_official_website(domain):
            continue
        if not _EMAIL_REGEX.match(clean):
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _deduplicate_team_members(team_members: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in team_members:
        clean = re.sub(r"\s+", " ", item.strip())
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result[:20]


def _get_base_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
