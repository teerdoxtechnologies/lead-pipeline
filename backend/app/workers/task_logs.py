"""
Per-task log tail for live run visibility.

A logging handler buffers recent log lines for the currently running Celery
task and flushes them (throttled) to Redis under ``lp:joblogs:{task_id}``
(7-day TTL). ``GET /jobs/{id}/status`` reads the tail back, so the frontend
Jobs sections render live logs with the polling they already do. The tail
survives task completion, so jobs that finished while away still show
their final lines.

Best-effort observability: every Redis/logging failure is swallowed so a
broken tail can never break a task. Wired via Celery ``include`` (imported
at worker startup); the API process only uses ``read_job_log_tail``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from celery import signals

LOG_TAIL_KEY_PREFIX = "lp:joblogs:"
LOG_TAIL_MAX_LINES = 100
LOG_TAIL_LINE_MAX_CHARS = 500
LOG_TAIL_TTL_SECONDS = 7 * 24 * 60 * 60
FLUSH_EVERY_LINES = 20
FLUSH_EVERY_SECONDS = 2.0

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = threading.local()
_buffers: Dict[str, Dict[str, Any]] = {}
_redis_client: Optional[Any] = None


def _get_redis() -> Optional[Any]:
    """Shared Redis client, or None when unavailable (tail stays dark)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis

        from app.config import get_settings

        _redis_client = redis.Redis.from_url(
            get_settings().redis_url, decode_responses=True
        )
    except Exception:
        return None
    return _redis_client


def _format_line(record: logging.LogRecord) -> str:
    try:
        message = record.getMessage()
    except Exception:
        return ""
    timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
    short_name = str(record.name).split(".")[-1]
    return f"{timestamp} {record.levelname} {short_name}: {message}"[
        :LOG_TAIL_LINE_MAX_CHARS
    ]


def _append(task_id: str, line: str) -> None:
    if not task_id or not line:
        return
    flush_now = False
    with _lock:
        buf = _buffers.setdefault(
            task_id, {"lines": [], "since_flush": 0, "last_flush": time.monotonic()}
        )
        buf["lines"].append(line)
        del buf["lines"][: -LOG_TAIL_MAX_LINES]
        buf["since_flush"] += 1
        if buf["since_flush"] >= FLUSH_EVERY_LINES or (
            time.monotonic() - buf["last_flush"]
        ) >= FLUSH_EVERY_SECONDS:
            flush_now = True
    if flush_now:
        _flush(task_id)


def _flush(task_id: str) -> None:
    with _lock:
        buf = _buffers.get(task_id)
        if not buf or not buf["since_flush"]:
            return
        payload = json.dumps(buf["lines"][-LOG_TAIL_MAX_LINES:])
        buf["since_flush"] = 0
        buf["last_flush"] = time.monotonic()
    try:
        client = _get_redis()
        if client is None:
            return
        client.setex(LOG_TAIL_KEY_PREFIX + task_id, LOG_TAIL_TTL_SECONDS, payload)
    except Exception:
        pass


class TaskLogTailHandler(logging.Handler):
    """Buffer log records for the active Celery task. Never raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            task_id = getattr(_state, "stack", None)
            current = task_id[-1] if task_id else None
            if not current:
                return
            _append(current, _format_line(record))
        except Exception:
            pass


def _stack() -> List[str]:
    stack = getattr(_state, "stack", None)
    if stack is None:
        stack = []
        _state.stack = stack
    return stack


@signals.task_prerun.connect
def _on_task_prerun(task_id=None, **kwargs) -> None:
    if task_id:
        _stack().append(task_id)
        with _lock:
            _buffers.setdefault(
                task_id, {"lines": [], "since_flush": 0, "last_flush": time.monotonic()}
            )


@signals.task_postrun.connect
def _on_task_postrun(task_id=None, **kwargs) -> None:
    try:
        if task_id:
            _flush(task_id)
    finally:
        with _lock:
            _buffers.pop(task_id, None)
        try:
            _stack().remove(task_id)
        except (ValueError, AttributeError):
            pass


def install() -> None:
    """Attach the tail handler to the root logger (idempotent)."""
    root = logging.getLogger()
    if not any(isinstance(h, TaskLogTailHandler) for h in root.handlers):
        root.addHandler(TaskLogTailHandler())


@signals.after_setup_logger.connect
def _on_logging_setup(**kwargs) -> None:
    """Re-attach after Celery configures logging.

    Celery's root-logger hijack replaces root handlers at worker startup,
    which wipes the handler attached at import. This hook runs after that
    setup, so the tail handler survives in the worker process.
    """
    install()


def read_job_log_tail(job_id: str, limit: int = 100) -> List[str]:
    """Latest buffered log lines for a job, newest last. Never raises."""
    if not job_id:
        return []
    try:
        client = _get_redis()
        if client is None:
            return []
        raw = client.get(LOG_TAIL_KEY_PREFIX + job_id)
    except Exception:
        return []
    if not raw:
        return []
    try:
        lines = json.loads(raw)
    except Exception:
        return []
    if not isinstance(lines, list):
        return []
    return [str(line) for line in lines][-limit:]


install()
