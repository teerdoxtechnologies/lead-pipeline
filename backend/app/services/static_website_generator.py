"""
Generate lean static websites for no-website leads.

The generator is intentionally deterministic: it uses campaign/lead data already
stored in Firestore and does not call AI providers or external services.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from app.config import get_settings

logger = logging.getLogger(__name__)
GIT_COMMAND_TIMEOUT_SECONDS = 120

_LOGO_STOP_WORDS = {
    "and",
    "clean",
    "cleaner",
    "cleaners",
    "cleaning",
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "inc",
    "llc",
    "ltd",
    "service",
    "services",
    "the",
}


@dataclass
class GeneratedWebsite:
    lead_id: str
    business_name: str
    slug: str
    path: str
    url: str | None
    files: list[str]
    skipped: bool = False
    reason: str | None = None


def eligible_for_static_website(lead: dict[str, Any]) -> bool:
    """A lead is eligible when Notion opted it in with needs_website=true."""
    return bool(lead.get("needs_website"))


def websites_root() -> Path:
    settings = get_settings()
    configured = Path(settings.generated_websites_root)
    if configured.is_absolute():
        return configured
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / configured).resolve()


def static_sites_repo_root() -> Path:
    settings = get_settings()
    configured = Path(settings.static_sites_repo_root)
    if configured.is_absolute():
        return configured
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / configured).resolve()


def templates_root() -> Path:
    settings = get_settings()
    configured = Path(settings.generated_website_templates_root)
    if configured.is_absolute():
        return configured
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / configured).resolve()


def public_url_for_slug(slug: str) -> str | None:
    base_url = (get_settings().base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/preview/{slug}/"


def public_audit_url_for_slug(slug: str) -> str | None:
    base_url = (get_settings().base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/audit/{slug}/"


def remove_generated_website_path(site_path: str | Path) -> bool:
    """Remove a previously generated website directory if it lives under the configured root."""
    root = websites_root().resolve()
    path = Path(site_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        logger.warning("Refusing to remove generated website path outside root: %s", path)
        return False
    if path == root:
        logger.warning("Refusing to remove generated websites root itself: %s", path)
        return False
    if not path.exists():
        return True
    try:
        shutil.rmtree(path)
        return True
    except Exception as exc:
        logger.warning("Failed to remove generated website path %s: %s", path, exc)
        return False


def build_static_website_for_lead(
    lead: dict[str, Any],
    campaign: dict[str, Any],
    existing_slugs: set[str] | None = None,
    overwrite: bool = False,
) -> GeneratedWebsite:
    root = websites_root()
    root.mkdir(parents=True, exist_ok=True)

    lead_id = lead["id"]
    business_name = lead.get("business_name") or "Local Business"
    slug = _unique_slug(
        _slugify(business_name),
        existing_slugs or _existing_directory_names(root),
        fallback_suffix=lead_id[:6],
    )
    site_dir = root / slug
    if site_dir.exists() and not overwrite:
        return GeneratedWebsite(
            lead_id=lead_id,
            business_name=business_name,
            slug=slug,
            path=str(site_dir),
            url=public_url_for_slug(slug),
            files=[],
            skipped=True,
            reason="site_directory_exists",
        )

    data = _site_context(lead, campaign, slug)
    files = _write_site(site_dir, data)
    if existing_slugs is not None:
        existing_slugs.add(slug)

    return GeneratedWebsite(
        lead_id=lead_id,
        business_name=business_name,
        slug=slug,
        path=str(site_dir),
        url=public_url_for_slug(slug),
        files=files,
    )


def git_commit_and_push_websites(
    generated: list[GeneratedWebsite],
    commit_message: str | None = None,
    push: bool = False,
) -> dict[str, Any]:
    root = static_sites_repo_root()
    summary: dict[str, Any] = {
        "committed": False,
        "pushed": False,
        "commit_message": commit_message,
        "error": None,
    }
    if not (root / ".git").exists():
        summary["error"] = f"{root} is not a git repository."
        return summary

    changed = [item for item in generated if not item.skipped]
    if not changed:
        summary["error"] = "No generated website changes to commit."
        return summary

    message = commit_message or f"Publish {len(changed)} generated business website{'s' if len(changed) != 1 else ''}"
    body = "Published websites:\n" + "\n".join(
        f"- {item.business_name} ({item.slug})"
        for item in changed
    )
    logger.info(
        "[Static Publish] Preparing generated website commit: root=%s changed=%d push=%s",
        root,
        len(changed),
        push,
    )
    try:
        for index, item in enumerate(changed, start=1):
            relative_path = Path(item.path).resolve().relative_to(root.resolve())
            logger.info(
                "[Static Publish] Staging generated website %d/%d: %s (%s)",
                index,
                len(changed),
                item.business_name,
                relative_path,
            )
            _run_git(root, ["add", str(relative_path)])
        status = _run_git(root, ["status", "--porcelain"]).strip()
        if not status:
            summary["error"] = "No git changes detected in websites repo."
            logger.info("[Static Publish] No generated website git changes detected.")
            return summary
        logger.info("[Static Publish] Creating generated website commit: %s", message)
        _run_git(root, ["commit", "-m", message, "-m", body])
        summary["committed"] = True
        summary["commit_message"] = message
        summary["commit_body"] = body
        if push:
            logger.info("[Static Publish] Pushing generated website commit from %s.", root)
            _run_git(root, ["push"])
            summary["pushed"] = True
            logger.info("[Static Publish] Generated website push completed.")
    except subprocess.CalledProcessError as exc:
        summary["error"] = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.error("[Static Publish] Generated website git step failed: %s", summary["error"])
    except subprocess.TimeoutExpired as exc:
        summary["error"] = f"Git command timed out after {exc.timeout}s."
        logger.error("[Static Publish] Generated website git step timed out: %s", summary["error"])
    return summary


def git_commit_and_push_static_paths(
    paths: list[str | Path],
    *,
    commit_message: str,
    commit_body: str,
    push: bool = True,
) -> dict[str, Any]:
    """Commit and push specific paths inside the static sites repository."""
    root = static_sites_repo_root()
    summary: dict[str, Any] = {
        "committed": False,
        "pushed": False,
        "commit_message": commit_message,
        "commit_body": commit_body,
        "error": None,
    }
    if not (root / ".git").exists():
        summary["error"] = f"{root} is not a git repository."
        return summary

    relative_paths: list[str] = []
    for path_value in paths:
        path = Path(path_value).resolve()
        try:
            relative_paths.append(str(path.relative_to(root.resolve())))
        except ValueError:
            summary["error"] = f"{path} is outside the static sites repository."
            return summary
    if not relative_paths:
        summary["error"] = "No static paths supplied."
        return summary

    unique_paths = sorted(set(relative_paths))
    logger.info(
        "[Static Publish] Preparing static path commit: root=%s paths=%d push=%s",
        root,
        len(unique_paths),
        push,
    )
    try:
        for index, relative_path in enumerate(unique_paths, start=1):
            logger.info(
                "[Static Publish] Staging static path %d/%d: %s",
                index,
                len(unique_paths),
                relative_path,
            )
            _run_git(root, ["add", "-A", relative_path])
        status = _run_git(root, ["status", "--porcelain"]).strip()
        if status:
            logger.info("[Static Publish] Creating static artifact commit: %s", commit_message)
            _run_git(root, ["commit", "-m", commit_message, "-m", commit_body])
            summary["committed"] = True
        else:
            logger.info("[Static Publish] No new static artifact commit needed; checking push requirement.")
        if push:
            logger.info("[Static Publish] Pushing static artifacts from %s.", root)
            _run_git(root, ["push"])
            summary["pushed"] = True
            logger.info("[Static Publish] Static artifact push completed.")
    except subprocess.CalledProcessError as exc:
        summary["error"] = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.error("[Static Publish] Static artifact git step failed: %s", summary["error"])
    except subprocess.TimeoutExpired as exc:
        summary["error"] = f"Git command timed out after {exc.timeout}s."
        logger.error("[Static Publish] Static artifact git step timed out: %s", summary["error"])
    return summary


def _run_git(root: Path, args: list[str]) -> str:
    command = ["git", *args]
    safe_command = _safe_git_command(args)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    timeout = GIT_COMMAND_TIMEOUT_SECONDS if args and args[0] == "push" else 30
    started = time.perf_counter()
    logger.info(
        "[Static Publish] Running git command in %s: %s",
        root,
        safe_command,
    )
    completed = subprocess.run(
        command,
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "[Static Publish] Git command completed in %sms: %s",
        duration_ms,
        safe_command,
    )
    return completed.stdout


def _safe_git_command(args: list[str]) -> str:
    if not args:
        return "git"
    if args[0] == "commit":
        return "git commit <message omitted>"
    return "git " + " ".join(args)


def _write_site(site_dir: Path, data: dict[str, str]) -> list[str]:
    (site_dir / "css").mkdir(parents=True, exist_ok=True)
    (site_dir / "js").mkdir(parents=True, exist_ok=True)
    for stale_file in ("services.html", "css/services.css"):
        stale_path = site_dir / stale_file
        if stale_path.exists():
            stale_path.unlink()

    files = {
        "index.html": _landing_html(data),
        "contact.html": _contact_html(data),
        "css/landing.css": _landing_css(data),
        "css/contact.css": _contact_css(data),
        "js/site.js": _site_js(data),
    }
    for relative, content in files.items():
        path = site_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return sorted(files)


def _site_context(lead: dict[str, Any], campaign: dict[str, Any], slug: str) -> dict[str, str]:
    business_name = lead.get("business_name") or "Local Business"
    niche = campaign.get("niche") or "local service"
    location = campaign.get("location") or "your area"
    primary_email = (lead.get("emails") or [""])[0]
    phone = lead.get("phone") or ""
    address = lead.get("address") or ""
    display_address = address or f"Service area: {location}"
    title_niche = _title_case_service(niche)
    clean_phone = re.sub(r"[^0-9+]", "", phone)
    map_query = quote_plus(address) if address else ""
    profile = _service_profile(niche, location, business_name)
    google_reviews = _customer_review_contexts(lead)
    gallery_images = _listing_gallery_images(lead)
    google_maps_url = str(lead.get("google_maps_url") or "").strip()

    return {
        "business_name": html.escape(business_name),
        "business_name_raw": business_name,
        "logo_text": html.escape(_logo_text(business_name)),
        "profile": profile["profile"],
        "niche": html.escape(niche),
        "title_niche": html.escape(title_niche),
        "location": html.escape(location),
        "primary_email": html.escape(primary_email),
        "phone": html.escape(phone),
        "address": html.escape(display_address),
        "slug": html.escape(slug),
        "hero_image": "https://images.unsplash.com/photo-1527515637462-cff94eecc1ac?auto=format&fit=crop&w=1900&q=84",
        "detail_image": "https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&w=1300&q=84",
        "team_image": "https://images.unsplash.com/photo-1584622781564-1d987f7333c1?auto=format&fit=crop&w=1300&q=84",
        "gallery_image": "https://images.unsplash.com/photo-1563453392212-326f5e854473?auto=format&fit=crop&w=1500&q=84",
        "gallery_images": gallery_images,
        "rating_label": html.escape(_rating_label(lead)),
        "customer_reviews": google_reviews,
        "google_maps_url": html.escape(google_maps_url),
        "google_maps_photos_url": html.escape(_google_maps_photos_url(google_maps_url)),
        "map_embed_src": html.escape(f"https://www.google.com/maps?q={map_query}&output=embed") if map_query else "",
        "hero_headline": html.escape(profile["hero_headline"]),
        "hero_lede": html.escape(profile["hero_lede"]),
        "intro_headline": html.escape(profile["intro_headline"]),
        "intro_copy": html.escape(profile["intro_copy"]),
        "process_headline": html.escape(profile["process_headline"]),
        "trust_headline": html.escape(profile["trust_headline"]),
        "trust_copy": html.escape(profile["trust_copy"]),
        "services_headline": html.escape(profile["services_headline"]),
        "services_lede": html.escape(profile["services_lede"]),
        "checklist_headline": html.escape(profile["checklist_headline"]),
        "contact_headline": html.escape(profile["contact_headline"]),
        "contact_lede": html.escape(profile["contact_lede"]),
        "meta_description": html.escape(
            f"{business_name} helps customers in {location} with reliable {niche} services."
        ),
        "mailto": html.escape(f"mailto:{primary_email}") if primary_email else "#contact",
        "phone_href": html.escape(f"tel:{clean_phone}") if clean_phone else "#contact",
    }


def _landing_html(d: dict[str, str]) -> str:
    gallery_section = _gallery_section(d)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{d['business_name']} | {d['title_niche']} in {d['location']}</title>
  <meta name="description" content="{d['meta_description']}">
  <link rel="stylesheet" href="css/landing.css">
  <script src="js/site.js" defer></script>
</head>
<body data-page="landing">
  <a class="skip-link" href="#main">Skip to main content</a>
  {_nav(d)}
  <main id="main">
    <section class="hero reveal">
      <div class="hero-media" aria-hidden="true">
        <img src="{d['hero_image']}" alt="" decoding="async">
      </div>
      <div class="hero-copy">
        <p class="rating-line" aria-label="{d['rating_label']}">★★★★★ <span>{d['rating_label']}</span></p>
        <h1>{d['hero_headline']}</h1>
        <p class="lede">Hand off the scrubbing, dusting, mopping, and detail work to professionals who know what a truly clean space should feel like.</p>
        <div class="actions">
          <a class="button primary" href="#quote">Get a quote</a>
          <a class="button secondary" href="#services">See services</a>
        </div>
      </div>
    </section>
    <section class="about-section reveal" id="about">
      <div class="section-copy">
        <p class="pill">About</p>
        <h2>{d['intro_headline']}</h2>
        <p>{d['intro_copy']}</p>
      </div>
      <div class="image-frame soft">
        <img src="{d['detail_image']}" alt="A bright, freshly cleaned living room" loading="lazy" decoding="async">
      </div>
    </section>
    <section class="service-section reveal" id="services">
      <div class="section-copy">
        <p class="pill">Services</p>
        <h2>Cleaning services we offer for spaces that need dependable care.</h2>
        <p>From routine upkeep to move-day and commercial cleaning, each service is shaped around the same promise: a cleaner space, clearer expectations, and less for the customer to manage.</p>
      </div>
      <div class="service-grid">
        {_landing_service_cards(d)}
      </div>
    </section>
    <section class="process-section reveal">
      <div class="section-copy">
        <p class="pill light">How it works</p>
        <h2>{d['process_headline']}</h2>
      </div>
      <div class="process-track" aria-label="Cleaning appointment process">
        {_process_steps()}
      </div>
    </section>
    <section class="quote-section reveal" id="quote">
      <div class="quote-copy">
        <p class="pill">Get a quote</p>
        <h2>Tell us what needs cleaning, and we will email you a quote.</h2>
        <p>Share the basics, then use the Notes field for extra information, access details, special requests, fragile surfaces, pets, or anything that should shape the quote.</p>
      </div>
      <div class="quote-form-wrap">
        {_quote_form(d)}
      </div>
    </section>
    <section class="testimonial-section reveal">
      <div class="section-copy">
        <p class="pill">Testimonials</p>
        <h2>See what customers are saying about the work.</h2>
      </div>
      <div class="testimonial-grid review-count-{len((d.get('customer_reviews') or _placeholder_reviews())[:9])}">
        {_testimonial_cards(d)}
      </div>
      <div class="testimonial-actions">
        <a class="button secondary" href="{d['google_maps_url'] or '#'}" target="_blank" rel="noopener">View more reviews</a>
      </div>
    </section>
    <section class="faq-section reveal">
      <p class="pill">FAQ</p>
      <h2>Questions customers ask before booking cleaning help.</h2>
      <details>
        <summary>How soon should I expect a response?</summary>
        <p>Send the request with as much detail as you have. If the cleaner needs anything else, they can follow up before sending a quote so you are not guessing.</p>
      </details>
      <details>
        <summary>Do cleaners bring supplies and equipment?</summary>
        <p>Usually, yes. If you prefer a specific product or there is a surface that needs special care, add that in the Notes field so it is clear before the appointment.</p>
      </details>
      <details>
        <summary>Can I request the same professionals again?</summary>
        <p>You can ask. It depends on schedule availability, but it is a reasonable request and worth mentioning when you book.</p>
      </details>
      <details>
        <summary>What if a room needs extra attention?</summary>
        <p>Tell us upfront. Stains, buildup, pet areas, high-touch surfaces, or fragile materials are exactly the kinds of details that help shape a better quote.</p>
      </details>
      <details>
        <summary>How should I prepare before the appointment?</summary>
        <p>A quick pickup helps. Put away personal items, secure valuables, and share access instructions so more of the appointment can go into the actual cleaning.</p>
      </details>
      <details>
        <summary>Can recurring cleaning be adjusted later?</summary>
        <p>Yes. If your space or schedule changes, say so. A recurring clean should fit the way the space is actually being used.</p>
      </details>
    </section>
    {gallery_section}
    <section class="cta-strip reveal">
      <div>
        <h2>Ready to walk into a space that already feels taken care of?</h2>
        <a class="button primary inverse" href="#quote">Request my quote</a>
      </div>
    </section>
  </main>
  {_footer(d)}
</body>
</html>
"""


def _contact_html(d: dict[str, str]) -> str:
    map_block = (
        f"""<div class="map-frame reveal">
        <iframe title="Map for {d['business_name']}" src="{d['map_embed_src']}" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>
      </div>"""
        if d.get("map_embed_src")
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Contact {d['business_name']}</title>
  <meta name="description" content="Contact {d['business_name']} for {d['niche']} service in {d['location']}.">
  <link rel="stylesheet" href="css/contact.css">
  <script src="js/site.js" defer></script>
</head>
<body data-page="contact">
  <a class="skip-link" href="#main">Skip to main content</a>
  {_nav(d)}
  <main id="main">
    <section class="contact-hero reveal">
      <p class="pill">Contact</p>
      <h1>{d['contact_headline']}</h1>
      <p class="lede">{d['contact_lede']}</p>
    </section>
    <section class="contact-layout">
      {_quote_form(d)}
      <aside class="contact-card reveal" aria-label="Business contact details">
        <a href="{d['mailto']}"><span>Email</span><strong>{d['primary_email'] or 'Email available on request'}</strong></a>
        <a href="{d['phone_href']}"><span>Phone</span><strong>{d['phone'] or 'Call details available soon'}</strong></a>
        <p><span>Address</span><strong>{d['address']}</strong></p>
      </aside>
    </section>
    {map_block}
  </main>
  {_footer(d)}
</body>
</html>
"""


def _services_html(d: dict[str, str]) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{d['business_name']} Services</title>
  <meta name="description" content="{d['business_name']} provides {d['niche']} support in {d['location']}.">
  <link rel="stylesheet" href="css/services.css">
  <script src="js/site.js" defer></script>
</head>
<body data-page="services">
  <main id="main"></main>
</body>
</html>
"""


def _nav(d: dict[str, str]) -> str:
    return f"""<header class="site-header">
    <a class="brand" href="index.html">{d['logo_text']}</a>
    <nav aria-label="Primary navigation">
      <a href="index.html">Home</a>
      <a href="index.html#about">About</a>
      <a href="index.html#services">Services</a>
      <a href="contact.html">Contact</a>
    </nav>
  </header>"""


def _footer(d: dict[str, str]) -> str:
    return f"""<footer class="site-footer">
    <div>
      <a class="footer-brand" href="index.html">{d['logo_text']}</a>
      <p>Professional cleaning for homes, offices, and move-ready spaces.</p>
    </div>
    <div>
      <h2>Navigation</h2>
      <a href="index.html#about">About</a>
      <a href="index.html#services">Services</a>
      <a href="contact.html">Contact</a>
    </div>
    <div>
      <h2>Contact</h2>
      <a href="{d['mailto']}">{d['primary_email'] or 'Email available on request'}</a>
      <a href="{d['phone_href']}">{d['phone'] or 'Phone available soon'}</a>
      <p>{d['address']}</p>
    </div>
  </footer>"""


def _quote_form(d: dict[str, str]) -> str:
    return f"""<form class="quote-form" data-contact-form data-email="{d['primary_email']}">
        <div class="form-row">
          <label>Full name<input name="name" autocomplete="name" required></label>
          <label>Email<input name="email" type="email" autocomplete="email" required></label>
        </div>
        <div class="form-row">
          <label>Phone<input name="phone" type="tel" autocomplete="tel"></label>
          <label>Property type<select name="property_type"><option>House</option><option>Apartment</option><option>Office</option><option>Other</option></select></label>
        </div>
        <div class="form-row compact">
          <label>Bedrooms<input name="bedrooms" type="number" min="0" inputmode="numeric"></label>
          <label>Bathrooms<input name="bathrooms" type="number" min="0" inputmode="numeric"></label>
          <label>Square footage<input name="square_footage" type="number" min="0" inputmode="numeric"></label>
        </div>
        <div class="form-row">
          <label>Frequency<select name="frequency"><option>One-time</option><option>Weekly</option><option>Biweekly</option><option>Monthly</option></select></label>
          <label>Preferred date<input name="preferred_date" type="date"></label>
        </div>
        <div class="form-row">
          <label>Service type<select name="service_type">{_contact_service_options(d)}</select></label>
          <label>Area or address<input name="address" autocomplete="street-address"></label>
        </div>
        <label>Notes<textarea name="message" rows="5" required></textarea></label>
        <button class="button primary" type="submit">Request my quote</button>
        <p class="form-status" data-form-status aria-live="polite"></p>
      </form>"""


def _process_steps() -> str:
    icons = [
        (
            "Quote",
            '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M12 16h24M12 24h18M12 32h12"/><rect x="9" y="8" width="30" height="32" rx="6"/></svg>',
            "Request a quote",
            "Send the basics: what needs cleaning, where the space is, and when help would feel ideal.",
        ),
        (
            "Plan",
            '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M15 25l6 6 13-15"/><circle cx="24" cy="24" r="17"/></svg>',
            "Confirm the plan",
            "Scope, timing, access notes, supplies, and special requests are clarified before the visit.",
        ),
        (
            "Clean",
            '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M30 8l4 10 10 4-10 4-4 10-4-10-10-4 10-4 4-10zM13 29l2 5 5 2-5 2-2 5-2-5-5-2 5-2 2-5z"/></svg>',
            "The work gets done",
            "Professionals move through the space with a practical checklist and attention to the details that show.",
        ),
        (
            "Done",
            '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M10 25l12 10 16-22"/><path d="M9 38h30"/></svg>',
            "You enjoy the clean",
            "The space feels lighter, calmer, and easier to use without another cleaning task waiting for you.",
        ),
    ]
    return "\n        ".join(
        f"""<article class="process-card">
          <div class="process-icon">{icon}</div>
          <span>{index:02d}</span>
          <h3>{title}</h3>
          <p>{copy}</p>
        </article>"""
        for index, (_label, icon, title, copy) in enumerate(icons, start=1)
    )


def _testimonial_cards(d: dict[str, str]) -> str:
    reviews = (d.get("customer_reviews") or _placeholder_reviews())[:9]
    return "\n        ".join(
        f"""<article class="review-card {_testimonial_card_span_class(len(reviews), index)}">
          <blockquote>{review['text']}</blockquote>
          <p><img class="review-avatar" src="{review['avatar_url'] or d['gallery_image']}" alt="" loading="lazy" decoding="async"><strong>{review['author']}</strong></p>
        </article>"""
        for index, review in enumerate(reviews)
    )


def _testimonial_card_span_class(total: int, index: int) -> str:
    remainder = total % 3
    if total == 1:
        return "span-full"
    if total == 2:
        return "span-half"
    if remainder == 1 and index == total - 1:
        return "span-full"
    if remainder == 2 and index >= total - 2:
        return "span-half"
    return "span-third"


def _listing_gallery_images(lead: dict[str, Any]) -> list[dict[str, str]]:
    images = lead.get("google_listing_images") or []
    cleaned: list[dict[str, str]] = []
    for index, image in enumerate(images[:12], start=1):
        if not isinstance(image, dict):
            continue
        url = str(image.get("url") or "").strip()
        if not url:
            continue
        width = image.get("width")
        height = image.get("height")
        orientation = _gallery_orientation(width, height, index)
        cleaned.append({
            "url": html.escape(url),
            "alt": html.escape(f"Cleaning work example {index}"),
            "orientation": orientation,
        })
    return cleaned if len(cleaned) >= 6 else []


def _fallback_gallery_images() -> list[dict[str, str]]:
    urls = [
        "https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&w=1300&q=84",
        "https://images.unsplash.com/photo-1527515637462-cff94eecc1ac?auto=format&fit=crop&w=1400&q=84",
        "https://images.unsplash.com/photo-1563453392212-326f5e854473?auto=format&fit=crop&w=1100&q=84",
        "https://images.unsplash.com/photo-1603712725038-e9334ae8f39f?auto=format&fit=crop&w=1100&q=84",
        "https://images.unsplash.com/photo-1585421514284-efb74c2b69ba?auto=format&fit=crop&w=1100&q=84",
        "https://images.unsplash.com/photo-1513694203232-719a280e022f?auto=format&fit=crop&w=1100&q=84",
    ]
    return [
        {
            "url": html.escape(url),
            "alt": html.escape(f"Cleaning work example {index}"),
            "orientation": "landscape" if index % 3 else "portrait",
        }
        for index, url in enumerate(urls, start=1)
    ]


def _gallery_orientation(width: Any, height: Any, index: int) -> str:
    try:
        width_value = int(width or 0)
        height_value = int(height or 0)
    except (TypeError, ValueError):
        width_value = height_value = 0
    if width_value and height_value:
        return "portrait" if height_value > width_value * 1.08 else "landscape"
    return "portrait" if index % 4 in {0, 3} else "landscape"


def _gallery_frames(d: dict[str, Any]) -> str:
    images = d.get("gallery_images") or []
    return "\n        ".join(
        f'<img class="gallery-frame {image["orientation"]} frame-{index}" src="{image["url"]}" alt="{image["alt"]}" loading="lazy" decoding="async">'
        for index, image in enumerate(images[:12], start=1)
    )


def _gallery_section(d: dict[str, Any]) -> str:
    if len(d.get("gallery_images") or []) < 6:
        return ""
    return f"""<section class="gallery-section reveal" aria-label="Cleaning image gallery">
      <div class="gallery-heading">
        <p>Recent work</p>
        <h2>Spaces after the work is done.</h2>
        <span>Listing photos from previous jobs and project spaces.</span>
      </div>
      <div class="masonry">
        {_gallery_frames(d)}
      </div>
      <div class="gallery-actions">
        <a class="button secondary" href="{d['google_maps_photos_url']}" target="_blank" rel="noopener">See more photos</a>
      </div>
    </section>"""


def _rating_label(lead: dict[str, Any]) -> str:
    rating = lead.get("google_rating") or lead.get("average_rating")
    review_count = lead.get("google_review_count")
    if rating:
        try:
            rating_value = f"{float(rating):.1f}"
        except (TypeError, ValueError):
            rating_value = str(rating)
        if review_count:
            return f"{rating_value} Google rating from {review_count} reviews"
        return f"{rating_value} Google rating"
    return "Google rating on Maps"


def _customer_review_contexts(lead: dict[str, Any]) -> list[dict[str, str]]:
    reviews = lead.get("google_reviews") or lead.get("customer_reviews") or []
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            continue
        text = str(review.get("text") or "").strip()
        if not text:
            continue
        author = _clean_review_author(str(review.get("author") or "Google reviewer"))
        normalized_text = re.sub(r"\s+", " ", text.lower())
        key = f"{author.lower()}|{normalized_text[:180]}"
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "author": html.escape(author),
            "text": html.escape(text),
            "avatar_url": html.escape(str(review.get("avatar_url") or "").strip()),
        })
        if len(cleaned) >= 9:
            break
    return cleaned


def _clean_review_author(author: str) -> str:
    return re.sub(r"\s+", " ", (author or "").splitlines()[0].strip()) or "Google reviewer"


def _placeholder_reviews() -> list[dict[str, str]]:
    quote = "The house felt lighter the moment I walked in. Booking was easy, the team listened to what mattered, and the clean felt genuinely finished."
    names = [
        "Avery M.",
        "Jordan P.",
        "Camille R.",
        "Morgan T.",
        "Riley S.",
        "Taylor B.",
        "Casey L.",
        "Parker D.",
    ]
    return [
        {"author": name, "text": quote, "avatar_url": ""}
        for name in names
    ]


def _google_maps_photos_url(google_maps_url: str) -> str:
    google_maps_url = (google_maps_url or "").strip()
    if not google_maps_url:
        return "#"
    if "!10m1!1e1" in google_maps_url:
        return google_maps_url
    if "?" in google_maps_url:
        base, query = google_maps_url.split("?", 1)
        return f"{base}!10m1!1e1?{query}"
    return f"{google_maps_url}!10m1!1e1"


def _service_profile(niche: str, location: str, business_name: str) -> dict[str, str]:
    if _is_cleaning_niche(niche):
        return {
            "profile": "cleaning",
            "hero_headline": f"A cleaner space without losing your day.",
            "hero_lede": "Hand off the cleaning work and come back to a space that feels lighter, calmer, and ready to enjoy.",
            "intro_headline": f"About {business_name}",
            "intro_copy": "We help busy households, offices, and property owners keep their spaces clean without turning every week into another chore list. The work is thorough, careful, and built around the kind of fresh, orderly finish customers notice right away.",
            "process_headline": "From quote request to finished clean, the process has a rhythm.",
            "trust_headline": "Why customers choose this cleaning team over another name on the list.",
            "trust_copy": "The difference is in the things customers feel quickly: clear communication, careful professionals, realistic expectations, and a finished clean that does not feel rushed.",
            "services_headline": "Cleaning services we offer for spaces that need dependable care.",
            "services_lede": "From weekly upkeep to move-day and commercial cleaning, each service is shaped around cleaner rooms, clearer expectations, and less for the customer to manage.",
            "checklist_headline": "What we pay attention to during a clean.",
            "contact_headline": "Tell us what needs cleaning.",
            "contact_lede": "Share the home details, preferred timing, and anything that needs extra attention. We will use that context to respond with the next step.",
        }
    return {
        "profile": "local_service",
        "hero_headline": f"{business_name} helps you get the job handled without the runaround.",
        "hero_lede": f"Responsive {niche} support for customers in {location} who want clear communication, practical scheduling, and work that feels easy to start.",
        "intro_headline": "When the work matters, the first conversation should be simple.",
        "intro_copy": f"Use this page to share the job details, ask about availability, and get a direct next step from {business_name}.",
        "process_headline": "Clear details before anyone shows up.",
        "trust_headline": "A local business should be easy to understand before you call.",
        "trust_copy": f"{business_name} keeps the next step direct: tell them what is happening, where you are, and when you need help. The goal is a useful response, not a sales maze.",
        "services_headline": f"{_title_case_service(niche)} help without the runaround.",
        "services_lede": "A practical starting point for customers comparing local options and deciding who to contact first.",
        "checklist_headline": "Better details usually mean a better first response.",
        "contact_headline": f"Tell {business_name} what you need handled.",
        "contact_lede": "Share the service details, preferred timing, and the best way to reach you. A specific request makes it easier to give you a useful next step.",
    }


def _landing_service_cards(d: dict[str, str]) -> str:
    if d.get("profile") == "cleaning":
        return """<article><span aria-hidden="true">01</span><h3>Standard home cleaning</h3><p>For kitchens, bathrooms, floors, surfaces, and the everyday buildup that makes a home feel harder to enjoy.</p><a class="button primary" href="#quote">Quote this service</a></article>
        <article><span aria-hidden="true">02</span><h3>Deep cleaning</h3><p>For buildup, corners, baseboards, fixtures, appliance fronts, and the details that need more time than routine upkeep.</p><a class="button primary" href="#quote">Quote this service</a></article>
        <article><span aria-hidden="true">03</span><h3>Post-construction cleaning</h3><p>For newly built or remodeled spaces that need dust, debris, and surface residue cleared before they feel ready.</p><a class="button primary" href="#quote">Quote this service</a></article>
        <article><span aria-hidden="true">04</span><h3>Commercial cleaning</h3><p>For offices, studios, storefronts, and shared spaces that need consistent cleaning without slowing down the workday.</p><a class="button primary" href="#quote">Quote this service</a></article>"""
    return """<article><h3>One-time help</h3><p>For a specific job, visit, cleanup, repair, appointment, or project that needs attention.</p><a class="button primary" href="#quote">Quote this</a></article>
        <article><h3>Recurring support</h3><p>For customers who need reliable local help on a predictable rhythm.</p><a class="button primary" href="#quote">Quote this</a></article>
        <article><h3>Questions before booking</h3><p>For quick clarity on service area, timing, scope, and what to expect next.</p><a class="button primary" href="#quote">Quote this</a></article>"""


def _service_list_cards(d: dict[str, str]) -> str:
    if d.get("profile") == "cleaning":
        return """<article><span>01</span><h2>Standard home cleaning</h2><p>Routine cleaning for kitchens, bathrooms, floors, dusting, trash, and the everyday work that keeps the home livable.</p></article>
      <article><span>02</span><h2>Move-in / move-out cleaning</h2><p>Detailed cleaning for empty homes, apartments, rentals, listings, and spaces that need to feel ready for what comes next.</p></article>
      <article><span>03</span><h2>Deep cleaning</h2><p>Detailed attention for neglected corners, buildup, baseboards, fixtures, appliance fronts, and high-touch areas.</p></article>
      <article><span>04</span><h2>Post-construction cleaning</h2><p>Final cleanup after building or remodeling, with attention to dust, debris, sawdust, and surface residue.</p></article>"""
    return """<article><span>01</span><h2>Service requests</h2><p>Describe the job, the location, timing, and any details that affect the work.</p></article>
      <article><span>02</span><h2>Scheduling support</h2><p>Coordinate availability and next steps before anyone commits time to a visit.</p></article>
      <article><span>03</span><h2>Local guidance</h2><p>Ask practical questions about service needs around your area.</p></article>
      <article><span>04</span><h2>Follow-up help</h2><p>Keep the conversation clear if the job changes, expands, or needs another visit.</p></article>"""


def _service_checklist(d: dict[str, str]) -> str:
    if d.get("profile") == "cleaning":
        return """<div class="checklist-grid">
          <article><h3>Kitchen</h3><ul><li>Counters and sinks</li><li>Appliance fronts</li><li>Floors and trash</li><li>Optional oven or fridge interior</li></ul></article>
          <article><h3>Bathrooms</h3><ul><li>Sinks, mirrors, and fixtures</li><li>Tub, shower, and toilet</li><li>Floors and high-touch areas</li><li>Optional grout or detail work</li></ul></article>
          <article><h3>Living spaces</h3><ul><li>Dusting and surfaces</li><li>Floors, rugs, and entryways</li><li>Trash and general tidying</li><li>Optional pet hair attention</li></ul></article>
          <article><h3>Bedrooms</h3><ul><li>Dusting and floors</li><li>Mirrors and fixtures</li><li>Trash removal</li><li>Optional linen changes if provided</li></ul></article>
        </div>"""
    return """<ul>
          <li>Where the service is needed</li>
          <li>What problem or request you want handled</li>
          <li>Your preferred timing or deadline</li>
          <li>Any access notes, photos, or special instructions</li>
        </ul>"""


def _contact_service_options(d: dict[str, str]) -> str:
    if d.get("profile") == "cleaning":
        return """<option>Standard home cleaning</option>
          <option>Deep cleaning</option>
          <option>Move-in or move-out cleaning</option>
          <option>Commercial or office cleaning</option>
          <option>Not sure yet</option>"""
    return """<option>Service request</option>
          <option>Scheduling question</option>
          <option>Quote request</option>
          <option>Not sure yet</option>"""


def _logo_text(business_name: str) -> str:
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9]+", business_name)
        if word.lower() not in _LOGO_STOP_WORDS
    ]
    if not words:
        words = re.findall(r"[A-Za-z0-9]+", business_name) or ["Local"]
    if len(words) <= 2 and sum(len(word) for word in words) <= 16:
        return " ".join(words)
    initials = "".join(word[0].upper() for word in words if word)
    return initials[:5] or words[0][:5].upper()


def _landing_css(d: dict[str, str]) -> str:
    return _base_css() + """
.hero{position:relative;min-height:calc(100vh - 86px);display:grid;align-items:center;justify-items:center;text-align:center;overflow:hidden;background:var(--ink)}
.hero::after{content:"";position:absolute;inset:0;background:radial-gradient(circle at 50% 44%,rgba(11,42,39,.42),rgba(11,42,39,.86) 74%),linear-gradient(180deg,rgba(11,42,39,.2),rgba(11,42,39,.78))}
.hero-media{position:absolute;inset:0}.hero-media img{width:100%;height:100%;object-fit:cover;filter:saturate(.95) contrast(1.05)}
.hero-copy{position:relative;z-index:1;width:min(900px,100%);padding:clamp(34px,7vw,92px);color:#fff;margin-inline:auto}
h1{font-family:var(--font-display);font-size:clamp(44px,6.1vw,82px);line-height:.98;margin:12px 0 20px;letter-spacing:0;overflow-wrap:anywhere}
h2{font-family:var(--font-display);font-size:clamp(34px,4.8vw,64px);line-height:1;margin:0 0 18px;letter-spacing:0;overflow-wrap:anywhere}
.lede{font-size:clamp(18px,2vw,23px);line-height:1.56;max-width:700px;color:var(--hero-soft);margin-inline:auto}
.rating-line{display:inline-flex;align-items:center;gap:10px;color:#ffd36b;font-weight:900;margin:0 0 12px}.rating-line span{color:#fff;font-size:14px;letter-spacing:.04em;text-transform:uppercase}
.actions{display:flex;gap:14px;flex-wrap:wrap;margin-top:34px;justify-content:center}.hero .button{min-height:62px;padding:0 32px;font-size:18px}.hero .secondary{color:#fff;border-color:rgba(255,255,255,.72);background:rgba(255,255,255,.08)}
.pill{display:inline-flex;align-items:center;min-height:28px;border-radius:999px;background:var(--chip);color:var(--muted);font-size:12px;font-weight:900;padding:0 11px;margin:0 0 18px}.pill.light{background:rgba(255,255,255,.16);color:#fff}
.about-section{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,.9fr);gap:clamp(24px,5vw,70px);align-items:center;padding:clamp(58px,8vw,110px) var(--page-x)}
.section-copy>p:not(.pill),.quote-copy p:not(.pill){font-size:19px;line-height:1.72;color:var(--soft);max-width:760px}
.image-frame{overflow:hidden;border-radius:24px;box-shadow:0 24px 70px var(--shadow)}.image-frame img{width:100%;height:100%;min-height:460px;object-fit:cover}
.service-section{background:var(--service-bg);color:#fff;padding:clamp(62px,8vw,116px) var(--page-x)}
.service-section>.section-copy{max-width:1040px}.service-section h2,.service-section p{color:#fff}.service-section .pill{background:rgba(255,255,255,.16);color:#fff}
.service-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:clamp(38px,6vw,92px) clamp(28px,8vw,120px);margin-top:58px}
.service-grid article{position:relative;display:grid;grid-template-columns:62px minmax(0,1fr);column-gap:24px;align-items:start;min-height:220px}.service-grid article>span{display:inline-flex;align-items:center;justify-content:center;width:54px;height:54px;border-radius:18px;background:#fff;color:var(--service-bg);font-weight:900;box-shadow:0 18px 38px rgba(0,0,0,.14)}.service-grid h3{grid-column:2;font-size:clamp(32px,4vw,50px);line-height:1.02;margin:0 0 20px}.service-grid p{grid-column:2;font-size:18px;line-height:1.72;color:#fff;margin:0 0 24px}.service-grid a{grid-column:2;grid-row:auto;justify-self:start;align-self:start;background:#fff;color:var(--service-bg)}
.process-section{padding:clamp(62px,8vw,112px) var(--page-x);background:var(--paper);color:var(--ink)}.process-section h2{color:var(--ink);max-width:900px}.process-section .pill.light{background:var(--chip);color:var(--muted)}
.process-track{position:relative;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:22px;margin-top:46px}.process-track::before{content:"";position:absolute;left:7%;right:7%;top:48px;height:2px;background:linear-gradient(90deg,rgba(164,59,31,.18),rgba(164,59,31,.62),rgba(164,59,31,.18))}.process-card{position:relative;z-index:1;background:var(--panel);border:1px solid var(--line);border-radius:24px;padding:22px;min-height:300px;display:flex;flex-direction:column;gap:14px;color:var(--ink);box-shadow:0 18px 55px var(--shadow);transition:transform .28s ease,background .28s ease,border-color .28s ease}.process-card:hover,.process-card:focus-within{transform:translateY(-6px);border-color:var(--accent)}.process-icon{width:76px;height:76px;border-radius:999px;background:var(--ink);color:var(--paper);display:grid;place-items:center;box-shadow:0 18px 45px var(--shadow)}.process-icon svg{width:42px;height:42px;fill:none;stroke:currentColor;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.process-card span{font-weight:900;color:var(--accent)}.process-card h3{font-size:25px;margin:0;color:var(--ink)}.process-card p{color:var(--soft);line-height:1.62;margin:0}
.quote-section{display:grid;gap:34px;justify-items:center;text-align:center;padding:clamp(62px,8vw,112px) var(--page-x);background:var(--panel)}.quote-copy{max-width:930px}.quote-copy p:not(.pill){margin-inline:auto}.quote-form-wrap{width:min(860px,100%)}.quote-form{padding:clamp(24px,4vw,46px);display:grid;gap:16px;background:var(--paper);border:1px solid var(--line);border-radius:24px;box-shadow:0 24px 70px var(--shadow);text-align:left}.form-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-row.compact{grid-template-columns:repeat(3,minmax(0,1fr))}
label{display:grid;gap:7px;font-weight:900;color:var(--ink)}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);font:inherit;padding:14px 15px;outline:none}textarea{resize:vertical;min-height:130px}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--focus)}.quote-form .button{margin-top:12px}
.testimonial-section{padding:clamp(56px,8vw,100px) var(--page-x);background:var(--paper)}.testimonial-section .section-copy{max-width:880px}.testimonial-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:16px;margin-top:34px}.review-card{height:100%;display:flex;flex-direction:column;justify-content:space-between;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 18px 45px var(--shadow);transition:transform .24s ease,border-color .24s ease}.review-card:hover{transform:translateY(-4px);border-color:var(--accent)}.review-card.span-third{grid-column:span 2}.review-card.span-half{grid-column:span 3}.review-card.span-full{grid-column:1/-1}.review-card p{display:flex;align-items:center;gap:10px;margin:18px 0 0;color:var(--soft);line-height:1.35}.review-avatar{width:38px;height:38px;border-radius:999px;object-fit:cover;flex:0 0 auto}.review-card blockquote{margin:0;color:var(--ink);line-height:1.58}.review-card strong{display:block;color:var(--ink)}.testimonial-actions{display:flex;justify-content:center;margin-top:28px}
.faq-section{padding:clamp(62px,8vw,110px) var(--page-x);background:var(--panel)}details{border-top:1px solid var(--line);padding:24px 0}details:last-child{border-bottom:1px solid var(--line)}summary{cursor:pointer;font-size:23px;font-weight:900;list-style:none;color:var(--ink)}summary::-webkit-details-marker{display:none}details p{max-width:780px;color:var(--soft);line-height:1.66}
.gallery-section{padding:clamp(70px,9vw,132px) var(--page-x);background:var(--paper)}.gallery-heading{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,360px);gap:18px 38px;align-items:end;margin:0 auto 44px;max-width:1180px}.gallery-heading p{grid-column:1/-1;margin:0;color:var(--accent);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.14em}.gallery-heading h2{font-size:clamp(34px,4.6vw,60px);margin:0;max-width:760px}.gallery-heading span{color:var(--soft);font-size:16px;line-height:1.55;justify-self:end;max-width:330px}.masonry{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));grid-auto-rows:92px;grid-auto-flow:dense;gap:7px;max-width:1180px;margin-inline:auto}.masonry img{width:100%;height:100%;object-fit:cover;border-radius:0;box-shadow:none;background:var(--panel);filter:saturate(.92) contrast(1.02);transition:filter .25s ease,transform .25s ease}.masonry img:hover{filter:saturate(1.04) contrast(1.06);transform:scale(1.006)}.masonry .gallery-frame{grid-column:span 4;grid-row:span 3}.masonry .frame-1{grid-column:span 5}.masonry .frame-2{grid-column:span 3}.masonry .frame-3{grid-column:span 4}.masonry .frame-4{grid-column:span 4}.masonry .frame-5{grid-column:span 4}.masonry .frame-6{grid-column:span 4}.masonry .frame-7{grid-column:span 5}.masonry .frame-8{grid-column:span 3}.masonry .frame-9{grid-column:span 4}.masonry .portrait{grid-row:span 4}.masonry .landscape{grid-row:span 3}.gallery-actions{display:flex;justify-content:center;margin-top:34px}
.cta-strip{display:grid;place-items:center;text-align:center;min-height:520px;padding:clamp(62px,8vw,110px) var(--page-x);background:linear-gradient(180deg,rgba(11,42,39,.82),rgba(11,42,39,.82)),url("https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&w=1800&q=84");background-size:cover;background-position:center;color:#fff}.cta-strip>div{display:grid;justify-items:center;gap:24px}.cta-strip h2{max-width:820px;color:#fff}.inverse{background:#fff;color:#0b2a27}
@media(max-width:1120px){.testimonial-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.review-card.span-third,.review-card.span-half{grid-column:span 1}.review-count-3 .review-card,.review-count-6 .review-card,.review-count-9 .review-card{grid-column:span 1}.review-count-3 .review-card:last-child,.review-count-7 .review-card:last-child,.review-count-9 .review-card:last-child{grid-column:1/-1}.process-track{grid-template-columns:repeat(2,minmax(0,1fr))}.process-track::before{display:none}.masonry{grid-auto-rows:78px}}
@media(max-width:980px){.about-section{grid-template-columns:1fr}.service-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:36px}.form-row,.form-row.compact{grid-template-columns:1fr}.cta-strip{align-items:center}.gallery-heading{grid-template-columns:1fr}.gallery-heading span{justify-self:start}.masonry{grid-template-columns:repeat(6,minmax(0,1fr));grid-auto-rows:96px}.masonry .gallery-frame{grid-column:span 3}.masonry .frame-1,.masonry .frame-4,.masonry .frame-7{grid-column:span 6}.masonry .portrait{grid-row:span 4}.masonry .landscape{grid-row:span 3}}
@media(max-width:640px){.hero{min-height:660px}.hero-copy{padding:30px 20px}.actions{align-items:stretch;flex-direction:column}.hero .button{width:100%}.service-grid,.process-track,.testimonial-grid{grid-template-columns:1fr}.review-card.span-third,.review-card.span-half,.review-card.span-full,.review-count-3 .review-card:last-child,.review-count-7 .review-card:last-child,.review-count-9 .review-card:last-child{grid-column:1}.masonry{display:grid;grid-template-columns:1fr;grid-auto-rows:auto;gap:8px}.masonry img{grid-column:auto!important;grid-row:auto!important;aspect-ratio:4/5}.masonry .landscape{aspect-ratio:4/3}.service-grid article{grid-template-columns:1fr}.service-grid h3,.service-grid p,.service-grid a{grid-column:1}.about-section,.quote-section,.service-section,.process-section,.testimonial-section,.faq-section,.gallery-section,.cta-strip{padding-left:20px;padding-right:20px}h1{font-size:44px}}
"""


def _contact_css(d: dict[str, str]) -> str:
    return _base_css() + """
.contact-hero{padding:70px var(--page-x) 24px;max-width:980px}
h1{font-family:var(--font-display);font-size:clamp(46px,7vw,92px);line-height:.94;margin:14px 0 20px;letter-spacing:0;overflow-wrap:anywhere}
.lede{font-size:clamp(18px,2vw,22px);line-height:1.58;color:var(--soft);max-width:760px}
.contact-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,420px);gap:16px;padding:24px var(--page-x) 70px;align-items:start}
.quote-form{padding:clamp(22px,4vw,44px);display:grid;gap:16px;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 18px 55px var(--shadow)}
.form-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-row.compact{grid-template-columns:repeat(3,minmax(0,1fr))}
label{display:grid;gap:7px;font-weight:900;color:var(--ink)}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:9px;background:var(--paper);color:var(--ink);font:inherit;padding:13px 14px;outline:none}textarea{resize:vertical;min-height:140px}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--focus)}
.contact-card{background:var(--ink);color:#fff;padding:18px;border-radius:14px;box-shadow:0 18px 55px var(--shadow);position:sticky;top:104px}
.contact-card a,.contact-card p{display:block;color:inherit;text-decoration:none;border-bottom:1px solid rgba(255,255,255,.18);padding:22px 10px;margin:0}
.contact-card span{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:rgba(255,255,255,.6);margin-bottom:7px;font-weight:900}.contact-card strong{font-size:20px;overflow-wrap:anywhere}
.map-frame{padding:0 var(--page-x) 80px}.map-frame iframe{width:100%;height:380px;border:1px solid var(--line);border-radius:14px;filter:saturate(.92) contrast(1.02)}
@media(max-width:900px){.contact-layout{grid-template-columns:1fr}.contact-card{position:relative;top:auto}.form-row,.form-row.compact{grid-template-columns:1fr}.map-frame iframe{height:320px}}
"""


def _services_css(d: dict[str, str]) -> str:
    return _base_css() + """
.services-hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,.85fr);gap:clamp(24px,5vw,58px);align-items:center;padding:64px var(--page-x) 44px}
.services-hero img{width:100%;aspect-ratio:5/4;object-fit:cover;border-radius:14px;box-shadow:0 24px 70px var(--shadow)}
h1{font-family:var(--font-display);font-size:clamp(46px,7vw,92px);line-height:.94;margin:14px 0 20px;letter-spacing:0;overflow-wrap:anywhere}.lede{font-size:clamp(18px,2vw,22px);line-height:1.58;color:var(--soft);max-width:720px;margin-bottom:28px}
.service-list{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:14px var(--page-x)}
.service-list article{min-height:300px;padding:28px;border-radius:14px;background:var(--panel);border:1px solid var(--line);box-shadow:0 18px 55px var(--shadow);display:flex;flex-direction:column;justify-content:space-between}.service-list span{color:var(--accent);font-weight:900}.service-list h2{font-size:clamp(25px,3vw,34px);margin:18px 0}.service-list p{color:var(--soft);line-height:1.58}
.detail-band{display:grid;grid-template-columns:minmax(280px,.82fr) minmax(0,1.18fr);gap:14px;align-items:stretch;margin:14px var(--page-x)}
.detail-band>div{border-radius:14px;background:var(--panel);border:1px solid var(--line);box-shadow:0 18px 55px var(--shadow);padding:clamp(26px,5vw,58px)}.detail-band .image-frame{padding:0;overflow:hidden}.image-frame img{width:100%;height:100%;min-height:460px;object-fit:cover}
.detail-band h2,.faq-section h2,.cta-strip h2{font-size:clamp(32px,4.2vw,58px);line-height:1;margin:10px 0 24px;font-family:var(--font-display)}
.checklist-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-top:26px}.checklist-grid article{border-top:2px solid var(--ink);padding-top:16px}.checklist-grid h3{font-size:24px;margin:0 0 10px}
ul{display:grid;gap:14px;margin:24px 0 0;padding-left:22px;color:var(--soft);font-size:18px;line-height:1.5}.checklist-grid ul{margin-top:0;font-size:16px}
.faq-section{margin:14px var(--page-x);padding:clamp(28px,5vw,58px);border-radius:14px;background:var(--panel);border:1px solid var(--line);box-shadow:0 18px 55px var(--shadow)}
details{border-top:1px solid var(--line);padding:22px 0}details:last-child{border-bottom:1px solid var(--line)}summary{cursor:pointer;font-size:22px;font-weight:900;list-style:none}summary::-webkit-details-marker{display:none}details p{max-width:760px;color:var(--soft);line-height:1.58}
.cta-strip{display:flex;justify-content:space-between;gap:28px;align-items:center;margin:14px var(--page-x);padding:clamp(28px,5vw,58px);border-radius:14px;background:var(--service-bg);color:#fff}.cta-strip h2{color:#fff}
@media(max-width:980px){.services-hero,.detail-band{grid-template-columns:1fr}.service-list{grid-template-columns:repeat(2,minmax(0,1fr))}.cta-strip{align-items:flex-start;flex-direction:column}h1{font-size:52px}}
@media(max-width:620px){.service-list,.checklist-grid{grid-template-columns:1fr}.service-list{margin-left:10px;margin-right:10px}.detail-band,.faq-section,.cta-strip{margin-left:10px;margin-right:10px}.service-list article{min-height:230px}}
"""


def _base_css() -> str:
    return """/* Hallmark: pre-emit critique P4 H4 E4 S4 R4 V4; cleaning template, image-led panels, restrained motion. */
*{box-sizing:border-box}html{color-scheme:light dark;scroll-behavior:smooth;overflow-x:clip}body{margin:0;font-family:var(--font-body);background:var(--paper);color:var(--ink);text-rendering:optimizeLegibility;overflow-x:clip}:root{--paper:#f5efe6;--panel:#fffaf3;--ink:#0b2a27;--soft:#4b5a56;--muted:#5d5047;--line:#d9cab9;--chip:#eadfce;--accent:#a43b1f;--service-bg:#8d004d;--focus:rgba(164,59,31,.25);--hero-soft:rgba(255,255,255,.88);--shadow:rgba(35,26,18,.14);--page-x:clamp(20px,6vw,84px);--font-display:Georgia,"Times New Roman",serif;--font-body:"Trebuchet MS",Verdana,sans-serif}@media(prefers-color-scheme:dark){:root{--paper:#101715;--panel:#18231f;--ink:#f6efe5;--soft:#c7d1ca;--muted:#cabbac;--line:#384a43;--chip:#27352f;--accent:#ffb088;--service-bg:#7b0044;--focus:rgba(255,176,136,.3);--shadow:rgba(0,0,0,.34)}}img{display:block;max-width:100%}.skip-link{position:absolute;left:16px;top:10px;z-index:50;transform:translateY(-160%);background:var(--ink);color:var(--paper);padding:10px 14px;border-radius:999px;text-decoration:none;font-weight:900}.skip-link:focus{transform:none}.site-header{min-height:86px;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px var(--page-x);background:var(--paper);position:sticky;top:0;z-index:10}.brand{display:inline-flex;align-items:center;justify-content:center;min-height:48px;color:var(--ink);font-family:var(--font-display);font-size:clamp(24px,2.8vw,34px);font-weight:900;text-decoration:none;max-width:46vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.01em;line-height:.9}.brand::after{content:"";width:34px;height:3px;border-radius:999px;background:var(--accent);margin-left:10px;align-self:flex-end;margin-bottom:7px}nav{display:flex;gap:8px;align-items:center}nav a{color:var(--soft);text-decoration:none;font-weight:900;padding:10px 12px;border-radius:999px}nav a[aria-current="page"],nav a:hover{background:var(--panel);color:var(--ink)}.button{display:inline-flex;align-items:center;justify-content:center;min-height:54px;padding:0 24px;border-radius:999px;text-decoration:none;font:inherit;font-weight:900;border:0;cursor:pointer;white-space:nowrap}.primary{background:var(--ink);color:var(--paper)}.secondary{border:1px solid currentColor;color:var(--ink);background:transparent}.primary:hover,.secondary:hover{transform:translateY(-1px)}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}.form-status{min-height:24px;margin:0;color:var(--soft);line-height:1.4}.site-footer{display:grid;grid-template-columns:minmax(0,1.2fr) repeat(2,minmax(180px,.5fr));gap:34px;padding:44px var(--page-x);background:#050607;color:#fff}.site-footer a,.site-footer p{color:rgba(255,255,255,.82);text-decoration:none;line-height:1.65}.site-footer h2{font:inherit;font-weight:900;font-size:14px;margin:0 0 14px;color:#fff}.site-footer div:not(:first-child){display:grid;align-content:start;gap:8px}.footer-brand{display:inline-block;font-family:var(--font-display);font-weight:900;font-size:24px;color:#fff!important;margin-bottom:12px}.footer-brand::after{content:"";display:block;width:34px;height:3px;background:var(--accent);border-radius:999px;margin-top:7px}@media(max-width:760px){.site-header{align-items:flex-start;flex-direction:column}.brand{max-width:100%}nav{width:100%;overflow-x:auto;padding-bottom:2px}nav a{flex:0 0 auto}.site-footer{grid-template-columns:1fr;padding:30px 20px}}@media(prefers-reduced-motion:no-preference){body{animation:pageIn .45s ease both}.reveal{opacity:0;transform:translateY(18px);transition:opacity .58s ease,transform .58s ease}.reveal.is-visible{opacity:1;transform:none}@keyframes pageIn{from{opacity:0}to{opacity:1}}}
"""


def _site_js(d: dict[str, str]) -> str:
    return """const current = document.body.dataset.page || "landing";
for (const link of document.querySelectorAll("nav a")) {
  const href = link.getAttribute("href") || "";
  if ((current === "landing" && href === "index.html") || href.startsWith(current)) {
    link.setAttribute("aria-current", "page");
  }
}

const revealItems = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    }
  }, { threshold: 0.16 });
  for (const item of revealItems) observer.observe(item);
} else {
  for (const item of revealItems) item.classList.add("is-visible");
}

const form = document.querySelector("[data-contact-form]");
if (form) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const status = form.querySelector("[data-form-status]");
    const email = form.dataset.email || "";
    if (!email) {
      if (status) status.textContent = "Please use the contact details on this page to send your request.";
      return;
    }
    const fields = new FormData(form);
    const subject = `Service request from ${fields.get("name") || "website visitor"}`;
    const body = [
      `Name: ${fields.get("name") || ""}`,
      `Email: ${fields.get("email") || ""}`,
      `Phone: ${fields.get("phone") || ""}`,
      `Property type: ${fields.get("property_type") || ""}`,
      `Bedrooms: ${fields.get("bedrooms") || ""}`,
      `Bathrooms: ${fields.get("bathrooms") || ""}`,
      `Square footage: ${fields.get("square_footage") || ""}`,
      `Frequency: ${fields.get("frequency") || ""}`,
      `Service type: ${fields.get("service_type") || ""}`,
      `Preferred date: ${fields.get("preferred_date") || ""}`,
      `Address/area: ${fields.get("address") || ""}`,
      "",
      String(fields.get("message") || "")
    ].join("\\n");
    window.location.href = `mailto:${email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    if (status) status.textContent = "Opening your email app with the request details.";
  });
}
"""


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:70] or "business"


def _unique_slug(base: str, existing: set[str], fallback_suffix: str) -> str:
    if base not in existing:
        return base
    fallback = _slugify(f"{base}-{fallback_suffix}")
    if fallback not in existing:
        return fallback
    index = 2
    while f"{fallback}-{index}" in existing:
        index += 1
    return f"{fallback}-{index}"


def _existing_directory_names(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def _title_case_service(niche: str) -> str:
    words = [word for word in re.split(r"\s+", niche.strip()) if word]
    return " ".join(word.capitalize() for word in words) if words else "Local Service"


def _is_cleaning_niche(niche: str) -> bool:
    normalized = niche.lower()
    return any(token in normalized for token in ("clean", "maid", "janitor", "housekeep"))


def generated_website_to_dict(item: GeneratedWebsite) -> dict[str, Any]:
    return {
        "lead_id": item.lead_id,
        "business_name": item.business_name,
        "slug": item.slug,
        "path": item.path,
        "url": item.url,
        "files": item.files,
        "skipped": item.skipped,
        "reason": item.reason,
    }


def metadata_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)
