"""Write static audit report artifacts for hosted client-facing pages."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.audit_report_renderer import render_audit_report_html

logger = logging.getLogger(__name__)


def audit_reports_root() -> Path:
    settings = get_settings()
    configured = Path(settings.audit_reports_root)
    if configured.is_absolute():
        return configured
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / configured).resolve()


def write_audit_report_artifact(report: dict[str, Any], lead: dict[str, Any] | None = None) -> str | None:
    """Render an audit report to ``audit/{slug}/index.html``.

    Firestore remains the source of truth; this static file is the hosted/shareable
    artifact for environments that serve the root-level ``audit`` directory.
    """
    slug = str(report.get("slug") or "").strip()
    if not slug:
        return None

    try:
        report_dir = audit_reports_root() / slug
        report_dir.mkdir(parents=True, exist_ok=True)
        html = render_audit_report_html(report, lead)
        html_path = report_dir / "index.html"
        html_path.write_text(html, encoding="utf-8")
        metadata = {
            "slug": slug,
            "report_id": report.get("id"),
            "lead_id": report.get("lead_id"),
            "source_website": report.get("source_website"),
            "overall_score": report.get("overall_score"),
        }
        (report_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=True, default=str),
            encoding="utf-8",
        )
        logger.info("Wrote audit report artifact for slug %s at %s", slug, html_path)
        return str(html_path)
    except Exception as exc:
        logger.warning("Could not write audit report artifact for slug %s: %s", slug, exc)
        return None
