"""Shared filters for deciding whether a URL is an official business website."""
from __future__ import annotations

from urllib.parse import urlparse


NON_OFFICIAL_WEBSITE_DOMAINS = {
    # Booking and job platforms
    "bookingkoala.com",
    "getjobber.com",
    "homeaglow.com",
    "booksy.com",
    "thumbtack.com",
    "angi.com",
    "angieslist.com",
    "fresha.com",
    "vagaro.com",
    "styleseat.com",
    "setmore.com",
    "calendly.com",
    "acuityscheduling.com",
    "mindbodyonline.com",
    "simplybook.me",
    "10to8.com",
    "appointy.com",
    "booking.com",
    "mangomint.com",
    "booker.com",
    "treatwell.com",
    "phorest.com",
    "zenoti.com",
    "glossgenius.com",
    "schedulicity.com",
    # Social, link-in-bio, and directory pages
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "linkedin.com",
    "pinterest.com",
    "linktr.ee",
    "wa.me",
    "whatsapp.com",
    "yelp.com",
    "tripadvisor.com",
    "yellowpages.com",
    "foursquare.com",
    "trustpilot.com",
    "bbb.org",
    "google.com",
    "nextdoor.com",
}


def domain_from_url(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.lower().lstrip("www.").strip(".")


def is_non_official_website(value: str) -> bool:
    domain = domain_from_url(value or "")
    if not domain:
        return False
    if domain.endswith(".edu"):
        return True
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in NON_OFFICIAL_WEBSITE_DOMAINS)
