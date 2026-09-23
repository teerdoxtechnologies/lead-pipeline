"""
FastAPI application entry point.

Responsibilities:
- Create and configure the FastAPI application instance
- Register CORS middleware
- Mount all API routers
- Configure structured JSON logging
- Initialise Firebase on startup
"""
from __future__ import annotations

import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pythonjsonlogger import jsonlogger

from app.config import get_settings
from app.firebase import get_db  # triggers Firebase init on first call
from app.routes import analyze, audits, config, email, export, leads, scrape, websites
from app.services.static_website_generator import websites_root

# ---------------------------------------------------------------------------
# Logging setup — JSON structured logs to stdout
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    # Replace any default handlers
    root_logger.handlers = [handler]


_configure_logging()

logger = logging.getLogger(__name__)
LOCAL_API_URL = "http://127.0.0.1:8000"

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

settings = get_settings()

app = FastAPI(
    title="Agency Scraper API",
    description=(
        "Lead generation and website audit service. "
        "Scrapes Google Maps, audits websites with deterministic browser checks, "
        "and pushes cold email drafts to Gmail."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(scrape.router, prefix="/api", tags=["Campaigns"])
app.include_router(leads.router, prefix="/api", tags=["Leads"])
app.include_router(audits.router, prefix="/api", tags=["Website Audits"])
app.include_router(analyze.router, prefix="/api", tags=["Reports"])
app.include_router(analyze.public_router, tags=["Reports"])
app.include_router(email.router, prefix="/api", tags=["Email Drafts"])
app.include_router(export.router, prefix="/api", tags=["Exports"])
app.include_router(config.router, prefix="/api", tags=["Config"])
app.include_router(websites.router, prefix="/api", tags=["Generated Websites"])

generated_sites_root = websites_root()
generated_sites_root.mkdir(parents=True, exist_ok=True)
app.mount(
    "/preview",
    StaticFiles(directory=str(generated_sites_root), html=True),
    name="preview-sites",
)


@app.middleware("http")
async def log_request_lifecycle(request: Request, call_next) -> Response:
    """Log every request boundary without logging bodies or secrets."""
    started = time.perf_counter()
    logger.info(
        "Request started: %s %s",
        request.method,
        request.url.path,
        extra={
            "event": "request_started",
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else None,
        },
    )
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "Request failed: %s %s after %sms",
            request.method,
            request.url.path,
            duration_ms,
            extra={
                "event": "request_failed",
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "Request completed: %s %s -> %s in %sms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        extra={
            "event": "request_completed",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response

# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    """Eagerly initialise Firebase so the first request isn't slow."""
    try:
        get_db()
        logger.info("Startup: Firebase connection verified.")
    except Exception as exc:
        logger.error("Startup: Firebase initialisation failed — %s", exc)
    logger.info(
        "API ready. Open docs at %s/docs",
        LOCAL_API_URL,
        extra={
            "event": "api_ready",
            "api_url": LOCAL_API_URL,
            "docs_url": f"{LOCAL_API_URL}/docs",
        },
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Health"])
async def health_check():
    """Simple liveness probe."""
    return {"status": "ok", "service": "agency-scraper"}
