"""
Celery application configuration.

Broker and result backend: Redis (local, no Docker).
All tasks are defined in app.workers.tasks.
"""
from __future__ import annotations

from celery import Celery

from app.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()

    app = Celery(
        "agency_scraper",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=["app.workers.tasks"],
    )

    app.conf.update(
        # Serialisation
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Timezone
        timezone="UTC",
        enable_utc=True,
        # Task execution
        task_track_started=True,
        task_acks_late=True,          # Ack only after task completes — safer for retries
        worker_prefetch_multiplier=1, # One task at a time per worker slot
        # Retry policy defaults
        task_max_retries=3,
        task_default_retry_delay=60,  # seconds
        # Results expire after 7 days
        result_expires=604_800,
        # Always use eager mode in testing (overridden by env)
        task_always_eager=False,
        # Route all tasks to the default queue
        task_default_queue="default",
        task_queues={
            "default": {
                "exchange": "default",
                "routing_key": "default",
            }
        },
        # Periodic tasks (run by `celery beat`, see scripts/start-worker.sh)
        beat_schedule={
            "reconcile-deleting-campaigns": {
                "task": "tasks.reconcile_deleting_campaigns",
                "schedule": 300.0,  # seconds
            },
        },
    )

    return app


celery_app = create_celery_app()
