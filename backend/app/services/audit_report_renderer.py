"""Customer-facing HTML renderer for deterministic website audit reports.

The renderer fills a fixed report template from stored audit data. It does not
call an AI model and does not decide whether an issue is real; that work happens
upstream in the audit/appraisal layer.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


DEFAULT_CALENDLY_URL = "https://calendly.com/teerdoxtechnologies"
DEFAULT_AGENCY_URL = "https://www.teerdox.com/"


def render_audit_report_html(
    report: dict[str, Any],
    lead: dict[str, Any] | None = None,
    *,
    calendly_url: str = DEFAULT_CALENDLY_URL,
    agency_url: str = DEFAULT_AGENCY_URL,
) -> str:
    """Render the public audit report page for a business."""
    lead = lead or {}
    business_name = _text(lead.get("business_name") or report.get("business_name") or "your business")
    source_website = _text(report.get("source_website") or lead.get("website"))
    reviewed_on = _date_label(report.get("created_at") or report.get("updated_at"))
    findings = _findings(report)
    top_findings = findings[:2]
    metrics = _metrics(report)
    browser_quality = report.get("browser_quality") if isinstance(report.get("browser_quality"), dict) else {}
    mobile_metrics = browser_quality.get("mobile") if isinstance(browser_quality.get("mobile"), dict) else {}
    desktop_metrics = browser_quality.get("desktop") if isinstance(browser_quality.get("desktop"), dict) else {}
    strengths = _strengths(report)
    proof_items = _proof_items(report, findings, mobile_metrics)
    evidence_metrics = _evidence_metrics(report, mobile_metrics, desktop_metrics, findings)

    title = f"Website Review for {business_name}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --color-ink: oklch(22% 0.034 151);
      --color-muted: oklch(43% 0.032 151);
      --color-paper: oklch(97% 0.018 118);
      --color-paper-warm: oklch(93% 0.028 103);
      --color-panel: oklch(99% 0.012 118);
      --color-panel-deep: oklch(27% 0.052 151);
      --color-panel-deep-ink: oklch(96% 0.018 118);
      --color-rule: oklch(82% 0.025 120);
      --color-accent: oklch(48% 0.135 151);
      --color-accent-ink: oklch(98% 0.012 118);
      --color-focus: oklch(46% 0.15 151);
      --font-display: Georgia, "Times New Roman", serif;
      --font-body: Aptos, "Segoe UI", Tahoma, sans-serif;
      --font-mono: "Cascadia Mono", Consolas, monospace;
      --space-2xs: 0.5rem;
      --space-xs: 0.75rem;
      --space-sm: 1rem;
      --space-md: 1.5rem;
      --space-lg: 2rem;
      --space-xl: 3rem;
      --space-2xl: 4rem;
      --radius-md: 0.875rem;
      --radius-lg: 1.25rem;
      --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
      --dur-fast: 140ms;
      --shadow-soft: 0 1.5rem 4rem color-mix(in oklch, var(--color-ink) 10%, transparent);
    }}

    html,
    body {{
      margin: 0;
      overflow-x: clip;
      color: var(--color-ink);
      background: var(--color-paper);
      font-family: var(--font-body);
      line-height: 1.55;
    }}

    * {{
      box-sizing: border-box;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(90deg, color-mix(in oklch, var(--color-rule) 38%, transparent) 1px, transparent 1px),
        linear-gradient(180deg, color-mix(in oklch, var(--color-rule) 32%, transparent) 1px, transparent 1px);
      background-size: 4rem 4rem;
      opacity: 0.2;
      mask-image: linear-gradient(180deg, black, transparent 72%);
    }}

    a {{
      color: var(--color-accent);
      text-underline-offset: 0.18em;
    }}

    :focus-visible {{
      outline: 3px solid var(--color-focus);
      outline-offset: 3px;
    }}

    .skip-link {{
      position: absolute;
      top: -5rem;
      left: var(--space-sm);
      z-index: 10;
      border-radius: 999rem;
      padding: var(--space-xs) var(--space-sm);
      color: var(--color-accent-ink);
      background: var(--color-accent);
      font-weight: 900;
    }}

    .skip-link:focus {{
      top: var(--space-sm);
    }}

    .page {{
      position: relative;
      width: min(74rem, calc(100% - 2rem));
      margin-inline: auto;
      padding-block: 0 var(--space-2xl);
    }}

    .button-link {{
      min-height: 2.9rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 0;
      border-radius: 999rem;
      padding-inline: var(--space-md);
      color: var(--color-accent-ink);
      background: var(--color-accent);
      font: 900 0.95rem/1 var(--font-body);
      text-decoration: none;
      white-space: nowrap;
      cursor: pointer;
      transition:
        transform var(--dur-fast) var(--ease-out),
        background-color var(--dur-fast) var(--ease-out);
    }}

    .button-link:hover {{
      background: color-mix(in oklch, var(--color-accent) 88%, var(--color-ink));
      transform: translateY(-1px);
    }}

    .button-link:active {{
      transform: translateY(0);
    }}

    .button-link.secondary {{
      border: 1px solid color-mix(in oklch, var(--color-panel-deep-ink) 58%, transparent);
      color: var(--color-panel-deep-ink);
      background: transparent;
    }}

    .button-link.secondary:hover {{
      color: var(--color-ink);
      background: var(--color-panel-deep-ink);
    }}

    .hero {{
      margin-top: 0;
      padding-block: clamp(2.25rem, 7vw, 5.5rem) var(--space-xl);
      border-block-end: 1px solid var(--color-rule);
    }}

    .kicker {{
      width: fit-content;
      margin: 0 0 var(--space-md);
      color: var(--color-accent);
      font-size: 0.78rem;
      font-weight: 900;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}

    h1,
    h2,
    h3 {{
      min-width: 0;
      overflow-wrap: anywhere;
      font-family: var(--font-display);
      line-height: 1.02;
      letter-spacing: -0.035em;
    }}

    h1 {{
      max-width: 15ch;
      margin: 0;
      font-size: clamp(2.75rem, 6vw, 5.2rem);
    }}

    .lead {{
      max-width: 66ch;
      margin: var(--space-md) 0 0;
      color: var(--color-muted);
      font-size: clamp(1.05rem, 1.5vw, 1.28rem);
    }}

    .proof-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-xs);
      margin-top: var(--space-lg);
      padding: 0;
      list-style: none;
    }}

    .proof-strip li {{
      min-height: 2.75rem;
      display: inline-flex;
      align-items: center;
      gap: var(--space-2xs);
      border: 1px solid var(--color-rule);
      border-radius: 999rem;
      padding-inline: var(--space-sm);
      background: color-mix(in oklch, var(--color-panel) 78%, var(--color-paper-warm));
      color: var(--color-muted);
      font-weight: 800;
    }}

    .proof-strip strong {{
      color: var(--color-ink);
      font-family: var(--font-mono);
      font-size: 0.98rem;
    }}

    section {{
      margin-top: var(--space-xl);
    }}

    .section-head {{
      display: grid;
      grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
      gap: var(--space-lg);
      align-items: end;
      margin-bottom: var(--space-md);
    }}

    .section-head h2:only-child {{
      grid-column: 1 / -1;
    }}

    .section-head.center {{
      grid-template-columns: minmax(0, 1fr);
      justify-items: center;
      text-align: center;
    }}

    h2 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 4rem);
    }}

    .section-head p {{
      max-width: 64ch;
      margin: 0;
      color: var(--color-muted);
    }}

    .grid-two {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: var(--space-sm);
    }}

    .stack {{
      display: grid;
      gap: var(--space-sm);
    }}

    .panel,
    .issue,
    .care-band {{
      min-width: 0;
      border: 1px solid var(--color-rule);
      border-radius: var(--radius-lg);
      background: var(--color-panel);
    }}

    .panel {{
      padding: var(--space-md);
    }}

    .panel h3 {{
      margin: 0 0 var(--space-xs);
      font-size: 1.45rem;
    }}

    .panel p,
    .panel li {{
      color: var(--color-muted);
    }}

    .panel p {{
      margin: 0;
    }}

    .panel ul {{
      margin: var(--space-xs) 0 0;
      padding-inline-start: 1.1rem;
    }}

    .care-band {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-lg);
      align-items: center;
      justify-items: center;
      padding: clamp(1.25rem, 4vw, 2.5rem);
      color: var(--color-panel-deep-ink);
      background:
        linear-gradient(135deg, var(--color-panel-deep), color-mix(in oklch, var(--color-panel-deep) 78%, var(--color-accent)));
      box-shadow: var(--shadow-soft);
      text-align: center;
    }}

    .care-band h2,
    .care-band p {{
      margin: 0;
    }}

    .care-band p {{
      max-width: 66ch;
      color: var(--color-panel-deep-ink);
      font-size: 1.08rem;
    }}

    .issue {{
      display: grid;
      grid-template-columns: minmax(0, 0.32fr) minmax(0, 0.68fr);
      gap: var(--space-md);
      padding: var(--space-md);
    }}

    .issue-number {{
      display: grid;
      align-content: start;
      gap: var(--space-xs);
    }}

    .issue-number span {{
      width: fit-content;
      border-radius: 999rem;
      padding: 0.4rem 0.7rem;
      color: var(--color-accent-ink);
      background: var(--color-accent);
      font-weight: 900;
    }}

    .issue-number b {{
      color: var(--color-muted);
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}

    .issue h3 {{
      margin: 0;
      font-size: clamp(1.6rem, 3vw, 2.7rem);
    }}

    .issue p {{
      margin: var(--space-xs) 0 0;
      color: var(--color-muted);
    }}

    .impact-note {{
      color: var(--color-ink);
      font-weight: 700;
    }}

    .detail-heading {{
      margin-top: var(--space-md);
      color: var(--color-ink);
      font-weight: 900;
    }}

    .detail-list {{
      display: grid;
      gap: var(--space-xs);
      margin: var(--space-xs) 0 0;
      padding-inline-start: 1.1rem;
      color: var(--color-muted);
      overflow-wrap: anywhere;
    }}

    .fix-list {{
      display: grid;
      gap: var(--space-xs);
      margin: var(--space-md) 0 0;
      padding: 0;
      list-style: none;
    }}

    .fix-list li {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: var(--space-xs);
      align-items: start;
      color: var(--color-muted);
    }}

    .fix-list li::before {{
      content: "";
      width: 0.6rem;
      height: 0.6rem;
      margin-top: 0.52rem;
      border-radius: 50%;
      background: var(--color-accent);
    }}

    .metric-board {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: var(--space-sm);
    }}

    .metric {{
      min-width: 0;
      padding: var(--space-md);
      border: 1px solid var(--color-rule);
      border-radius: var(--radius-md);
      background: var(--color-panel);
    }}

    .metric b {{
      display: block;
      margin-bottom: var(--space-xs);
      font-family: var(--font-display);
      font-size: 2.25rem;
      line-height: 1;
      letter-spacing: -0.04em;
    }}

    .metric span {{
      color: var(--color-muted);
      font-size: 0.92rem;
      font-weight: 700;
    }}

    .closing-cta {{
      display: grid;
      gap: var(--space-md);
      justify-items: center;
      padding: clamp(1.5rem, 5vw, 3rem);
      border-radius: var(--radius-lg);
      color: var(--color-panel-deep-ink);
      background: var(--color-panel-deep);
      box-shadow: var(--shadow-soft);
      text-align: center;
    }}

    .closing-cta h2,
    .closing-cta p {{
      margin: 0;
    }}

    .closing-cta p {{
      max-width: 60ch;
      color: var(--color-panel-deep-ink);
      font-size: 1.08rem;
    }}

    .cta-actions {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: var(--space-xs);
    }}

    @media (max-width: 56rem) {{
      .section-head,
      .grid-two,
      .issue,
      .care-band {{
        grid-template-columns: minmax(0, 1fr);
      }}

      .metric-board {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}

    @media (max-width: 30rem) {{
      .page {{
        width: min(100% - 1rem, 74rem);
      }}

      .issue,
      .panel,
      .metric {{
        padding: var(--space-sm);
      }}

      .metric-board {{
        grid-template-columns: minmax(0, 1fr);
      }}

      h1 {{
        font-size: clamp(2.2rem, 12vw, 3rem);
      }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      *,
      *::before,
      *::after {{
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
      }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#main">Skip to report</a>
  <main class="page" id="main">
    <section class="hero" aria-labelledby="report-title">
      <p class="kicker">Reviewed on {escape(reviewed_on)}</p>
      <h1 id="report-title">{escape(_hero_headline(top_findings))}</h1>
      <p class="lead">
        {escape(_hero_lead(business_name, top_findings))}
      </p>
      <ul class="proof-strip" aria-label="Review highlights">
        {_proof_strip_items(proof_items)}
      </ul>
    </section>

    <section aria-labelledby="why-care">
      <div class="care-band">
        <h2 id="why-care">Why this is worth fixing</h2>
        <p>
          {escape(_why_care_copy(top_findings))}
        </p>
      </div>
    </section>

    <section aria-labelledby="what-working">
      <div class="section-head">
        <h2 id="what-working">What your site already does well</h2>
      </div>

      <div class="grid-two">
        {_strength_cards(strengths)}
      </div>
    </section>

    <section aria-labelledby="all-issues">
      <div class="section-head">
        <h2 id="all-issues">All issues found</h2>
      </div>

      <div class="stack">
        {_issue_cards(findings, label="Issue")}
      </div>
    </section>

    <section aria-labelledby="evidence">
      <div class="section-head">
        <h2 id="evidence">Stats from the review</h2>
      </div>

      <div class="metric-board">
        {_metric_cards(evidence_metrics)}
      </div>
    </section>

    <section aria-labelledby="fix-first">
      <div class="section-head">
        <h2 id="fix-first">The two fixes to prioritize</h2>
      </div>

      <div class="stack">
        {_issue_cards(top_findings, label="Priority")}
      </div>
    </section>

    <section class="closing-cta" aria-labelledby="book-review">
      <h2 id="book-review">Want the highest-impact fixes handled for you?</h2>
      <p>
        Better readability, faster first impressions, and clearer contact paths
        can mean fewer visitors dropping off before they call, book, or ask for
        a quote. Let us fix that for you.
      </p>
      <div class="cta-actions">
        {_link_button(calendly_url, "Book a fix call")}
        {_link_button(agency_url, "Visit our website", secondary=True)}
      </div>
    </section>
  </main>
</body>
</html>"""


def _findings(report: dict[str, Any]) -> list[dict[str, str]]:
    raw_findings = report.get("findings")
    if isinstance(raw_findings, list) and raw_findings:
        return [_normalise_finding(item) for item in raw_findings if isinstance(item, dict)]

    weaknesses = report.get("weaknesses") if isinstance(report.get("weaknesses"), list) else []
    recommendations = report.get("recommendations") if isinstance(report.get("recommendations"), list) else []
    findings = []
    for index, weakness in enumerate(weaknesses):
        recommendation = recommendations[index] if index < len(recommendations) else "Fix this section so visitors have a clearer path to the next step."
        findings.append(_normalise_finding({
            "category": "website friction",
            "observation": weakness,
            "business_impact": "This can make the first visit feel less clear or less trustworthy.",
            "recommendation": recommendation,
        }))
    return findings or [_normalise_finding({
        "category": "website review",
        "observation": "No specific issues were stored for this report.",
        "business_impact": "The report can still be used as a baseline for future checks.",
        "recommendation": "Keep the conversion path clear and review the site again after major updates.",
    })]


def _normalise_finding(item: dict[str, Any]) -> dict[str, str]:
    return {
        "category": _text(item.get("category") or "website friction"),
        "observation": _sentence(_text(item.get("observation") or item.get("email_observation"))),
        "business_impact": _sentence(_text(item.get("business_impact")) or "This can make it harder for a visitor to take the next step."),
        "recommendation": _sentence(_text(item.get("recommendation")) or "Tighten this part of the site so the next step is easier to understand and act on."),
        "details": _detail_items(item.get("details")),
    }


def _metrics(report: dict[str, Any]) -> dict[str, int]:
    return {
        "overall": _score(report.get("overall_score")),
        "copy": _score(report.get("copy_score")),
        "seo": _score(report.get("seo_score")),
        "ux": _score(report.get("ux_score")),
    }


def _strengths(report: dict[str, Any]) -> list[tuple[str, str]]:
    strengths = report.get("strengths") if isinstance(report.get("strengths"), list) else []
    if strengths:
        return [
            (_strength_title(strength, index), _text(strength))
            for index, strength in enumerate(strengths[:4], start=1)
        ]

    fallback = [
        ("Owned web presence", "The website gives customers a place to learn about the business outside third-party directories."),
        ("Searchable service page", "The page contains enough content to give visitors and search engines basic context."),
        ("Clearer path available", "The current site can be improved without needing to start from scratch."),
        ("Measurable next step", "The same checks can be repeated after fixes so progress is visible."),
    ]
    return fallback


def _strength_title(strength: Any, index: int) -> str:
    text = _text(strength).lower()
    if "cta" in text or "conversion" in text or "action" in text:
        return "Conversion path exists"
    if "trust" in text or "proof" in text:
        return "Trust signals are present"
    if "seo" in text or "search" in text:
        return "Search basics are in place"
    if "value" in text or "context" in text:
        return "Customer value is visible"
    return f"Positive signal {index}"


def _proof_items(
    report: dict[str, Any],
    findings: list[dict[str, str]],
    mobile_metrics: dict[str, Any],
) -> list[tuple[str, str]]:
    lcp = _ms_label(mobile_metrics.get("lcp_ms"))
    small_targets = _count_label(mobile_metrics.get("small_tap_target_count"))
    low_contrast = _count_label(mobile_metrics.get("low_contrast_text_count"))
    return [
        (f"{_score(report.get('overall_score'))}/10", "overall score"),
        (str(len(findings)), "issues found"),
        (lcp, "mobile main content"),
        (small_targets if small_targets != "n/a" else low_contrast, "mobile usability signals"),
    ]


def _evidence_metrics(
    report: dict[str, Any],
    mobile_metrics: dict[str, Any],
    desktop_metrics: dict[str, Any],
    findings: list[dict[str, str]],
) -> list[tuple[str, str]]:
    metrics = [
        (f"{_score(report.get('overall_score'))}/10", "Overall customer-facing score"),
        (_ms_label(mobile_metrics.get("lcp_ms")), "Mobile main content appeared in this test"),
        (_count_label(mobile_metrics.get("above_fold_cta_count")), "Mobile calls to action visible above the fold"),
        (_count_label(mobile_metrics.get("low_contrast_text_count")), "Low-contrast text elements detected on mobile"),
        (_count_label(mobile_metrics.get("small_tap_target_count")), "Small tap targets detected on mobile"),
        (_number_label(mobile_metrics.get("cls")), "Mobile layout shift score"),
        (_ms_label(desktop_metrics.get("lcp_ms")), "Desktop main content appeared in this test"),
        (_count_label(desktop_metrics.get("image_failures")), "Failed images detected on desktop"),
        (f"{_score(report.get('copy_score'))}/10", "Copy clarity score"),
        (f"{_score(report.get('seo_score'))}/10", "Search basics score"),
        (f"{_score(report.get('ux_score'))}/10", "Experience score"),
        (str(len(findings)), "Issues found in this review"),
    ]
    selected: list[tuple[str, str]] = []
    for value, label in metrics:
        if value == "n/a":
            continue
        if (value, label) in selected:
            continue
        selected.append((value, label))
        if len(selected) == 4:
            return selected
    return selected or [("0", "Stored metrics available")]


def _hero_headline(findings: list[dict[str, str]]) -> str:
    if not findings:
        return "Your website has a few fixable points of friction."
    category = findings[0]["category"].lower()
    if "speed" in category or "load" in category:
        return "Your website can feel faster before visitors decide to leave."
    if "conversion" in category:
        return "Your website can make the next step easier to take."
    if "readability" in category:
        return "Your website can make important proof easier to read."
    if "trust" in category or "visual" in category:
        return "Your website can feel more trustworthy on the first visit."
    return "Your website can make booking feel easier."


def _hero_lead(business_name: str, findings: list[dict[str, str]]) -> str:
    if not findings:
        return (
            f"The audit for {business_name} found a live site with a few areas "
            "that can be tightened so visitors have an easier path to contact the business."
        )
    first = findings[0]
    return (
        f"The audit for {business_name} found a clear website, but the main "
        f"friction is this: {first['observation'].rstrip('.')}. "
        "The issue is not just technical; it can affect how quickly a visitor "
        "trusts the page and decides to get in touch."
    )


def _why_care_copy(findings: list[dict[str, str]]) -> str:
    if not findings:
        return (
            "Customers compare providers quickly. A website that feels clear, "
            "readable, and easy to act on gives them fewer reasons to keep looking."
        )
    impact = findings[0]["business_impact"].rstrip(".")
    return (
        "Your customers are usually moving quickly: compare a few providers, "
        "check proof, then call, book, or ask for a quote. "
        f"{impact}. That matters because your website is often where they decide "
        "whether the business feels careful and easy to work with."
    )


def _proof_strip_items(items: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"        <li><strong>{escape(value)}</strong> {escape(label)}</li>"
        for value, label in items
    )


def _strength_cards(strengths: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"""
        <article class="panel">
          <h3>{escape(title)}</h3>
          <p>{escape(copy)}</p>
        </article>"""
        for title, copy in strengths[:4]
    )


def _issue_cards(findings: list[dict[str, str]], *, label: str) -> str:
    return "\n".join(_issue_card(finding, index + 1, label=label) for index, finding in enumerate(findings))


def _issue_card(finding: dict[str, str], index: int, *, label: str) -> str:
    number = f"{index:02d}"
    heading_label = f"{label} {index}" if label != "Priority" else ("Primary issue" if index == 1 else "Secondary issue")
    return f"""
        <article class="issue">
          <div class="issue-number">
            <span>{escape(number)}</span>
            <b>{escape(heading_label)}</b>
          </div>
          <div>
            <h3>{escape(finding["observation"])}</h3>
            <p>{escape(finding["category"].title())}</p>
            <p class="impact-note">Why it matters: {escape(finding["business_impact"])}</p>
            {_detail_list(finding.get("details"))}
            <ul class="fix-list">
              <li>{escape(finding["recommendation"])}</li>
            </ul>
          </div>
        </article>"""


def _metric_cards(metrics: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"""
        <div class="metric">
          <b>{escape(value)}</b>
          <span>{escape(label)}</span>
        </div>"""
        for value, label in metrics
    )


def _detail_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)][:8]


def _detail_list(details: Any) -> str:
    if not details:
        return ""
    items = "\n".join(f"              <li>{escape(_text(detail))}</li>" for detail in details)
    return f"""
            <p class="detail-heading">Where to look:</p>
            <ul class="detail-list">
{items}
            </ul>"""


def _link_button(url: str, label: str, *, secondary: bool = False) -> str:
    classes = "button-link secondary" if secondary else "button-link"
    href = escape(url or "#", quote=True)
    return f'<a class="{classes}" href="{href}" target="_blank" rel="noreferrer">{escape(label)}</a>'


def _date_label(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%B %d, %Y")
    text = _text(value)
    if not text:
        return "desktop and mobile"
    return text.split("T", 1)[0]


def _ms_label(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if number >= 1000:
        return f"{number / 1000:.1f}s"
    return f"{number:.0f}ms"


def _number_label(value: Any) -> str:
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"


def _count_label(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "n/a"


def _score(value: Any) -> int:
    try:
        return max(0, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _sentence(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _text(value: Any) -> str:
    return str(value or "").strip()
