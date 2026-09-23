"""
Gmail API service.

Responsibilities:
- OAuth2 token management (load from file, refresh automatically)
- Create Gmail DRAFT messages via users.drafts.create
- NEVER calls users.messages.send

Authentication flow:
1. Load credentials from GMAIL_CREDENTIALS_PATH (OAuth2 client JSON from GCP)
2. Load token from GMAIL_TOKEN_PATH if it exists (refresh if expired)
3. If no token: trigger the installed-app OAuth2 flow (browser popup)
   — first run only; token is then saved for subsequent runs.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>()]+")

# Only drafts scope — no send permission
GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]


def get_google_credentials() -> Credentials:
    """
    Load Gmail OAuth2 credentials, refreshing or re-authorising as needed.
    Token is persisted to GMAIL_TOKEN_PATH.
    """
    settings = get_settings()
    creds: Optional[Credentials] = None

    # Load existing token
    if os.path.exists(settings.gmail_token_path):
        creds = Credentials.from_authorized_user_file(
            settings.gmail_token_path, GOOGLE_OAUTH_SCOPES
        )
        if not creds.has_scopes(GOOGLE_OAUTH_SCOPES):
            logger.info("Google token is missing required scopes; re-authorisation is required.")
            creds = None

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            logger.warning("Token refresh failed: %s — will re-authorise.", exc)
            creds = None

    # First-time or re-authorise flow
    if not creds or not creds.valid:
        if not os.path.exists(settings.gmail_credentials_path):
            raise FileNotFoundError(
                f"Gmail credentials file not found at '{settings.gmail_credentials_path}'. "
                "Download OAuth2 client credentials from Google Cloud Console."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            settings.gmail_credentials_path, GOOGLE_OAUTH_SCOPES
        )
        creds = flow.run_local_server(port=0)

        # Persist token for future runs
        with open(settings.gmail_token_path, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("Gmail token saved to %s", settings.gmail_token_path)

    return creds


def _get_gmail_credentials() -> Credentials:
    return get_google_credentials()


def _build_gmail_service():
    """Build and return the Gmail API service client."""
    creds = _get_gmail_credentials()
    return build("gmail", "v1", credentials=creds)


def create_gmail_draft(
    to_email: str,
    subject: str,
    body: str,
) -> Optional[str]:
    """
    Create a Gmail DRAFT (never sends) and return the draft ID.

    ``to_email``  — recipient address
    ``subject``   — email subject line
    ``body``      — plain-text email body

    Returns the Gmail draft ID string, or None if creation failed.
    """
    settings = get_settings()
    sender = settings.gmail_sender_email

    if not sender:
        logger.error("GMAIL_SENDER_EMAIL not configured — cannot create draft.")
        return None

    raw = _build_raw_message(sender=sender, to_email=to_email, subject=subject, body=body)

    try:
        service = _build_gmail_service()
        draft = (
            service.users()  # type: ignore[attr-defined]
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        draft_id = draft.get("id")
        logger.info("Gmail draft created: %s → %s (draft_id=%s)", sender, to_email, draft_id)
        return draft_id

    except HttpError as exc:
        logger.error("Gmail API error creating draft to %s: %s", to_email, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error creating Gmail draft: %s", exc, exc_info=True)
        return None


def update_gmail_draft(
    gmail_draft_id: str,
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    """Replace the contents of an existing Gmail draft. Returns True on success."""
    settings = get_settings()
    sender = settings.gmail_sender_email

    if not sender:
        logger.error("GMAIL_SENDER_EMAIL not configured — cannot update draft.")
        return False
    if not gmail_draft_id:
        logger.error("Missing Gmail draft ID — cannot update draft.")
        return False

    raw = _build_raw_message(sender=sender, to_email=to_email, subject=subject, body=body)

    try:
        service = _build_gmail_service()
        (
            service.users()  # type: ignore[attr-defined]
            .drafts()
            .update(userId="me", id=gmail_draft_id, body={"message": {"raw": raw}})
            .execute()
        )
        logger.info("Gmail draft updated: %s → %s (draft_id=%s)", sender, to_email, gmail_draft_id)
        return True
    except HttpError as exc:
        logger.error("Gmail API error updating draft %s to %s: %s", gmail_draft_id, to_email, exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error updating Gmail draft %s: %s", gmail_draft_id, exc, exc_info=True)
        return False


def _build_raw_message(sender: str, to_email: str, subject: str, body: str) -> str:
    mime_msg = MIMEMultipart("alternative")
    mime_msg["From"] = sender
    mime_msg["To"] = to_email
    mime_msg["Subject"] = subject
    mime_msg.attach(MIMEText(body, "plain"))
    mime_msg.attach(MIMEText(_plain_text_to_html(body), "html"))
    return base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")


def _plain_text_to_html(body: str) -> str:
    """Convert plain text to a compact HTML alternative with clickable links."""
    escaped = html.escape(body or "")

    def linkify(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,);":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trailing}'

    linked = _URL_RE.sub(linkify, escaped)
    return (
        "<html><body>"
        '<div style="white-space: pre-line; margin: 0; font-family: Arial, sans-serif;">'
        f"{linked}"
        "</div>"
        "</body></html>"
    )



def delete_gmail_draft(draft_id: str) -> bool:
    """Delete a Gmail draft by ID. Returns True when Gmail accepts the delete."""
    if not draft_id:
        return False

    try:
        service = _build_gmail_service()
        (
            service.users()  # type: ignore[attr-defined]
            .drafts()
            .delete(userId="me", id=draft_id)
            .execute()
        )
        logger.info("Gmail draft deleted: %s", draft_id)
        return True
    except HttpError as exc:
        logger.error("Gmail API error deleting draft %s: %s", draft_id, exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error deleting Gmail draft: %s", exc, exc_info=True)
        return False
