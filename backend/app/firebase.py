"""
Firebase Admin SDK initialisation and Firestore helper functions.

All database interactions in the rest of the codebase should go through
these helpers to keep Firestore access consistent and testable.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter
from google.cloud.firestore_v1.base_query import BaseQuery

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

_app: Optional[firebase_admin.App] = None
_db: Optional[Any] = None  # google.cloud.firestore.Client


def _init_firebase() -> None:
    """Initialise the Firebase Admin SDK exactly once."""
    global _app, _db
    if _app is not None:
        return

    settings = get_settings()
    cred = credentials.Certificate(settings.firebase_service_account_path)
    _app = firebase_admin.initialize_app(cred)
    _db = firestore.client()
    logger.info("Firebase Admin SDK initialised successfully.")


def get_db() -> Any:
    """Return the Firestore client, initialising Firebase if required."""
    if _db is None:
        _init_firebase()
    return _db


# ---------------------------------------------------------------------------
# Collection names (single source of truth)
# ---------------------------------------------------------------------------

CAMPAIGNS = "campaigns"
LEADS = "leads"
AUDIT_REPORTS = "audit_reports"
NO_WEBSITE_REPORTS = "no_website_reports"
EMAIL_DRAFTS = "email_drafts"
SEARCH_EVIDENCE_CACHE = "search_evidence_cache"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_document(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single document by ID.

    Returns the document data dict with an injected ``id`` key, or ``None``
    if the document does not exist.
    """
    db = get_db()
    doc_ref = db.collection(collection).document(doc_id)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    logger.debug("Document %s/%s does not exist.", collection, doc_id)
    return None


def set_document(
    collection: str, doc_id: str, data: Dict[str, Any]
) -> None:
    """
    Create or fully overwrite a document.
    Automatically injects SERVER_TIMESTAMP for created_at and updated_at if
    not already present.
    """
    db = get_db()
    payload = {
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
        **data,
    }
    db.collection(collection).document(doc_id).set(payload)
    logger.debug("Set document %s/%s", collection, doc_id)


def update_document(
    collection: str, doc_id: str, data: Dict[str, Any]
) -> None:
    """
    Merge-update an existing document.
    Always stamps updated_at with SERVER_TIMESTAMP.
    """
    db = get_db()
    payload = {**data, "updated_at": firestore.SERVER_TIMESTAMP}
    db.collection(collection).document(doc_id).update(payload)
    logger.debug("Updated document %s/%s", collection, doc_id)


def add_document(collection: str, data: Dict[str, Any]) -> str:
    """
    Add a new document with an auto-generated Firestore ID.

    Returns the new document ID.
    """
    db = get_db()
    payload = {
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
        **data,
    }
    _, doc_ref = db.collection(collection).add(payload)
    logger.debug("Added document %s/%s", collection, doc_ref.id)
    return doc_ref.id


def query_collection(
    collection: str,
    filters: Optional[List[Tuple[str, str, Any]]] = None,
    limit: int = 50,
    offset: int = 0,
    order_by: Optional[str] = None,
    direction: str = "ASCENDING",
) -> List[Dict[str, Any]]:
    """
    Query a Firestore collection with optional equality / comparison filters,
    ordering, and pagination via limit + offset.

    ``filters`` is a list of ``(field, operator, value)`` tuples, e.g.:
        [("campaign_id", "==", "abc"), ("has_website", "==", True)]

    Supported operators: ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``,
    ``in``, ``not-in``, ``array-contains``.
    """
    db = get_db()
    query: BaseQuery = db.collection(collection)

    if filters:
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))

    if order_by:
        dir_enum = (
            firestore.Query.ASCENDING
            if direction.upper() == "ASCENDING"
            else firestore.Query.DESCENDING
        )
        query = query.order_by(order_by, direction=dir_enum)

    if offset:
        query = query.offset(offset)

    query = query.limit(limit)

    results = []
    for doc in query.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        results.append(item)
    return results


def count_collection(
    collection: str,
    filters: Optional[List[Tuple[str, str, Any]]] = None,
) -> int:
    """
    COUNT aggregation: one round-trip, no document reads. Same filter
    tuples as :func:`query_collection`. Use for totals; fetch documents
    only when their contents are actually needed.
    """
    db = get_db()
    query: BaseQuery = db.collection(collection)
    if filters:
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
    total = 0
    for result in query.count(alias="total").get():
        value = getattr(result, "value", None)
        if value is None:
            try:
                value = result[0].value
            except Exception:
                value = 0
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total

    return results


def delete_document(collection: str, doc_id: str) -> None:
    """Hard-delete a document."""
    db = get_db()
    db.collection(collection).document(doc_id).delete()
    logger.debug("Deleted document %s/%s", collection, doc_id)
