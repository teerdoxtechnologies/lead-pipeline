"""
Big-brand exclusion list.

Businesses on this list are never prospects (national chains, big-box,
large franchises), so the scraper skips them at card time and the persist
step drops any that slip through. Matching is EXACT on the normalized
name with one documented quirk: a leading "the " is stripped first,
otherwise the flagship example ("The Home Depot" vs entry "home depot")
would miss.

Normalization mirrors _compact_text (serpapi_service) and
_slug_campaign_name (routes.scrape) on purpose: lowercase, non-letter
runs become one space. Kept local (not imported) to avoid coupling a
tiny pure helper to heavy service modules.
"""
from __future__ import annotations

import re

EXCLUDED_BRANDS = frozenset(
    {
        # Big-box / retail nationals.
        "home depot",
        "lowes",
        "walmart",
        "target",
        "costco",
        "best buy",
        "ikea",
        "sams club",
        "bj wholesale",
        "menards",
        "tractor supply",
        "harbor freight",
        "walgreens",
        "cvs",
        "kroger",
        "publix",
        "whole foods",
        "trader joes",
        "aldi",
        "petco",
        "petsmart",
        "dick sporting goods",
        "academy sports",
        "bass pro shops",
        "cabelas",
        "auto zone",
        "advance auto parts",
        "oreilly auto parts",
        "napa auto parts",
        "pep boys",
        "firestone",
        "discount tire",
        "sherwin williams",
        "floor and decor",
        "dollar general",
        "family dollar",
        # Home-service franchises.
        "roto rooter",
        "mr rooter",
        "mr electric",
        "mr handyman",
        "mr appliance",
        "aire serv",
        "one hour heating",
        "benjamin franklin plumbing",
        "ars rescue rooter",
        "terminix",
        "orkin",
        "truly nolen",
        "mosquito joe",
        "mosquito squad",
        "merry maids",
        "molly maid",
        "servpro",
        "servicemaster clean",
        "puroclean",
        "rainbow international",
        "stanley steemer",
        "chem dry",
        "zerorez",
        "coit",
        "belfor",
        "glass doctor",
        "safelite",
        "maaco",
        "meineke",
        "midas",
        "jiffy lube",
        "valvoline",
        "great clips",
        "sport clips",
        "supercuts",
        "fantastic sams",
        "massage envy",
        "anytime fitness",
        "planet fitness",
        "golds gym",
        "ups store",
        "fedex office",
    }
)


def normalize_brand_name(name: str) -> str:
    """Compacted lowercase name for exact comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z]+", " ", str(name or "").lower())).strip()


def strip_leading_the(normalized: str) -> str:
    """Drop a leading "the " so "the home depot" matches "home depot"."""
    if normalized.startswith("the "):
        return normalized[4:]
    return normalized


def is_excluded_brand(name: str) -> bool:
    """Exact membership test against EXCLUDED_BRANDS."""
    normalized = strip_leading_the(normalize_brand_name(name))
    return bool(normalized) and normalized in EXCLUDED_BRANDS
