"""Helpers for preserving outreach message history on a single lifecycle row."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def append_message_history(
    notes: Optional[str],
    *,
    stage: Optional[str],
    status: str,
    subject: Optional[str],
    body: Optional[str],
    at: Optional[str] = None,
) -> str:
    """Upsert a subject/body snapshot into the outreach notes history."""
    subject_text = str(subject or "").strip()
    body_text = str(body or "").strip()
    if not subject_text and not body_text:
        return str(notes or "").strip()

    timestamp = at or datetime.now(timezone.utc).isoformat()
    label = _stage_label(stage)
    status_label = _status_label(status)
    header = f"--- {label} {status_label} ---"
    entry = "\n".join(
        item
        for item in [
            header,
            f"At: {timestamp}",
            f"Subject: {subject_text}" if subject_text else "",
            "Body:",
            body_text,
        ]
        if item
    ).strip()

    slot = _history_slot(stage, status)
    sections = _split_history_sections(str(notes or "").strip())
    updated: list[str] = []
    replaced = False
    for section in sections:
        if _history_slot_from_section(section) == slot:
            if not replaced:
                updated.append(entry)
                replaced = True
            continue
        updated.append(section.strip())
    if not replaced:
        updated.append(entry)
    return "\n\n".join(part for part in updated if part).strip()


def _split_history_sections(notes: str) -> list[str]:
    if not notes:
        return []
    lines = notes.splitlines()
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("--- ") and line.endswith(" ---"):
            if current:
                sections.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append(current)
    return ["\n".join(section).strip() for section in sections if any(item.strip() for item in section)]


def _history_slot(stage: Optional[str], status: str) -> str:
    return _stage_label(stage)


def _history_slot_from_section(section: str) -> Optional[str]:
    first_line = section.splitlines()[0].strip() if section.strip() else ""
    if not (first_line.startswith("--- ") and first_line.endswith(" ---")):
        return None
    core = first_line[4:-4].strip()
    for suffix in (" Regenerated Draft", " Sent", " Draft"):
        if core.endswith(suffix):
            return core[: -len(suffix)]
    return None


def _stage_label(stage: Optional[str]) -> str:
    value = str(stage or "initial").strip()
    if value == "initial":
        return "Initial Email"
    if value.startswith("follow_up_"):
        suffix = value.removeprefix("follow_up_").replace("_", " ")
        return f"Follow Up {suffix}".title()
    return value.replace("_", " ").title()


def _status_bucket(status: str) -> str:
    value = str(status or "drafted").strip().lower()
    return "sent" if value == "sent" else "draft"


def _status_label(status: str) -> str:
    return "Sent" if _status_bucket(status) == "sent" else "Draft"
